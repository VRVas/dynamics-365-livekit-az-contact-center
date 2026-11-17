import asyncio
import base64
import importlib.util
import json
import sys
import unittest
from functools import partial
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, create_autospec, patch
from urllib.parse import urlsplit

from azure.communication.callautomation import (
    AudioFormat, CallConnectionState, MediaStreamingAudioChannelType,
    MediaStreamingContentType, MediaStreamingOptions, StreamingTransportType,
)
from azure.communication.callautomation.aio import CallAutomationClient
from azure.core.exceptions import HttpResponseError
from azure.core.pipeline.transport import AsyncHttpResponse, AsyncHttpTransport


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "infra/scripts"))
spec = importlib.util.spec_from_file_location(
    "probe_dynamics_call_control", ROOT / "infra/scripts/probe_dynamics_call_control.py"
)
probe = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = probe
spec.loader.exec_module(probe)

SCOPE = probe.ProbeScope(
    tenant_id="33333333-3333-4333-8333-333333333333",
    org_url="https://example.crm.dynamics.com",
    native_acs_resource_id="/subscriptions/33333333-3333-4333-8333-333333333333/resourceGroups/unit-native/providers/Microsoft.Communication/communicationServices/unit-native",
    workstream_id="44444444-4444-4444-8444-444444444444",
    queue_id="55555555-5555-4555-8555-555555555555",
    callback_url="https://voice.example.swedencentral.azurecontainerapps.io/acs/call-events",
)
inspect_conversation = partial(probe.inspect_conversation, scope=SCOPE)
select_native_call = partial(probe.select_native_call, scope=SCOPE)
attach_and_leave = partial(probe.attach_and_leave, scope=SCOPE)

NOW = datetime(2026, 10, 17, 12, 0, tzinfo=timezone.utc)
CONVERSATION = "11111111-1111-4111-8111-111111111111"
CORRELATION = "22222222-2222-4222-8222-222222222222"
SERVER_ID = "opaque-synthetic-server-id"


def call_item(**overrides):
    return {
        "msdyn_ocliveworkitemid": CONVERSATION,
        "_msdyn_liveworkstreamid_value": SCOPE.workstream_id,
        "_msdyn_cdsqueueid_value": SCOPE.queue_id,
        "statuscode@OData.Community.Display.V1.FormattedValue": "Active",
        "createdon": (NOW - timedelta(seconds=30)).isoformat(),
        **overrides,
    }


def event(**overrides):
    return {
        "eventType": "Microsoft.Communication.CallStarted",
        "topic": SCOPE.native_acs_resource_id,
        "eventTime": (NOW - timedelta(seconds=30)).isoformat(),
        "data": {"serverCallId": SERVER_ID, "correlationId": CORRELATION},
        **overrides,
    }


