"""Offline native routing payload, eligibility, freshness, ownership and recovery contracts."""

import copy
import io
import json
import os
from pathlib import Path
import re
import shutil
import sys
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from urllib.error import HTTPError
from uuid import NAMESPACE_URL, uuid4, uuid5


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "infra/scripts"))
import setup_dynamics_routing as setup


def uid(name):
    return str(uuid5(NAMESPACE_URL, "https://offline.invalid/native-routing/" + name))


def config():
    roles = [uid("basic"), uid("agent"), uid("app-profile")]
    return setup.validate_config({
        "version": 1, "organization_url": "https://example.crm.dynamics.com",
        "tenant_id": uid("tenant"), "organization_id": uid("organization"),
        "administrator": {"user_id": uid("admin"), "entra_object_id": uid("admin-entra")},
        "queue_id": uid("queue"),
        "workstreams": [{"workstream_id": uid(name), "capacity_profile_id": uid("inbound-profile")}
                        for name in ("inbound-one", "inbound-two")],
        "representatives": [{
            "user_id": uid(name), "entra_object_id": uid(name + "-entra"),
            "bookable_resource_id": uid(name + "-resource"), "approved": True,
            "voice_license_admin_confirmed": True, "native_access_admin_confirmed": True,
            "required_role_ids": roles.copy(),
            "capacity_limits": [{"capacity_profile_id": uid("inbound-profile"), "max_units": None}],
        } for name in ("first-human", "second-human")],
        "approval": {"reference": "offline-approved-change", "shared_capacity_and_queue_policy_reviewed": True},
    })


def http_error(code):
    return HTTPError("https://offline.invalid", code, "synthetic failure", {}, io.BytesIO())


