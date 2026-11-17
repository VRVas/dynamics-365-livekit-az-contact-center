"""
Human handover tool for transferring calls to human agents.
Integrates with the existing tool system and Real-Time API.
"""

import asyncio
import json
import os
import logging
from typing import Any, Dict, Optional

from .tools import Tool, ToolResult, ToolResultDirection, CURRENT_SESSION

# Create logger for this module
logger = logging.getLogger("human_handover")
logger.setLevel(logging.INFO)

# Global reference to ACS caller - will be set during tool creation
_acs_caller = None


# --------------------------------------------------------------------- #
# 1. OpenAI function-calling schema                                     #
# --------------------------------------------------------------------- #
_human_handover_schema: Dict[str, Any] = {
    "type": "function",
    "name": "talk_to_human",
    "description": (
        "Transfer the customer to a human agent when they request to speak "
        "with a person or when the assistant cannot help further"
    ),
    "parameters": {
        "type": "object",
        "properties": {},
        "required": [],
        "additionalProperties": False
    }
}


# --------------------------------------------------------------------- #
# 2. Tool target                                                        #
# --------------------------------------------------------------------- #
async def _talk_to_human_tool(args: Dict[str, Any]) -> ToolResult:
    """
    Transfer the customer to a human agent.
    
    This function:
    1. Plays a handover TTS message
    2. Initiates the call transfer to configured agent phone number
    3. Returns success/failure status
    
    Args:
        args: Empty dict (no parameters required)
        
    Returns:
        ToolResult: Result indicating transfer initiation status
    """
    print("HANDOVER_TOOL: talk_to_human tool called")
    logger.info("HANDOVER_TOOL: talk_to_human tool called")
    print(f"HANDOVER_TOOL: args received: {args}")
    logger.info(f"HANDOVER_TOOL: args received: {args}")
    
    try:
        # Check if ACS caller is available
        if not _acs_caller:
            error_msg = "ACS caller not available for transfer"
            print(f"HANDOVER_TOOL: ERROR - {error_msg}")
            logger.error(f"HANDOVER_TOOL: ERROR - {error_msg}")
            return ToolResult(
                text=json.dumps({
                    "success": False,
                    "error": "Call transfer service not available"
                }),
                destination=ToolResultDirection.TO_SERVER
            )
        
        # Per-call guard: live handover is only enabled for outbound test calls placed from
        # the web app (which supply an operator number). Inbound PSTN + in-browser mic
        # sessions keep transfer disabled - refuse and have the model relay the privacy note.
        # Resolve the EXACT session/call that invoked this tool from the ambient context (set by
        # RTMiddleTier.forward_messages). With many concurrent calls each handover targets its own line.
        rtmt_ref = CURRENT_SESSION.get()
        if rtmt_ref is not None and not getattr(rtmt_ref, "transfer_enabled", False):
            print("HANDOVER_TOOL: transfer disabled for this call (inbound/web) - returning privacy redirect")
            logger.info("HANDOVER_TOOL: transfer disabled for this call - returning privacy redirect")
            return ToolResult(
                text=json.dumps({
                    "success": False,
                    "error": "transfer_disabled",
                    "message": (
                        "Live transfer is not available on this call. Explain this briefly in the "
                        "caller's language and offer to keep helping, email the requested information, "
                        "or explain the documented official support channel. Do not claim a representative is connected."
                    )
                }),
                destination=ToolResultDirection.TO_SERVER
            )

        # Get current call connection ID from RTMT (via global access)
        # We'll access this through the global ACS caller which should have access to rtmt
        call_connection_id = None
        if rtmt_ref is not None:
            call_connection_id = rtmt_ref.current_call_connection_id
        
        # Fallback: try to get it from the environment variable if set by call handlers
        if not call_connection_id:
            call_connection_id = os.getenv("CURRENT_CALL_CONNECTION_ID")
        
        print(f"HANDOVER_TOOL: Call connection ID from RTMT: {call_connection_id}")
        logger.info(f"HANDOVER_TOOL: Call connection ID from RTMT: {call_connection_id}")
        
        if not call_connection_id:
            error_msg = "No active call connection found for transfer"
            print(f"HANDOVER_TOOL: ERROR - {error_msg}")
            logger.error(f"HANDOVER_TOOL: ERROR - {error_msg}")
            return ToolResult(
                text=json.dumps({
                    "success": False,
                    "error": "No active call found to transfer"
                }),
                destination=ToolResultDirection.TO_SERVER
            )
        
        # Check if this call has already been transferred (prevent duplicate transfers)
        if rtmt_ref is not None:
            rtmt = rtmt_ref
            if hasattr(rtmt, '_transferred_calls') and call_connection_id in rtmt._transferred_calls:
                error_msg = f"Call {call_connection_id} has already been transferred"
                print(f"HANDOVER_TOOL: WARNING - {error_msg}")
                logger.warning(f"HANDOVER_TOOL: WARNING - {error_msg}")
                return ToolResult(
                    text=json.dumps({
                        "success": False,
                        "error": "Call transfer already in progress",
                        "message": "Your call is already being transferred to an agent. Please stay on the line."
                    }),
                    destination=ToolResultDirection.TO_SERVER
                )
        
        # Additional validation: check if call connection ID looks valid
        if not call_connection_id or len(call_connection_id) < 10:
            error_msg = f"Invalid call connection ID: {call_connection_id}"
            print(f"HANDOVER_TOOL: ERROR - {error_msg}")
            logger.error(f"HANDOVER_TOOL: ERROR - {error_msg}")
            return ToolResult(
                text=json.dumps({
                    "success": False,
                    "error": "Invalid call connection ID for transfer"
                }),
                destination=ToolResultDirection.TO_SERVER
            )
        
        # Check if this call was already transferred by looking for transfer tracking
        if rtmt_ref is not None:
            rtmt = rtmt_ref
            # Check if we have a transfer tracking mechanism
            if hasattr(rtmt, '_transferred_calls'):
                if call_connection_id in rtmt._transferred_calls:
                    error_msg = f"Call {call_connection_id} transfer is already in progress"
                    print(f"HANDOVER_TOOL: WARNING - {error_msg}")
                    logger.warning(f"HANDOVER_TOOL: WARNING - {error_msg}")
                    return ToolResult(
                        text=json.dumps({
                            "success": False,
                            "error": "A transfer is already in progress for this call. Please wait."
                        }),
                        destination=ToolResultDirection.TO_SERVER
                    )
            else:
                # Initialize transfer tracking
                rtmt._transferred_calls = set()
        
        # Get agent phone number: the per-call operator override (set by the outbound /call
        # handler from the web app) takes priority; otherwise fall back to the configured env.
        operator_override = getattr(rtmt_ref, "operator_transfer_number", None) if rtmt_ref else None
        agent_phone_number = operator_override or os.getenv("AGENT_PHONE_NUMBER", "")
        print(f"HANDOVER_TOOL: operator override = {operator_override}")

        print(f"HANDOVER_TOOL: Agent phone number from env: {agent_phone_number}")
        logger.info(f"HANDOVER_TOOL: Agent phone number from env: {agent_phone_number}")
        print(f"HANDOVER_TOOL: Current call connection ID: {call_connection_id}")
        logger.info(f"HANDOVER_TOOL: Current call connection ID: {call_connection_id}")
        
        if not agent_phone_number:
            error_msg = "No agent phone number configured for transfer"
            print(f"HANDOVER_TOOL: ERROR - {error_msg}")
            logger.error(f"HANDOVER_TOOL: ERROR - {error_msg}")
            return ToolResult(
                text=json.dumps({
                    "success": False,
                    "error": "No agent phone number configured for transfer"
                }),
                destination=ToolResultDirection.TO_SERVER
            )
        if not _acs_caller.is_allowed_destination(agent_phone_number):
            logger.warning("Handover refused: operator destination has not been approved.")
            return ToolResult(
                text=json.dumps({"success": False, "error": "The operator destination has not been approved. No transfer was requested."}),
                destination=ToolResultDirection.TO_SERVER,
            )
        
        # Generate handover message
        handover_message = "I'll connect you to a human agent who can better assist you. Please stay on the line."
        print(f"HANDOVER_TOOL: Playing handover message: {handover_message}")
        logger.info(f"HANDOVER_TOOL: Playing handover message: {handover_message}")
        
        # TODO: TTS functionality temporarily disabled to simplify transfer testing
        # Play the handover message
        # try:
        #     await _acs_caller.play_message(call_connection_id, handover_message, "HandoverMessage")
        #     print("HANDOVER_TOOL: Handover message played successfully")
        #     logger.info("HANDOVER_TOOL: Handover message played successfully")
        # except Exception as e:
        #     print(f"HANDOVER_TOOL: Warning - Failed to play handover message: {e}")
        #     logger.warning(f"HANDOVER_TOOL: Warning - Failed to play handover message: {e}")
        #     # Continue with transfer even if message playing fails
        
        # Skip TTS delay for now - proceed directly to transfer
        print("HANDOVER_TOOL: Skipping TTS playback - proceeding directly to transfer")
        logger.info("HANDOVER_TOOL: Skipping TTS playback - proceeding directly to transfer")
        
        # Mark this call as being transferred BEFORE initiating transfer to prevent duplicates
        if rtmt_ref is not None:
            rtmt = rtmt_ref
            if not hasattr(rtmt, '_transferred_calls'):
                rtmt._transferred_calls = set()
            rtmt._transferred_calls.add(call_connection_id)
            print(f"HANDOVER_TOOL: Marked call {call_connection_id} as being transferred")
            logger.info(f"HANDOVER_TOOL: Marked call {call_connection_id} as being transferred")
            
            # MUTE AI AUDIO: Set transfer_active flag to prevent AI from talking during handover
            rtmt.transfer_active = True
            print(f"HANDOVER_TOOL: AI audio muted - transfer_active flag set to True")
            logger.info(f"HANDOVER_TOOL: AI audio muted - transfer_active flag set to True")
            
            # TRANSCRIPTION: Mark handover in transcript buffer so speaker mapping switches to "operator"
            if rtmt.transcript_buffer:
                rtmt.transcript_buffer.mark_handover()
                print(f"HANDOVER_TOOL: Transcript buffer notified of handover")
                logger.info(f"HANDOVER_TOOL: Transcript buffer notified of handover")
            
            # ACS TRANSCRIPTION: Mark handover in ACS transcription handler for speaker identification
            try:
                from backend.acs_transcription import get_transcription_handler
                transcription_handler = get_transcription_handler()
                transcription_handler.mark_handover(call_connection_id)
                print(f"HANDOVER_TOOL: ACS transcription handler notified of handover")
                logger.info(f"HANDOVER_TOOL: ACS transcription handler notified of handover")
            except Exception as e:
                logger.warning(f"HANDOVER_TOOL: Could not notify ACS transcription handler: {e}")
            
            # CCAGENT ASSIST: Escalate session to alert human operators
            if rtmt.agent_assist_client and rtmt.agent_assist_session:
                import asyncio
                asyncio.create_task(
                    rtmt.agent_assist_client.escalate_session(
                        rtmt.agent_assist_session,
                        f"Human handover requested - transferring to agent: {agent_phone_number}"
                    )
                )
                print(f"HANDOVER_TOOL: CCAgent Assist session escalated")
                logger.info(f"HANDOVER_TOOL: CCAgent Assist session escalated")
        
        # Perform the actual call transfer with improved strategy
        print(f"HANDOVER_TOOL: Initiating transfer to agent: {agent_phone_number}")
        logger.info(f"HANDOVER_TOOL: Initiating transfer to agent: {agent_phone_number}")
        
        # Try warm transfer first for better reliability with busy agents
        # If that fails, fallback to blind transfer
        transfer_result = await _acs_caller.transfer_call_to_participant(
            call_connection_id, 
            agent_phone_number,
            use_warm_transfer=True  # Start with warm transfer for better error handling
        )
        
        print(f"HANDOVER_TOOL: Transfer result: {transfer_result}")
        logger.info(f"HANDOVER_TOOL: Transfer result: {transfer_result}")
        logger.info(f"HANDOVER_TOOL: Transfer result: {transfer_result}")
        
        if transfer_result.get("success", False):
            # Mark this call as transferred to prevent duplicate transfers
            if rtmt_ref is not None:
                rtmt = rtmt_ref
                if not hasattr(rtmt, '_transferred_calls'):
                    rtmt._transferred_calls = set()
                rtmt._transferred_calls.add(call_connection_id)
                print(f"HANDOVER_TOOL: Marked call {call_connection_id} as transferred")
                logger.info(f"HANDOVER_TOOL: Marked call {call_connection_id} as transferred")
            
            success_result = {
                "success": True,
                "message": "Transfer to human agent initiated successfully. Please stay on the line.",
                "agent_number": agent_phone_number,
                "call_connection_id": call_connection_id
            }
            return ToolResult(
                text=json.dumps(success_result),
                destination=ToolResultDirection.TO_SERVER
            )
        else:
            error_msg = transfer_result.get("error", "Unknown transfer error")
            print(f"HANDOVER_TOOL: Transfer failed: {error_msg}")
            logger.error(f"HANDOVER_TOOL: Transfer failed: {error_msg}")
            
            # Provide specific error messages based on common failure scenarios
            if "560486" in str(error_msg):
                user_message = "Our agent is currently on another call. Let me try a different approach to connect you."
            elif "560503" in str(error_msg):
                user_message = "I'm sorry, our agent line is currently unavailable. Please try calling back later or leave a message."
            elif "560504" in str(error_msg) or "560505" in str(error_msg):
                user_message = "I'm experiencing a technical issue connecting you to an agent. Please try again in a moment."
            elif "10129" in str(error_msg):
                user_message = "I'm experiencing a technical issue with the transfer system. Please try again in a moment."
            else:
                user_message = "I'm sorry, I'm unable to transfer you to an agent right now. Please try calling back later."
            
            # Clear the transfer state since the transfer failed
            if rtmt_ref is not None:
                rtmt = rtmt_ref
                if hasattr(rtmt, '_transferred_calls') and call_connection_id in rtmt._transferred_calls:
                    rtmt._transferred_calls.remove(call_connection_id)
                    print(f"HANDOVER_TOOL: Cleared transfer state for failed call {call_connection_id}")
                    logger.info(f"HANDOVER_TOOL: Cleared transfer state for failed call {call_connection_id}")
            
            return ToolResult(
                text=json.dumps({
                    "success": False,
                    "error": user_message
                }),
                destination=ToolResultDirection.TO_SERVER
            )
        
    except Exception as e:
        error_msg = f"Error during human handover: {e}"
        print(f"HANDOVER_TOOL: ERROR - {error_msg}")
        logger.error(f"HANDOVER_TOOL: ERROR - {error_msg}")
        import traceback
        print(f"HANDOVER_TOOL: Traceback: {traceback.format_exc()}")
        logger.error(f"HANDOVER_TOOL: Traceback: {traceback.format_exc()}")
        
        # Clear the transfer state if an exception occurred
        try:
            rtmt = CURRENT_SESSION.get()
            if rtmt is not None:
                if hasattr(rtmt, '_transferred_calls'):
                    call_connection_id = rtmt.current_call_connection_id
                    if call_connection_id and call_connection_id in rtmt._transferred_calls:
                        rtmt._transferred_calls.remove(call_connection_id)
                        print(f"HANDOVER_TOOL: Cleared transfer state for exception call {call_connection_id}")
                        logger.info(f"HANDOVER_TOOL: Cleared transfer state for exception call {call_connection_id}")
        except Exception as cleanup_error:
            print(f"HANDOVER_TOOL: Error during cleanup: {cleanup_error}")
            logger.error(f"HANDOVER_TOOL: Error during cleanup: {cleanup_error}")
        
        return ToolResult(
            text=json.dumps({
                "success": False,
                "error": "I'm sorry, I'm unable to transfer you to an agent right now. Please try calling back later."
            }),
            destination=ToolResultDirection.TO_SERVER
        )


