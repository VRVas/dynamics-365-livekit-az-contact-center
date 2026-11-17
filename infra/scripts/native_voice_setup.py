"""Prepare the documented native Dynamics voice deployment; never publishes or places calls."""

import argparse
import copy
import hashlib
import json
import os
import re
import secrets
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from dotenv import dotenv_values
import yaml

import stage_native_media_topic as topics


CONTROLLER = "NativeSpeechTurnPilot"
CONTEXT = "NativeMediaContext"
VARIABLE = "msdyn_ConversationId"
CASE_VARIABLE = topics.CASE_VARIABLE
GLOBAL_VARIABLE_COMPONENT = 12
REQUIRED_READS = {
    "prvReadmsdyn_liveworkstream": "Global",
    "prvReadActivity": "Local",
    "prvReadmsdyn_ocphonecallengagementctx": "Local",
    "prvReadQueue": "Global",
    "prvReadmsdyn_ocphonenumber": "Global",
    "prvReadUser": "Local",
}
DEPTH = {"Basic": 0, "Local": 1, "Deep": 2, "Global": 3}
BUNDLE_FILES = {
    "controller": "NativeSpeechTurnPilot.topic.yaml",
    "context": "NativeMediaContext.topic.yaml",
    "entry": "ConversationStart.topic.yaml",
}
STARTUP_AUDIO_FLAGS = {
    "msdyn_skipwaitmusicforivr": True,
    "msdyn_playrecordingtranscriptionnotifications": False,
}
STARTUP_AUDIO_PROPAGATION_SECONDS = 900
ACS_RESOURCE_ID = re.compile(
    r"/subscriptions/([0-9a-fA-F-]{36})/resourceGroups/([A-Za-z0-9_.()-]+)/"
    r"providers/Microsoft\.Communication/communicationServices/([A-Za-z0-9-]+)"
)
PROVIDER_TABLE = "msdyn_occommunicationprovidersetting"
PROVIDER_ENTRY_TABLE = "msdyn_occommunicationprovidersettingentry"
PVA_EXTENSION = "OmnichannelPrimePVAExtensionAnchor"
# Voice channel settings (Learn: msdyn_ocvoicechannelsetting). Consult must stay on;
# the transfer and Teams controls are reported against their expected off state.
CONSULT_CONTROL = "msdyn_agentexternalparticipantcontrolenabled"
TRANSFER_CONTROLS = (
    "msdyn_agentexternalparticipanttransfercontrolenabled",
    "msdyn_agentexternalteamsparticipantcontrolenabled",
    "msdyn_agentexternalteamsparticipanttransfercontrolenabled",
)
TOPIC_COMPONENT = 9
STARTER_ESCALATION = re.compile(r"\bnot\s+(?:currently\s+)?configured\b", re.IGNORECASE)
REDIRECT_ONLY_TRIGGERS = {"OnRedirect", "OnSystemRedirect"}
READER_ROLE_NAME = "Contact Center Native Voice Reader"
READER_MAX_REMOVALS = 2
READER_MAX_ADDITIONS = 2


def guid(value):
    if not isinstance(value, str) or not re.fullmatch(
        r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", value
    ) or UUID(value).int == 0:
        raise ValueError("Use a nonzero UUID, not a display name or placeholder.")
    return str(UUID(value))


def organization(value):
    if not isinstance(value, str) or not re.fullmatch(r"https://[a-z0-9-]+\.crm\d*\.dynamics\.com", value):
        raise ValueError("Use the HTTPS Dataverse organization origin without a path or trailing slash.")
    return value


def voice_origin(value):
    url = urllib.parse.urlsplit(value)
    if (
        url.scheme != "https" or not url.hostname or not url.hostname.endswith(".azurecontainerapps.io")
        or url.path or url.query or url.fragment or url.username or url.password or url.port
    ):
        raise ValueError("Use the HTTPS Azure Container Apps voice origin without a path or trailing slash.")
    return value


def acs_resource_id(value):
    match = ACS_RESOURCE_ID.fullmatch(value) if isinstance(value, str) else None
    if not match:
        raise ValueError("Use the full native Dynamics ACS resource ID, not its endpoint or immutable call ID.")
    guid(match[1])
    return value


def private_path(path):
    path = Path(path).expanduser().resolve()
    if path.is_relative_to(topics.ROOT):
        raise ValueError("Generated deployment material and backups must be outside the repository.")
    if not path.parent.is_dir():
        raise ValueError("Create the private output parent directory first.")
    return path


def write_private(path, content):
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w", newline="") as output:
        output.write(content)


def read_private(path):
    path = Path(path).expanduser().resolve()
    if os.name != "nt" and path.stat().st_mode & 0o077:
        raise ValueError("Restrict the input file to its owner (chmod 600) before reading deployment material.")
    return path.read_bytes().decode("utf-8")


def secret_value(path, key):
    read_private(path)
    value = dotenv_values(path, interpolate=False).get(key)
    if not isinstance(value, str) or len(value) < 32 or "\n" in value or "\r" in value:
        raise ValueError(f"The private file must contain a single-line {key} of at least 32 characters.")
    return value


def env_file(values):
    if any(not isinstance(value, str) or "\r" in value or "\n" in value for value in values.values()):
        raise ValueError("Environment values must be single-line strings.")
    return "".join(f"{key}={json.dumps(value, ensure_ascii=False)}\n" for key, value in values.items())


def bootstrap_env(args):
    ingest = secret_value(args.secrets_file, "INGEST_API_KEY")
    values = dotenv_values(args.secrets_file, interpolate=False)
    authorization = values.get("HUMAIN_AUTHORIZATION") or ""
    output = private_path(args.output)
    write_private(output, env_file({
        "INGEST_API_KEY": ingest, "HUMAIN_AUTHORIZATION": authorization,
        "HUMAIN_TOKEN_ENDPOINT": "https://livekit-admin.sanad.ink/api/tokens/client",
        "HUMAIN_VOICE": "mul_nourah", "HUMAIN_ASR_MODEL": "bayan_playground", "HUMAIN_LLM_PROVIDER": "allam",
        "HUMAIN_INTERRUPTION_POLICY_MODE": "off", "HUMAIN_TURN_TAKING_STRATEGY": "fast_vad",
        "ACS_AUTO_CONFERENCE_ENABLED": "false", "DYNAMICS_OUTBOUND_ENABLED": "false",
        "DYNAMICS_NATIVE_MEDIA_ENABLED": "false",
    }))
    print(json.dumps({
        "bootstrap_env": str(output), "humain_credentials_present": bool(authorization),
        "observer_credentials_deliberately_omitted": True, "cloud_changes": False,
    }))


def observer_expiry(expires_at, permanent):
    if permanent:
        if expires_at:
            raise ValueError("Permanent observation must not also specify an expiry.")
        return ""
    if not expires_at:
        raise ValueError("Choose a future observer expiry or explicit permanent mode.")
    expiry = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
    if expiry.tzinfo is None or expiry <= datetime.now(timezone.utc):
        raise ValueError("The observer expiry must be a future timestamp with a time zone.")
    return expiry.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def native_acs_subscription(resource, tenant):
    subscription = guid(ACS_RESOURCE_ID.fullmatch(acs_resource_id(resource))[1])
    account = json.loads(subprocess.run(
        ["az", "account", "show", "--subscription", subscription, "-o", "json"],
        check=True, capture_output=True, text=True, timeout=30,
    ).stdout)
    if account["tenantId"].lower() != guid(tenant):
        raise ValueError("The native ACS subscription is not in the Dynamics tenant.")
    return subscription


def native_acs_connection_string(resource, subscription):
    result = subprocess.run([
        "az", "rest", "--method", "post", "--subscription", subscription,
        "--url", "https://management.azure.com" + resource + "/listKeys?api-version=2023-04-01",
        "-o", "json", "--only-show-errors",
    ], check=True, capture_output=True, text=True, timeout=45)
    connection = json.loads(result.stdout)["primaryConnectionString"]
    if not isinstance(connection, str) or "endpoint=https://" not in connection or "accesskey=" not in connection.lower():
        raise ValueError("The native ACS connection string was not returned in the expected format.")
    return connection


def copy_sheet(args):
    resource, tenant, event_app = acs_resource_id(args.native_acs_resource_id), guid(args.tenant_id), guid(args.event_grid_app_id)
    output = private_path(args.output)
    if output.exists():
        raise ValueError("Use a new copy-sheet filename. Delete the old sheet once the connection readback passes.")
    connection = native_acs_connection_string(resource, native_acs_subscription(resource, tenant))
    fields = (
        ("ACS Resource Name", ACS_RESOURCE_ID.fullmatch(resource)[3]), ("ACS Resource ID", resource),
        ("Connection string", connection), ("Event grid app id", event_app),
        ("Event grid app tenant id (prefilled; confirm it matches)", tenant),
    )
    write_private(output, "".join(f"{label}: {value}\n" for label, value in fields) + (
        "\nPaste each value into Use existing resource. Delete this file after the connection readback passes.\n"
    ))
    print(json.dumps({
        "private_copy_sheet": str(output), "fields": [label for label, _ in fields],
        "connection_string_printed": False, "cloud_changes": False,
    }))


def runtime_env(args):
    resource = acs_resource_id(args.native_acs_resource_id)
    if args.app_number and not re.fullmatch(r"\+[1-9][0-9]{6,14}", args.app_number):
        raise ValueError("An optional application ACS number must use E.164 format.")
    expiry = observer_expiry(args.expires_at, args.permanent)
    workstreams = [guid(args.workstream_id), *(guid(value) for value in args.additional_workstream_id)]
    if len(set(workstreams)) != len(workstreams):
        raise ValueError("Native workstream IDs must be distinct.")
    output = private_path(args.output)
    if output.exists():
        raise ValueError("Use a new runtime environment filename.")
    subscription = native_acs_subscription(resource, args.tenant_id)
    media_key = secret_value(args.secrets_file, "DYNAMICS_NATIVE_MEDIA_API_KEY")
    probe_key = secret_value(args.secrets_file, "DYNAMICS_NATIVE_PROBE_API_KEY")
    connection = native_acs_connection_string(resource, subscription)
    write_private(output, env_file({
        "DYNAMICS_ORG_URL": organization(args.org_url),
        **({"ACS_SOURCE_NUMBER": args.app_number} if args.app_number else {}),
        "ACS_AUTO_CONFERENCE_ENABLED": "false",
        "DYNAMICS_OUTBOUND_ENABLED": "false",
        "DYNAMICS_NATIVE_MEDIA_ENABLED": "true",
        "DYNAMICS_NATIVE_MEDIA_TRANSPORT": "voip",
        "DYNAMICS_NATIVE_MEDIA_FAST_START": str(args.fast_start).lower(),
        "DYNAMICS_NATIVE_MEDIA_WORKSTREAM_ID": guid(args.workstream_id),
        "DYNAMICS_NATIVE_MEDIA_ADDITIONAL_WORKSTREAM_IDS": ",".join(workstreams[1:]),
        "DYNAMICS_NATIVE_MEDIA_API_KEY": media_key,
        "DYNAMICS_NATIVE_MEDIA_ACS_CONNECTION_STRING": connection,
        "DYNAMICS_NATIVE_PROBE_RESOURCE_ID": resource,
        "DYNAMICS_NATIVE_PROBE_API_KEY": probe_key,
        "DYNAMICS_NATIVE_PROBE_EXPIRES_AT": expiry,
        "DYNAMICS_NATIVE_PROBE_PERMANENT": str(args.permanent).lower(),
    }))
    print(json.dumps({"private_runtime_env": str(output), "fast_start": args.fast_start, "applied_to_azure": False, "native_key_printed": False}))


def dump(document):
    return yaml.dump(
        document, Dumper=topics.CopilotDumper, sort_keys=False,
        allow_unicode=True, indent=2, line_break="\r\n",
    )


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


