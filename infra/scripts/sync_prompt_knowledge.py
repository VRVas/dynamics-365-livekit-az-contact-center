"""Validate and bundle deployment-approved knowledge into the Python services."""
import argparse
import hashlib
import re
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "infra/data/knowledge.md"
TARGETS = (
    ROOT / "apps/chat/orchestrator/knowledge.md",
    ROOT / "apps/voice/src/app/knowledge.md",
)
MAX_KB_BYTES = 64 * 1024


def validate(content: bytes):
    text = content.decode("utf-8")
    if not text.startswith("# Organization service knowledge\n"):
        raise ValueError("The approved KB must use the Organization service knowledge heading.")
    if not re.search(r"^Revision: [A-Za-z0-9._-]{1,64}$", text, re.MULTILINE):
        raise ValueError("The approved KB requires an explicit revision.")
    if len(content) > MAX_KB_BYTES:
        raise ValueError("The embedded KB exceeds the application's 64 KiB context budget.")
    topics = re.findall(r"^### (\d+)\.", text, re.MULTILINE)
    if len({int(topic) for topic in topics}) != len(topics):
        raise ValueError("Knowledge topic IDs must be unique.")
    sources = re.findall(r'^\[(S\d+)\]: (\S+) "([^"]+)"$', text, re.MULTILINE)
    if len({source[0] for source in sources}) != len(sources):
        raise ValueError("Knowledge source IDs must be unique.")
    for _, address, _ in sources:
        url = urlsplit(address)
        if url.scheme != "https" or not url.hostname or url.username or url.password:
            raise ValueError("Knowledge sources must be HTTPS URLs without embedded credentials.")
    referenced = set(re.findall(r"\[(S\d+)\]", re.sub(r"^\[S\d+\]:.*$", "", text, flags=re.MULTILINE)))
    if referenced != {source[0] for source in sources}:
        raise ValueError("Every knowledge reference must resolve to a used, registered source.")
    if re.search(r"^Status: unconfigured$", text, re.MULTILINE):
        if topics or sources:
            raise ValueError("Unconfigured knowledge must not contain business topics or source claims.")
    elif not topics or not sources:
        raise ValueError("Configured knowledge needs numbered topics and approved source references.")
    return {"bytes": len(content), "topics": len(topics), "sources": len(sources),
            "sha256": hashlib.sha256(content).hexdigest()}


def sync(check=False):
    content = SOURCE.read_bytes()
    summary = validate(content)
    for target in TARGETS:
        if check:
            if not target.is_file() or target.read_bytes() != content:
                raise RuntimeError(f"KB bundle out of date: {target}. Run python infra/scripts/sync_prompt_knowledge.py")
        else:
            target.write_bytes(content)
    return summary


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="Fail if a packaged KB differs; do not modify files.")
    args = parser.parse_args()
    print(sync(args.check))


if __name__ == "__main__":
    main()
