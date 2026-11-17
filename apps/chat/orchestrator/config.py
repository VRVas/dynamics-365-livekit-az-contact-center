"""Configuration for the chat orchestrator.

All settings come from environment variables (.env via python-dotenv). No secrets in code.
"""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

# Load .env from this module's directory so the app works regardless of the process CWD.
load_dotenv(Path(__file__).with_name(".env"))

# --- Foundry ---
FOUNDRY_PROJECT_ENDPOINT: str = os.environ.get("FOUNDRY_PROJECT_ENDPOINT", "").rstrip("/")
FOUNDRY_MODEL: str = os.environ.get("FOUNDRY_MODEL", "gpt-5.4-mini")

# --- Model behaviour ---
REASONING_EFFORT: str = os.environ.get("GENAI_REASONING_EFFORT", "low")
SERVICE_TIER: str = os.environ.get("GENAI_SERVICE_TIER", "priority")


def require(name: str, value: str) -> str:
    if not value:
        raise RuntimeError(f"Missing required configuration: {name}")
    return value
