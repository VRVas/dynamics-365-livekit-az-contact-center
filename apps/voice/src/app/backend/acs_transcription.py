# backend/acs_transcription.py
"""
ACS Call Transcription WebSocket handler.

Receives real-time transcription data from Azure Communication Services
and forwards it to the CCAgent Assist Console with proper speaker identification.

Based on Microsoft docs:
https://learn.microsoft.com/en-us/azure/communication-services/how-tos/call-automation/real-time-transcription-tutorial

TranscriptionData format:
{
    "kind": "TranscriptionData",
    "transcriptionData": {
        "text": "Hello, how can I help you?",
        "format": "display",
        "confidence": 0.87,
        "offset": 1234567890,
        "duration": 1500000,
        "words": [...],
        "participantRawID": "4:+12025550101",  # Technical example phone participant
        "resultStatus": "Final"  # or "Intermediate"
    }
}
"""

import json
import logging
from aiohttp import web, WSMsgType
from dataclasses import dataclass, field
from typing import Optional, Dict, Set
from datetime import datetime

logger = logging.getLogger("acs_transcription")


@dataclass
class ParticipantTracker:
    """
    Track participants in a call to determine speaker roles.
    
    The first PSTN participant is assumed to be the customer.
    Any participant added later (during handover) is the operator.
    """
    customer_id: Optional[str] = None
    operator_ids: Set[str] = field(default_factory=set)
    app_participant_id: Optional[str] = None  # The bot/app participant
    handover_active: bool = False
    
    def identify_speaker(self, participant_raw_id: str) -> str:
        """
        Map a participantRawID to a speaker role.
        
        Args:
            participant_raw_id: The raw ID from transcription data
                               Format: "4:+phonenumber" for PSTN
                               
        Returns:
            Speaker role: "customer", "operator", or "unknown"
        """
        if not participant_raw_id:
            return "unknown"
        
        # Skip app/bot participants (we don't transcribe the AI)
        if self.app_participant_id and participant_raw_id == self.app_participant_id:
            return "ai_agent"
        
        # First participant is customer
        if self.customer_id is None:
            self.customer_id = participant_raw_id
            logger.info(f"ACS_TRANSCRIPTION: First participant identified as CUSTOMER: {participant_raw_id}")
            return "customer"
        
        # Known customer
        if participant_raw_id == self.customer_id:
            return "customer"
        
        # Known operator
        if participant_raw_id in self.operator_ids:
            return "operator"
        
        # New participant after handover = operator
        if self.handover_active:
            self.operator_ids.add(participant_raw_id)
            logger.info(f"ACS_TRANSCRIPTION: New participant during handover identified as OPERATOR: {participant_raw_id}")
            return "operator"
        
        # Unknown participant before handover - assume customer's alternative ID
        logger.warning(f"ACS_TRANSCRIPTION: Unknown participant before handover: {participant_raw_id}, treating as customer")
        return "customer"
    
    def mark_handover(self):
        """Mark that a handover to human agent has occurred."""
        self.handover_active = True
        logger.info("ACS_TRANSCRIPTION: Handover marked - new participants will be identified as operator")


