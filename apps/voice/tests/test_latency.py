import asyncio
import json
import unittest
from unittest.mock import patch

from backend.latency import MAX_EVENTS, LatencyTrace, measure


CALL = "11111111-1111-4111-8111-111111111111"


class LatencyTests(unittest.TestCase):
    def test_span_uses_a_monotonic_clock_and_records_errors_without_exception_text(self):
        with patch("backend.latency.time.perf_counter", side_effect=[10, 10.1, 10.25]):
            trace = LatencyTrace(CALL)
            with self.assertRaises(ValueError):
                with trace.span("token_request"):
                    raise ValueError("private-token-or-content")
        event = trace.snapshot()["events"][0]
        self.assertEqual(event["elapsed_ms"], 250)
        self.assertEqual(event["duration_ms"], 150)
        self.assertEqual(event["status"], "error")
        self.assertEqual(event["error_type"], "ValueError")
        self.assertNotIn("private-token-or-content", json.dumps(trace.snapshot()))

    def test_cancellation_is_observed_without_being_swallowed(self):
        trace = LatencyTrace(CALL)
        with self.assertRaises(asyncio.CancelledError):
            with trace.span("registration"):
                raise asyncio.CancelledError
        self.assertEqual(trace.snapshot()["events"][0]["status"], "cancelled")

    def test_first_milestones_are_not_duplicated_and_snapshots_are_copies(self):
        trace = LatencyTrace(CALL)
        trace.mark("registered", once=True)
        trace.mark("registered", once=True)
        document = trace.snapshot()
        self.assertEqual(len(document["events"]), 1)
        document["events"][0]["stage"] = "changed"
        self.assertEqual(trace.snapshot()["events"][0]["stage"], "registered")

    def test_timings_are_bounded_and_do_not_accept_sensitive_labels(self):
        trace = LatencyTrace(CALL)
        for data in ({"text": "customer words"}, {"token": "secret"}, {"tool": "+12025550101"}, {"language": "en\nextra"}):
            with self.subTest(data=data), self.assertRaises(ValueError):
                trace.mark("sample", **data)
        with self.assertRaises(ValueError):
            trace.mark("https://private.example/token")
        for index in range(MAX_EVENTS + 5):
            trace.mark("sample", count=index)
        self.assertEqual(len(trace.snapshot()["events"]), MAX_EVENTS)
        self.assertEqual(trace.dropped_events, 5)

    def test_audio_energy_is_not_confused_with_first_pcm_or_comfort_noise(self):
        trace = LatencyTrace(CALL)
        trace.audio("output", b"\1\0" * 480)
        trace.audio("output", b"\0\4" * 480)
        trace.audio("output", bytes(960))
        trace.audio("output", b"\0\4" * 480)
        trace.audio("output", b"\0\4" * 480)
        stages = [event["stage"] for event in trace.snapshot()["events"]]
        self.assertEqual(stages, ["first_output_pcm"])
        trace.audio("output", b"\0\4" * 480)
        trace.audio("output", b"\0\4" * 480)
        stages = [event["stage"] for event in trace.snapshot()["events"]]
        self.assertEqual(stages, ["first_output_pcm", "first_output_energy"])
        self.assertEqual(trace.snapshot()["events"][1]["threshold_dbfs"], -40)
        self.assertNotIn("pcm", json.dumps(trace.snapshot()).replace("first_output_pcm", ""))

    def test_disabled_measurement_preserves_original_exception(self):
        with self.assertRaises(RuntimeError):
            with measure(None, "unused"):
                raise RuntimeError("original")


if __name__ == "__main__":
    unittest.main()
