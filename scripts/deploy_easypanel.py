"""Deploy de Sofía Pro a un servicio NUEVO en EasyPanel (paralelo a la vieja).

Crea/actualiza el servicio `sofia-pro` en el proyecto EasyPanel, inyecta las env
(MISMA Supabase que la vieja, pero ANTHROPIC_MODEL_PRINCIPAL=claude-sonnet-4-6) y
dispara el deploy. Idempotente: si el servicio ya existe, solo actualiza env + redeploya.

Uso:
    EASYPANEL_URL=https://<panel>.easypanel.host \
    EASYPANEL_API_TOKEN=<token> \
    uv run python scripts/deploy_easypanel.py

Requisitos (env o .env):
    EASYPANEL_URL, EASYPANEL_API_TOKEN  → credenciales del panel.
    Las env del servicio se leen del .env local de este repo (se reescribe el modelo).

NOTA: EasyPanel usa una API tRPC; la forma EXACTA del payload (source git vs image vs
dockerfile, nombres de procedimientos) depende de cómo se creó la vieja `sofia-v3`.
Este script asume build desde Git con Dockerfile. Ajusta `SOURCE`/`BUILD` abajo para
que coincida con el patrón de la vieja antes de correrlo en serio.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import httpx

# ------------------------------------------------------------------
# Configuración del servicio nuevo
# ------------------------------------------------------------------
PROJECT_NAME = os.getenv("EASYPANEL_PROJECT", "maple-v3")
SERVICE_NAME = os.getenv("EASYPANEL_SERVICE", "sofia-pro")
DOMAIN = os.getenv("EASYPANEL_DOMAIN", "sofia-pro.cxjnjn.easypanel.host")

# Source: build desde Git con el Dockerfile del repo. Ajustar al patrón de la vieja.
GIT_REPO = os.getenv("DEPLOY_GIT_REPO", "")  # ej. https://github.com/<org>/sofia-pro
GIT_REF = os.getenv("DEPLOY_GIT_REF", "main")

# Env vars que NO se copian del .env local (secretos de panel / no aplican).
ENV_SKIP_KEYS = {"EASYPANEL_URL", "EASYPANEL_API_TOKEN", "EASYPANEL_PROJECT", "EASYPANEL_SERVICE"}

REPO_ROOT = Path(__file__).resolve().parent.parent


def _read_local_env() -> dict[str, str]:
    """Lee el .env del repo y fuerza el modelo de Sofía Pro a Sonnet."""
    env: dict[str, str] = {}
    env_path = REPO_ROOT / ".env"
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            if key in ENV_SKIP_KEYS:
                continue
            env[key] = val.strip()
    # Sofía Pro es model-driven con Sonnet (decisión Oscar).
    env["ANTHROPIC_MODEL_PRINCIPAL"] = "claude-sonnet-4-6"
    env["ENV"] = "production"
    return env


def _env_to_blob(env: dict[str, str]) -> str:
    return "\n".join(f"{k}={v}" for k, v in env.items())


def _trpc(client: httpx.Client, base: str, procedure: str, payload: dict) -> httpx.Response:
    """Llama un procedimiento tRPC de EasyPanel (mutation)."""
    url = f"{base.rstrip('/')}/api/trpc/{procedure}"
    return client.post(url, json={"json": payload})


def main() -> int:
    base = os.getenv("EASYPANEL_URL", "")
    token = os.getenv("EASYPANEL_API_TOKEN", "")
    if not base or not token:
        print("ERROR: faltan EASYPANEL_URL y/o EASYPANEL_API_TOKEN en el entorno.", file=sys.stderr)
        return 2

    env = _read_local_env()
    if not env.get("ANTHROPIC_API_KEY") or not env.get("SUPABASE_URL"):
        print("ERROR: el .env local no tiene ANTHROPIC_API_KEY / SUPABASE_URL.", file=sys.stderr)
        return 2

    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    with httpx.Client(headers=headers, timeout=60.0) as client:
        # 1. Crear el servicio (idempotente: si existe, EasyPanel devuelve error que ignoramos).
        create_payload: dict = {
            "projectName": PROJECT_NAME,
            "serviceName": SERVICE_NAME,
        }
        if GIT_REPO:
            create_payload["source"] = {"type": "git", "repo": GIT_REPO, "ref": GIT_REF}
            create_payload["build"] = {"type": "dockerfile", "file": "Dockerfile"}
        print(f"→ createService {PROJECT_NAME}/{SERVICE_NAME} …")
        r = _trpc(client, base, "services.app.createService", create_payload)
        if r.status_code >= 400 and "already" not in r.text.lower():
            print(f"  createService: HTTP {r.status_code} — {r.text[:300]}")
        else:
            print(f"  createService OK ({r.status_code})")

        # 2. Inyectar env vars.
        print("→ updateEnv …")
        r = _trpc(
            client, base, "services.app.updateEnv",
            {"projectName": PROJECT_NAME, "serviceName": SERVICE_NAME, "env": _env_to_blob(env)},
        )
        print(f"  updateEnv: HTTP {r.status_code} — {r.text[:200]}")

        # 3. Dominio + puerto (8000).
        print("→ updateDomains …")
        r = _trpc(
            client, base, "services.app.updateDomains",
            {
                "projectName": PROJECT_NAME,
                "serviceName": SERVICE_NAME,
                "domains": [{"host": DOMAIN, "https": True, "port": 8000}],
            },
        )
        print(f"  updateDomains: HTTP {r.status_code} — {r.text[:200]}")

        # 4. Deploy.
        print("→ deployService …")
        r = _trpc(
            client, base, "services.app.deployService",
            {"projectName": PROJECT_NAME, "serviceName": SERVICE_NAME},
        )
        print(f"  deployService: HTTP {r.status_code} — {r.text[:200]}")

    print(f"\nListo. URL esperada: https://{DOMAIN}/chat")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
