"""Persistencia de conversaciones y mensajes vía PostgREST con service_role.

Decisión: en lugar de asyncpg directo, usamos PostgREST con HTTP a Supabase.
Pros: ya tenemos service_role; no necesitamos DB password; respeta RLS si la
desactivamos manualmente; mismo path en dev y prod.

Esquema en migrations/001_init_schema.sql:
- sofia_conversations(session_id PK, canal, identificador, estado_capturado JSONB, ...)
- sofia_messages(id PK, session_id FK, role, content, ...)
- sofia_turn_logs(id PK, session_id, turn_number, ...)
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import httpx

from app.config import Settings, get_settings
from app.core.state import (
    Canal,
    EstadoCapturado,
    EstadoConversacion,
    FaseJourney,
    Modo,
)

log = logging.getLogger(__name__)


class Repository:
    """Wrapper async sobre PostgREST con service_role."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._client: httpx.AsyncClient | None = None

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=f"{self.settings.supabase_url}/rest/v1",
                headers={
                    "apikey": self.settings.supabase_service_key,
                    "Authorization": f"Bearer {self.settings.supabase_service_key}",
                    "Content-Type": "application/json",
                    "Prefer": "return=representation",
                },
                timeout=15.0,
            )
        return self._client

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    # ----------------------------------------------------------------
    # sofia_conversations
    # ----------------------------------------------------------------

    async def get_conversation(self, session_id: str) -> EstadoConversacion | None:
        """Devuelve el estado de la sesión o None si no existe."""
        resp = await self.client.get(
            "/sofia_conversations",
            params={"session_id": f"eq.{session_id}", "select": "*"},
        )
        resp.raise_for_status()
        rows = resp.json()
        if not rows:
            return None
        return _row_to_estado(rows[0])

    async def upsert_conversation(self, estado: EstadoConversacion) -> None:
        """Inserta o actualiza la sesión completa."""
        payload = _estado_to_row(estado)
        resp = await self.client.post(
            "/sofia_conversations",
            params={"on_conflict": "session_id"},
            headers={"Prefer": "resolution=merge-duplicates,return=minimal"},
            json=payload,
        )
        if resp.status_code >= 400:
            log.error(
                "upsert_conversation failed",
                extra={"status": resp.status_code, "body": resp.text[:300]},
            )
            resp.raise_for_status()

    async def ensure_conversation(self, session_id: str, canal: Canal) -> None:
        """Crea la conversación si no existe (para poder guardar mensajes)."""
        if await self.get_conversation(session_id) is None:
            await self.upsert_conversation(EstadoConversacion.nueva(session_id))

    # ----------------------------------------------------------------
    # Handoff bot ↔ humano (Bloque WhatsApp inbox)
    # ----------------------------------------------------------------

    async def is_bot_active(self, session_id: str) -> bool:
        """¿El bot debe responder en esta conversación? Default True (conversación
        nueva o sin bandera). False = la atiende un humano (Lily)."""
        resp = await self.client.get(
            "/sofia_conversations",
            params={"session_id": f"eq.{session_id}", "select": "bot_activo"},
        )
        resp.raise_for_status()
        rows = resp.json()
        if not rows:
            return True
        return bool(rows[0].get("bot_activo", True))

    async def hay_identificador_humano(self, identificadores: list[str]) -> bool:
        """¿Alguno de estos identificadores (número o <lid>@lid) está en la lista
        'solo humano'? Se usa en el webhook para NO responder a contactos que
        atiende un humano, robusto al formato (@lid vs número)."""
        ids = [i for i in identificadores if i]
        if not ids:
            return False
        # PostgREST in.(...): los identificadores son dígitos o '<lid>@lid' (sin
        # comas/paréntesis), así que van tal cual; httpx codifica el '@'.
        lista = ",".join(f'"{i}"' for i in ids)
        resp = await self.client.get(
            "/whatsapp_humano",
            params={"identificador": f"in.({lista})", "select": "identificador", "limit": "1"},
        )
        resp.raise_for_status()
        return bool(resp.json())

    async def set_bot_active(
        self, session_id: str, active: bool, *, atendido_por: str | None = None
    ) -> bool:
        """Enciende/apaga el bot para una conversación (handoff). `atendido_por`
        opcional: 'bot' | 'humano'."""
        payload: dict[str, Any] = {"bot_activo": active}
        if atendido_por:
            payload["atendido_por"] = atendido_por
        resp = await self.client.patch(
            "/sofia_conversations",
            params={"session_id": f"eq.{session_id}"},
            json=payload,
        )
        if resp.status_code >= 400:
            log.error(
                "set_bot_active failed",
                extra={"status": resp.status_code, "body": resp.text[:200]},
            )
            return False
        return True

    # ----------------------------------------------------------------
    # Contactos de WhatsApp (nombres para la bandeja del panel)
    # ----------------------------------------------------------------

    async def upsert_contacto(
        self,
        identificador: str,
        *,
        pushname: str | None = None,
        nombre_guardado: str | None = None,
    ) -> None:
        """Guarda/actualiza el nombre de un contacto de WhatsApp por número.

        - `pushname`: el nombre que el contacto se puso a sí mismo (viene en cada
          mensaje). - `nombre_guardado`: el nombre en la agenda del teléfono de la
          línea (lo que Lily guardó), obtenido vía Evolution findContacts.
        Solo escribe los campos provistos (upsert merge), sin pisar el otro.
        """
        if not identificador:
            return
        payload: dict[str, Any] = {
            "identificador": identificador,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        if pushname:
            payload["pushname"] = pushname
        if nombre_guardado:
            payload["nombre_guardado"] = nombre_guardado
        if len(payload) == 2:  # solo identificador + updated_at → nada que guardar
            return
        try:
            resp = await self.client.post(
                "/whatsapp_contactos",
                params={"on_conflict": "identificador"},
                headers={"Prefer": "resolution=merge-duplicates"},
                json=payload,
            )
            if resp.status_code >= 400:
                log.warning(
                    "upsert_contacto failed",
                    extra={"status": resp.status_code, "body": resp.text[:200]},
                )
        except Exception as exc:  # nunca romper el flujo del webhook por esto
            log.warning("upsert_contacto error", extra={"error": str(exc)})

    # ----------------------------------------------------------------
    # Recordatorios de cita
    # ----------------------------------------------------------------

    async def fetch_upcoming_appointments(
        self, now_iso: str, horizon_iso: str
    ) -> list[dict[str, Any]]:
        """Citas próximas (status pendiente/confirmada) entre ahora y el horizonte,
        con los datos del lead (nombre, session, teléfono) para el recordatorio."""
        resp = await self.client.get(
            "/appointments",
            params={
                "select": "id,fecha_hora,status,leads(parent_name,conversation_session_id,parent_phone)",
                "status": "in.(pendiente,confirmada)",
                "and": f"(fecha_hora.gte.{now_iso},fecha_hora.lte.{horizon_iso})",
                "order": "fecha_hora.asc",
            },
        )
        resp.raise_for_status()
        return list(resp.json())

    async def fetch_sent_reminders(self, appt_ids: list[int]) -> set[tuple[int, str]]:
        """Qué recordatorios (appointment_id, tipo) ya se enviaron, para no repetir."""
        if not appt_ids:
            return set()
        lista = ",".join(str(i) for i in appt_ids)
        resp = await self.client.get(
            "/appointment_reminders",
            params={"appointment_id": f"in.({lista})", "select": "appointment_id,tipo"},
        )
        resp.raise_for_status()
        return {(int(r["appointment_id"]), r["tipo"]) for r in resp.json()}

    async def mark_reminder_sent(self, appointment_id: int, tipo: str) -> bool:
        """Registra que se envió un recordatorio. 409 (ya existe) = no re-enviar."""
        resp = await self.client.post(
            "/appointment_reminders",
            json={"appointment_id": appointment_id, "tipo": tipo},
            headers={"Prefer": "return=minimal"},
        )
        return resp.status_code < 400

    # ----------------------------------------------------------------
    # Mensajes que envió el BOT (para distinguirlos de respuestas manuales de Lily)
    # ----------------------------------------------------------------

    async def registrar_mensaje_bot(self, message_id: str) -> None:
        """Registra el id de un mensaje que envió el bot (via Evolution). Sirve para
        que el webhook distinga un fromMe del bot de una respuesta manual de Lily."""
        if not message_id:
            return
        try:
            await self.client.post(
                "/whatsapp_bot_messages",
                json={"message_id": message_id},
                headers={"Prefer": "resolution=merge-duplicates,return=minimal"},
            )
        except Exception as exc:  # nunca romper un envío por esto
            log.warning("registrar_mensaje_bot error", extra={"error": str(exc)})

    async def texto_ultimo_asistente(self, session_id: str) -> str:
        """Contenido del último mensaje del asistente en la conversación (para el
        seguro anti-carrera del auto-handoff)."""
        try:
            resp = await self.client.get(
                "/sofia_messages",
                params={
                    "session_id": f"eq.{session_id}",
                    "role": "eq.assistant",
                    "select": "content",
                    "order": "id.desc",
                    "limit": "1",
                },
            )
            resp.raise_for_status()
            rows = resp.json()
            return (rows[0]["content"] if rows else "") or ""
        except Exception:  # noqa: BLE001
            return ""

    async def es_mensaje_del_bot(self, message_id: str) -> bool:
        """¿Este id fue enviado por el bot? (True = del bot; False = de Lily a mano)."""
        if not message_id:
            return False
        try:
            resp = await self.client.get(
                "/whatsapp_bot_messages",
                params={"message_id": f"eq.{message_id}", "select": "message_id", "limit": "1"},
            )
            resp.raise_for_status()
            return bool(resp.json())
        except Exception as exc:
            log.warning("es_mensaje_del_bot error", extra={"error": str(exc)})
            # Ante la duda, asumimos que ES del bot (no apagar el bot por error).
            return True

    # ----------------------------------------------------------------
    # sofia_messages
    # ----------------------------------------------------------------

    async def insert_message(
        self,
        session_id: str,
        role: str,  # 'user' | 'assistant' | 'system'
        content: str,
        *,
        tipo: str = "texto",
        metadata: dict[str, Any] | None = None,
        tokens_input: int | None = None,
        tokens_output: int | None = None,
        cost_usd: Decimal | None = None,
        model_used: str | None = None,
        cache_hit: bool = False,
        latency_ms: int | None = None,
    ) -> int:
        """Inserta un mensaje. Devuelve el id."""
        payload: dict[str, Any] = {
            "session_id": session_id,
            "role": role,
            "content": content,
            "tipo": tipo,
            "metadata": metadata or {},
            "cache_hit": cache_hit,
        }
        if tokens_input is not None:
            payload["tokens_input"] = tokens_input
        if tokens_output is not None:
            payload["tokens_output"] = tokens_output
        if cost_usd is not None:
            payload["cost_usd"] = float(cost_usd)
        if model_used:
            payload["model_used"] = model_used
        if latency_ms is not None:
            payload["latency_ms"] = latency_ms

        resp = await self.client.post("/sofia_messages", json=payload)
        if resp.status_code >= 400:
            log.error(
                "insert_message failed",
                extra={"status": resp.status_code, "body": resp.text[:300]},
            )
            resp.raise_for_status()
        rows = resp.json()
        return int(rows[0]["id"])

    async def list_recent_messages(
        self,
        session_id: str,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Últimos N mensajes (más viejos primero, listos para historial LLM)."""
        resp = await self.client.get(
            "/sofia_messages",
            params={
                "session_id": f"eq.{session_id}",
                "select": "role,content,created_at,tipo",
                "order": "id.desc",
                "limit": str(limit),
            },
        )
        resp.raise_for_status()
        rows = resp.json()
        return list(reversed(rows))

    # ----------------------------------------------------------------
    # sofia_turn_logs
    # ----------------------------------------------------------------

    async def insert_turn_log(
        self,
        *,
        session_id: str,
        turn_number: int,
        user_message: str | None = None,
        intent: str | None = None,
        rag_chunks: list[dict[str, Any]] | None = None,
        tools_used: list[str] | None = None,
        prompt_compuesto: str | None = None,
        llm_response: str | None = None,
        validators_passed: dict[str, Any] | None = None,
        validators_failed: dict[str, Any] | None = None,
        final_response: str | None = None,
        regenerations: int = 0,
        tokens_input: int | None = None,
        tokens_output: int | None = None,
        tokens_cached: int | None = None,
        cost_usd: Decimal | None = None,
        latency_ms: int | None = None,
        model_used: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> int:
        payload: dict[str, Any] = {
            "session_id": session_id,
            "turn_number": turn_number,
            "tools_used": tools_used or [],
            "validators_passed": validators_passed or {},
            "validators_failed": validators_failed or {},
            "regenerations": regenerations,
        }
        if metadata is not None:
            payload["metadata"] = metadata
        if user_message is not None:
            payload["user_message"] = user_message
        if intent is not None:
            payload["intent"] = intent
        if rag_chunks is not None:
            payload["rag_chunks"] = rag_chunks
        if prompt_compuesto is not None:
            payload["prompt_compuesto"] = prompt_compuesto
        if llm_response is not None:
            payload["llm_response"] = llm_response
        if final_response is not None:
            payload["final_response"] = final_response
        if tokens_input is not None:
            payload["tokens_input"] = tokens_input
        if tokens_output is not None:
            payload["tokens_output"] = tokens_output
        if tokens_cached is not None:
            payload["tokens_cached"] = tokens_cached
        if cost_usd is not None:
            payload["cost_usd"] = float(cost_usd)
        if latency_ms is not None:
            payload["latency_ms"] = latency_ms
        if model_used:
            payload["model_used"] = model_used

        resp = await self.client.post("/sofia_turn_logs", json=payload)
        if resp.status_code >= 400:
            log.error(
                "insert_turn_log failed",
                extra={"status": resp.status_code, "body": resp.text[:300]},
            )
            resp.raise_for_status()
        rows = resp.json()
        return int(rows[0]["id"])

    async def count_turns(self, session_id: str) -> int:
        """Cuenta cuántos turn_logs hay para esta sesión."""
        resp = await self.client.head(
            "/sofia_turn_logs",
            params={"session_id": f"eq.{session_id}", "select": "id"},
            headers={"Prefer": "count=exact"},
        )
        resp.raise_for_status()
        cr = resp.headers.get("content-range", "0/0")
        # formato '0-N/total' o '*/total'
        total = cr.split("/")[-1]
        return int(total) if total.isdigit() else 0

    # ----------------------------------------------------------------
    # Admin queries (Bloque 5)
    # ----------------------------------------------------------------

    async def list_conversations(
        self,
        *,
        canal: str | None = None,
        since: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Lista conversaciones para el admin dashboard."""
        params: dict[str, str] = {
            "select": "session_id,canal,identificador,fase_journey,agendado,modo,tester,created_at,updated_at",
            "order": "updated_at.desc",
            "limit": str(limit),
        }
        if canal:
            params["canal"] = f"eq.{canal}"
        if since:
            params["updated_at"] = f"gte.{since}"
        resp = await self.client.get("/sofia_conversations", params=params)
        resp.raise_for_status()
        return list(resp.json())

    async def get_conversation_raw(self, session_id: str) -> dict[str, Any] | None:
        resp = await self.client.get(
            "/sofia_conversations",
            params={"session_id": f"eq.{session_id}", "select": "*"},
        )
        resp.raise_for_status()
        rows = resp.json()
        return rows[0] if rows else None

    async def list_all_messages(self, session_id: str) -> list[dict[str, Any]]:
        """Devuelve TODOS los mensajes de la sesión, ordenados cronológicamente."""
        resp = await self.client.get(
            "/sofia_messages",
            params={
                "session_id": f"eq.{session_id}",
                "select": "id,role,content,tipo,metadata,created_at,tokens_input,tokens_output,cost_usd,model_used,cache_hit,latency_ms",
                "order": "id.asc",
            },
        )
        resp.raise_for_status()
        return list(resp.json())

    async def list_turn_logs(self, session_id: str) -> list[dict[str, Any]]:
        resp = await self.client.get(
            "/sofia_turn_logs",
            params={
                "session_id": f"eq.{session_id}",
                "select": "*",
                "order": "turn_number.asc",
            },
        )
        resp.raise_for_status()
        return list(resp.json())

    async def stats(
        self,
        *,
        from_date: str | None = None,
        to_date: str | None = None,
    ) -> dict[str, Any]:
        """Agrega métricas del período. Retorna dict para `/admin/stats`."""
        from collections import Counter
        from decimal import Decimal

        params: dict[str, str] = {"select": "*", "limit": "10000"}
        if from_date:
            params["created_at"] = f"gte.{from_date}"
        if to_date:
            params["created_at"] = f"lte.{to_date}" if not from_date else params["created_at"]
        resp = await self.client.get("/sofia_turn_logs", params=params)
        resp.raise_for_status()
        rows: list[dict[str, Any]] = resp.json()

        total_turns = len(rows)
        total_cost = sum(Decimal(str(r.get("cost_usd") or 0)) for r in rows)
        total_input = sum(int(r.get("tokens_input") or 0) for r in rows)
        total_output = sum(int(r.get("tokens_output") or 0) for r in rows)
        total_cached = sum(int(r.get("tokens_cached") or 0) for r in rows)
        intents = Counter(r.get("intent") for r in rows if r.get("intent"))
        models = Counter(r.get("model_used") for r in rows if r.get("model_used"))

        # Conversaciones únicas + agendadas
        sessions = {r.get("session_id") for r in rows}
        conv_count = len(sessions)

        # Agendados — leer de sofia_conversations
        agendados_resp = await self.client.head(
            "/sofia_conversations",
            params={"agendado": "eq.true", "select": "session_id"},
            headers={"Prefer": "count=exact"},
        )
        agendados_resp.raise_for_status()
        cr = agendados_resp.headers.get("content-range", "0/0")
        agendados_total = int(cr.split("/")[-1]) if cr.split("/")[-1].isdigit() else 0

        avg_cost = float(total_cost / total_turns) if total_turns else 0.0
        cache_ratio = (
            (total_cached / (total_input + total_cached)) if (total_input + total_cached) else 0.0
        )

        return {
            "total_turns": total_turns,
            "total_conversations": conv_count,
            "total_agendados": agendados_total,
            "total_cost_usd": float(total_cost),
            "avg_cost_per_turn_usd": avg_cost,
            "total_tokens_input": total_input,
            "total_tokens_output": total_output,
            "total_tokens_cached": total_cached,
            "cache_ratio": round(cache_ratio, 3),
            "top_intents": intents.most_common(10),
            "models_used": dict(models),
            "from_date": from_date,
            "to_date": to_date,
        }

    async def top_expensive_conversations(self, limit: int = 10) -> list[dict[str, Any]]:
        """Top sesiones por costo acumulado."""
        from collections import defaultdict
        from decimal import Decimal

        resp = await self.client.get(
            "/sofia_turn_logs",
            params={"select": "session_id,cost_usd,turn_number", "limit": "10000"},
        )
        resp.raise_for_status()
        rows = resp.json()
        agg: dict[str, dict[str, Any]] = defaultdict(lambda: {"cost": Decimal(0), "turns": 0})
        for r in rows:
            sid = r.get("session_id")
            if not sid:
                continue
            agg[sid]["cost"] += Decimal(str(r.get("cost_usd") or 0))
            agg[sid]["turns"] += 1
        ranked = sorted(agg.items(), key=lambda x: x[1]["cost"], reverse=True)[:limit]
        return [
            {"session_id": sid, "cost_usd": float(d["cost"]), "turns": d["turns"]}
            for sid, d in ranked
        ]


# ----------------------------------------------------------------
# Helpers de mapeo
# ----------------------------------------------------------------


def _estado_to_row(estado: EstadoConversacion) -> dict[str, Any]:
    return {
        "session_id": estado.session_id,
        "canal": estado.canal.value,
        "identificador": estado.identificador,
        "estado_capturado": estado.estado_capturado.model_dump(mode="json"),
        "frases_usadas": estado.frases_usadas,
        "fase_journey": estado.fase_journey.value,
        "agendado": estado.agendado,
        "fecha_agendado": estado.fecha_agendado.isoformat() if estado.fecha_agendado else None,
        "modo": estado.modo.value,
        "notas_internas": estado.notas_internas,
        "tester": estado.tester,
    }


def _row_to_estado(row: dict[str, Any]) -> EstadoConversacion:
    fase_raw = row.get("fase_journey") or FaseJourney.BIENVENIDA.value
    try:
        fase = FaseJourney(fase_raw)
    except ValueError:
        fase = FaseJourney.BIENVENIDA

    modo_raw = row.get("modo") or Modo.NORMAL.value
    try:
        modo = Modo(modo_raw)
    except ValueError:
        modo = Modo.NORMAL

    return EstadoConversacion(
        session_id=row["session_id"],
        canal=Canal(row["canal"]),
        identificador=row["identificador"],
        estado_capturado=EstadoCapturado.model_validate(row.get("estado_capturado") or {}),
        frases_usadas=list(row.get("frases_usadas") or []),
        fase_journey=fase,
        agendado=bool(row.get("agendado", False)),
        fecha_agendado=row.get("fecha_agendado"),
        modo=modo,
        notas_internas=row.get("notas_internas"),
        tester=bool(row.get("tester", False)),
    )


_singleton: Repository | None = None


def get_repository() -> Repository:
    global _singleton
    if _singleton is None:
        _singleton = Repository()
    return _singleton
