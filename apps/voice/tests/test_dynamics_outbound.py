import json
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from aiohttp.test_utils import TestClient, TestServer
from azure.core.exceptions import ClientAuthenticationError

from app import create_app
from backend.azure import get_azure_credentials
from backend.dynamics_outbound import (
    PREFIX,
    DynamicsOutboundClient,
    DynamicsOutboundConfig,
    OutboundError,
    OutboundRequest,
)


ORG = "https://example.crm17.dynamics.com"
KEY = "synthetic-outbound-key-for-tests-only"
CONFIG_ID = "11111111-1111-4111-8111-111111111111"
REQUEST_ID = "22222222-2222-4222-8222-222222222222"
CONTACT_ID = "33333333-3333-4333-8333-333333333333"
TASK_ID = "44444444-4444-4444-8444-444444444444"
DELIVERY_ID = "55555555-5555-4555-8555-555555555555"
ACTIVE_CONFIG = {
    "msdyn_name": "Synthetic outbound",
    "statecode": 0,
    "msdyn_type": 100000000,
    "msdyn_dialmode_type": 100000000,
    "msdyn_engagement_type": 100000100,
    "_msdyn_workstream_value": "66666666-6666-4666-8666-666666666666",
}
TASK = {
    "deliveryTaskId": TASK_ID,
    "proactiveEngagementConfigId": CONFIG_ID,
    "requestId": REQUEST_ID,
    "status": "Pending",
    "deliveries": [{"id": DELIVERY_ID, "status": "Pending"}],
}
CREATED = {"DeliveryTaskId": TASK_ID, "DeliveryId": DELIVERY_ID}


def config(**overrides):
    return replace(DynamicsOutboundConfig(ORG, CONFIG_ID, KEY, True), **overrides)


def request_body(**overrides):
    now = datetime.now(timezone.utc)
    return {
        "request_id": REQUEST_ID,
        "contact_id": CONTACT_ID,
        "phone_number": "+12025550101",
        "not_before": (now + timedelta(minutes=1)).isoformat(),
        "expires_at": (now + timedelta(minutes=6)).isoformat(),
        "consent_confirmed": True,
        "language": "en",
        "context": {"CaseReference": "synthetic-case"},
        **overrides,
    }


def http_response(status=200, payload=None, *, raw=None, headers=None):
    response = MagicMock()
    response.status = status
    response.headers = headers or {}
    response.read = AsyncMock(return_value=json.dumps(payload).encode() if raw is None else raw)
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=response)
    context.__aexit__ = AsyncMock(return_value=False)
    return context


