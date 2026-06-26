"""Deploy / redeploy de Sofía Pro en EasyPanel (servicio NUEVO, paralelo a la vieja).

Servicio `sofia-pro` en el proyecto `maple-v3` (mismo panel y misma Supabase que la
vieja `sofia`/`sofia-v3`). Source = GitHub `rrintecai-sudo/sofia-pro` (público) con
build Dockerfile. autoDeploy=False → cada `git push` se publica corriendo este script.

Flujo (idempotente):
  createService → updateSourceGithub → updateBuild → updateEnv → deployService(forceRebuild)

Las env se copian de la vieja (`maple-v3/sofia`) vía inspectService y se cambia
ANTHROPIC_MODEL_PRINCIPAL → claude-sonnet-4-6 (Sofía Pro es model-driven con Sonnet).

Uso:
    cd ../../sofia-maple && set -a && . ./.env.local && set +a   # EASYPANEL_URL + TOKEN
    uv run python scripts/deploy_easypanel.py            # full (crea + configura + deploy)
    uv run python scripts/deploy_easypanel.py --redeploy  # solo redeploy (tras git push)
"""

from __future__ import annotations

import json
import os
import sys
import urllib.parse
import urllib.request

PROJECT = os.getenv("EASYPANEL_PROJECT_PRO", "maple-v3")
SERVICE = os.getenv("EASYPANEL_SERVICE_PRO", "sofia-pro")
OLD_SERVICE = os.getenv("EASYPANEL_SERVICE_OLD", "sofia")  # de quien copiamos env
GH_OWNER = "rrintecai-sudo"
GH_REPO = "sofia-pro"
GH_REF = "main"
DOMAIN = "sofia-pro.cxjnjn.easypanel.host"
MODEL_PRO = "claude-sonnet-4-6"


def _hdr() -> dict[str, str]:
    return {"Authorization": f"Bearer {os.environ['EASYPANEL_API_TOKEN']}", "Content-Type": "application/json"}


def _base() -> str:
    return os.environ["EASYPANEL_URL"].rstrip("/")


def _post(proc: str, payload: dict) -> tuple[int, object]:
    req = urllib.request.Request(
        f"{_base()}/api/trpc/{proc}", data=json.dumps({"json": payload}).encode(), headers=_hdr(), method="POST"
    )
    try:
        r = urllib.request.urlopen(req, timeout=120)
        return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        return e.code, e.read()[:400].decode(errors="replace")


def _get(proc: str, payload: dict) -> object:
    url = f"{_base()}/api/trpc/{proc}?input=" + urllib.parse.quote(json.dumps({"json": payload}))
    req = urllib.request.Request(url, headers=_hdr())
    try:
        return json.load(urllib.request.urlopen(req, timeout=60))
    except urllib.error.HTTPError as e:
        return {"_err": e.code, "_body": e.read()[:300].decode(errors="replace")}


def _env_pro() -> str:
    """Copia la env de la vieja y fuerza el modelo Sonnet."""
    d = _get("services.app.inspectService", {"projectName": PROJECT, "serviceName": OLD_SERVICE})
    env = d.get("json", {}).get("env", "")  # type: ignore[union-attr]
    out = []
    for line in env.splitlines():
        if line.startswith("ANTHROPIC_MODEL_PRINCIPAL="):
            out.append(f"ANTHROPIC_MODEL_PRINCIPAL={MODEL_PRO}")
        else:
            out.append(line)
    return "\n".join(out)


def main() -> int:
    if "EASYPANEL_URL" not in os.environ or "EASYPANEL_API_TOKEN" not in os.environ:
        print("ERROR: exporta EASYPANEL_URL y EASYPANEL_API_TOKEN (de sofia-maple/.env.local).", file=sys.stderr)
        return 2

    redeploy_only = "--redeploy" in sys.argv
    if not redeploy_only:
        print("createService:", _post("services.app.createService", {"projectName": PROJECT, "serviceName": SERVICE}))
        print("updateSourceGithub:", _post("services.app.updateSourceGithub", {
            "projectName": PROJECT, "serviceName": SERVICE,
            "owner": GH_OWNER, "repo": GH_REPO, "ref": GH_REF, "path": "/", "autoDeploy": False,
        }))
        print("updateBuild:", _post("services.app.updateBuild", {
            "projectName": PROJECT, "serviceName": SERVICE, "build": {"type": "dockerfile", "file": "./Dockerfile"},
        }))
        print("updateEnv:", _post("services.app.updateEnv", {
            "projectName": PROJECT, "serviceName": SERVICE, "env": _env_pro(),
        }))
        print("updateDeploy:", _post("services.app.updateDeploy", {
            "projectName": PROJECT, "serviceName": SERVICE, "replicas": 1, "command": None, "zeroDowntime": True,
        }))

    print("deployService:", _post("services.app.deployService", {
        "projectName": PROJECT, "serviceName": SERVICE, "forceRebuild": True,
    }))
    print(f"\nListo. URL: https://{DOMAIN}/chat")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
