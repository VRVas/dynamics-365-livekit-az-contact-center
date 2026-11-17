import importlib.util
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("sync_prompt_knowledge", ROOT / "infra/scripts/sync_prompt_knowledge.py")
sync = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sync)


class EmbeddedKnowledgeBundleTests(unittest.TestCase):
    def test_neutral_canonical_content_is_identical_in_each_package(self):
        summary = sync.sync(check=True)
        self.assertEqual(summary["topics"], 0)
        self.assertEqual(summary["sources"], 0)
        self.assertIn("Status: unconfigured", sync.SOURCE.read_text())
        self.assertLessEqual(summary["bytes"], 64 * 1024)

    def test_private_approved_content_has_no_fixed_industry_topic_count(self):
        source = b'# Organization service knowledge\n\nRevision: unit-v1\n\n### 01. Technical topic\n\nApproved text [S01].\n\n[S01]: https://example.org/source "Source"\n'
        result = sync.validate(source)
        self.assertEqual(result["topics"], 1)
        self.assertEqual(result["sources"], 1)
        for invalid in (
            source.replace(b"Revision: unit-v1", b"Missing revision"),
            source.replace(b"[S01]: https://example.org/source", b"[S01]: http://example.org/source"),
            source + b"\n### 01. Duplicate\n",
            source.replace(b"Revision: unit-v1", b"Revision: unit-v1\nStatus: unconfigured"),
        ):
            with self.subTest(invalid=invalid[:80]), self.assertRaises(ValueError):
                sync.validate(invalid)

    def test_missing_stale_or_truncated_bundles_fail_explicitly(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "knowledge.md"
            with patch.object(sync, "TARGETS", (target,)):
                with self.assertRaises(RuntimeError):
                    sync.sync(check=True)
                sync.sync()
                self.assertEqual(target.read_bytes(), sync.SOURCE.read_bytes())
                target.write_text("Incomplete")
                with self.assertRaises(RuntimeError):
                    sync.sync(check=True)
        with self.assertRaises(ValueError):
            sync.validate(b"")

    def test_context_budget_is_still_enforced(self):
        with self.assertRaisesRegex(ValueError, "64 KiB"):
            sync.validate(b"# Organization service knowledge\nRevision: unit-v1\n" + b"x" * (64 * 1024))
