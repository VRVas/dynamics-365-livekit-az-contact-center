"""Authenticated control plane for Dynamics-managed proactive voice deliveries."""

import hmac
import json
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

from aiohttp import web
from backend.dataverse import DataverseClient, DataverseError as OutboundError

logger = logging.getLogger("dynamics_outbound")
MAX_REQUEST_BYTES = 16 * 1024
PREFIX = "/dynamics/outbound"


def _uuid(value: Any, name: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(
        r"[0-9a-fA-F]{8}(-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", value
    ):
        raise ValueError(f"{name} must be a UUID.")
    identifier = UUID(value)
    if identifier.int == 0:
        raise ValueError(f"{name} must not be the empty UUID.")
    return str(identifier)


def _timestamp(value: Any, name: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be an ISO 8601 timestamp with a time zone.")
    try:
        result = datetime.fromisoformat(value)
        if result.tzinfo is None:
            raise ValueError(f"{name} must include a time zone.")
        return result.astimezone(timezone.utc)
    except (ValueError, OverflowError) as error:
        raise ValueError(f"{name} must be an ISO 8601 timestamp with a time zone.") from error


def _utc(value: datetime) -> str:
    return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")


@dataclass(frozen=True)
class DynamicsOutboundConfig:
    org_url: str = ""
    engagement_id: str = ""
    api_key: str = field(default="", repr=False)
    enabled: bool = False
    tenant_id: str | None = None

    def __post_init__(self):
        if self.org_url:
            url = urlsplit(self.org_url)
            if (
                url.scheme != "https"
                or not re.fullmatch(r"[a-z0-9-]+\.crm\d*\.dynamics\.com", url.netloc)
                or url.path or url.query or url.fragment
            ):
                raise ValueError("DYNAMICS_ORG_URL must be an HTTPS Dataverse organization URL without a path.")
        if self.engagement_id:
            _uuid(self.engagement_id, "DYNAMICS_OUTBOUND_CONFIG_ID")
        if self.api_key and (len(self.api_key) < 32 or "\r" in self.api_key or "\n" in self.api_key):
            raise ValueError("DYNAMICS_OUTBOUND_API_KEY must contain at least 32 characters and no line breaks.")
        if self.tenant_id:
            _uuid(self.tenant_id, "AZURE_TENANT_ID")
        if self.enabled and not (self.configured and self.engagement_id):
            raise ValueError("Enabling Dynamics outbound requires an organization URL, API key and engagement ID.")

    @property
    def configured(self) -> bool:
        return bool(self.org_url and self.api_key)

    @classmethod
    def from_env(cls) -> "DynamicsOutboundConfig":
        enabled = os.environ.get("DYNAMICS_OUTBOUND_ENABLED", "false").lower()
        if enabled not in {"true", "false"}:
            raise ValueError("DYNAMICS_OUTBOUND_ENABLED must be true or false.")
        return cls(
            org_url=os.environ.get("DYNAMICS_ORG_URL", "").strip().rstrip("/"),
            engagement_id=os.environ.get("DYNAMICS_OUTBOUND_CONFIG_ID", "").strip().lower(),
            api_key=os.environ.get("DYNAMICS_OUTBOUND_API_KEY", ""),
            enabled=enabled == "true",
            tenant_id=os.environ.get("AZURE_TENANT_ID") or None,
        )


@dataclass(frozen=True)
class OutboundRequest:
    request_id: str
    contact_id: str
    phone_number: str
    not_before: datetime
    expires_at: datetime
    language: str
    context: dict[str, str]

    @classmethod
    def parse(cls, body: Any, *, now: datetime | None = None) -> "OutboundRequest":
        allowed = {
            "request_id", "contact_id", "phone_number", "not_before", "expires_at",
            "consent_confirmed", "language", "context",
        }
        if not isinstance(body, dict) or set(body) - allowed:
            raise ValueError("Provide an outbound request object containing only the documented fields.")
        if body.get("consent_confirmed") is not True:
            raise ValueError("consent_confirmed must be true for this customer and purpose.")
        request_id = _uuid(body.get("request_id"), "request_id")
        contact_id = _uuid(body.get("contact_id"), "contact_id")
        number = body.get("phone_number")
        if not isinstance(number, str) or not re.fullmatch(r"\+[1-9]\d{6,14}", number):
            raise ValueError("phone_number must be an E.164 telephone number.")
        start = _timestamp(body.get("not_before"), "not_before")
        end = _timestamp(body.get("expires_at"), "expires_at")
        now = now or datetime.now(timezone.utc)
        if end <= now or end <= start or end > now + timedelta(days=30):
            raise ValueError("expires_at must be after now and not_before, and at most 30 days ahead.")
        language = body.get("language", "ar")
        if not isinstance(language, str) or language not in {"ar", "en"}:
            raise ValueError("language must be ar or en.")
        context = body.get("context", {})
        if not isinstance(context, dict) or len(context) > 16:
            raise ValueError("context must contain at most 16 string attributes.")
        for key, value in context.items():
            if (
                not isinstance(key, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", key)
                or key.lower().startswith("moacc_")
                or not isinstance(value, str) or len(value) > 512
            ):
                raise ValueError("Context names must be identifiers without the moacc_ prefix; values are strings up to 512 characters.")
        if len(json.dumps(context, ensure_ascii=False).encode()) > 4096:
            raise ValueError("context must not exceed 4 KiB.")
        return cls(request_id, contact_id, number, start, end, language, dict(context))

    def native_payload(self, engagement_id: str) -> dict[str, Any]:
        contact = {
            "uniqueIdentifier": self.contact_id,
            "mobilePhoneNumber": self.phone_number,
            "timeZone": "UTC",
            "inputAttributes": {
                **self.context,
                "moacc_request_id": self.request_id,
                "moacc_language": self.language,
            },
        }
        return {
            "ApiVersion": "1.0",
            "ProactiveEngagementConfigId": engagement_id,
            "RequestId": self.request_id,
            "EarliestScheduleDate": _utc(self.not_before),
            "ExpirationDate": _utc(self.expires_at),
            "Contacts": json.dumps([contact], ensure_ascii=False, separators=(",", ":")),
        }


class DynamicsOutboundClient(DataverseClient):
    def __init__(self, config: DynamicsOutboundConfig):
        super().__init__(config.org_url, config.tenant_id)
        self.config = config

    def health(self) -> dict[str, bool]:
        return {
            "configured": self.config.configured,
            "submission_enabled": self.config.enabled,
            "engagement_selected": bool(self.config.engagement_id),
        }

    async def _request(
        self, method: str, path: str, *, body: dict | None = None, params: dict | None = None,
    ) -> dict[str, Any]:
        if not self.config.configured:
            raise OutboundError("not_configured", "The Dynamics outbound connector is not configured.", 503)
        return await super()._request(method, path, body=body, params=params)

    async def _engagement(self) -> dict[str, Any]:
        if not self.config.engagement_id:
            raise OutboundError("engagement_not_selected", "Select a native proactive engagement before submitting requests.", 503)
        return await self._request(
            "GET", f"msdyn_proactive_engagement_configs({self.config.engagement_id})",
            params={"$select": "msdyn_name,statecode,msdyn_type,msdyn_dialmode_type,msdyn_engagement_type,_msdyn_workstream_value"},
        )

    @staticmethod
    def _configuration_blockers(engagement: dict[str, Any]) -> list[str]:
        blockers = []
        if engagement.get("statecode") != 0:
            blockers.append("engagement_not_active")
        if engagement.get("msdyn_type") != 100000000:
            blockers.append("engagement_not_voice")
        if engagement.get("msdyn_dialmode_type") != 100000000:
            blockers.append("engagement_not_copilot_mode")
        if engagement.get("msdyn_engagement_type") != 100000100:
            blockers.append("engagement_not_ai_led")
        if not engagement.get("_msdyn_workstream_value"):
            blockers.append("engagement_missing_workstream")
        return blockers

    async def readiness(self) -> dict[str, Any]:
        identity = await self._request("GET", "WhoAmI")
        try:
            _uuid(identity.get("UserId"), "Dynamics user ID")
        except ValueError as error:
            raise OutboundError("invalid_dynamics_response", "Dynamics did not identify the application user.", 502) from error
        blockers = [] if self.config.enabled else ["submission_disabled"]
        engagement = None
        if self.config.engagement_id:
            engagement = await self._engagement()
            blockers.extend(self._configuration_blockers(engagement))
        else:
            blockers.append("engagement_not_selected")
        return {
            **self.health(), "dataverse_connected": True,
            "ready_for_submission": not blockers, "blockers": blockers,
            "engagement_id": self.config.engagement_id or None,
            "engagement_name": engagement.get("msdyn_name") if engagement else None,
            "scope": "Connector and engagement checks only; not a telephone, permissions-to-dial or latency test.",
        }

    async def submit(self, request: OutboundRequest) -> dict[str, str]:
        if not self.config.enabled:
            raise OutboundError("submission_disabled", "New Dynamics outbound submissions are disabled.", 503)
        blockers = self._configuration_blockers(await self._engagement())
        if blockers:
            raise OutboundError("invalid_engagement", "Native engagement is not ready: " + ", ".join(blockers), 409)
        payload = await self._request(
            "POST", "CCaaS_CreateProactiveDelivery",
            body=request.native_payload(self.config.engagement_id),
        )
        try:
            task_id = _uuid(payload.get("DeliveryTaskId"), "DeliveryTaskId")
            delivery_id = _uuid(payload.get("DeliveryId"), "DeliveryId")
        except ValueError as error:
            raise OutboundError(
                "invalid_dynamics_response",
                "Dynamics did not return valid delivery identifiers. Reuse the original request_id if retrying.", 502,
            ) from error
        logger.info("Dynamics accepted request=%s task=%s", request.request_id, task_id)
        return {
            "status": "accepted", "request_id": request.request_id,
            "delivery_task_id": task_id, "delivery_id": delivery_id,
            "status_url": f"{PREFIX}/tasks/{task_id}",
        }

    async def get_task(self, task_id: str) -> dict[str, Any]:
        task_id = _uuid(task_id, "task_id")
        if not self.config.engagement_id:
            raise OutboundError("engagement_not_selected", "Select a proactive engagement to scope task access.", 503)
        payload = await self._request("GET", f"CCaaS_GetDeliveryTasks(DeliveryTaskId={task_id})")
        tasks = payload.get("value")
        if not isinstance(tasks, list):
            raise OutboundError("invalid_dynamics_response", "Dynamics did not return a delivery-task collection.", 502)
        if not tasks:
            raise OutboundError("task_not_found", "The delivery task was not found.", 404)
        if len(tasks) != 1 or not isinstance(tasks[0], dict):
            raise OutboundError("invalid_dynamics_response", "Dynamics returned an unexpected delivery-task collection.", 502)
        task = tasks[0]
        try:
            actual_id = _uuid(task.get("deliveryTaskId"), "deliveryTaskId")
            engagement_id = _uuid(task.get("proactiveEngagementConfigId"), "proactiveEngagementConfigId")
        except ValueError as error:
            raise OutboundError("invalid_dynamics_response", "Dynamics returned a task without valid ownership identifiers.", 502) from error
        if actual_id != task_id:
            raise OutboundError("invalid_dynamics_response", "Dynamics returned a different delivery task.", 502)
        if engagement_id != self.config.engagement_id:
            raise OutboundError("task_not_found", "The delivery task is not part of the configured engagement.", 404)
        if not isinstance(task.get("status"), str) or not task["status"]:
            raise OutboundError("invalid_dynamics_response", "Dynamics returned a task without a status.", 502)
        return task

    async def cancel(self, task_id: str) -> dict[str, str]:
        task = await self.get_task(task_id)
        await self._request("POST", "CCaaS_CancelDeliveryTask", body={
            "ApiVersion": "1.0", "DeliveryTaskId": task["deliveryTaskId"],
        })
        logger.info("Dynamics task cancellation requested task=%s", task["deliveryTaskId"])
        return {
            "status": "cancellation_requested", "delivery_task_id": task["deliveryTaskId"],
            "scope": "Pending and future attempts only; this does not confirm an active call was disconnected.",
        }


def register_dynamics_outbound(app: web.Application, client: DynamicsOutboundClient) -> None:
    async def handler(request: web.Request):
        request_id = None
        try:
            if not client.config.configured:
                raise OutboundError("not_configured", "The Dynamics outbound connector is not configured.", 503)
            supplied_key = request.headers.get("X-API-Key", "")
            if not hmac.compare_digest(supplied_key.encode(), client.config.api_key.encode()):
                raise OutboundError("unauthorized", "A valid outbound API key is required.", 401)
            action = request.match_info.route.name
            if action == "dynamics-outbound-readiness":
                return web.json_response(await client.readiness())
            if action == "dynamics-outbound-submit":
                if request.content_length is not None and request.content_length > MAX_REQUEST_BYTES:
                    raise OutboundError("request_too_large", "Outbound requests must not exceed 16 KiB.", 413)
                raw = await request.read()
                if len(raw) > MAX_REQUEST_BYTES:
                    raise OutboundError("request_too_large", "Outbound requests must not exceed 16 KiB.", 413)
                try:
                    body = json.loads(raw)
                except (ValueError, UnicodeDecodeError) as error:
                    raise ValueError("Provide a valid JSON request body.") from error
                delivery = OutboundRequest.parse(body)
                request_id = delivery.request_id
                return web.json_response(await client.submit(delivery), status=202)
            task_id = _uuid(request.match_info["task_id"], "task_id")
            if action == "dynamics-outbound-cancel":
                if request.can_read_body:
                    raise ValueError("The cancellation endpoint does not accept a request body.")
                return web.json_response(await client.cancel(task_id), status=202)
            return web.json_response({"delivery_task": await client.get_task(task_id)})
        except ValueError as error:
            logger.warning("Invalid Dynamics outbound request: %s", error)
            return web.json_response({"error": {"code": "invalid_request", "message": str(error)}}, status=400)
        except OutboundError as error:
            logger.warning("Dynamics outbound refused code=%s request=%s", error.code, request_id)
            payload: dict[str, Any] = {"error": {"code": error.code, "message": str(error)}}
            if request_id:
                payload["request_id"] = request_id
            headers = {"Retry-After": error.retry_after} if error.retry_after else {}
            return web.json_response(payload, status=error.status, headers=headers)

    app.router.add_get(f"{PREFIX}/readiness", handler, name="dynamics-outbound-readiness")
    app.router.add_post(f"{PREFIX}/requests", handler, name="dynamics-outbound-submit")
    app.router.add_get(f"{PREFIX}/tasks/{{task_id}}", handler, name="dynamics-outbound-status")
    app.router.add_post(f"{PREFIX}/tasks/{{task_id}}/cancel", handler, name="dynamics-outbound-cancel")
