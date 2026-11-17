"""Check the AI-led deployment entry point against the restored deployment scope."""

import json
import re
import unittest
from pathlib import Path
from urllib.parse import unquote, urlsplit

import yaml

from test_native_voice_docs import headings


ROOT = Path(__file__).resolve().parents[2]
GUIDE = ROOT / "docs/dynamics-native-voice/deploy-with-copilot.md"
SKILL = ROOT / ".github/skills/deploy-dynamics-native/SKILL.md"


class DeploymentHandoffTests(unittest.TestCase):
    def test_interruption_policy_is_wired_with_the_accepted_default(self):
        parameters = json.loads((ROOT / "infra/main.parameters.json").read_text())["parameters"]
        self.assertEqual(parameters["humainInterruptionPolicyMode"]["value"],
                         "${HUMAIN_INTERRUPTION_POLICY_MODE=off}")
        main = (ROOT / "infra/main.bicep").read_text()
        resources = (ROOT / "infra/resources.bicep").read_text()
        for template in (main, resources):
            self.assertIn("param humainInterruptionPolicyMode string = 'off'", template)
        self.assertIn("humainInterruptionPolicyMode: humainInterruptionPolicyMode", main)
        self.assertIn("{ name: 'HUMAIN_INTERRUPTION_POLICY_MODE', value: humainInterruptionPolicyMode }", resources)
        self.assertIn("HUMAIN_INTERRUPTION_POLICY_MODE=off", (ROOT / "apps/voice/.env.example").read_text())

    def test_turn_strategy_is_wired_with_the_accepted_fast_default(self):
        parameters = json.loads((ROOT / "infra/main.parameters.json").read_text())["parameters"]
        self.assertEqual(parameters["humainTurnTakingStrategy"]["value"],
                         "${HUMAIN_TURN_TAKING_STRATEGY=fast_vad}")
        main = (ROOT / "infra/main.bicep").read_text()
        resources = (ROOT / "infra/resources.bicep").read_text()
        for template in (main, resources):
            self.assertIn("param humainTurnTakingStrategy string = 'fast_vad'", template)
        self.assertIn("humainTurnTakingStrategy: humainTurnTakingStrategy", main)
        self.assertIn("{ name: 'HUMAIN_TURN_TAKING_STRATEGY', value: humainTurnTakingStrategy }", resources)
        self.assertIn("HUMAIN_TURN_TAKING_STRATEGY=fast_vad", (ROOT / "apps/voice/.env.example").read_text())

    def test_accepted_turn_policy_is_in_the_runbook_and_skill(self):
        for document in (GUIDE, SKILL, GUIDE.parent / "README.md"):
            source = document.read_text()
            with self.subTest(document=document.name):
                self.assertIn("HUMAIN_TURN_TAKING_STRATEGY=fast_vad", source)
                self.assertIn("HUMAIN_INTERRUPTION_POLICY_MODE=off", source)

    def test_ledger_starts_unapproved_and_separates_acceptance_from_configuration(self):
        ledger = json.loads((GUIDE.parent / "examples/deployment-ledger.example.json").read_text())
        self.assertEqual(ledger["currentStage"], 0)
        self.assertEqual(ledger["status"], "pending")
        self.assertEqual(ledger["stageRecords"], [])
        self.assertIsNone(ledger["stageRecordFormat"]["approvalReference"])
        self.assertIsNone(ledger["propagation"]["callerAcceptanceNotBeforeUtc"])
        self.assertTrue(all(value == "not-tested" for value in ledger["acceptance"].values()))
        self.assertTrue(ledger["secretsMustNotBeStoredInThisLedger"])
        self.assertEqual(ledger["secretLocations"], [])

    def test_skill_is_discoverable_and_points_to_the_execution_guide(self):
        source = SKILL.read_text()
        self.assertTrue(source.startswith("---\n"))
        metadata = yaml.safe_load(source.split("---", 2)[1])
        self.assertEqual(metadata["name"], SKILL.parent.name)
        self.assertIsInstance(metadata["description"], str)
        self.assertIn("Deploy", metadata["description"])
        self.assertIn("handover", metadata["description"])
        self.assertIn("deploy-with-copilot.md", source)
        self.assertNotIn("model:", source)
        self.assertNotIn("gpt-", source)

    def test_execution_order_covers_both_platforms_and_human_gates(self):
        source = GUIDE.read_text()
        stages = re.findall(r"^## Stage (\d+): (.+)$", source, re.MULTILINE)
        self.assertEqual([int(number) for number, _ in stages], list(range(13)))
        for component in (
            "azure.yaml", "main.bicep", "dynamics-voice-poc.bicep",
            "dynamics-handover-probe.bicep", "setup_dynamics_crm.py",
            "setup_dynamics_workspace.py", "setup_native_supervision.py",
            "check_native_supervision_runtime.py", "--base-receipt",
            "--approve-extension", "--confirm-organization", "startup-audio",
            "--confirm-notice-policy", "not_before_call_acceptance",
        ):
            self.assertIn(component, source)
        for human_gate in (
            "licens", "eligibility", "charges", "Topic checker", "HUMAIN_AUTHORIZATION",
            "representative", "15 minutes", "recording/transcription", "ARMED",
        ):
            self.assertIn(human_gate, source)
        self.assertIn("same Entra", source)
        self.assertIn("accepted on a real call", source)
        self.assertIn("different kinds of evidence", source)

    def test_accepted_audio_contract_and_parked_delay_are_explicit(self):
        source = " ".join(GUIDE.read_text().split())
        for value in (
            "play_startup_notice: true", "wait_for_startup=true",
            "--startup-notice --crm-case-context", "--permanent --fast-start",
            "5-7 second", "work in progress", "on hold",
        ):
            self.assertIn(value, source)
        for document in (GUIDE, SKILL, ROOT / "docs/dynamics-native-voice/README.md"):
            text = " ".join(document.read_text().split())
            with self.subTest(document=document.name):
                self.assertIn("5-7 second", text)
                self.assertIn("on hold", text)
                self.assertNotIn("startup_latency_optimization_version: 1", text)
        native_source = (ROOT / "apps/voice/src/app/backend/native_media.py").read_text()
        self.assertNotIn("startup_latency_optimization_version", native_source)
        self.assertFalse((ROOT / "apps/voice/tests/test_native_startup_latency.py").exists())

    def test_fresh_commands_do_not_replay_experiment_migrations_or_calls(self):
        source = GUIDE.read_text()
        blocks = re.findall(r"```bash\n(.*?)\n```", source, re.DOTALL)
        self.assertGreaterEqual(len(blocks), 6)
        for block in blocks:
            with self.subTest(block=block.splitlines()[0]):
                self.assertNotRegex(block, r"\bupgrade-(?:native|application|startup)")
                self.assertNotIn("--legacy-baseline", block)
                self.assertNotIn("capture-legacy-baseline", block)
                self.assertNotIn("watch_native_voice.py", block)
                self.assertNotIn("azd up", block)
                self.assertNotIn("git reset", block)
                self.assertNotIn("eval ", block)
                self.assertNotIn("env get-values", block)

    def test_new_entry_points_have_only_portable_resolvable_links(self):
        for document in (GUIDE, SKILL, ROOT / "docs/deployment.md",
                         ROOT / ".github/skills/deploy-contact-center/SKILL.md",
                         ROOT / "docs/channel-integration.md"):
            source = document.read_text()
            self.assertNotIn("/home/codespace/", source)
            self.assertNotIn("/workspaces/moacc", source)
            self.assertNotRegex(source, r"\b[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\b")
            for target in re.findall(r"\[[^\]]*\]\(([^)\s]+)\)", source):
                url = urlsplit(target)
                if url.scheme or url.netloc:
                    continue
                path = (document.parent / unquote(url.path)).resolve() if url.path else document
                with self.subTest(document=document.name, target=target):
                    self.assertTrue(path.is_relative_to(ROOT))
                    self.assertTrue(path.exists())
                    if url.fragment and path.suffix == ".md":
                        self.assertIn(unquote(url.fragment), headings(path.read_text()))

    def test_modular_entry_point_preserves_private_content_and_real_executors(self):
        source = (ROOT / "docs/deployment.md").read_text()
        for contract in (
            "template_setup.py prepare", "template_setup.py preview", "template_setup.py configure",
            "template_setup.py record", "template_setup.py status", "--include-reviewed-worktree",
            "--approve-seed", "extension-review.json", "additionalTools",
            "phonePolicy.allowedOutboundNumbers", "Canonical-SHA256", "component",
            "accepted on a real call", "Human stop", "Git", "Bicep",
        ):
            self.assertIn(contract, source)
        self.assertIn("one question about additional tools", source)
        self.assertIn("not executable", source)
        for rule in ("Stop after the second failure", "CLI first", "Read back after every write"):
            self.assertIn(rule, source)
        skill = ROOT / ".github/skills/deploy-contact-center/SKILL.md"
        metadata = yaml.safe_load(skill.read_text().split("---", 2)[1])
        self.assertEqual(metadata["name"], "deploy-contact-center")
        self.assertIn("structured questions", skill.read_text())


if __name__ == "__main__":
    unittest.main()
