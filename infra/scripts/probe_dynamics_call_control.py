"""Bounded native-call attachment diagnostic. Never dials or transfers a customer."""

import argparse
import asyncio
import hashlib
import json
import re
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass, fields
from pathlib import Path
from uuid import UUID, uuid4

from native_voice_setup import organization, read_private, voice_origin

MAX_BYTES = 256 * 1024


@dataclass(frozen=True)
class ProbeScope:
    tenant_id: str
    org_url: str
    native_acs_resource_id: str
    workstream_id: str
    queue_id: str
    callback_url: str

    @property
    def subscription_id(self):
        return self.native_acs_resource_id.split("/")[2]

    @property
    def voice_origin(self):
        return self.callback_url.removesuffix("/acs/call-events")


class ProbeError(RuntimeError):
    pass


def guid(value):
    if not isinstance(value, str) or not re.fullmatch(
        r"[0-9a-fA-F]{8}(-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", value
    ):
        raise ProbeError("A required native identifier is not a GUID.")
    parsed = UUID(value)
    if not parsed.int:
        raise ProbeError("An empty GUID cannot identify a call.")
    return str(parsed)


def load_scope(path):
    value = json.loads(read_private(path))
    if not isinstance(value, dict) or set(value) != {field.name for field in fields(ProbeScope)}:
        raise ProbeError("Provide the complete private scope described by probe-scope.example.json.")
    if any(not isinstance(item, str) or not item or item != item.strip() for item in value.values()):
        raise ProbeError("Every probe scope value must be an explicit, trimmed string.")
    for key in ("tenant_id", "workstream_id", "queue_id"):
        value[key] = guid(value[key])
    value["org_url"] = organization(value["org_url"])
    resource = re.fullmatch(
        r"/subscriptions/([^/]+)/resourceGroups/[A-Za-z0-9_.()-]+/providers/"
        r"Microsoft\.Communication/communicationServices/[A-Za-z0-9-]+",
        value["native_acs_resource_id"],
    )
    if not resource:
        raise ProbeError("Supply the exact native ACS resource ID.")
    guid(resource[1])
    if not value["callback_url"].endswith("/acs/call-events"):
        raise ProbeError("Use the deployed voice application's /acs/call-events callback.")
    voice_origin(value["callback_url"].removesuffix("/acs/call-events"))
    return ProbeScope(**value)


def timestamp(value):
    if not isinstance(value, str):
        raise ProbeError("Native event timestamp is missing.")
    try:
        result = datetime.fromisoformat(value)
        if result.tzinfo is None:
            raise ValueError
        return result.astimezone(timezone.utc)
    except (ValueError, OverflowError) as error:
        raise ProbeError("Native event timestamp is invalid.") from error


def az_json(*arguments):
    result = subprocess.run(
        ["az", *arguments, "--output", "json", "--only-show-errors"],
        capture_output=True, text=True, timeout=60, check=False,
    )
    if result.returncode:
        raise ProbeError(f"Azure CLI {arguments[0]} operation failed; no secret-bearing output was printed.")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise ProbeError("Azure CLI returned an invalid JSON response.") from error


