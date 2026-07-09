"""Loop de agente model-driven para Sofía Pro (arquitectura "Claude conduce").

A diferencia de la Sofía vieja (`orchestrator.py`, code-driven: el código arma
bloques fijos de precios/ruteo/menús y el modelo solo rellena → loops y evasiones),
aquí **el modelo (Sonnet 4.6) CONDUCE la conversación** y el **código es dueño de los
DATOS vía TOOLS**. El modelo decide qué decir y cuándo llamar una herramienta; los
números/fechas/disponibilidad SIEMPRE salen de la BD a través de las tools, nunca se
inventan.

Flujo de un turno:
  1. Cargar historial reciente + mensaje nuevo.
  2. Llamar a Claude con el system prompt (identidad+reglas+KB cacheable) y las tools.
  3. Mientras `stop_reason == "tool_use"`: ejecutar la(s) tool(s), devolver el
     `tool_result` y re-llamar. Cuando el modelo responde texto → ese es el turno.
  4. Persistir mensajes + métricas y devolver `AgenteResult`.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

from app.adapters.anthropic_client import get_anthropic
from app.config import get_settings
from app.core.appointment_extractor import TZ_MONTERREY
from app.core.repository import get_repository
from app.core.state import Canal, EstadoConversacion
from app.integrations.appointments import (
    create_appointment,
    get_pending_appointment_by_lead,
    update_appointment,
)
from app.integrations.events import emit_event
from app.integrations.leads import advance_stage_if_lower, create_lead, get_lead_by_session
from app.notifications.email import (
    render_cita_pendiente_email,
    render_confirmacion_email_papa,
    send_email,
)
from app.observability.costs import calculate_cost
from app.tools.availability_checker import (
    evaluar_dia,
    is_slot_available,
    proximos_dias_habiles,
)
from app.tools.becas import get_becas
from app.tools.campus import get_campus_by_id, get_campus_para_nivel
from app.tools.estancias import get_estancias, render_estancias_bloque
from app.tools.horarios import get_horario
from app.tools.precios import get_precio

log = logging.getLogger(__name__)

KB_PATH = Path(__file__).resolve().parent.parent / "kb" / "sofia_kb_oficial.md"

# Tope de iteraciones de tool-use por turno (anti-loop de herramientas).
MAX_TOOL_ITERS = 8
# Mensajes de historial a cargar para el contexto del modelo.
HISTORIAL_LIMIT = 24


# ============================================================
# System prompt — reglas operativas (la persona/identidad vive en la KB)
# ============================================================

SYSTEM_RULES = """\
Eres **Sofía**, embajadora digital de admisiones de **Maple Collège** (colegio privado en \
Saltillo, Coahuila, 20 años de trayectoria). Hablas por chat con papás/mamás que evalúan \
inscribir a su hijo. Tu voz es cálida, cercana y mexicana; tuteas, eres concisa y humana — \
nunca robótica ni acartonada. Sigues FIELMENTE la identidad, filosofía y conocimiento de la \
base de conocimiento (KB) que viene abajo.

## Cómo conduces (lo más importante)
- TÚ llevas la conversación: natural, con repreguntas, sin guiones rígidos y SIN entrar en \
loop. Si el papá insiste o repregunta, AVANZA con información nueva — jamás repitas la misma \
frase ni des respuestas muertas.
- Conduce con calidez hacia una **cita de informes** con Lily, pero sin presionar: el agendado \
es consecuencia natural de que el papá entienda lo que elige.
- **Contesta primero, empuja después.** Si el papá hizo una pregunta directa y respondible (con \
KB o tools), RESPÓNDELA en ese mismo turno ANTES de ofrecer fechas o empujar la cita. Nunca la \
dejes pendiente para saltar al agendado — eso se siente evasivo.
- Conciso: respuestas de chat (1-2 párrafos cortos). Máximo UNA pregunta por turno.

## Reglas DURAS (datos)
1. **NUNCA** des un precio, horario, costo, fecha de disponibilidad ni agendes "de memoria". \
SIEMPRE usa la herramienta correspondiente. Los números y fechas vienen de la BD vía tools.
2. **Edad → nivel/grado** (para saber qué consultar):
   - Maternal: 0-2 años (Cubs <1a, Babies ~1a, Infants ~1.5a, Toddlers 2a+).
   - Kinder: 3-5 años (K1=3, K2=4, K3=5).
   - Primaria: 6-11 años (1°=6, 2°=7, 3°=8, 4°=9, 5°=10, 6°=11).
   - Secundaria: 12-14 años.
   Primaria 1°-3° = "primaria baja"; 4°-6° = "primaria alta" (tienen costos distintos).
