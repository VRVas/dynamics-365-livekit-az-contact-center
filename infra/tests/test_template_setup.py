"""Offline module selection, private preparation, approval and resume contracts."""

import base64
from collections import Counter
import copy
from datetime import datetime, timezone
import io
import itertools
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch
import urllib.error
from urllib.parse import parse_qs, urlsplit
import sys

from dotenv import dotenv_values
import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "infra/scripts"))
import template_setup as setup
from native_voice_setup import env_file, write_private


def profile():
    value = json.loads(setup.EXAMPLE.read_text())
    value["azure"].update(tenantId="00000000-0000-4000-8000-000000000001",
                          subscriptionId="00000000-0000-4000-8000-000000000002")
    value["approvals"]["content"] = True
    return value


class ModuleTests(unittest.TestCase):
    def test_every_module_combination_has_exact_dependency_and_service_closure(self):
        names = list(setup.MODULES)
        checked = 0
        for choices in itertools.product((False, True), repeat=len(names)):
            requested = {name for name, enabled in zip(names, choices) if enabled}
            if not requested:
                continue
            expected = set(requested)
            if "native-copilot" in expected:
                expected.add("native-knowledge")
            if expected & {"native-knowledge", "pause-ai"}:
                expected.add("native-workspace")
            if "native-workspace" in expected:
                expected.update(("native-voice", "crm"))
            if "agent-console" in expected:
                expected.add("web-chat")
            actual = setup.resolve_modules(sorted(requested))
            self.assertEqual(set(actual), expected)
            services = set(setup.services_for(actual))
            self.assertEqual("orchestrator-api" in services, "web-chat" in expected)
            self.assertEqual("ui-v2" in services, bool(expected & {"web-chat", "browser-voice", "application-phone"}))
            self.assertEqual("voice" in services, bool(expected & {"browser-voice", "application-phone", "native-voice"}))
            self.assertEqual("assist" in services, bool(expected & {"crm", "knowledge", "agent-assist"}))
            self.assertEqual("agent-console" in services, "agent-console" in expected)
            checked += 1
        self.assertEqual(checked, 8191)

    def test_native_voice_alone_does_not_enable_legacy_phone_or_crm(self):
        value = profile()
        value["modules"] = ["native-voice"]
        value["dynamics"]["organizationUrl"] = "https://example.crm.dynamics.com"
        setup.validate(value, require_target=True)
        self.assertEqual(setup.plan(value)["services"], ["voice"])
        config = setup.runtime_config(value)
        self.assertTrue(config["features"]["nativeVoice"])
        self.assertFalse(config["features"]["crm"])
        self.assertFalse(config["features"]["applicationTelephony"])
        self.assertFalse(setup.public_config(value)["features"]["phone"])

    def test_runtime_region_and_accepted_voice_defaults_are_preserved(self):
        value = profile()
        value["azure"]["location"] = "eastus2"
        env = setup.deployment_values(value)
        self.assertEqual({env[key] for key in ("AZURE_LOCATION", "APP_LOCATION", "COSMOS_LOCATION", "SEARCH_LOCATION")}, {"eastus2"})
        self.assertEqual((env["HUMAIN_TURN_TAKING_STRATEGY"], env["HUMAIN_INTERRUPTION_POLICY_MODE"]), ("fast_vad", "off"))
        for key in ("DYNAMICS_NATIVE_MEDIA_ENABLED", "DYNAMICS_NATIVE_SUPERVISION_ENABLED", "ACS_AUTO_CONFERENCE_ENABLED"):
            self.assertEqual(env[key], "false")

    def test_malformed_profile_values_fail_with_explicit_validation_errors(self):
        mutations = [
            ("modules", None, [{}]), ("brand", "layout", {}), ("locale", "supported", [{}]),
            ("knowledge", "mode", []), ("knowledge", "allowedDomains", [{}]),
            ("business", "caseCategories", [{}]), ("demo", "mode", {}),
            ("azure", "tenantId", []), ("dynamics", "organizationUrl", []),
            ("phonePolicy", "allowedOutboundNumbers", [{}]), ("approvals", "content", "yes"),
        ]
        for section, key, invalid in mutations:
            value = profile()
            if key is None:
                value[section] = invalid
            else:
                value[section][key] = invalid
            with self.subTest(section=section, key=key), self.assertRaises(ValueError):
                setup.validate(value)

    def test_dialing_requires_its_own_approval_not_generated_data_approval(self):
        value = profile()
        value["modules"] = ["application-phone"]
        value["phonePolicy"]["allowedOutboundNumbers"] = ["+12025550123"]
        setup.validate(value)
        self.assertFalse(setup.public_config(value)["features"]["phone"])
        value["approvals"]["outboundDestinations"] = True
        self.assertTrue(setup.public_config(value)["features"]["phone"])
        self.assertFalse(setup.runtime_config(value)["features"]["crm"])
        self.assertNotIn("+12025550123", json.dumps(setup.public_config(value)))


    def test_plan_lists_every_module_with_a_decision_and_a_reason(self):
        value = profile()
        value["modules"] = ["web-chat", "pause-ai"]
        result = setup.plan(value)
        catalog = {entry["module"]: entry for entry in result["modules"]}
        self.assertEqual(list(catalog), list(setup.MODULES))
        for module, entry in catalog.items():
            with self.subTest(module=module):
                self.assertTrue(entry["summary"] and entry["licensesAndPermissions"] and entry["reason"])
                self.assertEqual(entry["dependsOn"], list(setup.MODULES[module]))
                self.assertEqual(entry["decision"], "include" if module in result["resolvedModules"] else "exclude")
                self.assertEqual("addsIfSelected" in entry, entry["decision"] == "exclude")
        self.assertEqual(catalog["web-chat"]["reason"], "Requested.")
        self.assertEqual(catalog["crm"]["reason"], "Required by native-workspace, pause-ai.")
        self.assertEqual(catalog["native-workspace"]["reason"], "Required by pause-ai.")
        self.assertEqual(catalog["native-copilot"]["addsIfSelected"],
                         {"modules": ["native-copilot", "native-knowledge"], "services": []})
        self.assertEqual(catalog["agent-console"]["addsIfSelected"], {"modules": ["agent-console"], "services": ["agent-console"]})
        self.assertEqual(catalog["knowledge"]["addsIfSelected"]["services"], [])
        value["modules"] = ["browser-voice"]
        catalog = {entry["module"]: entry for entry in setup.plan(value)["modules"]}
        self.assertEqual(catalog["native-copilot"]["addsIfSelected"],
                         {"modules": ["crm", "native-copilot", "native-knowledge", "native-voice", "native-workspace"],
                          "services": ["assist"]})

    def test_plan_gates_are_the_union_of_the_selected_modules_gates(self):
        names = list(setup.MODULES)
        for size in (1, 2, 3):
            for requested in itertools.combinations(names, size):
                value = profile()
                value["modules"] = list(requested)
                result = setup.plan(value)
                selected = [entry for entry in result["modules"] if entry["decision"] == "include"]
                expected = {gate for entry in selected for gate in entry["humanGates"]} | set(result["commonGates"])
                with self.subTest(modules=requested):
                    self.assertEqual({gate["id"] for gate in result["humanGates"]}, expected)
                    self.assertEqual({entry["module"] for entry in selected}, set(result["resolvedModules"]))
        for term, meaning in setup.plan(profile())["terms"].items():
            with self.subTest(term=term):
                self.assertTrue(set(re.findall(r"\(([a-z-]+(?:, [a-z-]+)*)\)", meaning)[-1].split(", ")) <= set(setup.MODULES))


class PreparationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.private = Path(temporary.name)
        self.value = profile()
        self.profile_path = self.private / "profile.json"
        self.destination = self.private / "workspace"
        # The publish check has its own tests; preparation tests must not depend on the live checkout.
        neutral_source = patch("check_template.check", return_value={"status": "verified"})
        neutral_source.start()
        self.addCleanup(neutral_source.stop)
        self.paths = [Path(path) for path in (
            "azure.yaml", setup.HOOK_WRAPPER, "infra/template-profile.example.json", *setup.UI_CONFIGS, *setup.RUNTIME_CONFIGS,
            "infra/data/knowledge.md", "infra/data/knowledge.json", "infra/data/knowledge-config.json",
            "infra/data/dynamics-workspace/knowledge-ar.md",
            *(root + "/" + name for root in setup.CONTENT_ROOTS for name in ("system_prompt.md", "knowledge.md", "profiles.json")),
        )]
        self.paths = [path for path in self.paths if (ROOT / path).is_file()]

    def private_file(self, name, content):
        target = self.private / name
        write_private(target, content)
        return str(target)

    def prepare(self):
        self.profile_path.write_text(json.dumps(self.value))
        self.profile_path.chmod(0o600)
        setup.validate(self.value, require_target=True)
        with patch.object(setup, "source_paths", return_value=self.paths):
            return setup.prepare(self.value, self.destination, self.profile_path)

    def initialize_environment(self):
        prepared = json.loads((self.destination / "deployment-profile.json").read_text())
        ledger = json.loads((self.destination / "deployment-ledger.json").read_text())
        directory = self.destination / ".azure" / prepared["name"]
        directory.mkdir(parents=True)
        values = setup.deployment_values(prepared)
        values["TEMPLATE_PREPARED_PROFILE_SHA256"] = ledger["preparedProfileSha256"]
        write_private(directory / ".env", env_file(values))
        return prepared

    def test_only_selected_services_are_staged_and_private_content_is_self_contained(self):
        prompt = self.private_file("prompt.md", "Approved technical assistant instructions.\n")
        self.value["assistant"]["systemPromptFile"] = prompt
        result = self.prepare()
        self.assertEqual(result["services"], ["orchestrator-api", "ui-v2"])
        manifest = yaml.safe_load((self.destination / "azure.yaml").read_text())
        self.assertEqual(set(manifest["services"]), {"orchestrator-api", "ui-v2"})
        self.assertNotIn("postprovision", manifest["hooks"])
        self.assertEqual(self.destination.stat().st_mode & 0o777, 0o700)
        prepared = self.initialize_environment()
        Path(prompt).unlink()
        self.assertTrue(Path(prepared["assistant"]["systemPromptFile"]).is_relative_to(self.destination / ".deployment-inputs"))
        with patch.object(setup, "ROOT", self.destination):
            self.assertEqual(setup.status()["callerAcceptance"]["status"], "not-tested")
            self.assertEqual(setup.gate("package")["status"], "verified")
        public = json.loads((self.destination / setup.UI_CONFIGS[0]).read_text())
        self.assertNotIn("systemPromptFile", json.dumps(public))
        self.assertNotIn("logoFile", public["brand"])
        for relative in setup.RUNTIME_CONFIGS:
            self.assertEqual((self.destination / relative).stat().st_mode & 0o777, 0o600)

    def test_bad_content_is_rejected_before_destination_is_created(self):
        self.value["assistant"]["knowledgeFile"] = self.private_file("bad.md", "Unapproved unstructured content.")
        with self.assertRaises(ValueError):
            self.prepare()
        self.assertFalse(self.destination.exists())

    def test_non_neutral_source_is_rejected_before_preparation(self):
        with patch("check_template.check", side_effect=ValueError("Source contains customer defaults")):
            with self.assertRaisesRegex(ValueError, "customer defaults"):
                self.prepare()
        self.assertFalse(self.destination.exists())

    def test_existing_destination_and_unapproved_content_are_not_overwritten(self):
        self.destination.mkdir()
        with self.assertRaises(ValueError):
            self.prepare()
        self.destination.rmdir()
        self.value["approvals"]["content"] = False
        with self.assertRaises(ValueError):
            self.prepare()
        self.assertFalse(self.destination.exists())

    def test_selected_knowledge_validates_corpus_before_copying(self):
        self.value["modules"] = ["web-chat", "knowledge"]
        self.value["knowledge"]["allowedDomains"] = ["example.org"]
        with self.assertRaisesRegex(ValueError, "nonempty"):
            self.prepare()
        self.assertFalse(self.destination.exists())
        self.value["knowledge"]["mode"] = "web"
        self.prepare()
        self.assertIn("INGEST_API_KEY=", (self.destination / "selection.env").read_text())
        self.assertNotIn("INGEST_API_KEY", (self.destination / "deployment-ledger.json").read_text())

    def test_image_upload_is_copied_without_leaking_its_private_path(self):
        logo = self.private / "approved-logo.png"
        logo.write_bytes(base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Wl6wX8AAAAASUVORK5CYII="))
        logo.chmod(0o600)
        self.value["brand"]["logoFile"] = str(logo)
        self.prepare()
        for relative in setup.UI_CONFIGS:
            target = self.destination / relative
            self.assertEqual(json.loads(target.read_text())["brand"]["logoUrl"], "/brand-logo.png")
            self.assertTrue((target.parents[2] / "public/brand-logo.png").is_file())

    def test_source_and_approval_drift_are_blocked(self):
        self.prepare()
        prepared = self.initialize_environment()
        with patch.object(setup, "ROOT", self.destination):
            with self.assertRaisesRegex(ValueError, "preview"):
                setup.gate("apply")
            with patch.dict(os.environ, {"TEMPLATE_READ_ONLY_PREVIEW": "true"}):
                setup.gate("provision")
            prepared["approvals"]["azurePreview"] = True
            (self.destination / "deployment-profile.json").write_text(json.dumps(prepared))
            setup.gate("apply")
            generated = self.destination / setup.RUNTIME_CONFIGS[0]
            generated.write_text("{}")
            with self.assertRaisesRegex(ValueError, "changed"):
                setup.gate("package")

    def test_environment_module_drift_is_blocked_before_cloud_work(self):
        self.prepare()
        prepared = self.initialize_environment()
        path = self.destination / ".azure" / prepared["name"] / ".env"
        path.write_text(path.read_text().replace('DEPLOY_NATIVE_VOICE="false"', 'DEPLOY_NATIVE_VOICE="true"'))
        with patch.object(setup, "ROOT", self.destination), self.assertRaisesRegex(ValueError, "DEPLOY_NATIVE_VOICE"):
            setup.gate("package")

    def test_outbound_approval_revocation_is_not_ignored_by_preparation_receipts(self):
        self.value["modules"] = ["application-phone"]
        self.value["phonePolicy"]["allowedOutboundNumbers"] = ["+12025550123"]
        self.value["approvals"]["outboundDestinations"] = True
        self.prepare()
        prepared = self.initialize_environment()
        prepared["approvals"]["outboundDestinations"] = False
        (self.destination / "deployment-profile.json").write_text(json.dumps(prepared))
        with patch.object(setup, "ROOT", self.destination), self.assertRaisesRegex(ValueError, "revoked"):
            setup.gate("package")

    def test_public_logo_changes_invalidate_content_approval(self):
        logo = self.private / "logo.png"
        logo.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 24)
        logo.chmod(0o600)
        self.value["brand"]["logoFile"] = str(logo)
        self.prepare()
        self.initialize_environment()
        public_logo = (self.destination / setup.UI_CONFIGS[0]).parents[2] / "public/brand-logo.png"
        public_logo.write_bytes(b"modified")
        with patch.object(setup, "ROOT", self.destination), self.assertRaisesRegex(ValueError, "changed"):
            setup.gate("package")

    def test_additional_tool_requirements_can_be_prepared_but_not_silently_deployed(self):
        self.value["additionalTools"] = [{"name": "lookup_status", "purpose": "Read an owner-approved external status API."}]
        self.prepare()
        self.initialize_environment()
        with patch.object(setup, "ROOT", self.destination):
            with self.assertRaisesRegex(ValueError, "not installed"):
                setup.gate("package")
            adapter = self.destination / "reviewed-adapter.py"
            evidence = self.destination / "adapter-tests.txt"
            write_private(adapter, "def lookup_status():\n    return {'status': 'fixture'}\n")
            write_private(evidence, "Technical test evidence fixture.\n")
            review = {"approved": True, "approvalReference": "Unit-test approval reference", "toolNames": ["lookup_status"],
                      "sourceFiles": {adapter.name: setup.digest(adapter)}, "testEvidence": {evidence.name: setup.digest(evidence)}}
            write_private(self.destination / "extension-review.json", json.dumps(review))
            setup.gate("package")
            adapter.write_text("changed")
            with self.assertRaisesRegex(ValueError, "changed"):
                setup.gate("package")

    def test_resume_records_preserve_evidence_and_detect_tampering(self):
        self.prepare()
        evidence = Path(self.private_file("approval.txt", "Technical approval fixture."))
        args = SimpleNamespace(stage="content", status="approved", note="Approved scope.",
                               approval_reference="Unit-test conversation reference", evidence=[evidence])
        with patch.object(setup, "ROOT", self.destination):
            result = setup.record_stage(args)
            self.assertEqual(Path(result["record"]).stat().st_mode & 0o777, 0o600)
            self.assertEqual(setup.status()["stages"]["content"]["status"], "approved")
            evidence.write_text("changed")
            with self.assertRaisesRegex(ValueError, "changed"):
                setup.status()

    def test_reconfiguring_never_resets_live_runtime_overrides(self):
        self.prepare()
        self.initialize_environment()
        with patch.object(setup, "ROOT", self.destination), patch.object(setup.subprocess, "run") as process:
            result = setup.configure()
        self.assertEqual(result["status"], "already-configured")
        process.assert_not_called()

    def test_preparation_requires_the_hook_wrapper(self):
        self.paths.remove(setup.HOOK_WRAPPER)
        with self.assertRaisesRegex(ValueError, "hook_python.sh"):
            self.prepare()
        self.assertFalse(self.destination.exists())

    def test_configure_and_gate_keep_the_azd_python_setting(self):
        self.prepare()
        prepared = json.loads((self.destination / "deployment-profile.json").read_text())
        directory = self.destination / ".azure" / prepared["name"]
        directory.mkdir(parents=True)
        interpreter = str(self.private / "venv/bin/python")
        write_private(directory / ".env", env_file({"AZURE_ENV_NAME": prepared["name"], "PYTHON": interpreter}))
        with patch.object(setup, "ROOT", self.destination):
            self.assertEqual(setup.configure()["status"], "configured")
            self.assertEqual(setup.gate("package")["status"], "verified")
            with patch.dict(os.environ, {"TEMPLATE_READ_ONLY_PREVIEW": "true"}):
                self.assertEqual(setup.gate("provision")["status"], "verified")
            self.assertEqual(setup.configure()["status"], "already-configured")
        values = dotenv_values(directory / ".env", interpolate=False)
        self.assertEqual(values["PYTHON"], interpreter)
        self.assertEqual(values["DEPLOY_WEB_CHAT"], "true")

    def test_preview_points_to_the_full_scope(self):
        self.prepare()
        self.initialize_environment()
        with patch.object(setup, "ROOT", self.destination), \
                patch.object(setup.subprocess, "run", return_value=SimpleNamespace(returncode=0)) as process:
            result = setup.preview()
        self.assertEqual(process.call_args.args[0][:3], ["azd", "provision", "--preview"])
        self.assertIn("omits Communication Services, Email, identities and role assignments", result["note"])
        self.assertIn("template_setup.py scope", result["note"])
        self.assertFalse(result["approvalRecorded"])

    def test_unprepared_checkout_is_blocked(self):
        with self.assertRaisesRegex(ValueError, "unprepared"):
            setup.gate("provision")



