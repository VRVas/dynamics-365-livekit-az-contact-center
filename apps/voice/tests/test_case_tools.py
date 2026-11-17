import asyncio
import hashlib
import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from backend.tools.case_tools import CaseServiceClient
from backend.tools.tools import CURRENT_SESSION
from backend.acs import AcsCaller
from backend.crm_context import CRMContextError
from backend.humain import HumainConfig
from backend.rtmt import RTMiddleTier
from crm_fixtures import CONVERSATION, REFERENCE, case_receipt, customer_context, unknown_customer_context
from runtime_fixtures import enabled_deployment


class CaseToolsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.enterContext(patch.dict(os.environ, {
            "AGENT_ASSIST_BASE_URL": "https://assist.example",
            "AGENT_ASSIST_API_KEY": "unit-test-key",
        }, clear=True))
        self.client = CaseServiceClient(enabled_deployment())
        self.http = MagicMock(closed=False)
        self.http.close = AsyncMock()
        self.response = SimpleNamespace(status=200, json=AsyncMock(return_value=case_receipt()))
        self.http.post.return_value.__aenter__ = AsyncMock(return_value=self.response)
        self.client._http = self.http
        self.addAsyncCleanup(self.client.close)
        self.session = SimpleNamespace(
            _room_name="conversation-a", customer_phone="+15555550100",
            customer_name="Test Beneficiary", customer_email="beneficiary@example.com", language="ar",
        )
        token = CURRENT_SESSION.set(self.session)
        self.addCleanup(CURRENT_SESSION.reset, token)

    async def test_only_case_and_knowledge_tools_are_registered(self):
        self.assertEqual(set(self.client.tools()), {
            "search_knowledge", "resolve_service_location", "create_case",
            "get_case", "add_report_followup",
            "customer_context", "list_cases",
        })

    async def test_recording_is_session_scoped_and_idempotent(self):
        args = {"category": "general", "description": "Service issue", "district": "Test district",
                "street": "A street", "landmark": "", "confirmed": True}
        await self.client.execute("create_report", args)
        first = self.http.post.call_args.kwargs["json"]
        await self.client.execute("create_report", args)
        second = self.http.post.call_args.kwargs["json"]
        self.assertEqual(first["operationId"], second["operationId"])
        self.assertRegex(first["operationId"], r"^[a-f0-9]{64}$")
        self.assertTrue(first["actorId"].startswith("profile:"))
        self.assertEqual(first["actorId"], "profile:" + hashlib.sha256(self.session.customer_phone.encode()).hexdigest())
        self.assertNotIn(self.session.customer_phone, first["actorId"])
        self.assertEqual(first["location"]["district"], "Test district")
        self.assertFalse(self.http.post.call_args.kwargs["allow_redirects"])

    async def test_knowledge_has_only_query_and_bound_privacy_context(self):
        await self.client.execute("knowledge", {"query": "Service requirements"})
        body = self.http.post.call_args.kwargs["json"]
        self.assertEqual(body["locale"], "ar")
        self.assertNotIn("actorId", body)
        self.assertNotIn("transcript", body)
        self.assertIn(self.session.customer_email, body["sensitiveTerms"])

    async def test_unavailable_and_oversized_responses_are_explicit_failures(self):
        tool = self.client.tools()["get_case"]
        self.response.status = 503
        self.response.json.return_value = {"error": {"message": "No outcome confirmed"}}
        result = json.loads((await tool.target({"reference": REFERENCE})).to_text())
        self.assertFalse(result["success"])
        self.response.status = 200
        self.response.json.return_value = {"success": True, "source": "dynamics", "content": "ن" * 14000}
        result = json.loads((await tool.target({"reference": REFERENCE})).to_text())
        self.assertFalse(result["success"])

    async def test_guest_and_profile_calls_do_not_share_ownership(self):
        await self.client.execute("get_report", {"reference": REFERENCE})
        owner = self.http.post.call_args.kwargs["json"]["actorId"]
        self.session.customer_phone = None
        await self.client.execute("get_report", {"reference": REFERENCE})
        guest = self.http.post.call_args.kwargs["json"]["actorId"]
        self.assertNotEqual(owner, guest)
        self.assertTrue(guest.startswith("session:"))
        self.assertEqual(guest, "session:" + hashlib.sha256(self.session._room_name.encode()).hexdigest())

    async def test_tool_arguments_cannot_override_trusted_actor_or_action(self):
        with self.assertRaises(ValueError):
            await self.client.execute("knowledge", {
                "query": "Services", "action": "get_report", "actorId": "another-profile",
            })
        self.http.post.assert_not_called()

    async def test_unconfigured_service_never_fabricates_a_receipt(self):
        self.client.key = ""
        result = await self.client.tools()["get_case"].target({"reference": REFERENCE})
        self.assertFalse(json.loads(result.to_text())["success"])

    async def test_uncertain_creation_returns_a_lookup_not_a_success_receipt(self):
        self.response.status = 503
        self.response.json.return_value = {"error": {"message": "Outcome unconfirmed"}}
        args = {"category": "general", "description": "Service issue", "district": "Test district",
                "street": "A street", "landmark": "", "confirmed": True}
        result = json.loads((await self.client.tools()["create_case"].target(args)).to_text())
        self.assertFalse(result["success"])
        self.assertEqual(result["outcome"], "unknown")
        self.assertIn("Do not say it was not recorded", result["instruction"])
        self.assertRegex(result["lookup_reference"], r"^OP-[A-F0-9]{64}$")
        owner, operation = self.client.request_identity(self.session, "create_report", args)
        self.assertEqual(result["lookup_reference"], "OP-" + hashlib.sha256(f"{owner}:{operation}".encode()).hexdigest().upper())
        self.assertNotIn("reference", result)
        self.response.status = 200
        self.response.json.return_value = case_receipt()
        await self.client.execute("get_report", {"reference": result["lookup_reference"]})
        self.assertEqual(self.http.post.call_args.kwargs["json"]["reference"], result["lookup_reference"])
        self.assertEqual(self.session.selected_case.case_number, REFERENCE)
        self.assertFalse(self.session.selected_case.case_number.startswith("OP-"))

    async def test_unconfirmed_operation_lookup_preserves_the_original_reference(self):
        reference = "OP-" + "A" * 64
        self.response.status = 503
        self.response.json.return_value = {"error": {
            "code": "CRM_WRITE_UNCONFIRMED", "message": "The original write may already exist.",
        }}
        result = json.loads((await self.client.tools()["get_case"].target({"reference": reference})).to_text())
        self.assertFalse(result["success"])
        self.assertEqual(result["outcome"], "unknown")
        self.assertEqual(result["lookup_reference"], reference)
        self.assertIsNone(self.session.selected_case)

    async def test_crm_tools_accept_only_the_bound_conversation_identity(self):
        for name in ("customer_context", "list_cases"):
            with self.subTest(name=name):
                schema = self.client.tools()[name].schema["parameters"]
                self.assertEqual(schema["properties"], {})
                self.assertEqual(schema["required"], [])
                self.assertFalse(schema["additionalProperties"])
                await self.client.execute(name, {})
                payload = self.http.post.call_args.kwargs["json"]
                self.assertEqual(set(payload), {"action", "actorId"})
                for field in ("actorId", "operationId", "nativeConversationId", "phone"):
                    with self.assertRaises(ValueError):
                        await self.client.execute(name, {field: CONVERSATION})
        for tool in self.client.tools().values():
            self.assertFalse({"actorId", "operationId", "nativeConversationId"} & tool.schema["parameters"]["properties"].keys())

    async def test_native_conversation_id_is_added_only_from_server_session(self):
        create = {"category": "general", "description": "Service issue", "district": "Test district",
                  "street": "Test street", "landmark": "", "confirmed": True}
        operations = (
            ("customer_context", {}), ("list_cases", {}),
            ("create_report", create), ("get_report", {"reference": REFERENCE.lower()}),
            ("add_followup", {"reference": REFERENCE, "note": "Still broken", "confirmed": True}),
        )
        self.session.native_conversation_id = CONVERSATION
        for action, args in operations:
            with self.subTest(action=action):
                await self.client.execute(action, args)
                payload = self.http.post.call_args.kwargs["json"]
                self.assertEqual(payload["nativeConversationId"], CONVERSATION)
                self.assertEqual("operationId" in payload, action in {"create_report", "add_followup"})
                with self.assertRaises(ValueError):
                    await self.client.execute(action, {**args, "nativeConversationId": CONVERSATION})
        for action, args in (
            ("knowledge", {"query": "Service hours"}),
            ("resolve_location", {"district": "Test district", "street": "Test street", "landmark": ""}),
        ):
            await self.client.execute(action, args)
            self.assertNotIn("nativeConversationId", self.http.post.call_args.kwargs["json"])

    async def test_invalid_bound_native_id_and_unbound_crm_calls_fail_without_http(self):
        for native_id in ("not-a-uuid", "00000000-0000-0000-0000-000000000000", 1):
            self.session.native_conversation_id = native_id
            with self.assertRaises(ValueError):
                await self.client.execute("customer_context", {})
        token = CURRENT_SESSION.set(None)
        try:
            for action in ("customer_context", "list_cases"):
                with self.assertRaisesRegex(ValueError, "active voice conversation"):
                    await self.client.execute(action, {})
        finally:
            CURRENT_SESSION.reset(token)
        self.http.post.assert_not_called()

    async def test_non_dynamics_case_result_is_rejected_without_a_fallback(self):
        self.response.json.return_value = {"success": True, "source": "cosmos", "reference": REFERENCE}
        with self.assertRaisesRegex(ValueError, "Dynamics result"):
            await self.client.execute("get_report", {"reference": REFERENCE})

    async def test_mutations_require_literal_true_confirmation(self):
        for confirmed in (False, 1, "true", None):
            with self.subTest(confirmed=confirmed):
                create = {"category": "general", "description": "Service issue", "district": "Test district",
                          "street": "Test street", "landmark": "", "confirmed": confirmed}
                followup = {"reference": REFERENCE, "note": "Still broken", "confirmed": confirmed}
                for action, args in (("create_report", create), ("add_followup", followup)):
                    with self.assertRaisesRegex(ValueError, "Explicit confirmation"):
                        await self.client.execute(action, args)
                result = json.loads((await self.client.tools()["create_case"].target(create)).to_text())
                self.assertFalse(result["success"])
                self.assertIsNone(result["lookup_reference"])
        self.http.post.assert_not_called()

    async def test_invalid_bound_phone_is_an_explicit_error_not_a_guest_or_reconciliation_identity(self):
        self.session.customer_phone = "not-an-e164-phone"
        arguments = {
            "category": "general", "description": "Service issue", "district": "Test district",
            "street": "Test street", "landmark": "", "confirmed": True,
        }
        result = json.loads((await self.client.tools()["create_case"].target(arguments)).to_text())
        self.assertFalse(result["success"])
        self.assertIsNone(result["lookup_reference"])
        self.http.post.assert_not_called()

    async def test_prefetch_restores_context_and_never_caches_between_calls(self):
        first = RTMiddleTier(HumainConfig(authorization="synthetic"))
        second = first.clone_for_session()
        first.customer_phone = "+12025550101"
        second.customer_phone = "+12025550102"
        first.native_conversation_id = CONVERSATION
        self.response.json.side_effect = [
            customer_context(phone=first.customer_phone),
            customer_context(phone=second.customer_phone, name="Another Resident", first_name="Another"),
        ]
        one = await self.client.fetch_customer_context(first)
        two = await self.client.fetch_customer_context(second)
        self.assertEqual(one.customer.phone, first.customer_phone)
        self.assertEqual(two.customer.phone, second.customer_phone)
        self.assertNotIn(one.customer.phone, two.serialized)
        self.assertIs(CURRENT_SESSION.get(), self.session)
        self.assertEqual(self.http.post.call_count, 2)
        first_payload, second_payload = [call.kwargs["json"] for call in self.http.post.call_args_list]
        self.assertNotEqual(first_payload["actorId"], second_payload["actorId"])
        self.assertEqual(first_payload["nativeConversationId"], CONVERSATION)
        self.assertNotIn("nativeConversationId", second_payload)
        self.response.json.side_effect = None
        self.response.json.return_value = unknown_customer_context()
        unknown = await self.client.fetch_customer_context(first)
        self.assertIsNone(unknown.customer)
        self.assertIs(CURRENT_SESSION.get(), self.session)

    async def test_concurrent_prefetch_has_independent_current_session_bindings(self):
        voices = [RTMiddleTier(HumainConfig(authorization="synthetic")) for _ in range(2)]
        for index, voice in enumerate(voices):
            voice.customer_phone = f"+1202555010{index + 1}"
        arrived = []
        both = asyncio.Event()

        async def execute(action, arguments):
            current = CURRENT_SESSION.get()
            arrived.append(current)
            if len(arrived) == 2:
                both.set()
            await both.wait()
            self.assertIs(CURRENT_SESSION.get(), current)
            return json.dumps(customer_context(phone=current.customer_phone))

        with patch.object(self.client, "execute", side_effect=execute) as request:
            async with asyncio.timeout(1):
                results = await asyncio.gather(*(self.client.fetch_customer_context(voice) for voice in voices))
        self.assertEqual(request.await_count, 2)
        self.assertEqual([result.customer.phone for result in results], [voice.customer_phone for voice in voices])
        self.assertIs(CURRENT_SESSION.get(), self.session)

    async def test_prefetch_rejects_wrong_customer_and_service_errors_explicitly(self):
        for value in (
            customer_context(phone="+12025550199"),
            {**unknown_customer_context(), "identityVerified": True},
            {**unknown_customer_context(), "dataScope": "all_contacts"},
            {"success": False, "error": "Service unavailable"},
        ):
            self.response.json.return_value = value
            with self.assertRaises(CRMContextError):
                await self.client.fetch_customer_context(self.session)
            self.assertIs(CURRENT_SESSION.get(), self.session)
        self.response.status = 503
        self.response.json.return_value = {"error": {"message": "Dynamics is unavailable"}}
        with self.assertRaisesRegex(ValueError, "Dynamics is unavailable"):
            await self.client.fetch_customer_context(self.session)
        self.assertIs(CURRENT_SESSION.get(), self.session)

    async def test_link_warnings_and_activities_survive_tools_and_humain_rpc(self):
        voice = RTMiddleTier(HumainConfig(authorization="synthetic"))
        voice.customer_phone = self.session.customer_phone
        voice.native_conversation_id = CONVERSATION
        voice._agents.add("synthetic-agent")
        voice.tools = self.client.tools()
        for status in ("failed", "linked", "not_requested"):
            receipt = {
                **customer_context()["cases"][0],
                "recentActivities": [{"type": "followup", "note": "Synthetic update", "timestamp": "2026-10-02T12:00:00Z"}],
                "conversationLink": {"status": status},
            }
            if status == "failed":
                receipt["conversationLink"].update(error="LINK_FAILED", message="Recording succeeded; the link was not confirmed.")
            self.response.json.return_value = receipt
            for name, arguments in (
                ("get_case", {"reference": REFERENCE}),
                ("create_case", {
                    "category": "general", "description": "Service issue", "district": "Test district",
                    "street": "Test street", "landmark": "", "confirmed": True,
                }),
                ("add_report_followup", {"reference": REFERENCE, "note": "Synthetic update", "confirmed": True}),
            ):
                invocation = SimpleNamespace(
                    caller_identity="synthetic-agent", request_id="synthetic-rpc", response_timeout=1,
                    payload=json.dumps({"tool": name, "arguments": arguments}),
                )
                result = json.loads(await voice._handle_tool_call(invocation))
                self.assertEqual(result["result"], receipt)

    async def test_uncertain_create_lookup_survives_the_humain_rpc_error_envelope(self):
        voice = RTMiddleTier(HumainConfig(authorization="synthetic"))
        voice.customer_phone = self.session.customer_phone
        voice.tools = self.client.tools()
        voice._agents.add("synthetic-agent")
        self.response.json.side_effect = asyncio.TimeoutError
        arguments = {
            "category": "general", "description": "Service issue", "district": "Test district",
            "street": "Test street", "landmark": "", "confirmed": True,
        }
        invocation = SimpleNamespace(
            caller_identity="synthetic-agent", request_id="synthetic-rpc", response_timeout=1,
            payload=json.dumps({"tool": "create_case", "arguments": arguments}),
        )
        result = json.loads(await voice._handle_tool_call(invocation))
        self.assertFalse(result["ok"])
        self.assertRegex(result["lookup_reference"], r"^OP-[A-F0-9]{64}$")
        self.assertNotIn("reference", result)

    async def test_rpc_deadline_keeps_reconciliation_reference_and_cancels_http(self):
        voice = RTMiddleTier(HumainConfig(authorization="synthetic"))
        voice.customer_phone = self.session.customer_phone
        voice.tools = self.client.tools()
        voice._agents.add("synthetic-agent")
        cancelled = asyncio.Event()

        async def never_responds():
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        self.response.json.side_effect = never_responds
        arguments = {
            "category": "general", "description": "Service issue", "district": "Test district",
            "street": "Test street", "landmark": "", "confirmed": True,
        }
        invocation = SimpleNamespace(
            caller_identity="synthetic-agent", request_id="synthetic-rpc", response_timeout=0.01,
            payload=json.dumps({"tool": "create_case", "arguments": arguments}),
        )
        result = json.loads(await voice._handle_tool_call(invocation))
        self.assertFalse(result["ok"])
        self.assertEqual(result["lookup_reference"], self.client.creation_lookup_reference(voice, arguments))
        self.assertTrue(cancelled.is_set())
        self.assertIs(CURRENT_SESSION.get(), self.session)
        self.assertFalse(voice._rpc_tasks)

    async def test_selected_receipt_survives_all_case_actions_and_failed_native_link(self):
        self.session.native_conversation_id = CONVERSATION
        receipt = case_receipt(link_status="failed", link_details={
            "error": "CONVERSATION_NOT_ACTIVE", "httpStatus": 409, "stage": "validate",
            "message": "Synthetic private linking warning.",
        })
        receipt["recentActivities"] = [{"type": "followup", "note": "Synthetic private note."}]
        self.response.json.return_value = receipt
        for action, arguments in (
            ("get_report", {"reference": REFERENCE}),
            ("create_report", {
                "category": "general", "description": "Service issue", "district": "Test district",
                "street": "Test street", "landmark": "", "confirmed": True,
            }),
            ("add_followup", {"reference": REFERENCE, "note": "Synthetic update", "confirmed": True}),
        ):
            with self.subTest(action=action):
                returned = json.loads(await self.client.execute(action, arguments))
                selected = self.session.selected_case
                self.assertEqual(returned, receipt)
                self.assertEqual(json.loads(selected.serialized), receipt)
                self.assertEqual(selected.case_id, receipt["caseId"])
                self.assertEqual(selected.case_number, REFERENCE)
                self.assertEqual(selected.native_conversation_id, CONVERSATION)
                self.assertEqual(selected.conversation_link_status, "failed")
                self.assertEqual(selected.snapshot()["case_link_http_status"], 409)
                self.assertEqual(selected.snapshot()["case_link_stage"], "validate")
                self.assertNotIn("Synthetic private", json.dumps(selected.snapshot()))
                self.assertEqual(self.http.post.call_args.kwargs["json"]["nativeConversationId"], CONVERSATION)

    async def test_link_readback_evidence_and_actual_ticket_casing_are_retained(self):
        receipt = case_receipt(reference=REFERENCE.lower(), link_status="linked", link_details={
            "verifiedAt": "2026-10-29T15:00:00Z", "alreadyLinked": True,
        })
        self.response.json.return_value = receipt
        await self.client.execute("get_report", {"reference": REFERENCE})
        self.assertEqual(self.session.selected_case.case_number, REFERENCE.lower())
        self.assertEqual(json.loads(self.session.selected_case.serialized), receipt)
        snapshot = self.session.selected_case.snapshot()
        self.assertEqual(snapshot["case_link_status"], "linked")
        self.assertEqual(snapshot["case_link_verified_at"], receipt["conversationLink"]["verifiedAt"])
        self.assertTrue(snapshot["case_link_already_linked"])

    async def test_invalid_mismatched_and_non_ticket_receipts_do_not_leave_a_stale_selected_case(self):
        invalid = [
            {key: value for key, value in case_receipt().items() if key != "caseId"},
            case_receipt(case_id="not-a-uuid"),
            case_receipt(case_id="00000000-0000-0000-0000-000000000000"),
            case_receipt(reference="CAS-99999-OTHER"),
            case_receipt(reference=CONVERSATION),
            case_receipt(reference=CONVERSATION.replace("-", "")),
            case_receipt(reference="OP-" + "A" * 64),
            case_receipt(link_status="unknown"),
            {key: value for key, value in case_receipt().items() if key != "conversationLink"},
        ]
        for receipt in invalid:
            self.response.json.return_value = case_receipt()
            await self.client.execute("get_report", {"reference": REFERENCE})
            self.assertIsNotNone(self.session.selected_case)
            self.response.json.return_value = receipt
            with self.subTest(reference=receipt.get("reference")), self.assertRaises(ValueError):
                await self.client.execute("get_report", {"reference": REFERENCE})
            self.assertIsNone(self.session.selected_case)

    async def test_profile_and_case_lists_never_select_a_case_and_clones_clear_receipts(self):
        voice = RTMiddleTier(HumainConfig(authorization="synthetic"))
        voice.customer_phone = "+12025550101"
        token = CURRENT_SESSION.set(voice)
        try:
            context = customer_context()
            context["cases"].append(case_receipt(reference="CAS-02002-B2C3"))
            self.response.json.return_value = context
            await self.client.execute("customer_context", {})
            await self.client.execute("list_cases", {})
            self.assertIsNone(voice.selected_case)
            self.response.json.return_value = case_receipt()
            await self.client.execute("get_report", {"reference": REFERENCE})
            self.assertIsNotNone(voice.selected_case)
            clone = voice.clone_for_session()
            self.assertIsNone(clone.selected_case)
            self.assertEqual(clone._case_selection_revision, 0)
        finally:
            CURRENT_SESSION.reset(token)

    async def test_late_old_case_response_cannot_overwrite_a_newer_selection(self):
        old_started, finish_old = asyncio.Event(), asyncio.Event()
        newer = "CAS-02002-B2C3"

        def post(*args, **kwargs):
            reference = kwargs["json"]["reference"]

            async def response_json():
                if reference == REFERENCE:
                    old_started.set()
                    await finish_old.wait()
                return case_receipt(reference=reference)

            context = MagicMock()
            context.__aenter__ = AsyncMock(return_value=SimpleNamespace(status=200, json=response_json))
            context.__aexit__ = AsyncMock(return_value=False)
            return context

        self.http.post.side_effect = post
        older = asyncio.create_task(self.client.execute("get_report", {"reference": REFERENCE}))
        try:
            await asyncio.wait_for(old_started.wait(), 1)
            await self.client.execute("get_report", {"reference": newer})
            finish_old.set()
            await asyncio.wait_for(older, 1)
        finally:
            older.cancel()
            await asyncio.gather(older, return_exceptions=True)
        self.assertEqual(self.session.selected_case.case_number, newer)

    async def test_changed_native_identity_and_handover_do_not_capture_late_receipts(self):
        self.session.native_conversation_id = CONVERSATION

        async def changed_identity():
            self.session.native_conversation_id = "88888888-8888-4888-8888-888888888888"
            return case_receipt()

        self.response.json.side_effect = changed_identity
        with self.assertRaisesRegex(ValueError, "conversation changed"):
            await self.client.execute("get_report", {"reference": REFERENCE})
        self.assertIsNone(self.session.selected_case)
        self.response.json.side_effect = None
        self.session.transfer_active = True
        self.response.json.return_value = case_receipt()
        await self.client.execute("get_report", {"reference": REFERENCE})
        self.assertIsNone(self.session.selected_case)

    async def test_outbound_calls_keep_transcription_configuration(self):
        client = SimpleNamespace(create_call=AsyncMock(return_value=SimpleNamespace(call_connection_id="call-1")), close=AsyncMock())
        with patch("backend.acs.CallAutomationClient.from_connection_string", return_value=client):
            caller = AcsCaller("+15555550100", "unit-test", "https://voice.example/events",
                               "wss://voice.example/audio", "wss://voice.example/transcription",
                               "https://ai.example.cognitiveservices.azure.com",
                               allowed_outbound_numbers=("+15555550101",))
        self.assertEqual(await caller.initiate_call("+15555550101"), "call-1")
        self.assertIs(client.create_call.call_args.kwargs["transcription"], caller._transcription_cfg)
        await caller.close()
