import copy
import base64
import io
import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import watch_native_voice as monitor


CALL = "11111111-1111-4111-8111-111111111111"
INSIGHT = "22222222-2222-4222-8222-222222222222"
WORKSTREAM = "33333333-3333-4333-8333-333333333333"
QUEUE = "44444444-4444-4444-8444-444444444444"
METRICS = {
    "callDuration": 100000.0,
    "customerLongestMonologueInMs": 3000.0,
    "avgPauseBeforeSpeakingInMs": 800.0,
    "switchCount": 4.0,
    "talkingSpeedWordsPerMin": 180.0,
    "talkToListenRatio": 0.6,
}


def retained_media(state="retained", **values):
    return {
        "conversation_id": CALL, "identity_retention_requested": True,
        "identity_retention_seconds": 900, "identity_cleanup_status": state,
        "phase": "handover_ready", **values,
    }


def api_with_insight(rows=None, **document):
    return SimpleNamespace(
        org="https://example.crm17.dynamics.com",
        headers={"Authorization": "Bearer unit-test-only"},
        get=MagicMock(return_value={
            "value": [{"msdyn_conversationinsightid": INSIGHT}] if rows is None else rows,
            **document,
        }),
    )


def response(data):
    result = MagicMock()
    result.__enter__.return_value.status = 200
    result.__enter__.return_value.read.return_value = json.dumps(data).encode()
    return result