WRAPPER = ROOT / setup.HOOK_WRAPPER


@unittest.skipIf(os.name == "nt" or not shutil.which("sh"), "azd hooks run with sh.")
class HookWrapperTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.bin = self.root / "bin"
        self.venv = self.executable(self.root / "venv/bin/python", 'echo "venv $*"')
        self.executable(self.bin / "python3", 'echo "system $*"')
        self.shell = shutil.which("sh")

    def executable(self, path, body):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("#!/bin/sh\n" + body + "\n")
        path.chmod(0o700)
        return path

    def run_wrapper(self, **environment):
        return subprocess.run([self.shell, str(WRAPPER), "infra/scripts/template_setup.py", "gate", "--phase", "provision"],
                              env={"PATH": str(self.bin), **environment}, capture_output=True, text=True, timeout=30)

    def test_python_setting_wins_without_asking_azd(self):
        calls = self.root / "azd-called"
        self.executable(self.bin / "azd", f': > "{calls}"')
        result = self.run_wrapper(PYTHON=str(self.venv))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "venv infra/scripts/template_setup.py gate --phase provision\n")
        self.assertFalse(calls.exists())

    def test_azd_environment_value_is_used_from_a_fresh_shell(self):
        self.executable(self.bin / "azd", f'[ "$*" = "env get-value PYTHON" ] && echo "{self.venv}"')
        result = self.run_wrapper()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(result.stdout.startswith("venv "))

    def test_missing_azd_value_falls_back_to_python3(self):
        self.executable(self.bin / "azd", 'echo "not a path"; echo "ERROR: key not found" >&2; exit 1')
        result = self.run_wrapper()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(result.stdout.startswith("system "))
        self.assertEqual(result.stderr, "")

    def test_without_azd_the_wrapper_uses_python3(self):
        result = self.run_wrapper()
        self.assertTrue(result.stdout.startswith("system "))

    def test_missing_interpreter_fails_with_the_fix(self):
        result = self.run_wrapper(PYTHON=str(self.root / "missing/python"))
        self.assertEqual(result.returncode, 127)
        self.assertIn("azd env set PYTHON", result.stderr)
        self.assertEqual(result.stdout, "")

    def test_every_azd_hook_runs_python_through_the_wrapper(self):
        hooks = yaml.safe_load((ROOT / "azure.yaml").read_text())["hooks"]
        self.assertTrue(hooks)
        for name, hook in hooks.items():
            for command in hook["run"].split("&&"):
                with self.subTest(hook=name, command=command):
                    self.assertEqual(hook["shell"], "sh")
                    self.assertRegex(command.strip(), r"^sh infra/scripts/hook_python\.sh infra/scripts/\w+\.py\b")


