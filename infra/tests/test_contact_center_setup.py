import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "infra/scripts"))
import contact_center_setup as setup
import stage_native_media_topic as topics


ORG = "https://example.crm17.dynamics.com"
TENANT = "11111111-1111-4111-8111-111111111111"
SUBSCRIPTION = "22222222-2222-4222-8222-222222222222"
OTHER_TENANT = "33333333-3333-4333-8333-333333333333"
NEXT_LINK = f"https://management.azure.com/subscriptions/{SUBSCRIPTION}/resources?$skiptoken=abc"


@contextmanager
def private_test_directory():
    # Private outputs live outside the checkout; the "checkout" subfolder stands in for the repository.
    with tempfile.TemporaryDirectory(prefix="contact-center-setup-test-") as name:
        directory = Path(name).resolve()
        if directory.is_relative_to(ROOT):
            raise RuntimeError("Point TMPDIR outside the checkout; tests must not write into the repository.")
        checkout = directory / "checkout"
        checkout.mkdir(mode=0o700)
        with patch.object(topics, "ROOT", checkout):
            yield directory


class FakeDataverse:
    """Serve one (provisioned, telephony, voice status) tuple per poll; 'error' makes the channel read fail."""

    def __init__(self, timeline, *, journal=None, post_error=None):
        self.org, self.tenant = ORG, TENANT
        self.timeline, self.index = list(timeline), 0
        self.journal, self.post_error = journal, post_error
        self.posts, self.reads = [], []

    def get(self, path, query=None):
        self.reads.append(path)
        provisioned, telephony, status = self.timeline[min(self.index, len(self.timeline) - 1)]
        settings = {"msdyn_IsCCaaSProvisioned": provisioned, "msdyn_IsCCaaSTelephonyProvisioned": telephony}
        for name, value in settings.items():
            if path == f"RetrieveSetting(SettingName='{name}')":
                return {"SettingDetail": {"Name": name, "Value": str(value), "DataType": 0}}
        if path == "CCaaS_GetContactCenterChannels()":
            if status == "error":
                raise urllib.error.HTTPError(ORG, 404, "Not Found", {}, None)
            channels = [{"ChannelType": 1, "Status": 1001}]
            if status is not None:
                channels.append({"ChannelType": 5, "Status": status})
            return {"ContactCenterChannels": json.dumps(channels)}
        raise AssertionError("Unexpected Dataverse read: " + path)

    def request(self, path, *, method="GET", body=None, headers=None, raw=False):
        if self.journal is not None:
            assert (self.journal / "intent.json").is_file(), "The intent must be journaled before the POST."
        self.posts.append((path, method, body))
        if self.post_error:
            raise self.post_error
        return {}


class FakeTime:
    def __init__(self, api):
        self.api, self.now, self.sleeps = api, 0.0, []

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds
        self.api.index += 1

    def clock(self):
        return self.now


