"""Motor de Sofía sobre OpenAI (gpt-4o-mini) — GEMELO del motor de Anthropic.

Réplica fiel de `procesar_turno_agente` (app/core/agente.py) pero usando la API de
OpenAI con function-calling. REUSA todo lo del motor original: system prompt, base
de conocimiento, tools reales, formato WhatsApp, persistencia y métricas. NO toca
agente.py — la Sofía de Sonnet queda intacta.

Se selecciona con SOFIA_ENGINE=openai (ver config). Corre como servicio aparte
(`sofia-gpt`), en paralelo a la Sofía de Sonnet.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from app.config import get_settings
from app.core.agente import (
    HISTORIAL_LIMIT,
    MAX_TOOL_ITERS,
    TOOLS_SPEC,
    AgenteResult,
    _a_formato_whatsapp,
    _anexar_mensaje_valor,
    _build_system_blocks,
    _ejecutar_tool,
)
from app.core.repository import get_repository
from app.core.state import Canal, EstadoConversacion
from app.observability.costs import calculate_cost

log = logging.getLogger(__name__)

# Tools en formato OpenAI (derivadas de las mismas specs de Anthropic).
OPENAI_TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": t["name"],
            "description": t["description"],
            "parameters": t["input_schema"],
        },
    }
    for t in TOOLS_SPEC
]


# Afinado SOLO para el motor OpenAI (gpt-4o-mini sigue el prompt más al pie de la
# letra; estas reglas corrigen lo que se observó en pruebas). NO afecta a Sonnet.
_OPENAI_TUNING = """
# INSTRUCCIONES CRÍTICAS DE OPERACIÓN (síguelas al pie de la letra)
- CONTESTA PRIMERO, EMPUJA DESPUÉS. Si el papá hace una pregunta concreta (precio,
  nivel, inglés, horario, becas), RESPÓNDELA de una vez con el dato real de la base
  de conocimiento o las tools. NUNCA respondas solo con otra pregunta ("¿en qué
  grado?") cuando ya puedes dar la respuesta. Ej.: si preguntan el precio de
  primaria, DA los dos rangos ($6,100 de 1° a 3°, $6,300 de 4° a 6°) y, si hace
  falta, luego preguntas el grado.
- NUNCA inventes días ni horarios. Antes de ofrecer o confirmar CUALQUIER día u
  hora, llama SIEMPRE la tool `dias_disponibles_visita` y ofrece EXACTAMENTE lo que
  devuelva (día + sus horarios juntos, en un solo mensaje). Si no llamaste la tool,
  no menciones ningún día ni hora.
- Mapea la edad al nivel correcto con la KB (p. ej. 3 años = 1° de Kinder; 6 años =
  1° de Primaria). No lo dejes vago.
- Para agendar, llama `agendar_visita` solo cuando tengas los datos. El WhatsApp del
  papá se captura AUTOMÁTICAMENTE por el sistema: NO lo pidas y NUNCA pongas el
  correo en el campo de teléfono. Pide solo lo que falte (nombre del papá/mamá,
  correo, y nombre y edad del hijo).
- Si el papá dice que NINGUNA de las opciones de horario le sirve, NO insistas: dile
  que Lily lo contactará directamente para agendar, y captura su correo.
"""


def _system_text(canal: Canal) -> str:
    """Convierte los bloques de system de Anthropic a un solo mensaje de sistema.

    Mantiene el orden (reglas + KB estable primero, contexto temporal al final), lo
    que además deja que el caché automático de OpenAI reuse el prefijo estable.
    Agrega el bloque de afinado específico de OpenAI al final.
    """
    base = "\n\n".join(b["text"] for b in _build_system_blocks(canal))
    return base + "\n\n" + _OPENAI_TUNING


async def procesar_turno_openai(
    *,
    mensaje: str,
    session_id: str,
    canal: Canal,
    tester: bool = False,
) -> AgenteResult:
    """Un turno de Sofía con el motor OpenAI (gpt-4o-mini). Mismo contrato que el
    motor de Anthropic: devuelve un AgenteResult y persiste igual."""
    t0 = time.monotonic()
    settings = get_settings()
    repo = get_repository()
    model = settings.openai_model_principal

    from app.adapters.openai_client import get_openai

    client = get_openai().client

    # Historial reciente → mensajes (role user/assistant, igual que el motor original).
    historial = await repo.list_recent_messages(session_id, limit=HISTORIAL_LIMIT)
    messages: list[dict[str, Any]] = [{"role": "system", "content": _system_text(canal)}]
    for r in historial:
        if r.get("role") in ("user", "assistant") and r.get("content"):
            messages.append({"role": r["role"], "content": r["content"]})
    turn_number = sum(1 for m in messages if m["role"] == "user") + 1
    messages.append({"role": "user", "content": mensaje})

    # Conversación + persistir mensaje del usuario.
    estado = await repo.get_conversation(session_id)
    if estado is None:
        estado = EstadoConversacion.nueva(session_id)
        estado.tester = tester
        await repo.upsert_conversation(estado)
    await repo.insert_message(session_id, "user", mensaje)

    tot_in = tot_out = tot_cached = 0
    tools_used: list[str] = []
    final_text = ""

    for _ in range(MAX_TOOL_ITERS):
        resp = await client.chat.completions.create(
            model=model,
            messages=messages,  # type: ignore[arg-type]
            tools=OPENAI_TOOLS,  # type: ignore[arg-type]
            tool_choice="auto",
            temperature=0.5,
            max_tokens=800,
        )
        u = resp.usage
        if u is not None:
            tot_in += u.prompt_tokens or 0
            tot_out += u.completion_tokens or 0
            details = getattr(u, "prompt_tokens_details", None)
            tot_cached += getattr(details, "cached_tokens", 0) or 0

        m = resp.choices[0].message

        if m.tool_calls:
            # Eco del turno del asistente (con las tool_calls) tal cual OpenAI lo espera.
            messages.append(
                {
                    "role": "assistant",
                    "content": m.content or "",
                    "tool_calls": [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {"name": tc.function.name, "arguments": tc.function.arguments},
                        }
                        for tc in m.tool_calls
                    ],
                }
            )
            for tc in m.tool_calls:
                tools_used.append(tc.function.name)
                try:
                    args = json.loads(tc.function.arguments or "{}")
                except json.JSONDecodeError:
                    args = {}
                try:
                    resultado = await _ejecutar_tool(
                        tc.function.name, args, session_id=session_id, canal=canal
                    )
                except Exception as exc:  # la tool no debe tumbar el turno
                    log.warning("tool falló (openai)", extra={"tool": tc.function.name, "error": str(exc)})
                    resultado = "(no pude consultar ese dato ahora; defiérelo con honestidad)"
                messages.append({"role": "tool", "tool_call_id": tc.id, "content": resultado})
            continue

        final_text = (m.content or "").strip()
        break

    if not final_text:
        final_text = "Disculpa, se me cruzaron los cables un momento 😅. ¿Me repites tu última pregunta?"

    final_text = _anexar_mensaje_valor(final_text)

    if canal == Canal.WHATSAPP:
        final_text = _a_formato_whatsapp(final_text)

    latency_ms = int((time.monotonic() - t0) * 1000)
    # Costo conservador: contamos todo el input a precio completo (sin descuento de
    # caché) para no subestimar. gpt-4o-mini ya está en PRICING.
    cost = calculate_cost(model, input_tokens=tot_in, output_tokens=tot_out)

    await repo.insert_message(
        session_id,
        "assistant",
        final_text,
        tokens_input=tot_in,
        tokens_output=tot_out,
        cost_usd=cost,
        model_used=model,
        cache_hit=tot_cached > 0,
        latency_ms=latency_ms,
    )
    try:
        await repo.insert_turn_log(
            session_id=session_id,
            turn_number=turn_number,
            user_message=mensaje,
            tools_used=tools_used,
            final_response=final_text,
            tokens_input=tot_in,
            tokens_output=tot_out,
            tokens_cached=tot_cached,
            cost_usd=cost,
            latency_ms=latency_ms,
            model_used=model,
            metadata={"arquitectura": "sofia_pro_openai", "tester": tester},
        )
    except Exception as exc:  # pragma: no cover
        log.warning("insert_turn_log falló (openai)", extra={"error": str(exc)})

    return AgenteResult(
        session_id=session_id,
        response=final_text,
        turn_number=turn_number,
        tokens_input=tot_in,
        tokens_output=tot_out,
        tokens_cached=tot_cached,
        cost_usd=float(cost),
        latency_ms=latency_ms,
        tools_used=tools_used,
    )
