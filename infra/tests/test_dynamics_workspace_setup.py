"""Offline provisioning/ownership tests; optional private installed-client contracts."""

import copy
import fnmatch
import html
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
import urllib.error
import xml.etree.ElementTree as ET
from unittest.mock import MagicMock, patch
from types import SimpleNamespace
from uuid import NAMESPACE_URL, uuid5


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "infra/scripts"))
import setup_dynamics_routing as routing
import setup_dynamics_workspace as setup


def uid(name):
    return str(uuid5(NAMESPACE_URL, "https://offline.invalid/workspace-test/" + name))


def config():
    return setup.validate_config({
        "org_url": "https://example.crm.dynamics.com", "tenant_id": uid("tenant"), "app_id": uid("app"),
        "namespace": "service-workspace-v1", "prefix": "sample_rep", "label": "Service representative",
        "enable_knowledge": True, "enable_copilot": True,
        "representative_ids": [uid("representative")], "workstream_ids": [uid("workstream"), uid("workstream2")],
        "approvals": {key: True for key in (
            "provision_arabic", "publish_app", "publish_knowledge", "shared_copilot_settings",
            "native_copilot_status_check", "bind_representatives_and_workstreams", "licenses_and_region_reviewed",
        )},
    })


def native_fixture():
    cfg = config()
    xml = (
        '<form><tabs><tab><columns><column><sections><section><rows><row><cell>'
        '<control id="RelatedCases"><parameters>'
        '<ViewId>{' + uid("my_active_cases") + '}</ViewId><ViewIds>{' + uid("my_active_cases") + '}</ViewIds>'
        '<RelationshipName>incident_customer_contacts</RelationshipName><RecordsPerPage>4</RecordsPerPage>'
        '</parameters></control></cell></row></rows></section></sections></column></columns></tab></tabs>'
        '<DisplayConditions FallbackForm="true" Order="30"><Role Id="{' + uid("role") + '}"/></DisplayConditions></form>'
    )
    source_form = {
        "formid": uid("contact_form"), "name": "Contact for Multisession experience", "objecttypecode": "contact",
        "type": 2, "ismanaged": True, "isdefault": True, "formxml": xml, "formactivationstate": 1,
        "componentstate": 0, "@odata.etag": 'W/"1"',
    }
    source_case_form = {
        "formid": uid("case_form"), "name": "Enhanced full case form", "objecttypecode": "incident",
        "type": 2, "ismanaged": True, "isdefault": False, "formactivationstate": 1,
        "componentstate": 0, "@odata.etag": 'W/"10"',
        "formxml": '<form><tabs><tab name="general"><columns><column><sections><section name="native">'
                   '<rows><row><cell><control id="title" datafieldname="title" '
                   'classid="{4273EDBD-AC1D-40d3-9FB2-095C621B552D}"/></cell></row></rows>'
                   '</section></sections></column></columns></tab></tabs>'
                   '<controlDescriptions><controlDescription forControl="native-summary"/></controlDescriptions>'
                   '<DisplayConditions FallbackForm="false" Order="29"><Everyone/></DisplayConditions></form>',
    }
    component = {"componenttype": 60, "objectid": uid("contact_form"), "isdefault": True, "rootcomponentbehavior": 0}
    app = {
        "appmoduleid": cfg["app_id"], "appmoduleidunique": uid("app_published"), "uniquename": "msdyn_CustomerServiceWorkspace",
        "name": "Copilot Service workspace", "@odata.etag": 'W/"2"', "isdefault": False, "navigationtype": 1,
        "descriptor": json.dumps({"appInfo": {
            "Components": [{"Type": 60, "Id": uid("contact_form")}],
            "AppComponents": {"Entities": [{"Id": uid("contact_entity")}]}, "PublishedOn": "old",
        }, "publisherInfo": {"Id": uid("publisher"), "Name": "Microsoft"}}),
        "configxml": '<AppModule><AppModuleComponents><AppModuleComponent type="60" id="' + uid("contact_form") +
                     '"/></AppModuleComponents><Navigation>preserved</Navigation></AppModule>',
    }
    profile = {
        "msdyn_appconfigurationid": uid("source_profile"), "msdyn_uniquename": "msdyn_csw_app_configuration",
        "msdyn_appmoduleuniquename": "msdyn_CustomerServiceWorkspace", "ismanaged": True, "@odata.etag": 'W/"3"',
        setup.PROFILE_USERS: [{"systemuserid": cfg["representative_ids"][0]}, {"systemuserid": uid("unapproved")}],
    }
    unrelated_profile = {
        "msdyn_appconfigurationid": uid("other_app_profile"), "msdyn_uniquename": "other_app",
        "msdyn_appmoduleuniquename": "UnrelatedApp", "@odata.etag": 'W/"4"',
        setup.PROFILE_USERS: [{"systemuserid": cfg["representative_ids"][0]}],
    }
    shared = {
        "legacy": {"msdyn_enabledforagents": True, "msdyn_allagentsenabled": True,
                   "msdyn_whenagentjoins": False, "msdyn_conversationends": True, "msdyn_showcreatecase": True},
        "modern": {"msdyn_summarytype": 0, "msdyn_useagentlanguage": True, "msdyn_allowtranslation": False,
                   "msdyn_allowcrossgeo": False, **setup.SUMMARY_PATCHES["modern"]},
        "copilot": {"msdyn_copilotenabled": True, "msdyn_agentassistenabled": True, "msdyn_allagentsenabled": True,
                    "msdyn_allworkstreamsenabled": True, "msdyn_knowledgearticlesourceenabled": False,
                    "msdyn_systemstatus": 100230002, "msdyn_emailassistenabled": True,
                    "msdyn_allowcrossgeo": False, "msdyn_conversationtocaseautonomousflowenabled": False},
    }
    for family, row in shared.items():
        row[setup.SHARED_TABLES[family][1]] = uid(family)
        row["@odata.etag"] = 'W/"5"'
    native = {
        "app": app, "app_draft": copy.deepcopy(app), "app_components": [component],
        "app_validation": {"AppValidationResponse": {"ValidationIssueList": []}},
        "profile": profile, "profiles": [profile, unrelated_profile],
        "source_assignments": {cfg["representative_ids"][0]: uid("source_profile")},
        "profile_relations": {
            "msdyn_appconfiguration_applicationextension": [{"msdyn_applicationextensionid": uid("extension")}],
            "msdyn_appconfig_msdyn_channelprovider": [{"msdyn_channelproviderid": uid("channel")}],
        },
        "contact_form": source_form, "contact_forms": [source_form],
        "case_form": source_case_form, "case_forms": [source_case_form],
        "case_attributes": [
            {"LogicalName": field, "AttributeType": "String", "IsValidForRead": True,
             "IsValidForCreate": True, "IsValidForUpdate": True}
            for field in setup.CASE_SECTION_FIELDS
        ],
        "voice_session": {"_msdyn_anchortab_value": uid("anchor")},
        "knowledge_tab": {"msdyn_applicationtabtemplateid": uid("knowledge_tab")},
        "all_cases": {"savedqueryid": uid("all_cases"), "@odata.etag": 'W/"6"',
                      "fetchxml": "<fetch><entity name='incident'/></fetch>"},
        "workstreams": [{"msdyn_liveworkstreamid": value, "msdyn_sessiontemplate_default": "msdyn_voicecall_session",
                         "@odata.etag": 'W/"7"'} for value in cfg["workstream_ids"]],
        "languages": [1033, 1025], "locales": {"en-US": uid("en"), "ar-SA": uid("ar")},
        "user_settings": {cfg["representative_ids"][0]: {
            "systemuserid": cfg["representative_ids"][0], "uilanguageid": 1033,
            "helplanguageid": 0, "localeid": 1033, "@odata.etag": 'W/"8"',
        }},
        "shared": shared, "unrelated_knowledge": [{
            "knowledgearticleid": uid("unrelated_article"), "articlepublicnumber": "OTHER-01",
            "statecode": 3, "statuscode": 7, "isrootarticle": False, "@odata.etag": 'W/"9"',
        }],
        "macro_actions": [{"msdyn_name": name, "msdyn_runtimeapi": name} for name in sorted(setup.NATIVE_ACTIONS)],
    }
    for key in ("copilot_tool", "knowledge_tool", "scripts_tool"):
        native[key] = {"msdyn_panetoolconfigurationid": uid(key), "msdyn_name": key, "ismanaged": True}
    return native


def knowledge_fixture(boundary="## Sources"):
    canonical = "# Organization service knowledge\n\nRevision: unit-v1\n\n"
    canonical += "\n\n".join(f"### {number:02d}. Technical topic {number}\n\nApproved fixture text [S{(number - 1) % 3 + 1:02d}]."
                             for number in range(1, 13))
    canonical += "\n\n" + (boundary + "\n" if boundary else "") + "\n".join(
        f'[S{number:02d}]: https://example.org/reference/{number} "Reference {number}"' for number in range(1, 4))
    arabic = "# Representative knowledge translation\n\nRevision: unit-v1\nCanonical-SHA256: " + setup.digest(canonical) + "\n\n"
    arabic += "\n\n".join(f"### {number:02d}. موضوع تقني {number}\n\nنص تقني للتحقق [S{(number - 1) % 3 + 1:02d}]."
                         for number in range(1, 13))
    return canonical, arabic


def plan_fixture(cfg=None):
    cfg, native = cfg or config(), native_fixture()
    return {
        "schema": setup.SCHEMA, "config": cfg, "native": native,
        "workspace": setup.render_workspace(cfg, native),
        "knowledge": setup.render_knowledge(cfg, native["locales"], *knowledge_fixture()),
        "knowledge_visibility": [setup.visibility_filter_item(cfg, user) for user in cfg["representative_ids"]],
        "input_hashes": setup.input_hashes(), "gates": [],
        "acceptance": {"browser_rendering": "not tested", "native_generated_answers": "not tested",
                       "summary_quality": "not tested", "clean_tenant_replay": "not tested",
                       "live_case_querying": "not configured"},
    }


class MemoryJournal(setup.Journal):
    def __init__(self):
        self.state = {
            "owned": {}, "generated_parameters": {}, "links": {}, "shared": {}, "stages": {},
            "language_job": None, "pending": None, "app_registered": False,
            "app_published": False, "bound": False, "rolled_back": False,
        }
        self.events = []

    def save(self, label, updates=None, evidence=None):
        event = {"label": label, "updates": copy.deepcopy(updates or {}), "evidence": copy.deepcopy(evidence)}
        self.events.append(event)
        self.merge(copy.deepcopy(updates or {}))
        return Path("offline-receipt.json")


RELATION_TABLES = {
    "systemuserroles_association": "roles", "teamroles_association": "roles", "teammembership_association": "teams",
}
COPILOT_SUCCESS = json.dumps({"tries": 2, "status": "success", "last_updated_timestamp": "2026-01-01T00:39:00Z",
                              "message": "Provisioned or synced DV copilot for KB articles successfully."})


class FakeAPI:
    def __init__(self, plan):
        self.plan = copy.deepcopy(plan)
        native = self.plan["native"]
        self.org = plan["config"]["org_url"]
        self.tenant = plan["config"]["tenant_id"]
        self.data, self.links, self.writes = {}, {}, []
        self.version = 100
        self.languages = list(native["languages"])
        self.active = False
        self.ready_automatically = True
        self.fail_next = None
        self.who = uid("administrator")
        self.metadata = {}
        self.sibling_sync = False
        self.profile_app_default = None
        self.links_on_profile_create = []
        self.honor_caller = True
        self.stale_reads = {}
        self.app = copy.deepcopy(native["app"])
        self.draft = copy.deepcopy(self.app)
        self.draft["appmoduleidunique"] = uid("app_draft")
        self.published_components = copy.deepcopy(native["app_components"])
        self.draft_components = copy.deepcopy(self.published_components)
        initial = [
            ("systemforms", native["contact_form"]), ("systemforms", native["case_form"]),
            ("savedqueries", native["all_cases"]),
            *[("msdyn_liveworkstreams", row) for row in native["workstreams"]],
            *[("msdyn_appconfigurations", row) for row in native["profiles"]],
            *[("usersettingscollection", row) for row in native["user_settings"].values()],
            *[(setup.SHARED_TABLES[key][0], row) for key, row in native["shared"].items()],
            *[("knowledgearticles", row) for row in native["unrelated_knowledge"]],
        ]
        for table, row in initial:
            primary = self.primary(table)
            self.data.setdefault(table, {})[row[primary]] = copy.deepcopy(row)
        self.data["msdyn_knowledgesearchfilters"] = {
            uid("visibility"): {
                "msdyn_knowledgesearchfilterid": uid("visibility"),
                "msdyn_entityname": "knowledgearticle", "msdyn_attributename": "isinternal",
                "msdyn_enabled": True, "msdyn_defaultvalues": '["1","0"]',
            },
        }
        for row in native["profiles"]:
            self.links[("msdyn_appconfigurations", row["msdyn_appconfigurationid"], setup.PROFILE_USERS)] = {
                user["systemuserid"] for user in row[setup.PROFILE_USERS]
            }
        self.job_status = 30

    def primary(self, table):
        return setup.TABLE_IDS.get(table, {
            "savedqueries": "savedqueryid", "msdyn_liveworkstreams": "msdyn_liveworkstreamid",
            "usersettingscollection": "systemuserid", "asyncoperations": "asyncoperationid",
            "systemusers": "systemuserid", "roles": "roleid", "teams": "teamid", "queues": "queueid",
            "msdyn_capacityprofiles": "msdyn_capacityprofileid", "bookableresources": "bookableresourceid",
            routing.CAPACITY: routing.CAPACITY_ID,
            "msdyn_liveworkstreamcapacityprofiles": "msdyn_liveworkstreamcapacityprofileid",
            "msdyn_knowledgepersonalfilters": "msdyn_knowledgepersonalfilterid",
            **{table: primary for table, primary in setup.SHARED_TABLES.values()},
        }.get(table))

    def touch(self, row):
        self.version += 1
        row["@odata.etag"] = f'W/"{self.version}"'
        return row

    def make_ready(self):
        row = self.data["msdyn_agentcopilotsettings"][uid("copilot")]
        row.update(msdyn_systemstatus=100230003, msdyn_dvcopilotstatus=COPILOT_SUCCESS)

    @staticmethod
    def select(row, query):
        row = copy.deepcopy(row)
        if query and query.get("$select"):
            fields = query["$select"].split(",")
            return {key: value for key, value in row.items() if key in fields or key == "@odata.etag"}
        return row

    def get(self, path, query=None):
        if path == "WhoAmI":
            return {"UserId": self.who, "BusinessUnitId": uid("business-unit")}
        if path == "RetrieveProvisionedLanguages()":
            return {"RetrieveProvisionedLanguages": list(self.languages)}
        if path.startswith("ValidateApp("):
            return {"AppValidationResponse": {"ValidationIssueList": []}}
        if path.startswith("RetrieveAppComponents("):
            return {"value": copy.deepcopy(self.published_components)}
        if path.startswith("msdyn_getAppConfigByAgent("):
            user = re.search("UserId='([^']+)'", path)[1]
            owned = self.plan["workspace"]["ids"]["profile"]
            members = self.links.get(("msdyn_appconfigurations", owned, setup.PROFILE_USERS), set())
            identifier = owned if user in members else uid("source_profile")
            return {"AppConfigurations": json.dumps([{"AppConfigId": identifier}])}
        match = re.fullmatch(r"(\w+)\(([a-f0-9-]+)\)(.*)", path)
        if not match:
            raise AssertionError("Unexpected GET " + path)
        table, identifier, suffix = match.groups()
        if table == "appmodules":
            return copy.deepcopy(self.draft if suffix else self.app)
        row = self.data.get(table, {}).get(identifier)
        if row is None:
            raise urllib.error.HTTPError("https://offline.invalid", 404, "absent", None, io.BytesIO())
        if self.stale_reads.get(identifier):
            self.stale_reads[identifier] -= 1
            row = {**row, "formxml": self.plan["native"]["case_form"]["formxml"]}
        if table == "asyncoperations" and self.job_status == 30:
            if 1025 not in self.languages:
                self.languages.append(1025)
        return self.select(row, query)

    def rows(self, table, query):
        if table == "msdyn_ocliveworkitems":
            return [{"activityid": uid("active")} ] if self.active else []
        if table == "appmodulecomponents":
            source = self.draft_components if uid("app_draft") in query["$filter"] else self.published_components
            return copy.deepcopy(source)
        if table == "workflows" and query.get("$filter", "").startswith("name eq 'Check Dataverse"):
            return [{"workflowid": uid("native_status_action"), "name": "Check Dataverse Copilot Status"}]
        if table.startswith("EntityDefinitions("):
            return copy.deepcopy(self.metadata.get(table, []))
        match = re.fullmatch(r"(\w+)\(([a-f0-9-]+)\)/(\w+)", table)
        if match:
            source, identifier, relation = match.groups()
            targets = self.links.get((source, identifier, relation), set())
            primary = query.get("$select")
            if not primary:
                raise AssertionError("Relation queries must select a target primary")
            if relation in RELATION_TABLES:
                return [self.select(self.data[RELATION_TABLES[relation]][value], query) for value in sorted(targets)]
            return [{primary: value} for value in sorted(targets)]
        rows = list(self.data.get(table, {}).values())
        condition = query.get("$filter", "")
        pattern = r"(\w+) eq (?:'([^']*)'|([a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12})|(true|false))"
        if " or " in condition and " and " not in condition:
            wanted = {(field, identifier or literal) for field, literal, identifier, _ in re.findall(pattern, condition)}
            rows = [row for row in rows if any(row.get(field) == value for field, value in wanted)]
            condition = ""
        for field, literal, identifier, boolean in re.findall(pattern, condition):
            expected = identifier or literal
            if boolean:
                expected = boolean == "true"
            rows = [row for row in rows if row.get(field) == expected]
        output = [self.select(row, query) for row in rows]
        if table == "msdyn_appconfigurations":
            for row in output:
                row[setup.PROFILE_USERS] = [{"systemuserid": user} for user in sorted(self.links.get(
                    (table, row["msdyn_appconfigurationid"], setup.PROFILE_USERS), set(),
                ))]
        return output

    def translate_payload(self, payload):
        row = {}
        for field, value in payload.items():
            if field.endswith("@odata.bind"):
                field = setup.LOOKUPS[field.removesuffix("@odata.bind")]
                value = value.rsplit("(", 1)[1].rstrip(")")
            row[field] = value
        return row

    def sync_aggregate(self, tab, *, changed=None):
        children = [row for row in self.data["msdyn_templateparameters"].values()
                    if row["_msdyn_applicationtabtemplateid_value"] == tab]
        row = self.data["msdyn_applicationtabtemplates"][tab]
        row["msdyn_templateparameters"] = json.dumps({child["msdyn_name"]: {"value": child.get("msdyn_value")} for child in children})
        self.touch(row)
        if self.sibling_sync and changed:
            for child in children:
                if child["msdyn_templateparameterid"] != changed:
                    self.touch(child)
                    child.update(versionnumber=self.version, modifiedon=f"2026-01-01T00:00:{self.version % 60:02d}Z")

    def app_descriptor(self, app, components, *, staged=False):
        descriptor = json.loads(app["descriptor"])
        descriptor["appInfo"]["Components"] = [] if staged else [
            {"Type": row["componenttype"], "Id": row["objectid"]} for row in components
        ]
        descriptor["appInfo"]["VersionNumber"] = 99
        descriptor["appInfo"]["SolutionId"] = uid("new_layer")
        descriptor["publisherInfo"]["Id"] = uid("new_layer_publisher")
        app["descriptor"] = json.dumps(descriptor)
        tree = ET.fromstring(app["configxml"])
        element = tree.find("AppModuleComponents")
        element[:] = []
        for row in components:
            ET.SubElement(element, "AppModuleComponent", type=str(row["componenttype"]), id=row["objectid"])
        app["configxml"] = ET.tostring(tree, encoding="unicode")
        self.touch(app)

    def request(self, path, *, method="GET", body=None, headers=None, raw=False):
        self.writes.append({"path": path, "method": method, "body": copy.deepcopy(body), "headers": copy.deepcopy(headers)})
        if self.fail_next:
            code, self.fail_next = self.fail_next, None
            raise urllib.error.HTTPError("https://offline.invalid", code, "simulated", None, io.BytesIO())
        if path == "FullTextSearchKnowledgeArticle":
            return {"value": [copy.deepcopy(row) for row in self.data.get("knowledgearticles", {}).values()
                              if row.get("statecode") == 3]}
        if path == "ProvisionLanguageAsync":
            identifier = uid("language_job")
            self.data.setdefault("asyncoperations", {})[identifier] = {
                "asyncoperationid": identifier, "statecode": 3, "statuscode": self.job_status, "errorcode": 0,
            }
            return {"AsyncOperationId": identifier}
        if path == "msdyn_CheckDVCopilotStatus":
            if self.ready_automatically:
                self.make_ready()
            return {}
        if path == "CreateKnowledgeArticleTranslation":
            original = self.data["knowledgearticles"][body["Source"]["knowledgearticleid"]]
            row = copy.deepcopy(original)
            identifier = uid("translation-" + original["knowledgearticleid"])
            row.update({
                "knowledgearticleid": identifier, "isprimary": False, "statecode": 0, "statuscode": 1,
                "_languagelocaleid_value": body["Language"]["languagelocaleid"],
                "_parentarticlecontentid_value": original["knowledgearticleid"],
            })
            self.data["knowledgearticles"][identifier] = self.touch(row)
            return {"knowledgearticleid": identifier}
        if path in ("AddAppComponents", "RemoveAppComponents"):
            for component in body["Components"]:
                identifier = component["formid"]
                if path == "AddAppComponents":
                    self.draft_components.append({"componenttype": 60, "objectid": identifier,
                                                  "isdefault": False, "rootcomponentbehavior": 0})
                else:
                    self.draft_components = [row for row in self.draft_components if row["objectid"] != identifier]
            self.app_descriptor(self.draft, self.draft_components, staged=True)
            return {}
        if path == "PublishXml":
            self.published_components = copy.deepcopy(self.draft_components)
            self.app_descriptor(self.app, self.published_components)
            self.draft = copy.deepcopy(self.app)
            self.draft["appmoduleidunique"] = uid("app_draft")
            return {}
        if path == "$batch":
            text = body.decode()
            count = 0
            for block in text.split("Content-Type: application/http")[1:]:
                match = re.search(r"\r\n(PATCH|POST|DELETE) https://[^/]+/api/data/v9.2/(.*?) HTTP/1.1\r\nIf-Match: (.*?)\r\n", block)
                verb, route, match_etag = match.groups()
                data = re.search(r"\r\n\r\n(\{[^\r]+\})\r\n", block)
                self.request(route, method=verb, body=json.loads(data[1]) if data else None, headers={"If-Match": match_etag})
                count += 1
            return "".join("HTTP/1.1 204 No Content\r\n" for _ in range(count))
        if method == "POST" and "(" not in path and self.primary(path):
            row = self.translate_payload(body)
            primary = self.primary(path)
            identifier = row[primary]
            if identifier in self.data.get(path, {}):
                raise AssertionError("Duplicate create")
            row.setdefault("statecode", 0)
            row.setdefault("statuscode", 1)
            row.setdefault("ismanaged", False)
            if path == "systemforms":
                row["componentstate"] = 0
            if path == "msdyn_appconfigurations":
                row["msdyn_appmoduleuniquename"] = self.profile_app_default or row["msdyn_appmoduleuniquename"]
                for relation, target in self.links_on_profile_create:
                    self.links.setdefault((path, identifier, relation), set()).add(target)
            if path == "msdyn_knowledgepersonalfilters":
                caller = (headers or {}).get("MSCRMCallerID") if self.honor_caller else None
                row.update(_ownerid_value=caller or self.who, _createdby_value=caller or self.who,
                           _createdonbehalfby_value=self.who if caller else None)
            if path == "knowledgearticles":
                root_id = uid("root-" + identifier)
                row.update({"isrootarticle": False, "isprimary": True, "islatestversion": True,
                            "_rootarticleid_value": root_id, "_parentarticlecontentid_value": root_id})
                root = {"knowledgearticleid": root_id, "articlepublicnumber": row["articlepublicnumber"],
                        "isrootarticle": True, "statecode": 0, "statuscode": 1}
                self.data.setdefault(path, {})[root_id] = self.touch(root)
            self.data.setdefault(path, {})[identifier] = self.touch(row)
            if path == "msdyn_applicationtabtemplates":
                for name in ("entityName", "entityId", "formId", "validateRecord"):
                    child = {
                        "msdyn_templateparameterid": uid(identifier + "-" + name), "msdyn_name": name,
                        "msdyn_value": None, "_msdyn_applicationtabtemplateid_value": identifier,
                    }
                    self.data.setdefault("msdyn_templateparameters", {})[child["msdyn_templateparameterid"]] = self.touch(child)
                self.sync_aggregate(identifier)
            elif path == "msdyn_templateparameters":
                self.sync_aggregate(row["_msdyn_applicationtabtemplateid_value"], changed=identifier)
            return {}
        match = re.fullmatch(r"(\w+)\(([a-f0-9-]+)\)(.*)", path)
        if not match:
            raise AssertionError("Unexpected mutation: " + path)
        table, identifier, suffix = match.groups()
        row = self.data[table][identifier]
        if headers and headers.get("If-Match") != row["@odata.etag"]:
            raise urllib.error.HTTPError("https://offline.invalid", 412, "etag", None, io.BytesIO())
        if suffix:
            relation = suffix.split("/")[1].split("(")[0]
            members = self.links.setdefault((table, identifier, relation), set())
            if method == "POST":
                members.add(body["@odata.id"].rsplit("(", 1)[1].rstrip(")"))
            elif method == "DELETE":
                members.remove(re.search(r"\(([a-f0-9-]+)\)", suffix)[1])
            else:
                raise AssertionError(method)
            return {}
        if method == "PATCH":
            row.update(self.translate_payload(body))
            self.touch(row)
            if table == "msdyn_templateparameters":
                self.sync_aggregate(row["_msdyn_applicationtabtemplateid_value"], changed=identifier)
            if table == "msdyn_agentcopilotsettings" and body.get("msdyn_knowledgearticlesourceenabled") and self.ready_automatically:
                self.make_ready()
            return {}
        raise AssertionError("Unexpected method " + method)


class RenderingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.plan = plan_fixture()

    def test_exact_native_inventory_and_language_scoping(self):
        workspace = self.plan["workspace"]
        rows = workspace["records"]
        expected = {"msdyn_appconfigurations": 1, "msdyn_sessiontemplates": 2,
                    "msdyn_applicationtabtemplates": 6, "msdyn_productivityagentscripts": 6,
                    "msdyn_productivityagentscriptsteps": 56, "workflows": 9,
                    "msdyn_panetabconfigurations": 3, "systemforms": 2}
        for table, count in expected.items():
            self.assertEqual(sum(row["table"] == table for row in rows), count)
        scripts = {row["id"]: row for row in rows if row["table"] == "msdyn_productivityagentscripts"}
        self.assertEqual(sorted(row["payload"]["msdyn_language"] for row in scripts.values()), [1025] * 3 + [1033] * 3)
        routes = [row for row in rows if "msdyn_routeactionid@odata.bind" in row["payload"]]
        self.assertEqual(len(routes), 2)
        for row in routes:
            payload = row["payload"]
            source = payload["msdyn_agentscriptid@odata.bind"].split("(")[1][:-1]
            target = payload["msdyn_routeactionid@odata.bind"].split("(")[1][:-1]
            self.assertEqual(scripts[source]["payload"]["msdyn_language"], scripts[target]["payload"]["msdyn_language"])
        self.assertEqual(sum(link["relation"] == setup.SESSION_SCRIPTS for link in workspace["links"]), 4)

    def test_contact_clone_preserves_source_and_other_xml(self):
        native = self.plan["native"]
        source = copy.deepcopy(native["contact_form"])
        clone = next(row["payload"] for row in self.plan["workspace"]["records"] if row["key"] == "contact_form")
        tree = ET.fromstring(clone["formxml"])
        display = tree.find("DisplayConditions")
        self.assertEqual((display.get("Order"), display.get("FallbackForm")), ("31", "false"))
        self.assertFalse(clone["isdefault"])
        grid = tree.find(".//control[@id='RelatedCases']")
        self.assertEqual(grid.findtext("parameters/RelationshipName"), "incident_customer_contacts")
        self.assertEqual(grid.findtext("parameters/RecordsPerPage"), "4")
        self.assertEqual(grid.findtext("parameters/ViewId").strip("{}").lower(), uid("all_cases"))
        self.assertEqual(source, native["contact_form"])
        for name in ("ViewId", "ViewIds"):
            grid.find("parameters/" + name).text = "{" + uid("my_active_cases") + "}"
        display.set("FallbackForm", "true")
        display.set("Order", "30")
        self.assertEqual(ET.canonicalize(ET.tostring(tree, encoding="unicode")), ET.canonicalize(source["formxml"]))

    def test_form_clone_rejects_wrong_relationship_or_ambiguous_grid(self):
        native = self.plan["native"]
        for xml in (
            native["contact_form"]["formxml"].replace("incident_customer_contacts", "unrelated_relationship"),
            native["contact_form"]["formxml"].replace("</form>", '<control id="RelatedCases"/></form>'),
        ):
            source = {**native["contact_form"], "formxml": xml}
            with self.assertRaises(setup.GateError):
                setup.contact_form(source, native["contact_forms"], native["all_cases"], uid("clone"), "Sample")

    def test_clones_append_after_explicit_orders_without_rewriting_unordered_native_forms(self):
        native = self.plan["native"]
        for entity, source in (("contact", native["contact_form"]), ("case", native["case_form"])):
            forms = [
                copy.deepcopy(source),
                {"formxml": "<form/>"},
                {"formxml": '<form><DisplayConditions FallbackForm="true"/></form>'},
                {"formxml": '<form><DisplayConditions Order="50"/></form>'},
            ]
            before = copy.deepcopy(forms)
            with self.subTest(entity=entity):
                if entity == "contact":
                    payload = setup.contact_form(source, forms, native["all_cases"], uid("clone"), "Sample")
                else:
                    payload = setup.case_form(source, forms, native["case_attributes"], uid("clone"), "Sample")
                display = ET.fromstring(payload["formxml"]).find("DisplayConditions")
                self.assertEqual(display.get("Order"), "51")
                self.assertEqual(display.get("FallbackForm"), "false")
                self.assertFalse(payload["isdefault"])
                self.assertEqual(
                    [ET.tostring(child) for child in display],
                    [ET.tostring(child) for child in ET.fromstring(source["formxml"]).find("DisplayConditions")],
                )
                self.assertEqual(forms, before)

    def test_form_order_requires_a_real_numeric_baseline_instead_of_defaulting_to_zero(self):
        forms = [{"formxml": "<form/>"}, {"formxml": "<form><DisplayConditions/></form>"}]
        for entity in ("contact", "case"):
            with self.subTest(entity=entity), self.assertRaisesRegex(setup.GateError, "no explicit order baseline"):
                setup.next_form_display_order(forms, entity)

    def test_invalid_explicit_form_order_is_not_treated_as_missing(self):
        for entity in ("contact", "case"):
            for value in ("", "invalid", "-1", "1.5"):
                forms = [
                    {"formxml": '<form><DisplayConditions Order="30"/></form>'},
                    {"formxml": '<form><DisplayConditions Order="' + value + '"/></form>'},
                ]
                with self.subTest(entity=entity, value=value), self.assertRaisesRegex(setup.GateError, "invalid explicit display order"):
                    setup.next_form_display_order(forms, entity)

    def test_scoped_case_clone_exposes_only_existing_columns_and_preserves_native_controls(self):
        native = self.plan["native"]
        before = copy.deepcopy(native["case_form"])
        payload = next(row["payload"] for row in self.plan["workspace"]["records"] if row["key"] == "case_form")
        tree = ET.fromstring(payload["formxml"])
        section = tree.find("./tabs/tab[@name='general']/columns/column/sections/section")
        self.assertEqual(section.get("name"), "case_location")
        self.assertEqual(section.get("visible"), "true")
        self.assertIsNone(section.get("labelid"))
        self.assertEqual([node.attrib for node in section.findall("labels/label")],
                         [{"description": setup.CASE_SECTION_HEADING, "languagecode": "1033"}])
        self.assertEqual({node.get("datafieldname") for node in section.findall(".//control")},
                         set(setup.CASE_SECTION_FIELDS))
        for node in section.findall(".//control"):
            self.assertEqual(node.get("disabled"), "false")
        self.assertFalse(payload["isdefault"])
        self.assertEqual(tree.find("DisplayConditions").attrib, {"FallbackForm": "false", "Order": "30"})
        self.assertEqual(before, native["case_form"])
        tree.find("./tabs/tab/columns/column/sections").remove(section)
        tree.find("DisplayConditions").set("Order", "29")
        self.assertEqual(ET.canonicalize(ET.tostring(tree, encoding="unicode")),
                         ET.canonicalize(before["formxml"]))
        for parameter in self.plan["workspace"]["parameters"]:
            if parameter["tab"] in {self.plan["workspace"]["ids"][key]
                                    for key in ("voice_case", "new_case", "new_case_customer")} and parameter["name"] == "formId":
                self.assertEqual(parameter["value"], payload["formid"])

    def test_case_clone_rejects_missing_or_incompatible_crm_columns(self):
        native = self.plan["native"]
        for attributes in (native["case_attributes"][:-1],
                           [{**row, "AttributeType": "Memo"} for row in native["case_attributes"]]):
            with self.assertRaisesRegex(setup.GateError, "custom case text columns"):
                setup.case_form(native["case_form"], native["case_forms"], attributes, uid("clone"), "Sample")
        with self.assertRaisesRegex(setup.GateError, "Details layout"):
            setup.case_form({**native["case_form"], "formxml": native["case_form"]["formxml"].replace(
                'name="general"', 'name="different"')}, native["case_forms"], native["case_attributes"],
                uid("clone"), "Sample")

    def localized_case_fixture(self):
        payload = next(row["payload"] for row in self.plan["workspace"]["records"] if row["key"] == "case_form")
        row = {
            "formid": payload["formid"], "formidunique": uid("case-layer"),
            "componentstate": 0, "@odata.etag": 'W/"12"',
            "formxml": setup.case_form_english_projection(payload["formxml"]),
        }
        full = {**row, "formxml": payload["formxml"]}
        return payload, row, full

    def test_case_localization_verifies_bilingual_heading_and_eight_field_labels_without_publishing(self):
        payload, row, full = self.localized_case_fixture()
        api = MagicMock()
        api.get.return_value = full
        proof = setup.localized_case_form_readback(api, row, payload["formxml"])
        self.assertEqual(proof["mode"], "published-english-projection-with-stored-translations")
        self.assertEqual(len(proof["verified_case_labels"]), 9)
        self.assertEqual({label["language"] for label in proof["verified_case_labels"]}, {1033, 1025})
        self.assertFalse(proof["browser_rendering_verified"])
        api.get.assert_called_once_with(
            setup.record_path("systemforms", row["formid"]) + "/Microsoft.Dynamics.CRM.RetrieveUnpublished"
        )
        api.request.assert_not_called()

    def test_case_localization_does_not_accept_missing_field_translation(self):
        payload, row, full = self.localized_case_fixture()
        for field in setup.CASE_SECTION_FIELDS:
            with self.subTest(field=field):
                tree = ET.fromstring(full["formxml"])
                cell = next(node for node in tree.findall(".//cell")
                            if node.find("control[@datafieldname='" + field + "']") is not None)
                labels = cell.find("labels")
                labels.remove(labels.find("label[@languagecode='1025']"))
                api = MagicMock()
                api.get.return_value = {**full, "formxml": ET.tostring(tree, encoding="unicode")}
                with self.assertRaisesRegex(setup.GateError, "translation is missing"):
                    setup.localized_case_form_readback(api, row, payload["formxml"])
                api.request.assert_not_called()

    def test_case_localization_rejects_drafts_version_changes_and_unrelated_layout_changes(self):
        payload, row, full = self.localized_case_fixture()
        for changes in (
            {"componentstate": 1}, {"@odata.etag": 'W/"13"'},
            {"formidunique": uid("another-layer")},
            {"formxml": full["formxml"].replace('datafieldname="title"', 'datafieldname="description"')},
        ):
            with self.subTest(changes=list(changes)):
                api = MagicMock()
                api.get.return_value = {**full, **changes}
                with self.assertRaises(setup.GateError):
                    setup.localized_case_form_readback(api, row, payload["formxml"])
                api.request.assert_not_called()

    def test_case_localization_requires_the_approved_bilingual_heading(self):
        payload, _, _ = self.localized_case_fixture()
        tree = ET.fromstring(payload["formxml"])
        tree.find(".//section[@name='case_location']/labels/label").set(
            "description", "Unapproved English-only heading"
        )
        expected = ET.tostring(tree, encoding="unicode")
        row = {"formid": payload["formid"], "formxml": setup.case_form_english_projection(expected)}
        api = MagicMock()
        with self.assertRaisesRegex(setup.GateError, "approved shared bilingual heading"):
            setup.localized_case_form_readback(api, row, expected)
        api.get.assert_not_called()

    def test_case_localization_rejects_an_unrequested_section_translation(self):
        payload, row, full = self.localized_case_fixture()
        tree = ET.fromstring(full["formxml"])
        labels = tree.find(".//section[@name='case_location']/labels")
        ET.SubElement(labels, "label", description="Unreviewed translation", languagecode="1025")
        api = MagicMock()
        api.get.return_value = {**full, "formxml": ET.tostring(tree, encoding="unicode")}
        with self.assertRaisesRegex(setup.GateError, "source form"):
            setup.localized_case_form_readback(api, row, payload["formxml"])
        api.request.assert_not_called()

    def test_inherited_localized_labels_must_match_the_native_source_view(self):
        payload, row, full = self.localized_case_fixture()
        tree = ET.fromstring(payload["formxml"])
        labels = ET.SubElement(tree.find(".//section[@name='native']"), "labels")
        ET.SubElement(labels, "label", description="Authored native caption", languagecode="1033")
        expected = ET.tostring(tree, encoding="unicode")
        row.update(formxml=setup.case_form_english_projection(expected), _ancestorformid_value=uid("source"))
        tree.find(".//section[@name='native']/labels/label").set("description", "Native localized fallback")
        full["formxml"] = ET.tostring(tree, encoding="unicode")
        source = {
            "formid": uid("source"), "formidunique": uid("source-layer"),
            "@odata.etag": 'W/"20"', "componentstate": 0,
            "formxml": '<form><section name="native"><labels><label description="Authored native caption" '
                       'languagecode="1033"/></labels></section></form>',
        }
        source_full = {**source, "formxml": source["formxml"].replace(
            "Authored native caption", "Native localized fallback"
        )}
        api = MagicMock()
        api.get.side_effect = [full, source, source_full]
        proof = setup.localized_case_form_readback(api, row, expected)
        self.assertEqual(proof["inherited_labels_verified"], 1)
        source_full["formxml"] = source_full["formxml"].replace("Native localized fallback", "Different source")
        api.get.side_effect = [full, source, source_full]
        with self.assertRaisesRegex(setup.GateError, "non-source translation"):
            setup.localized_case_form_readback(api, row, expected)
        api.request.assert_not_called()

    def test_find_cases_uses_customer_relationship_not_an_owner_filtered_entity_list(self):
        workspace = self.plan["workspace"]
        row = next(row for row in workspace["records"] if row["key"] == "case_lookup")
        self.assertEqual(row["payload"]["msdyn_pagetype"], 509180001)
        parameters = {row["name"]: row["value"] for row in workspace["parameters"] if row["tab"] == workspace["ids"]["case_lookup"]}
        self.assertEqual(parameters["entityName"], "{customerEntityName}")
        self.assertEqual(parameters["entityId"], "{customerRecordId}")
        self.assertEqual(parameters["validateRecord"], "true")
        self.assertIn(workspace["ids"]["contact_form"], parameters["formId"])
        self.assertNotIn("viewId", parameters)

    def test_voice_case_route_reads_the_persisted_conversation_link_without_writing_it(self):
        workspace = self.plan["workspace"]
        parameter = next(row for row in workspace["parameters"] if row["tab"] == workspace["ids"]["voice_case"]
                         and row["name"] == "entityId")
        self.assertEqual(parameter["value"], "{" + setup.CONVERSATION_CASE_SLUG + "}")
        self.assertIn("activityid eq {LiveWorkItemId}", parameter["value"])
        self.assertNotIn("CaseNumber", parameter["value"])
        row = next(row for row in workspace["records"] if row["key"] == "macro_open_case")
        actions = json.loads(row["payload"]["clientdata"])["properties"]["definition"]["actions"]
        guard = actions["Condition_conversation_context"]
        self.assertEqual(guard["expression"], {"contains": ["${LiveWorkItemId}", "-"]})
        self.assertIn("${" + setup.CONVERSATION_CASE_SLUG + "}",
                      json.dumps(guard["actions"], ensure_ascii=False))
        self.assertNotIn("Link_record_to_conversation", row["payload"]["clientdata"])
        self.assertIn("${caseId}", json.dumps(guard["else"], ensure_ascii=False))

    def test_new_case_templates_use_native_customer_mapping_without_unsafe_name_json_or_array_order(self):
        workspace = self.plan["workspace"]
        for key in ("new_case", "new_case_customer"):
            parameters = {row["name"]: row["value"] for row in workspace["parameters"] if row["tab"] == workspace["ids"][key]}
            self.assertEqual(parameters["entityName"], "incident")
            self.assertNotIn("entityId", parameters)
            self.assertNotIn("validateRecord", parameters)
            self.assertEqual(parameters["data"], "{}")
            if key == "new_case_customer":
                self.assertEqual(json.loads(parameters["createFromEntity"]),
                                 {"entityType": "{customerEntityName}", "id": "{customerRecordId}"})
            else:
                self.assertNotIn("createFromEntity", parameters)
        row = next(row for row in workspace["records"] if row["key"] == "macro_new_case")
        self.assertNotIn("Custom_Array", row["payload"]["clientdata"])
        self.assertNotIn("${customerName}", row["payload"]["clientdata"])

    def test_contact_only_pins_and_macro_leave_accounts_on_defaults(self):
        pins = [item for item in self.plan["workspace"]["parameters"]
                if item["name"] == "formId" and item["key"].startswith(("voice_customer", "case_customer"))]
        self.assertEqual(len(pins), 2)
        for pin in pins:
            self.assertIn("objecttypecode eq '{", pin["value"])
            self.assertIn(self.plan["workspace"]["ids"]["contact_form"], pin["value"])
        item = next(row for row in self.plan["workspace"]["records"] if row["key"] == "macro_review_customer")
        actions = json.loads(item["payload"]["clientdata"])["properties"]["definition"]["actions"]
        linked = actions["Condition_customer_linked"]
        contact = linked["actions"]["Condition_customer_is_contact"]
        self.assertIn("FormId", contact["actions"]["Open_customer"]["inputs"])
        self.assertNotIn("FormId", contact["else"]["actions"]["Open_customer"]["inputs"])
        self.assertEqual(linked["else"]["actions"]["Find_customer"]["inputs"], {"EntityName": "contact"})

    def test_macro_actions_are_bounded_and_do_not_automatically_save_or_resolve(self):
        found = set()
        def walk(actions):
            for item in actions.values():
                found.add(item["type"])
                if item["type"] == "If":
                    walk(item["actions"])
                    walk(item["else"]["actions"])
        for row in self.plan["workspace"]["records"]:
            if row["table"] == "workflows":
                document = json.loads(row["payload"]["clientdata"])
                self.assertEqual(document["subcategory"], "CDSClientAutomation")
                walk(document["properties"]["definition"]["actions"])
        self.assertEqual(found, setup.NATIVE_ACTIONS | {"If"})
        with self.assertRaises(setup.GateError):
            setup.action("Send_Email")

    def test_native_custom_array_input_order_is_preserved_in_clientdata(self):
        def walk(actions):
            for action in actions.values():
                if action["type"] == "If":
                    walk(action["actions"])
                    walk(action["else"]["actions"])
                elif "Custom_Array" in action["inputs"]:
                    self.assertEqual(list(action["inputs"])[-1], "Custom_Array")
        for row in self.plan["workspace"]["records"]:
            if row["table"] == "workflows":
                document = json.loads(row["payload"]["clientdata"])
                walk(document["properties"]["definition"]["actions"])
        self.assertFalse(setup.content_equal("clientdata", '{"inputs":{"a":1,"b":2}}', '{"inputs":{"b":2,"a":1}}'))

    def test_bilingual_knowledge_covers_exact_canonical_registry(self):
        knowledge = self.plan["knowledge"]
        self.assertEqual((knowledge["article_count"], knowledge["topic_count"], knowledge["source_count"]), (4, 12, 3))
        self.assertEqual([topic for group in knowledge["groups"] for topic in group["topics"]], list(range(1, 13)))
        self.assertEqual(set(source for group in knowledge["groups"] for source in group["sources"]), set(knowledge["registry"]))
        for group in knowledge["groups"]:
            self.assertEqual(set(group["versions"]), {"en-US", "ar-SA"})
            for language, version in group["versions"].items():
                self.assertIn('dir="rtl"' if language == "ar-SA" else 'dir="ltr"', version["content"])
                for source in group["sources"]:
                    self.assertIn(knowledge["registry"][source]["url"], version["content"])
                self.assertLessEqual(len(version["description"]), 155)
                self.assertLessEqual(len(version["keywords"]), 4000)

    def test_canonical_changes_require_review_not_stale_translation(self):
        canonical, arabic = knowledge_fixture()
        with self.assertRaisesRegex(setup.GateError, "Canonical knowledge changed"):
            setup.render_knowledge(config(), self.plan["native"]["locales"],
                                   canonical=canonical + "\nchanged\n", arabic=arabic)

    def test_wrapped_bullet_qualifications_are_not_dropped_from_knowledge(self):
        rendered = setup.knowledge_html("- Requirement\n  only where applicable.\n- Other requirement", {})
        self.assertEqual(rendered, "<ul><li>Requirement only where applicable.</li><li>Other requirement</li></ul>")

    def test_source_registry_heading_keeps_registry_text_out_of_the_last_topic_body(self):
        knowledge = setup.render_knowledge(config(), self.plan["native"]["locales"],
                                           *knowledge_fixture("## Source registry"))
        self.assertEqual(knowledge["topic_count"], 12)
        for language, heading in (("en-US", "Technical topic 12"), ("ar-SA", "موضوع تقني 12")):
            content = knowledge["groups"][-1]["versions"][language]["content"]
            start = content.index("<h2>12. " + heading + "</h2>")
            end = content.index("<h2>", start + 1)
            body = html.unescape(content[start:end])
            with self.subTest(language=language):
                self.assertIn('href="https://example.org/reference/3"', content[start:end])
                self.assertNotIn("[S", body)
                self.assertNotIn("Source registry", body)
                self.assertNotIn('"Reference', body)

    def test_registry_after_the_last_topic_without_a_heading_is_rejected(self):
        canonical, arabic = knowledge_fixture(boundary=None)
        self.assertEqual(len(setup.parse_topics(canonical)), 12)
        self.assertIn("[S01]: https://example.org/reference/1", setup.parse_topics(canonical)[12])
        with self.assertRaisesRegex(setup.GateError, "English topic 12 contains source-registry lines.*## Source registry"):
            setup.render_knowledge(config(), self.plan["native"]["locales"], canonical, arabic)

    def test_ids_are_stable_but_bound_to_org_namespace_and_prefix(self):
        first = setup.owned_id(config(), "profile")
        self.assertEqual(first, setup.owned_id(config(), "profile"))
        for key, value in (("namespace", "another-deployment"), ("prefix", "another_rep"),
                           ("org_url", "https://other.crm.dynamics.com")):
            cfg = config()
            cfg[key] = value
            self.assertNotEqual(first, setup.owned_id(cfg, "profile"))

    def test_config_rejects_placeholders_duplicates_ms_names_and_unknown_writes(self):
        for key, value in (("org_url", "https://example.invalid"), ("namespace", "<NEW_UUID>"),
                           ("prefix", "msdyn_default"), ("representative_ids", []),
                           ("workstream_ids", [uid("w"), uid("w")]), ("approvals", {"publish_all": True}),
                           ("extra_patch", {"statecode": 3})):
            cfg = config()
            cfg[key] = value
            with self.subTest(key=key), self.assertRaises((ValueError, setup.GateError)):
                setup.validate_config(cfg)

    def test_namespace_rejects_environment_record_ids(self):
        cfg = config()
        cfg["namespace"] = uid("environment_record")
        with self.assertRaises(setup.GateError):
            setup.validate_config(cfg)

    def test_existing_configurations_normalize_exactly_as_before_the_new_options(self):
        original = config()
        for later in ("setup_representative", "representative_knowledge_visibility"):
            self.assertNotIn(later, original["approvals"])
        self.assertNotIn("representative_setup", original)
        self.assertEqual(setup.validate_config(original), original)
        extended = setup.validate_config({**original, "approvals": {
            **original["approvals"], "setup_representative": True,
        }})
        self.assertTrue(extended["approvals"]["setup_representative"])
        self.assertNotIn("representative_knowledge_visibility", extended["approvals"])

    def test_representative_setup_must_name_an_approved_representative_with_exact_fields(self):
        selected = {"user_id": uid("representative"), "entra_object_id": uid("representative-entra"),
                    "queue_id": uid("fallback-queue"), "capacity_profile_id": uid("inbound-profile")}
        self.assertEqual(setup.validate_config({**config(), "representative_setup": selected})["representative_setup"],
                         selected)
        for change in ({"user_id": uid("someone-else")}, {"extra": uid("x")}, {"queue_id": "<FALLBACK_QUEUE_ID>"}):
            with self.subTest(change=list(change)), self.assertRaises((ValueError, setup.GateError)):
                setup.validate_config({**config(), "representative_setup": {**selected, **change}})

    def test_ambiguity_is_always_an_error(self):
        for rows in ([], [{"id": 1}, {"id": 2}]):
            with self.assertRaisesRegex(setup.GateError, "exactly one"):
                setup.one(rows, "native source")

    def test_profile_memberships_are_separately_paginated_not_truncated_expands(self):
        api = MagicMock()
        identifier = uid("profile_inventory")
        api.rows.side_effect = [
            [{"msdyn_appconfigurationid": identifier}],
            [{"systemuserid": uid("first_user")}, {"systemuserid": uid("later_page_user")}],
        ]
        profiles = setup.all_profiles(api)
        self.assertNotIn("$expand", api.rows.call_args_list[0].args[1])
        self.assertEqual(api.rows.call_args_list[1].args[0],
                         f"msdyn_appconfigurations({identifier})/" + setup.PROFILE_USERS)
        self.assertEqual(len(profiles[0][setup.PROFILE_USERS]), 2)


