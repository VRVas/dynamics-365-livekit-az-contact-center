import contextlib
import copy
import io
import json
import re
import shlex
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import native_runtime_settings as runtime
import stage_native_media_topic as topics


SUBSCRIPTION = "55555555-5555-4555-8555-555555555555"
WORKSTREAM_ID = "33333333-3333-4333-8333-333333333333"
APP = "ca-voice"
IMAGE = "registry.example.azurecr.io/voice:reviewed"
NUMBER = "+12025550101"
ACS_ID = f"/subscriptions/{SUBSCRIPTION}/resourceGroups/native-rg/providers/Microsoft.Communication/communicationServices/native-acs"


GUIDE = Path(__file__).resolve().parents[2] / "docs/dynamics-native-voice/operations.md"


def documented(script, values):
    source = GUIDE.read_text().replace("\\\n", " ")
    commands = re.findall(rf"(?m)^python infra/scripts/{re.escape(script)} ([^\n#]+)", source)
    return [[re.sub(r"\$(\w+)", lambda match: values[match[1]], token) for token in shlex.split(command)] for command in commands]


@contextlib.contextmanager
def private_test_directory():
    # Temporary folders stay outside the checkout; a fake checkout models the private-output boundary.
    with tempfile.TemporaryDirectory(prefix="native-runtime-settings-test-") as name:
        directory = Path(name).resolve()
        checkout = directory / "checkout"
        checkout.mkdir(mode=0o700)
        with patch.object(topics, "ROOT", checkout), patch.object(runtime, "HEALTH_INTERVAL", 0):
            yield directory


class FakeContainerApps:
    def __init__(self):
        self.env = [
            {"name": "ACS_SOURCE_NUMBER", "value": ""},
            {"name": "ACS_AUTO_CONFERENCE_ENABLED", "value": "False"},
            {"name": "DYNAMICS_NATIVE_MEDIA_ENABLED", "value": "True"},
            {"name": "DYNAMICS_NATIVE_MEDIA_TRANSPORT", "value": "voip"},
            {"name": "DYNAMICS_NATIVE_MEDIA_FAST_START", "value": "False"},
            {"name": "DYNAMICS_NATIVE_MEDIA_WORKSTREAM_ID", "value": WORKSTREAM_ID},
            {"name": "DYNAMICS_NATIVE_PROBE_RESOURCE_ID", "value": ACS_ID},
            {"name": "DYNAMICS_NATIVE_PROBE_EXPIRES_AT", "value": ""},
            {"name": "DYNAMICS_NATIVE_PROBE_PERMANENT", "value": "True"},
            {"name": "HUMAIN_VOICE", "value": "mul_nourah"},
            {"name": "DYNAMICS_NATIVE_MEDIA_API_KEY", "secretRef": "native-media-key"},
            {"name": "HUMAIN_AUTHORIZATION", "secretRef": "humain-authorization"},
        ]
        self.image, self.latest = IMAGE, f"{APP}--initial"
        self.revisions = [self.revision(self.latest)]
        self.siblings = {"ca-orchestrator": "ca-orchestrator--one", "ca-ui": "ca-ui--one"}
        self.calls, self.update_error, self.update_hook, self.health = [], None, None, "Healthy"

    def revision(self, name):
        return {"name": name, "properties": {
            "provisioningState": "Provisioned", "healthState": self.health if hasattr(self, "health") else "Healthy",
            "runningState": "Running", "template": {"containers": [{"name": "voice", "image": self.image, "env": copy.deepcopy(self.env)}]},
        }}

    def __call__(self, arguments, *, timeout=120):
        self.calls.append(list(arguments))
        if arguments[:2] == ["containerapp", "show"]:
            return {"name": APP, "properties": {
                "latestRevisionName": self.latest,
                "configuration": {"activeRevisionsMode": "Single", "secrets": [{"name": "native-media-key"}, {"name": "humain-authorization"}]},
                "template": {"containers": [{"name": "voice", "image": self.image, "env": copy.deepcopy(self.env),
                                             "resources": {"cpu": 2.0, "memory": "4Gi"}}], "scale": {"minReplicas": 1, "maxReplicas": 1}},
            }}
        if arguments[:3] == ["containerapp", "revision", "list"]:
            return copy.deepcopy(self.revisions)
        if arguments[:2] == ["containerapp", "list"]:
            return [{"name": name, "properties": {"latestRevisionName": revision}}
                    for name, revision in {APP: self.latest, **self.siblings}.items()]
        if arguments[:2] == ["containerapp", "update"]:
            if self.update_error is not None:
                raise self.update_error
            for pair in arguments[arguments.index("--set-env-vars") + 1:]:
                key, _, value = pair.partition("=")
                entry = next((row for row in self.env if row["name"] == key), None)
                if entry is None:
                    self.env.append({"name": key, "value": value})
                else:
                    entry.clear()
                    entry.update({"name": key, "value": value})
            if self.update_hook is not None:
                self.update_hook(self)
            self.latest = f"{APP}--{arguments[arguments.index('--revision-suffix') + 1]}"
            self.revisions.append(self.revision(self.latest))
            return {"name": APP}
        raise AssertionError(f"Unexpected az call {arguments}")

    def updates(self):
        return [call for call in self.calls if call[:2] == ["containerapp", "update"]]