def price_item(meter, price, unit, **extra):
    return {"meterName": meter, "retailPrice": price, "unitOfMeasure": unit, "type": "Consumption",
            "armRegionName": "swedencentral", "tierMinimumUnits": 0.0, "currencyCode": "USD",
            "isPrimaryMeterRegion": True, "effectiveStartDate": "2022-06-01T00:00:00Z", **extra}


PRICE_ITEMS = [
    price_item("Standard vCPU Active Usage", 0.000024, "1 Second"),
    price_item("Standard vCPU Idle Usage", 0.000003, "1 Second"),
    price_item("Standard Memory Active Usage", 0.000003, "1 GiB Second"),
    price_item("Standard Memory Idle Usage", 0.000003, "1 GiB Second"),
    price_item("Standard vCPU Active Usage", 0.5, "1 Second", tierMinimumUnits=100.0),
    price_item("Standard NC T4 v3 GPU Usage", 0.000095, "1 Second"),
]


def price_page(items, next_link=None):
    return io.BytesIO(json.dumps({"Items": items, "NextPageLink": next_link}).encode())


class ScopeTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(setup.bicep_declaration.cache_clear)

    def scope(self, modules, **options):
        value = profile()
        value["modules"] = modules
        value["azure"]["location"] = "swedencentral"
        return setup.scope(value, **options)

    def bicep_copy(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        copy_root = Path(temporary.name)
        for relative in (*setup.BICEP.values(), "infra/main.parameters.json"):
            (copy_root / relative).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / relative, copy_root / relative)
        return copy_root

    def test_scope_reads_models_regions_and_replicas_from_the_bicep(self):
        copy_root = self.bicep_copy()
        bicep = copy_root / setup.BICEP["resources"]
        source = bicep.read_text()
        ui = source.index("module uiApp ")
        source = source[:ui] + source[ui:].replace("minReplicas: 1", "minReplicas: 2", 1)
        bicep.write_text(source.replace("modelCapacity: 50", "modelCapacity: 20"))
        with patch.object(setup, "ROOT", copy_root):
            result = self.scope(["web-chat"])
        apps = {app["app"]: app for app in result["containerApps"]}
        self.assertEqual(set(apps), {"ca-orchestrator-api", "ca-ui-v2"})
        self.assertEqual(apps["ca-ui-v2"]["minReplicas"], 2)
        self.assertEqual((result["warmReplicas"]["replicas"], result["warmReplicas"]["vCpu"],
                          result["warmReplicas"]["memoryGiB"]), (3, 1.5, 3.0))
        [model] = result["models"]
        self.assertEqual((model["model"], model["sku"], model["capacity"], model["deploymentSetting"]),
                         ("gpt-5.4-mini", "GlobalStandard", 20, "AZURE_AI_MODEL_DEPLOYMENT_NAME"))
        self.assertIn("Global deployment", model["processing"])
        self.assertIn("not provisioned throughput", model["capacityMeaning"])
        self.assertIn("omits Communication Services, Email, identities and role assignments", result["azdPreview"])
        self.assertTrue(result["prices"].startswith("Not queried"))

    def test_scope_reader_fails_closed_when_the_bicep_changes_shape(self):
        copy_root = self.bicep_copy()
        bicep = copy_root / setup.BICEP["resources"]
        bicep.write_text(bicep.read_text().replace("minReplicas: 1\n        maxReplicas: 1\n      }\n    }\n  }\n}",
                                                   "maxReplicas: 1\n      }\n    }\n  }\n}"))
        with patch.object(setup, "ROOT", copy_root), self.assertRaisesRegex(ValueError, "minReplicas of voiceApp in infra/resources.bicep"):
            self.scope(["browser-voice"])
        (copy_root / setup.BICEP["knowledge"]).write_text("// moved\n")
        with patch.object(setup, "ROOT", copy_root), self.assertRaisesRegex(ValueError, "no longer declares search"):
            self.scope(["knowledge"])

    def test_scope_names_the_retrieval_model_and_the_geography_of_each_resource(self):
        value = profile()
        value["modules"] = ["knowledge", "email"]
        value["azure"].update(location="francecentral", communicationsDataLocation="Europe")
        value["knowledge"]["mode"] = "web"
        result = setup.scope(value)
        [model] = result["models"]
        self.assertEqual((model["model"], model["deploymentSetting"]), ("gpt-5.4-nano", "INSIGHTS_MODEL_DEPLOYMENT_NAME"))
        self.assertIn("Foundry IQ", model["purpose"])
        resources = {entry["resource"]: entry for entry in result["resources"]}
        self.assertEqual(resources["Azure AI Search"]["location"], "francecentral")
        self.assertEqual(resources["Azure AI Search"]["locationSetting"], "SEARCH_LOCATION")
        self.assertEqual((resources["Communication Services"]["location"], resources["Communication Services"]["dataLocation"]),
                         ("global", "Europe"))
        self.assertNotIn("Voice app", resources)
        self.assertTrue(any("public web" in note for note in result["geographyNotes"]))
        self.assertIn({"principal": "Search identity", "role": "Cognitive Services OpenAI User",
                       "roleDefinition": "5e0bd9bd-7b93-4f28-af87-19fc36ad61bd", "scope": "Foundry account",
                       "selectedBy": ["knowledge"]}, result["roleAssignments"])

    def test_every_app_warm_keeps_four_vcpu_and_eight_gib_running(self):
        result = self.scope(["web-chat", "browser-voice", "crm", "agent-console"])
        self.assertEqual(len(result["containerApps"]), 5)
        self.assertEqual({key: result["warmReplicas"][key] for key in ("replicas", "vCpu", "memoryGiB")},
                         {"replicas": 5, "vCpu": 4.0, "memoryGiB": 8.0})

    def test_prices_come_from_the_retail_prices_api_with_a_dated_query(self):
        with patch.object(setup.urllib.request, "urlopen", return_value=price_page(PRICE_ITEMS)) as urlopen:
            prices = self.scope(["web-chat"], prices=True)["prices"]
        url = urlopen.call_args.args[0]
        self.assertEqual(url, prices["queryUrl"])
        self.assertTrue(url.startswith(setup.PRICES_API + "?"))
        query = parse_qs(urlsplit(url).query)["$filter"][0]
        self.assertIn("armRegionName eq 'swedencentral'", query)
        self.assertIn("serviceName eq 'Azure Container Apps'", query)
        self.assertEqual(prices["queriedAt"][:10], datetime.now(timezone.utc).strftime("%Y-%m-%d"))
        estimate = prices["warmReplicaEstimate"]
        self.assertEqual((estimate["vCpu"], estimate["memoryGiB"]), (1.0, 2.0))
        self.assertEqual(estimate["allIdle"], round((0.000003 + 2 * 0.000003) * 730 * 3600, 2))
        self.assertEqual(estimate["allActive"], round((0.000024 + 2 * 0.000003) * 730 * 3600, 2))
        self.assertEqual([meter["retailPrice"] for meter in prices["meters"]], [0.000024, 0.000003, 0.000003, 0.000003])

    def test_prices_follow_only_retail_api_pages(self):
        pages = [price_page(PRICE_ITEMS[:2], "https://prices.azure.com/api/retail/prices?$skip=100"), price_page(PRICE_ITEMS[2:])]
        with patch.object(setup.urllib.request, "urlopen", side_effect=pages) as urlopen:
            self.scope(["web-chat"], prices=True)
        self.assertEqual(urlopen.call_args.args[0], "https://prices.azure.com/api/retail/prices?$skip=100")
        with patch.object(setup.urllib.request, "urlopen", return_value=price_page(PRICE_ITEMS, "https://example.com/next")):
            with self.assertRaisesRegex(ValueError, "unexpected next page"):
                self.scope(["web-chat"], prices=True)

    def test_prices_fail_closed_on_missing_changed_or_unreachable_meters(self):
        cases = (
            (price_page(PRICE_ITEMS[1:]), "no 'Standard vCPU Active Usage' price"),
            (price_page([price_item("Standard vCPU Active Usage", 0.000024, "1 Hour"), *PRICE_ITEMS[1:4]]), "isn't priced per '1 Second'"),
            (price_page([*PRICE_ITEMS, PRICE_ITEMS[0]]), "more than one"),
            (urllib.error.URLError("offline"), "Can't read Azure Retail Prices"),
            (io.BytesIO(b"<html>"), "Can't read Azure Retail Prices"),
        )
        for response, message in cases:
            outcome = {"side_effect": response} if isinstance(response, Exception) else {"return_value": response}
            with self.subTest(message=message), patch.object(setup.urllib.request, "urlopen", **outcome):
                with self.assertRaisesRegex(ValueError, message):
                    self.scope(["web-chat"], prices=True)

    def test_scope_command_reports_price_failures_without_output(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        profile_path = Path(temporary.name) / "profile.json"
        write_private(profile_path, json.dumps(profile()))
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(setup.urllib.request, "urlopen", side_effect=urllib.error.URLError("offline")), \
                patch("sys.stdout", stdout), patch("sys.stderr", stderr):
            self.assertEqual(setup.main(["scope", "--profile", str(profile_path), "--prices"]), 1)
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(json.loads(stderr.getvalue())["status"], "blocked")
        with patch("sys.stdout", stdout):
            self.assertEqual(setup.main(["scope", "--profile", str(profile_path)]), 0)
        self.assertEqual(json.loads(stdout.getvalue())["resolvedModules"], ["web-chat"])


class ScopeTemplateMatrixTests(unittest.TestCase):
    """The scope must match the compiled ARM template for every module combination."""

    LOCAL_MODULES = {"foundry", "knowledge", "dataplane"}
    LOCATIONS = {"AZURE_LOCATION": "location", "APP_LOCATION": "hostingLocation", "COSMOS_LOCATION": "cosmosLocation",
                 "SEARCH_LOCATION": "hostingLocation|searchLocation"}
    ROLE_TYPES = {"Microsoft.Authorization/roleAssignments", "Microsoft.DocumentDB/databaseAccounts/sqlRoleAssignments"}

    @classmethod
    def setUpClass(cls):
        name = os.environ.get("TEMPLATE_COMPILED_ARM")
        if not name:
            raise unittest.SkipTest("Set TEMPLATE_COMPILED_ARM to the fresh az bicep build output for the scope matrix.")
        from test_template_infrastructure import Context

        class ArmContext(Context):
            def evaluate(self, value):
                # Deployment outputs, such as the shared identity's principal ID, are never empty.
                if isinstance(value, str) and value.startswith("[reference("):
                    return "deployment-output"
                return super().evaluate(value)

            def child(self, resource):
                properties = resource["properties"]
                return ArmContext(properties["template"], properties.get("parameters"), self)

        cls.ArmContext = ArmContext
        path = Path(name)
        if any((ROOT / source).stat().st_mtime_ns > path.stat().st_mtime_ns for source in setup.BICEP.values()):
            raise AssertionError("Recompile Bicep before testing the scope matrix.")
        cls.template = json.loads(path.read_text())
        cls.flags = json.loads((ROOT / "infra/main.parameters.json").read_text())["parameters"]

    def location(self, context, value):
        if value is None or not value.startswith("["):
            return value
        names = sorted(set(re.findall(r"parameters\('(\w+)'\)", value)))
        if context.parent is None:
            return "|".join(names)
        resolved = set()
        for name in names:
            binding = context.bindings[name]
            binding = binding["value"] if isinstance(binding, dict) else binding
            resolved.update(self.location(context.parent, binding).split("|"))
        return "|".join(sorted(resolved))

    @staticmethod
    def copies(context, resource):
        count = resource.get("copy", {}).get("count")
        if count is None:
            return 1
        listed = re.fullmatch(r"\[length\(createArray\((.*)\)\)\]", count)
        if listed:
            return len(re.findall(r"(?:parameters|variables)\('", listed.group(1)))
        return len(context.template["variables"][re.fullmatch(r"\[length\(variables\('(\w+)'\)\)\]", count).group(1)])

    @staticmethod
    def role_definitions(context, resource, count):
        expression = resource["properties"]["roleDefinitionId"]
        ids = [context.template["variables"][name] for name in re.findall(r"variables\('(\w+)'\)", expression)]
        ids += re.findall(r"'([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})'", expression)
        return ids if "copyIndex()" in expression else ids * count

    @staticmethod
    def cpu(value):
        return float(re.fullmatch(r"\[json\('([0-9.]+)'\)\]", value).group(1))

    def compiled(self, flags):
        root = self.ArmContext(self.template, flags)
        found = {key: Counter() for key in ("types", "locations", "skus", "roles", "models", "apps")}
        found["identities"] = 0

        def add(kind, location, sku, count=1):
            found["types"][kind] += count
            if location is not None:
                found["locations"][(kind, location)] += count
            if sku is not None:
                found["skus"][(kind, sku)] += count

        def app(name, scale, container):
            found["apps"][(name, scale["minReplicas"], scale["maxReplicas"], self.cpu(container["resources"]["cpu"]),
                           float(container["resources"]["memory"].removesuffix("Gi")))] += 1

        def visit(context, resources):
            for name, resource in resources:
                if not context.active(resource):
                    continue
                kind, properties = resource["type"], resource.get("properties", {})
                count = self.copies(context, resource)
                if kind == "Microsoft.Resources/deployments" and name in self.LOCAL_MODULES:
                    child = context.child(resource)
                    visit(child, enumerate(child.template["resources"]))
                elif kind == "Microsoft.Resources/deployments":
                    nested, parameters = properties["template"], properties["parameters"]
                    primary = next(item["type"] for item in nested["resources"].values()
                                   if not item.get("existing") and "condition" not in item and item["type"].count("/") == 1
                                   and item["type"] not in {"Microsoft.Resources/deployments", "Microsoft.Insights/diagnosticSettings",
                                                            "Microsoft.Authorization/roleAssignments", "Microsoft.Authorization/locks"})
                    sku = next((parameters[key]["value"] for key in ("acrSku", "skuName", "sku") if key in parameters), None)
                    if "workloadProfiles" in parameters:
                        sku = parameters["workloadProfiles"]["value"][0]["workloadProfileType"]
                    add(primary, self.location(context, parameters["location"]["value"]), sku)
                    for assignment in parameters.get("roleAssignments", {}).get("value", []):
                        found["roles"][assignment["roleDefinitionIdOrName"]] += 1
                    if primary == "Microsoft.App/containerApps":
                        app(parameters["name"]["value"], parameters["scaleSettings"]["value"], parameters["containers"]["value"][0])
                elif kind in self.ROLE_TYPES:
                    found["roles"].update(self.role_definitions(context, resource, count))
                elif kind == "Microsoft.CognitiveServices/accounts/deployments":
                    found["models"][(properties["model"]["name"], properties["model"]["version"], resource["sku"]["name"],
                                     context.evaluate(resource["sku"]["capacity"]), properties.get("serviceTier", "Standard"))] += 1
                else:
                    sku = resource.get("sku", {}).get("name")
                    if {"name": "EnableServerless"} in properties.get("capabilities", []):
                        sku = "Serverless"
                    if properties.get("workloadProfiles"):
                        sku = properties["workloadProfiles"][0]["workloadProfileType"]
                    add(kind, self.location(context, resource.get("location")), sku, count)
                    if resource.get("identity", {}).get("type") == "SystemAssigned":
                        found["identities"] += count
                    if kind == "Microsoft.App/containerApps":
                        app(resource["name"], properties["template"]["scale"], properties["template"]["containers"][0])

        visit(root, enumerate(resource for resource in self.template["resources"]
                              if resource["type"] != "Microsoft.Resources/deployments"))
        module = next(resource for resource in self.template["resources"] if resource["type"] == "Microsoft.Resources/deployments")
        resources = root.child(module)
        visit(resources, resources.template["resources"].items())
        return found

    @staticmethod
    def described(result):
        found = {key: Counter() for key in ("types", "locations", "skus", "roles", "models", "apps")}
        for entry in result["resources"]:
            found["types"][entry["type"]] += entry["count"]
            location = entry["location"] if entry["location"] == "global" else ScopeTemplateMatrixTests.LOCATIONS.get(entry.get("locationSetting"))
            if location:
                found["locations"][(entry["type"], location)] += entry["count"]
            if "sku" in entry:
                found["skus"][(entry["type"], entry["sku"])] += entry["count"]
        found["roles"].update(entry["roleDefinition"] for entry in result["roleAssignments"])
        found["models"].update((model["model"], model["version"], model["sku"], model["capacity"], model["serviceTier"])
                               for model in result["models"])
        found["apps"].update((app["app"], app["minReplicas"], app["maxReplicas"], app["cpu"], app["memoryGiB"])
                             for app in result["containerApps"])
        found["identities"] = sum(identity["kind"] == "system-assigned" for identity in result["identities"])
        return found

    def test_scope_matches_the_compiled_template_for_every_module_combination(self):
        names, compiled, described, checked = list(setup.MODULES), {}, {}, 0
        value = profile()
        for choices in itertools.product((False, True), repeat=len(names)):
            requested = [name for name, enabled in zip(names, choices) if enabled]
            if not requested:
                continue
            resolved = tuple(setup.resolve_modules(requested))
            flags = {key: module in resolved for module, key in setup.FLAGS.items()}
            arm = {parameter: flags[key] for parameter, mapping in self.flags.items() for key in flags
                   if mapping["value"].startswith("${" + key + "=")}
            if resolved not in described:
                value["modules"] = requested
                described[resolved] = self.described(setup.scope(value))
            key = tuple(sorted(arm.items()))
            if key not in compiled:
                compiled[key] = self.compiled(arm)
            self.assertEqual(described[resolved], compiled[key], f"Scope differs from the compiled template for {resolved}.")
            checked += 1
        self.assertEqual(checked, 8191)
        # Every reachable flag set: agent-console adds web-chat, and every module sets at least one flag.
        self.assertEqual(len(compiled), 383)


if __name__ == "__main__":
    unittest.main()