class OperatorTests(unittest.TestCase):
    def setUp(self):
        self.plan = plan_fixture()
        self.journal = MemoryJournal()
        self.api = FakeAPI(self.plan)
        self.operator = setup.Operator(self.api, self.plan, self.journal, wait_seconds=0, poll_seconds=1,
                                       copilot_wait_seconds=0)

    def complete(self):
        self.operator.provision_language()
        self.operator.provision_workspace()
        self.operator.provision_knowledge()
        self.operator.provision_copilot()
        self.operator.bind()
        self.operator.verify()

    def test_workspace_without_knowledge_or_copilot_never_changes_those_modules(self):
        cfg = self.plan["config"]
        cfg.update(enable_knowledge=False, enable_copilot=False)
        self.plan["workspace"] = setup.render_workspace(cfg, self.plan["native"])
        self.plan["knowledge"] = setup.render_knowledge(cfg, {})
        self.plan["native"]["shared"] = {}
        self.api = FakeAPI(self.plan)
        self.operator = setup.Operator(self.api, self.plan, self.journal, wait_seconds=0, poll_seconds=1,
                                       copilot_wait_seconds=0)
        self.complete()
        excluded = {"knowledgearticles", "categories", "CreateKnowledgeArticleTranslation",
                    "FullTextSearchKnowledgeArticle", *(row[0] for row in setup.SHARED_TABLES.values())}
        self.assertFalse(any(row["path"].split("(")[0] in excluded for row in self.api.writes))
        self.assertEqual(self.plan["knowledge"]["article_count"], 0)
        self.assertFalse(any(row["payload"]["msdyn_enabled"] for row in self.plan["workspace"]["records"]
                             if row["table"] == "msdyn_appcopilotconfigurations"))
        self.assertEqual([row["key"] for row in self.plan["workspace"]["records"]
                          if row["table"] == "msdyn_panetabconfigurations"], ["scripts_tool"])
        self.assertTrue(self.journal.state["stages"]["verify"])

    def test_complete_recipe_is_verified_and_idempotent_without_republication(self):
        self.complete()
        mutations = len([row for row in self.api.writes if row["path"] != "FullTextSearchKnowledgeArticle"])
        self.complete()
        self.assertEqual(mutations, len([row for row in self.api.writes if row["path"] != "FullTextSearchKnowledgeArticle"]))
        self.assertEqual(sum(row["path"] == "AddAppComponents" for row in self.api.writes), 1)
        self.assertEqual(sum(row["path"] == "PublishXml" for row in self.api.writes), 1)
        self.assertEqual(sum(row["path"] == "CreateKnowledgeArticleTranslation" for row in self.api.writes),
                         len(self.plan["knowledge"]["groups"]))
        self.assertTrue(self.journal.state["stages"]["verify"])
        self.assertEqual(len(self.journal.state["native_roots"]), len(self.plan["knowledge"]["groups"]))
        self.assertTrue(all(row["path"] not in ("PublishAllXml", "ProvisionLanguage") for row in self.api.writes))
        self.assertFalse(any(re.match(r"(contacts|incidents|usersettingscollection)\(", row["path"]) for row in self.api.writes))
        memberships = setup.profile_memberships(setup.all_profiles(self.api))
        self.assertEqual(memberships[uid("other_app_profile")], config()["representative_ids"])
        self.assertEqual(memberships[uid("source_profile")], [uid("unapproved")])
        added = next(row["body"]["Components"] for row in self.api.writes if row["path"] == "AddAppComponents")
        self.assertEqual({row["formid"] for row in added},
                         {self.plan["workspace"]["ids"][key] for key in ("contact_form", "case_form")})
        for request in self.api.writes:
            if request["path"] == "FullTextSearchKnowledgeArticle":
                self.assertEqual(request["headers"], {"MSCRMCallerID": config()["representative_ids"][0]})

    def test_native_search_uses_approved_localized_business_titles(self):
        self.complete()
        with patch.object(self.api, "request", wraps=self.api.request) as request:
            self.operator.verify_search(record=False)
        searches = [call.kwargs["body"]["SearchText"] for call in request.call_args_list
                    if call.args[0] == "FullTextSearchKnowledgeArticle"]
        expected = [group["versions"][language]["title"].removeprefix(self.plan["config"]["prefix"] + " | ")
                    for group in self.plan["knowledge"]["groups"] for language in ("en-US", "ar-SA")]
        self.assertEqual(searches, expected)

    def test_native_search_covers_more_groups_than_the_old_fixed_term_list(self):
        self.operator.verify_knowledge_visibility = MagicMock()
        original = self.plan["knowledge"]["groups"][0]
        self.plan["knowledge"]["groups"] = []
        found = []
        for index in range(1, 15):
            group = {**copy.deepcopy(original), "index": index, "number": f"KB-{index}"}
            self.plan["knowledge"]["groups"].append(group)
            for suffix, language in (("en", "en-US"), ("ar", "ar-SA")):
                identifier = uid(f"search-{index}-{suffix}")
                self.journal.state["owned"][f"article_{index}_{suffix}"] = {"id": identifier}
                found.append({"knowledgearticleid": identifier, "articlepublicnumber": group["number"],
                              "_languagelocaleid_value": self.plan["knowledge"]["locales"][language]})
        with patch.object(self.api, "request", return_value={"value": found}) as request:
            self.operator.verify_search(record=False)
        self.assertEqual(request.call_count, 28)

    def test_native_search_rejects_wrong_article_id_even_with_matching_number_and_locale(self):
        self.complete()
        original = self.api.request

        def wrong_id(path, **kwargs):
            result = original(path, **kwargs)
            if path == "FullTextSearchKnowledgeArticle":
                result["value"] = [{**row, "knowledgearticleid": uid("unowned-result")} for row in result["value"]]
            return result

        with patch.object(self.api, "request", side_effect=wrong_id), \
                self.assertRaisesRegex(setup.GateError, "full-text indexing"):
            self.operator.verify_search(record=False)

    def test_unrecorded_profile_channel_provider_is_rejected_even_without_a_baseline_provider(self):
        self.complete()
        key = ("msdyn_appconfigurations", self.plan["workspace"]["ids"]["profile"],
               "msdyn_appconfig_msdyn_channelprovider")
        self.api.links.setdefault(key, set()).add(uid("unrecorded-channel-provider"))
        with self.assertRaisesRegex(setup.GateError, "Unexpected association"):
            self.operator.verify_workspace()

    def test_external_only_knowledge_filter_blocks_ui_readiness_without_changing_shared_defaults(self):
        shared = self.api.data["msdyn_knowledgesearchfilters"][uid("visibility")]
        shared["msdyn_defaultvalues"] = '["0"]'
        before = copy.deepcopy(shared)
        with self.assertRaisesRegex(setup.GateError, "excludes internal"):
            self.operator.verify_search()
        self.assertEqual(shared, before)
        self.assertEqual(self.api.writes, [])


    def test_only_the_approved_representatives_personal_visibility_can_override_external_default(self):
        self.api.data["msdyn_knowledgesearchfilters"][uid("visibility")]["msdyn_defaultvalues"] = '["0"]'
        personal = {
            "msdyn_entityname": "knowledgearticle", "msdyn_attributename": "isinternal",
            "msdyn_enabled": True, "msdyn_preferredvalues": '["1","0"]',
            "_ownerid_value": uid("unapproved"),
        }
        self.api.data["msdyn_knowledgepersonalfilters"] = {uid("personal_visibility"): personal}
        with self.assertRaisesRegex(setup.GateError, "excludes internal"):
            self.operator.verify_knowledge_visibility()
        personal["_ownerid_value"] = config()["representative_ids"][0]
        self.operator.verify_knowledge_visibility()
        self.assertEqual(self.api.writes, [])
        self.assertEqual(self.api.data["msdyn_knowledgesearchfilters"][uid("visibility")]["msdyn_defaultvalues"], '["0"]')
        self.api.data["msdyn_knowledgepersonalfilters"][uid("duplicate_visibility")] = copy.deepcopy(personal)
        with self.assertRaisesRegex(setup.GateError, "Ambiguous personal"):
            self.operator.verify_knowledge_visibility()

    def test_owned_drift_and_unrecorded_records_are_not_overwritten(self):
        item = next(row for row in self.plan["workspace"]["records"] if row["key"] == "voice_customer")
        self.operator.create(item)
        self.api.data[item["table"]][item["id"]]["msdyn_title"] = "Out-of-band edit"
        count = len(self.api.writes)
        with self.assertRaisesRegex(setup.GateError, "drift"):
            self.operator.create(item)
        self.assertEqual(len(self.api.writes), count)
        self.journal.state["owned"].clear()
        with self.assertRaisesRegex(setup.GateError, "without an ownership receipt"):
            self.operator.create(item)

    def test_every_patch_is_native_etag_guarded(self):
        self.complete()
        for request in self.api.writes:
            if request["method"] == "PATCH":
                self.assertRegex(request["headers"]["If-Match"], r'^W/"\d+"$')
                self.assertNotIn("msdyn_systemstatus", request["body"])
        for bad in (None, "*", "123", 'W/"bad"'):
            with self.assertRaises(setup.GateError):
                setup.etag({"@odata.etag": bad})

    def test_language_gate_precedes_creating_arabic_scripts(self):
        self.api.languages = [1033]
        with self.assertRaisesRegex(setup.GateError, "Arabic 1025"):
            self.operator.provision_workspace()
        self.assertEqual(self.api.writes, [])
        self.operator.provision_language()
        self.assertEqual([row["path"] for row in self.api.writes], ["ProvisionLanguageAsync"])
        self.assertEqual(self.api.writes[0]["body"], {"Language": 1025})
        self.operator.provision_language()
        self.assertEqual(len(self.api.writes), 1)
        self.assertEqual(self.api.get(setup.record_path("usersettingscollection", uid("representative")))["uilanguageid"], 1033)

    def test_failed_language_job_does_not_create_scripts_or_disable_activity_feeds(self):
        self.api.languages, self.api.job_status = [1033], 31
        with self.assertRaisesRegex(setup.GateError, "failed/cancelled"):
            self.operator.provision_language()
        self.assertEqual([row["path"] for row in self.api.writes], ["ProvisionLanguageAsync"])
        self.assertIsNotNone(self.journal.state["language_job"])

    def test_active_conversations_block_workspace_and_bindings(self):
        self.api.active = True
        with self.assertRaisesRegex(setup.GateError, "wrap-up"):
            self.operator.provision_workspace()
        self.assertEqual(self.api.writes, [])

    def test_external_entitlement_gate_stops_before_workspace_mutations(self):
        self.plan["native"]["shared"]["copilot"]["msdyn_copilotenabled"] = False
        with self.assertRaisesRegex(setup.GateError, "unresolved native entitlement"):
            self.operator.provision_workspace()
        self.assertEqual(self.api.writes, [])

    def test_existing_template_parameters_are_owned_by_parent_creation_not_blind_name_adoption(self):
        tab = next(row for row in self.plan["workspace"]["records"] if row["key"] == "voice_customer")
        self.operator.create(tab)
        generated = self.journal.state["generated_parameters"][tab["id"]]
        self.assertEqual(len(generated), 4)
        child = generated[0]
        self.api.touch(self.api.data["msdyn_templateparameters"][child["msdyn_templateparameterid"]])
        with self.assertRaisesRegex(setup.GateError, "drifted"):
            self.operator.sync_parameters()

    def test_unknown_create_outcome_is_recovered_by_readback_not_replayed(self):
        item = self.plan["workspace"]["records"][0]
        original_finish = self.operator.finish
        with patch.object(self.operator, "finish", side_effect=RuntimeError("crash after commit")):
            with self.assertRaises(RuntimeError):
                self.operator.create(item)
        self.assertEqual(self.journal.state["pending"]["kind"], "create")
        self.assertNotIn(item["key"], self.journal.state["owned"])
        before = len(self.api.writes)
        self.operator.recover()
        self.assertEqual(before, len(self.api.writes))
        self.assertIsNone(self.journal.state["pending"])
        self.assertIn(item["key"], self.journal.state["owned"])
        self.assertIsNotNone(original_finish)

    def test_explicit_recovery_of_failed_create_confirms_absence(self):
        self.api.fail_next = 403
        item = self.plan["workspace"]["records"][0]
        with self.assertRaisesRegex(setup.GateError, "HTTP 403"):
            self.operator.create(item)
        with self.assertRaisesRegex(setup.GateError, "unconfirmed"):
            self.operator.begin({"kind": "another"})
        self.operator.recover()
        self.assertIsNone(self.journal.state["pending"])
        self.operator.create(item)
        self.assertIn(item["key"], self.journal.state["owned"])

    def test_failed_create_cannot_adopt_a_subsequent_matching_external_record(self):
        item = self.plan["workspace"]["records"][0]
        self.api.fail_next = 403
        with self.assertRaises(setup.GateError):
            self.operator.create(item)
        self.api.request(item["table"], method="POST", body=item["payload"])
        with self.assertRaisesRegex(setup.GateError, "collides"):
            self.operator.recover()
        self.assertNotIn(item["key"], self.journal.state["owned"])

    def test_rejected_owned_patch_cannot_adopt_another_writers_matching_value(self):
        item = next(row for row in self.plan["workspace"]["records"] if row["table"] == "workflows")
        self.operator.create(item)
        self.api.fail_next = 412
        with self.assertRaises(setup.GateError):
            self.operator.patch_owned(item["key"], {"statecode": 1, "statuscode": 2})
        row = self.api.data["workflows"][item["id"]]
        row.update(statecode=1, statuscode=2)
        self.api.touch(row)
        with self.assertRaisesRegex(setup.GateError, "another writer"):
            self.operator.recover()
        self.assertEqual(self.journal.state["owned"][item["key"]]["expected"]["statecode"], 0)

    def test_shared_setting_lost_response_requires_native_audit_not_ownership_guess(self):
        row = self.operator.shared_snapshot("copilot")
        operation = {
            "kind": "shared_patch", "family": "copilot", "path": setup.record_path("msdyn_agentcopilotsettings", uid("copilot")),
            "method": "PATCH", "body": {"msdyn_knowledgearticlesourceenabled": True},
            "before": {"msdyn_knowledgearticlesourceenabled": False}, "headers": {"If-Match": setup.etag(row)},
        }
        self.operator.begin(operation)
        self.api.request(operation["path"], method="PATCH", body=operation["body"], headers=operation["headers"])
        with self.assertRaisesRegex(setup.GateError, "confirmed native response"):
            self.operator.recover()
        self.assertEqual(self.journal.state["shared"], {})

    def test_unplanned_child_steps_are_not_silently_counted_as_a_verified_script(self):
        self.operator.provision_workspace()
        self.api.request("msdyn_productivityagentscriptsteps", method="POST", body={
            "msdyn_productivityagentscriptstepid": uid("unplanned_step"), "msdyn_name": "Unplanned step",
            "msdyn_agentscriptid@odata.bind": "/msdyn_productivityagentscripts(" + self.plan["workspace"]["ids"]["voice_script_en"] + ")",
            "msdyn_actiontype": 192350000, "msdyn_order": 99,
        })
        with self.assertRaisesRegex(setup.GateError, "Unexpected child"):
            self.operator.verify_workspace()

    def test_unplanned_tab_parameters_are_not_implicitly_adopted(self):
        self.operator.provision_workspace()
        self.api.request("msdyn_templateparameters", method="POST", body={
            "msdyn_templateparameterid": uid("unplanned_parameter"), "msdyn_name": "unplanned",
            "msdyn_value": "unexpected", "msdyn_applicationtabtemplateid@odata.bind":
                "/msdyn_applicationtabtemplates(" + self.plan["workspace"]["ids"]["voice_customer"] + ")",
        })
        with self.assertRaisesRegex(setup.GateError, "Unexpected template parameter"):
            self.operator.verify_parameters()

    def test_unplanned_workspace_associations_are_rejected(self):
        self.operator.provision_workspace()
        self.api.links[("msdyn_sessiontemplates", self.plan["workspace"]["ids"]["voice_session"],
                        "msdyn_sessiontemplate_applicationtab")].add(uid("unplanned_tab"))
        with self.assertRaisesRegex(setup.GateError, "Unexpected association"):
            self.operator.verify_workspace()

    def test_unrelated_app_draft_blocks_registration(self):
        for item in self.plan["workspace"]["records"]:
            if item["table"] == "systemforms":
                self.operator.create(item)
        self.api.draft["description"] = "Another administrator's pending edit"
        count = len(self.api.writes)
        with self.assertRaisesRegex(setup.GateError, "unrelated pending"):
            self.operator.register_form()
        self.assertEqual(count, len(self.api.writes))

    def reject_app_action(self, kind, *, removing=False, status=403):
        for item in self.plan["workspace"]["records"]:
            if item["table"] == "systemforms":
                self.operator.create(item)
        if kind == "app_remove" or removing:
            self.operator.register_form()
        run = self.operator.rollback if kind == "app_remove" or removing else self.operator.register_form
        path = {"app_add": "AddAppComponents", "app_remove": "RemoveAppComponents", "app_publish": "PublishXml"}[kind]
        request = self.api.request
        before = {}

        def reject(selected, **kwargs):
            if selected == path:
                before["state"] = copy.deepcopy(self.journal.state)
                before["state"]["pending"] = None
                if status is None:
                    raise TimeoutError("Unconfirmed native app action")
                self.api.fail_next = status
            return request(selected, **kwargs)

        error = TimeoutError if status is None else setup.GateError
        with patch.object(self.api, "request", side_effect=reject), self.assertRaises(error):
            run()
        self.assertEqual(self.journal.state["pending"]["kind"], kind)
        return run, before["state"]

    def test_known_app_rejections_recover_without_claiming_success_and_can_retry(self):
        for kind, removing in (("app_add", False), ("app_publish", False),
                               ("app_remove", False), ("app_publish", True)):
            with self.subTest(kind=kind, removing=removing):
                self.setUp()
                run, before = self.reject_app_action(kind, removing=removing)
                operation = self.journal.state["pending"]
                self.assertEqual(operation["http_status"], 403)
                self.assertEqual(operation["app_before"], self.operator.app_action_state())
                attempts = len(self.api.writes)
                self.operator.recover()
                self.assertEqual(self.journal.state, before)
                self.assertEqual(len(self.api.writes), attempts)
                self.assertEqual(self.journal.events[-1]["label"], "failed_app_action_confirmed_unapplied")
                run()
                if kind == "app_remove" or removing:
                    self.assertTrue(self.journal.state["rolled_back"])
                    self.assertFalse(self.journal.state["app_registered"])
                    self.assertFalse(self.journal.state["app_published"])
                    self.operator.published_app_ok(removing=True)
                else:
                    self.assertTrue(self.journal.state["app_registered"])
                    self.assertTrue(self.journal.state["app_published"])
                    self.operator.published_app_ok()

    def test_known_app_rejections_block_recovery_when_any_pre_action_state_drifts(self):
        dimensions = (
            "published_etag", "unpublished_etag", "published_authored", "unpublished_authored",
            "published_components", "unpublished_components", "component_etag", "draft_layer",
        )
        for kind, removing in (("app_add", False), ("app_publish", False),
                               ("app_remove", False), ("app_publish", True)):
            for dimension in dimensions:
                with self.subTest(kind=kind, removing=removing, dimension=dimension):
                    self.setUp()
                    self.reject_app_action(kind, removing=removing)
                    if dimension == "published_etag":
                        self.api.touch(self.api.app)
                    elif dimension == "unpublished_etag":
                        self.api.touch(self.api.draft)
                    elif dimension == "published_authored":
                        self.api.app["navigationtype"] = 2
                    elif dimension == "unpublished_authored":
                        self.api.draft["description"] = "Concurrent app edit"
                    elif dimension == "published_components":
                        self.api.published_components[0]["isdefault"] = False
                    elif dimension == "unpublished_components":
                        self.api.draft_components[0]["rootcomponentbehavior"] = 1
                    elif dimension == "component_etag":
                        self.api.draft_components[0]["@odata.etag"] = 'W/"999"'
                    else:
                        self.api.draft["appmoduleidunique"] = uid("concurrent_draft_layer")
                    state = copy.deepcopy(self.journal.state)
                    attempts = len(self.api.writes)
                    with self.assertRaisesRegex(setup.GateError, "state drifted"):
                        self.operator.recover()
                    self.assertEqual(self.journal.state, state)
                    self.assertEqual(len(self.api.writes), attempts)

    def test_rejected_app_action_cannot_adopt_another_writers_matching_post_state(self):
        for kind, removing in (("app_add", False), ("app_publish", False),
                               ("app_remove", False), ("app_publish", True)):
            with self.subTest(kind=kind, removing=removing):
                self.setUp()
                self.reject_app_action(kind, removing=removing)
                operation = self.journal.state["pending"]
                self.api.request(operation["path"], method="POST", body=operation["body"])
                state = copy.deepcopy(self.journal.state)
                attempts = len(self.api.writes)
                with self.assertRaisesRegex(setup.GateError, "state drifted"):
                    self.operator.recover()
                self.assertEqual(self.journal.state, state)
                self.assertEqual(len(self.api.writes), attempts)

    def test_uncertain_app_outcomes_are_not_cleared_just_because_pre_state_matches(self):
        for kind, removing in (("app_add", False), ("app_publish", False),
                               ("app_remove", False), ("app_publish", True)):
            for status in (None, 500):
                with self.subTest(kind=kind, removing=removing, status=status):
                    self.setUp()
                    self.reject_app_action(kind, removing=removing, status=status)
                    state = copy.deepcopy(self.journal.state)
                    self.assertEqual(state["pending"]["app_before"], self.operator.app_action_state())
                    attempts = len(self.api.writes)
                    with self.assertRaises(setup.GateError):
                        self.operator.recover()
                    self.assertEqual(self.journal.state, state)
                    self.assertEqual(len(self.api.writes), attempts)

    def test_known_app_rejections_without_pre_action_receipts_stay_blocked(self):
        for kind in ("app_add", "app_remove", "app_publish"):
            with self.subTest(kind=kind):
                self.setUp()
                self.reject_app_action(kind)
                del self.journal.state["pending"]["app_before"]
                state = copy.deepcopy(self.journal.state)
                with self.assertRaisesRegex(setup.GateError, "no recorded pre-action state"):
                    self.operator.recover()
                self.assertEqual(self.journal.state, state)

    def test_app_normalization_ignores_only_native_publication_metadata(self):
        baseline = self.plan["native"]["app"]
        changed = copy.deepcopy(baseline)
        descriptor = json.loads(changed["descriptor"])
        descriptor["appInfo"].update({"VersionNumber": 101, "Status": 0, "SolutionId": uid("generated")})
        descriptor["publisherInfo"]["Id"] = uid("other_publisher_layer")
        changed["descriptor"] = json.dumps(descriptor)
        self.assertEqual(setup.app_authored(baseline), setup.app_authored(changed))
        changed["navigationtype"] = 9
        self.assertNotEqual(setup.app_authored(baseline), setup.app_authored(changed))

    def test_shared_source_not_ready_blocks_success_and_binding(self):
        self.operator.provision_knowledge()
        self.api.ready_automatically = False
        with self.assertRaisesRegex(setup.GateError, "isn't Ready after"):
            self.operator.provision_copilot()
        self.assertFalse(self.journal.state["stages"].get("copilot", False))
        self.assertFalse(any("msdyn_systemstatus" in (row["body"] or {}) for row in self.api.writes if row["method"] == "PATCH"))
        with self.assertRaisesRegex(setup.GateError, "must verify"):
            self.operator.bind()

    def test_live_case_query_is_distinct_and_shared_email_translation_access_flags_preserved(self):
        self.operator.provision_knowledge()
        self.operator.provision_copilot()
        for family, baseline in self.plan["native"]["shared"].items():
            after = self.operator.shared_snapshot(family)
            for field, value in baseline.items():
                if type(value) is bool and field not in setup.SUMMARY_PATCHES[family]:
                    self.assertEqual(after[field], value)
        ready = next(event for event in self.journal.events if event["label"] == "native_copilot_ready")
        self.assertFalse(ready["evidence"]["live_case_querying_configured"])

    def test_published_article_edits_and_unrelated_knowledge_changes_stop(self):
        self.operator.provision_knowledge()
        key = "article_1_en"
        row = self.api.data["knowledgearticles"][self.journal.state["owned"][key]["id"]]
        row["title"] = "Someone else's correction"
        with self.assertRaisesRegex(setup.GateError, "drift"):
            self.operator.publish_article(key, self.plan["knowledge"]["groups"][0], "en-US")
        row["title"] = self.journal.state["owned"][key]["expected"]["title"]
        self.api.data["knowledgearticles"][uid("unrelated_article")]["statecode"] = 0
        with self.assertRaisesRegex(setup.GateError, "Unrelated article"):
            self.operator.verify_knowledge()

    def test_scoped_rollback_restores_bindings_archives_owned_articles_and_retains_evidence(self):
        self.complete()
        inventory = self.operator.rollback_inventory()
        self.assertEqual(len(inventory["archive_only_owned_published_articles"]), self.plan["knowledge"]["article_count"])
        self.operator.rollback()
        self.assertTrue(self.journal.state["rolled_back"])
        self.assertFalse(self.journal.state["bound"])
        self.operator.verify_bindings(bound=False)
        self.assertEqual(self.api.published_components, self.plan["native"]["app_components"])
        self.assertEqual(sum(row["path"] == "RemoveAppComponents" for row in self.api.writes), 1)
        self.assertEqual(self.api.data["knowledgearticles"][uid("unrelated_article")]["statecode"], 3)
        for item in self.journal.state["owned"].values():
            row = self.api.data[item["table"]][item["id"]]
            if item["table"] == "knowledgearticles":
                self.assertEqual((row["statecode"], row["statuscode"]), (5, 12))
            if item["table"] == "workflows":
                self.assertEqual((row["statecode"], row["statuscode"]), (0, 1))
        count = len(self.api.writes)
        self.operator.rollback()
        self.assertEqual(len(self.api.writes), count)
        self.assertIn(1025, self.api.languages)

    def test_partial_language_only_rollback_is_repeatable_and_keeps_shared_language(self):
        self.api.languages = [1033]
        self.operator.provision_language()
        self.operator.rollback()
        self.operator.rollback()
        self.assertTrue(self.journal.state["rolled_back"])
        self.assertIn(1025, self.api.languages)
        self.assertEqual([row["path"] for row in self.api.writes], ["ProvisionLanguageAsync"])

    def writes_to(self, path, method=None):
        return [row for row in self.api.writes if row["path"] == path and method in (None, row["method"])]

    def test_case_form_readback_that_passes_later_is_recovered_on_the_same_journal(self):
        case = self.plan["workspace"]["ids"]["case_form"]
        self.api.stale_reads[case] = 1
        with self.assertRaisesRegex(setup.GateError, "beyond the four field-label projections.*run recover"):
            self.operator.provision_workspace()
        mismatch = [event["evidence"] for event in self.journal.events if event["label"] == "create_readback_mismatch"]
        self.assertEqual([(row["key"], row["id"]) for row in mismatch], [("case_form", case)])
        self.assertIn("formxml", mismatch[0]["readback"])
        self.assertEqual(self.journal.state["pending"]["item"]["key"], "case_form")
        self.operator.recover()
        self.assertIsNone(self.journal.state["pending"])
        self.operator.provision_workspace()
        self.assertEqual(len(self.writes_to("systemforms", "POST")), 2)
        self.assertTrue(self.journal.state["stages"]["workspace"])

    def test_new_profile_with_the_native_app_default_gets_one_guarded_correction(self):
        self.api.profile_app_default = setup.NATIVE_PROFILE_APP_DEFAULT
        requested = self.plan["native"]["profile"]["msdyn_appmoduleuniquename"]
        profile = self.plan["workspace"]["ids"]["profile"]
        self.operator.provision_workspace()
        patches = self.writes_to(f"msdyn_appconfigurations({profile})", "PATCH")
        self.assertEqual([row["body"] for row in patches], [{"msdyn_appmoduleuniquename": requested}])
        self.assertRegex(patches[0]["headers"]["If-Match"], r'^W/"\d+"$')
        self.assertEqual(self.api.data["msdyn_appconfigurations"][profile]["msdyn_appmoduleuniquename"], requested)
        self.assertEqual(self.journal.state["owned"]["profile"]["app_correction"]["status"], "applied")
        writes = len(self.api.writes)
        self.operator.provision_workspace()
        self.operator.verify_workspace()
        self.assertEqual(len(self.api.writes), writes)
        self.assertEqual(len(self.writes_to("msdyn_appconfigurations", "POST")), 1)

    def test_profile_app_correction_needs_the_exact_default_and_an_unassigned_profile(self):
        profile = self.plan["workspace"]["ids"]["profile"]
        cases = (
            ("UnreviewedApp", [], "Owned record drift: profile/msdyn_appmoduleuniquename"),
            (setup.NATIVE_PROFILE_APP_DEFAULT, [(setup.PROFILE_USERS, uid("assigned-user"))], "assigned users"),
        )
        for stored, links, message in cases:
            with self.subTest(stored=stored):
                self.setUp()
                self.api.profile_app_default, self.api.links_on_profile_create = stored, links
                with self.assertRaisesRegex(setup.GateError, message):
                    self.operator.provision_workspace()
                self.assertEqual(self.writes_to(f"msdyn_appconfigurations({profile})", "PATCH"), [])

    def test_a_rejected_profile_correction_is_never_retried(self):
        self.api.profile_app_default = setup.NATIVE_PROFILE_APP_DEFAULT
        path = "msdyn_appconfigurations(" + self.plan["workspace"]["ids"]["profile"] + ")"
        original, attempts = self.api.request, []

        def reject(target, **kwargs):
            if target == path and kwargs.get("method") == "PATCH":
                attempts.append(target)
                raise urllib.error.HTTPError("https://offline.invalid", 403, "denied", None, io.BytesIO())
            return original(target, **kwargs)

        with patch.object(self.api, "request", side_effect=reject):
            with self.assertRaisesRegex(setup.GateError, "HTTP 403.*Run recover"):
                self.operator.provision_workspace()
            self.operator.recover()
            with self.assertRaisesRegex(setup.GateError, "did not apply. Stop and ask the owner"):
                self.operator.provision_workspace()
        self.assertEqual(len(attempts), 1)
        with self.assertRaisesRegex(setup.GateError, "one guarded app correction"):
            self.operator.verify_workspace()

    def test_generated_siblings_are_owned_before_the_first_parameter_write(self):
        self.api.sibling_sync = True
        self.operator.provision_workspace()
        events = self.journal.events
        recorded = [index for index, event in enumerate(events) if event["label"] == "recorded_owned_generated_parameter"]
        first_write = next(index for index, event in enumerate(events) if event["label"] == "before_owned_patch" and
                           event["updates"]["pending"]["path"].startswith("msdyn_templateparameters("))
        generated = [row for row in self.plan["workspace"]["parameters"]
                     if row["name"] in ("entityName", "entityId", "formId", "validateRecord")]
        self.assertEqual(len(recorded), len(generated))
        self.assertLess(max(recorded), first_write)
        created = [row["body"]["msdyn_templateparameterid"] for row in self.writes_to("msdyn_templateparameters", "POST")]
        self.assertEqual(len(created), len(set(created)))
        self.assertTrue(self.journal.state["stages"]["workspace"])
        writes = len(self.api.writes)
        self.operator.provision_workspace()
        self.assertEqual(len(self.api.writes), writes)

    def test_a_sibling_value_changed_by_someone_else_still_stops(self):
        self.api.sibling_sync = True
        tab = next(row for row in self.plan["workspace"]["records"] if row["key"] == "voice_customer")
        self.operator.create(tab)
        self.operator.record_generated_parameters()
        sibling = next(row for row in self.api.data["msdyn_templateparameters"].values()
                       if row["_msdyn_applicationtabtemplateid_value"] == tab["id"] and row["msdyn_name"] == "validateRecord")
        sibling["msdyn_value"] = "changed elsewhere"
        self.api.touch(sibling)
        with self.assertRaisesRegex(setup.GateError, "Owned record drift"):
            self.operator.sync_parameters()
        self.assertEqual(self.writes_to(setup.record_path("msdyn_templateparameters", sibling["msdyn_templateparameterid"])), [])

    def test_planned_profile_links_already_present_at_creation_are_recorded_not_posted(self):
        relation = "msdyn_appconfiguration_applicationextension"
        profile = self.plan["workspace"]["ids"]["profile"]
        self.api.links_on_profile_create = [(relation, uid("extension"))]
        self.operator.provision_workspace()
        self.assertEqual(self.writes_to(f"msdyn_appconfigurations({profile})/{relation}/$ref"), [])
        link = {"table": "msdyn_appconfigurations", "id": profile, "relation": relation,
                "target_table": "msdyn_applicationextensions", "target_id": uid("extension")}
        self.assertEqual(self.journal.state["links_present_at_creation"], {setup.digest(link): link})
        self.operator.verify_workspace()

    def test_an_unplanned_link_at_profile_creation_stops_and_is_never_removed(self):
        relation = "msdyn_appconfiguration_applicationextension"
        profile = self.plan["workspace"]["ids"]["profile"]
        self.api.links_on_profile_create = [(relation, uid("unplanned-extension"))]
        for _ in range(2):
            with self.assertRaisesRegex(setup.GateError, "aren't in the plan.*Nothing was removed"):
                self.operator.provision_workspace()
        self.assertIn("profile", self.journal.state["owned"])
        self.assertIsNone(self.journal.state["pending"])
        self.assertFalse([row for row in self.api.writes if row["method"] == "DELETE"])
        self.api.links[("msdyn_appconfigurations", profile, relation)].discard(uid("unplanned-extension"))
        self.operator.provision_workspace()
        self.assertTrue(self.journal.state["stages"]["workspace"])

    def hide_internal_articles(self, *, approved=True):
        self.api.data["msdyn_knowledgesearchfilters"][uid("visibility")]["msdyn_defaultvalues"] = '["0"]'
        self.plan["config"]["approvals"]["representative_knowledge_visibility"] = approved

    def test_knowledge_stage_creates_the_representatives_own_internal_filter(self):
        self.hide_internal_articles()
        shared = copy.deepcopy(self.api.data["msdyn_knowledgesearchfilters"])
        user = config()["representative_ids"][0]
        item = setup.visibility_filter_item(config(), user)
        self.operator.provision_knowledge()
        posts = self.writes_to("msdyn_knowledgepersonalfilters", "POST")
        self.assertEqual([row["body"] for row in posts], [{
            "msdyn_knowledgepersonalfilterid": item["id"], "msdyn_name": "Visibility",
            "msdyn_entityname": "knowledgearticle", "msdyn_attributename": "isinternal",
            "msdyn_enabled": True, "msdyn_preferredvalues": '["1"]',
        }])
        self.assertEqual(posts[0]["headers"], {"MSCRMCallerID": user})
        self.assertEqual(self.api.data["msdyn_knowledgepersonalfilters"][item["id"]]["_ownerid_value"], user)
        self.assertEqual(self.api.data["msdyn_knowledgesearchfilters"], shared)
        self.assertTrue(self.journal.state["stages"]["knowledge"])
        self.operator.provision_knowledge()
        self.assertEqual(len(self.writes_to("msdyn_knowledgepersonalfilters", "POST")), 1)

    def test_a_representative_running_the_kit_creates_its_filter_without_impersonation(self):
        self.hide_internal_articles()
        self.api.who = config()["representative_ids"][0]
        self.operator.provision_knowledge()
        self.assertEqual([row["headers"] for row in self.writes_to("msdyn_knowledgepersonalfilters", "POST")], [None])

    def test_no_internal_filter_without_approval_or_over_an_existing_personal_choice(self):
        self.hide_internal_articles(approved=False)
        with self.assertRaisesRegex(setup.GateError, "excludes internal.*approval is false"):
            self.operator.provision_knowledge()
        self.plan["config"]["approvals"]["representative_knowledge_visibility"] = True
        self.api.data["msdyn_knowledgepersonalfilters"] = {uid("own-choice"): {
            "msdyn_knowledgepersonalfilterid": uid("own-choice"), "msdyn_entityname": "knowledgearticle",
            "msdyn_attributename": "isinternal", "msdyn_enabled": True, "msdyn_preferredvalues": '["0"]',
            "_ownerid_value": config()["representative_ids"][0],
        }}
        before = copy.deepcopy(self.api.data["msdyn_knowledgepersonalfilters"])
        with self.assertRaisesRegex(setup.GateError, "excludes internal"):
            self.operator.provision_knowledge()
        self.assertEqual(self.api.data["msdyn_knowledgepersonalfilters"], before)
        self.assertEqual(self.writes_to("msdyn_knowledgepersonalfilters", "POST"), [])

    def test_a_filter_not_owned_by_the_representative_fails_closed(self):
        self.hide_internal_articles()
        self.api.honor_caller = False
        with self.assertRaisesRegex(setup.GateError, "isn't owned by representative"):
            self.operator.provision_knowledge()
        self.assertFalse(self.journal.state["stages"].get("knowledge"))

    def test_copilot_waits_for_ready_status_and_worker_success_without_forcing_either(self):
        self.operator.provision_knowledge()
        self.api.ready_automatically = False
        settings = self.api.data["msdyn_agentcopilotsettings"][uid("copilot")]
        processing = json.dumps({"tries": 1, "status": "new"})
        states = iter([{"msdyn_systemstatus": 100230003, "msdyn_dvcopilotstatus": processing},
                       {"msdyn_dvcopilotstatus": processing}, {"msdyn_dvcopilotstatus": COPILOT_SUCCESS}])
        self.operator.copilot_wait_seconds = 3600
        with patch.object(setup.time, "sleep", side_effect=lambda seconds: settings.update(next(states))):
            self.operator.provision_copilot()
        readbacks = [event["evidence"] for event in self.journal.events if event["label"] == "native_copilot_status_readback"]
        self.assertEqual([(row["systemstatus"], row["worker_status"]) for row in readbacks],
                         [(100230002, None), (100230003, "new"), (100230003, "success")])
        self.assertTrue(self.journal.state["stages"]["copilot"])
        for row in self.api.writes:
            self.assertFalse({"msdyn_systemstatus", "msdyn_dvcopilotstatus"} & set(row["body"] or {}))
            self.assertFalse(row["path"].startswith("workflows("))

    def test_copilot_timeout_names_the_admin_pages_to_check(self):
        self.operator.provision_knowledge()
        self.api.ready_automatically = False
        with self.assertRaises(setup.GateError) as raised:
            self.operator.provision_copilot()
        for text in ("isn't Ready after 0 minutes", "status 100230002", "Support experience > Productivity > Copilot settings",
                     "Customer Support > Overview > Knowledge sources", "--copilot-wait-minutes",
                     "Don't turn on the managed Check DV Copilot Sync Status Flow"):
            self.assertIn(text, str(raised.exception))
        self.assertFalse(self.journal.state["stages"].get("copilot"))

    def test_an_expired_token_during_the_readiness_poll_is_renewed_once_for_the_read(self):
        self.operator.provision_knowledge()
        self.api.ready_automatically = False
        path, original, expired, renewed = setup.record_path("msdyn_agentcopilotsettings", uid("copilot")), self.api.get, [], []

        def expire_once(target, query=None):
            if target == path and self.journal.state.get("status_checked") and not expired:
                expired.append(target)
                raise urllib.error.HTTPError("https://offline.invalid", 401, "expired", None, io.BytesIO())
            return original(target, query)

        self.operator.reconnect = lambda: renewed.append(True) or self.api
        self.operator.copilot_wait_seconds = 60
        with patch.object(self.api, "get", side_effect=expire_once), \
                patch.object(setup.time, "sleep", side_effect=lambda seconds: self.api.make_ready()):
            self.operator.provision_copilot()
        self.assertEqual((len(expired), len(renewed)), (1, 1))
        self.assertIn("dataverse_token_renewed", [event["label"] for event in self.journal.events])
        self.assertTrue(self.journal.state["stages"]["copilot"])