class OutboundConfigTests(unittest.TestCase):
    def test_default_is_disabled_and_has_no_credentials(self):
        with patch.dict("os.environ", {}, clear=True):
            settings = DynamicsOutboundConfig.from_env()
        self.assertFalse(settings.enabled)
        self.assertFalse(settings.configured)

    def test_enabling_requires_all_configuration_and_valid_boolean(self):
        for env in (
            {"DYNAMICS_OUTBOUND_ENABLED": "sometimes"},
            {"DYNAMICS_OUTBOUND_ENABLED": "true"},
            {"DYNAMICS_OUTBOUND_ENABLED": "true", "DYNAMICS_ORG_URL": ORG, "DYNAMICS_OUTBOUND_API_KEY": KEY},
        ):
            with self.subTest(env=env), patch.dict("os.environ", env, clear=True), self.assertRaises(ValueError):
                DynamicsOutboundConfig.from_env()

    def test_rejects_unsafe_organization_urls(self):
        for url in (
            "http://example.crm17.dynamics.com", "https://example.crm17.dynamics.com.evil.test",
            "https://user:password@example.crm17.dynamics.com", "https://example.crm17.dynamics.com:443",
            ORG + "/api/data/v9.2", ORG + "?query=1", ORG + "#fragment", "https://localhost",
        ):
            with self.subTest(url=url), self.assertRaises(ValueError):
                config(org_url=url)

    def test_configuration_secrets_are_not_represented(self):
        self.assertNotIn(KEY, repr(config()))
        for value in ("short", KEY + "\n"):
            with self.assertRaises(ValueError):
                config(api_key=value)

    def test_dedicated_key_is_not_replaced_by_assist_key(self):
        with patch.dict("os.environ", {
            "DYNAMICS_ORG_URL": ORG, "AGENT_ASSIST_API_KEY": KEY,
        }, clear=True):
            self.assertFalse(DynamicsOutboundConfig.from_env().configured)

    def test_normalizes_environment_configuration(self):
        with patch.dict("os.environ", {
            "DYNAMICS_ORG_URL": ORG + "/", "DYNAMICS_OUTBOUND_API_KEY": KEY,
            "DYNAMICS_OUTBOUND_CONFIG_ID": CONFIG_ID, "DYNAMICS_OUTBOUND_ENABLED": "true",
        }, clear=True):
            self.assertEqual(DynamicsOutboundConfig.from_env(), config())

    def test_dataverse_credential_does_not_prefetch_cognitive_services_token(self):
        with patch.dict("os.environ", {"AZURE_CLIENT_ID": CONTACT_ID}, clear=True), patch(
            "backend.azure.ManagedIdentityCredential"
        ) as factory:
            credential = get_azure_credentials(CONFIG_ID, warmup_scope=None)
        factory.assert_called_once_with(client_id=CONTACT_ID)
        credential.get_token.assert_not_called()

    def test_existing_credential_warmup_is_preserved(self):
        with patch.dict("os.environ", {}, clear=True), patch("backend.azure.DefaultAzureCredential") as factory:
            credential = get_azure_credentials()
        factory.assert_called_once()
        credential.get_token.assert_called_once_with("https://cognitiveservices.azure.com/.default")


