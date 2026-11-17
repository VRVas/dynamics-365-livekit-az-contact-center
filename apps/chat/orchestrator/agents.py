"""Compatibility entrypoint for clients that previously built the chat workflow."""
from main import build_coordinator


def build_workflow(chat_client=None):
    return build_coordinator(chat_client)
