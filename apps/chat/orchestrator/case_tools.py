"""Thin chat adapters for approved knowledge and Dynamics case services."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Annotated, Awaitable, Callable

import httpx
from agent_framework import tool
from runtime_config import DEPLOYMENT

logger = logging.getLogger("orchestrator.case_tools")
REQUEST_TIMEOUT_SECONDS = 18
MAX_RESPONSE_BYTES = 64 * 1024


@dataclass
class ToolMetrics:
    knowledge_retrieval_calls: int = 0


@dataclass(frozen=True)
class ConversationContext:
    conversation_id: str
    profile_id: str
    locale: str
    sensitive_terms: tuple[str, ...]
    handover: Callable[[str, str], Awaitable[dict]] | None
    metrics: ToolMetrics = field(default_factory=ToolMetrics)
    customer_context: dict = field(default_factory=dict)


CONVERSATION: ContextVar[ConversationContext | None] = ContextVar("case_tools_conversation", default=None)
_client: httpx.AsyncClient | None = None


async def close_client():
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


async def invoke(action: str, arguments: dict) -> str:
    global _client
    enabled = DEPLOYMENT.features.knowledge if action == "knowledge" else DEPLOYMENT.features.crm
    if not enabled:
        return json.dumps({"success": False, "error": f"{'Knowledge' if action == 'knowledge' else 'CRM'} module is disabled.", "error_code": "MODULE_DISABLED"})
    if action == "knowledge" and not DEPLOYMENT.knowledge.allowedDomains:
        return json.dumps({"success": False, "error": "Knowledge retrieval requires deployment-approved domains."})
    if action == "create_report" and arguments.get("category") not in DEPLOYMENT.business.caseCategories:
        return json.dumps({"success": False, "error": "Choose a configured case category."})
    endpoint = os.environ.get("ASSIST_BASE_URL", "").rstrip("/")
    key = os.environ.get("ASSIST_INGEST_KEY", "")
    context = CONVERSATION.get()
    if action == "knowledge" and context is not None:
        context.metrics.knowledge_retrieval_calls += 1
    if not endpoint or not key:
        logger.error("Case service is not configured.")
        return json.dumps({"success": False, "error": "Service tools are currently unavailable. No action was confirmed."})
    payload = {"action": action, **arguments}
    lookup_reference = None
    if action == "get_report" and re.fullmatch(r"OP-[A-F0-9]{64}", str(arguments.get("reference", ""))):
        lookup_reference = arguments["reference"]
    if action == "knowledge":
        payload["locale"] = context.locale if context else DEPLOYMENT.organization.defaultLanguage
        customer = context.customer_context.get("customer") if context else None
        customer = customer if isinstance(customer, dict) else {}
        account = customer.get("account")
        account = account if isinstance(account, dict) else {}
        terms = (*context.sensitive_terms, *(
            customer.get(key) for key in ("name", "firstName", "email", "phone")
        ), account.get("name")) if context else ()
        payload["sensitiveTerms"] = list(dict.fromkeys(
            term for term in terms if isinstance(term, str) and term
        ))
    elif action != "resolve_location":
        if context is None:
            logger.warning("A case transaction was refused without a bound conversation.")
            return json.dumps({"success": False, "error": "This action requires an active customer conversation."})
        is_profile = bool(context.profile_id and context.profile_id != "no_profile")
        subject = context.profile_id if is_profile else context.conversation_id
        payload["actorId"] = ("profile:" if is_profile else "session:") + hashlib.sha256(subject.encode()).hexdigest()
        if action in {"create_report", "add_followup"}:
            canonical = json.dumps(arguments, sort_keys=True, ensure_ascii=False)
            payload["operationId"] = hashlib.sha256(f"{context.conversation_id}:{action}:{canonical}".encode()).hexdigest()
            if action == "create_report":
                lookup_reference = "OP-" + hashlib.sha256(f"{payload['actorId']}:{payload['operationId']}".encode()).hexdigest().upper()
    if _client is None:
        _client = httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS, follow_redirects=False)
    try:
        async with asyncio.timeout(REQUEST_TIMEOUT_SECONDS):
            async with _client.stream(
                "POST", f"{endpoint}/api/cases", json=payload, headers={"X-API-Key": key},
            ) as response:
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(body) + len(chunk) > MAX_RESPONSE_BYTES:
                        raise ValueError("The service response exceeds its context budget.")
                    body.extend(chunk)
        result = json.loads(body)
        if not isinstance(result, dict):
            raise ValueError("Invalid service response.")
        if not response.is_success:
            logger.warning("Case service rejected %s (HTTP %s)", action, response.status_code)
            error = result.get("error")
            message = error.get("message") if isinstance(error, dict) else error
            code = error.get("code") if isinstance(error, dict) else None
            unknown = code in {"CRM_OUTCOME_UNKNOWN", "CRM_WRITE_UNCONFIRMED"} or (
                action in {"create_report", "add_followup"} and response.status_code >= 500
            )
            return json.dumps({
                "success": False, "lookup_reference": lookup_reference,
                "error_code": code,
                "outcome": "unknown" if unknown else "rejected",
                **({"instruction": "Do not say the write failed or was not recorded. It may already exist. Check the same lookup reference; never invent a replacement or discard the submitted details."} if unknown else {}),
                "error": message if isinstance(message, str) else "Service request failed.",
            })
        if action == "customer_context" and result.get("success") is True:
            if (
                result.get("source") != "dynamics"
                or result.get("identityVerified") is not False
                or result.get("dataScope") != "synthetic_profiles_only"
                or "customer" not in result
                or not (result["customer"] is None or isinstance(result["customer"], dict))
                or not isinstance(result.get("cases"), list)
                or not isinstance(result.get("casesTruncated"), bool)
            ):
                raise ValueError("Invalid Dynamics customer context.")
        encoded = json.dumps(result, ensure_ascii=False)
        if len(encoded.encode()) > MAX_RESPONSE_BYTES:
            raise ValueError("The response is too large. Ask a narrower question or retrieve the request status.")
        if action == "customer_context" and result.get("success") is True and context is not None:
            context.customer_context.clear()
            context.customer_context.update(result)
        return encoded
    except (httpx.HTTPError, ValueError, TimeoutError) as error:
        logger.error("Case service %s failed (%s)", action, type(error).__name__)
        return json.dumps({
            "success": False, "lookup_reference": lookup_reference,
            "outcome": "unknown" if action in {"create_report", "add_followup"} or lookup_reference else "unavailable",
            "error": "The service did not confirm an outcome. A lookup reference is not a receipt; check its status before repeating an action.",
        })


@tool
async def search_knowledge(
    query: Annotated[str, "A general service question, with no names, contact details, precise personal addresses, request identifiers or transcript."],
) -> str:
    """Fallback for missing or time-sensitive public guidance. Do not call when the embedded KB already answers the question."""
    return await invoke("knowledge", {"query": query})


@tool
async def get_customer_context() -> str:
    """Refresh the selected eligible customer's Dynamics profile and cases. Not identity verification.

    Use only when context is missing or an intentional profile refresh is needed, not on every turn.
    A guest or unmatched profile has no customer data; never create a replacement contact.
    """
    return await invoke("customer_context", {})


@tool
async def list_customer_cases() -> str:
    """Read the current customer's latest Dynamics cases when no case number is known.

    Reuse a fresh result already supplied this turn; do not reload the complete customer profile.
    """
    return await invoke("list_cases", {})


@tool
async def resolve_service_location(
    district: Annotated[str, "Customer-supplied district; empty when location is not required"],
    street: Annotated[str, "Customer-supplied street; may be empty"],
    landmark: Annotated[str, "Customer-supplied nearby landmark; may be empty"],
) -> str:
    """Validate location completeness for a service request. Does not geocode or verify jurisdiction."""
    return await invoke("resolve_location", {"location": {"district": district, "street": street, "landmark": landmark}})


@tool
async def create_case(
    category: Annotated[str, "One of the deployment's configured case categories in the system instructions"],
    description: Annotated[str, "Customer's description of the issue"],
    district: Annotated[str, "Confirmed district"],
    street: Annotated[str, "Confirmed street, or empty when a landmark is supplied"],
    landmark: Annotated[str, "Confirmed landmark, or empty when a street is supplied"],
    confirmed: Annotated[bool, "True only after the customer confirms recording these details"],
) -> str:
    """Record a confirmed service request and return its receipt. Does not dispatch or take any external action."""
    return await invoke("create_report", {
        "category": category, "description": description, "confirmed": confirmed,
        "location": {"district": district, "street": street, "landmark": landmark},
    })


@tool
async def get_case(reference: Annotated[str, "A Dynamics case number or OP- lookup reference returned by this service"]) -> str:
    """Read current Dynamics status, recent activities and conversation-link outcome."""
    return await invoke("get_report", {"reference": reference.strip()})


@tool
async def add_report_followup(
    reference: Annotated[str, "A Dynamics case number or OP- lookup reference returned by this service"],
    note: Annotated[str, "Additional information or a request for human review"],
    confirmed: Annotated[bool, "True only after the customer confirms adding the follow-up"],
) -> str:
    """Append a confirmed follow-up without changing resolution status or promising an outcome."""
    return await invoke("add_followup", {"reference": reference.strip(), "note": note, "confirmed": confirmed})


@tool
async def talk_to_human(reason: Annotated[str, "Concise reason for transferring this conversation"]) -> str:
    """Request the existing human-agent queue for this conversation."""
    if not DEPLOYMENT.features.agentConsole:
        return json.dumps({"success": False, "error": "Operator console module is disabled.", "error_code": "MODULE_DISABLED"})
    context = CONVERSATION.get()
    if context is None or context.handover is None:
        logger.warning("Human assistance requested without a bound conversation.")
        return json.dumps({"success": False, "error": "Human assistance requires an active conversation."})
    return json.dumps(await context.handover(context.conversation_id, reason), ensure_ascii=False)