class Dataverse:
    def __init__(self, org, tenant):
        self.org = organization(org)
        self.tenant = guid(tenant)
        result = subprocess.run(
            ["az", "account", "get-access-token", "--tenant", self.tenant, "--resource", self.org, "-o", "json"],
            check=True, capture_output=True, text=True, timeout=45,
        )
        identity = json.loads(result.stdout)
        if identity.get("tenant") and identity["tenant"].lower() != self.tenant:
            raise ValueError("Azure CLI returned a token for another tenant.")
        self.headers = {
            "Authorization": "Bearer " + identity["accessToken"],
            "Accept": "application/json", "OData-Version": "4.0", "OData-MaxVersion": "4.0",
            "Prefer": 'odata.include-annotations="OData.Community.Display.V1.FormattedValue"',
        }

    def request(self, path, *, method="GET", body=None, headers=None, raw=False):
        request = urllib.request.Request(
            self.org + "/api/data/v9.2/" + path, method=method,
            data=body if isinstance(body, bytes) else json.dumps(body).encode() if body is not None else None,
            headers={**self.headers, **({"Content-Type": "application/json"} if body is not None else {}), **(headers or {})},
        )
        with urllib.request.urlopen(request, timeout=45) as response:
            content = response.read(8 * 1024 * 1024 + 1)
        if len(content) > 8 * 1024 * 1024:
            raise ValueError("Dataverse response exceeded the setup-tool size limit.")
        return content.decode() if raw else json.loads(content) if content.strip() else {}

    def get(self, path, query=None):
        return self.request(path + ("?" + urllib.parse.urlencode(query) if query else ""))

    def rows(self, table, query):
        path = table + "?" + urllib.parse.urlencode(query)
        rows = []
        for _ in range(20):
            document = self.request(path)
            rows.extend(document["value"])
            link = document.get("@odata.nextLink")
            if not link:
                return rows
            prefix = self.org + "/api/data/v9.2/"
            if not link.startswith(prefix):
                raise ValueError("Unexpected Dataverse pagination origin.")
            path = link[len(prefix):]
        raise ValueError("Inventory is too large; narrow the selected environment or query.")

    def patch_many(self, updates):
        batch, change = "batch_" + uuid4().hex, "changeset_" + uuid4().hex
        lines = [f"--{batch}", f"Content-Type: multipart/mixed; boundary={change}", ""]
        for index, update in enumerate(updates, 1):
            identifier = guid(update["id"])
            etag = update["etag"]
            if not re.fullmatch(r'W/"\d+"', etag):
                raise ValueError("Expected a native Dataverse ETag.")
            lines.extend([
                f"--{change}", "Content-Type: application/http", "Content-Transfer-Encoding: binary",
                f"Content-ID: {index}", "",
                f"PATCH {self.org}/api/data/v9.2/botcomponents({identifier}) HTTP/1.1",
                "Content-Type: application/json", f"If-Match: {etag}", "",
                json.dumps({"data": update["data"]}), "",
            ])
        lines.extend([f"--{change}--", f"--{batch}--", ""])
        response = self.request(
            "$batch", method="POST", body="\r\n".join(lines).encode(),
            headers={"Content-Type": f"multipart/mixed; boundary={batch}"}, raw=True,
        )
        codes = [int(code) for code in re.findall(r"HTTP/1\.1 (\d{3})", response)]
        if len(codes) != len(updates) or any(code not in {200, 204} for code in codes):
            raise RuntimeError(f"Atomic draft update was not confirmed (HTTP statuses {codes}); inspect before retrying.")


def components(api, bot_id, *, crm_case_context=False):
    names = ("Conversation Start", CONTROLLER, CONTEXT, VARIABLE, *((CASE_VARIABLE,) if crm_case_context else ()))
    query = {
        "$select": "botcomponentid,name,componenttype,schemaname,language,data",
        "$filter": f"_parentbotid_value eq {guid(bot_id)} and (" + " or ".join(f"name eq '{name}'" for name in names) + ")",
        "$top": "10",
    }
    rows = api.rows("botcomponents", query)
    by_name = {row["name"]: row for row in rows}
    if len(rows) != len(names) or set(by_name) != set(names):
        raise ValueError("Create exactly one copy of each required native topic/global, including CaseNumber when CRM case context is enabled.")
    for name in ("Conversation Start", CONTROLLER, CONTEXT):
        if by_name[name]["componenttype"] != 9:
            raise ValueError("Expected a native topic component.")
    return by_name


def validate_context_variable(record):
    definition = topics.load_topic(record["data"])
    if (
        record.get("name") != VARIABLE or record.get("componenttype") != GLOBAL_VARIABLE_COMPONENT
        or definition.get("kind") != "Variable" or definition.get("name") != VARIABLE
        or definition.get("scope") != "Conversation"
        or definition.get("isExternalInitializationAllowed") is not True
        or definition.get("isOutputToExternalCallers") is not False
    ):
        raise ValueError("msdyn_ConversationId must be a conversation-scoped, input-only external global variable.")
    return definition


def validate_case_variable(record):
    definition = topics.load_topic(record["data"])
    if (
        record.get("name") != CASE_VARIABLE or record.get("componenttype") != GLOBAL_VARIABLE_COMPONENT
        or definition.get("kind") != "Variable" or definition.get("name") != CASE_VARIABLE
        or definition.get("scope") != "Conversation" or definition.get("isOutputToExternalCallers") is not True
        or type(definition.get("isExternalInitializationAllowed")) is not bool
    ):
        raise ValueError("CaseNumber must be a conversation-scoped global variable with external output enabled.")
    return definition


def context_variable(args, api):
    if args.apply and args.confirm_organization != args.org_url:
        raise ValueError("--apply requires --confirm-organization matching --org-url.")
    if args.apply:
        if args.receipt_file is None:
            raise ValueError("Provide a new --receipt-file for the credential-free private rollback record.")
        args.receipt_file = private_path(args.receipt_file)
    bot_id = guid(args.bot_id)
    bot = api.get(f"bots({bot_id})", {"$select": "botid,schemaname,language,publishedon"})
    schema = bot.get("schemaname")
    if (
        bot.get("botid") != bot_id or not isinstance(schema, str)
        or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", schema) or bot.get("language") != 1033
    ):
        raise ValueError("Inspect the actual English-primary native agent schema before creating its context global.")
    schema += ".GlobalVariableComponent." + VARIABLE
    query = {
        "$select": "botcomponentid,name,componenttype,schemaname,language,_parentbotid_value,data",
        "$filter": f"_parentbotid_value eq {bot_id} and name eq '{VARIABLE}'", "$top": "2",
    }
    rows = api.rows("botcomponents", query)
    if len(rows) > 1:
        raise ValueError("The native context global is ambiguous; no component was changed.")
    if rows:
        row = rows[0]
        definition = validate_context_variable(row)
        if row.get("_parentbotid_value") != bot_id or row.get("schemaname") != schema:
            raise ValueError("The existing context global belongs to a different agent/schema; it was preserved.")
        print(json.dumps({
            "status": "already_configured", "component_id": row["botcomponentid"],
            "definition": {key: definition[key] for key in (
                "kind", "name", "scope", "isExternalInitializationAllowed", "isOutputToExternalCallers",
            )}, "published": False, "remote_changes": False,
        }))
        return
    definition = {
        "kind": "Variable", "name": VARIABLE, "scope": "Conversation",
        "isExternalInitializationAllowed": True, "isOutputToExternalCallers": False,
        "initializationTimeoutInMilliseconds": 0,
    }
    create_unused_global(args, api, bot_id, schema, definition, bot, query, validate_context_variable)


def case_context_variable(args, api):
    bot_id = guid(args.bot_id)
    query = {
        "$select": "botcomponentid,name,componenttype,schemaname,language,data",
        "$filter": f"_parentbotid_value eq {bot_id} and (name eq '{VARIABLE}' or name eq '{CASE_VARIABLE}')",
        "$top": "5",
    }
    rows = api.rows("botcomponents", query)
    standards = [row for row in rows if row.get("name") == VARIABLE]
    cases = [row for row in rows if row.get("name") == CASE_VARIABLE]
    if len(standards) != 1 or len(cases) > 1 or len(rows) != len(standards) + len(cases):
        raise ValueError("The native global-variable names or definitions are ambiguous; no component was changed.")
    standard = standards[0]
    validate_context_variable(standard)
    if not re.fullmatch(r"[A-Za-z0-9_]+\.GlobalVariableComponent\.msdyn_ConversationId", standard.get("schemaname", "")):
        raise ValueError("Inspect the standard native global-variable schema before creating CaseNumber.")
    if cases:
        definition = validate_case_variable(cases[0])
        print(json.dumps({
            "status": "already_configured", "component_id": cases[0]["botcomponentid"],
            "definition": {key: definition[key] for key in (
                "kind", "name", "scope", "isExternalInitializationAllowed", "isOutputToExternalCallers",
            )},
            "published": False, "remote_changes": False,
        }))
        return
    definition = {
        "kind": "Variable", "name": CASE_VARIABLE, "scope": "Conversation",
        "isExternalInitializationAllowed": False, "isOutputToExternalCallers": True,
    }
    schema = standard["schemaname"].rsplit(".", 1)[0] + "." + CASE_VARIABLE
    create_unused_global(args, api, bot_id, schema, definition, standard, query, validate_case_variable)


def create_unused_global(args, api, bot_id, schema, definition, source, query, validate):
    name = definition["name"]
    if not args.apply:
        print(json.dumps({
            "status": "ready_to_create_unused_global", "componenttype": GLOBAL_VARIABLE_COMPONENT,
            "schemaname": schema, "definition": definition, "published": False, "remote_changes": False,
        }))
        return
    if args.receipt_file is None:
        raise ValueError("Provide a new --receipt-file for the credential-free private rollback record.")
    identifier = str(uuid4())
    before_bot = api.get(f"bots({bot_id})", {"$select": "publishedon"})
    body = {
        "botcomponentid": identifier, "name": name, "componenttype": GLOBAL_VARIABLE_COMPONENT,
        "schemaname": schema, "parentbotid@odata.bind": f"/bots({bot_id})", "data": dump(definition),
    }
    if source.get("language") is not None:
        body["language"] = source["language"]
    receipt = {
        "version": 1, "org_url": api.org, "tenant_id": api.tenant, "bot_id": bot_id,
        "component_id": identifier, "componenttype": GLOBAL_VARIABLE_COMPONENT,
        "schemaname": schema, "definition": definition, "previous_component": None,
        "publishedon_before": before_bot.get("publishedon"), "status": "creation_pending",
        "rollback": {
            "method": "DELETE", "path": f"botcomponents({identifier})",
            "precondition": "Only delete this newly created component if its parent, schema and definition are unchanged and it is not published or referenced.",
        },
    }
    receipt_path = Path(args.receipt_file)
    write_private(receipt_path, json.dumps(receipt, indent=2) + "\n")
    api.request("botcomponents", method="POST", body=body)
    saved = api.get(f"botcomponents({identifier})", {
        "$select": "botcomponentid,name,componenttype,schemaname,_parentbotid_value,data",
    })
    if (
        saved.get("botcomponentid") != identifier or saved.get("_parentbotid_value") != bot_id or saved.get("schemaname") != schema
        or validate(saved) != definition
    ):
        raise RuntimeError(f"{name} readback differed from its planned definition; inspect the rollback receipt before proceeding.")
    current = api.rows("botcomponents", query)
    current_variables = [row for row in current if row.get("name") == name]
    if len(current_variables) != 1 or current_variables[0].get("botcomponentid") != identifier:
        raise RuntimeError(f"A {name} component was created concurrently; reconcile the private rollback receipt before publishing.")
    after_bot = api.get(f"bots({bot_id})", {"$select": "publishedon"})
    receipt.update({
        "status": "unused_global_created", "etag": saved.get("@odata.etag"),
        "publishedon_after": after_bot.get("publishedon"),
    })
    receipt["rollback"]["if_match"] = saved.get("@odata.etag")
    with receipt_path.open("w") as output:
        json.dump(receipt, output, indent=2)
        output.write("\n")
    if after_bot.get("publishedon") != before_bot.get("publishedon"):
        raise RuntimeError("The bot was published concurrently; inspect the receipt before further changes.")
    print(json.dumps({
        "status": receipt["status"], "component_id": identifier, "definition": definition,
        "receipt_file": str(receipt_path), "published": False, "existing_components_modified": False,
    }))


def agent_baseline_plan(bot, org, tenant, bot_id):
    if (
        bot.get("botid") != bot_id or type(bot.get("language")) is not int or bot["language"] != 1033
        or bot.get("ismanaged") is not False or type(bot.get("statecode")) is not int or bot["statecode"] != 0
        or bot.get("supportedlanguages") not in (None, "", "1025")
        or type(bot.get("authenticationmode")) is not int or bot["authenticationmode"] not in (1, 2)
        or type(bot.get("authenticationtrigger")) is not int or bot["authenticationtrigger"] not in (0, 1)
        or type(bot.get("accesscontrolpolicy")) is not int or bot["accesscontrolpolicy"] not in (0, 2)
        or not isinstance(bot.get("@odata.etag"), str)
        or not re.fullmatch(r'W/"\d+"', bot.get("@odata.etag", ""))
    ):
        raise ValueError("Select the exact active, unmanaged English-primary native agent without unrelated language/auth configuration.")
    configuration = json.loads(bot["configuration"])
    if not isinstance(configuration, dict):
        raise ValueError("The native agent configuration must be a structured object.")
    settings, ai, recognizer = (configuration.get(key) for key in ("settings", "aISettings", "recognizer"))
    ai_fields = ("useModelKnowledge", "isFileAnalysisEnabled", "isSemanticSearchEnabled")
    # Saving Basic voice drops the explicit PVARecognizer object; keep it absent rather than re-adding it.
    voice_saved_without_recognizer = recognizer is None and configuration.get("isTelephonyEnabled") is True
    if (
        not isinstance(settings, dict) or type(settings.get("GenerativeActionsEnabled")) is not bool
        or not isinstance(ai, dict) or any(type(ai.get(key)) is not bool for key in ai_fields)
        or not voice_saved_without_recognizer and (
            not isinstance(recognizer, dict) or set(recognizer) != {"$kind"}
            or recognizer["$kind"] not in ("PVARecognizer", "GenerativeAIRecognizer")
        )
    ):
        raise ValueError("Inspect the actual supported agent configuration before changing its native baseline.")
    settings["GenerativeActionsEnabled"] = False
    for key in ai_fields:
        ai[key] = False
    if not voice_saved_without_recognizer:
        configuration["recognizer"] = {"$kind": "PVARecognizer"}
    desired = {
        "configuration": json.dumps(configuration, ensure_ascii=False, separators=(",", ":")),
        "authenticationmode": 1, "authenticationtrigger": 0, "accesscontrolpolicy": 0,
        "supportedlanguages": "1025",
    }
    return {
        "version": 1, "kind": "native-agent-baseline", "org_url": org, "tenant_id": tenant,
        "bot_id": bot_id, "before": bot, "desired": desired, "if_match": bot["@odata.etag"],
        "changes": [
            key for key, value in desired.items()
            if (json.loads(bot[key]) != configuration if key == "configuration" else bot.get(key) != value)
        ],
    }


