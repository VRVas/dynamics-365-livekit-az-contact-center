"""Plan and apply allowlisted native runtime settings on the voice container app only; never writes secrets."""

import argparse
import hashlib
import json
import os
import re
import shlex
import subprocess
import time
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from native_voice_setup import guid, observer_expiry, organization, private_path, read_private, write_private


WORKSTREAM = "DYNAMICS_NATIVE_MEDIA_WORKSTREAM_ID"
ADDITIONAL = "DYNAMICS_NATIVE_MEDIA_ADDITIONAL_WORKSTREAM_IDS"
PERMANENT = "DYNAMICS_NATIVE_PROBE_PERMANENT"
EXPIRES = "DYNAMICS_NATIVE_PROBE_EXPIRES_AT"
PROBE_RESOURCE = "DYNAMICS_NATIVE_PROBE_RESOURCE_ID"
BOOLEAN_KEYS = {
    "ACS_AUTO_CONFERENCE_ENABLED", "DYNAMICS_OUTBOUND_ENABLED", "DYNAMICS_NATIVE_MEDIA_ENABLED",
    "DYNAMICS_NATIVE_MEDIA_FAST_START", PERMANENT,
}
# Secrets change only through azd provision so they stay Container Apps secrets.
SECRET_KEYS = {
    "DYNAMICS_NATIVE_MEDIA_API_KEY", "DYNAMICS_NATIVE_MEDIA_ACS_CONNECTION_STRING", "DYNAMICS_NATIVE_PROBE_API_KEY",
    "HUMAIN_AUTHORIZATION", "INGEST_API_KEY", "ACS_CONNECTION_STRING", "DYNAMICS_OUTBOUND_API_KEY",
    "AGENT_ASSIST_API_KEY", "ACS_EMAIL_CONNECTION_STRING",
}
HEALTH_READS = 12
HEALTH_INTERVAL = 10


class CommandError(RuntimeError):
    pass


def now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def boolean(value):
    if value.lower() not in {"true", "false"}:
        raise ValueError("use true or false")
    return value.lower()


def choice(*options):
    def validate(value):
        if value not in options:
            raise ValueError("use one of " + ", ".join(options))
        return value
    return validate


def e164(value):
    if not re.fullmatch(r"\+[1-9][0-9]{6,14}", value):
        raise ValueError("use an E.164 number: + followed by the country code and number")
    return value


def workstreams(value):
    items = [guid(item.strip()) for item in value.split(",")] if value else []
    if len(set(items)) != len(items):
        raise ValueError("workstream IDs must be distinct")
    return ",".join(items)


def acs_resource(value):
    if not re.fullmatch(
        r"/subscriptions/[0-9a-fA-F-]{36}/resourceGroups/[A-Za-z0-9_.()-]+/"
        r"providers/Microsoft\.Communication/communicationServices/[A-Za-z0-9-]+", value,
    ):
        raise ValueError("use the full native ACS resource ID")
    return value


def expiry(value):
    return observer_expiry(value, False) if value else ""


def https_url(value):
    url = urllib.parse.urlsplit(value)
    if url.scheme != "https" or not url.hostname or url.username or url.password or url.fragment:
        raise ValueError("use an HTTPS URL without credentials or fragment")
    return value


def identifier(value):
    if not re.fullmatch(r"[a-z0-9][a-z0-9_.-]{0,63}", value):
        raise ValueError("use a lowercase identifier")
    return value


ALLOWED = {
    "DYNAMICS_ORG_URL": organization,
    "ACS_SOURCE_NUMBER": e164,
    "ACS_AUTO_CONFERENCE_ENABLED": boolean,
    "DYNAMICS_OUTBOUND_ENABLED": boolean,
    "DYNAMICS_NATIVE_MEDIA_ENABLED": boolean,
    "DYNAMICS_NATIVE_MEDIA_TRANSPORT": choice("callautomation", "voip"),
    "DYNAMICS_NATIVE_MEDIA_FAST_START": boolean,
    WORKSTREAM: guid,
    ADDITIONAL: workstreams,
    PROBE_RESOURCE: acs_resource,
    EXPIRES: expiry,
    PERMANENT: boolean,
    "HUMAIN_TOKEN_ENDPOINT": https_url,
    "HUMAIN_VOICE": identifier,
    "HUMAIN_ASR_MODEL": choice("bayan_playground", "bayan_api", "bayan_realtime"),
    "HUMAIN_LLM_PROVIDER": choice("gemma4", "allam"),
    "HUMAIN_INTERRUPTION_POLICY_MODE": choice("control", "shadow", "off"),
    "HUMAIN_TURN_TAKING_STRATEGY": choice("livekit_v1_mini", "fast_vad"),
}