class VoiceChannelTests(unittest.TestCase):
    def setUp(self):
        manager = private_test_directory()
        self.root = manager.__enter__()
        self.addCleanup(manager.__exit__, None, None, None)
        self.directory = self.root / "voice-channel"

    def run_voice(self, api, *, apply=False, wait_minutes=30, confirm=None):
        args = SimpleNamespace(
            tenant_id=TENANT, org_url=ORG, output_dir=self.directory, apply=apply,
            confirm_organization=confirm if confirm is not None else (ORG if apply else None), wait_minutes=wait_minutes,
        )
        api.journal = self.directory
        timer = FakeTime(api)
        with redirect_stdout(io.StringIO()) as output, redirect_stderr(io.StringIO()):
            code = setup.voice_channel(args, api, sleep=timer.sleep, clock=timer.clock)
        return code, json.loads(output.getvalue()), timer

    def test_preview_plans_the_create_request_and_writes_nothing(self):
        api = FakeDataverse([(False, False, None)])
        code, result, _ = self.run_voice(api)
        self.assertEqual(code, 0)
        self.assertEqual(result["status"], "planned")
        self.assertEqual(result["planned_request"], {
            "method": "POST", "action": "CCaaS_CreateContactCenter", "body": {"Channels": "[5]"},
        })
        self.assertEqual(api.posts, [])
        self.assertFalse(self.directory.exists())

    def test_apply_journals_then_creates_once_and_polls_until_voice_is_ready(self):
        api = FakeDataverse([(False, False, None), (True, False, 2101), (True, False, 2101), (True, True, 1001)])
        code, result, timer = self.run_voice(api, apply=True)
        self.assertEqual(code, 0)
        self.assertEqual(result["status"], "voice_ready")
        self.assertEqual(api.posts, [("CCaaS_CreateContactCenter", "POST", {"Channels": "[5]"})])
        self.assertEqual(timer.sleeps, [60, 60, 60])
        self.assertIn("US toll-free trial number", result["next"])
        self.assertIn(" pipeline ", result["then_run"])
        intent = json.loads((self.directory / "intent.json").read_text())
        self.assertEqual((intent["action"], intent["body"]), ("CCaaS_CreateContactCenter", {"Channels": "[5]"}))
        receipt = json.loads(Path(result["receipt"]).read_text())
        self.assertEqual([poll["voice_channel_status"] for poll in receipt["poll_history"]], [2101, 2101, 1001])
        if os.name != "nt":
            self.assertEqual(self.directory.stat().st_mode & 0o777, 0o700)
            self.assertTrue(all(path.stat().st_mode & 0o777 == 0o600 for path in self.directory.iterdir()))

    def test_existing_contact_center_gets_only_the_voice_enable_operation(self):
        api = FakeDataverse([(True, False, None), (True, True, 1001)])
        code, result, _ = self.run_voice(api, apply=True)
        self.assertEqual(code, 0)
        [(action, method, body)] = api.posts
        self.assertEqual((action, method), ("CCaaS_UpdateContactCenterChannels", "POST"))
        self.assertEqual(list(body), ["ChannelOperations"])
        self.assertEqual(json.loads(body["ChannelOperations"]), [{"Channel": 5, "Operation": 1}])

    def test_timeout_stops_with_admin_center_step_and_rerun_never_resends(self):
        api = FakeDataverse([(False, False, None), (True, False, 2101), (True, False, 2101)])
        code, result, timer = self.run_voice(api, apply=True, wait_minutes=2)
        self.assertEqual(code, 1)
        self.assertEqual(timer.sleeps, [60, 60])
        self.assertEqual(result["status"], "needs_human")
        self.assertFalse(result["request_resent"])
        self.assertIn("Manage channels", result["ask_owner"])
        self.assertIn("2 minutes", result["stopped_because"])
        self.assertIn(str(self.directory), result["then_run"])
        rerun = FakeDataverse([(True, False, 2101), (True, True, 1001)])
        code, result, _ = self.run_voice(rerun, apply=True)
        self.assertEqual((code, result["status"]), (0, "voice_ready"))
        self.assertEqual(rerun.posts, [])
        self.assertEqual(result["journal"]["outcome"], "accepted")

    def test_rejected_post_is_recorded_and_never_sent_again(self):
        error = urllib.error.HTTPError(ORG, 400, "Bad Request", {}, None)
        api = FakeDataverse([(False, False, None)], post_error=error)
        code, result, timer = self.run_voice(api, apply=True)
        self.assertEqual(code, 1)
        self.assertEqual(len(api.posts), 1)
        self.assertEqual(timer.sleeps, [])
        self.assertIn("HTTP 400", result["stopped_because"])
        record = json.loads((self.directory / "post-result.json").read_text())
        self.assertEqual((record["outcome"], record["http_status"]), ("rejected", 400))
        for apply in (True, False):
            with self.subTest(apply=apply):
                rerun = FakeDataverse([(False, False, None)])
                code, result, _ = self.run_voice(rerun, apply=apply)
                self.assertEqual((code, result["status"], rerun.posts), (1, "needs_human", []))
                self.assertIn("Customer Support > Channels > Manage channels", result["ask_owner"])

    def test_unanswered_post_is_uncertain_and_a_rerun_only_polls(self):
        api = FakeDataverse([(False, False, None)], post_error=TimeoutError())
        code, result, _ = self.run_voice(api, apply=True)
        self.assertEqual((code, result["post"]["http_status"]), (1, None))
        self.assertEqual(json.loads((self.directory / "post-result.json").read_text())["outcome"], "uncertain")
        rerun = FakeDataverse([(False, False, 2101), (True, True, 1001)])
        code, result, timer = self.run_voice(rerun, apply=True)
        self.assertEqual((code, rerun.posts, timer.sleeps), (0, [], [60]))

    def test_failed_status_stops_polling_without_resending(self):
        api = FakeDataverse([(False, False, None), (True, False, 2101), (True, False, 2102)])
        code, result, timer = self.run_voice(api, apply=True)
        self.assertEqual((code, len(api.posts), timer.sleeps), (1, 1, [60, 60]))
        self.assertIn("2102", result["stopped_because"])
        self.assertEqual(result["readback"]["voice_channel_state"], "failed")

    def test_ready_or_in_progress_voice_sends_nothing(self):
        for timeline, status, sleeps in (
            ([(True, True, 1001)], "voice_ready", []),
            ([(False, False, 2101), (True, True, 1001)], "voice_ready", [60]),
        ):
            with self.subTest(timeline=timeline):
                api = FakeDataverse(timeline)
                code, result, timer = self.run_voice(api, apply=True)
                self.assertEqual((code, result["status"], api.posts, timer.sleeps), (0, status, [], sleeps))

    def test_unreadable_channels_after_provisioning_stop_for_a_human(self):
        for apply in (False, True):
            with self.subTest(apply=apply):
                api = FakeDataverse([(True, False, "error")])
                code, result, _ = self.run_voice(api, apply=apply)
                self.assertEqual((code, result["status"], api.posts), (1, "needs_human", []))
                self.assertEqual(result["readback"]["voice_channel_state"], "unreadable")

    def test_apply_needs_the_exact_organization_before_any_read(self):
        api = MagicMock(org=ORG, tenant=TENANT)
        for apply, confirm, message in (
            (True, "https://other.crm17.dynamics.com", "requires --confirm-organization"),
            (False, ORG, "only with --apply"),
        ):
            args = SimpleNamespace(tenant_id=TENANT, org_url=ORG, output_dir=self.directory, apply=apply,
                                   confirm_organization=confirm, wait_minutes=30)
            with self.subTest(apply=apply), self.assertRaisesRegex(ValueError, message):
                setup.voice_channel(args, api)
        api.get.assert_not_called()
        api.request.assert_not_called()

    def test_journal_from_another_organization_is_refused(self):
        self.directory.mkdir(mode=0o700)
        setup.write_private(self.directory / "intent.json", json.dumps({
            "kind": "voice-channel", "org_url": "https://other.crm17.dynamics.com", "tenant_id": TENANT,
        }))
        api = FakeDataverse([(False, False, None)])
        with self.assertRaisesRegex(ValueError, "another organization"):
            self.run_voice(api, apply=True)
        self.assertEqual(api.posts, [])

    def test_channel_list_parsing(self):
        self.assertEqual(setup.voice_status({"ContactCenterChannels": '[{"ChannelType":"5","Status":"2101"}]'}), 2101)
        self.assertIsNone(setup.voice_status({"ContactCenterChannels": '[{"ChannelType":1,"Status":1001}]'}))
        for document in (
            {"ContactCenterChannels": '[{"ChannelType":5,"Status":2101},{"ChannelType":5,"Status":1001}]'},
            {"ContactCenterChannels": '{"ChannelType":5}'}, {},
        ):
            with self.subTest(document=document), self.assertRaises(ValueError):
                setup.voice_status(document)