RESOURCE_LOOKUP = "EntityDefinitions(LogicalName='bookableresource')/ManyToOneRelationships"


def representative_config(**approvals):
    cfg = config()
    return setup.validate_config({**cfg, "representative_setup": {
        "user_id": cfg["representative_ids"][0], "entra_object_id": uid("representative-entra"),
        "queue_id": uid("fallback-queue"), "capacity_profile_id": uid("inbound-profile"),
    }, "approvals": {**cfg["approvals"], "setup_representative": True, **approvals}})


def representative_environment(api, cfg, *, roles=("Basic User",), team_roles=(), resources=(), capacity=False,
                               member=False):
    selected = cfg["representative_setup"]
    user, unit, queue, profile = (selected["user_id"], uid("business-unit"), selected["queue_id"],
                                  selected["capacity_profile_id"])
    api.data["systemusers"] = {user: {
        "systemuserid": user, "azureactivedirectoryobjectid": selected["entra_object_id"], "applicationid": None,
        "applicationiduri": None, "isdisabled": False, "islicensed": True, "accessmode": 0,
        "_businessunitid_value": unit, "fullname": "Sample Representative", "@odata.etag": 'W/"1"',
    }}
    api.data["usersettingscollection"][user]["timezonecode"] = 85
    api.data["roles"] = {uid("role-" + name): {
        "roleid": uid("role-" + name), "name": name, "_businessunitid_value": unit,
        "_parentrootroleid_value": uid("root-" + name), "@odata.etag": 'W/"2"',
    } for name in ("Basic User", *setup.REPRESENTATIVE_ROLES, "Omnichannel API access")}
    api.links[("systemusers", user, "systemuserroles_association")] = {uid("role-" + name) for name in roles}
    if team_roles:
        api.data["teams"] = {uid("team"): {"teamid": uid("team"), "teamtype": 0}}
        api.links[("systemusers", user, "teammembership_association")] = {uid("team")}
        api.links[("teams", uid("team"), "teamroles_association")] = {uid("role-" + name) for name in team_roles}
    queue_row = {field: None for field in routing.QUEUE_FIELDS.split(",")}
    queue_row.update(queueid=queue, name="Fallback voice queue", statecode=0, msdyn_isomnichannelqueue=True,
                     msdyn_queuetype=192350002, msdyn_priority=1)
    api.data["queues"] = {queue: {**queue_row, "@odata.etag": 'W/"3"'}}
    api.links[("queues", queue, routing.MEMBERS)] = {uid("other-member"), *([user] if member else [])}
    api.data["msdyn_capacityprofiles"] = {profile: {
        "msdyn_capacityprofileid": profile, "statecode": 0, "componentstate": 0, "msdyn_defaultmaxunits": 1,
        "msdyn_blockassignment": True, "msdyn_resetduration": 192350000, "@odata.etag": 'W/"4"',
    }}
    for identifier in cfg["workstream_ids"]:
        api.data["msdyn_liveworkstreams"][identifier].update(
            statecode=0, msdyn_direction=0, msdyn_streamsource=192440000,
            _msdyn_defaultqueue_value=queue if identifier == cfg["workstream_ids"][0] else uid("other-queue"))
    api.data["msdyn_liveworkstreamcapacityprofiles"] = {uid("binding"): {
        "msdyn_liveworkstreamcapacityprofileid": uid("binding"), "_msdyn_workstream_id_value": cfg["workstream_ids"][0],
        "_msdyn_capacityprofile_id_value": profile, "statecode": 0, "componentstate": 0,
    }}
    api.data["bookableresources"] = {row["bookableresourceid"]: {"_userid_value": user, "resourcetype": 3, **row}
                                     for row in resources}
    if capacity:
        resource = resources[0]["bookableresourceid"]
        api.data[routing.CAPACITY] = {uid("existing-capacity"): {
            routing.CAPACITY_ID: uid("existing-capacity"), "_msdyn_bookableresourceid_value": resource,
            "_msdyn_capacityprofileid_value": profile, "msdyn_maxunits": None, "msdyn_name": "existing",
            "statecode": 0, "statuscode": 1,
        }}
    api.metadata = {
        "EntityDefinitions(LogicalName='queue')/ManyToManyRelationships": [{
            "SchemaName": routing.MEMBERS, "Entity1LogicalName": "queue", "Entity2LogicalName": "systemuser",
            "Entity1NavigationPropertyName": routing.MEMBERS, "Entity2NavigationPropertyName": routing.MEMBERS,
            "IntersectEntityName": "queuemembership",
        }],
        "EntityDefinitions(LogicalName='msdyn_bookableresourcecapacityprofile')/ManyToOneRelationships": [{
            "ReferencingEntity": "msdyn_bookableresourcecapacityprofile", "ReferencingAttribute": attribute,
            "ReferencedEntity": entity, "ReferencedAttribute": primary, "ReferencingEntityNavigationPropertyName": attribute,
        } for attribute, entity, primary in (
            ("msdyn_bookableresourceid", "bookableresource", "bookableresourceid"),
            ("msdyn_capacityprofileid", "msdyn_capacityprofile", "msdyn_capacityprofileid"),
        )],
        RESOURCE_LOOKUP: [{"ReferencedEntity": "systemuser", "ReferencingAttribute": "userid",
                           "ReferencingEntityNavigationPropertyName": "UserId"}],
    }


