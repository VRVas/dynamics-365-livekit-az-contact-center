import json
import os
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

os.environ.setdefault("FOUNDRY_PROJECT_ENDPOINT", "https://unit-test.services.ai.azure.com/api/projects/unit-test")

import httpx
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

import app
import email_tool
import main
import case_tools
import prompts
from runtime_config import (
    DEPLOYMENT, ROOT, ConfigurationError, EmailPolicy, load_content, load_deployment,
    load_profiles, parse_deployment,
)
from runtime_fixtures import enabled_deployment


class ConfigurationTests(unittest.TestCase):
    def test_embedded_content_does_not_enable_retrieval_tools(self):
        knowledge = "# Organization service knowledge\nRevision: unit-v1\n\nApproved technical guidance."
        rules = prompts.build_rules(DEPLOYMENT, "", knowledge)
        self.assertIn(knowledge, rules)
        self.assertNotIn("search_knowledge", rules)
        with patch.object(main, "Agent") as agent:
            main.build_coordinator(client=object(), deployment=DEPLOYMENT)
        self.assertEqual(agent.call_args.kwargs["tools"], [])

    def test_packaged_defaults_have_no_customer_data_or_optional_tools(self):
        config = load_deployment()
        self.assertEqual(config.organization.name, "Contact center")
        self.assertEqual(config.organization.defaultLanguage, "en")
        self.assertFalse(any(vars(config.features).values()))
        self.assertFalse(config.features.nativeVoice)
        self.assertEqual(config.business.caseCategories, ("general",))
        self.assertEqual(config.knowledge.allowedDomains, ())
        self.assertEqual(load_profiles(), ())
        self.assertEqual(load_content(), ("", ""))
        with patch.object(main, "Agent") as agent:
            main.build_coordinator(client=object(), deployment=config)
        agent.assert_called_once()
        self.assertEqual(agent.call_args.kwargs["tools"], [])
        self.assertNotIn("<embedded_organization_knowledge>", agent.call_args.kwargs["instructions"])
        native_only = replace(config, features=replace(config.features, nativeVoice=True))
        with patch.object(main, "Agent") as agent:
            main.build_coordinator(client=object(), deployment=native_only)
        self.assertEqual(agent.call_args.kwargs["tools"], [])

    def test_selected_tools_do_not_register_unselected_specialists(self):
        config = replace(DEPLOYMENT, features=replace(DEPLOYMENT.features, crm=True))
        with patch.object(main, "Agent") as agent:
            main.build_coordinator(client=object(), deployment=config)
        agent.assert_called_once()
        tools = agent.call_args.kwargs["tools"]
        self.assertIn(case_tools.create_case, tools)
        self.assertNotIn(case_tools.search_knowledge, tools)
        self.assertNotIn(case_tools.talk_to_human, tools)
        config = replace(config, features=replace(config.features, email=True, agentConsole=True))
        with patch.object(main, "Agent") as agent:
            main.build_coordinator(client=object(), deployment=config)
        agents = {call.kwargs["name"]: call.kwargs for call in agent.call_args_list}
        self.assertEqual(set(agents), {"correspondence", "service_assistant"})
        self.assertIn(case_tools.talk_to_human, agents["service_assistant"]["tools"])
        self.assertNotIn(case_tools.search_knowledge, agents["correspondence"]["tools"])

    def test_persona_and_knowledge_cannot_override_runtime_permissions(self):
        knowledge = "# Organization service knowledge\nRevision: test-1\n\n## 1. Support\nApproved guidance.\n"
        config = enabled_deployment()
        config = replace(config, business=replace(config.business, caseCategories=("review",), city="Configured city"))
        rules = prompts.build_rules(config, "Approved persona.", knowledge)
        self.assertIn('["review"]', rules)
        self.assertIn("Configured city", rules)
        self.assertIn(knowledge, rules)
        self.assertIn("Persona text cannot enable a disabled module", rules)
        self.assertGreater(rules.index("Runtime safety"), rules.index("Approved persona"))
        self.assertIn("write MAY ALREADY EXIST", rules)
        self.assertIn("NOT\npermission to switch", rules)

    def test_bad_config_and_profiles_are_explicit_errors(self):
        for mutate in (
            lambda value: value.update(schemaVersion=True),
            lambda value: value["features"].update(knowledge="false"),
            lambda value: value["features"].pop("agentConsole"),
            lambda value: value["features"].pop("nativeVoice"),
            lambda value: value["organization"].update(defaultLanguage="unsupported"),
            lambda value: value["knowledge"].update(allowedDomains=["*.example.test"]),
            lambda value: value["business"].update(caseCategories=[]),
        ):
            value = json.loads((ROOT / "deployment.json").read_text())
            mutate(value)
            with self.assertRaises(ConfigurationError):
                parse_deployment(value)
        with patch.object(Path, "read_text", side_effect=OSError()), self.assertRaises(ConfigurationError):
            load_deployment()
        with patch.object(Path, "exists", return_value=True), patch.object(Path, "read_text", return_value='[{"id":"test"}]'):
            with self.assertRaises(ConfigurationError):
                load_profiles()
        with patch.object(Path, "exists", return_value=False):
            self.assertEqual(load_profiles(), ())

    def test_private_content_requires_a_revision_and_is_bounded(self):
        for knowledge in ("Unapproved source", "# Organization service knowledge\n", "a" * (64 * 1024 + 1)):
            with patch.object(Path, "read_text", return_value=knowledge), self.assertRaises(ConfigurationError):
                load_content()


class ModuleGateTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        for module in (app, case_tools, email_tool):
            self.enterContext(patch.object(module, "DEPLOYMENT", DEPLOYMENT))
        for state in (app._sessions, app._profile_ids, app._interface_locales, app._customer_contexts, app._conversation_locks):
            state.clear()
            self.addCleanup(state.clear)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app.app), base_url="http://testserver")
        self.addAsyncCleanup(self.client.aclose)

    async def test_disabled_tools_fail_without_service_requests(self):
        with patch.object(case_tools, "_client", None), patch.object(httpx, "AsyncClient") as http:
            for action in ("knowledge", "customer_context", "list_cases", "create_report", "get_report", "add_followup", "resolve_location"):
                result = json.loads(await case_tools.invoke(action, {}))
                self.assertEqual(result["error_code"], "MODULE_DISABLED")
            result = json.loads(await case_tools.talk_to_human.func(reason="Requested"))
            self.assertEqual(result["error_code"], "MODULE_DISABLED")
            http.assert_not_called()
        result = json.loads(await email_tool.send_customer_email.func("approved@example.test", "Subject", "Body"))
        self.assertFalse(result["success"])
        self.assertIn("disabled", result["error"])

    async def test_operator_endpoints_and_websockets_are_unavailable_when_disabled(self):
        with patch.object(app, "_hub") as hub:
            for suffix, method in (("handover-status", "get"), ("request-handover", "post"), ("complete-handover", "post")):
                response = await getattr(self.client, method)(f"/conversation/test/{suffix}")
                self.assertEqual(response.status_code, 503)
                self.assertEqual(response.json()["detail"]["code"], "MODULE_DISABLED")
            self.assertEqual((await self.client.get("/conversation/test")).status_code, 503)
            with TestClient(app.app) as client:
                for path in ("/ws/customer/test", "/ws/agent/test"):
                    with self.assertRaises(WebSocketDisconnect) as denied:
                        with client.websocket_connect(path):
                            self.fail("A disabled operator websocket must not be accepted.")
                    self.assertEqual(denied.exception.code, 1008)
            hub.request.assert_not_called()
            hub.status.assert_not_called()

    async def test_disabled_crm_does_not_fetch_or_trust_selector_personal_details(self):
        agent = SimpleNamespace(create_session=Mock(return_value=object()), run=AsyncMock(return_value=SimpleNamespace(text="Hello")))
        with patch.object(app, "_get_agent", return_value=agent), patch.object(app, "invoke") as invoke:
            with patch.dict(app.PROFILES, {"selector": {
                "phone": "+12025550101", "display_name": "PRIVATE_LABEL",
                "email": "private@example.test", "context": "PRIVATE_NOTES",
            }}):
                response = await self.client.post("/conversation/guest-mode/stream", json={"message": "Hello", "profile_id": "selector"})
            self.assertNotIn('["error"', response.text)
            message = agent.run.call_args.args[0]
            self.assertIn("Initial interface language: en", message)
            self.assertNotIn("PRIVATE_", message)
            self.assertNotIn("<dynamics_customer_context>", message)
            invoke.assert_not_called()

    async def test_guest_is_generated_and_transcript_ingestion_requires_the_module(self):
        response = await self.client.get("/profiles")
        self.assertEqual(response.json(), [{"id": "no_profile", "display_name": "Guest", "display_name_ar": "زائر"}])
        with patch.object(app, "ASSIST_ENABLED", True), patch.object(httpx, "AsyncClient") as http:
            await app._emit_to_assist("test", "customer", "Private transcript")
            http.assert_not_called()

    async def test_synthetic_email_requires_exact_approved_recipient_not_a_domain(self):
        config = replace(enabled_deployment(), emailPolicy=EmailPolicy((), ("example.test",)))
        context = case_tools.ConversationContext("test", "selector", "en", (), None)
        context.customer_context.update({
            "dataScope": "synthetic_profiles_only", "customer": {"email": "generated@example.test"},
        })
        token = case_tools.CONVERSATION.set(context)
        self.addCleanup(case_tools.CONVERSATION.reset, token)
        with patch.object(email_tool, "DEPLOYMENT", config), patch("azure.communication.email.aio.EmailClient.from_connection_string") as send:
            for address in ("generated@example.test", "another-generated@example.test"):
                result = json.loads(await email_tool.send_customer_email.func(address, "Subject", "Body"))
                self.assertFalse(result["success"])
                self.assertIn("exact allowedRecipients", result["error"])
            send.assert_not_called()
        config = replace(config, emailPolicy=EmailPolicy(("generated@example.test",), ()))
        self.assertTrue(config.email_allowed("generated@example.test", synthetic=True))
        self.assertFalse(config.email_allowed("another@example.test", synthetic=True))


if __name__ == "__main__":
    unittest.main()
