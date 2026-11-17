"""Check that this checkout is neutral and safe to publish. Run it before every commit.

Findings are printed as file:line with a fix, and the exit code is 1. Checks:
- neutral defaults in the bundled configuration;
- owner files (untracked images, PDFs, office documents, archives, media) that
  preparation would copy: move them to your private workspace;
- identifiers (GUIDs), phone numbers, email addresses, Azure and Dynamics host
  names, secrets and local machine paths in every tracked or reviewed file;
- private terms, such as past customer names. Keep them out of the source: list
  them one per line in an owner-only file (chmod 600) outside the checkout, and
  pass it with --private-terms, set TEMPLATE_PRIVATE_TERMS_FILE, or keep it at
  ~/.config/contact-center-template/private-terms.txt.
"""

import argparse
import bisect
import json
import os
from pathlib import Path
import re
import subprocess
import sys

from template_setup import ROOT, RUNTIME_CONFIGS, UI_CONFIGS, ENVIRONMENT_EXAMPLES, source_paths


TERMS_VARIABLE = "TEMPLATE_PRIVATE_TERMS_FILE"
DEFAULT_TERMS = Path.home() / ".config/contact-center-template/private-terms.txt"
# Owner-supplied material that preparation would copy from an untracked file.
OWNER_FILE_SUFFIXES = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tif", ".tiff", ".heic", ".heif", ".svg", ".ico",
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".csv", ".tsv", ".ppt", ".pptx", ".odt", ".ods", ".odp", ".rtf",
    ".pages", ".numbers", ".key", ".vsdx", ".msg", ".eml",
    ".zip", ".7z", ".rar", ".tar", ".gz", ".tgz", ".bz2", ".xz",
    ".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac", ".mp4", ".mov", ".avi", ".mkv", ".webm", ".wmv",
}
# Documents and packages that never belong in the tracked source.
PACKAGE_SUFFIXES = {".pdf", ".zip", ".7z", ".rar", ".tar", ".gz", ".tgz", ".doc", ".docx", ".xls", ".xlsx",
                    ".ppt", ".pptx", ".msg", ".eml"}
MARKUP_SUFFIXES = {".html", ".htm", ".svg"}
PLACEHOLDER_WORDS = {"example", "examples", "test", "sample", "placeholder", "synthetic", "your", "other",
                     "another", "wrong", "fake", "dummy"}
SECRET_MARKERS = ("synthetic", "example", "placeholder", "dummy", "fake", "test", "sample", "redacted", "xxxx",
                  "your", "<", "${", "{")

# Public, well-known IDs that may appear in source. Add new ones here with a comment naming each.
PUBLIC_IDS = {
    # Azure built-in role definitions
    "5e0bd9bd-7b93-4f28-af87-19fc36ad61bd",  # Cognitive Services OpenAI User
    "64702f94-c441-49e6-a78b-ef80e0188fee",  # Azure AI Developer
    "53ca6127-db72-4b80-b1b0-d745d6d5456d",  # Azure AI User
    "a97b65f3-24c7-4388-baec-2e87135dc908",  # Cognitive Services User
    "1407120a-92aa-4202-b7e9-c0e197c71c8f",  # Search Index Data Reader
    "8ebe5a00-799e-43f5-93ac-243d3dce84a7",  # Search Index Data Contributor
    "7ca78c08-252a-4471-8644-bb5ff32d4ba0",  # Search Service Contributor
    "7f951dda-4ed3-4680-a7ca-43fe172d538d",  # AcrPull
    "4633458b-17de-408a-b874-0445c86b69e6",  # Key Vault Secrets User
    # Microsoft first-party application IDs
    "04b07795-8ddb-461a-bbee-02f9e1bf7b46",  # Microsoft Azure CLI
    "4962773b-9cdb-44cf-a8bf-237846a00ab7",  # Microsoft.EventGrid (secure webhook delivery)
    # Dynamics 365 form control class IDs
    "4273edbd-ac1d-40d3-9fb2-095c621b552d",  # Single line of text
}
# Public service hosts that would otherwise look tenant-specific.
PUBLIC_HOSTS = {
    # HUMAIN's documented public hosts
    "livekit-admin.sanad.ink",
    "livekit.sanad.ink",
    "humain-voice-agent.share.sanad.ink",
    "globaldisco.crm.dynamics.com",  # Dataverse global discovery service
}
# Public contact addresses that may appear in source. Add new ones here with a comment naming each.
PUBLIC_EMAILS = {
    "opencode@microsoft.com",  # Microsoft Open Source Code of Conduct contact (CODE_OF_CONDUCT.md, CONTRIBUTING.md)
    "vvasilescu@microsoft.com",  # Author contact (README Authors section, CITATION.cff)
}
# UK numbers that Ofcom reserves for drama, without the leading 0 (+44 20 7946 0xxx, +44 7700 900xxx, ...).
OFCOM_DRAMA = ("2079460", "7700900", "1134960", "1144960", "1154960", "1164960", "1174960", "1184960",
               "1214960", "1314960", "1414960", "1514960", "1614960", "1914980", "2896496", "2920180",
               "1632960", "8081570", "9098790", "3069990")
