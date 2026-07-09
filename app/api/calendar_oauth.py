"""'Conectar con Google' para Lily (OAuth de Google Calendar).

Flujo:
1. Lily abre  /calendar/conectar  → la mandamos a la pantalla de permiso de Google.
2. Da "Permitir" → Google regresa a  /calendar/google/callback?code=...
3. Canjeamos el código por un refresh_token y lo guardamos (tabla
   google_calendar_oauth). A partir de ahí Sofía escribe las citas en SU calendario.

Requiere GOOGLE_OAUTH_CLIENT_ID / _SECRET (credencial de tipo "Web application" de
Google Cloud) y GOOGLE_OAUTH_REDIRECT_URI apuntando a este callback.
"""

from __future__ import annotations

import logging
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Query
from fastapi.responses import HTMLResponse, RedirectResponse

from app.config import get_settings
from app.core.repository import get_repository

log = logging.getLogger(__name__)

router = APIRouter(tags=["calendar-oauth"])

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO = "https://www.googleapis.com/oauth2/v2/userinfo"
CALENDAR_SCOPE = "https://www.googleapis.com/auth/calendar"


def _pagina(titulo: str, detalle: str, ok: bool = True) -> HTMLResponse:
    color = "#0a7d33" if ok else "#b00020"
    html = f"""<!doctype html><html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Maple Collège</title></head>
<body style="font-family:system-ui,-apple-system,sans-serif;max-width:520px;margin:12vh auto;padding:0 24px;text-align:center;color:#222">
<div style="font-size:52px">{'🍁' if ok else '⚠️'}</div>
<h1 style="color:{color};font-size:22px">{titulo}</h1>
<p style="color:#555;line-height:1.5">{detalle}</p>
</body></html>"""
    return HTMLResponse(html)


@router.get("/calendar/conectar")
async def conectar() -> RedirectResponse:
    """Link que abre Lily: la lleva a la pantalla de permiso de Google."""
    s = get_settings()
    params = {
        "client_id": s.google_oauth_client_id,
        "redirect_uri": s.google_oauth_redirect_uri,
        "response_type": "code",
        "scope": CALENDAR_SCOPE,
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "true",
    }
    return RedirectResponse(f"{GOOGLE_AUTH_URL}?{urlencode(params)}")


@router.get("/calendar/google/callback")
async def callback(
    code: str | None = Query(default=None),
    error: str | None = Query(default=None),
) -> HTMLResponse:
    """Google regresa aquí tras el permiso. Guardamos el refresh_token de Lily."""
    if error or not code:
        return _pagina(
            "No se pudo conectar",
            "Parece que no se otorgó el permiso. Puedes intentar de nuevo con el mismo link.",
            ok=False,
        )
    s = get_settings()
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(
            GOOGLE_TOKEN_URL,
            data={
                "code": code,
                "client_id": s.google_oauth_client_id,
                "client_secret": s.google_oauth_client_secret,
                "redirect_uri": s.google_oauth_redirect_uri,
                "grant_type": "authorization_code",
            },
        )
        if resp.status_code >= 400:
            log.error("oauth token exchange failed", extra={"body": resp.text[:200]})
            return _pagina("No se pudo conectar", "Hubo un error con Google. Intenta de nuevo.", ok=False)
        tok = resp.json()
        refresh = tok.get("refresh_token")
        access = tok.get("access_token")
        email = ""
        try:
            u = await client.get(GOOGLE_USERINFO, headers={"Authorization": f"Bearer {access}"})
            email = (u.json() or {}).get("email", "")
        except Exception:  # noqa: BLE001
            pass

    if not refresh:
        return _pagina(
            "Casi listo",
            "Google no envió un permiso permanente. Vuelve a abrir el link y asegúrate de dar 'Permitir'.",
            ok=False,
        )

    # calendar_id = 'primary' = el calendario principal de Lily (el que ya usa).
    await get_repository().guardar_oauth_google(refresh, "primary", email)
    log.info("google calendar conectado (OAuth)", extra={"email": email})
    return _pagina(
        "¡Listo! Tu calendario quedó conectado ✅",
        f"A partir de ahora las citas de Sofía aparecerán en tu Google Calendar"
        f"{' (' + email + ')' if email else ''}. Ya puedes cerrar esta ventana.",
    )
