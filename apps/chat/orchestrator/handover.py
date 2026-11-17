"""In-process human-handover engine for the chat orchestrator.

Reinstates the original queue + websocket-relay handover (from apps/chat/api) directly
inside the deployed agent-framework orchestrator. In-memory, single-replica (azd scale = 1).

Wire-compatible with the existing clients (DO NOT change their protocol):
  ui_v2/useWebSocket.ts  (customer)   and   agent_console/useAgentWebSocket.ts (operator)

Customer  WS /ws/customer/{session_id}
  send: {type:"chat_message", content} | {type:"ping"}
  recv: {type:"queue_position", position} | {type:"agent_assigned", agent_id, sender_id}
        | {type:"chat_message", content, sender_type:"agent"} | {type:"handover_complete"}
        | {type:"pong"} | {type:"error", message}
Agent     WS /ws/agent/{agent_id}
  send: {type:"accept_session", session_id} | {type:"chat_message", session_id, content}
        | {type:"complete_session", session_id} | {type:"ping"}
  recv: {type:"queue_update", queue:[{session_id, reason, waiting_seconds, queued_at, position}]}
        | {type:"session_assigned", session_id}
        | {type:"chat_message", content, sender_type:"customer", session_id} | {type:"pong"}
HTTP  POST /conversation/{id}/request-handover  -> {success, queue_position, status}
      GET  /conversation/{id}/handover-status   -> status dict
      POST /conversation/{id}/complete-handover  -> {success, status}
      GET  /conversation/{id}                    -> [{role, content}]  (operator transcript)
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from fastapi import WebSocket, WebSocketDisconnect

logger = logging.getLogger("orchestrator.handover")


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _empty_status() -> dict[str, Any]:
    return {
        "status": "none",
        "requested_at": None,
        "activated_at": None,
        "completed_at": None,
        "human_agent_id": None,
        "human_agent_name": None,
        "escalation_reason": None,
    }


class HandoverHub:
    """Single in-memory hub holding queue, per-conversation status, websocket registries,
    session->agent assignment, and a lightweight transcript log for operator history."""

    def __init__(self, on_transcript: Optional[Callable[[str, str, str], None]] = None) -> None:
        # on_transcript(conversation_id, speaker, text): best-effort forward to agent-assist.
        self._on_transcript = on_transcript
        self._status: dict[str, dict[str, Any]] = {}
        self._queue: list[dict[str, Any]] = []  # [{session_id, reason, queued_at(float)}]
        self._customer_ws: dict[str, WebSocket] = {}
        self._agent_ws: dict[str, WebSocket] = {}
        self._agent_name: dict[str, str] = {}
        self._session_agent: dict[str, str] = {}  # conversation_id -> agent_id
        self._history: dict[str, list[dict[str, str]]] = {}

    # ----------------------------------------------------------------- transcript
    def log_turn(self, conv_id: str, role: str, content: str) -> None:
        """Record an AI-phase or human turn so an operator can see the prior transcript."""
        if content and content.strip():
            self._history.setdefault(conv_id, []).append({"role": role, "content": content})

    def history(self, conv_id: str) -> list[dict[str, str]]:
        return self._history.get(conv_id, [])

    # ----------------------------------------------------------------- status
    def status(self, conv_id: str) -> dict[str, Any]:
        return self._status.get(conv_id) or _empty_status()

    # ----------------------------------------------------------------- queue helpers
    def _position(self, conv_id: str) -> int:
        for i, q in enumerate(self._queue):
            if q["session_id"] == conv_id:
                return i + 1
        return 0

    def _queue_payload(self) -> list[dict[str, Any]]:
        now = time.time()
        return [
            {
                "session_id": q["session_id"],
                "reason": q.get("reason"),
                "waiting_seconds": int(now - q["queued_at"]),
                "queued_at": datetime.fromtimestamp(q["queued_at"], timezone.utc).isoformat(),
                "position": i + 1,
            }
            for i, q in enumerate(self._queue)
        ]

    async def _broadcast_queue(self) -> None:
        payload = {"type": "queue_update", "queue": self._queue_payload()}
        for aid, ws in list(self._agent_ws.items()):
            await self._safe_send(ws, payload, agent_id=aid)

    async def _notify_positions(self) -> None:
        for i, q in enumerate(self._queue):
            await self._send_customer(q["session_id"], {"type": "queue_position", "position": i + 1})

    # ----------------------------------------------------------------- HTTP actions
    async def request(self, conv_id: str, reason: str) -> dict[str, Any]:
        st = self.status(conv_id)
        if st["status"] in ("requested", "active"):
            return {
                "success": False,
                "message": f"Handover already {st['status']}",
                "queue_position": self._position(conv_id),
                "status": st,
            }
        st = {**_empty_status(), "status": "requested", "requested_at": _now_iso(), "escalation_reason": reason}
        self._status[conv_id] = st
        if not any(q["session_id"] == conv_id for q in self._queue):
            self._queue.append({"session_id": conv_id, "reason": reason, "queued_at": time.time()})
        await self._broadcast_queue()
        pos = self._position(conv_id)
        await self._send_customer(conv_id, {"type": "queue_position", "position": pos})
        logger.info("handover requested for %s (queue pos %s)", conv_id, pos)
        return {"success": True, "message": "Handover requested", "queue_position": pos, "status": st}

    async def complete(self, conv_id: str) -> dict[str, Any]:
        st = {**self.status(conv_id), "status": "completed", "completed_at": _now_iso()}
        self._status[conv_id] = st
        self._queue = [q for q in self._queue if q["session_id"] != conv_id]
        self._session_agent.pop(conv_id, None)
        await self._send_customer(conv_id, {"type": "handover_complete"})
        await self._broadcast_queue()
        await self._notify_positions()
        logger.info("handover completed for %s", conv_id)
        return {"success": True, "message": "Handover completed", "status": st}

    # ----------------------------------------------------------------- ws send helpers
    async def _safe_send(self, ws: WebSocket, payload: dict[str, Any], *, agent_id: str | None = None, conv_id: str | None = None) -> bool:
        try:
            await ws.send_text(json.dumps(payload))
            return True
        except Exception:  # noqa: BLE001
            if agent_id and self._agent_ws.get(agent_id) is ws:
                self._agent_ws.pop(agent_id, None)
            if conv_id and self._customer_ws.get(conv_id) is ws:
                self._customer_ws.pop(conv_id, None)
            return False

    async def _send_customer(self, conv_id: str, payload: dict[str, Any]) -> bool:
        ws = self._customer_ws.get(conv_id)
        return await self._safe_send(ws, payload, conv_id=conv_id) if ws else False

    async def _send_agent(self, agent_id: str, payload: dict[str, Any]) -> bool:
        ws = self._agent_ws.get(agent_id)
        return await self._safe_send(ws, payload, agent_id=agent_id) if ws else False

    # ----------------------------------------------------------------- customer ws
    async def customer_ws(self, ws: WebSocket, conv_id: str) -> None:
        await ws.accept()
        self._customer_ws[conv_id] = ws
        agent_id = self._session_agent.get(conv_id)
        if agent_id:  # already assigned (covers accept-before-connect race)
            await self._safe_send(ws, {"type": "agent_assigned", "agent_id": agent_id, "sender_id": agent_id}, conv_id=conv_id)
        else:
            pos = self._position(conv_id)
            if pos:
                await self._safe_send(ws, {"type": "queue_position", "position": pos}, conv_id=conv_id)
        try:
            while True:
                data = json.loads(await ws.receive_text())
                t = data.get("type", "chat_message")
                if t == "ping":
                    await self._safe_send(ws, {"type": "pong"}, conv_id=conv_id)
                    continue
                if t == "chat_message":
                    content = (data.get("content") or "").strip()
                    if not content:
                        continue
                    self.log_turn(conv_id, "user", content)
                    if self._on_transcript:
                        self._on_transcript(conv_id, "customer", content)
                    aid = self._session_agent.get(conv_id)
                    if aid:
                        await self._send_agent(aid, {
                            "type": "chat_message", "content": content,
                            "sender_type": "customer", "session_id": conv_id, "timestamp": _now_iso(),
                        })
                    else:
                        await self._safe_send(ws, {
                            "type": "chat_message", "content": "Waiting for an agent to join\u2026",
                            "sender_type": "system", "session_id": conv_id,
                        }, conv_id=conv_id)
        except WebSocketDisconnect:
            pass
        except Exception as exc:  # noqa: BLE001
            logger.warning("customer ws error %s: %s", conv_id, exc)
        finally:
            if self._customer_ws.get(conv_id) is ws:
                self._customer_ws.pop(conv_id, None)

    # ----------------------------------------------------------------- agent ws
    async def agent_ws(self, ws: WebSocket, agent_id: str) -> None:
        await ws.accept()
        self._agent_ws[agent_id] = ws
        await self._safe_send(ws, {"type": "queue_update", "queue": self._queue_payload()}, agent_id=agent_id)
        try:
            while True:
                data = json.loads(await ws.receive_text())
                t = data.get("type")
                if t == "ping":
                    await self._safe_send(ws, {"type": "pong"}, agent_id=agent_id)
                elif t == "accept_session":
                    sid = data.get("session_id")
                    if not sid:
                        continue
                    self._session_agent[sid] = agent_id
                    self._queue = [q for q in self._queue if q["session_id"] != sid]
                    self._status[sid] = {
                        **self.status(sid), "status": "active",
                        "activated_at": _now_iso(), "human_agent_id": agent_id,
                    }
                    await self._send_customer(sid, {"type": "agent_assigned", "agent_id": agent_id, "sender_id": agent_id})
                    await self._safe_send(ws, {"type": "session_assigned", "session_id": sid}, agent_id=agent_id)
                    await self._broadcast_queue()
                    await self._notify_positions()
                    logger.info("agent %s accepted session %s", agent_id, sid)
                elif t == "chat_message":
                    sid = data.get("session_id")
                    content = (data.get("content") or "").strip()
                    if not sid or not content:
                        continue
                    self.log_turn(sid, "assistant", content)
                    if self._on_transcript:
                        self._on_transcript(sid, "operator", content)
                    await self._send_customer(sid, {
                        "type": "chat_message", "content": content,
                        "sender_type": "agent", "session_id": sid, "timestamp": _now_iso(),
                    })
                elif t == "complete_session":
                    sid = data.get("session_id")
                    if sid:
                        await self.complete(sid)
                        await self._safe_send(ws, {"type": "session_completed", "session_id": sid}, agent_id=agent_id)
        except WebSocketDisconnect:
            pass
        except Exception as exc:  # noqa: BLE001
            logger.warning("agent ws error %s: %s", agent_id, exc)
        finally:
            if self._agent_ws.get(agent_id) is ws:
                self._agent_ws.pop(agent_id, None)
