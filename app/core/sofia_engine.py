"""Despachador de motor: elige Sonnet (Anthropic) o gpt-4o-mini (OpenAI).

Según `settings.sofia_engine`. Por defecto 'anthropic' → la Sofía aprobada corre
idéntica. Un servicio con SOFIA_ENGINE=openai usa el motor gemelo de OpenAI. El
webhook llama a `procesar_turno_sofia` sin saber cuál motor está detrás.
"""

from __future__ import annotations

import logging

from app.config import get_settings
from app.core.agente import AgenteResult, procesar_turno_agente
from app.core.state import Canal

log = logging.getLogger(__name__)


async def procesar_turno_sofia(
    *,
    mensaje: str,
    session_id: str,
    canal: Canal,
    tester: bool = False,
) -> AgenteResult:
    if get_settings().sofia_engine == "openai":
        from app.core.agente_openai import procesar_turno_openai

        return await procesar_turno_openai(
            mensaje=mensaje, session_id=session_id, canal=canal, tester=tester
        )
    return await procesar_turno_agente(
        mensaje=mensaje, session_id=session_id, canal=canal, tester=tester
    )