def arguments(directory, *extra, settings=(f"ACS_SOURCE_NUMBER={NUMBER}",)):
    values = ["--subscription-id", SUBSCRIPTION, "--resource-group", "app-rg", "--output-dir", str(directory / "runtime-01")]
    for setting in settings:
        values += ["--set", setting]
    return runtime.parser().parse_args([*values, *extra])


def run(fake, args):
    stream = io.StringIO()
    with patch.object(runtime, "az", fake), contextlib.redirect_stdout(stream):
        runtime.settings(args)
    return json.loads(stream.getvalue())


APPLY = ("--apply", "--confirm-app", APP)


class RuntimeSettingsTests(unittest.TestCase):
    def test_plan_shows_the_diff_and_azd_commands_without_writing(self):
        with private_test_directory() as directory:
            fake = FakeContainerApps()
            planned = run(fake, arguments(directory, settings=(f"ACS_SOURCE_NUMBER={NUMBER}", "DYNAMICS_NATIVE_MEDIA_FAST_START=true")))
            self.assertEqual(planned["status"], "planned")
            self.assertEqual(planned["changes"], [
                {"key": "ACS_SOURCE_NUMBER", "before": "", "after": NUMBER},
                {"key": "DYNAMICS_NATIVE_MEDIA_FAST_START", "before": "False", "after": "true"},
            ])
            self.assertEqual(planned["azd_env_set"], [
                f'azd env set ACS_SOURCE_NUMBER {NUMBER} --environment "$ENV"',
                'azd env set DYNAMICS_NATIVE_MEDIA_FAST_START true --environment "$ENV"',
            ])
            self.assertEqual(fake.updates(), [])
            self.assertEqual(stat.S_IMODE((directory / "runtime-01/plan.json").stat().st_mode), 0o600)

    def test_unknown_secret_and_secret_reference_settings_are_refused(self):
        for setting, message in (
            ("UNRELATED_SETTING=1", "isn't an allowlisted"),
            ("DYNAMICS_NATIVE_MEDIA_API_KEY=synthetic-value", "is a secret"),
            ("ACS_SOURCE_NUMBER=secretref:native-media-key", "not a secret reference"),
            ("ACS_SOURCE_NUMBER=2025550101", "E.164"),
        ):
            with self.subTest(setting=setting), private_test_directory() as directory:
                fake = FakeContainerApps()
                with patch.object(runtime, "az", fake), self.assertRaisesRegex(ValueError, message):
                    runtime.settings(arguments(directory, settings=(setting,)))
                self.assertEqual(fake.calls, [])
        with private_test_directory() as directory:
            fake = FakeContainerApps()
            fake.env[0] = {"name": "ACS_SOURCE_NUMBER", "secretRef": "acs-number"}
            with patch.object(runtime, "az", fake), self.assertRaisesRegex(ValueError, "secret reference on the running app"):
                runtime.settings(arguments(directory))
            self.assertFalse((directory / "runtime-01").exists())

    def test_apply_pushes_the_number_with_one_revision_and_verifies_the_rest_is_unchanged(self):
        with private_test_directory() as directory:
            fake = FakeContainerApps()
            run(fake, arguments(directory, "--azd-environment", "demo"))
            with patch.object(runtime, "az", fake), self.assertRaisesRegex(ValueError, "--confirm-app"):
                runtime.settings(arguments(directory, "--apply"))
            result = run(fake, arguments(directory, *APPLY))
            [update] = fake.updates()
            self.assertEqual(update[update.index("--set-env-vars") + 1:], [f"ACS_SOURCE_NUMBER={NUMBER}"])
            self.assertEqual(update[update.index("--container-name") + 1], "voice")
            self.assertRegex(update[update.index("--revision-suffix") + 1], r"^rt[0-9a-f]{8}$")
            self.assertFalse({"--image", "--yaml", "--replace-env-vars", "--remove-env-vars"} & set(update))
            self.assertEqual(result["status"], "applied")
            self.assertTrue(all(result["checks"].values()))
            self.assertEqual(result["azd_env_set"], [f"azd env set ACS_SOURCE_NUMBER {NUMBER} --environment demo"])
            self.assertTrue(Path(result["private_receipt"]).is_file())
            self.assertEqual(next(row["value"] for row in fake.revisions[-1]["properties"]["template"]["containers"][0]["env"]
                                  if row["name"] == "ACS_SOURCE_NUMBER"), NUMBER)

            again = run(fake, arguments(directory, *APPLY))
            self.assertEqual(again["status"], "reconciled")
            self.assertEqual(len(fake.updates()), 1)

    def test_unplanned_changes_fail_verification_without_a_retry(self):
        def other_variable(fake):
            next(row for row in fake.env if row["name"] == "HUMAIN_VOICE")["value"] = "another_voice"

        def image(fake):
            fake.image = "registry.example.azurecr.io/voice:unreviewed"

        def sibling(fake):
            fake.siblings["ca-ui"] = "ca-ui--two"

        for hook, check in ((other_variable, "only_planned_variables_changed"), (image, "image_unchanged"),
                            (sibling, "sibling_latest_revisions_unchanged")):
            with self.subTest(check=check), private_test_directory() as directory:
                fake = FakeContainerApps()
                run(fake, arguments(directory))
                fake.update_hook = hook
                with patch.object(runtime, "az", fake), self.assertRaisesRegex(RuntimeError, check + ".*No retry"):
                    runtime.settings(arguments(directory, *APPLY))
                self.assertEqual(len(fake.updates()), 1)

    def test_changes_since_planning_block_apply(self):
        with private_test_directory() as directory:
            fake = FakeContainerApps()
            run(fake, arguments(directory))
            fake.latest = f"{APP}--deployed-elsewhere"
            with patch.object(runtime, "az", fake), self.assertRaisesRegex(ValueError, "changed since planning"):
                runtime.settings(arguments(directory, *APPLY))
            self.assertEqual(fake.updates(), [])
            self.assertFalse((directory / "runtime-01/intent.json").exists())

    def test_failed_update_reads_back_and_never_resends(self):
        with private_test_directory() as directory:
            fake = FakeContainerApps()
            run(fake, arguments(directory))
            fake.update_error = runtime.CommandError("az containerapp update failed: InvalidParameterValue")
            with patch.object(runtime, "az", fake), self.assertRaisesRegex(RuntimeError, "InvalidParameterValue.*No retry.*plan once more"):
                runtime.settings(arguments(directory, *APPLY))
            self.assertTrue((directory / "runtime-01/failed.json").is_file())
            fake.update_error = None
            with patch.object(runtime, "az", fake), self.assertRaisesRegex(RuntimeError, "InvalidParameterValue.*one_new_revision"):
                runtime.settings(arguments(directory, *APPLY))
            self.assertEqual(len(fake.updates()), 1)

    def test_unhealthy_revision_is_reported_not_retried(self):
        with private_test_directory() as directory, patch.object(runtime, "HEALTH_READS", 2):
            fake = FakeContainerApps()
            run(fake, arguments(directory))
            fake.health = "Unhealthy"
            with patch.object(runtime, "az", fake), self.assertRaisesRegex(RuntimeError, "revision_ready"):
                runtime.settings(arguments(directory, *APPLY))
            self.assertEqual(len(fake.updates()), 1)

    def test_unsafe_combinations_are_refused_at_plan_time(self):
        for settings, current, message in (
            (("DYNAMICS_NATIVE_MEDIA_TRANSPORT=callautomation",), {"DYNAMICS_NATIVE_MEDIA_FAST_START": "true"}, "needs DYNAMICS_NATIVE_MEDIA_TRANSPORT=voip"),
            (("DYNAMICS_NATIVE_MEDIA_FAST_START=true", "DYNAMICS_NATIVE_MEDIA_TRANSPORT=callautomation"), {}, "needs DYNAMICS_NATIVE_MEDIA_TRANSPORT=voip"),
            (("ACS_AUTO_CONFERENCE_ENABLED=true",), {}, "ACS_AUTO_CONFERENCE_ENABLED=false"),
            (("DYNAMICS_NATIVE_PROBE_EXPIRES_AT=2099-01-01T00:00:00Z",), {}, "needs DYNAMICS_NATIVE_PROBE_EXPIRES_AT empty"),
            (("DYNAMICS_NATIVE_PROBE_PERMANENT=false",), {}, "future observer expiry"),
            (("DYNAMICS_NATIVE_PROBE_EXPIRES_AT=2020-01-01T00:00:00Z",), {}, "future timestamp"),
            ((f"DYNAMICS_NATIVE_MEDIA_ADDITIONAL_WORKSTREAM_IDS={WORKSTREAM_ID}",), {}, "differ from the primary"),
        ):
            with self.subTest(settings=settings), private_test_directory() as directory:
                fake = FakeContainerApps()
                for row in fake.env:
                    if "value" in row:
                        row["value"] = current.get(row["name"], row["value"])
                with patch.object(runtime, "az", fake), self.assertRaisesRegex(ValueError, message):
                    runtime.settings(arguments(directory, settings=settings))
                self.assertEqual(fake.updates(), [])
                self.assertFalse((directory / "runtime-01").exists())

    def test_documented_commands_use_only_allowlisted_settings(self):
        values = {"SUBSCRIPTION_ID": SUBSCRIPTION, "APP_RG": "app-rg", "APP_NUMBER": NUMBER, "ENV": "demo",
                  "WORK": "/private/work", "PROBE_EXPIRES_AT": "2099-01-01T00:00:00Z"}
        commands = documented("native_runtime_settings.py", values)
        self.assertGreaterEqual(len(commands), 5)
        for command in commands:
            args = runtime.parser().parse_args(command)
            self.assertTrue(runtime.requested_settings(args.set))
            if args.apply:
                self.assertEqual(args.confirm_app, APP)

    def test_azure_cli_wrapper_requests_json_and_reports_failures_without_values(self):
        with patch.object(runtime.subprocess, "run", return_value=SimpleNamespace(stdout='{"name": "ca-voice"}')) as call:
            self.assertEqual(runtime.az(["containerapp", "show", "--name", APP]), {"name": APP})
        self.assertEqual(call.call_args.args[0], ["az", "containerapp", "show", "--name", APP, "--only-show-errors", "-o", "json"])
        error = subprocess.CalledProcessError(1, ["az"], stderr="ERROR: (InvalidParameterValue) Bad request")
        with patch.object(runtime.subprocess, "run", side_effect=error), self.assertRaisesRegex(runtime.CommandError, "az containerapp update failed: ERROR"):
            runtime.az(["containerapp", "update", "--set-env-vars", f"ACS_SOURCE_NUMBER={NUMBER}"])

    def test_same_boolean_in_another_case_is_not_a_change(self):
        with private_test_directory() as directory:
            fake = FakeContainerApps()
            result = run(fake, arguments(directory, settings=("DYNAMICS_NATIVE_MEDIA_FAST_START=false",)))
            self.assertEqual(result["status"], "already_configured")
            self.assertEqual(result["azd_env_set"], ['azd env set DYNAMICS_NATIVE_MEDIA_FAST_START false --environment "$ENV"'])
            self.assertFalse((directory / "runtime-01").exists())


if __name__ == "__main__":
    unittest.main()
