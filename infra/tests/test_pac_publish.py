import contextlib
import copy
import http.client
import io
import json
import re
import shlex
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch
from uuid import NAMESPACE_URL, uuid5

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "infra/scripts"))
import pac_publish as publish

ORG = "https://example.crm17.dynamics.com"
TENANT = str(uuid5(NAMESPACE_URL, "synthetic-publish-tenant"))
BOT = str(uuid5(NAMESPACE_URL, "synthetic-publish-bot"))
BEFORE, AFTER = "2026-11-01T10:00:00Z", "2026-11-17T10:05:00Z"
ADDRESS = "@".join(("maker", "example.com"))


def bot(publishedon=BEFORE, status=None, at=None, *, statecode=0, statuscode=1, raw=None):
    operation = {
        "operationStart": at, "operationEnd": at, "status": status,
        "diagnosticDetails": [{
            "$kind": "ComponentDiagnosticResult", "componentId": str(uuid5(NAMESPACE_URL, "synthetic-topic")),
            "diagnosticList": [{"$kind": "ListPropertyItemError", "propertyName": "Synonyms",
                                "errorCode": "SynonymsNotUnique", "index": 0}],
        }] if status == "Failed" else [],
    }
    sync = raw if raw is not None else json.dumps({"lastFinishedPublishOperation": operation}) if status else None
    return {"botid": BOT, "name": "Routing agent", "schemaname": "new_routingAgent", "statecode": statecode,
            "statuscode": statuscode, "publishedon": publishedon, "synchronizationstatus": sync}


class Dataverse:
    """Returns the scripted bot rows in order, then keeps returning the last one."""

    def __init__(self, *rows):
        self.rows, self.reads = list(rows), 0

    def get(self, path, query=None):
        assert path == f"bots({BOT})" and query == {"$select": publish.SELECT}
        self.reads += 1
        row = self.rows[min(self.reads, len(self.rows)) - 1]
        if isinstance(row, Exception):
            raise row
        return copy.deepcopy(row)


