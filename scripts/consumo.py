"""Monitor de consumo (tokens + costo) de Sofía — Pro vs vieja.

Fuente: tabla `sofia_turn_logs` en Supabase, donde CADA turno guarda su `cost_usd`,
tokens y modelo (lo calcula el código en `observability/costs.py`). Es el gasto que
mide nuestra app; la fuente de verdad de FACTURACIÓN es console.anthropic.com → Usage.

Pro vs vieja: los turnos de Sofía Pro llevan metadata.arquitectura == 'sofia_pro_agente'.

Uso:
    uv run python scripts/consumo.py            # hoy + últimos 7 días + total
    uv run python scripts/consumo.py --today    # solo hoy
    uv run python scripts/consumo.py --days 30   # ventana de N días
    uv run python scripts/consumo.py --watch     # refresca cada 30s (Ctrl-C para salir)
"""

from __future__ import annotations

import json
import sys
import time
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

TZ = ZoneInfo("America/Monterrey")
ENV_PATH = Path(__file__).resolve().parent.parent / ".env"


def _env(key: str) -> str:
    for ln in ENV_PATH.read_text().splitlines():
        if ln.startswith(key + "="):
            return ln.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


SUPA_URL = _env("SUPABASE_URL")
SUPA_KEY = _env("SUPABASE_SERVICE_KEY")


def _fetch(since_iso: str | None) -> list[dict]:
    """Trae turn_logs (paginado) desde `since_iso` (o todo)."""
    rows: list[dict] = []
    offset = 0
    page = 1000
    while True:
        params = {
            "select": "cost_usd,tokens_input,tokens_output,tokens_cached,model_used,created_at,metadata,session_id",
            "order": "created_at.desc",
            "limit": str(page),
            "offset": str(offset),
        }
        if since_iso:
            params["created_at"] = f"gte.{since_iso}"
        url = f"{SUPA_URL}/rest/v1/sofia_turn_logs?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(
            url, headers={"apikey": SUPA_KEY, "Authorization": f"Bearer {SUPA_KEY}"}
        )
        batch = json.load(urllib.request.urlopen(req, timeout=30))
        rows.extend(batch)
        if len(batch) < page:
            break
        offset += page
    return rows


def _es_pro(r: dict) -> bool:
    md = r.get("metadata") or {}
    if isinstance(md, dict) and md.get("arquitectura") == "sofia_pro_agente":
        return True
    return False


def _agg(rows: list[dict]) -> dict:
    a = {
        "turnos": 0, "costo": 0.0, "tok_in": 0, "tok_out": 0, "tok_cache": 0,
        "modelos": defaultdict(lambda: {"turnos": 0, "costo": 0.0}),
        "sesiones": set(),
    }
    for r in rows:
        a["turnos"] += 1
        a["costo"] += float(r.get("cost_usd") or 0)
        a["tok_in"] += int(r.get("tokens_input") or 0)
        a["tok_out"] += int(r.get("tokens_output") or 0)
        a["tok_cache"] += int(r.get("tokens_cached") or 0)
        a["sesiones"].add(r.get("session_id"))
        m = r.get("model_used") or "?"
        a["modelos"][m]["turnos"] += 1
        a["modelos"][m]["costo"] += float(r.get("cost_usd") or 0)
    return a


def _print_block(titulo: str, rows: list[dict]) -> None:
    pro = [r for r in rows if _es_pro(r)]
    vieja = [r for r in rows if not _es_pro(r)]
    print(f"\n━━ {titulo} ━━")
    for nombre, rs in (("🆕 PRO  ", pro), ("📦 vieja", vieja)):
        a = _agg(rs)
        if a["turnos"] == 0:
            print(f"  {nombre}: (sin turnos)")
            continue
        print(
            f"  {nombre}: ${a['costo']:.4f}  |  {a['turnos']} turnos  |  "
            f"{len(a['sesiones'])} convos  |  in {a['tok_in']:,} · out {a['tok_out']:,} · cache {a['tok_cache']:,}"
        )
        for m, mv in sorted(a["modelos"].items(), key=lambda x: -x[1]["costo"]):
            print(f"        └ {m}: ${mv['costo']:.4f} ({mv['turnos']} turnos)")
    total = sum(float(r.get("cost_usd") or 0) for r in rows)
    print(f"  TOTAL {titulo.lower()}: ${total:.4f}")


def run_once(dias: int, solo_hoy: bool) -> None:
    ahora = datetime.now(TZ)
    inicio_hoy = ahora.replace(hour=0, minute=0, second=0, microsecond=0)
    print(f"\n=== Consumo Sofía · {ahora:%Y-%m-%d %H:%M} (Monterrey) ===")
    print("(gasto calculado por la app; facturación real: console.anthropic.com → Usage)")

    if solo_hoy:
        _print_block("HOY", _fetch(inicio_hoy.astimezone(ZoneInfo("UTC")).isoformat()))
        return

    rows_ventana = _fetch((ahora - timedelta(days=dias)).astimezone(ZoneInfo("UTC")).isoformat())
    hoy_rows = [r for r in rows_ventana if datetime.fromisoformat(r["created_at"].replace("Z", "+00:00")).astimezone(TZ) >= inicio_hoy]
    _print_block("HOY", hoy_rows)
    _print_block(f"ÚLTIMOS {dias} DÍAS", rows_ventana)
    # total histórico
    _print_block("TOTAL HISTÓRICO", _fetch(None))


def main() -> int:
    if not SUPA_URL or not SUPA_KEY:
        print("ERROR: faltan SUPABASE_URL / SUPABASE_SERVICE_KEY en .env", file=sys.stderr)
        return 2
    args = sys.argv[1:]
    solo_hoy = "--today" in args
    watch = "--watch" in args
    dias = 7
    if "--days" in args:
        dias = int(args[args.index("--days") + 1])
    if watch:
        try:
            while True:
                run_once(dias, solo_hoy)
                time.sleep(30)
        except KeyboardInterrupt:
            print("\nbye")
    else:
        run_once(dias, solo_hoy)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
