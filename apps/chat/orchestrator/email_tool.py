"""Email tool (Azure Communication Services Email / ECS) for the chat coordinator.

Sends a transactional email to the customer via ACS Email using the ACS connection string
(ACS_EMAIL_CONNECTION_STRING) and the Azure Managed Domain sender (ACS_EMAIL_SENDER, e.g.
DoNotReply@<guid>.azurecomm.net). Unconfigured email is reported as unavailable.
NOTE: this file is deliberately named `email_tool` (not `email`) so it
does not shadow Python's stdlib `email` package that the ACS SDK depends on.
"""
from __future__ import annotations

import json
import html
import logging
import os
import re
from typing import Annotated

from agent_framework import tool
from runtime_config import DEPLOYMENT

logger = logging.getLogger("orchestrator.tools.email")

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


@tool
async def send_customer_email(
    to_email: Annotated[str, "Confirmed recipient email from current Dynamics context or supplied by the customer"],
    subject: Annotated[str, "Concise, specific subject line"],
    body: Annotated[str, "Email body. Plain text or light Markdown; it is sent as text + basic HTML."],
) -> str:
    """Send a confirmation/summary email to the customer via Azure Communication Services Email."""
    if not DEPLOYMENT.features.email:
        return json.dumps({"success": False, "error": "Email module is disabled. No email was sent."})
    to_email = (to_email or "").strip()
    if not _EMAIL_RE.match(to_email):
        return json.dumps({"success": False, "error": "A valid recipient email address is required."})
    from case_tools import CONVERSATION
    context = CONVERSATION.get()
    snapshot = context.customer_context if context else {}
    synthetic = snapshot.get("dataScope") == "synthetic_profiles_only"
    if not DEPLOYMENT.email_allowed(to_email, synthetic=synthetic):
        return json.dumps({"success": False, "error": "Recipient is not approved by emailPolicy. Synthetic CRM recipients require an exact allowedRecipients entry; a domain alone is insufficient. No email was sent."})

    conn = os.environ.get("ACS_EMAIL_CONNECTION_STRING", "").strip()
    sender = os.environ.get("ACS_EMAIL_SENDER", "").strip()

    if not conn or not sender:
        logger.warning("Email requested before the sending service was configured.")
        return json.dumps({"success": False, "error": "Email delivery is not configured. No email was sent."})

    try:
        from azure.communication.email.aio import EmailClient

        html_body = (
            "<div dir=\"auto\" style=\"font-family:Arial,Helvetica,sans-serif;font-size:14px;line-height:1.5\">"
            + html.escape(body or "").replace("\n", "<br>")
            + "</div>"
        )
        message = {
            "senderAddress": sender,
            "recipients": {"to": [{"address": to_email}]},
            "content": {"subject": subject, "plainText": body, "html": html_body},
        }
        async with EmailClient.from_connection_string(conn) as client:
            poller = await client.begin_send(message)
            result = await poller.result()
        message_id = result.get("id") if isinstance(result, dict) else getattr(result, "id", None)
        status = result.get("status") if isinstance(result, dict) else getattr(result, "status", None)
        if status != "Succeeded" or not message_id:
            logger.error("Email service did not confirm submission (%s)", status)
            return json.dumps({"success": False, "error": "Email submission was not confirmed."})
        logger.info("Email submitted (id=%s)", message_id)
        return json.dumps({"success": True, "status": "submitted", "to": to_email, "subject": subject, "message_id": message_id})
    except Exception as exc:  # noqa: BLE001
        logger.exception("email send failed")
        return json.dumps({"success": False, "error": "The email service did not confirm submission. Do not retry automatically."})
