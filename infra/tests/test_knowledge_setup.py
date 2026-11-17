import importlib.util
import io
import json
import copy
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

scripts = Path(__file__).resolve().parents[1] / "scripts"


def module(name):
    spec = importlib.util.spec_from_file_location(name, scripts / f"{name}.py")
    loaded = importlib.util.module_from_spec(spec)
    sys.modules[name] = loaded
    spec.loader.exec_module(loaded)
    return loaded


setup = module("setup_knowledge")


class KnowledgeProvisioningTests(unittest.TestCase):
    def test_request_uses_real_bearer_auth_and_pinned_supported_api(self):
        requests = []

        def open_request(request, **_kwargs):
            requests.append(request)
            return io.BytesIO(b'{"ok": true}')

        with patch.object(setup, "urlopen", side_effect=open_request):
            self.assertTrue(setup.request("https://search.example", "indexes/example", "PUT", "unit-test-token", {})["ok"])
        self.assertEqual(requests[0].get_header("Authorization"), "Bearer unit-test-token")
        self.assertIn("api-version=2026-08-01-preview", requests[0].full_url)

    def test_repository_has_no_bundled_customer_corpus_or_approved_domains(self):
        self.assertEqual(json.loads((scripts.parent / "data/knowledge.json").read_text()), [])
        configuration = json.loads((scripts.parent / "data/knowledge-config.json").read_text())
        self.assertEqual(configuration["allowedDomains"], [])
        self.assertFalse(configuration["approved"])

    def test_only_approved_deployment_sources_are_accepted(self):
        configuration = {"schemaVersion": 1, "approved": True, "mode": "indexed", "allowedDomains": ["example.org"]}
        document = {"id": "technical-fixture", "title": "Reference", "content": "Technical fixture.",
                    "sourceUrl": "https://example.org/source", "approvalStatus": "approved",
                    "language": "en", "retrievedAt": "2026-01-01T00:00:00Z"}
        with tempfile.TemporaryDirectory() as directory:
            config, corpus = Path(directory) / "config.json", Path(directory) / "corpus.json"
            config.write_text(json.dumps(configuration))
            corpus.write_text(json.dumps([document]))
            self.assertEqual(setup.load_inputs(config, corpus)[1], [document])
            for url in ("http://example.org/source", "https://user@example.org/source", "https://example.org.evil.invalid/source"):
                corpus.write_text(json.dumps([{**document, "sourceUrl": url}]))
                with self.subTest(url=url), self.assertRaises(ValueError):
                    setup.load_inputs(config, corpus)
            corpus.write_text("[]")
            with self.assertRaisesRegex(ValueError, "nonempty"):
                setup.load_inputs(config, corpus)
            config.write_text(json.dumps({**configuration, "mode": "web"}))
            self.assertEqual(setup.load_inputs(config, corpus)[1], [])
            config.write_text(json.dumps({**configuration, "approved": False}))
            with self.assertRaisesRegex(ValueError, "approved"):
                setup.load_inputs(config, corpus)
