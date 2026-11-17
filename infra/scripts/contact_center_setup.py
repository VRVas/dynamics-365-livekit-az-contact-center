"""Prepare a new Dynamics contact center: voice channel, voice-pipeline gate and phone-number eligibility.

Read-only by default. Only `voice-channel --apply --confirm-organization <org-url>` changes Dataverse, and it
sends at most one request per private --output-dir.
"""

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import time
import urllib.error
import urllib.parse
from datetime import date, datetime, timezone
from pathlib import Path
from uuid import uuid4

from native_voice_setup import Dataverse, guid, organization, private_path, read_private, write_private


LEARN = {
    "retrieve_setting": "https://learn.microsoft.com/en-us/power-apps/developer/data-platform/webapi/reference/retrievesetting",
    "provision_channels": "https://learn.microsoft.com/en-us/dynamics365/contact-center/implement/provision-channels",
    "install_voice": "https://learn.microsoft.com/en-us/dynamics365/customer-service/administer/voice-channel-install",
    "trial_numbers": "https://learn.microsoft.com/en-us/dynamics365/customer-service/administer/voice-channel-trial-phone-numbers",
    "conversation_table": "https://learn.microsoft.com/en-us/dynamics365/developer/reference/entities/msdyn_ocliveworkitem",
    "datacenter_regions": "https://learn.microsoft.com/en-us/power-platform/admin/new-datacenter-regions",
    "voice_regions": "https://learn.microsoft.com/en-us/dynamics365/customer-service/administer/voice-channel-region-availability",
    "billing_property": "https://learn.microsoft.com/en-us/rest/api/billing/billing-property/get?view=rest-billing-2024-04-01",
    "resources_list": "https://learn.microsoft.com/en-us/rest/api/resources/resources/list?view=rest-resources-2021-04-01",
    "number_eligibility": "https://learn.microsoft.com/en-us/azure/communication-services/concepts/numbers/sub-eligibility-number-capability",
    "uk_numbers": "https://learn.microsoft.com/en-us/azure/communication-services/concepts/numbers/phone-number-management-for-united-kingdom",
    "acs_retirement": "https://learn.microsoft.com/en-us/azure/communication-services/acs-retirement-and-breaking-changes-guide",
    "dynamics_acs_migration": "https://learn.microsoft.com/en-us/dynamics365/contact-center/administer/migrate-from-azure-communication-services#plan-for-phone-numbers",
}
SETTINGS = {"provisioned": "msdyn_IsCCaaSProvisioned", "telephony": "msdyn_IsCCaaSTelephonyProvisioned"}
CREATE = {"action": "CCaaS_CreateContactCenter", "body": {"Channels": "[5]"}}
ENABLE = {"action": "CCaaS_UpdateContactCenterChannels", "body": {"ChannelOperations": '[{"Channel":5,"Operation":1}]'}}
VOICE_CHANNEL_TYPE = "5"
VOICE_STATES = {2101: "in_progress", 1001: "done", 2102: "failed"}
POLL_SECONDS = 60
PIPELINE_QUERY = {
    "$select": "msdyn_infracreatedby,createdon",
    "$filter": "msdyn_channel eq '192440000'",
    "$orderby": "createdon desc",
    "$top": "5",
}
ELASTIC = "Elastic"
# Dynamics 365 URL suffixes from Learn's datacenter-regions page; printed for information only.
REGIONS = {
    "crm": "North America", "crm2": "South America", "crm3": "Canada", "crm4": "Europe", "crm5": "Asia Pacific",
    "crm6": "Australia", "crm7": "Japan", "crm8": "India", "crm9": "US Government Community Cloud",
    "crm11": "United Kingdom", "crm12": "France", "crm14": "South Africa", "crm15": "United Arab Emirates",
    "crm16": "Germany", "crm17": "Switzerland", "crm19": "Norway", "crm20": "Singapore", "crm21": "Korea",
    "crm22": "Sweden",
}
BILLING_API = "2024-04-01"
RESOURCES_API = "2021-04-01"
BILLING_FIELDS = ("billingAccountAgreementType", "billingAccountStatus", "billingAccountSoldToCountry")
ACS_TYPE = "Microsoft.Communication/CommunicationServices"
ACS_CUTOFF = "2026-10-23"
ADMIN_CENTER = (
    "Ask the owner to open Copilot Service admin center > Customer Support > Channels > Manage channels, "
    "turn on Voice (Add voice = Yes, accept the Voice and SMS Terms) and select Save."
)
TRIAL_CALL = (
    "Provisioning added a trial ACS resource and a US toll-free trial number. Find the number in Copilot Service "
    "admin center > Customer Support > Channels > Phone numbers and place one call to it (Learn: the trial number "
    "can only be called from within the US). Then run the pipeline gate."
)


