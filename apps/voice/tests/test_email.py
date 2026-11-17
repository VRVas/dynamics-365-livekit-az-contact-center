import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from azure.core.exceptions import HttpResponseError

from backend.tools.send_customer_email import _send_customer_email_tool
from backend.tools.tools import CURRENT_SESSION
from runtime_fixtures import enabled_deployment


class EmailToolTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.enterContext(patch("backend.tools.send_customer_email.DEPLOYMENT", enabled_deployment(language="en")))
        self.enterContext(patch.dict("os.environ", {
            "ACS_EMAIL_CONNECTION_STRING": "synthetic-connection",
            "ACS_EMAIL_SENDER": "DoNotReply@example.azurecomm.net",
        }, clear=True))
        self.poller = SimpleNamespace(result=AsyncMock(return_value={"id": "test-operation", "status": "Succeeded"}))
        self.client = SimpleNamespace(begin_send=AsyncMock(return_value=self.poller))
        self.factory = self.enterContext(patch("backend.tools.send_customer_email.EmailClient.from_connection_string"))
        self.factory.return_value.__aenter__ = AsyncMock(return_value=self.client)
        self.factory.return_value.__aexit__ = AsyncMock(return_value=None)

    async def test_omitted_recipient_uses_the_current_phone_profile(self):
        token = CURRENT_SESSION.set(SimpleNamespace(customer_email="profile@example.com", customer_name="Profile Customer"))
        try:
            result = json.loads((await _send_customer_email_tool({"infoBody": "Your requested service summary."})).to_text())
        finally:
            CURRENT_SESSION.reset(token)
        message = self.client.begin_send.call_args.args[0]
        self.assertEqual(message["recipients"]["to"], [{"address": "profile@example.com"}])
        self.assertIn("Dear Profile Customer", message["content"]["plainText"])
        self.assertEqual(result, {
            "success": True, "status": "submitted", "delivery_confirmed": False,
            "to": "profile@example.com", "message_id": "test-operation",
        })

    async def test_explicit_recipient_overrides_profile_and_body_is_escaped(self):
        token = CURRENT_SESSION.set(SimpleNamespace(customer_email="profile@example.com", customer_name="Profile Customer"))
        try:
            result = await _send_customer_email_tool({
                "emailAddress": "confirmed@example.com",
                "name": "A & B",
                "infoBody": "First line\n<script>not executable</script>",
            })
        finally:
            CURRENT_SESSION.reset(token)
        message = self.client.begin_send.call_args.args[0]
        self.assertTrue(json.loads(result.to_text())["success"])
        self.assertEqual(message["recipients"]["to"], [{"address": "confirmed@example.com"}])
        self.assertIn("Contact center", message["content"]["subject"])
        self.assertIn("A &amp; B", message["content"]["html"])
        self.assertNotIn("<script>", message["content"]["html"])
        self.assertIn("&lt;script&gt;", message["content"]["html"])

    async def test_requested_english_email_does_not_inherit_the_arabic_startup_language(self):
        token = CURRENT_SESSION.set(SimpleNamespace(customer_email="profile@example.com", language="ar"))
        try:
            result = await _send_customer_email_tool({"infoBody": "Your requested service summary.", "language": "en"})
        finally:
            CURRENT_SESSION.reset(token)
        self.assertTrue(json.loads(result.to_text())["success"])
        self.assertIn("Contact center", self.client.begin_send.call_args.args[0]["content"]["subject"])

    async def test_arabic_email_uses_arabic_correspondence(self):
        result = await _send_customer_email_tool({
            "emailAddress": "confirmed@example.com", "infoBody": "ملخص المعلومات المطلوبة", "language": "ar",
        })
        self.assertTrue(json.loads(result.to_text())["success"])
        self.assertIn("Contact center", self.client.begin_send.call_args.args[0]["content"]["subject"])
        self.assertIn("متابعة طلبك", self.client.begin_send.call_args.args[0]["content"]["subject"])

    async def test_missing_invalid_recipient_or_body_never_sends(self):
        for arguments in (
            {"infoBody": "summary"},
            {"emailAddress": 12, "infoBody": "summary"},
            {"emailAddress": "bad address", "infoBody": "summary"},
            {"emailAddress": "valid@example.com"},
            {"emailAddress": "valid@example.com", "infoBody": " "},
            {"emailAddress": "valid@example.com", "infoBody": "summary", "name": 12},
        ):
            with self.subTest(arguments=arguments):
                result = json.loads((await _send_customer_email_tool(arguments)).to_text())
                self.assertFalse(result["success"])
        self.factory.assert_not_called()

    async def test_only_succeeded_operation_with_receipt_is_success(self):
        for payload in (
            {"id": "operation", "status": "Failed"},
            {"id": "operation", "status": "Canceled"},
            {"id": "operation", "status": "Running"},
            {"status": "Succeeded"},
            {"id": "", "status": "Succeeded"},
            {"id": "operation"},
        ):
            with self.subTest(payload=payload):
                self.poller.result.return_value = payload
                result = json.loads((await _send_customer_email_tool({
                    "emailAddress": "confirmed@example.com", "infoBody": "summary",
                })).to_text())
                self.assertFalse(result["success"])

    async def test_service_error_is_visible_without_leaking_error_details(self):
        self.client.begin_send.side_effect = HttpResponseError("sensitive upstream detail")
        with self.assertLogs("customer_email") as captured:
            result = json.loads((await _send_customer_email_tool({
                "emailAddress": "confirmed@example.com", "infoBody": "summary",
            })).to_text())
        self.assertFalse(result["success"])
        self.assertNotIn("sensitive upstream detail", json.dumps(result))
        self.assertNotIn("sensitive upstream detail", "\n".join(captured.output))

    async def test_concurrent_calls_do_not_share_recipient_context(self):
        async def send_for(email):
            token = CURRENT_SESSION.set(SimpleNamespace(customer_email=email, customer_name="Customer"))
            try:
                await asyncio.sleep(0)
                return json.loads((await _send_customer_email_tool({"infoBody": "summary"})).to_text())["to"]
            finally:
                CURRENT_SESSION.reset(token)
        recipients = await asyncio.gather(send_for("first@example.com"), send_for("second@example.com"))
        self.assertEqual(recipients, ["first@example.com", "second@example.com"])
        sent = [call.args[0]["recipients"]["to"][0]["address"] for call in self.client.begin_send.call_args_list]
        self.assertEqual(sent, recipients)
        self.assertIsNone(CURRENT_SESSION.get())


if __name__ == "__main__":
    unittest.main()
