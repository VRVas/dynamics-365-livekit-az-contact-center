import copy
import http.client
import importlib.util
import io
import json
import os
import re
import secrets
import sys
import tempfile
import unittest
import urllib.error
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch
from uuid import uuid5, NAMESPACE_URL

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "infra/scripts"))
import setup_native_supervision as setup
import stage_native_media_topic as topics


def uid(name):
    return str(uuid5(NAMESPACE_URL, "synthetic-supervision-" + name))


def config():
    return setup.validate_config({
        "version": 1, "organization_url": "https://example.crm17.dynamics.com",
        "tenant_id": uid("tenant"), "namespace_id": uid("namespace"), "business_unit_id": uid("bu"),
        "runtime_user_id": uid("runtime"), "runtime_application_id": uid("app"),
        "approved_actor_ids": [uid("human")], "controller_component_ids": [uid("controller")],
        "workstream_ids": [uid("stream")], "voice_origin": "https://voice.example.azurecontainerapps.io",
        "session_template_ids": [uid("session")], "session_binding_reviewed": True,
        "trust_system_administrators": False,
    })


class API:
    org = "https://example.crm17.dynamics.com"

    def __init__(self):
        self.records, self.links, self.privileges, self.writes = {}, {}, {}, []
        self.metadata = None
        self.privilege_names = {}
        self.commands = []
        self.solution_components = []
        self.fail_after_create = False
        cfg = config()
        for who in ("human", "runtime"):
            self.records[f"systemusers({uid(who)})"] = {
                "systemuserid": uid(who), "isdisabled": False, "accessmode": 0, "islicensed": True,
                "_businessunitid_value": uid("bu"),
                "applicationid": uid("app") if who == "runtime" else None,
                "systemuserroles_association": [{"roleid": uid("original_role"), "name": "Existing unchanged role"}],
            }
        source = topics.render_speech_turn_pilot(topics.load_topic(topics.TEMPLATE.read_text()), crm_case_context=True)
        self.records[f"botcomponents({uid('controller')})"] = {
            "botcomponentid": uid("controller"), "componenttype": 9, "data": source,
            "modifiedon": "2026-01-01T00:00:00Z", "_parentbotid_value": uid("bot"), "@odata.etag": 'W/"1"',
        }
        self.records[f"bots({uid('bot')})"] = {"publishedon": "2026-01-01T00:00:01Z"}
        self.records[f"msdyn_liveworkstreams({uid('stream')})"] = {
            "statecode": 0, "msdyn_direction": 0, "msdyn_streamsource": 192440000,
            "_msdyn_defaultqueue_value": uid("queue"), "_msdyn_bot_user_value": uid("botuser"),
        }
        self.records[f"msdyn_sessiontemplates({uid('session')})"] = {"msdyn_uniquename": "owned_test_session", "ismanaged": False}
        self.links[f"msdyn_sessiontemplates({uid('session')})/msdyn_sessiontemplate_applicationtab"] = {
            uid("existing_tab"): {"msdyn_applicationtabtemplateid": uid("existing_tab")},
        }
        self.count = 1

    def get(self, path, query=None):
        if path == "importjobs":
            return {"value": []}
        if path == "WhoAmI":
            return {"UserId": uid("operator"), "OrganizationId": uid("organization"), "BusinessUnitId": uid("bu")}
        if path.endswith("/Microsoft.Dynamics.CRM.RetrieveUnpublished"):
            path = path.split("/Microsoft.", 1)[0]
        if path == "RetrieveProvisionedLanguages()":
            return {"RetrieveProvisionedLanguages": [1033, 1025]}
        if path.startswith("EntityDefinitions"):
            if self.metadata is None:
                raise urllib.error.HTTPError(path, 404, "missing", {}, None)
            return copy.deepcopy(self.metadata)
        if path.startswith("RetrieveRolePrivilegesRole"):
            role = re.search(r"RoleId=([0-9a-f-]+)", path).group(1)
            return {"RolePrivileges": [{"PrivilegeId": key, "Depth": depth} for key, depth in self.privileges.get(role, {}).items()]}
        if path not in self.records:
            raise urllib.error.HTTPError(path, 404, "missing", {}, None)
        return copy.deepcopy(self.records[path])

    def rows(self, path, query):
        if path == "solutions":
            publisher = query.get("$filter", "").rsplit(" ", 1)[-1]
            return [copy.deepcopy(row) for key, row in self.records.items()
                    if key.startswith("solutions(") and row.get("_publisherid_value") == publisher]
        if path == "privileges":
            identifiers = re.findall(r"privilegeid eq ([0-9a-f-]+)", query["$filter"])
            if identifiers:
                return [{"name": self.privilege_names[identifier], "privilegeid": identifier}
                        for identifier in identifiers if identifier in self.privilege_names]
            return [{"name": name, "privilegeid": uid(name)} for name in re.findall(r"name eq '([^']+)'", query["$filter"])]
        if path == "msdyn_ocliveworkitems":
            return []
        if path == setup.TABLE:
            return self.commands
        if path == "solutioncomponents":
            return self.solution_components
        if path == "msdyn_templateparameters":
            parent = re.search(r"_msdyn_applicationtabtemplateid_value eq ([0-9a-f-]+)", query["$filter"]).group(1)
            return [copy.deepcopy(row) for path, row in self.records.items()
                    if path.startswith("msdyn_templateparameters(")
                    and row.get("_msdyn_applicationtabtemplateid_value") == parent
                    and row.get("msdyn_name") == "webresourceName"]
        rows = list(self.links.get(path, {}).values())
        if "$filter" in query:
            identifier = query["$filter"].rsplit(" ", 1)[-1]
            return [row for row in rows if identifier in row.values()]
        if rows:
            return copy.deepcopy(rows)
        # Reverse association lookup for rollback preflight.
        match = re.fullmatch(r"(roles|fieldsecurityprofiles)\(([^)]+)\)/(systemuserroles_association|systemuserprofiles_association)", path)
        if match:
            results = []
            if match[1] == "roles":
                for source, members in self.links.items():
                    if source.startswith("systemusers(") and match[2] in members:
                        results.append({"systemuserid": source.split("(", 1)[1].split(")", 1)[0]})
            return results
        return []

    def request(self, path, *, method="GET", body=None, headers=None, raw=False):
        if method == "GET":
            return self.get(path)
        self.writes.append((method, path, copy.deepcopy(body), headers))
        if path == "EntityDefinitions" and method == "POST":
            assert headers["MSCRM.SolutionUniqueName"] == setup.solution_name(config())
            assert len(body["Attributes"]) == 1 and body["Attributes"][0]["IsPrimaryName"] is True
            assert "EntitySetName" not in body and "IsOptimisticConcurrencyEnabled" not in body
            self.metadata = {
                "MetadataId": uid("metadata"), "EntitySetName": setup.TABLE, "OwnershipType": "UserOwned",
                "IsOptimisticConcurrencyEnabled": True,
                "Attributes": [
                    {**item, "MetadataId": uid(item["SchemaName"]), "LogicalName": item["SchemaName"].lower(),
                     "AttributeType": item["@odata.type"].split(".")[-1].removesuffix("AttributeMetadata"),
                     "IsSecured": item.get("IsSecured", False)} for item in body["Attributes"]
                ] + [{"LogicalName": setup.PRIMARY, "AttributeType": "Uniqueidentifier",
                      "MetadataId": uid(setup.PRIMARY), "IsSecured": False}],
            }
            self.solution_components.append({"objectid": uid("metadata")})
            return {}
        if path.startswith("EntityDefinitions") and method == "DELETE":
            removed = {self.metadata["MetadataId"], *(item["MetadataId"] for item in self.metadata["Attributes"])}
            self.metadata = None
            self.solution_components = [row for row in self.solution_components if row["objectid"] not in removed]
            return {}
        if path == f"EntityDefinitions({uid('metadata')})/Attributes" and method == "POST":
            assert headers["MSCRM.SolutionUniqueName"] == setup.solution_name(config())
            name = body["SchemaName"].lower()
            assert not any(item["LogicalName"] == name for item in self.metadata["Attributes"])
            self.metadata["Attributes"].append({
                **copy.deepcopy(body), "LogicalName": name, "MetadataId": uid(name),
                "AttributeType": body["AttributeType"],
            })
            self.solution_components.append({"objectid": uid(name)})
            return {}
        if path == "PublishXml":
            for key, row in self.records.items():
                if key.startswith("webresourceset("):
                    row["componentstate"] = 0
            return {}
        if path.endswith("AddPrivilegesRole"):
            role = path.split("(", 1)[1].split(")", 1)[0]
            self.privileges.setdefault(role, {}).update({item["PrivilegeId"]: item["Depth"] for item in body["Privileges"]})
            return {}
        if path.endswith("RemovePrivilegeRole"):
            role = path.split("(", 1)[1].split(")", 1)[0]
            self.privileges[role].pop(body["Privilege"]["privilegeid"], None)
            return {}
        if path.endswith("/$ref"):
            base = path[:-5]
            if method == "POST":
                target = body["@odata.id"].rsplit("/", 1)[1]
                table, identifier = target.split("(", 1)
                identifier = identifier[:-1]
                primary = {"systemusers": "systemuserid", "roles": "roleid",
                           "msdyn_applicationtabtemplates": "msdyn_applicationtabtemplateid"}[table]
                self.links.setdefault(base, {})[identifier] = {primary: identifier}
            else:
                base, identifier = base.rsplit("(", 1)
                self.links.get(base, {}).pop(identifier[:-1], None)
            return {}
        if method == "POST":
            primary = {
                "publishers": "publisherid", "solutions": "solutionid",
                "roles": "roleid", "fieldsecurityprofiles": "fieldsecurityprofileid", "fieldpermissions": "fieldpermissionid",
                "webresourceset": "webresourceid", "msdyn_applicationtabtemplates": "msdyn_applicationtabtemplateid",
                "msdyn_templateparameters": "msdyn_templateparameterid",
            }[path]
            row = copy.deepcopy(body)
            for key, value in body.items():
                if key.endswith("@odata.bind"):
                    row["_" + key.split("@", 1)[0] + "_value"] = value.rsplit("(", 1)[1][:-1]
            row["@odata.etag"] = 'W/"1"'
            row["createdon"] = datetime.now(timezone.utc).isoformat()
            row["_createdby_value"] = uid("operator")
            if path == "webresourceset":
                assert headers["MSCRM.SolutionUniqueName"] == setup.solution_name(config())
                row["componentstate"] = 1
                self.solution_components.append({"objectid": row[primary]})
            self.records[f"{path}({row[primary]})"] = row
            if path == "msdyn_templateparameters":
                assert row.get("msdyn_uniquename")
                parent = "msdyn_applicationtabtemplates(" + row["_msdyn_applicationtabtemplateid_value"] + ")"
                self.records[parent]["msdyn_templateparameters"] = json.dumps({row["msdyn_name"]: {"value": row["msdyn_value"]}})
            if self.fail_after_create:
                self.fail_after_create = False
                raise urllib.error.HTTPError(path, 503, "ambiguous response", {}, None)
            return {}
        if method in {"PATCH", "DELETE"}:
            row = self.records[path]
            assert headers["If-Match"] == row["@odata.etag"]
            if method == "DELETE":
                self.solution_components = [
                    item for item in self.solution_components if item["objectid"] not in row.values()
                ]
                del self.records[path]
            else:
                row.update(body)
                self.count += 1
                row.update({"@odata.etag": f'W/"{self.count}"', "modifiedon": "2026-01-02T00:00:00Z"})
                if path.startswith("msdyn_templateparameters("):
                    parent = "msdyn_applicationtabtemplates(" + row["_msdyn_applicationtabtemplateid_value"] + ")"
                    self.records[parent]["msdyn_templateparameters"] = json.dumps({row["msdyn_name"]: {"value": row["msdyn_value"]}})
            return {}
        raise AssertionError((method, path))


class SetupTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.api = API()
        self.config = config()
        self.plan = setup.build_plan(self.api, self.config)
        self.journal = setup.Journal(Path(self.directory.name), self.plan)
        self.runner = setup.Setup(self.api, self.plan, self.journal)

    def stage(self):
        for action in ("schema", "access", "controller", "webresource", "bind", "publish"):
            getattr(self.runner, action)()

    def notice_controller(self, *, legacy=True, applied=True):
        source = topics.render_speech_turn_pilot(
            topics.load_topic(topics.TEMPLATE.read_text()), crm_case_context=True,
        ).replace("__VOICE_ORIGIN__", self.config["voice_origin"])
        document = topics.load_topic(source)
        node = next(node for node in document["beginDialog"]["actions"] if node.get("id") == "native_start")
        node["body"]["content"] = node["body"]["content"].replace(
            "speech_turn_control: true", "speech_turn_control: true, play_startup_notice: true",
        )
        node["url"] += "?wait_for_context=true"
        node["latencyMessageSettings"] = topics.startup_notice_latency_settings(self.config["voice_origin"])
        notice = next(node for node in document["beginDialog"]["actions"] if node.get("id") == "native_connecting_notice")
        notice["activity"] = {"speak": ['<break time="20ms" />']}
        if legacy:
            node["latencyMessageSettings"]["latencyMessage"]["text"] = [setup.LEGACY_NOTICE_TEXT]
        self.api.records[f"botcomponents({uid('controller')})"]["data"] = setup.native.dump(document)
        self.plan = setup.build_plan(self.api, self.config)
        self.journal = setup.Journal(Path(self.directory.name), self.plan)
        self.runner = setup.Setup(self.api, self.plan, self.journal)
        if applied:
            self.stage()
            self.api.records[f"bots({uid('bot')})"]["publishedon"] = "2026-01-03T00:00:00Z"

    def upgrade_notice(self):
        with patch.object(self.runner, "assert_idle_disabled") as guarded:
            self.runner.upgrade_startup_notice()
        guarded.assert_called_once()

    def upgrade_playback(self):
        with patch.object(self.runner, "assert_idle_disabled") as guarded:
            self.runner.upgrade_startup_playback()
        guarded.assert_called_once()

    def upgrade_native_startup(self):
        with patch.object(self.runner, "assert_idle_disabled", return_value={
            "native_startup_notice_supported": True, "startup_failure_notice_supported": True,
        }) as guarded:
            self.runner.upgrade_native_startup()
        guarded.assert_called_once()

    def upgrade_application_startup(self):
        with patch.object(self.runner, "assert_idle_disabled", return_value={
            "application_startup_preparation_supported": True, "startup_failure_notice_supported": True,
        }) as guarded:
            self.runner.upgrade_application_startup()
        guarded.assert_called_once()

    def historical_quiet_controller(self):
        self.notice_controller(applied=False)
        change = self.plan["controllers"][uid("controller")]
        document = topics.load_topic(change["after_data"])
        actions = document["beginDialog"]["actions"]
        guard = next(node for node in actions if node.get("id") == "native_supervision_check_startup_failure")
        actions[actions.index(guard)] = guard["elseActions"][0]

        def old_fields(value):
            if isinstance(value, dict):
                if value.get("kind") == "HttpRequestAction":
                    value["responseSchema"]["properties"].pop("startup_failed_unpaused", None)
                if value.get("kind") == "SetVariable" and isinstance(value.get("value"), str):
                    value["value"] = value["value"].replace(", startup_failed_unpaused: false", "")
                for child in value.values():
                    old_fields(child)
            elif isinstance(value, list):
                for child in value:
                    old_fields(child)

        old_fields(document)
        change["after_data"] = setup.native.dump(document)
        change["after_sha256"] = setup.digest(change["after_data"])
        self.journal = setup.Journal(Path(self.directory.name), self.plan)
        self.runner = setup.Setup(self.api, self.plan, self.journal)
        self.stage()
        self.api.records[f"bots({uid('bot')})"]["publishedon"] = "2026-01-03T00:00:00Z"

    def rejected_legacy_fixture(self, status=400, code="0x80040216"):
        self.plan.pop("schema_requests", None)
        publisher = self.runner.row("publisher", "publishers", "publisherid", {
            "uniquename": setup.solution_name(self.config) + "_publisher", "friendlyname": setup.OWNER,
            "description": setup.OWNER, "customizationprefix": "moacc",
        })
        self.runner.row("solution", "solutions", "solutionid", {
            "uniquename": setup.solution_name(self.config), "friendlyname": setup.OWNER,
            "version": "1.0.0.0", "publisherid@odata.bind": f"/publishers({publisher})",
        })
        self.journal.append("intent", "schema", operation="metadata", before=None, body=self.plan["schema"])
        request = {
            "method": "POST", "path": "EntityDefinitions", "body": self.plan["schema"],
            "headers": {"MSCRM.SolutionUniqueName": setup.solution_name(self.config)},
            "at": datetime.now(timezone.utc).isoformat(),
        }
        self.journal.append("rejected", "schema", status=status)
        directory = Path(self.directory.name)
        request_path, response_path = directory / "native-intent.json", directory / "native-result.json"
        setup.native.write_private(request_path, json.dumps(request))
        setup.native.write_private(response_path, json.dumps({
            "http_status": status, "body": json.dumps({"error": {"code": code, "message": "native rejection"}}),
        }))
        protected_path = f"msdyn_sessiontemplates({uid('session')})"
        spec = [{"key": "template", "mode": "get", "path": protected_path, "query": None}]
        setup.native.write_private(directory / "protected-read-spec.json", json.dumps(spec))
        setup.native.write_private(directory / "protected-before.json", json.dumps({"template": self.api.get(protected_path)}))
        setup.native.write_private(directory / "roles-before.json", json.dumps({
            user: self.api.rows(f"systemusers({user})/systemuserroles_association", {"$select": "roleid,name"})
            for user in [*self.config["approved_actor_ids"], self.config["runtime_user_id"]]
        }))
        return request_path, response_path

    def test_rejected_unapplied_resolution_is_new_evidence_not_table_creation(self):
        request, response = self.rejected_legacy_fixture()
        original_events = copy.deepcopy(self.journal.events)
        writes = len(self.api.writes)
        with patch.object(self.runner, "assert_idle_disabled"):
            result = self.runner.reconcile_rejected_schema(request, response, "approved-recovery-reference")
        self.assertEqual(self.api.writes.__len__(), writes)
        self.assertEqual(self.journal.events[:-1], original_events)
        self.assertFalse(result["table_created"])
        self.assertIsNone(self.journal.last("done", "schema"))
        resolved = self.journal.last("rejected_unapplied_resolved", "schema")
        self.assertEqual(resolved["evidence"]["http_status"], 400)
        self.assertEqual(resolved["evidence"]["native_code"], "0x80040216")
        self.assertEqual(resolved["evidence"]["intent_hash"], self.journal.last("intent", "schema")["hash"])
        with patch.object(self.runner, "assert_idle_disabled"):
            self.runner.rollback()
        mutations = self.api.writes[writes:]
        self.assertEqual([path for method, path, _, _ in mutations if method == "DELETE"], [
            f"solutions({setup.owned(self.config, 'solution')})",
            f"publishers({setup.owned(self.config, 'publisher')})",
        ])
        self.assertTrue(all(method == "DELETE" for method, _, _, _ in mutations))
        self.assertIsNone(self.api.metadata)
        self.assertIsNotNone(self.journal.last("rejected_unapplied_cleanup_verified", "schema"))

    def test_unknown_or_server_error_is_not_resolved_by_absence(self):
        request, response = self.rejected_legacy_fixture(status=503, code="0x80040216")
        with patch.object(self.runner, "assert_idle_disabled"), self.assertRaisesRegex(ValueError, "definitely rejected"):
            self.runner.reconcile_rejected_schema(request, response, "approved-recovery-reference")
        self.assertIsNone(self.journal.last("rejected_unapplied_resolved", "schema"))

    def test_timeout_throttled_and_unknown_actual_responses_cannot_clear_a_recorded_intent(self):
        request, response = self.rejected_legacy_fixture()
        for status in (None, 408, 429, 500, 503):
            with self.subTest(status=status):
                response.write_text(json.dumps({
                    "http_status": status, "body": json.dumps({"error": {"code": "0x80040216"}}),
                }))
                with self.assertRaisesRegex(ValueError, "Absence cannot"):
                    self.runner.reconcile_rejected_schema(request, response, "approved-recovery-reference")
        self.assertIsNone(self.journal.last("rejected_unapplied_resolved", "schema"))

    def test_request_or_actual_response_drift_cannot_be_resolved(self):
        request, response = self.rejected_legacy_fixture()
        value = json.loads(request.read_text())
        value["body"] = {**value["body"], "SchemaName": "foreign_Table"}
        request.write_text(json.dumps(value))
        with self.assertRaisesRegex(ValueError, "differs from"):
            self.runner.reconcile_rejected_schema(request, response, "approved-recovery-reference")
        self.assertIsNone(self.journal.last("rejected_unapplied_resolved", "schema"))

    def test_foreign_references_or_protected_drift_stop_resolution(self):
        request, response = self.rejected_legacy_fixture()
        self.api.solution_components.append({"objectid": uid("foreign-component")})
        with patch.object(self.runner, "assert_idle_disabled"), self.assertRaisesRegex(ValueError, "not empty"):
            self.runner.reconcile_rejected_schema(request, response, "approved-recovery-reference")
        self.api.solution_components.clear()
        self.api.records[f"solutions({uid('foreign-solution')})"] = {
            "solutionid": uid("foreign-solution"), "_publisherid_value": setup.owned(self.config, "publisher"),
        }
        with patch.object(self.runner, "assert_idle_disabled"), self.assertRaisesRegex(ValueError, "Foreign publisher"):
            self.runner.reconcile_rejected_schema(request, response, "approved-recovery-reference")
        del self.api.records[f"solutions({uid('foreign-solution')})"]
        self.api.records[f"msdyn_sessiontemplates({uid('session')})"]["msdyn_uniquename"] = "changed"
        with patch.object(self.runner, "assert_idle_disabled"), self.assertRaisesRegex(ValueError, "Protected"):
            self.runner.reconcile_rejected_schema(request, response, "approved-recovery-reference")

    def test_later_drift_or_changed_native_response_blocks_resolved_cleanup(self):
        request, response = self.rejected_legacy_fixture()
        with patch.object(self.runner, "assert_idle_disabled"):
            self.runner.reconcile_rejected_schema(request, response, "approved-recovery-reference")
        value = json.loads(response.read_text())
        value["http_status"] = 500
        response.write_text(json.dumps(value))
        writes = len(self.api.writes)
        with patch.object(self.runner, "assert_idle_disabled"), self.assertRaisesRegex(ValueError, "Absence cannot"):
            self.runner.rollback()
        self.assertEqual(len(self.api.writes), writes)

    def test_plan_is_read_only_and_preserves_case_handoff_and_notice_content(self):
        self.assertEqual(self.api.writes, [])
        before = self.plan["controllers"][uid("controller")]["before"]["data"]
        after = self.plan["controllers"][uid("controller")]["after_data"]
        self.assertIn("native_set_case_number", before)
        self.assertIn("native_set_case_number", after)
        self.assertIn("supervision_contract: 1", after)
        self.assertIn("native_supervision_hold", after)
        self.assertIn("id: native_startup_failure_confirmed", after)
        self.assertIn("id: native_connection_error", after)

    def test_documented_primary_create_then_seven_individual_secured_columns(self):
        requests = self.plan["schema_requests"]
        primary = requests["table"]["Attributes"][0]
        self.assertEqual(len(requests["table"]["Attributes"]), 1)
        self.assertEqual(primary["SchemaName"], "moacc_name")
        self.assertEqual(primary["RequiredLevel"], {
            "Value": "None", "CanBeChanged": True,
            "ManagedPropertyLogicalName": "canmodifyrequirementlevelsettings",
        })
        self.assertTrue(primary["Description"]["LocalizedLabels"][0]["Label"])
        self.assertEqual(primary["FormatName"], {"Value": "Text"})
        for field in ("EntitySetName", "IsOptimisticConcurrencyEnabled"):
            self.assertNotIn(field, requests["table"])
        self.assertNotIn("IsSecured", primary)
        self.assertEqual(set(requests["columns"]), set(setup.SECURED))
        self.assertTrue(all(column["IsSecured"] for column in requests["columns"].values()))
        self.runner.schema()
        table_writes = [body for method, path, body, _ in self.api.writes if method == "POST" and path == "EntityDefinitions"]
        column_writes = [body for method, path, body, _ in self.api.writes if method == "POST" and path.endswith("/Attributes")]
        self.assertEqual(table_writes, [requests["table"]])
        self.assertEqual(column_writes, list(requests["columns"].values()))
        self.assertEqual(len(self.api.metadata["Attributes"]), 9)
        setup.check_schema(self.api.metadata)
        writes = len(self.api.writes)
        self.runner.schema()
        self.assertEqual(len(self.api.writes), writes)

    def test_partial_table_create_response_loss_recovers_without_recreating_table(self):
        request = self.api.request
        failed = False
        def lose_table_response(path, **kwargs):
            nonlocal failed
            result = request(path, **kwargs)
            if path == "EntityDefinitions" and kwargs.get("method") == "POST" and not failed:
                failed = True
                raise urllib.error.HTTPError(path, 503, "table response lost", {}, None)
            return result
        with patch.object(self.api, "request", side_effect=lose_table_response), self.assertRaises(urllib.error.HTTPError):
            self.runner.schema()
        self.assertEqual(len(self.api.metadata["Attributes"]), 2)
        self.assertIsNone(self.journal.last("done", "schema"))
        self.runner.schema()
        self.assertEqual(sum(method == "POST" and path == "EntityDefinitions" for method, path, _, _ in self.api.writes), 1)
        self.assertEqual(len(self.api.metadata["Attributes"]), 9)
        self.assertIsNotNone(self.journal.last("done", "schema"))

    def test_partial_column_response_loss_recovers_exact_column_without_duplicate(self):
        request = self.api.request
        failed = False
        def lose_column_response(path, **kwargs):
            nonlocal failed
            result = request(path, **kwargs)
            if path.endswith("/Attributes") and kwargs.get("body", {}).get("SchemaName") == "moacc_outcome" and not failed:
                failed = True
                raise urllib.error.HTTPError(path, 503, "column response lost", {}, None)
            return result
        with patch.object(self.api, "request", side_effect=lose_column_response), self.assertRaises(urllib.error.HTTPError):
            self.runner.schema()
        self.assertIsNotNone(self.journal.last("done", "schema_table"))
        self.assertIsNone(self.journal.last("done", "schema_column_moacc_outcome"))
        self.assertIsNone(self.journal.last("done", "schema"))
        writes = len(self.api.writes)
        with self.assertRaisesRegex(ValueError, "missing declared columns"):
            self.runner.access()
        with self.assertRaisesRegex(ValueError, "verified owned schema"):
            self.runner.publish()
        self.assertEqual(len(self.api.writes), writes)
        self.runner.schema()
        columns = [body["SchemaName"] for method, path, body, _ in self.api.writes if method == "POST" and path.endswith("/Attributes")]
        self.assertEqual(columns.count("moacc_outcome"), 1)
        self.assertEqual(set(columns), set(setup.SECURED))
        self.assertEqual(len(columns), 7)

    def test_metadata_parent_cache_lag_retries_only_reads_not_column_creation(self):
        request, get = self.api.request, self.api.get
        stale_reads = 0
        def created(path, **kwargs):
            nonlocal stale_reads
            result = request(path, **kwargs)
            if path.endswith("/Attributes") and kwargs.get("body", {}).get("SchemaName") == "moacc_action":
                stale_reads = 2
            return result
        def delayed(path, query=None):
            nonlocal stale_reads
            result = get(path, query)
            if path.startswith("EntityDefinitions") and stale_reads:
                stale_reads -= 1
                result["Attributes"] = [item for item in result["Attributes"] if item["LogicalName"] != "moacc_action"]
            return result
        with patch.object(self.api, "request", side_effect=created), patch.object(self.api, "get", side_effect=delayed), \
             patch.object(setup.time, "sleep") as sleep:
            self.runner.schema()
        self.assertEqual(sleep.call_count, 2)
        self.assertEqual(sum(path.endswith("/Attributes") and body.get("SchemaName") == "moacc_action"
                             for method, path, body, _ in self.api.writes if method == "POST"), 1)
        self.assertIsNotNone(self.journal.last("done", "schema"))

    def test_schema_readback_requests_documented_strong_metadata_consistency(self):
        with patch.object(self.api, "request", wraps=self.api.request) as request:
            self.assertIsNone(setup.read_schema(self.api))
        self.assertEqual(request.call_args.kwargs["headers"], {"Consistency": "Strong"})
        self.assertTrue(request.call_args.args[0].startswith("EntityDefinitions("))

    def test_definite_minimal_table_rejection_does_not_attempt_columns_or_permutations(self):
        request = self.api.request
        def reject_table(path, **kwargs):
            if path == "EntityDefinitions" and kwargs.get("method") == "POST":
                raise urllib.error.HTTPError(path, 400, "native validation failed", {}, None)
            return request(path, **kwargs)
        with patch.object(self.api, "request", side_effect=reject_table), self.assertRaises(urllib.error.HTTPError) as caught:
            self.runner.schema()
        self.assertEqual(caught.exception.code, 400)
        self.assertIsNone(self.api.metadata)
        self.assertIsNotNone(self.journal.last("rejected", "schema_table"))
        self.assertFalse(any(path.endswith("/Attributes") for _, path, _, _ in self.api.writes))
        self.assertIsNone(self.journal.last("done", "schema"))

    def test_definite_column_rejection_does_not_adopt_external_matching_column(self):
        request = self.api.request
        def reject_column(path, **kwargs):
            if path.endswith("/Attributes") and kwargs.get("body", {}).get("SchemaName") == "moacc_action":
                raise urllib.error.HTTPError(path, 400, "column failed", {}, None)
            return request(path, **kwargs)
        with patch.object(self.api, "request", side_effect=reject_column), self.assertRaises(urllib.error.HTTPError):
            self.runner.schema()
        key = "schema_column_moacc_action"
        self.assertIsNotNone(self.journal.last("rejected", key))
        body = self.plan["schema_requests"]["columns"]["moacc_action"]
        self.api.metadata["Attributes"].append({
            **copy.deepcopy(body), "LogicalName": "moacc_action", "MetadataId": uid("external-column"),
        })
        before = len(self.api.writes)
        with self.assertRaisesRegex(ValueError, "trusted request"):
            self.runner.schema()
        self.assertEqual(len(self.api.writes), before)
        self.assertIsNone(self.journal.last("done", "schema"))

    def failure(self, url, status, body=b"", headers=None):
        message = http.client.HTTPMessage()
        for name, value in (headers or {}).items():
            message[name] = value
        return urllib.error.HTTPError(url, status, "failure", message, io.BytesIO(body))

    def test_definite_second_column_404_keeps_evidence_and_the_same_stage_resumes(self):
        key = "Qq7" + secrets.token_urlsafe(48)
        body = json.dumps({"error": {"code": "0x80060888", "message": "Resource not found. X-API-Key: " + key}}).encode()
        ids = {"x-ms-service-request-id": "service-request-1", "REQ_ID": "req-1"}
        requests = self.plan["schema_requests"]
        first, second = list(requests["columns"])[:2]
        request, rejected = self.api.request, []
        def missing_second_column(path, **kwargs):
            if path.endswith("/Attributes") and kwargs["body"]["SchemaName"] == second and not rejected:
                rejected.append(path)
                raise self.failure(path, 404, body, ids)
            return request(path, **kwargs)
        with patch.object(self.api, "request", side_effect=missing_second_column), \
                self.assertRaises(urllib.error.HTTPError) as caught:
            self.runner.schema()
        error = caught.exception
        self.assertEqual(error.code, 404)
        for value in ("POST EntityDefinitions(", "/Attributes returned HTTP 404", "0x80060888", "service-request-1", "req-1"):
            self.assertIn(value, str(error))
        rejection = self.journal.last("rejected", "schema_column_" + second)
        self.assertEqual((rejection["status"], rejection["method"], rejection["error_code"]), (404, "POST", "0x80060888"))
        self.assertEqual((rejection["request_ids"], error.journal_event), (ids, rejection["sequence"]))
        self.assertIn("X-API-Key: [redacted]", rejection["body"])
        receipts = "".join(path.read_text() for path in Path(self.directory.name).glob("event-*.json"))
        self.assertNotIn(key, str(error) + receipts)
        primary = requests["table"]["Attributes"][0]["SchemaName"].lower()
        self.assertEqual({item["LogicalName"] for item in self.api.metadata["Attributes"]} - {setup.PRIMARY},
                         {primary, first})
        self.runner.schema()
        posts = [body["SchemaName"] for method, path, body, _ in self.api.writes
                 if method == "POST" and path.startswith("EntityDefinitions")]
        self.assertEqual(sorted(posts), sorted([requests["table"]["SchemaName"], *requests["columns"]]))
        setup.check_schema(self.api.metadata)
        self.assertIsNotNone(self.journal.last("done", "schema"))

    def test_uncertain_write_failure_keeps_evidence_and_its_trusted_intent(self):
        request = self.api.request
        def lost(path, **kwargs):
            result = request(path, **kwargs)
            if path == "roles":
                raise self.failure(path, 503, b'{"error":{"code":"0x80044151","message":"Busy"}}',
                                   {"REQ_ID": "req-2", "Retry-After": "5"})
            return result
        with patch.object(self.api, "request", side_effect=lost), self.assertRaises(urllib.error.HTTPError) as caught:
            self.runner.row("example", "roles", "roleid", {"name": "owned"})
        self.assertIn("req-2", str(caught.exception))
        failure = self.journal.last("request_failed", "example")
        self.assertEqual((failure["status"], failure["request_ids"], failure["retry_after"]), (503, {"REQ_ID": "req-2"}, "5"))
        self.assertIsNone(self.journal.last("rejected", "example"))
        self.assertIsNotNone(self.runner.trusted_intent("example"))
        writes = len(self.api.writes)
        self.runner.row("example", "roles", "roleid", {"name": "owned"})
        self.assertEqual(len(self.api.writes), writes)
        self.assertIsNotNone(self.journal.last("done", "example"))

    def test_failure_evidence_is_bounded_and_drops_keys_tokens_and_addresses(self):
        token = "Qq7" + secrets.token_urlsafe(48)
        jwt = ".".join(("eyJ" + "a" * 20, "b" * 20, "c" * 20))
        address = "@".join(("operator", "example.com"))
        text = (f"X-API-Key: {token}; Authorization: Bearer {jwt}; owner {address}; "
                f"{'access'}key={token}; bare {token}; keep 0x80040216 in EntityDefinitions. ")
        details = setup.http_failure(self.failure(
            "https://example.crm17.dynamics.com/api/data/v9.2/roles?$select=name", 400, (text * 400).encode(),
            {"X-MS-Service-Request-Id": "abc def", "Set-Cookie": "session=" + token},
        ))
        self.assertEqual((details["path"], details["request_ids"]), ("roles", {"x-ms-service-request-id": "abcdef"}))
        self.assertLessEqual(len(details["body"]), setup.FAILURE_BODY_CHARACTERS)
        self.assertTrue(details["body_truncated"])
        self.assertIn("0x80040216 in EntityDefinitions", details["body"])
        for secret in (token, jwt, address):
            self.assertNotIn(secret, json.dumps(details))
        prefix = "bare " + ("Ab1" * 21837)[:65510] + " "
        cut = setup.http_failure(self.failure("roles", 500, (prefix + token).encode()))
        self.assertEqual(cut["body"], "bare [redacted] ")
        self.assertTrue(cut["body_truncated"])

    def test_cli_reports_a_failed_stage_read_and_journals_it_under_the_stage(self):
        import contextlib
        token = "Qq7" + secrets.token_urlsafe(48)
        directory = Path(self.directory.name)
        setup.native.write_private(directory / "config.json", json.dumps(self.config))
        setup.native.write_private(directory / "plan.json", json.dumps(self.plan))
        ids = {"x-ms-service-request-id": "service-request-3", "REQ_ID": "req-3"}
        get = self.api.get
        def unavailable(path, query=None):
            if path.startswith("EntityDefinitions"):
                raise self.failure("https://example.crm17.dynamics.com/api/data/v9.2/" + path, 503, json.dumps({
                    "error": {"code": "0x80044151", "message": "Busy. Authorization: Bearer " + token},
                }).encode(), ids)
            return get(path, query)
        output = io.StringIO()
        with patch.object(setup.native, "Dataverse", return_value=self.api), \
                patch.object(setup.native, "private_path", side_effect=lambda value: Path(value)), \
                patch.object(setup.Setup, "assert_idle_disabled"), patch.object(self.api, "get", side_effect=unavailable), \
                contextlib.redirect_stdout(output), self.assertRaises(SystemExit) as stopped:
            setup.cli(["schema", "--config", str(directory / "config.json"), "--output-dir", str(directory),
                       "--apply", "--confirm-organization", self.config["organization_url"]])
        self.assertEqual(stopped.exception.code, 1)
        report = json.loads(output.getvalue().splitlines()[-1])
        self.assertEqual((report["status"], report["http_status"], report["error_code"]), ("failed", 503, "0x80044151"))
        self.assertEqual((report["request_ids"], report["path"]),
                         (ids, f"EntityDefinitions(LogicalName='{setup.ENTITY}')"))
        self.assertIn("retry once only with a concrete change", report["next"])
        self.assertNotIn(token, output.getvalue())
        failure = setup.Journal(directory, self.plan).last("request_failed", "schema")
        self.assertEqual((failure["request_ids"], failure["sequence"]), (ids, report["journal_event"]))
        self.assertIsNone(self.api.metadata)

    def test_native_table_defaults_must_be_verified_not_written_to_make_them_match(self):
        request = self.api.request
        def wrong_concurrency(path, **kwargs):
            result = request(path, **kwargs)
            if path == "EntityDefinitions" and kwargs.get("method") == "POST":
                self.api.metadata["IsOptimisticConcurrencyEnabled"] = False
            return result
        with patch.object(self.api, "request", side_effect=wrong_concurrency), self.assertRaisesRegex(ValueError, "optimistic concurrency"):
            self.runner.schema()
        self.assertFalse(any(method in {"PATCH", "PUT"} or path.endswith("/Attributes") for method, path, _, _ in self.api.writes))

    def test_native_entity_set_name_mismatch_stops_before_creating_columns(self):
        request = self.api.request
        def unexpected_name(path, **kwargs):
            result = request(path, **kwargs)
            if path == "EntityDefinitions" and kwargs.get("method") == "POST":
                self.api.metadata["EntitySetName"] = "unexpected_entity_set"
            return result
        with patch.object(self.api, "request", side_effect=unexpected_name), self.assertRaisesRegex(ValueError, "entity-set"):
            self.runner.schema()
        self.assertFalse(any(path.endswith("/Attributes") for _, path, _, _ in self.api.writes))

    def test_old_plan_without_two_step_requests_is_refused_before_any_native_write(self):
        del self.plan["schema_requests"]
        with self.assertRaisesRegex(ValueError, "fresh reviewed plan"):
            self.runner.schema()
        self.assertEqual(self.api.writes, [])

    def test_full_scoped_stage_is_idempotent_and_requires_native_bot_publication(self):
        self.stage()
        before = len(self.api.writes)
        self.stage()
        self.assertEqual(len(self.api.writes), before)
        with self.assertRaisesRegex(ValueError, "publish"):
            self.runner.verify()
        self.api.records[f"bots({uid('bot')})"]["publishedon"] = "2026-01-03T00:00:00Z"
        values = self.runner.verify()
        self.assertEqual(values["DYNAMICS_NATIVE_SUPERVISION_ENABLED"], "true")
        self.assertNotIn("API_KEY", str(values))
        self.assertFalse(any("PublishAll" in path or "PvaPublish" in path for _, path, _, _ in self.api.writes))
        publish = next(body["ParameterXml"] for _, path, body, _ in self.api.writes if path == "PublishXml")
        self.assertNotIn("<appmodule", publish)

    def test_requesters_cannot_create_or_update_outcomes_and_runtime_cannot_create_commands(self):
        self.runner.schema()
        self.runner.access()
        for _, row in self.api.records.items():
            if row.get("entityname") == setup.ENTITY:
                requester = row["_fieldsecurityprofileid_value"] == setup.owned(self.config, "profile_requester")
                request_field = row["attributelogicalname"] in setup.REQUESTS
                self.assertEqual(row["cancreate"], 4 if requester and request_field else 0)
                if requester or request_field:
                    self.assertEqual(row["canupdate"], 0)
        requester = self.api.privileges[setup.owned(self.config, "role_requester")]
        self.assertEqual(requester, {uid("prvCreate" + setup.ENTITY): "Basic", uid("prvRead" + setup.ENTITY): "Basic"})
        runtime = self.api.privileges[setup.owned(self.config, "role_runtime")]
        self.assertNotIn(uid("prvCreate" + setup.ENTITY), runtime)
        self.assertEqual(self.api.records[f"systemusers({uid('human')})"]["systemuserroles_association"][0]["name"], "Existing unchanged role")

    def native_generated_parameter(self):
        original = self.api.request
        def create(path, **kwargs):
            result = original(path, **kwargs)
            if path == "msdyn_applicationtabtemplates" and kwargs.get("method") == "POST":
                tab = self.api.records[f"msdyn_applicationtabtemplates({setup.owned(self.config, 'tab')})"]
                self.api.records[f"msdyn_templateparameters({uid('generated-parameter')})"] = {
                    "msdyn_templateparameterid": uid("generated-parameter"),
                    "msdyn_name": "webresourceName",
                    "msdyn_uniquename": tab["msdyn_uniquename"] + "_webresourceName",
                    "msdyn_value": None,
                    "_msdyn_applicationtabtemplateid_value": tab["msdyn_applicationtabtemplateid"],
                    "_createdby_value": tab["_createdby_value"], "createdon": tab["createdon"],
                    "@odata.etag": 'W/"1"',
                }
            return result
        return create

    def test_native_generated_parameter_is_updated_conditionally_not_duplicated(self):
        with patch.object(self.api, "request", side_effect=self.native_generated_parameter()):
            self.stage()
        intent = self.journal.last("intent", "tab_parameter")
        self.assertEqual(intent["operation"], "generated_parameter_update")
        self.assertIsNone(intent["before"]["msdyn_value"])
        self.assertFalse(any(path == "msdyn_templateparameters" for _, path, _, _ in self.api.writes))
        count = len(self.api.writes)
        self.runner.webresource()
        self.assertEqual(count, len(self.api.writes))
        self.api.records[f"bots({uid('bot')})"]["publishedon"] = "2026-01-03T00:00:00Z"
        self.runner.verify()
        with patch.object(self.runner, "assert_idle_disabled"):
            self.runner.rollback()
        self.assertIsNone(self.api.records[f"msdyn_templateparameters({uid('generated-parameter')})"]["msdyn_value"])

    def test_generated_parameter_with_foreign_provenance_or_existing_value_is_refused(self):
        original = self.native_generated_parameter()
        def changed(path, **kwargs):
            result = original(path, **kwargs)
            if path == "msdyn_applicationtabtemplates" and kwargs.get("method") == "POST":
                self.api.records[f"msdyn_templateparameters({uid('generated-parameter')})"]["msdyn_value"] = "foreign.html"
            return result
        with patch.object(self.api, "request", side_effect=changed), self.assertRaisesRegex(ValueError, "unchanged native child"):
            self.runner.webresource()
        self.assertIsNone(self.journal.last("done", "tab_parameter"))

    def test_admin_bypass_needs_explicit_trust_and_application_requesters_are_rejected(self):
        actor = self.api.records[f"systemusers({uid('human')})"]
        actor["systemuserroles_association"].append({"name": "System Administrator"})
        with self.assertRaisesRegex(ValueError, "Administrators"):
            setup.build_plan(self.api, self.config)
        trusted = {**self.config, "trust_system_administrators": True}
        self.assertEqual(setup.build_plan(self.api, trusted)["trusted_administrators"], [uid("human")])
        actor["applicationid"] = uid("app")
        with self.assertRaises(ValueError):
            setup.build_plan(self.api, trusted)

    def test_native_custom_privilege_names_preserve_schema_capitalization(self):
        self.runner.schema()
        original = self.api.rows
        def native_names(path, query):
            rows = original(path, query)
            if path == "privileges":
                return [{**row, "name": row["name"].replace(setup.ENTITY, "moacc_NativeAICommand")}
                        for row in rows]
            return rows
        with patch.object(self.api, "rows", side_effect=native_names):
            self.runner.access()
        self.assertIsNotNone(self.journal.last("done", "privileges_requester"))
        self.assertIsNotNone(self.journal.last("done", "privileges_runtime"))

    def test_only_native_seeded_defaults_are_removed_before_owned_role_assignment(self):
        self.runner.schema()
        original = self.api.request
        defaults = {uid(name): "Global" for name in setup.SEEDED_ROLE_DEFAULTS}
        self.api.privilege_names.update({uid(name): name for name in setup.SEEDED_ROLE_DEFAULTS})
        def seeded(path, **kwargs):
            result = original(path, **kwargs)
            if path == "roles" and kwargs.get("method") == "POST":
                self.api.privileges[kwargs["body"]["roleid"]] = dict(defaults)
            elif path.endswith("AddPrivilegesRole"):
                role = path.split("(", 1)[1].split(")", 1)[0]
                self.api.privileges[role].update(defaults)
            return result
        with patch.object(self.api, "request", side_effect=seeded):
            self.runner.access()
        self.assertEqual(len([event for event in self.journal.events if event["kind"] == "seed_default_removed"]),
                         len(defaults) * 4)
        before = len(self.api.writes)
        self.runner.access()
        self.assertEqual(len(self.api.writes), before)
        for audience in ("requester", "runtime"):
            expected = {uid(name): depth for name, depth in setup.role_privileges(audience).items()}
            self.assertEqual(self.api.privileges[setup.owned(self.config, "role_" + audience)], expected)

    def test_unreviewed_or_already_verified_role_privileges_are_never_removed(self):
        self.runner.schema()
        original = self.api.request
        self.api.privilege_names[uid("unapproved")] = "prvDeleteContact"
        def unrelated(path, **kwargs):
            result = original(path, **kwargs)
            if path == "roles" and kwargs.get("method") == "POST":
                self.api.privileges[kwargs["body"]["roleid"]] = {uid("unapproved"): "Global"}
            return result
        with patch.object(self.api, "request", side_effect=unrelated), self.assertRaisesRegex(ValueError, "unreviewed"):
            self.runner.access()
        self.assertFalse(any(path.endswith("RemovePrivilegeRole") for _, path, _, _ in self.api.writes))
        self.api.privileges[setup.owned(self.config, "role_requester")].clear()
        self.runner.access()
        name = "prvReadSharePointData"
        self.api.privilege_names[uid(name)] = name
        self.api.privileges[setup.owned(self.config, "role_requester")][uid(name)] = "Global"
        with self.assertRaisesRegex(ValueError, "drift"):
            self.runner.access()

    def test_seeded_defaults_are_not_removed_from_a_role_assigned_outside_the_receipt(self):
        self.runner.schema()
        original = self.api.request
        name = "prvReadSharePointData"
        self.api.privilege_names[uid(name)] = name
        def assigned(path, **kwargs):
            result = original(path, **kwargs)
            if path == "roles" and kwargs.get("method") == "POST":
                role = kwargs["body"]["roleid"]
                self.api.privileges[role] = {uid(name): "Global"}
                self.api.links[f"systemusers({uid('other-human')})/systemuserroles_association"] = {
                    role: {"roleid": role},
                }
            return result
        with patch.object(self.api, "request", side_effect=assigned), self.assertRaisesRegex(ValueError, "before.*assigned"):
            self.runner.access()
        self.assertFalse(any(path.endswith("RemovePrivilegeRole") for _, path, _, _ in self.api.writes))

    def legacy_runtime_role(self):
        original = setup.role_privileges
        def legacy(audience):
            return {key: value for key, value in original(audience).items()
                    if key not in {name for name, _ in setup.METADATA_READS}}
        with patch.object(setup, "role_privileges", side_effect=legacy):
            self.plan["role_privileges"] = {audience: legacy(audience) for audience in ("requester", "runtime")}
            self.runner.schema()
            self.runner.access()

    def test_explicit_metadata_upgrade_preserves_legacy_plan_and_exact_runtime_grants(self):
        self.legacy_runtime_role()
        original_plan = copy.deepcopy(self.plan)
        with self.assertRaisesRegex(ValueError, "explicit runtime-metadata"):
            self.runner.access()
        self.runner.upgrade_runtime_metadata()
        self.assertEqual(self.plan, original_plan)
        role = setup.owned(self.config, "role_runtime")
        self.assertEqual(self.api.privileges[role], {uid(name): depth for name, depth in setup.role_privileges("runtime").items()})
        before = len(self.api.writes)
        self.runner.upgrade_runtime_metadata()
        self.runner.access()
        self.assertEqual(len(self.api.writes), before)
        for stage in ("controller", "webresource", "bind", "publish"):
            getattr(self.runner, stage)()
        self.api.records[f"bots({uid('bot')})"]["publishedon"] = "2026-01-03T00:00:00Z"
        self.runner.verify()

    def test_metadata_upgrade_removes_only_new_known_platform_defaults(self):
        self.legacy_runtime_role()
        original = self.api.request
        default = "prvReadSharePointData"
        self.api.privilege_names[uid(default)] = default
        def seeded(path, **kwargs):
            result = original(path, **kwargs)
            if path.endswith("AddPrivilegesRole"):
                role = path.split("(", 1)[1].split(")", 1)[0]
                self.api.privileges[role][uid(default)] = "Global"
            return result
        with patch.object(self.api, "request", side_effect=seeded):
            self.runner.upgrade_runtime_metadata()
        self.assertNotIn(uid(default), self.api.privileges[setup.owned(self.config, "role_runtime")])
        self.assertIsNotNone(self.journal.last("done", "runtime_metadata_read"))
        self.assertIsNotNone(self.journal.last("done", "runtime_attribute_read"))

    def test_metadata_upgrade_refuses_existing_drift_and_foreign_role_members(self):
        self.legacy_runtime_role()
        role = setup.owned(self.config, "role_runtime")
        self.api.privileges[role][uid("foreign")] = "Global"
        before = len(self.api.writes)
        with self.assertRaisesRegex(ValueError, "unexpected runtime"):
            self.runner.upgrade_runtime_metadata()
        self.assertEqual(len(self.api.writes), before)
        del self.api.privileges[role][uid("foreign")]
        self.api.links[f"systemusers({uid('foreign-human')})/systemuserroles_association"] = {role: {"roleid": role}}
        with self.assertRaisesRegex(ValueError, "shared"):
            self.runner.upgrade_runtime_metadata()
        self.assertEqual(len(self.api.writes), before)

    def test_partial_entity_read_upgrade_resumes_only_the_remaining_metadata_reads(self):
        self.legacy_runtime_role()
        self.runner.upgrade_metadata_privilege("prvReadEntity", "runtime_metadata_read")
        before = len(self.api.writes)
        self.runner.upgrade_runtime_metadata()
        grants = [body for _, path, body, _ in self.api.writes[before:] if path.endswith("AddPrivilegesRole")]
        self.assertEqual(grants, [
            {"Privileges": [{"PrivilegeId": uid(name), "Depth": "Global"}]}
            for name in ("prvReadAttribute", "prvReadRelationship")
        ])
        self.assertEqual(self.runner.role_expectation(self.journal.last("done", "privileges_runtime")),
                         self.api.privileges[setup.owned(self.config, "role_runtime")])

    def test_ambiguous_owned_create_is_recovered_by_same_id_without_duplicate(self):
        self.api.fail_after_create = True
        with self.assertRaises(urllib.error.HTTPError):
            self.runner.row("example", "roles", "roleid", {"name": "owned"})
        writes = len(self.api.writes)
        self.runner.row("example", "roles", "roleid", {"name": "owned"})
        self.assertEqual(writes, len(self.api.writes))
        self.assertIsNotNone(self.journal.last("done", "example"))

    def test_definitely_rejected_create_cannot_adopt_a_later_external_matching_row(self):
        original = self.api.request
        def rejected(*args, **kwargs):
            original(*args, **kwargs)
            raise urllib.error.HTTPError(args[0], 409, "conflict", {}, None)
        with patch.object(self.api, "request", side_effect=rejected), self.assertRaises(urllib.error.HTTPError):
            self.runner.row("example", "roles", "roleid", {"name": "external matching data"})
        with self.assertRaisesRegex(ValueError, "predates"):
            self.runner.row("example", "roles", "roleid", {"name": "external matching data"})
        self.assertIsNone(self.journal.last("done", "example"))

    def test_owned_privilege_and_association_removals_are_not_silently_repaired(self):
        self.runner.schema()
        self.runner.access()
        role = setup.owned(self.config, "role_requester")
        self.api.privileges[role].clear()
        with self.assertRaisesRegex(ValueError, "regrant"):
            self.runner.access()

    def test_rollback_restores_only_owned_delta_and_refuses_command_history_or_drift(self):
        self.stage()
        original = self.plan["controllers"][uid("controller")]["before"]["data"]
        self.api.commands = [{setup.PRIMARY: uid("history")}]
        with patch.object(self.runner, "assert_idle_disabled"), self.assertRaisesRegex(ValueError, "history"):
            self.runner.rollback()
        self.api.commands = []
        with patch.object(self.runner, "assert_idle_disabled"):
            self.runner.rollback()
        self.assertEqual(self.api.records[f"botcomponents({uid('controller')})"]["data"], original)
        self.assertEqual(set(self.api.links[f"msdyn_sessiontemplates({uid('session')})/msdyn_sessiontemplate_applicationtab"]), {uid("existing_tab")})
        self.assertIsNone(self.api.metadata)
        with patch.object(self.runner, "assert_idle_disabled"):
            self.runner.rollback()

    def test_receipt_chain_and_unowned_objects_cannot_be_adopted(self):
        identifier = setup.owned(self.config, "role_requester")
        self.api.records[f"roles({identifier})"] = {"roleid": identifier, "name": "external"}
        with self.assertRaises(ValueError):
            self.runner.row("role_requester", "roles", "roleid", {"name": "external"})
        event = self.journal.append("test", "chain")
        path = Path(self.directory.name) / "event-0001.json"
        event["key"] = "tampered"
        path.write_text(json.dumps(event))
        with self.assertRaisesRegex(ValueError, "chain"):
            setup.Journal(Path(self.directory.name), self.plan)

    def test_default_and_non_apply_stage_do_not_write_native_state(self):
        import contextlib
        import io
        path = Path(self.directory.name) / "config.json"
        path.write_text(json.dumps(self.config))
        path.chmod(0o600)
        output = Path(self.directory.name) / "receipt"
        with patch.object(setup.native, "Dataverse", return_value=self.api), \
             patch.object(setup.native, "private_path", side_effect=lambda value: Path(value)), \
             contextlib.redirect_stdout(io.StringIO()):
            setup.main(["--config", str(path), "--output-dir", str(output)])
            setup.main(["controller", "--config", str(path), "--output-dir", str(output)])
            with self.assertRaisesRegex(ValueError, "confirm-organization"):
                setup.main(["controller", "--config", str(path), "--output-dir", str(output), "--apply"])
        self.assertEqual(self.api.writes, [])

    def test_verify_requires_reviewed_binding_and_rejects_outcome_schema_or_role_drift(self):
        self.stage()
        self.api.records[f"bots({uid('bot')})"]["publishedon"] = "2026-01-03T00:00:00Z"
        self.runner.config = {**self.config, "session_template_ids": []}
        with self.assertRaisesRegex(ValueError, "Bind"):
            self.runner.verify()
        self.runner.config = self.config
        self.api.privileges[setup.owned(self.config, "role_requester")][uid("extra-write")] = "Global"
        with self.assertRaisesRegex(ValueError, "privilege"):
            self.runner.verify()

    def test_notice_upgrade_changes_one_field_and_preserves_immutable_plan_and_history(self):
        self.notice_controller()
        path, key = f"botcomponents({uid('controller')})", "startup_notice_" + uid("controller")
        before = self.api.get(path)
        plan, history = copy.deepcopy(self.plan), copy.deepcopy(self.journal.events)
        files = {path: path.read_bytes() for path in Path(self.directory.name).glob("event-*.json")}
        writes = len(self.api.writes)
        self.upgrade_notice()
        after = self.api.get(path)
        old, new = topics.load_topic(before["data"]), topics.load_topic(after["data"])
        original = next(node for node in old["beginDialog"]["actions"] if node.get("id") == "native_start")
        revised = next(node for node in new["beginDialog"]["actions"] if node.get("id") == "native_start")
        message = revised["latencyMessageSettings"]["latencyMessage"]
        self.assertEqual(message["text"], message["speak"])
        self.assertIn(setup.NOTICE_SHA256, message["text"][0])
        message["text"] = original["latencyMessageSettings"]["latencyMessage"]["text"]
        self.assertEqual(new, old)
        self.assertEqual(self.plan, plan)
        self.assertEqual(self.journal.events[:len(history)], history)
        self.assertTrue(all(path.read_bytes() == value for path, value in files.items()))
        self.assertEqual(self.api.writes[writes:], [
            ("PATCH", path, {"data": after["data"]}, {"If-Match": before["@odata.etag"]}),
        ])
        intent, done = self.journal.last("intent", key), self.journal.last("done", key)
        for event in (intent, done):
            self.assertEqual(event["before_sha256"], setup.digest(before["data"]))
            self.assertEqual(event["after_sha256"], setup.digest(after["data"]))
            self.assertEqual(event["controller_receipt"], self.journal.last("done", "controller_" + uid("controller"))["hash"])
        self.assertEqual(intent["etag"], before["@odata.etag"])
        self.assertEqual(done["etag"], after["@odata.etag"])
        self.assertEqual(done["intent_hash"], intent["hash"])

    def test_notice_upgrade_and_controller_replay_are_idempotent_after_receipt_reload(self):
        self.notice_controller()
        self.upgrade_notice()
        self.journal = setup.Journal(Path(self.directory.name), self.plan)
        self.runner = setup.Setup(self.api, self.plan, self.journal)
        writes, events = len(self.api.writes), len(self.journal.events)
        self.upgrade_notice()
        self.runner.controller()
        self.assertEqual((len(self.api.writes), len(self.journal.events)), (writes, events))

    def test_playback_upgrade_changes_only_startup_message_url_and_latency(self):
        self.notice_controller()
        self.upgrade_notice()
        path = f"botcomponents({uid('controller')})"
        before = self.api.get(path)
        plan, history, writes = copy.deepcopy(self.plan), copy.deepcopy(self.journal.events), len(self.api.writes)
        self.upgrade_playback()
        after = self.api.get(path)
        old, revised = topics.load_topic(before["data"]), topics.load_topic(after["data"])
        old_nodes = {node["id"]: node for node in old["beginDialog"]["actions"]}
        new_nodes = {node["id"]: node for node in revised["beginDialog"]["actions"]}
        self.assertEqual(new_nodes["native_connecting_notice"]["activity"],
                         topics.startup_notice_message(self.config["voice_origin"]))
        self.assertEqual(new_nodes["native_start"]["url"], self.config["voice_origin"] + "/dynamics/native/sessions")
        self.assertEqual(new_nodes["native_start"]["latencyMessageSettings"],
                         {"kind": "LatencyMessageSettings", "allowLatencyMessage": False})
        new_nodes["native_connecting_notice"]["activity"] = old_nodes["native_connecting_notice"]["activity"]
        for field in ("url", "latencyMessageSettings"):
            new_nodes["native_start"][field] = old_nodes["native_start"][field]
        self.assertEqual(revised, old)
        self.assertEqual(self.plan, plan)
        self.assertEqual(self.journal.events[:len(history)], history)
        self.assertEqual(self.api.writes[writes:], [
            ("PATCH", path, {"data": after["data"]}, {"If-Match": before["@odata.etag"]}),
        ])
        self.assertEqual(self.runner.controller_expectation(uid("controller")), {"data": after["data"]})
        done = self.journal.last("done", "startup_playback_" + uid("controller"))
        self.assertEqual(done["controller_receipt"], self.journal.last("done", "startup_notice_" + uid("controller"))["hash"])
        self.assertIn("startup-playback", self.runner.runtime_env_filename())

    def test_playback_upgrade_accepts_the_original_notice_without_requiring_the_failed_intermediate(self):
        self.notice_controller()
        self.upgrade_playback()
        self.assertIsNone(self.journal.last("done", "startup_notice_" + uid("controller")))
        self.assertIsNotNone(self.journal.last("done", "startup_playback_" + uid("controller")))

    def test_playback_replay_preserves_history_and_does_not_repeat_a_write(self):
        self.notice_controller()
        self.upgrade_notice()
        self.upgrade_playback()
        self.journal = setup.Journal(Path(self.directory.name), self.plan)
        self.runner = setup.Setup(self.api, self.plan, self.journal)
        writes, events = len(self.api.writes), len(self.journal.events)
        self.upgrade_playback()
        self.upgrade_notice()
        self.runner.controller()
        self.assertEqual((len(self.api.writes), len(self.journal.events)), (writes, events))

    def test_fresh_native_playback_plan_needs_no_migration(self):
        source = topics.render_speech_turn_pilot(
            topics.load_topic(topics.TEMPLATE.read_text()), crm_case_context=True,
        ).replace("__VOICE_ORIGIN__", self.config["voice_origin"])
        document = topics.load_topic(source)
        start = next(node for node in document["beginDialog"]["actions"] if node["id"] == "native_start")
        start["body"]["content"] = start["body"]["content"].replace(
            "speech_turn_control: true", "speech_turn_control: true, play_startup_notice: true",
        )
        start["latencyMessageSettings"] = {"kind": "LatencyMessageSettings", "allowLatencyMessage": False}
        next(node for node in document["beginDialog"]["actions"] if node["id"] == "native_connecting_notice")["activity"] = topics.startup_notice_message(self.config["voice_origin"])
        self.api.records[f"botcomponents({uid('controller')})"]["data"] = setup.native.dump(document)
        self.plan = setup.build_plan(self.api, self.config)
        self.journal = setup.Journal(Path(self.directory.name), self.plan)
        self.runner = setup.Setup(self.api, self.plan, self.journal)
        self.upgrade_playback()
        self.assertEqual(self.api.writes, [])
        self.stage()
        writes, events = len(self.api.writes), len(self.journal.events)
        self.upgrade_playback()
        self.assertEqual((len(self.api.writes), len(self.journal.events)), (writes, events))
        self.assertEqual(self.runner.runtime_env_filename(), "supervision.runtime.env")

    def test_playback_refuses_unrecognized_endpoints_messages_and_latency_settings(self):
        self.notice_controller()
        source = self.api.get(f"botcomponents({uid('controller')})")["data"]
        edits = (
            ("native_start", "url", self.config["voice_origin"] + "/dynamics/native/sessions?wait_for_context=false"),
            ("native_start", "latencyMessageSettings", {"kind": "LatencyMessageSettings", "allowLatencyMessage": False}),
            ("native_connecting_notice", "activity", {"speak": ["unrelated announcement"]}),
        )
        for identifier, field, value in edits:
            with self.subTest(field=field):
                document = topics.load_topic(source)
                next(node for node in document["beginDialog"]["actions"] if node.get("id") == identifier)[field] = value
                with self.assertRaisesRegex(ValueError, "unrelated"):
                    setup.upgrade_playback_source(setup.native.dump(document), self.config["voice_origin"])
        with patch.object(topics, "NOTICE_AUDIO") as asset:
            asset.read_bytes.return_value = b"changed asset"
            with self.assertRaisesRegex(ValueError, "asset changed"):
                setup.upgrade_playback_source(source, self.config["voice_origin"])

    def test_playback_upgrade_keeps_idle_gate_and_refuses_unrecorded_matching_revision(self):
        self.notice_controller()
        with patch.object(self.runner, "assert_idle_disabled", side_effect=ValueError("not idle")), \
                self.assertRaisesRegex(ValueError, "not idle"):
            self.runner.upgrade_startup_playback()
        path = f"botcomponents({uid('controller')})"
        self.api.records[path]["data"] = setup.upgrade_playback_source(self.api.records[path]["data"], self.config["voice_origin"])
        writes = len(self.api.writes)
        with self.assertRaisesRegex(ValueError, "changed before"):
            self.upgrade_playback()
        self.assertEqual(len(self.api.writes), writes)

    def test_playback_lost_response_recovers_by_readback_without_replaying(self):
        self.notice_controller()
        self.upgrade_notice()
        path = f"botcomponents({uid('controller')})"
        request = self.api.request

        def lost(target, **kwargs):
            result = request(target, **kwargs)
            if target == path and kwargs.get("method") == "PATCH":
                raise urllib.error.HTTPError(target, 503, "ambiguous", {}, None)
            return result

        with patch.object(self.api, "request", side_effect=lost), self.assertRaises(urllib.error.HTTPError):
            self.upgrade_playback()
        writes = len(self.api.writes)
        self.journal = setup.Journal(Path(self.directory.name), self.plan)
        self.runner = setup.Setup(self.api, self.plan, self.journal)
        self.upgrade_playback()
        self.assertEqual(len(self.api.writes), writes)
        self.assertIsNotNone(self.journal.last("done", "startup_playback_" + uid("controller")))

    def test_playback_conditional_patch_preserves_concurrent_edits(self):
        self.notice_controller()
        path = f"botcomponents({uid('controller')})"

        def changed(target, **kwargs):
            self.api.records[path].update({"data": "foreign edit", "@odata.etag": 'W/"999"'})
            self.assertNotEqual(kwargs["headers"]["If-Match"], self.api.records[path]["@odata.etag"])
            raise urllib.error.HTTPError(target, 412, "changed", {}, None)

        with patch.object(self.api, "request", side_effect=changed), self.assertRaises(urllib.error.HTTPError):
            self.upgrade_playback()
        self.assertEqual(self.api.records[path]["data"], "foreign edit")
        self.assertIsNone(self.journal.last("done", "startup_playback_" + uid("controller")))

    def test_playback_runtime_pin_requires_publication_and_keeps_old_env_evidence(self):
        self.notice_controller()
        self.upgrade_notice()
        old_filename = self.runner.runtime_env_filename()
        old_content = setup.native.env_file(self.runner.verify())
        setup.native.write_private(Path(self.directory.name) / old_filename, old_content)
        self.upgrade_playback()
        self.api.records[f"botcomponents({uid('controller')})"]["modifiedon"] = "2026-01-04T00:00:00Z"
        with self.assertRaisesRegex(ValueError, "publish this exact bot"):
            self.runner.verify()
        self.api.records[f"bots({uid('bot')})"]["publishedon"] = "2026-01-05T00:00:00Z"
        pins = json.loads(self.runner.verify()["DYNAMICS_NATIVE_SUPERVISION_CONTROLLERS"])
        self.assertEqual(pins[0]["sha256"], setup.digest(self.api.records[f"botcomponents({uid('controller')})"]["data"]))
        self.assertNotEqual(self.runner.runtime_env_filename(), old_filename)
        self.assertEqual((Path(self.directory.name) / old_filename).read_text(), old_content)

    def test_playback_rollback_restores_both_revisions_before_the_original_controller(self):
        self.notice_controller()
        self.upgrade_notice()
        path = f"botcomponents({uid('controller')})"
        before_playback = self.api.records[path]["data"]
        self.upgrade_playback()
        writes = len(self.api.writes)
        with patch.object(self.runner, "assert_idle_disabled"):
            self.runner.rollback()
            self.runner.rollback()
        change = self.plan["controllers"][uid("controller")]
        restored = [body for method, target, body, _ in self.api.writes[writes:] if method == "PATCH" and target == path]
        self.assertEqual(restored, [{"data": before_playback}, {"data": change["after_data"]}, {"data": change["before"]["data"]}])

    def test_playback_cli_forwards_the_explicit_upgrade_stage(self):
        self.notice_controller()
        directory = Path(self.directory.name)
        setup.native.write_private(directory / "plan.json", json.dumps(self.plan))
        setup.native.write_private(directory / "config.json", json.dumps(self.config))
        args = ["upgrade-startup-playback", "--config", str(directory / "config.json"), "--output-dir", str(directory)]
        writes = len(self.api.writes)
        with patch.object(setup.native, "Dataverse", return_value=self.api), \
                patch.object(setup.native, "private_path", side_effect=lambda value: Path(value)), \
                patch.object(setup.Setup, "assert_idle_disabled"), patch("builtins.print"):
            setup.main(args)
            self.assertEqual(len(self.api.writes), writes)
            with self.assertRaisesRegex(ValueError, "confirm-organization"):
                setup.main([*args, "--apply"])
            setup.main([*args, "--apply", "--confirm-organization", self.config["organization_url"]])
        self.assertEqual(len(self.api.writes), writes + 1)

    def test_native_owner_revision_repairs_historical_quiet_failure_and_preserves_receipts(self):
        self.historical_quiet_controller()
        self.upgrade_notice()
        self.upgrade_playback()
        path = f"botcomponents({uid('controller')})"
        before, plan, history = self.api.get(path), copy.deepcopy(self.plan), copy.deepcopy(self.journal.events)
        writes = len(self.api.writes)
        self.upgrade_native_startup()
        after = self.api.get(path)
        document = topics.load_topic(after["data"])
        nodes = {node["id"]: node for node in document["beginDialog"]["actions"]}
        self.assertIn("native_startup_notice: true", nodes["native_start"]["body"]["content"])
        self.assertNotIn("play_startup_notice", nodes["native_start"]["body"]["content"])
        self.assertEqual(nodes["native_start"]["latencyMessageSettings"],
                         topics.native_startup_latency_settings(self.config["voice_origin"]))
        self.assertEqual(nodes["native_connecting_notice"]["activity"], {"speak": ['<break time="20ms" />']})
        self.assertIn("native_supervision_check_startup_failure", nodes)
        self.assertEqual(self.plan, plan)
        self.assertEqual(self.journal.events[:len(history)], history)
        self.assertEqual(self.api.writes[writes:], [
            ("PATCH", path, {"data": after["data"]}, {"If-Match": before["@odata.etag"]}),
        ])
        self.assertIn("native-startup", self.runner.runtime_env_filename())
        self.assertEqual(self.runner.controller_expectation(uid("controller")), {"data": after["data"]})
        self.journal = setup.Journal(Path(self.directory.name), self.plan)
        self.runner = setup.Setup(self.api, self.plan, self.journal)
        count = len(self.api.writes)
        self.upgrade_native_startup()
        self.runner.controller()
        self.assertEqual(len(self.api.writes), count)

    def test_native_owner_requires_the_compatible_backend_before_any_write(self):
        self.notice_controller()
        count = len(self.api.writes)
        for health in ({}, {"native_startup_notice_supported": True}, {"startup_failure_notice_supported": True}):
            with self.subTest(health=health), patch.object(self.runner, "assert_idle_disabled", return_value=health):
                with self.assertRaisesRegex(ValueError, "backend before"):
                    self.runner.upgrade_native_startup()
        self.assertEqual(len(self.api.writes), count)

    def test_native_owner_can_be_installed_on_a_fresh_plan_without_duplicate_fields(self):
        document = topics.load_topic(topics.render_speech_turn_pilot(
            topics.load_topic(topics.TEMPLATE.read_text()), crm_case_context=True,
        ))
        source = setup.native.dump(topics.native_startup_controller(
            document, "__VOICE_ORIGIN__",
        )).replace("__VOICE_ORIGIN__", self.config["voice_origin"])
        self.api.records[f"botcomponents({uid('controller')})"]["data"] = source
        self.plan = setup.build_plan(self.api, self.config)
        self.journal = setup.Journal(Path(self.directory.name), self.plan)
        self.runner = setup.Setup(self.api, self.plan, self.journal)
        self.upgrade_native_startup()
        self.assertEqual(self.api.writes, [])
        self.stage()
        count = len(self.api.writes)
        self.upgrade_native_startup()
        self.assertEqual(len(self.api.writes), count)
        for node in topics.load_topic(self.plan["controllers"][uid("controller")]["after_data"])["beginDialog"]["actions"]:
            if node.get("kind") == "SetVariable" and node.get("variable") in {"Topic.NativeResult", "Topic.NativeStopResult"}:
                self.assertEqual(node["value"].count("startup_failed_unpaused:"), 1)

    def test_native_owner_lost_patch_response_is_reconciled_without_replaying(self):
        self.historical_quiet_controller()
        self.upgrade_notice()
        self.upgrade_playback()
        path, request = f"botcomponents({uid('controller')})", self.api.request

        def lost(target, **kwargs):
            value = request(target, **kwargs)
            if target == path and kwargs.get("method") == "PATCH":
                raise urllib.error.HTTPError(target, 503, "ambiguous", {}, None)
            return value

        with patch.object(self.api, "request", side_effect=lost), self.assertRaises(urllib.error.HTTPError):
            self.upgrade_native_startup()
        count = len(self.api.writes)
        self.upgrade_native_startup()
        self.assertEqual(len(self.api.writes), count)
        self.assertIsNotNone(self.journal.last("done", "native_startup_" + uid("controller")))

    def test_narrow_native_owner_rollback_preserves_command_history_and_workspace(self):
        self.historical_quiet_controller()
        self.upgrade_notice()
        self.upgrade_playback()
        path = f"botcomponents({uid('controller')})"
        before = self.api.records[path]["data"]
        self.upgrade_native_startup()
        self.api.commands = [{setup.PRIMARY: uid("preserved-history")}]
        links, writes = copy.deepcopy(self.api.links), len(self.api.writes)
        with patch.object(self.runner, "assert_idle_disabled"):
            self.runner.rollback_native_startup()
            self.runner.rollback_native_startup()
        self.assertEqual(self.api.records[path]["data"], before)
        self.assertEqual(self.runner.controller_expectation(uid("controller")), {"data": before})
        self.assertEqual(self.api.links, links)
        self.assertEqual(len(self.api.commands), 1)
        self.assertEqual([(method, target) for method, target, _, _ in self.api.writes[writes:]], [("PATCH", path)])

    def test_native_owner_verify_requires_its_new_publication(self):
        self.historical_quiet_controller()
        self.upgrade_native_startup()
        self.api.records[f"botcomponents({uid('controller')})"]["modifiedon"] = "2026-01-04T00:00:00Z"
        with self.assertRaisesRegex(ValueError, "publish this exact bot"):
            self.runner.verify()
        self.api.records[f"bots({uid('bot')})"]["publishedon"] = "2026-01-05T00:00:00Z"
        pins = json.loads(self.runner.verify()["DYNAMICS_NATIVE_SUPERVISION_CONTROLLERS"])
        self.assertEqual(pins[0]["sha256"], setup.digest(self.api.records[f"botcomponents({uid('controller')})"]["data"]))

    def test_native_owner_cli_upgrade_and_narrow_restore_are_explicit(self):
        self.historical_quiet_controller()
        directory = Path(self.directory.name)
        setup.native.write_private(directory / "plan.json", json.dumps(self.plan))
        setup.native.write_private(directory / "config.json", json.dumps(self.config))
        common = ["--config", str(directory / "config.json"), "--output-dir", str(directory)]
        health = {"native_startup_notice_supported": True, "startup_failure_notice_supported": True}
        with patch.object(setup.native, "Dataverse", return_value=self.api), \
                patch.object(setup.native, "private_path", side_effect=lambda value: Path(value)), \
                patch.object(setup.Setup, "assert_idle_disabled", return_value=health), patch("builtins.print"):
            before = len(self.api.writes)
            setup.main(["upgrade-native-startup", *common])
            self.assertEqual(len(self.api.writes), before)
            with self.assertRaisesRegex(ValueError, "confirm-organization"):
                setup.main(["upgrade-native-startup", *common, "--apply"])
            setup.main(["upgrade-native-startup", *common, "--apply", "--confirm-organization", self.config["organization_url"]])
            setup.main(["rollback-native-startup", *common, "--apply", "--confirm-organization", self.config["organization_url"]])
        self.assertEqual(len(self.api.writes), before + 2)

    def test_application_owner_revision_changes_only_three_startup_fields_and_preserves_history(self):
        self.historical_quiet_controller()
        self.upgrade_notice()
        self.upgrade_playback()
        self.upgrade_native_startup()
        path = f"botcomponents({uid('controller')})"
        before, plan, history = self.api.get(path), copy.deepcopy(self.plan), copy.deepcopy(self.journal.events)
        writes = len(self.api.writes)
        self.upgrade_application_startup()
        after = self.api.get(path)
        document, original = topics.load_topic(after["data"]), topics.load_topic(before["data"])
        nodes = {node["id"]: node for node in document["beginDialog"]["actions"]}
        original_nodes = {node["id"]: node for node in original["beginDialog"]["actions"]}
        start = nodes["native_start"]
        self.assertIn("play_startup_notice: true", start["body"]["content"])
        self.assertNotIn("native_startup_notice", start["body"]["content"])
        self.assertEqual(start["latencyMessageSettings"], {"kind": "LatencyMessageSettings", "allowLatencyMessage": False})
        guard = next(node for node in nodes["native_check_start"]["conditions"] if node["id"] == "native_start_not_prepared")
        self.assertEqual(guard["condition"], topics.startup_prepared_condition("application"))
        start["body"] = original_nodes["native_start"]["body"]
        start["latencyMessageSettings"] = original_nodes["native_start"]["latencyMessageSettings"]
        guard["condition"] = topics.startup_prepared_condition("native")
        self.assertEqual(document, original)
        self.assertEqual(self.plan, plan)
        self.assertEqual(self.journal.events[:len(history)], history)
        self.assertEqual(self.api.writes[writes:], [
            ("PATCH", path, {"data": after["data"]}, {"If-Match": before["@odata.etag"]}),
        ])
        self.assertIn("application-startup", self.runner.runtime_env_filename())
        self.assertEqual(self.runner.controller_expectation(uid("controller")), {"data": after["data"]})
        done = self.journal.last("done", "application_startup_" + uid("controller"))
        self.assertEqual(done["controller_receipt"], self.journal.last("done", "native_startup_" + uid("controller"))["hash"])
        self.journal = setup.Journal(Path(self.directory.name), self.plan)
        self.runner = setup.Setup(self.api, self.plan, self.journal)
        count = len(self.api.writes)
        self.upgrade_application_startup()
        self.runner.controller()
        self.assertEqual(len(self.api.writes), count)

    def test_application_owner_requires_compatible_backend_before_any_write(self):
        self.historical_quiet_controller()
        self.upgrade_native_startup()
        count = len(self.api.writes)
        for health in ({}, {"application_startup_preparation_supported": True}, {"startup_failure_notice_supported": True}):
            with self.subTest(health=health), patch.object(self.runner, "assert_idle_disabled", return_value=health):
                with self.assertRaisesRegex(ValueError, "backend before"):
                    self.runner.upgrade_application_startup()
        self.assertEqual(len(self.api.writes), count)

    def test_fresh_application_owner_plan_does_not_need_an_upgrade_revision(self):
        self.api.records[f"botcomponents({uid('controller')})"]["data"] = topics.render_speech_turn_pilot(
            topics.load_topic(topics.TEMPLATE.read_text()), startup_notice=True, crm_case_context=True,
        ).replace("__VOICE_ORIGIN__", self.config["voice_origin"])
        self.plan = setup.build_plan(self.api, self.config)
        self.journal = setup.Journal(Path(self.directory.name), self.plan)
        self.runner = setup.Setup(self.api, self.plan, self.journal)
        self.upgrade_application_startup()
        self.assertEqual(self.api.writes, [])
        self.stage()
        count = len(self.api.writes)
        self.upgrade_application_startup()
        self.assertEqual(len(self.api.writes), count)
        self.assertIsNone(self.journal.last("done", "application_startup_" + uid("controller")))

    def test_application_owner_lost_patch_response_is_reconciled_without_replaying(self):
        self.historical_quiet_controller()
        self.upgrade_native_startup()
        path, request = f"botcomponents({uid('controller')})", self.api.request

        def lost(target, **kwargs):
            value = request(target, **kwargs)
            if target == path and kwargs.get("method") == "PATCH":
                raise urllib.error.HTTPError(target, 503, "ambiguous", {}, None)
            return value

        with patch.object(self.api, "request", side_effect=lost), self.assertRaises(urllib.error.HTTPError):
            self.upgrade_application_startup()
        count = len(self.api.writes)
        self.upgrade_application_startup()
        self.assertEqual(len(self.api.writes), count)
        self.assertIsNotNone(self.journal.last("done", "application_startup_" + uid("controller")))

    def test_narrow_application_rollback_preserves_history_and_requires_latest_revision_first(self):
        self.historical_quiet_controller()
        self.upgrade_native_startup()
        path = f"botcomponents({uid('controller')})"
        before = self.api.records[path]["data"]
        self.upgrade_application_startup()
        self.api.commands = [{setup.PRIMARY: uid("preserved-history")}]
        links, writes = copy.deepcopy(self.api.links), len(self.api.writes)
        with patch.object(self.runner, "assert_idle_disabled"):
            with self.assertRaisesRegex(ValueError, "newer application-startup"):
                self.runner.rollback_native_startup()
            self.assertEqual(len(self.api.writes), writes)
            self.runner.rollback_application_startup()
            self.runner.rollback_application_startup()
        self.assertEqual(self.api.records[path]["data"], before)
        self.assertEqual(self.runner.controller_expectation(uid("controller")), {"data": before})
        self.assertIn("native-startup", self.runner.runtime_env_filename())
        self.assertEqual(self.api.links, links)
        self.assertEqual(len(self.api.commands), 1)
        self.assertEqual([(method, target) for method, target, _, _ in self.api.writes[writes:]], [("PATCH", path)])

    def test_full_rollback_restores_application_then_native_revision(self):
        self.historical_quiet_controller()
        self.upgrade_native_startup()
        path = f"botcomponents({uid('controller')})"
        native_source = self.api.records[path]["data"]
        self.upgrade_application_startup()
        writes = len(self.api.writes)
        with patch.object(self.runner, "assert_idle_disabled"):
            self.runner.rollback()
        bodies = [body for method, target, body, _ in self.api.writes[writes:] if method == "PATCH" and target == path]
        self.assertEqual(bodies[0], {"data": native_source})
        self.assertEqual(bodies[-1], {"data": self.plan["controllers"][uid("controller")]["before"]["data"]})
        self.assertLess(self.journal.last("rolled_back", "application_startup_" + uid("controller"))["sequence"],
                        self.journal.last("rolled_back", "native_startup_" + uid("controller"))["sequence"])

    def test_application_owner_rollback_refuses_foreign_edits(self):
        self.historical_quiet_controller()
        self.upgrade_native_startup()
        self.upgrade_application_startup()
        self.api.records[f"botcomponents({uid('controller')})"]["data"] += "\n# foreign edit"
        count = len(self.api.writes)
        with patch.object(self.runner, "assert_idle_disabled"), self.assertRaisesRegex(ValueError, "drift"):
            self.runner.rollback_application_startup()
        self.assertEqual(len(self.api.writes), count)

    def test_application_owner_cli_requires_apply_and_can_verify_after_narrow_rollback(self):
        self.historical_quiet_controller()
        self.upgrade_native_startup()
        directory = Path(self.directory.name)
        setup.native.write_private(directory / "plan.json", json.dumps(self.plan))
        setup.native.write_private(directory / "config.json", json.dumps(self.config))
        common = ["--config", str(directory / "config.json"), "--output-dir", str(directory)]
        health = {"application_startup_preparation_supported": True, "startup_failure_notice_supported": True}
        with patch.object(setup.native, "Dataverse", return_value=self.api), \
                patch.object(setup.native, "private_path", side_effect=lambda value: Path(value)), \
                patch.object(setup.Setup, "assert_idle_disabled", return_value=health), patch("builtins.print"):
            before = len(self.api.writes)
            setup.main(["upgrade-application-startup", *common])
            self.assertEqual(len(self.api.writes), before)
            with self.assertRaisesRegex(ValueError, "confirm-organization"):
                setup.main(["upgrade-application-startup", *common, "--apply"])
            setup.main(["upgrade-application-startup", *common, "--apply", "--confirm-organization", self.config["organization_url"]])
            for _ in range(2):
                setup.main(["rollback-application-startup", *common, "--apply", "--confirm-organization", self.config["organization_url"]])
            setup.main(["verify", *common])
        self.assertEqual(len(self.api.writes), before + 2)

    def test_fresh_audio_correct_controller_needs_no_upgrade_revision(self):
        self.notice_controller(legacy=False)
        writes, events = len(self.api.writes), len(self.journal.events)
        self.upgrade_notice()
        self.assertEqual((len(self.api.writes), len(self.journal.events)), (writes, events))
        self.assertEqual(self.runner.runtime_env_filename(), "supervision.runtime.env")

    def test_fresh_unapplied_audio_correct_plan_is_a_noop_not_an_adoption(self):
        self.notice_controller(legacy=False, applied=False)
        self.upgrade_notice()
        self.assertEqual(self.api.writes, [])
        self.assertEqual(self.journal.events, [])
        self.api.records[f"botcomponents({uid('controller')})"]["data"] = self.plan["controllers"][uid("controller")]["after_data"]
        with self.assertRaisesRegex(ValueError, "fresh plan"):
            self.upgrade_notice()
        self.assertEqual(self.journal.events, [])

    def test_notice_upgrade_requires_applied_owned_controller_and_idle_feature_off(self):
        self.notice_controller(applied=False)
        with patch.object(self.runner, "assert_idle_disabled"), self.assertRaisesRegex(ValueError, "completed owned"):
            self.runner.upgrade_startup_notice()
        self.assertEqual(self.api.writes, [])
        self.stage()
        writes, events = len(self.api.writes), len(self.journal.events)
        with patch.object(self.runner, "assert_idle_disabled", side_effect=ValueError("not idle/off")), self.assertRaisesRegex(ValueError, "not idle/off"):
            self.runner.upgrade_startup_notice()
        self.assertEqual((len(self.api.writes), len(self.journal.events)), (writes, events))

    def test_notice_upgrade_refuses_source_or_owner_drift_and_unrecorded_matching_upgrade(self):
        self.notice_controller()
        path = f"botcomponents({uid('controller')})"
        baseline = copy.deepcopy(self.api.records[path])
        writes, events = len(self.api.writes), len(self.journal.events)
        for changes in (
            {"data": baseline["data"] + "\n# unrelated edit"},
            {"data": setup.upgrade_notice_source(baseline["data"], self.config["voice_origin"])},
            {"_parentbotid_value": uid("foreign-bot")},
            {"componenttype": 12},
        ):
            with self.subTest(changes=list(changes)):
                self.api.records[path] = {**baseline, **changes}
                with patch.object(self.runner, "assert_idle_disabled"), self.assertRaises(ValueError):
                    self.runner.upgrade_startup_notice()
                self.assertEqual((len(self.api.writes), len(self.journal.events)), (writes, events))

    def test_notice_migration_is_not_an_arbitrary_text_speech_or_asset_upgrader(self):
        self.notice_controller()
        source = self.plan["controllers"][uid("controller")]["after_data"]
        for field, value in (
            ("text", ["unreviewed notice"]), ("speak", ['<audio src="https://elsewhere.invalid/notice.wav" />']),
            ("latencyMessageDelayInMilliseconds", 200), ("url", self.config["voice_origin"] + "/dynamics/native/sessions"),
        ):
            document = topics.load_topic(source)
            node = next(node for node in document["beginDialog"]["actions"] if node.get("id") == "native_start")
            if field in {"text", "speak"}:
                node["latencyMessageSettings"]["latencyMessage"][field] = value
            elif field == "url":
                node[field] = value
            else:
                node["latencyMessageSettings"][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                setup.upgrade_notice_source(setup.native.dump(document), self.config["voice_origin"])
        with patch.object(topics, "NOTICE_AUDIO") as asset:
            asset.read_bytes.return_value = b"unreviewed replacement"
            with self.assertRaisesRegex(ValueError, "asset changed"):
                setup.upgrade_notice_source(source, self.config["voice_origin"])

    def test_notice_lost_patch_response_is_recovered_without_a_second_native_write(self):
        self.notice_controller()
        key, path = "startup_notice_" + uid("controller"), f"botcomponents({uid('controller')})"
        request = self.api.request

        def lost(target, **kwargs):
            result = request(target, **kwargs)
            if target == path and kwargs.get("method") == "PATCH":
                raise urllib.error.HTTPError(target, 503, "ambiguous", {}, None)
            return result

        with patch.object(self.api, "request", side_effect=lost), self.assertRaises(urllib.error.HTTPError):
            self.upgrade_notice()
        self.assertIsNone(self.journal.last("done", key))
        with self.assertRaises(ValueError):
            self.runner.verify()
        with patch.object(self.runner, "assert_idle_disabled"), self.assertRaisesRegex(ValueError, "interrupted"):
            self.runner.rollback()
        writes = len(self.api.writes)
        self.journal = setup.Journal(Path(self.directory.name), self.plan)
        self.runner = setup.Setup(self.api, self.plan, self.journal)
        self.upgrade_notice()
        self.assertEqual(len(self.api.writes), writes)
        self.assertIsNotNone(self.journal.last("done", key))

    def test_notice_rejected_patch_cannot_adopt_a_later_matching_external_change(self):
        self.notice_controller()
        key, path = "startup_notice_" + uid("controller"), f"botcomponents({uid('controller')})"
        with patch.object(self.api, "request", side_effect=urllib.error.HTTPError(path, 412, "rejected", {}, None)), self.assertRaises(urllib.error.HTTPError):
            self.upgrade_notice()
        self.api.records[path]["data"] = setup.upgrade_notice_source(self.api.records[path]["data"], self.config["voice_origin"])
        writes = len(self.api.writes)
        with self.assertRaisesRegex(ValueError, "changed before"):
            self.upgrade_notice()
        self.assertEqual(len(self.api.writes), writes)
        self.assertIsNone(self.journal.last("done", key))

    def test_notice_pending_intent_cannot_rebase_to_a_changed_etag(self):
        self.notice_controller()
        path = f"botcomponents({uid('controller')})"
        with patch.object(self.api, "request", side_effect=urllib.error.HTTPError(path, 503, "not applied", {}, None)), self.assertRaises(urllib.error.HTTPError):
            self.upgrade_notice()
        self.api.records[path]["@odata.etag"] = 'W/"900"'
        writes = len(self.api.writes)
        with self.assertRaisesRegex(ValueError, "ETag changed"):
            self.upgrade_notice()
        self.assertEqual(len(self.api.writes), writes)

    def test_notice_conditional_patch_preserves_an_edit_after_preflight(self):
        self.notice_controller()
        path = f"botcomponents({uid('controller')})"

        def intervene(target, **kwargs):
            self.assertEqual(target, path)
            self.assertEqual(kwargs["method"], "PATCH")
            self.api.records[path].update({"data": "foreign edit", "@odata.etag": 'W/"900"'})
            self.assertNotEqual(kwargs["headers"]["If-Match"], self.api.records[path]["@odata.etag"])
            raise urllib.error.HTTPError(target, 412, "changed", {}, None)

        with patch.object(self.api, "request", side_effect=intervene), self.assertRaises(urllib.error.HTTPError):
            self.upgrade_notice()
        self.assertEqual(self.api.records[path]["data"], "foreign edit")
        self.assertIsNone(self.journal.last("done", "startup_notice_" + uid("controller")))

    def test_notice_upgrade_preflights_every_owned_controller_before_first_write(self):
        self.notice_controller(applied=False)
        other = uid("second-controller")
        self.config["controller_component_ids"].append(other)
        self.api.records[f"botcomponents({other})"] = {
            **self.api.get(f"botcomponents({uid('controller')})"), "botcomponentid": other,
        }
        self.plan = setup.build_plan(self.api, self.config)
        self.journal = setup.Journal(Path(self.directory.name), self.plan)
        self.runner = setup.Setup(self.api, self.plan, self.journal)
        self.stage()
        self.api.records[f"botcomponents({other})"]["data"] += "\n# foreign second-controller edit"
        writes, events = len(self.api.writes), len(self.journal.events)
        with self.assertRaisesRegex(ValueError, "changed before"):
            self.upgrade_notice()
        self.assertEqual((len(self.api.writes), len(self.journal.events)), (writes, events))

    def test_notice_verify_pins_revision_only_after_native_publication_and_keeps_old_env(self):
        self.notice_controller()
        directory = Path(self.directory.name)
        original_env = setup.native.env_file(self.runner.verify())
        setup.native.write_private(directory / "supervision.runtime.env", original_env)
        self.upgrade_notice()
        self.api.records[f"botcomponents({uid('controller')})"]["modifiedon"] = "2026-01-04T00:00:00Z"
        with self.assertRaisesRegex(ValueError, "publish this exact bot"):
            self.runner.verify()
        self.api.records[f"bots({uid('bot')})"]["publishedon"] = "2026-01-05T00:00:00Z"
        pin = json.loads(self.runner.verify()["DYNAMICS_NATIVE_SUPERVISION_CONTROLLERS"])
        expected = setup.digest(self.api.records[f"botcomponents({uid('controller')})"]["data"])
        self.assertEqual(pin, [{"id": uid("controller"), "sha256": expected}])
        self.assertNotEqual(expected, self.plan["controllers"][uid("controller")]["after_sha256"])
        setup.native.write_private(directory / "plan.json", json.dumps(self.plan))
        setup.native.write_private(directory / "config.json", json.dumps(self.config))
        with patch.object(setup.native, "Dataverse", return_value=self.api), \
             patch.object(setup.native, "private_path", side_effect=lambda value: Path(value)), patch("builtins.print"):
            for _ in range(2):
                setup.main(["verify", "--config", str(directory / "config.json"), "--output-dir", str(directory)])
        self.assertEqual((directory / "supervision.runtime.env").read_text(), original_env)
        self.assertNotEqual(self.runner.runtime_env_filename(), "supervision.runtime.env")
        self.assertEqual((directory / self.runner.runtime_env_filename()).read_text(), setup.native.env_file(self.runner.verify()))

    def test_notice_cli_is_explicit_apply_only(self):
        self.notice_controller()
        directory = Path(self.directory.name)
        setup.native.write_private(directory / "plan.json", json.dumps(self.plan))
        setup.native.write_private(directory / "config.json", json.dumps(self.config))
        args = ["upgrade-startup-notice", "--config", str(directory / "config.json"), "--output-dir", str(directory)]
        writes = len(self.api.writes)
        with patch.object(setup.native, "Dataverse", return_value=self.api), \
             patch.object(setup.native, "private_path", side_effect=lambda value: Path(value)), \
             patch.object(setup.Setup, "assert_idle_disabled"), patch("builtins.print"):
            setup.main(args)
            self.assertEqual(len(self.api.writes), writes)
            with self.assertRaisesRegex(ValueError, "confirm-organization"):
                setup.main([*args, "--apply"])
            setup.main([*args, "--apply", "--confirm-organization", self.config["organization_url"]])
        self.assertEqual(len(self.api.writes), writes + 1)

    def test_notice_rollback_restores_revision_then_original_supervision_controller(self):
        self.notice_controller()
        self.upgrade_notice()
        writes = len(self.api.writes)
        with patch.object(self.runner, "assert_idle_disabled"):
            self.runner.rollback()
            self.runner.rollback()
        path = f"botcomponents({uid('controller')})"
        bodies = [body for method, target, body, _ in self.api.writes[writes:] if method == "PATCH" and target == path]
        change = self.plan["controllers"][uid("controller")]
        self.assertEqual(bodies, [{"data": change["after_data"]}, {"data": change["before"]["data"]}])
        self.assertLess(self.journal.last("rolled_back", "startup_notice_" + uid("controller"))["sequence"],
                        self.journal.last("rolled_back", "controller_" + uid("controller"))["sequence"])

    def test_notice_rollback_refuses_foreign_edits_before_any_restore(self):
        self.notice_controller()
        self.upgrade_notice()
        path = f"botcomponents({uid('controller')})"
        self.api.records[path]["data"] += "\n# foreign edit"
        writes = len(self.api.writes)
        with patch.object(self.runner, "assert_idle_disabled"), self.assertRaisesRegex(ValueError, "owned data changed"):
            self.runner.rollback()
        self.assertEqual(len(self.api.writes), writes)

    def test_notice_rollback_preserves_an_edit_after_preflight(self):
        self.notice_controller()
        self.upgrade_notice()
        key, path = "startup_notice_" + uid("controller"), f"botcomponents({uid('controller')})"
        append, writes = self.journal.append, len(self.api.writes)

        def intervene(kind, target, **values):
            event = append(kind, target, **values)
            if kind == "rollback_intent" and target == key:
                self.api.records[path].update({"data": "foreign edit", "@odata.etag": 'W/"900"'})
            return event

        with patch.object(self.runner, "assert_idle_disabled"), patch.object(self.journal, "append", side_effect=intervene), \
             self.assertRaisesRegex(ValueError, "changed before restore"):
            self.runner.rollback()
        self.assertEqual(self.api.records[path]["data"], "foreign edit")
        self.assertEqual(len(self.api.writes), writes)
        self.assertIsNone(self.journal.last("rolled_back", key))

    def test_workspace_composition_requires_explicit_new_checkpoint_for_notice_revision(self):
        import setup_dynamics_workspace as workspace

        self.notice_controller()
        directory = Path(self.directory.name)
        setup.native.write_private(directory / "plan.json", json.dumps(self.plan))
        self.api.tenant = self.config["tenant_id"]
        with patch.object(workspace, "private_path", side_effect=lambda value: Path(value)):
            previous = workspace.supervision_receipt(directory, self.api)[0]
            self.upgrade_notice()
            self.api.records[f"bots({uid('bot')})"]["publishedon"] = "2026-01-05T00:00:00Z"
            with self.assertRaisesRegex(workspace.GateError, "predates"):
                workspace.supervision_receipt(directory, self.api, pinned=previous["checkpoint"])
            current = workspace.supervision_receipt(directory, self.api)[0]
            self.assertGreater(current["checkpoint"]["sequence"], previous["checkpoint"]["sequence"])
            self.assertEqual(current["plan_sha256"], previous["plan_sha256"])
            self.api.records[f"webresourceset({setup.owned(self.config, 'resource_native-supervision.js')})"]["description"] = "foreign"
            with self.assertRaisesRegex(ValueError, "configuration drifted"):
                workspace.supervision_receipt(directory, self.api)

    def assert_notice_rollback_recovers_lost_response(self, failure_number):
        self.notice_controller()
        self.upgrade_notice()
        path = f"botcomponents({uid('controller')})"
        request, writes, calls = self.api.request, len(self.api.writes), 0

        def lost(target, **kwargs):
            nonlocal calls
            result = request(target, **kwargs)
            if target == path and kwargs.get("method") == "PATCH":
                calls += 1
                if calls == failure_number:
                    raise urllib.error.HTTPError(target, 503, "lost restore", {}, None)
            return result

        with patch.object(self.runner, "assert_idle_disabled"), patch.object(self.api, "request", side_effect=lost):
            with self.assertRaises(urllib.error.HTTPError):
                self.runner.rollback()
            self.runner.rollback()
        self.assertEqual(sum(method == "PATCH" and target == path for method, target, _, _ in self.api.writes[writes:]), 2)
        self.assertEqual(self.api.records[path]["data"], self.plan["controllers"][uid("controller")]["before"]["data"])

    def test_notice_rollback_resumes_after_revision_restore_loses_response(self):
        self.assert_notice_rollback_recovers_lost_response(1)

    def test_notice_rollback_resumes_after_original_controller_restore_loses_response(self):
        self.assert_notice_rollback_recovers_lost_response(2)

    def test_controller_unknown_tail_is_not_deleted_by_renderer(self):
        source = topics.load_topic(topics.render_speech_turn_pilot(topics.load_topic(topics.TEMPLATE.read_text())))
        source["beginDialog"]["actions"].append({"id": "unrelated_owned_action", "kind": "EndDialog"})
        with self.assertRaisesRegex(ValueError, "Unrecognized"):
            topics.add_supervision(source)

    def test_metadata_constraint_drift_blocks_owned_rollback(self):
        self.runner.schema()
        self.api.metadata["Attributes"][1]["MaxLength"] = 20
        with patch.object(self.runner, "assert_idle_disabled"), self.assertRaisesRegex(ValueError, "metadata changed"):
            self.runner.rollback()

    def test_lost_controller_restore_response_recovers_without_overwriting_other_data(self):
        self.stage()
        original = self.api.request
        failed = False
        def lose_restore(path, **kwargs):
            nonlocal failed
            result = original(path, **kwargs)
            if path.startswith("botcomponents(") and kwargs.get("method") == "PATCH" and not failed:
                failed = True
                raise urllib.error.HTTPError(path, 503, "lost restore response", {}, None)
            return result
        with patch.object(self.runner, "assert_idle_disabled"), patch.object(self.api, "request", side_effect=lose_restore):
            with self.assertRaises(urllib.error.HTTPError):
                self.runner.rollback()
            self.runner.rollback()
        self.assertIsNone(self.api.metadata)
        self.assertEqual(self.api.records[f"botcomponents({uid('controller')})"]["data"],
                         self.plan["controllers"][uid("controller")]["before"]["data"])

    def assert_rollback_final_read_preserves_edit(self, key, field):
        self.stage()
        path = self.journal.last("done", key)["path"]
        append = self.journal.append
        start = len(self.api.writes)
        edited = False

        def edit_after_preflight(kind, target, **values):
            nonlocal edited
            event = append(kind, target, **values)
            if kind == "rollback_intent" and target == key:
                self.api.records[path].update({field: "intervening reviewed edit", "@odata.etag": 'W/"900"'})
                edited = True
            return event

        with patch.object(self.runner, "assert_idle_disabled"), \
             patch.object(self.journal, "append", side_effect=edit_after_preflight), \
             self.assertRaisesRegex(ValueError, "changed before"):
            self.runner.rollback()
        self.assertTrue(edited)
        self.assertEqual(self.api.records[path][field], "intervening reviewed edit")
        self.assertFalse(any(method in {"PATCH", "DELETE"} and target == path
                             for method, target, _, _ in self.api.writes[start:]))
        self.assertIsNone(self.journal.last("rolled_back", key))

    def test_controller_edit_after_preflight_is_not_restored_with_its_fresh_etag(self):
        self.assert_rollback_final_read_preserves_edit("controller_" + uid("controller"), "data")

    def test_deletion_target_edit_after_preflight_is_not_deleted_with_its_fresh_etag(self):
        self.assert_rollback_final_read_preserves_edit("resource_native-supervision.js", "description")

    def assert_rollback_conditional_write_preserves_later_edit(self, key, field, method):
        self.stage()
        path = self.journal.last("done", key)["path"]
        request = self.api.request
        conditional_attempts = []

        def edit_after_final_read(target, **kwargs):
            if target == path and kwargs.get("method") == method:
                validated_etag = self.api.records[path]["@odata.etag"]
                self.api.records[path].update({field: "edit after final validation", "@odata.etag": 'W/"901"'})
                conditional_attempts.append(kwargs["headers"]["If-Match"])
                self.assertEqual(conditional_attempts[-1], validated_etag)
                raise urllib.error.HTTPError(target, 412, "ETag changed", {}, None)
            return request(target, **kwargs)

        with patch.object(self.runner, "assert_idle_disabled"), \
             patch.object(self.api, "request", side_effect=edit_after_final_read), \
             self.assertRaises(urllib.error.HTTPError) as caught:
            self.runner.rollback()
        self.assertEqual(caught.exception.code, 412)
        self.assertEqual(len(conditional_attempts), 1)
        self.assertEqual(self.api.records[path][field], "edit after final validation")
        self.assertIsNone(self.journal.last("rolled_back", key))

    def test_controller_edit_after_final_read_is_protected_by_validated_etag(self):
        self.assert_rollback_conditional_write_preserves_later_edit("controller_" + uid("controller"), "data", "PATCH")

    def test_deletion_target_edit_after_final_read_is_protected_by_validated_etag(self):
        self.assert_rollback_conditional_write_preserves_later_edit("resource_native-supervision.js", "description", "DELETE")

    def release_binding(self):
        key = "session_" + uid("session")
        path = f"msdyn_sessiontemplates({uid('session')})/msdyn_sessiontemplate_applicationtab"
        return key, path, setup.owned(self.config, "tab")

    def test_unbind_preserves_command_history_and_all_non_binding_objects(self):
        self.stage()
        self.api.commands = [{setup.PRIMARY: uid("history"), "moacc_outcome": "paused"}]
        records = copy.deepcopy(self.api.records)
        privileges = copy.deepcopy(self.api.privileges)
        metadata = copy.deepcopy(self.api.metadata)
        commands = copy.deepcopy(self.api.commands)
        start = len(self.api.writes)
        key, path, target = self.release_binding()
        with patch.object(self.runner, "assert_idle_disabled") as guard:
            result = self.runner.unbind()
        guard.assert_called_once()
        self.assertEqual(result["status"], "unbound")
        self.assertEqual(self.api.writes[start:], [("DELETE", path + f"({target})/$ref", None, None)])
        self.assertEqual(self.api.records, records)
        self.assertEqual(self.api.privileges, privileges)
        self.assertEqual(self.api.metadata, metadata)
        self.assertEqual(self.api.commands, commands)
        intent = self.journal.last("release_intent", key)
        released = self.journal.last("released", key)
        for event in (intent, released):
            self.assertEqual(event["path"], path)
            self.assertEqual(event["target"], target)
            self.assertEqual(event["primary"], "msdyn_applicationtabtemplateid")
            self.assertEqual(event["before"], sorted([uid("existing_tab"), target]))
            self.assertEqual(event["after"], [uid("existing_tab")])
        self.assertGreater(released["sequence"], intent["sequence"])
        before = (len(self.api.writes), len(self.journal.events))
        self.assertEqual(self.runner.verify_unbound(), result)
        with patch.object(self.runner, "assert_idle_disabled"):
            self.assertEqual(self.runner.unbind(), result)
        self.assertEqual((len(self.api.writes), len(self.journal.events)), before)
        self.assertNotIn("DYNAMICS_NATIVE_SUPERVISION_ENABLED", json.dumps(result))

    def test_unbind_needs_disabled_idle_guard_and_completed_owned_binding_evidence(self):
        self.stage()
        start = len(self.api.writes)
        with patch.object(self.runner, "assert_idle_disabled", side_effect=ValueError("not idle")), \
             self.assertRaisesRegex(ValueError, "not idle"):
            self.runner.unbind()
        self.assertEqual(len(self.api.writes), start)
        self.assertFalse(any(event["kind"] == "release_intent" for event in self.journal.events))
        key, _, _ = self.release_binding()
        self.journal.append("rejected", key, status=409)
        with patch.object(self.runner, "assert_idle_disabled"), self.assertRaisesRegex(ValueError, "trusted binding"):
            self.runner.unbind()
        self.assertEqual(len(self.api.writes), start)

    def test_unbind_refuses_foreign_membership_and_unrecorded_missing_edge(self):
        self.stage()
        key, path, target = self.release_binding()
        self.api.links[path][uid("foreign")] = {"msdyn_applicationtabtemplateid": uid("foreign")}
        start = len(self.api.writes)
        with patch.object(self.runner, "assert_idle_disabled"), self.assertRaisesRegex(ValueError, "exact captured baseline"):
            self.runner.unbind()
        self.assertEqual(len(self.api.writes), start)
        del self.api.links[path][uid("foreign")]
        del self.api.links[path][target]
        with patch.object(self.runner, "assert_idle_disabled"), self.assertRaisesRegex(ValueError, "exact captured baseline"):
            self.runner.unbind()
        self.assertIsNone(self.journal.last("release_intent", key))
        self.assertIsNone(self.journal.last("released", key))

    def test_interrupted_unbind_resumes_from_recorded_intent_without_redeleting(self):
        self.stage()
        key, path, target = self.release_binding()
        original = self.api.request
        def lost_response(target_path, **kwargs):
            result = original(target_path, **kwargs)
            if target_path == path + f"({target})/$ref" and kwargs.get("method") == "DELETE":
                raise urllib.error.HTTPError(target_path, 503, "lost delete response", {}, None)
            return result
        with patch.object(self.runner, "assert_idle_disabled"), \
             patch.object(self.api, "request", side_effect=lost_response), self.assertRaises(urllib.error.HTTPError):
            self.runner.unbind()
        self.assertIsNotNone(self.journal.last("release_intent", key))
        self.assertIsNone(self.journal.last("released", key))
        with self.assertRaisesRegex(ValueError, "cannot reactivate"):
            self.runner.bind()
        with self.assertRaisesRegex(ValueError, "cannot reactivate"):
            self.runner.verify()
        with self.assertRaisesRegex(ValueError, "completed release"):
            self.runner.verify_unbound()
        start = len(self.api.writes)
        with patch.object(self.runner, "assert_idle_disabled"):
            result = self.runner.unbind()
        self.assertEqual(result["status"], "unbound")
        self.assertEqual(len(self.api.writes), start)
        self.assertEqual(sum(event["kind"] == "release_intent" and event["key"] == key for event in self.journal.events), 1)
        self.assertIsNotNone(self.journal.last("released", key))

    def test_completed_release_cannot_rebind_verify_enable_or_silently_remove_a_revived_edge(self):
        self.stage()
        _, path, target = self.release_binding()
        with patch.object(self.runner, "assert_idle_disabled"):
            self.runner.unbind()
        with self.assertRaisesRegex(ValueError, "cannot reactivate"):
            self.runner.bind()
        with self.assertRaisesRegex(ValueError, "cannot reactivate"):
            self.runner.verify()
        self.api.links[path][target] = {"msdyn_applicationtabtemplateid": target}
        start = len(self.api.writes)
        with patch.object(self.runner, "assert_idle_disabled"), self.assertRaisesRegex(ValueError, "revived"):
            self.runner.unbind()
        with self.assertRaisesRegex(ValueError, "released baseline"):
            self.runner.verify_unbound()
        self.assertEqual(len(self.api.writes), start)
        self.assertIn(target, self.api.links[path])

    def test_foreign_change_during_unbind_is_preserved_and_never_receipted_as_released(self):
        self.stage()
        key, path, target = self.release_binding()
        original = self.api.request
        def concurrent_change(target_path, **kwargs):
            result = original(target_path, **kwargs)
            if target_path == path + f"({target})/$ref" and kwargs.get("method") == "DELETE":
                self.api.links[path][uid("foreign")] = {"msdyn_applicationtabtemplateid": uid("foreign")}
            return result
        with patch.object(self.runner, "assert_idle_disabled"), \
             patch.object(self.api, "request", side_effect=concurrent_change), \
             self.assertRaisesRegex(ValueError, "exact captured baseline"):
            self.runner.unbind()
        self.assertIn(uid("foreign"), self.api.links[path])
        self.assertIsNone(self.journal.last("released", key))
        start = len(self.api.writes)
        with patch.object(self.runner, "assert_idle_disabled"), self.assertRaisesRegex(ValueError, "unrelated membership"):
            self.runner.unbind()
        self.assertEqual(len(self.api.writes), start)

    def test_unbind_cli_is_explicit_and_verify_unbound_is_read_only_without_enable_file(self):
        import contextlib
        import io
        self.stage()
        directory = Path(self.directory.name)
        config_path = directory / "config.json"
        setup.native.write_private(config_path, json.dumps(self.config))
        setup.native.write_private(directory / "plan.json", json.dumps(self.plan))
        common = ["--config", str(config_path), "--output-dir", str(directory)]
        output = io.StringIO()
        start = len(self.api.writes)
        with patch.object(setup.native, "Dataverse", return_value=self.api), \
             patch.object(setup.native, "private_path", side_effect=lambda value: Path(value)), \
             patch.object(setup.Setup, "assert_idle_disabled") as guard, contextlib.redirect_stdout(output):
            setup.main(["unbind", *common])
            self.assertEqual(len(self.api.writes), start)
            guard.assert_not_called()
            with self.assertRaisesRegex(ValueError, "confirm-organization"):
                setup.main(["unbind", *common, "--apply"])
            setup.main(["unbind", *common, "--apply", "--confirm-organization", self.config["organization_url"]])
            after_unbind = len(self.api.writes)
            setup.main(["verify-unbound", *common])
            self.assertEqual(len(self.api.writes), after_unbind)
            with self.assertRaisesRegex(ValueError, "cannot reactivate"):
                setup.main(["verify", *common])
        result = json.loads(output.getvalue().splitlines()[-1])
        self.assertEqual(result["status"], "unbound")
        self.assertFalse(result["native_changes"])
        self.assertFalse((directory / "supervision.runtime.env").exists())


class RendererTests(unittest.TestCase):
    def test_deployment_parameters_are_opt_in_and_preserve_crm_identity_outputs(self):
        document = json.loads((ROOT / "infra/main.parameters.json").read_text())["parameters"]
        self.assertEqual(document["dynamicsNativeSupervisionEnabled"]["value"], "${DYNAMICS_NATIVE_SUPERVISION_ENABLED=false}")
        main = (ROOT / "infra/main.bicep").read_text()
        resources = (ROOT / "infra/resources.bicep").read_text()
        for name in ("dynamicsNativeSupervisionEnabled", "dynamicsNativeSupervisionActorIds", "dynamicsNativeSupervisionControllers"):
            self.assertIn("param " + name, main)
            self.assertIn("param " + name, resources)
            self.assertIn(name + ": " + name, main)
        for name in ("CLIENT_ID", "PRINCIPAL_ID", "RESOURCE_ID"):
            self.assertIn("output MOACC_CRM_UAMI_" + name, main)

    def test_all_branches_exist_and_quiet_hold_has_no_external_retry_or_native_transfer(self):
        source = topics.render_speech_turn_pilot(topics.load_topic(topics.TEMPLATE.read_text()), supervision=True)
        doc = topics.load_topic(source)
        def walk(value):
            if isinstance(value, dict):
                if "id" in value: yield value
                for child in value.values(): yield from walk(child)
            elif isinstance(value, list):
                for child in value: yield from walk(child)
        nodes = list(walk(doc))
        ids = [node["id"] for node in nodes]
        self.assertEqual(len(ids), len(set(ids)))
        for node in nodes:
            if node.get("kind") == "GotoAction":
                self.assertIn(node["actionId"], ids)
        by_id = {node["id"]: node for node in nodes}
        self.assertEqual(by_id["native_supervision_hold"]["kind"], "Question")
        self.assertEqual(by_id["native_supervision_hold_again"]["actionId"], "native_supervision_hold")
        self.assertFalse(by_id["native_supervision_poll"]["latencyMessageSettings"]["allowLatencyMessage"])
        self.assertNotIn("/turn", by_id["native_supervision_poll"]["url"])
        self.assertEqual(sum(node.get("kind") == "TransferConversationV2" for node in nodes), 1)
        self.assertIn('Topic.NativeResult.azure_leg_detached', by_id["native_handover_ready"]["condition"])
        failure = by_id["native_supervision_check_startup_failure"]
        condition = failure["conditions"][0]["condition"]
        for guard in (
            'Lower(Topic.NativeStopResult.conversation_id) = Topic.NativeConversationId',
            'Topic.NativeStopResult.supervision_contract = 1',
            'Topic.NativeStopResult.action = "stop"', 'Topic.NativeStopResult.phase = "stopped"',
            "Topic.NativeStopResult.azure_leg_detached", "Topic.NativeStopResult.startup_failed_unpaused",
            'Topic.NativeStopResult.ai_state = "active"', "!Topic.NativeStopResult.ai_pause_acknowledged",
        ):
            self.assertIn(guard, condition)
        self.assertEqual([node["id"] for node in failure["conditions"][0]["actions"]], [
            "native_report_failure", "native_connection_error", "native_end_failed_topic",
        ])
        self.assertEqual(failure["elseActions"], [{
            "kind": "GotoAction", "id": "native_supervision_cleanup_hold", "actionId": "native_supervision_hold",
        }])
        self.assertEqual(by_id["native_cleanup"]["responseSchema"]["properties"]["startup_failed_unpaused"], "Boolean")
        for name in ("native_reset_start_result", "native_reset_stop_result", "native_reset_wait_result"):
            self.assertIn("startup_failed_unpaused: false", by_id[name]["value"])

    def test_checked_in_supervision_snapshot_matches_optional_renderer(self):
        import sync_native_voice_topic as sync
        self.assertEqual(sync.SUPERVISION_OUTPUT.read_bytes(), sync.generated(supervision=True).encode())