def utc():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def emit(document):
    print(json.dumps(document, indent=2, ensure_ascii=False))


def private_json(path):
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"{path.name} must be a regular owner-only file, not a symlink.")
    document = json.loads(read_private(path))
    if not isinstance(document, dict):
        raise ValueError(f"{path.name} is not a journal record.")
    return document


def private_directory(path):
    if path.is_symlink() or not path.is_dir() or (os.name != "nt" and path.stat().st_mode & 0o077):
        raise ValueError("Use an owner-only (chmod 700) --output-dir that is not a symlink.")
    return path


def setting(api, name):
    detail = api.get(f"RetrieveSetting(SettingName='{name}')").get("SettingDetail")
    value = detail.get("Value") if isinstance(detail, dict) else None
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in {"true", "false"}:
        return value.strip().lower() == "true"
    raise ValueError(f"RetrieveSetting returned no true/false value for {name}; no request was sent.")


def voice_status(document):
    channels = document.get("ContactCenterChannels") if isinstance(document, dict) else None
    if isinstance(channels, str):
        channels = json.loads(channels)
    if not isinstance(channels, list) or not all(isinstance(row, dict) for row in channels):
        raise ValueError("ContactCenterChannels is not a JSON list of channels.")
    voice = [row for row in channels if str(row.get("ChannelType")) == VOICE_CHANNEL_TYPE]
    if len(voice) > 1:
        raise ValueError("ContactCenterChannels lists the voice channel more than once.")
    if not voice:
        return None
    status = voice[0].get("Status")
    if isinstance(status, str) and status.strip().isdigit():
        return int(status)
    return status if type(status) is int else str(status)


def read_state(api):
    state = {key: setting(api, name) for key, name in SETTINGS.items()}
    try:
        status, readable = voice_status(api.get("CCaaS_GetContactCenterChannels()")), True
    except (urllib.error.HTTPError, ValueError):
        # Before provisioning the custom API may not answer; the documented settings still decide.
        status, readable = None, False
    if not readable:
        voice = "unreadable"
    elif status is None:
        voice = "not_listed"
    else:
        voice = VOICE_STATES.get(status, "unknown")
    return {**state, "voice_status": status, "voice": voice}


def public_state(state):
    return {
        SETTINGS["provisioned"]: state["provisioned"], SETTINGS["telephony"]: state["telephony"],
        "voice_channel_status": state["voice_status"], "voice_channel_state": state["voice"],
    }


def ready(state):
    return state["telephony"] is True and state["voice"] == "done"


def load_journal(output, org, tenant):
    if not output.exists():
        return None
    private_directory(output)
    intent_path, result_path = output / "intent.json", output / "post-result.json"
    if not (intent_path.exists() or intent_path.is_symlink()):
        if result_path.exists() or result_path.is_symlink():
            raise ValueError("post-result.json has no matching intent.json; restore the original --output-dir.")
        return None
    intent = private_json(intent_path)
    if intent.get("kind") != "voice-channel" or intent.get("org_url") != org or intent.get("tenant_id") != tenant:
        raise ValueError("This --output-dir journals a request for another organization; use that organization's directory.")
    result = private_json(result_path) if result_path.exists() or result_path.is_symlink() else None
    return {"intent": intent, "result": result}


