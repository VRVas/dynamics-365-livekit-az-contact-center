import asyncio
import hashlib
import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ.setdefault("FOUNDRY_PROJECT_ENDPOINT", "https://unit-test.services.ai.azure.com/api/projects/unit-test")
os.environ.setdefault("ASSIST_ENABLED", "false")

import httpx
from fastapi.testclient import TestClient
import app
import case_tools
import prompts
from handover import HandoverHub
from runtime_fixtures import enabled_deployment


def dynamics_case():
    return {
        "success": True, "source": "dynamics", "reference": "CAS-TEST-0001",
        "caseId": "CASE_ID", "title": "Service request", "status": "Active",
        "stateCode": 0, "statusCode": 1, "priorityCode": 2, "category": "general",
        "description": "ISSUE_DETAILS",
        "location": {"district": "DISTRICT", "street": "STREET", "landmark": ""},
        "createdAt": "2026-01-01T00:00:00Z", "updatedAt": "2026-01-02T00:00:00Z",
        "externalSubmission": False, "instruction": "Do not claim dispatch or external submission.",
    }


def dynamics_context(marker="A", *, matched=True):
    return {
        "success": True, "source": "dynamics", "identityVerified": False,
        "dataScope": "synthetic_profiles_only",
        "customer": {
            "id": f"CRM_ID_{marker}", "name": f"CRM_NAME_{marker}",
            "firstName": f"CRM_FIRST_NAME_{marker}", "email": f"CRM_EMAIL_{marker}",
            "phone": f"CRM_PHONE_{marker}", "context": f"CRM_NOTES_{marker}",
            "account": {
                "accountid": f"CRM_ACCOUNT_ID_{marker}", "name": f"CRM_ACCOUNT_NAME_{marker}",
                "description": f"CRM_ACCOUNT_DESCRIPTION_{marker}",
            },
        } if matched else None,
        "cases": [dynamics_case()] if matched else [], "casesTruncated": False,
    }


class CaseServiceClientTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.enterContext(patch.object(case_tools, "DEPLOYMENT", enabled_deployment()))
        self.enterContext(patch.dict(os.environ, {"ASSIST_BASE_URL": "https://assist.example", "ASSIST_INGEST_KEY": "unit-test"}))
        self.requests = []
        self.response_status = 200
        self.response_json = dynamics_case()

        async def handler(request):
            self.requests.append(json.loads(request.content))
            return httpx.Response(self.response_status, json=self.response_json)

        case_tools._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        self.addAsyncCleanup(case_tools.close_client)

    def bind_conversation(self, profile_id="SYNTHETIC_ACTOR", conversation_id="test-conversation"):
        context = case_tools.ConversationContext(conversation_id, profile_id, "ar", (), AsyncMock())
        token = case_tools.CONVERSATION.set(context)
        self.addCleanup(case_tools.CONVERSATION.reset, token)
        return context

    async def test_private_operations_require_bound_context(self):
        token = case_tools.CONVERSATION.set(None)
        self.addCleanup(case_tools.CONVERSATION.reset, token)
        for action in ("customer_context", "list_cases", "get_report", "create_report", "add_followup"):
            response = json.loads(await case_tools.invoke(action, {}))
            self.assertFalse(response["success"])
        self.assertEqual(self.requests, [])

    async def test_parallel_conversations_keep_subjects_and_languages_separate(self):
        async def retrieve(subject, locale):
            token = case_tools.CONVERSATION.set(case_tools.ConversationContext(subject, subject, locale, ("private name",), AsyncMock()))
            try:
                await case_tools.invoke("knowledge", {"query": "Service requirements"})
                await case_tools.invoke("get_report", {"reference": "CAS-TEST-0001"})
            finally:
                case_tools.CONVERSATION.reset(token)
        await asyncio.gather(retrieve("+15555550100", "ar"), retrieve("+15555550101", "en"))
        queries = [request for request in self.requests if request["action"] == "knowledge"]
        self.assertEqual({query["locale"] for query in queries}, {"en", "ar"})
        self.assertTrue(all("actorId" not in query for query in queries))
        owners = {request["actorId"] for request in self.requests if request["action"] == "get_report"}
        self.assertEqual(owners, {"profile:" + hashlib.sha256(phone.encode()).hexdigest() for phone in ("+15555550100", "+15555550101")})

    async def test_retrieval_count_is_explicit_and_scoped_to_the_current_turn(self):
        context = case_tools.ConversationContext("count-session", "no_profile", "ar", (), AsyncMock())
        token = case_tools.CONVERSATION.set(context)
        try:
            self.assertEqual(context.metrics.knowledge_retrieval_calls, 0)
            await case_tools.invoke("knowledge", {"query": "Current service update"})
            self.assertEqual(context.metrics.knowledge_retrieval_calls, 1)
            self.assertEqual(case_tools.ConversationContext("another", "no_profile", "en", (), AsyncMock()).metrics.knowledge_retrieval_calls, 0)
        finally:
            case_tools.CONVERSATION.reset(token)

    async def test_customer_and_case_list_tools_share_the_backend_actor(self):
        context = self.bind_conversation()
        self.response_json = dynamics_context()
        self.assertEqual(json.loads(await case_tools.get_customer_context.func()), self.response_json)
        self.assertEqual(context.customer_context, self.response_json)
        self.response_json = {"success": True, "source": "dynamics", "cases": [dynamics_case()], "casesTruncated": True}
        self.assertEqual(json.loads(await case_tools.list_customer_cases.func()), self.response_json)
        expected_actor = "profile:" + hashlib.sha256(context.profile_id.encode()).hexdigest()
        self.assertEqual(self.requests, [
            {"action": "customer_context", "actorId": expected_actor},
            {"action": "list_cases", "actorId": expected_actor},
        ])

    async def test_guest_actor_is_session_scoped_without_contact_creation(self):
        context = self.bind_conversation(profile_id="no_profile")
        self.response_json = dynamics_context(matched=False)
        result = json.loads(await case_tools.get_customer_context.func())
        self.assertIsNone(result["customer"])
        self.assertEqual(result["cases"], [])
        self.assertEqual(self.requests, [{
            "action": "customer_context",
            "actorId": "session:" + hashlib.sha256(context.conversation_id.encode()).hexdigest(),
        }])

    async def test_context_refresh_updates_only_this_conversations_sensitive_terms(self):
        context = self.bind_conversation()
        for marker in ("OLD", "CURRENT"):
            self.response_json = dynamics_context(marker)
            await case_tools.get_customer_context.func()
        await case_tools.invoke("knowledge", {"query": "General service requirements"})
        terms = self.requests[-1]["sensitiveTerms"]
        self.assertIn("CRM_EMAIL_CURRENT", terms)
        self.assertIn("CRM_NAME_CURRENT", terms)
        self.assertIn("CRM_ACCOUNT_NAME_CURRENT", terms)
        self.assertNotIn("CRM_EMAIL_OLD", terms)
        self.assertNotIn("actorId", self.requests[-1])
        other = case_tools.ConversationContext("other", "no_profile", "en", (), AsyncMock())
        self.assertEqual(other.customer_context, {})
        self.assertIsNot(other.customer_context, context.customer_context)

    async def test_customer_context_rejects_non_dynamics_or_unapproved_scope(self):
        context = self.bind_conversation()
        for field, value in (
            ("source", "cosmos"), ("identityVerified", True),
            ("dataScope", "all_customers"), ("cases", None), ("casesTruncated", None),
        ):
            with self.subTest(field=field):
                self.response_json = {**dynamics_context(), field: value}
                result = json.loads(await case_tools.get_customer_context.func())
                self.assertFalse(result["success"])
                self.assertEqual(context.customer_context, {})

    async def test_write_operation_id_and_uncertain_reference_match_shared_contract(self):
        context = self.bind_conversation()
        arguments = {
            "category": "general", "description": "Service issue", "confirmed": True,
            "location": {"district": "DISTRICT", "street": "STREET", "landmark": ""},
        }
        self.response_status = 503
        self.response_json = {"error": {"message": "Dynamics is unavailable."}}
        first = json.loads(await case_tools.invoke("create_report", arguments))
        second = json.loads(await case_tools.invoke("create_report", dict(reversed(list(arguments.items())))))
        operation_id = hashlib.sha256(
            f"{context.conversation_id}:create_report:{json.dumps(arguments, sort_keys=True, ensure_ascii=False)}".encode()
        ).hexdigest()
        actor_id = "profile:" + hashlib.sha256(context.profile_id.encode()).hexdigest()
        expected = "OP-" + hashlib.sha256(f"{actor_id}:{operation_id}".encode()).hexdigest().upper()
        self.assertFalse(first["success"])
        self.assertEqual(first["outcome"], "unknown")
        self.assertIn("Do not say the write failed", first["instruction"])
        self.assertEqual(first["lookup_reference"], expected)
        self.assertEqual(second["lookup_reference"], expected)
        self.assertEqual(self.requests[0]["operationId"], operation_id)
        self.assertEqual(self.requests[0], self.requests[1])
        self.assertNotIn("nativeConversationId", self.requests[0])

    async def test_unconfirmed_operation_lookup_keeps_its_original_reference(self):
        self.bind_conversation()
        reference = "OP-" + "A" * 64
        self.response_status = 503
        self.response_json = {"error": {
            "code": "CRM_WRITE_UNCONFIRMED", "message": "The original write may already exist.",
        }}
        result = json.loads(await case_tools.get_case.func(reference))
        self.assertEqual(result["lookup_reference"], reference)
        self.assertEqual(result["error_code"], "CRM_WRITE_UNCONFIRMED")
        self.assertEqual(result["outcome"], "unknown")
        self.assertIn("Do not say", result["instruction"])
        self.assertEqual([request["action"] for request in self.requests], ["get_report"])

    async def test_confirmation_and_location_validation_remain_in_shared_backend(self):
        self.bind_conversation()
        self.response_status = 400
        self.response_json = {"error": {"message": "Confirmation and a location are required."}}
        result = json.loads(await case_tools.create_case.func(
            category="general", description="Service issue",
            district="", street="", landmark="", confirmed=False,
        ))
        self.assertFalse(result["success"])
        self.assertEqual(len(self.requests), 1)
        self.assertFalse(self.requests[0]["confirmed"])
        self.assertEqual(self.requests[0]["location"], {"district": "", "street": "", "landmark": ""})

    async def test_case_detail_and_followup_preserve_dynamics_fields_without_native_link_input(self):
        self.bind_conversation()
        self.response_json.update({
            "recentActivities": [{"note": "CONFIRMED_NOTE", "createdAt": "2026-01-02T00:00:00Z"}],
            "conversationLink": {"status": "not_requested"},
        })
        result = json.loads(await case_tools.get_case.func(" CAS-TEST-0001 "))
        self.assertEqual(result, self.response_json)
        result = json.loads(await case_tools.add_report_followup.func("CAS-TEST-0001", "CONFIRMED_NOTE", True))
        self.assertEqual(result, self.response_json)
        self.assertEqual(self.requests[1]["note"], "CONFIRMED_NOTE")
        self.assertTrue(self.requests[1]["confirmed"])
        self.assertRegex(self.requests[1]["operationId"], r"^[a-f0-9]{64}$")
        self.assertTrue(all("nativeConversationId" not in request for request in self.requests))

    async def test_large_context_is_rejected_without_caching_or_fallback(self):
        context = self.bind_conversation()
        self.response_json = dynamics_context()
        self.response_json["customer"]["context"] = "x" * case_tools.MAX_RESPONSE_BYTES
        result = json.loads(await case_tools.get_customer_context.func())
        self.assertFalse(result["success"])
        self.assertEqual(context.customer_context, {})
        self.assertLess(len(json.dumps(result)), case_tools.MAX_RESPONSE_BYTES)

    async def test_total_request_deadline_is_enforced(self):
        context = self.bind_conversation()
        await case_tools.close_client()

        async def slow_response(request):
            await asyncio.sleep(1)
            return httpx.Response(200, json=dynamics_context())

        case_tools._client = httpx.AsyncClient(transport=httpx.MockTransport(slow_response))
        with patch.object(case_tools, "REQUEST_TIMEOUT_SECONDS", 0.01):
            result = json.loads(await asyncio.wait_for(case_tools.get_customer_context.func(), timeout=0.5))
        self.assertFalse(result["success"])
        self.assertEqual(context.customer_context, {})

    async def test_string_error_responses_remain_visible(self):
        self.bind_conversation()
        self.response_status = 503
        self.response_json = {"error": "Dynamics is unavailable."}
        result = json.loads(await case_tools.list_customer_cases.func())
        self.assertFalse(result["success"])
        self.assertEqual(result["error"], "Dynamics is unavailable.")


class ConversationRoutingTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.object(app, "DEPLOYMENT", enabled_deployment()))
        self.enterContext(patch.object(case_tools, "DEPLOYMENT", enabled_deployment()))
        self.enterContext(patch.dict(app.PROFILES, {"test-selector": {"display_name": "Test selector", "phone": "+12025550101"}}))
        app._sessions.clear()
        app._profile_ids.clear()
        app._interface_locales.clear()
        app._customer_contexts.clear()
        app._conversation_locks.clear()
        self.old_hub = app._hub
        app._hub = HandoverHub()
        self.addCleanup(setattr, app, "_hub", self.old_hub)
        self.messages = []

        async def run(message, session):
            self.messages.append(message)
            context = case_tools.CONVERSATION.get()
            if context.profile_id == 'no_profile':
                self.assertEqual(context.sensitive_terms, ())
            if "representative" in message:
                await context.handover(context.conversation_id, "Customer requested assistance")
            return SimpleNamespace(text="Your request has been received.")

        self.agent = SimpleNamespace(create_session=lambda: object(), run=run)
        self.enterContext(patch.object(app, "_get_agent", return_value=self.agent))
        self.enterContext(patch.object(app, "ASSIST_ENABLED", False))
        self.client = self.enterContext(TestClient(app.app))

    def test_language_switch_preserves_session_and_updates_preference(self):
        for locale in ("en", "ar"):
            response = self.client.post("/conversation/language-session/stream", json={"message": "Service hours", "profile_id": "no_profile", "locale": locale})
            self.assertEqual(response.status_code, 200)
            self.assertNotIn('["error"', response.text)
        self.assertEqual(len(app._sessions), 1)
        self.assertIn("explicitly changed the UI language: ar", self.messages[-1])
        events = [json.loads(line) for line in response.text.splitlines()]
        metadata = next(body for kind, body in events if kind == "response")
        self.assertEqual(metadata["knowledge_retrieval_calls"], 0)
        self.assertEqual(metadata["embedded_knowledge_sha256"], prompts.EMBEDDED_KNOWLEDGE_SHA256)

    def test_arabic_is_default_even_for_an_english_question(self):
        self.assertIsNone(app.MessageRequest(message="Hello").locale)
        response = self.client.post("/conversation/default-language/stream", json={"message": "How can I get help?"})
        self.assertNotIn('["error"', response.text)
        self.assertIn("Initial interface language: ar", self.messages[-1])
        self.assertIn("Respond in Arabic", self.messages[-1])

    def test_unchanged_ui_does_not_override_an_explicit_conversation_request(self):
        for message in ("Please speak English.", "How can I get help?", "How do I request a review?"):
            response = self.client.post("/conversation/sticky-language/stream", json={
                "message": message, "profile_id": "no_profile", "locale": "ar",
            })
            self.assertNotIn('["error"', response.text)
        self.assertIn("Initial interface language: ar", self.messages[0])
        self.assertEqual(self.messages[1:], ["How can I get help?", "How do I request a review?"])
        self.client.post("/conversation/sticky-language/stream", json={"message": "Next question", "locale": "en"})
        self.assertIn("explicitly changed the UI language: en", self.messages[-1])
        self.client.post("/conversation/sticky-language/stream", json={"message": "Without a UI selection"})
        self.assertEqual(self.messages[-1], "Without a UI selection")

    def test_tool_requested_handover_is_sent_to_the_customer_ui(self):
        response = self.client.post("/conversation/handover-session/stream", json={"message": "Please connect a representative", "profile_id": "no_profile"})
        self.assertIn('["handover_requested"', response.text)
        self.assertEqual(app._hub.status("handover-session")["status"], "requested")

    def test_profile_switch_cannot_change_an_active_case_subject(self):
        self.client.post("/conversation/profile-session/stream", json={"message": "Hello", "profile_id": "no_profile"})
        profile_id = next(key for key in app.PROFILES if key != "no_profile")
        response = self.client.post("/conversation/profile-session/stream", json={"message": "Status", "profile_id": profile_id})
        self.assertIn("Start a new conversation before changing profiles", response.text)


class CoordinatorConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.deployment = enabled_deployment()

    def test_context_and_case_list_bindings_avoid_mandatory_profile_reloads(self):
        import main
        with patch.object(main, "Agent") as agent_type:
            main.build_coordinator(client=object(), deployment=self.deployment)
        agents = {call.kwargs["name"]: call.kwargs for call in agent_type.call_args_list}
        coordinator_tools = agents["service_assistant"]["tools"]
        self.assertIn(case_tools.get_customer_context, coordinator_tools)
        self.assertIn(case_tools.list_customer_cases, coordinator_tools)
        for operation in (case_tools.create_case, case_tools.get_case,
                          case_tools.add_report_followup, case_tools.resolve_service_location):
            self.assertIn(operation, coordinator_tools)
        self.assertEqual(set(agents), {"service_assistant", "service_guidance", "correspondence"})
        self.assertIn("never delegate transactions through another model", agents["service_assistant"]["instructions"])
        self.assertIn(case_tools.get_customer_context, agents["correspondence"]["tools"])
        self.assertEqual(agents["service_guidance"]["tools"], [case_tools.search_knowledge])

    def test_crm_prompt_contract_preserves_scope_receipts_and_freshness(self):
        import main
        rules = prompts.build_rules(self.deployment)
        self.assertIn("Greet the customer naturally", prompts.CUSTOMER_CONTEXT_INSTRUCTIONS)
        self.assertIn("never invent a name", prompts.CUSTOMER_CONTEXT_INSTRUCTIONS)
        self.assertIn("identityVerified is false", prompts.CUSTOMER_CONTEXT_INSTRUCTIONS)
        self.assertIn("casesTruncated", rules)
        self.assertIn("conversationLink", rules)
        self.assertIn("OP-", rules)
        self.assertIn('Never label it "not recorded"', rules)
        self.assertIn("reuse a fresh result within the same turn", rules)

    def test_actual_sdk_constructs_the_case_tool_graph_without_network(self):
        import main
        with patch("socket.socket.connect", side_effect=AssertionError("No network is allowed in this test")):
            coordinator = main.build_coordinator(deployment=self.deployment)
        self.assertEqual(coordinator.name, "service_assistant")

    def test_full_knowledge_is_in_every_customer_agent_system_prompt(self):
        import main
        knowledge = "# Organization service knowledge\nRevision: test-1\n\n## 1. Support\nApproved technical guidance.\n"
        rules = prompts.build_rules(self.deployment, "Approved persona.", knowledge)
        with patch.object(main, "build_rules", return_value=rules), patch.object(main, "Agent") as agent_type:
            main.build_coordinator(client=object(), deployment=self.deployment)
        for call in agent_type.call_args_list:
            instructions = call.kwargs["instructions"]
            self.assertEqual(instructions.count("<embedded_organization_knowledge>"), 1)
            self.assertIn(knowledge, instructions)
            self.assertIn("Runtime safety and authorization", instructions)
            self.assertIn("Default response language: ar", instructions)
            self.assertIn("permission to switch", instructions)
