"""Read-only by default; onboard approved humans using native queue/capacity associations."""

import argparse
import copy
import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError
from uuid import UUID, uuid4, uuid5

from native_voice_setup import Dataverse, guid, organization, private_path, read_private, write_private


VERSION = 1
REVIEW_SECONDS = 900
MEMBERS = "queuemembership_association"
CAPACITY = "msdyn_bookableresourcecapacityprofiles"
CAPACITY_ID = "msdyn_bookableresourcecapacityprofileid"
REQUIRED_ROLES = {"basic user", "omnichannel agent", "app profile user"}
USER_FIELDS = (
    "systemuserid,azureactivedirectoryobjectid,applicationid,applicationiduri,"
    "isdisabled,islicensed,accessmode,_businessunitid_value"
)
QUEUE_FIELDS = (
    "queueid,name,statecode,msdyn_isomnichannelqueue,msdyn_queuetype,"
    "msdyn_assignmentstrategy,msdyn_priority,msdyn_operatinghoursbasedassignment,"
    "_msdyn_operatinghourid_value,_msdyn_prequeueoverflowrulesetid_value,"
    "_msdyn_inqueueoverflowrulesetid_value,_msdyn_assignmentinputcontractid_value,"
    "msdyn_queueservicelevelthreshold,msdyn_shortabandonedthreshold,"
    "msdyn_maxqueuesize,msdyn_processqueueperiodically"
)
CAPACITY_FIELDS = (
    CAPACITY_ID + ",_msdyn_bookableresourceid_value,_msdyn_capacityprofileid_value,"
    "msdyn_maxunits,msdyn_name,statecode,statuscode,_ownerid_value,_owningbusinessunit_value,"
    "_createdby_value,createdon,modifiedon"
)
ROLE_FIELDS = "roleid,name,_businessunitid_value,_parentrootroleid_value"
DEFINITIVE_REJECTIONS = {400, 401, 403, 404, 405, 412, 422}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def compact(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(compact(value).encode()).hexdigest()


def utc():
    return datetime.now(timezone.utc).isoformat()


def timestamp(value):
    require(isinstance(value, str), "A timezone-aware receipt timestamp is required.")
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    require(result.tzinfo is not None, "A timezone-aware receipt timestamp is required.")
    return result.astimezone(timezone.utc)


def shape(value, keys):
    require(isinstance(value, dict) and set(value) == set(keys),
            "Unexpected or missing configuration/receipt properties; use the repository example.")


def unique_ids(values, label, maximum=30):
    require(isinstance(values, list) and 1 <= len(values) <= maximum, label + ": supply a bounded nonempty list.")
    result = [guid(value) for value in values]
    require(len(set(result)) == len(result), label + ": duplicate IDs are not allowed.")
    return result


def validate_config(value):
    shape(value, {
        "version", "organization_url", "tenant_id", "organization_id", "administrator",
        "queue_id", "workstreams", "representatives", "approval",
    })
    require(type(value["version"]) is int and value["version"] == VERSION, "Unsupported configuration version.")
    config = copy.deepcopy(value)
    config["organization_url"] = organization(config["organization_url"])
    for key in ("tenant_id", "organization_id", "queue_id"):
        config[key] = guid(config[key])
    shape(config["administrator"], {"user_id", "entra_object_id"})
    for key in config["administrator"]:
        config["administrator"][key] = guid(config["administrator"][key])
    streams = config["workstreams"]
    require(isinstance(streams, list) and 1 <= len(streams) <= 10, "Supply 1–10 exact inbound workstreams.")
    for stream in streams:
        shape(stream, {"workstream_id", "capacity_profile_id"})
        for key in stream:
            stream[key] = guid(stream[key])
    unique_ids([row["workstream_id"] for row in streams], "workstreams")
    profiles = {row["capacity_profile_id"] for row in streams}
    reps = config["representatives"]
    require(isinstance(reps, list) and 1 <= len(reps) <= 10, "Supply 1–10 existing humans; apply requires at least two.")
    for rep in reps:
        shape(rep, {
            "user_id", "entra_object_id", "bookable_resource_id", "approved",
            "voice_license_admin_confirmed", "native_access_admin_confirmed",
            "required_role_ids", "capacity_limits",
        })
        for key in ("user_id", "entra_object_id", "bookable_resource_id"):
            rep[key] = guid(rep[key])
        for key in ("approved", "voice_license_admin_confirmed", "native_access_admin_confirmed"):
            require(type(rep[key]) is bool, "Representative approval flags must be explicit booleans.")
        rep["required_role_ids"] = unique_ids(rep["required_role_ids"], "required roles")
        limits = rep["capacity_limits"]
        require(isinstance(limits, list) and len(limits) == len(profiles),
                "Each human must explicitly specify every distinct selected capacity profile once.")
        for limit in limits:
            shape(limit, {"capacity_profile_id", "max_units"})
            limit["capacity_profile_id"] = guid(limit["capacity_profile_id"])
            units = limit["max_units"]
            require(units is None or type(units) is int and 1 <= units <= 100,
                    "max_units must be null (inherit) or an explicitly approved integer from 1 to 100.")
        require(set(unique_ids([row["capacity_profile_id"] for row in limits], "capacity limits")) == profiles,
                "Capacity limits must reference exactly the selected workstream profiles.")
    for key in ("user_id", "entra_object_id", "bookable_resource_id"):
        unique_ids([rep[key] for rep in reps], "representatives/" + key)
    approval = config["approval"]
    shape(approval, {"reference", "shared_capacity_and_queue_policy_reviewed"})
    reference = approval["reference"]
    require(isinstance(reference, str) and reference.strip() == reference and 1 <= len(reference) <= 200
            and not any(ord(char) < 32 for char in reference), "Supply a private administrative approval reference.")
    require(type(approval["shared_capacity_and_queue_policy_reviewed"]) is bool,
            "Shared-capacity/queue-policy approval must be an explicit boolean.")
    return config


def ordered(rows):
    return sorted(rows, key=compact)


def clean(value):
    if isinstance(value, dict):
        return {key: clean(item) for key, item in value.items() if "@" not in key}
    if isinstance(value, list):
        return ordered([clean(item) for item in value])
    return value


def configuration(snapshot):
    return clean({key: value for key, value in snapshot.items() if key not in {"nonclosed_conversations", "dynamic_capacity"}})


def optional(api, table, identifier, fields=None):
    try:
        return api.get(f"{table}({guid(identifier)})", {"$select": fields} if fields else None)
    except HTTPError as error:
        if error.code != 404:
            raise
        error.close()
        return None


def relationship_contract(api):
    members = api.rows("EntityDefinitions(LogicalName='queue')/ManyToManyRelationships", {
        "$filter": "SchemaName eq 'queuemembership_association'",
        "$select": "SchemaName,Entity1LogicalName,Entity2LogicalName,Entity1NavigationPropertyName,"
                   "Entity2NavigationPropertyName,IntersectEntityName",
    })
    require(len(members) == 1, "The native queue membership relationship is missing or ambiguous.")
    row = members[0]
    entities = {row["Entity1LogicalName"], row["Entity2LogicalName"]}
    side = "Entity1" if row["Entity1LogicalName"] == "queue" else "Entity2"
    require(row["SchemaName"] == MEMBERS and entities == {"queue", "systemuser"} and row[side + "NavigationPropertyName"] == MEMBERS
            and row["IntersectEntityName"] == "queuemembership", "Unexpected native queue membership contract.")
    capacity = api.rows("EntityDefinitions(LogicalName='msdyn_bookableresourcecapacityprofile')/ManyToOneRelationships", {
        "$filter": "ReferencingAttribute eq 'msdyn_bookableresourceid' or ReferencingAttribute eq 'msdyn_capacityprofileid'",
        "$select": "ReferencingEntity,ReferencingAttribute,ReferencedEntity,ReferencedAttribute,"
                   "ReferencingEntityNavigationPropertyName",
    })
    expected = {
        ("msdyn_bookableresourceid", "bookableresource", "bookableresourceid", "msdyn_bookableresourceid"),
        ("msdyn_capacityprofileid", "msdyn_capacityprofile", "msdyn_capacityprofileid", "msdyn_capacityprofileid"),
    }
    require(len(capacity) == 2 and all(row["ReferencingEntity"] == "msdyn_bookableresourcecapacityprofile" for row in capacity)
            and {(row["ReferencingAttribute"], row["ReferencedEntity"], row["ReferencedAttribute"],
                  row["ReferencingEntityNavigationPropertyName"]) for row in capacity} == expected,
            "Unexpected native resource/capacity navigation contract.")
    return {"queue_membership": members, "resource_capacity": capacity}


def user_state(api, user_id):
    user = api.get(f"systemusers({user_id})", {"$select": USER_FIELDS})
    roles = api.rows(f"systemusers({user_id})/systemuserroles_association", {"$select": ROLE_FIELDS})
    teams = []
    for team in api.rows(f"systemusers({user_id})/teammembership_association", {"$select": "teamid,teamtype"}):
        identifier = guid(team["teamid"])
        teams.append({"team": team, "roles": api.rows(f"teams({identifier})/teamroles_association", {"$select": ROLE_FIELDS})})
    return {"user": user, "direct_roles": roles, "teams": teams}


# Shared with the workspace kit's single-representative stage.
def user_resources(api, user_id):
    return api.rows("bookableresources", {
        "$select": "bookableresourceid,_userid_value,resourcetype,statecode",
        "$filter": "_userid_value eq " + user_id,
    })


def resource_capacities(api, resource_ids):
    return api.rows(CAPACITY, {
        "$select": CAPACITY_FIELDS,
        "$filter": " or ".join("_msdyn_bookableresourceid_value eq " + identifier for identifier in resource_ids),
    }) if resource_ids else []


def voice_queue_ok(queue, queue_id):
    return (queue.get("queueid") == queue_id and queue.get("statecode") == 0
            and queue.get("msdyn_isomnichannelqueue") is True and queue.get("msdyn_queuetype") == 192350002)


def inbound_voice_workstream_ok(row, workstream_id, queue_id):
    return (row.get("msdyn_liveworkstreamid") == workstream_id and row.get("statecode") == 0
            and row.get("msdyn_direction") == 0 and row.get("msdyn_streamsource") == 192440000
            and row.get("_msdyn_defaultqueue_value") == queue_id)


def single_profile_binding_ok(bindings, workstream_id, profile_id):
    rows = [row for row in bindings if row.get("_msdyn_workstream_id_value") == workstream_id]
    return (len(rows) == 1 and rows[0].get("statecode") == 0 and rows[0].get("componentstate") == 0
            and rows[0].get("_msdyn_capacityprofile_id_value") == profile_id)


def capacity_profile_ok(row, identifier):
    return (row.get("msdyn_capacityprofileid") == identifier and row.get("statecode") == 0
            and row.get("componentstate") == 0 and type(row.get("msdyn_defaultmaxunits")) is int
            and 1 <= row["msdyn_defaultmaxunits"] <= 100 and type(row.get("msdyn_blockassignment")) is bool
            and type(row.get("msdyn_resetduration")) is int)


def capacity_body(row_id, resource_id, profile_id, max_units):
    return {
        CAPACITY_ID: row_id,
        "msdyn_name": resource_id + " " + profile_id,
        "msdyn_bookableresourceid@odata.bind": f"/bookableresources({resource_id})",
        "msdyn_capacityprofileid@odata.bind": f"/msdyn_capacityprofiles({profile_id})",
        "msdyn_maxunits": max_units, "statecode": 0, "statuscode": 1,
    }


def membership_body(organization_url, user_id):
    return {"@odata.id": organization_url + f"/api/data/v9.2/systemusers({user_id})"}


def discover(api, config):
    require(api.org == config["organization_url"] and guid(api.tenant) == config["tenant_id"],
            "Dataverse client organization/tenant differs from the configuration.")
    who = api.get("WhoAmI")
    require(guid(who["OrganizationId"]) == config["organization_id"]
            and guid(who["UserId"]) == config["administrator"]["user_id"],
            "WhoAmI does not match the exact approved organization and setup administrator.")
    profiles = sorted({row["capacity_profile_id"] for row in config["workstreams"]})
    streams = [row["workstream_id"] for row in config["workstreams"]]
    snapshot = {
        "who": who, "administrator": user_state(api, config["administrator"]["user_id"]),
        "contracts": relationship_contract(api),
        "queue": api.get(f"queues({config['queue_id']})", {"$select": QUEUE_FIELDS}),
        "members": api.rows(f"queues({config['queue_id']})/{MEMBERS}", {"$select": "systemuserid"}),
        "workstreams": {identifier: api.get(f"msdyn_liveworkstreams({identifier})") for identifier in streams},
        "profiles": {identifier: api.get(f"msdyn_capacityprofiles({identifier})") for identifier in profiles},
        "workstream_profiles": api.rows("msdyn_liveworkstreamcapacityprofiles", {
            "$filter": " or ".join("_msdyn_workstream_id_value eq " + value for value in streams),
        }),
        "profile_consumers": api.rows("msdyn_liveworkstreamcapacityprofiles", {
            "$filter": " or ".join("_msdyn_capacityprofile_id_value eq " + value for value in profiles),
        }),
        "representatives": {},
    }
    snapshot["members"] = ordered([{"systemuserid": guid(row["systemuserid"])} for row in snapshot["members"]])
    require(len({row["systemuserid"] for row in snapshot["members"]}) == len(snapshot["members"]),
            "Duplicate native queue members are ambiguous.")
    for rep in config["representatives"]:
        state = user_state(api, rep["user_id"])
        state["resources"] = user_resources(api, rep["user_id"])
        state["capacities"] = resource_capacities(api, [guid(row["bookableresourceid"]) for row in state["resources"]])
        snapshot["representatives"][rep["user_id"]] = state
    role_ids = {identifier for state in [snapshot["administrator"], *snapshot["representatives"].values()]
                for identifier in effective_roles(state)}
    snapshot["role_privileges"] = {}
    for identifier in sorted(role_ids):
        privileges = api.get(f"RetrieveRolePrivilegesRole(RoleId={identifier})")["RolePrivileges"]
        require(isinstance(privileges, list), "Native role privileges were not returned as a readable collection.")
        snapshot["role_privileges"][identifier] = privileges
    snapshot["dynamic_capacity"] = api.rows("msdyn_agentcapacityprofileunits", {
        "$select": "msdyn_agentcapacityprofileunitid,_msdyn_agentid_value,_msdyn_capacityprofileid_value,"
                   "statecode,msdyn_consumedcapacityprofileunits,msdyn_availablecapacityprofileunits",
        "$filter": " or ".join("_msdyn_agentid_value eq " + rep["user_id"] for rep in config["representatives"]),
    })
    snapshot["nonclosed_conversations"] = api.rows("msdyn_ocliveworkitems", {
        "$select": "activityid,statecode,statuscode",
        "$filter": "statuscode ne 4 or statecode eq 4", "$top": "1",
    })
    return snapshot


def effective_roles(state):
    rows = [*state["direct_roles"], *(role for team in state["teams"] for role in team["roles"])]
    result = {}
    for row in rows:
        identifier = guid(row["roleid"])
        require(isinstance(row.get("name"), str) and row["name"].strip(), "A native role name is missing.")
        require(identifier not in result or clean(result[identifier]) == clean(row), "Conflicting effective role records.")
        result[identifier] = row
    return result


def human_gates(state, expected, label):
    row, gates = state["user"], []
    valid = (
        row.get("systemuserid") == expected["user_id"]
        and row.get("azureactivedirectoryobjectid") == expected["entra_object_id"]
        and row.get("isdisabled") is False and row.get("islicensed") is True
        and type(row.get("accessmode")) is int and row["accessmode"] == 0
        and "applicationid" in row and row["applicationid"] is None
        and "applicationiduri" in row and row["applicationiduri"] in (None, "")
    )
    if not valid:
        gates.append(label + ": exact enabled, licensed, interactive non-application identity required.")
    roles = effective_roles(state)
    if any("omnichannelapiaccess" in re.sub(r"[^a-z]", "", role["name"].lower()) for role in roles.values()):
        gates.append(label + ": unsuitable Omnichannel API access role assigned directly or through a team.")
    return gates


def pair_rows(state, rep, profile):
    return [row for row in state["capacities"] if row.get("_msdyn_bookableresourceid_value") == rep["bookable_resource_id"]
            and row.get("_msdyn_capacityprofileid_value") == profile]


def assess(config, snapshot):
    gates = human_gates(snapshot["administrator"], config["administrator"], "Setup administrator")
    if not voice_queue_ok(snapshot["queue"], config["queue_id"]):
        gates.append("Select an existing active native Omnichannel Voice queue.")
    if len(config["representatives"]) < 2:
        gates.append("At least two individually approved eligible existing human representatives are required.")
    if not config["approval"]["shared_capacity_and_queue_policy_reviewed"]:
        gates.append("Administrator confirmation of shared capacity impact and existing queue policy is missing.")
    for stream in config["workstreams"]:
        row = snapshot["workstreams"][stream["workstream_id"]]
        if not inbound_voice_workstream_ok(row, stream["workstream_id"], config["queue_id"]):
            gates.append("Every selected workstream must be active inbound native voice with the exact default queue.")
        if not single_profile_binding_ok(snapshot["workstream_profiles"], stream["workstream_id"], stream["capacity_profile_id"]):
            gates.append("Every selected workstream must have exactly one active published binding to its exact profile.")
    for identifier, row in snapshot["profiles"].items():
        if not capacity_profile_ok(row, identifier):
            gates.append("Existing capacity profiles must be active/published with an explicit positive default and native policy.")
    for index, rep in enumerate(config["representatives"], 1):
        label = f"Representative {index}"
        state = snapshot["representatives"][rep["user_id"]]
        gates.extend(human_gates(state, rep, label))
        for key, message in (
            ("approved", "not individually approved"),
            ("voice_license_admin_confirmed", "administrator voice-license/service-plan confirmation missing; islicensed is not proof"),
            ("native_access_admin_confirmed", "administrator native persona/app/record-access confirmation missing"),
        ):
            if not rep[key]:
                gates.append(label + ": " + message + ".")
        roles = effective_roles(state)
        if not set(rep["required_role_ids"]) <= set(roles):
            gates.append(label + ": an explicitly required role ID is not effectively assigned.")
        names = {roles[key]["name"].strip().casefold() for key in rep["required_role_ids"] if key in roles}
        if not REQUIRED_ROLES <= names:
            gates.append(label + ": required role IDs must include Basic User, Omnichannel agent and App Profile User.")
        if any(not snapshot["role_privileges"].get(identifier) for identifier in rep["required_role_ids"]):
            gates.append(label + ": a required role has no readable privilege definition.")
        active = [row for row in state["resources"] if row.get("statecode") == 0]
        if not (len(active) == 1 and active[0].get("resourcetype") == 3
                and active[0].get("bookableresourceid") == rep["bookable_resource_id"]
                and active[0].get("_userid_value") == rep["user_id"]):
            gates.append(label + ": exactly one active User bookable resource with the approved ID is required.")
        for limit in rep["capacity_limits"]:
            rows = pair_rows(state, rep, limit["capacity_profile_id"])
            if rows and not (len(rows) == 1 and rows[0].get("statecode") == 0 and rows[0].get("statuscode") == 1
                             and "msdyn_maxunits" in rows[0] and rows[0]["msdyn_maxunits"] == limit["max_units"]
                             and type(rows[0]["msdyn_maxunits"]) is type(limit["max_units"])):
                gates.append(label + ": conflicting, inactive or duplicate capacity association; existing values are never overwritten.")
    if snapshot["nonclosed_conversations"]:
        gates.append("Drain all nonclosed conversations in the organization before changing shared routing capacity.")
    for row in snapshot["dynamic_capacity"]:
        consumed = row.get("msdyn_consumedcapacityprofileunits")
        if row.get("statecode") not in (0, 1) or type(consumed) is not int or consumed < 0:
            gates.append("Dynamic capacity consumption is unreadable; do not infer idle from missing values.")
        elif row["statecode"] == 0 and consumed:
            gates.append("A selected representative has consumed native capacity; drain work before apply.")
    return sorted(set(gates))


def render_operations(config, snapshot, plan_id):
    if assess(config, snapshot):
        return []
    capacities, memberships = [], []
    for rep in config["representatives"]:
        state = snapshot["representatives"][rep["user_id"]]
        for limit in rep["capacity_limits"]:
            profile = limit["capacity_profile_id"]
            if pair_rows(state, rep, profile):
                continue
            key = "capacity/" + rep["user_id"] + "/" + profile
            row_id = str(uuid5(UUID(plan_id), key))
            capacities.append({
                "key": key, "kind": "capacity", "user_id": rep["user_id"], "row_id": row_id,
                "method": "POST", "path": CAPACITY, "before": None,
                "body": capacity_body(row_id, rep["bookable_resource_id"], profile, limit["max_units"]),
                "resource_id": rep["bookable_resource_id"], "profile_id": profile,
            })
        if not any(row["systemuserid"] == rep["user_id"] for row in snapshot["members"]):
            memberships.append({
                "key": "membership/" + rep["user_id"], "kind": "membership", "user_id": rep["user_id"],
                "method": "POST", "path": f"queues({config['queue_id']})/{MEMBERS}/$ref",
                "body": membership_body(config["organization_url"], rep["user_id"]),
                "before": {"present": False},
            })
    return capacities + memberships


def build_plan(api, config):
    config = validate_config(config)
    before = discover(api, config)
    plan_id = str(uuid4())
    return {
        "version": VERSION, "kind": "native-routing-plan", "created_at": utc(), "plan_id": plan_id,
        "config": config, "before": before, "gates": assess(config, before),
        "operations": render_operations(config, before, plan_id),
    }


def validate_plan(plan):
    shape(plan, {"version", "kind", "created_at", "plan_id", "config", "before", "gates", "operations"})
    require(type(plan["version"]) is int and plan["version"] == VERSION
            and plan["kind"] == "native-routing-plan", "Unexpected routing plan.")
    timestamp(plan["created_at"])
    guid(plan["plan_id"])
    require(validate_config(plan["config"]) == plan["config"], "Configuration is not normalized.")
    require(plan["gates"] == assess(plan["config"], plan["before"])
            and plan["operations"] == render_operations(plan["config"], plan["before"], plan["plan_id"]),
            "The immutable plan does not match its approved scope and native payloads.")


class Journal:
    """Exclusive append-only receipts; a pending intent is never implicitly retried."""

    def __init__(self, directory, plan):
        self.directory, self.plan_hash = Path(directory), digest(plan)
        self.operations = {row["key"]: row for row in plan["operations"]}
        self.sequence, self.previous, self.latest = 0, None, None
        self.completed, self.pending = {}, None
        for path in sorted(self.directory.glob("receipt-*.json")):
            self.accept(json.loads(read_private(path)))

    def accept(self, event):
        shape(event, {"version", "sequence", "previous", "plan_sha256", "at", "kind", "data"})
        require(type(event["version"]) is int and event["version"] == VERSION and event["plan_sha256"] == self.plan_hash
                and type(event["sequence"]) is int
                and event["sequence"] == self.sequence + 1 and event["previous"] == self.previous,
                "Receipt chain is incomplete, modified or belongs to a different plan.")
        timestamp(event["at"])
        data, kind = event["data"], event["kind"]
        if kind == "before":
            op = data["operation"]
            require(not self.pending and op == self.operations.get(op["key"]) and op["key"] not in self.completed,
                    "Receipt contains an unapproved or duplicate operation.")
            self.pending = {"operation": op, "started_at": event["at"], "acknowledged": False, "rejected": False}
        elif kind in {"acknowledged", "failure", "after", "not_applied"}:
            require(self.pending and data["key"] == self.pending["operation"]["key"], "Receipt has no matching pending intent.")
            if kind == "acknowledged":
                self.pending["acknowledged"] = True
            elif kind == "failure":
                require(type(data["definitively_rejected"]) is bool, "Invalid request rejection evidence.")
                self.pending["rejected"] = data["definitively_rejected"]
            elif kind == "after":
                self.completed[data["key"]] = {
                    "operation": self.pending["operation"], "before": self.pending["operation"]["before"],
                    "after": data["after"], "ownership": data["ownership"], "confirmed_at": event["at"],
                }
                self.pending = None
            else:
                require(self.pending["rejected"] and not self.pending["acknowledged"],
                        "Only a definitively rejected, unacknowledged request can be cleared.")
                self.pending = None
        else:
            require(kind in {"review", "verified", "recovery_observation", "rollback_plan"}, "Unknown receipt type.")
        self.sequence, self.previous, self.latest = event["sequence"], digest(event), event

    def persist(self, event):
        write_private(self.directory / f"receipt-{event['sequence']:06d}.json", json.dumps(event, indent=2, ensure_ascii=False))

    def save(self, kind, data):
        event = {
            "version": VERSION, "sequence": self.sequence + 1, "previous": self.previous,
            "plan_sha256": self.plan_hash, "at": utc(), "kind": kind, "data": copy.deepcopy(data),
        }
        self.persist(event)
        self.accept(event)
        return event


class Routing:
    def __init__(self, api, plan, journal):
        validate_plan(plan)
        require(journal.plan_hash == digest(plan), "Receipt belongs to another routing plan.")
        self.api, self.plan, self.journal, self.config = api, plan, journal, plan["config"]

    def expected(self, additional=None):
        snapshot = copy.deepcopy(self.plan["before"])
        outcomes = list(self.journal.completed.values()) + ([additional] if additional else [])
        for item in outcomes:
            op = item["operation"]
            if op["kind"] == "capacity":
                snapshot["representatives"][op["user_id"]]["capacities"].append(item["after"])
            else:
                snapshot["members"].append({"systemuserid": op["user_id"]})
        return snapshot

    def unchanged(self, snapshot, additional=None):
        require(configuration(snapshot) == configuration(self.expected(additional)),
                "Native identity/roles/policy/membership/capacity state drifted from the reviewed plan and receipts.")

    def review(self):
        require(not self.journal.pending, "An outstanding write needs read-only recovery before a new review.")
        snapshot = discover(self.api, self.config)
        self.unchanged(snapshot)
        gates = assess(self.config, snapshot)
        if self.plan["gates"]:
            gates.append("The initial plan was audit-only and gated; resolve prerequisites and create a new plan.")
        return self.journal.save("review", {"snapshot": snapshot, "gates": gates})

    def outcome(self, snapshot, op):
        if op["kind"] == "membership":
            return {"present": True} if any(row["systemuserid"] == op["user_id"] for row in snapshot["members"]) else None
        rows = [row for row in snapshot["representatives"][op["user_id"]]["capacities"]
                if row.get("_msdyn_bookableresourceid_value") == op["resource_id"]
                and row.get("_msdyn_capacityprofileid_value") == op["profile_id"]]
        if not rows:
            return None
        expected = {
            CAPACITY_ID: op["row_id"], "_msdyn_bookableresourceid_value": op["resource_id"],
            "_msdyn_capacityprofileid_value": op["profile_id"],
            "msdyn_name": op["body"]["msdyn_name"], "msdyn_maxunits": op["body"]["msdyn_maxunits"],
            "statecode": 0, "statuscode": 1,
        }
        require(len(rows) == 1 and all(key in rows[0] and rows[0][key] == value
                                     and type(rows[0][key]) is type(value) for key, value in expected.items()),
                "The pending capacity association has a conflicting ID, binding, state or limit.")
        row = rows[0]
        pending = self.journal.pending
        require(row.get("_createdby_value") == self.config["administrator"]["user_id"]
                and timestamp(pending["started_at"]) - timedelta(seconds=5) <= timestamp(row.get("createdon"))
                <= datetime.now(timezone.utc) + timedelta(seconds=5),
                "The reserved capacity row lacks matching creator/time ownership evidence.")
        require(re.fullmatch(r'W/"\d+"', row.get("@odata.etag", "")), "Native capacity readback lacks an ETag.")
        return row

    def finish(self, snapshot):
        op = self.journal.pending["operation"]
        after = self.outcome(snapshot, op)
        require(after is not None, "Write is unconfirmed: persisted association is not visible; run recover, not apply.")
        self.unchanged(snapshot, {"operation": op, "after": after})
        self.journal.save("after", {
            "key": op["key"], "after": after,
            "ownership": "created-capacity-row" if op["kind"] == "capacity" else "shared-membership-no-exclusive-ownership",
        })

    def recover(self):
        require(self.journal.pending, "There is no outstanding mutation to recover.")
        snapshot = discover(self.api, self.config)
        op = self.journal.pending["operation"]
        after = self.outcome(snapshot, op)
        if after is not None:
            self.finish(snapshot)
            return {"status": "persisted_outcome_recovered", "mutations": 0}
        self.unchanged(snapshot)
        # A late create under the reserved ID must not be hidden by changed lookups.
        if op["kind"] == "capacity":
            require(optional(self.api, CAPACITY, op["row_id"], CAPACITY_FIELDS) is None,
                    "Reserved capacity ID exists outside the expected resource/profile pair.")
        if self.journal.pending["rejected"] and not self.journal.pending["acknowledged"]:
            self.journal.save("not_applied", {"key": op["key"], "observed": None})
            return {"status": "rejection_confirmed_re_review_before_retry", "mutations": 0}
        self.journal.save("recovery_observation", {"key": op["key"], "observed": None, "in_doubt": True})
        return {"status": "in_doubt_no_replay_allowed", "mutations": 0}

    def verification(self, snapshot):
        gates = assess(self.config, snapshot)
        if self.journal.pending:
            gates.append("Outstanding mutation requires recovery.")
        try:
            self.unchanged(snapshot)
        except ValueError as error:
            gates.append(str(error))
        missing = []
        for index, rep in enumerate(self.config["representatives"], 1):
            if not any(row["systemuserid"] == rep["user_id"] for row in snapshot["members"]):
                missing.append(f"Representative {index}: queue membership missing.")
            state = snapshot["representatives"][rep["user_id"]]
            for limit in rep["capacity_limits"]:
                if not pair_rows(state, rep, limit["capacity_profile_id"]):
                    missing.append(f"Representative {index}: capacity association missing.")
        return {"configuration_verified": not gates and not missing, "gates": gates, "missing": missing,
                "live_distribution_verified": False, "full_item4_acceptance": False}

    def verify(self):
        snapshot = discover(self.api, self.config)
        result = self.verification(snapshot)
        self.journal.save("verified", {"snapshot": snapshot, "result": result})
        return result

    def apply(self, *, confirm_organization, reviewed_plan_sha256, reviewed_receipt_sha256, maintenance_window_confirmed):
        require(confirm_organization == self.config["organization_url"] and maintenance_window_confirmed is True,
                "Explicit exact organization and drained/no-concurrent-edit maintenance-window confirmations are required.")
        require(reviewed_plan_sha256 == digest(self.plan), "Reviewed plan digest does not match.")
        require(not self.journal.pending, "An outstanding mutation requires recover; automatic replay is forbidden.")
        review = self.journal.latest
        require(review and review["kind"] == "review" and digest(review) == reviewed_receipt_sha256,
                "A fresh, explicitly reviewed latest review receipt is required.")
        require(not review["data"]["gates"] and not self.plan["gates"], "Eligibility/approval gates block apply.")

        def fresh():
            age = (datetime.now(timezone.utc) - timestamp(review["at"])).total_seconds()
            require(0 <= age <= REVIEW_SECONDS, "Reviewed state is stale or future-dated; perform and review a new read-only review.")

        fresh()
        snapshot = discover(self.api, self.config)
        self.unchanged(snapshot)
        require(configuration(snapshot) == configuration(review["data"]["snapshot"]), "Reviewed state no longer matches live state.")
        mutations = 0
        for op in self.plan["operations"]:
            if op["key"] in self.journal.completed:
                continue
            if mutations:
                snapshot = discover(self.api, self.config)
            self.unchanged(snapshot)
            require(not assess(self.config, snapshot), "Current eligibility or idle-call guards block apply; review native state.")
            fresh()
            if op["kind"] == "capacity":
                require(optional(self.api, CAPACITY, op["row_id"], CAPACITY_FIELDS) is None,
                        "The reserved capacity ID already exists without a matching ownership receipt; never adopt it.")
            self.journal.save("before", {"operation": op, "configuration_sha256": digest(configuration(snapshot))})
            try:
                response = self.api.request(op["path"], method="POST", body=op["body"])
                self.journal.save("acknowledged", {"key": op["key"], "response": response})
                mutations += 1
                snapshot = discover(self.api, self.config)
                self.finish(snapshot)
            except Exception as error:
                code = error.code if isinstance(error, HTTPError) else None
                if isinstance(error, HTTPError):
                    error.close()
                self.journal.save("failure", {
                    "key": op["key"], "error_type": type(error).__name__, "http_status": code,
                    "definitively_rejected": code in DEFINITIVE_REJECTIONS and not self.journal.pending["acknowledged"],
                })
                raise ValueError("Mutation/readback was not fully confirmed. Keep the receipt and run read-only recover before any retry.") from error
        result = self.verification(snapshot)
        self.journal.save("verified", {"snapshot": snapshot, "result": result})
        return {**result, "mutations": mutations}

    def rollback_plan(self):
        snapshot = discover(self.api, self.config)
        gates = assess(self.config, snapshot)
        if self.journal.pending:
            gates.append("Outstanding mutation requires recovery; no deletion candidate is safe.")
        try:
            self.unchanged(snapshot)
        except ValueError as error:
            gates.append(str(error))
        candidates, retained = [], []
        for item in self.journal.completed.values():
            op = item["operation"]
            if op["kind"] == "membership":
                retained.append({"user_id": op["user_id"], "reason": "Shared N:N membership has no exclusive creator/ETag ownership; retain for administrator review."})
                continue
            current = optional(self.api, CAPACITY, op["row_id"], CAPACITY_FIELDS)
            if gates or not current or clean(current) != clean(item["after"]) \
                    or current.get("@odata.etag") != item["after"].get("@odata.etag"):
                retained.append({"capacity_association_id": op["row_id"], "reason": "Changed row, pending intent, eligibility drift or idle guard; retain."})
                continue
            candidates.append({
                "method": "DELETE", "path": f"{CAPACITY}({op['row_id']})",
                "headers": {"If-Match": current["@odata.etag"]}, "before": item["before"], "after": current,
                "manual_only": True, "requires_fresh_idle_and_shared_impact_review": True,
            })
        result = {
            "mutations": 0, "automatic_rollback_supported": False, "gates": gates,
            "manual_conditional_capacity_candidates": candidates, "retain": retained,
            "preexisting_members_and_capacities": "Retain; never owned by this plan.",
        }
        self.journal.save("rollback_plan", {"original": self.plan["before"], "observed": snapshot,
                                           "ownership": self.journal.completed, "result": result})
        return result


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    mode = result.add_mutually_exclusive_group()
    for action in ("plan", "review", "apply", "verify", "recover", "rollback-plan"):
        mode.add_argument("--" + action, dest="mode", action="store_const", const=action)
    result.set_defaults(mode="plan")
    result.add_argument("--config", type=Path)
    result.add_argument("--directory", required=True, type=Path)
    result.add_argument("--confirm-organization", type=organization)
    result.add_argument("--reviewed-plan-sha256")
    result.add_argument("--reviewed-receipt-sha256")
    result.add_argument("--maintenance-window-confirmed", action="store_true")
    return result


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        directory = private_path(args.directory)
        if args.mode != "apply":
            require(not (args.confirm_organization or args.reviewed_plan_sha256 or args.reviewed_receipt_sha256
                         or args.maintenance_window_confirmed), "Mutation confirmations are valid only with --apply.")
        if args.mode == "plan":
            require(args.config is not None, "--config is required for read-only planning.")
            require(not directory.exists(), "Use a new private plan directory; existing ownership receipts must not be overwritten.")
            config = validate_config(json.loads(read_private(args.config)))
            api = Dataverse(config["organization_url"], config["tenant_id"])
            plan = build_plan(api, config)
            directory.mkdir(mode=0o700)
            write_private(directory / "plan.json", json.dumps(plan, indent=2, ensure_ascii=False))
            journal = Journal(directory, plan)
            journal.save("review", {"snapshot": plan["before"], "gates": plan["gates"]})
            outcome = {"gates": plan["gates"], "planned_mutations": len(plan["operations"]), "mutations": 0}
        else:
            require(args.config is None, "Reuse the immutable plan configuration; do not supply --config for an existing plan.")
            require(directory.is_dir() and (os.name == "nt" or not directory.stat().st_mode & 0o077),
                    "An existing owner-only private plan directory is required.")
            plan = json.loads(read_private(directory / "plan.json"))
            validate_plan(plan)
            journal = Journal(directory, plan)
            config = plan["config"]
            api = Dataverse(config["organization_url"], config["tenant_id"])
            routing = Routing(api, plan, journal)
            if args.mode == "apply":
                outcome = routing.apply(
                    confirm_organization=args.confirm_organization, reviewed_plan_sha256=args.reviewed_plan_sha256,
                    reviewed_receipt_sha256=args.reviewed_receipt_sha256,
                    maintenance_window_confirmed=args.maintenance_window_confirmed,
                )
            elif args.mode == "review":
                event = routing.review()
                outcome = {"gates": event["data"]["gates"], "mutations": 0}
            else:
                outcome = getattr(routing, args.mode.replace("-", "_"))()
        # Detailed identity/before/after material stays in the private receipts.
        summary = {key: outcome[key] for key in (
            "gates", "missing", "planned_mutations", "mutations", "status", "configuration_verified",
        ) if key in outcome}
        print(json.dumps({"mode": args.mode, "plan_sha256": digest(plan), "receipt_sha256": journal.previous, **summary}))
        if outcome.get("configuration_verified") is False or outcome.get("status") == "in_doubt_no_replay_allowed":
            return 1
        return 0
    except (ValueError, KeyError, TypeError, OSError, subprocess.SubprocessError) as error:
        message = str(error)
        if isinstance(error, HTTPError):
            message = f"Dataverse HTTP {error.code}; no automatic retry."
            error.close()
        elif isinstance(error, subprocess.SubprocessError):
            message = "Azure CLI token acquisition failed; check the authorized administrator login."
        print(json.dumps({"error": message, "status": "blocked_or_incomplete"}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