class RepresentativeSetupTests(unittest.TestCase):
    def build(self, arrange=None, **environment):
        self.cfg = representative_config()
        self.plan = plan_fixture(self.cfg)
        self.api = FakeAPI(self.plan)
        representative_environment(self.api, self.cfg, **environment)
        if arrange:
            arrange(self.api)
        self.plan["representative"] = setup.plan_representative(self.api, self.cfg)
        self.journal = MemoryJournal()
        self.operator = setup.Operator(self.api, self.plan, self.journal, wait_seconds=0, poll_seconds=1,
                                       copilot_wait_seconds=0)
        return self.plan["representative"]

    def test_first_representative_gets_only_missing_roles_resource_capacity_and_queue_membership(self):
        planned = self.build()
        user, queue = uid("representative"), uid("fallback-queue")
        self.assertEqual(planned["gates"], [])
        self.assertEqual([op["kind"] for op in planned["operations"]],
                         ["role", "role", "role", "resource", "capacity", "membership"])
        summary = self.operator.provision_representative()
        self.assertEqual(summary["roles"], list(setup.REPRESENTATIVE_ROLES))
        self.assertTrue(summary["queue_member"])
        roles = [row["body"]["@odata.id"].rsplit("(", 1)[1][:-1]
                 for row in self.api.writes if row["path"] == f"systemusers({user})/systemuserroles_association/$ref"]
        self.assertEqual(sorted(roles), sorted(uid("role-" + name) for name in setup.REPRESENTATIVE_ROLES))
        resource = planned["resource_id"]
        self.assertEqual([row["body"] for row in self.api.writes if row["path"] == "bookableresources"], [{
            "bookableresourceid": resource, "name": "Sample Representative", "resourcetype": 3, "timezone": 85,
            "UserId@odata.bind": f"/systemusers({user})",
        }])
        association = next(op["item"]["id"] for op in planned["operations"] if op["kind"] == "capacity")
        self.assertEqual([row["body"] for row in self.api.writes if row["path"] == routing.CAPACITY],
                         [routing.capacity_body(association, resource, uid("inbound-profile"), None)])
        self.assertEqual([row["body"] for row in self.api.writes if row["path"] == f"queues({queue})/{routing.MEMBERS}/$ref"],
                         [routing.membership_body(self.cfg["org_url"], user)])
        self.assertEqual(({row["method"] for row in self.api.writes}, len(self.api.writes)), ({"POST"}, 6))
        self.assertTrue(self.journal.state["stages"]["representative"])
        self.operator.provision_representative()
        self.assertEqual(len(self.api.writes), 6)
        self.assertEqual(self.operator.verify_representative()["capacity_association_id"], association)

    def test_existing_roles_resource_capacity_and_membership_are_left_as_they_are(self):
        resource = {"bookableresourceid": uid("existing-resource"), "statecode": 0}
        planned = self.build(roles=("Basic User", "Omnichannel agent"),
                             team_roles=("App Profile User", "Customer Service Representative"),
                             resources=[resource], capacity=True, member=True)
        self.assertEqual((planned["gates"], planned["operations"]), ([], []))
        summary = self.operator.provision_representative()
        self.assertEqual(self.api.writes, [])
        self.assertEqual((summary["bookable_resource_id"], summary["capacity_association_id"]),
                         (uid("existing-resource"), uid("existing-capacity")))

    def test_gated_plans_write_nothing(self):
        queue, profile = uid("fallback-queue"), uid("inbound-profile")
        cases = (
            ({"resources": [{"bookableresourceid": uid("old-resource"), "statecode": 1}]}, None, "bookable resources"),
            ({"roles": ("Basic User", "Omnichannel API access")}, None, "Omnichannel API access"),
            ({}, lambda api: api.data["queues"][queue].update(msdyn_queuetype=192350000), "fallback queue"),
            ({}, lambda api: api.data["msdyn_capacityprofiles"][profile].update(componentstate=1), "capacity profile"),
            ({}, lambda api: [row.update(_msdyn_defaultqueue_value=uid("other-queue"))
                              for row in api.data["msdyn_liveworkstreams"].values()], "No selected workstream"),
        )
        for environment, arrange, fragment in cases:
            with self.subTest(fragment=fragment):
                planned = self.build(arrange, **environment)
                self.assertTrue(any(fragment in gate for gate in planned["gates"]), planned["gates"])
                with self.assertRaisesRegex(setup.GateError, "gated at planning"):
                    self.operator.provision_representative()
                self.assertEqual(self.api.writes, [])

    def test_planning_is_read_only_and_reports_representative_gates(self):
        self.build(lambda api: api.data["queues"][uid("fallback-queue")].update(statecode=1))
        with patch.object(setup, "discover", return_value=copy.deepcopy(self.plan["native"])), \
                patch.object(setup, "render_knowledge", return_value=copy.deepcopy(self.plan["knowledge"])):
            plan = setup.build_plan(self.api, self.cfg)
        self.assertEqual(self.api.writes, [])
        self.assertIn("Representative setup: The fallback queue must be an active Omnichannel voice queue.", plan["gates"])
        with self.assertRaisesRegex(setup.GateError, "lookup contract"):
            self.build(lambda api: api.metadata[RESOURCE_LOOKUP][0].update(ReferencingEntityNavigationPropertyName="userid"))

    def test_changes_after_planning_stop_before_the_next_write(self):
        self.build()
        self.api.links[("systemusers", uid("representative"), "systemuserroles_association")].add(uid("role-Omnichannel agent"))
        with self.assertRaisesRegex(setup.GateError, "direct_roles changed since planning"):
            self.operator.provision_representative()
        self.assertEqual(self.api.writes, [])
        self.build()
        create = self.operator.create

        def create_then_queue_changes(item, **kwargs):
            row = create(item, **kwargs)
            if item["table"] == routing.CAPACITY:
                self.api.data["queues"][uid("fallback-queue")]["msdyn_priority"] = 9
            return row

        with patch.object(self.operator, "create", side_effect=create_then_queue_changes), \
                self.assertRaisesRegex(setup.GateError, "queue changed since planning"):
            self.operator.provision_representative()
        self.assertFalse([row for row in self.api.writes if row["path"].startswith("queues(")])

    def test_lost_resource_response_is_recovered_by_readback_not_sent_again(self):
        self.build()
        finish = self.operator.finish

        def lose_response(label, *args, **kwargs):
            if label == "created_and_read_back":
                raise RuntimeError("connection lost after the resource was created")
            return finish(label, *args, **kwargs)

        with patch.object(self.operator, "finish", side_effect=lose_response), self.assertRaises(RuntimeError):
            self.operator.provision_representative()
        self.assertEqual(self.journal.state["pending"]["item"]["key"], "representative_resource")
        self.operator.recover()
        self.operator.provision_representative()
        self.assertEqual(sum(row["path"] == "bookableresources" for row in self.api.writes), 1)
        self.assertTrue(self.journal.state["stages"]["representative"])

    def test_binding_waits_for_a_selected_representative_setup(self):
        self.build()
        self.journal.state["stages"].update(workspace=True, knowledge=True, copilot=True)
        with self.assertRaisesRegex(setup.GateError, "representative/knowledge/Copilot stage must verify"):
            self.operator.bind()
        self.assertEqual(self.api.writes, [])