class Clock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class PacPublishTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.receipt = Path(directory.name) / "publish-receipt"
        self.clock, self.pac_calls = Clock(), []

    def run_main(self, api, *extra, apply=True, exit_code=0, stdout="", raises=None):
        def pac(command, **kwargs):
            self.pac_calls.append(command)
            if raises:
                raise raises
            return subprocess.CompletedProcess(command, exit_code, stdout, "")
        output = io.StringIO()
        arguments = ["--tenant-id", TENANT, "--org-url", ORG, "--bot-id", BOT, "--receipt-dir", str(self.receipt),
                     *(["--apply", "--confirm-organization", ORG] if apply else []), *extra]
        with patch.object(publish.native, "Dataverse", return_value=api), \
                patch.object(publish.shutil, "which", return_value="/opt/pac/pac"), \
                patch.object(publish.subprocess, "run", side_effect=pac), \
                patch.object(publish, "monotonic", self.clock.monotonic), \
                patch.object(publish, "sleep", self.clock.sleep), contextlib.redirect_stdout(output):
            code = publish.main(arguments)
        return code, json.loads(output.getvalue().splitlines()[-1])

    def events(self):
        return [json.loads(path.read_text()) for path in sorted(self.receipt.glob("event-*.json"))]

    def test_exit_zero_with_a_server_failure_is_reported_failed(self):
        api = Dataverse(bot(), bot(status="Failed", at="2026-11-17T10:04:00Z"))
        code, report = self.run_main(api, stdout=f"Connected as {ADDRESS}\nFailed to publish the copilot.\n")
        self.assertEqual((code, report["status"], report["pac"]["exit_code"]), (1, "failed", 0))
        self.assertTrue(report["pac"]["reported_failure"])
        self.assertFalse(report["pac_exit_code_matched_readback"])
        self.assertEqual(report["after"]["last_publish"]["diagnostics"][0]["error_code"], "SynonymsNotUnique")
        self.assertIn("--retry-change", report["next"])
        self.assertEqual(self.pac_calls, [["/opt/pac/pac", "copilot", "publish", "--bot", BOT, "--environment", ORG]])
        self.assertNotIn(ADDRESS, json.dumps(report) + json.dumps(self.events()))
        self.assertEqual([event["kind"] for event in self.events()], ["attempt", "pac", "readback"])
        self.assertEqual(self.receipt.stat().st_mode & 0o777, 0o700)
        self.assertTrue(all(path.stat().st_mode & 0o777 == 0o600 for path in self.receipt.iterdir()))

    def test_exit_one_with_a_server_success_is_published(self):
        earlier_failure = bot(status="Failed", at="2026-11-10T09:00:00Z")
        api = Dataverse(earlier_failure, earlier_failure, bot(AFTER, status="Succeeded", at="2026-11-17T10:04:00Z"))
        code, report = self.run_main(api, exit_code=1, stdout="Error: the request timed out.\n")
        self.assertEqual((code, report["status"], report["published_by_this_run"]), (0, "published", True))
        self.assertFalse(report["pac_exit_code_matched_readback"])
        self.assertEqual(self.clock.now, publish.POLL_SECONDS)

    def test_pac_timeout_is_decided_by_the_readback(self):
        api = Dataverse(bot(), bot(AFTER, status="Succeeded", at="2026-11-17T10:04:00Z"))
        expired = subprocess.TimeoutExpired("pac", publish.PAC_TIMEOUT_SECONDS, output=b"Publishing...")
        code, report = self.run_main(api, raises=expired)
        self.assertEqual((code, report["status"], report["pac"]["timed_out"]), (0, "published", True))
        self.assertNotIn("pac_exit_code_matched_readback", report)

    def test_bounded_wait_ends_unconfirmed_and_a_rerun_only_reads_back(self):
        code, report = self.run_main(Dataverse(bot()), "--timeout-seconds", "60", stdout="Published.\n")
        self.assertEqual((code, report["status"]), (1, "unconfirmed"))
        self.assertEqual(self.clock.now, 60)
        self.assertIn("only reads back", report["next"])
        with self.assertRaisesRegex(ValueError, "unconfirmed"):
            self.run_main(Dataverse(bot()), "--retry-change", "Signed in to PAC again")
        late = Dataverse(bot(AFTER, status="Succeeded", at="2026-11-17T10:09:00Z"))
        code, report = self.run_main(late)
        self.assertEqual((code, report["status"], report["published_by_this_run"]), (0, "published", False))
        self.assertEqual(len(self.pac_calls), 1)
        self.assertTrue(self.events()[-1]["reconciled"])

    def test_one_retry_needs_a_named_change_then_the_script_stops(self):
        first = bot(status="Failed", at="2026-11-17T10:04:00Z")
        self.run_main(Dataverse(bot(), first), stdout="Failed to publish")
        with self.assertRaisesRegex(ValueError, "retry once with --retry-change"):
            self.run_main(Dataverse(first))
        second = bot(status="Failed", at="2026-11-17T10:20:00Z")
        code, report = self.run_main(Dataverse(first, second), "--retry-change", "Removed the duplicate synonym")
        self.assertEqual((code, report["status"], report["attempt"]), (1, "failed", 2))
        self.assertIn("Stop and ask the owner", report["next"])
        with self.assertRaisesRegex(ValueError, "both allowed attempts"):
            self.run_main(Dataverse(second), "--retry-change", "Another untested change")
        self.assertEqual(len(self.pac_calls), 2)
        self.assertEqual([event["kind"] for event in self.events()], ["attempt", "pac", "readback"] * 2)
        self.assertEqual(self.events()[3]["retry_change"], "Removed the duplicate synonym")

    def test_late_success_after_a_failure_is_recorded_without_publishing_again(self):
        self.run_main(Dataverse(bot(), bot(status="Failed", at="2026-11-17T10:04:00Z")))
        code, report = self.run_main(Dataverse(bot(AFTER, status="Succeeded", at="2026-11-17T10:30:00Z")))
        self.assertEqual((code, report["status"], report["published_by_this_run"]), (0, "published", False))
        self.assertEqual(len(self.pac_calls), 1)

    def test_success_needs_both_a_new_succeeded_operation_and_an_advanced_publishedon(self):
        for name, after in (
            ("unknown shape", bot(AFTER, raw=json.dumps({"publishState": "Done"}))),
            ("publishedon unchanged", bot(status="Succeeded", at="2026-11-17T10:04:00Z")),
            ("unknown status", bot(AFTER, status="Completed", at="2026-11-17T10:04:00Z")),
        ):
            with self.subTest(name):
                self.setUp()
                code, report = self.run_main(Dataverse(bot(), after), "--timeout-seconds", "30")
                self.assertEqual((code, report["status"]), (1, "unconfirmed"))

    def test_an_unrecognized_status_is_shown_as_read(self):
        code, report = self.run_main(Dataverse(bot(), bot(AFTER, raw=json.dumps({"publishState": "Done"}))),
                                     "--timeout-seconds", "30")
        self.assertEqual(code, 1)
        self.assertIsNone(report["after"]["last_publish"])
        self.assertIn("publishState", report["after"]["synchronizationstatus_excerpt"])

    def test_readback_denial_stops_waiting_and_keeps_request_ids(self):
        headers = http.client.HTTPMessage()
        headers["REQ_ID"] = "req-1"
        denied = urllib.error.HTTPError(f"{ORG}/api/data/v9.2/bots({BOT})", 403, "Forbidden", headers, io.BytesIO(
            b'{"error":{"code":"0x80040220","message":"Principal user is missing prvReadbot privilege."}}'))
        code, report = self.run_main(Dataverse(bot(), denied))
        self.assertEqual((code, report["status"], self.clock.now), (1, "unconfirmed", 0))
        self.assertEqual(report["readback_error"]["request_ids"], {"REQ_ID": "req-1"})
        self.assertEqual(report["readback_error"]["error_code"], "0x80040220")

    def test_preview_and_guards_never_run_pac(self):
        code, report = self.run_main(Dataverse(bot()), apply=False)
        self.assertEqual((code, report["status"], report["cloud_changes"]), (0, "plan_only", False))
        self.assertFalse(self.receipt.exists())
        for extra, message in (
            (["--confirm-organization", ORG], "only with --apply"),
            (["--retry-change", "Removed the duplicate synonym"], "only with --apply"),
        ):
            with self.subTest(extra=extra), self.assertRaisesRegex(ValueError, message):
                self.run_main(Dataverse(bot()), *extra, apply=False)
        with self.assertRaisesRegex(ValueError, "--confirm-organization matching"):
            self.run_main(Dataverse(bot()), "--apply", apply=False)
        with self.assertRaisesRegex(ValueError, "only after a failed attempt"):
            self.run_main(Dataverse(bot()), "--retry-change", "Removed the duplicate synonym")
        with self.assertRaisesRegex(ValueError, "Active and Provisioned"):
            self.run_main(Dataverse(bot(statecode=1, statuscode=2)))
        self.assertEqual(self.pac_calls, [])
        output = io.StringIO()
        with patch.object(publish.native, "Dataverse", return_value=Dataverse(bot())), \
                contextlib.redirect_stdout(output), self.assertRaises(SystemExit) as stopped:
            publish.cli(["--tenant-id", TENANT, "--org-url", ORG, "--bot-id", BOT,
                         "--receipt-dir", str(self.receipt), "--apply"])
        self.assertEqual(stopped.exception.code, 1)
        self.assertEqual(json.loads(output.getvalue())["status"], "failed")

    def test_receipt_for_another_agent_is_refused(self):
        self.run_main(Dataverse(bot(), bot(status="Failed", at="2026-11-17T10:04:00Z")))
        other = str(uuid5(NAMESPACE_URL, "synthetic-other-bot"))
        with patch.object(publish.native, "Dataverse") as dataverse, \
                self.assertRaisesRegex(ValueError, "another organization or agent"):
            publish.main(["--tenant-id", TENANT, "--org-url", ORG, "--bot-id", other,
                          "--receipt-dir", str(self.receipt), "--apply", "--confirm-organization", ORG])
        dataverse.assert_not_called()

    def test_documented_commands_match_the_real_cli(self):
        values = {"$TENANT_ID": TENANT, "$ORG_URL": ORG, "$BOT_ID": BOT, "$WORK/publish-receipt": str(self.receipt)}
        applied = []
        for document in (ROOT / "docs/dynamics-native-voice").glob("*.md"):
            source = document.read_text().replace("\\\n", " ")
            for command in re.findall(r"(?m)^python infra/scripts/pac_publish\.py ([^\n]+)", source):
                with self.subTest(document=document.name, command=command):
                    parsed = publish.parser().parse_args([values.get(arg, arg) for arg in shlex.split(command)])
                    applied.append(parsed.apply and parsed.confirm_organization == parsed.org_url)
        self.assertIn(True, applied)
        self.assertIn(False, applied)


if __name__ == "__main__":
    unittest.main()
