"""CLI smoke test for the chat orchestrator.

Runs an explicitly requested check against the configured live Foundry agent.

Usage (from this directory, with .env populated and `az login` done):
    python smoke.py "Hello"
"""
from __future__ import annotations

import asyncio
import sys

from agents import build_workflow


def _event_text(event: object) -> str:
    """Best-effort extraction of human-readable text from a workflow event (refined in the fix loop)."""
    data = getattr(event, "data", None)
    for attr in ("text", "message", "content"):
        val = getattr(data, attr, None)
        if isinstance(val, str) and val:
            return val
    messages = getattr(getattr(data, "agent_response", None), "messages", None)
    if messages:
        return " | ".join(getattr(m, "text", "") for m in messages if getattr(m, "text", ""))
    return ""


async def main() -> None:
    query = sys.argv[1] if len(sys.argv) > 1 else "Hello"
    workflow = build_workflow()

    print(f"\n=== USER: {query}\n")
    result = await workflow.run(query)
    print("EVENT COUNT:", len(result))
    print("\n--- EVENTS ---")
    for ev in result:
        print(" ", type(ev).__name__, "::", repr(ev)[:220])
    print("\n--- OUTPUTS (text) ---")
    for o in result.get_outputs():
        text = getattr(o, "text", None)
        if not text:
            msgs = getattr(o, "messages", None) or []
            text = "\n".join(getattr(m, "text", "") for m in msgs)
        print((text or repr(o))[:3000])
        print("---")
    print("\n--- REQUEST_INFO ---")
    for r in result.get_request_info_events():
        print(repr(r)[:800])


if __name__ == "__main__":
    asyncio.run(main())