def decide(state, journal):
    """Return the next step: ready, failed, wait, send, admin_center or ask."""
    if ready(state):
        return "ready", None
    if state["voice"] == "failed":
        return "failed", None
    if state["voice"] in {"in_progress", "done"}:
        return "wait", None
    if journal is not None:
        outcome = (journal["result"] or {}).get("outcome")
        return ("wait", None) if outcome == "accepted" else ("admin_center", None)
    if state["provisioned"] is False and state["telephony"] is False:
        return "send", CREATE
    if state["provisioned"] is True and state["telephony"] is False and state["voice"] != "unreadable":
        return "send", ENABLE
    return "ask", None


def readback_command(args):
    return " ".join([
        "python infra/scripts/contact_center_setup.py voice-channel", "--tenant-id", args.tenant_id,
        "--org-url", args.org_url, "--output-dir", shlex.quote(str(args.output_dir)),
    ])


def gate_command(args):
    return f"python infra/scripts/contact_center_setup.py pipeline --tenant-id {args.tenant_id} --org-url {args.org_url}"


def fallback(args, reason):
    return {
        "stopped_because": reason, "request_resent": False, "ask_owner": ADMIN_CENTER,
        "then_run": readback_command(args),
        "then": "Repeat the readback until it reports voice_ready. Adding --apply --confirm-organization to the "
                "same command polls again; it never resends a request journaled in this --output-dir.",
        "learn": [LEARN["provision_channels"], LEARN["install_voice"]],
    }


def journal_summary(journal):
    if journal is None:
        return None
    intent, result = journal["intent"], journal["result"] or {}
    return {
        "action": intent.get("action"), "journaled_at": intent.get("created_at"),
        "outcome": result.get("outcome", "unknown (no result recorded)"), "http_status": result.get("http_status"),
    }


def poll(api, minutes, sleep, clock):
    deadline, polls = clock() + minutes * 60, []
    while True:
        sleep(POLL_SECONDS)
        state = read_state(api)
        polls.append({"at": utc(), **public_state(state)})
        print(json.dumps({"poll": len(polls), **public_state(state)}), file=sys.stderr, flush=True)
        if ready(state):
            return "voice_ready", state, polls
        if state["voice"] == "failed":
            return "failed", state, polls
        if clock() >= deadline:
            return "timed_out", state, polls