# --------------------------------------------------------------------- #
# 3. Factory                                                            #
# --------------------------------------------------------------------- #
def human_handover_tool(acs_caller=None) -> Tool:
    """
    Register with:
        rtmt.tools["talk_to_human"] = human_handover_tool(caller)
    
    Args:
        acs_caller: AcsCaller instance for performing actual transfers
    """
    global _acs_caller
    _acs_caller = acs_caller
    
    print("HANDOVER_FACTORY: Creating human handover tool instance")
    logger.info("HANDOVER_FACTORY: Creating human handover tool instance")
    print(f"HANDOVER_FACTORY: ACS caller provided: {acs_caller is not None}")
    logger.info(f"HANDOVER_FACTORY: ACS caller provided: {acs_caller is not None}")
    
    tool_instance = Tool(schema=_human_handover_schema, target=_talk_to_human_tool)
    
    print(f"HANDOVER_FACTORY: Tool instance created successfully: {type(tool_instance)}")
    logger.info(f"HANDOVER_FACTORY: Tool instance created successfully: {type(tool_instance)}")
    print(f"HANDOVER_FACTORY: Tool schema: {_human_handover_schema}")
    logger.info(f"HANDOVER_FACTORY: Tool schema: {_human_handover_schema}")
    
    return tool_instance