class FakeAPI:
    def __init__(self, cfg=None):
        self.cfg = cfg or config()
        self.org, self.tenant = self.cfg["organization_url"], self.cfg["tenant_id"]
        self.who = {"OrganizationId": self.cfg["organization_id"], "UserId": self.cfg["administrator"]["user_id"],
                    "BusinessUnitId": uid("business-unit")}
        self.reads, self.writes, self.changed = [], [], []
        self.fail_before, self.fail_after, self.fail_read_once, self.after_write = {}, {}, False, None
        self.records = {table: {} for table in (
            "systemusers", "queues", "msdyn_liveworkstreams", "msdyn_capacityprofiles", setup.CAPACITY,
        )}
        self.roles, self.user_roles, self.user_teams, self.team_roles = {}, {}, {}, {}
        self.privileges = {}
        self.resources, self.bindings, self.dynamic, self.conversations = [], [], [], []
        self.members = {uid("first-human"), uid("untouched-existing-member")}
        for expected in [self.cfg["administrator"], *self.cfg["representatives"]]:
            identifier = expected["user_id"]
            self.records["systemusers"][identifier] = {
                "systemuserid": identifier, "azureactivedirectoryobjectid": expected["entra_object_id"],
                "applicationid": None, "applicationiduri": None, "isdisabled": False,
                "islicensed": True, "accessmode": 0, "_businessunitid_value": uid("business-unit"),
                "@odata.etag": 'W/"1"',
            }
            self.user_roles[identifier], self.user_teams[identifier] = [], []
        for key, name in (("basic", "Basic User"), ("agent", "Omnichannel agent"), ("app-profile", "App Profile User"),
                          ("admin-role", "System Administrator"), ("api-role", "Omnichannel API access")):
            self.roles[uid(key)] = {"roleid": uid(key), "name": name, "_businessunitid_value": uid("business-unit"),
                                    "_parentrootroleid_value": uid(key), "@odata.etag": 'W/"2"'}
            self.privileges[uid(key)] = [{"PrivilegeId": uid(key + "-privilege"), "Depth": "Global"}]
        self.user_roles[uid("admin")] = [uid("admin-role")]
        self.user_roles[uid("first-human")] = [uid("basic"), uid("agent"), uid("app-profile")]
        self.user_roles[uid("second-human")] = [uid("basic"), uid("agent")]
        self.user_teams[uid("second-human")] = [{"teamid": uid("human-team"), "teamtype": 2}]
        self.team_roles[uid("human-team")] = [uid("app-profile")]
        queue = {field: None for field in setup.QUEUE_FIELDS.split(",")}
        queue.update({
            "queueid": uid("queue"), "name": "Offline voice queue", "statecode": 0,
            "msdyn_isomnichannelqueue": True, "msdyn_queuetype": 192350002,
            "msdyn_assignmentstrategy": 192350003, "msdyn_priority": 1,
            "msdyn_operatinghoursbasedassignment": False, "msdyn_processqueueperiodically": False,
            "_msdyn_assignmentinputcontractid_value": uid("untouched-routing-contract"), "@odata.etag": 'W/"3"',
        })
        self.records["queues"][uid("queue")] = queue
        profile = {
            "msdyn_capacityprofileid": uid("inbound-profile"), "msdyn_name": "Offline inbound profile",
            "statecode": 0, "componentstate": 0, "msdyn_defaultmaxunits": 1,
            "msdyn_blockassignment": True, "msdyn_resetduration": 192350000, "@odata.etag": 'W/"4"',
        }
        self.records["msdyn_capacityprofiles"][uid("inbound-profile")] = profile
        for index, name in enumerate(("inbound-one", "inbound-two", "shared-outside-scope")):
            self.records["msdyn_liveworkstreams"][uid(name)] = {
                "msdyn_liveworkstreamid": uid(name), "msdyn_name": name, "statecode": 0,
                "msdyn_direction": 0, "msdyn_streamsource": 192440000,
                "_msdyn_defaultqueue_value": uid("queue"), "_msdyn_bot_user_value": uid("existing-bot"),
                "@odata.etag": 'W/"5"',
            }
            self.bindings.append({
                "msdyn_liveworkstreamcapacityprofileid": uid("binding-" + str(index)),
                "_msdyn_workstream_id_value": uid(name), "_msdyn_capacityprofile_id_value": uid("inbound-profile"),
                "statecode": 0, "componentstate": 0, "@odata.etag": 'W/"6"',
            })
        for name in ("first-human", "second-human"):
            self.resources.append({
                "bookableresourceid": uid(name + "-resource"), "_userid_value": uid(name),
                "resourcetype": 3, "statecode": 0, "@odata.etag": 'W/"7"',
            })
        self.add_capacity(uid("preexisting-capacity"), uid("first-human-resource"), uid("inbound-profile"), None)
        self.add_capacity(uid("unrelated-capacity"), uid("first-human-resource"), uid("unrelated-profile"), 3)
        self.dynamic.append({
            "msdyn_agentcapacityprofileunitid": uid("dynamic-capacity"),
            "_msdyn_agentid_value": uid("first-human"), "_msdyn_capacityprofileid_value": uid("inbound-profile"),
            "statecode": 0, "msdyn_consumedcapacityprofileunits": 0, "msdyn_availablecapacityprofileunits": 1,
        })
        self.member_contract = [{
            "SchemaName": setup.MEMBERS, "Entity1LogicalName": "queue", "Entity2LogicalName": "systemuser",
            "Entity1NavigationPropertyName": setup.MEMBERS, "Entity2NavigationPropertyName": setup.MEMBERS,
            "IntersectEntityName": "queuemembership",
        }]
        self.capacity_contract = [{
            "ReferencingEntity": "msdyn_bookableresourcecapacityprofile",
            "ReferencingAttribute": attribute, "ReferencedEntity": entity, "ReferencedAttribute": primary,
            "ReferencingEntityNavigationPropertyName": attribute,
        } for attribute, entity, primary in (
            ("msdyn_bookableresourceid", "bookableresource", "bookableresourceid"),
            ("msdyn_capacityprofileid", "msdyn_capacityprofile", "msdyn_capacityprofileid"),
        )]

    def add_capacity(self, identifier, resource, profile, units):
        now = setup.utc()
        self.records[setup.CAPACITY][identifier] = {
            setup.CAPACITY_ID: identifier, "_msdyn_bookableresourceid_value": resource,
            "_msdyn_capacityprofileid_value": profile, "msdyn_maxunits": units,
            "msdyn_name": resource + " " + profile, "statecode": 0, "statuscode": 1,
            "_ownerid_value": uid("admin"), "_owningbusinessunit_value": uid("business-unit"),
            "_createdby_value": uid("admin"), "createdon": now, "modifiedon": now, "@odata.etag": 'W/"8"',
        }

    def project(self, row, query):
        if query and "$select" in query:
            fields = {*query["$select"].split(","), "@odata.etag"}
            return copy.deepcopy({key: value for key, value in row.items() if key in fields})
        return copy.deepcopy(row)

    def get(self, path, query=None):
        self.reads.append(("GET", path, copy.deepcopy(query)))
        if self.fail_read_once and self.changed:
            self.fail_read_once = False
            raise ConnectionError("synthetic readback interruption")
        if path == "WhoAmI":
            return copy.deepcopy(self.who)
        if path.startswith("RetrieveRolePrivilegesRole(RoleId="):
            identifier = path.removeprefix("RetrieveRolePrivilegesRole(RoleId=").removesuffix(")")
            return {"RolePrivileges": copy.deepcopy(self.privileges[identifier])}
        match = re.fullmatch(r"([a-z0-9_]+)\(([^)]+)\)", path)
        if not match or match[1] not in self.records:
            raise AssertionError("Unexpected native GET: " + path)
        row = self.records[match[1]].get(match[2])
        if row is None:
            raise http_error(404)
        return {**self.project(row, query), "@odata.context": self.org + "/api/data/v9.2/$metadata#" + match[1] + "/$entity"}

    def rows(self, path, query):
        self.reads.append(("GET", path, copy.deepcopy(query)))
        if path.startswith("EntityDefinitions"):
            rows = self.member_contract if "/ManyToManyRelationships" in path else self.capacity_contract
        elif path == f"queues({uid('queue')})/{setup.MEMBERS}":
            rows = [{"systemuserid": identifier} for identifier in sorted(self.members)]
        elif "/systemuserroles_association" in path:
            user = path.split("(", 1)[1].split(")", 1)[0]
            rows = [self.roles[identifier] for identifier in self.user_roles[user]]
        elif "/teammembership_association" in path:
            rows = self.user_teams[path.split("(", 1)[1].split(")", 1)[0]]
        elif "/teamroles_association" in path:
            rows = [self.roles[identifier] for identifier in self.team_roles[path.split("(", 1)[1].split(")", 1)[0]]]
        elif path == "msdyn_liveworkstreamcapacityprofiles":
            field = "_msdyn_workstream_id_value" if query["$filter"].startswith("_msdyn_workstream_id_value") else "_msdyn_capacityprofile_id_value"
            identifiers = re.findall(r"eq ([0-9a-f-]+)", query["$filter"])
            rows = [row for row in self.bindings if row[field] in identifiers]
        elif path == "bookableresources":
            rows = [row for row in self.resources if row["_userid_value"] == query["$filter"].split(" eq ")[1]]
        elif path == setup.CAPACITY:
            identifiers = re.findall(r"eq ([0-9a-f-]+)", query["$filter"])
            rows = [row for row in self.records[setup.CAPACITY].values() if row["_msdyn_bookableresourceid_value"] in identifiers]
        elif path == "msdyn_agentcapacityprofileunits":
            rows = self.dynamic
        elif path == "msdyn_ocliveworkitems":
            assert query["$filter"] == "statuscode ne 4 or statecode eq 4"
            assert query["$top"] == "1"
            rows = [row for row in self.conversations if row["statuscode"] != 4 or row["statecode"] == 4][:1]
        else:
            raise AssertionError("Unexpected native collection GET: " + path)
        return [self.project(row, query) for row in rows]

    def request(self, path, *, method="GET", body=None, headers=None):
        if method != "POST" or headers is not None:
            raise AssertionError("Unexpected native write method/headers.")
        self.writes.append({"path": path, "method": method, "body": copy.deepcopy(body)})
        attempt = len(self.writes)
        if attempt in self.fail_before:
            raise self.fail_before.pop(attempt)
        if path == setup.CAPACITY:
            expected_keys = {
                setup.CAPACITY_ID, "msdyn_name", "msdyn_maxunits", "statecode", "statuscode",
                "msdyn_bookableresourceid@odata.bind", "msdyn_capacityprofileid@odata.bind",
            }
            if set(body) != expected_keys or body["statecode"] != 0 or body["statuscode"] != 1:
                raise AssertionError("Unexpected native capacity create payload.")
            if body[setup.CAPACITY_ID] in self.records[setup.CAPACITY]:
                raise http_error(409)
            resource = re.fullmatch(r"/bookableresources\(([^)]+)\)", body["msdyn_bookableresourceid@odata.bind"])[1]
            profile = re.fullmatch(r"/msdyn_capacityprofiles\(([^)]+)\)", body["msdyn_capacityprofileid@odata.bind"])[1]
            self.add_capacity(body[setup.CAPACITY_ID], resource, profile, body["msdyn_maxunits"])
            assert body["msdyn_name"] == resource + " " + profile
        elif path == f"queues({uid('queue')})/{setup.MEMBERS}/$ref":
            assert set(body) == {"@odata.id"}
            prefix = self.org + "/api/data/v9.2/systemusers("
            assert body["@odata.id"].startswith(prefix) and body["@odata.id"].endswith(")")
            self.members.add(body["@odata.id"][len(prefix):-1])
        else:
            raise AssertionError("Only exact native membership/capacity creates are allowed.")
        self.changed.append(copy.deepcopy(self.writes[-1]))
        if self.after_write:
            self.after_write(self, attempt)
        if attempt in self.fail_after:
            raise self.fail_after.pop(attempt)
        return {}


