"""Keep the deployment runbook navigable as the integration evolves."""

import base64
import html
import json
import re
import shlex
import shutil
import subprocess
import textwrap
import unittest
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlsplit

from native_voice_setup import parser


ROOT = Path(__file__).resolve().parents[2]
GUIDE = ROOT / "docs/dynamics-native-voice"
DIAGRAM_VIEWS = {
    "native-call-flow-audio": "0 0 1280 720",
    "native-call-flow-handover": "0 0 1280 720",
    "feature-voice-flow": "0 0 1040 320",
    "feature-crm-flow": "0 0 1040 336",
    "feature-handover-flow": "0 0 1040 320",
    "feature-workspace-view": "0 0 1040 384",
    "feature-pause-flow": "0 0 1040 320",
}


class SvgFragments(HTMLParser):
    def __init__(self, source):
        super().__init__()
        self.source = source
        self.starts = [0]
        for match in re.finditer("\n", source):
            self.starts.append(match.end())
        self.depth = 0
        self.start = 0
        self.fragments = []
        self.feed(source)

    def source_offset(self):
        line, column = self.getpos()
        return self.starts[line - 1] + column

    def handle_starttag(self, tag, attrs):
        if tag == "svg":
            if self.depth == 0:
                self.start = self.source_offset()
            self.depth += 1

    def handle_endtag(self, tag):
        if tag == "svg":
            self.depth -= 1
            if self.depth == 0:
                end = self.source.index(">", self.source_offset()) + 1
                self.fragments.append(self.source[self.start:end])


class FeatureCards(HTMLParser):
    def __init__(self, source):
        super().__init__()
        self.cards = []
        self.current = None
        self.in_points = False
        self.feed(source)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        classes = attrs.get("class", "").split()
        if tag == "details" and "feature-card" in classes:
            self.current = {
                "id": attrs["id"], "rank": int(attrs["data-feature-rank"]),
                "tier": attrs["data-feature-tier"], "visuals": [], "integration": 0, "points": 0,
            }
        elif self.current is not None:
            if tag == "svg":
                self.current["visuals"].append(attrs.get("id"))
            elif tag == "p" and "feature-integration" in classes:
                self.current["integration"] += 1
            elif tag == "ul" and "feature-points" in classes:
                self.in_points = True
            elif tag == "li" and self.in_points:
                self.current["points"] += 1

    def handle_endtag(self, tag):
        if tag == "ul":
            self.in_points = False
        elif tag == "details" and self.current is not None:
            self.cards.append(self.current)
            self.current = None


def headings(source):
    result = set()
    counts = {}
    fence = None
    for line in source.splitlines():
        marker = re.match(r"^\s*(`{3,}|~{3,})", line)
        if marker:
            if fence is None:
                fence = marker[1][0]
            elif marker[1][0] == fence:
                fence = None
            continue
        if fence:
            continue
        match = re.match(r"^#{1,6}\s+(.+?)\s*#*\s*$", line)
        if not match:
            continue
        slug = re.sub(r"[^\w\- ]", "", match[1].lower()).replace(" ", "-")
        count = counts.get(slug, 0)
        result.add(slug + (f"-{count}" if count else ""))
        counts[slug] = count + 1
    return result


