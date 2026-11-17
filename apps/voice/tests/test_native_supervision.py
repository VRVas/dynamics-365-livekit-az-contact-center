import asyncio
import copy
import hashlib
import json
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from backend.dataverse import DataverseClient, DataverseError
from backend.native_media import NativeMediaConfig, NativeMediaError, NativeMediaSession
from backend.native_supervision import (
    CONTRACT, ENTITY, PRIMARY, REQUEST_FIELDS, RESULT_FIELDS, TABLE, NativeCommandConsumer, SupervisionConfig,
)
from backend.native_voip import NativeVoipError, NativeVoipSession
from backend.tools.tools import CURRENT_SESSION, Tool, ToolResult, ToolResultDirection
from azure.communication.identity import CommunicationUserIdentifier
from test_native_media import BOT, CONTROLLER, CONVERSATION, OTHER, WORKSTREAM, Fixture, voice_factory
from test_humain import invocation


COMMAND = "99999999-9999-4999-8999-999999999999"
SOURCE = "supervision_contract: 1\nnative_supervision_hold\nnative_supervision_quiet_end\n"
SETTINGS = SupervisionConfig(True, (OTHER,), ((CONTROLLER, hashlib.sha256(SOURCE.encode()).hexdigest()),))


def command(**values):
    return {
        PRIMARY: COMMAND, "@odata.etag": 'W/"1"', "moacc_conversationid": CONVERSATION,
        "moacc_action": "pause", "_createdby_value": OTHER, "_createdonbehalfby_value": None,
        "overriddencreatedon": None, "createdon": datetime.now(timezone.utc).isoformat(),
        "modifiedon": datetime.now(timezone.utc).isoformat(), "moacc_outcome": None, **values,
    }


class Store:
    def __init__(self, row=None):
        self.row = row or command()
        self.actor = {"systemuserid": OTHER, "isdisabled": False, "applicationid": None, "accessmode": 0, "islicensed": True}
        self.updates = []
        self.get = AsyncMock(side_effect=self.read)

    async def read(self, path, *, params=None):
        if path.startswith("systemusers("):
            return self.actor
        if path.startswith("EntityDefinitions"):
            return {
                "EntitySetName": TABLE, "OwnershipType": "UserOwned",
                "IsOptimisticConcurrencyEnabled": True,
                "Attributes": [
                    {"LogicalName": name, "IsSecured": name in RESULT_FIELDS | REQUEST_FIELDS}
                    for name in RESULT_FIELDS | {PRIMARY, "moacc_action", "moacc_conversationid"}
                ],
            }
        if path.startswith("botcomponents("):
            return {"data": SOURCE, "_parentbotid_value": BOT, "modifiedon": "2026-01-01T00:00:00Z"}
        if path.startswith("bots("):
            return {"publishedon": "2026-01-01T00:00:01Z"}
        if path.startswith(TABLE + "("):
            return copy.deepcopy(self.row)
        if path == TABLE:
            is_completed = params.get("$filter") == "moacc_outcome eq 'paused'"
            return {"value": [copy.deepcopy(self.row)] if is_completed == (self.row["moacc_outcome"] == "paused") else []}
        raise AssertionError(path)

    async def update_native_command(self, identifier, values, etag):
        if etag != self.row["@odata.etag"]:
            raise DataverseError("dynamics_conflict", "Changed", 409)
        assert identifier == COMMAND and set(values).issubset(RESULT_FIELDS)
        self.updates.append(values.copy())
        self.row.update(values)
        self.row["@odata.etag"] = f'W/"{len(self.updates) + 1}"'
        return copy.deepcopy(self.row)


class CommandTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store = Store()
        self.apply = AsyncMock(return_value={"outcome": "paused", "revision": 1, "inflight": 0})
        self.consumer = NativeCommandConsumer(SETTINGS, self.store, self.apply, lambda: {})

    async def test_platform_creator_and_etag_claim_precede_real_ack(self):
        await self.consumer.process(copy.deepcopy(self.store.row))
        self.assertEqual([item["moacc_outcome"] for item in self.store.updates], ["processing", "paused"])
        self.apply.assert_awaited_once()
        self.assertIn("moacc_ackon", self.store.row)
        await self.consumer.process(copy.deepcopy(self.store.row))
        self.apply.assert_awaited_once()

    async def test_unauthorized_imported_expired_wrong_action_and_disabled_or_app_creator(self):
        cases = [
            {"_createdby_value": BOT}, {"_createdonbehalfby_value": OTHER},
            {"overriddencreatedon": "2026-01-01T00:00:00Z"},
            {"createdon": (datetime.now(timezone.utc) - timedelta(seconds=31)).isoformat()},
            {"createdon": (datetime.now(timezone.utc) + timedelta(seconds=60)).isoformat()},
            {"moacc_action": "resume"}, {"moacc_conversationid": "latest"},
            {"createdon": None},
        ]
        for values in cases:
            with self.subTest(values=values):
                self.store.row = command(**values)
                await self.consumer.process(copy.deepcopy(self.store.row))
                self.assertEqual(self.store.row["moacc_outcome"], "rejected")
        for change in ({"isdisabled": True}, {"applicationid": BOT}, {"accessmode": 4}, {"islicensed": False}):
            self.store.row = command()
            before = self.store.actor.copy()
            self.store.actor.update(change)
            await self.consumer.process(copy.deepcopy(self.store.row))
            self.assertEqual(self.store.row["moacc_reason"], "actor_not_interactive")
            self.store.actor = before
        self.apply.assert_not_awaited()

    async def test_old_processing_is_unconfirmed_not_reexecuted(self):
        self.store.row = command(moacc_outcome="processing", modifiedon="2026-01-01T00:00:00Z")
        await self.consumer.process(copy.deepcopy(self.store.row))
        self.assertEqual(self.store.row["moacc_outcome"], "failed")
        self.apply.assert_not_awaited()

    async def test_native_empty_text_normalization_does_not_strand_the_claim(self):
        original = self.store.update_native_command
        async def native_update(identifier, values, etag):
            return await original(identifier, {
                key: None if isinstance(value, str) and value == "" else value
                for key, value in values.items()
            }, etag)
        with patch.object(self.store, "update_native_command", side_effect=native_update):
            await self.consumer.process(copy.deepcopy(self.store.row))
        self.assertEqual(self.store.row["moacc_outcome"], "paused")
        self.assertIsNone(self.store.row["moacc_reason"])
        self.apply.assert_awaited_once()

    async def test_native_partial_patch_representation_is_verified_by_explicit_readback(self):
        original = self.store.update_native_command
        async def partial_update(identifier, values, etag):
            result = await original(identifier, values, etag)
            return {key: value for key, value in result.items() if value is not None}
        with patch.object(self.store, "update_native_command", side_effect=partial_update):
            await self.consumer.process(copy.deepcopy(self.store.row))
        self.assertEqual(self.store.row["moacc_outcome"], "paused")
        self.apply.assert_awaited_once()
        readbacks = [call for call in self.store.get.call_args_list if call.args[0] == f"{TABLE}({COMMAND})"]
        self.assertEqual(len(readbacks), 2)
        self.assertIn("moacc_reason", readbacks[0].kwargs["params"]["$select"])
        self.assertEqual(len(self.store.updates), 2)

    async def test_missing_nullable_outcome_field_is_not_a_confirmed_update(self):
        self.store.update_native_command = AsyncMock(return_value={
            PRIMARY: COMMAND, "@odata.etag": 'W/"2"', "moacc_outcome": "processing",
        })
        with self.assertRaises(DataverseError) as caught:
            await self.consumer._update(copy.deepcopy(self.store.row), {
                "moacc_outcome": "processing", "moacc_reason": "",
            })
        self.assertEqual(caught.exception.code, "supervision_outcome_unconfirmed")

    async def test_competing_etag_claim_does_not_execute_or_overwrite_completion(self):
        row = copy.deepcopy(self.store.row)
        self.store.row["@odata.etag"] = 'W/"20"'
        with self.assertRaises(DataverseError) as caught:
            await self.consumer.process(row)
        self.assertEqual(caught.exception.code, "dynamics_conflict")
        self.apply.assert_not_awaited()

    async def test_idle_sweep_rejects_closed_or_no_ai_and_does_not_leave_requested(self):
        self.apply.return_value = {"outcome": "rejected", "reason": "no_native_ai_session"}
        await self.consumer.sweep()
        self.assertEqual(self.store.row["moacc_outcome"], "rejected")

    async def test_readiness_checks_secured_schema_pin_and_publication(self):
        await self.consumer.verify()
        self.assertTrue(self.consumer.ready)
        original = self.store.read
        async def unsecure(path, **kwargs):
            result = await original(path, **kwargs)
            if path.startswith("EntityDefinitions"):
                result["Attributes"][0]["IsSecured"] = False
                result["Attributes"].append({"LogicalName": "moacc_outcome", "IsSecured": False})
            return result
        self.store.get.side_effect = unsecure
        with self.assertRaises(ValueError):
            await self.consumer.verify()
        self.store.get.side_effect = original
        other = NativeCommandConsumer(replace(SETTINGS, controllers=((CONTROLLER, "0" * 64),)), self.store, self.apply, lambda: {})
        with self.assertRaises(ValueError):
            await other.verify()

    async def test_private_transport_only_allows_outcome_writes_with_etag(self):
        client = DataverseClient("https://example.crm17.dynamics.com")
        client._request = AsyncMock(return_value={})
        for values, tag in (({"moacc_action": "pause"}, 'W/"1"'), ({"moacc_outcome": "paused"}, "*")):
            with self.assertRaises(ValueError):
                await client.update_native_command(COMMAND, values, tag)
        await client.update_native_command(COMMAND, {"moacc_outcome": "processing"}, 'W/"1"')
        self.assertEqual(client._request.call_args.kwargs["etag"], 'W/"1"')

    async def test_disabled_feature_never_queries_or_starts_a_consumer(self):
        disabled = NativeCommandConsumer(SupervisionConfig(), self.store, self.apply, lambda: {})
        await disabled.start()
        await disabled.close()
        self.store.get.assert_not_awaited()
        self.apply.assert_not_awaited()
        self.assertIsNone(disabled.task)


class FenceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.voice = voice_factory()
        self.voice.native_pause_enabled = True
        self.voice._ready = True
        self.voice._source = SimpleNamespace(capture_frame=AsyncMock(), clear_queue=MagicMock())
        self.voice._client_ws = SimpleNamespace(closed=False, send_json=AsyncMock())
        self.voice._agents.add("agent")

    async def test_pause_fences_inflight_capture_and_flushes_partial_input(self):
        import base64
        entered, release = asyncio.Event(), asyncio.Event()
        async def capture(frame):
            entered.set()
            await release.wait()
        self.voice._source.capture_frame.side_effect = capture
        data = {"audio": base64.b64encode(b"\1\0" * 1000).decode()}
        capture_task = asyncio.create_task(self.voice._capture_audio(data))
        await entered.wait()
        sink = AsyncMock()
        pause = asyncio.create_task(self.voice.pause_audio(sink))
        await asyncio.sleep(0)
        self.assertTrue(self.voice.ai_paused)
        release.set()
        await asyncio.gather(capture_task, pause)
        await self.voice._capture_audio(data)
        self.voice._source.capture_frame.assert_awaited_once()
        self.voice._source.clear_queue.assert_called_once()
        self.assertEqual(self.voice._input_buffer, b"")
        sink.assert_awaited_once()

    async def test_output_already_in_sink_finishes_before_pause_ack_and_late_output_drops(self):
        entered, release = asyncio.Event(), asyncio.Event()
        order = []
        async def send(message):
            entered.set()
            await release.wait()
            order.append("earlier_frame")
        self.voice._is_acs = True
        self.voice.native_audio_sink = send
        earlier = asyncio.create_task(self.voice._send_to_client({"type": "response.output_audio.delta", "delta": "AAAA"}))
        await entered.wait()
        async def ack():
            order.append("pause_ack")
        pause = asyncio.create_task(self.voice.pause_audio(ack))
        await asyncio.sleep(0)
        late = asyncio.create_task(self.voice._send_to_client({"type": "response.output_audio.delta", "delta": "AAAA"}))
        release.set()
        await asyncio.gather(earlier, pause, late)
        self.assertEqual(order, ["earlier_frame", "pause_ack"])

    async def test_new_tools_block_but_admitted_write_survives_provider_rpc_cancellation(self):
        entered, release = asyncio.Event(), asyncio.Event()
        outcomes = []
        async def write(arguments):
            entered.set()
            await release.wait()
            outcomes.append("confirmed")
            return ToolResult("confirmed", ToolResultDirection.TO_SERVER)
        self.voice.tools["lookup"] = Tool(
            target=write, schema={"name": "lookup", "description": "test", "parameters": {"type": "object"}},
        )
        call = invocation({"tool": "lookup", "arguments": {}}, timeout=2)
        handler = asyncio.create_task(self.voice._handle_tool_call(call))
        await entered.wait()
        await self.voice.pause_audio(AsyncMock())
        blocked = json.loads(await self.voice._handle_tool_call(call))
        self.assertFalse(blocked["ok"])
        handler.cancel()
        await asyncio.gather(handler, return_exceptions=True)
        self.assertEqual(self.voice.inflight_tools, 1)
        release.set()
        await self.voice.drain_native_tools()
        self.assertEqual(outcomes, ["confirmed"])

    async def test_native_startup_blocks_new_tools_before_language_ack(self):
        target = AsyncMock(return_value=ToolResult("not used", ToolResultDirection.TO_SERVER))
        self.voice.tools["lookup"] = Tool(
            target=target, schema={"name": "lookup", "description": "test", "parameters": {"type": "object"}},
        )
        self.voice.native_startup_pending = True
        result = json.loads(await self.voice._handle_tool_call(invocation({"tool": "lookup", "arguments": {}}, timeout=2)))
        self.assertFalse(result["ok"])
        target.assert_not_awaited()

    async def test_private_pause_requires_correlated_mute_and_empty_queue_ack(self):
        voip = NativeVoipSession(CONVERSATION, OTHER, CommunicationUserIdentifier("synthetic"))
        voip.authorized = True
        voip.media_ready.set()
        voip.ws = SimpleNamespace(closed=False, send_json=AsyncMock(), send_bytes=AsyncMock())
        pending = asyncio.create_task(voip.pause(COMMAND, 1))
        await asyncio.sleep(0)
        for fields in ({"revision": 2}, {"command_id": BOT}, {"muted": False}, {"queued_frames": 1}):
            voip.acknowledge_pause({"command_id": COMMAND, "revision": 1, "muted": True, "queued_frames": 0, **fields})
            self.assertFalse(pending.done())
        voip.acknowledge_pause({"command_id": COMMAND, "revision": 1, "muted": True, "queued_frames": 0})
        await pending
        await voip.send_audio({"type": "response.output_audio.delta", "delta": "AAAA"})
        voip.ws.send_bytes.assert_not_awaited()

    async def test_private_ack_timeout_stays_latched_and_stale_ack_cannot_succeed(self):
        voip = NativeVoipSession(CONVERSATION, OTHER, CommunicationUserIdentifier("synthetic"))
        voip.authorized = True
        voip.media_ready.set()
        voip.ws = SimpleNamespace(closed=False, send_json=AsyncMock(), send_bytes=AsyncMock())
        real_timeout = asyncio.timeout
        with patch("backend.native_voip.asyncio.timeout", side_effect=lambda _: real_timeout(0.01)):
            with self.assertRaises(NativeVoipError):
                await voip.pause(COMMAND, 1)
        self.assertTrue(voip.pause_requested)
        self.assertTrue(voip.pause_ack.cancelled())
        voip.acknowledge_pause({"command_id": COMMAND, "revision": 1, "muted": True, "queued_frames": 0})
        self.assertTrue(voip.pause_ack.cancelled())

    async def test_timed_out_admitted_tool_keeps_reconciliation_warning(self):
        async def unknown(arguments):
            raise TimeoutError
        self.voice.tools["lookup"] = Tool(
            target=unknown, schema={"name": "lookup", "description": "test", "parameters": {"type": "object"}},
        )
        await self.voice._handle_tool_call(invocation({"tool": "lookup", "arguments": {}}))
        await self.voice.drain_native_tools()
        self.assertEqual(self.voice.inflight_tools, 1)


class LifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.f = Fixture()
        self.pilot = self.f.pilot
        self.pilot.config = replace(self.pilot.config, transport="voip", supervision=SETTINGS)
        self.pilot.supervision.ready = True
        self.session = NativeMediaSession(CONVERSATION, "en", supervision_contract=CONTRACT)
        self.session.phase = "active"
        self.session.controller_session_id = CONTROLLER
        self.session.voice = voice_factory()
        self.session.voice.native_pause_enabled = True
        self.session.voice._ready = True
        self.session.voip = SimpleNamespace(pause=AsyncMock(), failed=asyncio.Event())
        self.pilot.sessions[CONVERSATION] = self.session

    async def test_pause_is_separate_idempotent_and_wrong_or_stale_session_is_rejected(self):
        created = datetime.now(timezone.utc)
        self.assertEqual((await self.pilot.pause_command(OTHER, COMMAND, created))["outcome"], "rejected")
        self.assertEqual((await self.pilot.pause_command(CONVERSATION, COMMAND, created - timedelta(minutes=1)))["outcome"], "rejected")
        first = await self.pilot.pause_command(CONVERSATION, COMMAND, created)
        self.assertEqual(first["outcome"], "paused")
        self.assertFalse(self.session.voice.transfer_active)
        self.assertFalse(self.session.release.is_set())
        self.assertEqual(self.session.action, "supervised")
        again = await self.pilot.pause_command(CONVERSATION, OTHER, created)
        self.assertEqual(again["revision"], 1)
        self.session.voip.pause.assert_awaited_once()

    async def test_ack_loss_fails_closed_without_a_success_receipt_or_requeue(self):
        self.session.voip.pause.side_effect = NativeVoipError("lost ack")
        result = await self.pilot.pause_command(CONVERSATION, COMMAND, datetime.now(timezone.utc))
        self.assertEqual(result["outcome"], "failed")
        self.assertTrue(self.session.intervention_latched)
        self.assertTrue(self.session.voice.ai_paused)
        self.assertFalse(self.session.pause_acknowledged)
        self.assertFalse(self.session.release.is_set())

    async def test_wrong_workstream_is_rejected_without_touching_audio(self):
        self.f.item["_msdyn_liveworkstreamid_value"] = OTHER
        result = await self.pilot.pause_command(CONVERSATION, COMMAND, datetime.now(timezone.utc))
        self.assertEqual(result["outcome"], "rejected")
        self.assertFalse(self.session.intervention_latched)
        self.session.voip.pause.assert_not_awaited()

    async def test_deadline_and_provider_loss_release_only_ai_then_quiet_end_on_native_end(self):
        for loss in (False, True):
            with self.subTest(provider_loss=loss):
                self.session.intervention_latched = True
                self.session.detach_attempted = False
                self.session.provider_lost = asyncio.Event()
                if loss:
                    self.session.provider_lost.set()
                async def detach(session):
                    session.detach_attempted = True
                    session.detached = True
                self.pilot._detach = AsyncMock(side_effect=detach)
                closed = {**self.f.item, "statuscode@OData.Community.Display.V1.FormattedValue": "Closed"}
                self.pilot._conversation = AsyncMock(side_effect=[self.f.item, closed])
                async def no_wait(awaitable, **kwargs):
                    awaitable.close()
                    raise TimeoutError
                with patch("backend.native_media.asyncio.wait_for", no_wait):
                    await self.pilot._hold_supervised(
                        self.session, self.f.workstream,
                        asyncio.get_running_loop().time() + (100 if loss else -1),
                    )
                self.pilot._detach.assert_awaited_once()
                self.assertEqual(self.session.reason, "native_call_not_active")

    async def test_feature_rejects_old_controller_and_requires_config(self):
        with self.assertRaises(NativeMediaError):
            self.pilot.start(OTHER, "en", speech_turn_control=True)
        with self.assertRaises(ValueError):
            SupervisionConfig(True)
        with self.assertRaises(ValueError):
            NativeMediaConfig(supervision=SETTINGS)

    async def test_cancelled_language_tool_after_pause_never_requeues(self):
        from backend.native_media import NativeSpeechControl
        self.session.speech_control = NativeSpeechControl("ar")
        self.session.speech_control.acknowledge("ar", 0)
        self.session.voice.transcript_buffer.mark_completed("language-test", "Please speak English.")
        async def cancelled(session):
            session.intervention_latched = True
            raise asyncio.CancelledError
        self.pilot._wait_for_language = cancelled
        token = CURRENT_SESSION.set(self.session.voice)
        try:
            with self.assertRaises(asyncio.CancelledError):
                await self.pilot._select_response_language(self.session, {"language": "en"})
        finally:
            CURRENT_SESSION.reset(token)
        self.assertFalse(self.session.release.is_set())
        self.assertFalse(self.session.voice.transfer_active)

    async def test_supervised_control_failure_finishes_quietly_without_transfer(self):
        self.session.intervention_latched = True
        self.pilot.supervision.verify = AsyncMock()
        self.pilot._resolve_pending_context = AsyncMock(return_value=(self.f.workstream, "+12025550101"))
        self.pilot._connect = AsyncMock()
        async def fail_context(session, workstream, deadline):
            session.reason = "supervision_context_unconfirmed"
        async def detach(session):
            session.detach_attempted = True
            session.detached = True
        self.pilot._hold_supervised = fail_context
        self.pilot._detach = AsyncMock(side_effect=detach)
        await self.pilot._run(self.session)
        self.assertEqual(self.session.action, "quiet_hold")
        self.assertTrue(self.session.done.is_set())
        self.assertNotEqual(self.session.action, "handover")

    async def test_irreversible_handover_wins_over_later_pause(self):
        self.session.phase = "releasing"
        self.session.release.set()
        result = await self.pilot.pause_command(CONVERSATION, COMMAND, datetime.now(timezone.utc))
        self.assertEqual(result["outcome"], "rejected")
        self.session.voip.pause.assert_not_awaited()

    async def test_deadline_starting_during_context_reads_wins_without_a_late_pause(self):
        async def late_controller(session):
            session.phase = "releasing"
            session.release.set()
        self.pilot._check_controller = late_controller
        result = await self.pilot.pause_command(CONVERSATION, COMMAND, datetime.now(timezone.utc))
        self.assertEqual(result["outcome"], "rejected")
        self.assertFalse(self.session.intervention_latched)
        self.session.voip.pause.assert_not_awaited()
