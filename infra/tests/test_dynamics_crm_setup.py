import copy
import hashlib
import io
import json
import re
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError
from urllib.parse import parse_qs


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "infra/scripts"))
import setup_dynamics_crm as crm


def identifier(number):
    return f"00000000-0000-4000-8000-{number:012d}"


def config():
    return crm.validate_config({
        "version": 1, "organization_url": "https://synthetic.crm.dynamics.com",
        "tenant_id": identifier(1), "business_unit_id": identifier(2), "deployment_key": "unittest-service",
        "managed_identity": {
            "resource_id": f"/subscriptions/{identifier(3)}/resourceGroups/synthetic/providers/Microsoft.ManagedIdentity/userAssignedIdentities/test-crm",
            "client_id": identifier(4), "principal_id": identifier(5),
        },
        "native_reader_client_id": identifier(6), "workstream_ids": [identifier(7)],
    })


def seed():
    return crm.validate_seed({
        "version": 1, "synthetic_only": True,
        "account": {"key": "unit-account", "name": "Technical fixture", "description": "Owned fixture record."},
        "contacts": [
            {"key": f"unit-contact-{index}", "synthetic": True, "firstname": "Technical", "lastname": f"Fixture {index}",
             "phone": "+1202555" + f"{100 + index:04d}", "email": None,
             "preferred_language": language, "description": "Contact fixture."}
            for index, language in enumerate(("en", "ar"))
        ],
        "cases": [
            {"key": f"unit-case-{index}", "contact_key": f"unit-contact-{index}", "category": "general",
             "title": f"Technical case {index}", "description": "Case fixture.",
             "location": {"district": "Fixture area", "street": "Fixture street", "landmark": ""},
             "priority": 2, "state": state, "notes": [{"key": "unit-note", "text": "Customer update."}],
             **({"resolution": "Resolved fixture."} if state == "resolved" else {})}
            for index, state in enumerate(("active", "resolved"))
        ],
    })


def http_error(status, detail="synthetic failure"):
    return HTTPError("https://synthetic.crm.dynamics.com", status, "failure", {}, io.BytesIO(detail.encode()))


class MemoryJournal(crm.Journal):
    def __init__(self, directory=None, configuration=None, synthetic_seed=None, mode="apply"):
        self.directory = Path("not-created-unit-test-output")
        self.files = {}
        self.document = {
            "version": crm.VERSION, "mode": mode, "configuration": configuration or config(),
            "configuration_hash": crm.fingerprint(configuration or config()), "seed_hash": crm.fingerprint(synthetic_seed or seed()),
            "status": "in_progress", "events": [], "owned_records": [], "checks": {},
        }

    def write(self, name, value):
        if name in self.files:
            raise FileExistsError(name)
        self.files[name] = copy.deepcopy(value)