class CorrelationTests(unittest.TestCase):
    def test_exact_native_conversation_and_phone_context(self):
        read = MagicMock(side_effect=[call_item(), {"value": [{"msdyn_callsid": CORRELATION}]}])
        self.assertEqual(inspect_conversation(read, CONVERSATION, NOW), CORRELATION)
        self.assertIn(CONVERSATION, read.call_args.args[0])

    def test_wrong_workstream_queue_state_and_old_call_are_refused(self):
        for fields in (
            {"_msdyn_liveworkstreamid_value": CONVERSATION},
            {"_msdyn_cdsqueueid_value": CONVERSATION},
            {"statuscode@OData.Community.Display.V1.FormattedValue": "Closed"},
            {"msdyn_ocliveworkitemid": CORRELATION},
            {"createdon": (NOW - timedelta(minutes=11)).isoformat()},
            {"createdon": (NOW + timedelta(minutes=2)).isoformat()},
        ):
            read = MagicMock(return_value=call_item(**fields))
            with self.subTest(fields=fields), self.assertRaises(probe.ProbeError):
                inspect_conversation(read, CONVERSATION, NOW)
            read.assert_called_once()

    def test_missing_duplicate_or_partial_phone_context_is_refused(self):
        for document in (
            None, {"value": []}, {"value": [{}, {}]}, {"value": [None]},
            {"value": [{"msdyn_callsid": CORRELATION}], "@odata.nextLink": "more"},
            {"value": [{"msdyn_callsid": "not-a-guid"}]},
        ):
            with self.subTest(document=document), self.assertRaises(probe.ProbeError):
                inspect_conversation(MagicMock(side_effect=[call_item(), document]), CONVERSATION, NOW)

    def test_no_time_or_phone_number_guessing(self):
        with self.assertRaises(probe.ProbeError):
            select_native_call([event(data={"serverCallId": SERVER_ID, "correlationId": CONVERSATION})], CORRELATION, NOW)

    def test_exact_match_is_deduplicated(self):
        result = select_native_call([event(), event()], CORRELATION, NOW)
        self.assertEqual(result, {"server_call_id": SERVER_ID, "correlation_id": CORRELATION})

    def test_multiple_server_calls_for_same_correlation_are_refused(self):
        events = [event(), event(data={"serverCallId": "other", "correlationId": CORRELATION})]
        with self.assertRaises(probe.ProbeError):
            select_native_call(events, CORRELATION, NOW)

    def test_ended_call_is_never_selected_regardless_of_event_order(self):
        ended = event(eventType="Microsoft.Communication.CallEnded")
        for events in ([event(), ended], [ended, event()]):
            with self.subTest(events=events), self.assertRaises(probe.ProbeError):
                select_native_call(events, CORRELATION, NOW)

    def test_source_kind_and_freshness_are_validated(self):
        for fields in (
            {"topic": None},
            {"topic": SCOPE.native_acs_resource_id + "-wrong"},
            {"eventType": "Microsoft.Communication.RecordingFileStatusUpdated"},
            {"eventTime": (NOW - timedelta(minutes=11)).isoformat()},
            {"eventTime": (NOW + timedelta(minutes=2)).isoformat()},
            {"data": None},
            {"data": {"correlationId": CORRELATION}},
            {"data": {"serverCallId": SERVER_ID}},
        ):
            with self.subTest(fields=fields), self.assertRaises(probe.ProbeError):
                select_native_call([event(**fields)], CORRELATION, NOW)

    def test_unexpected_native_document_is_not_a_successful_readiness_check(self):
        for item in (None, [], "not an object"):
            with self.subTest(item=item), self.assertRaises(probe.ProbeError):
                inspect_conversation(MagicMock(return_value=item), CONVERSATION, NOW)

    def test_observer_collection(self):
        self.assertEqual(probe.decode_events({"events": [event()]}), [event()])

    def test_pstn_incoming_event_supplies_exact_native_locator(self):
        result = select_native_call(
            [event(eventType="Microsoft.Communication.IncomingCall")], CORRELATION, NOW
        )
        self.assertEqual(result["server_call_id"], SERVER_ID)

    def test_bad_observer_payload_stops_explicitly(self):
        for document in (
            None, {}, [], {"events": "invalid"}, {"events": [None]}, {"events": [event()] * 129},
        ):
            with self.subTest(document=document), self.assertRaises(probe.ProbeError):
                probe.decode_events(document)

    def test_guid_and_timezone_validation(self):
        for value in (None, "not-a-guid", "00000000-0000-0000-0000-000000000000"):
            with self.assertRaises(probe.ProbeError):
                probe.guid(value)
        for value in (None, "bad", "2026-10-17T12:00:00", "0001-01-01T00:00:00+01:00"):
            with self.assertRaises(probe.ProbeError):
                probe.timestamp(value)


class AttachmentTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.target = {"server_call_id": SERVER_ID, "correlation_id": CORRELATION}
        self.connection = SimpleNamespace(
            get_call_properties=AsyncMock(return_value=SimpleNamespace(
                server_call_id=SERVER_ID, call_connection_state=CallConnectionState.CONNECTED,
            )),
            hang_up=AsyncMock(),
        )

        async def participants():
            yield object()
            yield object()

        self.connection.list_participants = participants
        self.automation = SimpleNamespace(
            connect_call=AsyncMock(return_value=SimpleNamespace(call_connection_id="synthetic-probe-connection")),
            get_call_connection=MagicMock(return_value=self.connection),
        )

    async def test_connect_without_media_or_dialing_and_remove_only_own_leg(self):
        result = await attach_and_leave(self.automation, self.target)
        self.assertTrue(result["connected"])
        self.assertTrue(result["own_leg_detached"])
        self.assertEqual(result["telephone_calls_created"], 0)
        self.assertEqual(result["participant_count"], 2)
        kwargs = self.automation.connect_call.call_args.kwargs
        self.assertEqual(kwargs["callback_url"], SCOPE.callback_url)
        self.assertEqual(kwargs["server_call_id"], SERVER_ID)
        self.assertNotIn("media_streaming", kwargs)
        self.assertNotIn("transcription", kwargs)
        self.connection.hang_up.assert_awaited_once_with(is_for_everyone=False)

    async def test_rejected_connection_is_not_retried(self):
        self.automation.connect_call.side_effect = HttpResponseError(
            f"Synthetic rejection {SERVER_ID} +12025550101"
        )
        with self.assertRaises(probe.ProbeError) as captured:
            await attach_and_leave(self.automation, self.target)
        self.assertIn("Synthetic rejection", str(captured.exception))
        self.assertNotIn(SERVER_ID, str(captured.exception))
        self.assertNotIn("+12025550101", str(captured.exception))
        self.automation.connect_call.assert_awaited_once()
        self.connection.hang_up.assert_not_awaited()

    async def test_missing_locator_while_connecting_waits_for_verified_connection(self):
        self.connection.get_call_properties.side_effect = [
            SimpleNamespace(server_call_id=None, call_connection_state=CallConnectionState.CONNECTING),
            SimpleNamespace(server_call_id=SERVER_ID, call_connection_state=CallConnectionState.CONNECTED),
        ]
        with patch.object(probe.asyncio, "sleep", new_callable=AsyncMock):
            result = await attach_and_leave(self.automation, self.target)
        self.assertTrue(result["connected"])
        self.connection.hang_up.assert_awaited_once_with(is_for_everyone=False)

    async def test_connected_without_locator_is_not_accepted(self):
        self.connection.get_call_properties.return_value.server_call_id = None
        with self.assertRaises(probe.ProbeError):
            await attach_and_leave(self.automation, self.target)
        self.connection.hang_up.assert_awaited_once_with(is_for_everyone=False)

    async def test_unknown_connection_outcome_never_guesses_cleanup_id(self):
        self.automation.connect_call.side_effect = TimeoutError
        with self.assertRaises(probe.ProbeError):
            await attach_and_leave(self.automation, self.target)
        self.automation.get_call_connection.assert_not_called()

    async def test_own_leg_is_removed_on_state_or_locator_error(self):
        for properties in (
            SimpleNamespace(server_call_id="wrong", call_connection_state=CallConnectionState.CONNECTED),
            SimpleNamespace(server_call_id=SERVER_ID, call_connection_state=CallConnectionState.DISCONNECTED),
        ):
            self.connection.get_call_properties.return_value = properties
            self.connection.hang_up.reset_mock()
            with self.subTest(properties=properties), self.assertRaises(probe.ProbeError):
                await attach_and_leave(self.automation, self.target)
            self.connection.hang_up.assert_awaited_once_with(is_for_everyone=False)

    async def test_polling_is_bounded_and_never_dials_again(self):
        self.connection.get_call_properties.return_value.call_connection_state = CallConnectionState.CONNECTING
        with patch.object(probe.asyncio, "sleep", new_callable=AsyncMock) as sleep:
            with self.assertRaises(probe.ProbeError):
                await attach_and_leave(self.automation, self.target)
        self.assertEqual(self.connection.get_call_properties.await_count, 6)
        self.assertEqual(sleep.await_count, 5)
        self.automation.connect_call.assert_awaited_once()
        self.connection.hang_up.assert_awaited_once_with(is_for_everyone=False)

    async def test_cleanup_failure_is_not_success(self):
        self.connection.hang_up.side_effect = HttpResponseError("Synthetic detach failure")
        with self.assertRaisesRegex(probe.ProbeError, "End the test call now"):
            await attach_and_leave(self.automation, self.target)

    async def test_cancellation_still_detaches_own_leg(self):
        self.connection.get_call_properties.side_effect = asyncio.CancelledError
        with self.assertRaises(asyncio.CancelledError):
            await attach_and_leave(self.automation, self.target)
        self.connection.hang_up.assert_awaited_once_with(is_for_everyone=False)


class SdkWireContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_public_sdk_connect_serializes_a_real_request(self):
        transport = create_autospec(AsyncHttpTransport, instance=True)
        response = MagicMock(spec=AsyncHttpResponse)
        response.status_code = 200
        response.headers = {"content-type": "application/json"}
        response.content_type = "application/json"
        response.text.return_value = json.dumps({
            "callConnectionId": CONVERSATION,
            "serverCallId": SERVER_ID,
            "callConnectionState": "connected",
            "correlationId": CORRELATION,
            "targets": [],
        })
        response.body.return_value = response.text.return_value.encode()
        transport.send.return_value = response
        key = base64.b64encode(b"synthetic-test-key").decode()
        connection_string = f"endpoint=https://native.example;accesskey={key}"
        async with CallAutomationClient.from_connection_string(
            connection_string, retry_total=0, transport=transport
        ) as client:
            result = await client.connect_call(
                server_call_id=SERVER_ID, callback_url="https://callback.example/events",
                operation_context="synthetic-wire-contract",
            )
            await client.connect_call(
                server_call_id=SERVER_ID, callback_url="https://callback.example/events",
                media_streaming=MediaStreamingOptions(
                    transport_url="wss://media.example/native?token=synthetic-capability",
                    transport_type=StreamingTransportType.WEBSOCKET,
                    content_type=MediaStreamingContentType.AUDIO,
                    audio_channel_type=MediaStreamingAudioChannelType.UNMIXED,
                    audio_format=AudioFormat.PCM24_K_MONO,
                    enable_bidirectional=True, start_media_streaming=False,
                ),
            )
            response.status_code = 202
            response.text.return_value = ""
            response.body.return_value = b""
            await client.get_call_connection(CONVERSATION).start_media_streaming(operation_context="verified-call")
        self.assertEqual(result.call_connection_id, CONVERSATION)
        self.assertEqual(result.server_call_id, SERVER_ID)
        self.assertEqual(transport.send.await_count, 3)
        request = transport.send.call_args_list[0].args[0]
        body = json.loads(request.body)
        self.assertEqual(urlsplit(request.url).path, "/calling/callConnections:connect")
        self.assertEqual(body["callLocator"]["serverCallId"], SERVER_ID)
        self.assertEqual(body["callbackUri"], "https://callback.example/events")
        self.assertNotIn("mediaStreamingOptions", body)
        media_request = transport.send.call_args_list[1].args[0]
        media = json.loads(media_request.body)
        self.assertNotIn("transcriptionOptions", media)
        self.assertEqual(media["mediaStreamingOptions"], {
            "transportUrl": "wss://media.example/native?token=synthetic-capability",
            "transportType": "websocket", "contentType": "audio", "audioChannelType": "unmixed",
            "startMediaStreaming": False, "enableBidirectional": True, "audioFormat": "pcm24KMono",
        })
        start = transport.send.call_args_list[2].args[0]
        self.assertEqual(urlsplit(start.url).path, f"/calling/callConnections/{CONVERSATION}:startMediaStreaming")
        self.assertEqual(json.loads(start.body)["operationContext"], "verified-call")


if __name__ == "__main__":
    unittest.main()
