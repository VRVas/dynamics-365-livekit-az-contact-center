"""One-shot direct-code deployment into the configured Foundry project.

Builds .foundry/direct-code/{metadata.json, agent-code.zip} (flat), uploads via the Foundry
direct-code REST API (CodeAgents/HostedAgents preview), and polls the version until it leaves
`creating`. Reads config from .env (gitignored). Run from this folder with `az login` done.

Not uploaded itself — only the files in ZIP_FILES are zipped. No secrets are hardcoded here.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

import httpx
from dotenv import load_dotenv

HERE = Path(__file__).parent
load_dotenv(HERE / ".env")

PROJECT_ENDPOINT = os.environ["FOUNDRY_PROJECT_ENDPOINT"].rstrip("/")
AGENT_NAME = "moacc-contact-center"
API = "2025-11-15-preview"
FEATURES = "CodeAgents=V1Preview,HostedAgents=V1Preview"
MODEL = os.environ.get("AZURE_AI_MODEL_DEPLOYMENT_NAME") or os.environ.get("FOUNDRY_MODEL", "gpt-5.4-mini")

DC = HERE / ".foundry" / "direct-code"
META_PATH = DC / "metadata.json"
ZIP_PATH = DC / "agent-code.zip"

# Flat zip contents — only what main.py needs at runtime (no agents.py/app.py/smoke.py/.env/.venv).
ZIP_FILES = [
    "main.py", "prompts.py", "case_tools.py", "runtime_config.py", "deployment.json",
    "system_prompt.md", "knowledge.md", "profiles.json", "email_tool.py", "config.py", "requirements.txt",
]


def build_metadata() -> None:
    DC.mkdir(parents=True, exist_ok=True)
    env_vars = {
        "AZURE_AI_MODEL_DEPLOYMENT_NAME": MODEL,
        "ASSIST_BASE_URL": os.environ.get("ASSIST_BASE_URL", ""),
        "ASSIST_INGEST_KEY": os.environ.get("ASSIST_INGEST_KEY", ""),
        "ACS_EMAIL_CONNECTION_STRING": os.environ.get("ACS_EMAIL_CONNECTION_STRING", ""),
        "ACS_EMAIL_SENDER": os.environ.get("ACS_EMAIL_SENDER", ""),
    }
    meta = {
        "description": "Configurable service coordinator with optional grounded guidance, CRM and correspondence.",
        "definition": {
            "kind": "hosted",
            "protocol_versions": [{"protocol": "responses", "version": "1.0.0"}],
            "cpu": "1",
            "memory": "2Gi",
            "environment_variables": env_vars,
            "code_configuration": {
                "runtime": "python_3_13",
                "entry_point": ["python", "main.py"],
                "dependency_resolution": "remote_build",
            },
        },
    }
    META_PATH.write_text(json.dumps(meta, indent=2), encoding="utf-8")


def build_zip() -> str:
    from runtime_config import load_content, load_deployment, load_profiles
    load_deployment()
    load_content()
    load_profiles()
    DC.mkdir(parents=True, exist_ok=True)
    if ZIP_PATH.exists():
        ZIP_PATH.unlink()
    with ZipFile(ZIP_PATH, "w", ZIP_DEFLATED) as zf:
        for name in ZIP_FILES:
            p = HERE / name
            if name == "profiles.json" and not p.exists():
                continue
            if not p.exists():
                raise SystemExit(f"missing file for zip: {name}")
            zf.write(p, name)  # flat at root
    return hashlib.sha256(ZIP_PATH.read_bytes()).hexdigest()


def get_token() -> str:
    out = subprocess.run(
        ["az", "account", "get-access-token", "--resource", "https://ai.azure.com", "--query", "accessToken", "-o", "tsv"],
        capture_output=True, text=True, shell=True,
    )
    if out.returncode != 0:
        raise SystemExit(f"token error: {out.stderr.strip()}")
    return out.stdout.strip()


def _resolve_version(body: dict) -> str | None:
    for k in ("version", "agentVersion", "versionId", "name", "id"):
        v = body.get(k)
        if v:
            return str(v)
    return None


def main() -> None:
    build_metadata()
    sha = build_zip()
    print(f"zip sha256={sha}  size={ZIP_PATH.stat().st_size} bytes")
    token = get_token()
    base = {"Authorization": f"Bearer {token}", "Accept": "application/json", "Foundry-Features": FEATURES}

    with httpx.Client(timeout=300) as c:
        g = c.get(f"{PROJECT_ENDPOINT}/agents/{AGENT_NAME}?api-version={API}", headers=base)
        exists = g.status_code == 200
        print(f"existence check: {g.status_code} (exists={exists})")

        files = {
            "metadata": ("metadata.json", META_PATH.read_bytes(), "application/json"),
            "code": (f"{AGENT_NAME}.zip", ZIP_PATH.read_bytes(), "application/zip"),
        }
        if exists:
            url = f"{PROJECT_ENDPOINT}/agents/{AGENT_NAME}/versions?api-version={API}"
            headers = {**base, "x-ms-code-zip-sha256": sha}
        else:
            url = f"{PROJECT_ENDPOINT}/agents?api-version={API}"
            headers = {**base, "x-ms-agent-name": AGENT_NAME, "x-ms-code-zip-sha256": sha}

        r = c.post(url, headers=headers, files=files)
        print(f"upload: HTTP {r.status_code}")
        print(r.text[:2500])
        r.raise_for_status()
        body = r.json()
        version = _resolve_version(body)
        if not version:
            lv = c.get(f"{PROJECT_ENDPOINT}/agents/{AGENT_NAME}/versions?api-version={API}", headers=base)
            arr = lv.json().get("value") or lv.json().get("versions") or []
            if arr:
                version = _resolve_version(arr[-1])
        print(f"version={version}")
        if not version:
            return

        for i in range(80):
            pv = c.get(f"{PROJECT_ENDPOINT}/agents/{AGENT_NAME}/versions/{version}?api-version={API}", headers=base)
            st = pv.json()
            status = str(st.get("status") or st.get("provisioningState") or st.get("state") or "").lower()
            print(f"poll {i}: status={status!r}")
            if status and status != "creating":
                print(json.dumps(st, indent=2)[:3500])
                break
            time.sleep(15)


if __name__ == "__main__":
    main()