3. **Contenido** (cómo es cada grado/programa, filosofía, metodología) → de la KB, fiel, sin \
inventar detalles.
4. Si algo **NO está en la KB ni en las tools** (nº exacto de alumnos, ratio por salón, \
comedor, psicólogo, uniformes, lista de deportes, transporte): sé **HONESTA** — "déjame \
confirmártelo" / "eso lo ves a detalle en la visita" y ofrece capturar su WhatsApp. \
**NUNCA inventes.**
5. **Becas/descuentos:** solo los OFICIALES (beca por hermanos, socioeconómica) y solo si \
preguntan; consúltalos con la tool. No ofrezcas descuentos que no existan.
6. **Dos o más hijos:** atiende a cada uno por su nombre y su grado; no los mezcles.
7. Antes de **agendar** necesitas: nombre del papá/mamá, su teléfono (WhatsApp), su **correo** \
(para enviarle la confirmación), y nombre, edad y nivel del hijo. Pide lo que falte de forma \
natural (no como formulario). **Pregunta el nombre del papá/mamá y el del hijo POR SEPARADO y \
con claridad** (p. ej. primero "¿cómo te llamas tú?" y luego "¿y cómo se llama tu hijo/a?"). Si \
el papá te da un solo nombre, **aclara de quién es** antes de agendar; **NUNCA registres el \
mismo nombre como papá y como hijo** salvo que lo confirme. Llama `dias_disponibles_visita` y \
**ofrece el día JUNTO CON sus horarios concretos en UN SOLO mensaje** (ej. "Tenemos el martes 8 \
a las 9:00, 10:00 u 11:00 a.m. — ¿cuál te acomoda?"), NO en dos mensajes separados. Copia los \
horarios TAL CUAL te los da la tool. Si el papá dice que **ninguna** de esas opciones le sirve, \
NO insistas ni sigas ofreciendo: dile que **Lily lo contactará directamente** para agendar sin \
problema y captura su WhatsApp y correo. Cuando el papá elija día y hora, llama `agendar_visita`. \
Al confirmar la cita, **incluye SIEMPRE la dirección del campus y el link de Google Maps** que te \
devuelve la tool (cópialo tal cual), y avísale que le llegó un correo de confirmación.

## Fechas, horas y datos faltantes (errores que NO debes cometer)
- **Copia las fechas TAL CUAL salen de la herramienta** — el día, el número y el mes exactos. \
NUNCA recalcules ni "ajustes" una fecha (no cambies "viernes 26" por "viernes 27"). Si dudas, \
vuelve a llamar `dias_disponibles_visita`.
- **No confirmes un horario hasta que la tool lo valide.** Nunca digas "perfecto, el lunes a las \
10 quedó" antes de que `agendar_visita` confirme: di que lo vas a verificar y, si la tool dice \
que está ocupado, ofrece las alternativas que te dé. Confirma SOLO con el OK de la tool.
- Una vez que `agendar_visita` confirma una cita, **YA ESTÁ AGENDADA**: NO vuelvas a ofrecer \
horarios ni digas que "se ocupó". Solo confírmala con calidez. Si el papá quiere otra fecha, \
recién entonces llamas la tool de nuevo.
- **La hora/fecha actual** úsala SOLO del contexto temporal que te di; si no la tienes, no la \
inventes. Mejor no des una hora de reloj exacta a menos que sea necesario.
- Si un dato **no está en KB/tools**, defiere UNA vez con honestidad y luego **avanza** \
(ofrece capturar su WhatsApp o pasar a la visita). NO repitas "no lo tengo / lo ves en la \
visita" turno tras turno — eso se siente evasivo y en loop.