class IdentityObservationTests(unittest.TestCase):
    def test_legacy_monitor_does_not_require_new_identity_fields(self):
        report = {}
        monitor.observe_identity(report, {})
        self.assertEqual(report, {})

    def test_immediate_mode_rejects_truthy_or_malformed_retention(self):
        report = {"identity_mode": "immediate"}
        monitor.observe_identity(report, {})
        self.assertEqual(report["immediate_cleanup_mode_checks"], 1)
        for value in (True, "true", 1, 0, None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                monitor.observe_identity(report, {"identity_retention_requested": value})

    def test_retained_mode_requires_explicit_typed_grace_and_status(self):
        report = {"identity_mode": "retained"}
        for media in (
            {}, retained_media(identity_retention_requested=False),
            retained_media(identity_retention_seconds=900.0),
            retained_media(identity_retention_seconds=899),
            retained_media("unknown"),
        ):
            with self.subTest(media=media), self.assertRaises(ValueError):
                monitor.observe_identity(report, media)

    def test_retention_must_be_observed_while_the_native_call_is_closed(self):
        report = {"identity_mode": "retained", "native_statuscode": 2}
        monitor.observe_identity(report, retained_media())
        self.assertNotIn("identity_retained_at_native_closure", report)
        report["native_statuscode"] = 4
        monitor.observe_identity(report, retained_media())
        self.assertTrue(report["identity_retained_at_native_closure"])
        self.assertEqual(len(report["identity_states"]), 1)
        monitor.observe_identity(report, retained_media("deleted"))
        self.assertEqual([state["state"] for state in report["identity_states"]], ["retained", "deleted"])

    def test_failed_cleanup_is_recorded_and_surfaced(self):
        report = {"identity_mode": "retained"}
        with self.assertRaisesRegex(ValueError, "cleanup failed"):
            monitor.observe_identity(report, retained_media("failed"))
        self.assertEqual(report["identity_cleanup_status"], "failed")


class CaseLinkObservationTests(unittest.TestCase):
    def test_ordinary_monitor_does_not_require_case_fields(self):
        report = {}
        monitor.observe_case_links(report, {})
        self.assertEqual(report, {})

    def test_missing_attributes_are_not_reported_as_unlinked_records(self):
        with self.assertRaisesRegex(ValueError, "omitted"):
            monitor.observe_case_links({"observe_case_links": True}, {})

    def test_customer_and_case_and_regarding_are_distinct(self):
        report = {"observe_case_links": True, "native_statuscode": 2, "media_phase": "active"}
        item = {
            "_msdyn_customer_value": WORKSTREAM, "_msdyn_issueid_value": None,
            "_regardingobjectid_value": WORKSTREAM,
            "_regardingobjectid_value@Microsoft.Dynamics.CRM.lookuplogicalname": "contact",
        }
        monitor.observe_case_links(report, item)
        monitor.observe_case_links(report, item)
        self.assertEqual(len(report["case_link_timeline"]), 1)
        first = report["case_link_timeline"][0]
        self.assertEqual(first["native_customer_id"], WORKSTREAM)
        self.assertIsNone(first["native_case_id"])
        self.assertEqual(first["regarding_record_type"], "contact")
        item["_msdyn_issueid_value"] = INSIGHT
        monitor.observe_case_links(report, item)
        report["representative_accepted_at"] = "2026-01-01T00:00:01Z"
        monitor.observe_case_links(report, item)
        self.assertEqual(len(report["case_link_timeline"]), 3)
        self.assertEqual(report["case_link_timeline"][-1]["native_case_id"], INSIGHT)
        self.assertTrue(report["case_link_timeline"][-1]["representative_accepted"])

    def test_case_cleared_after_acceptance_is_recorded_without_inventing_a_cause(self):
        report = {"observe_case_links": True}
        item = dict(zip(monitor.CASE_LINK_FIELDS, (WORKSTREAM, INSIGHT, None)))
        monitor.observe_case_links(report, item)
        report["representative_accepted_at"] = "2026-01-01T00:00:01Z"
        item["_msdyn_issueid_value"] = None
        monitor.observe_case_links(report, item)
        self.assertEqual([row["native_case_id"] for row in report["case_link_timeline"]], [INSIGHT, None])
        self.assertNotIn("cause", report)

    def test_regarding_record_type_and_history_bound_are_enforced(self):
        report = {"observe_case_links": True}
        item = dict(zip(monitor.CASE_LINK_FIELDS, (WORKSTREAM, None, INSIGHT)))
        with self.assertRaisesRegex(ValueError, "record-type"):
            monitor.observe_case_links(report, item)
        item["_regardingobjectid_value"] = None
        report["case_link_timeline"] = [{}] * 32
        with self.assertRaisesRegex(ValueError, "transition limit"):
            monitor.observe_case_links(report, item)

    def test_selected_case_receipt_is_compared_with_the_actual_native_reference(self):
        report = {"observe_case_links": True}
        item = dict(zip(monitor.CASE_LINK_FIELDS, (WORKSTREAM, None, None)))
        media = {
            "selected_case_id": INSIGHT, "selected_case_number": "CAS-001-TEST",
            "case_link_status": "failed", "case_link_error": "CRM_REQUEST_FAILED",
            "case_link_http_status": 412, "case_link_stage": "patch",
        }
        monitor.observe_case_links(report, item, media)
        self.assertFalse(report["case_link_timeline"][-1]["selected_case_matches_native"])
        self.assertEqual(report["case_link_timeline"][-1]["adapter_link_http_status"], 412)
        item["_msdyn_issueid_value"] = INSIGHT
        report["representative_accepted_at"] = "2026-01-01T00:00:01Z"
        monitor.observe_case_links(report, item, media)
        self.assertTrue(report["case_link_timeline"][-1]["selected_case_matches_native"])
        self.assertEqual(report["case_link_timeline"][-1]["adapter_link_status"], "failed")
        self.assertEqual(len(report["case_link_timeline"]), 2)

    def test_missing_selected_case_is_not_treated_as_a_failed_association(self):
        report = {"observe_case_links": True}
        item = dict(zip(monitor.CASE_LINK_FIELDS, (WORKSTREAM, None, None)))
        media = {"selected_case_id": "", "selected_case_number": "", "case_link_status": "not_selected"}
        monitor.observe_case_links(report, item, media)
        self.assertIsNone(report["case_link_timeline"][-1]["selected_case_matches_native"])
        with self.assertRaisesRegex(ValueError, "together"):
            monitor.observe_case_links(report, item, {**media, "selected_case_number": "CAS-001-TEST"})


class NativeMetricsReadTests(unittest.TestCase):
    def test_missing_row_does_not_claim_a_processing_failure(self):
        api = api_with_insight([])
        with patch.object(monitor.urllib.request, "urlopen") as opened:
            result = monitor.read_native_metrics(api, CALL)
        self.assertEqual(result["status"], "no_insight_record")
        opened.assert_not_called()

    def test_ambiguous_or_paginated_rows_are_not_guessed(self):
        for api in (
            api_with_insight([{"msdyn_conversationinsightid": INSIGHT}] * 2),
            api_with_insight(**{"@odata.nextLink": "https://example.invalid/next"}),
        ):
            with self.assertRaises(ValueError):
                monitor.read_native_metrics(api, CALL)

    def test_native_file_uses_exact_viewer_url_and_keeps_only_metrics(self):
        api = api_with_insight()
        payload = {"insights": METRICS, "participants": [{"private": "omitted"}]}
        with patch.object(monitor.urllib.request, "urlopen", return_value=response(payload)) as opened:
            result = monitor.read_native_metrics(api, CALL)
        self.assertEqual(
            opened.call_args.args[0].full_url,
            f"{api.org}/api/data/v9.0/msdyn_conversationinsights({INSIGHT})/msdyn_insights/$value",
        )
        self.assertEqual(result["metrics"], METRICS)
        self.assertTrue(result["positive_analyzed_duration"])
        self.assertNotIn("participants", result)
        self.assertNotIn("unit-test-only", json.dumps(result))

    def test_zero_duration_file_is_not_claimed_as_substantive_analysis(self):
        with patch.object(monitor.urllib.request, "urlopen", return_value=response({
            "insights": {**METRICS, "callDuration": 0},
        })):
            result = monitor.read_native_metrics(api_with_insight(), CALL)
        self.assertEqual(result["status"], "file_available")
        self.assertFalse(result["positive_analyzed_duration"])

    def test_only_404_is_reported_as_absent_output(self):
        for code in (404, 401, 403, 500):
            error = urllib.error.HTTPError("https://unit.test", code, "Unit test", {}, io.BytesIO())
            with self.subTest(code=code), patch.object(monitor.urllib.request, "urlopen", side_effect=error):
                if code == 404:
                    self.assertEqual(monitor.read_native_metrics(api_with_insight(), CALL)["status"], "file_absent")
                else:
                    with self.assertRaises(urllib.error.HTTPError):
                        monitor.read_native_metrics(api_with_insight(), CALL)

    def test_invalid_schema_or_values_are_not_success_shaped(self):
        for payload in (
            [], {}, {"insights": {}}, {"insights": {**METRICS, "callDuration": "100"}},
            {"insights": {**METRICS, "callDuration": float("nan")}},
            {"insights": {**METRICS, "callDuration": True}},
        ):
            with self.subTest(payload=payload), patch.object(
                monitor.urllib.request, "urlopen", return_value=response(payload),
            ), self.assertRaises(ValueError):
                monitor.read_native_metrics(api_with_insight(), CALL)


class SpeechRoleTagTests(unittest.TestCase):
    def api(self, messages, conversation_id=CALL):
        body = base64.b64encode(json.dumps([{
            "TranscriptMetadata": {"LiveWorkItemId": conversation_id},
            "Content": json.dumps(messages),
        }]).encode()).decode()
        return SimpleNamespace(get=MagicMock(side_effect=[
            {"value": [{"msdyn_transcriptid": INSIGHT}]},
            {"value": [{"documentbody": body}]},
        ]))

    def test_native_role_tags_are_observed_not_assigned(self):
        messages = [
            {"tags": "SpeechToText,FromCustomer,opaque", "fromUserId": "+12025550101", "content": "Private caller text"},
            {"tags": "SpeechToText,FromPrimaryAgent,FromBotAgent,opaque", "fromUserId": INSIGHT},
            {"tags": "SpeechToText,opaque", "fromUserId": "8:acs:unit-private-identity"},
        ]
        result = monitor.read_speech_role_tags(self.api(messages), CALL)
        self.assertEqual(result, {
            "status": "available",
            "groups": [
                {"source_kind": "acs", "role_tokens": [], "messages": 1},
                {"source_kind": "other", "role_tokens": ["FromPrimaryAgent", "FromBotAgent"], "messages": 1},
                {"source_kind": "phone", "role_tokens": ["FromCustomer"], "messages": 1},
            ],
        })
        text = json.dumps(result)
        for private in ("+12025550101", "unit-private-identity", "Private caller text", INSIGHT):
            self.assertNotIn(private, text)

    def test_pending_source_is_explicit(self):
        api = SimpleNamespace(get=MagicMock(return_value={"value": []}))
        self.assertEqual(monitor.read_speech_role_tags(api, CALL)["status"], "transcript_pending")
        api.get.side_effect = [{"value": [{"msdyn_transcriptid": INSIGHT}]}, {"value": []}]
        self.assertEqual(monitor.read_speech_role_tags(api, CALL)["status"], "source_messages_pending")

    def test_attachment_must_belong_to_exact_call(self):
        with self.assertRaisesRegex(ValueError, "does not match"):
            monitor.read_speech_role_tags(self.api([], conversation_id=INSIGHT), CALL)

    def test_unknown_tag_schema_is_not_silently_ignored(self):
        with self.assertRaisesRegex(ValueError, "invalid tags"):
            monitor.read_speech_role_tags(self.api([{"tags": ["FromCustomer"]}]), CALL)

    def test_empty_speech_is_not_a_completed_role_snapshot(self):
        self.assertEqual(monitor.read_speech_role_tags(self.api([]), CALL)["status"], "no_speech_messages")

    def test_missing_metadata_is_an_explicit_error(self):
        body = base64.b64encode(json.dumps([{"TranscriptMetadata": None, "Content": "[]"}]).encode()).decode()
        api = SimpleNamespace(get=MagicMock(side_effect=[
            {"value": [{"msdyn_transcriptid": INSIGHT}]},
            {"value": [{"documentbody": body}]},
        ]))
        with self.assertRaises(ValueError):
            monitor.read_speech_role_tags(api, CALL)


class IdentityFollowupTests(unittest.TestCase):
    def setUp(self):
        self.clock = 0.0
        self.report = {
            "conversation_id": CALL, "workstream_id": WORKSTREAM, "queue_id": QUEUE,
            "identity_mode": "retained", "status": "observation_limit_reached",
            "handover_ready_seen": True, "representative_accepted_at": "2026-01-01T00:00:01Z",
            "microphone_ready_seen": True, "audio_frames_seen": True,
        }
        self.api = SimpleNamespace(get=MagicMock(return_value={
            "statuscode": 4, "_msdyn_liveworkstreamid_value": WORKSTREAM,
            "_msdyn_cdsqueueid_value": QUEUE,
            "statuscode@OData.Community.Display.V1.FormattedValue": "Closed",
        }))
        self.timings = {
            "conversation_id": CALL, "origin_utc": "2026-01-01T00:00:00Z",
            "events": [
                {"stage": "voip_identity_revoke", "status": "ok", "elapsed_ms": 900},
                {"stage": "voip_identity_retained", "status": "revocation_requested", "elapsed_ms": 1000},
                {"stage": "voip_identity_delete", "status": "ok", "elapsed_ms": 901500, "duration_ms": 500},
            ],
        }
        self.output = Path("/tmp/native-identity-monitor-unit-output.json")

    def sleep(self, seconds):
        self.clock += seconds

    def run_followup(self, snapshots, timings=None, *, real_writes=False):
        states = iter(snapshots)

        def media(path, missing=False):
            if path.endswith("/timings"):
                return self.timings if timings is None else timings
            return next(states)

        with patch.object(monitor.time, "monotonic", side_effect=lambda: self.clock), patch.object(
            monitor.time, "sleep", side_effect=self.sleep,
        ), patch.object(monitor, "read_native_metrics", return_value={"status": "file_absent"}) as metric_reader, patch.object(
            monitor, "read_speech_role_tags", return_value={"status": "available", "groups": []},
        ), patch.object(
            monitor, "write_private", wraps=monitor.write_private if real_writes else None,
        ) as writer:
            monitor.follow_identity_cleanup(self.report, self.api, media, self.output)
        return metric_reader, writer

    def test_fully_observed_grace_and_deletion_are_separate_from_missing_metrics(self):
        metrics, writer = self.run_followup([retained_media(), retained_media("deleted")])
        self.assertTrue(self.report["identity_trial_valid"])
        self.assertEqual(self.report["call_window_status"], "observation_limit_reached")
        self.assertEqual(self.report["observed_identity_grace_ms"], 900000)
        self.assertEqual(self.report["identity_followup_status"], "deletion_confirmed")
        self.assertEqual(metrics.call_count, 2)
        self.assertEqual(writer.call_count, 2)
        self.assertEqual(
            [call.args[0].name for call in writer.call_args_list],
            [
                "native-identity-monitor-unit-output.snapshot-01.json",
                "native-identity-monitor-unit-output.snapshot-02.json",
            ],
        )
        self.assertEqual(
            [snapshot["identity_state_at_read"] for snapshot in self.report["post_call_metrics"]],
            ["retained", "deleted"],
        )
        self.assertTrue(all(snapshot["status"] == "file_absent" for snapshot in self.report["post_call_metrics"]))
        self.assertEqual(self.report["speech_role_tags"]["identity_state_at_read"], "retained")

    def test_real_writer_preserves_checkpoints_and_allows_final_report(self):
        with tempfile.TemporaryDirectory() as directory:
            self.output = Path(directory) / "trial.json"
            self.run_followup([retained_media(), retained_media("deleted")], real_writes=True)
            self.assertFalse(self.output.exists())
            first = Path(directory) / "trial.snapshot-01.json"
            second = Path(directory) / "trial.snapshot-02.json"
            first_content = first.read_bytes()
            self.assertEqual(json.loads(first_content)["identity_cleanup_status"], "retained")
            self.assertEqual(json.loads(second.read_text())["identity_cleanup_status"], "deleted")
            monitor.write_private(self.output, json.dumps(self.report) + "\n")
            self.assertTrue(json.loads(self.output.read_text())["identity_trial_valid"])
            self.assertEqual(first.read_bytes(), first_content)
            for path in (self.output, first, second):
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_early_shutdown_cleanup_does_not_count_as_full_grace(self):
        timings = copy.deepcopy(self.timings)
        timings["events"][-1]["elapsed_ms"] = 10500
        self.run_followup([retained_media(), retained_media("deleted")], timings)
        self.assertEqual(self.report["identity_followup_status"], "deletion_confirmed")
        self.assertFalse(self.report["identity_trial_valid"])

    def test_deletion_without_observed_retention_at_closure_is_incomplete(self):
        self.run_followup([retained_media("deleted")])
        self.assertFalse(self.report["identity_trial_valid"])

    def test_retained_identity_without_audio_is_not_a_valid_hybrid_trial(self):
        self.report["audio_frames_seen"] = False
        self.run_followup([retained_media(), retained_media("deleted")])
        self.assertFalse(self.report["identity_trial_valid"])

    def test_lost_or_mismatched_session_is_not_claimed_as_cleanup(self):
        for media in (None, retained_media(conversation_id=INSIGHT)):
            with self.subTest(media=media), self.assertRaises(ValueError):
                self.run_followup([media])
        self.assertFalse(self.report["identity_trial_valid"])

    def test_wrong_native_ownership_is_rejected_before_status_read(self):
        self.api.get.return_value["_msdyn_liveworkstreamid_value"] = INSIGHT
        with self.assertRaisesRegex(ValueError, "workstream or queue"):
            self.run_followup([])

    def test_end_without_identity_allocation_does_not_wait_out_a_grace(self):
        with self.assertRaisesRegex(ValueError, "without entering"):
            self.run_followup([retained_media("not_allocated", phase="stopped")])

    def test_followup_has_a_finite_deadline(self):
        self.sleep = lambda _: setattr(self, "clock", monitor.IDENTITY_FOLLOWUP_SECONDS + 1)
        with self.assertRaisesRegex(ValueError, "bounded follow-up"):
            self.run_followup([retained_media()])
        self.assertFalse(self.report["identity_trial_valid"])


class MonitorCommandTests(unittest.TestCase):
    def test_retention_followup_requires_explicit_retained_mode(self):
        args = [
            "monitor", "--tenant-id", CALL, "--org-url", "https://example.crm17.dynamics.com",
            "--voice-origin", "https://voice.example.azurecontainerapps.io",
            "--workstream-id", WORKSTREAM, "--queue-id", QUEUE,
            "--representative-id", INSIGHT, "--secrets-file", "unused.env",
            "--output", "/tmp/unused-native-monitor.json", "--follow-identity-cleanup",
        ]
        with patch("sys.argv", args), patch("sys.stderr", new=io.StringIO()), self.assertRaises(SystemExit) as error:
            monitor.main()
        self.assertEqual(error.exception.code, 2)

    def test_dry_run_does_not_authenticate_or_write(self):
        with tempfile.TemporaryDirectory() as directory:
            for limit, window in ((60, 210), (90, 210), (120, 600), (150, 210), (180, 600)):
                with self.subTest(limit=limit, window=window):
                    args = [
                        "monitor", "--tenant-id", CALL, "--org-url", "https://example.crm17.dynamics.com",
                        "--voice-origin", "https://voice.example.azurecontainerapps.io",
                        "--workstream-id", WORKSTREAM, "--queue-id", QUEUE,
                        "--representative-id", INSIGHT, "--secrets-file", "unused.env",
                        "--output", str(Path(directory) / "report.json"),
                        "--caller-limit-seconds", str(limit), "--identity-mode", "retained", "--follow-identity-cleanup",
                        "--dial-window-seconds", str(window),
                    ]
                    with patch("sys.argv", args), patch.object(monitor, "Dataverse") as api, patch.object(
                        monitor, "write_private",
                    ) as writer, patch("sys.stdout", new=io.StringIO()):
                        monitor.main()
                    api.assert_not_called()
                    writer.assert_not_called()

    def test_unreviewed_caller_limits_are_rejected_before_authentication(self):
        for limit in (0, 59, 61, 181):
            with self.subTest(limit=limit):
                args = [
                    "monitor", "--tenant-id", CALL, "--org-url", "https://example.crm17.dynamics.com",
                    "--voice-origin", "https://voice.example.azurecontainerapps.io",
                    "--workstream-id", WORKSTREAM, "--queue-id", QUEUE,
                    "--representative-id", INSIGHT, "--secrets-file", "unused.env",
                    "--output", "/tmp/unused-native-monitor.json",
                    "--caller-limit-seconds", str(limit), "--arm",
                ]
                with patch("sys.argv", args), patch.object(monitor, "Dataverse") as api, patch(
                    "sys.stderr", new=io.StringIO(),
                ), self.assertRaises(SystemExit) as error:
                    monitor.main()
                self.assertEqual(error.exception.code, 2)
                api.assert_not_called()


if __name__ == "__main__":
    unittest.main()