TENANT_HOST_SUFFIXES = (
    r"crm\d*\.dynamics\.com", r"operations\.dynamics\.com", r"azurecontainerapps\.io", r"azurewebsites\.net",
    r"azurecr\.io", r"vault\.azure\.net", r"documents\.azure\.com", r"search\.windows\.net", r"openai\.azure\.com",
    r"cognitiveservices\.azure\.com", r"services\.ai\.azure\.com", r"communication\.azure\.com", r"azurecomm\.net",
    r"service\.signalr\.net", r"webpubsub\.azure\.com", r"(?:blob|queue|table|file|dfs)\.core\.windows\.net",
    r"servicebus\.windows\.net", r"database\.windows\.net", r"azure-api\.net", r"azurefd\.net",
    r"cloudapp\.azure\.com", r"environment\.api\.powerplatform\.com", r"powerappsportals\.com", r"sanad\.ink",
)

GUID = re.compile(r"\b[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}\b")
PHONE = re.compile(r"(?<![\w+/=.-])\+[1-9](?:[ .()-]{0,2}[0-9]){6,14}(?![0-9])")
EMAIL = re.compile(r"(?<![\w.%+-])[A-Za-z0-9][A-Za-z0-9._%+-]*@([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,})\b")
HOST = re.compile(r"(?<![\w.-])((?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+)(?:" + "|".join(TENANT_HOST_SUFFIXES) + r")(?![\w-])",
                  re.I)