## Herramientas
Tienes herramientas para costos, horarios, estancias (horario extendido), campus, becas, días \
disponibles para visita y para agendar. Úsalas en cuanto el papá toque uno de esos temas. \
Puedes llamar varias en un mismo turno. Si una tool no devuelve el dato, NO lo inventes: \
defiere con honestidad."""


@lru_cache(maxsize=1)
def _load_kb() -> str:
    try:
        return KB_PATH.read_text(encoding="utf-8")
    except OSError as exc:  # pragma: no cover - degradación si falta el archivo
        log.error("No se pudo leer la KB", extra={"path": str(KB_PATH), "error": str(exc)})
        return ""


_DIAS_ES = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
_MESES_ES = [
    "enero", "febrero", "marzo", "abril", "mayo", "junio",
    "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre",
]


def _fecha_es(dt: datetime) -> str:
    return f"{_DIAS_ES[dt.weekday()]} {dt.day} de {_MESES_ES[dt.month - 1]}"


def _hora_es(dt: datetime) -> str:
    sufijo = "a.m." if dt.hour < 12 else "p.m."
    h12 = dt.hour % 12 or 12
    return f"{h12}:{dt.minute:02d} {sufijo}"


# Formato para WhatsApp: NO renderiza markdown. Negrita = *un asterisco*; los links
# van como URL cruda (clickeable). Va en un bloque NO cacheado (después de la KB).
_FORMATO_WHATSAPP = (
    "FORMATO WHATSAPP: escribes por WhatsApp, que NO renderiza markdown. Para negrita usa "
    "*un solo asterisco* (nunca **doble**). Los enlaces van como URL cruda (https://…), "
    "NUNCA como [texto](url). Evita encabezados y tablas; usa texto corto con saltos de línea."
)

# Post-proceso determinista (red de seguridad por si el modelo igual mete markdown).
_MD_LINK = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")
_MD_BOLD = re.compile(r"\*\*(.+?)\*\*", re.S)


def _a_formato_whatsapp(texto: str) -> str:
    """Convierte markdown residual a formato WhatsApp: [t](url)→'t: url', **x**→*x*."""
    texto = _MD_LINK.sub(r"\1: \2", texto)
    texto = _MD_BOLD.sub(r"*\1*", texto)
    return texto


def _build_system_blocks(canal: Canal) -> list[dict[str, Any]]:
    """Bloques del system prompt. La KB (grande y estable) se cachea; la fecha
    actual y el formato por canal van DESPUÉS del breakpoint de cache."""
    ahora = datetime.now(TZ_MONTERREY)
    bloques = [
        {"type": "text", "text": SYSTEM_RULES},
        {
            "type": "text",
            "text": "# BASE DE CONOCIMIENTO OFICIAL\n\n" + _load_kb(),
            # TTL de 1 hora: las charlas de WhatsApp son lentas (el papá tarda en
            # responder). Con 5 min el caché expira entre turnos y se re-cobra la KB
            # completa cada vez; con 1 h se mantiene caliente toda la conversación.
            "cache_control": {"type": "ephemeral", "ttl": "1h"},
        },
        {
            "type": "text",
            "text": (
                f"Contexto temporal — hoy es {_fecha_es(ahora)} de {ahora.year}, "
                f"{_hora_es(ahora)} (hora de Saltillo/Monterrey)."
            ),
        },
    ]
    if canal == Canal.WHATSAPP:
        bloques.append({"type": "text", "text": _FORMATO_WHATSAPP})
    return bloques


# ============================================================
# Definición de tools (JSON Schema que ve el modelo)
# ============================================================

_NIVEL_ENUM = ["maternal", "kinder", "primaria", "secundaria"]

TOOLS_SPEC: list[dict[str, Any]] = [
    {
        "name": "consultar_costos",
        "description": (
            "Devuelve la colegiatura y cuotas REALES de un nivel (desde la BD). "
            "Para primaria, pasa el grado (1-6) para distinguir baja (1-3) de alta (4-6); "
            "si no lo sabes aún, se devuelven ambas. Usa desglose=true cuando el papá pida "
            "el detalle completo de gastos iniciales (inscripción, seguros, total, 'qué más se paga')."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "nivel": {"type": "string", "enum": _NIVEL_ENUM},
                "grado": {
                    "type": "integer",
                    "description": "Grado escolar 1-6 (solo aplica a primaria/kinder).",
                },
                "desglose": {
                    "type": "boolean",
                    "description": "true = detalle completo de gastos iniciales con montos.",
                },
            },
            "required": ["nivel"],
        },
    },
    {
        "name": "consultar_horario",
        "description": "Horario de clases REAL de un nivel (desde la BD). En kinder y primaria pasa el grado.",
        "input_schema": {
            "type": "object",
            "properties": {
                "nivel": {"type": "string", "enum": _NIVEL_ENUM},
                "grado": {"type": "integer", "description": "Grado escolar 1-6 (kinder/primaria)."},
            },
            "required": ["nivel"],
        },
    },
    {
        "name": "consultar_estancia",
        "description": (
            "Modalidades de estancia / horario extendido (mañana, media, completa, express, "
            "academia individual) con horarios y costos REALES de la BD."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "consultar_campus",
        "description": "Ubicación y dirección de los campus de Maple Collège. Opcionalmente filtra por nivel.",
        "input_schema": {
            "type": "object",
            "properties": {"nivel": {"type": "string", "enum": _NIVEL_ENUM}},
        },
    },
    {
        "name": "consultar_becas",
        "description": "Becas y descuentos OFICIALES vigentes (hermanos, socioeconómica). Úsala solo si preguntan.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "dias_disponibles_visita",
        "description": "Próximos días hábiles CON SUS HORARIOS concretos libres para una cita de informes con Lily. Úsala antes de ofrecer fechas y ofrece día+horarios juntos en un mensaje.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "agendar_visita",
        "description": (
            "Agenda la cita de informes. Llama SOLO cuando tengas todos los datos y el papá "
            "haya confirmado día y hora. Verifica disponibilidad: si el horario no está libre, "
            "devuelve alternativas para que las ofrezcas (no se crea la cita)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "nombre_papa": {"type": "string"},
                "telefono": {"type": "string", "description": "WhatsApp del papá/mamá."},
                "email": {"type": "string"},
                "nombre_hijo": {"type": "string"},
                "edad_hijo": {"type": "integer"},
                "nivel": {"type": "string", "enum": _NIVEL_ENUM},
                "dia_iso": {"type": "string", "description": "Fecha en formato YYYY-MM-DD."},
                "hora": {"type": "string", "description": "Hora en formato HH:MM (24h), ej. '10:00'."},
            },
            "required": [
                "nombre_papa", "telefono", "nombre_hijo", "edad_hijo",
                "nivel", "dia_iso", "hora",
            ],
        },
    },
]


# ============================================================
# Normalización de niveles → claves de la BD
# ============================================================


def _subniveles_precio(nivel: str, grado: int | None) -> list[str]:
    nivel = (nivel or "").lower()
    if nivel == "primaria":
        if grado and grado <= 3:
            return ["primaria_baja"]
        if grado and grado >= 4:
            return ["primaria_alta"]
        return ["primaria_baja", "primaria_alta"]
    if nivel in ("maternal", "kinder", "secundaria"):
        return [nivel]
    return [nivel]


def _subniveles_horario(nivel: str, grado: int | None) -> list[str]:
    nivel = (nivel or "").lower()
    if nivel == "kinder":
        return [f"kinder_{grado}"] if grado in (1, 2, 3) else ["kinder_1", "kinder_2", "kinder_3"]
    if nivel == "primaria":
        if grado and grado <= 3:
            return ["primaria_baja"]
        if grado and grado >= 4:
            return ["primaria_alta"]
        return ["primaria_baja", "primaria_alta"]
    if nivel in ("maternal", "secundaria"):
        return [nivel]
    return [nivel]


# ============================================================
# Ejecución de tools
# ============================================================


async def _tool_consultar_costos(inp: dict[str, Any]) -> str:
    nivel = inp.get("nivel", "")
    grado = inp.get("grado")
    desglose = bool(inp.get("desglose"))
    bloques: list[str] = []
    for sub in _subniveles_precio(nivel, grado):
        precio = await get_precio(sub)
        if precio:
            bloques.append(precio.bloque_gastos_completo() if desglose else precio.bloque_costos())
    if not bloques:
        return "No tengo el precio de ese nivel a la mano en este momento. Defiérelo con honestidad."
    return "\n\n".join(bloques)


async def _tool_consultar_horario(inp: dict[str, Any]) -> str:
    nivel = inp.get("nivel", "")
    grado = inp.get("grado")
    bloques: list[str] = []
    for sub in _subniveles_horario(nivel, grado):
        h = await get_horario(sub)
        if h:
            bloques.append(h.bloque())
    if not bloques:
        return "No tengo el horario de ese nivel a la mano. Defiérelo con honestidad."
    return "\n".join(bloques)


async def _tool_consultar_estancia(_inp: dict[str, Any]) -> str:
    estancias = await get_estancias()
    bloque = render_estancias_bloque(estancias)
    return bloque or "No tengo las modalidades de estancia a la mano. Defiérelo con honestidad."


async def _tool_consultar_campus(inp: dict[str, Any]) -> str:
    nivel = (inp.get("nivel") or "").lower()
    campuses = []
    if nivel:
        c = await get_campus_para_nivel(nivel)
        if c:
            campuses = [c]
    if not campuses:
        for cid in (1, 2):
            c = await get_campus_by_id(cid)
            if c:
                campuses.append(c)
    if not campuses:
        return "No tengo la dirección de los campus a la mano. Defiérelo con honestidad."
    lineas = []
    for c in campuses:
        linea = f"{c.nombre}: {c.direccion_legible()}"
        if c.google_maps_url:
            linea += f" ({c.google_maps_url})"
        if c.niveles:
            linea += f" — niveles: {', '.join(c.niveles)}"
        lineas.append(linea)
    return "\n".join(lineas)


async def _tool_consultar_becas(_inp: dict[str, Any]) -> str:
    becas = await get_becas()
    if not becas:
        return "No tengo el detalle de becas a la mano. Defiérelo: lo revisa Lily en la visita."
    lineas = []
    for b in becas:
        pct = f" ({b.porcentaje:.0f}%)" if b.porcentaje is not None else ""
        linea = f"- {b.descripcion}{pct}"
        if b.condiciones:
            linea += f". Condiciones: {b.condiciones}"
        lineas.append(linea)
    return "Becas oficiales vigentes:\n" + "\n".join(lineas)


async def _tool_dias_disponibles_visita(_inp: dict[str, Any]) -> str:
    _FALLBACK = (
        "No tengo horarios libres a la mano ahora. NO inventes horarios: dile al papá que "
        "Lily lo contactará directamente para agendar, y captura su WhatsApp y correo."
    )
    dias = await proximos_dias_habiles(cantidad=3)
    if not dias:
        return _FALLBACK
    hoy = datetime.now(TZ_MONTERREY).date()
    bloques = []
    for d in dias:
        # evaluar_dia devuelve las HORAS libres concretas de ese día.
        res = await evaluar_dia(d)
        if not res.available or not res.alternativas:
            continue
        etq = " (hoy)" if d.date() == hoy else ""
        horas = res.alternativas[:4]  # máx 4 horarios por día para no abrumar
        horas_txt = ", ".join(_hora_es(h) for h in horas)
        bloques.append(f"- {_fecha_es(d)}{etq}: {horas_txt}  [dia_iso={d.date().isoformat()}]")
    if not bloques:
        return _FALLBACK
    return (
        "Días y horarios disponibles. Ofrécelos EXACTAMENTE así, en UN SOLO mensaje "
        "(día + sus horarios juntos), sin cambiar el número de día ni inventar horas:\n"
        + "\n".join(bloques)
        + "\nCuando el papá elija, pasa ese dia_iso y la hora TAL CUAL a agendar_visita. "
        "Si el papá dice que NINGUNA opción le sirve, NO insistas ni ofrezcas más: dile que "
        "Lily lo contactará directamente para agendar sin problema, y captura su WhatsApp y correo."
    )


def _parse_slot(dia_iso: str, hora: str) -> datetime | None:
    try:
        hora = hora.strip()
        if ":" not in hora:
            hora = f"{int(hora):02d}:00"
        partes = hora.split(":")
        h, m = int(partes[0]), int(partes[1])
        y, mo, d = (int(x) for x in dia_iso.split("-"))
        return datetime(y, mo, d, h, m, tzinfo=TZ_MONTERREY)
    except (ValueError, IndexError):
        return None


def _fmt_dt_almacenado(valor: object) -> str | None:
    """Formatea una fecha guardada (datetime o ISO str) a '<fecha> a las <hora>'."""
    if valor is None:
        return None
    dt = valor
    if isinstance(valor, str):
        try:
            dt = datetime.fromisoformat(valor.replace("Z", "+00:00"))
        except ValueError:
            return None
    if not isinstance(dt, datetime):
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(TZ_MONTERREY)
    return f"{_fecha_es(dt)} a las {_hora_es(dt)}"


async def _resolver_campus(nivel: str, edad: int | None):
    """Campus + campus_id para el nivel. Mapea 'primaria' por edad a baja/alta,
    porque la tabla campus se indexa por sub-nivel (primaria_baja/alta)."""
    nivel_campus = nivel
    if nivel == "primaria":
        nivel_campus = "primaria_alta" if (edad is not None and edad >= 9) else "primaria_baja"
    campus = await get_campus_para_nivel(nivel_campus) if nivel_campus else None
    return campus, (campus.id if campus else None)


async def _enviar_correos_cita(
    *, inp: dict[str, Any], dt: datetime, campus, appt_id: int, canal: Canal, nivel_lead: str | None
) -> bool:
    """Aviso interno a Lily + confirmación al papá (Resend). Devuelve si el correo
    al papá se entregó. Nunca lanza (el correo no es load-bearing)."""
    settings = get_settings()
    email_papa = (inp.get("email") or "").strip()
    correo_enviado = False
    try:
        subj_lily, body_lily = render_cita_pendiente_email(
            nombre_papa=inp.get("nombre_papa"),
            nombre_hijo=inp.get("nombre_hijo"),
            edad_hijo=inp.get("edad_hijo"),
            nivel=nivel_lead,
            fecha_hora_iso=dt.isoformat(),
            canal=canal.value,
            appointment_id=appt_id,
            approval_url=settings.appointment_approval_url or None,
        )
        if settings.lily_email:
            await send_email(settings.lily_email, subj_lily, body_lily)
        if email_papa:
            subj_p, text_p, html_p = render_confirmacion_email_papa(
                nombre_papa=inp.get("nombre_papa"), fecha_hora=dt, campus=campus
            )
            res = await send_email(email_papa, subj_p, text_p, html=html_p)
            correo_enviado = bool(getattr(res, "delivered", False))
    except Exception as exc:  # pragma: no cover
        log.warning("envío de correos falló", extra={"error": str(exc), "appt_id": appt_id})
    return correo_enviado


async def _marcar_conversacion_agendada(session_id: str, dt: datetime) -> None:
    """Best-effort: marca la conversación como agendada con la fecha vigente."""
    try:
        repo = get_repository()
        estado = await repo.get_conversation(session_id)
        if estado is not None:
            estado.agendado = True
            estado.fecha_agendado = dt
            estado.estado_capturado.cita_agendada = True
            await repo.upsert_conversation(estado)
    except Exception as exc:  # pragma: no cover
        log.warning("no se pudo marcar agendado", extra={"error": str(exc), "session_id": session_id})


def _texto_confirmacion(*, dt: datetime, campus, correo_enviado: bool, reagendada: bool) -> str:
    """Texto que ve el modelo: fecha + campus + link de Maps EXACTO (de la tabla)."""
    maps_url = (campus.google_maps_url if campus else None) or ""
    lugar = f" en {campus.nombre} ({campus.direccion_legible()})" if campus else ""
    verbo = "reprogramada" if reagendada else "registrada (pendiente de confirmación de Lily)"
    partes = [f"✅ Cita {verbo} para el {_fecha_es(dt)} a las {_hora_es(dt)}{lugar}."]
    if maps_url:
        partes.append(f"Incluye en tu respuesta este link de Google Maps TAL CUAL: {maps_url}")
    if correo_enviado:
        partes.append("Ya le enviamos un correo de confirmación con todos los datos.")
    partes.append("Confírmale al papá con calidez la fecha, la hora, la dirección y el link de Maps.")
    return " ".join(partes)


async def _sync_calendario(
    *, appt_id: int, dt: datetime, inp: dict[str, Any], campus: Any, reagendar: bool
) -> None:
    """Crea (o mueve, si es reagendado) el evento en el Google Calendar de Lily y
    guarda el `google_event_id` en la cita. Best-effort: NUNCA rompe el agendado."""
    from app.integrations.appointments import get_google_event_id, update_appointment
    from app.tools.calendar import get_calendar_tool

    try:
        cal = get_calendar_tool()
        if reagendar:
            gid = await get_google_event_id(appt_id)
            if gid and await cal.actualizar_evento(gid, dt):
                return  # evento movido en su lugar
        ev = await cal.agendar_cita(
            nombre_papa=inp.get("nombre_papa") or "Papá/Mamá",
            nombre_hijo=inp.get("nombre_hijo"),
            nivel=(inp.get("nivel") or ""),
            fecha=dt,
            campus=getattr(campus, "nombre", None) or "Campus 1",
        )
        if not ev.simulado:
            await update_appointment(appt_id, {"google_event_id": ev.evento_id})
    except Exception as exc:  # noqa: BLE001
        log.warning("sync calendario falló", extra={"error": str(exc), "appt_id": appt_id})


async def _tool_agendar_visita(inp: dict[str, Any], *, session_id: str, canal: Canal) -> str:
    dt = _parse_slot(inp.get("dia_iso", ""), inp.get("hora", ""))
    if dt is None:
        return "No pude interpretar la fecha/hora. Pídele al papá el día y la hora de nuevo."

    nivel = (inp.get("nivel") or "").lower()
    nivel_lead = nivel if nivel in ("maternal", "kinder", "primaria", "secundaria") else None
    campus, campus_id = await _resolver_campus(nivel, inp.get("edad_hijo"))

    # IDEMPOTENCIA / REAGENDADO: se basa en la cita REAL del lead, no en una bandera
    # (que puede quedar huérfana). Si ya hay cita pendiente:
    #   - mismo horario → idempotente (solo confirma, sin recrear).
    #   - horario distinto → REPROGRAMA la cita existente y reenvía la confirmación.
    lead = await get_lead_by_session(session_id)
    existente = await get_pending_appointment_by_lead(lead.id) if lead else None
    if existente is not None:
        exist_dt = existente.fecha_hora
        if exist_dt.tzinfo is not None:
            exist_dt = exist_dt.astimezone(TZ_MONTERREY)
        if abs((exist_dt - dt).total_seconds()) < 60:
            return _texto_confirmacion(dt=exist_dt, campus=campus, correo_enviado=False, reagendada=False)

        # Reprogramar a un horario distinto.
        disp = await is_slot_available(dt)
        if not disp.available:
            if disp.alternativas:
                alts = "; ".join(f"{_fecha_es(a)} a las {_hora_es(a)}" for a in disp.alternativas)
                return (
                    f"Ese horario no está disponible ({disp.mensaje}). Ofrécele estas alternativas "
                    f"y vuelve a llamar la tool cuando elija: {alts}."
                )
            return f"Ese horario no está disponible: {disp.mensaje} Pregúntale otra fecha/hora."

        campos = {"fecha_hora": dt}
        if campus_id is not None:
            campos["campus_id"] = campus_id
        if not await update_appointment(existente.id, campos):
            return "No pude mover la cita en el sistema. Pídele que intente de nuevo en un momento."
        await _sync_calendario(appt_id=existente.id, dt=dt, inp=inp, campus=campus, reagendar=True)
        await _marcar_conversacion_agendada(session_id, dt)
        correo = await _enviar_correos_cita(
            inp=inp, dt=dt, campus=campus, appt_id=existente.id, canal=canal, nivel_lead=nivel_lead
        )
        return _texto_confirmacion(dt=dt, campus=campus, correo_enviado=correo, reagendada=True)

    # NUEVA cita: verificar disponibilidad real con la agenda de Lily.
    disp = await is_slot_available(dt)
    if not disp.available:
        if disp.alternativas:
            alts = "; ".join(f"{_fecha_es(a)} a las {_hora_es(a)}" for a in disp.alternativas)
            return (
                f"Ese horario no está disponible ({disp.mensaje}). Ofrécele estas alternativas "
                f"y vuelve a llamar la tool cuando elija: {alts}."
            )
        return f"Ese horario no está disponible: {disp.mensaje} Pregúntale otra fecha/hora."

    # Crear / reutilizar el lead.
    lead_id = lead.id if lead else None
    if lead_id is None:
        lead_id = await create_lead(
            parent_name=inp.get("nombre_papa", "Papá/Mamá"),
            channel=canal.value,
            conversation_session_id=session_id,
            parent_phone=inp.get("telefono"),
            parent_email=inp.get("email"),
            child_name=inp.get("nombre_hijo"),
            child_age=inp.get("edad_hijo"),
            nivel=nivel_lead,
            notes="Lead creado por Sofía Pro al agendar visita.",
        )
    if lead_id is None:
        return (
            "No pude registrar la cita en el sistema en este momento. Ofrece tomar sus datos "
            "para que Lily lo contacte directamente y confirme."
        )

    notas = (
        f"Cita agendada por Sofía Pro. Hijo: {inp.get('nombre_hijo')} "
        f"({inp.get('edad_hijo')} años, {nivel}). Tel: {inp.get('telefono')}."
    )
    appt_id = await create_appointment(
        lead_id=lead_id, fecha_hora=dt, notas=notas, campus_id=campus_id
    )
    if appt_id is None:
        return (
            "No pude crear la cita en el sistema. Ofrece tomar sus datos para que Lily lo "
            "contacte y confirme la visita."
        )

    # Avanzar el stage a 'cita_agendada' + eventos (para el panel). Best-effort.
    fecha_humana = f"{_fecha_es(dt)} a las {_hora_es(dt)}"
    try:
        await emit_event(
            "sofia_appointment_scheduled",
            lead_id=lead_id,
            session_id=session_id,
            description=(
                f"Sofía Pro agendó cita para {fecha_humana} en "
                f"{campus.nombre if campus else f'campus_id={campus_id}'} (pendiente de aprobación)"
            ),
            metadata={
                "appointment_id": appt_id,
                "fecha_hora": dt.isoformat(),
                "canal": canal.value,
                "status": "pendiente",
                "campus_id": campus_id,
            },
        )
        lead_now = await get_lead_by_session(session_id)
        if lead_now and lead_now.stage != "cita_agendada":
            if await advance_stage_if_lower(lead_id, lead_now.stage, "cita_agendada"):
                await emit_event(
                    "lead_stage_changed",
                    lead_id=lead_id,
                    session_id=session_id,
                    description=f"Stage avanzó de {lead_now.stage} a cita_agendada",
                    metadata={"from": lead_now.stage, "to": "cita_agendada"},
                )
    except Exception as exc:  # pragma: no cover
        log.warning("post-agendado (stage/evento) falló", extra={"error": str(exc), "lead_id": lead_id})

    await _sync_calendario(appt_id=appt_id, dt=dt, inp=inp, campus=campus, reagendar=False)
    await _marcar_conversacion_agendada(session_id, dt)
    correo = await _enviar_correos_cita(
        inp=inp, dt=dt, campus=campus, appt_id=appt_id, canal=canal, nivel_lead=nivel_lead
    )
    return _texto_confirmacion(dt=dt, campus=campus, correo_enviado=correo, reagendada=False)


async def _ejecutar_tool(
    name: str, inp: dict[str, Any], *, session_id: str, canal: Canal
) -> str:
    if name == "consultar_costos":
        return await _tool_consultar_costos(inp)
    if name == "consultar_horario":
        return await _tool_consultar_horario(inp)
    if name == "consultar_estancia":
        return await _tool_consultar_estancia(inp)
    if name == "consultar_campus":
        return await _tool_consultar_campus(inp)
    if name == "consultar_becas":
        return await _tool_consultar_becas(inp)
    if name == "dias_disponibles_visita":
        return await _tool_dias_disponibles_visita(inp)
    if name == "agendar_visita":
        return await _tool_agendar_visita(inp, session_id=session_id, canal=canal)
    return f"(herramienta desconocida: {name})"


# ============================================================
# Loop principal
# ============================================================


@dataclass
class AgenteResult:
    session_id: str
    response: str
    turn_number: int
    tokens_input: int
    tokens_output: int
    tokens_cached: int
    cost_usd: float
    latency_ms: int
    tools_used: list[str] = field(default_factory=list)


async def procesar_turno_agente(
    *,
    mensaje: str,
    session_id: str,
    canal: Canal,
    tester: bool = False,
) -> AgenteResult:
    """Procesa un turno con el loop de agente model-driven (Sofía Pro)."""
    t0 = time.monotonic()
    settings = get_settings()
    repo = get_repository()
    model = settings.anthropic_model_principal

    # Historial reciente → mensajes para el modelo.
    historial = await repo.list_recent_messages(session_id, limit=HISTORIAL_LIMIT)
    messages: list[dict[str, Any]] = [
        {"role": r["role"], "content": r["content"]}
        for r in historial
        if r.get("role") in ("user", "assistant") and r.get("content")
    ]
    turn_number = sum(1 for m in messages if m["role"] == "user") + 1
    messages.append({"role": "user", "content": mensaje})

    # Asegurar que la conversación exista (FK de sofia_messages → sofia_conversations).
    estado = await repo.get_conversation(session_id)
    if estado is None:
        estado = EstadoConversacion.nueva(session_id)
        estado.tester = tester
        await repo.upsert_conversation(estado)

    # Persistir el mensaje del usuario.
    await repo.insert_message(session_id, "user", mensaje)

    system_blocks = _build_system_blocks(canal)
    client = get_anthropic().client

    tot_in = tot_out = tot_cache_read = tot_cache_write = 0
    tools_used: list[str] = []
    final_text = ""

    for _ in range(MAX_TOOL_ITERS):
        resp = await client.messages.create(
            model=model,
            max_tokens=1024,
            temperature=0.6,
            system=system_blocks,  # type: ignore[arg-type]
            messages=messages,  # type: ignore[arg-type]
            tools=TOOLS_SPEC,  # type: ignore[arg-type]
        )
        u = resp.usage
        tot_in += u.input_tokens
        tot_out += u.output_tokens
        tot_cache_read += getattr(u, "cache_read_input_tokens", 0) or 0
        tot_cache_write += getattr(u, "cache_creation_input_tokens", 0) or 0

        if resp.stop_reason == "tool_use":
            messages.append({"role": "assistant", "content": resp.content})
            tool_results: list[dict[str, Any]] = []
            for block in resp.content:
                if getattr(block, "type", None) != "tool_use":
                    continue
                tools_used.append(block.name)
                try:
                    resultado = await _ejecutar_tool(
                        block.name, dict(block.input or {}), session_id=session_id, canal=canal
                    )
                except Exception as exc:  # pragma: no cover - la tool no debe tumbar el turno
                    log.warning(
                        "tool falló", extra={"tool": block.name, "error": str(exc)}
                    )
                    resultado = "(no pude consultar ese dato ahora; defiérelo con honestidad)"
                tool_results.append(
                    {"type": "tool_result", "tool_use_id": block.id, "content": resultado}
                )
            messages.append({"role": "user", "content": tool_results})
            continue

        # stop_reason normal → recolectar el texto final.
        final_text = "".join(
            b.text for b in resp.content if getattr(b, "type", None) == "text"
        ).strip()
        break

    if not final_text:
        final_text = (
            "Disculpa, se me cruzaron los cables un momento 😅. ¿Me repites tu última pregunta?"
        )

    # WhatsApp no renderiza markdown: red de seguridad determinista.
    if canal == Canal.WHATSAPP:
        final_text = _a_formato_whatsapp(final_text)

    latency_ms = int((time.monotonic() - t0) * 1000)
    cost = calculate_cost(
        model,
        input_tokens=tot_in,
        output_tokens=tot_out,
        cache_read_tokens=tot_cache_read,
        cache_write_tokens=tot_cache_write,
    )

    # Persistir respuesta + log del turno (best-effort para métricas A/B).
    await repo.insert_message(
        session_id,
        "assistant",
        final_text,
        tokens_input=tot_in,
        tokens_output=tot_out,
        cost_usd=cost,
        model_used=model,
        cache_hit=tot_cache_read > 0,
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
            tokens_cached=tot_cache_read,
            cost_usd=cost,
            latency_ms=latency_ms,
            model_used=model,
            metadata={"arquitectura": "sofia_pro_agente", "tester": tester},
        )
    except Exception as exc:  # pragma: no cover
        log.warning("insert_turn_log falló", extra={"error": str(exc)})

    return AgenteResult(
        session_id=session_id,
        response=final_text,
        turn_number=turn_number,
        tokens_input=tot_in,
        tokens_output=tot_out,
        tokens_cached=tot_cache_read,
        cost_usd=float(cost),
        latency_ms=latency_ms,
        tools_used=tools_used,
    )