def voice_channel(args, api, *, sleep=time.sleep, clock=time.monotonic):
    org = organization(args.org_url)
    if api.org != org:
        raise ValueError("The Dataverse client must match --org-url.")
    if args.apply and args.confirm_organization != org:
        raise ValueError("--apply requires --confirm-organization matching --org-url.")
    if not args.apply and args.confirm_organization is not None:
        raise ValueError("--confirm-organization is valid only with --apply.")
    if not 1 <= args.wait_minutes <= 120:
        raise ValueError("--wait-minutes must be between 1 and 120.")
    output = private_path(args.output_dir)
    journal = load_journal(output, org, api.tenant)
    state = read_state(api)
    step, request = decide(state, journal)
    result = {
        "org_url": org, "readback": public_state(state), "journal": journal_summary(journal),
        "custom_api_note": "CCaaS_* actions ship with Contact Center as Dataverse custom APIs but aren't documented "
                           "on Learn; the admin center is Microsoft's documented path.",
    }
    stops = {
        "failed": "The voice channel reports Status 2102 (failed).",
        "admin_center": "The journaled request did not report success and voice isn't in progress.",
        "ask": "The readback is inconsistent or unreadable, so the tool can't choose a request safely.",
    }
    if not args.apply:
        if step == "send":
            result.update(status="planned", planned_request={"method": "POST", **request}, remote_changes=False,
                          next="Review, then rerun with --apply --confirm-organization " + org + ".")
        elif step == "ready":
            result.update(status="voice_ready", next=TRIAL_CALL, then_run=gate_command(args))
        elif step == "wait":
            result.update(status="in_progress", next="Rerun this readback in a few minutes, or add --apply "
                          "--confirm-organization to poll. Nothing is resent.")
        else:
            result.update(status="needs_human", **fallback(args, stops[step]))
        emit(result)
        return 0 if step in {"send", "ready", "wait"} else 1
    if not output.exists():
        output.mkdir(mode=0o700)
    polls = []
    if step == "send":
        write_private(output / "intent.json", json.dumps({
            "version": 1, "kind": "voice-channel", "org_url": org, "tenant_id": api.tenant,
            "action": request["action"], "body": request["body"], "created_at": utc(), "before": public_state(state),
        }, indent=2) + "\n")
        try:
            api.request(request["action"], method="POST", body=request["body"])
        except (OSError, ValueError) as error:
            code = error.code if isinstance(error, urllib.error.HTTPError) else None
            write_private(output / "post-result.json", json.dumps({
                "outcome": "rejected" if code else "uncertain", "http_status": code, "at": utc(),
            }) + "\n")
            try:
                state = read_state(api)
            except (OSError, ValueError):
                result["readback_error"] = "The readback after the failed request also failed; rerun the readback."
            reason = f"{request['action']} returned HTTP {code}." if code else f"{request['action']} got no confirmed answer."
            step, result["post"] = "post_failed", {"action": request["action"], "http_status": code}
            stops["post_failed"] = reason
        else:
            write_private(output / "post-result.json", json.dumps({"outcome": "accepted", "at": utc()}) + "\n")
            result["post"] = {"action": request["action"], "accepted": True}
            step = "wait"
    if step == "wait":
        step, state, polls = poll(api, args.wait_minutes, sleep, clock)
        stops["timed_out"] = f"Voice isn't ready after {args.wait_minutes} minutes of polling."
    elif step == "ready":
        step = "voice_ready"
    result.update(readback=public_state(state), polls=len(polls))
    if step == "voice_ready":
        result.update(status="voice_ready", next=TRIAL_CALL, then_run=gate_command(args))
    else:
        result.update(status="needs_human", **fallback(args, stops[step]))
    receipt = output / f"after-{uuid4().hex}.json"
    write_private(receipt, json.dumps({**result, "poll_history": polls, "finished_at": utc()}, indent=2) + "\n")
    result["receipt"] = str(receipt)
    emit(result)
    return 0 if step == "voice_ready" else 1


def region(org):
    host = urllib.parse.urlsplit(org).hostname
    match = re.search(r"\.(crm\d*)\.dynamics\.com$", host)
    label = match[1] if match else None
    return host, (f"{REGIONS[label]} ({label})" if label in REGIONS else "unknown; see the datacenter regions page")


def pipeline(args, api):
    org = organization(args.org_url)
    if api.org != org:
        raise ValueError("The Dataverse client must match --org-url.")
    rows = api.get("msdyn_ocliveworkitems", PIPELINE_QUERY).get("value")
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        raise ValueError("Dataverse returned an unexpected conversation list.")
    recent = sorted(
        ({"createdon": row.get("createdon"), "msdyn_infracreatedby": row.get("msdyn_infracreatedby")} for row in rows),
        key=lambda row: row["createdon"] or "", reverse=True,
    )[:5]
    host, location = region(org)
    result = {
        "organization_host": host, "region_for_information": location,
        "recent_voice_conversations": recent,
        "decision_rule": "Continue only when the most recent voice conversation's msdyn_infracreatedby is Elastic. "
                         "The region is printed for information; it doesn't decide.",
        "field_note": "msdyn_infracreatedby is marked 'for internal use only' in the Conversation table reference.",
        "learn": [LEARN["conversation_table"], LEARN["datacenter_regions"], LEARN["trial_numbers"]],
    }
    call = ("Place one call to the trial number (Copilot Service admin center > Customer Support > Channels > "
            "Phone numbers; Learn says it can only be called from within the US), then rerun this gate.")
    if not recent:
        emit({**result, "status": "no_voice_conversations", "next": call})
        return 1
    latest = recent[0]["msdyn_infracreatedby"]
    if latest == ELASTIC:
        emit({**result, "status": "elastic", "latest_pipeline": latest,
              "next": "Continue with the native voice stack. On the first HUMAIN call, check its lines in the saved transcript."})
        return 0
    if not latest:
        emit({**result, "status": "blocked", "latest_pipeline": latest,
              "next": "The newest voice conversation has no pipeline value yet. Wait a minute and rerun; if it stays "
                      "empty, " + call[0].lower() + call[1:]})
        return 1
    emit({**result, "status": "blocked", "latest_pipeline": latest,
          "next": f"Stop: the newest voice conversation ran on {latest!r}, not Elastic. Don't build native voice here; "
                  "on CTQM3, HUMAIN's lines were missing from the saved transcript. Agree a new environment with the "
                  "owner (default Switzerland, crm17), provision voice there and rerun this gate after one call."})
    return 1