class NativeVoiceDocumentationTests(unittest.TestCase):
    def test_diagrams_have_accessible_names_and_internal_icon_references(self):
        source = (GUIDE / "native-call-flow.html").read_text()
        fragments = SvgFragments(source).fragments
        self.assertEqual(len(fragments), len(DIAGRAM_VIEWS))
        self.assertEqual({ET.fromstring(fragment).get("id") for fragment in fragments}, set(DIAGRAM_VIEWS))
        ids = set()
        for fragment in fragments:
            svg = ET.fromstring(fragment)
            self.assertEqual(svg.attrib["viewBox"], DIAGRAM_VIEWS[svg.attrib["id"]])
            self.assertEqual(svg.attrib["role"], "img")
            self.assertEqual(svg[0].tag, "{http://www.w3.org/2000/svg}title")
            by_id = {}
            for element in svg.iter():
                identifier = element.get("id")
                if identifier:
                    self.assertNotIn(identifier, ids)
                    ids.add(identifier)
                    by_id[identifier] = element
                if element.get("class") == "arrow-text":
                    self.assertLessEqual(len(element.text), 14)
                    self.assertEqual(element.text, element.text.upper())
                if element.get("class") == "generic-icon":
                    self.assertEqual(element.get("fill"), "none")
                    self.assertEqual(element.get("stroke"), "#4f5d75")
            labels = svg.attrib["aria-labelledby"].split()
            self.assertEqual(len(labels), 2)
            for identifier in labels:
                self.assertTrue(identifier.startswith(svg.attrib["id"] + "-"))
                self.assertTrue(by_id[identifier].text.strip())
            self.assertLessEqual(len(svg[0].text), 60)
            for element in svg.iter():
                for key, value in element.attrib.items():
                    if key.endswith("href"):
                        self.assertTrue(value.startswith("#"))
                        self.assertIn(value[1:], by_id)
                    for identifier in re.findall(r"url\(#([^)]+)\)", value):
                        self.assertIn(identifier, by_id)
            nodes = [node for node in svg.iter() if node.get("class") in {"node", "feature-node"}]
            self.assertLessEqual(len(nodes), 9)
            if svg.attrib["id"].startswith("feature-"):
                self.assertLessEqual(len([node for node in svg.iter() if node.get("class") == "ff-focal"]), 2)
                for line in svg.iter("{http://www.w3.org/2000/svg}line"):
                    self.assertTrue(
                        line.get("x1") == line.get("x2") or line.get("y1") == line.get("y2"),
                        f"Non-orthogonal feature-diagram line: {line.attrib}",
                    )

    def test_diagrams_keep_reference_fonts_without_loading_remote_assets(self):
        source = (GUIDE / "native-call-flow.html").read_text()
        reference = (ROOT / "docs/architecture/voice-before-after.html").read_text()
        font_pattern = r'<style id="embedded-fonts">(.*?)</style>'
        fonts = re.search(font_pattern, source, re.DOTALL)[1]
        self.assertEqual(fonts, re.search(font_pattern, reference, re.DOTALL)[1])
        self.assertNotRegex(source, r"(?i)<(?:iframe|object|embed|img|image|link)\b")
        self.assertEqual(len(re.findall(r"(?i)<script\b", source)), 2)
        self.assertIn('<script id="diagram-downloads">', source)
        self.assertIn('<script id="page-navigation">', source)
        markup = re.sub(r"<script\b[^>]*>.*?</script>", "", source, flags=re.IGNORECASE | re.DOTALL)
        self.assertIsNone(re.search(r"\bon[a-z]+\s*=", markup, re.IGNORECASE),
                          "Inline HTML/SVG event-handler attributes are not allowed.")
        styles = "\n".join(re.findall(r"<style\b[^>]*>(.*?)</style>", source, re.DOTALL))
        self.assertNotRegex(styles, r"(?i)@import|image-set|(?:drop|box)-shadow")
        font_count = 0
        for value in re.findall(r"url\(([^)]+)\)", styles):
            if value.startswith("#"):
                continue
            self.assertTrue(value.startswith("data:font/ttf;base64,"), "Only embedded TTF fonts may use non-fragment CSS URLs.")
            binary = base64.b64decode(value.split(",", 1)[1], validate=True)
            self.assertEqual(binary[:4], b"\x00\x01\x00\x00")
            font_count += 1
        self.assertEqual(font_count, 8)
        self.assertIn("Embedded font license notices:", source)
        self.assertIn("Diagram Design license notice:", source)

    def test_native_downloads_reuse_the_standalone_architecture_exporter(self):
        source = (GUIDE / "native-call-flow.html").read_text()
        reference = (ROOT / "docs/architecture/voice-before-after.html").read_text()
        pattern = r'<script id="diagram-downloads">(.*?)</script>'
        script = re.search(pattern, source, re.DOTALL)[1]
        self.assertEqual(script, re.search(pattern, reference, re.DOTALL)[1])
        self.assertEqual(source.count('data-export-format="svg" disabled'), 7)
        self.assertEqual(source.count('data-export-format="png" disabled'), 7)
        self.assertEqual(source.count('class="export-status" role="status" aria-live="polite"'), 7)
        self.assertIn("<noscript>", source)
        self.assertNotRegex(script, r"\b(?:fetch|XMLHttpRequest|WebSocket|eval)\s*\(")
        for contract in (
            "document.fonts.ready", "getComputedStyle(element)", "new XMLSerializer()",
            "canvas.width = svg.width * 2", "canvas.height = svg.height * 2",
            "URL.revokeObjectURL", "Export failed: ", "console.error",
        ):
            self.assertIn(contract, script)
        ids = {ET.fromstring(fragment).get("id") for fragment in SvgFragments(source).fragments}
        self.assertEqual(ids, set(DIAGRAM_VIEWS))
        if shutil.which("node"):
            result = subprocess.run(["node", "--check"], input=script, text=True, capture_output=True, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_feature_breakdown_has_exact_requested_tiers_and_visuals(self):
        source = (GUIDE / "native-call-flow.html").read_text()
        cards = FeatureCards(source).cards
        self.assertEqual([card["rank"] for card in cards], list(range(1, 21)))
        for card in cards:
            rank = card["rank"]
            self.assertEqual(card["id"], f"feature-{rank:02}")
            self.assertEqual(card["tier"], "visual" if rank <= 5 else "integration" if rank <= 10 else "summary")
            self.assertEqual(len(card["visuals"]), 1 if rank <= 5 else 0)
            self.assertEqual(card["integration"], 1 if rank <= 10 else 0)
            self.assertEqual(card["points"], 3 if rank <= 10 else 2)
        extras = re.search(r'<section class="feature-extras".*?</section>', source, re.DOTALL)[0]
        self.assertEqual(len(re.findall(r"<li>", extras)), 12)
        self.assertNotRegex(extras, r"<(?:p|details|svg)\b")

    def test_feature_evidence_and_links_do_not_claim_unverified_rollouts(self):
        source = (GUIDE / "native-call-flow.html").read_text()
        features = source[source.index('<section id="features"'):source.index("  <footer>")]
        for boundary in (
            "approved synthetic profiles", "Case Details panel", "in each deployment",
            "verify it in your tenant", "Non-admin", "5-7 second",
            "Verified in test deployments", "1,200 seconds", "3,000-cycle",
        ):
            self.assertTrue(boundary in features, f"Missing feature boundary: {boundary}")
        for href in re.findall(r'\bhref="([^"]+)"', features):
            target = urlsplit(html.unescape(href))
            self.assertFalse(target.scheme, "Feature evidence should use existing repository sources.")
            path = (GUIDE / unquote(target.path)).resolve()
            self.assertTrue(path.exists(), href)
            if target.fragment and path.suffix == ".md":
                self.assertIn(target.fragment, headings(path.read_text()), href)
        text = html.unescape(re.sub(r"<[^>]+>", " ", features))
        self.assertNotRegex(
            text.lower(),
            r"\b(?:delve|foster|leverage|utilize|facilitate|empower|streamline|robust|"
            r"cutting-edge|paradigm shift|game changer|transformative|supercharge|harness)\b",
        )

    def test_native_tabs_keep_keyboard_hash_print_and_no_script_fallbacks(self):
        source = (GUIDE / "native-call-flow.html").read_text()
        for name in ("audio", "handover", "features"):
            self.assertIn(f'id="tab-{name}" href="#{name}"', source)
            panel = re.search(rf'<(?:figure|section) id="{name}"[^>]*>', source)[0]
            self.assertIn("tab-panel", panel)
            self.assertNotIn("hidden", panel)
        self.assertEqual(source.count('data-feature-action="expand"'), 1)
        self.assertEqual(source.count('data-feature-action="collapse"'), 1)
        self.assertIn(".tab-panel[hidden] { display: block !important; }", source)
        script = re.search(r'<script id="page-navigation">(.*?)</script>', source, re.DOTALL)[1]
        for contract in (
            "'tablist'", "'tab'", "'tabpanel'", "'aria-selected'", "'aria-controls'",
            "'ArrowRight'", "'ArrowLeft'", "'Home'", "'End'", "'hashchange'",
            "'beforeprint'", "'afterprint'", "detail.open = true", "history.replaceState",
        ):
            self.assertIn(contract, script)
        self.assertNotRegex(script, r"\b(?:fetch|XMLHttpRequest|WebSocket|eval)\s*\(")
        if shutil.which("node"):
            result = subprocess.run(["node", "--check"], input=script, text=True, capture_output=True, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_architecture_separates_current_native_baseline_from_proposals_and_history(self):
        source = (ROOT / "docs/architecture/voice-before-after.html").read_text()
        current = re.search(r'<section id="current"[^>]*>(.*?)</section>', source, re.DOTALL)[1]
        self.assertLess(source.index('id="tab-current"'), source.index('id="tab-production"'))
        for contract in (
            "2 Nov 2026", "Dynamics voice / native ACS", "HUMAIN / ALLAM", "Calling SDK",
            "/api/cases", "Dataverse", "synthetic", "CaseNumber", "Pause AI",
            "5-7 second", "on hold", "20-minute", "in each deployment",
            "does not pause", "before Copilot transfers", "dedicated CRM managed identity",
        ):
            self.assertIn(contract, current)
        svg = ET.fromstring(SvgFragments(current).fragments[0])
        self.assertEqual(svg.get("id"), "current-native-architecture")
        self.assertEqual(svg.get("viewBox"), "0 0 1920 1080")
        self.assertEqual(svg.get("role"), "img")
        by_id = {node.get("id"): node for node in svg.iter() if node.get("id")}
        for identifier in svg.get("aria-labelledby").split():
            self.assertTrue(by_id[identifier].text.strip())
        self.assertEqual(len([node for node in svg.iter() if node.get("data-node")]), 9)
        for node in svg.iter():
            for value in node.attrib.values():
                for identifier in re.findall(r"url\(#([^)]+)\)", value):
                    self.assertIn(identifier, by_id)
        for label in (
            "HISTORICAL AZURE-ONLY INVENTORY", "HISTORICAL PROVIDER MIGRATION",
            "EXTERNAL-IVR PROPOSAL", "Do not reuse this release ordering",
            "23 October 2026",
        ):
            self.assertIn(label, source)
        for stale in (
            "Dynamics CRM and Dynamics Contact Center integrations are not implemented.",
            "Neither snapshot includes the proposed Dynamics integrations.",
            "Browser and inbound phone transfer remain disabled.",
            "Current Azure and HUMAIN deployment",
        ):
            self.assertNotIn(stale, source)

    def test_native_figures_document_current_startup_crm_and_pause_boundaries(self):
        source = (GUIDE / "native-call-flow.html").read_text()
        for contract in (
            "2 November 2026", "1.3x recording notice", "5-7 second", "on hold",
            "confirmed notice cancellation", "language acknowledgement", "/api/cases",
            "dedicated CRM managed identity", "synthetic", "identity verification",
            "Global.CaseNumber", "Prefetched context is not selection", "Case Details",
            "Monitor/Join alone does not pause", "non-admin", "in each deployment",
            "does not transfer or automatically resume",
        ):
            self.assertTrue(contract in source, f"Missing native call-flow contract: {contract}")
        audio = ET.fromstring(SvgFragments(source).fragments[0])
        self.assertEqual({node.get("data-node") for node in audio.iter() if node.get("data-node")}, {
            "caller", "copilot-controller", "native-call", "observer", "media-bridge",
            "humain", "voip-microphone", "dataverse", "native-workspace",
        })

    def test_html_diagram_links_and_anchors_resolve(self):
        class Links(HTMLParser):
            def __init__(self, source):
                super().__init__()
                self.ids, self.links = set(), []
                self.feed(source)

            def handle_starttag(self, tag, attrs):
                attrs = dict(attrs)
                if attrs.get("id"):
                    self.ids.add(attrs["id"])
                if tag == "a" and attrs.get("href"):
                    self.links.append(attrs["href"])

        pages = [GUIDE / "native-call-flow.html", ROOT / "docs/architecture/voice-before-after.html"]
        parsed = {path: Links(path.read_text()) for path in pages}
        for document, contents in list(parsed.items()):
            for target in contents.links:
                url = urlsplit(target)
                if url.scheme or url.netloc:
                    continue
                path = (document.parent / unquote(url.path)).resolve() if url.path else document
                with self.subTest(document=document.name, target=target):
                    self.assertTrue(path.is_relative_to(ROOT))
                    self.assertTrue(path.is_file())
                    if url.fragment:
                        if path.suffix == ".md":
                            self.assertIn(unquote(url.fragment), headings(path.read_text()))
                        elif path.suffix == ".html":
                            if path not in parsed:
                                parsed[path] = Links(path.read_text())
                            self.assertIn(unquote(url.fragment), parsed[path].ids)

    def test_documented_setup_commands_match_the_real_cli(self):
        values = {
            "TENANT_ID": "11111111-1111-4111-8111-111111111111",
            "BOT_ID": "22222222-2222-4222-8222-222222222222",
            "WORKSTREAM_ID": "33333333-3333-4333-8333-333333333333",
            "BUSINESS_UNIT_ID": "44444444-4444-4444-8444-444444444444",
            "UAMI_CLIENT_ID": "55555555-5555-4555-8555-555555555555",
            "UAMI_PRINCIPAL_ID": "66666666-6666-4666-8666-666666666666",
            "EVENTGRID_APP_ID": "77777777-7777-4777-8777-777777777777",
            "NATIVE_ACS_ID": "/subscriptions/88888888-8888-4888-8888-888888888888/resourceGroups/native"
                             "/providers/Microsoft.Communication/communicationServices/native-acs",
            "ORG_URL": "https://example.crm4.dynamics.com",
            "VOICE_ORIGIN": "https://voice.example.azurecontainerapps.io",
        }
        seen = set()
        for document in GUIDE.glob("*.md"):
            source = document.read_text().replace("\\\n", " ")
            for command in re.findall(r"(?m)^[ \t]*python infra/scripts/native_voice_setup\.py ([^\n]+)", source):
                arguments = [values.get(arg.removeprefix("$"), arg) for arg in shlex.split(command)]
                if arguments == ["--help"]:
                    continue
                with self.subTest(document=document.name, command=arguments[0]):
                    parsed = parser().parse_args(arguments)
                    seen.add(parsed.command)
        self.assertEqual(seen, {
            "new-keys", "runtime-env", "discover", "bundle", "stage",
            "reader-role", "verify", "observer-parameters", "case-context-variable",
            "startup-audio", "agent-baseline", "context-variable", "connection", "copy-sheet",
        })

    @unittest.skipUnless(shutil.which("bash"), "Runbook commands target Linux, WSL or a dev container.")
    def test_bash_examples_parse_without_execution(self):
        for document in GUIDE.glob("*.md"):
            blocks = re.findall(r"(?ms)^([ \t]*)```bash\n(.*?)\n\1```", document.read_text())
            for index, (_, source) in enumerate(blocks, 1):
                with self.subTest(document=document.name, block=index):
                    result = subprocess.run(["bash", "-n"], input=textwrap.dedent(source), capture_output=True, text=True, check=False)
                    self.assertEqual(result.returncode, 0, result.stderr)

    def test_local_links_and_section_anchors_resolve(self):
        for document in [*GUIDE.glob("*.md"), ROOT / "README.md", ROOT / "apps/voice/README.md"]:
            source = document.read_text()
            for target in re.findall(r"\[[^\]]*\]\(([^)\s]+)\)", source):
                url = urlsplit(target)
                if url.scheme or url.netloc:
                    continue
                if document.parent != GUIDE and not any(value in target for value in (
                    "docs/dynamics-native-voice", "native-speech-turn-pilot.topic.yaml", "native_voice_setup.py",
                )):
                    continue
                path = (document.parent / unquote(url.path)).resolve() if url.path else document
                with self.subTest(document=document.name, target=target):
                    self.assertTrue(path.is_relative_to(ROOT), "Runbook links must not depend on a private workspace.")
                    self.assertTrue(path.exists(), "Broken local deployment link.")
                    if url.fragment and path.suffix == ".md":
                        self.assertIn(unquote(url.fragment), headings(path.read_text()), "Broken section anchor.")

    def test_portable_kit_and_runbook_do_not_embed_deployment_identifiers(self):
        forbidden = ("/home/codespace/", "/workspaces/moacc")
        identifiers = re.compile(
            r"\b[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}\b"
            r"|\+[1-9][0-9]{7,14}\b"
        )
        # Microsoft's first-party Microsoft.EventGrid application ID is public, not deployment data.
        public_identifiers = {"4962773b-9cdb-44cf-a8bf-237846a00ab7"}
        files = list(GUIDE.rglob("*"))
        files += [ROOT / "infra/scripts" / name for name in (
            "native_voice_setup.py", "watch_native_voice.py", "sync_native_voice_topic.py",
            "setup_dynamics_crm.py", "setup_dynamics_workspace.py",
            "setup_dynamics_routing.py",
            "setup_native_supervision.py",
            "check_native_supervision_runtime.py",
            "native_event_delivery.py", "native_runtime_settings.py",
            "check_native_reader_runtime.py", "pac_publish.py",
            "contact_center_setup.py", "acs_numbers.py",
        )]
        for path in files:
            if not path.is_file():
                continue
            text = path.read_text()
            found = [match for match in identifiers.findall(text) if match.lower() not in public_identifiers]
            with self.subTest(path=str(path.relative_to(ROOT))):
                self.assertEqual(found, [], "Use placeholders for deployment UUIDs and telephone numbers.")
            for value in forbidden:
                with self.subTest(path=str(path.relative_to(ROOT)), forbidden=value):
                    self.assertFalse(value in text, f"Nonportable value in {path.name}: {value}")

    def test_customer_and_workspace_setup_are_part_of_the_main_recipe(self):
        guide = (GUIDE / "README.md").read_text()
        root = (ROOT / "README.md").read_text()
        for concept in (
            "crm-setup.md", "workspace-setup.md", "--crm-case-context",
            "--startup-notice", "CaseNumber", "prvReadPluginAssembly",
        ):
            self.assertIn(concept, guide)
        for stale_claim in (
            "requests` Cosmos container",
            "Profiles remain separate hardcoded Python data.",
            "Profiles remain code-defined.",
        ):
            self.assertNotIn(stale_claim, root)
        reference = (GUIDE / "reference.md").read_text().lower()
        for boundary in ("synthetic", "prefetched", "uncertain", "live-case", "supervisor"):
            self.assertIn(boundary, reference)

    def test_winning_route_and_known_boundaries_are_explicit(self):
        guide = (GUIDE / "README.md").read_text()
        reference = (GUIDE / "reference.md").read_text()
        operations = (GUIDE / "operations.md").read_text()
        for concept in (
            "NativeSpeechTurnPilot", "NativeMediaContext", "speech_turn_control: true",
            "DYNAMICS_NATIVE_MEDIA_FAST_START", "data.to.kind", "APP_NUMBER",
        ):
            self.assertTrue(concept in guide, f"Main deployment guide omits {concept}")
        for concept in ("Acs_Unknown", "analytics", "human-phase", "120", "one", "30"):
            self.assertTrue(concept in reference, f"Reference omits {concept}")
        for concept in ("Observer lease renewal", "90 seconds", "No HTTP diagnostics"):
            self.assertTrue(concept in operations, f"Operations guide omits {concept}")


    def test_selection_matrix_matches_the_bicep_conditions_and_native_cli(self):
        resources = (ROOT / "infra/resources.bicep").read_text()
        variables = dict(re.findall(r"(?m)^var (\w+) = (.+)$", resources))
        conditions = dict(re.findall(r"(?m)^resource (\w+) '[^']+' = if \((.+)\) \{$", resources))

        def active(expression, flags):
            tokens = re.findall(r"\|\||&&|!|[()]|\w+", expression)
            self.assertEqual("".join(tokens), re.sub(r"\s+", "", expression), expression)
            words = {"||": " or ", "&&": " and ", "!": " not ", "(": "(", ")": ")"}
            python = "".join(
                words[token] if token in words
                else str(active(variables[token], flags)) if token in variables
                else str(flags.get(token, False)) for token in tokens
            )
            return eval(python, {"__builtins__": {}})

        guide = (GUIDE / "README.md").read_text()
        lifecycle = guide[guide.index("### First: confirm service lifecycle and eligibility"):guide.index("### 1.2 ")]
        matrix = lifecycle[lifecycle.index("#### What each selection needs from ACS"):lifecycle.index("#### Region:")]
        rows = {}
        for line in matrix.splitlines():
            cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
            match = re.fullmatch(r"`([a-z-]+)`(?: only)?", cells[0])
            if line.startswith("|") and match:
                rows[match[1]] = cells[1:]
        flags = {"native-voice": "deployNativeVoice", "application-phone": "deployApplicationPhone", "email": "deployEmail"}
        self.assertEqual(set(rows), set(flags))
        for selection, flag in flags.items():
            application_acs, email, number, native = rows[selection]
            with self.subTest(selection=selection):
                self.assertEqual(application_acs == "Created", active(conditions["acsEmail"], {flag: True}))
                self.assertEqual(email == "Created", active(conditions["emailService"], {flag: True}))
                self.assertEqual(number == "Required", flag == "deployApplicationPhone")
                self.assertEqual(native == "Required", flag == "deployNativeVoice")
        runtime = parser().parse_args([
            "runtime-env", "--tenant-id", "11111111-1111-4111-8111-111111111111",
            "--org-url", "https://example.crm17.dynamics.com",
            "--workstream-id", "22222222-2222-4222-8222-222222222222",
            "--native-acs-resource-id", "/subscriptions/33333333-3333-4333-8333-333333333333/resourceGroups/native"
                                        "/providers/Microsoft.Communication/communicationServices/native-acs",
            "--permanent", "--secrets-file", "keys.env", "--output", "runtime.env",
        ])
        self.assertEqual(runtime.app_number, "", "Native-only setup must not need an application number.")
        flat = " ".join(lifecycle.split())
        for stale in (
            "eligibility for both numbers", "The root deployment includes both",
            "still needs a separate application ACS number", "United States, as currently specified",
        ):
            self.assertNotIn(stale, flat)
        self.assertIn("(../../README.md#notice-azure-communication-services-retirement)", lifecycle)

        location = json.loads((ROOT / "infra/template-profile.example.json").read_text())["azure"]["communicationsDataLocation"]
        for source in ((ROOT / "infra/main.bicep").read_text(), resources):
            self.assertIn(f"param communicationsDataLocation string = '{location}'", source)
        self.assertIn("${ACS_DATA_LOCATION=" + location + "}", (ROOT / "infra/main.parameters.json").read_text())
        self.assertRegex((ROOT / "infra/scripts/template_setup.py").read_text(),
                         r'"ACS_DATA_LOCATION":\s*profile\["azure"\]\["communicationsDataLocation"\]')
        self.assertEqual(resources.count("dataLocation: communicationsDataLocation"), 2)
        row = next(line for line in lifecycle.splitlines() if line.startswith("| Application ACS and ACS Email"))
        self.assertIn(f"`{location}`", row)
        self.assertIn("azure.communicationsDataLocation", row)

    def test_portal_steps_carry_their_verification_date_and_screenshot_fallback(self):
        guide = (GUIDE / "README.md").read_text()
        sections = dict(re.findall(r"(?ms)^### (.+?)\n(.*?)(?=^##+ |\Z)", guide))
        for heading in (
            "3.4 Connect in the Copilot Service admin center", "4.2 Sync the number into Dynamics",
            "5.1 Create the minimal shell: one queue and one voice workstream",
            "5.2 Create a thin voice-enabled routing agent", "5.3 Finish the inbound workstream",
        ):
            text = " ".join(sections[heading].split())
            with self.subTest(heading=heading):
                self.assertIn("Last verified: November 2026", text)
                self.assertIn("screenshot that includes the left navigation, with no secrets showing", text)
        self.assertIn("Phone setup > Select voice", " ".join(sections["5.2 Create a thin voice-enabled routing agent"].split()))


if __name__ == "__main__":
    unittest.main()
