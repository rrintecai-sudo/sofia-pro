"""Webhook de WhatsApp vía Evolution API (Bloque WhatsApp — 28-may-2026).

Recibe POSTs de Evolution con eventos `MESSAGES_UPSERT` y procesa el mensaje
por el mismo `procesar_turno()` del orchestrator que usan los otros canales.

Diseño:
- Ignora mensajes propios (fromMe=true) → evita eco infinito.
- Ignora la instancia equivocada → seguridad contra apuntar webhook a la
  instancia v1 por error.
- Ignora grupos (`@g.us`) → este flujo es 1-a-1 prospecto/Sofía.
- Normaliza texto / audio (Whisper) / imagen (gpt-4o-mini vision).
- Usa el mismo debouncer (7s) que los otros canales para agrupar mensajes
  consecutivos del papá.
- Responde por Evolution.send_text al mismo número.

Evolution responde 200 rápido — el procesamiento corre en background_tasks.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Request, status

from app.adapters.anthropic_client import get_anthropic
from app.adapters.evolution_client import EvolutionChannel, get_evolution
from app.config import get_settings
from app.core.sofia_engine import procesar_turno_sofia
from app.core.debounce import get_debouncer
from app.core.repository import get_repository
from app.core.state import Canal

log = logging.getLogger(__name__)

router = APIRouter(tags=["whatsapp"])


# ============================================================
# Endpoint público
# ============================================================


@router.post("/webhook/whatsapp", status_code=status.HTTP_200_OK)
async def webhook_whatsapp(
    request: Request,
    background_tasks: BackgroundTasks,
) -> dict[str, str]:
    """Endpoint del webhook de Evolution. Responde 200 inmediatamente."""
    try:
        payload = await request.json()
    except Exception as exc:
        log.warning("whatsapp webhook with non-json body", extra={"error": str(exc)})
        return {"status": "ignored"}

    background_tasks.add_task(_process_event, payload)
    return {"status": "received"}


# ============================================================
# Procesamiento en background
# ============================================================


async def _process_event(payload: dict[str, Any]) -> None:
    """Maneja un evento de Evolution. Robusto a errores — solo loggea."""
    try:
        await _handle_event(payload)
    except Exception as exc:
        log.error(
            "whatsapp event processing failed",
            extra={
                "error": str(exc),
                "event": payload.get("event"),
                "instance": payload.get("instance"),
            },
        )


async def _handle_event(payload: dict[str, Any]) -> None:
    settings = get_settings()
    expected_instance = settings.evolution_instance

    event = (payload.get("event") or "").lower()
    instance = payload.get("instance")

    # Sólo procesamos messages.upsert; ignoramos QR updates, connection updates, etc.
    if event not in ("messages.upsert", "messages_upsert"):
        log.debug(
            "whatsapp ignored non-message event",
            extra={"event": event, "instance": instance},
        )
        return

    # 🔒 Hard guard: la instancia que envía DEBE ser la nuestra. Si Evolution
    # apuntara mal el webhook (o si el endpoint quedara expuesto), NO
    # procesamos eventos de otras instancias. La de producción v1 es
    # 'Maple Sofia'; nosotros sólo aceptamos la configurada en settings.
    if expected_instance and instance != expected_instance:
        log.warning(
            "whatsapp event from unexpected instance — ignored",
            extra={"got": instance, "expected": expected_instance},
        )
        return

    data = payload.get("data") or {}
    key = data.get("key") or {}

    remote_jid: str = key.get("remoteJid") or ""

    # Mensaje 'propio' (fromMe): puede ser un ECO del bot (ignorar) o una respuesta
    # MANUAL de Lily (auto-handoff → apagar el bot en ese chat).
    if key.get("fromMe") is True:
        await _manejar_mensaje_propio(data, key, remote_jid)
        return

    if not remote_jid:
        log.warning("whatsapp event sin remoteJid")
        return

    # Ignora grupos y broadcasts (sólo conversaciones 1-a-1 con prospectos)
    if remote_jid.endswith("@g.us") or remote_jid.endswith("@broadcast"):
        log.info("whatsapp ignored group/broadcast", extra={"remote_jid": remote_jid})
        return

    evolution = get_evolution()
    debouncer = get_debouncer()
    session_id = EvolutionChannel.session_id_for_remote(remote_jid)

    # Capturar el pushName (nombre que el contacto se puso a sí mismo) para que la
    # bandeja del panel muestre un nombre en vez del número crudo. Best-effort: la
    # clave es el mismo número que deriva el panel del session_id (parte antes de @).
    push_name = data.get("pushName") or data.get("pushname")
    if push_name and isinstance(push_name, str):
        numero_contacto = remote_jid.split("@", 1)[0]
        await get_repository().upsert_contacto(numero_contacto, pushname=push_name.strip())

    # Identificadores del contacto para el handoff, robustos al formato de WhatsApp
    # (número normal @s.whatsapp.net vs @lid de privacidad). Juntamos los dígitos
    # del número (de remoteJid o remoteJidAlt) y el @lid, para que el bloqueo
    # 'solo humano' coincida sin importar cómo venga direccionado el mensaje.
    identificadores = _identificadores_contacto(remote_jid, key.get("remoteJidAlt"))

    # Normalizar el mensaje a texto
    texto = await _extract_text(data, evolution)
    if not texto:
        log.info("whatsapp mensaje sin texto extraíble", extra={"session_id": session_id})
        return

    # Marcar como leído (best-effort)
    message_id = key.get("id")
    if message_id:
        await evolution.mark_as_read(session_id, message_id)

    # Push a debouncer
    seq_id = await debouncer.push_message(session_id, texto)

    # Esperar ventana
    await asyncio.sleep(debouncer.window_seconds)

    # try_claim — sólo el último seq_id procesa el turno
    claim = await debouncer.try_claim(session_id, seq_id)
    if not claim.claimed:
        return

    # HANDOFF: si un humano atiende este contacto, el bot NO responde — pero
    # guardamos el mensaje para la bandeja. Dos señales:
    #  - lista 'whatsapp_humano' (precarga de contactos de Lily / bloqueo por número),
    #  - bot_activo=false en la conversación (toggle "Yo atiendo" de la bandeja).
    repo = get_repository()
    es_humano = await repo.hay_identificador_humano(identificadores)

    # LEAD DE ANUNCIO: un clic en anuncio de Facebook/IG es un prospecto nuevo con
    # intención. Aunque su número esté en la lista 'humano' (la precarga del cutover
    # barrió leads viejos por error, p.ej. Pati), Sofía SÍ debe atenderlo. No anula
    # la toma manual de Lily (bot_activo=false), solo el bloqueo por lista.
    es_anuncio = _es_lead_de_anuncio(data)
    if es_humano and es_anuncio:
        log.info(
            "lead de anuncio en lista 'humano' → Sofía responde igual",
            extra={"session_id": session_id},
        )
        es_humano = False

    # CONVERSACIÓN PRE-EXISTENTE: si Sofía nunca ha atendido este chat pero en
    # WhatsApp ya había mensajes de ANTES del cutover, es un cliente que Lily ya
    # venía atendiendo → Sofía se hace a un lado sola (sin que Lily deba vigilar).
    # Se evalúa una sola vez: al detectarlo, marca bot_activo=false y ya no vuelve.
    if not es_humano and await repo.get_conversation(session_id) is None:
        preexistente = await _conversacion_preexistente(session_id)
        continuacion = await _parece_continuacion_humana(claim.joined)
        if preexistente or continuacion:
            await repo.ensure_conversation(session_id, Canal.WHATSAPP)
            await repo.set_bot_active(session_id, False, atendido_por="humano")
            await repo.insert_message(session_id, "user", claim.joined)
            log.info(
                "conversación de Lily (no primer contacto) → Sofía se hace a un lado",
                extra={
                    "session_id": session_id,
                    "motivo": "historial" if preexistente else "continuacion",
                },
            )
            return

    if es_humano or not await repo.is_bot_active(session_id):
        await repo.ensure_conversation(session_id, Canal.WHATSAPP)
        await repo.insert_message(session_id, "user", claim.joined)
        log.info(
            "whatsapp handoff a humano — mensaje guardado, sin responder",
            extra={"session_id": session_id, "por_lista": es_humano},
        )
        return

    # Procesar turno con typing indicator
    typing_task = asyncio.create_task(_keep_typing(evolution, session_id))
    try:
        result = await procesar_turno_sofia(
            mensaje=claim.joined,
            session_id=session_id,
            canal=Canal.WHATSAPP,
        )
    finally:
        typing_task.cancel()

    # Enviar respuesta
    await evolution.send_text(session_id, result.response)
    log.info(
        "whatsapp turn delivered",
        extra={
            "session_id": session_id,
            "tokens_input": result.tokens_input,
            "tokens_output": result.tokens_output,
            "cost_usd": float(result.cost_usd),
            "latency_ms": result.latency_ms,
        },
    )


# ============================================================
# Extracción de texto / audio / imagen
# ============================================================


def _identificadores_contacto(remote_jid: str, remote_jid_alt: str | None) -> list[str]:
    """Identificadores estables de un contacto para el handoff, robustos al
    formato de WhatsApp: dígitos del número (de @s.whatsapp.net/@c.us, sea el
    remoteJid o su alterno) + el '<lid>@lid' si aplica. Así el bloqueo 'solo
    humano' coincide venga como venga direccionado el mensaje."""
    ids: list[str] = []
    for jid in (remote_jid, remote_jid_alt):
        if not jid or not isinstance(jid, str):
            continue
        if jid.endswith("@s.whatsapp.net") or jid.endswith("@c.us"):
            num = jid.split("@", 1)[0].split(":", 1)[0]
            digits = "".join(ch for ch in num if ch.isdigit())
            if digits:
                ids.append(digits)
        elif jid.endswith("@lid"):
            ids.append(jid)
    return list(dict.fromkeys(ids))