class AcsTranscriptionHandler:
    """
    WebSocket handler for ACS real-time transcription stream.
    
    Receives TranscriptionData and TranscriptionMetadata messages from ACS
    and forwards transcriptions to the CCAgent Assist Console.
    """
    
    def __init__(self):
        # Track participants per call
        self._participant_trackers: Dict[str, ParticipantTracker] = {}
        
        # Reference to RTMT instance for accessing agent_assist_client
        self._rtmt_instance = None
    
    def get_tracker(self, call_id: str) -> ParticipantTracker:
        """Get or create a participant tracker for a call."""
        if call_id not in self._participant_trackers:
            self._participant_trackers[call_id] = ParticipantTracker()
        return self._participant_trackers[call_id]
    
    def mark_handover(self, call_id: str):
        """Mark handover for a specific call."""
        tracker = self.get_tracker(call_id)
        tracker.mark_handover()
    
    def cleanup_call(self, call_id: str):
        """Clean up tracking data when a call ends."""
        if call_id in self._participant_trackers:
            del self._participant_trackers[call_id]
            logger.info(f"ACS_TRANSCRIPTION: Cleaned up tracker for call {call_id}")
    
    async def handle_websocket(self, request: web.Request) -> web.WebSocketResponse:
        """
        WebSocket handler for ACS transcription stream.
        
        ACS sends transcription data to this endpoint in real-time.
        """
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        
        logger.info("ACS_TRANSCRIPTION: WebSocket connection established")
        
        call_id = None
        
        try:
            async for msg in ws:
                if msg.type == WSMsgType.TEXT:
                    try:
                        data = json.loads(msg.data)
                        await self._process_message(data, call_id)
                        
                        # Extract call ID from metadata if present
                        if data.get("kind") == "TranscriptionMetadata":
                            metadata = data.get("transcriptionMetadata", {})
                            call_id = metadata.get("callConnectionId")
                            if call_id:
                                logger.info(f"ACS_TRANSCRIPTION: Tracking call {call_id}")
                                
                    except json.JSONDecodeError as e:
                        logger.error(f"ACS_TRANSCRIPTION: Invalid JSON: {e}")
                        
                elif msg.type == WSMsgType.ERROR:
                    logger.error(f"ACS_TRANSCRIPTION: WebSocket error: {ws.exception()}")
                    
        except Exception as e:
            logger.error(f"ACS_TRANSCRIPTION: Error processing WebSocket: {e}")
            import traceback
            traceback.print_exc()
        finally:
            if call_id:
                self.cleanup_call(call_id)
            logger.info("ACS_TRANSCRIPTION: WebSocket connection closed")
        
        return ws
    
    async def _process_message(self, data: dict, call_id: Optional[str]):
        """Process a transcription message from ACS."""
        kind = data.get("kind")
        
        if kind == "TranscriptionMetadata":
            await self._handle_metadata(data)
            
        elif kind == "TranscriptionData":
            await self._handle_transcription(data, call_id)
            
        elif kind == "StopTranscription":
            logger.info("ACS_TRANSCRIPTION: Received stop transcription signal")
            
        else:
            logger.debug(f"ACS_TRANSCRIPTION: Unknown message kind: {kind}")
    
    async def _handle_metadata(self, data: dict):
        """Handle TranscriptionMetadata message."""
        metadata = data.get("transcriptionMetadata", {})
        call_id = metadata.get("callConnectionId")
        subscription_id = metadata.get("subscriptionId")
        locale = metadata.get("locale")
        
        logger.info(f"ACS_TRANSCRIPTION: Metadata - call={call_id}, locale={locale}, subscription={subscription_id}")
    
    async def _handle_transcription(self, data: dict, call_id: Optional[str]):
        """
        Handle TranscriptionData message - the actual transcript.
        
        Extracts speaker from participantRawID and forwards to CCAgent Assist.
        """
        transcription = data.get("transcriptionData", {})
        
        text = transcription.get("text", "").strip()
        if not text:
            return  # Skip empty transcriptions
        
        participant_id = transcription.get("participantRawID", "")
        result_status = transcription.get("resultStatus", "")
        confidence = transcription.get("confidence", 0)
        
        # Only process final transcriptions (not intermediate)
        if result_status != "Final":
            logger.debug(f"ACS_TRANSCRIPTION: Skipping intermediate transcript: {text[:50]}...")
            return
        
        # Get speaker role from participant tracker
        tracker = self.get_tracker(call_id or "unknown")
        speaker = tracker.identify_speaker(participant_id)

        # Resolve the RTMiddleTier session driving THIS call (by callConnectionId) so concurrent
        # calls read their own handover/agent-assist state, not a shared singleton's.
        from backend.session_registry import get_session_registry
        session = get_session_registry().get(call_id) or self._rtmt_instance

        # Post-handover the realtime leg is MUTED (its inbound audio is dropped so the muted AI can't
        # respond), so ACS Call Transcription becomes the SOLE transcript source and must forward BOTH
        # the customer AND the operator. Pre-handover the realtime leg already captures the customer
        # (+ AI), so we forward only the operator to avoid duplicate customer turns. Drive "post-handover"
        # off the call's rtmt.transfer_active flag (robust) OR this call's tracker. Never forward the AI.
        handover_active = bool(getattr(session, "transfer_active", False)) or tracker.handover_active

        # Diagnostic: log every FINAL frame we receive so a real call shows whether ACS keeps
        # streaming after the human is added and how each speaker is classified.
        logger.info(
            f"ACS_TRANSCRIPTION: frame speaker={speaker} handover={handover_active} "
            f"conf={confidence:.2f} pid={participant_id} text={text[:60]!r}"
        )

        if speaker == "ai_agent":
            return
        if not handover_active and speaker != "operator":
            return

        logger.info(f"ACS_TRANSCRIPTION: -> agent-assist [{speaker.upper()}] ({confidence:.2f}): {text}")

        # Send to CCAgent Assist Console for this call's session
        await self._send_to_agent_assist(speaker, text, session)
    
    async def _send_to_agent_assist(self, speaker: str, text: str, session=None):
        """Forward transcription to CCAgent Assist Console for the given call's session."""
        session = session or self._rtmt_instance
        if not session:
            logger.debug("ACS_TRANSCRIPTION: No session, skipping Agent Assist send")
            return
        
        agent_assist_client = getattr(session, 'agent_assist_client', None)
        agent_assist_session = getattr(session, 'agent_assist_session', None)
        
        if not agent_assist_client or not agent_assist_session:
            logger.debug("ACS_TRANSCRIPTION: Agent Assist not configured, skipping send")
            return
        
        try:
            await agent_assist_client.send_message(
                session=agent_assist_session,
                speaker=speaker,
                text=text
            )
            logger.debug(f"ACS_TRANSCRIPTION: Sent [{speaker}] to Agent Assist: {text[:50]}...")
        except Exception as e:
            logger.error(f"ACS_TRANSCRIPTION: Failed to send to Agent Assist: {e}")


# Global handler instance
_transcription_handler: Optional[AcsTranscriptionHandler] = None


def get_transcription_handler() -> AcsTranscriptionHandler:
    """Get or create the global transcription handler."""
    global _transcription_handler
    if _transcription_handler is None:
        _transcription_handler = AcsTranscriptionHandler()
    return _transcription_handler


async def transcription_websocket_handler(request: web.Request) -> web.WebSocketResponse:
    """
    Route handler for /transcription-acs endpoint.
    
    This is the WebSocket endpoint that ACS sends transcription data to.
    Configure this URL in the TranscriptionOptions when answering calls.
    """
    handler = get_transcription_handler()
    return await handler.handle_websocket(request)
