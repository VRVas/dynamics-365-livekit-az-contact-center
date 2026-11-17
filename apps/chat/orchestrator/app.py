"""FastAPI surface for the chat orchestrator.

Bridges the legacy ui_v2 contract (POST /conversation/{id}/stream emitting newline-delimited JSON
envelopes: ["ping",{}], ["start", agent], ["delta", {"content": ...}], ["end", {}], ["error", {...}])
to the service coordinator.

ORCHESTRATOR_TARGET selects the backend:
  - "deployed" (default): the Foundry-hosted `moacc-contact-center` agent (FoundryAgent).
  - "local": the same coordinator built in-process (build_coordinator) — snappier dev, identical logic.

A per-conversation AgentSession gives multi-turn memory. State is in-memory (Cosmos is a later wave).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

import httpx
from agent_framework import AgentSession
from azure.identity import AzureCliCredential
from dotenv import load_dotenv
from fastapi import Body, FastAPI, HTTPException, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from handover import HandoverHub
from case_tools import CONVERSATION, ConversationContext, close_client, invoke
from prompts import CUSTOMER_CONTEXT_INSTRUCTIONS, EMBEDDED_KNOWLEDGE_SHA256, EMBEDDED_KNOWLEDGE_BYTES
from runtime_config import DEPLOYMENT, load_profiles

load_dotenv(Path(__file__).with_name(".env"), override=False)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("orchestrator.app")

PROJECT_ENDPOINT = os.environ["FOUNDRY_PROJECT_ENDPOINT"]
AGENT_NAME = os.environ.get("FOUNDRY_AGENT_NAME", "moacc-contact-center")
TARGET = os.environ.get("ORCHESTRATOR_TARGET", "local").lower()


def _credential():
    """Managed identity in Azure (UAMI via AZURE_CLIENT_ID), Azure CLI locally.

    In Azure Container Apps there is no `az` CLI, so a user-assigned managed identity is used;
    ManagedIdentityCredential MUST take the UAMI client_id (ACA hosts multiple identities).
    Locally, AzureCliCredential reuses `az login` (process_timeout bumped for slow token refresh).
    """
    client_id = os.environ.get("AZURE_CLIENT_ID")
    if client_id:
        from azure.identity import ManagedIdentityCredential

        return ManagedIdentityCredential(client_id=client_id)
    return AzureCliCredential(process_timeout=30)


# --- Agent-assist ingestion: forward chat transcripts to the unified console / Cosmos ---
ASSIST_BASE_URL = os.environ.get("ASSIST_BASE_URL", "").rstrip("/")
ASSIST_INGEST_KEY = os.environ.get("ASSIST_INGEST_KEY", "")
ASSIST_ENABLED = (
    DEPLOYMENT.features.agentAssist
    and os.environ.get("ASSIST_ENABLED", "true").lower() == "true"
    and bool(ASSIST_BASE_URL)
    and bool(ASSIST_INGEST_KEY)
)
_bg_tasks: set[asyncio.Task] = set()


def _fire(coro) -> None:
    """Schedule a best-effort background task, holding a ref so it isn't garbage-collected."""
    task = asyncio.create_task(coro)
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)


async def _emit_to_assist(conversation_id: str, speaker: str, text: str) -> None:
    """Fire-and-forget: forward one turn to the agent-assist ingestion endpoint.

    Best-effort only — never blocks or breaks the chat stream.
    """
    if not DEPLOYMENT.features.agentAssist or not ASSIST_ENABLED or not text.strip():
        return
    payload = {
        "session_id": conversation_id,
        "message_id": f"{conversation_id}-{uuid.uuid4().hex[:8]}",
        "speaker": speaker,
        "channel": "chat",
        "text": text,
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
    }
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(
                f"{ASSIST_BASE_URL}/api/ingest/message",
                json=payload,
                headers={"X-API-Key": ASSIST_INGEST_KEY},
            )
    except Exception:  # noqa: BLE001
        logger.warning("assist ingest failed (%s/%s)", conversation_id, speaker)

