"""
Call utilities for managing Azure Communication Services operations,
including call transfers, hangup handling, and context management.
"""

import asyncio
import json
from contextlib import asynccontextmanager
from enum import Enum
from typing import Any, Set

from azure.communication.callautomation import PhoneNumberIdentifier
from azure.communication.callautomation.aio import CallAutomationClient, CallConnectionClient
from azure.core.exceptions import HttpResponseError


class ContextEnum(Enum):
    """
    Enumeration for call operation contexts used to track call state and flow.
    """
    TRANSFER_FAILED = "transfer_failed"
    GOODBYE = "goodbye"
    IVR_LANG_SELECT = "ivr_lang_select"


class CallHangupException(Exception):
    """
    Exception raised when a call is hung up during an operation.
    """
    pass


def _context_serializer(contexts: Set[ContextEnum] | None) -> str:
    """
    Serialize a set of contexts to a JSON string for ACS operation context.
    
    Args:
        contexts: Set of ContextEnum values to serialize
        
    Returns:
        JSON string representation of the contexts
    """
    if not contexts:
        return json.dumps([])
    
    return json.dumps([context.value for context in contexts])


@asynccontextmanager
async def _detect_hangup():
    """
    Context manager to detect call hangup exceptions and handle them gracefully.
    
    Raises:
        CallHangupException: If the call is hung up during the operation
    """
    try:
        yield
    except HttpResponseError as e:
        if e.status_code == 404:  # Call not found, likely hung up
            raise CallHangupException("Call was hung up") from e
        raise


async def _use_call_client(client: CallAutomationClient, call_connection_id: str) -> CallConnectionClient:
    """
    Get a call connection client for the specified call ID.
    
    Args:
        client: The CallAutomationClient instance
        call_connection_id: The call connection ID
        
    Returns:
        CallConnectionClient for the specified call
    """
    return client.get_call_connection(call_connection_id)


async def handle_transfer(
    client: CallAutomationClient,
    call_connection_id: str,
    target: str,
    context: ContextEnum | None = None,
) -> None:
    """
    Transfer a call to another participant.

    Args:
        client: CallAutomationClient instance
        call_connection_id: The ID of the call connection to transfer
        target: Phone number to transfer the call to
        context: Optional context for the transfer operation

    Raises:
        CallHangupException: If the call is hung up during transfer
    """
    print(f"Transferring call {call_connection_id} to: {target}")
    
    with _detect_hangup():
        call_client = await _use_call_client(client, call_connection_id)
        await call_client.transfer_call_to_participant(
            operation_context=_context_serializer({context} if context else None),
            target_participant=PhoneNumberIdentifier(target),
        )


async def handle_hangup(
    client: CallAutomationClient,
    call_connection_id: str,
    context: ContextEnum | None = None,
) -> None:
    """
    Hang up a call.

    Args:
        client: CallAutomationClient instance
        call_connection_id: The ID of the call connection to hang up
        context: Optional context for the hangup operation

    Raises:
        CallHangupException: If the call is already hung up
    """
    print(f"Hanging up call {call_connection_id}")
    
    with _detect_hangup():
        call_client = await _use_call_client(client, call_connection_id)
        await call_client.hang_up(for_everyone=True)