def agent_baseline_state(bot):
    ignored = {
        "@odata.context", "@odata.etag", "modifiedon", "versionnumber",
        "_modifiedby_value", "_modifiedonbehalfby_value",
    }
    result = {
        key: value for key, value in bot.items()
        if key not in ignored and key.split("@", 1)[0] not in ignored
        and not key.endswith("@OData.Community.Display.V1.FormattedValue")
    }
    result["configuration"] = json.loads(result["configuration"])
    return result


def agent_baseline(args, api):
    if args.apply and args.confirm_organization != args.org_url:
        raise ValueError("--apply requires --confirm-organization matching --org-url.")
    if not args.apply and args.confirm_organization is not None:
        raise ValueError("Organization confirmation is valid only with --apply.")
    output = private_path(args.output_dir)
    bot_id = guid(args.bot_id)
    path = f"bots({bot_id})"
    if not output.exists():
        if args.apply:
            raise ValueError("Preview the native agent baseline into a new private output directory before --apply.")
        bot = api.get(path)
        plan = agent_baseline_plan(bot, args.org_url, args.tenant_id, bot_id)
        output.mkdir(mode=0o700)
        write_private(output / "plan.json", json.dumps(plan, ensure_ascii=False, indent=2))
        print(json.dumps({
            "status": "planned", "plan_only": True, "private_plan": str(output / "plan.json"),
            "changed_fields": plan["changes"], "anonymous_telephone_access": True,
            "arabic_language_metadata_only": True, "voice_channel_ready": False,
            "native_changes": 0, "calls_placed": 0, "published": False,
        }))
        return
    plan = json.loads(read_private(output / "plan.json"))
    if plan != agent_baseline_plan(plan["before"], args.org_url, args.tenant_id, bot_id):
        raise ValueError("The native agent baseline plan or target differs; no change was submitted.")
    current = api.get(path)
    if not args.apply:
        print(json.dumps({
            "status": "saved_plan", "plan_only": True, "private_plan": str(output / "plan.json"),
            "changed_fields": plan["changes"], "native_changes": 0, "published": False, "calls_placed": 0,
        }))
        return
    plan_hash = digest(json.dumps(plan, sort_keys=True, separators=(",", ":")))
    intent_path = output / "intent.json"
    reconciled = intent_path.exists()
    if reconciled:
        intent = json.loads(read_private(intent_path))
        if intent.get("plan_hash") != plan_hash:
            raise ValueError("The pending native agent intent differs from the saved plan; no retry was submitted.")
    else:
        if current != plan["before"]:
            raise ValueError("The agent changed since preview; preserve its current state and review a new plan.")
        if plan["changes"]:
            write_private(intent_path, json.dumps({
                "plan_hash": plan_hash, "path": path, "if_match": plan["if_match"],
                "desired": plan["desired"], "started_at": datetime.now(timezone.utc).isoformat(),
            }, indent=2))
            api.request(path, method="PATCH", body=plan["desired"], headers={"If-Match": plan["if_match"]})
            current = api.get(path)
    expected = {**plan["before"], **plan["desired"]}
    if agent_baseline_state(current) != agent_baseline_state(expected):
        raise RuntimeError("The exact native agent baseline is not confirmed; preserve the original intent and do not retry its write.")
    receipt = {
        "status": "reconciled" if reconciled else "verified", "configuration_verified": True,
        "plan_hash": plan_hash, "bot_id": bot_id, "after": current,
        "native_changes": int(bool(plan["changes"]) and not reconciled),
        "publication_unchanged": True, "published": False, "calls_placed": 0,
        "anonymous_telephone_metadata_verified": True, "arabic_language_metadata_verified": True,
        "voice_channel_ready": False, "translations_or_audio_acceptance_claimed": False,
    }
    receipt_path = output / ("after-" + uuid4().hex + ".json")
    write_private(receipt_path, json.dumps(receipt, ensure_ascii=False, indent=2))
    print(json.dumps({key: value for key, value in receipt.items() if key != "after"}))


def caller_entry(existing, schema, callers):
    if not isinstance(schema, str) or not re.fullmatch(topics.CONTROLLER_SCHEMA, schema):
        raise ValueError("Select the actual NativeSpeechTurnPilot schema from this bot.")
    if not callers or len(callers) > 3 or len(set(callers)) != len(callers):
        raise ValueError("Supply one to three distinct, explicitly allowed test callers.")
    if any(not re.fullmatch(r"\+[1-9]\d{6,14}", caller) for caller in callers):
        raise ValueError("Test callers must be E.164 strings including '+'.")
    document = copy.deepcopy(existing)
    begin = document.get("beginDialog", {})
    actions = begin.get("actions")
    if document.get("kind") != "AdaptiveDialog" or begin.get("kind") != "OnConversationStart" or not isinstance(actions, list) or not actions:
        raise ValueError("Expected a nonempty native Conversation Start topic.")
    if any(action.get("id") == "native_voice_pilot_entry" for action in actions):
        if len(actions) != 1 or not isinstance(actions[0].get("elseActions"), list) or not actions[0]["elseActions"]:
            raise ValueError("The previous generated caller gate has an unexpected shape.")
        actions = actions[0]["elseActions"]
    elif any(action.get("id", "").startswith(("native_pilot_", "native_transcript_", "native_contract_")) for action in actions):
        raise ValueError("An experimental caller gate exists. Preserve or remove it deliberately before using the new setup kit.")
    begin["actions"] = [{
        "kind": "ConditionGroup", "id": "native_voice_pilot_entry",
        "conditions": [{
            "id": "native_voice_allowed_caller",
            "condition": "=" + " || ".join(f"System.Activity.From.Name = {json.dumps(caller)}" for caller in callers),
            "actions": [{"kind": "BeginDialog", "id": "native_voice_enter", "dialog": schema}],
        }],
        "elseActions": actions,
    }]
    return document


def bundle_documents(records, origin, key, callers, *, replace_controller=False, startup_notice=False, crm_case_context=False):
    if type(crm_case_context) is not bool:
        raise ValueError("The CRM case-context option must be a boolean.")
    if crm_case_context:
        if CASE_VARIABLE not in records:
            raise ValueError("Create the unused CaseNumber output global with case-context-variable before exporting this bundle.")
        validate_case_variable(records[CASE_VARIABLE])
    context = topics.load_topic(records[CONTEXT]["data"])
    actions = context.get("beginDialog", {}).get("actions", [])
    if len(actions) != 1:
        raise ValueError("NativeMediaContext must contain only the saved external conversation-ID initializer.")
    initializer = actions[0]
    validate_context_variable(records[VARIABLE])
    existing_external_global = isinstance(initializer, dict) and initializer.get("variable") == f"Global.{VARIABLE}"
    topics.validate_initializer(initializer, existing_external_global=existing_external_global)
    original = topics.load_topic(records[CONTROLLER]["data"])
    if "native_supervision_hold" in records[CONTROLLER]["data"]:
        raise ValueError("This controller has the opt-in supervision contract. Disable supervision and use its owned rollback/rebase receipt before replacing it.")
    if not crm_case_context and "native_set_case_number" in records[CONTROLLER]["data"]:
        raise ValueError("Preserve the existing CaseNumber integration with --crm-case-context.")
    if crm_case_context and not startup_notice and re.search(
        r"\b(?:play_startup_notice|native_startup_notice)\s*:\s*true\b", records[CONTROLLER]["data"],
    ):
        raise ValueError("Preserve the configured recording notice with --startup-notice.")
    controller = topics.render_speech_turn_pilot(
        topics.load_topic(topics.render_topic(
            initializer, voice_origin(origin), key, existing_external_global=existing_external_global,
        )),
        startup_notice=startup_notice, crm_case_context=crm_case_context,
    )
    if original.get("beginDialog", {}).get("actions") and topics.load_topic(controller) != original and not replace_controller:
        raise ValueError("The controller already has actions. Reconcile its edits, then explicitly use --replace-controller to propose an update.")
    entry = caller_entry(
        topics.load_topic(records["Conversation Start"]["data"]), records[CONTROLLER]["schemaname"], callers,
    )
    return {
        "controller": controller,
        "context": topics.render_context_topic(initializer, existing_external_global=existing_external_global),
        "entry": dump(entry),
    }


def export_bundle(args, api):
    output = private_path(args.output_dir)
    if output.exists():
        raise ValueError("Use a new output directory; existing bundles are never overwritten.")
    crm_case_context = getattr(args, "crm_case_context", False)
    records = components(api, args.bot_id, crm_case_context=crm_case_context)
    bot = api.get(f"bots({guid(args.bot_id)})", {"$select": "name,language,supportedlanguages,publishedon"})
    if bot["language"] != 1033 or "1025" not in (bot.get("supportedlanguages") or "").split(","):
        raise ValueError("The documented voice agent needs English primary and Arabic secondary before exporting.")
    key = secret_value(args.secrets_file, "DYNAMICS_NATIVE_MEDIA_API_KEY")
    documents = bundle_documents(
        records, args.voice_origin, key, args.test_caller, replace_controller=args.replace_controller,
        startup_notice=args.startup_notice, crm_case_context=crm_case_context,
    )
    output.mkdir(mode=0o700)
    updates = []
    for kind, name in (("controller", CONTROLLER), ("context", CONTEXT), ("entry", "Conversation Start")):
        text = documents[kind]
        write_private(output / BUNDLE_FILES[kind], text)
        updates.append({
            "name": name, "id": records[name]["botcomponentid"], "etag": records[name]["@odata.etag"],
            "file": BUNDLE_FILES[kind], "sha256": digest(text),
        })
    write_private(output / "before.json", json.dumps({"records": records, "publishedon": bot.get("publishedon")}, indent=2) + "\n")
    write_private(output / "bundle.json", json.dumps({
        "version": 1, "org_url": api.org, "tenant_id": api.tenant, "bot_id": guid(args.bot_id),
        "updates": updates, "crm_case_context": crm_case_context,
    }, indent=2) + "\n")
    print(json.dumps({"bundle": str(output), "topics": [row["name"] for row in updates], "remote_changes": False, "contains_private_control_key": True}))


def load_bundle(directory):
    directory = Path(directory).expanduser().resolve()
    manifest = json.loads(read_private(directory / "bundle.json"))
    before = json.loads(read_private(directory / "before.json"))
    if manifest.get("version") != 1 or len(manifest.get("updates", [])) != 3:
        raise ValueError("Unsupported deployment bundle.")
    if type(manifest.get("crm_case_context", False)) is not bool:
        raise ValueError("The bundle CRM case-context flag must be a boolean.")
    names = {CONTROLLER, CONTEXT, "Conversation Start"}
    if {row["name"] for row in manifest["updates"]} != names:
        raise ValueError("The bundle must target exactly the three documented topics.")
    updates = []
    for row in manifest["updates"]:
        if row["file"] not in BUNDLE_FILES.values():
            raise ValueError("Unexpected deployment bundle filename.")
        text = read_private(directory / row["file"])
        if digest(text) != row["sha256"]:
            raise ValueError("Generated YAML changed after export. Create a fresh reviewed bundle instead.")
        guid(row["id"])
        updates.append({**row, "data": text})
    controller = next(row["data"] for row in updates if row["name"] == CONTROLLER)
    if ("native_set_case_number" in controller) != manifest.get("crm_case_context", False):
        raise ValueError("The bundle CRM case-context flag does not match its controller.")
    return manifest, before, updates


