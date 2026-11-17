"""Replayable native Pause AI provisioning. Plan by default; no deployment, calls or bot publication."""

import argparse
import base64
import copy
import hashlib
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID, uuid5

import native_voice_setup as native
import stage_native_media_topic as topics

ENTITY = "moacc_nativeaicommand"
TABLE = ENTITY + "s"
PRIMARY = ENTITY + "id"
RESULTS = ("moacc_outcome", "moacc_reason", "moacc_ackrevision", "moacc_ackon", "moacc_inflight")
REQUESTS = ("moacc_conversationid", "moacc_action")
SECURED = REQUESTS + RESULTS
WEBROOT = topics.ROOT / "apps/voice/dynamics"
OWNER = "Native supervision contract 1"
MAX_EVENTS = 500
METADATA_READS = (
    ("prvReadEntity", "runtime_metadata_read"),
    ("prvReadAttribute", "runtime_attribute_read"),
    ("prvReadRelationship", "runtime_relationship_read"),
)
SEEDED_ROLE_DEFAULTS = frozenset({
    "prvReadSdkMessage", "prvReadSdkMessageProcessingStep", "prvReadSdkMessageProcessingStepImage",
    "prvReadPluginType", "prvReadPluginAssembly", "prvReadSharePointDocument",
    "prvReadSharePointData", "prvWriteSharePointData", "prvCreateSharePointData",
})
STAGES = ("plan", "schema", "access", "controller", "webresource", "bind", "publish", "verify", "unbind", "verify-unbound",
          "reconcile-rejected-schema", "upgrade-runtime-metadata", "upgrade-startup-notice",
          "upgrade-startup-playback", "upgrade-native-startup", "rollback-native-startup",
          "upgrade-application-startup", "rollback-application-startup", "all", "rollback")
NOTICE_SHA256 = "832a0365dde484512ea69bea45c406aa5074845c7f4361293ccf5f4dd4c8be14"
LEGACY_NOTICE_TEXT = "يتم تسجيل هذه المكالمة لأغراض الجودة والتدريب. This call is recorded for quality and training purposes."
REQUEST_ID_HEADERS = ("x-ms-service-request-id", "REQ_ID")
FAILURE_READ_BYTES = 64 * 1024
FAILURE_BODY_CHARACTERS = 2000
TOKEN_CHARACTER = r"[A-Za-z0-9+/_-]"
REDACTIONS = (
    (re.compile(r"(?i)\b((?:x-api-key|api[_-]?key|authorization|access[_-]?key|client[_-]?secret|password|secret|token|sig)"
                r"[\"']?\s*[:=]\s*[\"']?)(?:bearer\s+)?[^\s\"',;&<>{}]+"), r"\1[redacted]"),
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+"), "Bearer [redacted]"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]*"), "[redacted]"),
    (re.compile(rf"(?<!{TOKEN_CHARACTER})(?={TOKEN_CHARACTER}*[0-9])(?={TOKEN_CHARACTER}*[a-z])"
                rf"(?={TOKEN_CHARACTER}*[A-Z]){TOKEN_CHARACTER}{{32,}}={{0,2}}"), "[redacted]"),
    (re.compile(r"(?<![\w.+-])[\w.+-]{1,64}@[A-Za-z0-9-]{1,63}(?:\.[A-Za-z0-9-]{1,63}){1,8}"), "[email]"),
)
FAILURE_NEXT_STEP = (
    "Rerun the same stage with the same receipt directory; it reads back before writing. If the same request "
    "fails again, retry once only with a concrete change, then stop and ask the owner with this output."
)


def role_privileges(audience):
    return (
        {"prvCreate" + ENTITY: "Basic", "prvRead" + ENTITY: "Basic"} if audience == "requester" else
        {"prvRead" + ENTITY: "Global", "prvWrite" + ENTITY: "Global",
         "prvReadUser": "Local", "prvReadbot": "Global", "prvReadbotcomponent": "Global",
         **{name: "Global" for name, _ in METADATA_READS}}
    )


def creation_targets(config):
    targets = {
        "publisher": "publishers", "solution": "solutions",
        "tab": "msdyn_applicationtabtemplates", "tab_parameter": "msdyn_templateparameters",
        **{"resource_" + name: "webresourceset" for name in ("native-supervision.html", "native-supervision.js")},
    }
    for audience in ("requester", "runtime"):
        targets.update({"role_" + audience: "roles", "profile_" + audience: "fieldsecurityprofiles"})
        targets.update({"permission_" + audience + "_" + name: "fieldpermissions" for name in SECURED})
    return {key: f"{table}({owned(config, key)})" for key, table in targets.items()}


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def digest(value):
    return hashlib.sha256((value if isinstance(value, str) else canonical(value)).encode()).hexdigest()


def require(value, message):
    if not value:
        raise ValueError(message)


def redact(text):
    """Remove keys, tokens and email addresses before text is printed or journaled."""
    for pattern, replacement in REDACTIONS:
        text = pattern.sub(replacement, text)
    return text


def request_path(url):
    if not isinstance(url, str) or not url:
        return None
    path = urllib.parse.urlsplit(url).path if "://" in url else url.split("?", 1)[0]
    return path.split("/api/data/v9.2/", 1)[-1][:300]


