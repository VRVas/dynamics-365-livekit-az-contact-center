"""Plan or explicitly apply isolated CRM schema, identity and optional private generated records."""

import argparse
import copy
import hashlib
import json
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
from uuid import NAMESPACE_URL, UUID, uuid5

from native_voice_setup import (
    DEPTH, Dataverse, env_file, guid, organization, private_path, read_private, write_private,
)


VERSION = 1
PLUGIN = "Microsoft.Dynamics.OmnichannelPrime.Plugins"
PRIVILEGES = {
    "prvReadContact": "Local", "prvAppendToContact": "Local", "prvReadAccount": "Local",
    "prvCreateIncident": "Local", "prvReadIncident": "Local", "prvWriteIncident": "Local",
    "prvAppendIncident": "Local", "prvAppendToIncident": "Local",
    "prvCreateNote": "Local", "prvReadNote": "Local", "prvAppendNote": "Local",
    "prvReadActivity": "Local", "prvWriteActivity": "Local", "prvAppendActivity": "Local",
    "prvReadmsdyn_ocphonecallengagementctx": "Local",
    "prvReadmsdyn_liveworkstream": "Global", "prvReadQueue": "Global", "prvReadUser": "Local",
    "prvReadSubject": "Global", "prvReadAsyncOperation": "Basic", "prvReadPluginAssembly": "Global",
}
SHAREPOINT_DEFAULTS = {
    "prvReadSharePointDocument", "prvReadSharePointData",
    "prvWriteSharePointData", "prvCreateSharePointData",
}
# Some environments add these read-only dependencies when a role is created.
# They are not grants made by this kit, and must not be stripped by replacement.
PRESERVED_PLATFORM_READS = {
    "prvReadSdkMessage", "prvReadSdkMessageProcessingStep",
    "prvReadSdkMessageProcessingStepImage", "prvReadPluginType",
}
FIELDS = {
    "contact": {"moacc_actorid": 100, "moacc_demoenabled": None, "moacc_preferredlanguage": 2},
    "incident": {
        "moacc_requestkey": 64, "moacc_payloadhash": 64, "moacc_category": 32,
        "moacc_district": 120, "moacc_street": 160, "moacc_landmark": 200,
    },
}
KEYS = {
    "contact": ("moacc_contact_actorid", "moacc_actorid"),
    "incident": ("moacc_incident_requestkey", "moacc_requestkey"),
}
CATEGORIES = {"general"}
NATIVE_PRIVILEGES = {
    "prvReadmsdyn_ocphonecallengagementctx", "prvReadmsdyn_liveworkstream",
    "prvReadQueue", "prvReadPluginAssembly",
}
EMPTY_SEED = {"version": 1, "synthetic_only": True, "account": None, "contacts": [], "cases": []}
BINDINGS = {
    "ownerid": "_ownerid_value", "businessunitid": "_businessunitid_value",
    "parentcustomerid_account": "_parentcustomerid_value", "customerid_contact": "_customerid_value",
    "objectid_incident": "_objectid_value", "incidentid": "_incidentid_value",
}
PRIMARY = {
    "accounts": "accountid", "contacts": "contactid", "incidents": "incidentid",
    "annotations": "annotationid", "incidentresolutions": "activityid",
    "roles": "roleid", "systemusers": "systemuserid",
}