def stage_bundle(args):
    manifest, before, updates = load_bundle(args.bundle_dir)
    api = Dataverse(manifest["org_url"], manifest["tenant_id"])
    case_context = manifest.get("crm_case_context", False)
    current_records = components(api, manifest["bot_id"], crm_case_context=case_context)
    current_bot = api.get(f"bots({guid(manifest['bot_id'])})", {"$select": "publishedon"})
    plan = []
    for row in updates:
        current = current_records[row["name"]]
        if current["botcomponentid"] != row["id"]:
            raise ValueError("The target component identity changed; regenerate the bundle.")
        if topics.load_topic(current["data"]) == topics.load_topic(row["data"]):
            continue
        if current["@odata.etag"] != row["etag"] or current["data"] != before["records"][row["name"]]["data"]:
            raise ValueError("A draft changed since export. Do not overwrite the user's work; export a new bundle.")
        plan.append(row)
    if current_records[VARIABLE]["data"] != before["records"][VARIABLE]["data"]:
        raise ValueError("The external context variable changed since export.")
    if case_context:
        validate_case_variable(current_records[CASE_VARIABLE])
        if (
            current_records[CASE_VARIABLE]["botcomponentid"] != before["records"][CASE_VARIABLE]["botcomponentid"]
            or current_records[CASE_VARIABLE]["data"] != before["records"][CASE_VARIABLE]["data"]
        ):
            raise ValueError("The CaseNumber output global changed since export.")
        topics.validate_case_handoff(topics.load_topic(next(row["data"] for row in updates if row["name"] == CONTROLLER)))
    if not plan:
        print(json.dumps({"status": "already_staged", "published": False}))
        return
    if current_bot.get("publishedon") != before.get("publishedon"):
        raise ValueError("The agent was published after export; regenerate the bundle against the new baseline.")
    print(json.dumps({"draft_updates": [row["name"] for row in plan], "apply": args.apply, "publishes": False, "places_calls": False}))
    if not args.apply:
        return
    api.patch_many(plan)
    for row in plan:
        saved = api.get(f"botcomponents({row['id']})", {"$select": "data"})
        if topics.load_topic(saved["data"]) != topics.load_topic(row["data"]):
            raise RuntimeError("Saved draft verification failed; do not publish.")
    if api.get(f"bots({manifest['bot_id']})", {"$select": "publishedon"}).get("publishedon") != current_bot.get("publishedon"):
        raise RuntimeError("The agent was published concurrently; inspect its active version.")
    print(json.dumps({"status": "draft_staged", "next": "Refresh/reopen the topics, run Topic checker, then publish the agent once.", "published": False}))


def optional(api, path, query=None):
    try:
        return api.get(path, query)
    except urllib.error.HTTPError as error:
        if error.code == 404:
            error.close()
            return None
        raise


DISCOVERY_CATALOGS = (
    ("apps", "appmodules", "appmoduleid,name,uniquename,ismanaged",
     "contains(name,'Customer') or contains(name,'Contact') or contains(name,'Copilot')"),
    ("bots", "bots", "botid,name,schemaname,language,supportedlanguages,publishedon,ismanaged", None),
    ("workstreams", "msdyn_liveworkstreams",
     "msdyn_liveworkstreamid,msdyn_name,msdyn_direction,msdyn_streamsource,_msdyn_defaultqueue_value,"
     "_msdyn_bot_user_value,ismanaged,msdyn_isdefault", "statecode eq 0"),
    ("queues", "queues", "queueid,name,_businessunitid_value,msdyn_isdefaultqueue,msdyn_isomnichannelqueue",
     "statecode eq 0"),
)


def record_source(row, actor_id):
    """Classify a discovered record; only an exact ID confirmed by its creator may be used."""
    if row.get("ismanaged") is True:
        source, reason = "platform", "managed solution component"
    elif row.get("msdyn_isdefault") is True or row.get("msdyn_isdefaultqueue") is True:
        source, reason = "platform", "platform default record"
    elif row.get("msdyn_isomnichannelqueue") is False:
        source, reason = "not-routable", "not an Omnichannel routing queue"
    else:
        source, reason = "environment", "unmanaged record created in this environment"
    creator = row.get("_createdby_value")
    return {
        "source": source, "reason": reason,
        "owned": None if source == "environment" else False,
        "created_by_current_actor": bool(actor_id and creator and creator.lower() == actor_id.lower()),
    }


def discover(args, api):
    actor = api.get("WhoAmI")
    result = {
        "actor": actor,
        "scope": (
            "Catalogs show at most 10 newest records each. Use a record's Dynamics URL for an older ID. "
            "Topic metadata is restricted to the four required components."
        ),
        "ownership": (
            "Records with owned=false (platform or not routable) are never configured or adopted. Environment "
            "records may be this deployment's shells; use only an exact ID confirmed by the person who created it, "
            "never a display name."
        ),
    }
    for label, table, fields, criteria in DISCOVERY_CATALOGS:
        query = {"$select": fields + ",createdon,_createdby_value", "$top": "10", "$orderby": "createdon desc"}
        if criteria:
            query["$filter"] = criteria
        rows = api.rows(table, query)
        result[label] = [{**row, "classification": record_source(row, actor.get("UserId"))} for row in rows]
    if args.bot_id:
        names = " or ".join(f"name eq '{name}'" for name in ("Conversation Start", CONTROLLER, CONTEXT, VARIABLE))
        result["topic_metadata"] = api.rows("botcomponents", {
            "$select": "botcomponentid,name,schemaname,componenttype",
            "$filter": f"_parentbotid_value eq {guid(args.bot_id)} and ({names})", "$top": "10",
        })
    print(json.dumps(result, indent=2))


def entity_metadata(api, logical_name, required):
    entity = optional(api, f"EntityDefinitions(LogicalName='{logical_name}')", {
        "$select": "LogicalName,EntitySetName,PrimaryIdAttribute",
    })
    if entity is None:
        raise ValueError(f"Dataverse has no {logical_name} table. Provision voice and connect the native ACS resource first.")
    attributes = {
        row.get("LogicalName") for row in api.get(
            f"EntityDefinitions(LogicalName='{logical_name}')/Attributes", {"$select": "LogicalName,AttributeType"},
        ).get("value", [])
    }
    missing = sorted(set(required) - attributes)
    if missing:
        raise ValueError(
            f"{logical_name} has no {', '.join(missing)} column. The Dynamics connection schema differs from the "
            "one this check knows, so it can't confirm the connection. Ask the owner to confirm Manage telephony instead.",
        )
    set_name, key = entity.get("EntitySetName"), entity.get("PrimaryIdAttribute")
    if not all(isinstance(value, str) and re.fullmatch(r"[a-z][a-z0-9_]*", value) for value in (set_name, key)):
        raise ValueError(f"Unexpected {logical_name} table metadata.")
    return set_name, key, attributes


def comparable(value):
    if not isinstance(value, str):
        return None
    value = value.strip()
    try:
        return str(UUID(value.strip("{}")))
    except ValueError:
        return value.lower()


def connection(args, api):
    resource, event_app, tenant = acs_resource_id(args.native_acs_resource_id), guid(args.event_grid_app_id), guid(args.tenant_id)
    setting_set, setting_key, setting_columns = entity_metadata(api, PROVIDER_TABLE, ("msdyn_istrial",))
    entry_set, entry_key, entry_columns = entity_metadata(api, PROVIDER_ENTRY_TABLE, ("msdyn_key", "msdyn_name", "msdyn_value"))
    lookups = {
        row.get("ReferencingAttribute") for row in api.get(
            f"EntityDefinitions(LogicalName='{PROVIDER_ENTRY_TABLE}')/ManyToOneRelationships",
            {"$select": "ReferencingAttribute,ReferencedEntity", "$filter": f"ReferencedEntity eq '{PROVIDER_TABLE}'"},
        ).get("value", []) if row.get("ReferencedEntity") == PROVIDER_TABLE
    }
    lookup = lookups.pop() if len(lookups) == 1 else None
    if not isinstance(lookup, str) or lookup not in entry_columns:
        raise ValueError(f"{PROVIDER_ENTRY_TABLE} has no single lookup to {PROVIDER_TABLE}; the connection can't be read back.")
    state = ["statecode"] if "statecode" in setting_columns else []
    settings = api.rows(setting_set, {"$select": ",".join([setting_key, "msdyn_istrial", *state])})
    entries = api.rows(entry_set, {"$select": ",".join([
        entry_key, "msdyn_key", "msdyn_name", "msdyn_value", f"_{lookup}_value",
        *(["statecode"] if "statecode" in entry_columns else []),
    ])})
    grouped = {}
    for entry in entries:
        parent = comparable(entry.get(f"_{lookup}_value"))
        if parent and entry.get("statecode", 0) == 0:
            grouped.setdefault(parent, []).append(entry)
    expected = {
        "acs_resource_id": comparable(resource), "event_grid_app_id": event_app, "event_grid_app_tenant_id": tenant,
    }

    def matches(setting):
        rows = grouped.get(comparable(setting.get(setting_key)), [])
        return {
            label: sorted({row.get("msdyn_key") or row.get("msdyn_name") or "(unnamed)"
                           for row in rows if comparable(row.get("msdyn_value")) == value})
            for label, value in expected.items()
        }, sorted({row.get("msdyn_key") or row.get("msdyn_name") or "(unnamed)" for row in rows})

    active = [row for row in settings if row.get("statecode", 0) == 0]
    candidates = [(row, *matches(row)) for row in active]
    connected = [row for row in candidates if row[1]["acs_resource_id"]]
    if len(connected) != 1:
        raise ValueError(
            ("No" if not connected else "More than one") + " active Dynamics communication provider record references "
            f"the native ACS resource ID ({len(active)} active provider records read). Connect only the dedicated native "
            "ACS resource with Use existing resource, never the application ACS, then read back again.",
        )
    setting, found, keys = connected[0]
    missing = [label for label in ("event_grid_app_id", "event_grid_app_tenant_id") if not found[label]]
    if missing:
        raise ValueError(
            f"The connected provider record doesn't hold the expected {' and '.join(missing)} "
            f"(entry keys present: {', '.join(keys) or 'none'}). Reconnect with the delivery app's IDs only after the owner reviews this readback.",
        )
    if setting.get("msdyn_istrial") is not False:
        raise ValueError("Dynamics still marks this connection as the voice trial (msdyn_istrial is not false). End the trial, then read back again.")
    print(json.dumps({
        "status": "connected", "provider_setting_id": setting.get(setting_key), "is_trial": False,
        "matched_entry_keys": found,
        "other_active_provider_records": [
            {"id": row.get(setting_key), "is_trial": row.get("msdyn_istrial")} for row, *_ in candidates if row is not setting
        ],
        "values_printed": False, "remote_changes": 0,
    }, indent=2))


def startup_audio_hash(value):
    return digest(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False))


def startup_audio_validate(snapshot, workstream_id):
    if not isinstance(snapshot, dict) or set(snapshot) != {"workstream", "voice", "languages", "music"}:
        raise ValueError("Incomplete native startup-audio snapshot.")
    workstream, voice = snapshot["workstream"], snapshot["voice"]
    if not isinstance(workstream, dict) or not isinstance(voice, dict):
        raise ValueError("Incomplete native workstream or voice-settings row.")
    if (
        guid(workstream.get("msdyn_liveworkstreamid")) != workstream_id
        or any(type(workstream.get(key)) is not int or workstream[key] != value for key, value in (
            ("statecode", 0), ("msdyn_direction", 0), ("msdyn_streamsource", 192440000),
        ))
    ):
        raise ValueError("Select the exact active native inbound voice workstream.")
    guid(workstream.get("_msdyn_defaultqueue_value"))
    guid(workstream.get("_msdyn_bot_user_value"))
    voice_id = guid(voice.get("msdyn_ocvoicechannelsettingid"))
    if (
        guid(voice.get("_msdyn_liveworkstreamid_value")) != workstream_id
        or type(voice.get("statecode")) is not int or voice["statecode"] != 0
    ):
        raise ValueError("The active native voice-settings row must belong to the selected workstream.")
    for field in (*STARTUP_AUDIO_FLAGS, "msdyn_recordingenabled", "msdyn_requestuserconsentforrecording"):
        if type(voice.get(field)) is not bool:
            raise ValueError("The native voice snapshot must include boolean startup, recording and consent settings.")
    languages, music = snapshot["languages"], snapshot["music"]
    if (
        not isinstance(languages, list) or not languages or not isinstance(music, dict)
        or not all(isinstance(row, dict) for row in (*languages, *music.values()))
    ):
        raise ValueError("Read the existing native per-language wait/hold music assignments before proceeding.")
    language_ids, music_ids = set(), set()
    for language in languages:
        identifier = guid(language.get("msdyn_ocvoicechannellanguagesettingid"))
        if identifier in language_ids or guid(language.get("_msdyn_ocvoicechannelsettingid_value")) != voice_id:
            raise ValueError("The native language rows are ambiguous or belong to another voice-settings row.")
        language_ids.add(identifier)
        guid(language.get("_msdyn_languageid_value"))
        for field in ("_msdyn_waitmusicid_value", "_msdyn_holdmusicid_value"):
            if field not in language:
                raise ValueError("The native language snapshot omitted a wait/hold music assignment.")
            if language[field] is not None:
                music_ids.add(guid(language[field]))
    if set(music) != music_ids or any(guid(row.get("msdyn_ocphonemusicid")) != key for key, row in music.items()):
        raise ValueError("The native music records do not match the saved per-language assignments.")
    for row in (workstream, voice, *languages, *music.values()):
        if not isinstance(row.get("@odata.etag"), str) or not re.fullmatch(r'W/"\d+"', row["@odata.etag"]):
            raise ValueError("The native startup-audio snapshot requires native Dataverse ETags.")


