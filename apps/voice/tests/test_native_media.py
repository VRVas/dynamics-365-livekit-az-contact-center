import asyncio
import base64
import json
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from aiohttp import WSMessage, WSMsgType, WSServerHandshakeError, web
from aiohttp.test_utils import TestClient, TestServer
from azure.communication.callautomation import CallConnectionState, CallParticipant, PhoneNumberIdentifier
from azure.communication.identity import CommunicationUserIdentifier as IdentityUser
from azure.core.exceptions import HttpResponseError

from backend.dataverse import DataverseClient, DataverseError
from backend.crm_context import CRMContextError, parse_case_receipt, parse_customer_context
from backend.humain import HumainConfig, HumainError
from backend.native_call_probe import NativeCallProbe
from backend.native_notice import NativeNoticeError, NativeStartupNotice, notice_asset
from backend.native_voip import NativeVoipError, NativeVoipSession
from backend.native_media import (
    PREFIX,
    NativeMediaAccessLogger,
    NativeMediaConfig,
    NativeMediaError,
    NativeMediaPilot,
    NativeMediaSession,
    NativeSpeechControl,
)
from backend.rtmt import FRAME_BYTES, RTMiddleTier, SpeakerRole
from backend.tools.tools import CURRENT_SESSION, Tool
from crm_fixtures import REFERENCE, case_receipt, customer_context, unknown_customer_context


ORG = "https://example.crm17.dynamics.com"
VOICE_URL = "https://voice.example"
KEY = "synthetic-native-media-control-key"
CONVERSATION = "11111111-1111-4111-8111-111111111111"
WORKSTREAM = "22222222-2222-4222-8222-222222222222"
QUEUE = "33333333-3333-4333-8333-333333333333"
BOT = "44444444-4444-4444-8444-444444444444"
CONTROLLER = "55555555-5555-4555-8555-555555555555"
CORRELATION = "66666666-6666-4666-8666-666666666666"
CONNECTION = "77777777-7777-4777-8777-777777777777"
OTHER = "88888888-8888-4888-8888-888888888888"
SERVER_ID = "synthetic-opaque-server-id"
PHONE = "+12025550101"
RESOURCE = (
    f"/subscriptions/{OTHER}/resourceGroups/poc/providers/"
    "Microsoft.Communication/communicationServices/native-acs"
)


def config(**overrides):
    return replace(NativeMediaConfig(
        True, WORKSTREAM, KEY,
        "endpoint=https://native.example;accesskey=" + base64.b64encode(b"synthetic").decode(),
        VOICE_URL,
    ), **overrides)