def _texto_propio(data: dict[str, Any]) -> str:
    """Texto plano de un mensaje 'propio' (respuesta de Lily). Sin transcribir media."""
    msg = data.get("message") or {}
    if isinstance(msg.get("conversation"), str):
        return msg["conversation"].strip()
    ext = msg.get("extendedTextMessage")
    if isinstance(ext, dict):
        return (ext.get("text") or "").strip()
    return ""


# Marcas típicas de un clic en anuncio de Facebook/Instagram (click-to-WhatsApp).
_AD_KEYS = {"externaladreply", "ctwaclid", "conversionsource"}


def _es_lead_de_anuncio(data: dict[str, Any]) -> bool:
    """True si el mensaje entrante viene de un clic en anuncio (click-to-WhatsApp).
    Un clic de anuncio es SIEMPRE un prospecto nuevo con intención → Sofía debe
    responderle aunque el número esté en la lista de 'humano' (la precarga barrió
    leads viejos por error). Busca las marcas en cualquier parte del payload."""

    def _busca(obj: Any) -> bool:
        if isinstance(obj, dict):
            for k, v in obj.items():
                if k.lower() in _AD_KEYS or _busca(v):
                    return True
        elif isinstance(obj, list):
            return any(_busca(it) for it in obj)
        return False

    return _busca(data.get("message"))