app = FastAPI(title="Chat orchestrator", version="0.2.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

_agent: Any = None
_sessions: dict[str, AgentSession] = {}
_profile_ids: dict[str, str] = {}
_interface_locales: dict[str, str] = {}
_customer_contexts: dict[str, dict[str, Any]] = {}
_conversation_locks: dict[str, asyncio.Lock] = {}

# In-memory human-handover engine (queue + websocket relay). on_transcript forwards both the
# customer and operator turns into agent-assist. Single-replica (azd scale = 1).
_hub = HandoverHub(on_transcript=lambda cid, speaker, text: _fire(_emit_to_assist(cid, speaker, text)))


def _get_agent() -> Any:
    """Build the target agent once: the deployed Foundry agent (default) or the in-process coordinator."""
    global _agent
    if _agent is None:
        if TARGET == "local":
            from main import build_coordinator

            _agent = build_coordinator()
            logger.info("Orchestrator target: LOCAL in-process coordinator")
        else:
            from agent_framework.foundry import FoundryAgent

            _agent = FoundryAgent(
                project_endpoint=PROJECT_ENDPOINT,
                agent_name=AGENT_NAME,
                credential=_credential(),
                allow_preview=True,
            )
            logger.info("Orchestrator target: DEPLOYED Foundry agent '%s'", AGENT_NAME)
    return _agent


def _session(conversation_id: str) -> AgentSession:
    sess = _sessions.get(conversation_id)
    if sess is None:
        sess = _get_agent().create_session()
    return sess


class MessageRequest(BaseModel):
    message: str
    profile_id: str | None = None
    locale: Literal["en", "ar"] | None = None
    media: Any | None = None


def _nd(mark: str, content: Any) -> str:
    """Encode one newline-delimited-JSON envelope line."""
    return json.dumps([mark, content], ensure_ascii=False) + "\n"


def _text(resp: Any) -> str:
    text = getattr(resp, "text", None)
    if text:
        return text
    messages = getattr(resp, "messages", None) or []
    return "\n".join(getattr(m, "text", "") for m in messages)


@app.get("/health")
async def health() -> dict[str, Any]:
    return {"status": "ok", "target": TARGET, "agent": AGENT_NAME,
            "features": asdict(DEPLOYMENT.features),
            "default_language": DEPLOYMENT.organization.defaultLanguage,
            "knowledge_mode": "embedded_first" if DEPLOYMENT.features.knowledge else "embedded_only" if EMBEDDED_KNOWLEDGE_BYTES else "disabled",
            "embedded_knowledge_sha256": EMBEDDED_KNOWLEDGE_SHA256,
            "embedded_knowledge_bytes": EMBEDDED_KNOWLEDGE_BYTES}


# Selector entries only: Dynamics supplies all customer details given to the model.
PROFILES = {
    "no_profile": {"display_name": "Guest", "display_name_ar": "زائر", "phone": "", "email": "", "context": ""},
    **{profile.id: asdict(profile) for profile in load_profiles()},
}


@app.get("/profiles")
async def profiles() -> list[dict[str, Any]]:
    """Approved selector hints, not authenticated customer records."""
    return [{"id": pid, "display_name": p["display_name"], "display_name_ar": p.get("display_name_ar")}
            for pid, p in PROFILES.items()]

def require_agent_console() -> None:
    if not DEPLOYMENT.features.agentConsole:
        raise HTTPException(503, detail={"code": "MODULE_DISABLED", "message": "Operator console module is disabled."})


@app.get("/conversation/{conversation_id}/handover-status")
async def handover_status(conversation_id: str) -> dict[str, Any]:
    require_agent_console()
    return _hub.status(conversation_id)


@app.get("/conversation/{conversation_id}")
async def conversation_history(conversation_id: str) -> list[dict[str, str]]:
    """Operator transcript: the AI-phase turns the agent console loads when it accepts a session."""
    require_agent_console()
    return _hub.history(conversation_id)


@app.post("/conversation/{conversation_id}/request-handover")
async def request_handover(conversation_id: str, body: dict[str, Any] | None = Body(default=None)) -> dict[str, Any]:
    require_agent_console()
    reason = (body or {}).get("reason") or "Customer requested a human agent"
    return await _hub.request(conversation_id, reason)


@app.post("/conversation/{conversation_id}/complete-handover")
async def complete_handover(conversation_id: str) -> dict[str, Any]:
    require_agent_console()
    return await _hub.complete(conversation_id)


@app.post("/conversation/{conversation_id}/stream")
async def stream(conversation_id: str, req: MessageRequest) -> StreamingResponse:
    async def gen():
        token = None
        try:
            yield _nd("ping", {})
            _fire(_emit_to_assist(conversation_id, "customer", req.message))
            if DEPLOYMENT.features.agentConsole:
                _hub.log_turn(conversation_id, "user", req.message)
            async with _conversation_locks.setdefault(conversation_id, asyncio.Lock()):
                if req.profile_id and req.profile_id not in PROFILES:
                    raise ValueError("Unknown customer profile.")
                if conversation_id not in _profile_ids:
                    _profile_ids[conversation_id] = req.profile_id or "no_profile"
                elif req.profile_id and req.profile_id != _profile_ids[conversation_id]:
                    raise ValueError("Start a new conversation before changing profiles.")
                agent = _get_agent()
                is_new = conversation_id not in _sessions
                profile_id = _profile_ids[conversation_id]
                phone = PROFILES.get(profile_id, {}).get("phone")
                interface_locale = req.locale or _interface_locales.get(conversation_id, DEPLOYMENT.organization.defaultLanguage)
                context = ConversationContext(
                    conversation_id=conversation_id, profile_id=phone or profile_id,
                    locale=interface_locale,
                    sensitive_terms=(phone,) if profile_id != "no_profile" and phone else (),
                    handover=_hub.request if DEPLOYMENT.features.agentConsole else None,
                    customer_context=_customer_contexts.setdefault(conversation_id, {}),
                )
                token = CONVERSATION.set(context)
                if DEPLOYMENT.features.crm and is_new and profile_id != "no_profile" and not context.customer_context:
                    crm_result = json.loads(await invoke("customer_context", {}))
                    if crm_result.get("success") is not True:
                        error = crm_result.get("error")
                        message = error.get("message") if isinstance(error, dict) else error
                        raise ValueError(
                            "Dynamics customer context could not be loaded. "
                            + (message if isinstance(message, str) else "Please retry or request human assistance.")
                        )
                session = _session(conversation_id)
                message = req.message
                if is_new or _interface_locales.get(conversation_id) != interface_locale:
                    language = "Arabic" if interface_locale == "ar" else "English"
                    event = "Initial interface language" if is_new else "The user explicitly changed the UI language"
                    message = (
                        f"[{event}: {interface_locale}. Respond in {language} until the user explicitly asks "
                        "for another language or changes the UI selection again. Merely using another "
                        "language does not change this choice.]\n" + message
                    )
                if DEPLOYMENT.features.crm and is_new and profile_id != "no_profile":
                    serialized_context = json.dumps(context.customer_context, ensure_ascii=False).replace("<", "\\u003c").replace(">", "\\u003e")
                    message = (
                        f"[{CUSTOMER_CONTEXT_INSTRUCTIONS.strip()}]\n"
                        "<dynamics_customer_context>\n"
                        f"{serialized_context}\n"
                        "</dynamics_customer_context>\n\n"
                        f"[Customer message]: {message}"
                    )
                result = await agent.run(message, session=session)
                _sessions[conversation_id] = session
                _interface_locales[conversation_id] = interface_locale
                text = _text(result)
                if text:
                    _fire(_emit_to_assist(conversation_id, "ai_agent", text))
                    if DEPLOYMENT.features.agentConsole:
                        _hub.log_turn(conversation_id, "assistant", text)
                    yield _nd("start", AGENT_NAME)
                    yield _nd("delta", {"content": text})
                    yield _nd("end", {})
                yield _nd("response", {
                    "embedded_knowledge_sha256": EMBEDDED_KNOWLEDGE_SHA256,
                    "knowledge_retrieval_calls": context.metrics.knowledge_retrieval_calls,
                })
                if DEPLOYMENT.features.agentConsole and _hub.status(conversation_id)["status"] == "requested":
                    yield _nd("handover_requested", {})
        except Exception as exc:  # noqa: BLE001
            logger.exception("stream failed for %s", conversation_id)
            yield _nd("error", {"message": str(exc)})
        finally:
            if token is not None:
                CONVERSATION.reset(token)

    return StreamingResponse(gen(), media_type="application/x-ndjson")


@app.on_event("shutdown")
async def close_case_client():
    await close_client()


@app.websocket("/ws/customer/{conversation_id}")
async def ws_customer(ws: WebSocket, conversation_id: str) -> None:
    if not DEPLOYMENT.features.agentConsole:
        await ws.close(code=1008, reason="Operator console module is disabled.")
        return
    await _hub.customer_ws(ws, conversation_id)


@app.websocket("/ws/agent/{agent_id}")
async def ws_agent(ws: WebSocket, agent_id: str) -> None:
    if not DEPLOYMENT.features.agentConsole:
        await ws.close(code=1008, reason="Operator console module is disabled.")
        return
    await _hub.agent_ws(ws, agent_id)
