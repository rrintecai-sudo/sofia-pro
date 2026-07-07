"""Programador en proceso (single-replica) del bot.

Dos tareas periódicas:
  1. Recordatorios de cita por WhatsApp: 24 h, 2 h y 1 h antes de la cita.
     Idempotente vía la tabla `appointment_reminders` (PK appointment_id+tipo).
  2. Sync de nombres de contactos: trae la agenda del teléfono (Evolution
     findContacts) y guarda el `nombre_guardado` por número, para que la bandeja
     muestre el nombre que Lily guardó en vez del número.

Corre como una tarea de asyncio arrancada en el lifespan de FastAPI. Como el
servicio tiene una sola réplica, no hay riesgo de doble envío.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from app.adapters.evolution_client import get_evolution
from app.core.repository import get_repository

log = logging.getLogger(__name__)

TZ = ZoneInfo("America/Monterrey")
_DIAS = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
_MESES = [
    "enero", "febrero", "marzo", "abril", "mayo", "junio",
    "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre",
]

REMINDER_TICK_S = 300  # revisa recordatorios cada 5 min
CONTACTOS_CADA = 6  # sincroniza contactos cada 6 ticks (~30 min)
GRACE = timedelta(minutes=35)  # margen tras cruzar el umbral (cubre el polling)
OFFSETS = [
    ("24h", timedelta(hours=24)),
    ("2h", timedelta(hours=2)),
    ("30m", timedelta(minutes=30)),
]


def _fmt_dia(dt: datetime) -> str:
    return f"{_DIAS[dt.weekday()]} {dt.day} de {_MESES[dt.month - 1]}"


def _fmt_hora(dt: datetime) -> str:
    sufijo = "a.m." if dt.hour < 12 else "p.m."
    h12 = dt.hour % 12 or 12
    return f"{h12}:{dt.minute:02d} {sufijo}"


def _mensaje(tipo: str, nombre: str, dt_local: datetime) -> str:
    saludo = f"Hola {nombre} 👋" if nombre else "¡Hola! 👋"
    if tipo == "24h":
        return (
            f"{saludo} Te recordamos tu cita de informes en Maple Collège el "
            f"{_fmt_dia(dt_local)} a las {_fmt_hora(dt_local)} 🍁 ¡Te esperamos! "
            "Si necesitas reagendar, respóndenos por aquí."
        )
    if tipo == "2h":
        return (
            f"{saludo} Tu cita de informes en Maple Collège es hoy a las "
            f"{_fmt_hora(dt_local)} (en unas 2 horas). ¡Te esperamos! 😊"
        )
    return (
        f"{saludo} Te esperamos en aproximadamente 30 minutos para tu cita de informes en "
        f"Maple Collège (hoy a las {_fmt_hora(dt_local)}). 📍"
    )


def _session_id_de_lead(lead: dict) -> str | None:
    sid = lead.get("conversation_session_id")
    if sid:
        return sid
    phone = lead.get("parent_phone") or ""
    digits = "".join(ch for ch in phone if ch.isdigit())
    return f"whatsapp:{digits}@s.whatsapp.net" if digits else None


async def tick_reminders() -> None:
    """Envía los recordatorios que crucen su umbral en esta pasada."""
    repo = get_repository()
    evo = get_evolution()
    now = datetime.now(timezone.utc)
    horizon = now + timedelta(hours=25)

    appts = await repo.fetch_upcoming_appointments(now.isoformat(), horizon.isoformat())
    if not appts:
        return
    ids = [int(a["id"]) for a in appts]
    ya_enviados = await repo.fetch_sent_reminders(ids)

    for a in appts:
        try:
            fecha = datetime.fromisoformat(str(a["fecha_hora"]))
        except (ValueError, KeyError):
            continue
        if fecha.tzinfo is None:
            fecha = fecha.replace(tzinfo=timezone.utc)
        remaining = fecha - now
        lead = a.get("leads") or {}
        sid = _session_id_de_lead(lead)
        if not sid:
            continue
        nombre_full = (lead.get("parent_name") or "").strip()
        nombre = nombre_full.split(" ")[0] if nombre_full else ""
        dt_local = fecha.astimezone(TZ)

        for tipo, offset in OFFSETS:
            if (int(a["id"]), tipo) in ya_enviados:
                continue
            # Enviar solo al cruzar el umbral (dentro de la ventana de gracia); si la
            # cita se creó ya pasado el umbral, ese recordatorio se omite.
            if offset - GRACE < remaining <= offset:
                # RECLAMAR ANTES DE ENVIAR: insertamos la fila (PK appointment_id+tipo)
                # primero. Si el insert lo gana este proceso (True), enviamos; si otro
                # proceso ya lo reclamó (409 → False), NO enviamos. Así nunca se
                # duplica, aunque haya más de un programador corriendo.
                claimed = await repo.mark_reminder_sent(int(a["id"]), tipo)
                if not claimed:
                    continue
                try:
                    await evo.send_text(sid, _mensaje(tipo, nombre, dt_local))
                    log.info(
                        "recordatorio enviado",
                        extra={"appointment": a["id"], "tipo": tipo},
                    )
                except Exception as exc:  # noqa: BLE001
                    log.warning(
                        "recordatorio falló",
                        extra={"appointment": a["id"], "tipo": tipo, "error": str(exc)},
                    )


async def sync_contactos() -> None:
    """Backfill del pushName (nombre que el contacto se puso) desde la agenda de
    Evolution, para chats que no hayan mensajeado desde que capturamos el pushName.

    Nota: la API de Evolution NO expone el nombre que Lily guardó en su teléfono
    (solo `pushName`). El nombre curado por Lily se edita desde el panel y vive en
    `nombre_guardado`; aquí NO lo tocamos.
    """
    repo = get_repository()
    evo = get_evolution()
    contactos = await evo.find_contacts()
    n = 0
    for c in contactos:
        jid = c.get("remoteJid") or c.get("id") or ""
        pushname = c.get("pushName")
        if not jid or not pushname or not isinstance(pushname, str):
            continue
        if not jid.endswith("@s.whatsapp.net") and not jid.endswith("@c.us"):
            continue  # ignora grupos/@lid/broadcast
        numero = jid.split("@", 1)[0]
        await repo.upsert_contacto(numero, pushname=pushname.strip())
        n += 1
    if n:
        log.info("pushNames sincronizados", extra={"n": n})


async def run_scheduler() -> None:
    """Loop principal del programador. Se cancela en el shutdown."""
    log.info("scheduler iniciado (recordatorios + sync de contactos)")
    loops = 0
    while True:
        try:
            await tick_reminders()
            if loops % CONTACTOS_CADA == 0:
                await sync_contactos()
        except asyncio.CancelledError:
            log.info("scheduler detenido")
            raise
        except Exception as exc:  # noqa: BLE001
            log.error("scheduler tick falló", extra={"error": str(exc)})
        loops += 1
        await asyncio.sleep(REMINDER_TICK_S)
