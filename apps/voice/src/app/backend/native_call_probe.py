"""Authenticated call-metadata observer with an optional lease. No call control."""

import hmac
import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from uuid import UUID

from aiohttp import web


logger = logging.getLogger("native_call_probe")
PATH = "/dynamics/handover/probe/events"
RETENTION = timedelta(minutes=15)
MAX_EVENTS = 128
MAX_BODY_BYTES = 64 * 1024
EVENT_TYPES = {"Microsoft.Communication.IncomingCall"}


def _time(value):
    if not isinstance(value, str):
        raise ValueError("An explicit UTC timestamp is required.")
    try:
        result = datetime.fromisoformat(value)
        if result.tzinfo is None:
            raise ValueError
        return result.astimezone(timezone.utc)
    except (ValueError, OverflowError) as error:
        raise ValueError("An explicit timestamp with a time zone is required.") from error


class NativeCallProbe:
    def __init__(self, resource_id="", api_key="", expires_at="", *, permanent=False):
        if not isinstance(permanent, bool):
            raise ValueError("Native call probe permanent mode must be a boolean.")
        if permanent and expires_at:
            raise ValueError("Permanent native call observation must not also specify an expiry.")
        self.resource_id = resource_id
        self._api_key = api_key
        self.expires_at = _time(expires_at) if expires_at else None
        self.permanent = permanent
        self._events: dict[str, dict] = {}
        if resource_id or api_key or expires_at or permanent:
            if (
                not isinstance(resource_id, str)
                or not re.fullmatch(
                    r"/subscriptions/[0-9a-f-]{36}/resourceGroups/[A-Za-z0-9_.()-]+/"
                    r"providers/Microsoft\.Communication/communicationServices/[A-Za-z0-9-]+",
                    resource_id, re.IGNORECASE,
                )
                or not isinstance(api_key, str) or len(api_key) < 32
                or "\r" in api_key or "\n" in api_key
                or (not permanent and self.expires_at is None)
            ):
                raise ValueError("Native call probe requires its ACS resource ID, a dedicated 32-character key and either an expiry or explicit permanent mode.")

    @classmethod
    def from_env(cls):
        permanent = os.environ.get("DYNAMICS_NATIVE_PROBE_PERMANENT", "false").strip().lower()
        if permanent not in {"true", "false"}:
            raise ValueError("DYNAMICS_NATIVE_PROBE_PERMANENT must be true or false.")
        return cls(
            os.environ.get("DYNAMICS_NATIVE_PROBE_RESOURCE_ID", ""),
            os.environ.get("DYNAMICS_NATIVE_PROBE_API_KEY", ""),
            os.environ.get("DYNAMICS_NATIVE_PROBE_EXPIRES_AT", ""),
            permanent=permanent == "true",
        )

    @property
    def enabled(self):
        return self.permanent or (self.expires_at is not None and datetime.now(timezone.utc) < self.expires_at)

    def health(self):
        return {"enabled": self.enabled, "permanent": self.permanent, "retention_seconds": int(RETENTION.total_seconds())}

    def clear(self):
        self._events.clear()

    def _prune(self, now):
        self._events = {
            key: value for key, value in self._events.items()
            if now - _time(value["eventTime"]) <= RETENTION
        }

    def find_call(self, correlation_id: str) -> str | None:
        if not self.enabled:
            raise ValueError("The native call observer is disabled or expired.")
        correlation_id = str(UUID(correlation_id))
        self._prune(datetime.now(timezone.utc))
        locators = {
            event["data"]["serverCallId"] for event in self._events.values()
            if event["data"]["correlationId"] == correlation_id
        }
        if len(locators) > 1:
            raise ValueError("The native call correlation has multiple server locators.")
        return next(iter(locators), None)

    def observe(self, batch, now):
        if not isinstance(batch, list) or not 1 <= len(batch) <= 32:
            raise ValueError("Expected an Event Grid batch with 1-32 events.")
        accepted = {}
        ignored_expired = 0
        for event in batch:
            if not isinstance(event, dict):
                raise ValueError("Expected an Event Grid event object.")
            topic = event.get("topic")
            if not isinstance(topic, str) or topic.lower() != self.resource_id.lower():
                raise ValueError("Unexpected ACS event source.")
            data = event.get("data")
            if not isinstance(data, dict):
                raise ValueError("Event data must be an object.")
            if event.get("eventType") == "Microsoft.EventGrid.SubscriptionValidationEvent":
                code = data.get("validationCode")
                if len(batch) != 1 or not isinstance(code, str) or not code or len(code) > 256:
                    raise ValueError("Invalid Event Grid validation request.")
                return {"validationResponse": code}
            if event.get("eventType") not in EVENT_TYPES:
                raise ValueError("Only incoming-call metadata is accepted.")
            event_id = event.get("id")
            server_id = data.get("serverCallId")
            correlation = data.get("correlationId")
            if (
                not isinstance(event_id, str) or not event_id or len(event_id) > 128
                or not isinstance(server_id, str) or not server_id or len(server_id) > 4096
                or not isinstance(correlation, str)
            ):
                raise ValueError("Call metadata identifiers are missing or too large.")
            try:
                correlation = str(UUID(correlation))
            except ValueError as error:
                raise ValueError("Call correlation ID is invalid.") from error
            event_time = _time(event.get("eventTime"))
            if event_time > now + timedelta(seconds=60):
                raise ValueError("Call event timestamp is in the future.")
            if now - event_time > RETENTION:
                ignored_expired += 1
                continue
            sanitized = {
                "id": event_id, "topic": topic, "eventType": event["eventType"],
                "eventTime": event_time.isoformat(),
                "data": {"serverCallId": server_id, "correlationId": correlation},
            }
            previous = accepted.get(event_id) or self._events.get(event_id)
            if previous is not None and previous != sanitized:
                raise ValueError("Conflicting redelivery of a native call event.")
            accepted[event_id] = sanitized
        self._prune(now)
        if len(self._events.keys() | accepted.keys()) > MAX_EVENTS:
            raise ValueError("Native metadata buffer is full; wait for expiry before testing.")
        self._events.update(accepted)
        return {"accepted": len(accepted), "ignored_expired": ignored_expired}

    async def handle(self, request):
        if not self.enabled:
            self.clear()
            return web.json_response({"error": "Native call probe is disabled or expired."}, status=503)
        supplied = request.headers.get("X-API-Key", "")
        if not hmac.compare_digest(supplied.encode(), self._api_key.encode()):
            logger.warning("Rejected unauthenticated native call probe request")
            return web.json_response({"error": "A valid native probe API key is required."}, status=401)
        now = datetime.now(timezone.utc)
        try:
            if request.method == "GET":
                correlation = str(UUID(request.query.get("correlation_id", "")))
                self._prune(now)
                return web.json_response({
                    "events": [event for event in self._events.values() if event["data"]["correlationId"] == correlation],
                }, headers={"Cache-Control": "no-store"})
            body = bytearray()
            async for chunk in request.content.iter_chunked(8192):
                body.extend(chunk)
                if len(body) > MAX_BODY_BYTES:
                    return web.json_response({"error": "Probe event payload exceeds 64 KiB."}, status=413)
            try:
                batch = json.loads(body)
            except (ValueError, UnicodeDecodeError) as error:
                raise ValueError("Event Grid payload is not valid JSON.") from error
            result = self.observe(batch, now)
            logger.info("Native call metadata received: %s", {
                key: value for key, value in result.items() if key != "validationResponse"
            })
            return web.json_response(result)
        except ValueError as error:
            logger.warning("Invalid native call metadata request (%s)", type(error).__name__)
            return web.json_response({"error": str(error)}, status=400)

    def register(self, app):
        app.router.add_get(PATH, self.handle)
        app.router.add_post(PATH, self.handle)