# Menciones a Lily (o su equipo por nombre) → señal fuerte de que el papá ya viene
# hablando con una persona, no con Sofía.
_RE_HUMANO = re.compile(r"lil[iy]|lilian|miss\s+lil", re.IGNORECASE)

_CLASIF_CONTINUACION = (
    "Eres un clasificador para una escuela. Te doy el/los PRIMER(OS) mensaje(s) que "
    "una persona envió por WhatsApp. Responde SOLO una letra:\n"
    "A = primer contacto / consulta nueva (pide informes, precios, inscripción, saluda "
    "sin referirse a nada previo).\n"
    "B = CONTINUACIÓN de una conversación que la persona YA traía con alguien del equipo "
    "(agradece, retoma o confirma algo ya acordado, una visita/cita ya hablada, saluda a "
    "alguien por su nombre, se despide, etc.).\n"
    "Ante la duda, responde A."
)


async def _parece_continuacion_humana(mensaje: str) -> bool:
    """True si el mensaje parece la CONTINUACIÓN de una conversación que el papá ya
    tenía con una persona del equipo (no un primer contacto). Evita que Sofía se meta
    en conversaciones que Lily ya venía atendiendo aunque no haya historial en el
    sistema. Señal dura: menciona a Lily. Señal fina: clasificador barato."""
    if not mensaje:
        return False
    if _RE_HUMANO.search(mensaje):
        return True
    try:
        r = await get_anthropic().client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=1,
            system=_CLASIF_CONTINUACION,
            messages=[{"role": "user", "content": mensaje[:600]}],
        )
        texto = "".join(b.text for b in r.content if getattr(b, "type", "") == "text")
        return texto.strip().upper().startswith("B")
    except Exception as exc:  # noqa: BLE001
        log.warning("clasificador continuacion falló", extra={"error": str(exc)})
        return False  # ante la duda, Sofía responde (no bloquear leads por un error)


_CUTOVER_TS: float | None = None


def _cutover_ts() -> float:
    """Epoch (segundos) del momento en que Sofía entró a producción."""
    global _CUTOVER_TS
    if _CUTOVER_TS is None:
        from datetime import datetime

        _CUTOVER_TS = datetime.fromisoformat(get_settings().sofia_cutover_iso).timestamp()
    return _CUTOVER_TS