class OutboundRequestTests(unittest.TestCase):
    def test_serialized_native_payload_preserves_identity_and_context(self):
        request = OutboundRequest.parse(request_body())
        payload = request.native_payload(CONFIG_ID)
        self.assertEqual(payload["ApiVersion"], "1.0")
        self.assertEqual(payload["RequestId"], REQUEST_ID)
        self.assertEqual(payload["ProactiveEngagementConfigId"], CONFIG_ID)
        self.assertIsInstance(payload["Contacts"], str)
        self.assertEqual(json.loads(payload["Contacts"]), [{
            "uniqueIdentifier": CONTACT_ID,
            "mobilePhoneNumber": "+12025550101",
            "timeZone": "UTC",
            "inputAttributes": {
                "CaseReference": "synthetic-case",
                "moacc_request_id": REQUEST_ID,
                "moacc_language": "en",
            },
        }])
        self.assertTrue(payload["ExpirationDate"].endswith("Z"))
        self.assertTrue(payload["EarliestScheduleDate"].endswith("Z"))
        self.assertNotIn("CallFrom", payload)

    def test_same_logical_request_keeps_native_idempotency_key(self):
        body = request_body()
        first = OutboundRequest.parse(body).native_payload(CONFIG_ID)
        second = OutboundRequest.parse(body).native_payload(CONFIG_ID)
        self.assertEqual(first, second)

    def test_all_required_inputs_and_consent_are_validated(self):
        for field in ("request_id", "contact_id", "phone_number", "not_before", "expires_at", "consent_confirmed"):
            body = request_body()
            del body[field]
            with self.subTest(field=field), self.assertRaises(ValueError):
                OutboundRequest.parse(body)
        for consent in (False, "true", 1, None):
            with self.subTest(consent=consent), self.assertRaises(ValueError):
                OutboundRequest.parse(request_body(consent_confirmed=consent))

    def test_unsupported_inputs_are_not_silently_accepted(self):
        for body in (None, [], "not an object", request_body(operator_number="+12025550102")):
            with self.subTest(body=body), self.assertRaises(ValueError):
                OutboundRequest.parse(body)

    def test_phone_and_identifier_validation(self):
        for field, values in (
            ("phone_number", ("12025550101", "+01234567", "+1", 123, "+12025550101\n")),
            ("request_id", ("not-a-uuid", "00000000-0000-0000-0000-000000000000", 1)),
            ("contact_id", ("a'*", None)),
        ):
            for value in values:
                with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                    OutboundRequest.parse(request_body(**{field: value}))

    def test_language_values_and_shapes_are_validated(self):
        for value in ("fr", [], {}, None, 1):
            with self.subTest(value=value), self.assertRaises(ValueError):
                OutboundRequest.parse(request_body(language=value))

    def test_context_limits_and_reserved_names(self):
        for value in (
            [], {"moacc_request_id": "override"}, {"MOACC_language": "override"},
            {"bad-key": "x"}, {"Valid": 1}, {"Valid": "x" * 513},
            {f"Key{i}": "x" for i in range(17)},
            {f"Key{i}": "x" * 512 for i in range(9)},
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                OutboundRequest.parse(request_body(context=value))

    def test_timestamps_require_offsets_and_convert_to_utc(self):
        now = datetime(2030, 1, 1, tzinfo=timezone.utc)
        request = OutboundRequest.parse(request_body(
            not_before="2030-01-01T03:01:00+03:00",
            expires_at="2030-01-01T03:06:00+03:00",
        ), now=now)
        self.assertEqual(request.native_payload(CONFIG_ID)["ExpirationDate"], "2030-01-01T00:06:00.000Z")
        for value in (
            "2030-01-01", "2030-01-01T00:01:00", "invalid", None,
            "0001-01-01T00:00:00+01:00",
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                OutboundRequest.parse(request_body(not_before=value), now=now)

    def test_exact_expiration_boundaries(self):
        now = datetime(2030, 1, 1, tzinfo=timezone.utc)
        start = now + timedelta(minutes=1)
        for end in (now, start, now + timedelta(days=30, microseconds=1)):
            with self.subTest(end=end), self.assertRaises(ValueError):
                OutboundRequest.parse(request_body(
                    not_before=start.isoformat(), expires_at=end.isoformat(),
                ), now=now)
        request = OutboundRequest.parse(request_body(
            not_before=start.isoformat(), expires_at=(now + timedelta(days=30)).isoformat(),
        ), now=now)
        self.assertEqual(request.expires_at, now + timedelta(days=30))


class NativeClientTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client = DynamicsOutboundClient(config())
        self.client._credential = MagicMock()
        self.client._credential.get_token.return_value = SimpleNamespace(token="synthetic-token")
        self.client._http = MagicMock(closed=False)
        self.client._http.close = AsyncMock()
        self.addAsyncCleanup(self.client.close)

    async def test_native_http_authentication_audience_and_no_redirects(self):
        self.client._http.request.return_value = http_response(payload={"UserId": CONTACT_ID})
        self.assertEqual(await self.client._request("GET", "WhoAmI"), {"UserId": CONTACT_ID})
        self.client._credential.get_token.assert_called_once_with(ORG + "/.default")
        call = self.client._http.request.call_args
        self.assertEqual(call.args, ("GET", ORG + "/api/data/v9.2/WhoAmI"))
        self.assertFalse(call.kwargs["allow_redirects"])
        self.assertEqual(call.kwargs["headers"]["Authorization"], "Bearer synthetic-token")
        self.assertNotIn(KEY, str(call))

    async def test_native_transport_failures_do_not_retry_mutations(self):
        self.client._http.request.side_effect = TimeoutError
        with self.assertRaises(OutboundError) as captured:
            await self.client._request("POST", "CCaaS_CreateProactiveDelivery", body={})
        self.assertEqual(captured.exception.code, "dynamics_outcome_unknown")
        self.client._http.request.assert_called_once()

    async def test_native_authentication_failures_are_explicit_and_sanitized(self):
        self.client._credential.get_token.side_effect = ClientAuthenticationError("private token details")
        with self.assertRaises(OutboundError) as captured:
            await self.client._request("GET", "WhoAmI")
        self.assertEqual(captured.exception.status, 503)
        self.assertNotIn("private token details", str(captured.exception))
        self.client._http.request.assert_not_called()

    async def test_upstream_statuses_do_not_become_success(self):
        for status, expected in ((301, 502), (400, 502), (401, 502), (403, 502), (404, 404), (500, 502)):
            self.client._http.request.return_value = http_response(status, raw=b"private upstream response")
            with self.subTest(status=status), self.assertRaises(OutboundError) as captured:
                await self.client._request("GET", "WhoAmI")
            self.assertEqual(captured.exception.status, expected)
            self.assertNotIn("private upstream response", str(captured.exception))

    async def test_rate_limit_delay_is_preserved_only_when_valid(self):
        for value, expected in (("12", "12"), ("<invalid>", None), ("999999999999", None)):
            self.client._http.request.return_value = http_response(429, headers={"Retry-After": value})
            with self.subTest(value=value), self.assertRaises(OutboundError) as captured:
                await self.client._request("POST", "CCaaS_CreateProactiveDelivery", body={})
            self.assertEqual(captured.exception.status, 429)
            self.assertEqual(captured.exception.retry_after, expected)

    async def test_malformed_success_response_is_not_accepted(self):
        for response in (http_response(raw=b"not json"), http_response(payload=[])):
            self.client._http.request.return_value = response
            with self.assertRaises(OutboundError) as captured:
                await self.client._request("GET", "WhoAmI")
            self.assertEqual(captured.exception.code, "invalid_dynamics_response")

    async def test_empty_success_is_valid_for_void_native_action(self):
        for status in (200, 202, 204):
            self.client._http.request.return_value = http_response(status, raw=b"")
            with self.subTest(status=status):
                self.assertEqual(await self.client._request("POST", "CCaaS_CancelDeliveryTask", body={}), {})

    async def test_readiness_is_read_only_and_separates_dispatch_gate(self):
        self.client.config = config(enabled=False, engagement_id="")
        self.client._request = AsyncMock(return_value={"UserId": CONTACT_ID})
        result = await self.client.readiness()
        self.assertTrue(result["dataverse_connected"])
        self.assertFalse(result["ready_for_submission"])
        self.assertEqual(result["blockers"], ["submission_disabled", "engagement_not_selected"])
        self.client._request.assert_awaited_once_with("GET", "WhoAmI")

    async def test_disabled_creation_performs_no_native_request(self):
        self.client.config = config(enabled=False)
        self.client._request = AsyncMock()
        with self.assertRaises(OutboundError) as captured:
            await self.client.submit(OutboundRequest.parse(request_body()))
        self.assertEqual(captured.exception.code, "submission_disabled")
        self.client._request.assert_not_awaited()

    async def test_wrong_native_policy_prevents_creation(self):
        for field, value in (
            ("statecode", 1), ("msdyn_type", 100000001),
            ("msdyn_dialmode_type", 100000002), ("msdyn_engagement_type", 100000101),
            ("_msdyn_workstream_value", None),
        ):
            self.client._request = AsyncMock(return_value={**ACTIVE_CONFIG, field: value})
            with self.subTest(field=field), self.assertRaises(OutboundError) as captured:
                await self.client.submit(OutboundRequest.parse(request_body()))
            self.assertEqual(captured.exception.code, "invalid_engagement")
            self.assertEqual(self.client._request.await_count, 1)

    async def test_created_response_requires_both_valid_native_ids(self):
        for response in ({}, {"DeliveryId": DELIVERY_ID}, {**CREATED, "DeliveryTaskId": "invalid"}):
            self.client._request = AsyncMock(side_effect=[ACTIVE_CONFIG, response])
            with self.subTest(response=response), self.assertRaises(OutboundError) as captured:
                await self.client.submit(OutboundRequest.parse(request_body()))
            self.assertEqual(captured.exception.code, "invalid_dynamics_response")

    async def test_native_task_id_is_guid_literal_not_a_guessed_parameter_shape(self):
        self.client._request = AsyncMock(return_value={"value": [TASK]})
        self.assertEqual(await self.client.get_task(TASK_ID), TASK)
        self.client._request.assert_awaited_once_with("GET", f"CCaaS_GetDeliveryTasks(DeliveryTaskId={TASK_ID})")

    async def test_task_shapes_and_scope_are_checked_before_cancel(self):
        for payload, code in (
            ({}, "invalid_dynamics_response"),
            ({"value": []}, "task_not_found"),
            ({"value": [TASK, TASK]}, "invalid_dynamics_response"),
            ({"value": [{**TASK, "deliveryTaskId": DELIVERY_ID}]}, "invalid_dynamics_response"),
            ({"value": [{**TASK, "proactiveEngagementConfigId": CONTACT_ID}]}, "task_not_found"),
            ({"value": [{**TASK, "status": None}]}, "invalid_dynamics_response"),
        ):
            self.client._request = AsyncMock(return_value=payload)
            with self.subTest(payload=payload), self.assertRaises(OutboundError) as captured:
                await self.client.cancel(TASK_ID)
            self.assertEqual(captured.exception.code, code)
            self.assertEqual(self.client._request.await_count, 1)

    async def test_cancellation_remains_available_when_creation_is_disabled(self):
        self.client.config = config(enabled=False)
        self.client._request = AsyncMock(side_effect=[{"value": [TASK]}, {}])
        result = await self.client.cancel(TASK_ID)
        self.assertEqual(result["status"], "cancellation_requested")
        self.assertIn("does not confirm an active call", result["scope"])
        self.client._request.assert_awaited_with(
            "POST", "CCaaS_CancelDeliveryTask", body={"ApiVersion": "1.0", "DeliveryTaskId": TASK_ID},
        )


class OutboundRouteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.connector = DynamicsOutboundClient(config())
        self.connector._request = AsyncMock()
        self.enterContext(patch.dict("os.environ", {"HUMAIN_ENABLED": "false"}, clear=True))
        self.enterContext(patch("app.load_dotenv"))
        self.enterContext(patch("app.DynamicsOutboundClient", return_value=self.connector))
        self.http = TestClient(TestServer(await create_app()))
        await self.http.start_server()
        self.addAsyncCleanup(self.http.close)
        self.headers = {"X-API-Key": KEY}

    async def test_every_control_route_is_authenticated(self):
        for method, path in (
            ("GET", f"{PREFIX}/readiness"),
            ("POST", f"{PREFIX}/requests"),
            ("GET", f"{PREFIX}/tasks/{TASK_ID}"),
            ("POST", f"{PREFIX}/tasks/{TASK_ID}/cancel"),
        ):
            with self.subTest(path=path):
                response = await self.http.request(method, path, headers={"X-API-Key": "wrong"})
                self.assertEqual(response.status, 401)
                self.assertEqual((await response.json())["error"]["code"], "unauthorized")
        self.connector._request.assert_not_awaited()

    async def test_api_key_is_never_accepted_in_query_parameters(self):
        response = await self.http.get(f"{PREFIX}/readiness", params={"api_key": KEY})
        self.assertEqual(response.status, 401)
        self.connector._request.assert_not_awaited()

    async def test_native_create_track_cancel_lifecycle(self):
        self.connector._request.side_effect = [
            ACTIVE_CONFIG, CREATED, {"value": [TASK]}, {"value": [TASK]}, {},
        ]
        response = await self.http.post(f"{PREFIX}/requests", headers=self.headers, json=request_body())
        self.assertEqual(response.status, 202)
        accepted = await response.json()
        self.assertEqual(accepted, {
            "status": "accepted", "request_id": REQUEST_ID,
            "delivery_task_id": TASK_ID, "delivery_id": DELIVERY_ID,
            "status_url": f"{PREFIX}/tasks/{TASK_ID}",
        })
        self.assertNotIn("connected", accepted)
        response = await self.http.get(accepted["status_url"], headers=self.headers)
        self.assertEqual(response.status, 200)
        self.assertEqual((await response.json())["delivery_task"], TASK)
        response = await self.http.post(f"{PREFIX}/tasks/{TASK_ID}/cancel", headers=self.headers)
        self.assertEqual(response.status, 202)
        self.assertEqual((await response.json())["status"], "cancellation_requested")
        create = self.connector._request.await_args_list[1]
        self.assertEqual(create.args, ("POST", "CCaaS_CreateProactiveDelivery"))
        self.assertEqual(create.kwargs["body"]["RequestId"], REQUEST_ID)
        self.assertEqual(create.kwargs["body"]["ProactiveEngagementConfigId"], CONFIG_ID)

    async def test_readiness_reports_actual_native_policy_not_just_environment_variables(self):
        self.connector._request.side_effect = [
            {"UserId": CONTACT_ID}, {**ACTIVE_CONFIG, "msdyn_dialmode_type": 100000002},
        ]
        response = await self.http.get(f"{PREFIX}/readiness", headers=self.headers)
        self.assertEqual(response.status, 200)
        result = await response.json()
        self.assertFalse(result["ready_for_submission"])
        self.assertEqual(result["blockers"], ["engagement_not_copilot_mode"])

    async def test_bad_json_inputs_are_explicit_without_native_requests(self):
        for data in (b"{", b"null", b"[]", b"\xff", b'{"consent_confirmed":false}'):
            with self.subTest(data=data):
                response = await self.http.post(f"{PREFIX}/requests", headers=self.headers, data=data)
                self.assertEqual(response.status, 400)
                self.assertEqual((await response.json())["error"]["code"], "invalid_request")
        self.connector._request.assert_not_awaited()

    async def test_request_size_limit(self):
        response = await self.http.post(f"{PREFIX}/requests", headers=self.headers, data=b" " * (16 * 1024 + 1))
        self.assertEqual(response.status, 413)
        self.connector._request.assert_not_awaited()

    async def test_bad_task_id_and_cancel_body_do_not_reach_dynamics(self):
        response = await self.http.get(f"{PREFIX}/tasks/not-a-guid", headers=self.headers)
        self.assertEqual(response.status, 400)
        response = await self.http.post(f"{PREFIX}/tasks/{TASK_ID}/cancel", headers=self.headers, json={})
        self.assertEqual(response.status, 400)
        self.connector._request.assert_not_awaited()

    async def test_unknown_submission_outcome_echoes_original_request_id(self):
        self.connector._request.side_effect = [
            ACTIVE_CONFIG, OutboundError("dynamics_outcome_unknown", "Outcome unknown.", 504),
        ]
        response = await self.http.post(f"{PREFIX}/requests", headers=self.headers, json=request_body())
        self.assertEqual(response.status, 504)
        result = await response.json()
        self.assertEqual(result["request_id"], REQUEST_ID)
        self.assertNotIn(KEY, json.dumps(result))
        self.assertNotIn("phone_number", result)

    async def test_unconfigured_service_fails_closed(self):
        self.connector.config = DynamicsOutboundConfig()
        response = await self.http.post(f"{PREFIX}/requests", headers=self.headers, json=request_body())
        self.assertEqual(response.status, 503)
        self.connector._request.assert_not_awaited()

    async def test_disabled_submission_does_not_disable_status(self):
        self.connector.config = config(enabled=False)
        response = await self.http.post(f"{PREFIX}/requests", headers=self.headers, json=request_body())
        self.assertEqual(response.status, 503)
        self.connector._request.assert_not_awaited()
        self.connector._request.return_value = {"value": [TASK]}
        response = await self.http.get(f"{PREFIX}/tasks/{TASK_ID}", headers=self.headers)
        self.assertEqual(response.status, 200)

    async def test_health_reports_configuration_without_secrets_or_live_requests(self):
        response = await self.http.get("/health")
        result = await response.json()
        self.assertEqual(result["dynamics_outbound"], {
            "configured": True, "submission_enabled": True, "engagement_selected": True,
        })
        self.assertNotIn(KEY, json.dumps(result))
        self.connector._request.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