def read_json(url, headers=None):
    request = urllib.request.Request(url, headers=headers or {"Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            raw = response.read(MAX_BYTES + 1)
    except urllib.error.HTTPError as error:
        raise ProbeError(f"Read-only service request failed: HTTP {error.code}.") from error
    except urllib.error.URLError as error:
        raise ProbeError("Read-only service request could not connect.") from error
    if len(raw) > MAX_BYTES:
        raise ProbeError("Read-only response exceeded the diagnostic size limit.")
    try:
        return json.loads(raw)
    except (ValueError, UnicodeDecodeError) as error:
        raise ProbeError("Read-only service returned invalid JSON.") from error


def inspect_conversation(read, conversation_id, now, *, scope):
    conversation_id = guid(conversation_id)
    fields = "msdyn_ocliveworkitemid,_msdyn_liveworkstreamid_value,_msdyn_cdsqueueid_value,statuscode,createdon"
    item = read(f"msdyn_ocliveworkitems({conversation_id})?$select={fields}")
    if (
        not isinstance(item, dict)
        or item.get("msdyn_ocliveworkitemid") != conversation_id
        or item.get("_msdyn_liveworkstreamid_value") != scope.workstream_id
        or item.get("_msdyn_cdsqueueid_value") != scope.queue_id
        or item.get("statuscode@OData.Community.Display.V1.FormattedValue") != "Active"
    ):
        raise ProbeError("The exact conversation is not an active call in the configured PoC workstream and queue.")
    age = now - timestamp(item.get("createdon"))
    if age < timedelta(seconds=-60) or age > timedelta(minutes=10):
        raise ProbeError("Use a fresh, explicitly agreed test call, no older than ten minutes.")
    query = urllib.parse.urlencode({
        "$select": "msdyn_callsid",
        "$filter": f"_msdyn_liveworkitemid_value eq {conversation_id}",
        "$top": "2",
    })
    document = read("msdyn_ocphonecallengagementctxes?" + query)
    if not isinstance(document, dict):
        raise ProbeError("The native phone-context response is not an object.")
    rows = document.get("value")
    if (
        not isinstance(rows, list) or len(rows) != 1
        or not isinstance(rows[0], dict) or "@odata.nextLink" in document
    ):
        raise ProbeError("The native conversation does not have one unambiguous phone-call context.")
    return guid(rows[0].get("msdyn_callsid"))


def decode_events(document):
    events = document.get("events") if isinstance(document, dict) else None
    if (
        not isinstance(events, list) or len(events) > 128
        or not all(isinstance(event, dict) for event in events)
    ):
        raise ProbeError("The diagnostic observer returned invalid or excessive events.")
    return events


def select_native_call(events, call_id, now, *, scope):
    call_id = guid(call_id)
    candidates = {}
    ended = set()
    for event in events:
        topic = event.get("topic") if isinstance(event, dict) else None
        if not isinstance(topic, str) or topic.lower() != scope.native_acs_resource_id.lower():
            raise ProbeError("An event does not belong to the pinned Dynamics ACS resource.")
        event_type = event.get("eventType")
        if event_type not in {
            "Microsoft.Communication.IncomingCall",
            "Microsoft.Communication.CallStarted",
            "Microsoft.Communication.CallEnded",
        }:
            raise ProbeError("The observer received an unexpected event type.")
        age = now - timestamp(event.get("eventTime"))
        if age < timedelta(seconds=-60) or age > timedelta(minutes=10):
            continue
        data = event.get("data")
        if not isinstance(data, dict):
            raise ProbeError("Native call event data is missing.")
        server_id = data.get("serverCallId")
        if not isinstance(server_id, str) or not server_id or len(server_id) > 4096:
            raise ProbeError("Native event has no usable server call locator.")
        if event_type == "Microsoft.Communication.CallEnded":
            ended.add(server_id)
            continue
        correlation = data.get("correlationId")
        if not isinstance(correlation, str):
            raise ProbeError("Native call event has no correlation ID.")
        if correlation.lower() == call_id:
            candidates[server_id] = {"server_call_id": server_id, "correlation_id": call_id}
    candidates = {key: value for key, value in candidates.items() if key not in ended}
    if len(candidates) != 1:
        raise ProbeError("No unique native ACS call event matches the phone context. Do not guess by time or number.")
    return next(iter(candidates.values()))


async def attach_and_leave(automation, target, *, scope):
    from azure.core.exceptions import AzureError
    from azure.communication.callautomation import CallConnectionState

    connection = None
    result = {"connected": False, "own_leg_detached": False, "telephone_calls_created": 0}
    try:
        async with asyncio.timeout(15):
            properties = await automation.connect_call(
                server_call_id=target["server_call_id"],
                callback_url=scope.callback_url,
                operation_context="native-control-probe-" + uuid4().hex,
            )
            if not properties.call_connection_id:
                raise ProbeError("ACS did not return an application connection ID; attachment outcome is unknown.")
            connection = automation.get_call_connection(properties.call_connection_id)
            for attempt in range(6):
                current = await connection.get_call_properties()
                if current.server_call_id and current.server_call_id != target["server_call_id"]:
                    raise ProbeError("ACS returned a different server call; the probe is stopping.")
                if current.call_connection_state == CallConnectionState.CONNECTED:
                    if current.server_call_id != target["server_call_id"]:
                        raise ProbeError("Connected application has no matching native server call locator.")
                    result["connected"] = True
                    result["application_connection_id"] = properties.call_connection_id
                    result["participant_count"] = sum([1 async for _ in connection.list_participants()])
                    break
                if current.call_connection_state != CallConnectionState.CONNECTING:
                    raise ProbeError("The probe connection ended or entered an unexpected call state.")
                if attempt < 5:
                    await asyncio.sleep(1)
            if not result["connected"]:
                raise ProbeError("ACS did not confirm the application joined within the probe window.")
    except (AzureError, TimeoutError) as error:
        status = getattr(error, "status_code", None)
        detail = str(error).replace(target["server_call_id"], "[native call locator]")
        detail = re.sub(r"\+\d{7,15}", "[phone]", detail)
        detail = re.sub(r"eyJ[A-Za-z0-9_.-]+", "[token]", detail)[:600]
        raise ProbeError(
            f"Native call attachment was not confirmed ({type(error).__name__}, HTTP {status}). "
            f"{detail} No retry was made. End the test call if it remains open."
        ) from error
    finally:
        if connection is not None:
            try:
                async with asyncio.timeout(10):
                    await connection.hang_up(is_for_everyone=False)
                result["own_leg_detached"] = True
            except (AzureError, TimeoutError) as error:
                raise ProbeError("Could not confirm removal of the probe's own leg. End the test call now.") from error
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True, help="Owner-only private target scope JSON; no default tenant or workstream.")
    parser.add_argument("--conversation-id", required=True, type=UUID)
    parser.add_argument(
        "--env-file", type=Path,
        required=True,
        help="Private azd environment containing the native probe API key.",
    )
    parser.add_argument("--execute", action="store_true", help="Briefly attach to the selected live call; never dial.")
    args = parser.parse_args()
    scope = load_scope(args.config)
    read_private(args.env_file)
    account = az_json("account", "show", "--subscription", scope.subscription_id)
    if account.get("tenantId", "").lower() != scope.tenant_id:
        raise ProbeError("Wrong Azure tenant; no probe requested.")
    from dotenv import dotenv_values
    key = dotenv_values(args.env_file, interpolate=False).get("DYNAMICS_NATIVE_PROBE_API_KEY")
    if not key or len(key) < 32:
        raise ProbeError("The dedicated probe API key is missing; no attachment requested.")
    health = read_json(scope.voice_origin + "/health")
    if health.get("automatic_phone_conference_enabled") is not False:
        raise ProbeError("Automatic phone conferencing is not confirmed disabled; no attachment permitted.")
    if health.get("native_call_probe", {}).get("enabled") is not True:
        raise ProbeError("The native metadata observer is disabled or expired.")
    auth = az_json("account", "get-access-token", "--tenant", scope.tenant_id, "--resource", scope.org_url)
    headers = {
        "Authorization": "Bearer " + auth["accessToken"], "Accept": "application/json",
        "Prefer": 'odata.include-annotations="OData.Community.Display.V1.FormattedValue"',
    }

    def read(path):
        return read_json(scope.org_url + "/api/data/v9.2/" + path, headers)

    conversation_id = str(args.conversation_id)
    call_id = inspect_conversation(read, conversation_id, datetime.now(timezone.utc), scope=scope)
    observed = read_json(
        scope.voice_origin + "/dynamics/handover/probe/events?correlation_id=" + call_id,
        {"X-API-Key": key, "Accept": "application/json"},
    )
    target = select_native_call(decode_events(observed), call_id, datetime.now(timezone.utc), scope=scope)
    summary = {
        "conversation_id": conversation_id, "native_correlation_verified": True,
        "locator_fingerprint": hashlib.sha256(target["server_call_id"].encode()).hexdigest()[:16],
        "attachment_requested": args.execute, "native_human_handover": "not_tested",
    }
    if args.execute:
        if inspect_conversation(read, conversation_id, datetime.now(timezone.utc), scope=scope) != call_id:
            raise ProbeError("Native phone context changed; no attachment requested.")
        keys = az_json(
            "rest", "--method", "post",
            "--url", f"https://management.azure.com{scope.native_acs_resource_id}/listKeys?api-version=2023-04-01",
            "--subscription", scope.subscription_id,
        )

        async def run():
            from azure.communication.callautomation.aio import CallAutomationClient
            async with CallAutomationClient.from_connection_string(
                keys["primaryConnectionString"], retry_total=0,
                connection_timeout=5, read_timeout=5,
            ) as client:
                return await attach_and_leave(client, target, scope=scope)

        summary.update(asyncio.run(run()))
        summary["native_record_active_after_detach"] = (
            inspect_conversation(read, conversation_id, datetime.now(timezone.utc)) == call_id
        )
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    try:
        main()
    except (ProbeError, subprocess.TimeoutExpired) as error:
        print(json.dumps({"status": "stopped", "error": str(error) if isinstance(error, ProbeError) else "Azure CLI timed out."}))
        raise SystemExit(1)