def cli_error(stderr):
    codes = re.findall(r'"code"\s*:\s*"([A-Za-z0-9_.]+)"', stderr or "")
    statuses = re.findall(r"\b(Bad Request|Unauthorized|Forbidden|Not Found|Conflict|Too Many Requests)\b", stderr or "")
    found = list(dict.fromkeys(codes + statuses))
    return ", ".join(found[:3]) if found else "no error code returned"


def az_json(*args):
    try:
        result = subprocess.run(
            ["az", *args, "-o", "json", "--only-show-errors"], capture_output=True, text=True, timeout=90,
        )
    except FileNotFoundError:
        raise RuntimeError("Install the Azure CLI and run az login first.") from None
    if result.returncode:
        raise RuntimeError(f"az {args[0]} failed ({cli_error(result.stderr)}). Check az login and your read access "
                           "to the subscription and its billing account.")
    return json.loads(result.stdout or "null")


def arm_url(path, query):
    return "https://management.azure.com" + path + "?" + urllib.parse.urlencode(
        query, quote_via=urllib.parse.quote, safe="$/'",
    )


def created_before(created, cutoff):
    try:
        return date.fromisoformat(created[:10]) < cutoff
    except (TypeError, ValueError):
        return None


def acs_resources(subscription, cutoff):
    url = arm_url(f"/subscriptions/{subscription}/resources", {
        "$filter": f"resourceType eq '{ACS_TYPE}'", "$expand": "createdTime", "api-version": RESOURCES_API,
    })
    rows = []
    for _ in range(10):
        page = az_json("rest", "--method", "get", "--subscription", subscription, "--url", url)
        if not isinstance(page, dict) or not isinstance(page.get("value"), list):
            raise ValueError("The ARM resource list returned an unexpected shape.")
        rows.extend(row for row in page["value"] if isinstance(row, dict))
        url = page.get("nextLink")
        if not url:
            break
        if not url.startswith(f"https://management.azure.com/subscriptions/{subscription}/"):
            raise ValueError("The ARM resource list returned an unexpected next link.")
    else:
        raise ValueError("More than 10 pages of ACS resources; review the subscription in the Azure portal.")
    resources = []
    for row in rows:
        if str(row.get("type", "")).lower() != ACS_TYPE.lower():
            continue
        group = re.search(r"/resourceGroups/([^/]+)/", str(row.get("id", "")), re.IGNORECASE)
        resources.append({
            "name": row.get("name"), "resource_group": group[1] if group else None, "location": row.get("location"),
            "created_time": row.get("createdTime"), "created_before_cutoff": created_before(row.get("createdTime"), cutoff),
        })
    return sorted(resources, key=lambda row: str(row["created_time"]))