class NativeChannelUpgradeTests(unittest.TestCase):
    def setUp(self):
        self.plan = plan_fixture()
        self.journal = MemoryJournal()
        self.api = MagicMock()
        self.operator = setup.Operator(self.api, self.plan, self.journal)
        source = {
            "msdyn_appconfigurationid": uid("native-source"),
            "msdyn_uniquename": "msdyn_csw_oc_stepup_app_configuration",
            "ismanaged": True, "statecode": 0,
        }
        self.rows = {setup.record_path("msdyn_appconfigurations", source["msdyn_appconfigurationid"]): source}
        sources, links = [], []
        for name, table, relation in (
            ("msdyn_CIFV2Loader", "msdyn_applicationextensions", "msdyn_appconfiguration_applicationextension"),
            ("msdyn_CCaaSClientSDKLoader", "msdyn_applicationextensions", "msdyn_appconfiguration_applicationextension"),
            ("msdyn_omnichannel", "msdyn_channelproviders", "msdyn_appconfig_msdyn_channelprovider"),
        ):
            identifier = uid(name)
            row = {setup.target_primary(table): identifier, "msdyn_uniquename": name, "ismanaged": True}
            if table == "msdyn_channelproviders":
                row.update(statecode=0, msdyn_channelurl="https://example.invalid/native-channel")
            sources.append({"table": table, "relation": relation, "row": row})
            link = {"table": "msdyn_appconfigurations", "id": self.plan["workspace"]["ids"]["profile"],
                    "relation": relation, "target_table": table, "target_id": identifier}
            links.append(link)
            self.journal.state["links"][setup.digest(link)] = link
            self.rows[setup.record_path(table, identifier)] = row
        self.manifest = {
            "kind": "native-channel-profile-upgrade-v1", "base_plan_sha256": setup.digest(self.plan),
            "organization_url": self.plan["config"]["org_url"],
            "profile_id": self.plan["workspace"]["ids"]["profile"],
            "source_profile": source, "sources": sources, "links": links,
        }
        self.journal.state["native_channel_upgrade"] = {"receipt": "/private/native-channels.json", "sha256": "a" * 64}
        self.api.get.side_effect = lambda path: copy.deepcopy(self.rows[path])
        self.api.rows.side_effect = lambda path, query: [
            {setup.target_primary(item["table"]): item["row"][setup.target_primary(item["table"])]}
            for item in self.manifest["sources"] if path.endswith("/" + item["relation"])
        ]
        self.operator.link_present = MagicMock(return_value=True)

    def verify(self):
        with patch.object(setup, "checked_json", return_value=self.manifest):
            return self.operator.native_channel_links()

    def test_exact_recorded_native_components_are_an_explicit_additive_delta(self):
        self.assertEqual(self.verify(), self.manifest["links"])
        self.api.request.assert_not_called()

    def test_matching_edge_without_original_provenance_is_rejected(self):
        self.journal.state["links"].pop(setup.digest(self.manifest["links"][0]))
        with self.assertRaisesRegex(setup.GateError, "original recorded association"):
            self.verify()

    def test_wrong_profile_unmanaged_sources_unknown_components_or_provider_url_are_rejected(self):
        original = copy.deepcopy(self.manifest)
        mutations = (
            lambda value: value.update(profile_id=uid("other-profile")),
            lambda value: value["source_profile"].update(ismanaged=False),
            lambda value: value["sources"][0]["row"].update(ismanaged=False),
            lambda value: value["sources"][0]["row"].update(msdyn_uniquename="unapproved-loader"),
            lambda value: value["sources"][2]["row"].update(msdyn_channelurl="http://example.invalid/native"),
        )
        for mutate in mutations:
            self.manifest = copy.deepcopy(original)
            mutate(self.manifest)
            with self.subTest(mutation=mutate), self.assertRaises(setup.GateError):
                self.verify()

    def test_changed_component_or_missing_source_membership_is_rejected(self):
        row = self.manifest["sources"][0]["row"]
        path = setup.record_path(self.manifest["sources"][0]["table"], row["msdyn_applicationextensionid"])
        self.rows[path] = {**self.rows[path], "msdyn_uniquename": "changed-native-source"}
        with self.assertRaisesRegex(setup.GateError, "drift"):
            self.verify()
        self.rows[path] = copy.deepcopy(row)
        self.api.rows.return_value = []
        self.api.rows.side_effect = None
        with self.assertRaisesRegex(setup.GateError, "no longer belongs"):
            self.verify()

    def test_no_registration_does_not_allow_any_delta(self):
        self.journal.state.pop("native_channel_upgrade")
        self.assertEqual(self.operator.native_channel_links(), [])
        self.api.get.assert_not_called()

    def test_receipt_cannot_authorize_an_extra_relationship(self):
        self.manifest["links"].append({
            "table": "msdyn_appconfigurations", "id": self.plan["workspace"]["ids"]["profile"],
            "relation": "msdyn_appconfig_msdyn_channelprovider", "target_table": "msdyn_channelproviders",
            "target_id": uid("unapproved-extra-provider"),
        })
        with self.assertRaisesRegex(setup.GateError, "unknown or missing association"):
            self.verify()


class ReceiptAndCliTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.previous_script = str(Path(directory.name) / "previous.py")

    def test_unaccepted_source_change_remains_blocked(self):
        plan = plan_fixture()
        plan["input_hashes"]["script"] = "0" * 64
        with self.assertRaisesRegex(setup.GateError, "recipe/configuration changed"):
            setup.require_recipe(plan, MemoryJournal())

    def test_script_update_needs_explicit_approval_original_backup_and_unchanged_payloads(self):
        plan = plan_fixture()
        original_source = "previous reviewed script"
        plan["input_hashes"]["script"] = setup.digest(original_source)
        journal = MemoryJournal()
        operator = MagicMock(plan=plan, journal=journal)
        before = copy.deepcopy(plan)
        with self.assertRaisesRegex(setup.GateError, "approve-recipe-update"):
            setup.accept_recipe_update(operator, self.previous_script, approved=False,
                                       confirmed_organization=plan["config"]["org_url"])
        with patch.object(setup, "read_private", return_value="wrong source"), \
                self.assertRaisesRegex(setup.GateError, "previous script"):
            setup.accept_recipe_update(operator, self.previous_script, approved=True,
                                       confirmed_organization=plan["config"]["org_url"])
        with patch.object(setup, "read_private", return_value=original_source), \
                patch.object(setup, "render_knowledge", return_value=plan["knowledge"]):
            setup.accept_recipe_update(operator, self.previous_script, approved=True,
                                       confirmed_organization=plan["config"]["org_url"])
            setup.require_recipe(plan, journal)
            count = len(journal.events)
            setup.accept_recipe_update(operator, self.previous_script, approved=True,
                                       confirmed_organization=plan["config"]["org_url"])
        self.assertEqual(len(journal.events), count)
        self.assertEqual(plan, before)
        self.assertEqual(journal.events[-1]["label"], "recipe_update_accepted")
        operator.verify_workspace.assert_called_once_with()
        operator.verify_knowledge.assert_called_once_with(search=True, record_search=False)
        operator.published_app_ok.assert_called_once_with()

    def test_recipe_update_rejects_pending_state_data_changes_and_changed_rendering(self):
        plan = plan_fixture()
        plan["input_hashes"]["script"] = setup.digest("previous")
        journal = MemoryJournal()
        operator = MagicMock(plan=plan, journal=journal)
        journal.state["pending"] = {"kind": "create"}
        with self.assertRaisesRegex(setup.GateError, "pending mutations"):
            setup.accept_recipe_update(operator, self.previous_script, approved=True,
                                       confirmed_organization=plan["config"]["org_url"])
        journal.state["pending"] = None
        changed = {**setup.input_hashes(), "canonical": "f" * 64}
        with patch.object(setup, "input_hashes", return_value=changed), \
                self.assertRaisesRegex(setup.GateError, "configuration/data changes"):
            setup.accept_recipe_update(operator, self.previous_script, approved=True,
                                       confirmed_organization=plan["config"]["org_url"])
        with patch.object(setup, "read_private", return_value="previous"), \
                patch.object(setup, "render_workspace", return_value={}), \
                self.assertRaisesRegex(setup.GateError, "exact planned"):
            setup.accept_recipe_update(operator, self.previous_script, approved=True,
                                       confirmed_organization=plan["config"]["org_url"])
        self.assertEqual(journal.events, [])

    def test_accepted_recipe_update_still_rejects_tampered_version_plan_or_backup(self):
        plan = plan_fixture()
        original_source = "previous reviewed script"
        plan["input_hashes"]["script"] = setup.digest(original_source)
        journal = MemoryJournal()
        operator = MagicMock(plan=plan, journal=journal)
        with patch.object(setup, "read_private", return_value=original_source), \
                patch.object(setup, "render_knowledge", return_value=plan["knowledge"]):
            setup.accept_recipe_update(operator, self.previous_script, approved=True,
                                       confirmed_organization=plan["config"]["org_url"])
            for key, value in (("script", "f" * 64), ("canonical", "e" * 64)):
                with patch.object(setup, "input_hashes", return_value={**setup.input_hashes(), key: value}), \
                        self.assertRaisesRegex(setup.GateError, "exact original plan"):
                    setup.require_recipe(plan, journal)
            changed_plan = {**plan, "gates": ["unrelated change"]}
            with self.assertRaisesRegex(setup.GateError, "exact original plan"):
                setup.require_recipe(changed_plan, journal)
        with patch.object(setup, "read_private", return_value="tampered original"), \
                self.assertRaisesRegex(setup.GateError, "source backup"):
            setup.require_recipe(plan, journal)

    def test_default_command_and_mutation_confirmation(self):
        parsed = setup.parser().parse_args(["--config", "private.json", "--output-dir", "private-output"])
        self.assertEqual(parsed.command, "plan")
        self.assertFalse(parsed.apply)
        with patch.object(setup, "read_private", return_value=json.dumps(config())):
            with self.assertRaisesRegex(setup.GateError, "confirm-organization"):
                setup.main(["workspace", "--config", "private.json", "--output-dir", "private-output", "--apply"])

    def test_successive_script_updates_need_exact_previous_backups_and_preserve_every_version(self):
        plan = plan_fixture()
        plan["input_hashes"]["script"] = setup.digest("original")
        journal = MemoryJournal()
        operator = MagicMock(plan=plan, journal=journal)
        first = {**setup.input_hashes(), "script": setup.digest("first approved version")}
        second = {**first, "script": setup.digest("second approved version")}
        backup = str(Path(self.previous_script).with_name("first-approved.py"))
        sources = {self.previous_script: "original", backup: "first approved version"}
        read = lambda path: sources[str(path)]
        with patch.object(setup, "read_private", side_effect=read), \
                patch.object(setup, "render_knowledge", return_value=plan["knowledge"]):
            with patch.object(setup, "input_hashes", return_value=first):
                setup.accept_recipe_update(operator, self.previous_script, approved=True,
                                           confirmed_organization=plan["config"]["org_url"])
            first_event = copy.deepcopy(journal.events[-1])
            with patch.object(setup, "input_hashes", return_value=second):
                with self.assertRaisesRegex(setup.GateError, "previous script"):
                    setup.accept_recipe_update(operator, self.previous_script, approved=True,
                                               confirmed_organization=plan["config"]["org_url"])
                with self.assertRaisesRegex(setup.GateError, "exact original plan"):
                    setup.require_recipe(plan, journal)
                setup.accept_recipe_update(operator, backup, approved=True,
                                           confirmed_organization=plan["config"]["org_url"])
                setup.require_recipe(plan, journal)
                self.assertEqual(len(journal.state["recipe_updates"]), 2)
                self.assertEqual(journal.events[0], first_event)
                sources[self.previous_script] = "changed original"
                with self.assertRaisesRegex(setup.GateError, "source backup"):
                    setup.require_recipe(plan, journal)

    def test_receipt_chain_is_bound_to_plan_and_rejects_a_gap(self):
        files = {}
        directory = Path("/private-workspace-fixture")
        with patch.object(Path, "glob", return_value=[]), patch.object(setup, "write_private", side_effect=lambda path, value: files.update({path: value})):
            journal = setup.Journal(directory, {"schema": 1})
            journal.save("intent", {"pending": {"kind": "create"}})
            journal.save("done", {"pending": None, "stages": {"workspace": True}})
        with patch.object(Path, "glob", return_value=list(files)), patch.object(setup, "read_private", side_effect=lambda path: files[path]):
            recovered = setup.Journal(directory, {"schema": 1})
            self.assertTrue(recovered.state["stages"]["workspace"])
            with self.assertRaisesRegex(setup.GateError, "different plan"):
                setup.Journal(directory, {"schema": 2})
        second = sorted(files)[1]
        with patch.object(Path, "glob", return_value=[second]), patch.object(setup, "read_private", side_effect=lambda path: files[path]):
            with self.assertRaisesRegex(setup.GateError, "incomplete"):
                setup.Journal(directory, {"schema": 1})

    def test_repository_output_is_rejected_by_shared_private_path_utility(self):
        with self.assertRaises(ValueError):
            setup.private_path(ROOT / "generated-workspace.json")

    def test_building_plan_never_issues_mutations(self):
        plan = plan_fixture()
        api = FakeAPI(plan)
        with patch.object(setup, "discover", return_value=copy.deepcopy(plan["native"])), \
                patch.object(setup, "render_knowledge", return_value=copy.deepcopy(plan["knowledge"])):
            rendered = setup.build_plan(api, plan["config"])
        self.assertEqual(rendered["knowledge"]["article_count"], 4)
        self.assertEqual(api.writes, [])

    def test_selected_stage_without_apply_is_only_a_dry_run(self):
        plan = plan_fixture()
        directory = Path("/private-workspace-fixture")
        journal = MemoryJournal()
        api = FakeAPI(plan)
        read = lambda path: json.dumps(plan["config"] if str(path) == "private.json" else plan)
        with patch.object(setup, "read_private", side_effect=read), \
                patch.object(setup, "private_path", return_value=directory), \
                patch.object(Path, "exists", return_value=True), \
                patch.object(Path, "is_dir", return_value=True), \
                patch.object(Path, "stat", return_value=type("Mode", (), {"st_mode": 0o40700})()), \
                patch.object(Path, "unlink"), patch.object(setup, "write_private"), \
                patch.object(setup, "Journal", return_value=journal), \
                patch.object(setup, "Dataverse", return_value=api), patch("sys.stdout", new_callable=io.StringIO):
            self.assertEqual(setup.main(["workspace", "--config", "private.json", "--output-dir", str(directory)]), 0)
        self.assertEqual(api.writes, [])
        self.assertEqual(journal.events[-1]["label"], "stage_dry_run")

    def test_copilot_wait_minutes_reach_the_stage_and_are_bounded(self):
        plan = plan_fixture()
        directory = Path("/private-workspace-fixture")
        read = lambda path: json.dumps(plan["config"] if str(path) == "private.json" else plan)
        arguments = ["copilot", "--config", "private.json", "--output-dir", str(directory), "--apply",
                     "--confirm-organization", plan["config"]["org_url"]]
        with patch.object(setup, "read_private", side_effect=read), \
                patch.object(setup, "private_path", return_value=directory), \
                patch.object(Path, "exists", return_value=True), \
                patch.object(Path, "is_dir", return_value=True), \
                patch.object(Path, "stat", return_value=type("Mode", (), {"st_mode": 0o40700})()), \
                patch.object(Path, "unlink"), patch.object(setup, "write_private"), \
                patch.object(setup, "Journal", return_value=MemoryJournal()), \
                patch.object(setup, "Dataverse", return_value=FakeAPI(plan)), \
                patch.object(setup, "Operator") as operator, patch("sys.stdout", new_callable=io.StringIO):
            self.assertEqual(setup.main([*arguments, "--copilot-wait-minutes", "90"]), 0)
            self.assertEqual(operator.call_args.kwargs["copilot_wait_seconds"], 5400)
            operator.return_value.provision_copilot.assert_called_once_with()
            for minutes in ("0", "241"):
                with self.subTest(minutes=minutes), self.assertRaisesRegex(setup.GateError, "copilot-wait-minutes"):
                    setup.main([*arguments, "--copilot-wait-minutes", minutes])