def http_failure(error, method=None, path=None):
    """Bounded, secret-free evidence from a failed HTTP response: status, error code, request IDs and body."""
    try:
        headers = list((error.headers or {}).items())
    except AttributeError:
        headers = []
    found = {str(name).lower(): value for name, value in headers if isinstance(value, str)}
    request_ids = {
        name: value for name in REQUEST_ID_HEADERS
        if (value := re.sub(r"[^A-Za-z0-9._:,-]", "", found.get(name.lower(), ""))[:200])
    }
    retry_after = found.get("retry-after", "").strip()
    try:
        raw = error.read(FAILURE_READ_BYTES + 1) or b""
    except (OSError, ValueError, AttributeError):
        raw = b""
    text = raw[:FAILURE_READ_BYTES].decode("utf-8", "replace")
    if len(raw) > FAILURE_READ_BYTES:
        # Drop the value cut at the read limit, so a partial key cannot escape redaction.
        text = text[:len(text) - len(re.match(r"[^\s\"',;&<>{}]*", text[::-1])[0])]
    try:
        code = json.loads(text)["error"]["code"]
    except (ValueError, KeyError, TypeError):
        code = None
    body = redact(text)
    return {
        "status": error.code, "method": method, "path": request_path(path or getattr(error, "url", None)),
        "error_code": code if isinstance(code, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", code) else None,
        "request_ids": request_ids,
        "retry_after": retry_after if re.fullmatch(r"\d{1,6}", retry_after) else None,
        "body": body[:FAILURE_BODY_CHARACTERS],
        "body_truncated": len(raw) > FAILURE_READ_BYTES or len(body) > FAILURE_BODY_CHARACTERS,
    }


def failure_message(details, sequence=None):
    request = " ".join(part for part in (details.get("method"), details.get("path")) if part) or "Request"
    ids = ", ".join(f"{name}={value}" for name, value in details["request_ids"].items()) or "none returned"
    message = f"{request} returned HTTP {details['status']}"
    if details.get("error_code"):
        message += f" ({details['error_code']})"
    message += "; request IDs: " + ids
    if details.get("body"):
        message += "; response: " + details["body"]
    if sequence:
        message += f"; journal event {sequence:04d}"
    return message + "."


class RequestFailed(urllib.error.HTTPError):
    """An HTTP failure that carries its bounded, secret-free evidence and keeps the original status code."""

    def __init__(self, error, details, event=None):
        self.details = details
        self.journal_event = event["sequence"] if event else None
        super().__init__(getattr(error, "url", None), error.code, failure_message(details, self.journal_event),
                         getattr(error, "headers", None), None)


def record_failure(journal, kind, key, details):
    if journal is None:
        return None
    try:
        return journal.append(kind, key, **details)
    except (ValueError, OSError):
        return None  # The raised error still carries the evidence.


def validate_config(value):
    fields = {
        "version", "organization_url", "tenant_id", "namespace_id", "business_unit_id",
        "runtime_user_id", "runtime_application_id", "approved_actor_ids", "controller_component_ids",
        "workstream_ids", "voice_origin", "session_template_ids", "session_binding_reviewed",
        "trust_system_administrators",
    }
    require(isinstance(value, dict) and set(value) == fields and value["version"] == 1, "Use the exact version-1 supervision configuration.")
    result = copy.deepcopy(value)
    result["organization_url"] = native.organization(value["organization_url"])
    result["voice_origin"] = native.voice_origin(value["voice_origin"])
    for key in ("tenant_id", "namespace_id", "business_unit_id", "runtime_user_id", "runtime_application_id"):
        result[key] = native.guid(value[key])
    for key, limit, required in (
        ("approved_actor_ids", 16, True), ("controller_component_ids", 4, True),
        ("workstream_ids", 4, True), ("session_template_ids", 8, False),
    ):
        require(isinstance(value[key], list) and (not required or value[key]) and len(value[key]) <= limit, "Invalid " + key)
        result[key] = [native.guid(item) for item in value[key]]
        require(len(set(result[key])) == len(result[key]), "Duplicate " + key)
    for key in ("session_binding_reviewed", "trust_system_administrators"):
        require(type(value[key]) is bool, key + " must be a boolean.")
    require(result["runtime_user_id"] not in result["approved_actor_ids"], "The runtime application cannot be a pause requester.")
    require(not result["session_template_ids"] or result["session_binding_reviewed"],
            "An existing session-template delta needs explicit ownership/migration review.")
    return result


def owned(config, name):
    return str(uuid5(UUID(config["namespace_id"]), OWNER + ":" + name))


def solution_name(config):
    return "moacc_supervision_" + config["namespace_id"].split("-")[0]


def label(en, ar=None):
    return {"@odata.type": "Microsoft.Dynamics.CRM.Label", "LocalizedLabels": [
        {"@odata.type": "Microsoft.Dynamics.CRM.LocalizedLabel", "Label": en, "LanguageCode": 1033},
        {"@odata.type": "Microsoft.Dynamics.CRM.LocalizedLabel", "Label": ar or en, "LanguageCode": 1025},
    ]}


def schema():
    def attribute(name, kind, secured=False, **extra):
        return {
            "@odata.type": "Microsoft.Dynamics.CRM." + kind + "AttributeMetadata",
            "AttributeType": kind, "AttributeTypeName": {"Value": kind + "Type"},
            "SchemaName": name, "DisplayName": label(name), "IsSecured": secured,
            "RequiredLevel": {
                "Value": "None", "CanBeChanged": True,
                "ManagedPropertyLogicalName": "canmodifyrequirementlevelsettings",
            },
            **({"FormatName": {"Value": "Text"}} if kind == "String" else {}),
            **extra,
        }
    return {
        "@odata.type": "Microsoft.Dynamics.CRM.EntityMetadata",
        "SchemaName": "moacc_NativeAICommand", "EntitySetName": TABLE,
        "DisplayName": label("Native AI command", "طلب تحكم المساعد"),
        "DisplayCollectionName": label("Native AI commands", "طلبات تحكم المساعد"),
        "Description": label(OWNER), "OwnershipType": "UserOwned",
        "IsOptimisticConcurrencyEnabled": True,
        "IsActivity": False, "HasActivities": False, "HasNotes": False,
        "Attributes": [
            attribute("moacc_name", "String", IsPrimaryName=True, MaxLength=100,
                      Description=label("Name for the native AI control command.", "اسم طلب التحكم بالمساعد.")),
            attribute("moacc_conversationid", "String", True, MaxLength=36),
            attribute("moacc_action", "String", True, MaxLength=16),
            attribute("moacc_outcome", "String", True, MaxLength=32),
            attribute("moacc_reason", "String", True, MaxLength=64),
            attribute("moacc_ackrevision", "Integer", True, MinValue=0, MaxValue=2147483647),
            attribute("moacc_inflight", "Integer", True, MinValue=0, MaxValue=8),
            attribute("moacc_ackon", "DateTime", True, Format="DateAndTime"),
        ],
    }


def schema_requests():
    intended = schema()
    table = {key: copy.deepcopy(value) for key, value in intended.items()
             if key not in {"EntitySetName", "IsOptimisticConcurrencyEnabled", "Attributes"}}
    primary = copy.deepcopy(next(item for item in intended["Attributes"] if item.get("IsPrimaryName")))
    primary.pop("IsSecured", None)
    table["Attributes"] = [primary]
    return {
        "table": table,
        "columns": {item["SchemaName"].lower(): copy.deepcopy(item)
                    for item in intended["Attributes"] if not item.get("IsPrimaryName")},
    }


def schema_view(row):
    attrs = {item["LogicalName"]: item for item in row.get("Attributes", []) if item["LogicalName"].startswith("moacc_")}
    def labels(value):
        return {str(item["LanguageCode"]): item["Label"] for item in (value or {}).get("LocalizedLabels", [])}
    return {
        "EntitySetName": row.get("EntitySetName"), "OwnershipType": row.get("OwnershipType"),
        "IsOptimisticConcurrencyEnabled": row.get("IsOptimisticConcurrencyEnabled"),
        "labels": {name: labels(row.get(name)) for name in ("DisplayName", "DisplayCollectionName", "Description")},
        "columns": {name: {
            **{key: item.get(key) for key in ("AttributeType", "IsSecured", "IsPrimaryName", "MaxLength", "MinValue", "MaxValue", "Format")},
            "RequiredLevel": (item.get("RequiredLevel") or {}).get("Value"),
            "FormatName": (item.get("FormatName") or {}).get("Value"),
            "DisplayName": labels(item.get("DisplayName")),
            "Description": labels(item.get("Description")),
            "IsAuditEnabled": (item.get("IsAuditEnabled") or {}).get("Value"),
        } for name, item in attrs.items()},
    }


def check_schema(row, *, partial=False):
    expected = {
        item["SchemaName"].lower(): {
            "AttributeType": item["@odata.type"].split(".")[-1].removesuffix("AttributeMetadata"),
            "IsSecured": item["IsSecured"],
            "RequiredLevel": item["RequiredLevel"]["Value"],
            **{key: item[key] for key in ("IsPrimaryName", "MaxLength", "MinValue", "MaxValue", "Format") if key in item},
        } for item in schema()["Attributes"]
    }
    actual = schema_view(row)
    actual["columns"].pop(PRIMARY, None)
    require(actual["EntitySetName"] == TABLE and actual["OwnershipType"] == "UserOwned",
            "Native table readback has an unexpected entity-set name or ownership.")
    require(actual["IsOptimisticConcurrencyEnabled"] is True,
            "Native table readback must confirm optimistic concurrency; no automatic metadata update is allowed.")
    require("moacc_name" in actual["columns"] and set(actual["columns"]).issubset(expected)
            if partial else set(actual["columns"]) == set(expected),
            "Native table readback has unexpected or missing declared columns.")
    for name, columns in actual["columns"].items():
        require(all(columns.get(key) == value for key, value in expected[name].items()),
                "Native column definition differs from its reviewed contract: " + name)


def read_schema(api):
    query = {
        "$select": "MetadataId,EntitySetName,OwnershipType,IsOptimisticConcurrencyEnabled,DisplayName,DisplayCollectionName,Description",
        "$expand": "Attributes",
    }
    try:
        return api.request(
            f"EntityDefinitions(LogicalName='{ENTITY}')?" + urllib.parse.urlencode(query),
            headers={"Consistency": "Strong"},
        )
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return None
        raise


def optional(api, path, query=None):
    try:
        return api.get(path, query)
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return None
        raise


def matches(row, body):
    for key, value in body.items():
        if key.endswith("@odata.bind"):
            actual = row.get("_" + key.split("@", 1)[0].lower() + "_value")
            if actual != value.rsplit("(", 1)[-1].rstrip(")"):
                return False
        elif row.get(key) != value:
            return False
    return True


def etag(row):
    value = row.get("@odata.etag", "")
    require(isinstance(value, str) and re.fullmatch(r'W/"\d+"', value), "A native ETag is required; wildcard updates are forbidden.")
    return value


def check_generated_parameter(tab_receipt, row):
    tab = tab_receipt["after"]
    moment = lambda value: datetime.fromisoformat(value.replace("Z", "+00:00"))
    require(row.get("msdyn_name") == "webresourceName"
            and row.get("msdyn_uniquename") == tab["msdyn_uniquename"] + "_webresourceName"
            and row.get("_msdyn_applicationtabtemplateid_value") == tab["msdyn_applicationtabtemplateid"]
            and row.get("msdyn_value") is None
            and row.get("_createdby_value") == tab.get("_createdby_value")
            and bool(row.get("_createdby_value")),
            "The existing template parameter is not the unchanged native child of the owned tab.")
    require(moment(tab["createdon"]) <= moment(row["createdon"]) <= moment(tab_receipt["recorded_at"])
            and (moment(tab_receipt["recorded_at"]) - moment(tab["createdon"])).total_seconds() <= 60,
            "The template parameter was not generated during the recorded tab creation.")
    native.guid(row.get("msdyn_templateparameterid"))
    etag(row)


def approved_users(api, config):
    users = {}
    for identifier in [*config["approved_actor_ids"], config["runtime_user_id"]]:
        users[identifier] = api.get(f"systemusers({identifier})", {
            "$select": "systemuserid,isdisabled,applicationid,accessmode,islicensed,_businessunitid_value",
            "$expand": "systemuserroles_association($select=roleid,name)",
        })
        require(users[identifier].get("_businessunitid_value") == config["business_unit_id"],
                "This first slice requires the approved users and runtime in the configured business unit; never move users implicitly.")
    runtime = users[config["runtime_user_id"]]
    require(runtime.get("applicationid") == config["runtime_application_id"] and runtime.get("isdisabled") is False,
            "The configured runtime Dataverse application user does not match the deployed identity.")
    admins = []
    for identifier in config["approved_actor_ids"]:
        user = users[identifier]
        require(user.get("isdisabled") is False and not user.get("applicationid") and user.get("accessmode") == 0
                and user.get("islicensed") is True, "Approve only existing enabled, licensed interactive human users.")
        if any(role.get("name") == "System Administrator" for role in user.get("systemuserroles_association", [])):
            admins.append(identifier)
    require(not admins or config["trust_system_administrators"],
            "System Administrators bypass field security; explicitly acknowledge them as trusted infrastructure operators.")
    return admins


def build_plan(api, config):
    languages = api.get("RetrieveProvisionedLanguages()")["RetrieveProvisionedLanguages"]
    require({1033, 1025}.issubset(languages), "Provision English and Arabic through the existing workspace/language recipe first.")
    admins = approved_users(api, config)
    controllers = {}
    for identifier in config["controller_component_ids"]:
        record = api.get(f"botcomponents({identifier})", {
            "$select": "botcomponentid,componenttype,data,modifiedon,_parentbotid_value,schemaname",
        })
        require(record.get("componenttype") == 9, "Only explicitly selected native controller topics may be staged.")
        bot = api.get(f"bots({native.guid(record['_parentbotid_value'])})", {"$select": "publishedon"})
        require(bot.get("publishedon") and datetime.fromisoformat(record["modifiedon"].replace("Z", "+00:00"))
                <= datetime.fromisoformat(bot["publishedon"].replace("Z", "+00:00")),
                "Finish or discard existing unpublished native controller edits before planning supervision.")
        current = topics.load_topic(record["data"])
        require("native_supervision_hold" not in record["data"],
                "A supervision controller already exists. Reuse its original receipt, not a new adoption plan.")
        desired = native.dump(topics.add_supervision(current))
        controllers[identifier] = {
            "before": record, "after_data": desired, "after_sha256": digest(desired),
        }
    for identifier in config["workstream_ids"]:
        stream = api.get(f"msdyn_liveworkstreams({identifier})", {
            "$select": "statecode,msdyn_direction,msdyn_streamsource,_msdyn_defaultqueue_value,_msdyn_bot_user_value",
        })
        require(stream.get("statecode") == 0 and stream.get("msdyn_direction") == 0 and stream.get("msdyn_streamsource") == 192440000
                and stream.get("_msdyn_defaultqueue_value") and stream.get("_msdyn_bot_user_value"), "Select configured inbound native voice workstreams.")
    templates = {}
    for identifier in config["session_template_ids"]:
        row = api.get(f"msdyn_sessiontemplates({identifier})", {"$select": "msdyn_uniquename,ismanaged"})
        require(row.get("ismanaged") is False, "Never modify a managed/default Microsoft session template.")
        templates[identifier] = {
            "identity": row, "tabs": api.rows(
                f"msdyn_sessiontemplates({identifier})/msdyn_sessiontemplate_applicationtab",
                {"$select": "msdyn_applicationtabtemplateid"},
            ),
        }
    require(read_schema(api) is None, "A command table already exists; use its owned receipt rather than adopting it.")
    targets = creation_targets(config)
    require(all(optional(api, path) is None for path in targets.values()), "An owned ID already exists; retain and reuse its original receipt.")
    return {
        "version": 1, "config": config, "config_hash": digest(config), "controllers": controllers,
        "templates": templates, "trusted_administrators": admins,
        "schema": schema(), "schema_requests": schema_requests(), "creation_targets": targets,
        "role_privileges": {audience: role_privileges(audience) for audience in ("requester", "runtime")},
        "source_hashes": {name: digest((WEBROOT / name).read_text()) for name in ("native-supervision.html", "native-supervision.js")},
        "negative_permission_acceptance_required": True,
    }


def upgrade_notice_source(source, origin):
    require(hashlib.sha256(topics.NOTICE_AUDIO.read_bytes()).hexdigest() == NOTICE_SHA256,
            "The reviewed startup-notice asset changed.")
    document = topics.load_topic(source)

    def starts(value):
        if isinstance(value, dict):
            if value.get("id") == "native_start":
                yield value
            for child in value.values():
                yield from starts(child)
        elif isinstance(value, list):
            for child in value:
                yield from starts(child)

    nodes = list(starts(document))
    require(len(nodes) == 1, "The startup-notice upgrade requires one exact native_start action.")
    node = nodes[0]
    require(node.get("kind") == "HttpRequestAction"
            and node.get("url") == origin + "/dynamics/native/sessions?wait_for_context=true"
            and all(re.search(r"\b" + flag + r"\s*:\s*" + value + r"\b", node.get("body", {}).get("content", ""))
                    for flag, value in (("play_startup_notice", "true"), ("speech_turn_control", "true"),
                                        ("supervision_contract", "1"))),
            "Upgrade only the opted-in, supervised native context-wait controller.")
    expected = topics.startup_notice_latency_settings(origin)
    audio = f'<audio src="{origin}/dynamics/native/startup-notice/{NOTICE_SHA256}.wav" />'
    require(expected["latencyMessage"] == {"kind": "Message", "text": [audio], "speak": [audio]},
            "The approved startup-notice audio contract changed.")
    legacy = copy.deepcopy(expected)
    legacy["latencyMessage"]["text"] = [LEGACY_NOTICE_TEXT]
    settings = node.get("latencyMessageSettings")
    require(settings in (legacy, expected), "Startup-notice text, speech or latency settings drifted.")
    if settings == expected:
        return source
    settings["latencyMessage"]["text"] = settings["latencyMessage"]["speak"].copy()
    return native.dump(document)


def upgrade_playback_source(source, origin):
    require(hashlib.sha256(topics.NOTICE_AUDIO.read_bytes()).hexdigest() == NOTICE_SHA256,
            "The reviewed startup-notice asset changed.")
    document = topics.load_topic(source)
    actions = document.get("beginDialog", {}).get("actions", [])
    starts = [node for node in actions if node.get("id") == "native_start"]
    notices = [node for node in actions if node.get("id") == "native_connecting_notice"]
    require(len(starts) == len(notices) == 1, "The playback upgrade needs the exact top-level startup actions.")
    start, notice = starts[0], notices[0]
    require(start.get("kind") == "HttpRequestAction" and notice.get("kind") == "SendActivity"
            and actions.index(notice) < actions.index(start)
            and all(re.search(r"\b" + flag + r"\s*:\s*" + value + r"\b", start.get("body", {}).get("content", ""))
                    for flag, value in (("play_startup_notice", "true"), ("speech_turn_control", "true"),
                                        ("supervision_contract", "1"))),
            "Upgrade only the opted-in supervised startup, preserving its original action order.")
    message = topics.startup_notice_message(origin)
    disabled = {"kind": "LatencyMessageSettings", "allowLatencyMessage": False}
    endpoint = origin + "/dynamics/native/sessions"
    if start.get("url") == endpoint:
        require(notice.get("activity") == message and start.get("latencyMessageSettings") == disabled,
                "The non-blocking startup playback contract drifted.")
        return source
    expected = topics.startup_notice_latency_settings(origin)
    legacy = copy.deepcopy(expected)
    legacy["latencyMessage"]["text"] = [LEGACY_NOTICE_TEXT]
    require(start.get("url") == endpoint + "?wait_for_context=true"
            and start.get("latencyMessageSettings") in (legacy, expected)
            and notice.get("activity") == {"speak": ['<break time="20ms" />']},
            "The playback upgrade refuses an unrelated notice, endpoint or latency definition.")
    notice["activity"] = message
    start["url"] = endpoint
    start["latencyMessageSettings"] = disabled
    return native.dump(document)


def upgrade_native_startup_source(source, origin, original):
    require(hashlib.sha256(topics.NOTICE_AUDIO.read_bytes()).hexdigest() == NOTICE_SHA256,
            "The reviewed startup-notice asset changed.")
    before = topics.load_topic(original)
    failure = before.get("beginDialog", {}).get("actions", [])[-3:]
    require([node.get("id") for node in failure] == [
        "native_report_failure", "native_connection_error", "native_end_failed_topic",
    ], "The original controller failure tail is not recognized.")
    document = topics.load_topic(source)
    require("native_supervision_hold" in source and "supervision_contract: 1" in source,
            "Native startup migration requires the owned supervised controller.")
    revised = topics.native_startup_controller(document, origin, failure_actions=failure)
    return source if revised == document else native.dump(revised)


def upgrade_application_startup_source(source, origin):
    require(hashlib.sha256(topics.NOTICE_AUDIO.read_bytes()).hexdigest() == NOTICE_SHA256,
            "The reviewed startup-notice asset changed.")
    require("native_supervision_check_startup_failure" in source and "supervision_contract: 1" in source,
            "Application startup migration requires the owned guarded supervision controller.")
    document = topics.load_topic(source)
    revised = topics.application_startup_controller(document, origin)
    return source if revised == document else native.dump(revised)


class Journal:
    def __init__(self, directory, plan):
        self.directory, self.plan = directory, plan
        self.events = []
        previous = digest(plan)
        for path in sorted(directory.glob("event-*.json")):
            require(len(self.events) < MAX_EVENTS, "Receipt event limit reached.")
            item = json.loads(native.read_private(path))
            stored = item.pop("hash")
            require(item["previous"] == previous and digest(item) == stored, "Receipt chain is damaged.")
            item["hash"] = stored
            self.events.append(item)
            previous = stored

    def append(self, kind, key, **values):
        require(len(self.events) + (2 if kind.endswith("intent") else 1) <= MAX_EVENTS, "Receipt event limit reached.")
        event = {
            "sequence": len(self.events) + 1, "previous": self.events[-1]["hash"] if self.events else digest(self.plan),
            "recorded_at": datetime.now(timezone.utc).isoformat(), "kind": kind, "key": key, **values,
        }
        event["hash"] = digest(event)
        native.write_private(self.directory / f"event-{event['sequence']:04d}.json", json.dumps(event, indent=2, ensure_ascii=False) + "\n")
        self.events.append(event)
        return event

    def last(self, kind, key):
        return next((row for row in reversed(self.events) if row["kind"] == kind and row["key"] == key), None)


class Setup:
    def __init__(self, api, plan, journal):
        self.api, self.plan, self.journal = api, plan, journal
        self.config = plan["config"]

    def trusted_intent(self, key):
        intent = self.journal.last("intent", key)
        rejected = self.journal.last("rejected", key)
        return intent if intent and (not rejected or rejected["sequence"] < intent["sequence"]) else None

    def write(self, key, path, **kwargs):
        if kwargs.get("method") == "POST" and (
            path in {"EntityDefinitions", "webresourceset"} or
            re.fullmatch(r"EntityDefinitions\([0-9a-f-]{36}\)/Attributes", path)
        ):
            kwargs["headers"] = {
                **(kwargs.get("headers") or {}), "MSCRM.SolutionUniqueName": solution_name(self.config),
            }
        try:
            return self.api.request(path, **kwargs)
        except urllib.error.HTTPError as error:
            details = http_failure(error, kwargs.get("method", "GET"), path)
            # A definite rejection is not an ambiguous successful write.
            definite = 400 <= error.code < 500 and error.code not in {408, 429}
            event = record_failure(self.journal, "rejected" if definite else "request_failed", key, details)
            raise RequestFailed(error, details, event) from None

    def read_row(self, path):
        if path.startswith("webresourceset("):
            draft = optional(self.api, path + "/Microsoft.Dynamics.CRM.RetrieveUnpublished")
            if draft is not None:
                return draft
        return optional(self.api, path)

    def assert_idle_disabled(self):
        request = urllib.request.Request(self.config["voice_origin"] + "/health", headers={"Accept": "application/json"})
        with urllib.request.urlopen(request, timeout=15) as response:
            health = json.load(response)
        require(health.get("native_media_pilot", {}).get("supervision_enabled") is False,
                "Deploy with supervision disabled and verify health before native changes or rollback.")
        for workstream in self.config["workstream_ids"]:
            require(not self.api.rows("msdyn_ocliveworkitems", {
                "$select": "msdyn_ocliveworkitemid",
                "$filter": f"_msdyn_liveworkstreamid_value eq {workstream} and statuscode ne 4", "$top": "1",
            }), "Finish native calls and wrap-up before changing supervision configuration.")
        return health["native_media_pilot"]

    def assert_imports_clear(self):
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat().replace("+00:00", "Z")
        document = self.api.get("importjobs", {
            "$select": "importjobid,completedon,progress",
            "$filter": "createdon ge " + cutoff, "$top": "50",
        })
        require(isinstance(document.get("value"), list) and "@odata.nextLink" not in document
                and all(row.get("completedon") is not None for row in document["value"]),
                "Active or unbounded import status prevents rejected-schema reconciliation/cleanup.")

    def protected_readback(self):
        spec_text = native.read_private(self.journal.directory / "protected-read-spec.json")
        baseline_text = native.read_private(self.journal.directory / "protected-before.json")
        spec, baseline = json.loads(spec_text), json.loads(baseline_text)
        require(isinstance(spec, list) and 0 < len(spec) <= 128 and isinstance(baseline, dict)
                and len({item["key"] for item in spec}) == len(spec) == len(baseline),
                "The protected native read set is missing or ambiguous.")
        def clean(value):
            if isinstance(value, dict):
                return {key: clean(child) for key, child in value.items() if not key.startswith("@") and "@" not in key}
            if isinstance(value, list):
                return sorted((clean(child) for child in value), key=canonical)
            return value
        current = {}
        for item in spec:
            require(item.get("mode") in {"get", "rows"} and isinstance(item.get("path"), str),
                    "Protected proof permits reads only.")
            value = self.api.rows(item["path"], item.get("query") or {}) if item["mode"] == "rows" else self.api.get(item["path"], item.get("query"))
            current[item["key"]] = clean(value)
        require(current == baseline, "Protected controller, publication, queue/profile or binding state changed.")
        roles = json.loads(native.read_private(self.journal.directory / "roles-before.json"))
        for user in [*self.config["approved_actor_ids"], self.config["runtime_user_id"]]:
            actual = self.api.rows(f"systemusers({user})/systemuserroles_association", {"$select": "roleid,name"})
            require(sorted(row["roleid"] for row in actual) == sorted(row["roleid"] for row in roles[user]),
                    "Existing user roles changed; reject narrow cleanup.")
        return {"count": len(spec), "spec_sha256": digest(spec_text), "baseline_sha256": digest(baseline_text),
                "readback_sha256": digest(current), "existing_roles_unchanged": True}

    def rejected_schema_request(self, request_file, response_file):
        require("schema_requests" not in self.plan, "This resolver applies only to the rejected legacy one-step plan.")
        request_file, response_file = Path(request_file).resolve(), Path(response_file).resolve()
        directory = self.journal.directory.resolve()
        require(request_file.is_relative_to(directory) and response_file.is_relative_to(directory)
                and request_file.name.endswith("-intent.json")
                and response_file == request_file.with_name(request_file.name.removesuffix("-intent.json") + "-result.json"),
                "Pin the paired original private request/result receipts.")
        request_text, response_text = native.read_private(request_file), native.read_private(response_file)
        request, response = json.loads(request_text), json.loads(response_text)
        intent, rejection = self.journal.last("intent", "schema"), self.journal.last("rejected", "schema")
        require(intent and rejection and rejection.get("status") == 400
                and rejection["sequence"] > intent["sequence"] and not self.journal.last("done", "schema")
                and intent.get("operation") == "metadata" and intent.get("before") is None
                and intent.get("body") == self.plan["schema"],
                "Only the actual recorded, definitely rejected legacy schema intent can be resolved.")
        require(request.get("method") == "POST" and request.get("path") == "EntityDefinitions"
                and request.get("body") == intent["body"]
                and request.get("headers", {}).get("MSCRM.SolutionUniqueName") == solution_name(self.config),
                "Request receipt differs from the original schema intent.")
        moment = lambda value: datetime.fromisoformat(value.replace("Z", "+00:00"))
        require(moment(intent["recorded_at"]) <= moment(request["at"]) <= moment(rejection["recorded_at"]),
                "The request timestamp does not belong to the recorded rejection interval.")
        error = json.loads(response.get("body", "{}")).get("error", {})
        require(response.get("http_status") == 400 and error.get("code") == "0x80040216",
                "Absence cannot resolve unknown, timeout, throttled, 5xx or a different rejection outcome.")
        return {
            "intent_sequence": intent["sequence"], "intent_hash": intent["hash"],
            "request_body_sha256": digest(intent["body"]),
            "rejection_sequence": rejection["sequence"], "rejection_hash": rejection["hash"],
            "request_file": str(request_file.relative_to(directory)),
            "response_file": str(response_file.relative_to(directory)),
            "request_sha256": digest(request_text), "response_sha256": digest(response_text),
            "http_status": 400, "native_code": "0x80040216",
        }

    def rejected_schema_live_proof(self, *, resuming=False):
        self.assert_imports_clear()
        self.assert_idle_disabled()
        require(read_schema(self.api) is None, "A table exists; never classify this schema intent as unapplied.")
        completed = {event["key"] for event in self.journal.events if event["kind"] == "done"}
        intents = {event["key"] for event in self.journal.events if event["kind"] == "intent"}
        require(completed == {"publisher", "solution"} and intents == {"publisher", "solution", "schema"},
                "This reconciliation may cover only the two owned empty records and one rejected schema intent.")
        for key, path in self.plan["creation_targets"].items():
            if key not in {"publisher", "solution"}:
                require(self.read_row(path) is None, "Another namespace-owned object exists; narrow cleanup must stop.")
        objects = {}
        for key in ("publisher", "solution"):
            event = self.journal.last("done", key)
            current = self.read_row(event["path"])
            if current is None:
                require(resuming and self.journal.last("rollback_intent", key),
                        "A missing owned object lacks its recorded cleanup intent.")
            else:
                require(matches(current, event["body"]), "Owned object drift prevents narrow cleanup: " + key)
                etag(current)
            objects[key] = {"path": event["path"], "row": current}
        components = self.api.rows("solutioncomponents", {
            "$select": "objectid,componenttype", "$filter": f"_solutionid_value eq {owned(self.config, 'solution')}",
        })
        require(not components, "The owned solution is not empty.")
        references = self.api.rows("solutions", {
            "$select": "solutionid,uniquename", "$filter": f"_publisherid_value eq {owned(self.config, 'publisher')}",
        })
        expected = {owned(self.config, "solution")} if objects["solution"]["row"] is not None else set()
        require({row["solutionid"] for row in references} == expected, "Foreign publisher references prevent narrow cleanup.")
        protected = self.protected_readback()
        self.assert_imports_clear()
        self.assert_idle_disabled()
        require(read_schema(self.api) is None, "Metadata appeared during reconciliation.")
        return {"metadata_absent": True, "owned_objects": objects, "solution_components": components,
                "publisher_references": references, "protected": protected, "imports_clear": True,
                "calls_idle": True, "supervision_disabled": True}

    def reconcile_rejected_schema(self, request_file, response_file, approval):
        require(isinstance(approval, str) and 10 <= len(approval.strip()) <= 200
                and "\n" not in approval and "\r" not in approval, "An explicit operator approval reference is required.")
        evidence = self.rejected_schema_request(request_file, response_file)
        require(not self.journal.last("rejected_unapplied_resolved", "schema"),
                "This rejection already has immutable resolution evidence; use guarded rollback, not another resolution.")
        proof = self.rejected_schema_live_proof()
        operator = self.api.get("WhoAmI")
        event = self.journal.append("rejected_unapplied_resolved", "schema", approval_reference=approval,
                                    operator=operator, evidence=evidence, proof=proof)
        return {"status": "rejected_unapplied_resolved", "native_changes": False, "table_created": False,
                "journal_sequence": event["sequence"], "journal_hash": event["hash"]}

    def validate_rejected_resolution(self):
        resolved = self.journal.last("rejected_unapplied_resolved", "schema")
        require(resolved, "A reviewed immutable rejection resolution is required.")
        expected = resolved["evidence"]
        actual = self.rejected_schema_request(self.journal.directory / expected["request_file"],
                                             self.journal.directory / expected["response_file"])
        require(actual == expected, "Original rejection evidence changed after resolution.")
        proof = self.rejected_schema_live_proof(resuming=True)
        require(proof["protected"] == resolved["proof"]["protected"], "Protected proof differs from its approved resolution.")
        return proof

    def row(self, key, table, primary, body):
        identifier = owned(self.config, key)
        path = f"{table}({identifier})"
        desired = {primary: identifier, **body}
        current = self.read_row(path)
        intent = self.trusted_intent(key)
        if current:
            require(intent and matches(current, desired), "Owned record differs or predates this receipt: " + key)
        else:
            require(not self.journal.last("done", key), "An owned record was deleted outside this receipt.")
            if not intent:
                self.journal.append("intent", key, operation="create", path=path, table=table, body=desired, before=None)
            else:
                require(intent["body"] == desired, "Create payload changed after its private intent.")
            self.write(key, table, method="POST", body=desired)
            current = self.read_row(path)
            require(current is not None, "Create readback is missing: " + key)
            require(matches(current, desired), "Create readback differed: " + key)
        if not self.journal.last("done", key):
            self.journal.append("done", key, path=path, body=desired, after=current)
        return identifier

    def association(self, key, source, relation, table, identifier, primary):
        path = source + "/" + relation
        rows = self.api.rows(path, {"$select": primary, "$filter": f"{primary} eq {identifier}"})
        if rows:
            require(len(rows) == 1 and self.trusted_intent(key), "Association exists outside the owned receipt: " + key)
        else:
            require(not self.journal.last("done", key), "An owned association was removed outside the receipt.")
            if not self.trusted_intent(key):
                self.journal.append("intent", key, operation="associate", path=path, target=identifier, primary=primary, before=[])
            self.write(key, path + "/$ref", method="POST",
                       body={"@odata.id": self.api.org + f"/api/data/v9.2/{table}({identifier})"})
            require(len(self.api.rows(path, {"$select": primary, "$filter": f"{primary} eq {identifier}"})) == 1,
                    "Association readback failed.")
        if not self.journal.last("done", key):
            self.journal.append("done", key, path=path, target=identifier, primary=primary)

    def schema(self):
        require(self.plan["schema"] == schema(), "The schema recipe changed after plan creation.")
        requests = schema_requests()
        require(self.plan.get("schema_requests") == requests,
                "Use a fresh reviewed plan for documented table-then-column provisioning; never edit an old plan.")
        publisher = self.row("publisher", "publishers", "publisherid", {
            "uniquename": solution_name(self.config) + "_publisher", "friendlyname": OWNER,
            "description": OWNER, "customizationprefix": "moacc",
            "customizationoptionvalueprefix": 10000 + UUID(self.config["namespace_id"]).int % 90000,
        })
        self.row("solution", "solutions", "solutionid", {
            "uniquename": solution_name(self.config), "friendlyname": OWNER,
            "description": OWNER, "version": "1.0.0.0",
            "publisherid@odata.bind": f"/publishers({publisher})",
        })
        row = read_schema(self.api)
        if row:
            require(self.trusted_intent("schema_table"), "Refuse to adopt an existing custom table.")
            check_schema(row, partial=True)
        else:
            require(not self.journal.last("done", "schema_table"), "The owned command table was removed outside the receipt.")
            if not self.trusted_intent("schema_table"):
                self.journal.append("intent", "schema_table", operation="metadata", before=None, body=requests["table"])
            else:
                require(self.trusted_intent("schema_table")["body"] == requests["table"], "Metadata payload changed after its intent.")
            self.write("schema_table", "EntityDefinitions", method="POST", body=requests["table"])
            row = read_schema(self.api)
            require(row, "Custom table creation is unconfirmed; preserve this receipt before retrying.")
            check_schema(row, partial=True)
        table_id = native.guid(row["MetadataId"])
        table_receipt = self.journal.last("done", "schema_table")
        require(not table_receipt or table_receipt["after"]["MetadataId"] == table_id,
                "The owned table identity changed; do not adopt a replacement.")
        if not table_receipt:
            require(self.trusted_intent("schema_table")["body"] == requests["table"], "The table create contract changed.")
            self.journal.append("done", "schema_table", after=row, fingerprint=schema_view(row))
        path = f"EntityDefinitions({table_id})/Attributes"
        for name, body in requests["columns"].items():
            key = "schema_column_" + name
            current = next((item for item in row["Attributes"] if item["LogicalName"] == name), None)
            intent, done = self.trusted_intent(key), self.journal.last("done", key)
            if current is None:
                require(not done, "An owned secured column was removed outside its receipt: " + name)
                if intent:
                    require(intent["path"] == path and intent["body"] == body, "A column request changed after its private intent.")
                else:
                    self.journal.append("intent", key, operation="metadata_attribute", path=path,
                                        parent_id=table_id, before=None, body=body)
                self.write(key, path, method="POST", body=body)
                # A successful metadata POST can precede the expanded parent's read cache.
                # Retry reads only, never the create operation, within this short bound.
                for attempt in range(5):
                    row = read_schema(self.api)
                    require(row and row["MetadataId"] == table_id, "Column creation returned a different or missing parent table.")
                    current = next((item for item in row["Attributes"] if item["LogicalName"] == name), None)
                    if current is not None:
                        break
                    if attempt < 4:
                        time.sleep(0.5)
                require(current is not None, "Column creation is unconfirmed after bounded readback: " + name)
            require(self.trusted_intent(key) and self.trusted_intent(key)["path"] == path
                    and self.trusted_intent(key)["body"] == body,
                    "Refuse to adopt a column without its trusted request: " + name)
            check_schema(row, partial=True)
            native.guid(current.get("MetadataId"))
            if done:
                require(done["after"].get("MetadataId") == current.get("MetadataId"),
                        "An owned column identity changed: " + name)
            else:
                self.journal.append("done", key, parent_id=table_id, after=current,
                                    fingerprint=schema_view(row)["columns"][name])
        check_schema(row)
        if not self.journal.last("done", "schema"):
            self.journal.append("done", "schema", after=row, fingerprint=schema_view(row))

    def access(self):
        approved_users(self.api, self.config)
        check_schema(read_schema(self.api) or {})
        for audience in ("requester", "runtime"):
            role = self.row("role_" + audience, "roles", "roleid", {
                "name": OWNER + " | " + audience,
                "businessunitid@odata.bind": f"/businessunits({self.config['business_unit_id']})",
            })
            wanted = role_privileges(audience)
            planned = self.plan["role_privileges"][audience]
            legacy = {key: value for key, value in wanted.items() if key not in {name for name, _ in METADATA_READS}}
            predecessors = tuple({**legacy, **{name: "Global" for name, _ in METADATA_READS[:count]}}
                                 for count in range(len(METADATA_READS)))
            require(planned == wanted or (
                audience == "runtime" and planned in predecessors
                and self.journal.last("done", METADATA_READS[-1][1])
            ), "The permission recipe changed; apply the explicit runtime-metadata upgrade before replaying a legacy plan.")
            privileges = self.api.rows("privileges", {
                "$select": "privilegeid,name", "$filter": " or ".join(f"name eq '{name}'" for name in wanted),
            })
            depths = {name.casefold(): depth for name, depth in wanted.items()}
            require(len(privileges) == len(depths)
                    and {row["name"].casefold() for row in privileges} == set(depths)
                    and len({row["privilegeid"] for row in privileges}) == len(depths),
                    "Required supervision privileges were not provisioned uniquely.")
            expected = {row["privilegeid"]: depths[row["name"].casefold()] for row in privileges}
            actual = self.role_privileges_without_defaults(audience, role, expected)
            require(all(actual[key] == expected[key] for key in actual), "The dedicated supervision role has an unexpected privilege depth.")
            missing = [{"PrivilegeId": key, "Depth": depth} for key, depth in expected.items() if key not in actual]
            grant_key = "privileges_" + audience
            if self.journal.last("done", grant_key):
                require(actual == expected, "Dedicated role privileges changed outside the receipt; do not silently regrant.")
            if missing:
                if not self.trusted_intent(grant_key):
                    self.journal.append("intent", grant_key, operation="privileges", role=role, before=actual, expected=expected)
                self.write(grant_key, f"roles({role})/Microsoft.Dynamics.CRM.AddPrivilegesRole", method="POST", body={"Privileges": missing})
            saved = self.role_privileges_without_defaults(audience, role, expected)
            require(saved == expected, "Exact dedicated role privileges were not confirmed.")
            if not self.journal.last("done", grant_key):
                self.journal.append("done", grant_key, role=role, expected=expected)
            profile = self.row("profile_" + audience, "fieldsecurityprofiles", "fieldsecurityprofileid", {
                "name": OWNER + " | " + audience, "description": OWNER,
            })
            for name in SECURED:
                self.row("permission_" + audience + "_" + name, "fieldpermissions", "fieldpermissionid", {
                    "entityname": ENTITY, "attributelogicalname": name, "canread": 4,
                    "cancreate": 4 if audience == "requester" and name in REQUESTS else 0,
                    "canupdate": 4 if audience == "runtime" and name in RESULTS else 0,
                    "fieldsecurityprofileid@odata.bind": f"/fieldsecurityprofiles({profile})",
                })
            members = self.config["approved_actor_ids"] if audience == "requester" else [self.config["runtime_user_id"]]
            for member in members:
                self.association("role_member_" + audience + "_" + member, f"systemusers({member})",
                                 "systemuserroles_association", "roles", role, "roleid")
                self.association("profile_member_" + audience + "_" + member, f"fieldsecurityprofiles({profile})",
                                 "systemuserprofiles_association", "systemusers", member, "systemuserid")

    def role_privileges_without_defaults(self, audience, role, expected):
        actual = {row["PrivilegeId"]: row["Depth"] for row in self.api.get(
            f"RetrieveRolePrivilegesRole(RoleId={role})")["RolePrivileges"]}
        extras = set(actual) - set(expected)
        if not extras:
            return actual
        require(not self.journal.last("done", "privileges_" + audience),
                "Dedicated role privilege drift after verification; do not silently repair it.")
        require(role == owned(self.config, "role_" + audience)
                and self.journal.last("done", "role_" + audience),
                "Default removal is restricted to this receipt's newly created dedicated role.")
        definitions = self.api.rows("privileges", {
            "$select": "privilegeid,name",
            "$filter": " or ".join("privilegeid eq " + value for value in sorted(extras)),
        })
        require(len(definitions) == len(extras)
                and {row["privilegeid"] for row in definitions} == extras
                and all(row["name"] in SEEDED_ROLE_DEFAULTS and actual[row["privilegeid"]] == "Global"
                        for row in definitions),
                "The dedicated supervision role has unreviewed unrelated privileges.")
        require(not self.api.rows(f"roles({role})/systemuserroles_association", {"$select": "systemuserid"})
                and not self.api.rows(f"roles({role})/teamroles_association", {"$select": "teamid"}),
                "Remove seeded defaults only before the dedicated role is assigned.")
        for definition in definitions:
            key = "seed_default_" + audience + "_" + definition["privilegeid"]
            self.journal.append("seed_default_removal_intent", key, role=role, privilege=definition, depth="Global")
            self.write(key, f"roles({role})/Microsoft.Dynamics.CRM.RemovePrivilegeRole", method="POST", body={
                "Privilege": {"@odata.type": "Microsoft.Dynamics.CRM.privilege",
                              "privilegeid": definition["privilegeid"]},
            })
            current = {row["PrivilegeId"]: row["Depth"] for row in self.api.get(
                f"RetrieveRolePrivilegesRole(RoleId={role})")["RolePrivileges"]}
            require(definition["privilegeid"] not in current
                    and all(actual.get(identifier) == depth for identifier, depth in current.items()),
                    "Seeded privilege removal was not confirmed or the role changed concurrently.")
            actual = current
            self.journal.append("seed_default_removed", key, role=role, privilege=definition)
        require(not set(actual) - set(expected), "The exact dedicated role still has unrelated privileges.")
        return actual

    def role_expectation(self, event):
        expected = event["expected"]
        for upgrade in self.journal.events:
            if (upgrade["kind"] == "done" and upgrade["key"] in {key for _, key in METADATA_READS}
                    and upgrade["sequence"] > event["sequence"] and upgrade["role"] == event["role"]):
                require(upgrade["before"] == expected, "The metadata-read upgrade does not extend the recorded runtime role.")
                expected = upgrade["expected"]
        return expected

    def upgrade_runtime_metadata(self):
        wanted = role_privileges("runtime")
        legacy = {key: value for key, value in wanted.items() if key not in {name for name, _ in METADATA_READS}}
        predecessors = tuple({**legacy, **{name: "Global" for name, _ in METADATA_READS[:count]}}
                             for count in range(len(METADATA_READS)))
        require(self.plan["role_privileges"]["runtime"] in predecessors,
                "This upgrade applies only to the recorded legacy runtime role missing metadata read.")
        for name, key in METADATA_READS:
            self.upgrade_metadata_privilege(name, key)
        grant = self.journal.last("done", "privileges_runtime")
        actual = {row["PrivilegeId"]: row["Depth"] for row in self.api.get(
            f"RetrieveRolePrivilegesRole(RoleId={grant['role']})")["RolePrivileges"]}
        require(actual == self.role_expectation(grant), "The completed runtime metadata-read upgrade drifted.")

    def upgrade_metadata_privilege(self, name, key):
        grant = self.journal.last("done", "privileges_runtime")
        require(grant and grant["role"] == owned(self.config, "role_runtime"),
                "The owned runtime role must be verified before an explicit upgrade.")
        role = grant["role"]
        definition = self.api.rows("privileges", {
            "$select": "privilegeid,name", "$filter": f"name eq '{name}'",
        })
        require(len(definition) == 1 and definition[0]["name"] == name,
                "The exact read-only metadata privilege is unavailable or ambiguous.")
        privilege = native.guid(definition[0]["privilegeid"])
        baseline = self.role_expectation(grant)
        if privilege in baseline:
            require(baseline[privilege] == "Global", "The metadata-read grant has an unexpected depth.")
            return
        expected = {**baseline, privilege: "Global"}
        state = lambda: {row["PrivilegeId"]: row["Depth"] for row in self.api.get(
            f"RetrieveRolePrivilegesRole(RoleId={role})")["RolePrivileges"]}
        actual = state()
        role_receipt = self.journal.last("done", "role_runtime")
        require(role_receipt and matches(self.api.get(f"roles({role})"), role_receipt["body"]),
                "The dedicated runtime role changed before its metadata upgrade.")
        members = self.api.rows(f"roles({role})/systemuserroles_association", {"$select": "systemuserid"})
        require({row["systemuserid"] for row in members} == {self.config["runtime_user_id"]}
                and not self.api.rows(f"roles({role})/teamroles_association", {"$select": "teamid"}),
                "The runtime role is shared outside its recorded application user.")
        intent = self.trusted_intent(key)
        if intent:
            require(intent["before"] == baseline and intent["expected"] == expected,
                    "The metadata upgrade differs from its immutable intent.")
        else:
            require(actual == baseline, "Preserve unexpected runtime privileges; do not rebase the upgrade.")
            self.journal.append("intent", key, operation="privilege_upgrade", role=role,
                                before=actual, expected=expected)
        require(all(actual.get(identifier) == depth for identifier, depth in baseline.items()),
                "An existing runtime grant changed during the metadata upgrade.")
        if privilege not in actual:
            require(actual == baseline, "Unexpected grants appeared before the metadata upgrade.")
            self.write(key, f"roles({role})/Microsoft.Dynamics.CRM.AddPrivilegesRole",
                       method="POST", body={"Privileges": [{"PrivilegeId": privilege, "Depth": "Global"}]})
            actual = state()
        extras = set(actual) - set(expected)
        definitions = self.api.rows("privileges", {
            "$select": "privilegeid,name",
            "$filter": " or ".join("privilegeid eq " + value for value in sorted(extras)),
        }) if extras else []
        require(len(definitions) == len(extras) and {row["privilegeid"] for row in definitions} == extras
                and all(row["name"] in SEEDED_ROLE_DEFAULTS and actual[row["privilegeid"]] == "Global"
                        for row in definitions)
                and all(actual.get(identifier) == depth for identifier, depth in expected.items()),
                "The metadata upgrade returned unexpected grants or depths.")
        for definition in definitions:
            self.journal.append("seed_default_removal_intent", key, role=role, privilege=definition, depth="Global")
            self.write(key, f"roles({role})/Microsoft.Dynamics.CRM.RemovePrivilegeRole", method="POST", body={
                "Privilege": {"@odata.type": "Microsoft.Dynamics.CRM.privilege", "privilegeid": definition["privilegeid"]},
            })
            self.journal.append("seed_default_removed", key, role=role, privilege=definition)
        require(state() == expected, "The exact metadata-read upgrade was not confirmed.")
        self.journal.append("done", key, role=role, before=baseline, expected=expected)

    def controller(self):
        for identifier, change in self.plan["controllers"].items():
            path, key = f"botcomponents({identifier})", "controller_" + identifier
            row = self.api.get(path, {"$select": "data,modifiedon,_parentbotid_value"})
            require(row.get("_parentbotid_value") == change["before"]["_parentbotid_value"], "Controller bot ownership changed.")
            expected = self.controller_expectation(identifier)
            if self.journal.last("done", key):
                require(row["data"] == expected["data"], "The completed controller definition drifted.")
            if row["data"] != expected["data"]:
                require(row["data"] == change["before"]["data"], "Controller changed since the private plan; refuse to overwrite.")
                if not self.trusted_intent(key):
                    self.journal.append("intent", key, operation="patch", path=path, before=change["before"], body=expected)
                self.write(key, path, method="PATCH", body=expected, headers={"If-Match": etag(row)})
                row = self.api.get(path, {"$select": "data,modifiedon,_parentbotid_value"})
            require(self.trusted_intent(key) and row["data"] == expected["data"], "Controller stage ownership/readback failed.")
            if not self.journal.last("done", key):
                self.journal.append("done", key, path=path, body={"data": row["data"]}, after=row)

    def owned_controller_change(self, identifier):
        change = self.plan["controllers"][identifier]
        path, key = f"botcomponents({identifier})", "controller_" + identifier
        intent, done = self.trusted_intent(key), self.journal.last("done", key)
        require(intent and done and intent["sequence"] < done["sequence"]
                and intent.get("operation") == "patch" and intent.get("path") == done.get("path") == path
                and intent.get("before") == change["before"]
                and intent.get("body") == done.get("body") == {"data": change["after_data"]}
                and digest(change["after_data"]) == change["after_sha256"],
                "The startup-notice upgrade requires the exact completed owned controller baseline.")
        return change, done

    def notice_upgrade_spec(self, identifier):
        change, done = self.owned_controller_change(identifier)
        source = upgrade_notice_source(change["after_data"], self.config["voice_origin"])
        return {
            "operation": "startup_notice_upgrade", "path": f"botcomponents({identifier})", "controller_receipt": done["hash"],
            "before_sha256": change["after_sha256"],
            "after_sha256": digest(source), "body": {"data": source},
        }

    def notice_controller_expectation(self, identifier, *, include_rolled_back=False):
        change = self.plan["controllers"][identifier]
        expected = {"data": change["after_data"]}
        key = "startup_notice_" + identifier
        done = self.journal.last("done", key)
        if done:
            spec, intent = self.notice_upgrade_spec(identifier), self.trusted_intent(key)
            require(intent and intent["sequence"] < done["sequence"]
                    and all(intent.get(field) == done.get(field) == value for field, value in spec.items())
                    and done.get("intent_hash") == intent["hash"]
                    and intent.get("before", {}).get("data") == expected["data"]
                    and intent.get("etag") == etag(intent["before"])
                    and done.get("after", {}).get("data") == spec["body"]["data"]
                    and done.get("etag") == etag(done["after"]),
                    "The startup-notice revision differs from its immutable owned intent.")
            if include_rolled_back or not self.journal.last("rolled_back", key):
                expected = spec["body"]
        return expected

    def playback_upgrade_spec(self, identifier):
        _, original = self.owned_controller_change(identifier)
        before = self.notice_controller_expectation(identifier, include_rolled_back=True)
        predecessor = self.journal.last("done", "startup_notice_" + identifier)
        if not predecessor:
            predecessor = original
        source = upgrade_playback_source(before["data"], self.config["voice_origin"])
        return {
            "operation": "startup_playback_upgrade", "path": f"botcomponents({identifier})",
            "controller_receipt": predecessor["hash"],
            "before_sha256": digest(before["data"]), "after_sha256": digest(source), "body": {"data": source},
        }

    def playback_controller_expectation(self, identifier, *, include_rolled_back=False):
        expected = self.notice_controller_expectation(identifier, include_rolled_back=include_rolled_back)
        key = "startup_playback_" + identifier
        done = self.journal.last("done", key)
        if done:
            spec, intent = self.playback_upgrade_spec(identifier), self.trusted_intent(key)
            require(intent and intent["sequence"] < done["sequence"]
                    and all(intent.get(field) == done.get(field) == value for field, value in spec.items())
                    and done.get("intent_hash") == intent["hash"]
                    and digest(intent.get("before", {}).get("data", "")) == spec["before_sha256"]
                    and intent.get("etag") == etag(intent["before"])
                    and done.get("after", {}).get("data") == spec["body"]["data"]
                    and done.get("etag") == etag(done["after"]),
                    "The startup-playback revision differs from its immutable owned intent.")
            if include_rolled_back or not self.journal.last("rolled_back", key):
                expected = spec["body"]
        return expected

    def native_startup_upgrade_spec(self, identifier):
        change, predecessor = self.owned_controller_change(identifier)
        before = self.playback_controller_expectation(identifier, include_rolled_back=True)
        for prefix in ("startup_notice_", "startup_playback_"):
            predecessor = self.journal.last("done", prefix + identifier) or predecessor
        source = upgrade_native_startup_source(before["data"], self.config["voice_origin"], change["before"]["data"])
        return {
            "operation": "native_startup_upgrade", "path": f"botcomponents({identifier})",
            "controller_receipt": predecessor["hash"],
            "before_sha256": digest(before["data"]), "after_sha256": digest(source), "body": {"data": source},
        }

    def native_startup_controller_expectation(self, identifier, *, include_rolled_back=False):
        expected = self.playback_controller_expectation(identifier, include_rolled_back=include_rolled_back)
        key = "native_startup_" + identifier
        done = self.journal.last("done", key)
        if done:
            spec, intent = self.native_startup_upgrade_spec(identifier), self.trusted_intent(key)
            require(intent and intent["sequence"] < done["sequence"]
                    and all(intent.get(field) == done.get(field) == value for field, value in spec.items())
                    and done.get("intent_hash") == intent["hash"]
                    and digest(intent.get("before", {}).get("data", "")) == spec["before_sha256"]
                    and intent.get("etag") == etag(intent["before"])
                    and done.get("after", {}).get("data") == spec["body"]["data"]
                    and done.get("etag") == etag(done["after"]),
                    "The native-startup revision differs from its immutable owned intent.")
            if include_rolled_back or not self.journal.last("rolled_back", key):
                expected = spec["body"]
        return expected

    def application_startup_upgrade_spec(self, identifier):
        _, predecessor = self.owned_controller_change(identifier)
        before = self.native_startup_controller_expectation(identifier, include_rolled_back=True)
        for prefix in ("startup_notice_", "startup_playback_", "native_startup_"):
            predecessor = self.journal.last("done", prefix + identifier) or predecessor
        source = upgrade_application_startup_source(before["data"], self.config["voice_origin"])
        return {
            "operation": "application_startup_upgrade", "path": f"botcomponents({identifier})",
            "controller_receipt": predecessor["hash"],
            "before_sha256": digest(before["data"]), "after_sha256": digest(source), "body": {"data": source},
        }

    def controller_expectation(self, identifier):
        expected = self.native_startup_controller_expectation(identifier)
        key = "application_startup_" + identifier
        done = self.journal.last("done", key)
        if done:
            spec, intent = self.application_startup_upgrade_spec(identifier), self.trusted_intent(key)
            require(intent and intent["sequence"] < done["sequence"]
                    and all(intent.get(field) == done.get(field) == value for field, value in spec.items())
                    and done.get("intent_hash") == intent["hash"]
                    and digest(intent.get("before", {}).get("data", "")) == spec["before_sha256"]
                    and intent.get("etag") == etag(intent["before"])
                    and done.get("after", {}).get("data") == spec["body"]["data"]
                    and done.get("etag") == etag(done["after"]),
                    "The application-startup revision differs from its immutable owned intent.")
            if not self.journal.last("rolled_back", key):
                expected = spec["body"]
        return expected

    def receipt_body(self, event):
        for identifier in self.plan["controllers"]:
            if event["key"] in {"controller_" + identifier, "startup_notice_" + identifier,
                                "startup_playback_" + identifier, "native_startup_" + identifier,
                                "application_startup_" + identifier}:
                return self.controller_expectation(identifier)
        return event["body"]

    def runtime_env_filename(self):
        for prefix, label in (
            ("application_startup_", "application-startup"),
            ("native_startup_", "native-startup"), ("startup_playback_", "startup-playback"),
            ("startup_notice_", "startup-notice"),
        ):
            revisions = [self.journal.last("done", prefix + identifier) for identifier in self.plan["controllers"]
                         if not self.journal.last("rolled_back", prefix + identifier)]
            sequence = max((event["sequence"] for event in revisions if event), default=None)
            if sequence:
                return f"supervision.runtime.{label}-{sequence:04d}.env"
        return "supervision.runtime.env"

    def upgrade_startup_notice(self, *, playback=False, native_owner=False, application_owner=False):
        require(sum((playback, native_owner, application_owner)) <= 1, "Select only one startup revision.")
        health = self.assert_idle_disabled()
        if application_owner:
            require(health.get("application_startup_preparation_supported") is True
                    and health.get("startup_failure_notice_supported") is True,
                    "Deploy the application-only prepared-start and guarded failure backend before this controller.")
        elif native_owner:
            require(health.get("native_startup_notice_supported") is True
                    and health.get("startup_failure_notice_supported") is True,
                    "Deploy the native-only startup and guarded failure backend before this controller.")
        self.require_unreleased()
        require(not any(event["kind"] in {"rollback_intent", "rolled_back"} for event in self.journal.events),
                "A rolling-back receipt cannot upgrade its startup notice.")
        pending = []
        prefix = (
            "application_startup_" if application_owner else "native_startup_" if native_owner
            else "startup_playback_" if playback else "startup_notice_"
        )
        for identifier, change in self.plan["controllers"].items():
            transform = (
                lambda source, origin: upgrade_native_startup_source(source, origin, change["before"]["data"])
            ) if native_owner else upgrade_playback_source if playback else upgrade_notice_source
            if application_owner:
                transform = upgrade_application_startup_source
            controller_key = "controller_" + identifier
            if (not self.journal.last("intent", controller_key) and not self.journal.last("done", controller_key)
                    and transform(change["after_data"], self.config["voice_origin"]) == change["after_data"]):
                row = self.api.get(f"botcomponents({identifier})", {"$select": "data,_parentbotid_value,componenttype"})
                require(digest(change["after_data"]) == change["after_sha256"]
                        and row["data"] == change["before"]["data"] and row.get("componenttype") == 9
                        and row.get("_parentbotid_value") == change["before"]["_parentbotid_value"],
                        "The unapplied audio-correct controller differs from its fresh plan.")
                continue
            if application_owner:
                spec = self.application_startup_upgrade_spec(identifier)
                before_data = self.native_startup_controller_expectation(identifier)["data"]
            elif native_owner:
                spec = self.native_startup_upgrade_spec(identifier)
                before_data = self.playback_controller_expectation(identifier)["data"]
            else:
                spec = self.playback_upgrade_spec(identifier) if playback else self.notice_upgrade_spec(identifier)
                before_data = self.notice_controller_expectation(identifier)["data"] if playback else change["after_data"]
            key = prefix + identifier
            row = self.api.get(spec["path"], {"$select": "data,modifiedon,_parentbotid_value,componenttype"})
            require(row.get("_parentbotid_value") == change["before"]["_parentbotid_value"]
                    and row.get("componenttype") == 9, "Controller bot ownership or type changed.")
            if self.journal.last("done", key) or spec["before_sha256"] == spec["after_sha256"]:
                require(matches(row, self.controller_expectation(identifier)), "The completed startup-notice controller drifted.")
                continue
            intent = self.trusted_intent(key)
            if intent:
                require(all(intent.get(field) == value for field, value in spec.items())
                        and intent.get("before", {}).get("data") == before_data
                        and intent.get("etag") == etag(intent["before"]),
                        "The startup-notice upgrade differs from its immutable intent.")
            else:
                require(row["data"] == before_data, "Controller changed before its owned startup-notice upgrade.")
            require(row["data"] in (before_data, spec["body"]["data"]),
                    "Preserve unrelated controller edits during startup-notice recovery.")
            pending.append((key, spec, row, intent))
        require(len(self.journal.events) + len(pending) * 2 <= MAX_EVENTS, "Reserve the startup-notice revision receipts before writes.")
        for key, spec, row, intent in pending:
            if not intent:
                intent = self.journal.append("intent", key, **spec, before=row, etag=etag(row))
            if row["data"] != spec["body"]["data"]:
                require(etag(row) == intent["etag"], "The startup-notice baseline ETag changed; do not rebase its intent.")
                self.write(key, spec["path"], method="PATCH", body=spec["body"], headers={"If-Match": intent["etag"]})
                row = self.api.get(spec["path"], {"$select": "data,modifiedon,_parentbotid_value,componenttype"})
            require(matches(row, spec["body"])
                    and row.get("_parentbotid_value") == intent["before"]["_parentbotid_value"]
                    and row.get("componenttype") == 9, "Startup-notice revision readback failed.")
            self.journal.append("done", key, **spec, intent_hash=intent["hash"], after=row, etag=etag(row))

    def upgrade_startup_playback(self):
        self.upgrade_startup_notice(playback=True)

    def upgrade_native_startup(self):
        self.upgrade_startup_notice(native_owner=True)

    def upgrade_application_startup(self):
        self.upgrade_startup_notice(application_owner=True)

    def rollback_native_startup(self):
        require(all(
            not self.journal.last("done", "application_startup_" + identifier)
            or self.journal.last("rolled_back", "application_startup_" + identifier)
            for identifier in self.plan["controllers"]
        ), "Restore the newer application-startup revision before its native-startup predecessor.")
        self._rollback_startup("native_startup_")

    def rollback_application_startup(self):
        self._rollback_startup("application_startup_")

    def _rollback_startup(self, prefix):
        self.assert_idle_disabled()
        self.require_unreleased()
        pending = []
        for identifier, change in self.plan["controllers"].items():
            key = prefix + identifier
            done, intent = self.journal.last("done", key), self.trusted_intent(key)
            require(done and intent, "Complete/reconcile the owned startup revision before restoring it.")
            self.controller_expectation(identifier)
            row = self.api.get(f"botcomponents({identifier})")
            require(row.get("_parentbotid_value") == change["before"]["_parentbotid_value"],
                    "Controller bot ownership changed.")
            if self.journal.last("rolled_back", key):
                require(row["data"] == intent["before"]["data"], "The restored controller changed.")
                continue
            restored = self.journal.last("rollback_intent", key) and row["data"] == intent["before"]["data"]
            require(row["data"] == done["body"]["data"] or restored, "Controller drift prevents narrow restoration.")
            pending.append((key, intent, row))
        require(len(self.journal.events) + len(pending) * 2 <= MAX_EVENTS, "Reserve restoration receipts before writes.")
        for key, intent, row in pending:
            if not self.journal.last("rollback_intent", key):
                self.journal.append("rollback_intent", key, operation=intent["operation"], path=intent["path"], before=row)
            if row["data"] != intent["before"]["data"]:
                self.api.request(intent["path"], method="PATCH", body={"data": intent["before"]["data"]},
                                 headers={"If-Match": etag(row)})
            require(self.api.get(intent["path"])["data"] == intent["before"]["data"], "Narrow controller restore was not confirmed.")
            self.journal.append("rolled_back", key)

    def webresource(self):
        for name, kind in (("native-supervision.html", 1), ("native-supervision.js", 3)):
            content = (WEBROOT / name).read_text()
            require(digest(content) == self.plan["source_hashes"][name], "Webresource source changed after plan creation.")
            self.row("resource_" + name, "webresourceset", "webresourceid", {
                "name": "moacc_/" + name, "displayname": "Pause AI | إيقاف المساعد مؤقتاً",
                "description": OWNER, "webresourcetype": kind,
                "content": base64.b64encode(content.encode()).decode(),
            })
        tab = self.row("tab", "msdyn_applicationtabtemplates", "msdyn_applicationtabtemplateid", {
            "msdyn_name": OWNER, "msdyn_uniquename": "moacc_native_supervision_pause",
            "msdyn_title": "Pause AI | إيقاف المساعد", "msdyn_description": OWNER,
            "msdyn_pagetype": 509180002, "msdyn_pinned": False,
        })
        self.configure_tab_parameter(tab)

    def configure_tab_parameter(self, tab):
        body = {"msdyn_name": "webresourceName", "msdyn_value": "moacc_/native-supervision.html",
                "msdyn_applicationtabtemplateid@odata.bind": f"/msdyn_applicationtabtemplates({tab})"}
        intent = self.trusted_intent("tab_parameter")
        generated = intent and intent.get("operation") == "generated_parameter_update"
        planned = self.read_row(f"msdyn_templateparameters({owned(self.config, 'tab_parameter')})")
        if planned is not None and not generated:
            self.row("tab_parameter", "msdyn_templateparameters", "msdyn_templateparameterid", {
                **body, "msdyn_uniquename": solution_name(self.config) + "_webresource_name",
            })
            return
        rows = self.api.rows("msdyn_templateparameters", {
            "$filter": f"_msdyn_applicationtabtemplateid_value eq {tab} and msdyn_name eq 'webresourceName'",
        })
        if not rows and not generated:
            self.row("tab_parameter", "msdyn_templateparameters", "msdyn_templateparameterid", {
                **body, "msdyn_uniquename": solution_name(self.config) + "_webresource_name",
            })
            return
        require(len(rows) == 1, "The native webresourceName parameter is missing or ambiguous.")
        row = rows[0]
        path = f"msdyn_templateparameters({native.guid(row['msdyn_templateparameterid'])})"
        tab_receipt = self.journal.last("done", "tab")
        if generated:
            require(intent["path"] == path and intent["body"] == body, "The recorded native parameter identity changed.")
            check_generated_parameter(tab_receipt, intent["before"])
        else:
            require(intent is None, "A different parameter creation is still unconfirmed.")
            check_generated_parameter(tab_receipt, row)
            self.journal.append("intent", "tab_parameter", operation="generated_parameter_update",
                                path=path, before=row, body=body, parent=tab)
        if row.get("msdyn_value") != body["msdyn_value"]:
            require(row.get("msdyn_value") is None, "The native parameter was edited outside this receipt.")
            self.write("tab_parameter", path, method="PATCH", body={"msdyn_value": body["msdyn_value"]},
                       headers={"If-Match": etag(row)})
            row = self.api.get(path)
        require(matches(row, body), "Native parameter update readback differed.")
        if not self.journal.last("done", "tab_parameter"):
            self.journal.append("done", "tab_parameter", path=path, body=body, after=row, parent=tab)

    def bind(self):
        self.require_unreleased()
        require(self.config["session_template_ids"] and self.config["session_binding_reviewed"],
                "No approved session-template delta. Review the workspace owner's receipt contract before binding.")
        tab = owned(self.config, "tab")
        require(self.journal.last("done", "tab"), "Create and verify the owned tab first.")
        for identifier, saved in self.plan["templates"].items():
            source = f"msdyn_sessiontemplates({identifier})"
            before = {row["msdyn_applicationtabtemplateid"] for row in saved["tabs"]}
            current = {row["msdyn_applicationtabtemplateid"] for row in self.api.rows(
                source + "/msdyn_sessiontemplate_applicationtab", {"$select": "msdyn_applicationtabtemplateid"},
            )}
            require(current in (before, before | {tab}), "Unrelated session tab associations changed.")
            self.association("session_" + identifier, source, "msdyn_sessiontemplate_applicationtab",
                             "msdyn_applicationtabtemplates", tab, "msdyn_applicationtabtemplateid")

    def require_unreleased(self):
        require(not any(event["kind"] in {"release_intent", "released"} for event in self.journal.events),
                "This receipt has begun releasing its bindings; bind/verify cannot reactivate it.")

    def release_specs(self):
        selected = self.config["session_template_ids"]
        require(selected and set(selected) == set(self.plan["templates"]),
                "Release requires the exact selected session templates and their captured baselines.")
        primary = "msdyn_applicationtabtemplateid"
        target = owned(self.config, "tab")
        specs = []
        for identifier in selected:
            baseline = [native.guid(row[primary]) for row in self.plan["templates"][identifier]["tabs"]]
            require(len(baseline) == len(set(baseline)) and target not in baseline,
                    "The captured baseline must be distinct and exclude the owned addon tab.")
            spec = {
                "key": "session_" + identifier,
                "path": f"msdyn_sessiontemplates({identifier})/msdyn_sessiontemplate_applicationtab",
                "target": target, "primary": primary,
                "before": sorted([*baseline, target]), "after": sorted(baseline),
            }
            binding = self.trusted_intent(spec["key"])
            done = self.journal.last("done", spec["key"])
            descriptor = {key: spec[key] for key in ("path", "target", "primary")}
            require(binding and done and binding.get("operation") == "associate"
                    and matches(binding, descriptor) and matches(done, descriptor)
                    and done["sequence"] > binding["sequence"],
                    "Release requires the original trusted binding intent and successful readback.")
            release = self.journal.last("release_intent", spec["key"])
            released = self.journal.last("released", spec["key"])
            evidence = {key: spec[key] for key in ("path", "target", "primary", "before", "after")}
            require(not release or (matches(release, evidence) and release["sequence"] > done["sequence"]),
                    "Release intent does not match the captured baseline and owned edge.")
            require(not released or (release and matches(released, evidence) and released["sequence"] > release["sequence"]),
                    "Released evidence is missing, out of order or inconsistent.")
            rolled_back = self.journal.last("rolled_back", spec["key"])
            require(not rolled_back or (release and rolled_back["sequence"] > release["sequence"]),
                    "A binding already rolled back cannot be adopted as a new release.")
            specs.append(spec)
        return specs

    def binding_membership(self, spec):
        rows = self.api.rows(spec["path"], {"$select": spec["primary"]})
        identifiers = [native.guid(row[spec["primary"]]) for row in rows]
        require(len(identifiers) == len(set(identifiers)), "Ambiguous session tab membership.")
        return sorted(identifiers)

    def check_release_state(self, spec, current):
        release = self.journal.last("release_intent", spec["key"])
        released = self.journal.last("released", spec["key"])
        rolled_back = self.journal.last("rolled_back", spec["key"])
        if released or rolled_back:
            require(current == spec["after"], "A released binding was revived or its baseline changed.")
        elif release:
            require(current in (spec["before"], spec["after"]), "The interrupted release has unrelated membership changes.")
        else:
            require(current == spec["before"], "A new release requires the exact captured baseline plus owned tab.")

    def unbind(self):
        self.assert_idle_disabled()
        specs = self.release_specs()
        needed = sum(
            0 if self.journal.last("released", spec["key"]) else
            1 if self.journal.last("release_intent", spec["key"]) else 2
            for spec in specs
        )
        require(len(self.journal.events) + needed <= MAX_EVENTS, "Reserve the complete release receipt before native writes.")
        for spec in specs:
            self.check_release_state(spec, self.binding_membership(spec))
        for spec in specs:
            current = self.binding_membership(spec)
            self.check_release_state(spec, current)
            if self.journal.last("released", spec["key"]):
                continue
            evidence = {key: spec[key] for key in ("path", "target", "primary", "before", "after")}
            if not self.journal.last("release_intent", spec["key"]):
                self.journal.append("release_intent", spec["key"], **evidence)
            current = self.binding_membership(spec)
            self.check_release_state(spec, current)
            if current == spec["before"]:
                self.api.request(spec["path"] + "(" + spec["target"] + ")/$ref", method="DELETE")
            after = self.binding_membership(spec)
            require(after == spec["after"], "Release readback did not restore the exact captured baseline.")
            self.journal.append("released", spec["key"], **{**evidence, "after": after})
        return self.verify_unbound()

    def verify_unbound(self):
        specs = self.release_specs()
        for spec in specs:
            require(self.journal.last("released", spec["key"]), "Every selected binding needs a completed release receipt.")
            require(self.binding_membership(spec) == spec["after"], "Live membership differs from the released baseline.")
        return {"status": "unbound", "bindings": specs}

    def publish(self):
        require(all(self.journal.last("done", key) for key in ("schema", "resource_native-supervision.html", "resource_native-supervision.js")),
                "Only verified owned schema and webresources may be published.")
        for name in self.plan["source_hashes"]:
            row = self.read_row(f"webresourceset({owned(self.config, 'resource_' + name)})")
            require(row is not None, "Owned webresource is missing.")
            require(digest(base64.b64decode(row["content"]).decode()) == self.plan["source_hashes"][name], "Owned publication content drifted.")
        xml = (
            "<importexportxml><entities><entity>" + ENTITY + "</entity></entities><webresources>" +
            "".join("<webresource>" + owned(self.config, "resource_" + name) + "</webresource>"
                    for name in self.plan["source_hashes"]) + "</webresources></importexportxml>"
        )
        if not self.journal.last("done", "publish"):
            self.journal.append("intent", "publish", operation="publication", body={"ParameterXml": xml})
            self.write("publish", "PublishXml", method="POST", body={"ParameterXml": xml})
            # Published content is read through the normal endpoint; no PublishAll or bot publish.
            for name in self.plan["source_hashes"]:
                row = self.api.get(f"webresourceset({owned(self.config, 'resource_' + name)})")
                require(row.get("componentstate") == 0 and digest(base64.b64decode(row["content"]).decode()) == self.plan["source_hashes"][name],
                        "Published resource readback differs or remains unpublished.")
            self.journal.append("done", "publish", body={"ParameterXml": xml})

    def verify(self):
        self.require_unreleased()
        approved_users(self.api, self.config)
        require(self.config["session_template_ids"] and all(
            self.journal.last("done", "session_" + identifier) for identifier in self.config["session_template_ids"]
        ), "Bind the owned control tab through an explicitly reviewed workspace/session migration before emitting enable configuration.")
        check_schema(read_schema(self.api) or {})
        for key in ("schema", "privileges_requester", "privileges_runtime", "tab", "publish"):
            require(self.journal.last("done", key), "Complete the owned setup before verification: " + key)
        # Re-read all row receipts and associations, without granting or repairing anything.
        for event in self.journal.events:
            if event["kind"] != "done":
                continue
            if "body" in event and "path" in event:
                current = self.read_row(event["path"])
                require(current is not None and matches(current, self.receipt_body(event)), "Owned configuration drifted: " + event["key"])
            if "expected" in event and "role" in event:
                actual = {row["PrivilegeId"]: row["Depth"] for row in self.api.get(f"RetrieveRolePrivilegesRole(RoleId={event['role']})")["RolePrivileges"]}
                require(actual == self.role_expectation(event), "Dedicated role privilege drift.")
            if "target" in event and "path" in event:
                require(len(self.api.rows(event["path"], {
                    "$select": event["primary"], "$filter": f"{event['primary']} eq {event['target']}",
                })) == 1, "An owned role, field-profile or session association is missing.")
        tab = self.api.get(f"msdyn_applicationtabtemplates({owned(self.config, 'tab')})")
        aggregate = json.loads(tab.get("msdyn_templateparameters") or "{}")
        parameter = aggregate.get("webresourceName")
        value = parameter.get("value") if isinstance(parameter, dict) else parameter
        require(value == "moacc_/native-supervision.html", "Native tab parameter aggregation is not ready.")
        for name in self.plan["source_hashes"]:
            published = self.api.get(f"webresourceset({owned(self.config, 'resource_' + name)})")
            require(published.get("componentstate") == 0 and digest(base64.b64decode(published["content"]).decode()) == self.plan["source_hashes"][name],
                    "The installed webresource has not been published as reviewed.")
        pins = []
        for identifier, change in self.plan["controllers"].items():
            topic = self.api.get(f"botcomponents({identifier})", {"$select": "data,modifiedon,_parentbotid_value"})
            expected = self.controller_expectation(identifier)
            require(matches(topic, expected) and topic.get("_parentbotid_value") == change["before"]["_parentbotid_value"],
                    "Controller definition or bot ownership drifted.")
            bot = self.api.get(f"bots({native.guid(topic['_parentbotid_value'])})", {"$select": "publishedon"})
            require(bot.get("publishedon") and datetime.fromisoformat(topic["modifiedon"].replace("Z", "+00:00"))
                    <= datetime.fromisoformat(bot["publishedon"].replace("Z", "+00:00")),
                    "Validate and publish this exact bot in Copilot Studio, then rerun verify. This kit never publishes bots.")
            pins.append({"id": identifier, "sha256": digest(expected["data"])})
        return {
            "DYNAMICS_NATIVE_SUPERVISION_ENABLED": "true",
            "DYNAMICS_NATIVE_SUPERVISION_ACTOR_IDS": ",".join(self.config["approved_actor_ids"]),
            "DYNAMICS_NATIVE_SUPERVISION_CONTROLLERS": canonical(pins),
        }

    def rollback(self):
        self.assert_idle_disabled()
        resolved = self.journal.last("rejected_unapplied_resolved", "schema")
        if resolved:
            self.validate_rejected_resolution()
        completed = {event["key"]: event for event in self.journal.events if event["kind"] == "done"}
        unresolved = [
            event["key"] for event in self.journal.events
            if event["kind"] == "intent" and event["key"] not in completed
            and not (resolved and event["key"] == "schema"
                     and event["hash"] == resolved["evidence"]["intent_hash"])
        ]
        require(not unresolved, "Recover the interrupted stage with the same receipt before rollback.")
        require("schema_table" not in completed or "schema" in completed,
                "Reconcile the complete documented schema before full owned rollback.")
        # Preflight all mutable row evidence before the first deletion/restoration.
        for key, event in completed.items():
            if self.journal.last("rolled_back", key):
                continue
            if "body" in event and "path" in event:
                row = self.read_row(event["path"])
                if row is not None:
                    original = self.journal.last("intent", key)
                    field = "msdyn_value" if original and original["operation"] == "generated_parameter_update" else "data"
                    restored = original and original["operation"] in {
                        "patch", "generated_parameter_update", "startup_notice_upgrade", "startup_playback_upgrade",
                        "native_startup_upgrade", "application_startup_upgrade",
                    } and row.get(field) == original["before"][field]
                    restoring_notice = False
                    for identifier, change in self.plan["controllers"].items():
                        if key in {"controller_" + identifier, "startup_notice_" + identifier,
                                   "startup_playback_" + identifier, "native_startup_" + identifier,
                                   "application_startup_" + identifier}:
                            require(row.get("_parentbotid_value") == change["before"]["_parentbotid_value"],
                                    "Rollback refused because controller bot ownership changed.")
                            restored = restored and self.journal.last("rollback_intent", key)
                            for prefix in ("startup_notice_", "startup_playback_", "native_startup_", "application_startup_"):
                                revision_key = prefix + identifier
                                revision = self.trusted_intent(revision_key)
                                restoring_notice = restoring_notice or (
                                    revision and self.journal.last("rollback_intent", revision_key)
                                    and row.get("data") == revision["before"]["data"])
                    require(matches(row, self.receipt_body(event)) or restored or restoring_notice,
                            "Rollback refused because owned data changed: " + key)
            if "expected" in event and "role" in event:
                if optional(self.api, f"roles({event['role']})") is not None:
                    actual = {row["PrivilegeId"]: row["Depth"] for row in self.api.get(
                        f"RetrieveRolePrivilegesRole(RoleId={event['role']})")["RolePrivileges"]}
                    require(actual == self.role_expectation(event), "Rollback refused because dedicated role privileges changed.")
        for audience in ("requester", "runtime"):
            expected_members = set(self.config["approved_actor_ids"] if audience == "requester" else [self.config["runtime_user_id"]])
            for kind, table, relation, primary in (
                ("role", "roles", "systemuserroles_association", "systemuserid"),
                ("profile", "fieldsecurityprofiles", "systemuserprofiles_association", "systemuserid"),
            ):
                key = kind + "_" + audience
                if key not in completed or self.journal.last("rolled_back", key):
                    continue
                if optional(self.api, f"{table}({owned(self.config, key)})") is None:
                    continue
                members = {row[primary] for row in self.api.rows(
                    f"{table}({owned(self.config, key)})/{relation}", {"$select": primary},
                )}
                require(members.issubset(expected_members), "Rollback refuses to remove a newly added external member.")
                team_relation = "teamroles_association" if kind == "role" else "teamprofiles_association"
                require(not self.api.rows(f"{table}({owned(self.config, key)})/{team_relation}", {"$select": "teamid"}),
                        "Rollback refuses a new team association.")
                if kind == "profile":
                    profile = owned(self.config, key)
                    permissions = self.api.rows("fieldpermissions", {
                        "$select": "fieldpermissionid", "$filter": f"_fieldsecurityprofileid_value eq {profile}",
                    })
                    expected = {owned(self.config, "permission_" + audience + "_" + name) for name in SECURED}
                    require({row["fieldpermissionid"] for row in permissions}.issubset(expected),
                            "Rollback refuses foreign column permissions.")
        if ("tab" in completed and not self.journal.last("rolled_back", "tab")
                and optional(self.api, f"msdyn_applicationtabtemplates({owned(self.config, 'tab')})") is not None):
            sessions = self.api.rows(
                f"msdyn_applicationtabtemplates({owned(self.config, 'tab')})/msdyn_sessiontemplate_applicationtab",
                {"$select": "msdyn_sessiontemplateid"},
            )
            require({row["msdyn_sessiontemplateid"] for row in sessions}.issubset(self.config["session_template_ids"]),
                    "Rollback refuses foreign session bindings.")
        if "schema" in completed and not self.journal.last("rolled_back", "schema"):
            row = read_schema(self.api)
            if row is not None:
                require(schema_view(row) == completed["schema"]["fingerprint"], "Rollback refused because metadata changed.")
                require(not self.api.rows(TABLE, {"$select": PRIMARY, "$top": "1"}),
                        "Command history exists. Archive under a separate approved retention action; rollback never deletes commands.")
        pending = [key for key in completed if not self.journal.last("rolled_back", key)]
        require(len(self.journal.events) + len(pending) * 3 <= MAX_EVENTS, "Reserve a complete rollback receipt before native writes.")
        for event in reversed(self.journal.events.copy()):
            if event["kind"] != "done" or self.journal.last("rolled_back", event["key"]):
                continue
            key = event["key"]
            intent = self.journal.last("intent", key)
            if not intent:
                continue
            operation = intent["operation"]
            if operation in {"privileges", "privilege_upgrade", "metadata_attribute"}:
                continue  # Their owned parent deletion must be confirmed first.
            if not self.journal.last("rollback_intent", key):
                before = (
                    self.read_row(intent["path"]) if operation in {
                        "create", "patch", "generated_parameter_update", "startup_notice_upgrade", "startup_playback_upgrade",
                        "native_startup_upgrade", "application_startup_upgrade",
                    }
                    else read_schema(self.api) if operation == "metadata"
                    else None
                )
                self.journal.append("rollback_intent", key, operation=operation, path=intent.get("path"), before=before)
            if operation == "associate":
                query = {"$select": intent["primary"], "$filter": f"{intent['primary']} eq {intent['target']}"}
                if self.api.rows(intent["path"], query):
                    self.api.request(intent["path"] + "(" + intent["target"] + ")/$ref", method="DELETE")
                    require(not self.api.rows(intent["path"], query), "Association removal was not confirmed.")
            elif operation == "create":
                if resolved:
                    require(key in {"publisher", "solution"}, "Resolved rejection cleanup cannot touch other records.")
                    self.assert_imports_clear()
                    self.assert_idle_disabled()
                    require(read_schema(self.api) is None, "Metadata appeared; stop pair cleanup.")
                row = self.read_row(intent["path"])
                if row is not None:
                    require(matches(row, event["body"]), "Rollback refused because owned data changed before deletion: " + key)
                    if key == "solution":
                        require(not self.api.rows("solutioncomponents", {
                            "$select": "objectid", "$filter": f"_solutionid_value eq {owned(self.config, 'solution')}",
                        }), "Keep the solution container: it still contains components outside the removed owned delta.")
                    if resolved and key == "publisher":
                        require(not self.api.rows("solutions", {
                            "$select": "solutionid", "$filter": f"_publisherid_value eq {owned(self.config, 'publisher')}",
                        }), "Publisher references remain; do not delete it.")
                    self.api.request(intent["path"], method="DELETE", headers={"If-Match": etag(row)})
                    require(self.read_row(intent["path"]) is None, "Owned record deletion was not confirmed.")
                if key in {"role_requester", "role_runtime"}:
                    grant = key.replace("role_", "privileges_", 1)
                    if grant in completed and not self.journal.last("rolled_back", grant):
                        self.journal.append("rolled_back", grant)
                    if key == "role_runtime":
                        for _, upgrade in METADATA_READS:
                            if upgrade in completed:
                                self.journal.append("rolled_back", upgrade)
            elif operation in {"patch", "generated_parameter_update", "startup_notice_upgrade",
                               "startup_playback_upgrade", "native_startup_upgrade", "application_startup_upgrade"}:
                row = self.api.get(intent["path"])
                field = "msdyn_value" if operation == "generated_parameter_update" else "data"
                if row[field] != intent["before"][field]:
                    require(matches(row, event["body"]), "Rollback refused because the owned record changed before restore.")
                    self.api.request(intent["path"], method="PATCH", body={field: intent["before"][field]}, headers={"If-Match": etag(row)})
                require(self.api.get(intent["path"])[field] == intent["before"][field], "Owned record rollback readback failed.")
            elif operation == "metadata":
                row = read_schema(self.api)
                if row is not None:
                    require(not self.api.rows(TABLE, {"$select": PRIMARY, "$top": "1"}), "Command history appeared; keep the table.")
                    self.api.request(f"EntityDefinitions({native.guid(row['MetadataId'])})", method="DELETE")
                    require(read_schema(self.api) is None, "Metadata rollback readback failed.")
                if key == "schema_table":
                    for child in ("schema", *("schema_column_" + name for name in REQUESTS + RESULTS)):
                        if child in completed and not self.journal.last("rolled_back", child):
                            self.journal.append("rolled_back", child)
            self.journal.append("rolled_back", key)
        if resolved:
            proof = self.validate_rejected_resolution()
            require(all(item["row"] is None for item in proof["owned_objects"].values()), "Owned pair cleanup is incomplete.")
            if not self.journal.last("rejected_unapplied_cleanup_verified", "schema"):
                self.journal.append("rejected_unapplied_cleanup_verified", "schema",
                                    resolution_hash=resolved["hash"], proof=proof)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", nargs="?", choices=STAGES, default="plan")
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm-organization")
    parser.add_argument("--request-receipt", type=Path)
    parser.add_argument("--response-receipt", type=Path)
    parser.add_argument("--approval-reference")
    args = parser.parse_args(argv)
    state = {}
    try:
        run(args, state)
    except RequestFailed:
        raise
    except urllib.error.HTTPError as error:
        details = http_failure(error)
        event = record_failure(state.get("journal"), "request_failed", args.stage, details)
        raise RequestFailed(error, details, event) from None


def run(args, state):
    config = validate_config(json.loads(native.read_private(args.config)))
    output = native.private_path(args.output_dir)
    if args.stage == "verify-unbound":
        require(output.is_dir() and (output / "plan.json").is_file(), "Verify-unbound requires the existing release receipt.")
    if output.exists():
        require(output.is_dir() and output.stat().st_mode & 0o077 == 0, "Restrict the private receipt directory to its owner.")
    else:
        output.mkdir(mode=0o700)
    api = native.Dataverse(config["organization_url"], config["tenant_id"])
    plan_path = output / "plan.json"
    if plan_path.exists():
        plan = json.loads(native.read_private(plan_path))
        require(plan.get("config_hash") == digest(config), "Configuration differs from the immutable private plan.")
    else:
        plan = build_plan(api, config)
        native.write_private(plan_path, json.dumps(plan, ensure_ascii=False, indent=2) + "\n")
    journal = state["journal"] = Journal(output, plan)
    require(not any(event["kind"] == "rolled_back" for event in journal.events) or args.stage in {
        "rollback", "verify", "verify-unbound", "rollback-native-startup", "rollback-application-startup",
    },
            "A rolled-back receipt cannot be reapplied. Create a new reviewed plan.")
    setup = Setup(api, plan, journal)
    if args.stage == "plan" or (not args.apply and args.stage not in {"verify", "verify-unbound"}):
        print(json.dumps({"stage": args.stage, "status": "plan_only", "native_changes": False,
                          "private_receipt": str(output), "session_binding_review_required": not bool(config["session_template_ids"])}))
        return
    if args.stage == "verify-unbound":
        print(json.dumps({**setup.verify_unbound(), "native_changes": False}))
        return
    if args.stage == "verify":
        values = setup.verify()
        destination = output / setup.runtime_env_filename()
        content = native.env_file(values)
        if destination.exists():
            require(native.read_private(destination) == content, "The verified runtime configuration changed; preserve its original receipt.")
        else:
            native.write_private(destination, content)
        print(json.dumps({"status": "configuration_verified", "native_changes": False, "runtime_deployed": False,
                          "live_ui_and_non_admin_permission_acceptance": "still_required", "private_runtime_env": str(destination)}))
        return
    require(args.confirm_organization == config["organization_url"], "Writes require --apply and --confirm-organization matching the pinned organization.")
    if args.stage == "reconcile-rejected-schema":
        require(args.request_receipt and args.response_receipt, "Provide the paired original request and response receipts.")
        print(json.dumps(setup.reconcile_rejected_schema(args.request_receipt, args.response_receipt, args.approval_reference)))
        return
    if args.stage == "unbind":
        print(json.dumps({**setup.unbind(), "runtime_deployed": False, "private_receipt": str(output)}))
        return
    if args.stage == "all":
        setup.require_unreleased()
    setup.assert_idle_disabled()
    if args.stage == "rollback":
        setup.rollback()
    elif args.stage == "upgrade-runtime-metadata":
        setup.upgrade_runtime_metadata()
    elif args.stage == "upgrade-startup-notice":
        setup.upgrade_startup_notice()
    elif args.stage == "upgrade-startup-playback":
        setup.upgrade_startup_playback()
    elif args.stage == "upgrade-native-startup":
        setup.upgrade_native_startup()
    elif args.stage == "rollback-native-startup":
        setup.rollback_native_startup()
    elif args.stage == "upgrade-application-startup":
        setup.upgrade_application_startup()
    elif args.stage == "rollback-application-startup":
        setup.rollback_application_startup()
    else:
        stages = ["schema", "access", "controller", "webresource", "publish"] if args.stage == "all" else [args.stage]
        for stage in stages:
            getattr(setup, stage)()
        if args.stage == "all" and config["session_template_ids"]:
            setup.bind()
    print(json.dumps({"status": "owned_stage_read_back", "stage": args.stage, "bot_published": False,
                      "runtime_deployed": False, "private_receipt": str(output)}))


def cli(argv=None):
    try:
        main(argv)
    except RequestFailed as error:
        details = {key: value for key, value in error.details.items() if key != "status"}
        print(json.dumps({"status": "failed", "http_status": error.code, **details,
                          "journal_event": error.journal_event, "next": FAILURE_NEXT_STEP}, ensure_ascii=False))
        raise SystemExit(1) from None


if __name__ == "__main__":
    cli()
