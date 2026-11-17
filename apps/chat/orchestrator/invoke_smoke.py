"""One-shot cloud smoke test for the deployed Foundry hosted agent.

Connects to the Foundry-hosted `moacc-contact-center` agent (responses protocol) via the
Agent Framework `FoundryAgent` client and runs a single query. Auth via AzureCliCredential.

Usage: python invoke_smoke.py "Hello"
"""
from __future__ import annotations

import asyncio
import os
import sys

from agent_framework.foundry import FoundryAgent
from azure.identity import AzureCliCredential

from config import FOUNDRY_PROJECT_ENDPOINT, require

PROJECT = FOUNDRY_PROJECT_ENDPOINT
AGENT_NAME = os.environ.get("FOUNDRY_AGENT_NAME", "moacc-contact-center")


async def main() -> None:
    query = sys.argv[1] if len(sys.argv) > 1 else "Hello"
    agent = FoundryAgent(
        project_endpoint=require("FOUNDRY_PROJECT_ENDPOINT", PROJECT),
        agent_name=AGENT_NAME,
        credential=AzureCliCredential(),
        allow_preview=True,
    )
    print(f"USER: {query}\nInvoking deployed hosted agent (cold start may take ~30-60s)...", flush=True)
    resp = await agent.run(query)
    text = getattr(resp, "text", None) or str(resp)
    print("\nAGENT:", text)


if __name__ == "__main__":
    asyncio.run(main())
