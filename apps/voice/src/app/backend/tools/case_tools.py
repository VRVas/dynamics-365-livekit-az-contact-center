"""Voice tool bindings for the shared, authenticated service API."""
import hashlib
import json
import logging
import os
import re
from copy import deepcopy
from typing import TYPE_CHECKING
from uuid import UUID

import aiohttp

from backend.crm_context import CustomerContext, parse_case_receipt, parse_customer_context
from backend.tools.tools import CURRENT_SESSION, Tool, ToolResult, ToolResultDirection
from runtime_config import DEPLOYMENT, Deployment

if TYPE_CHECKING:
    from backend.rtmt import RTMiddleTier

logger = logging.getLogger("case_tools")
MAX_RESULT_BYTES = 12 * 1024


class CaseServiceError(ValueError):
    def __init__(self, message, code, status):
        super().__init__(message)
        self.code = code if isinstance(code, str) else None
        self.status = status

DEFINITIONS = {
    "search_knowledge": (
        "knowledge",
        "Fallback for missing or time-sensitive service guidance only. Do not call when the embedded KB already answers the question. Ask a general question without names, contacts, incident locations, request IDs or transcripts.",
        {"query": {"type": "string", "description": "General service question without personal information."}},
    ),
    "resolve_service_location": (
        "resolve_location", "Check supplied location completeness; does not geocode or verify jurisdiction.",
        {"district": {"type": "string"}, "street": {"type": "string"}, "landmark": {"type": "string"}},
    ),
    "customer_context": (
        "customer_context",
        "Retrieve this conversation's eligible Dynamics customer and recent cases. A caller-ID match is not verified identity. Do not repeat a prefetched lookup just to greet.",
        {},
    ),
    "list_cases": (
        "list_cases", "List recent Dynamics cases for this conversation's customer only.",
        {},
    ),
    "create_case": (
        "create_report", "Record a confirmed service request. Never claim an official external submission, dispatch or approval.",
        {
            "category": {"type": "string"},
            "description": {"type": "string"}, "district": {"type": "string"},
            "street": {"type": "string"}, "landmark": {"type": "string"},
            "confirmed": {"type": "boolean", "description": "True only after the customer confirms the details and recording."},
        },
    ),
    "get_case": (
        "get_report", "Retrieve this customer's Dynamics case by its returned ticket number, or an OP- lookup reference after an uncertain creation.",
        {"reference": {"type": "string"}},
    ),
    "add_report_followup": (
        "add_followup", "Append a confirmed follow-up; does not change resolution status.",
        {"reference": {"type": "string"}, "note": {"type": "string"}, "confirmed": {"type": "boolean"}},
    ),
}