class CompositionTests(unittest.TestCase):
    """Real receipt-chain readers, synthetic Dataverse, no filesystem or cloud writes."""

    def setUp(self):
        import setup_native_supervision as supervision
        self.sup = supervision
        self.root = Path("/private-workspace-composition-fixture")
        self.base_dir, self.sup_dir = self.root / "base", self.root / "supervision"
        self.dirs = {self.root, self.base_dir, self.sup_dir}
        self.files = {}
        original_glob, original_exists = Path.glob, Path.exists
        original_dir, original_stat, original_unlink = Path.is_dir, Path.stat, Path.unlink
        inside = lambda path: path == self.root or self.root in path.parents

        def write(path, value):
            path = Path(path)
            if path in self.files:
                raise FileExistsError(str(path))
            self.files[path] = value

        def glob(path, pattern):
            if inside(path):
                return (item for item in self.files if item.parent == path and fnmatch.fnmatch(item.name, pattern))
            return original_glob(path, pattern)

        replacements = (
            patch.object(setup, "read_private", side_effect=lambda path: self.files[Path(path)]),
            patch.object(setup, "write_private", side_effect=write),
            patch.object(setup, "private_path", side_effect=lambda path: Path(path)),
            patch.object(supervision.native, "read_private", side_effect=lambda path: self.files[Path(path)]),
            patch.object(supervision.native, "write_private", side_effect=write),
            patch.object(Path, "glob", glob),
            patch.object(Path, "exists", lambda path: path in self.files or path in self.dirs if inside(path) else original_exists(path)),
            patch.object(Path, "is_dir", lambda path: path in self.dirs if inside(path) else original_dir(path)),
            patch.object(Path, "stat", lambda path, **kwargs: SimpleNamespace(st_mode=0o40700 if path in self.dirs else 0o100600)
                         if inside(path) else original_stat(path, **kwargs)),
            patch.object(Path, "unlink", lambda path, **kwargs: self.files.pop(path) if inside(path) else original_unlink(path, **kwargs)),
        )
        for replacement in replacements:
            replacement.start()
            self.addCleanup(replacement.stop)
        self.plan = plan_fixture()
        self.files[self.base_dir / "plan.json"] = json.dumps(self.plan)
        self.api = FakeAPI(self.plan)
        self.base_journal = setup.Journal(self.base_dir, self.plan)
        self.operator = setup.Operator(self.api, self.plan, self.base_journal, wait_seconds=0)
        self.operator.provision_workspace()
        self.operator.provision_knowledge()
        self.operator.provision_copilot()
        self.operator.bind()
        self.operator.verify()
        self.config = supervision.validate_config({
            "version": 1, "organization_url": self.api.org, "tenant_id": self.api.tenant,
            "namespace_id": uid("supervision_namespace"), "business_unit_id": uid("bu"),
            "runtime_user_id": uid("runtime"), "runtime_application_id": uid("runtime_app"),
            "approved_actor_ids": self.plan["config"]["representative_ids"],
            "controller_component_ids": [uid("controller")], "workstream_ids": self.plan["config"]["workstream_ids"],
            "voice_origin": "https://voice.example.azurecontainerapps.io",
            "session_template_ids": [self.plan["workspace"]["ids"]["voice_session"]],
            "session_binding_reviewed": True, "trust_system_administrators": False,
        })
        self.session = self.config["session_template_ids"][0]
        self.tab = supervision.owned(self.config, "tab")
        self.before = setup.membership(self.api, self.session)
        self.sup_plan = {
            "version": 1, "config": self.config, "config_hash": supervision.digest(self.config),
            "creation_targets": supervision.creation_targets(self.config),
            "templates": {self.session: {"identity": {"ismanaged": False, "msdyn_uniquename": "sample_rep_voice"},
                                         "tabs": [{"msdyn_applicationtabtemplateid": value} for value in self.before]}},
            "source_hashes": {name: setup.digest(name) for name in ("native-supervision.html", "native-supervision.js")},
        }
        self.files[self.sup_dir / "plan.json"] = json.dumps(self.sup_plan)
        self.sup_journal = supervision.Journal(self.sup_dir, self.sup_plan)

        def verify(owner):
            owner.require_unreleased()
            for key in ("tab", "tab_parameter", "resource_native-supervision.html", "resource_native-supervision.js"):
                done = owner.journal.last("done", key)
                setup.require(done is not None, "Missing upstream ownership proof")
                current = self.api.get(done["path"])
                setup.require(supervision.matches(current, done["body"]), "Upstream addon record drift")
            return {"upstream_control_plane_verified": True}

        replacement = patch.object(supervision.Setup, "verify", verify)
        replacement.start()
        self.addCleanup(replacement.stop)
        self.selector_path, self.legacy_evidence = self.root / "selector.json", self.root / "legacy-evidence.json"
        self.files[self.legacy_evidence] = json.dumps({"kind": "actual-legacy-evidence", "synthetic_test_only": True})
        self.files[self.selector_path] = json.dumps({
            "kind": "native-monitor-selector-evidence-v1", "configured_selector_proven": True, "rendered_host_proven": False,
            "organization_url": self.api.org, "tenant_id": self.api.tenant,
            "profile_id": self.plan["workspace"]["ids"]["profile"], "app_id": self.plan["config"]["app_id"],
            "session_template_id": self.session, "template_name": "sample_rep_voice",
            "workstream_ids": self.config["workstream_ids"],
            "source": {"selector": "TemplateManager.getSessionTemplateForWorkStream",
                       "url": "https://oc-cdn-public-che.azureedge.net/convcontrol/synthetic/Scripts/app.js",
                       "sha256": setup.digest("synthetic native source")},
        })
        self.acceptance = self.root / "acceptance.json"
        self.release_path = self.root / "release.json"

    def provision_addon(self):
        bodies = {
            "tab": {
                "msdyn_applicationtabtemplateid": self.tab, "msdyn_uniquename": "moacc_native_supervision_pause",
                "msdyn_name": self.sup.OWNER, "msdyn_pagetype": 509180002,
            },
            "tab_parameter": {
                "msdyn_templateparameterid": self.sup.owned(self.config, "tab_parameter"),
                "msdyn_name": "webresourceName", "msdyn_value": "moacc_/native-supervision.html",
                "msdyn_applicationtabtemplateid@odata.bind": "/msdyn_applicationtabtemplates(" + self.tab + ")",
            },
            **{"resource_" + name: {"webresourceid": self.sup.owned(self.config, "resource_" + name),
                                    "name": "moacc_/" + name, "content": "synthetic-public-resource"}
               for name in ("native-supervision.html", "native-supervision.js")},
        }
        for key, body in bodies.items():
            path = self.sup_plan["creation_targets"][key]
            table, identifier = path.split("(", 1)
            identifier = identifier[:-1]
            self.sup_journal.append("intent", key, operation="create", path=path, before=None, body=body)
            if table == "webresourceset":
                self.api.data.setdefault(table, {})[identifier] = {**body, "componentstate": 0}
            else:
                self.api.request(table, method="POST", body=body)
            self.sup_journal.append("done", key, path=path, body=body, after=self.api.get(path))
        xml = "<importexportxml><entities><entity>" + self.sup.ENTITY + "</entity></entities><webresources>" + "".join(
            "<webresource>" + self.sup.owned(self.config, "resource_" + name) + "</webresource>"
            for name in self.sup_plan["source_hashes"]
        ) + "</webresources></importexportxml>"
        self.sup_journal.append("intent", "publish", operation="publication", body={"ParameterXml": xml})
        self.sup_journal.append("done", "publish", body={"ParameterXml": xml})
        key = "session_" + self.session
        path = setup.record_path("msdyn_sessiontemplates", self.session) + "/" + setup.EXTENSION_RELATION
        self.sup_journal.append("intent", key, operation="associate", path=path, target=self.tab,
                                primary="msdyn_applicationtabtemplateid", before=[])
        self.api.links[("msdyn_sessiontemplates", self.session, setup.EXTENSION_RELATION)].add(self.tab)
        self.sup_journal.append("done", key, path=path, target=self.tab, primary="msdyn_applicationtabtemplateid")

    def manifest(self, *, legacy=None):
        if legacy is None:
            base = setup.portable_base(self.base_dir)[0]
        else:
            base = {"kind": "legacy", "receipt": str(legacy), "sha256": setup.digest(setup.checked_json(legacy))}
        sup, _, edge, before, _ = setup.supervision_receipt(self.sup_dir, self.api)
        return {
            "kind": setup.EXTENSION_KIND, "accepted_at": "2026-01-01T00:00:00+00:00",
            "organization_url": self.api.org, "tenant_id": self.api.tenant,
            "base": base, "supervision": sup, "edge": edge, "before": before, "acceptance": setup.COMPOSITION_LIMITS,
        }

    def accept(self):
        arguments = ["accept-extension", "--base-receipt", str(self.base_dir), "--supervision-receipt", str(self.sup_dir),
                     "--output", str(self.acceptance), "--approve-extension", "--confirm-organization", self.api.org]
        with patch.object(setup, "Dataverse", return_value=self.api), patch("sys.stdout", new_callable=io.StringIO):
            self.assertEqual(setup.main(arguments), 0)
        self.base_journal = setup.Journal(self.base_dir, self.plan)
        return setup.checked_json(self.acceptance)

    def test_exact_acceptance_is_idempotent_and_base_replay_preserves_the_addon(self):
        base_original = dict(self.files)
        self.provision_addon()
        with self.assertRaisesRegex(setup.GateError, "Unexpected association"):
            self.operator.verify_workspace()
        self.accept()
        after = dict(self.files)
        writes = len([row for row in self.api.writes if row["path"] != "FullTextSearchKnowledgeArticle"])
        self.accept()
        self.assertEqual(after, self.files)
        self.assertEqual(writes, len([row for row in self.api.writes if row["path"] != "FullTextSearchKnowledgeArticle"]))
        for path, text in base_original.items():
            self.assertEqual(self.files[path], text, "Old receipts were rewritten")
        operator = setup.Operator(self.api, self.plan, self.base_journal, wait_seconds=0)
        mutations = len([row for row in self.api.writes if row["path"] != "FullTextSearchKnowledgeArticle"])
        operator.verify()
        operator.provision_workspace()
        self.assertEqual(setup.membership(self.api, self.session), sorted(self.before + [self.tab]))
        self.assertEqual(mutations, len([row for row in self.api.writes if row["path"] != "FullTextSearchKnowledgeArticle"]))
        self.accept()
        self.assertFalse(setup.checked_json(self.acceptance)["acceptance"]["rendered_host_verified"])

    def test_rollback_blocks_registered_and_unregistered_addon_edges(self):
        self.provision_addon()
        writes = len([row for row in self.api.writes if row["path"] != "FullTextSearchKnowledgeArticle"])
        with self.assertRaisesRegex(setup.GateError, "Unregistered session-tab"):
            self.operator.rollback()
        self.accept()
        operator = setup.Operator(self.api, self.plan, self.base_journal, wait_seconds=0)
        with self.assertRaisesRegex(setup.GateError, "Release the owned supervision addon"):
            operator.rollback()
        self.assertEqual(writes, len([row for row in self.api.writes if row["path"] != "FullTextSearchKnowledgeArticle"]))

    def test_verified_unbind_releases_without_deleting_command_history(self):
        self.provision_addon()
        manifest = self.accept()
        self.api.data["moacc_nativeaicommands"] = {uid("command"): {"moacc_nativeaicommandid": uid("command")}}
        owner = self.sup.Setup(self.api, self.sup_plan, self.sup.Journal(self.sup_dir, self.sup_plan))
        with patch.object(owner, "assert_idle_disabled"):
            owner.unbind()
        self.assertEqual(setup.membership(self.api, self.session), self.before)
        args = ["release-extension", "--extension-receipt", str(self.acceptance), "--output", str(self.release_path),
                "--approve-extension", "--confirm-organization", self.api.org]
        with patch.object(setup, "Dataverse", return_value=self.api), patch("sys.stdout", new_callable=io.StringIO):
            self.assertEqual(setup.main(args), 0)
            after = dict(self.files)
            self.assertEqual(setup.main(args), 0)
            self.assertEqual(after, self.files)
        self.assertTrue(self.api.data["moacc_nativeaicommands"])
        journal = setup.Journal(self.base_dir, self.plan)
        self.assertEqual(journal.state["extension"]["status"], "released")
        self.assertEqual(setup.checked_json(self.release_path)["acceptance_sha256"], setup.digest(manifest))
        operator = setup.Operator(self.api, self.plan, journal, wait_seconds=0)
        operator.verify_workspace()
        operator.rollback()
        operator.rollback()
        self.assertTrue(self.api.data["moacc_nativeaicommands"])

    def test_missing_release_or_revived_reference_never_unblocks_base_rollback(self):
        self.provision_addon()
        self.accept()
        self.api.links[("msdyn_sessiontemplates", self.session, setup.EXTENSION_RELATION)].remove(self.tab)
        args = ["release-extension", "--extension-receipt", str(self.acceptance), "--output", str(self.release_path),
                "--approve-extension", "--confirm-organization", self.api.org]
        with patch.object(setup, "Dataverse", return_value=self.api), self.assertRaisesRegex(ValueError, "completed release"):
            setup.main(args)
        self.assertNotIn(self.release_path, self.files)
        self.assertEqual(setup.Journal(self.base_dir, self.plan).state["extension"]["status"], "accepted")

    def test_wrong_org_tenant_head_edge_and_baseline_are_rejected(self):
        self.provision_addon()
        original = self.manifest()
        changes = (
            ("organization", lambda m: m.update(organization_url="https://other.crm.dynamics.com")),
            ("tenant", lambda m: m.update(tenant_id=uid("other_tenant"))),
            ("base_head", lambda m: m["base"]["checkpoint"].update(sha256="0" * 64)),
            ("sup_head", lambda m: m["supervision"]["checkpoint"].update(sha256="0" * 64)),
            ("base_plan", lambda m: m["base"].update(plan_sha256="0" * 64)),
            ("sup_plan", lambda m: m["supervision"].update(plan_sha256="0" * 64)),
            ("unowned_edge", lambda m: m["edge"].update(target_id=uid("unowned_tab"))),
            ("unowned_source", lambda m: m["edge"].update(id=uid("unowned_session"))),
            ("other_relationship", lambda m: m["edge"].update(relation=setup.SESSION_SCRIPTS)),
            ("baseline", lambda m: m.update(before=[])),
        )
        writes = len(self.api.writes)
        for name, change in changes:
            with self.subTest(name=name):
                manifest = copy.deepcopy(original)
                change(manifest)
                with self.assertRaises((ValueError, setup.GateError)):
                    setup.verify_extension(self.api, manifest)
        self.assertEqual(writes, len(self.api.writes))

    def test_rejected_or_pending_release_proof_cannot_be_accepted(self):
        self.provision_addon()
        manifest = self.manifest()
        self.sup_journal.append("rejected", "tab", status=403)
        with self.assertRaisesRegex(setup.GateError, "rejected"):
            setup.verify_extension(self.api, manifest)

    def test_captured_supervision_before_set_must_equal_the_whole_base(self):
        self.sup_plan["templates"][self.session]["tabs"] = self.sup_plan["templates"][self.session]["tabs"][:1]
        self.files[self.sup_dir / "plan.json"] = json.dumps(self.sup_plan)
        self.sup_journal = self.sup.Journal(self.sup_dir, self.sup_plan)
        self.provision_addon()
        with self.assertRaisesRegex(setup.GateError, "before-set"):
            setup.verify_extension(self.api, self.manifest())

    def test_pending_release_and_revived_released_edge_stay_blocked(self):
        self.provision_addon()
        manifest = self.accept()
        owner = self.sup.Setup(self.api, self.sup_plan, self.sup.Journal(self.sup_dir, self.sup_plan))
        spec = owner.release_specs()[0]
        owner.journal.append("release_intent", spec["key"], **{
            key: spec[key] for key in ("path", "target", "primary", "before", "after")
        })
        with self.assertRaisesRegex(setup.GateError, "pending or completed release"):
            setup.verify_extension(self.api, manifest)
        with patch.object(owner, "assert_idle_disabled"):
            owner.unbind()
        ref, _, _, _, _ = setup.supervision_receipt(self.sup_dir, self.api, released=True)
        release = {"kind": setup.RELEASE_KIND, "released_at": "2026-01-01T00:00:00+00:00",
                   "acceptance_sha256": setup.digest(manifest), "supervision": ref}
        setup.verify_extension_release(self.api, manifest, release)
        self.api.links[("msdyn_sessiontemplates", self.session, setup.EXTENSION_RELATION)].add(self.tab)
        writes = len(self.api.writes)
        with self.assertRaisesRegex(ValueError, "released baseline"):
            setup.verify_extension_release(self.api, manifest, release)
        self.assertEqual(writes, len(self.api.writes))

    def test_wrong_supervision_tenant_is_rejected_before_upstream_reads(self):
        self.provision_addon()
        manifest = self.manifest()
        self.sup_plan["config"]["tenant_id"] = uid("foreign_tenant")
        self.sup_plan["config_hash"] = self.sup.digest(self.sup_plan["config"])
        self.files[self.sup_dir / "plan.json"] = json.dumps(self.sup_plan)
        with patch.object(self.sup.Setup, "verify", side_effect=AssertionError("Must not read another scope")):
            with self.assertRaisesRegex(setup.GateError, "authenticated base scope"):
                setup.verify_extension(self.api, manifest)

    def test_a_valid_but_older_supervision_head_cannot_borrow_later_completion(self):
        self.provision_addon()
        manifest = self.manifest()
        first = self.sup_journal.events[0]
        manifest["supervision"]["checkpoint"] = {"sequence": first["sequence"], "sha256": first["hash"]}
        with self.assertRaisesRegex(setup.GateError, "predates"):
            setup.verify_extension(self.api, manifest)

    def test_a_new_unresolved_supervision_intent_blocks_earlier_acceptance(self):
        self.provision_addon()
        manifest = self.manifest()
        self.sup_journal.append("intent", "new_incomplete_operation", operation="patch",
                                path="workflows(" + uid("synthetic_workflow") + ")", before={}, body={})
        with self.assertRaisesRegex(setup.GateError, "unresolved"):
            setup.verify_extension(self.api, manifest)

    def test_source_hash_guard_is_not_relaxed_for_existing_portable_plans(self):
        changed = {**setup.input_hashes(), "script": "0" * 64}
        with patch.object(setup, "input_hashes", return_value=changed), \
                self.assertRaisesRegex(setup.GateError, "recipe/configuration changed"):
            setup.portable_base(self.base_dir)

    def test_explicit_script_update_can_verify_an_existing_accepted_pause_addon_before_acceptance(self):
        self.provision_addon()
        self.accept()
        operator = setup.Operator(self.api, self.plan, setup.Journal(self.base_dir, self.plan), wait_seconds=0)
        source = Path(setup.__file__).read_text()
        backup = self.root / "previous-script.py"
        self.files[backup] = source
        candidate = {**setup.input_hashes(), "script": setup.digest("next separately approved script")}
        with patch.object(setup, "input_hashes", return_value=candidate), \
                patch.object(setup, "render_knowledge", return_value=self.plan["knowledge"]):
            with self.assertRaisesRegex(setup.GateError, "recipe/configuration changed"):
                setup.portable_base(self.base_dir)
            setup.accept_recipe_update(operator, backup, approved=True,
                                       confirmed_organization=self.plan["config"]["org_url"])
            self.assertIsNone(operator.recipe_update_candidate)
            setup.portable_base(self.base_dir)
            setup.verify_extension(self.api, setup.checked_json(self.acceptance))
        self.assertEqual(self.files[self.base_dir / "plan.json"], json.dumps(self.plan))
        self.assertEqual(setup.membership(self.api, self.session), sorted(self.before + [self.tab]))

    def test_unexplained_extra_and_base_record_drift_remain_blocked(self):
        self.provision_addon()
        self.accept()
        operator = setup.Operator(self.api, self.plan, self.base_journal, wait_seconds=0)
        key = ("msdyn_sessiontemplates", self.session, setup.EXTENSION_RELATION)
        self.api.links[key].add(uid("unknown_extra"))
        with self.assertRaisesRegex(setup.GateError, "exactly baseline"):
            operator.verify_workspace()
        self.api.links[key].remove(uid("unknown_extra"))
        row = self.api.data["msdyn_sessiontemplates"][self.session]
        row["msdyn_title"] = "Unreviewed change"
        with self.assertRaisesRegex(setup.GateError, "Owned record drift"):
            operator.verify_workspace()

    def test_staged_only_supervision_plan_does_not_establish_ownership(self):
        with self.assertRaisesRegex(setup.GateError, "complete sequential journal"):
            setup.supervision_receipt(self.sup_dir, self.api)

    def test_legacy_capture_is_explicit_and_does_not_create_portable_ownership(self):
        legacy = setup.capture_legacy_baseline(
            self.api, self.sup_dir, self.plan["workspace"]["ids"]["profile"], self.plan["config"]["app_id"],
            self.legacy_evidence, self.selector_path,
        )
        path = self.root / "legacy-baseline.json"
        setup.immutable_json(path, legacy)
        self.assertEqual(legacy["kind"], setup.LEGACY_KIND)
        self.assertFalse(legacy["portable_ownership_claimed"])
        self.assertNotIn("workspace", legacy)
        before_files = dict(self.files)
        self.provision_addon()
        manifest = self.manifest(legacy=path)
        self.assertIsNone(setup.verify_extension(self.api, manifest))
        with self.assertRaisesRegex(setup.GateError, "legacy evidence"):
            setup.Operator(self.api, self.plan, self.base_journal, extension=manifest).extension_links()
        for name, text in before_files.items():
            self.assertEqual(self.files[name], text)

    def test_legacy_cli_capture_accept_verify_and_release_never_register_a_portable_base(self):
        baseline_path = self.root / "legacy-baseline.json"
        capture = ["capture-legacy-baseline", "--supervision-receipt", str(self.sup_dir),
                   "--profile-id", self.plan["workspace"]["ids"]["profile"], "--app-id", self.plan["config"]["app_id"],
                   "--legacy-evidence", str(self.legacy_evidence), "--selector-evidence", str(self.selector_path),
                   "--output", str(baseline_path)]
        accept = ["accept-extension", "--legacy-baseline", str(baseline_path), "--supervision-receipt", str(self.sup_dir),
                  "--output", str(self.acceptance), "--approve-extension", "--confirm-organization", self.api.org]
        verify = ["verify-extension", "--extension-receipt", str(self.acceptance)]
        release = ["release-extension", "--extension-receipt", str(self.acceptance), "--output", str(self.release_path),
                   "--approve-extension", "--confirm-organization", self.api.org]
        with patch.object(setup, "Dataverse", return_value=self.api), patch("sys.stdout", new_callable=io.StringIO):
            self.assertEqual(setup.main(capture), 0)
            original = self.files[baseline_path]
            self.assertEqual(setup.main(capture), 0)
            self.assertEqual(self.files[baseline_path], original)
            self.provision_addon()
            self.assertEqual(setup.main(accept), 0)
            self.assertEqual(setup.main(verify), 0)
            owner = self.sup.Setup(self.api, self.sup_plan, self.sup.Journal(self.sup_dir, self.sup_plan))
            with patch.object(owner, "assert_idle_disabled"):
                owner.unbind()
            self.assertEqual(setup.main(release), 0)
            self.assertEqual(setup.main([*verify, "--release-receipt", str(self.release_path)]), 0)
        self.assertIsNone(setup.Journal(self.base_dir, self.plan).state.get("extension"))
        self.assertEqual(setup.checked_json(self.acceptance)["base"]["kind"], "legacy")

    def test_configuration_receipts_cannot_claim_ui_acceptance_or_authorize_other_deltas(self):
        self.provision_addon()
        original = self.manifest()
        for field in ("rendered_host_verified", "acoustic_acceptance_verified"):
            manifest = copy.deepcopy(original)
            manifest["acceptance"][field] = True
            with self.assertRaisesRegex(setup.GateError, "claimed UI/acoustic"):
                setup.verify_extension(self.api, manifest)
        manifest = {**original, "allow_any_extra": True}
        with self.assertRaisesRegex(setup.GateError, "Unsupported extension"):
            setup.verify_extension(self.api, manifest)

    def test_legacy_capture_rejects_post_binding_baselines_and_stale_selector_proof(self):
        self.provision_addon()
        with self.assertRaisesRegex(setup.GateError, "before binding"):
            setup.capture_legacy_baseline(
                self.api, self.sup_dir, self.plan["workspace"]["ids"]["profile"], self.plan["config"]["app_id"],
                self.legacy_evidence, self.selector_path,
            )
        self.api.links[("msdyn_sessiontemplates", self.session, setup.EXTENSION_RELATION)].remove(self.tab)
        proof = setup.checked_json(self.selector_path)
        proof["session_template_id"] = uid("wrong_host")
        self.files[self.selector_path] = json.dumps(proof)
        with self.assertRaisesRegex(setup.GateError, "another organization/profile"):
            setup.capture_legacy_baseline(
                self.api, self.sup_dir, self.plan["workspace"]["ids"]["profile"], self.plan["config"]["app_id"],
                self.legacy_evidence, self.selector_path,
            )

    def test_explicit_local_approval_is_required_and_apply_is_prohibited(self):
        self.provision_addon()
        args = ["accept-extension", "--base-receipt", str(self.base_dir), "--supervision-receipt", str(self.sup_dir),
                "--output", str(self.acceptance)]
        with self.assertRaisesRegex(setup.GateError, "approve-extension"):
            setup.main(args)
        with self.assertRaisesRegex(setup.GateError, "--apply is never accepted"):
            setup.main([*args, "--apply"])
        self.assertNotIn(self.acceptance, self.files)


DECLARATIVE_CONTRACT_JS = r"""
const assert = require("node:assert/strict");
const fs = require("node:fs");
const p = JSON.parse(fs.readFileSync(0, "utf8"));
const rows = p.workspace.records;
const macro = key => JSON.parse(rows.find(r => r.key === "macro_" + key).payload.clientdata)
  .properties.definition.actions;
let context;
const resolve = value => typeof value === "string" ? value.replace(/\$\{([^{}]+)\}/g, (_, key) => context[key] ?? "") : value;
function condition(e) {
  if (e.not) return !condition(e.not);
  if (e.equals) return resolve(e.equals[0]) === resolve(e.equals[1]);
  if (e.contains) return resolve(e.contains[0]).includes(resolve(e.contains[1]));
  if (e.and) return e.and.every(condition);
  if (e.or) return e.or.some(condition);
  throw new Error("Unrecognized condition");
}
function execute(actions) {
  const calls = [];
  for (const action of Object.values(actions)) {
    if (action.type === "If") calls.push(...execute(condition(action.expression) ? action.actions : action.else.actions));
    else calls.push(action);
  }
  return calls;
}
let assertions = 0;
for (const customerType of ["contact", "account", ""]) {
  context = {customerEntityName: customerType, customerRecordId: customerType ? "saved-customer" : ""};
  const [navigation] = execute(macro("review_customer"));
  assert.equal(navigation.type, customerType ? "Open_Form" : "Open_Grid");
  assert.equal(navigation.inputs.FormId, customerType === "contact" ? p.workspace.ids.contact_form : undefined);
  assertions += 2;
}
for (const [entity, id, live] of [
  ["incident", "saved-case", "saved-conversation"], ["incident", "", "saved-conversation"],
  ["contact", "saved-customer", "saved-conversation"], ["incident", "saved-case", ""],
]) {
  context = {"Session.CurrentTab.entityName": entity, "Session.CurrentTab.entityId": id, LiveWorkItemId: live};
  assert.equal(execute(macro("link_case"))[0].type,
    entity === "incident" && id && live ? "Link_record_to_conversation" : "Open_Grid");
  assertions++;
}
const scripts = rows.filter(r => r.table === "msdyn_productivityagentscripts");
const steps = rows.filter(r => r.table === "msdyn_productivityagentscriptsteps");
for (const lcid of [1033, 1025]) {
  assert.equal(scripts.filter(r => r.payload.msdyn_language === lcid).length, 3);
  for (const key of ["voice_script", "case_script"]) {
    const script = scripts.find(r => r.key === key + (lcid === 1033 ? "_en" : "_ar"));
    const ownSteps = steps.filter(r => r.payload["msdyn_agentscriptid@odata.bind"].includes(script.id));
    assert.equal(ownSteps.length, key === "voice_script" ? 15 : 9);
    for (const step of ownSteps.filter(r => r.payload.msdyn_actiontype === 192350002)) {
      const target = scripts.find(r => step.payload["msdyn_routeactionid@odata.bind"].includes(r.id));
      assert.equal(target.payload.msdyn_language, lcid);
      assert.equal(steps.filter(r => r.payload["msdyn_agentscriptid@odata.bind"].includes(target.id)).length, 4);
    }
    assertions++;
  }
}
process.stdout.write(JSON.stringify({assertions, network: false, sdk: false}));
"""