def compact(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def sha(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def fingerprint(value):
    return sha(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


def text(value, maximum, label):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum or "\x00" in value:
        raise ValueError(f"{label} must be a nonempty string of at most {maximum} characters.")
    if value != value.strip():
        raise ValueError(f"{label} must not have leading or trailing whitespace.")
    return value


def shape(value, required, optional=()):
    if not isinstance(value, dict) or set(value) - set(required) - set(optional) or set(required) - set(value):
        raise ValueError("Unexpected or missing configuration/seed properties; use the repository examples.")


def slug(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-z][a-z0-9-]{2,39}", value):
        raise ValueError("Stable keys must contain 3–40 lowercase letters, digits or hyphens.")
    return value


def validate_config(value):
    shape(value, {
        "version", "organization_url", "tenant_id", "business_unit_id", "deployment_key",
        "managed_identity",
    }, {"native_reader_client_id", "workstream_ids", "native_voice", "case_categories", "location_required"})
    if type(value["version"]) is not int or value["version"] != VERSION:
        raise ValueError("Unsupported configuration version.")
    result = copy.deepcopy(value)
    result["organization_url"] = organization(value["organization_url"])
    for key in ("tenant_id", "business_unit_id"):
        result[key] = guid(value[key])
    result.setdefault("workstream_ids", [])
    result.setdefault("native_reader_client_id", "")
    result.setdefault("native_voice", bool(result["workstream_ids"]))
    result.setdefault("case_categories", ["general"])
    result.setdefault("location_required", False)
    if type(result["native_voice"]) is not bool or type(result["location_required"]) is not bool:
        raise ValueError("native_voice and location_required must be explicit booleans.")
    categories = result["case_categories"]
    if (not isinstance(categories, list) or not 1 <= len(categories) <= 30
            or any(not isinstance(item, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", item) for item in categories)
            or len(set(categories)) != len(categories)):
        raise ValueError("Supply the deployment's distinct supported case category identifiers.")
    if result["native_voice"]:
        result["native_reader_client_id"] = guid(result["native_reader_client_id"])
    elif result["native_reader_client_id"] or result["workstream_ids"]:
        raise ValueError("CRM-only setup must not configure native reader identities or workstreams.")
    slug(value["deployment_key"])
    identity = result["managed_identity"]
    shape(identity, {"resource_id", "client_id", "principal_id"})
    identity["client_id"], identity["principal_id"] = guid(identity["client_id"]), guid(identity["principal_id"])
    resource = identity["resource_id"]
    match = re.fullmatch(
        r"/subscriptions/([^/]+)/resourceGroups/([A-Za-z0-9_.()-]+)/providers/"
        r"Microsoft\.ManagedIdentity/userAssignedIdentities/([A-Za-z0-9_-]+)", resource or "",
    ) if isinstance(resource, str) else None
    if not match:
        raise ValueError("Use the existing Bicep-provisioned user-assigned managed identity resource ID.")
    guid(match[1])
    identities = [identity["client_id"], identity["principal_id"]]
    if result["native_voice"]:
        identities.append(result["native_reader_client_id"])
    if len(set(identities)) != len(identities):
        raise ValueError("The CRM client, its principal object and the native reader client must be distinct.")
    workstreams = result["workstream_ids"]
    if not isinstance(workstreams, list) or not (1 if result["native_voice"] else 0) <= len(workstreams) <= 10:
        raise ValueError("Native voice needs 1–10 explicit workstreams; CRM-only setup uses an empty list.")
    result["workstream_ids"] = [guid(item) for item in workstreams]
    if len(set(result["workstream_ids"])) != len(workstreams):
        raise ValueError("Workstream IDs must be unique.")
    return result


def required_privileges(config):
    return {name: depth for name, depth in PRIVILEGES.items()
            if config["native_voice"] or name not in NATIVE_PRIVILEGES}


def actor_id(phone):
    if not isinstance(phone, str) or not re.fullmatch(r"\+[1-9][0-9]{6,14}", phone):
        raise ValueError("Synthetic profile phones must already be normalized E.164 values.")
    return "profile:" + sha(phone)


def validate_seed(value, *, categories=None, location_required=False):
    shape(value, {"version", "synthetic_only", "account", "contacts", "cases"})
    if type(value["version"]) is not int or value["version"] != VERSION or value["synthetic_only"] is not True:
        raise ValueError("Only an explicitly synthetic version-1 seed is supported.")
    if value["account"] is None:
        if value["contacts"] != [] or value["cases"] != []:
            raise ValueError("An empty seed must not contain contacts or cases.")
        return copy.deepcopy(value)
    categories = CATEGORIES if categories is None else set(categories)
    account = value["account"]
    shape(account, {"key", "name", "description"})
    slug(account["key"])
    text(account["name"], 160, "account.name")
    text(account["description"], 1800, "account.description")
    contacts, cases = value["contacts"], value["cases"]
    if not isinstance(contacts, list) or not 1 <= len(contacts) <= 10:
        raise ValueError("Supply 1–10 synthetic contacts.")
    if not isinstance(cases, list) or not 0 <= len(cases) <= 30:
        raise ValueError("Supply at most 30 generated cases.")
    contact_keys, phones, case_keys = set(), set(), set()
    for contact in contacts:
        shape(contact, {
            "key", "synthetic", "firstname", "lastname", "phone", "email",
            "preferred_language", "description",
        })
        slug(contact["key"])
        if contact["synthetic"] is not True:
            raise ValueError("Every contact must explicitly attest synthetic eligibility.")
        actor_id(contact["phone"])
        if contact["key"] in contact_keys or contact["phone"] in phones:
            raise ValueError("Contact keys and profile phones must be unique.")
        contact_keys.add(contact["key"])
        phones.add(contact["phone"])
        for key in ("firstname", "lastname"):
            text(contact[key], 50, "contact." + key)
        text(contact["description"], 1800, "contact.description")
        if contact["preferred_language"] not in ("ar", "en"):
            raise ValueError("Preferred language must be ar or en, never a Dataverse choice integer.")
        email = contact["email"]
        if email is not None and (
            not isinstance(email, str) or len(email) > 100 or not re.fullmatch(r"[^\s@<>]+@[^\s@<>]+\.[^\s@<>]+", email)
        ):
            raise ValueError("Synthetic email must be null or a valid nonpersonal test address.")
    for case in cases:
        shape(case, {
            "key", "contact_key", "category", "title", "description", "location",
            "priority", "state", "notes",
        }, {"resolution"})
        slug(case["key"])
        if case["key"] in case_keys or not isinstance(case["contact_key"], str) or case["contact_key"] not in contact_keys:
            raise ValueError("Case keys must be unique and reference a seed contact.")
        case_keys.add(case["key"])
        if not isinstance(case["category"], str) or case["category"] not in categories:
            raise ValueError("Use a service category supported by the deployment profile.")
        text(case["title"], 200, "case.title")
        text(case["description"], 1800, "case.description")
        shape(case["location"], {"district", "street", "landmark"})
        for key, maximum in (("district", 120), ("street", 160), ("landmark", 200)):
            val = case["location"][key]
            if not isinstance(val, str) or len(val) > maximum or val != val.strip() or "\x00" in val:
                raise ValueError("Invalid synthetic location.")
        if location_required and not (case["location"]["district"] and (case["location"]["street"] or case["location"]["landmark"])):
            raise ValueError("This deployment requires a district and street or landmark.")
        if type(case["priority"]) is not int or case["priority"] not in (1, 2, 3):
            raise ValueError("Case priority must be 1, 2 or 3.")
        if case["state"] not in ("active", "resolved"):
            raise ValueError("Seed state must be active or resolved.")
        if case["state"] == "resolved":
            text(case.get("resolution"), 1800, "case.resolution")
        elif "resolution" in case:
            raise ValueError("An active case must not specify a resolution.")
        if not isinstance(case["notes"], list) or not 0 <= len(case["notes"]) <= 10:
            raise ValueError("Supply at most ten generated customer updates per case.")
        note_keys = set()
        for note in case["notes"]:
            shape(note, {"key", "text"})
            slug(note["key"])
            text(note["text"], 1800, "note.text")
            if note["key"] in note_keys:
                raise ValueError("Note keys must be unique within a case.")
            note_keys.add(note["key"])
    return copy.deepcopy(value)


def operation_guid(key):
    value = sha(key)
    return f"{value[:8]}-{value[8:12]}-5{value[13:16]}-a{value[17:20]}-{value[20:32]}"


def stable_id(config, kind, key):
    return str(uuid5(NAMESPACE_URL, f"{config['organization_url']}/moacc-crm/{config['deployment_key']}/{kind}/{key}"))


def marker(config):
    return f"CRM seed [{config['deployment_key']}]"


def role_name(config):
    return "Contact Center CRM " + config["deployment_key"]


def seed_records(config, seed):
    if seed["account"] is None:
        return [], []
    user_id = stable_id(config, "application-user", config["managed_identity"]["client_id"])
    owner = f"/systemusers({user_id})"
    account = seed["account"]
    account_id = stable_id(config, "account", account["key"])
    records = [{
        "table": "accounts", "unique": "accountnumber", "runtime": False,
        "body": {
            "accountid": account_id, "accountnumber": "MS-" + sha(config["deployment_key"] + ":" + account["key"])[:17],
            "name": account["name"], "description": account["description"], "ownerid@odata.bind": owner,
        },
    }]
    contacts = {}
    for contact in seed["contacts"]:
        contact_id = stable_id(config, "contact", contact["key"])
        contacts[contact["key"]] = (contact_id, actor_id(contact["phone"]))
        records.append({
            "table": "contacts", "unique": "moacc_actorid", "runtime": False,
            "body": {
                "contactid": contact_id, "firstname": contact["firstname"], "lastname": contact["lastname"],
                "emailaddress1": contact["email"], "mobilephone": contact["phone"],
                "moacc_actorid": actor_id(contact["phone"]), "moacc_demoenabled": True,
                "moacc_preferredlanguage": contact["preferred_language"],
                "description": contact["description"], "statecode": 0,
                "parentcustomerid_account@odata.bind": f"/accounts({account_id})", "ownerid@odata.bind": owner,
            },
        })
    closures = []
    for case in seed["cases"]:
        contact_id, actor = contacts[case["contact_key"]]
        operation = sha(f"seed:{config['deployment_key']}:{case['key']}")
        request_key = sha(f"{actor}:{operation}")
        case_id = operation_guid(request_key)
        description = case["description"]
        location = {key: case["location"][key] for key in ("district", "street", "landmark")}
        payload_hash = sha(compact({"category": case["category"], "description": description, "location": location}))
        records.append({
            "table": "incidents", "unique": "moacc_requestkey", "runtime": True,
            "body": {
                "incidentid": case_id, "title": case["title"], "description": description,
                "prioritycode": case["priority"], "caseorigincode": 3,
                "moacc_requestkey": request_key, "moacc_payloadhash": payload_hash,
                "moacc_category": case["category"], **{"moacc_" + key: val for key, val in location.items()},
                "customerid_contact@odata.bind": f"/contacts({contact_id})", "ownerid@odata.bind": owner,
            },
        })
        for note in case["notes"]:
            note_operation = sha(f"seed:{config['deployment_key']}:{case['key']}:{note['key']}")
            records.append({
                "table": "annotations", "unique": None, "runtime": True,
                "body": {
                    "annotationid": operation_guid(f"{case_id}:{note_operation}"),
                    "subject": "Customer update", "notetext": note["text"],
                    "isdocument": False, "objectid_incident@odata.bind": f"/incidents({case_id})",
                    "ownerid@odata.bind": owner,
                },
            })
        if case["state"] == "resolved":
            closures.append({
                "case_id": case_id, "status": 5,
                "body": {
                    "@odata.type": "Microsoft.Dynamics.CRM.incidentresolution",
                    "subject": "Resolution: " + case["title"],
                    "description": case["resolution"],
                    "incidentid@odata.bind": f"/incidents({case_id})", "ownerid@odata.bind": owner,
                },
            })
    return records, closures


def safe_error(error):
    result = {"type": type(error).__name__}
    if isinstance(error, HTTPError):
        result["http_status"] = error.code
        error.close()
    if isinstance(error, (ValueError, RuntimeError)):
        result["message"] = str(error)
    return result


def load_document(path, max_bytes=2 * 1024 * 1024):
    path = private_path(path)
    if path.stat().st_size > max_bytes:
        raise ValueError("Private input exceeds its bounded size limit.")
    return json.loads(read_private(path))


class Journal:
    def __init__(self, directory, config, seed, mode):
        self.directory = private_path(directory)
        self.directory.mkdir(mode=0o700)
        self.document = {
            "version": VERSION, "mode": mode, "started_at": datetime.now(timezone.utc).isoformat(),
            "configuration": config, "configuration_hash": fingerprint(config), "seed_hash": fingerprint(seed),
            "status": "in_progress", "events": [], "owned_records": [], "checks": {},
        }

    def write(self, name, value):
        write_private(self.directory / name, json.dumps(value, ensure_ascii=False, indent=2) + "\n")

    def observe(self, kind, value):
        event = {"kind": kind, "status": "observed", "after": copy.deepcopy(value)}
        self.document["events"].append(event)
        self.write(f"{len(self.document['events']):04d}-observed.json", event)
        return value

    def own(self, kind, path, identifier, after):
        entry = {"kind": kind, "path": path, "id": identifier, "created_by_this_run": True, "after": copy.deepcopy(after)}
        self.document["owned_records"].append(entry)

    def mutate(self, api, kind, path, *, body=None, before=None, after, method="POST", headers=None, owned=None):
        event = {
            "kind": kind, "status": "pending", "before": copy.deepcopy(before),
            "request": {"method": method, "path": path, "body": copy.deepcopy(body), "headers": headers or {}},
        }
        self.document["events"].append(event)
        number = len(self.document["events"])
        self.write(f"{number:04d}-before.json", event)
        try:
            api.request(path, method=method, body=body, headers=headers)
            event["write_response_received"] = True
            event["after"] = after()
            event["status"] = "confirmed"
            if owned:
                self.own(*(owned(event["after"]) if callable(owned) else owned), event["after"])
            return event["after"]
        except Exception as error:
            event["status"] = "failed_or_outcome_unknown"
            event["error"] = safe_error(error)
            try:
                event["after"] = after()
            except Exception as read_error:
                event["after_unavailable"] = safe_error(read_error)
            raise
        finally:
            self.write(f"{number:04d}-after.json", event)

    def finish(self, status, error=None):
        self.document.update(status=status, finished_at=datetime.now(timezone.utc).isoformat())
        if error is not None:
            self.document["error"] = safe_error(error)
        self.write("owned-records.json", self.document["owned_records"])
        self.write("receipt.json", self.document)


def optional(api, path, query=None):
    try:
        return api.get(path, query)
    except HTTPError as error:
        if error.code == 404:
            error.close()
            return None
        raise


def validate_identity(config):
    expected = config["managed_identity"]
    result = subprocess.run(
        ["az", "identity", "show", "--ids", expected["resource_id"], "-o", "json"],
        check=True, capture_output=True, text=True, timeout=45,
    )
    actual = json.loads(result.stdout)
    comparisons = {"id": expected["resource_id"], "clientId": expected["client_id"],
                   "principalId": expected["principal_id"], "tenantId": config["tenant_id"]}
    if any(not isinstance(actual.get(key), str) or actual[key].lower() != val.lower() for key, val in comparisons.items()):
        raise ValueError("The existing Azure identity does not match the supplied tenant/resource/client/principal IDs.")
    return {key: actual[key] for key in comparisons}


def label(value):
    return {"LocalizedLabels": [{"Label": value, "LanguageCode": 1033}]}


def attribute_body(name, maximum):
    boolean = maximum is None
    result = {
        "@odata.type": f"Microsoft.Dynamics.CRM.{'Boolean' if boolean else 'String'}AttributeMetadata",
        "SchemaName": name, "DisplayName": label(name), "Description": label("Contact center CRM contract."),
        "RequiredLevel": {"Value": "None"}, "IsSecured": False,
    }
    if boolean:
        result.update(DefaultValue=False, OptionSet={
            "@odata.type": "Microsoft.Dynamics.CRM.BooleanOptionSetMetadata",
            "TrueOption": {"Value": 1, "Label": label("Enabled")},
            "FalseOption": {"Value": 0, "Label": label("Disabled")},
        })
    else:
        result.update(MaxLength=maximum, FormatName={"Value": "Text"})
    return result


def validate_attribute(row, name, maximum):
    expected = "Boolean" if maximum is None else "String"
    if row.get("LogicalName") != name or row.get("AttributeType") != expected or row.get("IsSecured") is not False:
        raise ValueError("Existing custom field has an incompatible type, name or field-security setting.")
    if row.get("RequiredLevel", {}).get("Value") != "None":
        raise ValueError("Existing CRM columns must be optional; required-field settings were preserved.")
    if maximum is None:
        if row.get("DefaultValue") is not False:
            raise ValueError("Synthetic eligibility must default to false.")
    elif type(row.get("MaxLength")) is not int or row["MaxLength"] < maximum:
        raise ValueError("An existing CRM string field is too short; it was not changed.")
    elif row.get("FormatName", {}).get("Value") != "Text":
        raise ValueError("Existing CRM string columns must use Text format; no format conversion was attempted.")


def matches_body(row, body):
    for key, value in body.items():
        if key == "@odata.type":
            continue
        if key.endswith("@odata.bind"):
            lookup = BINDINGS.get(key[:-11])
            if not lookup or row.get(lookup) != value.rsplit("(", 1)[1][:-1]:
                return False
        elif row.get(key) != value:
            # Dataverse normalizes empty string attributes to null.
            if not (value == "" and row.get(key) is None):
                return False
    return True


def role_denial_subject(detail):
    payload = json.loads(detail)
    if not isinstance(payload, dict) or not isinstance(payload.get("error"), dict):
        raise ValueError("Runtime role denial has no structured Dataverse error.")
    error = payload["error"]
    message = error.get("message")
    if (error.get("code") != "0x80040220" or not isinstance(message, str)
            or "is missing prvReadRole privilege" not in message or "for entity 'role'" not in message):
        raise ValueError("Runtime role denial does not prove the expected security-role restriction.")
    identifier = r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}"
    patterns = {
        "principal_user_id": r"^Principal user \(Id=(" + identifier + r"),",
        "aad_object_id": r"\bAADObjectId='(" + identifier + r")'",
        "caller_user_id": r"\bcontext\.Caller=(" + identifier + r")\.",
    }
    subject = {}
    for key, pattern in patterns.items():
        matches = re.findall(pattern, message)
        if len(matches) != 1:
            raise ValueError("Runtime role denial has missing or ambiguous execution identity.")
        subject[key] = guid(matches[0])
    return subject


class Setup:
    def __init__(self, config, seed, api, journal, *, apply=False, previous=None, attempts=20, interval=3):
        if not 1 <= attempts <= 30 or not 0 <= interval <= 10:
            raise ValueError("Wait bounds must be 1–30 attempts and 0–10 seconds between reads.")
        self.config, self.seed, self.api, self.journal = config, seed, api, journal
        self.apply, self.previous, self.attempts, self.interval = apply, previous or {}, attempts, interval
        self.role_id = stable_id(config, "role", config["business_unit_id"])
        self.user_id = stable_id(config, "application-user", config["managed_identity"]["client_id"])
        self.role_name = role_name(config)
        self.privileges = required_privileges(config)
        self.runtime_headers = {"CallerObjectId": config["managed_identity"]["principal_id"]}
        self.new_role = False
        self.definitions = {}
        self.operator_user_id = None

    def wait(self, read, ready, description):
        for attempt in range(self.attempts):
            value = read()
            if ready(value):
                return value
            if attempt + 1 < self.attempts:
                time.sleep(self.interval)
        raise RuntimeError(f"Bounded wait expired: {description}. Reconcile the private receipt before retrying.")

    def proof(self, path):
        for document in (self.journal.document, self.previous):
            for entry in document.get("owned_records", []):
                if entry.get("path") == path and entry.get("created_by_this_run") is True:
                    return entry
        return None

    def required(self, condition, message):
        if not condition:
            raise ValueError(message)

    def preflight(self):
        identity = validate_identity(self.config)
        who = self.api.get("WhoAmI")
        self.required(guid(who["UserId"]) != self.user_id, "Use an administrative setup identity, not the runtime user.")
        admin = self.api.get(f"systemusers({guid(who['UserId'])})", {
            "$select": "systemuserid,applicationid,azureactivedirectoryobjectid,isdisabled",
        })
        self.required(
            admin.get("systemuserid") == guid(who["UserId"]) and admin.get("isdisabled") is False
            and admin.get("applicationid") != self.config["managed_identity"]["client_id"]
            and admin.get("azureactivedirectoryobjectid") != self.config["managed_identity"]["principal_id"],
            "Administrative setup and runtime identities must be separate.",
        )
        self.operator_user_id = guid(who["UserId"])
        unit = self.api.get(f"businessunits({self.config['business_unit_id']})", {"$select": "businessunitid,isdisabled"})
        self.required(unit.get("isdisabled") is False, "The configured business unit is disabled.")
        for identifier in self.config["workstream_ids"]:
            row = self.api.get(f"msdyn_liveworkstreams({identifier})", {
                "$select": "msdyn_liveworkstreamid,statecode,msdyn_direction,msdyn_streamsource",
            })
            self.required(
                row.get("statecode") == 0 and row.get("msdyn_direction") == 0 and row.get("msdyn_streamsource") == 192440000,
                "Use an active native inbound voice workstream; this kit never changes routing.",
            )
        definitions = self.api.rows("privileges", {
            "$select": "privilegeid,name", "$filter": " or ".join(f"name eq '{name}'" for name in self.privileges),
        })
        self.definitions = {row["name"]: guid(row["privilegeid"]) for row in definitions}
        self.required(
            set(self.definitions) == set(self.privileges) and len(definitions) == len(self.privileges)
            and len(set(self.definitions.values())) == len(self.privileges),
            "A required CRM privilege is missing or ambiguous.",
        )
        roles = self.api.rows("roles", {
            "$select": "roleid", "$filter": f"name eq '{self.role_name}' and _businessunitid_value eq {self.config['business_unit_id']}",
            "$top": "2",
        })
        self.required(not roles or len(roles) == 1 and roles[0]["roleid"] == self.role_id,
                      "An unfamiliar role uses the requested name; existing roles were preserved.")
        if roles:
            self.role_consumers()
        users = self.api.rows("systemusers", {
            "$select": "systemuserid", "$filter": f"applicationid eq {self.config['managed_identity']['client_id']}", "$top": "2",
        })
        self.required(not users or len(users) == 1 and users[0]["systemuserid"] == self.user_id,
                      "An unfamiliar application user already uses this identity; it was not adopted.")
        if users:
            self.validate_user(self.api.get(f"systemusers({self.user_id})"))
            self.memberships()
            self.team_roles()
        self.journal.observe("administrative-preflight", {"managed_identity": identity, "who_am_i": who, "business_unit": unit})

    def metadata(self, entity, name, maximum):
        base = f"EntityDefinitions(LogicalName='{entity}')/Attributes(LogicalName='{name}')"
        row = optional(self.api, base)
        if row is None:
            return None
        self.required(row.get("AttributeType") == ("Boolean" if maximum is None else "String"),
                      "An existing custom field has a different type; no conversion was attempted.")
        type_name = "BooleanAttributeMetadata" if maximum is None else "StringAttributeMetadata"
        return self.wait(
            lambda: optional(self.api, base + "/Microsoft.Dynamics.CRM." + type_name),
            lambda value: value is not None, "typed custom attribute visibility",
        )

    def key(self, entity, name):
        rows = self.api.get(f"EntityDefinitions(LogicalName='{entity}')/Keys", {
            "$filter": f"LogicalName eq '{name}'",
        })["value"]
        self.required(len(rows) <= 1, "Alternate-key metadata is ambiguous.")
        return rows[0] if rows else None

    def schema(self, republish=False):
        changed = False
        for entity, fields in FIELDS.items():
            for name, maximum in fields.items():
                row = self.metadata(entity, name, maximum)
                if row is None:
                    self.required(self.apply, "CRM schema is incomplete; review a plan before explicit --apply.")
                    path = f"EntityDefinitions(LogicalName='{entity}')/Attributes"
                    read = lambda e=entity, n=name, m=maximum: self.wait(
                        lambda: self.metadata(e, n, m), lambda val: val is not None, "custom attribute visibility",
                    )
                    row = self.journal.mutate(
                        self.api, "create-attribute", path, body=attribute_body(name, maximum), after=read,
                        owned=lambda value, p=path: (
                            "attribute", f"{p}({guid(value['MetadataId'])})", guid(value["MetadataId"]),
                        ),
                    )
                    changed = True
                validate_attribute(row, name, maximum)
                self.journal.observe("attribute-verified", {"entity": entity, "metadata": row})
        for entity, (name, attribute) in KEYS.items():
            row = self.key(entity, name)
            if row is None:
                self.required(self.apply, "A required alternate key is missing.")
                path = f"EntityDefinitions(LogicalName='{entity}')/Keys"
                row = self.journal.mutate(
                    self.api, "create-key", path,
                    body={"SchemaName": name, "DisplayName": label(name), "KeyAttributes": [attribute]},
                    after=lambda e=entity, n=name: self.wait(lambda: self.key(e, n), lambda val: val is not None, "key visibility"),
                    owned=lambda value, p=path: ("key", f"{p}({guid(value['MetadataId'])})", guid(value["MetadataId"])),
                )
                changed = True
            self.required(row.get("LogicalName") == name and row.get("KeyAttributes") == [attribute],
                          "An existing alternate key has incompatible columns.")
        if self.apply and (changed or republish):
            self.journal.mutate(
                self.api, "publish-targeted-schema", "PublishXml",
                body={"ParameterXml": "<importexportxml><entities><entity>contact</entity><entity>incident</entity></entities></importexportxml>"},
                before={"entities": ["contact", "incident"]},
                after=lambda: {entity: self.key(entity, name) for entity, (name, _) in KEYS.items()},
            )

        def active(row):
            if row is None:
                return False
            self.required(row.get("EntityKeyIndexStatus") in ("Pending", "InProgress", "Active", "Failed"),
                          "Unexpected alternate-key index state.")
            if row["EntityKeyIndexStatus"] == "Failed":
                raise RuntimeError("Alternate-key index creation failed; inspect duplicate non-null values, do not delete data.")
            return row["EntityKeyIndexStatus"] == "Active"

        for entity, (name, _) in KEYS.items():
            row = self.wait(lambda: self.key(entity, name), active, "alternate key Active")
            self.required(row.get("LogicalName") == name and row.get("KeyAttributes") == [KEYS[entity][1]],
                          "Alternate-key columns changed during verification; no seed writes were made.")
            self.journal.observe("key-active", {"entity": entity, "metadata": row})
        self.journal.document["checks"]["schema_and_active_keys"] = True

    def role_privileges(self):
        rows = self.api.get(f"RetrieveRolePrivilegesRole(RoleId={self.role_id})")["RolePrivileges"]
        unknown = [row["PrivilegeId"] for row in rows if not row.get("PrivilegeName")]
        names = {}
        for offset in range(0, len(unknown), 20):
            definitions = self.api.rows("privileges", {
                "$select": "privilegeid,name", "$filter": " or ".join(f"privilegeid eq {guid(val)}" for val in unknown[offset:offset + 20]),
            })
            names.update({row["privilegeid"]: row["name"] for row in definitions})
        result = {}
        for row in rows:
            name = row.get("PrivilegeName") or names.get(row["PrivilegeId"])
            self.required(name and row.get("Depth") in DEPTH, "Role privilege metadata could not be resolved.")
            record_filter = row.get("RecordFilterId")
            self.required(
                not row.get("RecordFilterUniqueName")
                and (record_filter is None or UUID(record_filter).int == 0),
                "Filtered role privileges are not supported; existing privileges were preserved.",
            )
            result[name] = {"id": guid(row["PrivilegeId"]), "depth": row["Depth"], "raw": row}
        return result

    def memberships(self):
        rows = self.api.rows(f"systemusers({self.user_id})/systemuserroles_association", {"$select": "roleid,name"})
        self.journal.observe("application-user-roles", rows)
        self.required(all(row["roleid"] == self.role_id for row in rows),
                      "Unexpected existing application-user roles were preserved; use a genuinely isolated identity.")
        return rows

    def role_consumers(self):
        path = f"roles({self.role_id})"
        users = self.api.rows(path + "/systemuserroles_association", {"$select": "systemuserid"})
        teams = self.api.rows(path + "/teamroles_association", {"$select": "teamid"})
        self.journal.observe("crm-role-consumers", {"users": users, "teams": teams})
        self.required(not teams and all(row.get("systemuserid") == self.user_id for row in users),
                      "The CRM role is shared with another user or team; its privileges and memberships were preserved.")

    def team_roles(self):
        teams = self.api.rows(f"systemusers({self.user_id})/teammembership_association", {"$select": "teamid"})
        self.required(len(teams) <= 20, "Runtime team inventory exceeds this isolated-user setup scope.")
        inherited = []
        for team in teams:
            roles = self.api.rows(f"teams({guid(team['teamid'])})/teamroles_association", {"$select": "roleid,name"})
            inherited.append({"teamid": team["teamid"], "roles": roles})
        self.journal.observe("application-user-team-roles", inherited)
        self.required(not any(row["roles"] for row in inherited),
                      "Existing team-assigned runtime roles were preserved; this kit requires an isolated user without inherited roles.")

    def validate_user(self, row):
        identity = self.config["managed_identity"]
        self.required(
            row.get("systemuserid") == self.user_id and row.get("applicationid") == identity["client_id"]
            and row.get("azureactivedirectoryobjectid") == identity["principal_id"]
            and row.get("_businessunitid_value") == self.config["business_unit_id"]
            and row.get("accessmode") == 4 and row.get("isdisabled") is False,
            "Existing application-user identity, business unit or access mode is incompatible; nothing was reassigned.",
        )

    def identity(self):
        path = f"roles({self.role_id})"
        role = optional(self.api, path)
        if role is None:
            self.required(self.apply, "The isolated CRM role is missing.")
            role = self.journal.mutate(
                self.api, "create-role", "roles", body={
                    "roleid": self.role_id, "name": self.role_name, "description": marker(self.config),
                    "businessunitid@odata.bind": f"/businessunits({self.config['business_unit_id']})",
                }, after=lambda: self.api.get(path), owned=("roles", path, self.role_id),
            )
            self.new_role = True
        self.required(
            role.get("name") == self.role_name and role.get("description") == marker(self.config)
            and role.get("ismanaged") is False and role.get("_businessunitid_value") == self.config["business_unit_id"],
            "Unfamiliar or managed role; no existing role was replaced.",
        )
        self.role_consumers()
        before = self.role_privileges()
        self.journal.observe("role-privileges-before", before)
        extras = set(before) - set(self.privileges)
        self.required(not extras - SHAREPOINT_DEFAULTS - PRESERVED_PLATFORM_READS,
                      "Unexpected role privileges were preserved; review them outside this kit.")
        self.required(not any(DEPTH[row["depth"]] > DEPTH[self.privileges[name]]
                              for name, row in before.items() if name in self.privileges),
                      "Existing wider privilege depths were preserved; they do not pass the minimum-role check.")
        defaults = extras & SHAREPOINT_DEFAULTS
        prior_defaults = {
            name for event in self.previous.get("events", []) if event.get("kind") == "platform-role-defaults"
            for name in event.get("after", [])
        }
        prior_privileges = {}
        for event in self.previous.get("events", []):
            if event.get("kind") in {
                "role-privileges-before", "role-privileges-after", "add-minimum-role-privileges",
                "remove-documented-sharepoint-default",
            } and isinstance(event.get("after"), dict):
                prior_privileges = event["after"]
        if self.new_role:
            self.journal.observe("platform-role-defaults", sorted(defaults))
        missing = [name for name, depth in self.privileges.items() if name not in before or DEPTH[before[name]["depth"]] < DEPTH[depth]]
        if missing or defaults:
            self.required(self.apply and self.proof(path),
                          "Changing an existing role requires an explicit apply and its original owned-record receipt.")
            self.required(self.new_role or defaults <= prior_defaults & set(prior_privileges),
                          "SharePoint privileges were not recorded as platform-added defaults; they were preserved.")
            if missing:
                self.journal.mutate(
                    self.api, "add-minimum-role-privileges", path + "/Microsoft.Dynamics.CRM.AddPrivilegesRole",
                    body={"Privileges": [{"PrivilegeId": self.definitions[name], "Depth": self.privileges[name]} for name in missing]},
                    before=before, after=self.role_privileges,
                )
            for name in sorted(defaults):
                self.journal.mutate(
                    self.api, "remove-documented-sharepoint-default", path + "/Microsoft.Dynamics.CRM.RemovePrivilegeRole",
                    body={"Privilege": {"@odata.type": "Microsoft.Dynamics.CRM.privilege", "privilegeid": before[name]["id"]}},
                    before=self.role_privileges(), after=self.role_privileges,
                )
        effective = self.role_privileges()
        self.required(
            all(effective.get(name, {}).get("depth") == depth for name, depth in self.privileges.items())
            and not set(effective) - set(self.privileges) - PRESERVED_PLATFORM_READS,
            "Minimum role privileges did not verify; existing privileges were not replaced.",
        )
        self.required(all(effective[name]["depth"] == "Global" for name in set(effective) & PRESERVED_PLATFORM_READS),
                      "Unexpected platform dependency depth; inspect without stripping existing privileges.")
        self.journal.observe("role-privileges-after", effective)
        self.journal.document["checks"]["preserved_platform_reads"] = sorted(set(effective) & PRESERVED_PLATFORM_READS)
        user_path = f"systemusers({self.user_id})"
        user = optional(self.api, user_path)
        if user is None:
            self.required(self.apply, "The isolated CRM application user is missing.")
            identity = self.config["managed_identity"]
            user = self.journal.mutate(
                self.api, "create-application-user", "systemusers", body={
                    "systemuserid": self.user_id, "applicationid": identity["client_id"],
                    "azureactivedirectoryobjectid": identity["principal_id"], "accessmode": 4,
                    "firstname": "Contact center", "lastname": "CRM service",
                    "businessunitid@odata.bind": f"/businessunits({self.config['business_unit_id']})",
                }, after=lambda: self.api.get(user_path), owned=("systemusers", user_path, self.user_id),
            )
        self.validate_user(user)
        memberships = self.memberships()
        self.team_roles()
        if not memberships:
            self.required(self.apply and self.proof(user_path),
                          "Role assignment requires a receipt-owned application user and explicit apply.")
            self.journal.mutate(
                self.api, "assign-crm-role", user_path + "/systemuserroles_association/$ref",
                body={"@odata.id": f"{self.config['organization_url']}/api/data/v9.2/{path}"},
                before=memberships, after=self.memberships,
            )
        self.required(len(self.memberships()) == 1, "The dedicated role assignment was not confirmed.")
        self.journal.document["checks"]["minimum_role"] = True

    def runtime_read(self, path):
        return self.api.request(path, headers=self.runtime_headers)

    def runtime_checks(self):
        who = self.runtime_read("WhoAmI")
        diagnostic_matches_runtime = who.get("UserId") == self.user_id
        self.required(
            who.get("BusinessUnitId") == self.config["business_unit_id"]
            and (diagnostic_matches_runtime
                 or self.operator_user_id is not None and who.get("UserId") == self.operator_user_id),
                      "CallerObjectId impersonation did not resolve to the isolated runtime user.")
        for entity, fields in FIELDS.items():
            table = "contacts" if entity == "contact" else "incidents"
            response = self.runtime_read(table + "?" + urlencode({"$select": ",".join(fields), "$top": "1"}))
            self.required(isinstance(response.get("value"), list), "Runtime CRM table-read verification failed.")
        assembly = None
        if self.config["native_voice"]:
            assemblies = self.runtime_read("pluginassemblies?" + urlencode({
                "$select": "pluginassemblyid,name", "$filter": f"name eq '{PLUGIN}'", "$top": "2",
            }))["value"]
            self.required(len(assemblies) == 1 and assemblies[0].get("name") == PLUGIN,
                          "Global prvReadPluginAssembly/native plug-in readiness did not verify.")
            assembly = assemblies[0]
        subject = None
        try:
            self.runtime_read("roles?$select=roleid&$top=1")
        except HTTPError as error:
            try:
                detail = error.read(16385)
                self.required(error.code == 403 and len(detail) <= 16384 and b"prvReadRole" in detail,
                              "Runtime role-read denial was not the expected prvReadRole permission check.")
                if not diagnostic_matches_runtime:
                    subject = role_denial_subject(detail)
                    self.required(
                        subject == {
                            "principal_user_id": self.user_id,
                            "aad_object_id": self.config["managed_identity"]["principal_id"],
                            "caller_user_id": self.user_id,
                        },
                        "CallerObjectId impersonation role-denial subject differs from the isolated runtime identity.",
                    )
            finally:
                error.close()
        else:
            raise ValueError("Runtime identity can read security roles; impersonation/least-privilege verification failed.")
        if subject is not None:
            self.journal.observe("runtime-identity-diagnostic-discrepancy", {
                "who_am_i": who, "verified_execution_subject": subject,
                "identity_proof": "exact Dataverse security-role denial subject, not an actual runtime token",
            })
        self.journal.observe("runtime-read-verification", {
            "who_am_i": who, "pluginassembly": assembly, "native_voice": self.config["native_voice"], "role_read_denied": True,
            "identity_verification": "exact_role_denial_subject" if subject is not None else "who_am_i",
            "effective_runtime_user_id": self.user_id,
            "method": "administrative CallerObjectId impersonation, not an actual Azure managed-identity token",
        })
        self.journal.document["checks"]["runtime_reads_and_role_denial"] = True

    def record(self, spec):
        table, body = spec["table"], spec["body"]
        identifier = body[PRIMARY[table]]
        path = f"{table}({identifier})"
        before = optional(self.api, path)
        if spec["unique"]:
            key = spec["unique"]
            value = body[key].replace("'", "''")
            rows = self.api.get(table, {"$select": PRIMARY[table], "$filter": f"{key} eq '{value}'", "$top": "2"})["value"]
            self.required(all(row[PRIMARY[table]] == identifier for row in rows),
                          "A seed natural/alternate key belongs to another record; it was not adopted or overwritten.")
        if before is not None:
            self.required(matches_body(before, body), "An existing deterministic seed record differs; user data was preserved.")
            return self.journal.observe("existing-seed-record", {"path": path, "record": before})["record"]
        self.required(self.apply, "A synthetic seed record is missing.")
        headers = {"If-None-Match": "*", **(self.runtime_headers if spec["runtime"] else {})}
        after = self.journal.mutate(
            self.api, "create-synthetic-record", path, method="PATCH", body=body,
            headers=headers, after=lambda: self.api.get(path), owned=(table, path, identifier),
        )
        self.required(matches_body(after, body), "Seed readback differs from the planned payload; inspect the private receipt.")
        if spec["runtime"]:
            self.required(after.get("_createdby_value") == self.user_id, "Runtime seed creation was not attributed to the isolated app user.")
        return after

    def close(self, closure):
        case_path = f"incidents({closure['case_id']})"

        def snapshot():
            return {
                "case": self.api.get(case_path),
                "resolutions": self.api.get("incidentresolutions", {
                    "$filter": f"_incidentid_value eq {closure['case_id']}", "$top": "2",
                })["value"],
            }

        def complete(value):
            self.required(len(value["resolutions"]) <= 1, "Multiple resolution activities exist; all were preserved.")
            return value["case"].get("statecode") == 1 and len(value["resolutions"]) == 1

        before = snapshot()
        if not complete(before):
            self.required(not before["resolutions"] and before["case"].get("statecode") == 0,
                          "An unfamiliar closure state was preserved; no replacement resolution was fabricated.")
            proof = self.proof(case_path)
            self.required(self.apply and proof is not None, "Only receipt-owned synthetic cases may be resolved by setup.")
            self.required(proof["after"].get("@odata.etag") and proof["after"]["@odata.etag"] == before["case"].get("@odata.etag"),
                          "The owned synthetic case changed since creation; do not close it automatically.")
            after = self.journal.mutate(
                self.api, "close-synthetic-case", "CloseIncident",
                body={"IncidentResolution": closure["body"], "Status": closure["status"]},
                before=before, after=lambda: self.wait(snapshot, complete, "case resolution readback"),
            )
            resolution = after["resolutions"][0]
            self.required(matches_body(resolution, closure["body"]), "The native resolution differs from the synthetic closure payload.")
            identifier = guid(resolution["activityid"])
            self.journal.own("incidentresolutions", f"incidentresolutions({identifier})", identifier, resolution)
        else:
            after = before
        self.required(after["case"].get("statuscode") == closure["status"]
                      and matches_body(after["resolutions"][0], closure["body"]),
                      "Existing resolution data does not match; it was preserved.")
        self.journal.observe("resolved-case-verified", after)
        for entry in self.journal.document["owned_records"]:
            if entry["path"] == case_path:
                entry["after"] = copy.deepcopy(after["case"])

    def seed_data(self):
        records, closures = seed_records(self.config, self.seed)
        closed_ids = {row["case_id"] for row in closures}
        for spec in records:
            row = self.record(spec)
            if spec["table"] == "incidents":
                self.required(row.get("statecode") == 0 or row["incidentid"] in closed_ids,
                              "An active seed case was changed by another actor; its state was preserved.")
        for closure in closures:
            self.close(closure)
        for spec in records:
            if spec["table"] == "contacts":
                body = spec["body"]
                rows = self.runtime_read("contacts?" + urlencode({
                    "$select": "contactid,moacc_actorid,moacc_demoenabled,moacc_preferredlanguage,statecode",
                    "$filter": f"moacc_actorid eq '{body['moacc_actorid']}' and moacc_demoenabled eq true and statecode eq 0",
                    "$top": "2",
                }))["value"]
                self.required(len(rows) == 1 and all(
                    rows[0].get(key) == body[key] for key in
                    ("contactid", "moacc_actorid", "moacc_demoenabled", "moacc_preferredlanguage", "statecode")
                ),
                              "Runtime synthetic-contact eligibility/read scope did not verify.")
                self.journal.observe("eligible-contact-verified", rows[0])
            elif spec["table"] in ("accounts", "incidents", "annotations"):
                body = spec["body"]
                path = f"{spec['table']}({body[PRIMARY[spec['table']]]})"
                row = self.runtime_read(path)
                self.required(matches_body(row, body), "Runtime seed record readback/relationship scope did not verify.")
                self.journal.observe("runtime-seed-readback", {"path": path, "record": row})
        self.journal.document["checks"]["synthetic_seed"] = bool(records)
        self.journal.observe("seed-provenance", {
            "selected": bool(records), "synthetic_only": self.seed["synthetic_only"],
            "record_count": len(records), "seed_sha256": fingerprint(self.seed),
        })

    def run(self, republish=False):
        self.preflight()
        self.schema(republish)
        self.identity()
        self.runtime_checks()
        self.seed_data()


def previous_receipt(path, config):
    document = load_document(path, max_bytes=32 * 1024 * 1024)
    if (
        document.get("version") != VERSION or document.get("mode") != "apply"
        or document.get("configuration_hash") != fingerprint(config)
        or not isinstance(document.get("owned_records"), list) or not isinstance(document.get("events"), list)
    ):
        raise ValueError("The prior receipt is not an apply receipt for this exact configuration.")
    return document


def offline_plan(config, seed):
    records, closures = seed_records(config, seed)
    return {
        "offline_contract_check_only": True, "remote_changes": False,
        "role_name": role_name(config), "required_privileges": required_privileges(config),
        "preserve_optional_platform_reads": sorted(PRESERVED_PLATFORM_READS),
        "only_removable_platform_defaults": sorted(SHAREPOINT_DEFAULTS),
        "fields": FIELDS, "alternate_keys": KEYS, "records": records, "closures": closures,
        "administrative_setup_is_not_runtime_identity": True,
        "native_conversation_mutations": False, "azure_provisioning": False,
    }


def rollback_plan(receipt):
    order = {"annotations": 0, "incidentresolutions": 1, "incidents": 2, "contacts": 3, "accounts": 4,
             "systemusers": 5, "roles": 6, "key": 7, "attribute": 8}
    owned = [row for row in receipt["owned_records"] if row.get("created_by_this_run") is True]
    return {
        "executed": False, "review_required": True, "source_status": receipt["status"],
        "owned_records_in_review_order": sorted(owned, key=lambda row: order.get(row["kind"], 99)),
        "role_and_membership_changes": [
            row for row in receipt["events"] if row.get("kind") in {
                "add-minimum-role-privileges", "remove-documented-sharepoint-default", "assign-crm-role",
            }
        ],
        "case_state_changes": [row for row in receipt["events"] if row.get("kind") == "close-synthetic-case"],
        "unknown_outcomes": [row for row in receipt["events"] if row.get("status") in {"pending", "failed_or_outcome_unknown"}],
        "guards": [
            "Read every current record and compare the entire saved snapshot/ETag before any inverse action.",
            "Refuse cascading deletion when ANY unowned case, contact, note, activity or conversation references a target.",
            "Reopen only unchanged owned resolved cases as administrator before reviewing resolution/activity deletion.",
            "Remove only the membership added by this receipt; never remove other user/team roles.",
            "Disable, do not delete, an owned app user only after all owned data is cleaned up and no other consumers remain.",
            "Delete an owned role only when no other user/team uses it. Never replay a whole privilege replacement.",
            "Check solution dependencies and data use before removing owned keys/attributes; preserve existing metadata.",
            "Never delete the Azure identity, business unit, native reader role, workstreams or unrelated Cosmos resources.",
            "This is an inventory for reviewed targeted rollback, not an executable delete script.",
        ],
    }


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--config", type=Path, required=True, help="Owner-only private JSON, outside the checkout.")
    result.add_argument("--seed", type=Path, help="Optional owner-only private generated seed; omitted means schema/identity only.")
    result.add_argument("--approve-seed", action="store_true", help="With --apply: confirm the reviewed generated records.")
    result.add_argument("--output-dir", type=Path, required=True, help="New private directory outside the checkout.")
    modes = result.add_mutually_exclusive_group()
    modes.add_argument("--apply", action="store_true", help="Explicitly create missing schema/isolated identity/seed.")
    modes.add_argument("--verify", action="store_true", help="Live read-only checks, including runtime impersonation.")
    modes.add_argument("--rollback-plan-from", type=Path, help="Offline targeted rollback inventory from one apply receipt.")
    result.add_argument("--confirm-organization", help="Required for --apply; must exactly match the private configuration.")
    result.add_argument("--previous-receipt", type=Path, action="append", default=[],
                        help="Original apply ownership receipt for a partial-run resume; repeat for multiple runs.")
    result.add_argument("--publish-schema", action="store_true", help="With --apply only: republish contact/incident after a reviewed partial run.")
    result.add_argument("--wait-attempts", type=int, default=20)
    result.add_argument("--wait-seconds", type=float, default=3)
    return result


def main(argv=None):
    args = parser().parse_args(argv)
    journal = None
    try:
        config = validate_config(load_document(args.config))
        seed = validate_seed(load_document(args.seed) if args.seed else EMPTY_SEED,
                             categories=config["case_categories"], location_required=config["location_required"])
        if args.apply and args.confirm_organization != config["organization_url"]:
            raise ValueError("--apply requires --confirm-organization matching the configuration exactly.")
        if args.apply and seed["account"] is not None and not args.approve_seed:
            raise ValueError("Preview the private generated records and pass --approve-seed before data writes.")
        if not args.apply and (args.confirm_organization or args.publish_schema):
            raise ValueError("Confirmation and schema publishing are valid only with --apply.")
        if not 1 <= args.wait_attempts <= 30 or not 0 <= args.wait_seconds <= 10:
            raise ValueError("Wait bounds must be 1–30 attempts and 0–10 seconds.")
        if len(args.previous_receipt) > 20:
            raise ValueError("Supply at most 20 original ownership receipts.")
        prior_documents = [previous_receipt(path, config) for path in args.previous_receipt]
        prior = {
            "owned_records": [row for doc in prior_documents for row in doc["owned_records"]],
            "events": [row for doc in prior_documents for row in doc["events"]],
        } if prior_documents else None
        mode = "apply" if args.apply else "verify" if args.verify else "rollback-plan" if args.rollback_plan_from else "plan"
        journal = Journal(args.output_dir, config, seed, mode)
        journal.write("plan.json", offline_plan(config, seed))
        write_private(journal.directory / "runtime.env", env_file({
            "CRM_DATAVERSE_ORG_URL": config["organization_url"],
            "CRM_CLIENT_ID": config["managed_identity"]["client_id"],
            "CRM_NATIVE_WORKSTREAM_IDS": ",".join(config["workstream_ids"]),
        }))
        if mode == "rollback-plan":
            journal.write("rollback-plan.json", rollback_plan(previous_receipt(args.rollback_plan_from, config)))
        elif mode in ("apply", "verify"):
            api = Dataverse(config["organization_url"], config["tenant_id"])
            Setup(config, seed, api, journal, apply=args.apply, previous=prior,
                  attempts=args.wait_attempts, interval=args.wait_seconds).run(args.publish_schema)
        journal.finish("succeeded")
        print(json.dumps({"mode": mode, "succeeded": True, "private_receipt": str(journal.directory / "receipt.json"),
                          "live_checks_performed": mode in ("apply", "verify"), "clean_tenant_replay_claimed": False}))
        return 0
    except (Exception, KeyboardInterrupt) as error:
        failure = safe_error(error)
        receipt_saved = False
        if journal is not None:
            try:
                journal.finish("failed", error)
                receipt_saved = True
            except Exception as receipt_error:
                failure["receipt_write_failure"] = safe_error(receipt_error)
        print(json.dumps({"succeeded": False, "error": failure,
                          "private_receipt": str(journal.directory / "receipt.json") if receipt_saved else None,
                          "recovery_journal_directory": str(journal.directory) if journal else None,
                          "automatic_rollback_performed": False}), file=sys.stderr)
        if isinstance(error, HTTPError):
            error.close()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
