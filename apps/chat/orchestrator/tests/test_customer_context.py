import asyncio
import hashlib
import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

os.environ.setdefault("FOUNDRY_PROJECT_ENDPOINT", "https://unit-test.services.ai.azure.com/api/projects/unit-test")
os.environ.setdefault("ASSIST_ENABLED", "false")

import httpx
import app
import case_tools
from handover import HandoverHub
from test_case_tools import dynamics_context
from runtime_fixtures import enabled_deployment


class DynamicsConversationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.enterContext(patch.object(app, "DEPLOYMENT", enabled_deployment()))
        self.enterContext(patch.object(case_tools, "DEPLOYMENT", enabled_deployment()))
        for state in (
            app._sessions, app._profile_ids, app._interface_locales,
            app._customer_contexts, app._conversation_locks,
        ):
            state.clear()
            self.addCleanup(state.clear)
        self.enterContext(patch.dict(os.environ, {
            "ASSIST_BASE_URL": "https://assist.example", "ASSIST_INGEST_KEY": "unit-test",
        }))
        self.enterContext(patch.object(app, "ASSIST_ENABLED", False))
        self.enterContext(patch.object(app, "_hub", HandoverHub()))
        self.enterContext(patch.object(app, "PROFILES", {
            "no_profile": {"display_name": "Guest"},
            **{
                f"profile-{marker.lower()}": {
                    "phone": f"ACTOR_{marker}", "display_name": f"STATIC_NAME_{marker}",
                    "context": f"STATIC_NOTES_{marker}", "email": f"STATIC_EMAIL_{marker}",
                } for marker in ("A", "B")
            },
        }))
        self.crm_contexts = {
            "profile:" + hashlib.sha256(f"ACTOR_{marker}".encode()).hexdigest(): dynamics_context(marker)
            for marker in ("A", "B")
        }
        self.crm_status = 200
        self.requests = []
        self.messages = []
        self.events = []
        self.tool_results = []
        self.fail_run = False

        async def case_service_response(request):
            payload = json.loads(request.content)
            self.requests.append(payload)
            self.events.append("fetch")
            await asyncio.sleep(0)
            if self.crm_status != 200:
                return httpx.Response(self.crm_status, json={"error": {"message": "Dynamics is unavailable."}})
            snapshot = self.crm_contexts.get(payload.get("actorId"), dynamics_context(matched=False))
            if payload["action"] == "customer_context":
                result = snapshot
            elif payload["action"] == "list_cases":
                result = {
                    "success": True, "source": "dynamics",
                    "cases": snapshot["cases"], "casesTruncated": snapshot["casesTruncated"],
                }
            else:
                result = {"success": True}
            return httpx.Response(200, json=result)

        case_tools._client = httpx.AsyncClient(transport=httpx.MockTransport(case_service_response))
        self.addAsyncCleanup(case_tools.close_client)

        async def run(message, session):
            context = case_tools.CONVERSATION.get()
            self.events.append("run")
            self.messages.append((context.conversation_id, message))
            if self.fail_run:
                raise RuntimeError("MODEL_UNAVAILABLE")
            if message == "REFRESH_CONTEXT":
                self.tool_results.append(json.loads(await case_tools.get_customer_context.func()))
            elif message == "READ_CURRENT_CASES":
                self.tool_results.append(json.loads(await case_tools.list_customer_cases.func()))
            elif message == "PUBLIC_GUIDANCE":
                await case_tools.invoke("knowledge", {"query": "General service requirements"})
            return SimpleNamespace(text="Service response.")

        self.agent = SimpleNamespace(create_session=Mock(side_effect=object), run=run)
        self.enterContext(patch.object(app, "_get_agent", return_value=self.agent))
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app.app), base_url="http://testserver")
        self.addAsyncCleanup(self.client.aclose)

    async def send(self, conversation_id, profile_id="profile-a", message="Hello", locale=None):
        return await self.client.post(f"/conversation/{conversation_id}/stream", json={
            "message": message, "profile_id": profile_id, "locale": locale,
        })

    async def test_profile_selector_exposes_only_display_hints_and_preserves_private_lookup(self):
        response = await self.client.get("/profiles")
        self.assertEqual(response.status_code, 200)
        entries = response.json()
        self.assertEqual({entry["id"] for entry in entries}, set(app.PROFILES))
        for entry in entries:
            self.assertEqual(set(entry), {"id", "display_name", "display_name_ar"})
        self.assertNotIn("ACTOR_", response.text)
        self.assertNotIn("STATIC_EMAIL_", response.text)
        self.assertNotIn("STATIC_NOTES_", response.text)
        self.assertEqual(app.PROFILES["profile-a"]["phone"], "ACTOR_A")
        self.assertEqual(self.requests, [])
        self.agent.create_session.assert_not_called()

    async def test_crm_context_is_loaded_before_first_run_and_replaces_static_profile_data(self):
        response = await self.send("selected")
        self.assertNotIn('["error"', response.text)
        self.assertEqual(self.events, ["fetch", "run"])
        message = self.messages[0][1]
        for value in ("CRM_NAME_A", "CRM_FIRST_NAME_A", "CRM_EMAIL_A", "CRM_NOTES_A", "CRM_ACCOUNT_NAME_A"):
            self.assertIn(value, message)
        self.assertNotIn("STATIC_", message)
        self.assertIn("Greet the customer naturally", message)
        self.assertIn('"identityVerified": false', message)
        self.assertIn('"dataScope": "synthetic_profiles_only"', message)
        self.assertIn("Initial interface language: ar", message)
        self.assertEqual(self.requests, [{
            "action": "customer_context",
            "actorId": "profile:" + hashlib.sha256(b"ACTOR_A").hexdigest(),
        }])
        self.assertIsNone(case_tools.CONVERSATION.get())

    async def test_profile_is_loaded_once_without_resetting_conversation_language(self):
        for message in ("Hello", "Please speak English.", "How can I get help?"):
            response = await self.send("sticky-profile", message=message, locale="ar")
            self.assertNotIn('["error"', response.text)
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(self.messages[1:], [
            ("sticky-profile", "Please speak English."), ("sticky-profile", "How can I get help?"),
        ])
        self.agent.create_session.assert_called_once()
        await self.send("sticky-profile", message="Next question", locale="en")
        self.assertIn("explicitly changed the UI language: en", self.messages[-1][1])
        await self.send("sticky-profile", profile_id=None, message="No UI selection")
        self.assertEqual(self.messages[-1][1], "No UI selection")
        self.assertEqual(len(self.requests), 1)

    async def test_unmatched_profile_has_no_static_name_email_or_automatic_contact_creation(self):
        actor = "profile:" + hashlib.sha256(b"ACTOR_A").hexdigest()
        self.crm_contexts[actor] = dynamics_context(matched=False)
        response = await self.send("unmatched")
        self.assertNotIn('["error"', response.text)
        self.assertIn('"customer": null', self.messages[0][1])
        self.assertIn("use a generic", self.messages[0][1])
        self.assertNotIn("STATIC_", self.messages[0][1])
        self.assertEqual([request["action"] for request in self.requests], ["customer_context"])

    async def test_guest_does_not_load_private_crm_context(self):
        response = await self.send("guest", profile_id="no_profile")
        self.assertNotIn('["error"', response.text)
        self.assertEqual(self.requests, [])
        self.assertEqual(app._customer_contexts["guest"], {})
        self.assertNotIn("<dynamics_customer_context>", self.messages[0][1])

    async def test_context_failure_is_visible_retryable_and_does_not_unlock_the_profile(self):
        self.crm_status = 503
        response = await self.send("retry")
        self.assertIn("Dynamics customer context could not be loaded", response.text)
        self.assertEqual(self.messages, [])
        self.assertNotIn("retry", app._sessions)
        self.assertEqual(app._customer_contexts["retry"], {})
        self.agent.create_session.assert_not_called()
        response = await self.send("retry", profile_id="profile-b")
        self.assertIn("Start a new conversation before changing profiles", response.text)
        self.assertEqual(len(self.requests), 1)
        self.crm_status = 200
        response = await self.send("retry", profile_id=None)
        self.assertNotIn('["error"', response.text)
        self.assertEqual(len(self.requests), 2)
        self.assertIn("CRM_NAME_A", self.messages[0][1])
        self.assertIn("Initial interface language: ar", self.messages[0][1])

    async def test_no_configured_service_does_not_fall_back_to_static_profile_data(self):
        with patch.dict(os.environ, {"ASSIST_BASE_URL": "", "ASSIST_INGEST_KEY": ""}):
            response = await self.send("unconfigured")
        self.assertIn("Dynamics customer context could not be loaded", response.text)
        self.assertEqual(self.messages, [])
        self.assertEqual(self.requests, [])

    async def test_parallel_profiles_never_share_customer_context(self):
        responses = await asyncio.gather(
            self.send("session-a", "profile-a"), self.send("session-b", "profile-b"),
        )
        self.assertTrue(all('["error"' not in response.text for response in responses))
        messages = dict(self.messages)
        self.assertIn("CRM_NAME_A", messages["session-a"])
        self.assertNotIn("CRM_NAME_B", messages["session-a"])
        self.assertIn("CRM_NAME_B", messages["session-b"])
        self.assertNotIn("CRM_NAME_A", messages["session-b"])
        self.assertIsNot(app._customer_contexts["session-a"], app._customer_contexts["session-b"])
        self.assertEqual(len({request["actorId"] for request in self.requests}), 2)
        self.assertIsNone(case_tools.CONVERSATION.get())

    async def test_parallel_first_turns_share_one_profile_load_and_session(self):
        responses = await asyncio.gather(self.send("same-session"), self.send("same-session"))
        self.assertTrue(all('["error"' not in response.text for response in responses))
        self.assertEqual(len(self.requests), 1)
        self.agent.create_session.assert_called_once()
        self.assertIn("<dynamics_customer_context>", self.messages[0][1])
        self.assertEqual(self.messages[1][1], "Hello")

    async def test_concurrent_profile_switch_does_not_change_the_case_actor(self):
        responses = await asyncio.gather(
            self.send("locked-session", "profile-a"), self.send("locked-session", "profile-b"),
        )
        self.assertEqual(sum('["error"' in response.text for response in responses), 1)
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(len(self.messages), 1)
        self.assertIn("CRM_NAME_A", self.messages[0][1])
        self.assertNotIn("CRM_NAME_B", self.messages[0][1])

    async def test_intentional_refresh_updates_session_context_and_knowledge_redaction(self):
        await self.send("refresh")
        actor = "profile:" + hashlib.sha256(b"ACTOR_A").hexdigest()
        self.crm_contexts[actor] = dynamics_context("UPDATED")
        await self.send("refresh", message="REFRESH_CONTEXT")
        await self.send("refresh", message="PUBLIC_GUIDANCE")
        self.assertEqual([request["action"] for request in self.requests], [
            "customer_context", "customer_context", "knowledge",
        ])
        self.assertEqual(app._customer_contexts["refresh"]["customer"]["email"], "CRM_EMAIL_UPDATED")
        self.assertIn("CRM_EMAIL_UPDATED", self.requests[-1]["sensitiveTerms"])
        self.assertNotIn("CRM_EMAIL_A", self.requests[-1]["sensitiveTerms"])
        self.assertNotIn("STATIC_EMAIL_A", self.requests[-1]["sensitiveTerms"])
        self.assertEqual(self.tool_results[0]["customer"]["email"], "CRM_EMAIL_UPDATED")

    async def test_case_list_reads_current_status_without_reloading_customer_profile(self):
        await self.send("current-status")
        actor = "profile:" + hashlib.sha256(b"ACTOR_A").hexdigest()
        self.crm_contexts[actor]["cases"][0]["status"] = "Resolved"
        await self.send("current-status", message="READ_CURRENT_CASES")
        self.assertEqual([request["action"] for request in self.requests], ["customer_context", "list_cases"])
        self.assertEqual(self.tool_results[0]["cases"][0]["status"], "Resolved")
        self.assertEqual(app._customer_contexts["current-status"]["cases"][0]["status"], "Active")

    async def test_failed_first_agent_run_does_not_lose_initial_crm_or_language_context(self):
        self.fail_run = True
        response = await self.send("agent-retry")
        self.assertIn("MODEL_UNAVAILABLE", response.text)
        self.assertNotIn("agent-retry", app._sessions)
        self.fail_run = False
        response = await self.send("agent-retry")
        self.assertNotIn('["error"', response.text)
        self.assertEqual(len(self.requests), 1)
        self.assertTrue(all("<dynamics_customer_context>" in message for _, message in self.messages))
        self.assertTrue(all("Initial interface language: ar" in message for _, message in self.messages))