def startup_audio_snapshot(api, workstream_id):
    workstream = api.get(f"msdyn_liveworkstreams({workstream_id})")
    voices = api.rows("msdyn_ocvoicechannelsettings", {
        "$filter": f"_msdyn_liveworkstreamid_value eq {workstream_id}", "$top": "2",
    })
    if not isinstance(voices, list) or len(voices) != 1 or not isinstance(voices[0], dict):
        raise ValueError("Select a workstream with exactly one native voice-settings row.")
    voice = voices[0]
    voice_id = guid(voice.get("msdyn_ocvoicechannelsettingid"))
    if guid(voice.get("_msdyn_liveworkstreamid_value")) != workstream_id:
        raise ValueError("The native voice-settings row belongs to another workstream.")
    languages = api.rows("msdyn_ocvoicechannellanguagesettings", {
        "$filter": f"_msdyn_ocvoicechannelsettingid_value eq {voice_id}",
    })
    if not isinstance(languages, list) or not all(isinstance(row, dict) for row in languages):
        raise ValueError("Incomplete native per-language settings.")
    music_ids = {
        guid(language[field]) for language in languages
        for field in ("_msdyn_waitmusicid_value", "_msdyn_holdmusicid_value") if language.get(field) is not None
    }
    snapshot = {
        "workstream": workstream, "voice": voice,
        "languages": sorted(languages, key=lambda row: guid(row.get("msdyn_ocvoicechannellanguagesettingid"))),
        "music": {key: api.get(f"msdyn_ocphonemusics({key})") for key in sorted(music_ids)},
    }
    startup_audio_validate(snapshot, workstream_id)
    return snapshot


def startup_audio_state(snapshot):
    def fields(row, *, patched=False):
        # Only the patched row's server-assigned modification audit data may change.
        ignored = {"@odata.context"}
        if patched:
            ignored |= {"@odata.etag", "modifiedon", "versionnumber", "_modifiedby_value", "_modifiedonbehalfby_value"}
        return {
            key: value for key, value in row.items()
            if key not in ignored and key.split("@", 1)[0] not in ignored
            and not key.endswith("@OData.Community.Display.V1.FormattedValue")
        }

    return {
        "workstream": fields(snapshot["workstream"]), "voice": fields(snapshot["voice"], patched=True),
        "languages": [fields(row) for row in snapshot["languages"]],
        "music": {key: fields(row) for key, row in snapshot["music"].items()},
    }


def startup_audio_plan(snapshot, org, tenant, workstream_id, created_at):
    startup_audio_validate(snapshot, workstream_id)
    try:
        timestamp = datetime.fromisoformat(created_at.replace("Z", "+00:00")) if isinstance(created_at, str) else None
    except ValueError:
        timestamp = None
    if timestamp is None or timestamp.utcoffset() != timedelta(0):
        raise ValueError("The startup-audio plan needs a UTC creation timestamp.")
    return {
        "version": 1, "kind": "native-startup-audio", "org_url": org, "tenant_id": tenant,
        "workstream_id": workstream_id, "created_at": created_at, "before": snapshot,
        "path": f"msdyn_ocvoicechannelsettings({guid(snapshot['voice']['msdyn_ocvoicechannelsettingid'])})",
        "if_match": snapshot["voice"]["@odata.etag"], "desired": dict(STARTUP_AUDIO_FLAGS),
        "changes": {
            key: {"before": snapshot["voice"][key], "after": value}
            for key, value in STARTUP_AUDIO_FLAGS.items() if snapshot["voice"][key] is not value
        },
    }


def startup_audio_load(path):
    if path.is_symlink() or not path.is_file():
        raise ValueError("Use intact, owner-only startup-audio receipt files, not symlinks.")
    document = json.loads(read_private(path))
    if not isinstance(document, dict):
        raise ValueError("Invalid startup-audio receipt.")
    return document


def startup_audio_receipt(output, plan, intent, snapshot, *, status, plan_only, native_changes, verified):
    readback = datetime.now(timezone.utc)
    path = output / f"after-{uuid4().hex}.json"
    result = {
        "status": status, "plan_only": plan_only, "configuration_verified": verified,
        "native_changes": native_changes, "calls_placed": 0, "runtime_audio_verified": False,
        "planned_native_changes": int(bool(plan["changes"])),
        "readback_at": readback.isoformat().replace("+00:00", "Z"),
        "not_before_call_acceptance": (
            (readback + timedelta(seconds=STARTUP_AUDIO_PROPAGATION_SECONDS)).isoformat().replace("+00:00", "Z")
            if verified else None
        ),
        "plan_file": str(output / "plan.json"), "after_file": str(path),
        "plan_sha256": startup_audio_hash(plan),
    }
    write_private(path, json.dumps({
        "version": 1, **result, "intent_sha256": startup_audio_hash(intent) if intent else None,
        "after": snapshot,
    }, indent=2) + "\n")
    return result


def startup_audio(args, api):
    org, tenant, workstream_id = organization(args.org_url), guid(args.tenant_id), guid(args.workstream_id)
    if api.org != org or api.tenant != tenant:
        raise ValueError("The authenticated native client must match the selected organization and tenant.")
    if args.apply:
        if args.confirm_organization != org or args.confirm_notice_policy is not True:
            raise ValueError("--apply requires --confirm-organization matching --org-url and --confirm-notice-policy after human notice-policy review.")
    elif args.confirm_organization or args.confirm_notice_policy:
        raise ValueError("Notice-policy and organization confirmations are valid only with --apply.")
    output = private_path(args.output_dir)
    if args.apply and not (output / "plan.json").is_file():
        raise ValueError("Run startup-audio without --apply first, then review and reuse its private --output-dir.")
    if output.exists():
        if not output.is_dir() or (os.name != "nt" and output.stat().st_mode & 0o077):
            raise ValueError("The existing startup-audio output directory must be owner-only (chmod 700).")
        plan = startup_audio_load(output / "plan.json")
        expected = startup_audio_plan(plan.get("before"), org, tenant, workstream_id, plan.get("created_at"))
        if startup_audio_hash(plan) != startup_audio_hash(expected):
            raise ValueError("The immutable startup-audio plan does not match the selected environment or exact two-flag scope.")
        current = startup_audio_snapshot(api, workstream_id)
    else:
        current = startup_audio_snapshot(api, workstream_id)
        plan = startup_audio_plan(current, org, tenant, workstream_id, datetime.now(timezone.utc).isoformat())
        output.mkdir(mode=0o700)
        write_private(output / "plan.json", json.dumps(plan, indent=2) + "\n")
    intent_path = output / "intent.json"
    intent = startup_audio_load(intent_path) if intent_path.exists() or intent_path.is_symlink() else None
    expected_after = copy.deepcopy(plan["before"])
    expected_after["voice"].update(STARTUP_AUDIO_FLAGS)
    after_state = startup_audio_hash(startup_audio_state(expected_after))
    current_state = startup_audio_hash(startup_audio_state(current))
    matches_after = current_state == after_state
    matches_before = (
        current_state == startup_audio_hash(startup_audio_state(plan["before"]))
        and current["voice"]["@odata.etag"] == plan["if_match"]
    )
    if intent is not None:
        expected_intent = {
            "version": 1, "plan_sha256": startup_audio_hash(plan), "created_at": intent.get("created_at"),
            "method": "PATCH", "path": plan["path"], "if_match": plan["if_match"],
            "body": dict(STARTUP_AUDIO_FLAGS), "confirm_organization": org, "notice_policy_reviewed": True,
        }
        if not plan["changes"] or startup_audio_hash(intent) != startup_audio_hash(expected_intent):
            raise ValueError("The existing intent is not owned by this exact immutable startup-audio plan.")
        rejected = output / "rejected.json"
        if rejected.exists() or rejected.is_symlink():
            rejection = startup_audio_load(rejected)
            if rejection.get("plan_sha256") != startup_audio_hash(plan) or rejection.get("intent_sha256") != startup_audio_hash(intent):
                raise ValueError("The rejected-write receipt does not belong to this startup-audio intent.")
            status, verified = "rejected_intent_requires_recovery", False
        elif matches_after and current["voice"]["@odata.etag"] != plan["if_match"]:
            status, verified = "intent_readback_verified", True
        else:
            status, verified = ("pending_intent_matches_before" if matches_before else "pending_intent_drift"), False
        result = startup_audio_receipt(
            output, plan, intent, current, status=status, plan_only=not args.apply, native_changes=0, verified=verified,
        )
        if not verified:
            raise RuntimeError(f"Existing conditional-write intent requires recovery; no PATCH was retried. Inspect {result['after_file']} and preserve the original plan/intent.")
        print(json.dumps(result))
        return
    if (output / "rejected.json").exists() or (output / "rejected.json").is_symlink():
        raise ValueError("A rejected-write receipt has no matching intent; recover the original private directory.")
    if not matches_before:
        raise ValueError("Native fields or the voice ETag changed since planning; no write. Reconcile the original plan instead of rebasing its hashes.")
    if not plan["changes"] or not args.apply:
        result = startup_audio_receipt(
            output, plan, None, current, status="already_configured" if matches_after else "planned",
            plan_only=not args.apply, native_changes=0, verified=matches_after,
        )
        print(json.dumps(result))
        return
    if api.rows("msdyn_ocliveworkitems", {
        "$select": "msdyn_ocliveworkitemid", "$top": "1",
        "$filter": f"_msdyn_liveworkstreamid_value eq {workstream_id} and statuscode ne 4",
    }):
        raise ValueError("Finish all selected-workstream calls and Dynamics wrap-up before changing startup audio.")
    intent = {
        "version": 1, "plan_sha256": startup_audio_hash(plan), "created_at": datetime.now(timezone.utc).isoformat(),
        "method": "PATCH", "path": plan["path"], "if_match": plan["if_match"],
        "body": dict(STARTUP_AUDIO_FLAGS), "confirm_organization": org, "notice_policy_reviewed": True,
    }
    write_private(intent_path, json.dumps(intent, indent=2) + "\n")
    try:
        api.request(plan["path"], method="PATCH", body=dict(STARTUP_AUDIO_FLAGS), headers={"If-Match": plan["if_match"]})
    except urllib.error.HTTPError as error:
        if 400 <= error.code < 500 and error.code not in {408, 429}:
            write_private(output / "rejected.json", json.dumps({
                "version": 1, "plan_sha256": startup_audio_hash(plan), "intent_sha256": startup_audio_hash(intent),
                "http_status": error.code, "rejected_at": datetime.now(timezone.utc).isoformat(),
            }, indent=2) + "\n")
        raise RuntimeError(f"Conditional startup-audio write returned HTTP {error.code}; no retry. Preserve this directory and inspect it before recovery.") from None
    except (OSError, ValueError, RuntimeError):
        raise RuntimeError("Conditional startup-audio write is unconfirmed; no retry. Rerun with the same private directory for read-only intent reconciliation.") from None
    try:
        current = startup_audio_snapshot(api, workstream_id)
    except (OSError, ValueError, KeyError, RuntimeError):
        raise RuntimeError("Startup-audio readback is unconfirmed; no second PATCH. Rerun with the same private directory for read-only intent reconciliation.") from None
    verified = (
        startup_audio_hash(startup_audio_state(current)) == after_state
        and current["voice"]["@odata.etag"] != plan["if_match"]
    )
    result = startup_audio_receipt(
        output, plan, intent, current, status="configured" if verified else "readback_drift",
        plan_only=False, native_changes=1, verified=verified,
    )
    if not verified:
        raise RuntimeError(f"Startup-audio readback differed from the exact two-flag plan; no retry or rollback. Inspect {result['after_file']} before recovery.")
    print(json.dumps(result))


USER_FIELDS = "systemuserid,applicationid,azureactivedirectoryobjectid,_businessunitid_value,accessmode,isdisabled"


def reader_target(args, api):
    org, tenant = organization(args.org_url), guid(args.tenant_id)
    if api.org != org or api.tenant != tenant:
        raise ValueError("The authenticated native client must match the selected organization and tenant.")
    name = args.role_name
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 ._-]{2,99}", name) or name != name.strip():
        raise ValueError("Use a reader role name of 3-100 letters, digits, spaces, dots, dashes or underscores.")
    unit, client = guid(args.business_unit_id), guid(args.client_id)
    seed = f"{org}/native-voice-reader/{unit}/{client}"
    return {
        "org_url": org, "tenant_id": tenant, "business_unit_id": unit, "client_id": client,
        "principal_id": guid(args.principal_id), "role_name": name,
        # Derived IDs prove that this tool created the role; a role is never adopted by its name.
        "role_id": str(uuid5(NAMESPACE_URL, seed + "/role")),
        "new_user_id": str(uuid5(NAMESPACE_URL, seed + "/application-user")),
    }


def role_privileges(api, role_id, names):
    rows = api.get(f"RetrieveRolePrivilegesRole(RoleId={role_id})")["RolePrivileges"]
    unknown = sorted({guid(row["PrivilegeId"]) for row in rows if not row.get("PrivilegeName")} - set(names))
    for offset in range(0, len(unknown), 20):
        names.update({guid(row["privilegeid"]): row["name"] for row in api.rows("privileges", {
            "$select": "privilegeid,name",
            "$filter": " or ".join(f"privilegeid eq {key}" for key in unknown[offset:offset + 20]),
        })})
    result = {}
    for row in rows:
        identifier = guid(row["PrivilegeId"])
        name = row.get("PrivilegeName") or names.get(identifier)
        if not isinstance(name, str) or row.get("Depth") not in DEPTH:
            raise ValueError("Role privilege metadata could not be resolved; no privilege was changed.")
        result[name] = {"id": identifier, "depth": row["Depth"]}
    return result


