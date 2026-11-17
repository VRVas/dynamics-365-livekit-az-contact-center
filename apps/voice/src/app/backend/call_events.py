"""
Call event handlers for managing Azure Communication Services events,
including transfer errors and context-based call flow control.
"""

import asyncio
from typing import Callable, Awaitable

from azure.communication.callautomation.aio import CallAutomationClient
from azure.core.messaging import CloudEvent

from .call_utils import ContextEnum, handle_hangup


class CallEventHandler:
    """
    Handles Azure Communication Services call events and manages call flow.
    """
    
    def __init__(self, client: CallAutomationClient):
        self.client = client
        self._prompts = None  # Will be set when prompts system is available
    
    async def on_transfer_error(
        self,
        call_connection_id: str,
        error_code: int,
        post_callback: Callable[[str], Awaitable[None]] | None = None,
    ) -> None:
        """
        Callback for when a call transfer fails.

        Args:
            call_connection_id: The ID of the call connection
            error_code: The error code from the transfer failure
            post_callback: Optional callback to execute after handling the error

        Logs the error and plays a TTS message to the user about the failure.
        """
        print(f"Error during call transfer for {call_connection_id}, error code: {error_code}")
        
        # TODO: Implement TTS message for transfer failure
        # This will be implemented when the prompts system is ready
        # For now, just hang up the call after a transfer failure
        
        try:
            await handle_hangup(
                client=self.client,
                call_connection_id=call_connection_id,
                context=ContextEnum.TRANSFER_FAILED,
            )
        except Exception as e:
            print(f"Error hanging up call after transfer failure: {e}")
        
        if post_callback:
            await post_callback(call_connection_id)
    
    async def handle_event(self, event: CloudEvent) -> None:
        """
        Handle incoming Azure Communication Services events.
        
        Args:
            event: The CloudEvent from ACS
        """
        event_type = event.type
        event_data = event.data
        
        if not event_data:
            return
        
        call_connection_id = event_data.get("callConnectionId")
        if not call_connection_id:
            return
        
        print(f"Handling event: {event_type} for call {call_connection_id}")
        
        # Handle transfer-related events
        if event_type == "Microsoft.Communication.CallTransferFailed":
            error_code = event_data.get("resultInformation", {}).get("subCode", 0)
            await self.on_transfer_error(call_connection_id, error_code)
        
        elif event_type == "Microsoft.Communication.CallTransferAccepted":
            print(f"Call transfer successful for {call_connection_id}")
        
        elif event_type == "Microsoft.Communication.CallDisconnected":
            print(f"Call disconnected: {call_connection_id}")
        
        elif event_type == "Microsoft.Communication.CallConnected":
            print(f"Call connected: {call_connection_id}")
        
        # Add more event handlers as needed
    
    def set_prompts_handler(self, prompts_handler):
        """
        Set the prompts handler for TTS messages.
        
        Args:
            prompts_handler: Handler for generating TTS prompts
        """
        self._prompts = prompts_handler


async def process_acs_events(events_data: list, event_handler: CallEventHandler) -> None:
    """
    Process a list of Azure Communication Services events.
    
    Args:
        events_data: List of event data dictionaries
        event_handler: CallEventHandler instance to process events
    """
    for event_data in events_data:
        try:
            event = CloudEvent.from_dict(event_data)
            await event_handler.handle_event(event)
        except Exception as e:
            print(f"Error processing event: {e}")
            # Continue processing other events even if one fails