def voice_factory(phone=PHONE, language="en"):
    session = RTMiddleTier(HumainConfig(authorization="synthetic-provider-key"))
    session.language = language
    session.tools = {"talk_to_human": Tool(
        target=AsyncMock(),
        schema={
            "name": "talk_to_human", "description": "Request a human.",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    )}
    return session


class Fixture:
    def __init__(self):
        self.item = {
            "msdyn_ocliveworkitemid": CONVERSATION,
            "_msdyn_liveworkstreamid_value": WORKSTREAM,
            "_msdyn_cdsqueueid_value": QUEUE,
            "_msdyn_lastsessionid_value": CONTROLLER,
            "_msdyn_activeagentid_value": BOT,
            "createdon": datetime.now(timezone.utc).isoformat(),
            "statuscode@OData.Community.Display.V1.FormattedValue": "Active",
        }
        self.workstream = {
            "msdyn_liveworkstreamid": WORKSTREAM,
            "msdyn_name": "Native test",
            "statecode": 0, "msdyn_direction": 0, "msdyn_streamsource": 192440000,
            "_msdyn_bot_user_value": BOT, "_msdyn_defaultqueue_value": QUEUE,
        }
        self.controller = {
            "activityid": CONTROLLER,
            "_msdyn_liveworkitemid_value": CONVERSATION, "msdyn_sessioncreationreason": 192350000,
            "msdyn_sessionclosedon": None, "msdyn_agentacceptedon": None,
        }
        self.context = {"value": [{"msdyn_callsid": CORRELATION, "msdyn_fromphone": PHONE}]}
        self.dataverse = SimpleNamespace(org_url=ORG, get=AsyncMock(side_effect=self.read), close=AsyncMock())
        self.observer = NativeCallProbe(
            RESOURCE, KEY, (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        )
        self.observer.observe([{
            "id": "native-event", "eventType": "Microsoft.Communication.IncomingCall",
            "topic": RESOURCE, "eventTime": datetime.now(timezone.utc).isoformat(),
            "data": {"serverCallId": SERVER_ID, "correlationId": CORRELATION},
        }], datetime.now(timezone.utc))
        self.pilot = NativeMediaPilot(config(), self.dataverse, self.observer, voice_factory)
        self.connection = SimpleNamespace(
            get_call_properties=AsyncMock(return_value=SimpleNamespace(
                call_connection_state=CallConnectionState.CONNECTED, server_call_id=SERVER_ID,
            )),
            start_media_streaming=AsyncMock(),
            hang_up=AsyncMock(),
        )
        async def detached(**kwargs):
            self.connection.get_call_properties.return_value.call_connection_state = CallConnectionState.DISCONNECTED

        self.connection.hang_up.side_effect = detached
        self.participants = [CallParticipant(identifier=PhoneNumberIdentifier(PHONE))]

        async def participants():
            for participant in self.participants:
                yield participant

        self.connection.list_participants = participants
        self.automation = SimpleNamespace(
            connect_call=AsyncMock(return_value=SimpleNamespace(call_connection_id=CONNECTION)),
            get_call_connection=MagicMock(return_value=self.connection),
            close=AsyncMock(),
        )
        self.pilot._automation = self.automation

    async def read(self, path, *, params=None):
        if path.startswith("msdyn_liveworkstreams("):
            return self.workstream
        if path.startswith("msdyn_ocliveworkitems("):
            return self.item
        if path.startswith("msdyn_ocsessions("):
            return self.controller
        if path == "msdyn_ocphonecallengagementctxes":
            return self.context
        if path == "msdyn_ocsessions":
            return {"value": [self.controller]}
        if path == "msdyn_ocliveworkitems":
            return {"value": []}
        if path == "WhoAmI":
            return {"UserId": OTHER}
        raise AssertionError(f"Unexpected Dataverse request {path}")


class ConfigAndIdentityTests(unittest.TestCase):
    def test_additional_entries_are_an_explicit_unique_uuid_allowlist(self):
        settings = config(additional_workstream_ids=(OTHER,))
        self.assertEqual(settings.workstream_ids, (WORKSTREAM, OTHER))
        for extra in ((WORKSTREAM,), ("bad",), (OTHER, OTHER), (OTHER, ""), OTHER):
            with self.subTest(extra=extra), self.assertRaises((ValueError, NativeMediaError)):
                config(additional_workstream_ids=extra)
        with patch.dict("os.environ", {
            "DYNAMICS_NATIVE_MEDIA_WORKSTREAM_ID": WORKSTREAM,
            "DYNAMICS_NATIVE_MEDIA_ADDITIONAL_WORKSTREAM_IDS": " " + OTHER + " ",
        }, clear=True):
            self.assertEqual(NativeMediaConfig.from_env().workstream_ids, (WORKSTREAM, OTHER))

    def test_fast_start_is_explicit_and_requires_native_voip(self):
        for setting in ("yes", "1"):
            with patch.dict("os.environ", {"DYNAMICS_NATIVE_MEDIA_FAST_START": setting}, clear=True):
                with self.assertRaises(ValueError):
                    NativeMediaConfig.from_env()
        with self.assertRaises(ValueError):
            config(fast_start=True)
        self.assertTrue(config(transport="voip", fast_start=True).fast_start)
        self.assertFalse(config(transport="voip").fast_start)

    def test_disabled_defaults_and_invalid_enabled_config(self):
        with patch.dict("os.environ", {}, clear=True):
            self.assertFalse(NativeMediaConfig.from_env().enabled)
        for environment in (
            {"DYNAMICS_NATIVE_MEDIA_ENABLED": "yes"},
            {"DYNAMICS_NATIVE_MEDIA_ENABLED": "true"},
            {"DYNAMICS_NATIVE_MEDIA_ENABLED": "true", "ACS_AUTO_CONFERENCE_ENABLED": "false"},
        ):
            with self.subTest(environment=environment), patch.dict("os.environ", environment, clear=True), self.assertRaises(ValueError):
                NativeMediaConfig.from_env()

    def test_private_settings_are_not_represented(self):
        settings = config()
        self.assertNotIn(KEY, repr(settings))
        self.assertNotIn(settings.acs_connection_string, repr(settings))
        session = NativeMediaSession(CONVERSATION, "en")
        self.assertNotIn(session.capability, repr(session))
        self.assertNotIn(session.capability, json.dumps(session.snapshot()))

    def test_start_response_matches_copilot_string_and_boolean_schema(self):
        snapshot = NativeMediaSession(CONVERSATION, "en").snapshot()
        for field in ("conversation_id", "phase", "action", "reason"):
            self.assertIsInstance(snapshot[field], str, field)
        self.assertEqual(snapshot["reason"], "")
        self.assertIsInstance(snapshot["azure_leg_detached"], bool)
        self.assertIs(snapshot["startup_failed_unpaused"], False)
        self.assertEqual(snapshot["pilot_limit_seconds"], 1200)

    def test_only_confirmed_unpaused_pre_active_failure_allows_a_native_failure_notice(self):
        session = NativeMediaSession(
            CONVERSATION, "en", phase="stopped", action="stop",
            reason="native_session_pending", detached=True, supervision_contract=1,
        )
        self.assertFalse(session.snapshot()["startup_failed_unpaused"])
        session.done.set()
        self.assertTrue(session.snapshot()["startup_failed_unpaused"])
        for field, value in (
            ("was_active", True), ("failure_notice_suppressed", True),
            ("intervention_latched", True), ("pause_acknowledged", True),
            ("ai_state", "paused"), ("ai_state", "pausing"), ("ai_state", "pause_failed"),
            ("detached", False), ("phase", "failed"), ("action", "quiet_hold"),
            ("reason", "control_requested_stop"), ("reason", "service_shutdown"),
            ("reason", "native_call_not_active"), ("reason", "human_already_assigned"),
            ("reason", "controller_changed"), ("reason", "controller_not_retained"), ("reason", ""),
        ):
            with self.subTest(field=field, value=value):
                previous = getattr(session, field)
                setattr(session, field, value)
                self.assertFalse(session.snapshot()["startup_failed_unpaused"])
                setattr(session, field, previous)

    def test_case_snapshot_defaults_are_bounded_and_never_select_profile_cases(self):
        session = NativeMediaSession(CONVERSATION, "en")
        self.assertEqual(session.snapshot()["selected_case_id"], "")
        self.assertEqual(session.snapshot()["selected_case_number"], "")
        self.assertEqual(session.snapshot()["case_link_status"], "not_selected")
        session.voice = voice_factory()
        session.voice.customer_context = parse_customer_context(json.dumps(customer_context()), expected_phone=PHONE)
        self.assertEqual(session.snapshot()["selected_case_number"], "")
        self.assertFalse(session.snapshot()["case_link_already_linked"])

    def test_selected_case_and_failed_link_are_visible_without_raw_receipt_data(self):
        session = NativeMediaSession(CONVERSATION, "en")
        session.voice = voice_factory()
        receipt = case_receipt(link_status="failed", link_details={
            "error": "CONVERSATION_NOT_ACTIVE", "httpStatus": 409, "stage": "validate",
            "message": "Private note resident@example.com +12025550101",
        })
        receipt["recentActivities"] = [{"note": "Synthetic private activity"}]
        session.voice.selected_case = parse_case_receipt(
            receipt, requested_reference=REFERENCE, native_conversation_id=CONVERSATION,
        )
        for phase in ("starting", "active", "handover_ready"):
            session.phase = phase
            snapshot = session.snapshot()
            self.assertEqual(snapshot["selected_case_id"], receipt["caseId"])
            self.assertEqual(snapshot["selected_case_number"], REFERENCE)
            self.assertEqual(snapshot["case_link_status"], "failed")
            self.assertEqual(snapshot["case_link_error"], "CONVERSATION_NOT_ACTIVE")
            self.assertEqual(snapshot["case_link_stage"], "validate")
            self.assertEqual(snapshot["case_link_http_status"], 409)
            self.assertNotIn("private", json.dumps(snapshot).lower())
            self.assertNotIn("resident@example.com", json.dumps(snapshot))
            self.assertNotIn(PHONE, json.dumps(snapshot))
            self.assertNotIn("recentActivities", snapshot)
            self.assertEqual(snapshot["human_acceptance"], "not_observed_by_media_adapter")
        session.voice.selected_case = parse_case_receipt(
            receipt, requested_reference=REFERENCE, native_conversation_id=OTHER,
        )
        self.assertEqual(session.snapshot()["selected_case_number"], "")

    def test_url_key_and_workstream_validation(self):
        for settings in (
            {"public_url": "http://voice.example"},
            {"public_url": "https://user:secret@voice.example"},
            {"public_url": VOICE_URL + "/path"},
            {"api_key": "short"},
            {"api_key": KEY + "\n"},
            {"workstream_id": "invalid"},
        ):
            with self.subTest(settings=settings), self.assertRaises((ValueError, NativeMediaError)):
                config(**settings)

    def test_media_access_log_never_contains_capability(self):
        logger = MagicMock()
        access = NativeMediaAccessLogger(logger, "%r")
        access.log(SimpleNamespace(
            path=f"{PREFIX}/sessions/{CONVERSATION}/media",
            path_qs=f"{PREFIX}/sessions/{CONVERSATION}/media?token=private-capability",
            remote="127.0.0.1", method="GET",
        ), SimpleNamespace(status=101), 0.1)
        self.assertNotIn("private-capability", str(logger.mock_calls))
        self.assertNotIn(CONVERSATION, str(logger.mock_calls))
        self.assertIn("[redacted]", str(logger.mock_calls))


class NativeMediaTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.f = Fixture()
        self.pilot = self.f.pilot
        self.session = NativeMediaSession(CONVERSATION, "en")
        self.addAsyncCleanup(self.pilot.close)

    async def test_readiness_performs_only_dataverse_reads(self):
        result = await self.pilot.readiness()
        self.assertTrue(result["dataverse_connected"])
        self.assertEqual(result["pilot_limit_seconds"], 1200)
        self.assertEqual(result["workstream"], "Native test")
        self.assertEqual(result["workstreams"], [{"id": WORKSTREAM, "name": "Native test"}])
        self.f.automation.connect_call.assert_not_awaited()

    async def test_readiness_validates_both_explicit_entries(self):
        self.pilot.config = config(additional_workstream_ids=(OTHER,))
        read = self.f.dataverse.get.side_effect

        async def additional(path, *, params=None):
            if path == f"msdyn_liveworkstreams({OTHER})":
                return {**self.f.workstream, "msdyn_liveworkstreamid": OTHER, "msdyn_name": "Second entry"}
            return await read(path, params=params)

        self.f.dataverse.get.side_effect = additional
        result = await self.pilot.readiness()
        self.assertEqual(result["workstreams"], [
            {"id": WORKSTREAM, "name": "Native test"}, {"id": OTHER, "name": "Second entry"},
        ])
        self.f.automation.connect_call.assert_not_awaited()

    async def test_additional_entry_resolves_its_own_workstream_and_queue(self):
        self.pilot.config = config(additional_workstream_ids=(OTHER,))
        self.f.item["_msdyn_liveworkstreamid_value"] = OTHER
        self.f.item["_msdyn_cdsqueueid_value"] = CONNECTION
        self.f.workstream["msdyn_liveworkstreamid"] = OTHER
        self.f.workstream["_msdyn_defaultqueue_value"] = CONNECTION
        workstream, phone = await self.pilot._resolve_call(self.session)
        self.assertEqual(phone, PHONE)
        self.assertEqual(workstream["msdyn_liveworkstreamid"], OTHER)
        self.assertIn(f"msdyn_liveworkstreams({OTHER})", [call.args[0] for call in self.f.dataverse.get.call_args_list])
        self.assertNotIn(f"msdyn_liveworkstreams({WORKSTREAM})", [call.args[0] for call in self.f.dataverse.get.call_args_list])
        self.f.item["_msdyn_liveworkstreamid_value"] = WORKSTREAM
        with self.assertRaisesRegex(NativeMediaError, "not active"):
            self.pilot._check_owner(self.f.item, workstream)

    async def test_unlisted_entry_is_rejected_before_workstream_or_media_lookup(self):
        self.f.item["_msdyn_liveworkstreamid_value"] = OTHER
        with self.assertRaises(NativeMediaError) as error:
            await self.pilot._resolve_call(self.session)
        self.assertEqual(error.exception.code, "native_call_not_active")
        self.assertFalse(any(call.args[0].startswith("msdyn_liveworkstreams(") for call in self.f.dataverse.get.call_args_list))
        self.f.automation.connect_call.assert_not_awaited()

    async def test_workstream_lookup_cannot_return_another_allowed_entry(self):
        self.pilot.config = config(additional_workstream_ids=(OTHER,))
        self.f.workstream["msdyn_liveworkstreamid"] = OTHER
        with self.assertRaises(NativeMediaError) as error:
            await self.pilot._resolve_call(self.session)
        self.assertEqual(error.exception.code, "invalid_workstream")
        self.f.automation.connect_call.assert_not_awaited()

    async def test_exact_native_context_is_resolved_without_phone_or_time_guessing(self):
        workstream, phone = await self.pilot._resolve_call(self.session)
        self.assertEqual(phone, PHONE)
        self.assertEqual(workstream["_msdyn_bot_user_value"], BOT)
        self.assertEqual(self.session.server_call_id, SERVER_ID)
        self.assertEqual(self.session.controller_session_id, CONTROLLER)
        self.assertEqual(self.session.correlation_id, CORRELATION)

    async def test_crm_lookup_cannot_start_before_native_phone_and_workstream_validation(self):
        provider = AsyncMock()
        self.pilot.customer_context_provider = provider
        self.f.item["_msdyn_liveworkstreamid_value"] = OTHER
        await self.pilot._run(self.session)
        provider.assert_not_awaited()
        self.f.automation.connect_call.assert_not_awaited()
        self.assertEqual(self.session.reason, "native_call_not_active")

    async def test_callautomation_crm_lookup_overlaps_acs_and_blocks_streaming_until_ready(self):
        lookup_started, connect_started = asyncio.Event(), asyncio.Event()
        finish_lookup, finish_connect = asyncio.Event(), asyncio.Event()

        async def context(voice):
            self.assertEqual(voice.native_conversation_id, CONVERSATION)
            self.assertEqual(voice.customer_phone, PHONE)
            lookup_started.set()
            await finish_lookup.wait()
            return parse_customer_context(json.dumps(customer_context()), expected_phone=PHONE)

        async def connect(**kwargs):
            connect_started.set()
            await finish_connect.wait()
            return SimpleNamespace(call_connection_id=CONNECTION)

        self.pilot.customer_context_provider = AsyncMock(side_effect=context)
        self.f.automation.connect_call.side_effect = connect
        await self.pilot._resolve_call(self.session)
        task = asyncio.create_task(self.pilot._connect(self.session, PHONE))
        try:
            await asyncio.wait_for(asyncio.gather(lookup_started.wait(), connect_started.wait()), 1)
            finish_connect.set()
            await asyncio.sleep(0)
            self.f.connection.start_media_streaming.assert_not_awaited()
            self.assertFalse(self.session.bound.is_set())
            finish_lookup.set()
            await asyncio.wait_for(task, 1)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self.assertEqual(self.session.voice.customer_name, "Example Resident")
        self.assertIn("Hello Example.", self.session.voice.intro_message)
        self.pilot.customer_context_provider.assert_awaited_once()
        self.f.connection.start_media_streaming.assert_awaited_once()

    async def test_crm_service_failure_has_explicit_reason_and_preserves_safe_handover_cleanup(self):
        self.pilot.customer_context_provider = AsyncMock(side_effect=ValueError("Synthetic private service detail"))
        with self.assertLogs("native_media", level="ERROR") as logs:
            await self.pilot._run(self.session)
        self.assertEqual(self.session.reason, "crm_context_unavailable")
        self.assertEqual(self.session.action, "handover")
        self.assertTrue(self.session.detached)
        self.f.connection.hang_up.assert_awaited_once_with(is_for_everyone=False)
        self.f.connection.start_media_streaming.assert_not_awaited()
        self.assertIsNone(self.session.voice)
        self.assertNotIn("Synthetic private service detail", "".join(logs.output))

    async def test_open_session_resolution_does_not_require_last_session_pointer(self):
        del self.f.item["_msdyn_lastsessionid_value"]
        await self.pilot._resolve_call(self.session)
        self.assertEqual(self.session.controller_session_id, CONTROLLER)
        lookup = next(c for c in self.f.dataverse.get.call_args_list if c.args[0] == "msdyn_ocsessions")
        self.assertIn(CONVERSATION, lookup.kwargs["params"]["$filter"])
        self.assertIn("msdyn_sessionclosedon eq null", lookup.kwargs["params"]["$filter"])

    async def test_multiple_open_sessions_are_rejected_not_sorted_by_recency(self):
        read = self.f.dataverse.get.side_effect

        async def ambiguous(path, *, params=None):
            if path == "msdyn_ocsessions":
                return {"value": [self.f.controller, self.f.controller]}
            return await read(path, params=params)

        self.f.dataverse.get.side_effect = ambiguous
        with self.assertRaises(NativeMediaError) as captured:
            await self.pilot._resolve_call(self.session)
        self.assertEqual(captured.exception.code, "ambiguous_native_session")
        self.f.automation.connect_call.assert_not_awaited()

    async def test_context_visibility_retry_is_bounded_and_does_not_connect_early(self):
        loop = asyncio.get_running_loop()
        initial = loop.time()
        elapsed = 0

        async def advance(seconds):
            nonlocal elapsed
            elapsed += seconds

        with patch.object(self.pilot, "_resolve_call", new_callable=AsyncMock) as resolve, patch(
            "backend.native_media.asyncio.sleep", side_effect=advance
        ), patch.object(loop, "time", side_effect=lambda: initial + elapsed):
            resolve.side_effect = NativeMediaError("native_session_pending", "Not visible yet.")
            await self.pilot._run(self.session)
        self.assertEqual(elapsed, 10)
        self.assertEqual(resolve.await_count, 21)
        self.f.automation.connect_call.assert_not_awaited()
        self.assertEqual(self.session.reason, "native_session_pending")

    async def test_new_conversation_404_is_pending_not_a_different_call_lookup(self):
        with patch.object(self.pilot, "_conversation", new_callable=AsyncMock) as lookup:
            lookup.side_effect = DataverseError("dynamics_not_found", "Record not visible.", 404)
            with self.assertRaises(NativeMediaError) as captured:
                await self.pilot._resolve_call(self.session)
        self.assertEqual(captured.exception.code, "native_conversation_pending")
        lookup.assert_awaited_once_with(CONVERSATION)
        self.f.automation.connect_call.assert_not_awaited()

    async def test_conversation_permission_error_is_not_retried_as_a_visibility_race(self):
        with patch.object(self.pilot, "_conversation", new_callable=AsyncMock) as lookup:
            lookup.side_effect = DataverseError("dynamics_access_denied", "Access denied.", 502)
            await self.pilot._run(self.session)
        lookup.assert_awaited_once()
        self.assertEqual(self.session.reason, "dynamics_access_denied")
        self.f.automation.connect_call.assert_not_awaited()

    async def test_pending_controller_can_resolve_without_repeating_call_creation(self):
        real_resolve = self.pilot._resolve_call
        calls = 0

        async def resolve(session):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise NativeMediaError("native_session_pending", "Not visible yet.")
            return await real_resolve(session)

        async def connect(session, phone):
            session.voice = voice_factory()
            session.voice._ready = True
            session.connection = self.f.connection
            session.connect_requested = True

        with patch.object(self.pilot, "_resolve_call", side_effect=resolve), patch.object(
            self.pilot, "_connect", side_effect=connect
        ) as attach, patch("backend.native_media.PILOT_SECONDS", 0):
            await self.pilot._run(self.session)
        self.assertEqual(calls, 2)
        attach.assert_awaited_once()
        self.assertEqual(self.session.action, "handover")
        self.f.connection.hang_up.assert_awaited_once_with(is_for_everyone=False)

    async def test_closed_native_controller_is_detected_without_last_pointer_updates(self):
        self.session.controller_session_id = CONTROLLER
        self.f.controller["msdyn_sessionclosedon"] = datetime.now(timezone.utc).isoformat()
        with self.assertRaises(NativeMediaError) as captured:
            await self.pilot._check_controller(self.session)
        self.assertEqual(captured.exception.code, "controller_not_retained")

    async def test_original_external_transfer_state_is_rejected(self):
        self.f.controller["msdyn_sessioncreationreason"] = 192350024
        with self.assertRaises(NativeMediaError) as captured:
            await self.pilot._resolve_call(self.session)
        self.assertEqual(captured.exception.code, "controller_not_retained")
        self.f.automation.connect_call.assert_not_awaited()

    async def test_wrong_native_owner_state_queue_and_workstream_are_rejected(self):
        original = dict(self.f.item)
        for key, value in (
            ("_msdyn_activeagentid_value", OTHER),
            ("_msdyn_liveworkstreamid_value", OTHER),
            ("_msdyn_cdsqueueid_value", OTHER),
            ("msdyn_ocliveworkitemid", OTHER),
            ("statuscode@OData.Community.Display.V1.FormattedValue", "Closed"),
        ):
            self.f.item = {**original, key: value}
            with self.subTest(key=key), self.assertRaises(NativeMediaError):
                await self.pilot._resolve_call(self.session)
        self.f.automation.connect_call.assert_not_awaited()

    async def test_missing_closed_or_human_accepted_controller_is_rejected(self):
        original = dict(self.f.controller)
        for key, value in (
            ("msdyn_sessionclosedon", datetime.now(timezone.utc).isoformat()),
            ("msdyn_agentacceptedon", datetime.now(timezone.utc).isoformat()),
            ("_msdyn_liveworkitemid_value", OTHER),
        ):
            self.f.controller = {**original, key: value}
            with self.subTest(key=key), self.assertRaises(NativeMediaError):
                await self.pilot._resolve_call(self.session)

    async def test_native_context_and_timestamps_cannot_silently_default(self):
        for created in (
            "not-a-time", "2026-10-17T19:00:00",
            (datetime.now(timezone.utc) - timedelta(minutes=11)).isoformat(),
        ):
            self.f.item["createdon"] = created
            with self.subTest(created=created), self.assertRaises(NativeMediaError):
                await self.pilot._resolve_call(self.session)
        self.f.item["createdon"] = datetime.now(timezone.utc).isoformat()
        for context in (
            {"value": []}, {"value": [None]}, {"value": [{}, {}]},
            {"value": [{"msdyn_callsid": CORRELATION, "msdyn_fromphone": PHONE}], "@odata.nextLink": "more"},
            {"value": [{"msdyn_callsid": CORRELATION, "msdyn_fromphone": "unknown"}]},
        ):
            self.f.context = context
            with self.subTest(context=context), self.assertRaises(NativeMediaError):
                await self.pilot._resolve_call(self.session)

    async def test_missing_or_ambiguous_observer_correlation_is_not_guessed(self):
        self.f.observer.clear()
        with self.assertRaises(NativeMediaError) as captured:
            await self.pilot._resolve_call(self.session)
        self.assertEqual(captured.exception.code, "native_event_missing")

    async def test_media_is_started_only_after_exact_connection_and_customer_validation(self):
        await self.pilot._resolve_call(self.session)
        await self.pilot._connect(self.session, PHONE)
        args = self.f.automation.connect_call.call_args.kwargs
        self.assertEqual(args["server_call_id"], SERVER_ID)
        self.assertNotIn("target_participant", args)
        self.assertNotIn("transcription", args)
        options = args["media_streaming"]
        self.assertFalse(options.start_media_streaming)
        self.assertTrue(options.enable_bidirectional)
        self.assertEqual(options.audio_format, "pcm24KMono")
        self.assertEqual(options.audio_channel_type, "unmixed")
        self.assertIn("wss://voice.example/dynamics/native/sessions/", options.transport_url)
        self.assertIn(self.session.capability, options.transport_url)
        self.assertNotIn(KEY, options.transport_url)
        self.assertEqual(self.session.voice.acs_input_participant_id, PhoneNumberIdentifier(PHONE).raw_id)
        self.assertEqual(self.session.connection_id, CONNECTION)
        self.assertTrue(self.session.bound.is_set())
        self.f.connection.start_media_streaming.assert_awaited_once()

    async def test_wrong_server_or_missing_customer_prevents_media(self):
        await self.pilot._resolve_call(self.session)
        self.f.connection.get_call_properties.return_value.server_call_id = "wrong"
        with self.assertRaises(NativeMediaError):
            await self.pilot._connect(self.session, PHONE)
        self.f.connection.start_media_streaming.assert_not_awaited()
        self.f.connection.get_call_properties.return_value.server_call_id = SERVER_ID
        self.f.participants = [CallParticipant(identifier=PhoneNumberIdentifier("+12025550102"))]
        with self.assertRaises(NativeMediaError):
            await self.pilot._connect(self.session, PHONE)
        self.f.connection.start_media_streaming.assert_not_awaited()

    async def test_native_handover_tool_only_signals_the_retained_controller(self):
        await self.pilot._resolve_call(self.session)
        await self.pilot._connect(self.session, PHONE)
        self.session.phase = "active"
        voice = self.session.voice
        token = CURRENT_SESSION.set(voice)
        try:
            result = await voice.tools["talk_to_human"].target({})
        finally:
            CURRENT_SESSION.reset(token)
        payload = json.loads(result.text)
        self.assertEqual(payload["status"], "handover_requested")
        self.assertTrue(self.session.release.is_set())
        self.assertTrue(voice.transfer_active)
        self.assertEqual(self.session.snapshot()["human_acceptance"], "not_observed_by_media_adapter")
        self.assertEqual(self.session.snapshot()["action"], "wait")
        self.f.connection.hang_up.assert_not_awaited()

    async def test_wrong_tool_session_is_rejected(self):
        await self.pilot._resolve_call(self.session)
        await self.pilot._connect(self.session, PHONE)
        self.session.phase = "active"
        with self.assertRaises(ValueError):
            await self.session.voice.tools["talk_to_human"].target({})
        self.assertFalse(self.session.release.is_set())

    async def test_handover_signal_is_not_released_until_own_leg_cleanup(self):
        await self.pilot._resolve_call(self.session)
        voice = voice_factory()
        voice._ready = True
        self.session.voice = voice
        self.session.connection = self.f.connection
        self.session.connect_requested = True
        self.session.reason = "customer_requested_human"

        async def prepared(session, phone):
            pass

        async def release():
            while self.session.phase != "active":
                await asyncio.sleep(0)
            self.session.release.set()

        with patch.object(self.pilot, "_connect", side_effect=prepared):
            await asyncio.gather(self.pilot._run(self.session), release())
        self.f.connection.hang_up.assert_awaited_once_with(is_for_everyone=False)
        self.assertEqual(self.session.action, "handover")
        self.assertEqual(self.session.phase, "handover_ready")
        self.assertTrue(self.session.detached)
        self.assertTrue(self.session.was_active)
        self.assertFalse(self.session.snapshot()["startup_failed_unpaused"])
        self.assertEqual(self.session.snapshot()["human_acceptance"], "not_observed_by_media_adapter")

    async def test_cleanup_failure_does_not_offer_native_transfer(self):
        self.session.connection = self.f.connection
        self.session.connect_requested = True
        self.f.connection.hang_up.side_effect = HttpResponseError("synthetic")
        await self.pilot._detach(self.session)
        self.assertFalse(self.session.detached)

    async def test_hangup_ack_without_disconnect_is_not_a_detached_leg(self):
        self.session.connection = self.f.connection
        self.session.connect_requested = True
        self.f.connection.hang_up.side_effect = None
        with patch("backend.native_media.asyncio.sleep", new_callable=AsyncMock):
            await self.pilot._detach(self.session)
        self.assertFalse(self.session.detached)
        self.assertEqual(self.f.connection.get_call_properties.await_count, 5)

    async def test_own_leg_already_removed_is_explicitly_accepted(self):
        self.session.connection = self.f.connection
        self.session.connect_requested = True
        error = HttpResponseError("already gone")
        error.status_code = 404
        self.f.connection.hang_up.side_effect = error
        await self.pilot._detach(self.session)
        self.assertTrue(self.session.detached)

    async def test_unknown_connect_result_is_not_a_safe_handover(self):
        self.f.automation.connect_call.side_effect = TimeoutError
        await self.pilot._run(self.session)
        self.assertEqual(self.session.phase, "failed")
        self.assertEqual(self.session.action, "stop")
        self.assertEqual(self.session.reason, "azure_cleanup_unconfirmed")
        self.f.automation.connect_call.assert_awaited_once()
        self.f.connection.hang_up.assert_not_awaited()

    async def test_failed_context_never_connects_and_returns_explicit_stop(self):
        self.f.controller["msdyn_sessioncreationreason"] = 192350024
        await self.pilot._run(self.session)
        self.assertEqual(self.session.action, "stop")
        self.assertEqual(self.session.reason, "controller_not_retained")
        self.assertFalse(self.session.snapshot()["startup_failed_unpaused"])
        self.f.automation.connect_call.assert_not_awaited()

    async def test_confirmed_context_timeout_can_be_announced_without_releasing_ai_audio(self):
        with patch.object(self.pilot, "_resolve_pending_context", side_effect=NativeMediaError(
            "native_session_pending", "Synthetic native session visibility timeout.",
        )), self.assertLogs("native_media", level="ERROR"):
            await self.pilot._run(self.session)
        self.assertTrue(self.session.done.is_set())
        self.assertTrue(self.session.detached)
        self.assertFalse(self.session.was_active)
        self.assertTrue(self.session.snapshot()["startup_failed_unpaused"])
        self.f.automation.connect_call.assert_not_awaited()

    async def test_same_native_conversation_is_idempotent_and_concurrent_calls_are_rejected(self):
        waiting = asyncio.Event()

        async def blocked(session):
            await waiting.wait()

        with patch.object(self.pilot, "_run", side_effect=blocked) as run:
            first = self.pilot.start(CONVERSATION, "en")
            second = self.pilot.start(CONVERSATION, "en")
            self.assertIs(first, second)
            with self.assertRaises(NativeMediaError):
                self.pilot.start(CONVERSATION, "ar")
            with self.assertRaises(NativeMediaError):
                self.pilot.start(OTHER, "en")
            waiting.set()
            await first.runner
            run.assert_called_once()
            first.done.set()

    async def test_config_or_observer_disable_prevents_connection(self):
        for disabled in ("config", "observer"):
            if disabled == "config":
                self.pilot.config = config(enabled=False)
            else:
                self.pilot.config = config()
                self.f.observer.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
            with self.subTest(disabled=disabled), self.assertRaises(NativeMediaError):
                self.pilot.start(CONVERSATION, "en")
        self.f.automation.connect_call.assert_not_awaited()

    async def test_stop_before_connection_never_attaches_or_requests_escalation(self):
        self.session.release.set()
        self.session.reason = "control_requested_stop"
        await self.pilot._run(self.session)
        self.f.automation.connect_call.assert_not_awaited()
        self.assertEqual(self.session.action, "stop")
        self.assertTrue(self.session.detached)

    async def test_stop_while_connecting_never_starts_media(self):
        async def connect(**kwargs):
            self.session.release.set()
            return SimpleNamespace(call_connection_id=CONNECTION)

        self.f.automation.connect_call.side_effect = connect
        await self.pilot._run(self.session)
        self.f.connection.start_media_streaming.assert_not_awaited()
        self.f.connection.hang_up.assert_awaited_once_with(is_for_everyone=False)
        self.assertEqual(self.session.action, "stop")

    async def test_active_ai_deadline_is_twenty_minutes_not_two(self):
        loop = asyncio.get_running_loop()
        initial = loop.time()
        elapsed = 0.0
        checkpoints = iter((120.0, 1199.999, 1200.0))
        polls = []

        async def connect(session, phone):
            session.voice = voice_factory()
            session.voice._ready = True
            session.connection = self.f.connection
            session.connect_requested = True

        async def advance(awaitable, *, timeout):
            nonlocal elapsed
            awaitable.close()
            self.assertEqual(timeout, 3)
            self.assertEqual(self.session.phase, "active")
            self.assertIsNone(self.session.reason)
            self.assertFalse(self.session.release.is_set())
            self.f.connection.hang_up.assert_not_awaited()
            polls.append(elapsed)
            elapsed = next(checkpoints)
            raise TimeoutError

        with patch.object(self.pilot, "_connect", side_effect=connect), patch(
            "backend.native_media.asyncio.wait_for", side_effect=advance
        ), patch.object(loop, "time", side_effect=lambda: initial + elapsed):
            await self.pilot._run(self.session)

        self.assertEqual(polls, [0.0, 120.0, 1199.999])
        self.assertEqual(elapsed, 1200.0)
        self.assertEqual(self.session.reason, "pilot_duration_limit")
        self.assertEqual(self.session.action, "handover")
        self.assertTrue(self.session.detached)
        self.f.connection.hang_up.assert_awaited_once_with(is_for_everyone=False)

    async def test_configured_time_limit_releases_only_own_leg_and_returns_to_copilot(self):
        async def connect(session, phone):
            session.voice = voice_factory()
            session.voice._ready = True
            session.connection = self.f.connection
            session.connect_requested = True

        with patch.object(self.pilot, "_connect", side_effect=connect), patch(
            "backend.native_media.PILOT_SECONDS", 0
        ):
            await self.pilot._run(self.session)
        self.assertEqual(self.session.reason, "pilot_duration_limit")
        self.assertEqual(self.session.action, "handover")
        self.f.connection.hang_up.assert_awaited_once_with(is_for_everyone=False)

    async def test_native_human_assignment_during_release_is_not_rerouted(self):
        async def connect(session, phone):
            session.voice = voice_factory()
            session.voice._ready = True
            session.connection = self.f.connection
            session.connect_requested = True

        async def leave(**kwargs):
            self.f.item["_msdyn_activeagentid_value"] = OTHER
            self.f.connection.get_call_properties.return_value.call_connection_state = CallConnectionState.DISCONNECTED

        self.f.connection.hang_up.side_effect = leave
        with patch.object(self.pilot, "_connect", side_effect=connect), patch(
            "backend.native_media.PILOT_SECONDS", 0
        ):
            await self.pilot._run(self.session)
        self.assertEqual(self.session.action, "stop")
        self.assertTrue(self.session.detached)


class AudioIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def test_native_media_only_publishes_the_known_customer_track(self):
        session = voice_factory()
        session.acs_input_participant_id = PhoneNumberIdentifier(PHONE).raw_id
        session._is_acs = True
        session._ready = True
        session._source = SimpleNamespace(capture_frame=AsyncMock())
        audio = base64.b64encode(b"\1\0" * (FRAME_BYTES // 2)).decode()

        class Socket:
            async def __aiter__(self):
                for identifier in ("copilot", None, session.acs_input_participant_id):
                    yield WSMessage(WSMsgType.TEXT, json.dumps({
                        "kind": "AudioData", "audioData": {"data": audio, "participantRawID": identifier},
                    }), "")

        session._client_ws = Socket()
        await session._receive_messages()
        session._source.capture_frame.assert_awaited_once()
        self.assertEqual(session.input_audio_frames, 1)
        self.assertEqual(session._customer_participant_id, session.acs_input_participant_id)

    async def test_clones_do_not_inherit_native_filter_or_media_counts(self):
        session = voice_factory()
        session.acs_input_participant_id = "customer"
        session.input_audio_frames = 5
        session.output_audio_frames = 7
        clone = session.clone_for_session()
        self.assertIsNone(clone.acs_input_participant_id)
        self.assertEqual((clone.input_audio_frames, clone.output_audio_frames), (0, 0))
        self.assertIsNone(clone.native_audio_sink)

    async def test_native_voip_audio_does_not_also_play_through_callautomation(self):
        voice = voice_factory()
        voice._is_acs = True
        voice._client_ws = SimpleNamespace(closed=False, send_json=AsyncMock())
        voice.native_audio_sink = AsyncMock()
        message = {"type": "response.output_audio.delta", "delta": base64.b64encode(b"\1\0" * 480).decode()}
        await voice._send_to_client(message)
        voice.native_audio_sink.assert_awaited_once_with(message)
        voice._client_ws.send_json.assert_not_awaited()
        self.assertEqual(voice.output_audio_frames, 1)
        voice.transfer_active = True
        await voice._send_to_client(message)
        self.assertEqual(voice.native_audio_sink.await_count, 1)
        await voice._send_to_client({"type": "input_audio_buffer.speech_started"})
        self.assertEqual(voice.native_audio_sink.await_count, 2)

    async def test_native_language_result_overrides_only_the_initial_prompt_language(self):
        voice = voice_factory(language="ar")
        voice._is_acs = True
        voice._client_ws = SimpleNamespace(receive=AsyncMock(return_value=SimpleNamespace(
            type=WSMsgType.TEXT,
            data=json.dumps({"kind": "AudioMetadata", "audioMetadata": {"sampleRate": 24000, "channels": 1}}),
        )))
        voice.native_audio_sink = AsyncMock()
        voice.tools["select_response_language"] = Tool(target=AsyncMock(), schema={"name": "select_response_language"})
        self.assertTrue(await voice._receive_handshake())
        self.assertIn("starting language, not a permanent restriction", voice.system_message)
        self.assertIn("bundled with the change in the NEW language", voice.system_message)
        self.assertGreater(
            voice.system_message.index("Native response-language changes"),
            voice.system_message.index("Final response-language policy"),
        )


class NativeVoipIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.f = Fixture()
        self.pilot = NativeMediaPilot(config(transport="voip"), self.f.dataverse, self.f.observer, voice_factory)
        self.pilot._automation = self.f.automation
        self.rtc = NativeVoipSession(CONVERSATION, CORRELATION, IdentityUser("8:acs:synthetic-native-voice"))
        self.rtc.registered.set()
        self.rtc.media_ready.set()
        self.rtc.call_id = CORRELATION
        self.gateway = SimpleNamespace(
            ready=True, start=AsyncMock(), close=AsyncMock(),
            prepare=AsyncMock(return_value=self.rtc), release=AsyncMock(),
            warm_page_ready=False, warm=AsyncMock(),
            retained_identity_count=0, identity_cleanup_failures=0,
            finish_identity_cleanup=AsyncMock(),
            identity_cleanup_pending=lambda _: False,
        )
        self.pilot._gateway = self.gateway
        self.f.connection.add_participant = AsyncMock()
        self.f.connection.get_participant = AsyncMock(
            return_value=SimpleNamespace(identifier=SimpleNamespace(raw_id=self.rtc.user.raw_id))
        )
        self.session = NativeMediaSession(CONVERSATION, "en", transport="voip")
        self.addAsyncCleanup(self.pilot.close)

    def configure_notice(self):
        self.session.play_startup_notice = True
        self.session.speech_control = NativeSpeechControl("en")
        self.session.speech_control.acknowledge("en", 0)
        self.f.connection.play_media = AsyncMock()
        self.f.connection.cancel_all_media_operations = AsyncMock()
        self.rtc.ws = SimpleNamespace(closed=False, send_bytes=AsyncMock(), send_json=AsyncMock())

    async def test_native_announcement_is_exclusive_typed_and_idempotent(self):
        for value in ("true", 1, None):
            with self.subTest(value=value), self.assertRaises(NativeMediaError):
                self.pilot.start(CONVERSATION, "en", speech_turn_control=True, native_startup_notice=value)
        with self.assertRaises(NativeMediaError):
            self.pilot.start(CONVERSATION, "en", speech_turn_control=True,
                             play_startup_notice=True, native_startup_notice=True)
        with self.assertRaises(NativeMediaError):
            self.pilot.start(CONVERSATION, "en", native_startup_notice=True)
        with patch.object(self.pilot, "_run", new_callable=AsyncMock):
            session = self.pilot.start(CONVERSATION, "en", speech_turn_control=True, native_startup_notice=True)
            await session.runner
            session.done.set()
        self.assertEqual(session.snapshot()["startup_notice_owner"], "native")
        self.assertIsNone(session.notice)
        self.assertIs(self.pilot.start(CONVERSATION, "en", speech_turn_control=True, native_startup_notice=True), session)
        with self.assertRaises(NativeMediaError):
            self.pilot.start(CONVERSATION, "en", speech_turn_control=True, play_startup_notice=True)

    async def test_native_owner_prepares_without_language_ack_or_application_playback(self):
        self.session.native_startup_notice = True
        self.session.speech_control = NativeSpeechControl("en")
        self.f.connection.play_media = AsyncMock()
        await self.pilot._resolve_call(self.session)
        with patch.object(self.pilot, "_wait_for_language", new_callable=AsyncMock) as wait:
            await self.pilot._connect(self.session, PHONE)
        wait.assert_not_awaited()
        self.f.connection.play_media.assert_not_awaited()
        self.assertIsNone(self.session.notice)
        self.assertTrue(self.session.voice.native_startup_pending)
        self.assertFalse(self.session.speech_control.ready)
        self.f.connection.start_media_streaming.assert_awaited_once()

    async def test_prepared_application_mode_is_typed_and_immutable(self):
        for value in ("true", 1, None):
            with self.subTest(value=value), self.assertRaises(NativeMediaError):
                self.pilot.start(CONVERSATION, "en", speech_turn_control=True,
                                 play_startup_notice=True, prepare_startup=value)
        with self.assertRaises(NativeMediaError):
            self.pilot.start(CONVERSATION, "en", speech_turn_control=True, prepare_startup=True)
        with patch.object(self.pilot, "_run", new_callable=AsyncMock):
            session = self.pilot.start(CONVERSATION, "en", speech_turn_control=True,
                                       play_startup_notice=True, prepare_startup=True)
            await session.runner
            session.done.set()
        self.assertTrue(session.prepared_startup_requested)
        self.assertEqual(session.snapshot()["startup_notice_owner"], "application")
        self.assertIs(self.pilot.start(CONVERSATION, "en", speech_turn_control=True,
                                      play_startup_notice=True, prepare_startup=True), session)
        with self.assertRaises(NativeMediaError) as caught:
            self.pilot.start(CONVERSATION, "en", speech_turn_control=True, play_startup_notice=True)
        self.assertEqual(caught.exception.code, "session_conflict")

    async def test_prepared_application_notice_starts_before_crm_and_language_ack(self):
        self.configure_notice()
        self.session.prepare_startup = True
        self.session.speech_control.acknowledged_revision = -1
        self.pilot.config = replace(self.pilot.config, fast_start=True)
        submitted, lookup_started, context_available = (asyncio.Event() for _ in range(3))

        async def context(voice):
            lookup_started.set()
            await context_available.wait()
            return parse_customer_context(json.dumps(customer_context()), expected_phone=PHONE)

        self.pilot.customer_context_provider = AsyncMock(side_effect=context)
        self.f.connection.play_media.side_effect = lambda *args, **kwargs: submitted.set()
        await self.pilot._resolve_call(self.session)
        with patch.object(self.pilot, "_wait_for_language", new_callable=AsyncMock) as wait:
            connecting = asyncio.create_task(self.pilot._connect(self.session, PHONE))
            try:
                await asyncio.wait_for(asyncio.gather(submitted.wait(), lookup_started.wait()), 1)
                self.assertFalse(connecting.done())
                self.assertFalse(context_available.is_set())
                self.assertFalse(self.session.speech_control.ready)
                self.f.connection.add_participant.assert_not_awaited()
                self.f.connection.start_media_streaming.assert_not_awaited()
                self.assertEqual(self.session.snapshot()["startup_notice_owner"], "application")
                context_available.set()
                await asyncio.wait_for(connecting, 1)
            finally:
                connecting.cancel()
                await asyncio.gather(connecting, return_exceptions=True)
        wait.assert_not_awaited()
        self.assertTrue(self.session.voice.native_startup_pending)
        self.assertIn("Example", self.session.voice.intro_message)
        self.f.connection.play_media.assert_awaited_once()
        self.session.notice.observe(self.notice_event("PlayCompleted"))
        await self.session.notice.stop()

    async def test_prepared_application_notice_cannot_start_without_verified_caller_membership(self):
        self.configure_notice()
        self.session.prepare_startup = True
        self.f.participants = []
        await self.pilot._resolve_call(self.session)
        with self.assertRaises(NativeMediaError) as caught:
            await self.pilot._connect(self.session, PHONE)
        self.assertEqual(caught.exception.code, "customer_not_in_native_call")
        self.f.connection.play_media.assert_not_awaited()
        self.f.connection.start_media_streaming.assert_not_awaited()

    async def test_prepared_application_crm_failure_stops_notice_without_inviting_humain(self):
        self.configure_notice()
        self.session.prepare_startup = True
        self.pilot.config = replace(self.pilot.config, fast_start=True)
        submitted = asyncio.Event()

        async def context(voice):
            await submitted.wait()
            raise CRMContextError("Synthetic invalid data")

        self.pilot.customer_context_provider = AsyncMock(side_effect=context)
        self.f.connection.play_media.side_effect = lambda *args, **kwargs: submitted.set()
        self.f.connection.cancel_all_media_operations.side_effect = lambda: self.session.notice.observe(self.notice_event())
        with self.assertLogs("native_media", level="ERROR"):
            await asyncio.wait_for(self.pilot._run(self.session), 1)
        self.assertTrue(submitted.is_set())
        self.assertEqual(self.session.notice.state, "stopped")
        self.assertEqual(self.session.reason, "crm_context_invalid")
        self.assertFalse(self.session.was_active)
        self.f.connection.cancel_all_media_operations.assert_awaited_once()
        self.f.connection.add_participant.assert_not_awaited()
        self.f.connection.start_media_streaming.assert_not_awaited()

    async def test_prepared_application_release_waits_for_confirmed_notice_stop(self):
        self.session.play_startup_notice = True
        self.session.prepare_startup = True
        self.session.speech_control = NativeSpeechControl("en")
        stopping, stopped, delivered = (asyncio.Event() for _ in range(3))
        voice = voice_factory()
        voice._ready = True
        voice._is_acs = True
        voice._client_ws = SimpleNamespace(closed=False)
        voice.native_startup_pending = True
        voice.native_audio_sink = AsyncMock(side_effect=lambda message: delivered.set())

        async def stop():
            stopping.set()
            await stopped.wait()

        self.session.notice = SimpleNamespace(stop=AsyncMock(side_effect=stop))

        async def prepared(session, phone):
            session.voice = voice

        with patch.object(self.pilot, "_connect", side_effect=prepared):
            runner = asyncio.create_task(self.pilot._run(self.session))
            try:
                await asyncio.wait_for(self.session.startup_prepared.wait(), 1)
                first = {"type": "response.output_audio.delta", "delta": base64.b64encode(b"\1\0" * 480).decode()}
                await voice._send_to_client(first)
                self.session.notice.stop.assert_not_awaited()
                self.session.speech_control.acknowledge("en", 0)
                await asyncio.wait_for(stopping.wait(), 1)
                self.assertTrue(voice.native_startup_pending)
                voice.native_audio_sink.assert_not_awaited()
                self.assertEqual(self.session.phase, "starting")
                stopped.set()
                await asyncio.wait_for(delivered.wait(), 1)
                voice.native_audio_sink.assert_awaited_once_with(first)
                self.assertEqual(self.session.phase, "active")
                self.assertFalse(voice.native_startup_pending)
            finally:
                stopped.set()
                self.session.reason = "control_requested_stop"
                self.session.release.set()
                await asyncio.wait_for(runner, 1)
                await asyncio.gather(*tuple(voice._tasks))

    async def test_prepared_application_revalidates_owner_after_notice_stop(self):
        self.session.play_startup_notice = True
        self.session.prepare_startup = True
        self.session.speech_control = NativeSpeechControl("en")
        voice = voice_factory()
        voice._ready = True
        voice.native_startup_pending = True
        voice.release_native_startup = AsyncMock()

        async def stop():
            self.f.item["_msdyn_activeagentid_value"] = OTHER

        self.session.notice = SimpleNamespace(stop=AsyncMock(side_effect=stop))

        async def prepared(session, phone):
            session.voice = voice

        with patch.object(self.pilot, "_connect", side_effect=prepared), self.assertLogs("native_media", level="ERROR"):
            runner = asyncio.create_task(self.pilot._run(self.session))
            await asyncio.wait_for(self.session.startup_prepared.wait(), 1)
            self.session.speech_control.acknowledge("en", 0)
            await asyncio.wait_for(runner, 1)
        voice.release_native_startup.assert_not_awaited()
        self.assertEqual(self.session.reason, "human_already_assigned")
        self.assertFalse(self.session.was_active)

    async def test_native_prepared_response_precedes_ack_but_audio_and_input_remain_gated(self):
        self.session.native_startup_notice = True
        self.session.speech_control = NativeSpeechControl("en")
        voice = voice_factory()
        voice._ready = True
        voice._is_acs = True
        voice._client_ws = SimpleNamespace(closed=False)
        delivered = asyncio.Event()
        frames = []

        async def sink(message):
            frames.append(message)
            delivered.set()

        voice.native_audio_sink = sink
        voice.native_startup_pending = True

        async def prepared(session, phone):
            session.voice = voice

        with patch.object(self.pilot, "_connect", side_effect=prepared):
            runner = asyncio.create_task(self.pilot._run(self.session))
            await asyncio.wait_for(self.session.startup_prepared.wait(), 1)
            self.assertEqual(self.session.phase, "starting")
            self.assertTrue(voice.native_startup_pending)
            first = {"type": "response.output_audio.delta", "delta": base64.b64encode(b"\1\0" * 480).decode()}
            await voice._send_to_client(first)
            self.assertEqual(frames, [])
            self.session.speech_control.acknowledge("en", 0)
            await asyncio.wait_for(delivered.wait(), 1)
            self.assertEqual(frames, [first])
            self.assertEqual(self.session.phase, "active")
            self.assertFalse(voice.native_startup_pending)
            self.assertTrue(self.session.was_active)
            self.session.reason = "control_requested_stop"
            self.session.release.set()
            await asyncio.wait_for(runner, 1)
            await asyncio.gather(*tuple(voice._tasks))

    async def test_native_prepared_audio_does_not_release_after_human_takes_ownership(self):
        self.session.native_startup_notice = True
        self.session.speech_control = NativeSpeechControl("en")
        voice = voice_factory()
        voice._ready = True
        voice.native_startup_pending = True
        voice.release_native_startup = AsyncMock()

        async def prepared(session, phone):
            session.voice = voice

        with patch.object(self.pilot, "_connect", side_effect=prepared), self.assertLogs("native_media", level="ERROR"):
            runner = asyncio.create_task(self.pilot._run(self.session))
            await asyncio.wait_for(self.session.startup_prepared.wait(), 1)
            self.f.item["_msdyn_activeagentid_value"] = OTHER
            self.session.speech_control.acknowledge("en", 0)
            await asyncio.wait_for(runner, 1)
        voice.release_native_startup.assert_not_awaited()
        self.assertEqual(self.session.reason, "human_already_assigned")
        self.assertFalse(self.session.snapshot()["startup_failed_unpaused"])

    def notice_event(self, kind="PlayCanceled"):
        return {
            "type": "Microsoft.Communication." + kind,
            "data": {
                "callConnectionId": CONNECTION, "correlationId": CORRELATION,
                "operationContext": f"startup-notice-{CONVERSATION}",
            },
        }

    async def test_notice_option_is_typed_idempotent_and_requires_native_speech_control(self):
        for invalid in ("true", 1, None):
            with self.subTest(invalid=invalid), self.assertRaises(NativeMediaError) as caught:
                self.pilot.start(CONVERSATION, "en", speech_turn_control=True, play_startup_notice=invalid)
            self.assertEqual(caught.exception.code, "invalid_startup_notice")
        with self.assertRaises(NativeMediaError):
            self.pilot.start(CONVERSATION, "en", play_startup_notice=True)
        with patch.object(self.pilot, "_run", new_callable=AsyncMock):
            current = self.pilot.start(CONVERSATION, "en", speech_turn_control=True, play_startup_notice=True)
            await current.runner
            current.done.set()
        self.assertIs(
            self.pilot.start(CONVERSATION, "en", speech_turn_control=True, play_startup_notice=True), current,
        )
        self.assertEqual(current.snapshot()["startup_notice_state"], "not_started")
        with self.assertRaises(NativeMediaError) as caught:
            self.pilot.start(CONVERSATION, "en", speech_turn_control=True)
        self.assertEqual(caught.exception.code, "session_conflict")
        self.assertNotIn("startup_notice_requested", NativeMediaSession(OTHER, "en").snapshot())

    async def test_notice_starts_without_waiting_for_parallel_voip_preparation(self):
        self.configure_notice()
        self.pilot.config = replace(self.pilot.config, fast_start=True)
        submitted, finish_preparation = asyncio.Event(), asyncio.Event()

        async def prepare(*args, **kwargs):
            await finish_preparation.wait()
            return self.rtc

        self.gateway.prepare.side_effect = prepare
        self.f.connection.play_media.side_effect = lambda *args, **kwargs: submitted.set()
        await self.pilot._resolve_call(self.session)
        connection = asyncio.create_task(self.pilot._connect(self.session, PHONE))
        await asyncio.wait_for(submitted.wait(), 1)
        self.assertFalse(connection.done())
        self.f.connection.start_media_streaming.assert_not_awaited()
        finish_preparation.set()
        await connection
        self.session.notice.observe(self.notice_event("PlayCompleted"))
        await self.session.notice.stop()

    async def test_application_notice_waits_for_native_language_turn_handoff(self):
        self.configure_notice()
        self.session.speech_control.acknowledged_revision = -1
        connected = asyncio.Event()
        original = self.pilot._connect_acs

        async def connect(*args):
            result = await original(*args)
            connected.set()
            return result

        await self.pilot._resolve_call(self.session)
        with patch.object(self.pilot, "_connect_acs", side_effect=connect):
            task = asyncio.create_task(self.pilot._connect(self.session, PHONE))
            await asyncio.wait_for(connected.wait(), 1)
            self.f.connection.play_media.assert_not_awaited()
            self.assertFalse(task.done())
            self.session.speech_control.acknowledge("en", 0)
            await task
        self.f.connection.play_media.assert_awaited_once()
        self.session.notice.observe(self.notice_event("PlayCompleted"))
        await self.session.notice.stop()

    async def test_context_is_signalled_before_media_preparation_finishes(self):
        entered = asyncio.Event()

        async def preparing(*args):
            entered.set()
            await asyncio.Event().wait()

        with patch.object(self.pilot, "_resolve_pending_context", return_value=(self.f.workstream, PHONE)), patch.object(
            self.pilot, "_connect", side_effect=preparing,
        ), patch.object(self.pilot, "_detach", new_callable=AsyncMock):
            task = asyncio.create_task(self.pilot._run(self.session))
            await asyncio.wait_for(entered.wait(), 1)
            self.assertTrue(self.session.context_ready.is_set())
            self.assertFalse(task.done())
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

    async def test_stop_during_connection_prevents_a_new_notice(self):
        self.configure_notice()

        async def connected(*args):
            self.session.release.set()
            return PhoneNumberIdentifier(PHONE).raw_id

        with patch.object(self.pilot, "_connect_acs", side_effect=connected), self.assertRaises(NativeMediaError):
            await self.pilot._connect(self.session, PHONE)
        self.f.connection.play_media.assert_not_awaited()
        self.assertIsNone(self.session.notice)

    async def test_first_humain_pcm_is_preserved_and_withheld_until_notice_cancellation(self):
        self.configure_notice()
        self.pilot.customer_context_provider = AsyncMock(return_value=parse_customer_context(
            json.dumps(customer_context()), expected_phone=PHONE,
        ))
        await self.pilot._resolve_call(self.session)
        await self.pilot._connect(self.session, PHONE)
        self.assertIn("Example", self.session.voice.intro_message)
        requested = asyncio.Event()
        self.f.connection.cancel_all_media_operations.side_effect = lambda: requested.set()
        pcm = b"\x11\x00" * 320
        task = asyncio.create_task(self.session.voice.native_audio_sink({
            "type": "response.output_audio.delta", "delta": base64.b64encode(pcm).decode(),
        }))
        await asyncio.wait_for(requested.wait(), 1)
        self.assertFalse(task.done())
        self.rtc.ws.send_bytes.assert_not_awaited()
        self.session.notice.observe(self.notice_event())
        await task
        self.rtc.ws.send_bytes.assert_awaited_once_with(pcm)
        self.assertEqual(self.session.snapshot()["startup_notice_state"], "stopped")
        self.f.connection.cancel_all_media_operations.assert_awaited_once()
        self.f.connection.hang_up.assert_not_awaited()

    async def test_no_humain_audio_if_notice_stop_is_unconfirmed(self):
        self.configure_notice()
        await self.pilot._resolve_call(self.session)
        await self.pilot._connect(self.session, PHONE)
        with patch("backend.native_notice.NOTICE_STOP_SECONDS", 0.01), self.assertLogs("native_notice", level="ERROR"):
            with self.assertRaises(NativeNoticeError):
                await self.session.voice.native_audio_sink({
                    "type": "response.output_audio.delta", "delta": base64.b64encode(b"\x01\x00" * 320).decode(),
                })
        self.rtc.ws.send_bytes.assert_not_awaited()
        self.assertEqual(self.session.snapshot()["startup_notice_state"], "failed")

    async def test_detach_stops_notice_before_removing_its_own_leg(self):
        self.configure_notice()
        await self.pilot._resolve_call(self.session)
        await self.pilot._connect(self.session, PHONE)
        events = []

        async def cancel():
            events.append("notice_cancelled")
            self.session.notice.observe(self.notice_event())

        async def release(*args, **kwargs):
            events.append("microphone_released")

        async def hang_up(**kwargs):
            events.append("own_leg_removed")
            self.f.connection.get_call_properties.return_value.call_connection_state = CallConnectionState.DISCONNECTED

        self.f.connection.cancel_all_media_operations.side_effect = cancel
        self.gateway.release.side_effect = release
        self.f.connection.hang_up.side_effect = hang_up
        await self.pilot._detach(self.session)
        self.assertEqual(events, ["notice_cancelled", "microphone_released", "own_leg_removed"])
        self.assertTrue(self.session.detached)
        self.f.connection.hang_up.assert_awaited_once_with(is_for_everyone=False)

    async def test_only_verified_native_participant_can_carry_humain_audio(self):
        await self.pilot._resolve_call(self.session)
        await self.pilot._connect(self.session, PHONE)
        options = self.f.automation.connect_call.call_args.kwargs["media_streaming"]
        self.assertFalse(options.enable_bidirectional)
        self.f.connection.add_participant.assert_awaited_once()
        self.assertEqual(self.f.connection.add_participant.call_args.kwargs["target_participant"].raw_id, self.rtc.user.raw_id)
        self.assertTrue(self.rtc.authorized)
        self.assertIs(self.session.voice.native_audio_sink.__self__, self.rtc)
        self.assertEqual(self.session.snapshot()["audio_transport"], "voip")

    async def test_acs_readiness_wait_is_not_limited_to_eight_fast_polls(self):
        self.f.connection.get_call_properties.side_effect = [
            SimpleNamespace(call_connection_state=CallConnectionState.CONNECTING, server_call_id=SERVER_ID)
            for _ in range(8)
        ] + [SimpleNamespace(call_connection_state=CallConnectionState.CONNECTED, server_call_id=SERVER_ID)]
        await self.pilot._resolve_call(self.session)
        with patch("backend.native_media.asyncio.sleep", new_callable=AsyncMock):
            raw_id = await self.pilot._connect_acs(self.session, PHONE)
        self.assertEqual(raw_id, PhoneNumberIdentifier(PHONE).raw_id)
        self.assertEqual(self.f.connection.get_call_properties.await_count, 9)
        states = [row["status"] for row in self.session.latency.snapshot()["events"] if row["stage"] == "acs_connection_state"]
        self.assertEqual(states, ["connecting", "connected"])

    async def test_acs_readiness_still_has_a_wall_clock_deadline(self):
        self.f.connection.get_call_properties.return_value.call_connection_state = CallConnectionState.CONNECTING
        await self.pilot._resolve_call(self.session)
        with patch("backend.native_media.ACS_CONNECT_SECONDS", 0), self.assertRaises(NativeMediaError) as captured:
            await self.pilot._connect_acs(self.session, PHONE)
        self.assertEqual(captured.exception.code, "native_connect_failed")
        self.f.connection.add_participant.assert_not_awaited()
        await self.pilot._detach(self.session)

    async def test_pending_context_can_appear_after_the_old_five_attempt_limit(self):
        self.pilot._resolve_call = AsyncMock(side_effect=[
            NativeMediaError("native_session_pending", "Synthetic pending session") for _ in range(5)
        ] + [(self.f.workstream, PHONE)])
        with patch("backend.native_media.asyncio.sleep", new_callable=AsyncMock):
            result = await self.pilot._resolve_pending_context(self.session)
        self.assertEqual(result, (self.f.workstream, PHONE))
        self.assertEqual(self.pilot._resolve_call.await_count, 6)
        self.f.automation.connect_call.assert_not_awaited()

    async def test_pending_context_deadline_keeps_the_original_explicit_reason(self):
        self.pilot._resolve_call = AsyncMock(side_effect=NativeMediaError(
            "native_session_pending", "Synthetic pending session",
        ))
        with patch("backend.native_media.CONTEXT_SECONDS", 0), self.assertRaises(NativeMediaError) as captured:
            await self.pilot._resolve_pending_context(self.session)
        self.assertEqual(captured.exception.code, "native_session_pending")

    async def test_context_permission_errors_are_never_retried(self):
        self.pilot._resolve_call = AsyncMock(side_effect=DataverseError("dynamics_access_denied", "Synthetic denial", 502))
        with self.assertRaises(DataverseError):
            await self.pilot._resolve_pending_context(self.session)
        self.pilot._resolve_call.assert_awaited_once()

    async def test_sdk_call_id_mismatch_prevents_humain_start(self):
        self.rtc.call_id = OTHER
        await self.pilot._resolve_call(self.session)
        with self.assertRaises(NativeVoipError):
            await self.pilot._connect(self.session, PHONE)
        self.assertFalse(self.rtc.authorized)
        self.f.connection.start_media_streaming.assert_not_awaited()

    async def test_microphone_cleanup_failure_blocks_successful_handover(self):
        self.session.voip = self.rtc
        self.session.connection = self.f.connection
        self.session.connect_requested = True
        self.gateway.release.side_effect = NativeVoipError("Synthetic cleanup failure")
        await self.pilot._detach(self.session)
        self.assertFalse(self.session.detached)
        self.f.connection.hang_up.assert_awaited_once_with(is_for_everyone=False)

    async def test_identity_retention_is_an_explicit_typed_idempotent_call_option(self):
        for value in ("true", 1, None):
            with self.subTest(value=value), self.assertRaises(NativeMediaError) as captured:
                self.pilot.start(CONVERSATION, "en", speech_turn_control=True, retain_identity_for_metrics=value)
            self.assertEqual(captured.exception.code, "invalid_identity_retention")
        with self.assertRaises(NativeMediaError):
            self.pilot.start(CONVERSATION, "en", retain_identity_for_metrics=True)
        with self.assertRaises(NativeMediaError):
            self.f.pilot.start(CONVERSATION, "en", retain_identity_for_metrics=True)
        with patch.object(self.pilot, "_run", new_callable=AsyncMock):
            current = self.pilot.start(
                CONVERSATION, "en", speech_turn_control=True, retain_identity_for_metrics=True,
            )
            await current.runner
            current.done.set()
        self.assertTrue(current.retain_identity_for_metrics)
        self.assertIs(self.pilot.start(
            CONVERSATION, "en", speech_turn_control=True, retain_identity_for_metrics=True,
        ), current)
        with self.assertRaises(NativeMediaError) as captured:
            self.pilot.start(CONVERSATION, "en", speech_turn_control=True)
        self.assertEqual(captured.exception.code, "session_conflict")

    async def test_pending_identity_blocks_only_a_second_retention_diagnostic(self):
        self.gateway.retained_identity_count = 1
        with self.assertRaises(NativeMediaError) as captured:
            self.pilot.start(CONVERSATION, "en", speech_turn_control=True, retain_identity_for_metrics=True)
        self.assertEqual(captured.exception.code, "identity_retention_busy")
        with patch.object(self.pilot, "_run", new_callable=AsyncMock):
            normal = self.pilot.start(CONVERSATION, "en", speech_turn_control=True)
            await normal.runner
            normal.done.set()
        self.assertFalse(normal.retain_identity_for_metrics)

    async def test_retention_status_is_opt_in_and_never_exposes_identity_or_capability(self):
        self.assertNotIn("identity_cleanup_status", self.session.snapshot())
        self.session.retain_identity_for_metrics = True
        self.assertEqual(self.session.snapshot()["identity_cleanup_status"], "not_allocated")
        self.session.voip = self.rtc
        self.rtc.identity_cleanup_status = "retained"
        snapshot = self.session.snapshot()
        self.assertTrue(snapshot["identity_retention_requested"])
        self.assertEqual(snapshot["identity_retention_seconds"], 900)
        self.assertEqual(snapshot["identity_cleanup_status"], "retained")
        self.assertNotIn(self.rtc.user.raw_id, json.dumps(snapshot))
        self.assertNotIn(self.rtc.capability, json.dumps(snapshot))
        self.gateway.retained_identity_count = 1
        self.gateway.identity_cleanup_failures = 1
        self.assertEqual(self.pilot.health()["retained_identity_count"], 1)
        self.assertEqual(self.pilot.health()["identity_cleanup_failures"], 1)

    async def test_pending_identity_status_survives_normal_history_expiry(self):
        previous = NativeMediaSession(
            CONVERSATION, "en", transport="voip", retain_identity_for_metrics=True,
            created_at=datetime.now(timezone.utc) - timedelta(minutes=16),
        )
        previous.voip = self.rtc
        previous.done.set()
        self.rtc.identity_cleanup_status = "retained"
        self.pilot.sessions[CONVERSATION] = previous
        self.gateway.retained_identity_count = 1
        self.gateway.identity_cleanup_pending = lambda current: current is self.rtc
        with patch.object(self.pilot, "_run", new_callable=AsyncMock):
            normal = self.pilot.start(OTHER, "en", speech_turn_control=True)
            await normal.runner
            normal.done.set()
        self.assertIs(self.pilot.sessions[CONVERSATION], previous)
        self.assertEqual(previous.snapshot()["identity_cleanup_status"], "retained")

    async def test_clean_handover_retains_identity_without_changing_own_leg_departure(self):
        self.session.voip = self.rtc
        self.session.connection = self.f.connection
        self.session.connect_requested = True
        self.session.reason = "customer_requested_human"
        self.session.retain_identity_for_metrics = True
        self.f.connection.get_call_properties.return_value.call_connection_state = CallConnectionState.DISCONNECTED
        await self.pilot._detach(self.session)
        self.gateway.release.assert_awaited_once_with(self.rtc, self.f.connection, retain_identity=True)
        self.f.connection.hang_up.assert_awaited_once_with(is_for_everyone=False)
        self.gateway.finish_identity_cleanup.assert_not_awaited()
        self.assertTrue(self.session.detached)

    async def test_failed_control_leg_departure_finishes_retained_identity_immediately(self):
        self.session.voip = self.rtc
        self.session.connection = self.f.connection
        self.session.connect_requested = True
        self.session.reason = "customer_requested_human"
        self.session.retain_identity_for_metrics = True
        self.f.connection.hang_up.side_effect = HttpResponseError("Synthetic control-leg failure")
        with self.assertLogs("native_media", level="ERROR"):
            await self.pilot._detach(self.session)
        self.assertFalse(self.session.detached)
        self.gateway.finish_identity_cleanup.assert_awaited_once_with(self.rtc)

    async def test_failed_setup_or_shutdown_never_requests_identity_retention(self):
        for reason, closing in (("provider_failed", False), ("customer_requested_human", True)):
            with self.subTest(reason=reason, closing=closing):
                self.gateway.release.reset_mock()
                self.session.voip = self.rtc
                self.session.connection = self.f.connection
                self.session.connect_requested = True
                self.session.reason = reason
                self.session.retain_identity_for_metrics = True
                self.pilot._closing = closing
                await self.pilot._detach(self.session)
                self.gateway.release.assert_awaited_once_with(self.rtc, self.f.connection)

    async def test_readiness_warms_gateway_without_creating_a_call_participant(self):
        result = await self.pilot.readiness()
        self.gateway.start.assert_awaited_once()
        self.gateway.prepare.assert_not_awaited()
        self.f.connection.add_participant.assert_not_awaited()
        self.assertEqual(result["transport"], "voip")
        self.assertTrue(result["voip_gateway_ready"])

    async def test_speech_controller_must_acknowledge_before_humain_starts(self):
        self.session.speech_control = NativeSpeechControl("en")
        self.pilot.customer_context_provider = AsyncMock(return_value=parse_customer_context(
            json.dumps(customer_context()), expected_phone=PHONE,
        ))
        membership_verified = asyncio.Event()
        participant = self.f.connection.get_participant.return_value

        async def membership(*args):
            membership_verified.set()
            return participant

        self.f.connection.get_participant.side_effect = membership
        await self.pilot._resolve_call(self.session)
        connecting = asyncio.create_task(self.pilot._connect(self.session, PHONE))
        await asyncio.wait_for(membership_verified.wait(), 1)
        self.f.connection.start_media_streaming.assert_not_awaited()
        self.assertIsNone(self.session.voice)
        self.assertIn("crm_context_ready", [event["stage"] for event in self.session.latency.snapshot()["events"]])
        self.session.speech_control.acknowledge("en", 0)
        await asyncio.wait_for(connecting, 1)
        self.assertIn("select_response_language", self.session.voice.tools)
        self.assertIn("wait for success", self.session.voice.system_message)
        self.f.connection.start_media_streaming.assert_awaited_once()

    async def test_crm_initial_english_requires_english_ack_and_allows_later_explicit_switch(self):
        self.session = NativeMediaSession(
            CONVERSATION, "ar", transport="voip", speech_control=NativeSpeechControl("ar"),
        )
        self.pilot.sessions[CONVERSATION] = self.session
        state = self.session.speech_control
        state.acknowledge("ar", 0)
        self.pilot.customer_context_provider = AsyncMock(return_value=parse_customer_context(
            json.dumps(customer_context(preferred_language="en")), expected_phone=PHONE,
        ))
        created, invited = [], asyncio.Event()

        def create_voice(phone, language):
            voice = voice_factory(phone, language)
            created.append(voice)
            return voice

        async def invite(**kwargs):
            self.assertEqual(created[0].language, "en")
            self.assertIn("Hello Example.", created[0].intro_message)
            self.assertEqual(created[0].customer_context.customer.preferred_language, "en")
            self.assertEqual((state.language, state.revision), ("en", 1))
            self.assertFalse(state.ready)
            invited.set()

        self.pilot.session_factory = MagicMock(side_effect=create_voice)
        self.f.connection.add_participant.side_effect = invite
        await self.pilot._resolve_call(self.session)
        connecting = asyncio.create_task(self.pilot._connect(self.session, PHONE))
        try:
            await asyncio.wait_for(invited.wait(), 1)
            self.assertEqual(self.session.language, "ar")
            self.assertEqual(self.session.effective_initial_language, "en")
            self.assertEqual(self.session.snapshot()["control_language"], "en")
            self.assertEqual(self.session.snapshot()["control_revision"], 1)
            self.assertFalse(self.session.snapshot()["control_ready"])
            self.assertIs(self.pilot.start(CONVERSATION, "ar", speech_turn_control=True), self.session)
            with self.assertRaises(NativeMediaError) as conflict:
                self.pilot.start(CONVERSATION, "en", speech_turn_control=True)
            self.assertEqual(conflict.exception.code, "session_conflict")
            state.acknowledge("ar", 0)
            await asyncio.sleep(0)
            self.assertFalse(connecting.done())
            self.f.connection.start_media_streaming.assert_not_awaited()
            self.assertIsNone(self.session.voice)
            with self.assertRaises(NativeMediaError):
                state.acknowledge("ar", 1)
            state.acknowledge("en", 1)
            await asyncio.wait_for(connecting, 1)
        finally:
            connecting.cancel()
            await asyncio.gather(connecting, return_exceptions=True)
        voice = self.session.voice
        self.assertEqual(voice.language, "en")
        self.assertEqual(voice.native_conversation_id, CONVERSATION)
        self.assertEqual(voice.customer_phone, PHONE)
        self.pilot.session_factory.assert_called_once_with(PHONE, "en")
        self.f.connection.start_media_streaming.assert_awaited_once()
        self.session.phase = "active"
        await voice._record_transcript(SpeakerRole.CUSTOMER, "synthetic-later-language", "Please answer in Arabic.")
        token = CURRENT_SESSION.set(voice)
        try:
            switching = asyncio.create_task(voice.tools["select_response_language"].target({"language": "ar"}))
        finally:
            CURRENT_SESSION.reset(token)
        await asyncio.sleep(0)
        self.assertEqual((state.language, state.revision), ("ar", 2))
        self.assertEqual(voice.language, "en")
        state.acknowledge("en", 1)
        await asyncio.sleep(0)
        self.assertFalse(switching.done())
        state.acknowledge("ar", 2)
        result = json.loads((await asyncio.wait_for(switching, 1)).to_text())
        self.assertEqual(result["response_language"], "Arabic")
        self.assertEqual(voice.language, "ar")
        self.assertEqual(self.session.language, "ar")
        self.assertEqual(self.session.effective_initial_language, "en")
        self.assertIs(self.pilot.start(CONVERSATION, "ar", speech_turn_control=True), self.session)
        self.pilot.customer_context_provider.assert_awaited_once()
        self.pilot.session_factory.assert_called_once()
        self.assertFalse(self.session.release.is_set())

    async def test_absent_null_and_matching_crm_language_preserve_initial_arabic_and_ack_revision(self):
        absent = customer_context()
        del absent["customer"]["preferredLanguage"]
        for payload in (absent, customer_context(), customer_context(preferred_language="ar"), unknown_customer_context()):
            with self.subTest(customer=payload["customer"]):
                self.session = NativeMediaSession(
                    CONVERSATION, "ar", transport="voip", speech_control=NativeSpeechControl("ar"),
                )
                self.session.speech_control.acknowledge("ar", 0)
                self.pilot.customer_context_provider = AsyncMock(return_value=parse_customer_context(
                    json.dumps(payload), expected_phone=PHONE,
                ))
                self.pilot.session_factory = MagicMock(side_effect=voice_factory)
                await self.pilot._resolve_call(self.session)
                await self.pilot._connect(self.session, PHONE)
                self.assertEqual(self.session.language, "ar")
                self.assertEqual(self.session.effective_initial_language, "ar")
                self.assertEqual(self.session.voice.language, "ar")
                self.assertIn("مرحباً", self.session.voice.intro_message)
                self.assertEqual(self.session.speech_control.revision, 0)
                self.assertTrue(self.session.speech_control.ready)
                self.pilot.session_factory.assert_called_once_with(PHONE, "ar")
                self.pilot.customer_context_provider.assert_awaited_once()

    async def test_different_crm_language_without_speech_control_is_rejected_before_invitation(self):
        self.session = NativeMediaSession(CONVERSATION, "ar", transport="voip")
        self.pilot.customer_context_provider = AsyncMock(return_value=parse_customer_context(
            json.dumps(customer_context(preferred_language="en")), expected_phone=PHONE,
        ))
        self.pilot.session_factory = MagicMock(side_effect=voice_factory)
        await self.pilot._resolve_call(self.session)
        with self.assertRaises(NativeMediaError) as captured:
            await self.pilot._connect(self.session, PHONE)
        self.assertEqual(captured.exception.code, "crm_language_control_required")
        self.assertEqual(self.session.language, "ar")
        self.pilot.session_factory.assert_not_called()
        self.f.connection.add_participant.assert_not_awaited()
        self.f.connection.start_media_streaming.assert_not_awaited()
        await self.pilot._detach(self.session)
        self.assertTrue(self.session.detached)

    async def test_malformed_crm_language_cannot_select_a_language_or_start_media(self):
        self.session = NativeMediaSession(
            CONVERSATION, "ar", transport="voip", speech_control=NativeSpeechControl("ar"),
        )
        self.pilot.customer_context_provider = AsyncMock(side_effect=lambda voice: parse_customer_context(
            json.dumps(customer_context(preferred_language="EN")), expected_phone=PHONE,
        ))
        self.pilot.session_factory = MagicMock(side_effect=voice_factory)
        await self.pilot._resolve_call(self.session)
        with self.assertRaises(NativeMediaError) as captured:
            await self.pilot._connect(self.session, PHONE)
        self.assertEqual(captured.exception.code, "crm_context_invalid")
        self.assertEqual((self.session.speech_control.language, self.session.speech_control.revision), ("ar", 0))
        self.pilot.session_factory.assert_not_called()
        self.f.connection.add_participant.assert_not_awaited()
        self.f.connection.start_media_streaming.assert_not_awaited()

    async def test_preferred_language_ack_timeout_never_starts_media_with_the_old_ack(self):
        self.session = NativeMediaSession(
            CONVERSATION, "ar", transport="voip", speech_control=NativeSpeechControl("ar"),
        )
        self.session.speech_control.acknowledge("ar", 0)
        self.pilot.customer_context_provider = AsyncMock(return_value=parse_customer_context(
            json.dumps(customer_context(preferred_language="en")), expected_phone=PHONE,
        ))
        await self.pilot._resolve_call(self.session)
        with patch("backend.native_media.LANGUAGE_ACK_SECONDS", 0.01), self.assertRaises(NativeMediaError) as captured:
            await self.pilot._connect(self.session, PHONE)
        self.assertEqual(captured.exception.code, "native_language_unconfirmed")
        self.assertEqual(self.session.speech_control.acknowledged_revision, 0)
        self.assertEqual(self.session.speech_control.revision, 1)
        self.assertFalse(self.session.speech_control.ready)
        self.assertIsNone(self.session.voice)
        self.assertFalse(self.session.bound.is_set())
        self.f.connection.start_media_streaming.assert_not_awaited()

    async def test_startup_notice_waits_for_crm_preference_and_matching_question_ack_while_voip_prepares(self):
        self.session = NativeMediaSession(CONVERSATION, "ar", transport="voip")
        self.configure_notice()
        self.session.speech_control = NativeSpeechControl("ar")
        self.session.speech_control.acknowledge("ar", 0)
        lookup_started, prepared, context_available, voice_created = (asyncio.Event() for _ in range(4))

        async def context(voice):
            lookup_started.set()
            await context_available.wait()
            return parse_customer_context(json.dumps(customer_context(preferred_language="en")), expected_phone=PHONE)

        async def prepare(*args, **kwargs):
            prepared.set()
            return self.rtc

        def create_voice(phone, language):
            voice_created.set()
            return voice_factory(phone, language)

        self.pilot.customer_context_provider = AsyncMock(side_effect=context)
        self.pilot.session_factory = MagicMock(side_effect=create_voice)
        self.gateway.prepare.side_effect = prepare
        await self.pilot._resolve_call(self.session)
        connecting = asyncio.create_task(self.pilot._connect(self.session, PHONE))
        try:
            await asyncio.wait_for(asyncio.gather(lookup_started.wait(), prepared.wait()), 1)
            self.f.connection.play_media.assert_not_awaited()
            self.pilot.session_factory.assert_not_called()
            context_available.set()
            await asyncio.wait_for(voice_created.wait(), 1)
            self.assertEqual((self.session.speech_control.language, self.session.speech_control.revision), ("en", 1))
            self.session.speech_control.acknowledge("ar", 0)
            await asyncio.sleep(0)
            self.f.connection.play_media.assert_not_awaited()
            self.f.connection.add_participant.assert_not_awaited()
            self.f.connection.start_media_streaming.assert_not_awaited()
            self.session.speech_control.acknowledge("en", 1)
            await asyncio.wait_for(connecting, 1)
        finally:
            connecting.cancel()
            await asyncio.gather(connecting, return_exceptions=True)
        self.pilot.session_factory.assert_called_once_with(PHONE, "en")
        self.pilot.customer_context_provider.assert_awaited_once()
        self.f.connection.play_media.assert_awaited_once()
        self.assertIn("Hello Example.", self.session.voice.intro_message)
        self.session.notice.observe(self.notice_event("PlayCompleted"))
        await self.session.notice.stop()

    async def test_crm_prefetch_overlaps_acs_and_voip_and_injects_before_microphone_invitation(self):
        self.pilot.config = config(transport="voip", fast_start=True)
        connecting, registering, looking_up = asyncio.Event(), asyncio.Event(), asyncio.Event()
        connected, registered, lookup_complete = asyncio.Event(), asyncio.Event(), asyncio.Event()
        voices, lookups = [], []

        async def context(voice):
            lookups.append(voice)
            self.assertEqual(voice.native_conversation_id, CONVERSATION)
            self.assertEqual(voice.customer_phone, PHONE)
            self.assertTrue(voice.customer_context_required)
            self.assertFalse(voice.config.enabled)
            looking_up.set()
            await lookup_complete.wait()
            return parse_customer_context(json.dumps(customer_context()), expected_phone=PHONE)

        def create_voice(phone, language):
            voice = voice_factory(phone, language)
            voices.append(voice)
            return voice

        async def connect(**kwargs):
            connecting.set()
            await connected.wait()
            return SimpleNamespace(call_connection_id=CONNECTION)

        async def prepare(*args, **kwargs):
            registering.set()
            await registered.wait()
            return self.rtc

        async def invite(**kwargs):
            self.assertTrue(lookup_complete.is_set())
            self.assertIsNotNone(voices[0].customer_context)
            self.assertIn("Example Resident", voices[0].system_message)
            self.assertIn("Hello Example.", voices[0].intro_message)
            self.assertIsNone(self.session.voice)
            self.assertFalse(self.session.bound.is_set())

        self.pilot.customer_context_provider = AsyncMock(side_effect=context)
        self.pilot.session_factory = MagicMock(side_effect=create_voice)
        self.f.automation.connect_call.side_effect = connect
        self.gateway.prepare.side_effect = prepare
        self.f.connection.add_participant.side_effect = invite
        await self.pilot._resolve_call(self.session)
        task = asyncio.create_task(self.pilot._connect(self.session, PHONE))
        try:
            await asyncio.wait_for(asyncio.gather(connecting.wait(), registering.wait(), looking_up.wait()), 1)
            self.pilot.session_factory.assert_not_called()
            connected.set()
            registered.set()
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            self.f.connection.add_participant.assert_not_awaited()
            self.f.connection.start_media_streaming.assert_not_awaited()
            self.assertIsNone(self.session.voice)
            lookup_complete.set()
            await asyncio.wait_for(task, 1)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self.pilot.customer_context_provider.assert_awaited_once_with(lookups[0])
        self.pilot.session_factory.assert_called_once_with(PHONE, "en")
        self.f.connection.add_participant.assert_awaited_once()
        self.assertIs(self.session.voice, voices[0])
        self.assertIsNot(self.session.voice, lookups[0])
        self.assertIsNone(lookups[0]._room)
        self.assertIs(self.session.voice.token_http, self.pilot._token_http)
        events = self.session.latency.snapshot()["events"]
        stages = [event["stage"] for event in events]
        self.assertEqual(stages.count("crm_lookup_start"), 1)
        self.assertEqual(stages.count("crm_lookup"), 1)
        self.assertEqual(stages.count("crm_context_ready"), 1)
        self.assertLess(stages.index("crm_lookup_start"), stages.index("acs_connection_ready"))
        self.assertLess(stages.index("crm_context_ready"), stages.index("voip_invite"))
        self.assertLess(stages.index("crm_context_ready"), stages.index("acs_start_media"))
        diagnostics = json.dumps([self.session.snapshot(), self.session.latency.snapshot(), self.pilot.health()])
        for value in (PHONE, "Example Resident", "resident@example.com", "Synthetic Account"):
            self.assertNotIn(value, diagnostics)

    async def test_unknown_crm_customer_is_valid_but_has_no_invented_profile_or_name(self):
        self.pilot.customer_context_provider = AsyncMock(return_value=parse_customer_context(
            json.dumps(unknown_customer_context()), expected_phone=PHONE,
        ))
        await self.pilot._resolve_call(self.session)
        await self.pilot._connect(self.session, PHONE)
        voice = self.session.voice
        self.assertIsNone(voice.customer_context.customer)
        self.assertIsNone(voice.customer_name)
        self.assertIsNone(voice.customer_first_name)
        self.assertIsNone(voice.customer_email)
        self.assertEqual(voice.customer_phone, PHONE)
        self.assertEqual(voice.native_conversation_id, CONVERSATION)
        self.assertEqual(voice.intro_message, voice.deployment.organization.greeting["en"])
        self.f.connection.add_participant.assert_awaited_once()
        self.assertIn("crm_context_ready", [event["stage"] for event in self.session.latency.snapshot()["events"]])

    async def test_crm_lookup_deadline_cancels_request_without_inviting_or_streaming(self):
        cancelled = asyncio.Event()

        async def never_ready(voice):
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        self.pilot.customer_context_provider = AsyncMock(side_effect=never_ready)
        await self.pilot._resolve_call(self.session)
        with patch("backend.native_media.CRM_CONTEXT_SECONDS", 0.01), self.assertRaises(NativeMediaError) as captured:
            await self.pilot._connect(self.session, PHONE)
        self.assertEqual(captured.exception.code, "crm_context_timeout")
        self.assertTrue(cancelled.is_set())
        self.f.connection.add_participant.assert_not_awaited()
        self.f.connection.start_media_streaming.assert_not_awaited()
        self.assertIsNone(self.session.voice)
        self.assertFalse(self.session.bound.is_set())
        self.assertNotIn("crm_context_ready", [event["stage"] for event in self.session.latency.snapshot()["events"]])
        await self.pilot._detach(self.session)
        self.gateway.release.assert_awaited_once_with(self.rtc, self.f.connection)
        self.f.connection.hang_up.assert_awaited_once_with(is_for_everyone=False)
        self.assertTrue(self.session.detached)

    async def test_invalid_crm_provider_result_is_not_a_successful_empty_context(self):
        for result in (None, unknown_customer_context()):
            with self.subTest(result=result):
                self.pilot.customer_context_provider = AsyncMock(return_value=result)
                await self.pilot._resolve_call(self.session)
                with self.assertRaises(NativeMediaError) as captured:
                    await self.pilot._connect(self.session, PHONE)
                self.assertEqual(captured.exception.code, "crm_context_invalid")
        self.pilot.customer_context_provider = AsyncMock(side_effect=CRMContextError("Synthetic invalid data"))
        with self.assertRaises(NativeMediaError) as captured:
            await self.pilot._connect(self.session, PHONE)
        self.assertEqual(captured.exception.code, "crm_context_invalid")
        self.f.connection.add_participant.assert_not_awaited()
        self.f.connection.start_media_streaming.assert_not_awaited()

    async def test_fast_start_overlaps_registration_but_never_invites_before_call_validation(self):
        self.pilot.config = config(transport="voip", fast_start=True)
        connecting, registering = asyncio.Event(), asyncio.Event()
        connected, registered = asyncio.Event(), asyncio.Event()

        async def connect(**kwargs):
            connecting.set()
            await connected.wait()
            return SimpleNamespace(call_connection_id=CONNECTION)

        async def prepare(*args, **kwargs):
            registering.set()
            await registered.wait()
            return self.rtc

        self.f.automation.connect_call.side_effect = connect
        self.gateway.prepare.side_effect = prepare
        await self.pilot._resolve_call(self.session)
        task = asyncio.create_task(self.pilot._connect(self.session, PHONE))
        try:
            await asyncio.wait_for(asyncio.gather(connecting.wait(), registering.wait()), 1)
            registered.set()
            await asyncio.sleep(0)
            self.f.connection.add_participant.assert_not_awaited()
            self.assertIsNone(self.session.voice)
            connected.set()
            await asyncio.wait_for(task, 1)
            self.assertTrue(self.rtc.authorized)
            self.assertIs(self.session.voice.token_http, self.pilot._token_http)
            self.f.connection.add_participant.assert_awaited_once()
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_failed_call_validation_releases_a_parallel_prepared_identity_without_inviting(self):
        self.pilot.config = config(transport="voip", fast_start=True)
        prepared = asyncio.Event()

        async def prepare(*args, **kwargs):
            prepared.set()
            return self.rtc

        async def connect(**kwargs):
            await prepared.wait()
            return SimpleNamespace(call_connection_id=CONNECTION)

        self.gateway.prepare.side_effect = prepare
        self.f.automation.connect_call.side_effect = connect
        self.f.participants.clear()
        await self.pilot._resolve_call(self.session)
        with self.assertRaises(NativeMediaError):
            await self.pilot._connect(self.session, PHONE)
        self.f.connection.add_participant.assert_not_awaited()
        await self.pilot._detach(self.session)
        self.gateway.release.assert_awaited_once_with(self.rtc, self.f.connection)
        self.f.connection.hang_up.assert_awaited_once_with(is_for_everyone=False)

    async def test_parallel_preparation_is_cancelled_if_the_call_setup_is_cancelled(self):
        self.pilot.config = config(transport="voip", fast_start=True)
        registered, cancelled = asyncio.Event(), asyncio.Event()
        lookup_started, lookup_cancelled = asyncio.Event(), asyncio.Event()

        async def prepare(*args, **kwargs):
            registered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        async def connect(**kwargs):
            await asyncio.Event().wait()

        async def lookup(voice):
            lookup_started.set()
            try:
                await asyncio.Event().wait()
            finally:
                lookup_cancelled.set()

        self.pilot.customer_context_provider = AsyncMock(side_effect=lookup)
        self.gateway.prepare.side_effect = prepare
        self.f.automation.connect_call.side_effect = connect
        await self.pilot._resolve_call(self.session)
        task = asyncio.create_task(self.pilot._connect(self.session, PHONE))
        await asyncio.wait_for(asyncio.gather(registered.wait(), lookup_started.wait()), 1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(task, 1)
        self.assertTrue(cancelled.is_set())
        self.assertTrue(lookup_cancelled.is_set())
        self.f.connection.add_participant.assert_not_awaited()

    async def test_failed_parallel_registration_still_cleans_the_acs_leg(self):
        self.pilot.config = config(transport="voip", fast_start=True)
        self.gateway.prepare.side_effect = NativeVoipError("Synthetic registration failure")
        await self.pilot._resolve_call(self.session)
        with self.assertRaises(NativeVoipError):
            await self.pilot._connect(self.session, PHONE)
        self.assertIs(self.session.connection, self.f.connection)
        self.f.connection.add_participant.assert_not_awaited()
        await self.pilot._detach(self.session)
        self.f.connection.hang_up.assert_awaited_once_with(is_for_everyone=False)
        self.assertTrue(self.session.detached)

    async def test_fast_start_warms_only_when_enabled_and_does_not_create_a_call(self):
        await self.pilot.warmup()
        self.gateway.warm.assert_not_awaited()
        self.pilot.config = config(transport="voip", fast_start=True)
        await self.pilot.warmup()
        self.gateway.warm.assert_awaited_once()
        self.f.automation.connect_call.assert_not_awaited()
        self.gateway.prepare.assert_not_awaited()


class NativeSpeechControlTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.f = Fixture()
        self.session = NativeMediaSession(
            CONVERSATION, "ar", phase="active", speech_control=NativeSpeechControl("ar"),
        )
        self.session.voice = voice_factory(language="ar")
        self.state = self.session.speech_control
        self.addAsyncCleanup(self.f.pilot.close)

    async def test_initial_control_is_not_acknowledged_by_start_or_status(self):
        result = self.session.snapshot()
        self.assertEqual(result["control_language"], "ar")
        self.assertEqual(result["control_revision"], 0)
        self.assertFalse(result["control_ready"])
        self.state.acknowledge("ar", 0)
        self.assertTrue(self.session.snapshot()["control_ready"])

    async def test_stale_turn_cannot_acknowledge_a_new_language(self):
        self.state.acknowledge("ar", 0)
        self.state.language = "en"
        self.state.revision = 1
        self.state.acknowledge("ar", 0)
        self.assertFalse(self.state.ready)
        self.assertEqual(self.state.language, "en")
        self.state.acknowledge("en", 1)
        self.assertTrue(self.state.ready)

    async def test_invalid_language_revision_and_future_acknowledgements_are_rejected(self):
        for language, revision in (("fr", 0), (None, 0), ("ar", -1), ("ar", True), ("ar", 0.0), ("ar", "0"), ("en", 0), ("ar", 1)):
            with self.subTest(language=language, revision=revision), self.assertRaises(NativeMediaError):
                self.state.acknowledge(language, revision)
        self.assertFalse(self.state.ready)

    async def test_trusted_language_tool_waits_for_matching_completed_listening_turn(self):
        self.state.acknowledge("ar", 0)
        await self.session.voice._record_transcript(SpeakerRole.CUSTOMER, "caller-language-1", "Please speak English.")
        token = CURRENT_SESSION.set(self.session.voice)
        try:
            switching = asyncio.create_task(self.f.pilot._select_response_language(self.session, {"language": "en"}))
        finally:
            CURRENT_SESSION.reset(token)
        await asyncio.sleep(0)
        self.assertEqual(self.state.language, "en")
        self.assertEqual(self.state.revision, 1)
        self.assertEqual(self.session.voice.language, "ar")
        self.state.acknowledge("ar", 0)
        await asyncio.sleep(0)
        self.assertFalse(switching.done())
        self.state.acknowledge("en", 1)
        result = await asyncio.wait_for(switching, 1)
        payload = json.loads(result.to_text())
        self.assertTrue(payload["success"])
        self.assertEqual(payload["response_language"], "English")
        self.assertIn("pending question in English now", payload["message"])
        self.assertEqual(self.session.voice.language, "en")
        self.assertEqual(self.session.language, "ar")
        self.assertFalse(self.session.release.is_set())

    async def test_same_language_tool_is_idempotent_and_preserves_revision(self):
        self.state.acknowledge("ar", 0)
        token = CURRENT_SESSION.set(self.session.voice)
        try:
            result = await self.f.pilot._select_response_language(self.session, {"language": "ar"})
        finally:
            CURRENT_SESSION.reset(token)
        self.assertTrue(json.loads(result.to_text())["success"])
        self.assertEqual(self.state.revision, 0)

    async def test_wrong_session_and_mixed_listener_text_cannot_trigger_language_control(self):
        self.state.acknowledge("ar", 0)
        with self.assertRaises(ValueError):
            await self.f.pilot._select_response_language(self.session, {"language": "en"})
        token = CURRENT_SESSION.set(self.session.voice)
        try:
            for payload in ({"language": "en", "text": "ignored"}, {"text": "speak English"}, {"language": []}):
                with self.subTest(payload=payload), self.assertRaises(ValueError):
                    await self.f.pilot._select_response_language(self.session, payload)
        finally:
            CURRENT_SESSION.reset(token)
        self.assertEqual(self.state.revision, 0)

    async def test_failed_switch_pauses_ai_and_releases_control_instead_of_claiming_success(self):
        self.state.acknowledge("ar", 0)
        await self.session.voice._record_transcript(SpeakerRole.CUSTOMER, "caller-language-1", "Please speak English.")
        token = CURRENT_SESSION.set(self.session.voice)
        try:
            with patch("backend.native_media.LANGUAGE_ACK_SECONDS", 0), self.assertRaises(NativeMediaError):
                await self.f.pilot._select_response_language(self.session, {"language": "en"})
        finally:
            CURRENT_SESSION.reset(token)
        self.assertTrue(self.session.release.is_set())
        self.assertTrue(self.session.voice.transfer_active)
        self.assertEqual(self.session.voice.language, "ar")
        self.assertEqual(self.session.reason, "native_language_unconfirmed")

    async def test_language_change_without_caller_speech_is_rejected(self):
        self.state.acknowledge("ar", 0)
        token = CURRENT_SESSION.set(self.session.voice)
        try:
            with patch.object(self.session.voice.customer_transcript_updated, "wait", side_effect=TimeoutError):
                with self.assertRaises(ValueError):
                    await self.f.pilot._select_response_language(self.session, {"language": "en"})
        finally:
            CURRENT_SESSION.reset(token)
        self.assertEqual(self.state.language, "ar")
        self.assertEqual(self.state.revision, 0)
        self.assertFalse(self.session.release.is_set())

    async def test_delayed_final_caller_transcript_can_authorize_a_language_request(self):
        self.state.acknowledge("ar", 0)
        token = CURRENT_SESSION.set(self.session.voice)
        try:
            switching = asyncio.create_task(self.f.pilot._select_response_language(self.session, {"language": "en"}))
        finally:
            CURRENT_SESSION.reset(token)
        await asyncio.sleep(0)
        self.assertEqual(self.state.revision, 0)
        await self.session.voice._record_transcript(SpeakerRole.CUSTOMER, "caller-language-1", "Please speak English.")
        for _ in range(10):
            if self.state.revision == 1:
                break
            await asyncio.sleep(0)
        self.assertEqual(self.state.revision, 1)
        self.state.acknowledge("en", 1)
        await asyncio.wait_for(switching, 1)
        self.assertEqual(self.state.last_request_item_id, "caller-language-1")

    async def test_one_caller_utterance_cannot_flip_the_language_twice(self):
        self.state.acknowledge("ar", 0)
        await self.session.voice._record_transcript(SpeakerRole.CUSTOMER, "caller-language-1", "Please speak English.")
        self.state.last_request_item_id = "caller-language-1"
        token = CURRENT_SESSION.set(self.session.voice)
        try:
            with patch.object(self.session.voice.customer_transcript_updated, "wait", side_effect=TimeoutError):
                with self.assertRaises(ValueError):
                    await self.f.pilot._select_response_language(self.session, {"language": "en"})
        finally:
            CURRENT_SESSION.reset(token)
        self.assertEqual(self.state.revision, 0)

    async def test_call_end_wakes_language_waiter(self):
        waiting = asyncio.create_task(self.f.pilot._wait_for_language(self.session))
        await asyncio.sleep(0)
        self.session.release.set()
        with self.assertRaises(NativeMediaError):
            await asyncio.wait_for(waiting, 1)

    async def test_speech_control_opt_in_requires_voip_and_a_real_boolean(self):
        for value in ("true", 1, None, True):
            with self.subTest(value=value), self.assertRaises(NativeMediaError):
                self.f.pilot.start(CONVERSATION, "ar", speech_turn_control=value)
        self.f.pilot.config = config(transport="voip")
        with patch.object(self.f.pilot, "_run", new_callable=AsyncMock):
            current = self.f.pilot.start(CONVERSATION, "ar", speech_turn_control=True)
            await current.runner
            self.assertIsNotNone(current.speech_control)
            self.assertIs(self.f.pilot.start(CONVERSATION, "ar", speech_turn_control=True), current)
            with self.assertRaises(NativeMediaError):
                self.f.pilot.start(CONVERSATION, "ar")
        current.done.set()


class NativeTranscriptTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.f = Fixture()
        self.session = NativeMediaSession(CONVERSATION, "en")
        self.session.voice = voice_factory()
        self.f.pilot.sessions[CONVERSATION] = self.session
        self.addAsyncCleanup(self.f.pilot.close)

    async def record(self, role, identifier, text):
        await self.session.voice._record_transcript(role, identifier, text)

    async def test_cursor_exposes_only_actual_ai_turns_without_rewording(self):
        await self.record(SpeakerRole.CUSTOMER, "customer-1", "A customer question")
        await self.record(SpeakerRole.AI_AGENT, "ai-1", "First actual response.")
        await self.record(SpeakerRole.CUSTOMER, "customer-2", "Another question")
        await self.record(SpeakerRole.AI_AGENT, "ai-2", "Second actual response.")
        first = self.session.snapshot(0)
        second = self.session.snapshot(1)
        self.assertEqual(first["utterance_sequence"], 1)
        self.assertEqual(first["utterance_id"], "ai-1")
        self.assertEqual(first["utterance_text"], "First actual response.")
        self.assertEqual(second["utterance_sequence"], 2)
        self.assertEqual(second["utterance_text"], "Second actual response.")
        self.assertEqual(self.session.snapshot(2)["utterance_text"], "")
        self.assertEqual(self.session.snapshot(0), first)
        self.assertTrue(first["utterance_timestamp"].endswith("+00:00"))
        self.f.automation.connect_call.assert_not_awaited()

    async def test_ordinary_status_contains_no_transcript_text(self):
        await self.record(SpeakerRole.AI_AGENT, "ai-1", "Private conversation text.")
        self.assertNotIn("Private conversation text.", json.dumps(self.session.snapshot()))
        self.assertEqual(self.session.snapshot()["utterance_sequence"], 0)

    async def test_provider_duplicate_is_not_a_second_native_turn(self):
        await self.record(SpeakerRole.AI_AGENT, "ai-1", "Actual response.")
        self.session.voice.transcript_updated.clear()
        await self.record(SpeakerRole.AI_AGENT, "ai-1", "Actual response.")
        self.assertFalse(self.session.voice.transcript_updated.is_set())
        self.assertEqual(self.session.snapshot(1)["utterance_text"], "")

    async def test_customer_transcripts_do_not_wake_ai_text_consumer(self):
        await self.record(SpeakerRole.CUSTOMER, "customer-1", "Question.")
        self.assertFalse(self.session.voice.transcript_updated.is_set())
        self.assertEqual(self.session.snapshot(0)["utterance_text"], "")

    async def test_long_poll_wakes_on_final_ai_text_not_only_on_call_end(self):
        waiting = asyncio.create_task(self.f.pilot._wait_for_transcript(self.session, 0))
        await asyncio.sleep(0)
        await self.record(SpeakerRole.AI_AGENT, "ai-1", "Actual response.")
        result = await asyncio.wait_for(waiting, 1)
        self.assertEqual(result["utterance_text"], "Actual response.")
        self.assertFalse(self.session.done.is_set())

    async def test_pending_turn_survives_handover_and_is_available_before_final_action(self):
        await self.record(SpeakerRole.AI_AGENT, "ai-1", "Actual response.")
        self.session.phase = "handover_ready"
        self.session.action = "handover"
        self.session.detached = True
        self.session.done.set()
        last = await self.f.pilot._wait_for_transcript(self.session, 0)
        self.assertEqual(last["utterance_text"], "Actual response.")
        self.assertEqual(last["action"], "handover")
        final = await self.f.pilot._wait_for_transcript(self.session, 1)
        self.assertEqual(final["utterance_text"], "")
        self.assertEqual(final["action"], "handover")

    async def test_waiter_cancels_cleanly_and_timeout_has_typed_empty_text(self):
        waiting = asyncio.create_task(self.f.pilot._wait_for_transcript(self.session, 0))
        await asyncio.sleep(0)
        waiting.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await waiting
        with patch("backend.native_media.WAIT_SECONDS", 0):
            result = await self.f.pilot._wait_for_transcript(self.session, 0)
        for name in ("utterance_id", "utterance_text", "utterance_timestamp", "reason"):
            self.assertIsInstance(result[name], str)
        self.assertEqual(result["utterance_sequence"], 0)

    async def test_waiter_handles_media_binding_after_control_request(self):
        voice = self.session.voice
        self.session.voice = None
        waiting = asyncio.create_task(self.f.pilot._wait_for_transcript(self.session, 0))
        await asyncio.sleep(0)
        self.session.voice = voice
        self.session.bound.set()
        await self.record(SpeakerRole.AI_AGENT, "ai-1", "Actual response.")
        result = await asyncio.wait_for(waiting, 1)
        self.assertEqual(result["utterance_id"], "ai-1")

    async def test_cursor_is_scoped_and_overlarge_text_is_not_silently_truncated(self):
        with self.assertRaises(NativeMediaError):
            self.session.snapshot(1)
        with self.assertRaises(NativeMediaError):
            self.session.snapshot(-1)
        await self.record(SpeakerRole.AI_AGENT, "ai-1", "x" * (16 * 1024 + 1))
        with self.assertRaises(NativeMediaError) as captured:
            self.session.snapshot(0)
        self.assertEqual(captured.exception.code, "transcript_too_large")

    async def test_native_text_does_not_replay_audio_in_the_media_adapter(self):
        voice = self.session.voice
        voice._is_acs = True
        voice._client_ws = SimpleNamespace(closed=False, send_json=AsyncMock())
        await self.record(SpeakerRole.AI_AGENT, "ai-1", "Actual response.")
        voice._client_ws.send_json.assert_not_awaited()
        self.assertEqual(voice.output_audio_frames, 0)
        self.assertEqual(self.session.snapshot(0)["utterance_text"], "Actual response.")


class NativeStartupAudioTests(unittest.IsolatedAsyncioTestCase):
    def voice(self):
        voice = voice_factory()
        voice._ready = True
        voice._is_acs = True
        voice._client_ws = SimpleNamespace(closed=False)
        voice._source = SimpleNamespace(capture_frame=AsyncMock(), clear_queue=MagicMock())
        voice.native_audio_sink = AsyncMock()
        voice.native_startup_pending = True
        return voice

    async def test_startup_buffer_preserves_and_paces_first_frames(self):
        voice = self.voice()
        frames = [
            {"type": "response.output_audio.delta", "delta": base64.b64encode(value * 480).decode()}
            for value in (b"\1\0", b"\2\0", b"\3\0")
        ]
        times = []

        async def delivered(message):
            times.append(asyncio.get_running_loop().time())

        voice.native_audio_sink.side_effect = delivered
        for message in frames:
            await voice._send_to_client(message)
        await voice._capture_audio({"audio": frames[0]["delta"]})
        voice.native_audio_sink.assert_not_awaited()
        voice._source.capture_frame.assert_not_awaited()
        self.assertEqual(voice.output_audio_frames, 0)
        await voice.release_native_startup()
        await asyncio.gather(*tuple(voice._tasks))
        self.assertEqual([call.args[0] for call in voice.native_audio_sink.await_args_list], frames)
        self.assertTrue(all(b - a >= 0.015 for a, b in zip(times, times[1:])))
        self.assertEqual(voice.output_audio_frames, 3)
        self.assertEqual(voice._native_startup_output_bytes, 0)
        self.assertFalse(voice.native_startup_pending)
        await voice._capture_audio({"audio": frames[0]["delta"]})
        voice._source.capture_frame.assert_awaited_once()

    async def test_startup_audio_is_bounded_and_invalid_pcm_is_not_queued(self):
        voice = self.voice()
        second = {"type": "response.output_audio.delta", "delta": base64.b64encode(b"\1\0" * 24000).decode()}
        for _ in range(10):
            await voice._send_to_client(second)
        with self.assertRaisesRegex(HumainError, "bounded buffer"):
            await voice._send_to_client(second)
        self.assertEqual(voice._native_startup_output_bytes, 480000)
        voice._clear_native_startup_output()
        for delta in (None, "invalid!", "", base64.b64encode(b"\1").decode()):
            with self.subTest(delta=delta), self.assertRaises(HumainError):
                await voice._send_to_client({"type": "response.output_audio.delta", "delta": delta})
        voice.native_audio_sink.assert_not_awaited()

    async def test_pause_discards_queued_startup_audio_and_does_not_resume_it(self):
        voice = self.voice()
        voice.native_pause_enabled = True
        await voice._send_to_client({
            "type": "response.output_audio.delta", "delta": base64.b64encode(b"\1\0" * 480).decode(),
        })
        await voice.pause_audio(AsyncMock())
        self.assertTrue(voice.ai_paused)
        self.assertEqual(voice._native_startup_output_bytes, 0)
        with self.assertRaises(HumainError):
            await voice.release_native_startup()
        voice.native_audio_sink.assert_not_awaited()


class RouteTests(unittest.IsolatedAsyncioTestCase):
    async def test_application_owner_uses_prepared_start_without_native_announcement(self):
        current = NativeMediaSession(CONVERSATION, "en", play_startup_notice=True, prepare_startup=True)
        current.startup_prepared.set()
        with patch.object(self.pilot, "start", return_value=current) as start:
            response = await self.http.post(
                f"{PREFIX}/sessions?wait_for_startup=true",
                json={"conversation_id": CONVERSATION, "language": "en", "retain_native_controller": True,
                      "speech_turn_control": True, "play_startup_notice": True}, headers=self.headers,
            )
        self.assertEqual(response.status, 202)
        start.assert_called_once_with(CONVERSATION, "en", speech_turn_control=True,
                                      play_startup_notice=True, prepare_startup=True)
        result = await response.json()
        self.assertTrue(result["native_startup_prepared"])
        self.assertEqual(result["startup_notice_owner"], "application")
        self.assertFalse(result["control_ready"])

    async def test_native_owner_waits_for_preparation_without_waiting_for_language_ack(self):
        current = NativeMediaSession(CONVERSATION, "en", native_startup_notice=True)
        started = asyncio.Event()

        def accepted(*args, **kwargs):
            started.set()
            return current

        with patch.object(self.pilot, "start", side_effect=accepted):
            request = asyncio.create_task(self.http.post(
                f"{PREFIX}/sessions?wait_for_startup=true",
                json={"conversation_id": CONVERSATION, "language": "en", "retain_native_controller": True,
                      "speech_turn_control": True, "native_startup_notice": True}, headers=self.headers,
            ))
            await asyncio.wait_for(started.wait(), 1)
            self.assertFalse(request.done())
            current.startup_prepared.set()
            response = await asyncio.wait_for(request, 1)
        result = await response.json()
        self.assertEqual(response.status, 202)
        self.assertTrue(result["native_startup_prepared"])
        self.assertEqual(result["startup_notice_owner"], "native")
        self.assertFalse(result["control_ready"])
        self.assertIsNone(current.notice)

    async def test_native_startup_wait_is_explicit_and_exclusive(self):
        for query, options in (
            ("", {"native_startup_notice": True}),
            ("wait_for_startup=false", {"native_startup_notice": True}),
            ("wait_for_startup=true", {}),
            ("wait_for_startup=true", {"play_startup_notice": True, "native_startup_notice": True}),
            ("wait_for_startup=true&wait_for_startup=true", {"native_startup_notice": True}),
            ("wait_for_context=true&wait_for_startup=true", {"native_startup_notice": True}),
        ):
            with self.subTest(query=query), patch.object(self.pilot, "start") as start:
                response = await self.http.post(
                    f"{PREFIX}/sessions?{query}",
                    json={"conversation_id": CONVERSATION, "language": "ar", "retain_native_controller": True,
                          "speech_turn_control": True, **options}, headers=self.headers,
                )
                self.assertEqual(response.status, 400)
                await response.read()
                start.assert_not_called()

    async def test_native_preparation_timeout_releases_the_managed_session(self):
        current = NativeMediaSession(CONVERSATION, "en", native_startup_notice=True)
        with patch.object(self.pilot, "start", return_value=current), patch(
            "backend.native_media.STARTUP_RESPONSE_SECONDS", 0.01,
        ), self.assertLogs("native_media", level="WARNING"):
            response = await self.http.post(
                f"{PREFIX}/sessions?wait_for_startup=true",
                json={"conversation_id": CONVERSATION, "language": "en", "retain_native_controller": True,
                      "speech_turn_control": True, "native_startup_notice": True}, headers=self.headers,
            )
        self.assertEqual(response.status, 504)
        self.assertTrue(current.release.is_set())
        self.assertEqual((await response.json())["error"]["code"], "native_startup_unconfirmed")

    async def test_native_speech_start_returns_while_context_and_provider_are_still_pending(self):
        current = NativeMediaSession(CONVERSATION, "ar", play_startup_notice=True)
        with patch.object(self.pilot, "start", return_value=current) as start, patch.object(
            self.pilot, "_wait_for_context", new_callable=AsyncMock,
        ) as wait:
            response = await asyncio.wait_for(self.http.post(
                f"{PREFIX}/sessions",
                json={"conversation_id": CONVERSATION, "language": "ar", "retain_native_controller": True,
                      "speech_turn_control": True, "play_startup_notice": True},
                headers=self.headers,
            ), 1)
        self.assertEqual(response.status, 202)
        self.assertFalse((await response.json())["provider_ready"])
        self.assertFalse(current.context_ready.is_set())
        self.assertFalse(current.done.is_set())
        self.assertFalse(current.release.is_set())
        start.assert_called_once_with(CONVERSATION, "ar", speech_turn_control=True, play_startup_notice=True)
        wait.assert_not_awaited()

    async def test_initial_request_waits_only_for_context_not_full_provider_readiness(self):
        current = NativeMediaSession(CONVERSATION, "ar", play_startup_notice=True)
        started = asyncio.Event()

        def start(*args, **kwargs):
            started.set()
            return current

        body = {
            "conversation_id": CONVERSATION, "language": "ar", "retain_native_controller": True,
            "speech_turn_control": True, "play_startup_notice": True,
        }
        with patch.object(self.pilot, "start", side_effect=start):
            task = asyncio.create_task(self.http.post(
                f"{PREFIX}/sessions?wait_for_context=true", json=body, headers=self.headers,
            ))
            await asyncio.wait_for(started.wait(), 1)
            self.assertFalse(task.done())
            current.context_ready.set()
            response = await asyncio.wait_for(task, 1)
        self.assertEqual(response.status, 202)
        data = await response.json()
        self.assertEqual(data["conversation_id"], CONVERSATION)
        self.assertFalse(data["provider_ready"])
        self.assertEqual(data["startup_notice_state"], "not_started")
        self.assertFalse(current.done.is_set())

    async def test_context_wait_finishes_if_setup_stops_without_context(self):
        current = NativeMediaSession(CONVERSATION, "ar", play_startup_notice=True, phase="stopped")
        current.done.set()
        with patch.object(self.pilot, "start", return_value=current):
            response = await self.http.post(
                f"{PREFIX}/sessions?wait_for_context=true",
                json={"conversation_id": CONVERSATION, "language": "ar", "retain_native_controller": True, "speech_turn_control": True, "play_startup_notice": True},
                headers=self.headers,
            )
        self.assertEqual(response.status, 202)
        self.assertEqual((await response.json())["phase"], "stopped")
        self.assertFalse(current.context_ready.is_set())

    async def test_context_wait_is_bounded_and_stops_the_pending_session_on_timeout(self):
        current = NativeMediaSession(CONVERSATION, "ar", play_startup_notice=True)
        with patch.object(self.pilot, "start", return_value=current), patch(
            "backend.native_media.CONTEXT_RESPONSE_SECONDS", 0.01,
        ), self.assertLogs("native_media", level="WARNING"):
            response = await self.http.post(
                f"{PREFIX}/sessions?wait_for_context=true",
                json={"conversation_id": CONVERSATION, "language": "ar", "retain_native_controller": True, "speech_turn_control": True, "play_startup_notice": True},
                headers=self.headers,
            )
        self.assertEqual(response.status, 504)
        self.assertTrue(current.release.is_set())
        self.assertEqual((await response.json())["error"]["code"], "native_context_unconfirmed")

    async def test_cancelling_a_context_waiter_does_not_cancel_accepted_setup(self):
        current = NativeMediaSession(CONVERSATION, "ar", play_startup_notice=True)
        task = asyncio.create_task(self.pilot._wait_for_context(current))
        await asyncio.sleep(0)
        with self.assertLogs("native_media", level="WARNING"):
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertFalse(current.release.is_set())
        self.assertFalse(current.done.is_set())
        self.assertIsNone(current.reason)

    async def test_context_wait_query_is_explicit_typed_and_notice_only(self):
        for query, notice in (
            ("wait_for_context=true", False), ("wait_for_context=1", True),
            ("wait_for_context=true&wait_for_context=false", True), ("other=true", True),
        ):
            with self.subTest(query=query), patch.object(self.pilot, "start") as start:
                response = await self.http.post(
                    f"{PREFIX}/sessions?{query}",
                    json={"conversation_id": CONVERSATION, "language": "ar", "retain_native_controller": True, "speech_turn_control": True, "play_startup_notice": notice},
                    headers=self.headers,
                )
                self.assertEqual(response.status, 400)
                await response.read()
                start.assert_not_called()

    async def test_notice_readiness_reports_asset_validation_without_starting_playback(self):
        response = await self.http.get(f"{PREFIX}/readiness", headers=self.headers)
        self.assertEqual(response.status, 200)
        ready = await response.json()
        self.assertTrue(ready["startup_notice_available"])
        self.assertEqual(ready["startup_notice_sha256"], notice_asset()[1])
        self.f.automation.connect_call.assert_not_awaited()
        with patch("backend.native_media.notice_asset", side_effect=NativeNoticeError("startup_notice_asset_invalid")):
            with self.assertLogs("native_media", level="ERROR"):
                response = await self.http.get(f"{PREFIX}/readiness", headers=self.headers)
        ready = await response.json()
        self.assertFalse(ready["startup_notice_available"])
        self.assertEqual(ready["startup_notice_error"], "startup_notice_asset_invalid")
        self.f.automation.connect_call.assert_not_awaited()

    async def test_notice_audio_is_public_but_only_the_exact_static_content_can_be_downloaded(self):
        content, digest = notice_asset()
        response = await self.http.get(f"{PREFIX}/startup-notice/{digest}.wav")
        self.assertEqual(response.status, 200)
        self.assertEqual(await response.read(), content)
        self.assertEqual(response.content_type, "audio/wav")
        self.assertIn("immutable", response.headers["Cache-Control"])
        response = await self.http.get(f"{PREFIX}/startup-notice/wrong.wav")
        self.assertEqual(response.status, 404)
        self.f.dataverse.get.assert_not_awaited()

    async def test_notice_callbacks_require_capability_and_exact_playback_identity(self):
        current = NativeMediaSession(CONVERSATION, "ar", play_startup_notice=True)
        current.notice = NativeStartupNotice(
            self.f.connection, CONNECTION, CORRELATION, current.latency, "synthetic-notice",
        )
        current.notice.submitted = True
        self.pilot.sessions[CONVERSATION] = current
        event = {
            "type": "Microsoft.Communication.PlayCompleted",
            "data": {
                "callConnectionId": CONNECTION, "correlationId": CORRELATION,
                "operationContext": "synthetic-notice",
            },
        }
        path = f"{PREFIX}/sessions/{CONVERSATION}/notice-events"
        response = await self.http.post(path, json=[event], headers=self.headers)
        self.assertEqual(response.status, 401)
        response = await self.http.post(path + "?token=wrong", json=[event])
        self.assertEqual(response.status, 401)
        path += "?token=" + current.capability
        response = await self.http.post(path, json=[{**event, "data": {**event["data"], "callConnectionId": OTHER}}])
        self.assertEqual(response.status, 400)
        self.assertFalse(current.notice.terminal.is_set())
        response = await self.http.post(path, json=[event])
        self.assertEqual(response.status, 200)
        self.assertEqual(await response.json(), {"accepted": 1})
        self.assertEqual(current.snapshot()["startup_notice_state"], "stopped")
        self.assertNotIn(current.capability, json.dumps(current.snapshot()))
        current.done.set()

    async def test_notice_callback_errors_and_oversized_batches_are_explicit(self):
        current = NativeMediaSession(CONVERSATION, "ar", play_startup_notice=True)
        current.notice = NativeStartupNotice(self.f.connection, CONNECTION, CORRELATION, current.latency, "notice")
        self.pilot.sessions[CONVERSATION] = current
        path = f"{PREFIX}/sessions/{CONVERSATION}/notice-events?token={current.capability}"
        for data, status in (({}, 400), ([], 400), ([{}] * 9, 400), (["x" * 17000], 413)):
            with self.subTest(status=status):
                response = await self.http.post(path, json=data)
                self.assertEqual(response.status, status)
                await response.read()
        current.done.set()

    async def test_notice_callback_capability_is_not_written_to_access_logs(self):
        log = MagicMock()
        logger = NativeMediaAccessLogger(log, "")
        request = SimpleNamespace(
            path=f"{PREFIX}/sessions/{CONVERSATION}/notice-events", remote="127.0.0.1",
            method="POST", query_string="token=synthetic-secret",
        )
        logger.log(request, SimpleNamespace(status=200), 0.01)
        fmt, *values = log.info.call_args.args
        output = fmt % tuple(values)
        self.assertNotIn(CONVERSATION, output)
        self.assertNotIn("synthetic-secret", output)
        self.assertIn("/[redacted]/notice-events", output)

    async def test_start_passes_notice_only_when_explicit_and_never_accepts_arbitrary_audio(self):
        body = {
            "conversation_id": CONVERSATION, "language": "ar", "retain_native_controller": True,
            "speech_turn_control": True, "play_startup_notice": True,
        }
        current = NativeMediaSession(CONVERSATION, "ar", play_startup_notice=True)
        with patch.object(self.pilot, "start", return_value=current) as start:
            response = await self.http.post(f"{PREFIX}/sessions", json=body, headers=self.headers)
            self.assertEqual(response.status, 202)
            start.assert_called_once_with(CONVERSATION, "ar", speech_turn_control=True, play_startup_notice=True)
        response = await self.http.post(
            f"{PREFIX}/sessions", json={**body, "notice_url": "https://example.invalid/arbitrary.wav"},
            headers=self.headers,
        )
        self.assertEqual(response.status, 400)

    async def test_detailed_timings_require_control_auth_and_are_not_in_poll_responses(self):
        current = NativeMediaSession(CONVERSATION, "ar")
        self.pilot.sessions[CONVERSATION] = current
        path = f"{PREFIX}/sessions/{CONVERSATION}"
        response = await self.http.get(path + "/timings")
        self.assertEqual(response.status, 401)
        response = await self.http.get(path + "/timings", headers=self.headers)
        self.assertEqual(response.status, 200)
        data = await response.json()
        self.assertEqual(data["conversation_id"], CONVERSATION)
        self.assertEqual(data["clock"], "monotonic")
        self.assertEqual(data["events"][0]["stage"], "session_created")
        self.assertNotIn(current.capability, json.dumps(data))
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        response = await self.http.get(path, headers=self.headers)
        self.assertNotIn("events", await response.json())
        current.done.set()

    async def asyncSetUp(self):
        self.f = Fixture()
        self.pilot = self.f.pilot
        app = web.Application(handler_args={"access_log_class": NativeMediaAccessLogger})
        self.pilot.register(app)
        self.http = TestClient(TestServer(app))
        await self.http.start_server()
        self.addAsyncCleanup(self.http.close)
        self.addAsyncCleanup(self.pilot.close)
        self.headers = {"X-API-Key": KEY}

    async def test_all_control_routes_require_a_dedicated_key(self):
        for method, path in (
            ("GET", f"{PREFIX}/readiness"),
            ("POST", f"{PREFIX}/sessions"),
            ("GET", f"{PREFIX}/sessions/{CONVERSATION}"),
            ("POST", f"{PREFIX}/sessions/{CONVERSATION}/stop"),
        ):
            response = await self.http.request(method, path, headers={"X-API-Key": "other-key"})
            self.assertEqual(response.status, 401)
            await response.read()
        self.f.dataverse.get.assert_not_awaited()

    async def test_transcript_feed_is_opt_in_authenticated_and_cursor_validated(self):
        session = NativeMediaSession(CONVERSATION, "en")
        session.voice = voice_factory()
        await session.voice._record_transcript(SpeakerRole.AI_AGENT, "ai-1", "Private actual response.")
        self.pilot.sessions[CONVERSATION] = session
        url = f"{PREFIX}/sessions/{CONVERSATION}"
        response = await self.http.get(url + "?transcript_after=0")
        self.assertEqual(response.status, 401)
        response = await self.http.get(url, headers=self.headers)
        self.assertNotIn("Private actual response.", json.dumps(await response.json()))
        response = await self.http.get(url + "?wait=true&transcript_after=0", headers=self.headers)
        self.assertEqual(response.status, 200)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual((await response.json())["utterance_text"], "Private actual response.")
        for cursor in ("-1", "1.5", "invalid", "2"):
            with self.subTest(cursor=cursor):
                response = await self.http.get(url, params={"transcript_after": cursor}, headers=self.headers)
                self.assertEqual(response.status, 400)
        response = await self.http.post(url + "/stop?transcript_after=0", headers=self.headers)
        self.assertEqual(response.status, 400)
        session.done.set()

    async def test_start_requires_explicit_retained_controller_and_no_extra_inputs(self):
        for body in (
            None, [], {},
            {"conversation_id": CONVERSATION, "language": "en", "retain_native_controller": False},
            {"conversation_id": CONVERSATION, "language": "en", "retain_native_controller": True, "operator_number": PHONE},
        ):
            response = await self.http.post(f"{PREFIX}/sessions", json=body, headers=self.headers)
            self.assertEqual(response.status, 400)
            await response.read()
        self.f.automation.connect_call.assert_not_awaited()

    async def test_chunked_start_body_is_read_completely_and_is_size_limited(self):
        body = json.dumps({
            "conversation_id": CONVERSATION, "language": "en", "retain_native_controller": True,
        }).encode()

        async def chunks(data):
            for byte in data:
                yield bytes([byte])

        session = NativeMediaSession(CONVERSATION, "en")
        with patch.object(self.pilot, "start", return_value=session) as start:
            response = await self.http.post(f"{PREFIX}/sessions", data=chunks(body), headers=self.headers)
            self.assertEqual(response.status, 202)
            result = await response.json()
            self.assertEqual(result["action"], "wait")
            self.assertNotIn(session.capability, json.dumps(result))
            start.assert_called_once_with(CONVERSATION, "en")
        response = await self.http.post(f"{PREFIX}/sessions", data=chunks(b" " * 2049), headers=self.headers)
        self.assertEqual(response.status, 413)

    async def test_listening_turn_is_authenticated_typed_and_never_accepts_speech_text(self):
        current = NativeMediaSession(CONVERSATION, "ar", speech_control=NativeSpeechControl("ar"))
        self.pilot.sessions[CONVERSATION] = current
        url = f"{PREFIX}/sessions/{CONVERSATION}/turn"
        body = {"language": "ar", "revision": 0}
        response = await self.http.post(url, json=body)
        self.assertEqual(response.status, 401)
        response = await self.http.post(url, json=body, headers=self.headers)
        self.assertEqual(response.status, 200)
        self.assertTrue((await response.json())["control_ready"])
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual(current.action, "wait")
        for payload in (
            {**body, "text": "connect me to a human"}, {**body, "intent": "handover"},
            {"language": "en", "revision": 0}, {"language": "ar", "revision": True},
        ):
            with self.subTest(payload=payload):
                response = await self.http.post(url, json=payload, headers=self.headers)
                self.assertIn(response.status, (400, 409))
        response = await self.http.post(url + "?transcript_after=0", json=body, headers=self.headers)
        self.assertEqual(response.status, 400)
        self.assertFalse(current.release.is_set())
        self.f.automation.connect_call.assert_not_awaited()
        current.done.set()

    async def test_listening_turn_cannot_be_used_on_the_legacy_controller(self):
        current = NativeMediaSession(CONVERSATION, "ar")
        self.pilot.sessions[CONVERSATION] = current
        response = await self.http.post(
            f"{PREFIX}/sessions/{CONVERSATION}/turn", json={"language": "ar", "revision": 0}, headers=self.headers,
        )
        self.assertEqual(response.status, 409)
        self.assertFalse(current.release.is_set())
        current.done.set()

    async def test_start_passes_explicit_speech_control_opt_in(self):
        body = {
            "conversation_id": CONVERSATION, "language": "ar", "retain_native_controller": True,
            "speech_turn_control": True,
        }
        current = NativeMediaSession(CONVERSATION, "ar", speech_control=NativeSpeechControl("ar"))
        with patch.object(self.pilot, "start", return_value=current) as start:
            response = await self.http.post(f"{PREFIX}/sessions", json=body, headers=self.headers)
            self.assertEqual(response.status, 202)
            start.assert_called_once_with(CONVERSATION, "ar", speech_turn_control=True)
            self.assertFalse((await response.json())["control_ready"])

    async def test_start_passes_identity_retention_only_when_explicitly_requested(self):
        body = {
            "conversation_id": CONVERSATION, "language": "ar", "retain_native_controller": True,
            "speech_turn_control": True, "retain_identity_for_metrics": True,
        }
        current = NativeMediaSession(
            CONVERSATION, "ar", transport="voip", speech_control=NativeSpeechControl("ar"),
            retain_identity_for_metrics=True,
        )
        with patch.object(self.pilot, "start", return_value=current) as start:
            response = await self.http.post(f"{PREFIX}/sessions", json=body, headers=self.headers)
            self.assertEqual(response.status, 202)
            start.assert_called_once_with(
                CONVERSATION, "ar", speech_turn_control=True, retain_identity_for_metrics=True,
            )
            result = await response.json()
            self.assertEqual(result["identity_retention_seconds"], 900)
            self.assertEqual(result["identity_cleanup_status"], "not_allocated")
        for invalid in (
            {**body, "identity_retention_seconds": 3600},
            {**body, "retain_identity_for_metrics": "true"},
            {**body, "speech_turn_control": False},
        ):
            response = await self.http.post(f"{PREFIX}/sessions", json=invalid, headers=self.headers)
            self.assertEqual(response.status, 400)
            await response.read()

    async def test_status_never_claims_human_connected(self):
        session = NativeMediaSession(CONVERSATION, "en", phase="handover_ready", action="handover")
        session.detached = True
        session.done.set()
        self.pilot.sessions[CONVERSATION] = session
        response = await self.http.get(f"{PREFIX}/sessions/{CONVERSATION}?wait=true", headers=self.headers)
        self.assertEqual(response.status, 200)
        body = await response.json()
        self.assertEqual(body["action"], "handover")
        self.assertEqual(body["human_acceptance"], "not_observed_by_media_adapter")
        self.assertNotIn("capability", body)
        self.assertEqual(response.headers["Cache-Control"], "no-store")

    async def test_selected_case_status_and_recovered_stop_are_authenticated_and_uncached(self):
        session = NativeMediaSession(CONVERSATION, "en", phase="handover_ready", action="handover")
        session.detached = True
        session.done.set()
        session.voice = voice_factory()
        session.voice.selected_case = parse_case_receipt(
            case_receipt(link_status="failed", link_details={"error": "READBACK_FAILED", "httpStatus": 403, "stage": "readback"}),
            requested_reference=REFERENCE, native_conversation_id=CONVERSATION,
        )
        self.pilot.sessions[CONVERSATION] = session
        url = f"{PREFIX}/sessions/{CONVERSATION}"
        unauthorized = await self.http.get(url)
        self.assertEqual(unauthorized.status, 401)
        self.assertNotIn(REFERENCE, await unauthorized.text())
        for method, path in (("GET", url), ("POST", url + "/stop")):
            response = await self.http.request(method, path, headers=self.headers)
            self.assertEqual(response.status, 200)
            self.assertEqual(response.headers["Cache-Control"], "no-store")
            body = await response.json()
            self.assertEqual(body["selected_case_number"], REFERENCE)
            self.assertEqual(body["case_link_status"], "failed")
            self.assertEqual(body["case_link_http_status"], 403)
            self.assertEqual(body["human_acceptance"], "not_observed_by_media_adapter")

    async def test_wrong_expired_and_mismatched_media_connections_are_rejected(self):
        session = NativeMediaSession(CONVERSATION, "en")
        session.bound.set()
        session.connection_id = CONNECTION
        session.voice = voice_factory()
        self.pilot.sessions[CONVERSATION] = session
        url = f"{PREFIX}/sessions/{CONVERSATION}/media"
        for token, connection_id, expected in (
            ("wrong", CONNECTION, 401), ("é", CONNECTION, 401),
            (session.capability, OTHER, 403),
        ):
            with self.subTest(token=token), self.assertRaises(WSServerHandshakeError) as captured:
                await self.http.ws_connect(url, params={"token": token}, headers={"x-ms-call-connection-id": connection_id})
            self.assertEqual(captured.exception.status, expected)
        session.done.set()
        with self.assertRaises(WSServerHandshakeError) as captured:
            await self.http.ws_connect(url, params={"token": session.capability}, headers={"x-ms-call-connection-id": CONNECTION})
        self.assertEqual(captured.exception.status, 401)

    async def test_verified_media_routes_to_exact_session_and_rejects_duplicate_socket(self):
        session = NativeMediaSession(CONVERSATION, "en")
        session.bound.set()
        session.connection_id = CONNECTION
        session.voice = voice_factory()
        self.pilot.sessions[CONVERSATION] = session
        entered = asyncio.Event()

        async def consume(ws, is_acs):
            self.assertTrue(is_acs)
            entered.set()
            await ws.receive()
            await ws.close()

        session.voice.forward_messages = consume
        url = f"{PREFIX}/sessions/{CONVERSATION}/media"
        ws = await self.http.ws_connect(
            url, params={"token": session.capability}, headers={"x-ms-call-connection-id": CONNECTION},
        )
        await asyncio.wait_for(entered.wait(), 1)
        with self.assertRaises(WSServerHandshakeError) as captured:
            await self.http.ws_connect(url, params={"token": session.capability}, headers={"x-ms-call-connection-id": CONNECTION})
        self.assertEqual(captured.exception.status, 409)
        await ws.close()
        await asyncio.wait_for(session.release.wait(), 1)
        self.assertEqual(session.reason, "media_stream_closed")
        session.done.set()


if __name__ == "__main__":
    unittest.main()