NATIVE_CONTRACT_JS = r"""
"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const crypto = require("node:crypto");
const p = JSON.parse(fs.readFileSync(0, "utf8"));
const rows = p.workspace.records;
const violations = [];
const hashes = {};
const blocked = name => () => { violations.push(name); throw new Error("Blocked boundary: " + name); };
function sandboxFor(env, app = false) {
  const source = fs.readFileSync(process.env[env], "utf8");
  hashes[env] = crypto.createHash("sha256").update(source).digest("hex");
  const s = {
    console: {log() {}, warn() {}, error() {}}, Promise, Map, Set, Date, JSON, performance,
    URL, URLSearchParams, encodeURIComponent, decodeURIComponent,
    setTimeout: app ? () => 0 : setTimeout, clearTimeout,
    XMLHttpRequest: class {constructor() {blocked("network")();}}, fetch: blocked("network"),
  };
  if (app) Object.assign(s, {navigator: {userAgent: "OfflineContract"}, name: "offline", document: {}});
  s.window = s; s.top = s; s.parent = s; s.self = s;
  vm.createContext(s);
  vm.runInContext(source, s, {timeout: 5000, filename: env});
  return s;
}
const payloadRow = payload => Object.fromEntries(Object.entries(payload).map(([field, value]) => {
  if (!field.endsWith("@odata.bind")) return [field, value];
  return ["_" + field.slice(0, -"@odata.bind".length).toLowerCase() + "_value", value.split("(")[1].slice(0, -1)];
}));
async function main() {
  let checks = 0;
  const app = sandboxFor("DYNAMICS_NATIVE_APP_RUNTIME", true);
  const runtime = app.AppRuntime;
  runtime.Common.TelemetryData.generate = () => ({addCustomParameters() {}, addCustomParameter() {}, addError() {}});
  for (const key of ["info", "warning", "error", "success"]) runtime.Logger[key] = () => {};
  app.Xrm = {WebApi: {
    retrieveMultipleRecords: async (entity, query) => {
      if (entity === "msdyn_ocliveworkitem") {
        assert.ok(decodeURIComponent(query).includes("activityid eq " + context.LiveWorkItemId));
        return {entities: [{_msdyn_issueid_value: context.persistedCaseId || null}]};
      }
      assert.equal(entity, "systemform");
      assert.ok(query.includes(p.workspace.ids.contact_form));
      return {entities: decodeURIComponent(query).includes("objecttypecode eq 'contact'") ?
        [{formid: p.workspace.ids.contact_form}] : []};
    },
    createRecord: blocked("create"), updateRecord: blocked("update"), deleteRecord: blocked("delete"),
  }};
  for (const key of ["voice_customer", "case_customer", "case_lookup"]) {
    const tab = rows.find(r => r.key === key);
    for (const customerType of ["contact", "account", ""]) {
      const template = {
        name: tab.payload.msdyn_uniquename, title: tab.payload.msdyn_title,
        parameters: new Map(p.workspace.parameters.filter(v => v.tab === tab.id).map(v => [v.name, v.value])),
      };
      const context = new Map([
        ["customerEntityName", customerType], ["customerRecordId", customerType ? p.config.app_id : ""],
        ["anchor._customerid_value@Microsoft.Dynamics.CRM.lookuplogicalname", customerType],
        ["anchor._customerid_value", customerType ? p.config.app_id : ""],
      ]);
      await runtime.Runtime.SessionManager.resolveAppParameters(template, context);
      const page = new runtime.Runtime.FormPageInput(template).getPageInput();
      assert.equal(page.entityName, customerType);
      assert.equal(page.formId, customerType === "contact" ? p.workspace.ids.contact_form : "");
      assert.equal(String(page.validateRecord), "true");
      checks++;
    }
  }
  for (const customerType of ["contact", "account"]) {
    const tab = rows.find(r => r.key === "new_case_customer");
    const template = {
      name: tab.payload.msdyn_uniquename, title: tab.payload.msdyn_title,
      parameters: new Map(p.workspace.parameters.filter(v => v.tab === tab.id).map(v => [v.name, v.value])),
    };
    const context = new Map([
      ["customerEntityName", customerType], ["customerRecordId", p.config.app_id],
      ["customerName", 'A "quoted" customer\\name'],
      ["data", '{"ocContext":{"unrelated":"voice session"}}'],
    ]);
    await runtime.Runtime.SessionManager.resolveAppParameters(template, context);
    const page = new runtime.Runtime.FormPageInput(template).getPageInput();
    assert.equal(page.entityId, undefined);
    assert.equal(page.validateRecord, undefined);
    assert.equal(Object.keys(page.data).length, 0);
    assert.equal(page.formId, p.workspace.ids.case_form);
    assert.equal(page.createFromEntity.id, p.config.app_id);
    assert.equal(page.createFromEntity.entityType, customerType);
    assert.deepEqual(Object.keys(page.createFromEntity).sort(), ["entityType", "id"]);
    checks++;
  }
  const m = sandboxFor("DYNAMICS_NATIVE_MACRO_RUNTIME");
  const internal = m.Microsoft.ProductivityMacros.Internal;
  const operation = internal.ProductivityMacroOperation;
  const loaded = JSON.parse(fs.readFileSync(process.env.DYNAMICS_NATIVE_ACTION_CATALOG, "utf8"));
  const catalog = Array.isArray(loaded) ? loaded :
    loaded.value || loaded.native?.macro_actions || loaded.msdyn_productivitymacroactiontemplate?.value;
  assert.ok(catalog, "Export the installed macro action catalog to a private JSON file.");
  for (const row of catalog) operation.macroActionTemplates.set(row.msdyn_name, {_runtimeAPI: row.msdyn_runtimeapi});
  operation.isWebResourceLoaded = true;
  internal.logSuccess = () => {};
  internal.logFailure = (...args) => { throw new Error(String(args[0])); };
  internal.logNestedApiData = () => {};
  internal.Utils.getFeatureControlSetting = () => false;
  const nativeResolveTemplateString = internal.resolveTemplateString;
  let context, calls;
  internal.resolveTemplateString = async (value, state = {}) => {
    if (value.startsWith("${$odata.incident.title.")) return context["Session.CurrentTab.entityId"] ? "Synthetic case" : "";
    if (value.startsWith("${$odata.msdyn_ocliveworkitem._msdyn_issueid_value.")) return context.persistedCaseId || "";
    return value.replace(/\$\{([^{}]+)\}/g, (_, key) => Object.hasOwn(state, key) ? state[key] : context[key] ?? "");
  };
  const session = {
    sessionId: "offline-session", getFocusedTab: () => ({tabId: "offline-tab"}),
    getTab: tabId => ({refresh: () => calls.push({kind: "refresh_tab", tabId}), focus() {}}),
    getContext: async () => ({parameters: {"anchor.incidentid": context["Session.CurrentTab.entityId"]}, get: () => undefined}),
    createTab: async options => {
      const tab = rows.find(r => r.table === "msdyn_applicationtabtemplates" &&
        r.payload.msdyn_uniquename === options.templateName);
      let pageInput;
      if (tab) {
        const template = {
          name: options.templateName, title: tab.payload.msdyn_title,
          parameters: new Map(p.workspace.parameters.filter(v => v.tab === tab.id).map(v => [v.name, v.value])),
        };
        for (const [key, value] of options.appContext || []) {
          if (template.parameters.has(key)) template.parameters.set(key, value);
        }
        await runtime.Runtime.SessionManager.resolveAppParameters(template, new Map(Object.entries(context)));
        pageInput = new runtime.Runtime.FormPageInput(template).getPageInput();
      }
      calls.push({kind: "template", templateName: options.templateName, pageInput});
      return "offline-tab";
    },
  };
  m.Microsoft.AppRuntime = {
    Internal: {createTab: async options => {calls.push({kind: "page", ...JSON.parse(JSON.stringify(options))}); return "offline-tab";}},
    Sessions: {getFocusedSession: () => session, getSession: () => session,
      refreshSession: async () => {calls.push({kind: "refresh_session"}); return {};}},
  };
  m.Xrm = {
    Utility: {getGlobalContext: () => ({
      getCurrentAppProperties: async () => ({appId: p.config.app_id}),
      getClientUrl: () => "https://offline.invalid", userSettings: {languageId: 1033},
    })},
    WebApi: {
      retrieveRecord: async (entity, id) => {
        assert.equal(entity, "appmodule"); assert.equal(id, p.config.app_id);
        return {name: "Copilot Service workspace", navigationtype: 1};
      },
      retrieveMultipleRecords: async (entity, query) => {
        if (entity === "msdyn_ocliveworkitem") {
          assert.ok(decodeURIComponent(query).includes("activityid eq " + context.LiveWorkItemId));
          assert.ok(query.includes("$select=_msdyn_issueid_value"));
          return {entities: [{_msdyn_issueid_value: context.persistedCaseId}]};
        }
        assert.equal(entity, "msdyn_applicationtabtemplate");
        const decoded = decodeURIComponent(query);
        const tab = rows.find(r => r.table === "msdyn_applicationtabtemplates" && decoded.includes(r.id));
        if (tab) return {entities: [{msdyn_uniquename: tab.payload.msdyn_uniquename}]};
        assert.ok(decoded.includes(p.native.knowledge_tab.msdyn_applicationtabtemplateid));
        return {entities: [{msdyn_uniquename: "msdyn_omnichannel_kbsearch"}]};
      },
      createRecord: blocked("create"), updateRecord: blocked("update"), deleteRecord: blocked("delete"),
    },
  };
  m.OmniChannelPackage = {Macros: {Instance: {linkToConversation: async (action, inputs) => {
    calls.push({kind: "link_intent", inputs: JSON.parse(JSON.stringify(inputs))});
    return {result: {[`${action}.EntityId`]: inputs.EntityId}};
  }}}};
  m.Microsoft.ProductivityMacros.RunHistory.setActionStatus = () => {};
  m.Microsoft.ProductivityMacros.RunHistory.createRunHistoryRecord = blocked("run_history");
  const customer = p.config.app_id, savedCase = p.config.tenant_id, live = p.config.workstream_ids[0];
  context = {LiveWorkItemId: live, persistedCaseId: savedCase};
  const openCase = JSON.parse(rows.find(r => r.key === "macro_open_case").payload.clientdata)
    .properties.definition.actions.Condition_conversation_context.actions.Condition_current_case_linked;
  assert.equal(await nativeResolveTemplateString(openCase.expression.not.equals[0], {LiveWorkItemId: live}, ""), savedCase);
  checks++;
  const fixtures = [
    {type: "contact", customer, savedCase, entity: "incident", id: savedCase, live},
    {type: "account", customer, savedCase, entity: "incident", id: savedCase, live},
    {type: "", customer: "", savedCase: "", entity: "msdyn_ocliveworkitem", id: live, live},
    {type: "contact", customer, savedCase, entity: "contact", id: customer, live},
    {type: "", customer: "", savedCase: "", entity: "incident", id: "", live},
    {type: "", customer: "", savedCase, entity: "incident", id: savedCase, live: ""},
  ];
  for (const f of fixtures) {
    context = {customerEntityName: f.type, customerRecordId: f.customer, customerName: "Synthetic customer",
      persistedCaseId: f.savedCase,
      caseId: f.savedCase, LiveWorkItemId: f.live, "Session.CurrentTab.entityName": f.entity, "Session.CurrentTab.entityId": f.id};
    for (const row of rows.filter(r => r.table === "workflows")) {
      if (row.key === "macro_refresh_case_context" && (f.live || f.entity !== "incident" || !f.id)) continue;
      calls = [];
      let timer;
      try {
        await Promise.race([
          m.Microsoft.LogicAppExecutor.ExecuteLogicApp(JSON.stringify(JSON.parse(row.payload.clientdata).properties)),
          new Promise((_, reject) => {timer = setTimeout(() => reject(new Error("Native macro timeout")), 4000);}),
        ]);
      } finally {clearTimeout(timer);}
      assert.ok(calls.length, row.key);
      const focused = f.entity === "incident" && Boolean(f.id);
      if (row.key === "macro_review_customer") {
        const [page] = calls;
        assert.equal(page.pageInput.entityName, f.type || "contact");
        assert.equal(page.pageInput.formId, f.type === "contact" ? p.workspace.ids.contact_form : undefined);
        if (f.customer) assert.equal(page.pageInput.entityId, f.customer);
      }
      if (row.key === "macro_link_case") {
        const links = calls.filter(c => c.kind === "link_intent");
        assert.equal(links.length, focused && f.live ? 1 : 0);
        if (links.length) assert.equal(links[0].inputs.EntityId, f.id);
      }
      if (row.key === "macro_followup_task") {
        const tasks = calls.filter(c => c.kind === "page" && c.pageInput.entityName === "task");
        assert.equal(tasks.length, focused ? 1 : 0);
        if (tasks.length) {
          assert.equal(tasks[0].pageInput.data.regardingobjectid, f.id);
          assert.equal(tasks[0].pageInput.entityId, undefined);
        }
      }
      if (row.key === "macro_new_case") {
        assert.equal(calls[0].pageInput.entityName, "incident");
        assert.equal(calls[0].pageInput.entityId, undefined);
        assert.equal(calls[0].pageInput.formId, p.workspace.ids.case_form);
        if (f.customer) {
          assert.equal(calls[0].pageInput.createFromEntity.id, f.customer);
          assert.equal(calls[0].pageInput.createFromEntity.entityType, f.type);
        } else assert.equal(calls[0].pageInput.createFromEntity, undefined);
      }
      if (row.key === "macro_open_case")
        assert.equal(calls.filter(c => c.kind === "template" &&
          c.templateName === p.config.prefix + "_voice_case").length, f.savedCase ? 1 : 0);
      if (row.key === "macro_find_cases" && f.customer) {
        assert.equal(calls[0].pageInput.entityName, f.type);
        assert.equal(calls[0].pageInput.entityId, f.customer);
        assert.equal(calls[0].pageInput.formId, f.type === "contact" ? p.workspace.ids.contact_form : "");
      }
      checks++;
    }
  }
  for (const [cached, persisted] of [["", savedCase], [savedCase, ""]]) {
    context = {customerEntityName: "contact", customerRecordId: customer, customerName: "Synthetic customer",
      caseId: cached, persistedCaseId: persisted, LiveWorkItemId: live,
      "Session.CurrentTab.entityName": "incident", "Session.CurrentTab.entityId": savedCase};
    calls = [];
    const row = rows.find(r => r.key === "macro_open_case");
    await m.Microsoft.LogicAppExecutor.ExecuteLogicApp(JSON.stringify(JSON.parse(row.payload.clientdata).properties));
    const linked = calls.filter(c => c.kind === "template" && c.templateName === p.config.prefix + "_voice_case");
    assert.equal(linked.length, persisted ? 1 : 0);
    if (persisted) assert.equal(linked[0].pageInput.entityId, persisted);
    assert.equal(calls.filter(c => c.kind === "link_intent").length, 0);
    checks++;
  }
  const script = sandboxFor("DYNAMICS_NATIVE_SCRIPT_RUNTIME");
  const C = script.MscrmControls.Callscript;
  const nativeErrors = [];
  const logger = {logSuccess() {}, logError(...args) {nativeErrors.push(args);}};
  const utils = {
    isNullOrUndefined: v => v === null || v === undefined,
    isNullOrEmptyString: v => v === null || v === undefined || v === "",
  };
  for (const languageId of [1033, 1025]) {
    for (const kind of ["voice", "case"]) {
      const suffix = languageId === 1033 ? "_en" : "_ar";
      const selected = rows.find(r => r.key === kind + "_script" + suffix);
      const webAPI = {
        retrieveRecord: async (entity, id) => {
          assert.equal(entity, "msdyn_productivityagentscript");
          return payloadRow(rows.find(r => r.id === id).payload);
        },
        retrieveMultipleRecords: async (entity, query) => {
          const xml = decodeURIComponent(query.slice(query.indexOf("=") + 1));
          if (entity === "msdyn_productivityagentscript") {
            assert.ok(xml.includes(`attribute='msdyn_language' operator='eq' value='${languageId}'`));
            assert.ok(xml.includes(p.config.prefix + "_" + kind));
            return {entities: [payloadRow(selected.payload)]};
          }
          assert.equal(entity, "msdyn_productivityagentscriptstep");
          const target = rows.find(r => r.table === "msdyn_productivityagentscripts" && xml.includes(r.id));
          assert.ok(target);
          const entities = rows.filter(r => r.table === "msdyn_productivityagentscriptsteps" &&
            r.payload["msdyn_agentscriptid@odata.bind"].includes(target.id)).map(r => {
              const record = payloadRow(r.payload);
              for (const field of ["_msdyn_macroactionid_value", "_msdyn_routeactionid_value"])
                if (record[field]) record[field + "@OData.Community.Display.V1.FormattedValue"] = "Synthetic native action";
              return record;
            });
          return {entities};
        },
      };
      const data = new C.DataManager({userSettings: {languageId}, utils, resources: {getString: key => key}, webAPI},
        logger, {getSessionTemplateName: async () => p.config.prefix + "_" + kind},
        {resolveReplaceableParameters: async value => value});
      const initial = await data.retrieveInitialData();
      assert.equal(initial.length, 1); assert.equal(initial[0].id, selected.id); checks++;
      const full = await data.retrieveCallscriptAndStepsData(selected.id);
      assert.equal(full.steps.length, kind === "voice" ? 15 : 9); checks++;
      if (kind === "voice") {
        const route = full.steps.find(step => step.action instanceof C.RouteAction);
        const expected = rows.find(r => r.key === "wrapup_script" + suffix);
        assert.equal(route.action.routeCallscriptId, expected.id);
        let routed;
        await route.action.executeAction({
          callscriptsForCurrentSession: [full],
          fetchCallscriptAndStepsData: id => data.retrieveCallscriptAndStepsData(id),
          updateCurrentCallScript: result => {routed = result;},
        });
        assert.equal(routed.id, expected.id); assert.equal(routed.steps.length, 4); checks++;
      }
    }
  }
  assert.equal(nativeErrors.length, 0, JSON.stringify(nativeErrors));
  assert.equal(violations.length, 0);
  process.stdout.write(JSON.stringify({checks, hashes, network: false, browser: false, persisted_new_kit: false}));
}
main().catch(error => {console.error(error.stack || error); process.exitCode = 1;});
"""


class ClientContractTests(unittest.TestCase):
    def run_node(self, script):
        self.assertIsNotNone(shutil.which("node"), "Node is required for native workspace client contracts.")
        result = subprocess.run(["node", "--eval", script], input=json.dumps(plan_fixture(), ensure_ascii=False),
                                text=True, capture_output=True, timeout=60, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_authored_declarative_contact_context_and_localization_contracts(self):
        result = self.run_node(DECLARATIVE_CONTRACT_JS)
        self.assertGreaterEqual(result["assertions"], 14)
        self.assertFalse(result["network"])
        self.assertFalse(result["sdk"])

    @unittest.skipUnless(all(os.environ.get(key) for key in (
        "DYNAMICS_NATIVE_APP_RUNTIME", "DYNAMICS_NATIVE_MACRO_RUNTIME",
        "DYNAMICS_NATIVE_SCRIPT_RUNTIME", "DYNAMICS_NATIVE_ACTION_CATALOG",
    )), "Optional installed-native-client proof requires private runtime/catalog files; no Microsoft bundles are vendored.")
    def test_private_installed_native_clients_with_network_and_mutations_blocked(self):
        result = self.run_node(NATIVE_CONTRACT_JS)
        self.assertEqual(result["checks"], 73)
        self.assertEqual(len(result["hashes"]), 3)
        self.assertFalse(result["network"])
        self.assertFalse(result["browser"])
        self.assertFalse(result["persisted_new_kit"])


if __name__ == "__main__":
    unittest.main()
