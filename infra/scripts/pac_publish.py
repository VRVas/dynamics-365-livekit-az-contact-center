"""Publish one Copilot Studio agent with `pac copilot publish`, then prove the result by Dataverse readback.

PAC's exit code is a hint. The result is `published` only when the agent's `publishedon` advanced and the
newest finished publish operation in its `synchronizationstatus` is Succeeded. Preview by default. The
script never publishes automatically again: after a failure it allows one retry that names a concrete change.
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
from datetime import datetime, timezone
from pathlib import Path
from time import monotonic, sleep

import native_voice_setup as native
from setup_native_supervision import http_failure, redact

SELECT = "botid,name,schemaname,statecode,statuscode,publishedon,synchronizationstatus"
SUCCEEDED, FAILED = "succeeded", "failed"
MAX_ATTEMPTS = 2
PAC_TIMEOUT_SECONDS = 900
POLL_SECONDS = 15
OUTPUT_CHARACTERS = 4000
DIAGNOSTICS = 10
STOP = "Stop and ask the owner. Share the command, PAC's output, the readback and its diagnostics."
NEXT = {
    "published": "Publication verified by readback.",
    "failed": "Fix the cause shown in the diagnostics, then retry once with --retry-change naming that change.",
    "unconfirmed": ("Rerun the same command without --retry-change; it only reads back. Don't publish again. "
                    "If it stays unconfirmed, stop and ask the owner."),
}


def safe(value, pattern=r"[A-Za-z0-9_.$-]{1,100}"):
    return value if isinstance(value, str) and re.fullmatch(pattern, value) else None


def moment(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) and value else None


def diagnostics(details):
    found = []
    for item in details if isinstance(details, list) else []:
        entries = item.get("diagnosticList") if isinstance(item, dict) else None
        for entry in entries if isinstance(entries, list) else []:
            if isinstance(entry, dict) and len(found) < DIAGNOSTICS:
                found.append({
                    "component_id": safe(item.get("componentId"), r"[0-9a-fA-F-]{36}"),
                    "error_code": safe(entry.get("errorCode")), "property": safe(entry.get("propertyName")),
                })
    return found


def state(bot):
    """The readback that proves a publication: publishedon and the newest finished publish operation."""
    text = bot.get("synchronizationstatus")
    try:
        document = json.loads(text) if isinstance(text, str) and text.strip() else None
    except ValueError:
        document = None
    finished = document.get("lastFinishedPublishOperation") if isinstance(document, dict) else None
    result = {key: bot.get(key) for key in ("name", "schemaname", "statecode", "statuscode", "publishedon")}
    result["last_publish"] = {
        **{key: safe(finished.get(key), r"[A-Za-z0-9 :.+_-]{1,64}") for key in ("status", "operationStart", "operationEnd")},
        "diagnostics": diagnostics(finished.get("diagnosticDetails")),
    } if isinstance(finished, dict) else None
    if result["last_publish"] is None and isinstance(text, str) and text.strip():
        # Show the real value when it doesn't have the expected shape.
        result["synchronizationstatus_excerpt"] = redact(text)[:1000]
    return result


def operation(value):
    publish = value.get("last_publish")
    return None if publish is None else (publish["status"], publish["operationStart"], publish["operationEnd"])


def outcome(before, after):
    status = ((after.get("last_publish") or {}).get("status") or "").casefold()
    fresh = after.get("last_publish") is not None and operation(after) != operation(before)
    advanced = moment(after.get("publishedon")) is not None and (
        moment(before.get("publishedon")) is None or moment(after["publishedon"]) > moment(before["publishedon"]))
    if fresh and status == FAILED:
        return "failed"
    if fresh and status == SUCCEEDED and advanced:
        return "published"
    return "pending"


def read_bot(api, bot_id):
    row = api.get(f"bots({bot_id})", {"$select": SELECT})
    if row.get("botid") != bot_id:
        raise ValueError("Dataverse returned a different agent.")
    return state(row)


def wait(api, bot_id, before, timeout):
    """Read back until the server shows a new result or the bounded wait ends. Never writes."""
    deadline = monotonic() + timeout
    current = error = None
    while True:
        try:
            current, error = read_bot(api, bot_id), None
        except urllib.error.HTTPError as failure:
            error = http_failure(failure, "GET", f"bots({bot_id})")
            if failure.code in {400, 401, 403, 404}:
                return "unconfirmed", current, error
        except (urllib.error.URLError, OSError) as failure:
            error = {"error": type(failure).__name__}
        else:
            result = outcome(before, current)
            if result != "pending":
                return result, current, None
        remaining = deadline - monotonic()
        if remaining <= 0:
            return "unconfirmed", current, error
        sleep(min(POLL_SECONDS, remaining))


def run_pac(command):
    try:
        completed = subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                                   timeout=PAC_TIMEOUT_SECONDS, check=False)
        output, code, timed_out = (completed.stdout or "") + (completed.stderr or ""), completed.returncode, False
    except subprocess.TimeoutExpired as expired:
        output = "".join(part.decode("utf-8", "replace") if isinstance(part, bytes) else part or ""
                         for part in (expired.stdout, expired.stderr))
        code, timed_out = None, True
    except OSError as error:
        return {"started": False, "exit_code": None, "timed_out": False, "error": type(error).__name__,
                "reported_failure": False, "output": ""}
    text = redact(output)
    return {
        "started": True, "exit_code": code, "timed_out": timed_out,
        "reported_failure": bool(re.search(r"(?i)\bfail(?:ed|ure)?\b|\berror\b", text)),
        "output": text[-OUTPUT_CHARACTERS:], "output_truncated": len(text) > OUTPUT_CHARACTERS,
    }


class Receipt:
    """Append-only, owner-only publication events: attempt, pac and readback."""

    def __init__(self, directory, args):
        self.directory, self.events = directory, []
        paths = sorted(directory.glob("event-*.json")) if directory.is_dir() else []
        for index, path in enumerate(paths, 1):
            event = json.loads(native.read_private(path))
            if path.name != f"event-{index:04d}.json" or event.get("sequence") != index:
                raise ValueError("The publication receipt is damaged; keep it. " + STOP)
            self.events.append(event)
        if any((event["org_url"], event["bot_id"]) != (args.org_url, args.bot_id) for event in self.attempts()):
            raise ValueError("This receipt directory belongs to another organization or agent.")

    def append(self, kind, **values):
        event = {"sequence": len(self.events) + 1, "recorded_at": datetime.now(timezone.utc).isoformat(),
                 "kind": kind, **values}
        native.write_private(self.directory / f"event-{event['sequence']:04d}.json",
                             json.dumps(event, indent=2, ensure_ascii=False) + "\n")
        self.events.append(event)
        return event

    def attempts(self):
        return [event for event in self.events if event["kind"] == "attempt"]

    def result(self, attempt):
        return next((event["result"] for event in reversed(self.events)
                     if event["kind"] == "readback" and event["attempt"] == attempt), None)


def finish(receipt, attempt, api, args, pac=None):
    if pac is not None and not pac["started"]:
        result, current, error = "failed", None, None
    else:
        result, current, error = wait(api, args.bot_id, attempt["before"], args.timeout_seconds)
    receipt.append("readback", attempt=attempt["attempt"], result=result, after=current, error=error,
                   reconciled=pac is None)
    report = {
        "status": result, "attempt": attempt["attempt"], "before": attempt["before"], "after": current,
        "receipt_dir": str(receipt.directory), "published_by_this_run": result == "published" and pac is not None,
        "next": STOP if result == "failed" and attempt["attempt"] >= MAX_ATTEMPTS else NEXT[result],
    }
    if pac is not None:
        report["pac"] = pac
        if result != "unconfirmed" and pac["exit_code"] is not None:
            report["pac_exit_code_matched_readback"] = (pac["exit_code"] == 0) == (result == "published")
    if error:
        report["readback_error"] = error
    print(json.dumps(report, ensure_ascii=False))
    return 0 if result == "published" else 1


def change(value):
    value = value.strip()
    if not 10 <= len(value) <= 200 or "\n" in value or "\r" in value:
        raise argparse.ArgumentTypeError("Name the concrete change in 10-200 characters on one line.")
    return value


def seconds(value):
    number = int(value)
    if not 30 <= number <= 1800:
        raise argparse.ArgumentTypeError("Use 30-1800 seconds.")
    return number


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--tenant-id", required=True, type=native.guid)
    result.add_argument("--org-url", required=True, type=native.organization)
    result.add_argument("--bot-id", required=True, type=native.guid)
    result.add_argument("--receipt-dir", required=True, type=Path,
                        help="Private directory outside the checkout. Reuse it for every readback and the one retry.")
    result.add_argument("--apply", action="store_true", help="Run `pac copilot publish` once, then read back.")
    result.add_argument("--confirm-organization", type=native.organization)
    result.add_argument("--retry-change", type=change,
                        help="After a failed attempt only: the concrete change made since. Allows one retry.")
    result.add_argument("--timeout-seconds", type=seconds, default=300,
                        help="How long to read back after PAC returns (30-1800, default 300).")
    result.add_argument("--pac", default="pac", help="Power Platform CLI executable (default: pac on PATH).")
    return result


def main(argv=None):
    args = parser().parse_args(argv)
    if args.apply and args.confirm_organization != args.org_url:
        raise ValueError("--apply requires --confirm-organization matching --org-url.")
    if not args.apply and (args.confirm_organization or args.retry_change):
        raise ValueError("--confirm-organization and --retry-change are valid only with --apply.")
    directory = native.private_path(args.receipt_dir)
    if directory.exists() and (not directory.is_dir() or (os.name != "nt" and directory.stat().st_mode & 0o077)):
        raise ValueError("The receipt directory must be an owner-only directory (chmod 700).")
    receipt = Receipt(directory, args)
    attempts = receipt.attempts()
    last = attempts[-1] if attempts else None
    previous = receipt.result(last["attempt"]) if last else None
    api = native.Dataverse(args.org_url, args.tenant_id)
    bot = read_bot(api, args.bot_id)
    command = [args.pac, "copilot", "publish", "--bot", args.bot_id, "--environment", args.org_url]
    if not args.apply:
        print(json.dumps({
            "status": "plan_only", "agent": bot, "command": command, "attempts": len(attempts),
            "last_result": previous, "since_last_attempt": outcome(last["before"], bot) if last else None,
            "cloud_changes": False,
        }, ensure_ascii=False))
        return 0
    if last and previous == "published":
        if args.retry_change:
            raise ValueError("The last attempt is already published; nothing to retry.")
        print(json.dumps({"status": "published", "attempt": last["attempt"], "already_verified": True,
                          "agent": bot, "published_by_this_run": False}, ensure_ascii=False))
        return 0
    if last and previous in {None, "unconfirmed"}:
        if args.retry_change:
            raise ValueError("The last publication is unconfirmed and may still finish. " + NEXT["unconfirmed"])
        return finish(receipt, last, api, args)
    if last:
        if outcome(last["before"], bot) == "published":
            return finish(receipt, last, api, args)
        if len(attempts) >= MAX_ATTEMPTS:
            raise ValueError("Publication failed on both allowed attempts. " + STOP)
        if not args.retry_change:
            raise ValueError("The last publication failed. " + NEXT["failed"])
    elif args.retry_change:
        raise ValueError("--retry-change applies only after a failed attempt.")
    if bot["statecode"] != 0 or bot["statuscode"] != 1:
        raise ValueError("The agent is not Active and Provisioned; check `pac copilot status` before publishing.")
    executable = shutil.which(args.pac)
    if not executable:
        raise ValueError("The Power Platform CLI was not found. Install it, or pass its path with --pac.")
    if not directory.exists():
        directory.mkdir(mode=0o700)
    attempt = receipt.append("attempt", attempt=len(attempts) + 1, org_url=args.org_url, tenant_id=args.tenant_id,
                             bot_id=args.bot_id, command=command, before=bot, retry_change=args.retry_change)
    pac = run_pac([executable, *command[1:]])
    receipt.append("pac", attempt=attempt["attempt"], **pac)
    return finish(receipt, attempt, api, args, pac)


def cli(argv=None):
    try:
        code = main(argv)
    except urllib.error.HTTPError as error:
        details = http_failure(error, "GET")
        print(json.dumps({"status": "failed", "http_status": details.pop("status"), **details}))
        code = 1
    except (ValueError, RuntimeError) as error:
        print(json.dumps({"status": "failed", "error": str(error)}))
        code = 1
    except (OSError, subprocess.SubprocessError) as error:
        print(json.dumps({"status": "failed", "error": type(error).__name__}))
        code = 1
    sys.exit(code)


if __name__ == "__main__":
    cli()
