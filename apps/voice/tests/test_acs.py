import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import create_autospec, patch

from aiohttp.test_utils import TestClient, TestServer
from azure.communication.callautomation import (
    CallParticipant, CommunicationUserIdentifier, PhoneNumberIdentifier,
)
from azure.communication.callautomation.aio import CallAutomationClient, CallConnectionClient
from azure.core.async_paging import AsyncItemPaged
from azure.core.exceptions import HttpResponseError
from azure.core.messaging import CloudEvent

from app import PROFILES, create_app
from backend.acs import AcsCaller, AUTO_CONFERENCE_CONTEXT
from backend.session_registry import SessionRegistry
from runtime_fixtures import enabled_deployment


SOURCE_NUMBER = "+12065550100"
CUSTOMER_NUMBER = "+12065550101"
AUTO_CONFERENCE_NUMBER = "+12025550103"
OPERATOR_NUMBER = "+12065550102"


def call_event(event_type, call_id="conference-test", **data):
    return {
        "specversion": "1.0",
        "source": "https://voice.example/acs",
        "id": f"{call_id}-{event_type}",
        "type": f"Microsoft.Communication.{event_type}",
        "time": "2026-10-10T18:42:00Z",
        "data": {"callConnectionId": call_id, **data},
    }


def participant_pages(*pages):
    async def get_next(continuation_token):
        return int(continuation_token or 0)

    async def extract_data(index):
        continuation_token = str(index + 1) if index + 1 < len(pages) else None
        return continuation_token, pages[index]

    return AsyncItemPaged(get_next, extract_data)


class AcsConferenceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.registry = SessionRegistry()
        self.connection = create_autospec(CallConnectionClient, instance=True)
        self.pages = [[
            CallParticipant(identifier=PhoneNumberIdentifier(CUSTOMER_NUMBER)),
            CallParticipant(identifier=CommunicationUserIdentifier("8:acs:synthetic-conference-test")),
        ]]
        self.connection.list_participants.side_effect = lambda: participant_pages(*self.pages)
        self.automation = create_autospec(CallAutomationClient, instance=True)
        self.automation.get_call_connection.return_value = self.connection
        self.enterContext(patch(
            "backend.acs.CallAutomationClient.from_connection_string",
            return_value=self.automation,
        ))
        self.caller = AcsCaller(
            source_number=SOURCE_NUMBER,
            acs_connection_string="synthetic-conference-test",
            acs_callback_path="https://voice.example/acs/call-events",
            acs_media_streaming_websocket_path="wss://voice.example/realtime-acs",
            auto_conference_enabled=True,
            auto_conference_number=AUTO_CONFERENCE_NUMBER,
            allowed_outbound_numbers=(CUSTOMER_NUMBER, AUTO_CONFERENCE_NUMBER, OPERATOR_NUMBER),
        )
        self.enterContext(patch.dict("os.environ", {
            "HUMAIN_AUTHORIZATION": "synthetic-conference-test",
            "ACS_SOURCE_NUMBER": SOURCE_NUMBER,
            "ACS_CONNECTION_STRING": "synthetic-conference-test",
            "ACS_CALLBACK_PATH": self.caller.acs_callback_path,
            "ACS_MEDIA_STREAMING_WEBSOCKET_PATH": "wss://voice.example/realtime-acs",
            "ACS_AUTO_CONFERENCE_ENABLED": "true", "ACS_AUTO_CONFERENCE_NUMBER": AUTO_CONFERENCE_NUMBER,
        }, clear=True))
        self.enterContext(patch("app.load_dotenv"))
        self.enterContext(patch.dict(PROFILES, {CUSTOMER_NUMBER: {}, AUTO_CONFERENCE_NUMBER: {}}))
        self.enterContext(patch("app.DEPLOYMENT", enabled_deployment(
            crm=False, allowed_numbers=(CUSTOMER_NUMBER, AUTO_CONFERENCE_NUMBER, OPERATOR_NUMBER),
        )))
        self.enterContext(patch("app.AcsCaller", return_value=self.caller))
        self.enterContext(patch("app.get_session_registry", return_value=self.registry))
        self.enterContext(patch(
            "backend.session_registry.get_session_registry", return_value=self.registry,
        ))
        self.http = TestClient(TestServer(await create_app()))
        await self.http.start_server()
        self.addAsyncCleanup(self.http.close)

    async def post_event(self, event, path="/acs/call-events"):
        response = await self.http.post(path, json=[event])
        self.assertEqual(response.status, 200)
        await response.read()

    async def start_call(self, direction, call_id, number=CUSTOMER_NUMBER):
        result = SimpleNamespace(call_connection_id=call_id)
        if direction == "outbound":
            self.automation.create_call.return_value = result
            response = await self.http.post("/call", json={"number": number})
        else:
            self.automation.answer_call.return_value = result
            response = await self.http.post("/acs/incoming-call", json=[{
                "id": f"{call_id}-incoming",
                "eventType": "Microsoft.Communication.IncomingCall",
                "subject": "conference-test",
                "eventTime": "2026-10-10T18:42:00Z",
                "dataVersion": "1.0",
                "data": {
                    "incomingCallContext": f"synthetic-{call_id}",
                    "from": {"phoneNumber": {"value": number}},
                },
            }])
        self.assertEqual(response.status, 200)
        await response.read()
        session = self.registry.get(call_id)
        self.assertIsNotNone(session)
        return session

    async def test_inbound_and_outbound_calls_conference_on_both_callback_routes(self):
        for direction in ("inbound", "outbound"):
            for path in ("/acs/call-events", "/acs"):
                with self.subTest(direction=direction, path=path):
                    self.connection.add_participant.reset_mock()
                    call_id = f"{direction}-{path.rsplit('/', 1)[-1]}"
                    session = await self.start_call(direction, call_id)
                    self.connection.add_participant.assert_not_awaited()

                    await self.post_event(call_event("CallConnected", call_id), path)

                    self.connection.add_participant.assert_awaited_once()
                    arguments = self.connection.add_participant.call_args.kwargs
                    self.assertEqual(
                        arguments["target_participant"].properties["value"], AUTO_CONFERENCE_NUMBER,
                    )
                    self.assertEqual(
                        arguments["source_caller_id_number"].properties["value"], SOURCE_NUMBER,
                    )
                    self.assertEqual(arguments["operation_context"], AUTO_CONFERENCE_CONTEXT)
                    self.automation.get_call_connection.assert_called_with(call_id)
                    self.assertEqual(session.current_call_connection_id, call_id)
                    self.assertFalse(session.transfer_active)
                    self.assertEqual(session.transfer_enabled, direction == "outbound")
                    self.connection.hang_up.assert_not_awaited()
                    self.connection.transfer_call_to_participant.assert_not_awaited()

    async def test_disabled_conference_keeps_both_call_directions_and_routes_working(self):
        self.caller.auto_conference_enabled = False
        for direction in ("inbound", "outbound"):
            for path in ("/acs/call-events", "/acs"):
                with self.subTest(direction=direction, path=path):
                    call_id = f"no-conference-{direction}-{path.rsplit('/', 1)[-1]}"
                    session = await self.start_call(direction, call_id)
                    await self.post_event(call_event("CallConnected", call_id), path)
                    self.assertEqual(session.current_call_connection_id, call_id)
                    self.assertFalse(session.transfer_active)
                    self.assertEqual(session.transfer_enabled, direction == "outbound")
                    self.assertFalse(self.caller._conference_attempts)
                    await self.post_event(call_event("CallDisconnected", call_id), path)
                    self.assertIsNone(self.registry.get(call_id))
        self.connection.list_participants.assert_not_called()
        self.connection.add_participant.assert_not_awaited()
        self.connection.hang_up.assert_not_awaited()
        self.connection.transfer_call_to_participant.assert_not_awaited()

    async def test_app_passes_conference_setting_and_reports_it_in_health(self):
        for setting, enabled in (("true", True), ("FALSE", False)):
            with self.subTest(setting=setting):
                with patch.dict("os.environ", {"ACS_AUTO_CONFERENCE_ENABLED": setting}), patch(
                    "app.AcsCaller", return_value=self.caller,
                ) as factory:
                    client = TestClient(TestServer(await create_app()))
                await client.start_server()
                try:
                    factory.assert_called_once()
                    self.assertEqual(factory.call_args.kwargs["auto_conference_enabled"], enabled)
                    response = await client.get("/health")
                    self.assertEqual(response.status, 200)
                    health = await response.json()
                    self.assertIs(health["automatic_phone_conference_enabled"], enabled)
                finally:
                    await client.close()

    async def test_invalid_conference_setting_is_an_explicit_configuration_error(self):
        with patch.dict("os.environ", {"ACS_AUTO_CONFERENCE_ENABLED": "sometimes"}):
            with self.assertLogs("voicerag", level="ERROR"):
                with self.assertRaisesRegex(ValueError, "ACS_AUTO_CONFERENCE_ENABLED"):
                    await create_app()

    async def test_existing_number_is_not_invited_again_on_both_call_directions_and_routes(self):
        self.pages[0].append(CallParticipant(
            identifier=PhoneNumberIdentifier(AUTO_CONFERENCE_NUMBER),
        ))
        for direction in ("inbound", "outbound"):
            for path in ("/acs/call-events", "/acs"):
                with self.subTest(direction=direction, path=path):
                    self.connection.list_participants.reset_mock()
                    call_id = f"already-present-{direction}-{path.rsplit('/', 1)[-1]}"
                    session = await self.start_call(direction, call_id, AUTO_CONFERENCE_NUMBER)
                    session.current_call_connection_id = None
                    event = call_event("CallConnected", call_id)
                    with self.assertLogs("backend.acs", level="INFO") as logs:
                        await self.post_event(event, path)
                    self.assertIn("already connected", "\n".join(logs.output))
                    self.assertIn("skipping duplicate invite", "\n".join(logs.output))
                    self.assertNotIn("awaiting participant connection", "\n".join(logs.output))

                    await self.post_event(event, "/acs" if path == "/acs/call-events" else "/acs/call-events")
                    self.connection.list_participants.assert_called_once_with()
                    self.connection.add_participant.assert_not_awaited()
                    self.assertEqual(session.current_call_connection_id, call_id)
                    self.assertFalse(session.transfer_active)
                    self.assertEqual(session.transfer_enabled, direction == "outbound")
                    self.connection.hang_up.assert_not_awaited()
                    self.connection.transfer_call_to_participant.assert_not_awaited()

    async def test_existing_number_on_a_later_participant_page_is_not_invited_again(self):
        self.pages.append([CallParticipant(
            identifier=PhoneNumberIdentifier(AUTO_CONFERENCE_NUMBER),
        )])
        with self.assertLogs("backend.acs", level="INFO") as logs:
            await self.post_event(call_event("CallConnected"))
        self.assertIn("already connected", "\n".join(logs.output))
        self.connection.list_participants.assert_called_once_with()
        self.connection.add_participant.assert_not_awaited()

    async def test_absent_number_is_invited_after_scanning_all_participant_pages(self):
        self.pages.append([CallParticipant(identifier=PhoneNumberIdentifier(SOURCE_NUMBER))])
        await self.post_event(call_event("CallConnected"))
        self.connection.list_participants.assert_called_once_with()
        self.connection.add_participant.assert_awaited_once()
        self.assertEqual(
            self.connection.add_participant.call_args.kwargs["target_participant"].raw_id,
            PhoneNumberIdentifier(AUTO_CONFERENCE_NUMBER).raw_id,
        )

    async def test_participant_lookup_failure_is_logged_without_blind_dialing_or_hanging_up(self):
        self.connection.list_participants.side_effect = HttpResponseError("Participant lookup rejected")
        for path in ("/acs/call-events", "/acs"):
            with self.subTest(path=path):
                self.connection.list_participants.reset_mock()
                session = await self.start_call("outbound", path)
                event = call_event("CallConnected", path)
                with self.assertLogs("backend.acs", level="ERROR") as logs:
                    await self.post_event(event, path)
                self.assertIn("Participant lookup rejected", "\n".join(logs.output))
                self.assertIn("customer call remains active", "\n".join(logs.output))
                await self.post_event(event, path)
                self.connection.list_participants.assert_called_once_with()
                self.connection.add_participant.assert_not_awaited()
                self.connection.hang_up.assert_not_awaited()
                self.assertEqual(session.current_call_connection_id, path)
                self.assertFalse(session.transfer_active)

    async def test_failure_reading_a_later_participant_page_does_not_assume_number_is_absent(self):
        async def get_next(continuation_token):
            if continuation_token:
                raise HttpResponseError("Next participant page unavailable")
            return self.pages[0]

        async def extract_data(page):
            return "next-page", page

        self.connection.list_participants.side_effect = lambda: AsyncItemPaged(
            get_next, extract_data,
        )
        with self.assertLogs("backend.acs", level="ERROR") as logs:
            await self.post_event(call_event("CallConnected"))
        self.assertIn("Next participant page unavailable", "\n".join(logs.output))
        self.connection.add_participant.assert_not_awaited()
        self.connection.hang_up.assert_not_awaited()

    async def test_fixed_destination_is_not_changed_by_operator_input(self):
        self.automation.create_call.return_value = SimpleNamespace(
            call_connection_id="operator-override-test",
        )
        response = await self.http.post("/call", json={
            "number": CUSTOMER_NUMBER, "operator_number": OPERATOR_NUMBER,
        })
        self.assertEqual(response.status, 200)
        await response.read()
        await self.post_event(call_event("CallConnected", "operator-override-test"))
        arguments = self.connection.add_participant.call_args.kwargs
        self.assertEqual(arguments["target_participant"].properties["value"], AUTO_CONFERENCE_NUMBER)
        self.assertEqual(
            self.registry.get("operator-override-test").operator_transfer_number, OPERATOR_NUMBER,
        )

    async def test_call_connected_before_session_registration_still_dials(self):
        for path in ("/acs/call-events", "/acs"):
            with self.subTest(path=path):
                self.connection.add_participant.reset_mock()
                await self.post_event(call_event("CallConnected", path), path)
                self.connection.add_participant.assert_awaited_once()
                self.assertEqual(self.registry.active_count, 0)

    async def test_duplicate_concurrent_callbacks_dial_once_across_routes(self):
        async def invite(*args, **kwargs):
            await asyncio.sleep(0)

        self.connection.add_participant.side_effect = invite
        event = call_event("CallConnected")
        await asyncio.gather(
            self.post_event(event),
            self.post_event(event, "/acs"),
            self.post_event(event),
        )
        self.connection.add_participant.assert_awaited_once()
        self.connection.list_participants.assert_called_once_with()

    async def test_concurrent_calls_each_invite_the_fixed_participant(self):
        call_ids = ["conference-one", "conference-two", "conference-three"]
        await asyncio.gather(*(
            self.post_event(call_event("CallConnected", call_id)) for call_id in call_ids
        ))
        self.assertEqual(self.connection.add_participant.await_count, len(call_ids))
        self.assertEqual(
            {call.args[0] for call in self.automation.get_call_connection.call_args_list},
            set(call_ids),
        )

    async def test_request_failure_is_logged_without_hanging_up_or_duplicate_redial(self):
        self.connection.add_participant.side_effect = HttpResponseError("PSTN invite rejected")
        event = call_event("CallConnected")
        with self.assertLogs("backend.acs", level="ERROR") as logs:
            await self.post_event(event)
        self.assertIn("customer call remains active", "\n".join(logs.output))
        await self.post_event(event, "/acs")
        self.connection.add_participant.assert_awaited_once()
        self.connection.hang_up.assert_not_awaited()

    async def test_invitation_request_is_not_reported_as_a_connected_participant(self):
        with self.assertLogs("backend.acs", level="INFO") as logs:
            await self.post_event(call_event("CallConnected"))
        self.assertIn("awaiting participant connection", "\n".join(logs.output))
        self.assertNotIn("joined call", "\n".join(logs.output))
        with self.assertLogs("backend.acs", level="INFO") as logs:
            await self.post_event(call_event(
                "AddParticipantSucceeded", operationContext=AUTO_CONFERENCE_CONTEXT,
            ))
        self.assertIn(f"{AUTO_CONFERENCE_NUMBER} joined call", "\n".join(logs.output))
        self.connection.add_participant.assert_awaited_once()

    async def test_no_answer_is_logged_without_ending_the_customer_call(self):
        await self.post_event(call_event("CallConnected"))
        with self.assertLogs("backend.acs", level="ERROR") as logs:
            await self.post_event(call_event(
                "AddParticipantFailed",
                operationContext=AUTO_CONFERENCE_CONTEXT,
                resultInformation={"subCode": 560408, "message": "No answer"},
            ))
        self.assertIn("No answer", "\n".join(logs.output))
        self.assertIn("customer call remains active", "\n".join(logs.output))
        self.connection.hang_up.assert_not_awaited()
        self.connection.add_participant.assert_awaited_once()

    async def test_automatic_invite_failure_does_not_clear_human_handover_state(self):
        session = await self.start_call("outbound", "handover-test")
        session._transferred_calls = {"handover-test"}
        session.transfer_active = True
        with self.assertLogs("backend.acs", level="ERROR"):
            await self.post_event(call_event(
                "AddParticipantFailed", "handover-test",
                operationContext=AUTO_CONFERENCE_CONTEXT,
            ), "/acs")
        self.assertEqual(session._transferred_calls, {"handover-test"})
        self.assertTrue(session.transfer_active)

        await self.post_event(call_event(
            "AddParticipantFailed", "handover-test",
            operationContext="human_handover_warm_add",
        ), "/acs")
        self.assertEqual(session._transferred_calls, set())

    async def test_disconnected_calls_release_conference_tracking_on_both_routes(self):
        for path in ("/acs/call-events", "/acs"):
            with self.subTest(path=path):
                await self.post_event(call_event("CallConnected", path), path)
                await self.post_event(call_event("CallDisconnected", path), path)
                self.assertNotIn(path, self.caller._conference_attempts)

    async def test_non_connection_events_do_not_dial(self):
        await self.post_event(call_event(
            "ParticipantsUpdated", participants=[],
        ))
        await self.post_event(call_event(
            "AddParticipantSucceeded", operationContext="human_handover_warm_add",
        ))
        self.connection.add_participant.assert_not_awaited()

    async def test_missing_connection_id_is_an_explicit_error(self):
        event = CloudEvent.from_dict(call_event("CallConnected", None))
        with self.assertLogs("backend.acs", level="ERROR"):
            with self.assertRaisesRegex(ValueError, "missing a callConnectionId"):
                await self.caller.handle_conference_event(event)
        self.connection.add_participant.assert_not_awaited()

    async def test_unexpected_errors_are_not_silently_treated_as_invite_failures(self):
        self.connection.add_participant.side_effect = RuntimeError("unexpected implementation error")
        with self.assertRaisesRegex(RuntimeError, "unexpected implementation error"):
            await self.caller.handle_conference_event(
                CloudEvent.from_dict(call_event("CallConnected")),
            )


if __name__ == "__main__":
    unittest.main()
