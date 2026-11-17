"""Publish safety: neutral defaults, owner files, private terms and generic leak checks."""

import io
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "infra/scripts"))
import check_template as check
import template_setup as setup

NEUTRAL_FILES = (
    *setup.RUNTIME_CONFIGS, *setup.UI_CONFIGS, "apps/voice/src/app/profiles.json", "apps/chat/orchestrator/profiles.json",
    "infra/data/knowledge.json", "infra/data/knowledge.md", "infra/data/knowledge-config.json",
    "docs/dynamics-native-voice/examples/crm-seed.example.json",
)
# Sensitive-looking fixtures are assembled at run time so this file passes the check itself.
REAL_GUID = "-".join(("9f3c2a71", "5b4e", "4d2a", "8c1f", "0e9d7b6a5c43"))
REAL_PHONE = "+44" + "2071234567"
REAL_EMAIL = "jane.doe@" + "realcorp.com"
REAL_HOST = "contoso-prod" + ".crm4.dynamics.com"
REAL_KEY = "q8Vb3NcR0tL2xY7pZ1mW4eH9sK6jD5aF0gU3iO8nB2c="
HOME_PATH = "/home/" + "alice/project"


@unittest.skipUnless(shutil.which("git"), "The publish check reads Git's file lists.")
class PublishCheckTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.private = Path(temporary.name)
        self.root = self.private / "checkout"
        self.root.mkdir()
        self.git("init", "-q")
        for relative in NEUTRAL_FILES:
            (self.root / relative).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / relative, self.root / relative)
        self.write(".gitignore", "ignored/\n")
        self.git("add", ".")
        for patcher in (patch.object(check, "ROOT", self.root), patch.object(setup, "ROOT", self.root),
                        patch.object(check, "DEFAULT_TERMS", self.private / "config/private-terms.txt"),
                        patch.dict(os.environ, {}, clear=False)):
            patcher.start()
            self.addCleanup(patcher.stop)
        os.environ.pop(check.TERMS_VARIABLE, None)

    def git(self, *arguments):
        subprocess.run(["git", *arguments], cwd=self.root, check=True, capture_output=True)

    def write(self, relative, content, *, track=False):
        target = self.root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            target.write_bytes(content)
        else:
            target.write_text(content)
        if track:
            self.git("add", relative)
        return target

    def terms(self, content, *, mode=0o600, path=None):
        target = path or self.private / "config/private-terms.txt"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
        target.chmod(mode)
        return target

    def findings(self, **options):
        with self.assertRaises(ValueError) as raised:
            check.check(**options)
        return str(raised.exception).splitlines()

    def test_neutral_checkout_passes_and_notes_skipped_terms(self):
        result = check.check()
        self.assertEqual(result["status"], "verified")
        self.assertTrue(result["privateTerms"].startswith("skipped: no private terms file"))
        self.assertNotIn("historyRewritten", result)

    def test_owner_files_anywhere_in_the_checkout_are_named_with_the_fix(self):
        self.write("brand guide.pdf", b"%PDF-1.7")
        self.write("docs/screens/call.png", b"\x89PNG\r\n\x1a\n")
        self.write("notes/rates.xlsx", b"PK\x03\x04")
        self.write("recordings/call.m4a", b"\x00\x00")
        self.write("ignored/logo.png", b"\x89PNG\r\n\x1a\n")
        self.write("apps/ui/public/icon.png", b"\x89PNG\r\n\x1a\n", track=True)
        self.write("docs/manual.pdf", b"%PDF-1.7", track=True)
        findings = self.findings()
        for name in ("brand guide.pdf", "docs/screens/call.png", "notes/rates.xlsx", "recordings/call.m4a"):
            self.assertIn(name + ": owner-supplied file in the checkout. Move it to your private workspace.", findings)
        self.assertIn("docs/manual.pdf: document or package in the source. Remove it from Git and keep it in your "
                      "private workspace.", findings)
        self.assertFalse([line for line in findings if "ignored/" in line or "icon.png" in line])
        for name in ("brand guide.pdf", "docs/screens/call.png", "notes/rates.xlsx", "recordings/call.m4a", "docs/manual.pdf"):
            shutil.move(self.root / name, self.private / Path(name).name)
        self.git("rm", "-q", "--cached", "docs/manual.pdf")
        self.assertEqual(check.check()["status"], "verified")

    def test_private_terms_match_whole_words_in_content_names_and_tests(self):
        self.terms("# past customers\nQuillfeather\nnorth wind traders\nzorbia\n")
        self.write("docs/a.md", "Built for QUILLFEATHER.\nQuillfeathers is a different word.\n")
        self.write("docs/b.md", "time zone: Asia/Zorbia\nnorth-wind_traders\nnorthwindtraders\n")
        self.write("docs/quillfeather-notes.md", "Neutral text.\n")
        self.write("infra/tests/test_names.py", "VALUE = 'zorbia'\n")
        findings = self.findings()
        self.assertIn("docs/a.md:1: private term (QU…). Fix: remove the customer-identifying content.", findings)
        self.assertIn("docs/b.md:1: private term (Zo…). Fix: remove the customer-identifying content.", findings)
        self.assertIn("docs/b.md:2: private term (no…). Fix: remove the customer-identifying content.", findings)
        self.assertIn("docs/b.md:3: private term (no…). Fix: remove the customer-identifying content.", findings)
        self.assertIn("docs/quillfeather-notes.md: private term in the file name. Rename or remove the file.", findings)
        self.assertIn("infra/tests/test_names.py:1: private term (zo…). Fix: remove the customer-identifying content.", findings)
        self.assertFalse([line for line in findings if line.startswith("docs/a.md:2")])
        self.assertNotIn("quillfeather", "\n".join(line.lower() for line in findings if "file name" not in line))

    def test_private_terms_file_selection_and_failures(self):
        self.write("docs/a.md", "Built for Quillfeather.\n")
        explicit = self.terms("quillfeather\n", path=self.private / "elsewhere/terms.txt")
        self.assertTrue(self.findings(private_terms_file=str(explicit)))
        with patch.dict(os.environ, {check.TERMS_VARIABLE: str(explicit)}):
            self.assertTrue(self.findings())
        self.terms("quillfeather\n")
        self.assertTrue(self.findings())
        failures = (
            ("chmod 600", lambda: self.terms("quillfeather\n", mode=0o644)),
            ("no terms", lambda: self.terms("# only a comment\n\n")),
            ("not found", lambda: self.private / "missing.txt"),
            ("outside the checkout", lambda: self.write("terms.txt", "quillfeather\n")),
        )
        for message, target in failures:
            with self.subTest(message=message), self.assertRaisesRegex(ValueError, message):
                check.check(private_terms_file=str(target()))

    def test_generic_checks_flag_real_values_and_allow_placeholders(self):
        lines = [
            f"tenant {REAL_GUID}", "placeholder 11111111-1111-4111-8111-111111111111",
            "role 5e0bd9bd-7b93-4f28-af87-19fc36ad61bd", "cosmos role 00000000-0000-0000-0000-000000000002",
            f"call {REAL_PHONE}", "drama +44 20 7946 0123", "mobile drama +44 7700 900123", "us +1 202 555 0123",
            "zeros +10000000000", "sequence +1" + "2345678900", "saudi +966" + "512345678",
            f"mail {REAL_EMAIL}", "mail someone@example.com", "mail team@docs.example.test", "mail conduct@" + "realcorp.org",
            "_parentcustomerid_value@Microsoft.Dynamics.CRM.lookuplogicalname", "statuscode@OData.Community.Display.V1.FormattedValue",
            "ownerid@odata.bind",
            f"https://{REAL_HOST}", "https://ca-voice.happyrock-1a2b3c4d" + ".swedencentral.azurecontainerapps.io",
            "https://tenant-voice" + ".sanad.ink", "https://example.crm17.dynamics.com", "https://YOUR-ORG.crm.dynamics.com",
            "privatelink.documents.azure.com", "https://livekit-admin.sanad.ink", "https://unit-test.services.ai.azure.com",
            f"DefaultEndpointsProtocol=https;AccountName=x;AccountKey={REAL_KEY}", "endpoint=https://x;AccessKey=synthetic-test-key",
            "AccountKey=<key>", "-----BEGIN " + "RSA PRIVATE KEY-----",
            "token eyJ" + "hbGciOiJIUzI1NiJ9.eyJ" + "zdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9P",
            "ghp_" + "Ab1" * 12,
            f"cd {HOME_PATH}", "cd /home/<user>/project", "cd /home/user/project", "cd /workspaces/" + "contoso-repo",
        ]
        self.write("docs/sample.md", "\n".join(lines) + "\n")
        with patch.object(check, "PUBLIC_EMAILS", {"conduct@" + "realcorp.org"}):
            findings = [line for line in self.findings() if line.startswith("docs/sample.md")]
        flagged = {int(line.split(":")[1]) for line in findings}
        expected = {lines.index(line) + 1 for line in (
            f"tenant {REAL_GUID}", f"call {REAL_PHONE}", "sequence +1" + "2345678900", "saudi +966" + "512345678",
            f"mail {REAL_EMAIL}", f"https://{REAL_HOST}", "https://ca-voice.happyrock-1a2b3c4d" + ".swedencentral.azurecontainerapps.io",
            "https://tenant-voice" + ".sanad.ink", f"DefaultEndpointsProtocol=https;AccountName=x;AccountKey={REAL_KEY}",
            "-----BEGIN " + "RSA PRIVATE KEY-----",
            "token eyJ" + "hbGciOiJIUzI1NiJ9.eyJ" + "zdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9P",
            "ghp_" + "Ab1" * 12, f"cd {HOME_PATH}", "cd /workspaces/" + "contoso-repo",
        )}
        self.assertEqual(flagged, expected)
        output = "\n".join(findings)
        for value in (REAL_GUID, REAL_PHONE, REAL_EMAIL, REAL_KEY, HOME_PATH):
            self.assertNotIn(value, output)

    def test_markup_ids_lockfile_hashes_and_embedded_data_are_ignored(self):
        self.write("docs/diagram.svg", f'<linearGradient id="grad-{REAL_GUID}"/><rect fill="url(#grad-{REAL_GUID})"/>\n'
                                       f'<use href="#grad-{REAL_GUID}"/>\n<text>{REAL_GUID}</text>\n', track=True)
        aws = "AKIA" + "ABCDEFGHIJKLMNOP"
        jwt = "eyJ" + "hbGciOiJIUzI1NiJ9.eyJ" + "zdWIiOi" + "Jx" * 80 + ".dozjgNryP4J3jVmNHl0w5N_XgL0n3I9P"
        self.write("package-lock.json", f'{{"integrity": "sha512-{aws}{"Q" * 60}=="}}\n')
        self.write("docs/page.html", f'<style>src: url(data:font/woff2;base64,{aws}{"Q" * 40})</style>\n'
                                     f'{"A" * 40}/{aws}/{"B" * 100}\n<p>{aws}</p>\n<p>{jwt}</p>\n')
        findings = self.findings()
        self.assertEqual([line.split(":")[:2] for line in findings],
                         [["docs/diagram.svg", "3"], ["docs/page.html", "3"], ["docs/page.html", "4"]])

    def test_local_paths_are_allowed_only_in_tests_that_assert_their_absence(self):
        absent = 'self.assertNotIn("' + HOME_PATH + '", text)\n'
        self.write("infra/tests/test_portable.py", absent + 'FORBIDDEN = ("' + HOME_PATH + '",)\n' + 'ROOT = "' + HOME_PATH + '"\n')
        self.write("docs/guide.md", absent)
        findings = self.findings()
        self.assertEqual([line.split(":")[:2] for line in findings], [["docs/guide.md", "1"], ["infra/tests/test_portable.py", "3"]])

    def test_neutral_defaults_name_the_file(self):
        config = self.root / setup.RUNTIME_CONFIGS[0]
        config.write_text(config.read_text().replace('"Contact center"', '"Northwind Services"', 1))
        self.assertIn(setup.RUNTIME_CONFIGS[0] + ": customer branding is not neutral. Restore the neutral default.", self.findings())
        self.write("deployment-ledger.json", "{}")
        with self.assertRaisesRegex(ValueError, "neutral source checkout"):
            check.check()

    def test_command_exits_non_zero_with_file_line_findings(self):
        self.write("docs/sample.md", "\n".join(f"id {REAL_GUID[:-1]}{index}" for index in range(7)) + "\n")
        stderr = io.StringIO()
        with patch("sys.stderr", stderr), patch("sys.stdout", io.StringIO()) as stdout:
            self.assertEqual(check.main([]), 1)
        lines = stderr.getvalue().splitlines()
        self.assertEqual(lines[0], "Publish check failed: 7 findings. Fix them before you commit or prepare.")
        self.assertTrue(lines[1].startswith("docs/sample.md:1: identifier that isn't a placeholder (9f3c2a71-…). Fix: "))
        self.assertEqual(lines[-1], "docs/sample.md: 2 more guid findings.")
        self.assertEqual(stdout.getvalue(), "")
        terms = self.terms("realcorp\n", path=self.private / "terms.txt")
        self.write("docs/sample.md", "Neutral.\n")
        self.write("docs/other.md", "Realcorp\n")
        with patch("sys.stderr", io.StringIO()) as stderr:
            self.assertEqual(check.main(["--private-terms", str(terms)]), 1)
        self.assertIn("docs/other.md:1: private term", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