def validate_reader_user(user, target):
    principal = user.get("azureactivedirectoryobjectid")
    if (
        comparable(user.get("applicationid")) != target["client_id"]
        or principal is not None and comparable(principal) != target["principal_id"]
        or comparable(user.get("_businessunitid_value")) != target["business_unit_id"]
        or user.get("accessmode") != 4 or user.get("isdisabled") is not False
    ):
        raise ValueError(
            "The application user for this managed identity has another object ID, business unit or access mode, "
            "or is disabled. Nothing was changed; review it with your security administrator.",
        )
    return user


def reader_user(api, target):
    users = api.rows("systemusers", {
        "$select": USER_FIELDS, "$filter": f"applicationid eq {target['client_id']}", "$top": "2",
    })
    if len(users) > 1:
        raise ValueError("More than one application user uses this managed identity; nothing was changed.")
    return validate_reader_user(users[0], target) if users else None


def reader_state(api, target, names):
    unit, role_id = target["business_unit_id"], target["role_id"]
    if api.get(f"businessunits({unit})", {"$select": "businessunitid,isdisabled"}).get("isdisabled") is not False:
        raise ValueError("Select the enabled business unit that holds the native conversation records.")
    required = {row["name"]: guid(row["privilegeid"]) for row in api.rows("privileges", {
        "$select": "privilegeid,name", "$filter": " or ".join(f"name eq '{name}'" for name in REQUIRED_READS),
    })}
    if set(required) != set(REQUIRED_READS):
        raise ValueError("A required native table privilege is missing; verify voice provisioning.")
    named = api.rows("roles", {
        "$select": "roleid,name", "$top": "5",
        "$filter": f"name eq '{target['role_name']}' and _businessunitid_value eq {unit}",
    })
    if any(comparable(row.get("roleid")) != role_id for row in named):
        raise ValueError(
            f"A role named {target['role_name']!r} already exists in this business unit and wasn't created by this "
            "tool. It was not adopted. Pass another --role-name, or remove that role after review.",
        )
    role = optional(api, f"roles({role_id})", {"$select": "roleid,name,ismanaged,_businessunitid_value"})
    if role is not None and (
        role.get("name") != target["role_name"] or role.get("ismanaged") is not False
        or comparable(role.get("_businessunitid_value")) != unit
    ):
        raise ValueError("The reader role ID exists with another name, business unit or managed state; it was preserved.")
    user = reader_user(api, target)
    user_id = guid(user["systemuserid"]) if user else None
    if role is not None:
        holders = api.rows(f"roles({role_id})/systemuserroles_association", {"$select": "systemuserid"})
        teams = api.rows(f"roles({role_id})/teamroles_association", {"$select": "teamid"})
        if teams or any(comparable(row.get("systemuserid")) != user_id for row in holders):
            raise ValueError("The reader role is shared with another user or team; its privileges and members were preserved.")
    associated = False
    if user is not None:
        roles = api.rows(f"systemusers({user_id})/systemuserroles_association", {"$select": "roleid,name"})
        others = sorted(row.get("name") or "unnamed" for row in roles if comparable(row.get("roleid")) != role_id)
        if others:
            raise ValueError(
                f"The application user already has other roles ({', '.join(others)}). They weren't removed; "
                "review them with your security administrator before granting the reader role.",
            )
        associated = bool(roles)
        for team in api.rows(f"systemusers({user_id})/teammembership_association", {"$select": "teamid"}):
            if api.rows(f"teams({guid(team['teamid'])})/teamroles_association", {"$select": "roleid"}):
                raise ValueError("The application user inherits roles through a team; nothing was changed.")
    return {
        "required": required, "role": role, "user": user, "associated": associated,
        "privileges": role_privileges(api, role_id, names) if role is not None else None,
    }


def privilege_gaps(privileges):
    privileges = privileges or {}
    return (
        sorted(name for name, row in privileges.items() if REQUIRED_READS.get(name) != row["depth"]),
        sorted(name for name, depth in REQUIRED_READS.items() if (privileges.get(name) or {}).get("depth") != depth),
    )


def reader_summary(target, state):
    extras, missing = privilege_gaps(state["privileges"])
    return {
        "role_id": target["role_id"], "role_exists": state["role"] is not None,
        "privileges_outside_required_set": extras,
        "missing_required_reads": missing if state["role"] is not None else sorted(REQUIRED_READS),
        "application_user_id": state["user"]["systemuserid"] if state["user"] else None,
        "role_associated": state["associated"],
    }


class ReaderJournal:
    """One immutable intent file before every write; a result or error file after its readback."""

    def __init__(self, directory):
        self.directory = directory / "journal"
        if not self.directory.exists():
            self.directory.mkdir(mode=0o700)

    def entries(self):
        result = {}
        for path in sorted(self.directory.iterdir()):
            match = re.fullmatch(r"(\d{4})-([a-z-]+)-(intent|result|error)\.json", path.name)
            if not match or path.is_symlink() or not path.is_file():
                raise ValueError("Unexpected file in the reader journal; preserve it and plan in a new --output-dir.")
            entry = result.setdefault(int(match[1]), {"kind": match[2]})
            entry[match[3]] = json.loads(read_private(path))
        return result

    def write(self, number, kind, outcome, value):
        write_private(self.directory / f"{number:04d}-{kind}-{outcome}.json", json.dumps({
            "kind": kind, **value, "recorded_at": datetime.now(timezone.utc).isoformat(),
        }, indent=2) + "\n")

    def begin(self, kind, value):
        number = max(self.entries(), default=0) + 1
        self.write(number, kind, "intent", value)
        return number

    def pending(self):
        return sorted((number, entry) for number, entry in self.entries().items() if "result" not in entry)

    def removals(self):
        return Counter(
            entry["intent"]["privilege"] for entry in self.entries().values()
            if entry["kind"] == "remove-privilege" and "result" in entry
        )

    def additions(self):
        return sum(entry["kind"] == "add-required-reads" and "result" in entry for entry in self.entries().values())


def reader_write(api, journal, kind, detail, path, body, confirm):
    number = journal.begin(kind, {**detail, "request": {"method": "POST", "path": path}})
    try:
        api.request(path, method="POST", body=body)
    except (OSError, ValueError, RuntimeError) as error:
        status = getattr(error, "code", None)
        if isinstance(error, urllib.error.HTTPError):
            error.close()
        journal.write(number, kind, "error", {"http_status": status, "error": type(error).__name__})
        confirmed, value = confirm()
        if not confirmed:
            raise RuntimeError(
                f"{kind} returned {f'HTTP {status}' if status else type(error).__name__} and the readback shows it "
                "wasn't applied. No retry was sent. Ask the owner; after the cause is fixed, plan again in a new "
                "--output-dir.",
            ) from None
        journal.write(number, kind, "result", {"confirmed": True, "after_error": True})
        return value
    confirmed, value = confirm()
    if not confirmed:
        journal.write(number, kind, "error", {"readback": "not confirmed"})
        raise RuntimeError(f"{kind} was sent but the readback doesn't confirm it. No retry was sent; ask the owner.")
    journal.write(number, kind, "result", {"confirmed": True})
    return value


def reader_role(args, api):
    target = reader_target(args, api)
    if args.apply and args.confirm_organization != target["org_url"]:
        raise ValueError("--apply requires --confirm-organization matching --org-url.")
    if not args.apply and args.confirm_organization is not None:
        raise ValueError("Organization confirmation is valid only with --apply.")
    output = private_path(args.output_dir)
    names = {}
    if not output.exists():
        if args.apply:
            raise ValueError("Plan the reader identity into a new private --output-dir before --apply.")
        state = reader_state(api, target, names)
        plan = {
            "version": 1, "kind": "native-reader-identity", "target": target,
            "policy": {
                "required_reads": REQUIRED_READS, "remove_every_other_privilege": True,
                "stop_when_a_removed_privilege_returns": READER_MAX_REMOVALS,
            },
            "observed": reader_summary(target, state), "created_at": datetime.now(timezone.utc).isoformat(),
        }
        output.mkdir(mode=0o700)
        write_private(output / "plan.json", json.dumps(plan, indent=2) + "\n")
        print(json.dumps({"status": "planned", "plan_only": True, "private_plan": str(output / "plan.json"),
                          **plan["observed"], "native_changes": 0}, indent=2))
        return
    if not output.is_dir() or (os.name != "nt" and output.stat().st_mode & 0o077):
        raise ValueError("The reader-identity output directory must be owner-only (chmod 700).")
    plan = json.loads(read_private(output / "plan.json"))
    if plan.get("kind") != "native-reader-identity" or plan.get("target") != target:
        raise ValueError("The saved reader plan targets another organization, identity or role; no change was submitted.")
    journal = ReaderJournal(output)
    if not args.apply:
        state = reader_state(api, target, names)
        print(json.dumps({"status": "audit", "plan_only": True, **reader_summary(target, state),
                          "pending_journal_steps": [number for number, _ in journal.pending()], "native_changes": 0}, indent=2))
        return
    role_id, org = target["role_id"], target["org_url"]

    def role_created(_=None):
        role = optional(api, f"roles({role_id})", {"$select": "roleid,name,ismanaged,_businessunitid_value"})
        return bool(role and role.get("name") == target["role_name"] and role.get("ismanaged") is False
                    and comparable(role.get("_businessunitid_value")) == target["business_unit_id"]), role

    def privilege_removed(intent):
        current = role_privileges(api, role_id, names)
        return intent["privilege"] not in current, current

    def reads_added(intent):
        current = role_privileges(api, role_id, names)
        return all((current.get(name) or {}).get("depth") == REQUIRED_READS[name] for name in intent["privileges"]), current

    def user_created(_=None):
        user = reader_user(api, target)
        return user is not None, user

    def role_associated(intent):
        rows = api.rows(f"systemusers({intent['user_id']})/systemuserroles_association", {"$select": "roleid,name"})
        return any(comparable(row.get("roleid")) == role_id for row in rows), rows

    checks = {
        "create-role": role_created, "remove-privilege": privilege_removed, "add-required-reads": reads_added,
        "create-application-user": user_created, "associate-role": role_associated,
    }
    reconciled = 0
    for number, entry in journal.pending():
        if entry["kind"] not in checks or not checks[entry["kind"]](entry["intent"])[0]:
            raise RuntimeError(
                f"Journal step {number:04d} ({entry['kind']}) from an earlier run isn't applied on the server, and it "
                "wasn't resent. Ask the owner; after the cause is fixed, plan again in a new --output-dir.",
            )
        journal.write(number, entry["kind"], "result", {"confirmed": True, "reconciled_on_resume": True})
        reconciled += 1
    state = reader_state(api, target, names)
    writes = 0
    if state["role"] is None:
        reader_write(api, journal, "create-role", {"role_id": role_id, "role_name": target["role_name"]}, "roles", {
            "roleid": role_id, "name": target["role_name"],
            "description": "Native voice reader. Holds only the documented native read privileges.",
            "businessunitid@odata.bind": f"/businessunits({target['business_unit_id']})",
        }, role_created)
        writes += 1
    current = role_privileges(api, role_id, names)
    removals, additions, returned = journal.removals(), journal.additions(), set()
    while True:
        extras, missing = privilege_gaps(current)
        if extras:
            name = extras[0]
            if removals[name] >= READER_MAX_REMOVALS:
                raise RuntimeError(
                    f"{name} came back after being removed {removals[name]} times. No further privilege changes were "
                    "made. Stop and ask the owner with this readback.",
                )
            if removals[name]:
                returned.add(name)
            current = reader_write(
                api, journal, "remove-privilege", {"privilege": name, "privilege_id": current[name]["id"]},
                f"roles({role_id})/Microsoft.Dynamics.CRM.RemovePrivilegeRole",
                {"Privilege": {"@odata.type": "Microsoft.Dynamics.CRM.privilege", "privilegeid": current[name]["id"]}},
                lambda: privilege_removed({"privilege": name}),
            )
            removals[name] += 1
            writes += 1
            continue
        if not missing:
            break
        if additions >= READER_MAX_ADDITIONS:
            raise RuntimeError("Required reads disappeared after being added twice. Stop and ask the owner with this readback.")
        current = reader_write(
            api, journal, "add-required-reads", {"privileges": missing},
            f"roles({role_id})/Microsoft.Dynamics.CRM.AddPrivilegesRole",
            {"Privileges": [{"PrivilegeId": state["required"][name], "Depth": REQUIRED_READS[name]} for name in missing]},
            lambda: reads_added({"privileges": missing}),
        )
        additions += 1
        writes += 1
    user = state["user"]
    if user is None:
        identity = {"systemuserid": target["new_user_id"], "applicationid": target["client_id"]}
        user = reader_write(api, journal, "create-application-user", identity, "systemusers", {
            **identity, "azureactivedirectoryobjectid": target["principal_id"], "accessmode": 4,
            "firstname": "Contact center", "lastname": "Native voice reader",
            "businessunitid@odata.bind": f"/businessunits({target['business_unit_id']})",
        }, user_created)
        writes += 1
    user_id = guid(user["systemuserid"])
    if not state["associated"] and not role_associated({"user_id": user_id})[0]:
        reader_write(
            api, journal, "associate-role", {"user_id": user_id, "role_id": role_id},
            f"systemusers({user_id})/systemuserroles_association/$ref",
            {"@odata.id": f"{org}/api/data/v9.2/roles({role_id})"}, lambda: role_associated({"user_id": user_id}),
        )
        writes += 1
    final = reader_state(api, target, names)
    summary = reader_summary(target, final)
    if summary["privileges_outside_required_set"] or summary["missing_required_reads"] or not final["associated"]:
        raise RuntimeError("The final readback doesn't match the required reads and one role assignment. Ask the owner; no retry was sent.")
    receipt = {
        "status": "configured" if writes else "reconciled" if reconciled else "already_configured", **summary,
        "application_user_created_by_this_tool": comparable(summary["application_user_id"]) == target["new_user_id"],
        "privileges_that_returned_after_removal": sorted(returned), "native_changes": writes,
        "journal_steps_reconciled": reconciled,
        "journal": str(journal.directory),
    }
    write_private(output / f"after-{uuid4().hex}.json", json.dumps({**receipt, "privileges": final["privileges"]}, indent=2) + "\n")
    print(json.dumps(receipt, indent=2))


