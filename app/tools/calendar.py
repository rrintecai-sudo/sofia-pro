"""Tool de Google Calendar para agendar citas de informes.

En Bloque 4 dejamos la interfaz lista. La integración OAuth real se completa
cuando Cecilia / el técnico de Maple nos pasen las credenciales OAuth de
admisiones@maplesaltillo.com (client_id, client_secret, refresh_token).

Mientras tanto, `agendar_cita` se comporta en dos modos:
- Si hay credenciales completas → crea el evento real.
- Si NO hay credenciales → simula y devuelve `EventoSimulado` (lo agenda en
  la DB local sin tocar Google), para que el flujo del orchestrator no se rompa.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

import httpx
import jwt

from app.config import Settings, get_settings

log = logging.getLogger(__name__)

GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_CALENDAR_API = "https://www.googleapis.com/calendar/v3"
GOOGLE_CALENDAR_SCOPE = "https://www.googleapis.com/auth/calendar"


@dataclass(frozen=True)
class EventoAgendado:
    """Resultado de agendar una cita de informes."""

    evento_id: str
    fecha: datetime
    campus: Literal["Campus 1", "Campus 2"]
    url_calendar: str | None
    simulado: bool  # True si no había credenciales OAuth — se "agendó" solo en estado


class CalendarTool:
    """Cliente Google Calendar OAuth."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._access_token: str | None = None
        self._token_expires_at: datetime | None = None

    def is_configured(self) -> bool:
        s = self.settings
        if s.google_service_account_json.strip():
            return True
        return bool(
            s.google_oauth_client_id
            and s.google_oauth_client_secret
            and s.google_oauth_refresh_token
        )

    async def _refresh_access_token(self) -> str:
        # Preferimos cuenta de servicio si está configurada (Lily solo comparte su
        # calendario con el correo de la cuenta — cero fricción del lado de ella).
        sa_json = self.settings.google_service_account_json.strip()
        if sa_json:
            return await self._token_from_service_account(sa_json)
        return await self._token_from_oauth()

    async def _token_from_oauth(self) -> str:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                GOOGLE_TOKEN_URL,
                data={
                    "client_id": self.settings.google_oauth_client_id,
                    "client_secret": self.settings.google_oauth_client_secret,
                    "refresh_token": self.settings.google_oauth_refresh_token,
                    "grant_type": "refresh_token",
                },
            )
        resp.raise_for_status()
        return self._store_token(resp.json())

    async def _token_from_service_account(self, sa_json: str) -> str:
        """Firma un JWT con la llave privada de la cuenta de servicio y lo canjea por
        un access_token. La cuenta debe tener acceso ('Hacer cambios en eventos') al
        calendario indicado en GOOGLE_CALENDAR_ID (Lily lo comparte con su correo)."""
        info = json.loads(sa_json)
        token_uri = info.get("token_uri", GOOGLE_TOKEN_URL)
        now = int(time.time())
        assertion = jwt.encode(
            {
                "iss": info["client_email"],
                "scope": GOOGLE_CALENDAR_SCOPE,
                "aud": token_uri,
                "iat": now,
                "exp": now + 3600,
            },
            info["private_key"],
            algorithm="RS256",
        )
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                token_uri,
                data={
                    "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                    "assertion": assertion,
                },
            )
        resp.raise_for_status()
        return self._store_token(resp.json())

    def _store_token(self, data: dict) -> str:
        self._access_token = data["access_token"]
        self._token_expires_at = datetime.utcnow() + timedelta(
            seconds=int(data.get("expires_in", 3500))
        )
        return self._access_token

    async def _get_token(self) -> str:
        if (
            self._access_token
            and self._token_expires_at
            and datetime.utcnow() < self._token_expires_at
        ):
            return self._access_token
        return await self._refresh_access_token()

    async def agendar_cita(
        self,
        *,
        nombre_papa: str,
        nombre_hijo: str | None,
        nivel: str,
        fecha: datetime,
        duracion_min: int = 60,
        campus: Literal["Campus 1", "Campus 2"] = "Campus 1",
        notas: str | None = None,
    ) -> EventoAgendado:
        """Agenda una cita de informes.

        Si no hay credenciales OAuth, devuelve un EventoAgendado(simulado=True)
        para que el orchestrator continúe el flujo (la cita queda registrada en
        sofia_conversations.estado_capturado, y un humano la replica en Calendar).
        """
        if not self.is_configured():
            log.info(
                "agendar_cita: simulada (sin OAuth)",
                extra={"papa": nombre_papa, "fecha": fecha.isoformat(), "campus": campus},
            )
            return EventoAgendado(
                evento_id=f"sim-{fecha.timestamp():.0f}",
                fecha=fecha,
                campus=campus,
                url_calendar=None,
                simulado=True,
            )

        # Construir evento
        end_time = fecha + timedelta(minutes=duracion_min)
        titulo = f"Cita de informes — {nombre_papa}"
        if nombre_hijo:
            titulo += f" (hijo: {nombre_hijo}, {nivel})"
        descripcion = notas or (
            f"Cita de informes Maple Collège.\n"
            f"Papá: {nombre_papa}\n"
            f"Hijo: {nombre_hijo or '?'}\n"
            f"Nivel buscado: {nivel}\n"
            f"Campus: {campus}\n\n"
            "Generada automáticamente por Sofía 2.0."
        )

        body = {
            "summary": titulo,
            "description": descripcion,
            "location": campus,
            "start": {"dateTime": fecha.isoformat(), "timeZone": "America/Mexico_City"},
            "end": {"dateTime": end_time.isoformat(), "timeZone": "America/Mexico_City"},
        }

        # Red de seguridad: si el calendario falla (token, permisos, Google caído),
        # NO rompemos el cierre de la cita — cae a modo simulado (queda en la
        # plataforma + recordatorios) y un humano la replica. Nunca revienta la
        # conversación por un problema de calendario.
        try:
            token = await self._get_token()
            cal_id = self.settings.google_calendar_id
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.post(
                    f"{GOOGLE_CALENDAR_API}/calendars/{cal_id}/events",
                    headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                    json=body,
                )
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:  # noqa: BLE001
            log.error(
                "agendar_cita: falló Google Calendar, cae a simulado",
                extra={"papa": nombre_papa, "fecha": fecha.isoformat(), "error": str(exc)},
            )
            return EventoAgendado(
                evento_id=f"sim-{fecha.timestamp():.0f}",
                fecha=fecha,
                campus=campus,
                url_calendar=None,
                simulado=True,
            )
        return EventoAgendado(
            evento_id=data["id"],
            fecha=fecha,
            campus=campus,
            url_calendar=data.get("htmlLink"),
            simulado=False,
        )


_singleton: CalendarTool | None = None


def get_calendar_tool() -> CalendarTool:
    global _singleton
    if _singleton is None:
        _singleton = CalendarTool()
    return _singleton
