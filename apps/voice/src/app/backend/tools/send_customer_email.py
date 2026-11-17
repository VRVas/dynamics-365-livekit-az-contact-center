from __future__ import annotations

import json
import logging
import os
import re
from html import escape
from typing import Any

from azure.communication.email.aio import EmailClient
from azure.core.exceptions import AzureError

from backend.tools.tools import CURRENT_SESSION, Tool, ToolResult, ToolResultDirection
from runtime_config import DEPLOYMENT, Deployment

logger = logging.getLogger("customer_email")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

_send_customer_email_schema = {
    "type": "function",
    "name": "send_customer_email",
    "description": (
        "Actually send the customer's requested email. If emailAddress is omitted, use the "
        "bound customer's current Dynamics email. Without current CRM context, ask for "
        "an address and provide it explicitly. Never invent a recipient. Report submission "
        "only after success=true; an error means no confirmed send. Submission is not proof "
        "that the email reached the inbox."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "emailAddress": {
                "type": "string",
                "description": "Explicit recipient confirmed by the caller; omit to use the current Dynamics email.",
            },
            "name": {
                "type": "string",
                "description": "Recipient name; defaults to the current Dynamics name when omitted.",
            },
            "infoBody": {
                "type": "string",
                "minLength": 1,
                "description": "Nonempty plain-text email body summarizing the requested information.",
            },
            "language": {
                "type": "string",
                "enum": ["ar", "en"],
                "description": "The explicitly requested email language; otherwise keep the chosen conversation language.",
            },
        },
        "required": ["infoBody"],
        "additionalProperties": False,
    },
}


def _failure(message: str) -> ToolResult:
    logger.warning("Email tool did not confirm a send: %s", message)
    return ToolResult(json.dumps({"success": False, "error": message}), ToolResultDirection.TO_SERVER)


async def _send_customer_email_tool(args: dict[str, Any], deployment: Deployment | None = None) -> ToolResult:
    deployment = deployment or DEPLOYMENT
    if not deployment.features.email:
        return _failure("Email module is disabled. No email was sent.")
    session = CURRENT_SESSION.get()
    profile_email = getattr(session, "customer_email", None)
    profile_name = getattr(session, "customer_name", None)
    email = args.get("emailAddress") or profile_email or ""
    name = args.get("name") or profile_name or "Customer"
    body = args.get("infoBody")
    if not isinstance(email, str) or not _EMAIL_RE.fullmatch(email.strip()):
        return _failure("No valid recipient email is available. Ask the caller for the address; do not claim an email was sent.")
    if not isinstance(name, str) or not isinstance(body, str) or not body.strip():
        return _failure("A recipient name and nonempty plain-text email body are required.")
    email, name, body = email.strip(), name.strip() or "Customer", body.strip()
    synthetic = getattr(session, "customer_context", None) is not None
    if not deployment.email_allowed(email, synthetic=synthetic):
        return _failure("Recipient is not approved by emailPolicy. Synthetic CRM recipients require an exact allowedRecipients entry; a domain alone is insufficient. No email was sent.")
    connection = os.environ.get("ACS_EMAIL_CONNECTION_STRING", "").strip()
    sender = os.environ.get("ACS_EMAIL_SENDER", "").strip()
    if not connection or not sender:
        return _failure("Email delivery is not configured. No email was sent.")

    language = args.get("language")
    if language is not None and language not in ("ar", "en"):
        return _failure("Choose Arabic or English for the email language.")
    if language is None:
        language = getattr(session, "language", deployment.organization.defaultLanguage)
    arabic = language == "ar"
    greeting = f"مرحباً {name}،" if arabic else f"Dear {name},"
    organization = deployment.organization.name
    closing = f"مع التحية،\n{organization}" if arabic else f"Kind regards,\n{organization}"
    message = {
        "senderAddress": sender,
        "recipients": {"to": [{"address": email}]},
        "content": {
            "subject": f"{organization} - متابعة طلبك" if arabic else f"{organization} - your service follow-up",
            "plainText": f"{greeting}\n\n{body}\n\n{closing}",
            "html": (
                '<div dir="auto" style="font-family:Arial,Helvetica,sans-serif;font-size:14px;line-height:1.5">'
                f"{escape(greeting)}<br><br>"
                + escape(body).replace("\n", "<br>")
                + "<br><br>" + escape(closing).replace("\n", "<br>") + "</div>"
            ),
        },
    }
    logger.info("Submitting requested customer email (recipient_source=%s)", "argument" if args.get("emailAddress") else "profile" if profile_email else "configured-default")
    try:
        async with EmailClient.from_connection_string(connection) as client:
            poller = await client.begin_send(message)
            result = await poller.result()
    except (AzureError, TimeoutError, ValueError) as error:
        logger.error("ACS email submission failed (%s)", type(error).__name__)
        return _failure("The email service did not confirm submission. Do not claim success or retry automatically.")

    status = result.get("status") if isinstance(result, dict) else None
    operation_id = result.get("id") if isinstance(result, dict) else None
    if status != "Succeeded" or not isinstance(operation_id, str) or not operation_id:
        return _failure("The email service returned no successful send receipt. Do not claim the email was sent.")
    logger.info("ACS email submission succeeded (operation_id=%s)", operation_id)
    return ToolResult(json.dumps({
        "success": True,
        "status": "submitted",
        "delivery_confirmed": False,
        "to": email,
        "message_id": operation_id,
    }), ToolResultDirection.TO_SERVER)


def send_customer_email_tool(deployment: Deployment | None = None) -> Tool:
    async def send(args):
        return await _send_customer_email_tool(args, deployment)
    return Tool(schema=_send_customer_email_schema, target=send)
