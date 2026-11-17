"""
CCAgent Assist Console Integration Client

This module provides a client for sending real-time transcripts and session updates
to the CCAgent Assist Console for live agent monitoring and AI-powered insights.

API Reference: See INTEGRATION.md for full API documentation.
"""

import aiohttp
import logging
import uuid
from datetime import datetime
from typing import Literal, Optional
from dataclasses import dataclass, field

logger = logging.getLogger("agent_assist")
logger.setLevel(logging.INFO)


@dataclass
class AgentAssistConfig:
    """Configuration for the Agent Assist Console connection"""
    base_url: str
    api_key: str
    channel: Literal["chat", "voice"] = "voice"
    enabled: bool = True


@dataclass 
class AgentAssistSession:
    """Tracks session state for a single call/conversation"""
    session_id: str
    channel: Literal["chat", "voice"]
    message_counter: int = 0
    is_escalated: bool = False
    is_completed: bool = False
    created_at: datetime = field(default_factory=datetime.utcnow)
    
    def next_message_id(self) -> str:
        """Generate a unique message ID for this session"""
        self.message_counter += 1
        return f"{self.session_id}-msg-{self.message_counter:04d}"


class AgentAssistClient:
    """
    Client for the CCAgent Assist Console API.
    
    Sends real-time transcripts to the console for:
    - Live conversation monitoring
    - AI-powered insights and sentiment analysis
    - Human escalation alerts
    
    Usage:
        config = AgentAssistConfig(
            base_url="https://assist.example.test",
            api_key="your-api-key"
        )
        client = AgentAssistClient(config)
        
        # Start a session
        session = client.create_session()
        
        # Send messages
        await client.send_message(session, "customer", "Hello, I need help")
        await client.send_message(session, "ai_agent", "Hi! How can I assist you?")
        
        # Escalate if needed
        await client.escalate_session(session, "Customer requested human agent")
        
        # Complete when done
        await client.complete_session(session)
    """
    
    def __init__(self, config: AgentAssistConfig):
        self.config = config
        self._http_session: Optional[aiohttp.ClientSession] = None
    
    @property
    def enabled(self) -> bool:
        return self.config.enabled and bool(self.config.base_url) and bool(self.config.api_key)
    
    async def _get_session(self) -> aiohttp.ClientSession:
        """Get or create the HTTP session"""
        if self._http_session is None or self._http_session.closed:
            self._http_session = aiohttp.ClientSession(
                base_url=self.config.base_url,
                headers={
                    "Content-Type": "application/json",
                    "X-API-Key": self.config.api_key
                }
            )
        return self._http_session
    
    async def close(self):
        """Close the HTTP session"""
        if self._http_session and not self._http_session.closed:
            await self._http_session.close()
    
    def create_session(self, session_id: Optional[str] = None) -> AgentAssistSession:
        """
        Create a new session for tracking a conversation.
        
        Args:
            session_id: Optional custom session ID. If not provided, generates a UUID.
            
        Returns:
            AgentAssistSession object for tracking this conversation
        """
        if session_id is None:
            session_id = f"call-{datetime.utcnow().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"
        
        session = AgentAssistSession(
            session_id=session_id,
            channel=self.config.channel
        )
        logger.info(f"AGENT_ASSIST: Created session {session_id}")
        return session
    
    async def send_message(
        self,
        session: AgentAssistSession,
        speaker: Literal["customer", "ai_agent", "operator"],
        text: str,
        message_id: Optional[str] = None
    ) -> bool:
        """
        Send a transcript message to the Agent Assist Console.
        
        Args:
            session: The session this message belongs to
            speaker: Who said this - "customer", "ai_agent", or "operator"
            text: The transcript text
            message_id: Optional custom message ID
            
        Returns:
            True if message was sent successfully, False otherwise
        """
        if not self.enabled:
            logger.debug("AGENT_ASSIST: Client disabled, skipping message")
            return False
        
        if not text or not text.strip():
            logger.debug("AGENT_ASSIST: Empty message, skipping")
            return False
        
        if message_id is None:
            message_id = session.next_message_id()
        
        payload = {
            "session_id": session.session_id,
            "message_id": message_id,
            "speaker": speaker,
            "text": text.strip(),
            "channel": session.channel,
            "timestamp": datetime.utcnow().isoformat() + "Z"
        }
        
        try:
            http = await self._get_session()
            async with http.post("/api/ingest/message", json=payload) as resp:
                if resp.status in (200, 201):  # 200 OK or 201 Created are both success
                    result = await resp.json()
                    is_new = result.get("isNewSession", False)
                    logger.info(f"AGENT_ASSIST: Sent [{speaker}] message to session {session.session_id}" + 
                               (" (new session)" if is_new else ""))
                    return True
                else:
                    error_text = await resp.text()
                    logger.error(f"AGENT_ASSIST: Failed to send message: {resp.status} - {error_text}")
                    return False
        except aiohttp.ClientError as e:
            logger.error(f"AGENT_ASSIST: Network error sending message: {e}")
            return False
        except Exception as e:
            logger.error(f"AGENT_ASSIST: Unexpected error sending message: {e}")
            return False
    
    async def escalate_session(
        self,
        session: AgentAssistSession,
        reason: str = "Human handover requested"
    ) -> bool:
        """
        Mark a session as needing human attention.
        
        This triggers visual alerts in the Agent Assist Console.
        
        Args:
            session: The session to escalate
            reason: Why escalation is needed
            
        Returns:
            True if escalation was successful, False otherwise
        """
        if not self.enabled:
            return False
        
        if session.is_escalated:
            logger.debug(f"AGENT_ASSIST: Session {session.session_id} already escalated")
            return True
        
        payload = {
            "humanAttentionNeeded": True,
            "escalationReason": reason
        }
        
        try:
            http = await self._get_session()
            async with http.patch(f"/api/sessions/{session.session_id}", json=payload) as resp:
                if resp.status in (200, 201):  # 200 OK or 201 Created are both success
                    session.is_escalated = True
                    logger.info(f"AGENT_ASSIST: Escalated session {session.session_id}: {reason}")
                    return True
                else:
                    error_text = await resp.text()
                    logger.error(f"AGENT_ASSIST: Failed to escalate session: {resp.status} - {error_text}")
                    return False
        except Exception as e:
            logger.error(f"AGENT_ASSIST: Error escalating session: {e}")
            return False
    
    async def deescalate_session(
        self,
        session: AgentAssistSession,
        reason: str = "Human agent call ended"
    ) -> bool:
        """
        Clear the human attention flag after the human agent hangs up.
        
        This updates the console to show the call is no longer requiring attention.
        
        Args:
            session: The session to de-escalate
            reason: Why de-escalation occurred
            
        Returns:
            True if de-escalation was successful, False otherwise
        """
        if not self.enabled:
            return False
        
        if not session.is_escalated:
            logger.debug(f"AGENT_ASSIST: Session {session.session_id} was not escalated")
            return True
        
        payload = {
            "humanAttentionNeeded": False,
            "deescalationReason": reason
        }
        
        try:
            http = await self._get_session()
            async with http.patch(f"/api/sessions/{session.session_id}", json=payload) as resp:
                if resp.status in (200, 201):  # 200 OK or 201 Created are both success
                    session.is_escalated = False
                    logger.info(f"AGENT_ASSIST: De-escalated session {session.session_id}: {reason}")
                    return True
                else:
                    error_text = await resp.text()
                    logger.error(f"AGENT_ASSIST: Failed to de-escalate session: {resp.status} - {error_text}")
                    return False
        except Exception as e:
            logger.error(f"AGENT_ASSIST: Error de-escalating session: {e}")
            return False
    
    async def complete_session(self, session: AgentAssistSession) -> bool:
        """
        Mark a session as completed.
        
        Call this when the conversation ends (call disconnected, chat closed).
        
        Args:
            session: The session to complete
            
        Returns:
            True if completion was successful, False otherwise
        """
        if not self.enabled:
            return False
        
        if session.is_completed:
            logger.debug(f"AGENT_ASSIST: Session {session.session_id} already completed")
            return True
        
        payload = {
            "status": "completed",
            "humanAttentionNeeded": False
        }
        
        try:
            http = await self._get_session()
            async with http.patch(f"/api/sessions/{session.session_id}", json=payload) as resp:
                if resp.status in (200, 201):  # 200 OK or 201 Created are both success
                    session.is_completed = True
                    logger.info(f"AGENT_ASSIST: Completed session {session.session_id}")
                    return True
                else:
                    error_text = await resp.text()
                    logger.error(f"AGENT_ASSIST: Failed to complete session: {resp.status} - {error_text}")
                    return False
        except Exception as e:
            logger.error(f"AGENT_ASSIST: Error completing session: {e}")
            return False


# Convenience function for creating a configured client
def create_agent_assist_client(
    base_url: Optional[str] = None,
    api_key: Optional[str] = None,
    channel: Literal["chat", "voice"] = "voice",
    enabled: bool = True
) -> AgentAssistClient:
    """
    Create an AgentAssistClient with the given configuration.
    
    If base_url or api_key are not provided, the client will be disabled.
    """
    config = AgentAssistConfig(
        base_url=base_url or "",
        api_key=api_key or "",
        channel=channel,
        enabled=enabled and bool(base_url) and bool(api_key)
    )
    return AgentAssistClient(config)