class PipelineTests(unittest.TestCase):
    def run_gate(self, rows, org=ORG):
        api = MagicMock(org=org, tenant=TENANT)
        api.get.return_value = {"value": rows}
        args = SimpleNamespace(tenant_id=TENANT, org_url=org)
        with redirect_stdout(io.StringIO()) as output:
            code = setup.pipeline(args, api)
        api.get.assert_called_once_with("msdyn_ocliveworkitems", {
            "$select": "msdyn_infracreatedby,createdon", "$filter": "msdyn_channel eq '192440000'",
            "$orderby": "createdon desc", "$top": "5",
        })
        return code, json.loads(output.getvalue())

    def test_newest_elastic_conversation_passes(self):
        code, result = self.run_gate([
            {"createdon": "2026-11-17T09:00:00Z", "msdyn_infracreatedby": "Elastic"},
            {"createdon": "2026-11-16T09:00:00Z", "msdyn_infracreatedby": "CTQM3"},
        ])
        self.assertEqual((code, result["status"], result["latest_pipeline"]), (0, "elastic", "Elastic"))
        self.assertEqual(result["organization_host"], "example.crm17.dynamics.com")
        self.assertEqual(result["region_for_information"], "Switzerland (crm17)")

    def test_any_other_newest_value_blocks_whatever_the_region(self):
        for org, rows in (
            (ORG, [{"createdon": "2026-11-17T09:00:00Z", "msdyn_infracreatedby": "CTQM3"},
                   {"createdon": "2026-11-16T09:00:00Z", "msdyn_infracreatedby": "Elastic"}]),
            ("https://example.crm3.dynamics.com", [{"createdon": "2026-11-17T09:00:00Z", "msdyn_infracreatedby": "elastic"}]),
            (ORG, [{"createdon": "2026-11-17T09:00:00Z", "msdyn_infracreatedby": None}]),
        ):
            with self.subTest(org=org, rows=rows):
                code, result = self.run_gate(rows, org)
                self.assertEqual((code, result["status"]), (1, "blocked"))
                self.assertEqual(result["latest_pipeline"], rows[0]["msdyn_infracreatedby"])
        self.assertEqual(result["region_for_information"], "Switzerland (crm17)")

    def test_no_voice_conversation_asks_for_one_trial_call(self):
        code, result = self.run_gate([])
        self.assertEqual((code, result["status"]), (1, "no_voice_conversations"))
        self.assertIn("one call to the trial number", result["next"])


class EligibilityTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.billing = {
            "billingAccountAgreementType": "MicrosoftCustomerAgreement", "billingAccountStatus": "Active",
            "billingAccountSoldToCountry": "GB", "billingCountry": None, "billingProfileDisplayName": "private",
        }
        self.pages = [
            {"value": [{
                "id": f"/subscriptions/{SUBSCRIPTION}/resourceGroups/rg-old/providers/Microsoft.Communication/CommunicationServices/acs-old",
                "name": "acs-old", "type": "Microsoft.Communication/CommunicationServices", "location": "global",
                "createdTime": "2025-03-01T10:00:00.1234567Z",
            }], "nextLink": NEXT_LINK},
            {"value": [{
                "id": f"/subscriptions/{SUBSCRIPTION}/resourceGroups/rg-new/providers/Microsoft.Communication/CommunicationServices/acs-new",
                "name": "acs-new", "type": "Microsoft.Communication/communicationServices", "location": "global",
                "createdTime": "2026-11-01T10:00:00Z",
            }]},
        ]
        self.tenant = TENANT

    def fake_az(self, command, **kwargs):
        self.calls.append(command)
        self.assertEqual(command[0], "az")
        if command[1:3] == ["account", "show"]:
            payload = {"tenantId": self.tenant, "user": {"name": "PRIVATE-PRINCIPAL"}}
        elif "billingProperty" in command[command.index("--url") + 1]:
            payload = {"properties": self.billing}
        else:
            payload = self.pages.pop(0)
        return subprocess.CompletedProcess(command, 0, json.dumps(payload), "")

    def run_check(self, cutoff="2026-10-23"):
        args = SimpleNamespace(tenant_id=TENANT, subscription_id=SUBSCRIPTION, acs_cutoff=cutoff)
        with patch.object(setup.subprocess, "run", side_effect=self.fake_az), redirect_stdout(io.StringIO()) as output:
            code = setup.eligibility(args)
        return code, json.loads(output.getvalue())

    def test_sold_to_country_and_acs_creation_times_are_reported(self):
        code, result = self.run_check()
        self.assertEqual(code, 0)
        self.assertEqual(result["billing"], {
            "billingAccountAgreementType": "MicrosoftCustomerAgreement", "billingAccountStatus": "Active",
            "billingAccountSoldToCountry": "GB",
        })
        billing_url = self.calls[1][self.calls[1].index("--url") + 1]
        self.assertIn("/providers/Microsoft.Billing/billingProperty/default?", billing_url)
        self.assertIn("api-version=2024-04-01", billing_url)
        self.assertIn("includeBillingCountry=true", billing_url)
        resources_url = self.calls[2][self.calls[2].index("--url") + 1]
        self.assertIn("$filter=resourceType%20eq%20'Microsoft.Communication/CommunicationServices'", resources_url)
        self.assertIn("$expand=createdTime", resources_url)
        self.assertEqual(self.calls[3][self.calls[3].index("--url") + 1], NEXT_LINK)
        self.assertEqual(
            [(row["name"], row["resource_group"], row["created_before_cutoff"]) for row in result["acs_resources"]],
            [("acs-old", "rg-old", True), ("acs-new", "rg-new", False)],
        )
        self.assertTrue(result["acs_resource_created_before_cutoff"])
        self.assertIn("acs-retirement-and-breaking-changes-guide", result["compare_with"]["acs_retirement_guide"])
        self.assertIn("sub-eligibility-number-capability", result["compare_with"]["phone_number_eligibility"])

    def test_missing_sold_to_country_is_incomplete_and_never_falls_back_to_billing_country(self):
        self.billing["billingAccountSoldToCountry"] = None
        self.billing["billingCountry"] = "GB"
        self.pages = [{"value": []}]
        code, result = self.run_check()
        self.assertEqual((code, result["status"]), (1, "incomplete"))
        self.assertIsNone(result["billing"]["billingAccountSoldToCountry"])
        self.assertNotIn("billingCountry", result["billing"])
        self.assertFalse(result["acs_resource_created_before_cutoff"])
        self.assertTrue(any("can't acquire phone numbers" in finding for finding in result["findings"]))

    def test_wrong_tenant_stops_before_billing_reads(self):
        self.tenant = OTHER_TENANT
        with self.assertRaisesRegex(ValueError, "isn't in --tenant-id"):
            self.run_check()
        self.assertEqual(len(self.calls), 1)

    def test_cli_errors_report_codes_without_personal_details(self):
        stderr = 'ERROR: Forbidden({"error":{"code":"AuthorizationFailed","message":"PRIVATE-PRINCIPAL has no access"}})'
        with patch.object(setup.subprocess, "run", return_value=subprocess.CompletedProcess([], 1, "", stderr)):
            with self.assertRaises(RuntimeError) as raised:
                setup.az_json("account", "show")
        self.assertIn("AuthorizationFailed", str(raised.exception))
        self.assertNotIn("PRIVATE-PRINCIPAL", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
