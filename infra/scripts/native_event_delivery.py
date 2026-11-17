"""Bootstrap, create and verify native Dynamics Event Grid delivery; never places calls or prints webhook URLs."""

import argparse
import hashlib
import json
import os
import re
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from dotenv import dotenv_values

from native_voice_setup import guid, private_path, read_private, write_private


GRAPH = "https://graph.microsoft.com/v1.0/"
# Public-cloud application ID of the first-party Microsoft.EventGrid principal, as published in earlier
# revisions of Microsoft's secure-webhook script. The tenant's principal is still checked by ID and
# display name; pass --event-grid-app-id when the portal shows another value (for example another cloud).
EVENT_GRID_APP_ID = "4962773b-9cdb-44cf-a8bf-237846a00ab7"
EVENT_GRID_NAME = "Microsoft.EventGrid"
ROLE = "AzureEventGridSecureWebhookSubscriber"
INCOMING = "Microsoft.Communication.IncomingCall"
RECORDING = "Microsoft.Communication.RecordingFileStatusUpdated"
TOPIC_TYPE = "Microsoft.Communication.CommunicationServices"
OBSERVER = "moacc-native-handover-probe"
OBSERVER_PATH = "/dynamics/handover/probe/events"
ROUTES = {
    "incoming": {
        "name": "native-dynamics-incoming", "event": INCOMING, "url": "INCOMING_CALL_WEBHOOK_URL",
        "filters": [{"key": "data.to.kind", "operatorType": "StringIn", "values": ["phoneNumber"]}],
    },
    "recording": {"name": "native-dynamics-recordings", "event": RECORDING, "url": "RECORDING_WEBHOOK_URL", "filters": []},
}
ENDPOINT_KEYS = ("INCOMING_CALL_WEBHOOK_URL", "RECORDING_WEBHOOK_URL", "EVENT_GRID_APP_ID", "EVENT_GRID_APP_TENANT_ID")
BOOTSTRAP_STEPS = ("application", "service-principal", "owner", "event-grid-assignment", "writer-assignment")
SUBSCRIPTION_STEPS = ("system-topic", "incoming-subscription", "recording-subscription")
ACS_ID = re.compile(
    r"/subscriptions/([0-9a-fA-F-]{36})/resourceGroups/([A-Za-z0-9_.()-]{1,90})/"
    r"providers/Microsoft\.Communication/communicationServices/([A-Za-z0-9-]{1,63})"
)
APP_SELECT = "id,appId,displayName,uniqueName,signInAudience,appRoles,passwordCredentials,keyCredentials,requiredResourceAccess"
SP_SELECT = "id,appId,displayName,appRoles,passwordCredentials,keyCredentials"
DEFAULT_DISPLAY_NAME = "Native Voice Events"
MAX_ATTEMPTS = 2
READBACK_READS = 6
READBACK_INTERVAL = 5
BANNER_GATE = (
    "Exit gate before any call: in Copilot Service admin center open Channels > Phone numbers > Manage "
    "(Manage telephony) and refresh it. Both 'Your Event Grid is unconfigured...' banners, for incoming calls "
    "and for call recording, must be gone. Last verified: November 2026. If the screen differs, ask the owner "
    "for a screenshot that includes the left navigation, with no secrets showing."
)


class CommandError(RuntimeError):
    pass


def now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def scrub(text, secrets=()):
    text = text or ""
    for value in secrets:
        if value:
            text = text.replace(value, "<redacted>")
    text = re.sub(r"https?://\S+", "<url>", text)
    text = re.sub(r"(?i)(accesskey|sig|code|token)=[^;&\s]+", r"\1=<redacted>", text)
    return re.sub(r"\s+", " ", text).strip()[-1200:]