class FakeDataverse:
    """In-memory Web API contract, deliberately not a substitute for live replay."""

    def __init__(self, configuration=None):
        self.config = configuration or config()
        self.user_id = crm.stable_id(self.config, "application-user", self.config["managed_identity"]["client_id"])
        self.role_id = crm.stable_id(self.config, "role", self.config["business_unit_id"])
        self.admin_id = identifier(8)
        self.records = {table: {} for table in crm.PRIMARY}
        self.records["systemusers"][self.admin_id] = {
            "systemuserid": self.admin_id, "applicationid": None, "azureactivedirectoryobjectid": identifier(9),
            "isdisabled": False,
        }
        self.attributes, self.keys, self.privileges, self.members, self.writes = {}, {}, {}, {}, []
        self.calls = []
        self.counter = 100
        self.all_privileges = sorted(set(crm.PRIVILEGES) | crm.SHAREPOINT_DEFAULTS | crm.PRESERVED_PLATFORM_READS | {"prvReadRole"})
        self.definitions = {name: identifier(200 + i) for i, name in enumerate(self.all_privileges)}
        self.defaults = crm.SHAREPOINT_DEFAULTS | crm.PRESERVED_PLATFORM_READS | (
            {"prvReadPluginAssembly"} if self.config["native_voice"] else set()
        )
        self.role_reads_allowed = False
        self.impersonation_ignored = False
        self.runtime_who_reports_authenticator = False
        self.role_denial_detail = None
        self.typed_metadata_not_found = 0
        self.inherited_role = None
        self.close_timeout = False
        self.missing_resolution = False
        self.fail_writes = None

    def etag(self):
        self.counter += 1
        return f'W/"{self.counter}"'

    def normalize(self, body):
        result = {}
        for key, value in body.items():
            if key.endswith("@odata.bind"):
                result[crm.BINDINGS[key[:-11]]] = value.rsplit("(", 1)[1][:-1]
            elif key != "@odata.type":
                result[key] = value
        return result

    def get(self, path, query=None):
        self.calls.append(("GET", path, query))
        if "?" in path:
            path, raw = path.split("?", 1)
            query = {key: values[0] for key, values in parse_qs(raw).items()}
        query = query or {}
        if path == "WhoAmI":
            result = {"UserId": self.admin_id, "BusinessUnitId": self.config["business_unit_id"]}
        elif path.startswith("businessunits("):
            result = {"businessunitid": self.config["business_unit_id"], "isdisabled": False}
        elif path.startswith("msdyn_liveworkstreams("):
            result = {"msdyn_liveworkstreamid": self.config["workstream_ids"][0],
                      "statecode": 0, "msdyn_direction": 0, "msdyn_streamsource": 192440000}
        elif path.startswith("EntityDefinitions"):
            entity = path.split("'")[1]
            if "/Attributes(LogicalName=" in path:
                name = path.split("'")[3]
                result = self.attributes.get((entity, name))
                if result is None:
                    raise http_error(404)
                if "/Microsoft.Dynamics.CRM." in path and self.typed_metadata_not_found:
                    self.typed_metadata_not_found -= 1
                    raise http_error(404)
            elif path.endswith("/Keys"):
                name = query["$filter"].split("'")[1]
                result = {"value": [self.keys[(entity, name)]] if (entity, name) in self.keys else []}
            else:
                raise AssertionError(path)
        elif path.startswith("RetrieveRolePrivilegesRole("):
            result = {"RolePrivileges": [
                {"PrivilegeName": name, "PrivilegeId": self.definitions[name], "Depth": depth}
                for name, depth in self.privileges.items()
            ]}
        elif path == "privileges":
            expression = query.get("$filter", "")
            result = {"value": [
                {"name": name, "privilegeid": uid} for name, uid in self.definitions.items()
                if f"name eq '{name}'" in expression or f"privilegeid eq {uid}" in expression
            ]}
        elif path.startswith("roles(") and path.endswith("/systemuserroles_association"):
            result = {"value": [{"systemuserid": user} for user, roles in self.members.items() if self.role_id in roles]}
        elif path.startswith("roles(") and path.endswith("/teamroles_association"):
            result = {"value": []}
        elif path.endswith("/systemuserroles_association"):
            result = {"value": [{"roleid": uid, "name": self.records["roles"].get(uid, {}).get("name", "unexpected")}
                                for uid in self.members.get(self.user_id, [])]}
        elif path.endswith("/teammembership_association"):
            result = {"value": [{"teamid": identifier(66)}] if self.inherited_role else []}
        elif path.endswith("/teamroles_association"):
            result = {"value": [{"roleid": self.inherited_role, "name": "Existing team role"}] if self.inherited_role else []}
        elif path == "pluginassemblies":
            result = {"value": [{"pluginassemblyid": identifier(11), "name": crm.PLUGIN}]}
        elif "(" in path:
            table, uid = path.split("(", 1)
            uid = uid[:-1]
            result = self.records.get(table, {}).get(uid)
            if result is None:
                raise http_error(404)
        else:
            rows = list(self.records[path].values())
            expression = query.get("$filter")
            if expression:
                for condition in expression.split(" and "):
                    key, value = condition.split(" eq ", 1)
                    value = value.strip("'")
                    expected = True if value == "true" else False if value == "false" else int(value) if value.isdigit() else value
                    rows = [row for row in rows if row.get(key) == expected]
            result = {"value": rows[:int(query.get("$top", "100"))]}
        return copy.deepcopy(result)

    def rows(self, table, query):
        return self.get(table, query)["value"]

    def request(self, path, *, method="GET", body=None, headers=None, raw=False):
        headers = headers or {}
        if method == "GET":
            runtime = bool(headers.get("CallerObjectId")) and not self.impersonation_ignored
            if runtime and path == "WhoAmI":
                return {"UserId": self.admin_id if self.runtime_who_reports_authenticator else self.user_id,
                        "BusinessUnitId": self.config["business_unit_id"]}
            if runtime and path.startswith("roles?") and not self.role_reads_allowed:
                raise http_error(403, self.role_denial_detail or '{"error":{"message":"Missing prvReadRole"}}')
            if runtime and path.startswith("pluginassemblies") and self.privileges.get("prvReadPluginAssembly") != "Global":
                raise http_error(403, '{"error":{"message":"Missing prvReadPluginAssembly"}}')
            return self.get(path)
        self.writes.append({"path": path, "method": method, "body": copy.deepcopy(body), "headers": dict(headers)})
        if self.fail_writes:
            raise http_error(self.fail_writes)
        if path.startswith("EntityDefinitions"):
            entity = path.split("'")[1]
            self.etag()
            row = {**body, "LogicalName": body["SchemaName"], "MetadataId": identifier(self.counter + 300)}
            if path.endswith("/Attributes"):
                row["AttributeType"] = "Boolean" if "BooleanAttributeMetadata" in body["@odata.type"] else "String"
                self.attributes[(entity, body["SchemaName"])] = row
            elif path.endswith("/Keys"):
                row["EntityKeyIndexStatus"] = "Active"
                self.keys[(entity, body["SchemaName"])] = row
            else:
                raise AssertionError(path)
        elif path == "PublishXml":
            return {}
        elif path == "roles":
            self.records["roles"][body["roleid"]] = {**self.normalize(body), "ismanaged": False, "@odata.etag": self.etag()}
            self.privileges = {name: "Global" for name in self.defaults}
        elif path.endswith("AddPrivilegesRole"):
            by_id = {uid: name for name, uid in self.definitions.items()}
            self.privileges.update({by_id[row["PrivilegeId"]]: row["Depth"] for row in body["Privileges"]})
        elif path.endswith("RemovePrivilegeRole"):
            name = next(name for name, uid in self.definitions.items() if uid == body["Privilege"]["privilegeid"])
            self.privileges.pop(name)
        elif path == "systemusers":
            self.records["systemusers"][body["systemuserid"]] = {
                **self.normalize(body), "isdisabled": False, "@odata.etag": self.etag(),
            }
        elif path.endswith("/systemuserroles_association/$ref"):
            self.members[self.user_id] = [self.role_id]
        elif path == "CloseIncident":
            resolution = self.normalize(body["IncidentResolution"])
            self.assert_close_payload(body)
            case = self.records["incidents"][resolution["_incidentid_value"]]
            case.update(statecode=1, statuscode=body["Status"], **{"@odata.etag": self.etag()})
            if not self.missing_resolution:
                uid = identifier(self.counter + 600)
                self.records["incidentresolutions"][uid] = {
                    **resolution, "activityid": uid, "statecode": 1, "statuscode": 2, "@odata.etag": self.etag(),
                }
            if self.close_timeout:
                raise TimeoutError("synthetic transport timeout")
        elif method == "PATCH":
            table, uid = path.split("(", 1)
            uid = uid[:-1]
            if uid in self.records[table] and headers.get("If-None-Match") == "*":
                raise http_error(412)
            row = self.normalize(body)
            row.update({
                "@odata.etag": self.etag(), "_createdby_value": self.user_id if headers.get("CallerObjectId") else self.admin_id,
                "statecode": row.get("statecode", 0), "statuscode": row.get("statuscode", 1),
            })
            self.records[table][uid] = row
        else:
            raise AssertionError((method, path))
        return {}

    @staticmethod
    def assert_close_payload(body):
        if "activityid" in body["IncidentResolution"] or body["Status"] != 5:
            raise AssertionError("CloseIncident generates the actual activity ID; solved status is 5.")