LOCAL_PATH = re.compile(r"(?<![\w.-])(?:/home/|/Users/|/workspaces/)([A-Za-z0-9._-]+)")
LOCAL_PATH_PLACEHOLDERS = {"user", "username", "me", "you"}
ABSENCE_MARKERS = ("assertnotin", "assertnotregex", "assertfalse", "not in ", ".not.", "forbidden")
CONNECTION_KEY = re.compile(r"(?i)\b(?:AccountKey|AccessKey|SharedAccessKey|PrimaryKey|SecondaryKey)[ \t]*=[ \t]*([^;\"'\s`]+)")
# (lowercase hints, pattern, may be long): a pattern runs only on text that contains one of its hints. Long
# tokens are matched before long base64-like runs are hidden.
TOKENS = (
    (("private key",), re.compile(r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY(?: BLOCK)?-----"), False),
    (("eyj",), re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"), True),
    (("ghp_", "gho_", "ghu_", "ghs_", "ghr_", "github_pat_"),
     re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{60,})"), False),
    (("akia",), re.compile(r"\bAKIA[0-9A-Z]{16}\b"), False),
    (("xox",), re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}"), False),
    (("aiza",), re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"), False),
    (("sk-",), re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{32,}"), False),
    (("sig=",), re.compile(r"(?i)[?&]sig=([A-Za-z0-9%+/=]{30,})"), True),
    (("bearer",), re.compile(r"(?i)\bBearer[ \t]+([A-Za-z0-9._~+/=-]{30,})"), True),
    (("ingest-key",), re.compile(r"poc[-]ingest[-]key[-]"), False),
)
HOST_HINTS = ("dynamics.com", ".azure", "windows.net", "azurecomm.net", "signalr.net", "azure-api.net", "azurefd.net",
              "powerplatform.com", "powerappsportals.com", "sanad.ink")
KEY_HINTS = ("accountkey", "accesskey", "sharedaccesskey", "primarykey", "secondarykey")
PATH_HINTS = ("/home/", "/users/", "/workspaces/")
# Content that is never a finding: lockfile integrity hashes and data: URIs, plus long base64 runs except for
# secret patterns (a long token can contain a base64-like run).
EMBEDDED = re.compile(r"sha(?:1|256|384|512)-[A-Za-z0-9+/=]{20,}|data:[\w/+.-]+;base64,[A-Za-z0-9+/=]+")
LONG_BASE64 = re.compile(r"[A-Za-z0-9+/]{120,}={0,2}")
MARKUP_IDS = re.compile(r"""\bid\s*=\s*(?:"[^"]*"|'[^']*')|url\(\s*['"]?#[^)]*\)|href\s*=\s*(?:"#[^"]*"|'#[^']*')""", re.I)
FIX = {
    "guid": "use a placeholder such as 11111111-1111-4111-8111-111111111111, or add a public ID to PUBLIC_IDS in "
            "check_template.py with a comment",
    "phone": "use a fictional number such as +12025550101",
    "email": "use an address at example.com",
    "host": "put a placeholder such as example in the host name, as in example.crm17.dynamics.com",
    "secret": "remove it, rotate the credential and use a placeholder",
    "path": "use a relative path or a placeholder such as /home/<user>",
    "term": "remove the customer-identifying content",
}
LIMIT_PER_KIND = 5


class PublishCheckError(ValueError):
    """The checkout isn't safe to publish; the message lists the findings, one per line."""

    def __init__(self, lines, total):
        super().__init__("\n".join(lines))
        self.total = total


def excerpt(value, keep=10):
    return value if len(value) <= keep else value[:keep] + "…"


def placeholder_guid(value):
    digits = value.replace("-", "").lower()
    return max(digits.count(character) for character in set(digits)) >= 26


def fictional_phone(number):
    digits = re.sub(r"\D", "", number)
    if len(set(digits[1:])) <= 1:
        return True
    if digits.startswith("1") and "555" in (digits[1:4], digits[4:7]):
        return True
    return digits.startswith("44") and digits[2:].startswith(OFCOM_DRAMA)


def placeholder_name(name):
    labels = name.lower().split(".")
    tokens = re.split(r"[.-]", name.lower())
    return labels[-1] in {"example", "test", "invalid", "localhost"} or any(
        token in PLACEHOLDER_WORDS or token.startswith("your") for token in tokens)


def placeholder_secret(value):
    lowered = value.lower()
    return any(marker in lowered for marker in SECRET_MARKERS)


def is_test_file(relative):
    return bool({"tests", "test", "__tests__"} & set(relative.parts) or re.match(r"test_|.*_test\.py$", relative.name)
                or re.search(r"\.(?:test|spec)\.", relative.name))


def private_terms(path=None):
    """Return the owner's private terms and a note. A missing default file is skipped; an explicit one is required."""
    explicit = path or os.environ.get(TERMS_VARIABLE)
    target = Path(explicit).expanduser() if explicit else DEFAULT_TERMS
    shown = "~/" + str(target.relative_to(Path.home())) if target.is_relative_to(Path.home()) else str(target)
    if not target.is_file():
        if explicit:
            source = "--private-terms" if path else TERMS_VARIABLE
            raise ValueError(f"Private terms file not found: {shown} (from {source}).")
        return [], f"skipped: no private terms file at {shown}"
    if target.resolve().is_relative_to(ROOT.resolve()):
        raise ValueError("Keep the private terms file outside the checkout.")
    if os.name != "nt" and target.stat().st_mode & 0o077:
        raise ValueError(f"Restrict the private terms file to its owner: chmod 600 {shown}")
    terms = sorted({line.strip().lower() for line in target.read_text(encoding="utf-8").splitlines()
                    if line.strip() and not line.strip().startswith("#")}, key=len, reverse=True)
    if not terms:
        raise ValueError(f"The private terms file has no terms: {shown}.")
    return terms, f"checked {len(terms)} terms from {shown}"


class TermPattern:
    """Whole-word, case-insensitive matcher; spaces, hyphens, dots and underscores between words are interchangeable."""

    def __init__(self, terms):
        words = [[word for word in re.split(r"[\s_.-]+", term) if word] for term in terms]
        self.hints = {parts[0].lower() for parts in words if parts}
        alternatives = (r"[\s_.-]*".join(re.escape(word) for word in parts) for parts in words if parts)
        self.pattern = re.compile(r"(?<![^\W_])(?:" + "|".join(alternatives) + r")(?![^\W_])", re.I)

    def search(self, text):
        return self.pattern.search(text)

    def finditer(self, text):
        return self.pattern.finditer(text)


def term_pattern(terms):
    return TermPattern(terms) if terms else None


def blank(match):
    """Hide matched text without moving later offsets or line numbers."""
    return re.sub(r"[^\n]", " ", match.group(0))


def scan(relative, original, terms):
    """Yield (kind, line, detail) findings for one file's text."""
    secrets_view = EMBEDDED.sub(blank, original)
    text = LONG_BASE64.sub(blank, secrets_view)
    if relative.suffix.lower() in MARKUP_SUFFIXES:
        text = MARKUP_IDS.sub(blank, text)
    breaks = [match.start() for match in re.finditer("\n", original)]
    lowered = original.lower()
    testing = is_test_file(relative)

    def where(match):
        number = bisect.bisect_left(breaks, match.start())
        return number + 1, original[breaks[number - 1] + 1 if number else 0:breaks[number] if number < len(breaks) else len(original)]

    for match in terms.finditer(text) if terms and any(hint in lowered for hint in terms.hints) else ():
        yield "term", where(match)[0], f"private term ({excerpt(match.group(0), 2)})"
    for match in GUID.finditer(text):
        value = match.group(0).lower()
        if value not in PUBLIC_IDS and not placeholder_guid(value):
            yield "guid", where(match)[0], f"identifier that isn't a placeholder ({excerpt(value, 9)})"
    for match in PHONE.finditer(text):
        if not fictional_phone(match.group(0)):
            yield "phone", where(match)[0], f"phone number that isn't fictional ({excerpt(match.group(0), 8)})"
    for match in EMAIL.finditer(text):
        domain = match.group(1)
        if match.group(0).lower() in PUBLIC_EMAILS:
            continue
        if not re.match(r"(?:odata|microsoft\.dynamics\.crm)\.", domain, re.I) and not placeholder_name(domain):
            yield "email", where(match)[0], f"email address outside example and test domains ({excerpt(match.group(0))})"
    for match in HOST.finditer(text) if any(hint in lowered for hint in HOST_HINTS) else ():
        host, tenant = match.group(0).lower(), match.group(1).lower()
        if host not in PUBLIC_HOSTS and not tenant.startswith("privatelink.") and not placeholder_name(tenant.rstrip(".")):
            yield "host", where(match)[0], f"real-looking host name ({excerpt(host, 16)})"
    for match in CONNECTION_KEY.finditer(secrets_view) if any(hint in lowered for hint in KEY_HINTS) else ():
        value = match.group(1)
        if re.fullmatch(r"[A-Za-z0-9+/=_-]{20,}", value) and not placeholder_secret(value):
            yield "secret", where(match)[0], f"embedded key ({excerpt(match.group(0), 14)})"
    for hints, pattern, long in TOKENS:
        for match in pattern.finditer(secrets_view if long else text) if any(hint in lowered for hint in hints) else ():
            if not placeholder_secret(match.group(0)):
                yield "secret", where(match)[0], f"credential material ({excerpt(match.group(0), 6)})"
    for match in LOCAL_PATH.finditer(text) if any(hint in lowered for hint in PATH_HINTS) else ():
        if match.group(1).lower() in LOCAL_PATH_PLACEHOLDERS:
            continue
        number, line = where(match)
        if testing and any(marker in line.lower() for marker in ABSENCE_MARKERS):
            continue
        yield "path", number, f"local machine path ({excerpt(match.group(0), 16)})"


def neutral_defaults():
    failures = []

    def require(condition, message):
        if not condition:
            failures.append(message)

    def document(relative):
        return json.loads((ROOT / relative).read_text())

    for relative in RUNTIME_CONFIGS:
        value = document(relative)
        require(value["organization"]["name"] == "Contact center", relative + ": customer branding is not neutral. Restore the neutral default.")
        require(not any(value["features"].values()), relative + ": optional runtime modules must start disabled.")
        require(value["business"] == {"caseCategories": ["general"], "locationRequired": False, "city": ""},
                relative + ": business defaults must be unconfigured.")
        require(value["knowledge"]["allowedDomains"] == [], relative + ": knowledge domains must be empty.")
        require(not any(value["emailPolicy"].values()) and value["phonePolicy"]["allowedOutboundNumbers"] == [],
                relative + ": delivery policies must start empty.")
    public = [document(relative) for relative in UI_CONFIGS]
    require(all(value == public[0] for value in public), ", ".join(UI_CONFIGS) + ": public frontend defaults differ.")
    require(public[0]["brand"]["name"]["en"] == "Contact center" and public[0]["brand"]["logoUrl"] is None,
            UI_CONFIGS[0] + ": public branding must remain neutral.")
    require(public[0]["quickActions"] == [] and not public[0]["features"]["profiles"],
            UI_CONFIGS[0] + ": no bundled business prompts or profile selectors.")
    for relative in ("apps/voice/src/app/profiles.json", "apps/chat/orchestrator/profiles.json", "infra/data/knowledge.json"):
        require(document(relative) == [], relative + ": business records must not be bundled.")
    seed = document("docs/dynamics-native-voice/examples/crm-seed.example.json")
    require(seed["account"] is None and seed["contacts"] == seed["cases"] == [],
            "docs/dynamics-native-voice/examples/crm-seed.example.json: the seed example must be empty.")
    knowledge = (ROOT / "infra/data/knowledge.md").read_text()
    require("Status: unconfigured" in knowledge and not re.search(r"^### \d+\.", knowledge, re.M),
            "infra/data/knowledge.md: the canonical source must not bundle business facts.")
    config = document("infra/data/knowledge-config.json")
    require(config["approved"] is False and config["allowedDomains"] == [],
            "infra/data/knowledge-config.json: knowledge needs deployment-specific approval.")
    return failures


def check(private_terms_file=None):
    if (ROOT / "deployment-ledger.json").exists():
        raise ValueError("Check the neutral source checkout, not a deliberately customized private deployment.")
    terms, terms_note = private_terms(private_terms_file)
    pattern = term_pattern(terms)
    failures = neutral_defaults()
    tracked = subprocess.check_output(["git", "ls-files", "--cached", "-z"], cwd=ROOT).decode().split("\0")
    for name in tracked:
        path = ROOT / name
        if name and path.is_file() and path.name.startswith(".env") and path.name not in ENVIRONMENT_EXAMPLES:
            failures.append(name + ": private environment file is tracked. Remove it from Git and rotate its secrets.")
        if name and path.is_file() and path.suffix.lower() in PACKAGE_SUFFIXES:
            failures.append(name + ": document or package in the source. Remove it from Git and keep it in your private workspace.")
    untracked = subprocess.check_output(["git", "ls-files", "--others", "--exclude-standard", "-z"], cwd=ROOT).decode().split("\0")
    owner_files = {Path(name) for name in untracked if name and Path(name).suffix.lower() in OWNER_FILE_SUFFIXES}
    for name in sorted(owner_files):
        failures.append(f"{name}: owner-supplied file in the checkout. Move it to your private workspace.")
    checked, total = 0, 0
    for relative in source_paths(include_reviewed_worktree=True):
        if pattern and pattern.search(str(relative)):
            failures.append(f"{relative}: private term in the file name. Rename or remove the file.")
        data = (ROOT / relative).read_bytes()
        if b"\0" in data or relative in owner_files:
            continue
        checked += 1
        counts, seen = {}, set()
        for kind, number, detail in sorted(scan(relative, data.decode("utf-8", errors="replace"), pattern), key=lambda item: item[1]):
            if (number, detail) in seen:
                continue
            seen.add((number, detail))
            counts[kind] = counts.get(kind, 0) + 1
            if counts[kind] <= LIMIT_PER_KIND:
                failures.append(f"{relative}:{number}: {detail}. Fix: {FIX[kind]}.")
        for kind, count in counts.items():
            if count > LIMIT_PER_KIND:
                failures.append(f"{relative}: {count - LIMIT_PER_KIND} more {kind} findings.")
                total += count - LIMIT_PER_KIND - 1
    if failures:
        raise PublishCheckError(failures, len(failures) + total)
    return {"status": "verified", "sourceFilesChecked": checked, "bundledBusinessRecords": 0,
            "privateTerms": terms_note, "cloudChanges": False}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--private-terms", metavar="FILE",
                        help=f"Owner-only file of private terms, one per line. Default: ${TERMS_VARIABLE}, "
                             "then ~/.config/contact-center-template/private-terms.txt if it exists.")
    args = parser.parse_args(argv)
    try:
        print(json.dumps(check(args.private_terms), indent=2))
    except PublishCheckError as error:
        print(f"Publish check failed: {error.total} finding{'s' if error.total != 1 else ''}. "
              "Fix them before you commit or prepare.", file=sys.stderr)
        print(error, file=sys.stderr)
        return 1
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        print(f"Publish check couldn't run: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
