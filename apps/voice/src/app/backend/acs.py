# backend/acs.py
"""
Async helper around Azure Communication Services Call Automation
for both inbound and outbound PSTN calls with bidirectional
WebSocket media streaming (pcm‑24 kHz, unmixed) and real-time transcription.
"""

import logging
import os
import re
from aiohttp import web
from azure.core.exceptions import AzureError
from azure.core.messaging import CloudEvent
from azure.communication.callautomation.aio import CallAutomationClient
from azure.communication.callautomation import (
    PhoneNumberIdentifier,
    MediaStreamingOptions,
    StreamingTransportType,
    MediaStreamingContentType,
    MediaStreamingAudioChannelType,
    AudioFormat,
    TranscriptionOptions,
)
from runtime_config import PHONE

logger = logging.getLogger(__name__)

AUTO_CONFERENCE_CONTEXT = "automatic_conference_add"


class AcsCaller:
    def __init__(self,
                 source_number: str,
                 acs_connection_string: str,
                 acs_callback_path: str,
                 acs_media_streaming_websocket_path: str,
                 acs_transcription_websocket_path: str = None,
                 cognitive_services_endpoint: str = None,
                 auto_conference_enabled: bool = False,
                 auto_conference_number: str = "",
                 allowed_outbound_numbers: tuple[str, ...] = ()):
        if any(not isinstance(number, str) or not PHONE.fullmatch(number) for number in allowed_outbound_numbers):
            raise ValueError("Outbound destinations must be normalized E.164 numbers.")
        self._allowed_outbound_numbers = frozenset(allowed_outbound_numbers)
        if auto_conference_enabled and (
            not PHONE.fullmatch(auto_conference_number)
            or auto_conference_number == source_number
        ):
            raise ValueError("Automatic conferencing requires an explicitly approved ACS_AUTO_CONFERENCE_NUMBER distinct from the source number.")
        if auto_conference_enabled and not self.is_allowed_destination(auto_conference_number):
            raise ValueError("Automatic conference destination is not in the approved outbound dialing policy.")
        self.source_number = source_number
        self.acs_callback_path = acs_callback_path
        self.auto_conference_enabled = auto_conference_enabled
        self.auto_conference_number = auto_conference_number
        self._conference_attempts: set[str] = set()
        if not auto_conference_enabled:
            logger.info("ACS_CONFERENCE: Automatic phone conferencing is disabled")
        
        # Transcription configuration (ACS Call Transcription)
        self._cognitive_services_endpoint = cognitive_services_endpoint
        self._transcription_ws_path = acs_transcription_websocket_path
        self._transcription_enabled = bool(cognitive_services_endpoint and acs_transcription_websocket_path)
        
        if self._transcription_enabled:
            print(f"ACS_TRANSCRIPTION: Enabled with endpoint {cognitive_services_endpoint[:50]}...")
        else:
            print(f"ACS_TRANSCRIPTION: Disabled - missing cognitive_services_endpoint or transcription_websocket_path")

        # one async client, reused for every call
        self._client = CallAutomationClient.from_connection_string(
            acs_connection_string)

        self._media_stream_cfg = MediaStreamingOptions(
            transport_url=acs_media_streaming_websocket_path,
            transport_type=StreamingTransportType.WEBSOCKET,
            content_type=MediaStreamingContentType.AUDIO,
            audio_channel_type=MediaStreamingAudioChannelType.UNMIXED,  # ← like TS
            start_media_streaming=True,
            enable_bidirectional=True,
            audio_format=AudioFormat.PCM24_K_MONO,
        )
        
        # Transcription options (ACS Call Transcription using Azure AI Services)
        self._transcription_cfg = None
        if self._transcription_enabled:
            self._transcription_cfg = TranscriptionOptions(
                transport_url=acs_transcription_websocket_path,
                transport_type=StreamingTransportType.WEBSOCKET,
                locale="en-US",  # Can be made configurable
                start_transcription=True,  # Auto-start transcription when call connects
            )

    # ------------------------------------------------------------------ #
    # Outbound
    # ------------------------------------------------------------------ #
    async def close(self):
        self._conference_attempts.clear()
        await self._client.close()

    async def handle_conference_event(self, event: CloudEvent) -> bool:
        """Invite the configured phone participant; consume only its completion events."""
        if not self.auto_conference_enabled:
            return False
        data = event.data or {}
        call_id = data.get("callConnectionId")

        if event.type == "Microsoft.Communication.CallConnected":
            if not isinstance(call_id, str) or not call_id:
                logger.error("ACS_CONFERENCE: CallConnected is missing a callConnectionId")
                raise ValueError("CallConnected is missing a callConnectionId")
            if call_id not in self._conference_attempts:
                # Mark before awaiting so concurrent/redelivered webhooks cannot dial twice.
                self._conference_attempts.add(call_id)
                try:
                    connection = self._client.get_call_connection(call_id)
                    conference_participant = PhoneNumberIdentifier(self.auto_conference_number)
                    async for participant in connection.list_participants():
                        identifier = participant.identifier
                        if (
                            isinstance(identifier, PhoneNumberIdentifier)
                            and identifier.raw_id == conference_participant.raw_id
                        ):
                            logger.info(
                                "ACS_CONFERENCE: %s already connected to call %s; skipping duplicate invite",
                                self.auto_conference_number, call_id,
                            )
                            return False
                    await connection.add_participant(
                        target_participant=conference_participant,
                        source_caller_id_number=PhoneNumberIdentifier(self.source_number),
                        operation_context=AUTO_CONFERENCE_CONTEXT,
                    )
                except AzureError:
                    logger.exception(
                        "ACS_CONFERENCE: Failed to check or invite %s to call %s; customer call remains active",
                        self.auto_conference_number, call_id,
                    )
                else:
                    logger.info(
                        "ACS_CONFERENCE: Invite requested for %s on call %s; awaiting participant connection",
                        self.auto_conference_number, call_id,
                    )
        elif event.type == "Microsoft.Communication.CallDisconnected":
            self._conference_attempts.discard(call_id)
        elif (
            event.type in (
                "Microsoft.Communication.AddParticipantSucceeded",
                "Microsoft.Communication.AddParticipantFailed",
            )
            and data.get("operationContext") == AUTO_CONFERENCE_CONTEXT
        ):
            if event.type == "Microsoft.Communication.AddParticipantSucceeded":
                logger.info(
                    "ACS_CONFERENCE: %s joined call %s",
                    self.auto_conference_number, call_id,
                )
            else:
                logger.error(
                    "ACS_CONFERENCE: %s could not join call %s; customer call remains active: %s",
                    self.auto_conference_number, call_id, data.get("resultInformation"),
                )
            return True
        return False

    async def initiate_call(self, target_number: str, callback_url: str = None, media_ws_path: str = None) -> str:
        if not self.is_allowed_destination(target_number):
            logger.warning("Outbound call refused by the destination policy.")
            raise ValueError("Outbound destination is not approved.")
        # media_ws_path is accepted for call-site compatibility; the media WS transport URL is
        # already baked into self._media_stream_cfg at construction time.
        result = await self._client.create_call(
            target_participant=PhoneNumberIdentifier(target_number),
            callback_url=callback_url or self.acs_callback_path,
            media_streaming=self._media_stream_cfg,
            source_caller_id_number=PhoneNumberIdentifier(self.source_number),
            transcription=self._transcription_cfg,
            cognitive_services_endpoint=self._cognitive_services_endpoint if self._transcription_enabled else None,
        )
        return result.call_connection_id

    def is_allowed_destination(self, number: str) -> bool:
        return isinstance(number, str) and bool(PHONE.fullmatch(number)) and number in self._allowed_outbound_numbers

    async def outbound_call_handler(self, request: web.Request) -> web.Response:
        try:
            request_body = await request.json()
            print(f"ACS_EVENTS: Received {len(request_body)} call events")
            
            for ev in request_body:
                event = CloudEvent.from_dict(ev)
                if event.data is None:
                    continue
                
                call_id = event.data.get('callConnectionId', 'unknown')
                print(f"ACS_EVENTS: {event.type} → {call_id}")

                if await self.handle_conference_event(event):
                    continue
                
                # Resolve the session driving THIS specific call (by callConnectionId) so several
                # concurrent PSTN calls each update their own state instead of a shared singleton.
                from backend.session_registry import get_session_registry
                _registry = get_session_registry()
                rtmt = _registry.get(call_id)
                if rtmt is not None:
                    
                    if event.type == "Microsoft.Communication.CallConnected":
                        print(f"ACS_EVENTS: Call connected - updating RTMT call ID from {rtmt.current_call_connection_id} to {call_id}")
                        rtmt.current_call_connection_id = call_id
                        
                    elif event.type == "Microsoft.Communication.CallDisconnected":
                        print(f"ACS_EVENTS: Call disconnected - call ID: {call_id}, current RTMT ID: {rtmt.current_call_connection_id}")
                        # Clear the current call connection ID if this was the active call
                        if rtmt.current_call_connection_id == call_id:
                            rtmt.current_call_connection_id = None
                            print(f"ACS_EVENTS: Cleared RTMT current call ID")
                        # Drop this call's session + transcription tracker from the registries.
                        try:
                            _registry.unregister(call_id)
                            from backend.acs_transcription import get_transcription_handler
                            get_transcription_handler().cleanup_call(call_id)
                        except Exception as _cleanup_exc:
                            logging.getLogger("rtmt").warning(f"ACS_EVENTS: cleanup on disconnect failed: {_cleanup_exc}")
                        transferred_calls = getattr(rtmt, "_transferred_calls", None)
                        if isinstance(transferred_calls, set) and call_id in transferred_calls:
                            print(f"ACS_EVENTS: Transfer call completed - human party hung up (call {call_id})")
                            logging.getLogger("rtmt").info(f"ACS_EVENTS: Transfer call completed - human party hung up (call {call_id})")
                            transferred_calls.remove(call_id)
                            
                            # De-escalate the session - human attention no longer needed
                            if hasattr(rtmt, 'agent_assist_client') and hasattr(rtmt, 'agent_assist_session'):
                                if rtmt.agent_assist_client and rtmt.agent_assist_session:
                                    await rtmt.agent_assist_client.deescalate_session(
                                        rtmt.agent_assist_session,
                                        reason="Human agent call ended"
                                    )
                                    print(f"ACS_EVENTS: De-escalated CCAgent session - humanAttentionNeeded set to false")
                                    logging.getLogger("rtmt").info(f"ACS_EVENTS: De-escalated CCAgent session")
                        
                        # UNMUTE AI: Clear transfer_active flag when call disconnects
                        if hasattr(rtmt, 'transfer_active') and rtmt.transfer_active:
                            rtmt.transfer_active = False
                            print(f"ACS_EVENTS: AI audio unmuted - transfer_active flag cleared")
                            logging.getLogger("rtmt").info(f"ACS_EVENTS: AI audio unmuted - transfer_active flag cleared")
                        
                    elif event.type == "Microsoft.Communication.CallTransferFailed":
                        transfer_error = event.data.get("resultInformation", {})
                        error_code = transfer_error.get("subCode", "unknown")
                        print(f"ACS_EVENTS: Call transfer failed - ID: {call_id}, Error: {transfer_error}, Current RTMT ID: {rtmt.current_call_connection_id}")
                        
                        # Clear transfer state on failure to allow retries
                        if hasattr(rtmt, '_transferred_calls') and call_id in rtmt._transferred_calls:
                            rtmt._transferred_calls.remove(call_id)
                            print(f"ACS_EVENTS: Cleared transfer state for failed call {call_id}")
                        
                        # UNMUTE AI: Clear transfer_active flag when transfer fails so AI can respond again
                        if hasattr(rtmt, 'transfer_active') and rtmt.transfer_active:
                            rtmt.transfer_active = False
                            print(f"ACS_EVENTS: AI audio unmuted after transfer failure - transfer_active flag cleared")
                            logging.getLogger("rtmt").info(f"ACS_EVENTS: AI audio unmuted after transfer failure")
                        
                        # Log specific error codes for debugging and provide guidance
                        if error_code == 560486:
                            print(f"ACS_EVENTS: Transfer failed - target number is BUSY (486#560486)")
                            print(f"ACS_EVENTS: Recommendation: Try warm transfer or different agent number")
                        elif error_code == 560503:
                            print(f"ACS_EVENTS: Transfer failed - target number may be unreachable or invalid")
                        elif error_code in [560504, 560505]:
                            print(f"ACS_EVENTS: Transfer failed - network or service issue")
                        else:
                            print(f"ACS_EVENTS: Transfer failed - unknown error code: {error_code}")
                        
                    elif event.type == "Microsoft.Communication.TranscriptionStarted":
                        print(f"ACS_EVENTS: Transcription started for call {call_id}")
                        logging.getLogger("rtmt").info(f"ACS_EVENTS: Transcription started for call {call_id}")
                        
                    elif event.type == "Microsoft.Communication.TranscriptionStopped":
                        print(f"ACS_EVENTS: Transcription stopped for call {call_id}")
                        logging.getLogger("rtmt").info(f"ACS_EVENTS: Transcription stopped for call {call_id}")
                        
                    elif event.type == "Microsoft.Communication.TranscriptionFailed":
                        error_info = event.data.get("resultInformation", {})
                        error_code = error_info.get("code", "unknown")
                        error_subcode = error_info.get("subCode", "unknown")
                        error_message = error_info.get("message", "unknown")
                        print(f"ACS_EVENTS: Transcription FAILED - call={call_id}, code={error_code}, subcode={error_subcode}, message={error_message}")
                        logging.getLogger("rtmt").error(f"ACS_EVENTS: Transcription FAILED: {error_info}")
                        
                    elif event.type == "Microsoft.Communication.AddParticipantSucceeded":
                        print(f"ACS_EVENTS: Add participant succeeded - ID: {call_id}")
                        # In warm transfer, this means the agent joined successfully
                        # You could now remove the app from the call if desired
                        
                    elif event.type == "Microsoft.Communication.AddParticipantFailed":
                        add_error = event.data.get("resultInformation", {})
                        error_code = add_error.get("subCode", "unknown")
                        print(f"ACS_EVENTS: Add participant failed - ID: {call_id}, Error: {add_error}")
                        
                        # For warm transfer failures, clear transfer state
                        if hasattr(rtmt, '_transferred_calls') and call_id in rtmt._transferred_calls:
                            rtmt._transferred_calls.remove(call_id)
                            print(f"ACS_EVENTS: Cleared transfer state for failed add participant {call_id}")
                        
                        # Log specific error codes
                        if error_code == 560486:
                            print(f"ACS_EVENTS: Add participant failed - target number is BUSY")
                        elif error_code == 560503:
                            print(f"ACS_EVENTS: Add participant failed - target number unreachable")
                        else:
                            print(f"ACS_EVENTS: Add participant failed - error code: {error_code}")
                        
                    elif event.type == "Microsoft.Communication.CallTransferAccepted":
                        print(f"ACS_EVENTS: Call transfer accepted - ID: {call_id}, Current RTMT ID: {rtmt.current_call_connection_id}")
                        # After successful transfer, the call is handed off
                        print(f"ACS_EVENTS: Transfer successful - call handed off, clearing RTMT call ID")
                        if rtmt.current_call_connection_id == call_id:
                            rtmt.current_call_connection_id = None
                            print(f"ACS_EVENTS: Cleared RTMT call ID after successful transfer")
                else:
                    print(f"ACS_EVENTS: No RTMT instance available for call tracking")
                    
            return web.Response(status=200)
        except Exception as exc:
            print(f"ACS_EVENTS: Error handling call events: {exc}")
            import traceback
            traceback.print_exc()
            return web.Response(text=str(exc), status=500)

    # ------------------------------------------------------------------ #
    # Inbound
    # ------------------------------------------------------------------ #
    async def handle_incoming_call(self, event_data: dict, callback_url: str) -> str:
        """
        Answers an incoming call and starts media streaming + transcription.
        Returns the callConnectionId so the caller can track it.
        
        If ACS transcription is enabled (cognitive_services_endpoint configured),
        transcription will be started automatically and streamed to the
        transcription WebSocket endpoint.
        """
        # Build answer_call parameters
        answer_params = {
            "incoming_call_context": event_data["incomingCallContext"],
            "callback_url": callback_url,
            "media_streaming": self._media_stream_cfg,
        }
        
        # Add transcription if enabled (requires Azure AI Services endpoint)
        if self._transcription_enabled and self._transcription_cfg:
            answer_params["transcription"] = self._transcription_cfg
            answer_params["cognitive_services_endpoint"] = self._cognitive_services_endpoint
            print(f"ACS_TRANSCRIPTION: Enabling real-time transcription for incoming call")
            print(f"ACS_TRANSCRIPTION: Cognitive services endpoint: {self._cognitive_services_endpoint}")
            print(f"ACS_TRANSCRIPTION: Transcription WS path: {self._transcription_ws_path}")
        else:
            print(f"ACS_TRANSCRIPTION: Transcription NOT enabled for this call")
            print(f"ACS_TRANSCRIPTION: _transcription_enabled={self._transcription_enabled}, _transcription_cfg={self._transcription_cfg}")
        
        result = await self._client.answer_call(**answer_params)
        call_id = result.call_connection_id
        print(f"Answered incoming call – connectionId: {call_id}")
        return call_id

    # ------------------------------------------------------------------ #
    # Call Transfer - Updated to handle both blind and warm transfer patterns
    # ------------------------------------------------------------------ #
    async def transfer_call_to_participant(self, call_connection_id: str, target_number: str, use_warm_transfer: bool = False) -> dict:
        """
        Transfer an active call to a human agent using either blind or warm transfer.
        
        Based on Microsoft docs and best practices:
        - Blind transfer: Fastest, removes app from call, shows original caller ID to agent
        - Warm transfer: Add agent first, then remove app (more reliable for busy agents)
        
        Args:
            call_connection_id: The ID of the active call to transfer
            target_number: The phone number of the agent to transfer to
            use_warm_transfer: If True, use warm transfer (add participant), if False use blind transfer
            
        Returns:
            dict: Transfer result with success status and details
        """
        if not self.is_allowed_destination(target_number):
            logger.warning("Application phone transfer refused by the destination policy.")
            return {"success": False, "error": "Transfer destination is not approved.", "call_connection_id": call_connection_id}
        try:
            print(f"ACS_TRANSFER: Attempting {'warm' if use_warm_transfer else 'blind'} transfer of call {call_connection_id} to {target_number}")
            
            # Validate and format the target phone number in E.164 format
            formatted_number = target_number
            try:
                import phonenumbers
                
                # Parse and validate the phone number
                parsed_number = phonenumbers.parse(target_number, None)
                if phonenumbers.is_valid_number(parsed_number):
                    # Format in E.164 (international format)
                    formatted_number = phonenumbers.format_number(parsed_number, phonenumbers.PhoneNumberFormat.E164)
                    print(f"ACS_TRANSFER: Phone number validated and formatted from {target_number} to {formatted_number}")
                else:
                    print(f"ACS_TRANSFER: Warning - Phone number may be invalid: {target_number}, using as-is")
                
            except Exception as phone_error:
                print(f"ACS_TRANSFER: Phone number validation warning: {phone_error}, using number as-is: {target_number}")
                formatted_number = target_number  # Use the original number if validation fails
            
            # Get the call connection
            call_connection = self._client.get_call_connection(call_connection_id)
            
            if use_warm_transfer:
                # WARM TRANSFER: Add agent as participant first, then remove app
                return await self._warm_transfer(call_connection, call_connection_id, formatted_number)
            else:
                # BLIND TRANSFER: Direct transfer using official API
                return await self._blind_transfer(call_connection, call_connection_id, formatted_number)
            
        except Exception as e:
            print(f"ACS_TRANSFER: Transfer failed - {e}")
            import traceback
            print(f"ACS_TRANSFER: Traceback: {traceback.format_exc()}")
            
            return {
                "success": False,
                "error": str(e),
                "call_connection_id": call_connection_id,
                "target_number": target_number
            }

    async def _blind_transfer(self, call_connection, call_connection_id: str, formatted_number: str) -> dict:
        """
        Perform a blind transfer using the official transfer_call_to_participant API.
        This removes the app from the call once transfer completes.
        """
        print(f"ACS_TRANSFER: Performing blind transfer to {formatted_number}")
        
        # Use the official transfer API with proper parameters
        transfer_result = await call_connection.transfer_call_to_participant(
            target_participant=PhoneNumberIdentifier(formatted_number),
            operation_context="human_handover_blind"
        )
        
        print(f"ACS_TRANSFER: Blind transfer initiated successfully")
        print(f"ACS_TRANSFER: Transfer result: {transfer_result}")
        
        # Extract operation context safely
        operation_context = None
        try:
            if hasattr(transfer_result, 'operation_context'):
                operation_context = transfer_result.operation_context
        except Exception as ctx_error:
            print(f"ACS_TRANSFER: Could not extract operation context: {ctx_error}")
        
        return {
            "success": True,
            "message": "Blind transfer initiated successfully",
            "call_connection_id": call_connection_id,
            "target_number": formatted_number,
            "transfer_type": "blind",
            "operation_context": operation_context
        }

    async def _warm_transfer(self, call_connection, call_connection_id: str, formatted_number: str) -> dict:
        """
        Perform a warm transfer by adding the agent as a participant.
        
        IMPORTANT: This keeps the app in the call as a silent participant, which allows:
        1. Continued audio streaming to your WebSocket (for transcription)
        2. Both customer and operator audio received via UNMIXED streaming
        3. Each audio packet has participantRawID to identify the speaker
        
        The app does NOT leave the call - it stays to receive audio for transcription.
        """
        print(f"ACS_TRANSFER: Performing warm transfer to {formatted_number} (app stays in call for transcription)")
        
        try:
            # Add the agent as a participant to the call
            # This requires providing our ACS number as caller ID for PSTN participants
            caller_id = PhoneNumberIdentifier(self.source_number)
            agent_participant = PhoneNumberIdentifier(formatted_number)
            
            print(f"ACS_TRANSFER: Adding agent {formatted_number} to call with caller ID {self.source_number}")
            
            add_result = await call_connection.add_participant(
                target_participant=agent_participant,
                source_caller_id_number=caller_id,
                operation_context="human_handover_warm_add"
            )
            
            print(f"ACS_TRANSFER: Add participant result: {add_result}")

            # add_participant renegotiates the call; ACS can pause Call Transcription as a side effect
            # of that participant-state change. Best-effort (re)start so the operator + ongoing customer
            # keep being transcribed post-handover. If transcription is already running, ACS returns an
            # error which we swallow (harmless).
            if self._transcription_enabled:
                try:
                    await call_connection.start_transcription()
                    print("ACS_TRANSFER: (re)started ACS Call Transcription after warm add_participant")
                except Exception as tx_err:
                    print(f"ACS_TRANSFER: start_transcription after add_participant returned: {tx_err}")

            # IMPORTANT: We intentionally do NOT remove ourselves from the call
            # This allows us to continue receiving audio streams for transcription
            # The app stays as a silent participant in the 3-way call
            
            return {
                "success": True,
                "message": "Warm transfer initiated - app stays in call for transcription",
                "call_connection_id": call_connection_id,
                "target_number": formatted_number,
                "transfer_type": "warm_with_transcription",
                "add_participant_result": str(add_result),
                "app_in_call": True  # App remains in call to receive audio
            }
            
        except Exception as e:
            print(f"ACS_TRANSFER: Warm transfer failed during add participant: {e}")
            import traceback
            print(f"ACS_TRANSFER: Traceback: {traceback.format_exc()}")
            
            # Don't fallback to blind transfer - we want to stay in call for transcription
            # Instead, return error so the AI can inform the customer
            return {
                "success": False,
                "error": str(e),
                "call_connection_id": call_connection_id,
                "target_number": formatted_number,
                "message": "Failed to add agent to call"
            }

    async def play_message(self, call_connection_id: str, message: str, context: str = "") -> None:
        """
        Play a TTS message to the call participant.
        
        Args:
            call_connection_id: The ID of the active call
            message: The message to play
            context: Optional context for logging
        """
        try:
            print(f"ACS_TTS: Playing message on call {call_connection_id}: '{message}' (context: {context})")
            
            # Get the call connection
            call_connection = self._client.get_call_connection(call_connection_id)
            
            # Play the TTS message
            # Note: This is a simplified implementation - you may need to adjust based on your TTS setup
            await call_connection.play_media_to_all(
                play_source=message,  # This might need to be adjusted based on your TTS configuration
                operation_context=context
            )
            
            print(f"ACS_TTS: Message played successfully")
            
        except Exception as e:
            print(f"ACS_TTS: Failed to play message - {e}")
            import traceback
            print(f"ACS_TTS: Traceback: {traceback.format_exc()}")