class ContractTests(unittest.TestCase):
    def test_examples_are_templates_and_source_seed_is_nonpersonal(self):
        document = json.loads((ROOT / "docs/dynamics-native-voice/examples/crm-config.example.json").read_text())
        with self.assertRaises(ValueError):
            crm.validate_config(document)
        sample = json.loads((ROOT / "docs/dynamics-native-voice/examples/crm-seed.example.json").read_text())
        self.assertEqual(crm.validate_seed(sample), crm.EMPTY_SEED)
        self.assertEqual(len(seed()["contacts"]), 2)

    def test_actor_and_operation_contract(self):
        phone = seed()["contacts"][0]["phone"]
        self.assertEqual(crm.actor_id(phone), "profile:" + hashlib.sha256(phone.encode()).hexdigest())
        key = "a" * 64
        value = hashlib.sha256(key.encode()).hexdigest()
        self.assertEqual(crm.operation_guid(key), f"{value[:8]}-{value[8:12]}-5{value[13:16]}-a{value[17:20]}-{value[20:32]}")
        for invalid in ("", " " + phone, phone + " ", "12345", "+٠١٢٣٤٥٦٧", "session:test"):
            with self.subTest(phone=invalid), self.assertRaises(ValueError):
                crm.actor_id(invalid)

    def test_nine_fields_false_gate_and_two_keys(self):
        self.assertEqual(sum(map(len, crm.FIELDS.values())), 9)
        self.assertEqual(len(crm.KEYS), 2)
        boolean = crm.attribute_body("moacc_demoenabled", None)
        self.assertIs(boolean["DefaultValue"], False)
        self.assertFalse(boolean["IsSecured"])
        self.assertEqual(boolean["OptionSet"]["FalseOption"]["Value"], 0)
        language = crm.attribute_body("moacc_preferredlanguage", 2)
        self.assertTrue(language["@odata.type"].endswith("StringAttributeMetadata"))
        self.assertEqual(language["MaxLength"], 2)
        self.assertEqual(crm.KEYS["contact"], ("moacc_contact_actorid", "moacc_actorid"))

    def test_custom_field_names_track_the_shared_case_service(self):
        source = (ROOT / "apps/assist/apps/console/src/lib/case-service.ts").read_text()
        names = {name for fields in crm.FIELDS.values() for name in fields}
        self.assertEqual(names, set(re.findall(r"\bmoacc_[a-z]+\b", source)))
        self.assertIn("moacc_demoenabled eq true and statecode eq 0", source)
        self.assertIn("moacc_preferredlanguage: z.enum(['ar', 'en']).nullable()", source)

    def test_minimum_privileges_never_grant_admin_delete_or_resolution(self):
        self.assertEqual(len(crm.PRIVILEGES), 21)
        self.assertEqual(crm.PRIVILEGES["prvReadPluginAssembly"], "Global")
        self.assertEqual(crm.PRIVILEGES["prvReadAsyncOperation"], "Basic")
        self.assertEqual(crm.PRIVILEGES["prvWriteActivity"], "Local")
        self.assertFalse(any("Delete" in name or "Role" in name or "Resolve" in name or "Publish" in name for name in crm.PRIVILEGES))
        self.assertNotIn("prvWriteContact", crm.PRIVILEGES)
        self.assertEqual(crm.SHAREPOINT_DEFAULTS, {
            "prvReadSharePointDocument", "prvReadSharePointData", "prvWriteSharePointData", "prvCreateSharePointData",
        })

    def test_payload_bindings_hashes_and_language(self):
        rows, closures = crm.seed_records(config(), seed())
        contacts = [row["body"] for row in rows if row["table"] == "contacts"]
        self.assertEqual({row["moacc_preferredlanguage"] for row in contacts}, {"ar", "en"})
        self.assertTrue(all(row["moacc_demoenabled"] and row["statecode"] == 0 for row in contacts))
        for row in rows:
            body = row["body"]
            if row["table"] == "incidents":
                payload = {
                    "category": body["moacc_category"], "description": body["description"],
                    "location": {key: body["moacc_" + key] for key in ("district", "street", "landmark")},
                }
                self.assertEqual(body["moacc_payloadhash"], hashlib.sha256(
                    json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(),
                ).hexdigest())
                self.assertEqual(body["incidentid"], crm.operation_guid(body["moacc_requestkey"]))
                self.assertIn("customerid_contact@odata.bind", body)
            if row["table"] == "annotations":
                self.assertEqual(body["subject"], "Customer update")
                self.assertIn("objectid_incident@odata.bind", body)
        self.assertNotIn("activityid", closures[0]["body"])
        self.assertEqual(crm.seed_records(config(), seed()), (rows, closures))

    def test_invalid_identity_config_is_rejected(self):
        changes = [
            ("organization_url", "http://synthetic.crm.dynamics.com"),
            ("organization_url", "https://synthetic.crm.dynamics.com/"),
            ("tenant_id", "not-an-id"), ("version", True), ("deployment_key", "quoted'key"),
            ("workstream_ids", []), ("workstream_ids", [identifier(7), identifier(7)]),
            ("native_reader_client_id", identifier(4)),
            ("native_reader_client_id", identifier(5)),
        ]
        for key, value in changes:
            document = config()
            document[key] = value
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                crm.validate_config(document)
        document = config()
        document["secret"] = "not-allowed"
        with self.assertRaises(ValueError):
            crm.validate_config(document)

    def test_invalid_seed_and_missing_eligibility_fail_closed(self):
        for field, value in (("synthetic", False), ("preferred_language", "fr"), ("email", "invalid"), ("phone", "invalid")):
            document = seed()
            document["contacts"][0][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                crm.validate_seed(document, location_required=True)
        document = seed()
        document["synthetic_only"] = False
        with self.assertRaises(ValueError):
            crm.validate_seed(document)
        document = seed()
        document["contacts"][1]["phone"] = document["contacts"][0]["phone"]
        with self.assertRaises(ValueError):
            crm.validate_seed(document)
        document = seed()
        document["cases"][0]["location"] = {"district": "Synthetic", "street": "", "landmark": ""}
        with self.assertRaises(ValueError):
            crm.validate_seed(document, location_required=True)
        crm.validate_seed(document, location_required=False)
        document = seed()
        document["cases"][0]["category"] = "unsupported"
        with self.assertRaises(ValueError):
            crm.validate_seed(document)
        document = seed()
        document["cases"][0]["contact_key"] = {}
        with self.assertRaisesRegex(ValueError, "reference a seed contact"):
            crm.validate_seed(document)

    def test_empty_seed_and_crm_only_privileges_are_supported(self):
        cfg = config()
        cfg.update(native_voice=False, native_reader_client_id="", workstream_ids=[])
        cfg = crm.validate_config(cfg)
        self.assertFalse(set(crm.required_privileges(cfg)) & crm.NATIVE_PRIVILEGES)
        self.assertEqual(crm.seed_records(cfg, crm.validate_seed(crm.EMPTY_SEED)), ([], []))
        invalid = copy.deepcopy(crm.EMPTY_SEED)
        invalid["contacts"] = seed()["contacts"]
        with self.assertRaises(ValueError):
            crm.validate_seed(invalid)

    def test_generated_values_remain_natural_without_visible_provenance_markers(self):
        content = seed()
        rows, closures = crm.seed_records(config(), content)
        self.assertEqual(rows[0]["body"]["description"], content["account"]["description"])
        self.assertEqual(next(row["body"]["description"] for row in rows if row["table"] == "contacts"),
                         content["contacts"][0]["description"])
        self.assertEqual(closures[0]["body"]["description"], content["cases"][1]["resolution"])
        self.assertNotIn("synthetic", closures[0]["body"]["subject"].lower())

    def test_strict_metadata_compatibility(self):
        base = {
            "LogicalName": "moacc_demoenabled", "AttributeType": "Boolean", "IsSecured": False,
            "DefaultValue": False, "RequiredLevel": {"Value": "None"},
        }
        crm.validate_attribute(base, "moacc_demoenabled", None)
        for key, value in (("DefaultValue", True), ("IsSecured", True), ("AttributeType", "String"),
                           ("RequiredLevel", {"Value": "ApplicationRequired"})):
            with self.subTest(key=key), self.assertRaises(ValueError):
                crm.validate_attribute({**base, key: value}, "moacc_demoenabled", None)
        language = {
            "LogicalName": "moacc_preferredlanguage", "AttributeType": "String", "IsSecured": False,
            "MaxLength": 10, "RequiredLevel": {"Value": "None"}, "FormatName": {"Value": "Text"},
        }
        crm.validate_attribute(language, "moacc_preferredlanguage", 2)
        for key, value in (("MaxLength", 1), ("FormatName", {"Value": "Email"})):
            with self.subTest(key=key), self.assertRaises(ValueError):
                crm.validate_attribute({**language, key: value}, "moacc_preferredlanguage", 2)

    def test_existing_identity_is_read_only_and_tenant_checked(self):
        cfg = config()
        actual = {
            "id": cfg["managed_identity"]["resource_id"], "clientId": cfg["managed_identity"]["client_id"],
            "principalId": cfg["managed_identity"]["principal_id"], "tenantId": cfg["tenant_id"],
        }
        with patch.object(crm.subprocess, "run", return_value=SimpleNamespace(stdout=json.dumps(actual))) as run:
            self.assertEqual(crm.validate_identity(cfg), actual)
            self.assertEqual(run.call_args.args[0][:3], ["az", "identity", "show"])
            self.assertEqual(run.call_args.kwargs["timeout"], 45)
            actual["tenantId"] = identifier(99)
            run.return_value.stdout = json.dumps(actual)
            with self.assertRaisesRegex(ValueError, "does not match"):
                crm.validate_identity(cfg)

    def test_shared_auth_rejects_another_tenant_before_api(self):
        with patch("native_voice_setup.subprocess.run", return_value=SimpleNamespace(stdout=json.dumps({
            "tenant": identifier(99), "accessToken": "synthetic-not-a-credential",
        }))):
            with self.assertRaisesRegex(ValueError, "another tenant"):
                crm.Dataverse(config()["organization_url"], config()["tenant_id"])

    def test_private_input_uses_shared_path_permission_reader_and_size_bound(self):
        with self.assertRaisesRegex(ValueError, "outside the repository"):
            crm.load_document(ROOT / "docs/dynamics-native-voice/examples/crm-seed.example.json")
        path = MagicMock()
        path.stat.return_value.st_size = 100
        with patch.object(crm, "private_path", return_value=path), \
                patch.object(crm, "read_private", return_value='{"synthetic_only": true}') as read:
            self.assertEqual(crm.load_document(Path("private.json")), {"synthetic_only": True})
            read.assert_called_once_with(path)
            path.stat.return_value.st_size = 2 * 1024 * 1024 + 1
            with self.assertRaisesRegex(ValueError, "bounded size"):
                crm.load_document(Path("private.json"))
            self.assertEqual(read.call_count, 1)


class SetupTests(unittest.TestCase):
    def setUp(self):
        self.config, self.seed = config(), seed()
        self.api, self.journal = FakeDataverse(self.config), MemoryJournal()
        self.subject = crm.Setup(self.config, self.seed, self.api, self.journal, apply=True, attempts=2, interval=0)
        self.identity_patch = patch.object(crm, "validate_identity", return_value={"verified": True})
        self.identity_patch.start()
        self.addCleanup(self.identity_patch.stop)

    def ready(self):
        self.subject.run()
        return self.journal.document

    def test_schema_only_crm_does_not_require_native_voice_or_seed_records(self):
        cfg = self.config
        cfg.update(native_voice=False, native_reader_client_id="", workstream_ids=[])
        cfg = crm.validate_config(cfg)
        api, journal = FakeDataverse(cfg), MemoryJournal(configuration=cfg, synthetic_seed=crm.EMPTY_SEED)
        subject = crm.Setup(cfg, crm.EMPTY_SEED, api, journal, apply=True, attempts=2, interval=0)
        subject.run()
        self.assertFalse(any("pluginassemblies" in row[1] or "msdyn_liveworkstreams" in row[1] for row in api.calls))
        self.assertFalse(set(api.privileges) & crm.NATIVE_PRIVILEGES)
        self.assertFalse(any(api.records[table] for table in ("accounts", "contacts", "incidents", "annotations", "incidentresolutions")))
        self.assertFalse(journal.document["checks"]["synthetic_seed"])
        self.assertTrue(journal.document["checks"]["runtime_reads_and_role_denial"])

    def test_complete_apply_and_unchanged_replay(self):
        self.ready()
        self.assertEqual(len(self.api.attributes), 9)
        self.assertEqual(len(self.api.keys), 2)
        self.assertEqual({key: len(self.api.records[key]) for key in (
            "accounts", "contacts", "incidents", "annotations", "incidentresolutions",
        )}, {"accounts": 1, "contacts": 2, "incidents": 2, "annotations": 2, "incidentresolutions": 1})
        self.assertEqual(set(self.api.privileges), set(crm.PRIVILEGES) | crm.PRESERVED_PLATFORM_READS)
        self.assertEqual(self.api.members[self.api.user_id], [self.api.role_id])
        self.assertEqual(self.journal.document["checks"]["minimum_role"], True)
        self.assertTrue(self.journal.document["checks"]["runtime_reads_and_role_denial"])
        writes_before = copy.deepcopy(self.api.writes)
        second = crm.Setup(self.config, self.seed, self.api, MemoryJournal(), apply=True, attempts=2, interval=0)
        second.run()
        self.assertEqual(self.api.writes, writes_before)
        self.assertEqual(second.journal.document["owned_records"], [])
        self.assertTrue(all(row["after"] is not None for row in self.journal.document["owned_records"]))
        for row in self.journal.document["owned_records"]:
            self.assertEqual(crm.guid(row["id"]), row["id"])
            if row["kind"] in ("attribute", "key"):
                self.assertEqual(row["id"], row["after"]["MetadataId"])
                self.assertTrue(row["path"].endswith(f"({row['id']})"))

    def test_targeted_publishing_and_safe_role_actions(self):
        self.ready()
        publishing = [row for row in self.api.writes if row["path"] == "PublishXml"]
        self.assertEqual(len(publishing), 1)
        self.assertEqual(publishing[0]["body"]["ParameterXml"],
                         "<importexportxml><entities><entity>contact</entity><entity>incident</entity></entities></importexportxml>")
        self.assertFalse(any("ReplacePrivileges" in row["path"] or row["path"] == "PublishAllXml" for row in self.api.writes))
        removed = {row["body"]["Privilege"]["privilegeid"] for row in self.api.writes if row["path"].endswith("RemovePrivilegeRole")}
        self.assertEqual(removed, {self.api.definitions[name] for name in crm.SHAREPOINT_DEFAULTS})
        self.assertTrue(crm.PRESERVED_PLATFORM_READS <= set(self.api.privileges))
        self.assertFalse(any("msdyn_ocliveworkitems" in row["path"] for row in self.api.writes))

    def test_runtime_created_records_and_actual_resolution_receipt(self):
        self.ready()
        for table in ("incidents", "annotations"):
            self.assertTrue(all(row["_createdby_value"] == self.api.user_id for row in self.api.records[table].values()))
        writes = [row for row in self.api.writes if row["method"] == "PATCH"]
        self.assertTrue(all(row["headers"]["If-None-Match"] == "*" for row in writes))
        activity = next(iter(self.api.records["incidentresolutions"].values()))
        owned = next(row for row in self.journal.document["owned_records"] if row["kind"] == "incidentresolutions")
        self.assertEqual(owned["id"], activity["activityid"])
        closure_event = next(row for row in self.journal.document["events"] if row["kind"] == "close-synthetic-case")
        self.assertEqual(closure_event["before"]["case"]["statecode"], 0)
        self.assertEqual(closure_event["after"]["case"]["statecode"], 1)
        self.assertEqual(closure_event["before"]["resolutions"], [])
        self.assertEqual(closure_event["after"]["resolutions"], [activity])

    def test_foreign_app_user_is_not_adopted(self):
        self.api.records["systemusers"][identifier(40)] = {
            "systemuserid": identifier(40), "applicationid": self.config["managed_identity"]["client_id"],
        }
        with self.assertRaisesRegex(ValueError, "unfamiliar application user"):
            self.subject.run()
        self.assertEqual(self.api.writes, [])

    def test_existing_user_roles_are_preserved(self):
        self.ready()
        self.api.members[self.api.user_id].append(identifier(41))
        before = copy.deepcopy(self.api.writes)
        with self.assertRaisesRegex(ValueError, "roles were preserved"):
            crm.Setup(self.config, self.seed, self.api, MemoryJournal(), apply=True).run()
        self.assertEqual(self.api.writes, before)
        self.assertIn(identifier(41), self.api.members[self.api.user_id])

    def test_inherited_team_roles_are_preserved_and_block_minimum_role_claim(self):
        self.ready()
        self.api.inherited_role = identifier(67)
        before = len(self.api.writes)
        with self.assertRaisesRegex(ValueError, "team-assigned runtime roles were preserved"):
            crm.Setup(self.config, self.seed, self.api, MemoryJournal(), apply=True).run()
        self.assertEqual(len(self.api.writes), before)
        self.assertEqual(self.api.inherited_role, identifier(67))

    def test_role_shared_with_another_user_is_never_modified(self):
        self.ready()
        self.api.members[identifier(68)] = [self.api.role_id]
        self.api.privileges.pop("prvReadPluginAssembly")
        before = copy.deepcopy(self.api.privileges)
        writes_before = len(self.api.writes)
        with self.assertRaisesRegex(ValueError, "shared with another user or team"):
            crm.Setup(self.config, self.seed, self.api, MemoryJournal(), apply=True,
                      previous=self.journal.document).run()
        self.assertEqual(self.api.privileges, before)
        self.assertEqual(len(self.api.writes), writes_before)

    def test_wider_and_unexpected_privileges_are_never_removed(self):
        self.ready()
        for privilege, depth in (("prvReadContact", "Global"), ("prvReadRole", "Global")):
            old = dict(self.api.privileges)
            self.api.privileges[privilege] = depth
            before = len(self.api.writes)
            with self.subTest(privilege=privilege), self.assertRaisesRegex(ValueError, "preserved"):
                crm.Setup(self.config, self.seed, self.api, MemoryJournal(), apply=True).identity()
            self.assertEqual(len(self.api.writes), before)
            self.assertEqual(self.api.privileges[privilege], depth)
            self.api.privileges = old

    def test_existing_role_change_requires_ownership_receipt(self):
        prior = copy.deepcopy(self.ready())
        self.api.privileges.pop("prvReadPluginAssembly")
        subject = crm.Setup(self.config, self.seed, self.api, MemoryJournal(), apply=True)
        subject.definitions = self.subject.definitions
        with self.assertRaisesRegex(ValueError, "original owned-record"):
            subject.identity()
        self.assertNotIn("prvReadPluginAssembly", self.api.privileges)
        subject.previous = prior
        subject.identity()
        self.assertEqual(self.api.privileges["prvReadPluginAssembly"], "Global")

    def test_sharepoint_removal_requires_documented_platform_provenance(self):
        prior = copy.deepcopy(self.ready())
        self.api.privileges["prvReadSharePointData"] = "Global"
        prior["events"] = []
        subject = crm.Setup(self.config, self.seed, self.api, MemoryJournal(), apply=True, previous=prior)
        subject.definitions = self.subject.definitions
        before = len(self.api.writes)
        with self.assertRaisesRegex(ValueError, "recorded as platform-added"):
            subject.identity()
        self.assertEqual(len(self.api.writes), before)
        self.assertIn("prvReadSharePointData", self.api.privileges)

    def test_sharepoint_reintroduced_after_success_is_preserved(self):
        prior = copy.deepcopy(self.ready())
        self.api.privileges["prvWriteSharePointData"] = "Global"
        subject = crm.Setup(self.config, self.seed, self.api, MemoryJournal(), apply=True, previous=prior)
        subject.definitions = self.subject.definitions
        before = len(self.api.writes)
        with self.assertRaisesRegex(ValueError, "recorded as platform-added"):
            subject.identity()
        self.assertIn("prvWriteSharePointData", self.api.privileges)
        self.assertEqual(len(self.api.writes), before)

    def test_partial_role_setup_can_resume_only_unremoved_observed_defaults(self):
        self.subject.preflight()
        original = self.api.request
        failed_once = False

        def partial(path, **kwargs):
            nonlocal failed_once
            if path.endswith("RemovePrivilegeRole") and not failed_once:
                failed_once = True
                raise http_error(503)
            return original(path, **kwargs)

        with patch.object(self.api, "request", side_effect=partial), self.assertRaises(HTTPError) as raised:
            self.subject.identity()
        raised.exception.close()
        self.assertTrue(crm.SHAREPOINT_DEFAULTS <= set(self.api.privileges))
        retry = crm.Setup(self.config, self.seed, self.api, MemoryJournal(), apply=True, previous=self.journal.document)
        retry.definitions = self.subject.definitions
        retry.identity()
        self.assertFalse(crm.SHAREPOINT_DEFAULTS & set(self.api.privileges))

    def test_missing_privilege_definition_stops_before_writes(self):
        self.api.definitions.pop("prvReadPluginAssembly")
        with self.assertRaisesRegex(ValueError, "required CRM privilege is missing"):
            self.subject.run()
        self.assertEqual(self.api.writes, [])

    def test_typed_metadata_404_retries_without_recreating_column(self):
        self.api.typed_metadata_not_found = 1
        self.ready()
        creates = [write for write in self.api.writes
                   if write["path"].endswith("/Attributes") and write["body"]["SchemaName"] == "moacc_actorid"]
        self.assertEqual(len(creates), 1)
        self.assertEqual(self.api.typed_metadata_not_found, 0)
        self.assertTrue(self.journal.document["checks"]["schema_and_active_keys"])

    def test_typed_metadata_non_404_is_not_hidden(self):
        self.ready()
        original = self.api.get

        def read(path, query=None):
            if "/Microsoft.Dynamics.CRM.StringAttributeMetadata" in path:
                raise http_error(403)
            return original(path, query)

        with patch.object(self.api, "get", side_effect=read):
            with self.assertRaises(HTTPError) as raised:
                self.subject.metadata("contact", "moacc_actorid", 100)
        self.assertEqual(raised.exception.code, 403)
        raised.exception.close()

    def test_existing_generic_attribute_waits_for_typed_view_without_create(self):
        self.ready()
        before = copy.deepcopy(self.api.writes)
        self.api.typed_metadata_not_found = 1
        self.subject.schema()
        self.assertEqual(self.api.typed_metadata_not_found, 0)
        self.assertEqual(self.api.writes, before)

    def test_admin_token_cannot_be_runtime_identity(self):
        self.api.records["systemusers"][self.api.admin_id]["applicationid"] = self.config["managed_identity"]["client_id"]
        with self.assertRaisesRegex(ValueError, "setup and runtime identities must be separate"):
            self.subject.preflight()
        self.assertEqual(self.api.writes, [])

    def test_runtime_identity_and_denial_must_actually_verify(self):
        self.ready()
        self.api.impersonation_ignored = True
        with self.assertRaisesRegex(ValueError, "impersonation"):
            self.subject.runtime_checks()
        self.api.impersonation_ignored = False
        self.api.role_reads_allowed = True
        with self.assertRaisesRegex(ValueError, "read security roles"):
            self.subject.runtime_checks()

    def test_runtime_plugin_assembly_privilege_is_not_optional(self):
        self.ready()
        self.api.privileges.pop("prvReadPluginAssembly")
        with self.assertRaises(HTTPError) as raised:
            self.subject.runtime_checks()
        self.assertEqual(raised.exception.code, 403)
        raised.exception.close()

    def role_denial(self, *, principal=None, aad=None, caller=None):
        return json.dumps({"error": {
            "code": "0x80040220",
            "message": (
                f"Principal user (Id={principal or self.subject.user_id}, type=8, "
                f"AADObjectId='{aad or self.config['managed_identity']['principal_id']}') "
                "is missing prvReadRole privilege for entity 'role'. "
                f"context.Caller={caller or self.subject.user_id}."
            ),
        }})

    def test_authenticator_who_requires_exact_impersonated_denial_subject(self):
        self.ready()
        self.api.runtime_who_reports_authenticator = True
        self.api.role_denial_detail = self.role_denial()
        before = copy.deepcopy(self.api.writes)
        self.subject.runtime_checks()
        evidence = [event for event in self.journal.document["events"]
                    if event["kind"] == "runtime-read-verification"][-1]["after"]
        self.assertEqual(evidence["identity_verification"], "exact_role_denial_subject")
        self.assertEqual(evidence["effective_runtime_user_id"], self.subject.user_id)
        self.assertEqual(evidence["who_am_i"]["UserId"], self.api.admin_id)
        self.assertEqual(self.api.writes, before)

    def test_authenticator_who_rejects_foreign_or_unproven_denial(self):
        self.ready()
        self.api.runtime_who_reports_authenticator = True
        valid = self.role_denial()
        invalid = (
            self.role_denial(principal=identifier(91)),
            self.role_denial(aad=identifier(92)),
            self.role_denial(caller=identifier(93)),
            '{"error":{"message":"Missing prvReadRole"}}',
            valid.replace("0x80040220", "0x80000000"),
            valid.replace("for entity 'role'", "for entity 'contact'"),
            valid.replace("context.Caller=", "another.Caller="),
            valid.replace("type=8,", f"type=8, AADObjectId='{identifier(94)}',"),
            "not JSON prvReadRole",
        )
        before = copy.deepcopy(self.api.writes)
        for detail in invalid:
            with self.subTest(detail=detail):
                self.api.role_denial_detail = detail
                with self.assertRaises(ValueError):
                    self.subject.runtime_checks()
        self.assertEqual(self.api.writes, before)

    def test_authenticator_who_cannot_use_proof_without_original_operator(self):
        self.ready()
        self.api.runtime_who_reports_authenticator = True
        self.api.role_denial_detail = self.role_denial()
        for operator in (None, identifier(95)):
            with self.subTest(operator=operator):
                self.subject.operator_user_id = operator
                with self.assertRaisesRegex(ValueError, "impersonation"):
                    self.subject.runtime_checks()

    def test_authenticator_who_still_rejects_runtime_role_access(self):
        self.ready()
        self.api.runtime_who_reports_authenticator = True
        self.api.role_denial_detail = self.role_denial()
        self.api.role_reads_allowed = True
        with self.assertRaisesRegex(ValueError, "read security roles"):
            self.subject.runtime_checks()

    def test_verify_never_creates_missing_schema_or_seed(self):
        read_only = crm.Setup(self.config, self.seed, self.api, MemoryJournal(), apply=False, attempts=1, interval=0)
        with self.assertRaisesRegex(ValueError, "schema is incomplete"):
            read_only.run()
        self.assertEqual(self.api.writes, [])
        self.ready()
        before = len(self.api.writes)
        read_only.run()
        self.assertEqual(len(self.api.writes), before)
        self.api.records["annotations"].clear()
        with self.assertRaisesRegex(ValueError, "seed record is missing"):
            read_only.run()
        self.assertEqual(len(self.api.writes), before)

    def test_existing_seed_edits_are_never_overwritten(self):
        self.ready()
        record = next(iter(self.api.records["contacts"].values()))
        record["description"] = "A user-owned edit"
        before = len(self.api.writes)
        with self.assertRaisesRegex(ValueError, "user data was preserved"):
            crm.Setup(self.config, self.seed, self.api, MemoryJournal(), apply=True).seed_data()
        self.assertEqual(record["description"], "A user-owned edit")
        self.assertEqual(len(self.api.writes), before)

    def test_actor_collision_refuses_an_unfamiliar_record(self):
        spec = next(row for row in crm.seed_records(self.config, self.seed)[0] if row["table"] == "contacts")
        self.api.records["contacts"][identifier(42)] = {"contactid": identifier(42), "moacc_actorid": spec["body"]["moacc_actorid"]}
        with self.assertRaisesRegex(ValueError, "another record"):
            self.subject.record(spec)
        self.assertEqual(self.api.writes, [])
        self.assertEqual(len(self.api.records["contacts"]), 1)

    def test_conditional_create_failure_does_not_retry_or_claim_ownership(self):
        spec = crm.seed_records(self.config, self.seed)[0][0]
        self.api.fail_writes = 412
        with self.assertRaises(HTTPError) as raised:
            self.subject.record(spec)
        raised.exception.close()
        self.assertEqual(len(self.api.writes), 1)
        self.assertEqual(self.journal.document["owned_records"], [])
        event = self.journal.document["events"][-1]
        self.assertEqual(event["status"], "failed_or_outcome_unknown")
        self.assertIsNone(event["before"])
        self.assertIn("after_unavailable", event)
        self.assertIn("0001-before.json", self.journal.files)
        self.assertIn("0001-after.json", self.journal.files)

    def test_only_owned_unchanged_cases_can_be_closed(self):
        records, closures = crm.seed_records(self.config, self.seed)
        for spec in records:
            self.subject.record(spec)
        for previous, error in (({}, "receipt-owned"), (self.journal.document, "changed since creation")):
            subject = crm.Setup(self.config, self.seed, self.api, MemoryJournal(), apply=True, previous=previous)
            if previous:
                self.api.records["incidents"][closures[0]["case_id"]]["@odata.etag"] = 'W/"999999"'
            before = len(self.api.writes)
            with self.subTest(error=error), self.assertRaisesRegex(ValueError, error):
                subject.close(closures[0])
            self.assertEqual(len(self.api.writes), before)

    def test_close_timeout_is_reconciled_without_replacement_action(self):
        records, closures = crm.seed_records(self.config, self.seed)
        for spec in records:
            self.subject.record(spec)
        self.api.close_timeout = True
        with self.assertRaises(TimeoutError):
            self.subject.close(closures[0])
        event = self.journal.document["events"][-1]
        self.assertEqual(event["status"], "failed_or_outcome_unknown")
        self.assertEqual(event["after"]["case"]["statecode"], 1)
        self.assertEqual(len(event["after"]["resolutions"]), 1)
        self.assertFalse(any(row["kind"] == "incidentresolutions" for row in self.journal.document["owned_records"]))
        self.subject.close(closures[0])
        self.assertEqual(sum(row["path"] == "CloseIncident" for row in self.api.writes), 1)

    def test_missing_resolution_has_bounded_failure_no_fabricated_activity(self):
        records, closures = crm.seed_records(self.config, self.seed)
        for spec in records:
            self.subject.record(spec)
        self.api.missing_resolution = True
        with patch.object(crm.time, "sleep") as sleep, self.assertRaisesRegex(RuntimeError, "Bounded wait"):
            self.subject.close(closures[0])
        self.assertLessEqual(sleep.call_count, 2)
        self.assertFalse(any(row["path"].startswith("incidentresolutions") for row in self.api.writes))
        self.assertEqual(sum(row["path"] == "CloseIncident" for row in self.api.writes), 1)

    def test_key_active_failed_and_bounded_waits(self):
        self.subject.schema()
        self.assertEqual(len(self.api.writes), 12)
        key = next(iter(self.api.keys.values()))
        key["EntityKeyIndexStatus"] = "Failed"
        before = len(self.api.writes)
        with self.assertRaisesRegex(RuntimeError, "index creation failed"):
            self.subject.schema()
        self.assertEqual(len(self.api.writes), before)
        key["EntityKeyIndexStatus"] = "InProgress"
        with patch.object(crm.time, "sleep") as sleep, self.assertRaisesRegex(RuntimeError, "Bounded wait"):
            self.subject.schema()
        self.assertEqual(sleep.call_count, 1)
        read = MagicMock(side_effect=[False, True])
        with patch.object(crm.time, "sleep") as sleep:
            self.assertTrue(self.subject.wait(read, bool, "test"))
        self.assertEqual(read.call_count, 2)
        self.assertEqual(sleep.call_count, 1)


class ReceiptAndCliTests(unittest.TestCase):
    def test_journal_reuses_private_output_helpers_and_exclusive_directory(self):
        directory = MagicMock()
        directory.__truediv__.side_effect = lambda name: Path("not-created-unit-test-output") / name
        with patch.object(crm, "private_path", return_value=directory) as private, patch.object(crm, "write_private") as write:
            journal = crm.Journal(Path("private-output"), config(), seed(), "plan")
            journal.finish("succeeded")
        private.assert_called_once_with(Path("private-output"))
        directory.mkdir.assert_called_once_with(mode=0o700)
        self.assertEqual(write.call_count, 2)
        self.assertEqual(write.call_args.args[0].name, "receipt.json")
        saved = json.loads(write.call_args.args[1])
        self.assertEqual(saved["status"], "succeeded")

    def test_journal_persists_intent_before_request_and_exact_after(self):
        journal = MemoryJournal()
        before, after = {"value": "previous"}, {"value": "confirmed"}
        api = MagicMock()

        def mutate(*args, **kwargs):
            self.assertIn("0001-before.json", journal.files)
            self.assertEqual(journal.files["0001-before.json"]["before"], before)

        api.request.side_effect = mutate
        journal.mutate(api, "test", "synthetic", body={"value": "confirmed"}, before=before, after=lambda: after)
        self.assertEqual(journal.files["0001-after.json"]["after"], after)
        self.assertEqual(journal.document["events"][0]["before"], before)
        self.assertEqual(journal.document["events"][0]["status"], "confirmed")

    def test_a_before_receipt_write_failure_prevents_cloud_mutation(self):
        journal, api = MemoryJournal(), MagicMock()
        with patch.object(journal, "write", side_effect=OSError("synthetic disk failure")), self.assertRaises(OSError):
            journal.mutate(api, "test", "synthetic", body={"value": 1}, after=lambda: {})
        api.request.assert_not_called()

    def call_cli(self, extra=(), documents=None):
        journal = MemoryJournal()
        args = ["--config", "private-config.json", "--seed", "private-seed.json", "--output-dir", "private-output", *extra]
        with patch.object(crm, "load_document", side_effect=documents or [config(), seed()]), \
                patch.object(crm, "Journal", return_value=journal), \
                patch.object(crm, "write_private"), patch.object(crm, "Dataverse") as api, \
                patch.object(crm, "Setup") as setup, patch("sys.stdout", new_callable=io.StringIO), \
                patch("sys.stderr", new_callable=io.StringIO) as stderr:
            status = crm.main(args)
        return status, journal, api, setup, stderr.getvalue()

    def test_default_plan_never_authenticates_or_writes_dataverse(self):
        status, journal, api, setup, _ = self.call_cli()
        self.assertEqual(status, 0)
        api.assert_not_called()
        setup.assert_not_called()
        self.assertTrue(journal.files["plan.json"]["offline_contract_check_only"])
        self.assertEqual(journal.document["status"], "succeeded")
        self.assertFalse(journal.document["owned_records"])

    def test_apply_requires_exact_org_confirmation_before_auth(self):
        for extra in (["--apply"], ["--apply", "--confirm-organization", "https://wrong.crm.dynamics.com"]):
            status, _, api, setup, _ = self.call_cli(extra)
            self.assertEqual(status, 1)
            api.assert_not_called()
            setup.assert_not_called()
        status, _, api, setup, _ = self.call_cli(["--apply", "--approve-seed", "--confirm-organization", config()["organization_url"]])
        self.assertEqual(status, 0)
        api.assert_called_once_with(config()["organization_url"], config()["tenant_id"])
        self.assertTrue(setup.call_args.kwargs["apply"])

    def test_invalid_seed_and_guard_flags_never_authenticate(self):
        invalid = seed()
        invalid["synthetic_only"] = False
        status, _, api, _, _ = self.call_cli(documents=[config(), invalid])
        self.assertEqual(status, 1)
        api.assert_not_called()
        for extra in (["--publish-schema"], ["--wait-attempts", "31"], ["--wait-seconds", "-1"]):
            status, _, api, _, _ = self.call_cli(extra)
            self.assertEqual(status, 1)
            api.assert_not_called()

    def test_verify_mode_passes_apply_false(self):
        status, _, api, setup, _ = self.call_cli(["--verify"])
        self.assertEqual(status, 0)
        self.assertFalse(setup.call_args.kwargs["apply"])
        api.assert_called_once()

    def test_rollback_plan_mode_never_authenticates(self):
        prior = MemoryJournal().document
        prior["status"] = "succeeded"
        status, journal, api, setup, _ = self.call_cli(
            ["--rollback-plan-from", "original-apply.json"], [config(), seed(), prior],
        )
        self.assertEqual(status, 0)
        api.assert_not_called()
        setup.assert_not_called()
        self.assertFalse(journal.files["rollback-plan.json"]["executed"])

    def test_apply_verify_and_rollback_are_mutually_exclusive(self):
        for flags in (["--apply", "--verify"], ["--apply", "--rollback-plan-from", "receipt.json"]):
            with self.subTest(flags=flags), patch("sys.stderr", new_callable=io.StringIO), self.assertRaises(SystemExit) as raised:
                crm.parser().parse_args(["--config", "config.json", "--seed", "seed.json", "--output-dir", "output", *flags])
            self.assertEqual(raised.exception.code, 2)

    def test_failure_is_persisted_without_credentials_or_http_error_bodies(self):
        journal = MemoryJournal()
        args = ["--config", "config.json", "--seed", "seed.json", "--output-dir", "output", "--verify"]
        with patch.object(crm, "load_document", side_effect=[config(), seed()]), \
                patch.object(crm, "Journal", return_value=journal), patch.object(crm, "write_private"), \
                patch.object(crm, "Dataverse", side_effect=http_error(401, "PRIVATE RESPONSE MUST NOT LEAK")), \
                patch("sys.stderr", new_callable=io.StringIO) as stderr:
            self.assertEqual(crm.main(args), 1)
        self.assertEqual(journal.files["receipt.json"]["status"], "failed")
        self.assertNotIn("PRIVATE RESPONSE", stderr.getvalue())
        self.assertEqual(journal.document["error"], {"type": "HTTPError", "http_status": 401})

    def test_receipt_disk_failure_reports_recovery_location_without_claiming_success(self):
        journal = MemoryJournal()
        args = ["--config", "config.json", "--seed", "seed.json", "--output-dir", "output", "--verify"]
        with patch.object(crm, "load_document", side_effect=[config(), seed()]), \
                patch.object(crm, "Journal", return_value=journal), patch.object(crm, "write_private"), \
                patch.object(crm, "Dataverse", side_effect=TimeoutError()), \
                patch.object(journal, "finish", side_effect=OSError()), \
                patch("sys.stderr", new_callable=io.StringIO) as stderr:
            self.assertEqual(crm.main(args), 1)
        result = json.loads(stderr.getvalue())
        self.assertIsNone(result["private_receipt"])
        self.assertEqual(result["recovery_journal_directory"], str(journal.directory))
        self.assertEqual(result["error"]["receipt_write_failure"], {"type": "OSError"})

    def test_rollback_inventory_contains_only_owned_records_and_flags_unknowns(self):
        document = MemoryJournal().document
        document.update(status="failed", owned_records=[
            {"kind": "accounts", "path": f"accounts({identifier(55)})", "id": identifier(55), "created_by_this_run": True, "after": {}},
            {"kind": "contacts", "path": f"contacts({identifier(56)})", "id": identifier(56), "created_by_this_run": False, "after": {}},
        ])
        document["events"] = [{"kind": "create-synthetic-record", "status": "failed_or_outcome_unknown", "before": None}]
        rollback = crm.rollback_plan(document)
        self.assertFalse(rollback["executed"])
        self.assertEqual(len(rollback["owned_records_in_review_order"]), 1)
        self.assertEqual(len(rollback["unknown_outcomes"]), 1)
        self.assertTrue(rollback["review_required"])

    def test_prior_receipt_must_match_config_and_apply_mode(self):
        document = MemoryJournal().document
        with patch.object(crm, "load_document", return_value=document):
            self.assertEqual(crm.previous_receipt(Path("receipt.json"), config()), document)
            document["mode"] = "plan"
            with self.assertRaises(ValueError):
                crm.previous_receipt(Path("receipt.json"), config())
            document["mode"] = "apply"
            document["configuration_hash"] = "wrong"
            with self.assertRaises(ValueError):
                crm.previous_receipt(Path("receipt.json"), config())

    def test_multiple_prior_receipts_combine_proof_without_new_ownership_claim(self):
        first, second = MemoryJournal().document, MemoryJournal().document
        first["owned_records"] = [{"path": "first", "created_by_this_run": True}]
        second["owned_records"] = [{"path": "second", "created_by_this_run": True}]
        status, journal, _, setup, _ = self.call_cli(
            ["--verify", "--previous-receipt", "first.json", "--previous-receipt", "second.json"],
            [config(), seed(), first, second],
        )
        self.assertEqual(status, 0)
        self.assertEqual(len(setup.call_args.kwargs["previous"]["owned_records"]), 2)
        self.assertEqual(journal.document["owned_records"], [])


if __name__ == "__main__":
    unittest.main()