def eligibility(args):
    subscription, tenant = guid(args.subscription_id), guid(args.tenant_id)
    try:
        cutoff = date.fromisoformat(args.acs_cutoff)
    except ValueError:
        raise ValueError("--acs-cutoff must be a date such as 2026-10-23.") from None
    account = az_json("account", "show", "--subscription", subscription)
    if not isinstance(account, dict) or str(account.get("tenantId", "")).lower() != tenant:
        raise ValueError("The subscription isn't in --tenant-id; sign in to the right tenant first.")
    billing = az_json("rest", "--method", "get", "--subscription", subscription, "--url", arm_url(
        f"/subscriptions/{subscription}/providers/Microsoft.Billing/billingProperty/default",
        {"api-version": BILLING_API, "includeBillingCountry": "true"},
    ))
    properties = billing.get("properties") if isinstance(billing, dict) else None
    if not isinstance(properties, dict):
        raise ValueError("Billing Property - Get returned no properties.")
    facts = {key: properties.get(key) for key in BILLING_FIELDS}
    missing = [key for key, value in facts.items() if not value]
    resources = acs_resources(subscription, cutoff)
    before = [row for row in resources if row["created_before_cutoff"]]
    unknown = [row for row in resources if row["created_before_cutoff"] is None]
    findings = []
    if missing:
        findings.append("Missing " + ", ".join(missing) + ". Ask someone who can read the billing account to rerun "
                        "this check. Don't use billingCountry; it returned null in practice.")
    if before:
        findings.append(f"{len(before)} ACS resource(s) in this subscription were created before {cutoff}. Existing ACS "
                        "customers can still buy numbers for now, but Microsoft may add restrictions; check the guide.")
    elif unknown:
        findings.append("ARM returned no creation time for " + ", ".join(str(row["name"]) for row in unknown)
                        + ". Check those resources' creation dates in the Azure portal activity log or with the owner.")
    else:
        findings.append(f"No ACS resource in this subscription was created before {cutoff}. The ACS retirement guide says "
                        "customers without one can't acquire phone numbers. Check other subscriptions in the tenant "
                        "before you propose application-phone or native-voice.")
    emit({
        "status": "incomplete" if missing else "review",
        "subscription_id": subscription, "billing": facts, "acs_cutoff": str(cutoff),
        "acs_resources": resources,
        "acs_resource_created_before_cutoff": True if before else (None if unknown else False),
        "findings": findings,
        "compare_with": {
            "phone_number_eligibility": LEARN["number_eligibility"], "united_kingdom_numbers": LEARN["uk_numbers"],
            "acs_retirement_guide": LEARN["acs_retirement"], "dynamics_acs_migration": LEARN["dynamics_acs_migration"],
        },
        "next": "Compare agreement type, account status and sold-to country with the eligibility table for the number "
                "country you plan to use, and the ACS creation times with the retirement guide, before proposing numbers.",
    })
    return 1 if missing else 0


HELP = {
    "voice-channel": f"""Provision the Dynamics voice channel through the Dataverse Web API.

Read-only by default: reads {SETTINGS['provisioned']} and {SETTINGS['telephony']} with the documented
RetrieveSetting function (both default to False), reads CCaaS_GetContactCenterChannels() and prints the one
request it would send.

With --apply --confirm-organization <org-url> it journals the request in --output-dir, then sends one POST:
  not provisioned:            CCaaS_CreateContactCenter          {{"Channels": "[5]"}}
  provisioned, no telephony:  CCaaS_UpdateContactCenterChannels  {{"ChannelOperations": "[{{\\"Channel\\":5,\\"Operation\\":1}}]"}}
It then polls CCaaS_GetContactCenterChannels() every minute. For ChannelType 5 (voice), Status 2101 means
in progress, 1001 done and 2102 failed. It's done when Status is 1001 and {SETTINGS['telephony']}
reads True. Voice-only provisioning took about 9 minutes.

It never sends a request twice. If the POST fails, the status turns 2102 or --wait-minutes pass, it stops
and prints the admin-center step: Copilot Service admin center > Customer Support > Channels > Manage
channels > Voice. A rerun with the same --output-dir only reads back and polls.

The two CCaaS actions ship with Contact Center as Dataverse custom APIs but aren't documented on Learn.
The admin center is Microsoft's documented path.

Provisioning adds a trial ACS resource and a US toll-free trial number. The pipeline gate needs one call
to that number.

Exit 0: planned, in progress (read-only) or voice_ready. Exit 1: stopped; follow the printed steps.

Learn: {LEARN['retrieve_setting']}
       {LEARN['provision_channels']}
       {LEARN['install_voice']}
       {LEARN['trial_numbers']}""",
    "pipeline": f"""Gate native voice on the Elastic voice pipeline. Read-only.

Reads the five newest voice conversations:
  GET msdyn_ocliveworkitems?$select=msdyn_infracreatedby,createdon
      &$filter=msdyn_channel eq '192440000'&$orderby=createdon desc&$top=5
Exit 0 only when the newest one's msdyn_infracreatedby is Elastic. Any other value exits 1: on CTQM3,
HUMAIN's lines were missing from the saved transcript. No voice conversation yet also exits 1: place one
call to the trial number (Copilot Service admin center > Customer Support > Channels > Phone numbers;
Learn says it can only be called from within the US), then rerun.

The org host and its region (crm17 = Switzerland, the owner's default) are printed for information. The
decision uses only the pipeline value, so run the gate in every environment.

msdyn_infracreatedby is marked "for internal use only" in the Conversation table reference.

Learn: {LEARN['conversation_table']}
       {LEARN['datacenter_regions']}
       {LEARN['voice_regions']}""",
    "eligibility": f"""Print the facts that decide ACS phone-number eligibility. Read-only (az rest on Azure Resource Manager).

Billing Property - Get (api-version {BILLING_API}, includeBillingCountry=true) gives
billingAccountAgreementType, billingAccountStatus and billingAccountSoldToCountry. Use the sold-to
country: billingCountry returned null in practice.

Lists the subscription's ACS resources ({ACS_TYPE}) with
$expand=createdTime. Number acquisition now needs an ACS resource created before the retirement
cutoff (--acs-cutoff).

It doesn't decide eligibility. Compare the JSON with the live Learn pages it prints. Exit 1 when a
billing field is missing (you may lack billing read access).

Learn: {LEARN['billing_property']}
       {LEARN['resources_list']}
       {LEARN['number_eligibility']}
       {LEARN['acs_retirement']}""",
}