def az(arguments, *, timeout=120):
    label = "az " + " ".join(argument for argument in arguments[:3] if not argument.startswith("-"))
    try:
        result = subprocess.run(
            ["az", *arguments, "--only-show-errors", "-o", "json"],
            check=True, capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.CalledProcessError as error:
        detail = re.sub(r"\s+", " ", error.stderr or "").strip()[-1200:]
        raise CommandError(f"{label} failed: {detail or 'no error text'}") from None
    except subprocess.TimeoutExpired:
        raise CommandError(f"{label} timed out after {timeout} seconds; its outcome is unknown.") from None
    return json.loads(result.stdout) if result.stdout.strip() else None


def name(value, label, pattern=r"[A-Za-z0-9][A-Za-z0-9._()-]{0,89}"):
    if not isinstance(value, str) or not re.fullmatch(pattern, value):
        raise ValueError(f"Use an explicit {label}.")
    return value


def requested_settings(pairs):
    settings = {}
    for pair in pairs:
        key, separator, value = pair.partition("=")
        if not separator:
            raise ValueError("Use --set KEY=VALUE.")
        if key in SECRET_KEYS:
            raise ValueError(f"{key} is a secret. Change secrets with azd provision so they stay Container Apps secrets; this tool never writes them.")
        if key not in ALLOWED:
            raise ValueError(f"{key} isn't an allowlisted native runtime setting. Allowed: {', '.join(sorted(ALLOWED))}.")
        if key in settings:
            raise ValueError(f"{key} is set twice.")
        if value.lower().startswith("secretref:") or "\n" in value or "\r" in value:
            raise ValueError(f"{key} must be a single-line plain value, not a secret reference.")
        try:
            settings[key] = ALLOWED[key](value)
        except ValueError as error:
            raise ValueError(f"{key}: {str(error).rstrip('.')}.") from None
    if not settings:
        raise ValueError("Pass at least one --set KEY=VALUE.")
    return settings


def container_env(container):
    env = {}
    for entry in container.get("env") or []:
        key = entry.get("name") if isinstance(entry, dict) else None
        if not isinstance(key, str) or key in env:
            raise ValueError("The container environment has missing or duplicate names; review it before changing settings.")
        env[key] = {"secretRef": entry["secretRef"]} if entry.get("secretRef") else {"value": entry.get("value") or ""}
    return env


def only_container(template, container_name):
    matches = [row for row in (template or {}).get("containers") or [] if isinstance(row, dict) and row.get("name") == container_name]
    if len(matches) != 1:
        raise ValueError(f"Container {container_name!r} was not found exactly once in the app template.")
    return matches[0]


def snapshot(target):
    document = az(["containerapp", "show", "--subscription", target["subscription_id"],
                   "--resource-group", target["resource_group"], "--name", target["app_name"]]) or {}
    properties = document.get("properties") or {}
    template, configuration = properties.get("template") or {}, properties.get("configuration") or {}
    if document.get("name") != target["app_name"]:
        raise ValueError("Azure returned another container app than requested.")
    container = only_container(template, target["container_name"])
    return {
        "image": container.get("image"), "env": container_env(container),
        "latest_revision": properties.get("latestRevisionName"),
        "secrets": sorted(str(row.get("name")) for row in configuration.get("secrets") or []),
        "resources": container.get("resources"), "scale": template.get("scale"),
        "revisions_mode": configuration.get("activeRevisionsMode"),
    }


def fingerprint(state):
    return {
        "image": state["image"], "latest_revision": state["latest_revision"], "secrets": state["secrets"],
        "env": {
            key: {"secretRef": entry["secretRef"]} if "secretRef" in entry else {"sha256": digest(entry["value"])}
            for key, entry in state["env"].items()
        },
        "resources_sha256": digest(canonical(state["resources"])), "scale_sha256": digest(canonical(state["scale"])),
    }


def revisions(target):
    rows = az(["containerapp", "revision", "list", "--subscription", target["subscription_id"],
               "--resource-group", target["resource_group"], "--name", target["app_name"], "--all"])
    if not isinstance(rows, list):
        raise ValueError("Unexpected container app revision list.")
    return {row.get("name"): row for row in rows if isinstance(row, dict)}


def siblings(target):
    rows = az(["containerapp", "list", "--subscription", target["subscription_id"], "--resource-group", target["resource_group"]])
    if not isinstance(rows, list):
        raise ValueError("Unexpected container app list.")
    return {
        row.get("name"): (row.get("properties") or {}).get("latestRevisionName")
        for row in rows if isinstance(row, dict) and row.get("name") != target["app_name"]
    }


def changes_for(state, settings):
    changes, unchanged = {}, []
    for key, value in settings.items():
        entry = state["env"].get(key)
        if entry is not None and "secretRef" in entry:
            raise ValueError(f"{key} is a secret reference on the running app; this tool doesn't change secret references.")
        before = entry["value"] if entry is not None else None
        same = before is not None and (before.lower() == value if key in BOOLEAN_KEYS else before == value)
        if same:
            unchanged.append(key)
        else:
            changes[key] = {"before": before, "after": value}
    return changes, unchanged


def validate_combination(state, changes):
    values = {key: entry["value"] for key, entry in state["env"].items() if "value" in entry}
    values.update({key: change["after"] for key, change in changes.items()})
    changed = set(changes)

    def value(key, default=""):
        return values.get(key, default).strip()

    if changed & {"DYNAMICS_NATIVE_MEDIA_ENABLED", "ACS_AUTO_CONFERENCE_ENABLED"} and (
        value("DYNAMICS_NATIVE_MEDIA_ENABLED", "false").lower() == "true" and value("ACS_AUTO_CONFERENCE_ENABLED", "true").lower() != "false"
    ):
        raise ValueError("Native media needs ACS_AUTO_CONFERENCE_ENABLED=false.")
    if changed & {"DYNAMICS_NATIVE_MEDIA_FAST_START", "DYNAMICS_NATIVE_MEDIA_TRANSPORT"} and (
        value("DYNAMICS_NATIVE_MEDIA_FAST_START", "false").lower() == "true" and value("DYNAMICS_NATIVE_MEDIA_TRANSPORT", "callautomation").lower() != "voip"
    ):
        raise ValueError("DYNAMICS_NATIVE_MEDIA_FAST_START=true needs DYNAMICS_NATIVE_MEDIA_TRANSPORT=voip.")
    if changed & {WORKSTREAM, ADDITIONAL}:
        primary = value(WORKSTREAM).lower()
        additional = [item.strip().lower() for item in value(ADDITIONAL).split(",") if item.strip()]
        if additional and not primary:
            raise ValueError(f"Set {WORKSTREAM} before additional workstreams.")
        if primary and primary in additional:
            raise ValueError("Additional workstreams must differ from the primary workstream.")
    if changed & {PERMANENT, EXPIRES, PROBE_RESOURCE}:
        permanent = value(PERMANENT, "false").lower() == "true"
        if permanent and value(EXPIRES):
            raise ValueError(f"Permanent observation needs {EXPIRES} empty.")
        if not permanent and value(PROBE_RESOURCE):
            observer_expiry(value(EXPIRES) or None, False)


def azd_commands(settings, environment):
    target = shlex.quote(environment) if environment else '"$ENV"'
    return [f"azd env set {key} {shlex.quote(value)} --environment {target}" for key, value in settings.items()]


def shown(value):
    if value is not None and re.search(r"(?i)accesskey=|sharedaccesssignature|sig=|password|bearer\s", value):
        return "<masked>"
    return value


def load(path):
    if path.is_symlink():
        raise ValueError("Plan and intent files must be regular owner-only files, not symlinks.")
    document = json.loads(read_private(path))
    if not isinstance(document, dict):
        raise ValueError("Invalid runtime-settings record.")
    return document


def verification(plan, intent, *, reads):
    target = plan["target"]
    expected = {**plan["before"]["env"], **{key: {"sha256": digest(change["after"])} for key, change in plan["changes"].items()}}
    for read in range(reads):
        state, found, sibling_now = snapshot(target), revisions(target), siblings(target)
        current = fingerprint(state)
        revision = (found.get(plan["revision"]) or {}).get("properties") or {}
        revision_container = only_container(revision.get("template"), target["container_name"]) if revision.get("template") else {}
        revision_env = container_env(revision_container) if revision_container else {}
        checks = {
            "one_new_revision": sorted(set(found) - set(intent["revisions_before"])) == [plan["revision"]],
            "latest_revision_is_new": state["latest_revision"] == plan["revision"],
            "image_unchanged": state["image"] == plan["before"]["image"] and revision_container.get("image") == plan["before"]["image"],
            "only_planned_variables_changed": current["env"] == expected,
            "new_revision_has_planned_values": all(
                revision_env.get(key) == {"value": change["after"]} for key, change in plan["changes"].items()
            ),
            "secrets_resources_and_scale_unchanged": all(
                current[key] == plan["before"][key] for key in ("secrets", "resources_sha256", "scale_sha256")
            ),
            "sibling_latest_revisions_unchanged": sibling_now == intent["siblings_before"],
            "revision_ready": revision.get("provisioningState") == "Provisioned" and revision.get("healthState") == "Healthy",
        }
        settled = all(value for key, value in checks.items() if key != "revision_ready")
        if checks["revision_ready"] or not settled or read == reads - 1:
            return checks, {"running_state": revision.get("runningState"), "health_state": revision.get("healthState")}
        time.sleep(HEALTH_INTERVAL)


def finish(output, plan, intent, *, status, failure=None, reads=HEALTH_READS):
    checks, revision = verification(plan, intent, reads=reads)
    verified = all(checks.values())
    result = {
        "status": status if verified else "unconfirmed", "app": plan["target"]["app_name"], "revision": plan["revision"],
        "checks": checks, "revision_state": revision,
        "changes": {key: {"before": shown(change["before"]), "after": shown(change["after"])} for key, change in plan["changes"].items()},
        "azd_env_set": plan["azd_env_set"], "secrets_written": 0, "calls_placed": 0,
    }
    path = output / f"after-{uuid4().hex}.json"
    write_private(path, json.dumps({**result, "failure": failure, "recorded_at": now()}, indent=2) + "\n")
    result["private_receipt"] = str(path)
    if not verified:
        failed = ", ".join(key for key, value in checks.items() if not value)
        if failure:
            raise RuntimeError(
                f"The update failed ({failure}) and the readback doesn't confirm: {failed}. No retry was made. Inspect {path}. "
                "Fix the cause and plan once more in a new directory; if that fails too, stop and ask the human."
            )
        raise RuntimeError(
            f"The update was sent, but the readback doesn't confirm: {failed}. No retry was made. Inspect {path}. "
            "If only revision_ready is false, rerun this command without --apply in a few minutes; otherwise stop and ask the human."
        )
    result["next"] = "Persist the values with the azd env set commands so a later azd provision keeps them, then run native readiness."
    print(json.dumps(result, indent=2))


def settings(args):
    target = {
        "subscription_id": guid(args.subscription_id),
        "resource_group": name(args.resource_group, "resource group name"),
        "app_name": name(args.app_name, "container app name", r"[a-z][a-z0-9-]{0,30}[a-z0-9]"),
        "container_name": name(args.container_name, "container name", r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?"),
    }
    if args.azd_environment is not None:
        name(args.azd_environment, "azd environment name", r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
    requested = requested_settings(args.set) if args.set else None
    output = private_path(args.output_dir)
    if output.exists() and (not output.is_dir() or output.is_symlink() or (os.name != "nt" and output.stat().st_mode & 0o077)):
        raise ValueError("The existing output directory must be an owner-only directory (chmod 700).")
    if not output.exists():
        if args.apply:
            raise ValueError("Plan without --apply first, review it, then reuse its --output-dir.")
        if requested is None:
            raise ValueError("Pass at least one --set KEY=VALUE.")
        state = snapshot(target)
        if state["revisions_mode"] != "Single":
            raise ValueError("This tool supports only single-revision mode; the voice app template uses Single.")
        changes, unchanged = changes_for(state, requested)
        validate_combination(state, changes)
        commands = azd_commands(requested, args.azd_environment)
        diff = [{"key": key, "before": shown(change["before"]), "after": shown(change["after"])} for key, change in changes.items()]
        if not changes:
            print(json.dumps({"status": "already_configured", "unchanged": unchanged, "azd_env_set": commands, "remote_changes": 0}, indent=2))
            return
        plan = {
            "version": 1, "kind": "native-runtime-settings", "target": target, "requested": requested, "changes": changes,
            "before": fingerprint(state), "revision": f"{target['app_name']}--rt{uuid4().hex[:8]}", "azd_env_set": commands,
            "created_at": now(),
        }
        output.mkdir(mode=0o700)
        write_private(output / "plan.json", json.dumps(plan, indent=2) + "\n")
        print(json.dumps({
            "status": "planned", "plan_only": True, "app": target["app_name"], "changes": diff, "unchanged": unchanged,
            "image": state["image"], "revision_to_create": plan["revision"], "azd_env_set": commands,
            "private_plan": str(output / "plan.json"), "remote_changes": 0,
            "next": f"Review, then rerun with --apply --confirm-app {target['app_name']}.",
        }, indent=2))
        return
    plan = load(output / "plan.json")
    if plan.get("version") != 1 or plan.get("kind") != "native-runtime-settings" or plan.get("target") != target:
        raise ValueError("The saved plan targets another app or container; plan again in a new directory.")
    if requested is not None and requested != plan["requested"]:
        raise ValueError("--set differs from the saved plan; plan again in a new directory.")
    intent_path, failed_path = output / "intent.json", output / "failed.json"
    if intent_path.exists() or intent_path.is_symlink():
        failure = load(failed_path)["error"] if failed_path.exists() or failed_path.is_symlink() else None
        finish(output, plan, load(intent_path), status="reconciled", failure=failure, reads=HEALTH_READS if args.apply and not failure else 1)
        return
    if not args.apply:
        drift = fingerprint(snapshot(target)) != plan["before"]
        print(json.dumps({"status": "saved_plan", "plan_only": True, "app_changed_since_plan": drift,
                          "revision_to_create": plan["revision"], "azd_env_set": plan["azd_env_set"], "remote_changes": 0}, indent=2))
        return
    if args.confirm_app != target["app_name"]:
        raise ValueError("--apply requires --confirm-app matching --app-name.")
    if fingerprint(snapshot(target)) != plan["before"]:
        raise ValueError("The voice app changed since planning (image, environment, secrets, resources or latest revision). Nothing was changed; plan again in a new directory.")
    intent = {
        "version": 1, "plan_sha256": digest(canonical(plan)), "revision": plan["revision"],
        "revisions_before": sorted(revisions(target)), "siblings_before": siblings(target), "created_at": now(),
    }
    write_private(intent_path, json.dumps(intent, indent=2) + "\n")
    try:
        az([
            "containerapp", "update", "--subscription", target["subscription_id"], "--resource-group", target["resource_group"],
            "--name", target["app_name"], "--container-name", target["container_name"],
            "--revision-suffix", plan["revision"].rsplit("--", 1)[1],
            "--set-env-vars", *(f"{key}={change['after']}" for key, change in plan["changes"].items()),
        ], timeout=900)
    except CommandError as error:
        write_private(failed_path, json.dumps({"error": str(error), "recorded_at": now()}, indent=2) + "\n")
        finish(output, plan, intent, status="applied", failure=str(error), reads=1)
        return
    finish(output, plan, intent, status="applied")


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--subscription-id", required=True, type=guid)
    result.add_argument("--resource-group", required=True)
    result.add_argument("--app-name", default="ca-voice")
    result.add_argument("--container-name", default="voice")
    result.add_argument("--set", action="append", metavar="KEY=VALUE", help="Allowlisted non-secret setting; repeat for several.")
    result.add_argument("--output-dir", required=True, type=Path, help="New private directory for the plan, intent and receipts.")
    result.add_argument("--azd-environment", help="azd environment name to print in the azd env set commands.")
    result.add_argument("--apply", action="store_true", help="Apply the saved plan with one az containerapp update.")
    result.add_argument("--confirm-app")
    return result


def main(argv=None):
    settings(parser().parse_args(argv))


if __name__ == "__main__":
    try:
        main()
    except (ValueError, KeyError, RuntimeError, OSError, subprocess.SubprocessError) as error:
        print(json.dumps({"status": "failed", "error": str(error) if isinstance(error, (ValueError, RuntimeError)) else type(error).__name__}))
        raise SystemExit(1)
