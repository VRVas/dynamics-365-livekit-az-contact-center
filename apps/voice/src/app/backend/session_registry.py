"""Process-global registry of active phone (ACS) voice sessions, keyed by ACS callConnectionId.

Browser (`/realtime`) sessions are fully self-contained inside their WebSocket coroutine and are
NOT registered here. Only ACS *phone* calls need cross-component routing: for a single call, ACS
opens the media-streaming WS (`/realtime-acs`) and the transcription WS (`/transcription-acs`) as
separate connections, and posts call-lifecycle webhooks (`/acs/call-events`, `/acs`) — all of them
carry the same `callConnectionId`. Every one of those must resolve back to the ONE
`RTMiddleTier` session that is driving that call so several concurrent PSTN calls never share state.

ACS stamps the media-streaming WebSocket with the `x-ms-call-connection-id` header (Azure
Communication Services audio-streaming quickstart), and includes `callConnectionId` in the
transcription metadata and every Call Automation event payload — those are the routing keys.
"""

import logging
from typing import Dict, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from backend.rtmt import RTMiddleTier

logger = logging.getLogger("session_registry")


class SessionRegistry:
    """Maps ACS callConnectionId -> the RTMiddleTier session handling that call."""

    def __init__(self) -> None:
        self._by_call_id: Dict[str, "RTMiddleTier"] = {}

    def register(self, call_id: Optional[str], session: "RTMiddleTier") -> None:
        if not call_id:
            return
        self._by_call_id[call_id] = session
        session.current_call_connection_id = call_id
        logger.info(f"SESSION_REGISTRY: registered call {call_id} (active calls={len(self._by_call_id)})")

    def get(self, call_id: Optional[str]) -> Optional["RTMiddleTier"]:
        if not call_id:
            return None
        return self._by_call_id.get(call_id)

    def unregister(self, call_id: Optional[str]) -> None:
        if call_id and call_id in self._by_call_id:
            del self._by_call_id[call_id]
            logger.info(f"SESSION_REGISTRY: unregistered call {call_id} (active calls={len(self._by_call_id)})")

    def most_recent(self) -> Optional["RTMiddleTier"]:
        """Fallback when a connection arrives without a usable call-id (e.g. the media WS header is
        missing). Safe only when a single phone call is active; with concurrent calls the header is
        always present so this is not reached."""
        if not self._by_call_id:
            return None
        return list(self._by_call_id.values())[-1]

    @property
    def active_count(self) -> int:
        return len(self._by_call_id)


_registry: Optional[SessionRegistry] = None


def get_session_registry() -> SessionRegistry:
    """Return the process-global phone-session registry (created on first use)."""
    global _registry
    if _registry is None:
        _registry = SessionRegistry()
    return _registry
