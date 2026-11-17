import json
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import app
from backend.humain import HumainConfig
from backend.native_media import NativeMediaPilot
from backend.tools.case_tools import CaseServiceClient
from backend.tools.send_customer_email import _send_customer_email_tool
from backend.tools.tools import CURRENT_SESSION
from runtime_config import (
    DEPLOYMENT, ROOT, ConfigurationError, EmailPolicy, PhonePolicy, load_content, load_deployment,
    load_profiles, parse_deployment,
)
from runtime_fixtures import enabled_deployment
from runtime_rules import build_system_prompt


class ConfigurationTests(unittest.TestCase):
    def test_embedded_facts_do_not_require_the_foundry_retrieval_module(self):
        knowledge = "# Organization service knowledge\nRevision: unit-v1\n\nApproved technical guidance."
        prompt = build_system_prompt(DEPLOYMENT, "", knowledge)
        self.assertIn(knowledge, prompt)
        self.assertNotIn("search_knowledge", prompt)
        self.assertEqual(CaseServiceClient(DEPLOYMENT).tools(), {})

    def test_neutral_defaults_have_no_enabled_modules_or_customer_content(self):
        config = load_deployment()
        self.assertEqual(config.organization.name, "Contact center")
        self.assertEqual(config.organization.defaultLanguage, "en")
        self.assertEqual(config.organization.timeZone, "UTC")
        self.assertFalse(any(vars(config.features).values()))
        self.assertFalse(config.features.nativeVoice)
        self.assertEqual(config.business.caseCategories, ("general",))
        self.assertFalse(config.business.locationRequired)
        self.assertEqual(config.business.city, "")
        self.assertEqual(config.knowledge.allowedDomains, ())
        self.assertEqual(load_profiles(), ())
        self.assertEqual(load_content(), ("", ""))
        prompt = build_system_prompt(config, "", "")
        for unavailable in ("search_knowledge", "create_case", "send_customer_email"):
            self.assertNotIn(unavailable, prompt)
        self.assertEqual(CaseServiceClient(config).tools(), {})
        self.assertEqual(HumainConfig(authorization="synthetic").turn_taking_strategy, "fast_vad")
        self.assertEqual(HumainConfig(authorization="synthetic").interruption_policy_mode, "off")

    def test_injected_config_drives_categories_language_and_additive_persona(self):
        config = enabled_deployment(language="ar")
        config = replace(config, business=replace(config.business, caseCategories=("review",), city="Configured city"))
        knowledge = "# Organization service knowledge\nRevision: test-1\n\n## 1. Guidance\nApproved information.\n"
        prompt = build_system_prompt(config, "Approved persona instructions.", knowledge)
        self.assertIn("Default response language: ar", prompt)
        self.assertIn("Configured city", prompt)
        self.assertIn('["review"]', prompt)
        self.assertIn(knowledge, prompt)
        self.assertGreater(prompt.index("Runtime safety and authorization"), prompt.index("Approved persona"))
        self.assertIn("write MAY ALREADY EXIST", prompt)
        self.assertIn("not verified", prompt)
        client = CaseServiceClient(config)
        self.assertEqual(client.tools()["create_case"].schema["parameters"]["properties"]["category"]["enum"], ["review"])
        knowledge_only = replace(config, features=replace(DEPLOYMENT.features, knowledge=True))
        self.assertEqual(set(CaseServiceClient(knowledge_only).tools()), {"search_knowledge"})
        crm_only = replace(config, features=replace(DEPLOYMENT.features, crm=True))
        self.assertNotIn("search_knowledge", CaseServiceClient(crm_only).tools())

    def test_invalid_configuration_never_falls_back_to_defaults(self):
        for mutate in (
            lambda value: value.update(schemaVersion=2),
            lambda value: value["features"].update(crm="false"),
            lambda value: value["features"].pop("browserVoice"),
            lambda value: value["features"].pop("nativeVoice"),
            lambda value: value["organization"].update(timeZone="Invalid/Zone"),
            lambda value: value["business"].update(caseCategories=[]),
            lambda value: value["knowledge"].update(allowedDomains=["https://docs.example.test"]),
            lambda value: value["emailPolicy"].update(allowedRecipients=["invalid"]),
            lambda value: value.update(customerRecords=[]),
        ):
            value = json.loads((ROOT / "deployment.json").read_text())
            mutate(value)
            with self.assertRaises(ConfigurationError):
                parse_deployment(value)
        with patch.object(Path, "read_text", return_value="{invalid"), self.assertRaises(ConfigurationError):
            load_deployment()

    def test_private_profiles_are_validated_without_generation_or_identity_claims(self):
        profile = {
            "id": "test-contact", "display_name": "Test contact", "display_name_ar": "",
            "phone": "+12025550101", "email": "approved@example.test", "context": "Selection hint only",
        }
        with patch.object(Path, "exists", return_value=True), patch.object(Path, "read_text", return_value=json.dumps([profile])):
            loaded = load_profiles()
            self.assertEqual(len(loaded), 1)
            self.assertEqual(loaded[0].phone, profile["phone"])
        with patch.object(Path, "exists", return_value=False):
            self.assertEqual(load_profiles(), ())
        for values in ([profile, profile], [{**profile, "id": "no_profile"}], [{**profile, "phone": "not-a-phone"}], {}):
            with patch.object(Path, "exists", return_value=True), patch.object(Path, "read_text", return_value=json.dumps(values)):
                with self.assertRaises(ConfigurationError):
                    load_profiles()

    def test_knowledge_requires_revision_and_budget_but_may_be_empty(self):
        approved = "# Organization service knowledge\nRevision: test-2\n\n## 1. Topic\nEvidence.\n"
        for knowledge, valid in (("", True), (approved, True), ("Unapproved heading", False),
                                 ("# Organization service knowledge\n", False), (" " * (64 * 1024 + 1), False)):
            def read(path, **_kwargs):
                return "Private persona." if path.name == "system_prompt.md" else knowledge
            with patch.object(Path, "read_text", autospec=True, side_effect=read):
                if valid:
                    self.assertEqual(load_content(), ("Private persona.", knowledge))
                else:
                    with self.assertRaises(ConfigurationError):
                        load_content()

    def test_email_policy_is_deny_by_default_and_exact_for_synthetic_records(self):
        enabled = replace(DEPLOYMENT, features=replace(DEPLOYMENT.features, email=True))
        self.assertFalse(enabled.email_allowed("generated@example.test"))
        policy = EmailPolicy(("approved@example.test",), ("example.test",))
        enabled = replace(enabled, emailPolicy=policy)
        self.assertTrue(enabled.email_allowed("approved@example.test", synthetic=True))
        self.assertFalse(enabled.email_allowed("generated@example.test", synthetic=True))
        self.assertTrue(enabled.email_allowed("requested@example.test"))
        self.assertFalse(enabled.email_allowed("requested@evil.example.test"))
        self.assertFalse(enabled.email_allowed("requested@example.test.evil.test"))


class ModuleGateTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.enterContext(patch.dict("os.environ", {"HUMAIN_ENABLED": "false"}, clear=True))
        self.enterContext(patch("app.load_dotenv"))
        self.native = self.enterContext(patch("app.NativeMediaPilot", wraps=NativeMediaPilot))

    async def start(self, deployment=DEPLOYMENT):
        client = TestClient(TestServer(await app.create_app(deployment)))
        await client.start_server()
        self.addAsyncCleanup(client.close)
        return client

    async def phone_app(self, numbers=()):
        self.enterContext(patch.dict("os.environ", {
            "HUMAIN_ENABLED": "true", "HUMAIN_AUTHORIZATION": "synthetic",
            "ACS_SOURCE_NUMBER": "+12025550102", "ACS_CONNECTION_STRING": "synthetic",
            "ACS_CALLBACK_PATH": "https://voice.example/acs", "ACS_MEDIA_STREAMING_WEBSOCKET_PATH": "wss://voice.example/realtime-acs",
        }))
        caller = SimpleNamespace(
            source_number="+12025550102", initiate_call=AsyncMock(return_value="approved-phone-test"),
            handle_incoming_call=AsyncMock(return_value="incoming-phone-test"),
            close=AsyncMock(), outbound_call_handler=AsyncMock(return_value=web.Response()),
        )
        self.enterContext(patch("app.AcsCaller", return_value=caller))
        self.enterContext(patch.dict(app.PROFILES, {}, clear=True))
        config = replace(DEPLOYMENT, features=replace(DEPLOYMENT.features, applicationTelephony=True),
                         phonePolicy=PhonePolicy(tuple(numbers)))
        return await self.start(config), caller

    async def test_native_only_session_has_handover_without_an_application_number(self):
        config = replace(DEPLOYMENT, features=replace(DEPLOYMENT.features, nativeVoice=True))
        with patch("app.AcsCaller") as caller:
            await self.start(config)
        caller.assert_not_called()
        session = self.native.call_args.args[3]("+12025550101", "en")
        self.assertIn("talk_to_human", session.tools)
        self.assertIsNone(session.acs_caller)
        self.assertIsNone(self.native.call_args.kwargs["customer_context_provider"])

    async def test_approved_outbound_needs_no_crm_or_profile(self):
        client, caller = await self.phone_app(("+12025550101",))
        with patch("app.CaseServiceClient.fetch_customer_context", new=AsyncMock()) as crm:
            response = await client.post("/call", json={"number": "+12025550101"})
        self.assertEqual(response.status, 200)
        caller.initiate_call.assert_awaited_once()
        crm.assert_not_awaited()
        self.assertNotIn("phonePolicy", await (await client.get("/config")).json())

    async def test_unapproved_operator_prevents_the_outbound_request(self):
        client, caller = await self.phone_app(("+12025550101",))
        response = await client.post("/call", json={"number": "+12025550101", "operator_number": "+12025550103"})
        self.assertEqual(response.status, 403)
        caller.initiate_call.assert_not_awaited()

    async def test_inbound_without_profiles_crm_or_dialing_policy_still_answers(self):
        client, caller = await self.phone_app()
        response = await client.post("/acs/incoming-call", json=[{
            "id": "technical-inbound", "eventType": "Microsoft.Communication.IncomingCall",
            "subject": "technical-test", "eventTime": "2026-01-01T00:00:00Z", "dataVersion": "1.0",
            "data": {"incomingCallContext": "synthetic-context", "from": {"phoneNumber": {"value": "+12025550101"}}},
        }])
        self.assertEqual(response.status, 200)
        caller.handle_incoming_call.assert_awaited_once()
        caller.initiate_call.assert_not_awaited()

    async def test_disabled_entries_fail_before_any_provider_or_transcript_ingestion(self):
        client = await self.start()
        with patch("backend.rtmt.mint_token") as mint:
            for path, method in (("/call", "post"), ("/realtime", "get"), ("/realtime-acs", "get"), ("/transcription-acs", "get")):
                response = await getattr(client, method)(path)
                self.assertEqual(response.status, 503)
                self.assertEqual((await response.json())["error"]["code"], "MODULE_DISABLED")
            mint.assert_not_called()
        health = await (await client.get("/health")).json()
        self.assertEqual(health["tools"], [])
        self.assertFalse(health["acs_configured"])
        self.assertFalse(health["assist_configured"])
        public = await (await client.get("/config")).json()
        self.assertEqual(set(public), {"organization", "features"})
        self.assertNotIn("emailPolicy", public)
        self.assertIsNone(self.native.call_args.kwargs["customer_context_provider"])
        native_session = self.native.call_args.args[3]("+12025550101", "en")
        self.assertEqual(set(native_session.tools), {"talk_to_human"})
        self.assertIsNone(native_session.acs_caller)
        with self.assertRaisesRegex(ValueError, "validated Dynamics conversation"):
            await native_session.tools["talk_to_human"].target({})
        paths = {route.resource.canonical for route in client.server.app.router.routes()}
        self.assertTrue(any(path.startswith("/dynamics/native") for path in paths))

    async def test_browser_voice_requires_no_application_acs_configuration(self):
        config = replace(DEPLOYMENT, features=replace(DEPLOYMENT.features, browserVoice=True))
        client = await self.start(config)
        with patch.object(app.RTMiddleTier, "forward_messages", new=AsyncMock()) as forward:
            ws = await client.ws_connect("/realtime")
            await ws.close()
            forward.assert_awaited_once()
        self.assertIsNone(self.native.call_args.kwargs["customer_context_provider"])
        health = await (await client.get("/health")).json()
        self.assertFalse(health["acs_configured"])
        self.assertEqual(health["tools"], [])

    async def test_crm_enabled_native_factory_retains_verified_context_provider(self):
        config = replace(DEPLOYMENT, features=replace(DEPLOYMENT.features, crm=True, nativeVoice=True))
        await self.start(config)
        provider = self.native.call_args.kwargs["customer_context_provider"]
        self.assertIsNotNone(provider)
        self.assertIsInstance(provider.__self__, CaseServiceClient)
        factory = self.native.call_args.args[3]
        session = factory("+12025550101", "en")
        self.assertIsNone(session.customer_context)
        self.assertIsNone(session.customer_name)
        self.assertIsNone(session.agent_assist_client)
        self.assertIsNone(session.acs_caller)

    async def test_native_selection_retains_existing_environment_safety_gates(self):
        config = replace(DEPLOYMENT, features=replace(DEPLOYMENT.features, nativeVoice=True))
        self.enterContext(patch.dict("os.environ", {"DYNAMICS_NATIVE_MEDIA_ENABLED": "true"}))
        with self.assertRaisesRegex(ValueError, "Disable ACS_AUTO_CONFERENCE_ENABLED"):
            await app.create_app(config)
        # Unselected native modules do not load or require native credentials.
        with patch("app.NativeMediaConfig.from_env", side_effect=AssertionError("Native module is not selected")):
            client = await self.start()
        self.assertFalse(self.native.call_args.args[0].enabled)
        health = await (await client.get("/health")).json()
        self.assertFalse(health["features"]["nativeVoice"])

    async def test_unapproved_phone_is_never_dialed(self):
        client, caller = await self.phone_app()
        self.enterContext(patch.dict(app.PROFILES, {"+12025550101": {"display_name": "Generated contact"}}))
        response = await client.post("/call", json={"number": "+12025550101"})
        self.assertEqual(response.status, 403)
        self.assertIn("not an approved", await response.text())
        caller.initiate_call.assert_not_awaited()

    async def test_auto_conference_requires_an_explicit_destination_before_any_client(self):
        from backend.acs import AcsCaller
        with patch("backend.acs.CallAutomationClient.from_connection_string") as client:
            for number in ("", "not-a-phone", "+12025550102"):
                with self.assertRaisesRegex(ValueError, "explicitly approved ACS_AUTO_CONFERENCE_NUMBER"):
                    AcsCaller(
                        "+12025550102", "synthetic", "https://voice.example/acs", "wss://voice.example/realtime-acs",
                        auto_conference_enabled=True, auto_conference_number=number,
                    )
            client.assert_not_called()

    async def test_auto_conference_requires_the_separate_dialing_allowlist(self):
        from backend.acs import AcsCaller
        with patch("backend.acs.CallAutomationClient.from_connection_string") as client:
            with self.assertRaisesRegex(ValueError, "dialing policy"):
                AcsCaller(
                    "+12025550102", "synthetic", "https://voice.example/acs", "wss://voice.example/realtime-acs",
                    auto_conference_enabled=True, auto_conference_number="+12025550103",
                )
            client.assert_not_called()

    async def test_synthetic_crm_recipient_requires_explicit_recipient_approval(self):
        config = replace(enabled_deployment(), emailPolicy=EmailPolicy((), ("example.test",)))
        session = SimpleNamespace(
            customer_email="generated@example.test", customer_name="Test contact",
            customer_context=object(), language="en",
        )
        token = CURRENT_SESSION.set(session)
        self.addCleanup(CURRENT_SESSION.reset, token)
        with patch("backend.tools.send_customer_email.EmailClient.from_connection_string") as send:
            for address in (session.customer_email, "another-generated@example.test"):
                result = await _send_customer_email_tool({"emailAddress": address, "infoBody": "Confirmed summary."}, config)
                self.assertFalse(json.loads(result.to_text())["success"])
            send.assert_not_called()
        self.enterContext(patch.dict("os.environ", {
            "ACS_EMAIL_CONNECTION_STRING": "synthetic", "ACS_EMAIL_SENDER": "sender@example.test",
        }))
        config = replace(config, emailPolicy=EmailPolicy((session.customer_email,), ()))
        client = SimpleNamespace(begin_send=AsyncMock(return_value=SimpleNamespace(
            result=AsyncMock(return_value={"status": "Succeeded", "id": "receipt"}),
        )))
        with patch("backend.tools.send_customer_email.EmailClient.from_connection_string") as send:
            send.return_value.__aenter__ = AsyncMock(return_value=client)
            send.return_value.__aexit__ = AsyncMock(return_value=None)
            result = await _send_customer_email_tool({"infoBody": "Confirmed summary."}, config)
            self.assertTrue(json.loads(result.to_text())["success"])


if __name__ == "__main__":
    unittest.main()