def parser():
    result = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = result.add_subparsers(dest="command", required=True)

    def command(name, summary):
        return commands.add_parser(
            name, help=summary, description=HELP[name], formatter_class=argparse.RawDescriptionHelpFormatter,
        )

    def dataverse(item):
        item.add_argument("--tenant-id", required=True, type=guid, help="Entra tenant of the Dynamics organization.")
        item.add_argument("--org-url", required=True, type=organization, help="For example https://<organization>.crm17.dynamics.com")

    voice = command("voice-channel", "Read, then optionally provision, the Dynamics voice channel.")
    dataverse(voice)
    voice.add_argument("--output-dir", required=True, type=Path,
                       help="Private journal directory outside the checkout. Reuse it on every rerun; it blocks resends.")
    voice.add_argument("--apply", action="store_true", help="Send at most one provisioning request, then poll.")
    voice.add_argument("--confirm-organization", type=organization, help="Repeat --org-url to approve --apply.")
    voice.add_argument("--wait-minutes", type=int, default=30, help="How long --apply polls before it stops (default 30).")
    gate = command("pipeline", "Exit 0 only if the newest voice conversation ran on the Elastic pipeline.")
    dataverse(gate)
    check = command("eligibility", "Print billing facts and ACS resource creation times for number eligibility.")
    check.add_argument("--tenant-id", required=True, type=guid, help="Entra tenant that owns the subscription.")
    check.add_argument("--subscription-id", required=True, type=guid, help="Subscription that will own the ACS resource.")
    check.add_argument("--acs-cutoff", default=ACS_CUTOFF,
                       help=f"Creation cutoff from the ACS retirement guide (default {ACS_CUTOFF}). Pass the guide's current date if it changed.")
    return result


def main(argv=None):
    args = parser().parse_args(argv)
    if args.command == "eligibility":
        return eligibility(args)
    api = Dataverse(args.org_url, args.tenant_id)
    return {"voice-channel": voice_channel, "pipeline": pipeline}[args.command](args, api)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except urllib.error.HTTPError as error:
        print(json.dumps({"status": "failed", "error": f"Dataverse returned HTTP {error.code}; nothing was resent."}))
        raise SystemExit(1)
    except (ValueError, KeyError, RuntimeError, OSError, subprocess.SubprocessError) as error:
        print(json.dumps({"status": "failed", "error": str(error) if isinstance(error, (ValueError, RuntimeError)) else type(error).__name__}))
        raise SystemExit(1)