def az(arguments, *, timeout=120, secrets=()):
    """Run one Azure CLI command; error text is scrubbed because argv may hold a webhook URL."""
    label = "az " + " ".join(argument for argument in arguments[:4] if not argument.startswith("-"))
    try:
        result = subprocess.run(
            ["az", *arguments, "--only-show-errors", "-o", "json"],
            check=True, capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.CalledProcessError as error:
        raise CommandError(f"{label} failed: {scrub(error.stderr, secrets) or 'no error text'}") from None
    except subprocess.TimeoutExpired:
        raise CommandError(f"{label} timed out after {timeout} seconds; its outcome is unknown.") from None
    return json.loads(result.stdout) if result.stdout.strip() else None


def graph_error(error):
    try:
        detail = json.loads(error.read(16384) or b"{}").get("error") or {}
    except (ValueError, AttributeError, OSError):
        detail = {}
    finally:
        error.close()
    text = f"{str(detail.get('code') or '')[:80]}: {re.sub(r'\s+', ' ', str(detail.get('message') or ''))[:300]}"
    return text.strip(": ") or "no error detail"


class Graph:
    def __init__(self, tenant):
        self.tenant = guid(tenant)
        try:
            result = subprocess.run(
                ["az", "account", "get-access-token", "--tenant", self.tenant,
                 "--resource", "https://graph.microsoft.com", "-o", "json"],
                check=True, capture_output=True, text=True, timeout=45,
            )
        except subprocess.CalledProcessError:
            raise ValueError("Azure CLI couldn't get a Microsoft Graph token. Run az login --tenant with the Dynamics tenant.") from None
        identity = json.loads(result.stdout)
        if str(identity.get("tenant", "")).lower() != self.tenant:
            raise ValueError("Azure CLI returned a Microsoft Graph token for another tenant. Sign in to the Dynamics tenant.")
        self.headers = {"Authorization": "Bearer " + identity["accessToken"], "Accept": "application/json"}

    def request(self, path, *, method="GET", body=None, headers=None):
        request = urllib.request.Request(
            GRAPH + path, method=method, data=json.dumps(body).encode() if body is not None else None,
            headers={**self.headers, **({"Content-Type": "application/json"} if body is not None else {}), **(headers or {})},
        )
        with urllib.request.urlopen(request, timeout=45) as response:
            content = response.read(4 * 1024 * 1024 + 1)
        if len(content) > 4 * 1024 * 1024:
            raise ValueError("Microsoft Graph response exceeded the setup-tool size limit.")
        return json.loads(content) if content.strip() else {}

    def get(self, path, query=None):
        return self.request(path + ("?" + urllib.parse.urlencode(query, quote_via=urllib.parse.quote, safe="$,'()") if query else ""))

    def rows(self, path, query=None):
        target = path + ("?" + urllib.parse.urlencode(query, quote_via=urllib.parse.quote, safe="$,'()") if query else "")
        rows = []
        for _ in range(20):
            document = self.request(target)
            if not isinstance(document.get("value"), list):
                raise ValueError("Microsoft Graph returned an unexpected list shape.")
            rows.extend(document["value"])
            link = document.get("@odata.nextLink")
            if not link:
                return rows
            if not link.startswith(GRAPH):
                raise ValueError("Unexpected Microsoft Graph pagination origin.")
            target = link[len(GRAPH):]
        raise ValueError("Microsoft Graph result is too large; review the tenant with an Entra administrator.")


class Journal:
    """Owner-only, create-once records: one intent per attempt, one outcome per attempt, one done per step."""

    def __init__(self, directory):
        self.directory = directory

    def load(self, name):
        path = self.directory / (name + ".json")
        if path.is_symlink():
            raise ValueError("Journal entries must be regular owner-only files, not symlinks.")
        return json.loads(read_private(path)) if path.exists() else None

    def record(self, name, document):
        write_private(self.directory / (name + ".json"), json.dumps({**document, "recorded_at": now()}, indent=2) + "\n")

    def attempts(self, step):
        count = 0
        while (self.directory / f"{step}.intent-{count + 1}.json").exists():
            count += 1
        return count


def settle(observe):
    for read in range(READBACK_READS):
        satisfied, evidence = observe()
        if satisfied or read == READBACK_READS - 1:
            return satisfied, evidence
        time.sleep(READBACK_INTERVAL)


def run_step(journal, step, observe, write, request, retry_step=None):
    """Write at most once per run and never resend an unconfirmed attempt without an explicit --retry-step.

    Every write here is duplicate-safe on the server (an upsert keyed by the planned unique name,
    one principal per application, unique app-role assignments and owner links, PUT by resource
    name), so one human-approved retry after a readback cannot create a second object.
    """
    satisfied, evidence = observe()
    attempts = journal.attempts(step)
    if satisfied:
        if journal.load(step + ".done") is None:
            journal.record(step + ".done", {"step": step, "evidence": evidence, "attempts": attempts})
            return "reconciled" if attempts else "already_present"
        return "unchanged"
    if journal.load(step + ".done") is not None:
        raise RuntimeError(f"{step} was confirmed earlier but its readback no longer matches. Nothing was written; stop and ask the human.")
    if attempts:
        failure = journal.load(f"{step}.failed-{attempts}")
        outcome = "was rejected" if failure and failure.get("definite") else "has no confirmed outcome"
        if attempts >= MAX_ATTEMPTS:
            raise RuntimeError(f"{step}: {attempts} attempts failed and the readback shows no result. Stop and ask the human; show the command, error and readback.")
        if retry_step != step:
            raise RuntimeError(
                f"{step}: attempt {attempts} {outcome} and the readback shows no result yet. No write was resent. "
                f"Rerun later for read-only reconciliation, or fix the cause and rerun once with --retry-step {step}."
            )
    number = attempts + 1
    journal.record(f"{step}.intent-{number}", {"step": step, "attempt": number, "request": request})
    failure, definite = None, False
    try:
        write(evidence)
    except urllib.error.HTTPError as error:
        code, detail = error.code, graph_error(error)
        definite = 400 <= code < 500 and code not in {408, 429}
        journal.record(f"{step}.failed-{number}", {"http_status": code, "definite": definite, "error": detail})
        failure = f"Microsoft Graph returned HTTP {code} ({detail})"
    except (CommandError, OSError, ValueError, subprocess.SubprocessError) as error:
        failure = str(error) if isinstance(error, CommandError) else type(error).__name__
        journal.record(f"{step}.failed-{number}", {"definite": False, "error": failure})
    # Read back first: a failed call may still have written. A definite rejection needs only one read.
    satisfied, evidence = observe() if definite else settle(observe)
    if satisfied:
        journal.record(step + ".done", {"step": step, "evidence": evidence, "attempts": number})
        return "written" if failure is None else "reconciled"
    if failure is not None:
        hint = (
            "That was the last attempt; stop and ask the human with the command, error and readback."
            if number >= MAX_ATTEMPTS else
            f"Fix the cause, then rerun once with --retry-step {step}; if it fails again, stop and ask the human."
        )
        raise RuntimeError(f"{step} failed: {failure}. Readback shows no result. No retry was made. {hint}")
    raise RuntimeError(f"{step} was sent but the readback doesn't show it yet. No write was resent. Rerun the same command in a few minutes for read-only reconciliation.")


def step_status(journal, steps, order):
    """Read-only progress report; steps after the first incomplete one are not observed."""
    status, blocked = {}, None
    for name in order:
        if blocked is not None:
            status[name] = "waiting for " + blocked
            continue
        try:
            present = steps[name][0]()[0]
        except RuntimeError as error:
            present, status[name] = False, "blocked: " + str(error)
        if not present:
            status.setdefault(name, "unconfirmed attempt" if journal.attempts(name) else "pending")
            blocked = name
        else:
            status[name] = "present"
    return status


def private_directory(path):
    if path.exists() and (not path.is_dir() or path.is_symlink() or (os.name != "nt" and path.stat().st_mode & 0o077)):
        raise ValueError("The existing output directory must be an owner-only directory (chmod 700).")
    return path


def load_plan(output, kind):
    plan = json.loads(read_private(output / "plan.json"))
    if not isinstance(plan, dict) or plan.get("version") != 1 or plan.get("kind") != kind:
        raise ValueError("The output directory holds another kind of plan; use the directory created by this command's plan step.")
    return plan


def receipt(output, document):
    path = output / f"after-{uuid4().hex}.json"
    write_private(path, json.dumps({**document, "recorded_at": now()}, indent=2) + "\n")
    return str(path)


# ---------------------------------------------------------------- delivery application (Graph)

def display_name(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 ._()-]{2,119}", value):
        raise ValueError("Use a plain delivery-app display name of 3-120 letters, digits, spaces or ._()-.")
    return value


def event_grid_principal(graph, app_id):
    fields = {"$select": "id,appId,displayName", "$top": "5"}
    by_id = graph.rows("servicePrincipals", {**fields, "$filter": f"appId eq '{app_id}'"})
    by_name = graph.rows("servicePrincipals", {**fields, "$filter": f"displayName eq '{EVENT_GRID_NAME}'"})
    if not by_id and len(by_name) == 1 and by_name[0].get("appId"):
        raise ValueError(
            f"The tenant's Microsoft.EventGrid principal has application ID {by_name[0]['appId']}, not {app_id}. "
            "If that is expected for this cloud, rerun the plan with --event-grid-app-id set to it; nothing was changed."
        )
    if not by_id:
        raise ValueError(
            f"No Microsoft.EventGrid service principal with application ID {app_id} exists in this tenant. "
            "Check that the Microsoft.EventGrid resource provider is registered, or ask an Entra administrator; nothing was changed."
        )
    if len(by_id) != 1 or len(by_name) != 1:
        raise ValueError(
            f"Expected exactly one Microsoft.EventGrid service principal; found {len(by_id)} by application ID and "
            f"{len(by_name)} by name. Review them with an Entra administrator; nothing was changed."
        )
    principal = by_id[0]
    if (
        principal.get("displayName") != EVENT_GRID_NAME or by_name[0].get("id") != principal.get("id")
        or str(principal.get("appId", "")).lower() != app_id
    ):
        raise ValueError("The Microsoft.EventGrid name and application ID point to different principals; nothing was changed.")
    return {"id": guid(principal["id"]), "appId": app_id, "displayName": EVENT_GRID_NAME}


def signed_in_user(graph):
    try:
        me = graph.get("me", {"$select": "id,displayName"})
    except urllib.error.HTTPError as error:
        error.close()
        raise ValueError(f"Sign in to Azure CLI as the user who will create the event subscriptions (Graph /me returned HTTP {error.code}).") from None
    return {"id": guid(me.get("id")), "displayName": str(me.get("displayName") or "")[:120]}


def app_role(identifier):
    return {
        "allowedMemberTypes": ["Application", "User"], "description": "Azure Event Grid Role",
        "displayName": ROLE, "id": identifier, "isEnabled": True, "value": ROLE,
    }


def role_matches(role, planned):
    return (
        isinstance(role, dict) and str(role.get("id", "")).lower() == planned["id"] and role.get("value") == ROLE
        and role.get("isEnabled") is True and sorted(role.get("allowedMemberTypes") or []) == ["Application", "User"]
    )


def applications_named(graph, name):
    return graph.rows("applications", {"$filter": f"displayName eq '{name.replace(chr(39), chr(39) * 2)}'", "$select": APP_SELECT, "$top": "5"})


def delivery_application(graph, plan):
    rows = applications_named(graph, plan["display_name"])
    ours = [row for row in rows if row.get("uniqueName") == plan["unique_name"]]
    if len(rows) != len(ours) or len(ours) > 1:
        raise RuntimeError(f"Another application is also named {plan['display_name']!r}; stop and ask an Entra administrator. Nothing was changed.")
    if not ours:
        return None
    app, problems = ours[0], []
    if app.get("signInAudience") != "AzureADMyOrg":
        problems.append("it is not single-tenant")
    roles = app.get("appRoles") or []
    if len(roles) != 1 or not role_matches(roles[0], plan["app_role"]):
        problems.append(f"its app roles differ from the single planned {ROLE} role")
    if app.get("passwordCredentials") or app.get("keyCredentials"):
        problems.append("it has credentials")
    if app.get("requiredResourceAccess"):
        problems.append("it requests API permissions")
    if problems:
        raise RuntimeError("The delivery application differs from the plan: " + "; ".join(problems) + ". Nothing was changed; review it with the owner.")
    return {"id": guid(app["id"]), "appId": guid(app["appId"])}


def service_principal(graph, app_id):
    rows = graph.rows("servicePrincipals", {"$filter": f"appId eq '{app_id}'", "$select": SP_SELECT, "$top": "2"})
    if len(rows) > 1:
        raise RuntimeError("More than one service principal uses the delivery app ID; stop and ask an Entra administrator.")
    if rows and (rows[0].get("passwordCredentials") or rows[0].get("keyCredentials")):
        raise RuntimeError("The delivery service principal has credentials; it needs none. Nothing was changed.")
    return rows[0] if rows else None


def assignments(graph, principal_id):
    return graph.rows(f"servicePrincipals/{principal_id}/appRoleAssignedTo", {
        "$select": "id,principalId,principalType,appRoleId,resourceId",
    })


def require_application(graph, plan):
    app = delivery_application(graph, plan)
    if app is None:
        raise RuntimeError("The delivery application isn't visible; rerun the same command for read-only reconciliation.")
    return app


def require_principal(graph, plan):
    app = require_application(graph, plan)
    principal = service_principal(graph, app["appId"])
    if principal is None:
        raise RuntimeError("The delivery service principal isn't visible; rerun the same command for read-only reconciliation.")
    return app, principal


def bootstrap_steps(graph, plan):
    role, writer, event_grid = plan["app_role"], plan["writer"]["id"], plan["event_grid_principal"]["id"]

    def application():
        app = delivery_application(graph, plan)
        return app is not None, app or {}

    def principal():
        app = require_application(graph, plan)
        found = service_principal(graph, app["appId"])
        visible = found is not None and any(role_matches(item, role) for item in found.get("appRoles") or [])
        return visible, {"appId": app["appId"], "principal_id": found and found.get("id")}

    def owner():
        app = require_application(graph, plan)
        owners = {row.get("id") for row in graph.rows(f"applications/{app['id']}/owners", {"$select": "id"})}
        return writer in owners, {"application_object_id": app["id"]}

    def assignment(principal_id):
        def observe():
            _, found = require_principal(graph, plan)
            matching = [
                row for row in assignments(graph, found["id"])
                if row.get("principalId") == principal_id and str(row.get("appRoleId", "")).lower() == role["id"]
            ]
            if len(matching) > 1:
                raise RuntimeError("Duplicate app-role assignments exist on the delivery app; stop and ask an Entra administrator.")
            return bool(matching), {"principal_id": found["id"]}
        return observe

    return {
        "application": (application, lambda _: graph.request(
            f"applications(uniqueName='{plan['unique_name']}')", method="PATCH", headers={"Prefer": "create-if-missing"},
            body={"displayName": plan["display_name"], "signInAudience": "AzureADMyOrg", "appRoles": [role]},
        ), {"method": "PATCH", "path": f"applications(uniqueName='{plan['unique_name']}')", "prefer": "create-if-missing"}),
        "service-principal": (principal, lambda evidence: graph.request(
            f"servicePrincipals(appId='{evidence['appId']}')", method="PATCH", headers={"Prefer": "create-if-missing"},
            body={"displayName": plan["display_name"]},
        ), {"method": "PATCH", "path": "servicePrincipals(appId=<delivery app>)", "prefer": "create-if-missing"}),
        "owner": (owner, lambda evidence: graph.request(
            f"applications/{evidence['application_object_id']}/owners/$ref", method="POST",
            body={"@odata.id": GRAPH + "directoryObjects/" + writer},
        ), {"method": "POST", "path": "applications/<delivery app>/owners/$ref", "owner": writer}),
        "event-grid-assignment": (assignment(event_grid), lambda evidence: graph.request(
            f"servicePrincipals/{evidence['principal_id']}/appRoleAssignedTo", method="POST",
            body={"principalId": event_grid, "resourceId": evidence["principal_id"], "appRoleId": role["id"]},
        ), {"method": "POST", "path": "servicePrincipals/<delivery app>/appRoleAssignedTo", "principal": event_grid}),
        "writer-assignment": (assignment(writer), lambda evidence: graph.request(
            f"servicePrincipals/{evidence['principal_id']}/appRoleAssignedTo", method="POST",
            body={"principalId": writer, "resourceId": evidence["principal_id"], "appRoleId": role["id"]},
        ), {"method": "POST", "path": "servicePrincipals/<delivery app>/appRoleAssignedTo", "principal": writer}),
    }


def bootstrap_readback(graph, plan):
    app, principal = require_principal(graph, plan)
    role, writer, event_grid = plan["app_role"]["id"], plan["writer"]["id"], plan["event_grid_principal"]["id"]
    problems = []
    owners = sorted(row.get("id") for row in graph.rows(f"applications/{app['id']}/owners", {"$select": "id"}))
    if owners != [writer]:
        problems.append(f"application owners are {owners}, expected only the subscription writer {writer}")
    principal_owners = sorted(row.get("id") for row in graph.rows(f"servicePrincipals/{principal['id']}/owners"))
    if principal_owners not in ([], [writer]):
        problems.append(f"unexpected service-principal owners {principal_owners}")
    granted = [(row.get("principalId"), str(row.get("appRoleId", "")).lower()) for row in assignments(graph, principal["id"])]
    expected = sorted([(event_grid, role), (writer, role)])
    if sorted(granted) != expected:
        unexpected = [item for item in granted if item not in expected or granted.count(item) > 1]
        missing = [item for item in expected if item not in granted]
        problems.append(f"unexpected grants {unexpected} or missing grants {missing} on the delivery app")
    if graph.rows(f"servicePrincipals/{principal['id']}/appRoleAssignments"):
        problems.append("the delivery principal holds app roles on other resources")
    if graph.rows(f"servicePrincipals/{principal['id']}/oauth2PermissionGrants"):
        problems.append("the delivery principal has delegated consent grants")
    if problems:
        raise RuntimeError("Delivery app readback failed: " + "; ".join(problems) + ". Nothing was removed; review with an Entra administrator.")
    return {
        "delivery_app_id": app["appId"], "delivery_app_object_id": app["id"], "service_principal_id": principal["id"],
        "tenant_id": plan["tenant_id"], "app_role_id": role, "event_grid_principal_id": event_grid,
        "subscription_writer_id": writer, "owners": owners, "secrets_created": 0, "consent_grants": 0,
    }


def bootstrap(args, graph):
    tenant = guid(args.tenant_id)
    if graph.tenant != tenant:
        raise ValueError("The Graph client must use the selected tenant.")
    output = private_directory(private_path(args.output_dir))
    if not output.exists():
        if args.apply:
            raise ValueError("Run bootstrap without --apply first, review the plan, then reuse its --output-dir.")
        name = display_name(args.display_name)
        event_grid = event_grid_principal(graph, guid(args.event_grid_app_id))
        writer = signed_in_user(graph)
        existing = applications_named(graph, name)
        if existing:
            raise ValueError(
                f"An application named {name!r} already exists. Resume its original bootstrap directory, "
                "or choose another --display-name. Nothing was changed."
            )
        plan = {
            "version": 1, "kind": "native-event-delivery-bootstrap", "tenant_id": tenant, "display_name": name,
            "unique_name": "native-voice-events-" + uuid4().hex[:12], "app_role": app_role(str(uuid4())),
            "event_grid_principal": event_grid, "writer": writer, "created_at": now(),
        }
        output.mkdir(mode=0o700)
        write_private(output / "plan.json", json.dumps(plan, indent=2) + "\n")
        print(json.dumps({
            "status": "planned", "plan_only": True, "private_plan": str(output / "plan.json"),
            "creates": ["single-tenant application " + repr(name) + f" with one app role {ROLE}", "its service principal",
                        "the signed-in user as application owner", "role assignment for Microsoft.EventGrid",
                        "role assignment for the signed-in user"],
            "event_grid_principal_id": event_grid["id"], "subscription_writer_id": writer["id"],
            "secrets": 0, "api_permissions": 0, "remote_changes": 0,
            "next": f"Review, then rerun with --apply --confirm-tenant {tenant}.",
        }, indent=2))
        return
    plan = load_plan(output, "native-event-delivery-bootstrap")
    if plan["tenant_id"] != tenant:
        raise ValueError("The saved plan belongs to another tenant.")
    if args.display_name != plan["display_name"] and args.display_name != DEFAULT_DISPLAY_NAME:
        raise ValueError("The saved plan uses another display name; omit --display-name or match the plan.")
    if event_grid_principal(graph, plan["event_grid_principal"]["appId"]) != plan["event_grid_principal"]:
        raise ValueError("The Microsoft.EventGrid principal changed since planning; plan again in a new directory.")
    if signed_in_user(graph)["id"] != plan["writer"]["id"]:
        raise ValueError("Sign in as the subscription writer recorded in the plan; ownership and grants are planned for that user.")
    steps = bootstrap_steps(graph, plan)
    journal = Journal(output)
    if not args.apply:
        print(json.dumps({"status": "saved_plan", "plan_only": True, "steps": step_status(journal, steps, BOOTSTRAP_STEPS),
                          "remote_changes": 0}, indent=2))
        return
    if args.confirm_tenant != tenant:
        raise ValueError("--apply requires --confirm-tenant matching --tenant-id.")
    results = {}
    for name in BOOTSTRAP_STEPS:
        observe, write, request = steps[name]
        results[name] = run_step(journal, name, observe, write, request, args.retry_step)
    summary = bootstrap_readback(graph, plan)
    path = receipt(output, {"status": "configured", "steps": results, **summary})
    print(json.dumps({
        "status": "configured", "steps": results, **summary, "private_receipt": path,
        "next": "In Dynamics, use delivery_app_id as Event grid app id and tenant_id as Event grid app tenant id. "
                "Create the subscriptions after the native ACS connection and number sync.",
    }, indent=2))


# ---------------------------------------------------------------- system topic and subscriptions (ARM)

def acs_target(resource_id):
    match = ACS_ID.fullmatch(resource_id or "")
    if not match:
        raise ValueError("Use the full native Dynamics ACS resource ID, not its name, endpoint or immutable ID.")
    return {"id": resource_id, "subscription": guid(match[1]), "resource_group": match[2], "name": match[3]}


def topic_name(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{2,127}", value):
        raise ValueError("Use the native Event Grid system-topic name (3-128 letters, digits or _.-).")
    return value


def dynamics_endpoint(value, label):
    if not isinstance(value, str) or not 16 <= len(value) <= 2048 or any(character.isspace() for character in value):
        raise ValueError(f"{label} must be the single-line URL copied from Dynamics.")
    url = urllib.parse.urlsplit(value)
    try:
        port = url.port
    except ValueError:
        port = -1
    if url.scheme != "https" or not url.hostname or url.username or url.password or url.fragment or port not in (None, 443):
        raise ValueError(f"{label} must be an HTTPS URL without credentials, fragment or custom port.")
    if url.hostname.lower().endswith(".azurecontainerapps.io"):
        raise ValueError(f"{label} points at a container app. Copy the Dynamics URL from Manage Azure Communication Services.")
    return value


def endpoints(path, tenant, app_id):
    read_private(path)
    values = dotenv_values(path, interpolate=False)
    unknown = sorted(set(values) - set(ENDPOINT_KEYS))
    missing = [key for key in ENDPOINT_KEYS if not values.get(key)]
    if unknown or missing:
        raise ValueError(f"The endpoints file needs exactly {', '.join(ENDPOINT_KEYS)}; missing {missing}, unexpected {unknown}.")
    urls = {role: dynamics_endpoint(values[spec["url"]], spec["url"]) for role, spec in ROUTES.items()}
    if urls["incoming"] == urls["recording"]:
        raise ValueError("The incoming-call and recording URLs are identical; copy each from its own Dynamics field.")
    if guid(values["EVENT_GRID_APP_TENANT_ID"]) != tenant:
        raise ValueError("Dynamics shows another Event grid app tenant id than --tenant-id; fix the connection or the tenant first.")
    if guid(values["EVENT_GRID_APP_ID"]) != app_id:
        raise ValueError("Dynamics shows another Event grid app id than --delivery-app-id; use the app Dynamics is connected with.")
    return urls


def same_url(left, right):
    if not isinstance(left, str) or not isinstance(right, str):
        return False
    first, second = urllib.parse.urlsplit(left), urllib.parse.urlsplit(right)
    return (
        (first.scheme, (first.hostname or "").lower(), first.port, urllib.parse.unquote(first.path))
        == (second.scheme, (second.hostname or "").lower(), second.port, urllib.parse.unquote(second.path))
        and sorted(urllib.parse.parse_qsl(first.query, keep_blank_values=True))
        == sorted(urllib.parse.parse_qsl(second.query, keep_blank_values=True))
    )


def acs_name_in_query(url, name):
    return any(value.lower() == name.lower() for _, value in urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))


def check_tenant(target, tenant):
    account = az(["account", "show", "--subscription", target["subscription"]])
    if str((account or {}).get("tenantId", "")).lower() != tenant:
        raise ValueError("The native ACS subscription is not in the Dynamics tenant (--tenant-id). Nothing was changed.")
    resource = az(["resource", "show", "--ids", target["id"]]) or {}
    if (
        str(resource.get("type", "")).lower() != "microsoft.communication/communicationservices"
        or str(resource.get("id", "")).lower() != target["id"].lower()
        or not re.fullmatch(r"[a-z0-9]{2,40}", str(resource.get("location", "")))
    ):
        raise ValueError("The native ACS resource ID doesn't resolve to a Communication Services resource.")
    return resource["location"]


def flat(resource):
    resource = resource if isinstance(resource, dict) else {}
    return {**(resource.get("properties") if isinstance(resource.get("properties"), dict) else {}), **resource}


def group_of(resource_id):
    match = re.search(r"/resourceGroups/([^/]+)/", resource_id or "", re.IGNORECASE)
    return match[1] if match else None


def system_topic(target):
    topics = az(["eventgrid", "system-topic", "list", "--subscription", target["subscription"]])
    if not isinstance(topics, list):
        raise ValueError("Unexpected Event Grid system-topic list.")
    matching = [flat(topic) for topic in topics if str(flat(topic).get("source", "")).lower() == target["id"].lower()]
    if len(matching) > 1:
        raise RuntimeError("More than one system topic reports the native ACS resource as its source; review them with the owner.")
    if matching and str(matching[0].get("topicType", "")).lower() != TOPIC_TYPE.lower():
        raise RuntimeError("The native ACS system topic has an unexpected topic type; review it with the owner.")
    return matching[0] if matching else None, [flat(topic) for topic in topics]


def route_state(item):
    item = flat(item)
    destination = item.get("destination") if isinstance(item.get("destination"), dict) else {}
    properties = {**destination, **(destination.get("properties") if isinstance(destination.get("properties"), dict) else {})}
    selection = item.get("filter") if isinstance(item.get("filter"), dict) else {}
    return {
        "name": item.get("name"), "provisioning_state": item.get("provisioningState"),
        "endpoint_type": destination.get("endpointType"), "endpoint_base_url": properties.get("endpointBaseUrl"),
        "endpoint_url": properties.get("endpointUrl"), "tenant": properties.get("azureActiveDirectoryTenantId"),
        "application": properties.get("azureActiveDirectoryApplicationIdOrUri"),
        "attributes": properties.get("deliveryAttributeMappings") or [],
        "event_types": selection.get("includedEventTypes") or None,
        "advanced_filters": [
            {"key": row.get("key"), "operatorType": row.get("operatorType"), "values": row.get("values")}
            for row in selection.get("advancedFilters") or [] if isinstance(row, dict)
        ],
        "subject_filters": [value for value in (selection.get("subjectBeginsWith"), selection.get("subjectEndsWith")) if value],
        "schema": item.get("eventDeliverySchema"), "expires": item.get("expirationTimeUtc"),
    }


def is_observer(state):
    url = urllib.parse.urlsplit(state["endpoint_base_url"] or "")
    return (
        state["name"] == OBSERVER and state["event_types"] == [INCOMING] and state["endpoint_type"] == "WebHook"
        and url.scheme == "https" and (url.hostname or "").endswith(".azurecontainerapps.io") and url.path == OBSERVER_PATH
    )


def route_problems(state, role, tenant, app_id, url):
    spec, problems = ROUTES[role], []
    checks = (
        (state["endpoint_type"] == "WebHook", "not a webhook"),
        (state["endpoint_url"] is not None, "its full endpoint URL couldn't be read"),
        (state["endpoint_url"] is None or same_url(state["endpoint_url"], url), "endpoint differs from the Dynamics URL"),
        (str(state["tenant"] or "").lower() == tenant, "Entra tenant differs"),
        (str(state["application"] or "").lower() == app_id, "Entra application differs"),
        (state["event_types"] == [spec["event"]], "event types differ"),
        (state["advanced_filters"] == spec["filters"], "advanced filters differ"),
        (not state["subject_filters"], "subject filters are set"),
        (state["schema"] == "EventGridSchema", "event schema differs"),
        (not state["expires"], "it expires"),
        (not state["attributes"], "it sends delivery attributes"),
    )
    for passed, problem in checks:
        if not passed:
            problems.append(problem)
    return problems


def assess(target, group, topic, tenant, app_id, urls):
    """Classify every subscription on the topic; raise on duplicate or foreign answering/recording routes."""
    listed = az([
        "eventgrid", "system-topic", "event-subscription", "list", "--subscription", target["subscription"],
        "--resource-group", group, "--system-topic-name", topic,
    ])
    if not isinstance(listed, list):
        raise ValueError("Unexpected Event Grid subscription list.")
    found = {role: {"matches": [], "conflicts": []} for role in ROUTES}
    reviewed = []
    for item in listed:
        state = route_state(item)
        types = state["event_types"]
        touched = [role for role, spec in ROUTES.items() if not types or spec["event"] in types or state["name"] == spec["name"]]
        if is_observer(state):
            reviewed.append({"name": state["name"], "kind": "passive observer"})
            continue
        if not touched:
            reviewed.append({"name": state["name"], "kind": "unrelated event types", "events": types})
            continue
        detail = route_state(az([
            "eventgrid", "system-topic", "event-subscription", "show", "--subscription", target["subscription"],
            "--resource-group", group, "--system-topic-name", topic, "--name", state["name"], "--include-full-endpoint-url",
        ], secrets=tuple(urls.values())))
        for role in touched:
            problems = route_problems(detail, role, tenant, app_id, urls[role])
            entry = {"name": state["name"], "provisioning_state": detail["provisioning_state"], "problems": problems}
            found[role]["conflicts" if problems else "matches"].append(entry)
    actions = {}
    for role, entries in found.items():
        event = ROUTES[role]["event"]
        if entries["conflicts"]:
            raise RuntimeError(
                f"A subscription on the native topic receives {event} but doesn't match the planned route: "
                + "; ".join(f"{row['name']} ({', '.join(row['problems'])})" for row in entries["conflicts"])
                + ". This tool doesn't overwrite or delete subscriptions; review them with the owner."
            )
        if len(entries["matches"]) > 1:
            raise RuntimeError(f"Duplicate {event} subscriptions: {[row['name'] for row in entries['matches']]}. Keep one; review with the owner.")
        if entries["matches"]:
            match = entries["matches"][0]
            actions[role] = {"name": match["name"], "action": "none" if match["provisioning_state"] == "Succeeded" else "recreate",
                             "provisioning_state": match["provisioning_state"]}
        else:
            actions[role] = {"name": ROUTES[role]["name"], "action": "create", "provisioning_state": None}
    return actions, reviewed


def subscription_context(args):
    target = acs_target(args.native_acs_resource_id)
    tenant, app_id = guid(args.tenant_id), guid(args.delivery_app_id)
    topic = topic_name(args.system_topic_name)
    urls = endpoints(Path(args.endpoints_file).expanduser(), tenant, app_id)
    location = check_tenant(target, tenant)
    return target, tenant, app_id, topic, urls, location


def topic_location(target, topic, existing, topics):
    if existing is not None:
        if existing.get("name") != topic:
            raise ValueError(f"The native ACS resource already has system topic {existing.get('name')!r}; rerun with that --system-topic-name.")
        return group_of(existing.get("id")), "reuse" if existing.get("provisioningState") == "Succeeded" else "recreate"
    if any(row.get("name") == topic and str(group_of(row.get("id")) or "").lower() == target["resource_group"].lower() for row in topics):
        raise ValueError(f"System topic {topic!r} already exists for another source; choose another --system-topic-name.")
    return target["resource_group"], "create"


def subscriptions(args):
    target, tenant, app_id, topic, urls, location = subscription_context(args)
    output = private_directory(private_path(args.output_dir))
    hashes = {role: digest(url) for role, url in urls.items()}
    if not output.exists():
        if args.apply:
            raise ValueError("Run subscriptions without --apply first, review the plan, then reuse its --output-dir.")
        existing, topics = system_topic(target)
        group, topic_action = topic_location(target, topic, existing, topics)
        if existing is not None:
            actions, reviewed = assess(target, group, topic, tenant, app_id, urls)
        else:
            actions, reviewed = {role: {"name": spec["name"], "action": "create"} for role, spec in ROUTES.items()}, []
        plan = {
            "version": 1, "kind": "native-event-subscriptions", "tenant_id": tenant, "native_acs_resource_id": target["id"],
            "acs_location": location, "system_topic": {"name": topic, "resource_group": group, "action": topic_action},
            "delivery_app_id": app_id, "endpoint_sha256": hashes,
            "routes": {role: {"name": row["name"], "action": row["action"]} for role, row in actions.items()},
            "created_at": now(),
        }
        output.mkdir(mode=0o700)
        write_private(output / "plan.json", json.dumps(plan, indent=2) + "\n")
        print(json.dumps({
            "status": "planned", "plan_only": True, "private_plan": str(output / "plan.json"),
            "system_topic": plan["system_topic"], "routes": {
                role: {
                    **plan["routes"][role], "event_type": ROUTES[role]["event"],
                    "advanced_filters": ROUTES[role]["filters"], "entra_tenant": tenant, "entra_application": app_id,
                    "endpoint_host": urllib.parse.urlsplit(urls[role]).hostname, "endpoint_sha256": hashes[role][:16],
                    "acs_name_in_query": acs_name_in_query(urls[role], target["name"]),
                } for role in ROUTES
            }, "other_subscriptions_reviewed": reviewed, "remote_changes": 0, "calls_placed": 0,
            "next": f"Review, then rerun with --apply --confirm-acs-name {target['name']}.",
        }, indent=2))
        return
    plan = load_plan(output, "native-event-subscriptions")
    if (
        plan["tenant_id"] != tenant or plan["native_acs_resource_id"].lower() != target["id"].lower()
        or plan["delivery_app_id"] != app_id or plan["system_topic"]["name"] != topic or plan["endpoint_sha256"] != hashes
    ):
        raise ValueError("The tenant, ACS resource, delivery app, topic or endpoints file differs from the saved plan; plan again in a new directory.")
    group = plan["system_topic"]["resource_group"]

    def topic_observe():
        existing, _ = system_topic(target)
        if existing is None:
            return False, {}
        if existing.get("name") != topic or str(group_of(existing.get("id"))).lower() != group.lower():
            raise RuntimeError("Another system topic now serves the native ACS resource; stop and ask the human.")
        return existing.get("provisioningState") == "Succeeded", {"system_topic_id": existing.get("id")}

    def route_observe(role):
        def observe():
            if not topic_observe()[0]:
                raise RuntimeError("The native system topic isn't ready; rerun the same command for read-only reconciliation.")
            actions, _ = assess(target, group, topic, tenant, app_id, urls)
            return actions[role]["action"] == "none", {"name": actions[role]["name"]}
        return observe

    def route_write(role):
        def write(evidence):
            spec = ROUTES[role]
            arguments = [
                "eventgrid", "system-topic", "event-subscription", "create", "--subscription", target["subscription"],
                "--resource-group", group, "--system-topic-name", topic, "--name", evidence["name"],
                "--endpoint-type", "webhook", "--endpoint", urls[role], "--event-delivery-schema", "eventgridschema",
                "--included-event-types", spec["event"], "--azure-active-directory-tenant-id", tenant,
                "--azure-active-directory-application-id-or-uri", app_id,
            ]
            for row in spec["filters"]:
                arguments += ["--advanced-filter", row["key"], row["operatorType"], *row["values"]]
            az(arguments, timeout=900, secrets=tuple(urls.values()))
        return write

    steps = {
        "system-topic": (topic_observe, lambda _: az([
            "eventgrid", "system-topic", "create", "--subscription", target["subscription"], "--resource-group", group,
            "--name", topic, "--source", target["id"], "--topic-type", TOPIC_TYPE, "--location", plan["acs_location"],
        ], timeout=600), {"command": "az eventgrid system-topic create", "name": topic, "source": target["id"]}),
        **{
            role + "-subscription": (route_observe(role), route_write(role), {
                "command": "az eventgrid system-topic event-subscription create", "event_type": ROUTES[role]["event"],
                "endpoint_sha256": hashes[role], "entra_tenant": tenant, "entra_application": app_id,
                "advanced_filters": ROUTES[role]["filters"],
            }) for role in ROUTES
        },
    }
    journal = Journal(output)
    if not args.apply:
        print(json.dumps({"status": "saved_plan", "plan_only": True, "steps": step_status(journal, steps, SUBSCRIPTION_STEPS),
                          "remote_changes": 0}, indent=2))
        return
    if args.confirm_acs_name != target["name"]:
        raise ValueError("--apply requires --confirm-acs-name matching the native ACS resource name.")
    results = {name: run_step(journal, name, *steps[name], args.retry_step) for name in SUBSCRIPTION_STEPS}
    summary = verified_routes(target, group, topic, tenant, app_id, urls)
    path = receipt(output, {"status": "subscriptions_verified", "steps": results, **summary})
    print(json.dumps({"status": "subscriptions_verified", "steps": results, **summary, "private_receipt": path,
                      "next": "Run verify, then complete the Manage telephony exit gate.", "exit_gate": BANNER_GATE}, indent=2))


def verified_routes(target, group, topic, tenant, app_id, urls):
    actions, reviewed = assess(target, group, topic, tenant, app_id, urls)
    pending = {role: row for role, row in actions.items() if row["action"] != "none"}
    if pending:
        raise RuntimeError(f"Native routes are not Succeeded and matching: {pending}. Nothing was changed.")
    return {
        "system_topic": topic, "routes": {
            role: {"name": actions[role]["name"], "event_type": ROUTES[role]["event"], "provisioning_state": "Succeeded",
                   "advanced_filters": ROUTES[role]["filters"], "endpoint_sha256": digest(urls[role])[:16],
                   "entra_tenant": tenant, "entra_application": app_id}
            for role in ROUTES
        }, "other_subscriptions_reviewed": reviewed, "calls_placed": 0,
    }


def delivery_app_check(graph, app_id, event_grid_app_id):
    apps = graph.rows("applications", {"$filter": f"appId eq '{app_id}'", "$select": APP_SELECT, "$top": "2"})
    if len(apps) != 1:
        raise RuntimeError("The delivery app ID isn't a single application in this tenant.")
    app = apps[0]
    roles = [row for row in app.get("appRoles") or [] if row.get("value") == ROLE]
    if (
        len(roles) != 1 or roles[0].get("isEnabled") is not True
        or sorted(roles[0].get("allowedMemberTypes") or []) != ["Application", "User"]
        or app.get("signInAudience") != "AzureADMyOrg" or app.get("passwordCredentials") or app.get("keyCredentials")
    ):
        raise RuntimeError(f"The delivery app needs one enabled {ROLE} role for applications and users, single-tenant sign-in and no credentials.")
    principal = service_principal(graph, app_id)
    if principal is None:
        raise RuntimeError("The delivery app has no service principal in this tenant.")
    event_grid = event_grid_principal(graph, event_grid_app_id)
    rows = assignments(graph, principal["id"])
    role_id = str(roles[0].get("id", "")).lower()
    if not any(row.get("principalId") == event_grid["id"] and str(row.get("appRoleId", "")).lower() == role_id for row in rows):
        raise RuntimeError(f"Microsoft.EventGrid isn't assigned {ROLE} on the delivery app.")
    unexpected = [
        row.get("principalId") for row in rows
        if str(row.get("appRoleId", "")).lower() != role_id
        or (row.get("principalType") != "User" and row.get("principalId") != event_grid["id"])
    ]
    if unexpected:
        raise RuntimeError(f"Unexpected grants on the delivery app: {unexpected}. Review them with an Entra administrator.")
    return {"delivery_app_verified": True, "event_grid_assignment": True,
            "user_assignments": sum(row.get("principalType") == "User" for row in rows)}


def verify(args, graph):
    target, tenant, app_id, topic, urls, _ = subscription_context(args)
    receipt_path = private_path(args.receipt_file) if args.receipt_file else None
    if receipt_path is not None and (receipt_path.exists() or receipt_path.is_symlink()):
        raise ValueError("Use a new private verify receipt filename.")
    existing, _ = system_topic(target)
    if existing is None or existing.get("name") != topic or existing.get("provisioningState") != "Succeeded":
        raise RuntimeError("The native ACS system topic is missing, renamed or not Succeeded.")
    summary = {
        "status": "verified", **verified_routes(target, group_of(existing.get("id")), topic, tenant, app_id, urls),
        **delivery_app_check(graph, app_id, guid(args.event_grid_app_id)), "exit_gate": BANNER_GATE,
    }
    if receipt_path is not None:
        write_private(receipt_path, json.dumps({**summary, "recorded_at": now()}, indent=2) + "\n")
        summary["private_receipt"] = str(receipt_path)
    print(json.dumps(summary, indent=2))


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    commands = result.add_subparsers(dest="command", required=True)
    setup = commands.add_parser("bootstrap", help="Plan or apply the dedicated Event Grid delivery app through Microsoft Graph.")
    setup.add_argument("--tenant-id", required=True, type=guid)
    setup.add_argument("--output-dir", required=True, type=Path, help="New private directory for the plan and journal, outside the checkout.")
    setup.add_argument("--display-name", default=DEFAULT_DISPLAY_NAME)
    setup.add_argument("--event-grid-app-id", default=EVENT_GRID_APP_ID, type=guid)
    setup.add_argument("--apply", action="store_true")
    setup.add_argument("--confirm-tenant", type=guid)
    setup.add_argument("--retry-step", choices=BOOTSTRAP_STEPS, help="Resend one failed step once, after fixing its cause.")
    for name, description in (
        ("subscriptions", "Plan or apply the native system topic and two Entra-authenticated subscriptions."),
        ("verify", "Read back the native topic, both subscriptions and the delivery app; no writes."),
    ):
        command = commands.add_parser(name, help=description)
        command.add_argument("--tenant-id", required=True, type=guid)
        command.add_argument("--native-acs-resource-id", required=True)
        command.add_argument("--system-topic-name", required=True, type=topic_name)
        command.add_argument("--delivery-app-id", required=True, type=guid)
        command.add_argument("--endpoints-file", required=True, type=Path, help="Owner-only file with the URLs and IDs copied from Dynamics.")
        if name == "subscriptions":
            command.add_argument("--output-dir", required=True, type=Path)
            command.add_argument("--apply", action="store_true")
            command.add_argument("--confirm-acs-name")
            command.add_argument("--retry-step", choices=SUBSCRIPTION_STEPS, help="Resend one failed step once, after fixing its cause.")
        else:
            command.add_argument("--event-grid-app-id", default=EVENT_GRID_APP_ID, type=guid)
            command.add_argument("--receipt-file", type=Path)
    return result


def main(argv=None):
    args = parser().parse_args(argv)
    if args.command == "subscriptions":
        subscriptions(args)
    elif args.command == "bootstrap":
        bootstrap(args, Graph(args.tenant_id))
    else:
        verify(args, Graph(args.tenant_id))


if __name__ == "__main__":
    try:
        main()
    except urllib.error.HTTPError as error:
        print(json.dumps({"status": "failed", "error": f"Microsoft Graph returned HTTP {error.code} ({graph_error(error)})."}))
        raise SystemExit(1)
    except (ValueError, KeyError, RuntimeError, OSError, subprocess.SubprocessError) as error:
        print(json.dumps({"status": "failed", "error": str(error) if isinstance(error, (ValueError, RuntimeError)) else type(error).__name__}))
        raise SystemExit(1)