class MemoryJournal(setup.Journal):
    def __init__(self, plan):
        self.events = []
        super().__init__(ROOT / ".unused-native-routing-receipts", plan)

    def persist(self, event):
        self.events.append(copy.deepcopy(event))


class RoutingTests(unittest.TestCase):
    def setUp(self):
        self.cfg = config()
        self.api = FakeAPI(self.cfg)

    def start(self):
        self.plan = setup.build_plan(self.api, self.cfg)
        self.journal = MemoryJournal(self.plan)
        self.journal.save("review", {"snapshot": self.plan["before"], "gates": self.plan["gates"]})
        self.routing = setup.Routing(self.api, self.plan, self.journal)
        return self.routing

    def apply(self, **overrides):
        kwargs = {
            "confirm_organization": self.cfg["organization_url"], "reviewed_plan_sha256": setup.digest(self.plan),
            "reviewed_receipt_sha256": self.journal.previous, "maintenance_window_confirmed": True,
        }
        kwargs.update(overrides)
        return self.routing.apply(**kwargs)

    def assert_gate(self, fragment):
        self.start()
        self.assertIn(fragment.lower(), " ".join(self.plan["gates"]).lower())
        self.assertEqual(self.plan["operations"], [])
        with self.assertRaisesRegex(ValueError, "gates"):
            self.apply()
        self.assertFalse(self.api.writes)

    def test_default_is_read_only_and_one_human_can_only_audit(self):
        self.assertEqual(setup.parser().parse_args(["--directory", "private-plan"]).mode, "plan")
        self.cfg["representatives"] = self.cfg["representatives"][:1]
        self.assert_gate("at least two")
        self.assertTrue(all(call[0] == "GET" for call in self.api.reads))

    def test_exact_native_create_and_membership_contracts_preserve_shared_state(self):
        original = copy.deepcopy(self.api.records)
        original_members = self.api.members.copy()
        self.start()
        self.assertEqual(self.plan["gates"], [])
        self.assertEqual([op["kind"] for op in self.plan["operations"]], ["capacity", "membership"])
        self.assertEqual(len(self.plan["before"]["profile_consumers"]), 3)
        self.assertFalse(self.api.writes)
        result = self.apply()
        self.assertTrue(result["configuration_verified"])
        self.assertFalse(result["live_distribution_verified"])
        self.assertFalse(result["full_item4_acceptance"])
        self.assertEqual(result["mutations"], 2)
        capacity, membership = self.api.writes
        self.assertEqual(capacity, {
            "method": "POST", "path": setup.CAPACITY, "body": {
                setup.CAPACITY_ID: self.plan["operations"][0]["row_id"],
                "msdyn_name": uid("second-human-resource") + " " + uid("inbound-profile"),
                "msdyn_bookableresourceid@odata.bind": f"/bookableresources({uid('second-human-resource')})",
                "msdyn_capacityprofileid@odata.bind": f"/msdyn_capacityprofiles({uid('inbound-profile')})",
                "msdyn_maxunits": None, "statecode": 0, "statuscode": 1,
            },
        })
        self.assertEqual(membership, {
            "method": "POST", "path": f"queues({uid('queue')})/queuemembership_association/$ref",
            "body": {"@odata.id": self.api.org + f"/api/data/v9.2/systemusers({uid('second-human')})"},
        })
        for table in original:
            for identifier, before in original[table].items():
                self.assertEqual(self.api.records[table][identifier], before)
        self.assertEqual(self.api.members, original_members | {uid("second-human")})
        self.assertEqual([event["kind"] for event in self.journal.events],
                         ["review", "before", "acknowledged", "after", "before", "acknowledged", "after", "verified"])
        outcomes = list(self.journal.completed.values())
        self.assertIsNone(outcomes[0]["before"])
        self.assertEqual(outcomes[0]["after"][setup.CAPACITY_ID], capacity["body"][setup.CAPACITY_ID])
        self.assertEqual(outcomes[0]["ownership"], "created-capacity-row")
        self.assertEqual(outcomes[1]["before"], {"present": False})
        self.assertEqual(outcomes[1]["after"], {"present": True})
        self.assertEqual(outcomes[1]["ownership"], "shared-membership-no-exclusive-ownership")
        self.assertFalse(any("agentcapacityprofileunits" in row["path"] for row in self.api.writes))

    def test_strict_configuration_and_explicit_capacity_limits(self):
        for value in (True, 0, -1, 101, 1.5, "1"):
            with self.subTest(units=value):
                cfg = config()
                cfg["representatives"][0]["capacity_limits"][0]["max_units"] = value
                with self.assertRaisesRegex(ValueError, "max_units"):
                    setup.validate_config(cfg)
        for field in ("user_id", "entra_object_id", "bookable_resource_id"):
            with self.subTest(duplicate=field):
                cfg = config()
                cfg["representatives"][1][field] = cfg["representatives"][0][field]
                with self.assertRaisesRegex(ValueError, "duplicate"):
                    setup.validate_config(cfg)
        cfg = config()
        cfg["invented_policy"] = True
        with self.assertRaisesRegex(ValueError, "properties"):
            setup.validate_config(cfg)
        cfg = config()
        cfg["representatives"][0]["capacity_limits"][0]["capacity_profile_id"] = uid("unapproved-profile")
        with self.assertRaisesRegex(ValueError, "exactly the selected"):
            setup.validate_config(cfg)
        for path in (ROOT / "docs/dynamics-native-voice/examples/routing-config.example.json",):
            with self.assertRaises(ValueError):
                setup.validate_config(json.loads(path.read_text()))

    def test_generic_license_flag_is_not_voice_entitlement_approval(self):
        self.cfg["representatives"][1]["voice_license_admin_confirmed"] = False
        self.assertTrue(self.api.records["systemusers"][uid("second-human")]["islicensed"])
        self.assert_gate("islicensed is not proof")

    def test_every_requested_human_and_shared_scope_must_be_explicitly_approved(self):
        for flag, fragment in (("approved", "not individually approved"),
                               ("native_access_admin_confirmed", "persona/app/record-access")):
            with self.subTest(flag=flag):
                self.setUp()
                self.cfg["representatives"][1][flag] = False
                self.assert_gate(fragment)
        self.setUp()
        self.cfg["approval"]["shared_capacity_and_queue_policy_reviewed"] = False
        self.assert_gate("shared capacity impact")

    def test_rejects_disabled_unlicensed_application_noninteractive_and_mismatched_humans(self):
        for field, value in (
            ("isdisabled", True), ("islicensed", False), ("accessmode", 4), ("accessmode", False),
            ("applicationid", uid("app")), ("applicationiduri", "https://offline.invalid/app"),
            ("azureactivedirectoryobjectid", uid("different-human")), ("systemuserid", uid("different-user")),
        ):
            with self.subTest(field=field, value=value):
                self.setUp()
                self.api.records["systemusers"][uid("second-human")][field] = value
                self.assert_gate("interactive non-application identity")

    def test_direct_and_inherited_api_access_roles_are_both_rejected(self):
        for target in ("direct", "team", "administrator"):
            with self.subTest(target=target):
                self.setUp()
                if target == "team":
                    self.api.team_roles[uid("human-team")].append(uid("api-role"))
                else:
                    self.api.user_roles[uid("admin" if target == "administrator" else "second-human")].append(uid("api-role"))
                self.assert_gate("unsuitable Omnichannel API access")

    def test_exact_role_ids_and_native_names_are_required(self):
        for change in ("missing", "same-name-wrong-id", "renamed", "inherited-missing"):
            with self.subTest(change=change):
                self.setUp()
                if change == "missing":
                    self.api.user_roles[uid("second-human")].remove(uid("agent"))
                elif change == "same-name-wrong-id":
                    self.cfg["representatives"][1]["required_role_ids"][1] = uid("unassigned-agent-role")
                elif change == "renamed":
                    self.api.roles[uid("agent")]["name"] = "Custom representative role"
                else:
                    self.api.team_roles[uid("human-team")] = []
                self.assert_gate("role")

    def test_empty_required_role_privileges_are_rejected(self):
        self.api.privileges[uid("agent")] = []
        self.assert_gate("no readable privilege definition")

    def test_role_privilege_changes_are_stale_even_when_role_ids_and_names_match(self):
        self.start()
        self.api.privileges[uid("agent")][0]["Depth"] = "Basic"
        with self.assertRaisesRegex(ValueError, "drifted"):
            self.apply()
        with self.assertRaisesRegex(ValueError, "drifted"):
            self.routing.review()
        self.assertFalse(self.api.writes)

    def test_missing_ambiguous_nonuser_or_wrong_bookable_resource_is_rejected(self):
        for change in ("missing", "duplicate-active", "wrong-type", "inactive", "wrong-id"):
            with self.subTest(change=change):
                self.setUp()
                resource = self.api.resources[-1]
                if change == "missing":
                    self.api.resources.pop()
                elif change == "duplicate-active":
                    self.api.resources.append({**resource, "bookableresourceid": uid("another-active-resource")})
                elif change == "wrong-type":
                    resource["resourcetype"] = 2
                elif change == "inactive":
                    resource["statecode"] = 1
                else:
                    self.cfg["representatives"][1]["bookable_resource_id"] = uid("different-resource")
                self.assert_gate("exactly one active User bookable resource")

    def test_inactive_historical_resource_is_preserved_not_reused(self):
        self.api.resources.append({
            **self.api.resources[-1], "bookableresourceid": uid("historical-resource"), "statecode": 1,
        })
        self.start()
        self.assertTrue(self.apply()["configuration_verified"])
        self.assertEqual(len(self.api.resources), 3)

    def test_native_relationship_metadata_is_checked_before_proposing_writes(self):
        for family in ("queue", "capacity"):
            with self.subTest(family=family):
                self.setUp()
                if family == "queue":
                    self.api.member_contract[0]["Entity1NavigationPropertyName"] = "guessed_relationship"
                else:
                    self.api.capacity_contract[0]["ReferencingEntityNavigationPropertyName"] = "WrongCase"
                with self.assertRaisesRegex(ValueError, "contract"):
                    self.start()
                self.assertFalse(self.api.writes)

    def test_workstreams_must_have_exact_native_inbound_queue_and_profile_bindings(self):
        for change in ("outbound", "other-queue", "other-profile", "duplicate-profile", "inactive-binding", "draft-binding"):
            with self.subTest(change=change):
                self.setUp()
                stream = self.api.records["msdyn_liveworkstreams"][uid("inbound-one")]
                if change == "outbound":
                    stream["msdyn_direction"] = 1
                elif change == "other-queue":
                    stream["_msdyn_defaultqueue_value"] = uid("other-queue")
                elif change == "other-profile":
                    self.api.bindings[0]["_msdyn_capacityprofile_id_value"] = uid("other-profile")
                elif change == "duplicate-profile":
                    self.api.bindings.append({**self.api.bindings[0], "msdyn_liveworkstreamcapacityprofileid": uid("duplicate-binding")})
                elif change == "inactive-binding":
                    self.api.bindings[0]["statecode"] = 1
                else:
                    self.api.bindings[0]["componentstate"] = 1
                self.assert_gate("workstream")

    def test_unsuitable_queue_or_capacity_profile_is_rejected(self):
        for table, field, value, fragment in (
            ("queues", "msdyn_queuetype", 192350000, "Voice queue"),
            ("queues", "statecode", 1, "Voice queue"),
            ("msdyn_capacityprofiles", "msdyn_defaultmaxunits", 0, "positive default"),
            ("msdyn_capacityprofiles", "msdyn_defaultmaxunits", True, "positive default"),
            ("msdyn_capacityprofiles", "statecode", 1, "active/published"),
        ):
            with self.subTest(table=table, field=field):
                self.setUp()
                next(iter(self.api.records[table].values()))[field] = value
                self.assert_gate(fragment)

    def test_existing_custom_limit_requires_exact_acknowledgement_and_is_never_patched(self):
        self.api.records[setup.CAPACITY][uid("preexisting-capacity")]["msdyn_maxunits"] = 2
        self.assert_gate("conflicting")
        self.cfg["representatives"][0]["capacity_limits"][0]["max_units"] = 2
        self.cfg["representatives"][1]["capacity_limits"][0]["max_units"] = 1
        self.start()
        self.assertTrue(self.apply()["configuration_verified"])
        self.assertEqual(self.api.records[setup.CAPACITY][uid("preexisting-capacity")]["msdyn_maxunits"], 2)
        self.assertEqual(self.api.writes[0]["body"]["msdyn_maxunits"], 1)

    def test_duplicate_inactive_missing_or_differently_inherited_capacity_is_rejected(self):
        for change in ("duplicate", "inactive", "missing-limit", "explicit-one"):
            with self.subTest(change=change):
                self.setUp()
                row = self.api.records[setup.CAPACITY][uid("preexisting-capacity")]
                if change == "duplicate":
                    self.api.add_capacity(uid("duplicate-capacity"), uid("first-human-resource"), uid("inbound-profile"), None)
                elif change == "inactive":
                    row["statecode"], row["statuscode"] = 1, 2
                elif change == "missing-limit":
                    del row["msdyn_maxunits"]
                else:
                    row["msdyn_maxunits"] = 1
                self.assert_gate("conflicting")

    def test_tenant_wide_idle_guard_covers_all_nonclosed_and_wrapup_states(self):
        for state, status in ((0, 1), (1, 2), (2, 3), (4, 5), (4, 4), (0, 99)):
            with self.subTest(state=state, status=status):
                self.setUp()
                self.api.conversations.append({"activityid": uid("unrelated-call"), "statecode": state, "statuscode": status})
                self.assert_gate("Drain all nonclosed")
        self.setUp()
        self.api.conversations.append({"activityid": uid("closed-call"), "statecode": 3, "statuscode": 4})
        self.start()
        self.assertEqual(self.plan["gates"], [])

    def test_dynamic_counters_are_guard_only_and_missing_rows_are_not_fabricated(self):
        for value in (1, None, -1):
            with self.subTest(consumed=value):
                self.setUp()
                self.api.dynamic[0]["msdyn_consumedcapacityprofileunits"] = value
                self.assert_gate("capacity")
        self.setUp()
        self.api.dynamic = []
        self.start()
        result = self.apply()
        self.assertTrue(result["configuration_verified"])
        self.assertFalse(result["live_distribution_verified"])
        self.assertEqual(self.api.dynamic, [])

    def test_exact_organization_actor_and_tenant_are_pinned(self):
        for change in ("organization", "actor", "tenant"):
            with self.subTest(change=change):
                self.setUp()
                if change == "organization":
                    self.api.who["OrganizationId"] = uid("different-org")
                elif change == "actor":
                    self.api.who["UserId"] = uid("different-admin")
                else:
                    self.api.tenant = uid("different-tenant")
                with self.assertRaisesRegex(ValueError, "organization|administrator"):
                    self.start()
                self.assertFalse(self.api.writes)

    def test_apply_requires_all_explicit_review_and_maintenance_confirmations(self):
        self.start()
        for changed in (
            {"confirm_organization": "https://another.crm.dynamics.com"},
            {"maintenance_window_confirmed": False}, {"reviewed_plan_sha256": "wrong"},
            {"reviewed_receipt_sha256": "wrong"},
        ):
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                self.apply(**changed)
        self.assertFalse(self.api.writes)

    def test_stale_initial_plan_review_and_future_dated_review_block_apply(self):
        for seconds in (-setup.REVIEW_SECONDS - 1, 30):
            with self.subTest(seconds=seconds):
                self.setUp()
                stamp = (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()
                with patch.object(setup, "utc", return_value=stamp):
                    self.start()
                with self.assertRaisesRegex(ValueError, "stale or future"):
                    self.apply()
                self.assertFalse(self.api.writes)
                self.routing.review()
                self.assertTrue(self.apply()["configuration_verified"])

    def test_only_latest_review_receipt_can_authorize_apply(self):
        self.start()
        reviewed = self.journal.previous
        self.routing.verify()
        with self.assertRaisesRegex(ValueError, "latest review"):
            self.apply(reviewed_receipt_sha256=reviewed)
        self.assertFalse(self.api.writes)

    def test_arbitrary_payload_and_unapproved_user_in_plan_are_rejected(self):
        for change in ("path", "body", "user"):
            with self.subTest(change=change):
                self.setUp()
                self.start()
                forged = copy.deepcopy(self.plan)
                if change == "path":
                    forged["operations"][0]["path"] = "systemusers"
                elif change == "body":
                    forged["operations"][0]["body"]["msdyn_maxunits"] = 99
                else:
                    forged["operations"][0]["user_id"] = uid("unapproved-user")
                with self.assertRaisesRegex(ValueError, "approved scope"):
                    setup.Routing(self.api, forged, MemoryJournal(forged))
                self.assertFalse(self.api.writes)

    def test_stale_identity_role_policy_membership_and_capacity_state_block_apply(self):
        changes = {
            "identity": lambda api: api.records["systemusers"][uid("second-human")].update({"isdisabled": True}),
            "role": lambda api: api.user_roles[uid("second-human")].remove(uid("agent")),
            "strategy": lambda api: api.records["queues"][uid("queue")].update({"msdyn_assignmentstrategy": 192350001}),
            "priority": lambda api: api.records["queues"][uid("queue")].update({"msdyn_priority": 2}),
            "hours": lambda api: api.records["queues"][uid("queue")].update({"_msdyn_operatinghourid_value": uid("hours")}),
            "overflow": lambda api: api.records["queues"][uid("queue")].update({"_msdyn_inqueueoverflowrulesetid_value": uid("overflow")}),
            "member-added": lambda api: api.members.add(uid("new-shared-member")),
            "member-removed": lambda api: api.members.remove(uid("untouched-existing-member")),
            "profile": lambda api: api.records["msdyn_capacityprofiles"][uid("inbound-profile")].update({"msdyn_defaultmaxunits": 2}),
            "association": lambda api: api.records[setup.CAPACITY][uid("preexisting-capacity")].update({"msdyn_maxunits": 1}),
            "other-capacity": lambda api: api.add_capacity(uid("new-other"), uid("second-human-resource"), uid("other-profile"), 3),
        }
        for label, change in changes.items():
            with self.subTest(label=label):
                self.setUp()
                self.start()
                change(self.api)
                with self.assertRaisesRegex(ValueError, "drifted"):
                    self.apply()
                with self.assertRaisesRegex(ValueError, "drifted"):
                    self.routing.review()
                self.assertFalse(self.api.writes)

    def test_new_call_or_capacity_consumption_after_review_blocks_before_write(self):
        for changed in ("call", "consumption"):
            with self.subTest(changed=changed):
                self.setUp()
                self.start()
                if changed == "call":
                    self.api.conversations.append({"activityid": uid("new-call"), "statecode": 1, "statuscode": 2})
                else:
                    self.api.dynamic[0]["msdyn_consumedcapacityprofileunits"] = 1
                with self.assertRaisesRegex(ValueError, "idle-call"):
                    self.apply()
                self.assertFalse(self.api.writes)

    def test_idle_guard_is_rechecked_between_capacity_and_queue_writes(self):
        self.start()

        def call_arrives(api, attempt):
            api.conversations.append({"activityid": uid("arriving-call"), "statecode": 1, "statuscode": 2})

        self.api.after_write = call_arrives
        with self.assertRaisesRegex(ValueError, "idle-call"):
            self.apply()
        self.assertEqual(len(self.api.changed), 1)
        self.assertEqual(len(self.journal.completed), 1)
        self.assertIsNone(self.journal.pending)
        self.assertNotIn(uid("second-human"), self.api.members)
        self.api.conversations = []
        self.api.after_write = None
        self.routing.review()
        self.assertEqual(self.apply()["mutations"], 1)

    def test_initially_gated_audit_requires_new_plan_even_after_calls_drain(self):
        self.api.conversations.append({"activityid": uid("initial-call"), "statecode": 1, "statuscode": 2})
        self.start()
        self.assertFalse(self.plan["operations"])
        self.api.conversations = []
        review = self.routing.review()
        self.assertIn("audit-only", " ".join(review["data"]["gates"]))
        with self.assertRaisesRegex(ValueError, "gates"):
            self.apply()
        self.start()
        self.assertTrue(self.apply()["configuration_verified"])

    def test_reserved_id_collision_is_not_adopted_even_outside_selected_resources(self):
        self.start()
        identifier = self.plan["operations"][0]["row_id"]
        self.api.add_capacity(identifier, uid("outside-resource"), uid("other-profile"), 1)
        with self.assertRaisesRegex(ValueError, "reserved capacity ID"):
            self.apply()
        self.assertFalse(self.api.writes)

    def test_verify_before_apply_is_incomplete_then_reapply_is_idempotent(self):
        self.start()
        verification = self.routing.verify()
        self.assertFalse(verification["configuration_verified"])
        self.assertEqual(len(verification["missing"]), 2)
        self.assertFalse(self.api.writes)
        self.routing.review()
        self.assertTrue(self.apply()["configuration_verified"])
        self.routing.review()
        self.assertEqual(self.apply()["mutations"], 0)
        self.assertEqual(len(self.api.writes), 2)
        self.assertTrue(self.routing.verify()["configuration_verified"])
        new_plan = setup.build_plan(self.api, self.cfg)
        self.assertEqual(new_plan["operations"], [])

    def test_preexisting_matching_outcomes_are_noops_and_never_owned(self):
        self.api.members.add(uid("second-human"))
        self.api.add_capacity(uid("preexisting-second"), uid("second-human-resource"), uid("inbound-profile"), None)
        self.start()
        self.assertEqual(self.plan["operations"], [])
        self.assertEqual(self.apply()["mutations"], 0)
        self.assertEqual(self.journal.completed, {})
        self.assertEqual(self.routing.rollback_plan()["manual_conditional_capacity_candidates"], [])
        self.assertFalse(self.api.writes)

    def test_definitive_rejection_needs_read_only_recovery_and_fresh_review_before_retry(self):
        self.start()
        self.api.fail_before[1] = http_error(403)
        with self.assertRaisesRegex(ValueError, "recover"):
            self.apply()
        self.assertTrue(self.journal.pending["rejected"])
        with self.assertRaisesRegex(ValueError, "outstanding mutation"):
            self.apply()
        self.assertEqual(self.routing.recover()["status"], "rejection_confirmed_re_review_before_retry")
        self.assertIsNone(self.journal.pending)
        self.routing.review()
        self.assertTrue(self.apply()["configuration_verified"])
        self.assertEqual(len(self.api.changed), 2)
        self.assertEqual(self.api.writes[0]["body"], self.api.writes[1]["body"])

    def test_transport_failure_with_absent_result_remains_in_doubt_without_replay(self):
        self.start()
        self.api.fail_before[1] = ConnectionError("unknown delivery")
        with self.assertRaisesRegex(ValueError, "recover"):
            self.apply()
        self.assertEqual(self.routing.recover()["status"], "in_doubt_no_replay_allowed")
        self.assertIsNotNone(self.journal.pending)
        with self.assertRaisesRegex(ValueError, "outstanding mutation"):
            self.apply()
        with self.assertRaisesRegex(ValueError, "recovery"):
            self.routing.review()
        self.assertEqual(len(self.api.writes), 1)
        self.assertFalse(self.api.changed)
        self.assertFalse(self.routing.rollback_plan()["manual_conditional_capacity_candidates"])

    def test_lost_capacity_response_recovers_owned_row_then_resumes_only_membership(self):
        self.start()
        self.api.fail_after[1] = ConnectionError("response lost")
        with self.assertRaisesRegex(ValueError, "recover"):
            self.apply()
        self.assertFalse(self.journal.pending["acknowledged"])
        self.assertEqual(self.routing.recover()["status"], "persisted_outcome_recovered")
        self.assertIsNone(self.journal.pending)
        self.assertEqual(len(self.journal.completed), 1)
        self.routing.review()
        self.assertTrue(self.apply()["configuration_verified"])
        self.assertEqual(len(self.api.writes), 2)

    def test_acknowledged_create_with_failed_readback_recovers_without_second_create(self):
        self.start()
        self.api.fail_read_once = True
        with self.assertRaisesRegex(ValueError, "recover"):
            self.apply()
        self.assertTrue(self.journal.pending["acknowledged"])
        self.routing.recover()
        self.routing.review()
        self.assertTrue(self.apply()["configuration_verified"])
        self.assertEqual(len([row for row in self.api.writes if row["path"] == setup.CAPACITY]), 1)

    def test_receipt_write_failure_never_permits_unjournaled_mutation_or_unsafe_retry(self):
        for failed_kind in ("before", "acknowledged"):
            with self.subTest(failed_kind=failed_kind):
                self.setUp()
                self.start()
                persist = self.journal.persist
                failed = []

                def fail_once(event):
                    if event["kind"] == failed_kind and not failed:
                        failed.append(True)
                        raise OSError("synthetic receipt storage failure")
                    persist(event)

                with patch.object(self.journal, "persist", side_effect=fail_once):
                    with self.assertRaises((OSError, ValueError)):
                        self.apply()
                if failed_kind == "before":
                    self.assertFalse(self.api.writes)
                    self.assertIsNone(self.journal.pending)
                else:
                    self.assertEqual(len(self.api.changed), 1)
                    self.assertIsNotNone(self.journal.pending)
                    self.assertEqual(self.routing.recover()["status"], "persisted_outcome_recovered")
                self.routing.review()
                self.assertTrue(self.apply()["configuration_verified"])
                self.assertEqual(len(self.api.changed), 2)

    def test_partial_apply_then_membership_rejection_keeps_capacity_receipt_and_resumes(self):
        self.start()
        self.api.fail_before[2] = http_error(403)
        with self.assertRaisesRegex(ValueError, "recover"):
            self.apply()
        self.assertEqual(len(self.journal.completed), 1)
        self.assertNotIn(uid("second-human"), self.api.members)
        self.routing.recover()
        self.routing.review()
        self.assertEqual(self.apply()["mutations"], 1)
        self.assertEqual(len([row for row in self.api.changed if row["path"] == setup.CAPACITY]), 1)

    def test_lost_membership_response_cannot_establish_exclusive_link_ownership(self):
        self.start()
        self.api.fail_after[2] = ConnectionError("response lost")
        with self.assertRaisesRegex(ValueError, "recover"):
            self.apply()
        self.routing.recover()
        result = self.routing.rollback_plan()
        self.assertEqual(len(result["manual_conditional_capacity_candidates"]), 1)
        self.assertEqual(len(result["retain"]), 1)
        self.assertIn("no exclusive", result["retain"][0]["reason"])
        self.assertIn(uid("second-human"), self.api.members)
        self.assertEqual(len(self.api.writes), 2)
        self.assertTrue(all(row["path"].startswith(setup.CAPACITY) for row in result["manual_conditional_capacity_candidates"]))

    def test_recovery_rejects_wrong_creator_time_or_id_and_duplicate_association(self):
        for change in ("creator", "time", "different-id", "duplicate"):
            with self.subTest(change=change):
                self.setUp()
                self.start()
                self.api.fail_after[1] = ConnectionError("response lost")
                with self.assertRaises(ValueError):
                    self.apply()
                identifier = self.plan["operations"][0]["row_id"]
                row = self.api.records[setup.CAPACITY][identifier]
                if change == "creator":
                    row["_createdby_value"] = uid("other-admin")
                elif change == "time":
                    row["createdon"] = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
                elif change == "different-id":
                    row[setup.CAPACITY_ID] = uid("someone-elses-association")
                else:
                    self.api.add_capacity(uid("duplicate-pair"), uid("second-human-resource"), uid("inbound-profile"), None)
                with self.assertRaisesRegex(ValueError, "ownership|conflicting"):
                    self.routing.recover()
                self.assertIsNotNone(self.journal.pending)
                self.assertEqual(len(self.api.writes), 1)

    def test_recovery_does_not_clear_a_reserved_id_with_changed_lookups(self):
        self.start()
        self.api.fail_after[1] = ConnectionError("response lost")
        with self.assertRaises(ValueError):
            self.apply()
        row = self.api.records[setup.CAPACITY][self.plan["operations"][0]["row_id"]]
        row["_msdyn_bookableresourceid_value"] = uid("outside-resource")
        with self.assertRaisesRegex(ValueError, "outside the expected"):
            self.routing.recover()
        self.assertIsNotNone(self.journal.pending)

    def test_concurrent_queue_edit_after_write_stops_partial_recovery_without_rebasing(self):
        self.start()
        self.api.after_write = lambda api, attempt: api.members.add(uid("concurrent-unapproved-member"))
        with self.assertRaisesRegex(ValueError, "recover"):
            self.apply()
        with self.assertRaisesRegex(ValueError, "drifted"):
            self.routing.recover()
        self.assertIn(uid("concurrent-unapproved-member"), self.api.members)
        self.assertEqual(len(self.api.writes), 1)

    def test_rollback_is_plan_only_and_exact_owned_capacity_etag_is_required(self):
        self.start()
        self.apply()
        result = self.routing.rollback_plan()
        candidate = result["manual_conditional_capacity_candidates"][0]
        self.assertEqual(candidate["headers"], {"If-Match": 'W/"8"'})
        self.assertIsNone(candidate["before"])
        self.assertTrue(candidate["manual_only"])
        self.assertEqual(result["mutations"], 0)
        identifier = self.plan["operations"][0]["row_id"]
        self.api.records[setup.CAPACITY][identifier]["@odata.etag"] = 'W/"99"'
        self.assertEqual(self.routing.rollback_plan()["manual_conditional_capacity_candidates"], [])
        self.assertEqual(len(self.api.writes), 2)
        self.assertIn(uid("preexisting-capacity"), self.api.records[setup.CAPACITY])
        self.assertIn(uid("untouched-existing-member"), self.api.members)

    def test_rollback_has_no_delete_candidate_when_busy_or_state_has_drifted(self):
        for change in ("busy", "capacity", "role"):
            with self.subTest(change=change):
                self.setUp()
                self.start()
                self.apply()
                if change == "busy":
                    self.api.conversations.append({"activityid": uid("active-call"), "statecode": 1, "statuscode": 2})
                elif change == "capacity":
                    self.api.records[setup.CAPACITY][self.plan["operations"][0]["row_id"]]["msdyn_maxunits"] = 2
                else:
                    self.api.user_roles[uid("second-human")].remove(uid("agent"))
                result = self.routing.rollback_plan()
                self.assertTrue(result["gates"])
                self.assertFalse(result["manual_conditional_capacity_candidates"])
                self.assertEqual(len(self.api.writes), 2)


class PersistentReceiptTests(unittest.TestCase):
    def setUp(self):
        # Test data is synthetic and stays inside the checkout, never in system scratch directories.
        self.directory = ROOT / (".routing-test-artifacts-" + uuid4().hex)
        self.directory.mkdir(mode=0o700)
        self.api, self.cfg = FakeAPI(), config()
        self.plan = setup.build_plan(self.api, self.cfg)

    def tearDown(self):
        shutil.rmtree(self.directory)

    def journal(self):
        journal = setup.Journal(self.directory, self.plan)
        journal.save("review", {"snapshot": self.plan["before"], "gates": self.plan["gates"]})
        return journal

    def test_private_receipts_survive_reload_with_exact_outcomes(self):
        journal = self.journal()
        routing = setup.Routing(self.api, self.plan, journal)
        routing.apply(confirm_organization=self.api.org, reviewed_plan_sha256=setup.digest(self.plan),
                      reviewed_receipt_sha256=journal.previous, maintenance_window_confirmed=True)
        restored = setup.Journal(self.directory, self.plan)
        self.assertEqual(restored.completed, journal.completed)
        self.assertIsNone(restored.pending)
        self.assertTrue(setup.Routing(self.api, self.plan, restored).verify()["configuration_verified"])
        for path in self.directory.glob("receipt-*.json"):
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_missing_or_modified_middle_receipt_and_different_plan_are_rejected(self):
        journal = self.journal()
        journal.save("review", {"snapshot": self.plan["before"], "gates": []})
        path = self.directory / "receipt-000001.json"
        original = path.read_text()
        altered = json.loads(original)
        altered["data"]["gates"].append("modified")
        path.write_text(json.dumps(altered))
        with self.assertRaisesRegex(ValueError, "Receipt chain"):
            setup.Journal(self.directory, self.plan)
        path.write_text(original)
        different = copy.deepcopy(self.plan)
        different["created_at"] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        with self.assertRaisesRegex(ValueError, "different plan"):
            setup.Journal(self.directory, different)
        path.unlink()
        with self.assertRaisesRegex(ValueError, "Receipt chain"):
            setup.Journal(self.directory, self.plan)

    def test_exclusive_intent_receipt_prevents_concurrent_writers(self):
        first = self.journal()
        second = setup.Journal(self.directory, self.plan)
        event = {"operation": self.plan["operations"][0], "configuration_sha256": setup.digest(setup.configuration(self.plan["before"]))}
        first.save("before", event)
        with self.assertRaises(FileExistsError):
            second.save("before", event)
        self.assertFalse(self.api.writes)
        self.assertEqual(setup.Journal(self.directory, self.plan).pending["operation"], self.plan["operations"][0])

    def test_cli_default_only_discovers_and_writes_private_artifacts(self):
        config_path = self.directory / "config.json"
        setup.write_private(config_path, json.dumps(self.cfg))
        output = self.directory / "plan"
        stdout = io.StringIO()
        with patch.object(setup, "private_path", side_effect=lambda path: Path(path)), \
                patch.object(setup, "Dataverse", return_value=self.api), patch("sys.stdout", stdout):
            code = setup.main(["--config", str(config_path), "--directory", str(output)])
        self.assertEqual(code, 0)
        self.assertFalse(self.api.writes)
        self.assertEqual(output.stat().st_mode & 0o777, 0o700)
        self.assertEqual((output / "plan.json").stat().st_mode & 0o777, 0o600)
        summary = json.loads(stdout.getvalue())
        self.assertEqual(summary["mode"], "plan")
        self.assertEqual(summary["mutations"], 0)
        self.assertNotIn(self.cfg["tenant_id"], stdout.getvalue())
        self.assertEqual(summary["plan_sha256"], setup.digest(json.loads((output / "plan.json").read_text())))

    def test_real_private_path_helper_blocks_checkout_outputs_before_dataverse(self):
        with patch.object(setup, "Dataverse") as client, patch("sys.stderr", io.StringIO()):
            code = setup.main(["--config", "unused-config", "--directory", str(self.directory / "forbidden")])
        self.assertEqual(code, 1)
        client.assert_not_called()

    def test_cli_verification_of_unapplied_plan_returns_nonzero_without_writes(self):
        setup.write_private(self.directory / "plan.json", json.dumps(self.plan))
        self.journal()
        stdout = io.StringIO()
        with patch.object(setup, "private_path", side_effect=lambda path: Path(path)), \
                patch.object(setup, "Dataverse", return_value=self.api), patch("sys.stdout", stdout):
            code = setup.main(["--directory", str(self.directory), "--verify"])
        self.assertEqual(code, 1)
        self.assertFalse(json.loads(stdout.getvalue())["configuration_verified"])
        self.assertFalse(self.api.writes)

    def test_owner_only_inputs_are_required(self):
        if os.name == "nt":
            self.skipTest("POSIX permission test")
        self.journal()
        path = self.directory / "receipt-000001.json"
        path.chmod(0o644)
        with self.assertRaisesRegex(ValueError, "owner"):
            setup.Journal(self.directory, self.plan)


class PortableGuideTests(unittest.TestCase):
    def test_guide_example_and_helper_have_no_deployment_literals(self):
        identifiers = re.compile(r"\b[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}\b|\+[1-9][0-9]{7,14}\b")
        paths = [
            ROOT / "infra/scripts/setup_dynamics_routing.py",
            ROOT / "docs/dynamics-native-voice/routing-setup.md",
            ROOT / "docs/dynamics-native-voice/examples/routing-config.example.json",
        ]
        for path in paths:
            with self.subTest(path=path.name):
                text = path.read_text()
                self.assertNotRegex(text, identifiers)
                self.assertNotIn("/home/codespace/", text)
                self.assertNotIn("/workspaces/moacc", text)
        example = json.loads(paths[-1].read_text())
        self.assertFalse(example["approval"]["shared_capacity_and_queue_policy_reviewed"])
        for rep in example["representatives"]:
            self.assertFalse(rep["approved"])
            self.assertFalse(rep["voice_license_admin_confirmed"])
            self.assertFalse(rep["native_access_admin_confirmed"])


if __name__ == "__main__":
    unittest.main()