def validate_basic_voice_agent(bot):
    try:
        configuration = json.loads(bot.get("configuration") or "null")
    except (TypeError, json.JSONDecodeError) as error:
        raise ValueError("The saved native agent configuration is not valid JSON.") from error
    if not isinstance(configuration, dict) or configuration.get("isTelephonyEnabled") is not True:
        raise ValueError("Enable and save Basic voice on the selected agent, then publish. A connected Telephony channel does not prove voice is enabled.")
    settings = configuration.get("settings")
    if not isinstance(settings, dict) or settings.get("TelephonyInitialized") is not True:
        raise ValueError("The native voice initialization has not been saved. Finish the supported Voice setup and publish before acceptance.")
    mode = configuration.get("voiceProcessingMode")
    if mode is not None and (not isinstance(mode, dict) or mode.get("$kind") != "ClassicVoiceProcessingMode"):
        raise ValueError("The native routing agent must use Basic/classic voice, not real-time generative voice.")
    if settings.get("GenerativeActionsEnabled") is not None and settings.get("GenerativeActionsEnabled") is not False:
        raise ValueError("The native routing agent must retain classic topic orchestration.")


def topic_nodes(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from topic_nodes(child)
    elif isinstance(value, list):
        for child in value:
            yield from topic_nodes(child)


def audit_topics(rows):
    """Fail on a reachable starter escalation stub, including Fallback's redirect to one."""
    parsed = {}
    for row in rows:
        if row.get("componenttype") != TOPIC_COMPONENT or row.get("statecode", 0) != 0:
            continue
        try:
            document = topics.load_topic(row.get("data") or "")
        except (ValueError, yaml.YAMLError) as error:
            raise ValueError(f"Topic {row.get('name')!r} could not be read as Copilot YAML; inspect it before preflight.") from error
        nodes = list(topic_nodes(document))
        parsed[row.get("schemaname")] = {
            "name": row.get("name"), "trigger": (document.get("beginDialog") or {}).get("kind"),
            "transfers": any(
                node.get("kind") in {"TransferConversationV2", "TransferConversation"}
                and isinstance(node.get("transferType"), dict) and node["transferType"].get("kind") == "TransferToAgent"
                for node in nodes
            ),
            "starter_text": bool(STARTER_ESCALATION.search(row.get("data") or "")),
            "redirects": sorted({
                node["dialog"] for node in nodes
                if node.get("kind") in {"BeginDialog", "ReplaceDialog"} and isinstance(node.get("dialog"), str)
            }),
        }
    reachable = [schema for schema, topic in parsed.items() if topic["trigger"] not in REDIRECT_ONLY_TRIGGERS]
    index = 0
    while index < len(reachable):
        for target in parsed[reachable[index]]["redirects"]:
            if target in parsed and target not in reachable:
                reachable.append(target)
        index += 1
    escalations = [schema for schema in reachable if parsed[schema]["trigger"] == "OnEscalate"]
    if not escalations:
        raise ValueError("The standard Escalate topic is missing or turned off. Turn it on with a Transfer to agent node.")
    stubs = {
        schema for schema in reachable
        if parsed[schema]["starter_text"] or (schema in escalations and not parsed[schema]["transfers"])
    }
    problems = [
        f"{parsed[schema]['name']} is the starter escalation stub (no Transfer to agent, or the 'not currently configured' message)"
        for schema in sorted(stubs)
    ] + [
        f"{parsed[schema]['name']} redirects to the stub {parsed[target]['name']}"
        for schema in reachable for target in parsed[schema]["redirects"] if target in stubs
    ]
    if problems:
        raise ValueError("Give Escalate a Transfer to agent node, publish, then verify again: " + "; ".join(problems) + ".")
    return {
        "reachable_topics_audited": len(reachable),
        "escalate_transfers_to_agent": [parsed[schema]["name"] for schema in escalations],
        "fallback_redirects": sorted({
            parsed[target]["name"] for schema in reachable if parsed[schema]["trigger"] == "OnUnknownIntent"
            for target in parsed[schema]["redirects"] if target in parsed
        }),
    }


def native_configuration_checks(api, workstream_id, bot_id, workstream):
    failure_action = workstream.get("_msdyn_botfailureactionconfig_value")
    if not isinstance(failure_action, str) or not failure_action:
        raise ValueError("The workstream's AI agent Fallback action is empty. Set it to Wait Music and Escalate, save, then verify again.")
    voices = api.rows("msdyn_ocvoicechannelsettings", {
        "$select": ",".join(("msdyn_ocvoicechannelsettingid", "_msdyn_liveworkstreamid_value", "statecode",
                             CONSULT_CONTROL, *TRANSFER_CONTROLS)),
        "$filter": f"_msdyn_liveworkstreamid_value eq {workstream_id}", "$top": "2",
    })
    if len(voices) != 1:
        raise ValueError(
            "Expected exactly one voice channel settings record linked to this workstream (msdyn_liveworkstreamid); "
            f"found {len(voices)}. Finish or inspect the voice channel setup; no record was guessed.",
        )
    voice = voices[0]
    if comparable(voice.get("_msdyn_liveworkstreamid_value")) != workstream_id:
        raise ValueError("The voice channel settings record returned belongs to another workstream; no record was guessed.")
    if voice.get("statecode") != 0:
        raise ValueError("The workstream's voice channel settings record is inactive.")
    if voice.get(CONSULT_CONTROL) is not True:
        raise ValueError("External phone consult is off for this workstream. Turn it on in the voice channel Behaviors, save, then verify again.")
    solutions = api.rows("solutions", {
        "$select": "uniquename,friendlyname,version,installedon",
        "$filter": f"uniquename eq '{PVA_EXTENSION}'", "$top": "2",
    })
    if len(solutions) != 1 or solutions[0].get("uniquename") != PVA_EXTENSION:
        raise ValueError(
            f"The Omnichannel Power Virtual Agent Extension ({PVA_EXTENSION}) isn't installed. Install it, confirm the "
            "install by its package status, then verify again. mspva_TelephonyExtension isn't required.",
        )
    topics_report = audit_topics(api.rows("botcomponents", {
        "$select": "botcomponentid,name,schemaname,componenttype,statecode,data",
        "$filter": f"_parentbotid_value eq {bot_id} and componenttype eq {TOPIC_COMPONENT}",
    }))
    return {
        "ai_agent_failure_action": {
            "id": failure_action,
            "name": workstream.get("_msdyn_botfailureactionconfig_value@OData.Community.Display.V1.FormattedValue"),
        },
        "voice_controls": {
            "external_phone_consult": True,
            **{name: {"value": voice.get(name), "expected": False, "as_expected": voice.get(name) is False}
               for name in TRANSFER_CONTROLS},
        },
        "omnichannel_pva_extension": {
            "installed": True, "version": solutions[0].get("version"), "installedon": solutions[0].get("installedon"),
        },
        "telephony_extension_required": False,
        "topics": topics_report,
    }


def verify(args, api):
    workstream = api.get(f"msdyn_liveworkstreams({guid(args.workstream_id)})", {
        "$select": "msdyn_name,msdyn_direction,msdyn_streamsource,statecode,_msdyn_defaultqueue_value,_msdyn_bot_user_value,"
                   "_msdyn_botfailureactionconfig_value",
    })
    bot = api.get(f"bots({guid(args.bot_id)})", {"$select": "name,language,supportedlanguages,publishedon,configuration"})
    if workstream["statecode"] != 0 or workstream["msdyn_direction"] != 0 or workstream["msdyn_streamsource"] != 192440000:
        raise ValueError("Select an active native inbound voice workstream.")
    if not workstream.get("_msdyn_bot_user_value") or not workstream.get("_msdyn_defaultqueue_value"):
        raise ValueError("Assign the published voice agent and native fallback queue in the workstream.")
    if bot["language"] != 1033 or "1025" not in (bot.get("supportedlanguages") or "").split(",") or not bot.get("publishedon"):
        raise ValueError("The documented agent must be published with English primary and Arabic secondary.")
    validate_basic_voice_agent(bot)
    native = native_configuration_checks(api, guid(args.workstream_id), guid(args.bot_id), workstream)
    published_at = datetime.fromisoformat(bot["publishedon"].replace("Z", "+00:00"))
    records = components(api, args.bot_id)
    for name in ("Conversation Start", CONTROLLER, CONTEXT):
        version = api.get(f"botcomponents({records[name]['botcomponentid']})", {"$select": "modifiedon"})
        if datetime.fromisoformat(version["modifiedon"].replace("Z", "+00:00")) > published_at:
            raise ValueError("A relevant topic has unpublished edits.")
    controller = topics.load_topic(records[CONTROLLER]["data"])
    nodes = {node["id"]: node for node in controller["beginDialog"]["actions"]}
    case_context = "native_set_case_number" in nodes
    if getattr(args, "crm_case_context", False) and not case_context:
        raise ValueError("The published controller is missing the requested CRM CaseNumber integration.")
    if case_context:
        records = components(api, args.bot_id, crm_case_context=True)
        validate_case_variable(records[CASE_VARIABLE])
        topics.validate_case_handoff(controller)
        version = api.get(f"botcomponents({records[CASE_VARIABLE]['botcomponentid']})", {"$select": "modifiedon"})
        if datetime.fromisoformat(version["modifiedon"].replace("Z", "+00:00")) > published_at:
            raise ValueError("The CaseNumber global has unpublished edits.")
    if (
        "speech_turn_control: true" not in nodes["native_start"]["body"]["content"]
        or nodes["native_listen_turn"]["entity"] != "StringPrebuiltEntity"
        or nodes["native_wait"]["method"] != "Post"
        or nodes["native_wait"]["body"]["content"] != "=JSON({language: Topic.NativeListenLanguage, revision: Topic.NativeLanguageRevision})"
    ):
        raise ValueError("The published controller does not match the documented speech-turn contract.")
    key = secret_value(args.secrets_file, "DYNAMICS_NATIVE_MEDIA_API_KEY")
    origin = voice_origin(args.voice_origin)
    for name in ("native_start", "native_wait", "native_cleanup"):
        node = nodes[name]
        if origin + "/dynamics/native/sessions" not in node["url"] or node.get("headers", {}).get("X-API-Key") != key:
            raise ValueError("A Copilot HTTP node has a different origin or control key than this deployment.")
    variable = topics.load_topic(records[VARIABLE]["data"])
    if variable.get("scope") != "Conversation" or variable.get("isExternalInitializationAllowed") is not True or variable.get("isOutputToExternalCallers") is not False:
        raise ValueError("The external conversation context definition changed.")
    if any(node.get("variable") == "init:Global.msdyn_ConversationId" for node in nodes.values()):
        raise ValueError("The runtime controller must not execute the conversation-ID initializer.")
    request = urllib.request.Request(origin + "/dynamics/native/readiness", headers={"X-API-Key": key, "Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=45) as response:
        ready = json.load(response)
    if not all(ready.get(key) is True for key in ("enabled", "observer_enabled", "dataverse_connected", "voip_gateway_ready")) or ready.get("transport") != "voip":
        raise ValueError("The Azure native VoIP pilot or its observer is not ready.")
    if case_context and ready.get("crm_case_context_supported") is not True:
        raise ValueError("The CaseNumber controller requires a backend with CRM case-context snapshot support.")
    if ready.get("fast_start") and ready.get("warm_page_ready") is not True:
        raise ValueError("Fast startup is selected, but its unused SDK page is not ready.")
    body = nodes["native_start"]["body"]["content"]
    for option in ("play_startup_notice", "native_startup_notice"):
        if option in body and not re.search(r"\b" + option + r"\s*:\s*(true|false)\b", body):
            raise ValueError("The controller has an invalid startup-notice option.")
    application_notice = bool(re.search(r"\bplay_startup_notice\s*:\s*true\b", body))
    native_notice = bool(re.search(r"\bnative_startup_notice\s*:\s*true\b", body))
    if application_notice and native_notice:
        raise ValueError("The startup notice must have exactly one announcement owner.")
    if application_notice or native_notice:
        owner = "application" if application_notice else "native"
        capability = "application_startup_preparation_supported" if application_notice else "native_startup_notice_supported"
        latency = (
            {"kind": "LatencyMessageSettings", "allowLatencyMessage": False}
            if application_notice else topics.native_startup_latency_settings(origin)
        )
        guards = [condition for condition in nodes["native_check_start"].get("conditions", [])
                  if condition.get("id") == "native_start_not_prepared"]
        if (
            ready.get("startup_notice_supported") is not True or ready.get("startup_notice_available") is not True
            or ready.get(capability) is not True
            or ready.get("startup_failure_notice_supported") is not True
            or ready.get("startup_notice_sha256") != hashlib.sha256(topics.NOTICE_AUDIO.read_bytes()).hexdigest()
            or nodes["native_connecting_notice"]["activity"] != {"speak": ['<break time="20ms" />']}
            or nodes["native_start"]["url"] != origin + "/dynamics/native/sessions?wait_for_startup=true"
            or nodes["native_start"].get("latencyMessageSettings") != latency
            or len(guards) != 1 or guards[0].get("condition") != topics.startup_prepared_condition(owner)
        ):
            raise ValueError("The opted-in startup notice needs the exact single-owner contract and a compatible prepared-start backend/asset.")
    if "workstreams" in ready:
        allowed = ready["workstreams"]
        if not isinstance(allowed, list) or not all(isinstance(row, dict) for row in allowed):
            raise ValueError("The voice API returned an invalid workstream allowlist.")
        matching = [row for row in allowed if row.get("id") == guid(args.workstream_id)]
        if len(matching) != 1 or matching[0].get("name") != workstream["msdyn_name"]:
            raise ValueError("The voice API is configured for a different workstream.")
    elif ready.get("workstream") != workstream["msdyn_name"]:
        raise ValueError("The voice API is configured for a different workstream.")
    with urllib.request.urlopen(origin + "/health", timeout=30) as response:
        health = json.load(response)
    if not health.get("voice_enabled") or health.get("automatic_phone_conference_enabled") is not False or health.get("dynamics_outbound", {}).get("submission_enabled") is not False:
        raise ValueError("Voice, legacy conferencing or outbound flags do not match this runbook.")
    print(json.dumps({
        "configuration_checks_passed": True, "workstream": workstream["msdyn_name"], "bot": bot["name"],
        "saved_basic_voice_enabled": True, "saved_voice_initialized": True,
        "native_configuration": native, "ready": ready, "calls_placed": 0,
        "remaining_gates": [
            *([] if "workstreams" in ready else ["Exact configured workstream ID (legacy readiness exposes only its name)"]),
            "Workstream selection of the published agent", "Native number/event delivery",
            "Representative queue membership, presence and capacity", "Live Arabic/English transcript and speaker labels",
            "Same-conversation human acceptance",
        ],
    }, indent=2))


def observer_parameters(args):
    origin = voice_origin(args.voice_origin)
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", args.system_topic_name):
        raise ValueError("Use the existing native ACS Event Grid system-topic name.")
    expiry = observer_expiry(args.expires_at, args.permanent)
    key = secret_value(args.secrets_file, "DYNAMICS_NATIVE_PROBE_API_KEY")
    document = {
        "$schema": "https://schema.management.azure.com/schemas/2019-04-01/deploymentParameters.json#",
        "contentVersion": "1.0.0.0", "parameters": {
            "systemTopicName": {"value": args.system_topic_name},
            "webhookUrl": {"value": origin + "/dynamics/handover/probe/events"},
            "expiresAt": {"value": expiry},
            "permanent": {"value": args.permanent},
            "apiKey": {"value": key},
        },
    }
    output = private_path(args.output)
    write_private(output, json.dumps(document, indent=2) + "\n")
    print(json.dumps({"private_parameters": str(output), "expires_at": expiry, "permanent": args.permanent, "deployed": False}))


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    commands = result.add_subparsers(dest="command", required=True)

    def authenticated(name, description):
        command = commands.add_parser(name, help=description)
        command.add_argument("--tenant-id", required=True, type=guid)
        command.add_argument("--org-url", required=True, type=organization)
        return command

    def observer_lease(command):
        lease = command.add_mutually_exclusive_group(required=True)
        lease.add_argument("--expires-at")
        lease.add_argument("--permanent", action="store_true")

    discovery = authenticated("discover", "Read IDs/names for runtime apps, bots, workstreams and queues; no secret content.")
    discovery.add_argument("--bot-id", type=guid)
    bundle = authenticated("bundle", "Export a private reviewed bundle from UI-created topics; no remote writes.")
    bundle.add_argument("--bot-id", required=True, type=guid)
    bundle.add_argument("--voice-origin", required=True, type=voice_origin)
    bundle.add_argument("--test-caller", action="append", required=True)
    bundle.add_argument("--secrets-file", required=True, type=Path)
    bundle.add_argument("--output-dir", required=True, type=Path)
    bundle.add_argument("--replace-controller", action="store_true")
    bundle.add_argument("--startup-notice", action="store_true", help="Opt into verified startup-only playback; requires a compatible backend.")
    bundle.add_argument("--crm-case-context", action="store_true", help="Pass the selected Dynamics ticketnumber on native handoff; requires the CaseNumber output global.")
    case_variable = authenticated("case-context-variable", "Inspect or create only the unused CaseNumber output global; never publishes.")
    case_variable.add_argument("--bot-id", required=True, type=guid)
    case_variable.add_argument("--apply", action="store_true")
    case_variable.add_argument("--receipt-file", type=Path, help="New credential-free private rollback receipt, required for --apply.")
    context = authenticated("context-variable", "Inspect or create only the unused native conversation input global; never publishes.")
    context.add_argument("--bot-id", required=True, type=guid)
    context.add_argument("--apply", action="store_true")
    context.add_argument("--confirm-organization", type=organization)
    context.add_argument("--receipt-file", type=Path, help="New credential-free private rollback receipt, required for --apply.")
    baseline = authenticated("agent-baseline", "Plan or conditionally apply only deterministic bilingual anonymous-native agent metadata; never publishes.")
    baseline.add_argument("--bot-id", required=True, type=guid)
    baseline.add_argument("--output-dir", required=True, type=Path)
    baseline.add_argument("--apply", action="store_true")
    baseline.add_argument("--confirm-organization", type=organization)
    startup = authenticated("startup-audio", "Plan only the accepted native startup-audio flags; never changes recording, consent or music assets.")
    startup.add_argument("--workstream-id", required=True, type=guid)
    startup.add_argument("--output-dir", required=True, type=Path, help="Private directory outside the checkout; reuse its immutable plan for apply/readback.")
    startup.add_argument("--apply", action="store_true", help="Apply a previously saved plan with one conditional PATCH; never retries a pending intent.")
    startup.add_argument("--confirm-organization", type=organization)
    startup.add_argument(
        "--confirm-notice-policy", action="store_true",
        help="Assert human review of the application-owned notice and disabled native recording/transcription notifications; not a compliance determination.",
    )
    stage = commands.add_parser("stage", help="Preview or atomically stage a private bundle; never publishes.")
    stage.add_argument("--bundle-dir", required=True, type=Path)
    stage.add_argument("--apply", action="store_true")
    role = authenticated(
        "reader-role",
        "Plan, apply or resume the native reader role and managed-identity app user; never adopts a role by name.",
    )
    role.add_argument("--business-unit-id", required=True, type=guid, help="Business unit that holds the native conversation records.")
    role.add_argument("--client-id", required=True, type=guid, help="Voice managed identity client ID (UAMI_CLIENT_ID in the runbook).")
    role.add_argument("--principal-id", required=True, type=guid, help="Voice managed identity principal ID (UAMI_PRINCIPAL_ID in the runbook).")
    role.add_argument("--role-name", default=READER_ROLE_NAME)
    role.add_argument("--output-dir", required=True, type=Path, help="Private directory outside the checkout; reuse it to apply and resume.")
    role.add_argument("--apply", action="store_true", help="Apply the saved plan; resumes from its journal without resending unconfirmed writes.")
    role.add_argument("--confirm-organization", type=organization)
    link = authenticated("connection", "Read back the Dynamics connection to the native ACS resource; no writes, no secret values.")
    link.add_argument("--native-acs-resource-id", required=True, type=acs_resource_id)
    link.add_argument("--event-grid-app-id", required=True, type=guid, help="Client ID of the Event Grid delivery app entered in Dynamics.")
    check = authenticated("verify", "Check saved publication, runtime flags and real application Dataverse readiness.")
    check.add_argument("--bot-id", required=True, type=guid)
    check.add_argument("--workstream-id", required=True, type=guid)
    check.add_argument("--voice-origin", required=True, type=voice_origin)
    check.add_argument("--secrets-file", required=True, type=Path)
    check.add_argument("--crm-case-context", action="store_true", help="Require and verify the CaseNumber handoff contract.")
    observer = commands.add_parser("observer-parameters", help="Write private parameters for an expiring or permanent passive observer.")
    observer.add_argument("--system-topic-name", required=True)
    observer.add_argument("--voice-origin", required=True, type=voice_origin)
    observer_lease(observer)
    observer.add_argument("--secrets-file", required=True, type=Path)
    observer.add_argument("--output", required=True, type=Path)
    keygen = commands.add_parser("new-keys", help="Generate three separate control keys in a private env file; no cloud writes.")
    keygen.add_argument("--output", required=True, type=Path)
    bootstrap = commands.add_parser("bootstrap-env", help="Write initial deployment settings without partially configuring the native observer.")
    bootstrap.add_argument("--secrets-file", required=True, type=Path)
    bootstrap.add_argument("--output", required=True, type=Path)
    sheet = commands.add_parser("copy-sheet", help="Write the four Dynamics connection values to a private copy sheet; prints no secret.")
    sheet.add_argument("--tenant-id", required=True, type=guid)
    sheet.add_argument("--native-acs-resource-id", required=True, type=acs_resource_id)
    sheet.add_argument("--event-grid-app-id", required=True, type=guid, help="Client ID of the Event Grid delivery app.")
    sheet.add_argument("--output", required=True, type=Path, help="New owner-only file outside the checkout.")
    runtime = commands.add_parser("runtime-env", help="Read native ACS keys into a complete private runtime file; does not apply it.")
    runtime.add_argument("--tenant-id", required=True, type=guid)
    runtime.add_argument("--org-url", required=True, type=organization)
    runtime.add_argument("--workstream-id", required=True, type=guid)
    runtime.add_argument("--additional-workstream-id", action="append", type=guid, default=[])
    runtime.add_argument("--native-acs-resource-id", required=True)
    runtime.add_argument("--app-number", default="", help="Optional application-owned ACS number; native VoIP does not dial it.")
    observer_lease(runtime)
    runtime.add_argument("--fast-start", action="store_true")
    runtime.add_argument("--secrets-file", required=True, type=Path)
    runtime.add_argument("--output", required=True, type=Path)
    return result


def main():
    args = parser().parse_args()
    if args.command == "new-keys":
        output = private_path(args.output)
        write_private(output, "".join(f'{key}="{secrets.token_urlsafe(48)}"\n' for key in (
            "INGEST_API_KEY", "DYNAMICS_NATIVE_MEDIA_API_KEY", "DYNAMICS_NATIVE_PROBE_API_KEY",
        )))
        print(json.dumps({"private_key_file": str(output), "keys_generated": 3, "cloud_changes": False}))
    elif args.command == "observer-parameters":
        observer_parameters(args)
    elif args.command == "bootstrap-env":
        bootstrap_env(args)
    elif args.command == "runtime-env":
        runtime_env(args)
    elif args.command == "copy-sheet":
        copy_sheet(args)
    elif args.command == "stage":
        stage_bundle(args)
    else:
        api = Dataverse(args.org_url, args.tenant_id)
        {
            "discover": discover, "bundle": export_bundle, "reader-role": reader_role, "connection": connection,
            "verify": verify, "case-context-variable": case_context_variable, "context-variable": context_variable,
            "startup-audio": startup_audio, "agent-baseline": agent_baseline,
        }[args.command](args, api)


if __name__ == "__main__":
    try:
        main()
    except urllib.error.HTTPError as error:
        print(json.dumps({"status": "failed", "error": f"Service returned HTTP {error.code}; no request values were printed."}))
        raise SystemExit(1)
    except (ValueError, KeyError, RuntimeError, yaml.YAMLError, OSError, subprocess.SubprocessError) as error:
        print(json.dumps({"status": "failed", "error": str(error) if isinstance(error, (ValueError, RuntimeError)) else type(error).__name__}))
        raise SystemExit(1)
