import json
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from backend.native_call_probe import MAX_EVENTS, NativeCallProbe, PATH, RETENTION


KEY = "synthetic-native-probe-key-for-tests-only"
RESOURCE = (
    "/subscriptions/11111111-1111-4111-8111-111111111111/resourceGroups/poc/"
    "providers/Microsoft.Communication/communicationServices/native-acs"
)
CORRELATION = "22222222-2222-4222-8222-222222222222"


def sample_event(now, **overrides):
    return {
        "id": "synthetic-start-event",
        "topic": RESOURCE,
        "eventType": "Microsoft.Communication.IncomingCall",
        "eventTime": now.isoformat(),
        "data": {
            "serverCallId": "synthetic-opaque-locator",
            "correlationId": CORRELATION,
            "startedBy": {"private": "must-not-be-retained"},
            "incomingCallContext": "must-not-be-retained",
            "from": {"phoneNumber": {"value": "must-not-be-retained"}},
        },
        **overrides,
    }


class MetadataTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime.now(timezone.utc)
        self.probe = NativeCallProbe(RESOURCE, KEY, (self.now + timedelta(hours=1)).isoformat())

    def test_configuration_disabled_by_default(self):
        with patch.dict("os.environ", {}, clear=True):
            self.assertFalse(NativeCallProbe.from_env().enabled)

    def test_partial_configuration_fails_explicitly(self):
        for arguments in (
            (RESOURCE, "", ""), (RESOURCE, KEY, ""), ("", KEY, self.now.isoformat()),
            (RESOURCE, "short", self.now.isoformat()),
            (RESOURCE, KEY, "2026-10-17T12:00:00"),
            ("https://wrong-resource", KEY, self.now.isoformat()),
        ):
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                NativeCallProbe(*arguments)

    def test_permanent_observer_is_explicit_and_keeps_retention(self):
        with patch.dict("os.environ", {
            "DYNAMICS_NATIVE_PROBE_RESOURCE_ID": RESOURCE,
            "DYNAMICS_NATIVE_PROBE_API_KEY": KEY,
            "DYNAMICS_NATIVE_PROBE_PERMANENT": "true",
        }, clear=True):
            probe = NativeCallProbe.from_env()
        self.assertTrue(probe.enabled)
        self.assertIsNone(probe.expires_at)
        self.assertEqual(probe.health(), {"enabled": True, "permanent": True, "retention_seconds": 900})
        probe.observe([sample_event(self.now)], self.now)
        probe._prune(self.now + RETENTION + timedelta(seconds=1))
        self.assertFalse(probe._events)
        self.assertTrue(probe.enabled)

    def test_permanent_mode_rejects_partial_or_ambiguous_configuration(self):
        for resource, key, expiry, permanent in (
            ("", "", "", True), (RESOURCE, "", "", True), ("", KEY, "", True),
            (RESOURCE, KEY, self.now.isoformat(), True), (RESOURCE, KEY, "", "true"),
        ):
            with self.subTest(resource=resource, permanent=permanent), self.assertRaises(ValueError):
                NativeCallProbe(resource, key, expiry, permanent=permanent)
        with patch.dict("os.environ", {"DYNAMICS_NATIVE_PROBE_PERMANENT": "yes"}, clear=True):
            with self.assertRaisesRegex(ValueError, "must be true or false"):
                NativeCallProbe.from_env()

    def test_secret_is_not_represented(self):
        self.assertNotIn(KEY, repr(self.probe))
        self.assertNotIn(KEY, json.dumps(self.probe.health()))

    def test_only_call_ids_and_timestamps_are_retained(self):
        self.assertEqual(self.probe.observe([sample_event(self.now)], self.now), {
            "accepted": 1, "ignored_expired": 0,
        })
        retained = json.dumps(self.probe._events)
        self.assertNotIn("must-not-be-retained", retained)
        self.assertNotIn("startedBy", retained)
        self.assertNotIn("incomingCallContext", retained)
        self.assertIn("synthetic-opaque-locator", retained)

    def test_duplicate_and_conflicting_events(self):
        event = sample_event(self.now)
        self.probe.observe([event, event], self.now)
        self.assertEqual(len(self.probe._events), 1)
        with self.assertRaisesRegex(ValueError, "Conflicting"):
            self.probe.observe([
                sample_event(self.now, data={"serverCallId": "other", "correlationId": CORRELATION})
            ], self.now)
        self.assertEqual(len(self.probe._events), 1)

    def test_batch_validation_is_atomic(self):
        with self.assertRaises(ValueError):
            self.probe.observe([sample_event(self.now), {}], self.now)
        self.assertFalse(self.probe._events)

    def test_event_kind_source_shape_and_time_are_validated(self):
        for event in (
            None,
            sample_event(self.now, topic=RESOURCE + "-wrong"),
            sample_event(self.now, topic=None),
            sample_event(self.now, eventType="Microsoft.Communication.RecordingFileStatusUpdated"),
            sample_event(self.now, data=None),
            sample_event(self.now, data={"serverCallId": "id", "correlationId": "bad"}),
            sample_event(self.now, data={"serverCallId": "", "correlationId": CORRELATION}),
            sample_event(self.now, eventTime=(self.now + timedelta(minutes=2)).isoformat()),
        ):
            with self.subTest(event=event), self.assertRaises(ValueError):
                self.probe.observe([event], self.now)
        self.assertFalse(self.probe._events)

    def test_retention_expiry_is_explicit_and_bounded(self):
        self.probe.observe([sample_event(self.now)], self.now)
        self.probe._prune(self.now + RETENTION + timedelta(seconds=1))
        self.assertFalse(self.probe._events)
        result = self.probe.observe([sample_event(self.now - RETENTION - timedelta(seconds=1))], self.now)
        self.assertEqual(result, {"accepted": 0, "ignored_expired": 1})

    def test_capacity_limit_never_evicts_another_current_call_silently(self):
        for start in range(0, MAX_EVENTS, 32):
            self.probe.observe([
                sample_event(self.now, id=f"event-{index}") for index in range(start, start + 32)
            ], self.now)
        with self.assertRaisesRegex(ValueError, "buffer is full"):
            self.probe.observe([sample_event(self.now)], self.now)
        self.assertEqual(len(self.probe._events), MAX_EVENTS)

    def test_subscription_validation_does_not_store_events(self):
        result = self.probe.observe([sample_event(
            self.now, eventType="Microsoft.EventGrid.SubscriptionValidationEvent",
            data={"validationCode": "synthetic-challenge"},
        )], self.now)
        self.assertEqual(result, {"validationResponse": "synthetic-challenge"})
        self.assertFalse(self.probe._events)


class MetadataRouteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.now = datetime.now(timezone.utc)
        self.probe = NativeCallProbe(RESOURCE, KEY, (self.now + timedelta(hours=1)).isoformat())
        app = web.Application()
        self.probe.register(app)
        self.http = TestClient(TestServer(app))
        await self.http.start_server()
        self.addAsyncCleanup(self.http.close)
        self.headers = {"X-API-Key": KEY}

    async def test_reads_and_event_deliveries_require_the_key(self):
        for method in ("GET", "POST"):
            response = await self.http.request(method, PATH, json=[sample_event(self.now)])
            self.assertEqual(response.status, 401)
            await response.read()
        self.assertFalse(self.probe._events)

    async def test_expired_probe_does_not_accept_events(self):
        self.probe.expires_at = self.now - timedelta(seconds=1)
        response = await self.http.post(PATH, headers=self.headers, json=[sample_event(self.now)])
        self.assertEqual(response.status, 503)
        self.assertFalse(self.probe._events)

    async def test_permanent_observer_still_checks_authentication_and_event_source(self):
        self.probe.permanent = True
        self.probe.expires_at = None
        response = await self.http.post(PATH, json=[sample_event(self.now)])
        self.assertEqual(response.status, 401)
        await response.read()
        response = await self.http.post(PATH, headers=self.headers, json=[sample_event(self.now, topic=RESOURCE + "-other")])
        self.assertEqual(response.status, 400)
        await response.read()
        self.assertFalse(self.probe._events)
        response = await self.http.post(PATH, headers=self.headers, json=[sample_event(self.now)])
        self.assertEqual(response.status, 200)
        self.assertEqual((await response.json())["accepted"], 1)

    async def test_actual_eventgrid_handshake_shape(self):
        response = await self.http.post(PATH, headers=self.headers, json=[sample_event(
            self.now, eventType="Microsoft.EventGrid.SubscriptionValidationEvent",
            data={"validationCode": "synthetic-challenge", "validationUrl": "https://not-followed.example"},
        )])
        self.assertEqual(response.status, 200)
        self.assertEqual(await response.json(), {"validationResponse": "synthetic-challenge"})
        self.assertFalse(self.probe._events)

    async def test_read_is_scoped_to_exact_correlation(self):
        response = await self.http.post(PATH, headers=self.headers, json=[sample_event(self.now)])
        self.assertEqual(response.status, 200)
        await response.read()
        response = await self.http.get(PATH, headers=self.headers, params={"correlation_id": CORRELATION})
        self.assertEqual(response.status, 200)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        body = await response.json()
        self.assertEqual(len(body["events"]), 1)
        self.assertNotIn("must-not-be-retained", json.dumps(body))
        response = await self.http.get(PATH, headers=self.headers, params={
            "correlation_id": "33333333-3333-4333-8333-333333333333",
        })
        self.assertEqual(await response.json(), {"events": []})

    async def test_invalid_json_and_missing_read_correlation_are_explicit(self):
        response = await self.http.get(PATH, headers=self.headers)
        self.assertEqual(response.status, 400)
        response = await self.http.post(PATH, headers=self.headers, data=b"{")
        self.assertEqual(response.status, 400)
        self.assertFalse(self.probe._events)

    async def test_event_body_size_is_bounded(self):
        response = await self.http.post(PATH, headers=self.headers, data=b" " * (64 * 1024 + 1))
        self.assertEqual(response.status, 413)
        self.assertFalse(self.probe._events)


if __name__ == "__main__":
    unittest.main()