async def _conversacion_preexistente(session_id: str) -> bool:
    """True si el chat de WhatsApp ya tenía mensajes ANTES del cutover — es decir,
    es una conversación que Lily ya venía atendiendo. Se apoya en el historial de
    WhatsApp (Evolution). Best-effort: ante cualquier duda/error, False (Sofía
    atiende) para no dejar sin respuesta a un prospecto nuevo."""
    msgs = await get_evolution().find_messages(session_id)
    if not msgs:
        return False
    cutover = _cutover_ts()
    for m in msgs:
        ts = m.get("messageTimestamp")
        try:
            ts = int(ts)
        except (TypeError, ValueError):
            continue
        if ts and ts < cutover:
            return True
    return False


async def _manejar_mensaje_propio(
    data: dict[str, Any], key: dict[str, Any], remote_jid: str
) -> None:
    """Mensaje fromMe: si NO lo envió el bot, es Lily contestando a mano → apaga el
    bot para ese chat (auto-handoff) y guarda el mensaje de Lily en la bandeja."""
    if not remote_jid or remote_jid.endswith("@g.us") or remote_jid.endswith("@broadcast"):
        return
    repo = get_repository()
    msg_id = key.get("id")
    # Si el id corresponde a un mensaje que envió el bot → es su propio eco → ignorar.
    if msg_id and await repo.es_mensaje_del_bot(msg_id):
        return
    session_id = EvolutionChannel.session_id_for_remote(remote_jid)
    texto = _texto_propio(data)
    # Seguro anti-carrera: si el texto coincide con el último mensaje del asistente,
    # es un eco del bot aunque su id no se haya registrado todavía → no apagar.
    if texto and texto == await repo.texto_ultimo_asistente(session_id):
        return

    # Respuesta MANUAL de Lily → auto-handoff.
    await repo.ensure_conversation(session_id, Canal.WHATSAPP)
    if await repo.is_bot_active(session_id):
        await repo.set_bot_active(session_id, False, atendido_por="humano")
        log.info(
            "auto-handoff: Lily respondió a mano → bot apagado",
            extra={"session_id": session_id},
        )
    if texto:
        await repo.insert_message(
            session_id, "assistant", texto, metadata={"sent_by": "humano"}
        )


async def _extract_text(data: dict[str, Any], evolution: EvolutionChannel) -> str:
    """Devuelve el texto del mensaje. Transcribe voz, describe imagen.

    Evolution puede mandar el media en base64 inline (config base64=true en el
    webhook). Si no, el adapter lo descarga con /chat/getBase64FromMediaMessage
    usando el message_id.
    """
    msg = data.get("message") or {}
    message_type = (data.get("messageType") or "").lower()
    key = data.get("key") or {}
    message_id = key.get("id")

    # 1. Texto puro
    if "conversation" in msg and isinstance(msg["conversation"], str):
        return msg["conversation"].strip()

    if "extendedTextMessage" in msg:
        ext = msg["extendedTextMessage"]
        if isinstance(ext, dict):
            text = (ext.get("text") or "").strip()
            if text:
                return text

    # 2. Audio (PTT/voice) → Whisper
    if "audioMessage" in msg or message_type in ("audiomessage", "ptt"):
        audio_b64 = msg.get("base64") or data.get("base64")
        try:
            return await evolution.transcribe_voice(
                {"audio_base64": audio_b64, "message_id": message_id}
            )
        except Exception as exc:
            log.warning("whatsapp transcribe_voice failed", extra={"error": str(exc)})
            return "(audio recibido — no pude transcribirlo)"

    # 3. Imagen → vision
    if "imageMessage" in msg or message_type == "imagemessage":
        img_b64 = msg.get("base64") or data.get("base64")
        try:
            descripcion = await evolution.describe_image(
                {"image_base64": img_b64, "message_id": message_id}
            )
        except Exception as exc:
            log.warning("whatsapp describe_image failed", extra={"error": str(exc)})
            descripcion = "(no pude analizar la imagen)"
        caption = ""
        if isinstance(msg.get("imageMessage"), dict):
            caption = (msg["imageMessage"].get("caption") or "").strip()
        if caption:
            return f"{caption}\n\n(imagen adjunta: {descripcion})"
        return f"(imagen adjunta: {descripcion})"

    # 4. Sticker
    if "stickerMessage" in msg or message_type == "stickermessage":
        return "(el papá envió un sticker)"

    # 5. Documento
    if "documentMessage" in msg or message_type == "documentmessage":
        doc = msg.get("documentMessage") or {}
        name = (doc.get("fileName") or "archivo") if isinstance(doc, dict) else "archivo"
        return f"(el papá envió un documento: {name})"

    return ""


async def _keep_typing(evolution: EvolutionChannel, session_id: str) -> None:
    """Re-emite el indicador 'escribiendo...' mientras Sofía piensa."""
    try:
        while True:
            await evolution.typing_indicator(session_id, on=True)
            await asyncio.sleep(4)
    except asyncio.CancelledError:
        return