class CaseServiceClient:
    def __init__(self, deployment: Deployment | None = None):
        self.deployment = deployment or DEPLOYMENT
        self.definitions = deepcopy(DEFINITIONS)
        self.definitions["create_case"][2]["category"]["enum"] = list(self.deployment.business.caseCategories)
        self.endpoint = (os.environ.get("AGENT_ASSIST_BASE_URL") or os.environ.get("CCAGENT_ASSIST_URL", "")).rstrip("/")
        self.key = os.environ.get("AGENT_ASSIST_API_KEY") or os.environ.get("CCAGENT_ASSIST_API_KEY", "")
        self._http = None

    @property
    def configured(self):
        return bool((self.deployment.features.crm or self.deployment.features.knowledge) and self.endpoint and self.key)

    def enabled(self, action):
        return self.deployment.features.knowledge if action == "knowledge" else self.deployment.features.crm

    async def close(self):
        if self._http is not None and not self._http.closed:
            await self._http.close()

    @staticmethod
    def request_identity(session, action, arguments):
        phone = getattr(session, "customer_phone", None)
        if phone is not None and (not isinstance(phone, str) or not re.fullmatch(r"\+[1-9]\d{6,14}", phone)):
            raise ValueError("The bound customer phone must be in E.164 format.")
        subject = phone or session._room_name
        owner = ("profile:" if phone else "session:") + hashlib.sha256(subject.encode()).hexdigest()
        canonical = json.dumps(arguments, sort_keys=True, ensure_ascii=False)
        operation = hashlib.sha256(f"{session._room_name}:{action}:{canonical}".encode()).hexdigest()
        return owner, operation

    @staticmethod
    def creation_lookup_reference(session, arguments) -> str | None:
        if (
            session is None or not isinstance(arguments, dict)
            or set(arguments) != set(DEFINITIONS["create_case"][2])
            or arguments.get("confirmed") is not True
        ):
            return None
        try:
            owner, operation_id = CaseServiceClient.request_identity(session, "create_report", arguments)
        except (ValueError, TypeError, AttributeError):
            return None
        return "OP-" + hashlib.sha256(f"{owner}:{operation_id}".encode()).hexdigest().upper()

    async def fetch_customer_context(self, session: "RTMiddleTier") -> CustomerContext:
        token = CURRENT_SESSION.set(session)
        try:
            result = await self.execute("customer_context", {})
            return parse_customer_context(result, expected_phone=session.customer_phone)
        finally:
            CURRENT_SESSION.reset(token)

    async def execute(self, action, arguments):
        if not self.enabled(action):
            raise ValueError(f"{'Knowledge' if action == 'knowledge' else 'CRM'} module is disabled.")
        if not self.configured:
            raise ValueError("Service tools are currently unavailable.")
        properties = next((properties for operation, _, properties in self.definitions.values() if operation == action), None)
        if properties is None or not isinstance(arguments, dict) or set(arguments) != set(properties):
            raise ValueError("Provide exactly the declared service tool arguments.")
        if action == "create_report" and arguments.get("category") not in self.deployment.business.caseCategories:
            raise ValueError("Choose a configured case category.")
        if action == "knowledge" and not self.deployment.knowledge.allowedDomains:
            raise ValueError("Knowledge retrieval requires deployment-approved domains.")
        if action in {"create_report", "add_followup"} and arguments["confirmed"] is not True:
            raise ValueError("Explicit confirmation is required before recording a request or follow-up.")
        session = CURRENT_SESSION.get()
        args = dict(arguments)
        payload = {"action": action}
        if action in {"create_report", "resolve_location"}:
            payload["location"] = {key: args.pop(key, "") for key in ("district", "street", "landmark")}
        if "reference" in args:
            if not isinstance(args["reference"], str):
                raise ValueError("A valid request reference is required.")
            args["reference"] = args["reference"].strip().upper()
        payload.update(args)
        if action == "knowledge":
            payload["locale"] = getattr(session, "language", self.deployment.organization.defaultLanguage)
            payload["sensitiveTerms"] = [
                value for value in (
                    getattr(session, "customer_name", None),
                    getattr(session, "customer_email", None),
                    getattr(session, "customer_phone", None),
                ) if value
            ]
        elif action != "resolve_location":
            if session is None:
                raise ValueError("This action requires an active voice conversation.")
            owner, operation_id = self.request_identity(session, action, arguments)
            payload["actorId"] = owner
            if action in {"create_report", "add_followup"}:
                payload["operationId"] = operation_id
            native_id = getattr(session, "native_conversation_id", None)
            if native_id is not None:
                if (
                    not isinstance(native_id, str)
                    or not re.fullmatch(r"[0-9a-fA-F]{8}(-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", native_id)
                    or not UUID(native_id).int
                ):
                    raise ValueError("The bound native conversation ID is invalid.")
                payload["nativeConversationId"] = str(UUID(native_id))
        selection_revision = None
        if action in {"create_report", "get_report", "add_followup"}:
            selection_revision = getattr(session, "_case_selection_revision", 0) + 1
            session._case_selection_revision = selection_revision
            session.selected_case = None
        if self._http is None or self._http.closed:
            self._http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=18))
        async with self._http.post(
            f"{self.endpoint}/api/cases", headers={"X-API-Key": self.key},
            json=payload, allow_redirects=False,
        ) as response:
            result = await response.json()
            if not isinstance(result, dict):
                raise ValueError("The service returned an invalid response.")
            if response.status != 200:
                detail = result.get("error")
                message = detail.get("message") if isinstance(detail, dict) else None
                if not isinstance(message, str):
                    message = "Service request failed."
                raise CaseServiceError(message, detail.get("code") if isinstance(detail, dict) else None, response.status)
            if action not in {"knowledge", "resolve_location"} and result.get("success") is True:
                if result.get("source") != "dynamics":
                    raise ValueError("The service did not return a Dynamics result.")
            encoded = json.dumps(result, ensure_ascii=False)
            if len(encoded.encode()) > MAX_RESULT_BYTES:
                raise ValueError("The response is too large. Ask a narrower question or check the request status.")
            if selection_revision is not None and result.get("success") is True:
                selected = parse_case_receipt(
                    result, requested_reference=payload.get("reference"),
                    native_conversation_id=payload.get("nativeConversationId"),
                )
                bound_id = getattr(session, "native_conversation_id", None)
                if (bound_id.lower() if isinstance(bound_id, str) else bound_id) != selected.native_conversation_id:
                    raise ValueError("The native conversation changed during the case lookup; no case was selected.")
                if selection_revision == session._case_selection_revision and not getattr(session, "transfer_active", False):
                    session.selected_case = selected
            return encoded

    def tools(self):
        result = {}
        for name, (action, description, properties) in self.definitions.items():
            if not self.enabled(action):
                continue
            async def run(arguments, operation=action):
                try:
                    text = await self.execute(operation, arguments)
                except (aiohttp.ClientError, TimeoutError, ValueError) as error:
                    logger.error("Case service %s failed (%s)", operation, type(error).__name__)
                    lookup_reference = None
                    session = CURRENT_SESSION.get()
                    if operation == "create_report" and self.configured:
                        lookup_reference = self.creation_lookup_reference(session, arguments)
                    elif operation == "get_report" and isinstance(arguments, dict):
                        reference = str(arguments.get("reference", "")).strip().upper()
                        if re.fullmatch(r"OP-[A-F0-9]{64}", reference):
                            lookup_reference = reference
                    unknown = isinstance(error, CaseServiceError) and (
                        error.code in {"CRM_OUTCOME_UNKNOWN", "CRM_WRITE_UNCONFIRMED"}
                        or (operation in {"create_report", "add_followup"} and error.status >= 500)
                    )
                    unknown |= operation in {"create_report", "add_followup"} and isinstance(error, (aiohttp.ClientError, TimeoutError))
                    text = json.dumps({
                        "success": False,
                        "lookup_reference": lookup_reference,
                        "outcome": "unknown" if unknown else "rejected",
                        **({"instruction": "The write may already exist. Do not say it was not recorded or definitely failed. Check the same operation reference before another action."} if unknown else {}),
                        "error": str(error) if isinstance(error, ValueError) else "The service could not confirm the outcome. Check status before repeating an action.",
                    })
                return ToolResult(text, ToolResultDirection.TO_SERVER)
            result[name] = Tool(target=run, schema={
                "type": "function", "name": name, "description": description,
                "parameters": {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False},
            })
        return result
