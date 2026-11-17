"""Reproduce a scoped bilingual native workspace; planning never writes to Dataverse."""

import argparse
import copy
import hashlib
import html
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import sys
import time
import urllib.error
import urllib.parse
import xml.etree.ElementTree as ET
from contextlib import contextmanager
from datetime import datetime, timezone
from uuid import NAMESPACE_URL, uuid4, uuid5

from native_voice_setup import Dataverse, guid, organization, private_path, read_private, write_private
import setup_dynamics_routing as routing


ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "infra/data/dynamics-workspace"
CANONICAL = ROOT / "infra/data/knowledge.md"
SCHEMA = 1
PROFILE_USERS = "msdyn_appconfiguration_systemuser"
SESSION_SCRIPTS = "msdyn_msdyn_prod_agentscript_msdyn_sessiontemplat"
TABLE_IDS = {
    "systemforms": "formid",
    "workflows": "workflowid",
    "categories": "categoryid",
    "knowledgearticles": "knowledgearticleid",
    **{f"msdyn_{name}s": f"msdyn_{name}id" for name in (
        "applicationtabtemplate", "sessiontemplate", "paneconfiguration",
        "panetabconfiguration", "appconfiguration", "appcopilotconfiguration",
        "productivityagentscript", "productivityagentscriptstep", "templateparameter",
    )},
}
LOOKUPS = {
    "ancestorformid": "_ancestorformid_value",
    "msdyn_agentscriptid": "_msdyn_agentscriptid_value",
    "msdyn_macroactionid": "_msdyn_macroactionid_value",
    "msdyn_routeactionid": "_msdyn_routeactionid_value",
    "msdyn_anchortab": "_msdyn_anchortab_value",
    "msdyn_ProductivityPaneId": "_msdyn_productivitypaneid_value",
    "msdyn_ToolId": "_msdyn_toolid_value",
    "msdyn_appconfigurationid": "_msdyn_appconfigurationid_value",
    "msdyn_applicationtabtemplateid": "_msdyn_applicationtabtemplateid_value",
    "parentcategoryid": "_parentcategoryid_value",
    "languagelocaleid": "_languagelocaleid_value",
    "UserId": "_userid_value",
    "msdyn_bookableresourceid": "_msdyn_bookableresourceid_value",
    "msdyn_capacityprofileid": "_msdyn_capacityprofileid_value",
}
NATIVE_ACTIONS = {
    "Open_Form", "Open_Grid", "Open_Application_Tab", "Create_New",
    "Get_current_page", "Refresh", "Refresh_Session", "Link_record_to_conversation",
}
CASE_SECTION_FIELDS = {
    "moacc_district": ("District", "الحي"),
    "moacc_street": ("Street", "الشارع"),
    "moacc_landmark": ("Landmark", "المعلم"),
    "moacc_category": ("Category", "التصنيف"),
}
CASE_SECTION_HEADING = "Service request details / تفاصيل طلب الخدمة"
CONVERSATION_CASE_SLUG = (
    "$odata.msdyn_ocliveworkitem._msdyn_issueid_value."
    "?$select=_msdyn_issueid_value&$filter=activityid eq {LiveWorkItemId}"
)
SUMMARY_PATCHES = {
    "legacy": {"msdyn_whenagentjoins": True, "msdyn_conversationends": True},
    "modern": {
        "msdyn_enabled": True, "msdyn_casesummaryenabled": True,
        "msdyn_ondemandenabled": True, "msdyn_whenagentjoinsenabled": True,
        "msdyn_whenconversationendsenabled": True,
    },
    "copilot": {"msdyn_knowledgearticlesourceenabled": True},
}
SHARED_TABLES = {
    "legacy": ("msdyn_conversationsummarysettings", "msdyn_conversationsummarysettingid"),
    "modern": ("msdyn_copilotsummarizationsettings", "msdyn_copilotsummarizationsettingid"),
    "copilot": ("msdyn_agentcopilotsettings", "msdyn_agentcopilotsettingid"),
}
APP_FIELDS = (
    "name", "uniquename", "description", "eventhandlers", "configxml", "descriptor",
    "appgraph", "isdefault", "navigationtype", "webresourceid", "welcomepageid",
    "clienttype", "formfactor", "optimizedfor",
)
BASE_APPROVALS = {
    "provision_arabic", "publish_app", "publish_knowledge",
    "shared_copilot_settings", "native_copilot_status_check",
    "bind_representatives_and_workstreams", "licenses_and_region_reviewed",
}
# Normalized only when supplied, so configurations planned before these existed keep their exact plan.
LATER_APPROVALS = {"setup_representative", "representative_knowledge_visibility"}
REPRESENTATIVE_SETUP_FIELDS = {"user_id", "entra_object_id", "queue_id", "capacity_profile_id"}
REPRESENTATIVE_ROLES = ("Omnichannel agent", "App Profile User", "Customer Service Representative")
ROUTE_FIELDS = "msdyn_liveworkstreamid,statecode,msdyn_direction,msdyn_streamsource,_msdyn_defaultqueue_value"
BINDING_FIELDS = ("msdyn_liveworkstreamcapacityprofileid,_msdyn_workstream_id_value,"
                  "_msdyn_capacityprofile_id_value,statecode,componentstate")
PROFILE_POLICY = ("msdyn_capacityprofileid", "statecode", "componentstate", "msdyn_defaultmaxunits",
                  "msdyn_blockassignment", "msdyn_resetduration")
PROFILE_APP_FIELD = "msdyn_appmoduleuniquename"
# Observed once: a new profile stored this native default instead of the requested app unique name.
NATIVE_PROFILE_APP_DEFAULT = "CustomerServiceApps"
# Native parent synchronization advances these on unchanged sibling parameters.
GENERATED_VOLATILE = {"modifiedon", "versionnumber", "_modifiedby_value", "_modifiedonbehalfby_value"}
VISIBILITY_FILTER = "msdyn_entityname eq 'knowledgearticle' and msdyn_attributename eq 'isinternal'"
REGISTRY_LINE = re.compile(r"^\[S\d+\]:", re.M)
COPILOT_READY = 100230003
COPILOT_ADMIN_PAGES = ("Copilot Service admin center > Support experience > Productivity > Copilot settings "
                       "(opt-in and data movement status) and Agents within Copilot > Customer Support > "
                       "Overview > Knowledge sources")


class GateError(RuntimeError):
    """A prerequisite or uncertain mutation requires an operator, not a retry loop."""


def require(condition, message):
    if not condition:
        raise GateError(message)


def canonical_json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value):
    if not isinstance(value, str):
        value = canonical_json(value)
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def utc():
    return datetime.now(timezone.utc).isoformat()


def quote(value):
    return "'" + value.replace("'", "''") + "'"


def record_path(table, identifier):
    return f"{table}({guid(identifier)})"


def target_primary(table):
    primary = TABLE_IDS.get(table, {
        "msdyn_applicationextensions": "msdyn_applicationextensionid",
        "msdyn_channelproviders": "msdyn_channelproviderid",
        "roles": "roleid",
        "systemusers": "systemuserid",
    }.get(table))
    require(primary is not None, "Unknown relationship target.")
    return primary


def etag(row):
    value = row.get("@odata.etag", "")
    require(isinstance(value, str) and bool(re.fullmatch(r'W/"\d+"', value)),
            "A native ETag is required; wildcard writes are prohibited.")
    return value


def one(rows, label):
    require(len(rows) == 1, f"Expected exactly one {label}; found {len(rows)}. Resolve missing/ambiguous native configuration.")
    return rows[0]


def validate_config(value):
    required = {"org_url", "tenant_id", "app_id", "namespace", "prefix", "representative_ids", "workstream_ids"}
    optional = {"label", "approvals", "enable_knowledge", "enable_copilot", "representative_setup"}
    require(isinstance(value, dict) and required <= value.keys() and value.keys() <= required | optional,
            "Configuration must contain the documented fields only.")
    config = copy.deepcopy(value)
    config["org_url"] = organization(config["org_url"])
    for key in ("tenant_id", "app_id"):
        config[key] = guid(config[key])
    require(isinstance(config["namespace"], str) and
            re.fullmatch(r"[a-z][a-z0-9_-]{2,63}", config["namespace"]) and
            not re.fullmatch(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", config["namespace"]),
            "Use a stable nonpersonal deployment namespace slug, not an environment record ID.")
    require(isinstance(config["prefix"], str) and
            re.fullmatch(r"[a-z][a-z0-9_]{2,23}", config["prefix"]) and
            not config["prefix"].startswith(("msdyn", "d365")),
            "Use a dedicated 3–24 character publisher prefix, not a Microsoft/default prefix.")
    for key in ("representative_ids", "workstream_ids"):
        require(isinstance(config[key], list) and 1 <= len(config[key]) <= 10,
                f"{key} must contain 1–10 explicitly approved IDs.")
        config[key] = [guid(item) for item in config[key]]
        require(len(set(config[key])) == len(config[key]), f"Duplicate {key}.")
    config.setdefault("label", "Service representative")
    for feature in ("enable_knowledge", "enable_copilot"):
        config.setdefault(feature, False)
        require(type(config[feature]) is bool, feature + " must be a boolean.")
    require(not config["enable_copilot"] or config["enable_knowledge"],
            "The native Copilot recipe includes knowledge Q&A and requires approved native knowledge.")
    require(isinstance(config["label"], str) and 1 <= len(config["label"]) <= 50 and
            "\n" not in config["label"] and "\r" not in config["label"],
            "Use a short nonpersonal workspace label.")
    config.setdefault("approvals", {})
    approvals = config["approvals"]
    require(isinstance(approvals, dict) and approvals.keys() <= BASE_APPROVALS | LATER_APPROVALS and
            all(type(item) is bool for item in approvals.values()),
            "Approvals must be documented boolean keys, never strings.")
    config["approvals"] = {key: approvals.get(key, False)
                           for key in sorted(BASE_APPROVALS | (approvals.keys() & LATER_APPROVALS))}
    if "representative_setup" in config:
        selected = config["representative_setup"]
        require(isinstance(selected, dict) and set(selected) == REPRESENTATIVE_SETUP_FIELDS,
                "representative_setup needs exactly user_id, entra_object_id, queue_id and capacity_profile_id.")
        config["representative_setup"] = {key: guid(item) for key, item in sorted(selected.items())}
        require(config["representative_setup"]["user_id"] in config["representative_ids"],
                "representative_setup.user_id must be one of the approved representative_ids.")
    return config


def owned_id(config, key):
    namespace = uuid5(NAMESPACE_URL, "dynamics-workspace/" + config["namespace"] + "/" + config["prefix"])
    return str(uuid5(namespace, config["org_url"] + "/" + key))


def content_equal(field, actual, expected):
    if field == "content":
        return html_digest(actual or "") == html_digest(expected)
    if field == "formxml":
        return ET.canonicalize(actual or "<missing/>") == ET.canonicalize(expected)
    if field == "clientdata":
        return json.loads(actual or "{}", object_pairs_hook=list) == json.loads(expected or "{}", object_pairs_hook=list)
    if field == "msdyn_templateparameters":
        return json.loads(actual or "{}") == json.loads(expected or "{}")
    return actual == expected


def check_payload(row, payload, label):
    for key, expected in payload.items():
        if key.endswith("@odata.bind"):
            field = LOOKUPS[key.removesuffix("@odata.bind")]
            expected = expected.rsplit("(", 1)[1].rstrip(")")
        else:
            field = key
        require(content_equal(field, row.get(field), expected), f"Owned record drift: {label}/{field}. No overwrite performed.")


def generated_semantics(row):
    return {key: value for key, value in row.items() if "@" not in key and key not in GENERATED_VOLATILE}


def visibility_values(value):
    try:
        selected = json.loads(value)
    except (TypeError, ValueError):
        raise GateError("Unsupported native Knowledge visibility-filter values.") from None
    require(isinstance(selected, list) and all(isinstance(item, str) for item in selected),
            "Unsupported native Knowledge visibility-filter values.")
    return selected


def case_section_label_id(identifier):
    return "{" + str(uuid5(NAMESPACE_URL, guid(identifier) + "/section-label")).upper() + "}"


def case_form_with_label_id(formxml, identifier):
    tree = ET.fromstring(formxml)
    section = one(tree.findall(".//section[@name='case_location']"), "case location section")
    desired = case_section_label_id(identifier)
    require(section.get("labelid") in (None, desired), "Case location section has an unreviewed label identity.")
    section.set("labelid", desired)
    return ET.tostring(tree, encoding="unicode")


def case_form_english_projection(formxml):
    tree = ET.fromstring(formxml)
    section = one(tree.findall(".//section[@name='case_location']"), "case location section")
    for group in section.findall(".//labels"):
        for label in list(group):
            if label.get("languagecode") == "1025":
                group.remove(label)
    return ET.tostring(tree, encoding="unicode")


def case_form_section_localization_repair(stored_xml, expected_xml, identifier):
    tree = ET.fromstring(stored_xml)
    section = one(tree.findall(".//section[@name='case_location']"), "stored case location section")
    wanted = one(ET.fromstring(expected_xml).findall(".//section[@name='case_location']"),
                 "requested case location section")
    require(section.get("id") == wanted.get("id") and section.get("labelid") is None,
            "The narrow correction requires the original section with no labelid.")
    require([node.attrib for node in section.findall("labels/label")] ==
            [node.attrib for node in wanted.findall("labels/label") if node.get("languagecode") == "1033"],
            "Stored section labels changed outside the known missing-Arabic condition.")
    arabic = one(wanted.findall("labels/label[@languagecode='1025']"), "requested Arabic section label")
    section.set("labelid", case_section_label_id(identifier))
    section.find("labels").append(copy.deepcopy(arabic))
    return ET.tostring(tree, encoding="unicode")


def localized_case_form_readback(api, row, expected_xml):
    """Prove stored labels separately; the ordinary systemform read is language-filtered."""
    if content_equal("formxml", row.get("formxml"), expected_xml):
        return {"mode": "exact", "formid": row["formid"]}
    expected = ET.fromstring(expected_xml)
    section = one(expected.findall(".//section[@name='case_location']"), "case location section")
    require(section.get("labelid") is None and
            [node.attrib for node in section.findall("labels/label")] ==
            [{"description": CASE_SECTION_HEADING, "languagecode": "1033"}],
            "The case location section must retain its approved shared bilingual heading.")
    owners = [section, *section.findall(".//cell")]
    require(len(owners) == 5, "Expected exactly the case location section and four existing-field labels.")

    def labels(tree):
        result = {}
        for owner in tree.iter():
            for label in owner.findall("labels/label"):
                identity = owner.get("id") or owner.get("name")
                require(identity is not None, "A localized label has no stable owner.")
                key = (owner.tag, identity.lower(), label.get("languagecode"))
                require(key not in result, "Duplicate localized form label.")
                result[key] = label.get("description")
        return result

    def layout(tree):
        tree = copy.deepcopy(tree)
        for parent in tree.iter():
            for child in list(parent):
                if child.tag == "labels":
                    parent.remove(child)
        return ET.tostring(tree, encoding="unicode")

    wanted = labels(expected)
    owned_keys = set()
    for owner in owners:
        languages = {node.get("languagecode") for node in owner.findall("labels/label")}
        expected_languages = {"1033"} if owner is section else {"1033", "1025"}
        require(languages == expected_languages, "Case location labels differ from the approved bilingual heading/field contract.")
        owned_keys.update((owner.tag, owner.get("id").lower(), language) for language in languages)
    require(content_equal("formxml", row.get("formxml"), case_form_english_projection(expected_xml)),
            "Published form differs beyond the four field-label projections.")

    def all_labels(published):
        full = api.get(record_path("systemforms", published["formid"]) + "/Microsoft.Dynamics.CRM.RetrieveUnpublished")
        require(published.get("formidunique") and published.get("componentstate") == full.get("componentstate") == 0 and
                full.get("formid") == published["formid"] and full.get("formidunique") == published["formidunique"] and
                etag(full) == etag(published),
                "Form has a distinct unpublished revision; do not normalize or publish unrelated drafts.")
        require(content_equal("formxml", layout(ET.fromstring(published["formxml"])),
                              layout(ET.fromstring(full["formxml"]))),
                "The full localization view differs from the published control layout.")
        return full

    full = all_labels(row)
    actual_labels = labels(ET.fromstring(full["formxml"]))
    require(all(actual_labels.get(key) == wanted[key] for key in owned_keys),
            "A requested stored form translation is missing or changed; locale correction is required.")
    inherited_labels = {key: value for key, value in actual_labels.items() if key not in owned_keys}
    require((set(wanted) - owned_keys) <= set(inherited_labels),
            "An inherited native form label is missing.")
    if inherited_labels:
        ancestor = row.get("_ancestorformid_value")
        require(ancestor, "Additional localized labels lack a source form.")
        source = api.get(record_path("systemforms", ancestor))
        inherited = labels(ET.fromstring(all_labels(source)["formxml"]))
        require(all(inherited.get(key) == value for key, value in inherited_labels.items()),
                "Unexpected non-source translation on the owned form.")
    return {
        "mode": "published-english-projection-with-stored-translations", "formid": row["formid"],
        "published_etag": etag(row), "localization_etag": etag(full), "formidunique": row["formidunique"],
        "published_formxml_sha256": digest(row["formxml"]), "localized_formxml_sha256": digest(full["formxml"]),
        "requested_formxml_sha256": digest(expected_xml),
        "verified_case_labels": [{"owner": key[1], "language": int(key[2]), "text": wanted[key]}
                                     for key in sorted(owned_keys)],
        "inherited_extra_labels": len(set(actual_labels) - set(wanted)),
        "inherited_labels_verified": len(inherited_labels), "browser_rendering_verified": False,
    }


def check_record_payload(api, row, payload, label):
    if "formxml" in payload and not content_equal("formxml", row.get("formxml"), payload["formxml"]):
        localized_case_form_readback(api, row, payload["formxml"])
        payload = {field: value for field, value in payload.items() if field != "formxml"}
    check_payload(row, payload, label)


def html_digest(value):
    class SemanticHTML(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.events = []

        def handle_starttag(self, tag, attrs):
            self.events.append(("start", tag, sorted(attrs)))

        def handle_endtag(self, tag):
            self.events.append(("end", tag))

        def handle_data(self, text):
            self.events.append(("text", text))

    parser = SemanticHTML()
    parser.feed(value)
    parser.close()
    return digest(parser.events)


def action(kind, inputs=None, after=None):
    require(kind in NATIVE_ACTIONS, "Unapproved macro action.")
    return {"type": kind, "inputs": inputs or {}, "runAfter": {after: ["Succeeded"]} if after else {}}


def branch(expression, yes, no):
    return {"type": "If", "expression": expression, "runAfter": {}, "actions": yes, "else": {"actions": no}}


def not_empty(slug):
    return {"not": {"equals": [slug, ""]}}


def macro_document(actions):
    return {
        "subcategory": "CDSClientAutomation", "schemaVersion": "1.0.0",
        "properties": {"definition": {
            "$schema": "https://schema.management.azure.com/providers/Microsoft.Logic/schemas/2016-06-01/workflowdefinition.json",
            "contentVersion": "1.0.0.0",
            "triggers": {"Start_macro_execution": {"type": "Start", "inputs": {}}},
            "actions": actions, "outputs": {},
        }},
    }


def new_case_parameters(form_id, *, customer=False):
    values = {"entityName": "incident", "formId": guid(form_id), "data": "{}"}
    if customer:
        # Native InitializeFrom uses existing mappings; names need no unsafe JSON interpolation.
        values["createFromEntity"] = json.dumps({"entityType": "{customerEntityName}", "id": "{customerRecordId}"})
    return values


def macros(ids, native, label):
    focused = {"and": [
        {"equals": ["${Session.CurrentTab.entityName}", "incident"]},
        not_empty("${Session.CurrentTab.entityId}"),
    ]}
    customer = {"and": [
        not_empty("${customerRecordId}"),
        {"or": [{"equals": ["${customerEntityName}", entity]} for entity in ("contact", "account")]},
    ]}
    find_cases = {"Condition_customer_linked": branch(
        customer,
        {"Find_customer_cases": action("Open_Application_Tab", {
            "PageType": "Entity Record", "ApplicationTemplateId": ids["case_lookup"],
        })},
        {"Find_customer": action("Open_Grid", {"EntityName": "contact"})},
    )}
    open_linked_case = {"Open_linked_case": action("Open_Application_Tab", {
        "PageType": "Entity Record", "ApplicationTemplateId": ids["voice_case"],
    })}
    cached_case = copy.deepcopy(open_linked_case)
    cached_case["Open_linked_case"]["inputs"]["Custom_Array"] = [{"Name": "entityId", "Value": "${caseId}"}]
    return {
        "review_customer": {"Condition_customer_linked": branch(
            not_empty("${customerRecordId}"),
            {"Condition_customer_is_contact": branch(
                {"equals": ["${customerEntityName}", "contact"]},
                {"Open_customer": action("Open_Form", {
                    "EntityName": "${customerEntityName}", "EntityId": "${customerRecordId}", "FormId": ids["contact_form"],
                })},
                {"Open_customer": action("Open_Form", {
                    "EntityName": "${customerEntityName}", "EntityId": "${customerRecordId}",
                })},
            )},
            {"Find_customer": action("Open_Grid", {"EntityName": "contact"})},
        )},
        "open_case": {"Condition_conversation_context": branch(
            {"contains": ["${LiveWorkItemId}", "-"]},
            {"Condition_current_case_linked": branch(
                not_empty("${" + CONVERSATION_CASE_SLUG + "}"), open_linked_case, copy.deepcopy(find_cases),
            )},
            {"Condition_cached_case_linked": branch(
                not_empty("${caseId}"), cached_case, copy.deepcopy(find_cases),
            )},
        )},
        "find_cases": find_cases,
        "new_case": {"Condition_customer_linked": branch(
            customer,
            {"New_case_with_customer": action("Open_Application_Tab", {
                "PageType": "Entity Record", "ApplicationTemplateId": ids["new_case_customer"],
            })},
            {"New_case_without_customer": action("Open_Application_Tab", {
                "PageType": "Entity Record", "ApplicationTemplateId": ids["new_case"],
            })},
        )},
        "link_case": {"Condition_saved_case_in_live_session": branch(
            {"and": focused["and"] + [{"contains": ["${LiveWorkItemId}", "-"]}]},
            {"Link_confirmed_case": action("Link_record_to_conversation", {
                "EntityName": "incident", "EntityId": "${Session.CurrentTab.entityId}",
                "PrimaryNameAttributeValue": "${$odata.incident.title.?$filter=incidentid eq '{Session.CurrentTab.entityId}'}",
            })},
            {"Find_saved_case": action("Open_Grid", {"EntityName": "incident"})},
        )},
        "followup_task": {"Condition_saved_case_focused": branch(
            focused,
            {"Prepare_followup_task": action("Create_New", {
                "EntityName": "task", "Custom_Array": [
                    {"Name": "subject", "Value": label + " | Case follow-up"},
                    {"Name": "regardingobjectid", "Value": "${Session.CurrentTab.entityId}"},
                    {"Name": "regardingobjectidtype", "Value": "incident"},
                    {"Name": "regardingobjectidname", "Value": "${$odata.incident.title.?$filter=incidentid eq '{Session.CurrentTab.entityId}'}"},
                    {"Name": "description", "Value": "Review next action, owner and agreed due date before saving. راجع الإجراء التالي والمسؤول والموعد المتفق عليه قبل الحفظ."},
                ],
            })},
            {"Find_saved_case": action("Open_Grid", {"EntityName": "incident"})},
        )},
        "refresh_tab": {
            "Get_current_tab": action("Get_current_page"),
            "Refresh_current_tab": action("Refresh", {"TabId": "@outputs('Get_current_tab')['TabId']"}, "Get_current_tab"),
        },
        "knowledge_search": {"Open_knowledge_search": action("Open_Application_Tab", {
            "PageType": "Web Resource", "ApplicationTemplateId": native["knowledge_tab"]["msdyn_applicationtabtemplateid"],
        })},
        "refresh_case_context": {"Refresh_case_session": action("Refresh_Session")},
    }


def next_form_display_order(forms, entity):
    orders = []
    for row in forms:
        conditions = ET.fromstring(row["formxml"]).find("DisplayConditions")
        if conditions is not None and conditions.get("Order") is not None:
            require(conditions.get("Order").isdigit(), f"Native {entity} form has an invalid explicit display order.")
            orders.append(int(conditions.get("Order")))
    require(orders, f"Native {entity} form has no explicit order baseline.")
    return str(max(orders) + 1)


def contact_form(source, forms, all_cases, identifier, label):
    tree = ET.fromstring(source["formxml"])
    grids = tree.findall(".//control[@id='RelatedCases']")
    grid = one(grids, "native RelatedCases grid")
    require(grid.findtext("parameters/RelationshipName") == "incident_customer_contacts",
            "The native grid must use the current-contact customerid relationship.")
    for name in ("ViewId", "ViewIds"):
        node = grid.find("parameters/" + name)
        require(node is not None, f"Native RelatedCases/{name} is missing.")
        node.text = "{" + all_cases["savedqueryid"].upper() + "}"
    display = tree.find("DisplayConditions")
    require(display is not None, "Source form has no display conditions; do not guess form order.")
    display.set("FallbackForm", "false")
    display.set("Order", next_form_display_order(forms, "contact"))
    payload = {
        "formid": identifier, "name": label + " | Contact cases / بلاغات العميل",
        "description": "Owned nondefault contact form; All Cases scoped by incident_customer_contacts.",
        "objecttypecode": "contact", "type": 2, "formxml": ET.tostring(tree, encoding="unicode"),
        "isdefault": False, "formactivationstate": 1,
        "ancestorformid@odata.bind": "/systemforms(" + source["formid"] + ")",
    }
    for key in ("formpresentation", "isairmerged", "isdesktopenabled", "istabletenabled"):
        if source.get(key) is not None:
            payload[key] = source[key]
    return payload


def case_form(source, forms, attributes, identifier, label):
    fields = {row["LogicalName"]: row for row in attributes}
    require(set(fields) == set(CASE_SECTION_FIELDS) and all(
        row.get("AttributeType") == "String" and
        all(row.get(flag) is True for flag in ("IsValidForRead", "IsValidForCreate", "IsValidForUpdate"))
        for row in fields.values()
    ), "Existing custom case text columns are missing or incompatible; run CRM setup, do not invent address notes.")
    tree = ET.fromstring(source["formxml"])
    require(not any(control.get("datafieldname") in fields for control in tree.findall(".//control")),
            "Source case form already contains the custom case controls; review their placement instead of duplicating them.")
    sections = tree.find("./tabs/tab[@name='general']/columns/column/sections")
    require(sections is not None, "Native case Details layout changed; do not guess a field placement.")
    title = one(tree.findall(".//control[@id='title']"), "native case title control")
    require(title.get("classid"), "Native case text-control class is missing.")
    display = tree.find("DisplayConditions")
    require(display is not None, "Source case form has no display conditions.")
    display.set("FallbackForm", "false")
    display.set("Order", next_form_display_order(forms, "case"))

    def xml_id(key):
        return "{" + str(uuid5(NAMESPACE_URL, identifier + "/" + key)).upper() + "}"

    def labels(parent, english, arabic):
        node = ET.SubElement(parent, "labels")
        for lcid, text in ((1033, english), (1025, arabic)):
            ET.SubElement(node, "label", description=text, languagecode=str(lcid))

    section = ET.Element("section", {
        "name": "case_location", "id": xml_id("section"), "showlabel": "true",
        "showbar": "true", "visible": "true", "columns": "1", "layout": "varwidth",
        "celllabelalignment": "Left", "celllabelposition": "Left", "labelwidth": "115",
    })
    heading = ET.SubElement(section, "labels")
    ET.SubElement(heading, "label", description=CASE_SECTION_HEADING, languagecode="1033")
    rows = ET.SubElement(section, "rows")
    for field, texts in CASE_SECTION_FIELDS.items():
        cell = ET.SubElement(ET.SubElement(rows, "row"), "cell", {
            "id": xml_id(field), "showlabel": "true", "visible": "true",
        })
        labels(cell, *texts)
        ET.SubElement(cell, "control", {
            "id": field, "datafieldname": field, "classid": title.get("classid"), "disabled": "false",
        })
    sections.insert(0, section)
    payload = {
        "formid": identifier, "name": label + " | Service case / حالة الخدمة",
        "description": "Owned nondefault case form; existing service columns, native timeline and Copilot controls.",
        "objecttypecode": "incident", "type": 2, "formxml": ET.tostring(tree, encoding="unicode"),
        "isdefault": False, "formactivationstate": 1,
        "ancestorformid@odata.bind": "/systemforms(" + source["formid"] + ")",
    }
    for key in ("formpresentation", "isairmerged", "isdesktopenabled", "istabletenabled"):
        if source.get(key) is not None:
            payload[key] = source[key]
    return payload


def render_workspace(config, native):
    prefix, label = config["prefix"], config["label"]
    ids, records, parameters, links = {}, [], [], []

    def ident(key):
        ids.setdefault(key, owned_id(config, key))
        return ids[key]

    def add(key, table, payload):
        identifier = ident(key)
        records.append({"key": key, "table": table, "id": identifier,
                        "payload": {TABLE_IDS[table]: identifier, **payload}})
        return identifier

    def link(table, source, relation, target_table, target):
        links.append({"table": table, "id": source, "relation": relation,
                      "target_table": target_table, "target_id": target})

    ident("contact_form")
    records.append({
        "key": "contact_form", "table": "systemforms", "id": ids["contact_form"],
        "payload": contact_form(native["contact_form"], native["contact_forms"], native["all_cases"],
                                ids["contact_form"], label),
    })
    ident("case_form")
    records.append({
        "key": "case_form", "table": "systemforms", "id": ids["case_form"],
        "payload": case_form(native["case_form"], native["case_forms"], native["case_attributes"],
                             ids["case_form"], label),
    })
    pins = {
        "voice_customer": ("{customerEntityName}", "{customerRecordId}"),
        "case_customer": ("{anchor._customerid_value@Microsoft.Dynamics.CRM.lookuplogicalname}", "{anchor._customerid_value}"),
        "case_lookup": ("{customerEntityName}", "{customerRecordId}"),
    }
    tabs = {
        **{key: {
            "entityName": entity, "entityId": identifier, "validateRecord": "true",
            "formId": "{$odata.systemform.formid.?$select=formid&$filter=objecttypecode eq '" +
                      entity + "' and formid eq " + ids["contact_form"] + "}",
        } for key, (entity, identifier) in pins.items()},
        "voice_case": {"entityName": "incident", "entityId": "{" + CONVERSATION_CASE_SLUG + "}",
                       "formId": ids["case_form"], "validateRecord": "true"},
        "new_case": new_case_parameters(ids["case_form"]),
        "new_case_customer": new_case_parameters(ids["case_form"], customer=True),
    }
    for key, values in tabs.items():
        tab = add(key, "msdyn_applicationtabtemplates", {
            "msdyn_name": label + " | " + key.replace("_", " "),
            "msdyn_uniquename": prefix + "_" + key,
            "msdyn_title": "Customer cases | بلاغات العميل" if key == "case_lookup" else
                           "New case: review | بلاغ جديد للمراجعة" if key.startswith("new_case") else
                           "Linked case | البلاغ المرتبط" if key == "voice_case" else "Customer | العميل",
            "msdyn_description": "Uses only real native associations. Opening a record does not link it.",
            "msdyn_pagetype": 509180001,
            "msdyn_pinned": False,
        })
        for name, value in values.items():
            parameters.append({"key": key + "_" + name, "id": ident(key + "_" + name),
                               "tab": tab, "name": name, "value": value})
    for key in ("voice_script", "case_script", "wrapup_script"):
        for language in ("en", "ar"):
            ident(key + "_" + language)
    for key, actions in macros(ids, native, label).items():
        add("macro_" + key, "workflows", {
            "name": prefix + " | " + key.replace("_", " "),
            "description": "Human initiated; no automatic save, case resolution, call or email. " +
                           ("Explicit confirmed association change; keep the saved case focused in the correct live session."
                            if key == "link_case" else "Review identity, record and context before use."),
            "category": 9000, "type": 1, "businessprocesstype": 0, "primaryentity": "systemuser",
            # The installed native client resolves Custom_Array after scalar inputs.
            "clientdata": json.dumps(macro_document(actions), ensure_ascii=False),
        })
    scripts = json.loads((DATA / "scripts.json").read_text())
    for key, script in scripts.items():
        for language, lcid in (("en", 1033), ("ar", 1025)):
            script_id = add(key + "_" + language, "msdyn_productivityagentscripts", {
                "msdyn_name": label + " | " + script["title"][language],
                "msdyn_uniquename": prefix + "_" + key + "_" + language,
                "msdyn_language": lcid,
                "msdyn_description": "Native " + language + " guidance; actions require representative confirmation.",
            })
            for order, step in enumerate(script["steps"], 1):
                step_key = key + "_" + language + "_" + step["key"]
                payload = {
                    "msdyn_name": step["title"][language],
                    "msdyn_uniquename": prefix + "_" + step_key,
                    "msdyn_order": order,
                    "msdyn_agentscriptid@odata.bind": f"/msdyn_productivityagentscripts({script_id})",
                }
                if "macro" in step:
                    payload.update({
                        "msdyn_actiontype": 192350001, "msdyn_description": step["instruction"][language],
                        "msdyn_macroactionid@odata.bind": f"/workflows({ids['macro_' + step['macro']]})",
                    })
                elif "route" in step:
                    payload.update({
                        "msdyn_actiontype": 192350002, "msdyn_description": step["instruction"][language],
                        "msdyn_routeactionid@odata.bind": f"/msdyn_productivityagentscripts({ids[step['route'] + '_' + language]})",
                    })
                else:
                    payload.update({"msdyn_actiontype": 192350000, "msdyn_textinstruction":
                                    step["instruction"][language] + "\n\n" + step["instruction"]["ar" if language == "en" else "en"]})
                add(step_key, "msdyn_productivityagentscriptsteps", payload)
    voice = add("voice_session", "msdyn_sessiontemplates", {
        "msdyn_name": label + " | Voice / مكالمة", "msdyn_uniquename": prefix + "_voice",
        "msdyn_description": "Scoped native voice handover; no changes to routing or voice defaults.",
        "msdyn_type": False, "msdyn_sessiontype": 0, "msdyn_panelstate": 162450002,
        "msdyn_title": label + " | {customerName}", "msdyn_enabletitlesync": False,
        "msdyn_enablebuildexpression": False,
        "msdyn_anchortab@odata.bind": "/msdyn_applicationtabtemplates(" + native["voice_session"]["_msdyn_anchortab_value"] + ")",
    })
    case = add("case_session", "msdyn_sessiontemplates", {
        "msdyn_name": label + " | Case / بلاغ", "msdyn_uniquename": prefix + "_case",
        "msdyn_description": "Dedicated case session; existing customer, case, timeline and native summary.",
        "msdyn_type": True, "msdyn_sessiontype": 1, "msdyn_entity": "incident",
        "msdyn_panelstate": 162450001, "msdyn_title": "{anchor.ticketnumber} | {anchor.title}",
        "msdyn_enabletitlesync": False, "msdyn_enablebuildexpression": False,
    })
    for session, tab in ((voice, "voice_customer"), (voice, "voice_case"), (case, "case_customer")):
        link("msdyn_sessiontemplates", session, "msdyn_sessiontemplate_applicationtab",
             "msdyn_applicationtabtemplates", ids[tab])
    for session, script in ((voice, "voice_script"), (case, "case_script")):
        for language in ("en", "ar"):
            link("msdyn_sessiontemplates", session, SESSION_SCRIPTS,
                 "msdyn_productivityagentscripts", ids[script + "_" + language])
    pane = add("pane", "msdyn_paneconfigurations", {
        "msdyn_name": label + " | Representative tools", "msdyn_uniquename": prefix + "_productivity",
        "msdyn_panestate": True, "msdyn_panemode": True,
    })
    tools = (["copilot_tool"] if config["enable_copilot"] else []) + (
        ["knowledge_tool"] if config["enable_knowledge"] else []
    ) + ["scripts_tool"]
    for order, key in enumerate(tools, 1):
        tool = native[key]
        add(key, "msdyn_panetabconfigurations", {
            "msdyn_name": label + " | " + tool["msdyn_name"], "msdyn_uniquename": prefix + "_" + key,
            "msdyn_isenabled": True, "msdyn_order": order,
            "msdyn_ProductivityPaneId@odata.bind": "/msdyn_paneconfigurations(" + pane + ")",
            "msdyn_ToolId@odata.bind": "/msdyn_panetoolconfigurations(" + tool["msdyn_panetoolconfigurationid"] + ")",
            **native.get("tool_presentation", {}).get(key, {}),
        })
    profile = add("profile", "msdyn_appconfigurations", {
        "msdyn_name": label, "msdyn_uniquename": prefix + "_representative",
        "msdyn_appmoduleuniquename": native["profile"]["msdyn_appmoduleuniquename"],
        "msdyn_description": "Explicit approved users only; nondefault, no role-based assignment.",
    })
    link("msdyn_appconfigurations", profile, "msdyn_msdyn_paneconfig_msdyn_appconfig",
         "msdyn_paneconfigurations", pane)
    link("msdyn_appconfigurations", profile, "msdyn_appconfiguration_sessiontemplate",
         "msdyn_sessiontemplates", case)
    for relation, table, primary in (
        ("msdyn_appconfiguration_applicationextension", "msdyn_applicationextensions", "msdyn_applicationextensionid"),
        ("msdyn_appconfig_msdyn_channelprovider", "msdyn_channelproviders", "msdyn_channelproviderid"),
    ):
        for row in native["profile_relations"][relation]:
            link("msdyn_appconfigurations", profile, relation, table, row[primary])
    for feature, enabled in ((829050000, True), (829050003, True), (829050004, True),
                             (829050001, False), (829050002, False), (829050006, False),
                             (829050008, False), (829050010, True)):
        add("copilot_" + str(feature), "msdyn_appcopilotconfigurations", {
            "msdyn_name": prefix + " | " + str(feature), "msdyn_copilotfeature": feature,
            "msdyn_enabled": enabled and config.get("enable_copilot", False),
            "msdyn_appconfigurationid@odata.bind": "/msdyn_appconfigurations(" + profile + ")",
        })
    # Route targets must exist before steps are created.
    records.sort(key=lambda item: item["table"] == "msdyn_productivityagentscriptsteps")
    return {"ids": ids, "records": records, "parameters": parameters, "links": links}


KNOWLEDGE_TOPICS_PER_ARTICLE = 6


def parse_topics(text):
    pieces = re.split(r"(?=^### \d+\. )", text, flags=re.M)[1:]
    topics = {}
    for piece in pieces:
        number = int(re.match(r"### (\d+)\.", piece)[1])
        require(number not in topics, "Duplicate knowledge topic.")
        topics[number] = re.split(r"^## ", piece, flags=re.M)[0].strip()
    return topics


def knowledge_html(text, registry):
    def inline(value):
        value = html.escape(value)
        return re.sub(r"\[(S\d+)\]", lambda match:
                      '<a href="' + html.escape(registry[match[1]]["url"], quote=True) + '">' +
                      html.escape(registry[match[1]]["title"]) + "</a>", value)
    chunks = []
    for block in re.split(r"\n\s*\n", text.strip()):
        if block.startswith("### "):
            chunks.append("<h2>" + inline(block[4:]) + "</h2>")
        elif block.startswith("- "):
            items = re.split(r"(?m)^- ", block)[1:]
            chunks.append("<ul>" + "".join("<li>" + inline(" ".join(item.split())) + "</li>"
                                          for item in items) + "</ul>")
        else:
            chunks.append("<p>" + inline(" ".join(block.splitlines())) + "</p>")
    return "".join(chunks)


def render_knowledge(config, locales, canonical=None, arabic=None):
    canonical = CANONICAL.read_text() if canonical is None else canonical
    arabic = (DATA / "knowledge-ar.md").read_text() if arabic is None else arabic
    revision = re.search(r"^Revision: (.+)$", canonical, re.M)
    require(revision, "Approved knowledge requires a revision.")
    if not config.get("enable_knowledge", False):
        return {
            "revision": revision[1], "canonical_sha256": digest(canonical),
            "source_count": 0, "topic_count": 0, "article_count": 0,
            "groups": [], "locales": locales, "registry": {},
            "root_category_id": owned_id(config, "category_root"),
            "root_category_title": config["prefix"] + " | Organization knowledge",
        }
    translated_revision = re.search(r"^Revision: (.+)$", arabic, re.M)
    translated_hash = re.search(r"^Canonical-SHA256: ([a-f0-9]{64})$", arabic, re.M)
    require(revision and translated_revision and translated_hash and
            revision[1] == translated_revision[1] and digest(canonical) == translated_hash[1],
            "Canonical knowledge changed. Review/rebase the Arabic rendering and its hash; do not silently publish stale translations.")
    registry = {m[1]: {"url": m[2], "title": m[3]}
                for m in re.finditer(r'^\[(S\d+)\]: (\S+) "([^"]+)"$', canonical, re.M)}
    en_topics, ar_topics = parse_topics(canonical), parse_topics(arabic)
    require(en_topics and set(en_topics) == set(ar_topics) and registry and len(en_topics) <= 99,
            "Native knowledge requires matching reviewed bilingual topics and a source registry.")
    for language, topics in (("English", en_topics), ("Arabic", ar_topics)):
        for number, text in sorted(topics.items()):
            require(not REGISTRY_LINE.search(text),
                    f"{language} topic {number:02d} contains source-registry lines. End the last topic with a "
                    "`## Source registry` heading and put the registry under it.")
    for number in en_topics:
        original_sources = set(re.findall(r"\[(S\d+)\]", en_topics[number]))
        translated_sources = set(re.findall(r"\[(S\d+)\]", ar_topics[number]))
        require(original_sources == translated_sources and original_sources <= registry.keys(),
                "Translated topics must retain the exact approved source references.")
    for item in registry.values():
        url = urllib.parse.urlsplit(item["url"])
        require(url.scheme == "https" and url.hostname and not url.username and not url.password,
                "Knowledge citations must be approved HTTPS sources without credentials.")
    used_sources, groups = set(), []
    topic_ids = sorted(en_topics)
    for offset in range(0, len(topic_ids), KNOWLEDGE_TOPICS_PER_ARTICLE):
        index = offset // KNOWLEDGE_TOPICS_PER_ARTICLE + 1
        selected = topic_ids[offset:offset + KNOWLEDGE_TOPICS_PER_ARTICLE]
        start, end = selected[0], selected[-1]
        english = re.sub(r"^### \d+\. ", "", en_topics[start].splitlines()[0])
        translated = re.sub(r"^### \d+\. ", "", ar_topics[start].splitlines()[0])
        sources = sorted(set(re.findall(r"\[(S\d+)\]", "\n".join(en_topics[n] for n in selected))))
        require(set(sources) <= registry.keys(), "Unknown canonical source.")
        used_sources.update(sources)
        number = config["prefix"].upper() + "-KB-" + f"{index:02d}"
        versions = {}
        for language, title, direction in (("en-US", english, "ltr"), ("ar-SA", translated, "rtl")):
            if language == "en-US":
                editorial = (
                    "Representative reference — owner-approved organization guidance. Source: knowledge.md, revision " +
                    revision[1] + ". This revision is not a claim that underlying laws or service conditions were newly researched. "
                    "Check changing requirements and individual decisions in the official service. Guidance is not a guarantee of eligibility, "
                    "approval, cost, exemption or deadline. Recorded requests here are Dynamics cases, not proof of an external action. "
                    "Knowledge does not prove a person's current case status or configure Copilot live-case querying."
                )
            else:
                editorial = (
                    "مرجع ممثل الخدمة — إرشادات معتمدة من مالك المحتوى. المصدر: knowledge.md، النسخة " +
                    revision[1] + ". تُراجع المصادر المعتمدة للمتطلبات المتغيرة والقرارات الفردية. "
                    "المادة ليست ضماناً للأهلية أو الموافقة أو التكلفة أو المهلة. تسجيل حالة Dynamics لا يثبت تنفيذ إجراء خارجي. "
                    "المعرفة لا تثبت حالة بلاغ شخص ولا تهيئ استعلام Copilot عن البلاغات الحية."
                )
            chunks = [f'<div lang="{language}" dir="{direction}"><p>{html.escape(editorial)}</p>']
            for topic in selected:
                text = ar_topics[topic] if language == "ar-SA" else en_topics[topic]
                chunks.append(knowledge_html(text, registry))
            if sources:
                chunks.append("<h2>" + ("المصادر الرسمية" if language == "ar-SA" else "Official sources") + "</h2><ul>")
                for source in sources:
                    item = registry[source]
                    chunks.append('<li><a dir="ltr" href="' + html.escape(item["url"], quote=True) + '">' +
                                  html.escape(item["title"]) + ": " + html.escape(item["url"]) + "</a></li>")
                chunks.append("</ul>")
            chunks.append("<p>" + html.escape(number + " | " + revision[1]) + "</p></div>")
            content = "".join(chunks)
            require(not re.search(r"\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b|\+\d{10,15}\b", content),
                    "Personal contact details must not enter representative knowledge.")
            versions[language] = {
                "title": config["prefix"] + " | " + title, "content": content,
                "description": ("إرشادات مراجعة؛ تحقق من التفاصيل المتغيرة." if language == "ar-SA" else
                                "Reviewed guidance; verify changing details.") + f" {start:02d}–{end:02d} | {revision[1]}",
                "keywords": "; ".join((config["prefix"], config["label"], english, translated)),
                "isinternal": True,
            }
        groups.append({
            "index": index, "topics": selected, "sources": sources,
            "number": number, "en_id": owned_id(config, "article_" + str(index) + "_en"),
            "category_id": owned_id(config, "category_" + str(index)),
            "category_title": config["prefix"] + f" {index:02d} | " + english + " / " + translated,
            "versions": versions,
        })
    require(used_sources == registry.keys(), "Every approved source must be used by the native package.")
    return {
        "revision": revision[1], "canonical_sha256": digest(canonical), "source_count": len(registry), "topic_count": len(topic_ids),
        "article_count": 2 * len(groups), "groups": groups, "locales": locales, "registry": registry,
        "root_category_id": owned_id(config, "category_root"),
        "root_category_title": config["prefix"] + " | Organization knowledge / معرفة المؤسسة",
    }


def app_authored(app, *, remove_form=None, without_components=False):
    remove_forms = {remove_form} if isinstance(remove_form, str) else set(remove_form or ())
    result = {key: copy.deepcopy(app.get(key)) for key in APP_FIELDS}
    require(result["descriptor"] and result["configxml"], "Native app descriptor/configxml missing.")
    descriptor = json.loads(result["descriptor"])
    info = descriptor["appInfo"]
    for key in ("PublishedOn", "Status", "VersionNumber", "SolutionId"):
        info.pop(key, None)
    descriptor.get("publisherInfo", {}).pop("Id", None)
    entities = info.get("AppComponents", {}).get("Entities")
    if entities is not None:
        info["AppComponents"]["Entities"] = sorted(entities, key=lambda row: row["Id"])
    info["Components"] = [] if without_components else sorted(
        [row for row in info.get("Components", [])
         if not (row["Type"] == 60 and row["Id"].strip("{}").lower() in remove_forms)],
        key=lambda row: (row["Type"], row["Id"]),
    )
    result["descriptor"] = descriptor
    tree = ET.fromstring(result["configxml"])
    components = tree.find("AppModuleComponents")
    if components is not None:
        for node in list(components):
            if without_components or (node.get("type") == "60" and
                                      node.get("id", "").strip("{}").lower() in remove_forms):
                components.remove(node)
        components[:] = sorted(components, key=lambda node: (node.get("type", ""), node.get("id", ""), node.get("schemaName", "")))
    result["configxml"] = ET.canonicalize(ET.tostring(tree, encoding="unicode"))
    return result


def component_set(rows):
    return {(row["componenttype"], row["objectid"].lower()) for row in rows}


def component_defaults(rows):
    return {f"{row['componenttype']}:{row['objectid'].lower()}":
            {key: row.get(key) for key in ("componenttype", "objectid", "isdefault", "rootcomponentbehavior")}
            for row in rows}


def article_inventory(rows):
    fields = ("knowledgearticleid", "articlepublicnumber", "statecode", "statuscode", "isrootarticle", "@odata.etag")
    return {row["knowledgearticleid"]: {field: row.get(field) for field in fields} for row in rows}


def all_profiles(api):
    profiles = api.rows("msdyn_appconfigurations", {
        "$select": "msdyn_appconfigurationid,msdyn_uniquename,msdyn_appmoduleuniquename",
    })
    for profile in profiles:
        profile[PROFILE_USERS] = api.rows(
            record_path("msdyn_appconfigurations", profile["msdyn_appconfigurationid"]) + "/" + PROFILE_USERS,
            {"$select": "systemuserid"},
        )
    return profiles


def profile_memberships(profiles):
    return {row["msdyn_appconfigurationid"]: sorted(user["systemuserid"] for user in row[PROFILE_USERS])
            for row in profiles}


def discover(api, config):
    def managed(table, field, value, extra=""):
        row = one(api.rows(table, {"$filter": field + " eq " + quote(value) + ((" and " + extra) if extra else "")}),
                  f"managed {table}/{value}")
        require(row.get("ismanaged") is True, f"{table}/{value} is not a Microsoft managed source.")
        return row

    who = api.get("WhoAmI")
    app = api.get(record_path("appmodules", config["app_id"]))
    require(app["uniquename"] == "msdyn_CustomerServiceWorkspace",
            "This bounded recipe supports the native Customer Service workspace app only.")
    draft = api.get(record_path("appmodules", config["app_id"]) + "/Microsoft.Dynamics.CRM.RetrieveUnpublished")
    require(app_authored(app) == app_authored(draft), "Unrelated unpublished app changes; finish or discard them before planning.")
    native = {
        "who": who, "app": app, "app_draft": draft,
        "profile": managed("msdyn_appconfigurations", "msdyn_uniquename", "msdyn_csw_app_configuration"),
        "voice_session": managed("msdyn_sessiontemplates", "msdyn_uniquename", "msdyn_voicecall_session"),
        "knowledge_tab": managed("msdyn_applicationtabtemplates", "msdyn_uniquename", "msdyn_omnichannel_kbsearch"),
        "case_form": managed("systemforms", "name", "Enhanced full case form", "objecttypecode eq 'incident' and type eq 2"),
        "contact_form": managed("systemforms", "name", "Contact for Multisession experience", "objecttypecode eq 'contact' and type eq 2"),
        "all_cases": managed("savedqueries", "name", "All Cases", "returnedtypecode eq 'incident' and querytype eq 0"),
    }
    require(native["voice_session"].get("_msdyn_anchortab_value"), "Native Active Conversation anchor missing.")
    for key, name in (("copilot_tool", "msdyn_agentcopilot_tool_config"),
                      ("knowledge_tool", "msdyn_knowledgesearch_tool_config"),
                      ("scripts_tool", "msdyn_callscript_tool_config")):
        native[key] = managed("msdyn_panetoolconfigurations", "msdyn_uniquename", name)
    view = ET.fromstring(native["all_cases"]["fetchxml"])
    require(view.find("entity").get("name") == "incident" and not any(
        node.get("attribute") in {"ownerid", "statecode", "statuscode"} for node in view.findall(".//condition")
    ), "All Cases must not exclude resolved cases or filter by current owner.")
    relation = one(api.rows("EntityDefinitions(LogicalName='contact')/OneToManyRelationships", {
        "$filter": "SchemaName eq 'incident_customer_contacts'",
        "$select": "SchemaName,ReferencedEntity,ReferencingEntity,ReferencingAttribute",
    }), "contact-customer case relationship")
    require((relation["ReferencedEntity"], relation["ReferencingEntity"], relation["ReferencingAttribute"]) ==
            ("contact", "incident", "customerid"), "Unexpected contact relationship schema.")
    native["contact_forms"] = api.rows("systemforms", {
        "$filter": "objecttypecode eq 'contact' and type eq 2",
        "$select": "formid,name,type,isdefault,formactivationstate,formxml",
    })
    native["case_forms"] = api.rows("systemforms", {
        "$filter": "objecttypecode eq 'incident' and type eq 2",
        "$select": "formid,name,type,isdefault,formactivationstate,formxml",
    })
    native["case_attributes"] = api.rows("EntityDefinitions(LogicalName='incident')/Attributes", {
        "$select": "LogicalName,AttributeType,IsValidForRead,IsValidForCreate,IsValidForUpdate",
        "$filter": " or ".join("LogicalName eq " + quote(field) for field in CASE_SECTION_FIELDS),
    })
    native["customer_case_mappings"] = {}
    for customer, primary in (("contact", "contactid"), ("account", "accountid")):
        mapping = one(api.rows("entitymaps", {
            "$select": "entitymapid",
            "$filter": "sourceentityname eq " + quote(customer) + " and targetentityname eq 'incident'",
        }), customer + " to case native mapping")
        mapped = api.rows("attributemaps", {
            "$select": "sourceattributename,targetattributename",
            "$filter": "_entitymapid_value eq " + mapping["entitymapid"],
        })
        require(any(row["sourceattributename"] == primary and row["targetattributename"] == "customerid"
                    for row in mapped), "Native InitializeFrom must bind the " + customer + " to the case customer.")
        native["customer_case_mappings"][customer] = mapped
    native["profile_relations"] = {}
    profile_path = record_path("msdyn_appconfigurations", native["profile"]["msdyn_appconfigurationid"])
    for relation in ("msdyn_appconfiguration_applicationextension", "msdyn_appconfig_msdyn_channelprovider"):
        native["profile_relations"][relation] = api.rows(profile_path + "/" + relation, {})
    source_panes = api.rows(profile_path + "/msdyn_msdyn_paneconfig_msdyn_appconfig", {})
    pane = one(source_panes, "managed profile productivity pane")
    source_tabs = api.rows("msdyn_panetabconfigurations", {
        "$filter": "_msdyn_productivitypaneid_value eq " + pane["msdyn_paneconfigurationid"],
    })
    native["tool_presentation"] = {}
    for key in ("copilot_tool", "knowledge_tool", "scripts_tool"):
        matching = [row for row in source_tabs if row["_msdyn_toolid_value"] == native[key]["msdyn_panetoolconfigurationid"]]
        source = one(matching, key + " presentation")
        native["tool_presentation"][key] = {field: source[field] for field in ("msdyn_iconpath", "msdyn_tooltip")
                                          if source.get(field) is not None}
    native["macro_actions"] = api.rows("msdyn_productivitymacroactiontemplates", {
        "$select": "msdyn_name,msdyn_runtimeapi,ismanaged",
        "$filter": "ismanaged eq true and (" + " or ".join("msdyn_name eq " + quote(name) for name in sorted(NATIVE_ACTIONS)) + ")",
    })
    require(len(native["macro_actions"]) == len(NATIVE_ACTIONS) and
            {row["msdyn_name"] for row in native["macro_actions"]} == NATIVE_ACTIONS and
            all(row.get("ismanaged") is True for row in native["macro_actions"]),
            "Installed native macro action catalog is missing or ambiguous.")
    native["languages"] = api.get("RetrieveProvisionedLanguages()")["RetrieveProvisionedLanguages"]
    native["locales"] = {}
    for code, lcid in ((("en-US", 1033), ("ar-SA", 1025)) if config["enable_knowledge"] else ()):
        locale = one(api.rows("languagelocale", {"$filter": f"localeid eq {lcid} and code eq {quote(code)}"}),
                     code + " knowledge locale")
        require(locale.get("statecode") == 0, f"{code} knowledge locale is inactive.")
        native["locales"][code] = locale["languagelocaleid"]
    native["user_settings"] = {}
    for user in config["representative_ids"]:
        row = api.get(record_path("systemusers", user), {"$select": "systemuserid,isdisabled,accessmode"})
        require(row["isdisabled"] is False and row["accessmode"] == 0, "Approved representative must be an enabled interactive read/write user.")
        native["user_settings"][user] = api.get(record_path("usersettingscollection", user), {
            "$select": "uilanguageid,helplanguageid,localeid",
        })
    native["workstreams"] = [api.get(record_path("msdyn_liveworkstreams", identifier), {
        "$select": "msdyn_liveworkstreamid,msdyn_name,msdyn_sessiontemplate_default",
    }) for identifier in config["workstream_ids"]]
    require(all(row.get("msdyn_sessiontemplate_default") == "msdyn_voicecall_session" for row in native["workstreams"]),
            "Selected workstreams do not all use the managed voice session; existing custom routes are not replaced.")
    native["profiles"] = all_profiles(api)
    native["source_assignments"] = {}
    memberships = profile_memberships(native["profiles"])
    for user in config["representative_ids"]:
        effective = api.get("msdyn_getAppConfigByAgent(UserId=" + quote(user) +
                            ",AppUniqueName=" + quote(app["uniquename"]) + ")")
        source = one(json.loads(effective["AppConfigurations"]), "effective source representative profile")["AppConfigId"]
        require(source in memberships, "Effective source profile is outside the discovered inventory.")
        native["source_assignments"][user] = source if user in memberships[source] else None
    native["shared"] = {}
    for family, (table, _) in (SHARED_TABLES.items() if config["enable_copilot"] else ()):
        query = {"$filter": "msdyn_summarytype eq 0"} if family == "modern" else {}
        native["shared"][family] = one(api.rows(table, query), family + " native settings row")
    native["app_components"] = api.rows("appmodulecomponents", {
        "$filter": "_appmoduleidunique_value eq " + app["appmoduleidunique"],
    })
    native["app_validation"] = api.get("ValidateApp(AppModuleId=" + config["app_id"] + ")")
    native["unrelated_knowledge"] = api.rows("knowledgearticles", {
        "$select": "knowledgearticleid,articlepublicnumber,statecode,statuscode,isrootarticle",
    }) if config["enable_knowledge"] else []
    return native


def representative_state(api, config):
    """Read the selected representative's roles, resource, capacity, fallback queue and route."""
    selected = config["representative_setup"]
    user, queue, profile = selected["user_id"], selected["queue_id"], selected["capacity_profile_id"]
    state = routing.user_state(api, user)
    resources = routing.user_resources(api, user)
    route = []
    for identifier in config["workstream_ids"]:
        route.append({
            "workstream": api.get(record_path("msdyn_liveworkstreams", identifier), {"$select": ROUTE_FIELDS}),
            "bindings": api.rows("msdyn_liveworkstreamcapacityprofiles", {
                "$select": BINDING_FIELDS, "$filter": "_msdyn_workstream_id_value eq " + identifier,
            }),
        })
    members = api.rows(record_path("queues", queue) + "/" + routing.MEMBERS, {"$select": "systemuserid"})
    return {
        **state,
        "fullname": api.get(record_path("systemusers", user), {"$select": "fullname"}).get("fullname"),
        "timezonecode": api.get(record_path("usersettingscollection", user), {"$select": "timezonecode"}).get("timezonecode"),
        "resources": resources,
        "capacities": routing.resource_capacities(api, [guid(row["bookableresourceid"]) for row in resources]),
        "queue": api.get(record_path("queues", queue), {"$select": routing.QUEUE_FIELDS}),
        "member": any(guid(row["systemuserid"]) == user for row in members),
        "profile": api.get(record_path("msdyn_capacityprofiles", profile)),
        "route": route,
    }


def representative_view(state):
    """Stable comparison of representative setup state; ignores ETags and counters."""
    return {
        "user": routing.clean(state["user"]),
        "direct_roles": sorted(guid(row["roleid"]) for row in state["direct_roles"]),
        "team_roles": sorted([guid(team["team"]["teamid"]), guid(row["roleid"])]
                             for team in state["teams"] for row in team["roles"]),
        "resources": sorted([guid(row["bookableresourceid"]), row.get("_userid_value"), row.get("resourcetype"),
                             row.get("statecode")] for row in state["resources"]),
        "capacities": sorted([guid(row[routing.CAPACITY_ID]), row.get("_msdyn_bookableresourceid_value"),
                              row.get("_msdyn_capacityprofileid_value"), row.get("msdyn_maxunits"),
                              row.get("statecode"), row.get("statuscode")] for row in state["capacities"]),
        "queue": routing.clean(state["queue"]),
        "member": state["member"],
        "profile": {field: state["profile"].get(field) for field in PROFILE_POLICY},
        "route": routing.clean(state["route"]),
    }


def plan_representative(api, config):
    """Plan only missing roles, resource, inbound capacity association and fallback-queue membership."""
    selected = config["representative_setup"]
    user, queue, profile = selected["user_id"], selected["queue_id"], selected["capacity_profile_id"]
    contracts = routing.relationship_contract(api)
    lookup = api.rows("EntityDefinitions(LogicalName='bookableresource')/ManyToOneRelationships", {
        "$filter": "ReferencingAttribute eq 'userid'",
        "$select": "ReferencedEntity,ReferencingAttribute,ReferencingEntityNavigationPropertyName",
    })
    require([(row["ReferencedEntity"], row["ReferencingAttribute"], row["ReferencingEntityNavigationPropertyName"])
             for row in lookup] == [("systemuser", "userid", "UserId")],
            "Unexpected bookable resource user lookup contract.")
    before = representative_state(api, config)
    gates = routing.human_gates(before, {"user_id": user, "entra_object_id": selected["entra_object_id"]},
                                "Representative")
    effective = {row["name"].strip().casefold() for row in routing.effective_roles(before).values()}
    business_unit = before["user"].get("_businessunitid_value")
    operations = []
    for name in REPRESENTATIVE_ROLES:
        if name.casefold() in effective:
            continue
        matches = api.rows("roles", {
            "$select": routing.ROLE_FIELDS,
            "$filter": "name eq " + quote(name) + " and _businessunitid_value eq " + guid(business_unit),
        }) if business_unit else []
        if len(matches) != 1:
            gates.append(f"Role {name}: expected exactly one in the representative's business unit; found {len(matches)}.")
            continue
        operations.append({"kind": "role", "name": name, "role": matches[0], "link": {
            "table": "systemusers", "id": user, "relation": "systemuserroles_association",
            "target_table": "roles", "target_id": guid(matches[0]["roleid"]),
        }})
    active = [row for row in before["resources"] if row.get("statecode") == 0]
    resource = None
    if not before["resources"]:
        name, zone = before["fullname"], before["timezonecode"]
        if isinstance(name, str) and name.strip() and type(zone) is int:
            resource = owned_id(config, "representative_resource")
            operations.append({"kind": "resource", "item": {
                "key": "representative_resource", "table": "bookableresources", "id": resource, "payload": {
                    "bookableresourceid": resource, "name": name.strip()[:100], "resourcetype": 3,
                    "timezone": zone, "UserId@odata.bind": "/systemusers(" + user + ")",
                },
            }})
        else:
            gates.append("The representative needs a full name and time zone before a bookable resource is created.")
    elif len(active) == 1 and active[0].get("resourcetype") == 3 and active[0].get("_userid_value") == user:
        resource = guid(active[0]["bookableresourceid"])
    else:
        gates.append("Review this user's bookable resources: expected none, or exactly one active User resource.")
    if not routing.capacity_profile_ok(before["profile"], profile):
        gates.append("The inbound capacity profile must be active and published with a default limit of 1-100.")
    if not routing.voice_queue_ok(before["queue"], queue):
        gates.append("The fallback queue must be an active Omnichannel voice queue.")
    if not any(routing.inbound_voice_workstream_ok(item["workstream"], identifier, queue) and
               routing.single_profile_binding_ok(item["bindings"], identifier, profile)
               for identifier, item in zip(config["workstream_ids"], before["route"])):
        gates.append("No selected workstream is inbound voice with this fallback queue and this capacity profile.")
    if resource:
        pairs = [row for row in before["capacities"] if row.get("_msdyn_bookableresourceid_value") == resource
                 and row.get("_msdyn_capacityprofileid_value") == profile]
        if not pairs:
            association = owned_id(config, "representative_capacity")
            operations.append({"kind": "capacity", "item": {
                "key": "representative_capacity", "table": routing.CAPACITY, "id": association,
                "payload": routing.capacity_body(association, resource, profile, None),
            }})
        elif not (len(pairs) == 1 and pairs[0].get("statecode") == 0 and pairs[0].get("statuscode") == 1):
            gates.append("The resource's association with this capacity profile is inactive or duplicated; it is never overwritten.")
    if not before["member"]:
        operations.append({"kind": "membership", "link": {
            "table": "queues", "id": queue, "relation": routing.MEMBERS,
            "target_table": "systemusers", "target_id": user,
        }})
    return {
        "user_id": user, "queue_id": queue, "capacity_profile_id": profile, "resource_id": resource,
        "contracts": {**contracts, "resource_user": lookup}, "before": before,
        "gates": sorted(set(gates)), "operations": operations,
    }


def visibility_filter_item(config, user):
    key = "knowledge_visibility_" + user
    identifier = owned_id(config, key)
    return {"key": key, "user": user, "table": "msdyn_knowledgepersonalfilters", "id": identifier, "payload": {
        "msdyn_knowledgepersonalfilterid": identifier, "msdyn_name": "Visibility",
        "msdyn_entityname": "knowledgearticle", "msdyn_attributename": "isinternal",
        "msdyn_enabled": True, "msdyn_preferredvalues": json.dumps(["1"]),
    }}


def input_hashes():
    return {"script": digest(Path(__file__).read_text()), "canonical": digest(CANONICAL.read_text()),
            **{name: digest((DATA / name).read_text()) for name in (
                "scripts.json", "knowledge-ar.md",
            )}}


def recipe_updates(plan, journal, candidate=None):
    latest = journal.state.get("recipe_update")
    history = journal.state.get("recipe_updates", [] if latest is None else [latest])
    require(isinstance(history, list) and bool(history) == (latest is not None) and
            (not history or history[-1] == latest),
            "Accepted recipe update history is missing or inconsistent.")
    if candidate is not None:
        history = [*history, candidate]
    previous = plan["input_hashes"]
    for update in history:
        require(isinstance(update, dict) and update.get("kind") == "workspace-script-update-v1" and
                update.get("plan_sha256") == digest(plan) and
                update.get("previous_input_hashes") == previous and
                isinstance(update.get("input_hashes"), dict) and
                set(update["input_hashes"]) == set(previous) and
                all(update["input_hashes"][key] == value for key, value in previous.items() if key != "script"),
                "Accepted recipe update does not match the exact original plan and previous script/data.")
        require(digest(read_private(private_path(update["previous_script"]))) == previous["script"],
                "A previous plan-bound source backup is missing or changed.")
        previous = update["input_hashes"]
    return history, previous


def require_recipe(plan, journal, candidate=None):
    history, expected = recipe_updates(plan, journal, candidate)
    require(expected == input_hashes(),
            "Accepted recipe update does not match the exact original plan and current script/data." if history else
            "Base recipe/configuration changed; do not rewrite its immutable plan. A reviewed script-only update needs explicit acceptance.")


def accept_recipe_update(operator, previous_script, *, approved, confirmed_organization):
    plan, journal = operator.plan, operator.journal
    require(approved and confirmed_organization == plan["config"]["org_url"] and previous_script,
            "Recipe acceptance needs --approve-recipe-update, --previous-script and the exact --confirm-organization.")
    require(not journal.state["pending"] and not journal.state["rolled_back"],
            "Recover pending mutations before a recipe update; rolled-back deployments cannot migrate.")
    history, previous = recipe_updates(plan, journal)
    current = input_hashes()
    require(set(current) == set(plan["input_hashes"]) and
            current["script"] != plan["input_hashes"]["script"] and
            all(current[key] == value for key, value in plan["input_hashes"].items() if key != "script"),
            "Only an explicitly reviewed script change is accepted; configuration/data changes require a separate migration.")
    previous_script = private_path(previous_script)
    if history and current == previous:
        require(history[-1]["previous_script"] == str(previous_script),
                "The accepted recipe update references another previous source backup.")
        require_recipe(plan, journal)
        return
    require(digest(read_private(previous_script)) == previous["script"],
            "The supplied previous script does not match the previous accepted recipe or original immutable plan.")
    require(plan["workspace"] == render_workspace(plan["config"], plan["native"]) and
            plan["knowledge"] == render_knowledge(plan["config"], plan["knowledge"]["locales"]),
            "A script-only update must preserve the exact planned workspace and approved knowledge payloads.")
    update = {
        "kind": "workspace-script-update-v1", "accepted_at": utc(),
        "plan_sha256": digest(plan), "previous_input_hashes": previous,
        "input_hashes": current, "previous_script": str(previous_script),
    }
    operator.recipe_update_candidate = update
    try:
        operator.idle()
        operator.user_settings_unchanged()
        operator.verify_workspace()
        operator.published_app_ok()
        operator.verify_knowledge(search=True, record_search=False)
    finally:
        operator.recipe_update_candidate = None
    journal.save("recipe_update_accepted", {"recipe_update": update,
                                          "recipe_updates": [copy.deepcopy(item) for item in [*history, update]]},
                 {"dataverse_writes": False, "original_plan_and_events_unchanged": True,
         "existing_workspace_and_approved_native_search_verified": True})


def build_plan(api, config):
    native = discover(api, config)
    workspace = render_workspace(config, native)
    knowledge = render_knowledge(config, native["locales"])
    representative = plan_representative(api, config) if "representative_setup" in config else None
    visibility = [visibility_filter_item(config, user) for user in config["representative_ids"]
                  ] if config["enable_knowledge"] else []
    created = [op["item"] for op in (representative or {}).get("operations", []) if "item" in op]
    for item in [*workspace["records"], *created, *visibility]:
        unique = item["payload"].get("msdyn_uniquename")
        if unique:
            require(not api.rows(item["table"], {"$filter": "msdyn_uniquename eq " + quote(unique)}),
                    "An existing workspace uses this prefix. Reuse its original receipt; never adopt by display name.")
        try:
            api.get(record_path(item["table"], item["id"]))
        except urllib.error.HTTPError as error:
            if error.code == 404:
                error.close()
                continue
            raise
        raise GateError("An owned deterministic ID already exists without this receipt; do not adopt it.")
    for group in knowledge["groups"]:
        require(not api.rows("knowledgearticles", {"$filter": "articlepublicnumber eq " + quote(group["number"])}),
                "Knowledge number collision. Reuse the original receipt or select a new ownership prefix.")
    gates = []
    if 1025 not in native["languages"]:
        gates.append("Provision Arabic (1025) before creating localized scripts.")
    if 1033 not in native["languages"]:
        gates.append("English UI (1033) must be provisioned by the administrator before this bilingual recipe.")
    if not config["approvals"]["licenses_and_region_reviewed"]:
        gates.append("Licenses/region review has not been acknowledged in this plan.")
    if config["enable_copilot"] and (not all(native["shared"]["copilot"].get(field) is True for field in (
        "msdyn_copilotenabled", "msdyn_agentassistenabled", "msdyn_allagentsenabled", "msdyn_allworkstreamsenabled",
    )) or not all(native["shared"]["legacy"].get(field) is True for field in (
        "msdyn_enabledforagents", "msdyn_allagentsenabled",
    ))):
        gates.append("Native Copilot isn't opted in or isn't available to all representatives and workstreams. "
                     "Complete the admin opt-in (workspace-setup.md, Native Copilot opt-in), then re-plan before "
                     "creating owned workspace records.")
    if representative:
        gates.extend("Representative setup: " + gate for gate in representative["gates"])
    plan = {
        "schema": SCHEMA, "created_at": utc(), "config": config, "input_hashes": input_hashes(),
        "native": native, "workspace": workspace, "knowledge": knowledge,
        "gates": gates,
        "acceptance": {
            "native_generated_answers": "not tested" if config["enable_copilot"] else "not selected",
            "summary_quality": "not tested" if config["enable_copilot"] else "not selected",
            "browser_rendering": "not tested", "clean_tenant_replay": "not tested",
            "live_case_querying": "not configured by knowledge-source enablement",
        },
    }
    if representative:
        plan["representative"] = representative
    if visibility:
        plan["knowledge_visibility"] = visibility
    return plan


class Journal:
    """Exclusive, append-only, hash-linked private receipts; no credential headers."""

    def __init__(self, directory, plan):
        self.directory = Path(directory)
        self.plan_hash = digest(plan)
        self.sequence, self.previous = 0, None
        self.state = {
            "owned": {}, "generated_parameters": {}, "links": {}, "shared": {}, "stages": {},
            "language_job": None, "pending": None, "app_registered": False,
            "app_published": False, "bound": False, "rolled_back": False,
        }
        for path in sorted(self.directory.glob("event-*.json")):
            event = json.loads(read_private(path))
            require(event["sequence"] == self.sequence + 1 and event["previous"] == self.previous and
                    event["plan_sha256"] == self.plan_hash, "Receipt chain is incomplete or belongs to a different plan.")
            self.sequence, self.previous = event["sequence"], digest(event)
            self.merge(event["updates"])

    def merge(self, updates):
        for key, value in updates.items():
            if isinstance(value, dict) and isinstance(self.state.get(key), dict):
                self.state[key].update(value)
            else:
                self.state[key] = value

    def save(self, label, updates=None, evidence=None):
        updates = updates or {}
        event = {
            "schema": SCHEMA, "sequence": self.sequence + 1, "previous": self.previous,
            "plan_sha256": self.plan_hash, "at": utc(), "label": label,
            "updates": updates, "evidence": evidence,
        }
        path = self.directory / f"event-{event['sequence']:06d}.json"
        write_private(path, json.dumps(event, ensure_ascii=False, indent=2))
        self.sequence, self.previous = event["sequence"], digest(event)
        self.merge(updates)
        return path


class Operator:
    def __init__(self, api, plan, journal, *, wait_seconds=900, poll_seconds=15, extension=None,
                 copilot_wait_seconds=3600, reconnect=None):
        self.api, self.plan, self.journal = api, plan, journal
        self.config = plan["config"]
        self.native = plan["native"]
        self.workspace = plan["workspace"]
        self.knowledge = plan["knowledge"]
        self.wait_seconds, self.poll_seconds = wait_seconds, poll_seconds
        self.copilot_wait_seconds = copilot_wait_seconds
        self.reconnect = reconnect
        self.extension = extension
        self.recipe_update_candidate = None

    @property
    def state(self):
        return self.journal.state

    def approval(self, key):
        require(self.config["approvals"].get(key, False),
                f"Stage blocked: explicit {key} approval is false in the reviewed plan.")

    def read(self, table, identifier):
        return self.api.get(record_path(table, identifier))

    def optional(self, table, identifier):
        try:
            return self.read(table, identifier)
        except urllib.error.HTTPError as error:
            if error.code == 404:
                error.close()
                return None
            raise

    def idle(self):
        rows = self.api.rows("msdyn_ocliveworkitems", {
            "$select": "activityid,statecode,statuscode",
            "$filter": "(" + " or ".join("_msdyn_liveworkstreamid_value eq " + value
                                       for value in self.config["workstream_ids"]) +
                       ") and (statuscode eq 1 or statuscode eq 2 or statuscode eq 3 or statuscode eq 5 or statecode eq 4)",
            "$top": "1",
        })
        require(not rows, "Active/open/waiting/wrap-up conversation found. Drain the selected workstreams first.")

    def languages(self):
        return self.api.get("RetrieveProvisionedLanguages()")["RetrieveProvisionedLanguages"]

    def user_settings_unchanged(self):
        for user, before in self.native["user_settings"].items():
            after = self.api.get(record_path("usersettingscollection", user), {
                "$select": "uilanguageid,helplanguageid,localeid",
            })
            require(all(after.get(key) == before.get(key) for key in ("uilanguageid", "helplanguageid", "localeid")),
                    "Representative UI/help/locale settings changed since planning; this kit never changes them.")

    def wait(self, label, probe, *, seconds=None, timeout=None):
        deadline = time.monotonic() + (self.wait_seconds if seconds is None else seconds)
        while True:
            result = probe()
            if result:
                return result
            if time.monotonic() >= deadline:
                raise GateError(timeout() if timeout else label + " did not complete within the bounded wait. "
                                "Inspect the receipt/native admin status; resume the same stage, not a new plan.")
            time.sleep(min(self.poll_seconds, max(0, deadline - time.monotonic())))

    def read_fresh(self, read):
        """Renew an expired token once for a read-only status check; writes are never repeated."""
        try:
            return read()
        except urllib.error.HTTPError as error:
            if error.code != 401 or self.reconnect is None:
                raise
            error.close()
        self.api = self.reconnect()
        self.journal.save("dataverse_token_renewed", evidence={"reason": "HTTP 401 during a read-only status check"})
        return read()

    def begin(self, operation):
        require(not self.state["pending"], "An unconfirmed mutation is recorded. Run recover (readback only) before any new writes.")
        require(not self.state["rolled_back"], "This deployment was rolled back; a new deployment needs a new namespace/prefix.")
        self.journal.save("before_" + operation["kind"], {"pending": operation})

    def request(self, operation):
        if operation["kind"] in ("app_add", "app_remove", "app_publish"):
            operation = {**operation, "app_before": self.app_action_state()}
        self.begin(operation)
        try:
            body = operation.get("body")
            if operation["path"] == "$batch":
                body = body.encode()
            response = self.api.request(
                operation["path"], method=operation["method"], body=body,
                headers=operation.get("headers"), raw=operation.get("raw", False),
            )
        except urllib.error.HTTPError as error:
            self.journal.save("http_failure", {"pending": {**operation, "http_status": error.code}})
            error.close()
            raise GateError(f"Native {operation['kind']} returned HTTP {error.code}. Its intent is saved. Run recover "
                            "(read-only) next; retry once only after a concrete change, then stop and ask the owner.") from None
        pending = {**operation, "response": response}
        self.journal.save("response_" + operation["kind"], {"pending": pending})
        return response

    def finish(self, label, updates=None, evidence=None):
        self.journal.save(label, {**(updates or {}), "pending": None}, evidence)

    def current_owned(self, key):
        require(key in self.state["owned"], "Missing exact ownership receipt for " + key)
        item = self.state["owned"][key]
        row = self.read(item["table"], item["id"])
        check_record_payload(self.api, row, item["expected"], key)
        return row

    def complete_create(self, item):
        row = self.read(item["table"], item["id"])
        payload = item["payload"]
        corrected = (item["table"] == "msdyn_appconfigurations" and row.get(PROFILE_APP_FIELD) == NATIVE_PROFILE_APP_DEFAULT
                     and payload.get(PROFILE_APP_FIELD) not in (None, NATIVE_PROFILE_APP_DEFAULT))
        checked = {**payload, PROFILE_APP_FIELD: NATIVE_PROFILE_APP_DEFAULT} if corrected else payload
        try:
            check_record_payload(self.api, row, checked, item["key"])
        except GateError as error:
            self.journal.save("create_readback_mismatch", evidence={
                "key": item["key"], "table": item["table"], "id": item["id"], "reason": str(error), "readback": row,
            })
            raise GateError(str(error) + " The create was sent once and its intent is saved. Wait a few minutes, then "
                            "run recover (read-only) with the same plan and output directory. Never send the create "
                            "again.") from None
        expected = copy.deepcopy(checked)
        for key in ("statecode", "statuscode"):
            if key in row:
                expected[key] = row[key]
        owned = {**item, "expected": expected, "after_etag": etag(row)}
        if corrected:
            owned["app_correction"] = {"field": PROFILE_APP_FIELD, "stored": NATIVE_PROFILE_APP_DEFAULT,
                                       "requested": payload[PROFILE_APP_FIELD], "status": "required"}
        updates, evidence, unplanned = {"owned": {item["key"]: owned}}, None, []
        if item["table"] == "msdyn_applicationtabtemplates":
            generated = self.api.rows("msdyn_templateparameters", {
                "$filter": "_msdyn_applicationtabtemplateid_value eq " + item["id"],
            })
            require(len({child["msdyn_name"] for child in generated}) == len(generated),
                    "Ambiguous auto-generated template parameters.")
            updates["generated_parameters"] = {item["id"]: generated}
        if item["table"] == "systemforms":
            require(row.get("componentstate") == 0, "New scoped form did not auto-publish; do not broaden entity publication.")
        if item["table"] == "msdyn_appconfigurations":
            present, unplanned = self.profile_links(item["id"])
            updates["links"] = {digest(link): link for link in present}
            updates["links_present_at_creation"] = {digest(link): link for link in present}
            owned["unplanned_links_at_creation"] = unplanned
            evidence = {"links_present_at_creation": present, "unplanned_links_present_at_creation": unplanned}
        self.finish("created_and_read_back", updates, evidence)
        require(not unplanned, self.unplanned_links_message(unplanned))
        return row

    def profile_links(self, profile):
        """Read a new profile's links once; only planned ones that the platform already added are recorded."""
        planned = [link for link in self.workspace["links"]
                   if link["table"] == "msdyn_appconfigurations" and link["id"] == profile]
        relations = {(link["relation"], link["target_table"]) for link in planned} | {
            ("msdyn_appconfiguration_applicationextension", "msdyn_applicationextensions"),
            ("msdyn_appconfig_msdyn_channelprovider", "msdyn_channelproviders"),
        }
        present, unplanned = [], []
        for relation, table in sorted(relations):
            primary = target_primary(table)
            for row in self.api.rows(record_path("msdyn_appconfigurations", profile) + "/" + relation, {"$select": primary}):
                link = {"table": "msdyn_appconfigurations", "id": profile, "relation": relation,
                        "target_table": table, "target_id": row[primary]}
                (present if link in planned else unplanned).append(link)
        return present, unplanned

    @staticmethod
    def unplanned_links_message(links):
        return ("The new profile already has links that aren't in the plan: " +
                ", ".join(link["relation"] + "/" + link["target_id"] for link in links) +
                ". Nothing was removed. Stop and ask the owner to review them before resuming.")

    def after_create(self, key):
        item = self.state["owned"][key]
        still = [link for link in item.get("unplanned_links_at_creation", []) if self.link_present(link)]
        require(not still, self.unplanned_links_message(still))
        if item.get("app_correction"):
            self.correct_profile_app(key)

    def correct_profile_app(self, key):
        """One guarded PATCH of a new, unassigned profile that stored the native app default."""
        item = self.state["owned"][key]
        correction = item["app_correction"]
        if correction["status"] == "applied":
            return
        row = self.current_owned(key)
        field, requested = correction["field"], correction["requested"]
        if correction["status"] == "attempted":
            require(row.get(field) == requested and item["expected"].get(field) == requested,
                    f"The one allowed correction of the new profile's app ({correction['stored']} to {requested}) "
                    "did not apply. Stop and ask the owner; don't retry it.")
        else:
            require(correction["status"] == "required" and row.get(field) == correction["stored"],
                    "Unexpected profile app correction state.")
            require(not self.api.rows(record_path("msdyn_appconfigurations", item["id"]) + "/" + PROFILE_USERS,
                                      {"$select": "systemuserid"}),
                    "The new profile already has assigned users; its app correction is only for an unassigned profile.")
            app = self.api.get(record_path("appmodules", self.config["app_id"]))
            require(app.get("uniquename") == requested == self.native["app"]["uniquename"],
                    "The target app's unique name changed since planning; the profile was not corrected.")
            others = {row["msdyn_appconfigurationid"]: [row.get("msdyn_uniquename"), row.get(field)]
                      for row in all_profiles(self.api) if row["msdyn_appconfigurationid"] != item["id"]}
            planned = {row["msdyn_appconfigurationid"]: [row.get("msdyn_uniquename"), row.get(field)]
                       for row in self.native["profiles"]}
            require(others == planned, "Other experience profiles changed since planning; the profile was not corrected.")
            self.journal.save("profile_app_correction_intent",
                              {"owned": {key: {**item, "app_correction": {**correction, "status": "attempted"}}}})
            self.patch_owned(key, {field: requested})
            item = self.state["owned"][key]
        self.journal.save("profile_app_correction_verified",
                          {"owned": {key: {**item, "app_correction": {**item["app_correction"], "status": "applied"}}}})

    def create(self, item, *, caller=None):
        if item["key"] not in self.state["owned"]:
            require(self.optional(item["table"], item["id"]) is None,
                    "Existing record without an ownership receipt: " + item["key"])
            for field in ("msdyn_uniquename", "articlepublicnumber"):
                if field in item["payload"]:
                    require(not self.api.rows(item["table"], {"$filter": field + " eq " + quote(item["payload"][field])}),
                            "A unique-name/article-number collision appeared after planning.")
            operation = {"kind": "create", "path": item["table"], "method": "POST", "body": item["payload"], "item": item}
            if caller:
                operation["headers"] = {"MSCRMCallerID": caller}
            self.request(operation)
            self.complete_create(item)
        else:
            self.current_owned(item["key"])
        self.after_create(item["key"])
        return self.read(item["table"], item["id"])

    def patch_owned(self, key, patch):
        row = self.current_owned(key)
        item = self.state["owned"][key]
        if all(content_equal(field, row.get(field), value) for field, value in patch.items()):
            return row
        operation = {
            "kind": "owned_patch", "key": key, "path": record_path(item["table"], item["id"]),
            "method": "PATCH", "body": patch, "headers": {"If-Match": etag(row)},
            "before": {field: row.get(field) for field in patch},
        }
        self.request(operation)
        return self.complete_owned_patch(operation)

    def complete_owned_patch(self, operation):
        item = copy.deepcopy(self.state["owned"][operation["key"]])
        row = self.read(item["table"], item["id"])
        item["expected"].update(operation["body"])
        check_record_payload(self.api, row, item["expected"], operation["key"])
        item["after_etag"] = etag(row)
        self.finish("owned_patch_read_back", {"owned": {operation["key"]: item}})
        return row

    def link_path(self, link):
        return record_path(link["table"], link["id"]) + "/" + link["relation"]

    def link_present(self, link):
        primary = target_primary(link["target_table"])
        rows = self.api.rows(self.link_path(link), {"$select": primary})
        return any(row[primary] == link["target_id"] for row in rows)

    def link(self, link):
        key = digest(link)
        if key in self.state["links"]:
            require(self.link_present(link), "Recorded native association disappeared.")
            return
        require(not self.link_present(link), "Unrecorded existing association; no automatic adoption.")
        row = self.read(link["table"], link["id"])
        self.request({
            "kind": "link", "path": self.link_path(link) + "/$ref", "method": "POST",
            "body": {"@odata.id": self.config["org_url"] + "/api/data/v9.2/" +
                     record_path(link["target_table"], link["target_id"])},
            "headers": {"If-Match": etag(row)}, "link": link,
        })
        require(self.link_present(link), "Native association was not persisted.")
        self.finish("association_read_back", {"links": {key: link}})

    def provision_language(self):
        self.user_settings_unchanged()
        if 1025 not in self.languages():
            self.approval("provision_arabic")
            self.idle()
            if not self.state["language_job"]:
                response = self.request({
                    "kind": "language", "path": "ProvisionLanguageAsync", "method": "POST", "body": {"Language": 1025},
                })
                job = guid(response.get("AsyncOperationId"))
                self.finish("arabic_job_recorded", {"language_job": job})

            def complete():
                job = self.read("asyncoperations", self.state["language_job"])
                self.journal.save("arabic_job_readback", evidence={
                    key: job.get(key) for key in ("asyncoperationid", "statecode", "statuscode", "errorcode", "message")
                })
                if job.get("statecode") == 3:
                    require(job.get("statuscode") == 30, "ProvisionLanguageAsync failed/cancelled. Inspect its System Job; do not disable Activity Feeds.")
                return 1025 in self.languages() and job.get("statuscode") == 30

            self.wait("Arabic language provisioning", complete)
        self.user_settings_unchanged()
        self.journal.save("arabic_available", {"stages": {"language": True}}, {"languages": self.languages()})

    def record_generated_parameters(self):
        """Own every planned native-generated child before any value write; a write advances its siblings' ETags."""
        for desired in self.workspace["parameters"]:
            key = "parameter_" + desired["key"]
            if key in self.state["owned"]:
                continue
            generated = self.state["generated_parameters"].get(desired["tab"], [])
            matches = [row for row in generated if row["msdyn_name"] == desired["name"]]
            if not matches:
                continue
            before = one(matches, "recorded generated parameter")
            identifier = before["msdyn_templateparameterid"]
            current = self.read("msdyn_templateparameters", identifier)
            require(current["_msdyn_applicationtabtemplateid_value"] == desired["tab"] and
                    generated_semantics(current) == generated_semantics(before) and etag(current) == etag(before),
                    "Generated template parameter drifted since creation.")
            self.journal.save("recorded_owned_generated_parameter", {"owned": {key: {
                "key": key, "table": "msdyn_templateparameters", "id": identifier,
                "expected": {
                    **generated_semantics(before),
                    "msdyn_applicationtabtemplateid@odata.bind": "/msdyn_applicationtabtemplates(" + desired["tab"] + ")",
                }, "after_etag": etag(current), "generated": True,
            }}})

    def sync_parameters(self):
        self.record_generated_parameters()
        for desired in self.workspace["parameters"]:
            key = "parameter_" + desired["key"]
            if key not in self.state["owned"]:
                self.create({
                    "key": key, "table": "msdyn_templateparameters", "id": desired["id"],
                    "payload": {
                        "msdyn_templateparameterid": desired["id"], "msdyn_name": desired["name"],
                        "msdyn_uniquename": self.config["prefix"] + "_" + desired["key"],
                        "msdyn_value": desired["value"],
                        "msdyn_applicationtabtemplateid@odata.bind": "/msdyn_applicationtabtemplates(" + desired["tab"] + ")",
                    },
                })
            # Owned children are compared semantically and patched with the fresh ETag read now.
            self.patch_owned(key, {"msdyn_value": desired["value"]})
        self.verify_parameters()

    def verify_parameters(self):
        for tab in sorted({item["tab"] for item in self.workspace["parameters"]}):
            def synchronized():
                children = self.api.rows("msdyn_templateparameters", {
                    "$filter": "_msdyn_applicationtabtemplateid_value eq " + tab,
                })
                require(len({row["msdyn_name"] for row in children}) == len(children), "Duplicate native template parameter.")
                known = {row["msdyn_templateparameterid"]: {
                    field: row.get(field) for field in (
                        "msdyn_templateparameterid", "msdyn_name", "msdyn_value", "_msdyn_applicationtabtemplateid_value",
                    )
                } for row in self.state["generated_parameters"].get(tab, [])}
                for item in self.state["owned"].values():
                    if item["table"] == "msdyn_templateparameters" and item["expected"].get(
                        "msdyn_applicationtabtemplateid@odata.bind"
                    ) == "/msdyn_applicationtabtemplates(" + tab + ")":
                        known[item["id"]] = item["expected"]
                require({row["msdyn_templateparameterid"] for row in children} == set(known),
                        "Unexpected template parameter outside the exact creation/ownership receipt.")
                for row in children:
                    check_payload(row, known[row["msdyn_templateparameterid"]], "template parameter")
                aggregate = {row["msdyn_name"]: {"value": row.get("msdyn_value")} for row in children}
                for desired in (item for item in self.workspace["parameters"] if item["tab"] == tab):
                    require(aggregate.get(desired["name"]) == {"value": desired["value"]},
                            "Native template parameter readback mismatch.")
                return json.loads(self.read("msdyn_applicationtabtemplates", tab).get("msdyn_templateparameters") or "{}") == aggregate
            self.wait("Native template-parameter aggregate synchronization", synchronized)

    def app_pair(self):
        path = record_path("appmodules", self.config["app_id"])
        return self.api.get(path), self.api.get(path + "/Microsoft.Dynamics.CRM.RetrieveUnpublished")

    def app_components(self, app):
        return self.api.rows("appmodulecomponents", {"$filter": "_appmoduleidunique_value eq " + app["appmoduleidunique"]})

    def app_action_state(self):
        app, draft = self.app_pair()

        def snapshot(row):
            components = [
                {key: value for key, value in component.items() if "@" not in key or key == "@odata.etag"}
                for component in self.app_components(row)
            ]
            return {
                "etag": etag(row), "appmoduleidunique": row["appmoduleidunique"],
                "authored": app_authored(row), "components": sorted(components, key=canonical_json),
            }

        return {"published": snapshot(app), "unpublished": snapshot(draft)}

    def app_component_body(self):
        return {"AppId": self.config["app_id"], "Components": [
            {"@odata.type": "Microsoft.Dynamics.CRM.systemform", "formid": identifier}
            for identifier in sorted(self.form_ids())
        ]}

    def form_ids(self):
        return {row["id"] for row in self.workspace["records"] if row["table"] == "systemforms"}

    def publication_body(self):
        return {"ParameterXml": "<importexportxml><appmodules><appmodule>" +
                self.config["app_id"] + "</appmodule></appmodules></importexportxml>"}

    def validate_staged_app(self, *, removing=False):
        app, draft = self.app_pair()
        forms = self.form_ids()
        baseline = component_set(self.native["app_components"])
        expected = baseline if removing else baseline | {(60, form) for form in forms}
        staged = self.app_components(draft)
        require(component_set(staged) == expected, "Staged app component delta is not exactly the owned forms.")
        require(app_authored(draft, without_components=True) == app_authored(self.native["app"], without_components=True),
                "Unrelated app draft edits found; refuse publication.")
        preserved = [row for row in staged if row["objectid"].lower() not in forms]
        require(component_defaults(preserved) == component_defaults(self.native["app_components"]),
                "Existing app component defaults changed.")
        if not removing:
            for form in forms:
                require(one([row for row in staged if row["objectid"].lower() == form], "new form component")["isdefault"] is False,
                        "Owned form must not become an app default.")
        return app, draft

    def published_app_ok(self, *, removing=False):
        app, draft = self.app_pair()
        forms = self.form_ids()
        expected = component_set(self.native["app_components"]) | (set() if removing else {(60, form) for form in forms})
        require(component_set(self.app_components(app)) == expected, "Published app component set differs.")
        require(app_authored(app, remove_form=None if removing else forms) == app_authored(self.native["app"]),
                "App authored content changed outside the owned component.")
        require(app_authored(app) == app_authored(draft), "Unpublished app changes remain.")
        native_components = self.api.get("RetrieveAppComponents(AppModuleId=" + self.config["app_id"] + ")")["value"]
        require(component_set(native_components) == expected, "RetrieveAppComponents did not confirm the exact published delta.")

    def register_form(self):
        self.approval("publish_app")
        for item in self.workspace["records"]:
            if item["table"] == "systemforms":
                form = self.current_owned(item["key"])
                require(form.get("componentstate") == 0 and form["isdefault"] is False,
                        "Owned clones must be published and nondefault.")
        if self.state["app_published"]:
            self.published_app_ok()
            return
        self.idle()
        if not self.state["app_registered"]:
            app, draft = self.app_pair()
            require(app_authored(app) == app_authored(self.native["app"]) == app_authored(draft),
                    "App changed since plan or has unrelated pending edits.")
            require(component_defaults(self.app_components(app)) == component_defaults(self.native["app_components"]),
                    "App defaults/components changed since plan.")
            self.request({"kind": "app_add", "path": "AddAppComponents", "method": "POST", "body": self.app_component_body()})
            self.validate_staged_app()
            self.finish("scoped_forms_registered", {"app_registered": True})
        _, before = self.validate_staged_app()
        validation = self.api.get("ValidateApp(AppModuleId=" + self.config["app_id"] + ")")
        def errors(value):
            return {canonical_json(row) for row in value["AppValidationResponse"]["ValidationIssueList"]
                    if row.get("ErrorType") == "Error"}
        require(not errors(validation) - errors(self.native["app_validation"]), "New native app validation errors; no publication.")
        _, checked = self.validate_staged_app()
        require(etag(checked) == etag(before) and app_authored(checked) == app_authored(before),
                "App draft changed during validation.")
        self.idle()
        self.request({"kind": "app_publish", "path": "PublishXml", "method": "POST", "body": self.publication_body()})
        self.published_app_ok()
        self.finish("app_only_publication_read_back", {"app_published": True}, {"validation": validation})

    def provision_workspace(self):
        require(1025 in self.languages(), "Arabic 1025 is not provisioned. Run the language stage before any LCID1025 scripts.")
        require(1033 in self.languages(), "English UI (1033) is an external prerequisite for this bilingual recipe.")
        if self.config["enable_copilot"]:
            require(all(self.native["shared"]["copilot"].get(field) is True for field in (
                "msdyn_copilotenabled", "msdyn_agentassistenabled", "msdyn_allagentsenabled", "msdyn_allworkstreamsenabled",
            )) and all(self.native["shared"]["legacy"].get(field) is True for field in (
                "msdyn_enabledforagents", "msdyn_allagentsenabled",
            )), "The plan captured unresolved native entitlement/access gates. Resolve them externally and create a fresh plan before workspace records.")
            self.copilot_prerequisites()
        self.approval("publish_app")
        self.idle()
        for key in ("contact_form", "case_form"):
            source = self.read("systemforms", self.native[key]["formid"])
            require(etag(source) == etag(self.native[key]) and source["formxml"] == self.native[key]["formxml"],
                    "Shared source " + key + " changed since planning.")
        for item in self.workspace["records"]:
            self.create(item)
            if item["table"] == "workflows":
                self.patch_owned(item["key"], {"statecode": 1, "statuscode": 2})
        self.sync_parameters()
        for link in self.workspace["links"]:
            self.link(link)
        self.register_form()
        self.verify_workspace()
        self.journal.save("workspace_records_verified", {"stages": {"workspace": True}})

    def verify_workspace(self):
        correction = self.state["owned"].get("profile", {}).get("app_correction")
        require(not correction or correction["status"] == "applied",
                "The new profile still needs its one guarded app correction. Run the workspace stage.")
        for item in self.workspace["records"]:
            row = self.current_owned(item["key"])
            if item["table"] == "workflows":
                require((row.get("statecode"), row.get("statuscode")) == (1, 2), "Native macro is not activated.")
            elif "statecode" in row:
                require(row["statecode"] == 0, "Native workspace record is inactive.")
        self.verify_parameters()
        for link in self.workspace["links"]:
            require(digest(link) in self.state["links"] and self.link_present(link), "Missing recorded workspace association.")
        grouped = {}
        for link in self.workspace["links"]:
            key = (self.link_path(link), link["target_table"])
            grouped.setdefault(key, set()).add(link["target_id"])
        profile_path = record_path("msdyn_appconfigurations", self.workspace["ids"]["profile"])
        grouped.setdefault((profile_path + "/msdyn_appconfig_msdyn_channelprovider", "msdyn_channelproviders"), set())
        for link in self.native_channel_links():
            key = (self.link_path(link), link["target_table"])
            grouped.setdefault(key, set()).add(link["target_id"])
        for link in self.extension_links():
            key = (self.link_path(link), link["target_table"])
            require(key in grouped, "An extension cannot introduce a new base relationship scope.")
            grouped[key].add(link["target_id"])
        for (path, table), expected in grouped.items():
            primary = target_primary(table)
            actual = self.api.rows(path, {"$select": primary})
            require(len(actual) == len(expected) and {row[primary] for row in actual} == expected,
                    "Unexpected association on an owned workspace record.")
        children = (
            ("msdyn_productivityagentscriptsteps", "msdyn_agentscriptid", "msdyn_productivityagentscripts"),
            ("msdyn_panetabconfigurations", "msdyn_ProductivityPaneId", "msdyn_paneconfigurations"),
            ("msdyn_appcopilotconfigurations", "msdyn_appconfigurationid", "msdyn_appconfigurations"),
        )
        for table, navigation, parent_table in children:
            for parent in (item for item in self.workspace["records"] if item["table"] == parent_table):
                expected = {item["id"] for item in self.workspace["records"] if item["table"] == table and
                            item["payload"].get(navigation + "@odata.bind") == "/" + record_path(parent_table, parent["id"])}
                primary = TABLE_IDS[table]
                actual = self.api.rows(table, {"$select": primary,
                                              "$filter": LOOKUPS[navigation] + " eq " + parent["id"]})
                require(len(actual) == len(expected) and {row[primary] for row in actual} == expected,
                        "Unexpected child record on an owned script/pane/profile.")
        self.published_app_ok()
        before_forms = {row["formid"]: row for row in self.native["contact_forms"] + self.native.get("case_forms", [])}
        for identifier, before in before_forms.items():
            after = self.read("systemforms", identifier)
            require(after["formxml"] == before["formxml"] and after["isdefault"] == before["isdefault"],
                    "A preexisting contact/case form/order/default changed; cannot claim preservation.")
        source_view = self.read("savedqueries", self.native["all_cases"]["savedqueryid"])
        require(etag(source_view) == etag(self.native["all_cases"]), "Shared All Cases view changed; no view edits are owned.")

    def native_channel_links(self):
        registration = self.state.get("native_channel_upgrade")
        if registration is None:
            return []
        require(isinstance(registration, dict) and set(registration) == {"receipt", "sha256"},
                "Native-channel upgrade needs its exact pinned receipt.")
        manifest = checked_json(registration["receipt"], registration["sha256"])
        require(manifest.get("kind") == "native-channel-profile-upgrade-v1" and
                manifest.get("base_plan_sha256") == digest(self.plan) and
                manifest.get("organization_url") == self.config["org_url"] and
                manifest.get("profile_id") == self.workspace["ids"]["profile"],
                "Native-channel upgrade belongs to another base, organization or profile.")
        source = manifest["source_profile"]
        require(source.get("msdyn_uniquename") == "msdyn_csw_oc_stepup_app_configuration" and
                source.get("ismanaged") is True and source.get("statecode") == 0,
                "Native channels require the installed managed workspace-with-channels source.")
        check_payload(self.read("msdyn_appconfigurations", source["msdyn_appconfigurationid"]), source,
                      "native channel source profile")
        contract = {
            "msdyn_CIFV2Loader": ("msdyn_applicationextensions", "msdyn_appconfiguration_applicationextension"),
            "msdyn_CCaaSClientSDKLoader": ("msdyn_applicationextensions", "msdyn_appconfiguration_applicationextension"),
            "msdyn_omnichannel": ("msdyn_channelproviders", "msdyn_appconfig_msdyn_channelprovider"),
        }
        sources = manifest["sources"]
        require(isinstance(sources, list) and len(sources) == 3 and
                {item["row"].get("msdyn_uniquename") for item in sources} == set(contract),
                "Only the exact installed native provider and CIF/CCaaS loaders may be added.")
        links = []
        for item in sources:
            row = item["row"]
            table, relation = contract[row["msdyn_uniquename"]]
            require(item["table"] == table and item["relation"] == relation and row.get("ismanaged") is True,
                    "Native-channel source is not the reviewed managed component type.")
            identifier = row[target_primary(table)]
            check_payload(self.read(table, identifier), row, "native channel managed component")
            source_path = record_path("msdyn_appconfigurations", source["msdyn_appconfigurationid"]) + "/" + relation
            require(identifier in {record[target_primary(table)] for record in
                                   self.api.rows(source_path, {"$select": target_primary(table)})},
                    "Native-channel component no longer belongs to the reviewed installed source.")
            if table == "msdyn_channelproviders":
                url = urllib.parse.urlsplit(row.get("msdyn_channelurl", ""))
                require(row.get("statecode") == 0 and url.scheme == "https" and url.hostname and
                        not url.username and not url.password,
                        "The installed native provider must be active with its original HTTPS endpoint.")
            link = {"table": "msdyn_appconfigurations", "id": self.workspace["ids"]["profile"],
                    "relation": relation, "target_table": table, "target_id": identifier}
            require(link not in self.workspace["links"] and digest(link) in self.state["links"] and
                    self.state["links"][digest(link)] == link and self.link_present(link),
                    "Native-channel delta lacks its original recorded association/readback.")
            links.append(link)
        require(sorted(manifest["links"], key=canonical_json) == sorted(links, key=canonical_json),
                "Native-channel receipt contains an unknown or missing association.")
        return links

    def extension_links(self):
        registration = self.state.get("extension")
        require(not registration or registration.get("status") in {"accepted", "released"},
                "Unknown extension registration state.")
        if self.extension is not None:
            manifest = self.extension
        elif registration:
            manifest = checked_json(registration["receipt"], registration["sha256"])
        else:
            return []
        require(manifest["base"]["kind"] == "portable" and manifest["base"]["plan_sha256"] == digest(self.plan),
                "Extension belongs to another base; legacy evidence is never portable ownership.")
        if registration and registration.get("status") == "released":
            verify_extension_release(self.api, manifest, checked_json(
                registration["release_receipt"], registration["release_sha256"],
            ))
            return []
        verify_extension(self.api, manifest, recipe_update_candidate=self.recipe_update_candidate)
        return [manifest["edge"]]

    def category(self, key, identifier, title, parent=None):
        payload = {"categoryid": identifier, "title": title,
                   "description": "Owned by workspace kit " + self.config["prefix"] + " / " + self.config["namespace"]}
        if parent:
            payload["parentcategoryid@odata.bind"] = "/categories(" + parent + ")"
        return self.create({"key": key, "table": "categories", "id": identifier, "payload": payload})

    def check_article_identity(self, row, group, language, primary=None):
        require(row["articlepublicnumber"] == group["number"] and row["isrootarticle"] is False and
                row["_languagelocaleid_value"] == self.knowledge["locales"][language],
                "Unexpected native knowledge identity/language.")
        if primary:
            require(row["_parentarticlecontentid_value"] == primary["knowledgearticleid"] and
                    row["_rootarticleid_value"] == primary["_rootarticleid_value"] and row["isprimary"] is False,
                    "Arabic article is not the native translation of this English primary.")
        else:
            require(row["isprimary"] is True, "English content must be a primary article, not a root container.")

    def translations(self, group):
        return self.api.rows("knowledgearticles", {
            "$filter": "_parentarticlecontentid_value eq " + group["en_id"] +
                       " and _languagelocaleid_value eq " + self.knowledge["locales"]["ar-SA"] +
                       " and isrootarticle eq false",
        })

    def complete_translation(self, group, response):
        primary = self.current_owned("article_" + str(group["index"]) + "_en")
        row = one(self.translations(group), "native Arabic translation")
        require(not response or row["knowledgearticleid"] == response["knowledgearticleid"], "Translation action/readback ID mismatch.")
        self.check_article_identity(row, group, "ar-SA", primary)
        require(row["statecode"] == 0, "New translation was changed outside this run before ownership readback.")
        check_payload(row, {field: primary[field] for field in (
            "title", "content", "description", "keywords", "isinternal",
        )}, "new native translation source copy")
        key = "article_" + str(group["index"]) + "_ar"
        expected = {field: row[field] for field in (
            "knowledgearticleid", "articlepublicnumber", "title", "content", "description", "keywords",
            "isinternal", "statecode", "statuscode", "isrootarticle", "isprimary",
            "_languagelocaleid_value", "_parentarticlecontentid_value", "_rootarticleid_value",
        )}
        item = {"key": key, "table": "knowledgearticles", "id": row["knowledgearticleid"],
                "expected": expected, "after_etag": etag(row)}
        self.finish("native_translation_read_back", {"owned": {key: item}})
        return row

    def publish_article(self, key, group, language):
        row = self.current_owned(key)
        desired = group["versions"][language]
        changes = {field: value for field, value in desired.items() if not content_equal(field, row.get(field), value)}
        if changes:
            require(row["statecode"] == 0, "Published/approved content changed; use a separately reviewed versioning workflow, never overwrite it.")
            row = self.patch_owned(key, changes)
        if row["statecode"] == 0:
            row = self.patch_owned(key, {"statecode": 1, "statuscode": 5})
        if row["statecode"] == 1:
            row = self.patch_owned(key, {
                "statecode": 3, "statuscode": 7,
                "publishon": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
            })
        require((row["statecode"], row["statuscode"]) == (3, 7) and row["islatestversion"],
                "Owned knowledge article was not genuinely Published/latest.")

    def provision_knowledge(self):
        if not self.config["enable_knowledge"]:
            self.journal.save("knowledge_not_selected", evidence={"published_articles": 0, "configured": False})
            return
        self.approval("publish_knowledge")
        require(self.knowledge["groups"], "No approved knowledge was supplied; do not publish placeholders.")
        require(1025 in self.languages(), "Provision Arabic before bilingual native knowledge setup.")
        self.category("category_root", self.knowledge["root_category_id"], self.knowledge["root_category_title"])
        for group in self.knowledge["groups"]:
            index = str(group["index"])
            self.category("category_" + index, group["category_id"], group["category_title"], self.knowledge["root_category_id"])
            primary = self.create({
                "key": "article_" + index + "_en", "table": "knowledgearticles", "id": group["en_id"],
                "payload": {
                    "knowledgearticleid": group["en_id"], "articlepublicnumber": group["number"],
                    "languagelocaleid@odata.bind": "/languagelocale(" + self.knowledge["locales"]["en-US"] + ")",
                    **group["versions"]["en-US"],
                },
            })
            self.check_article_identity(primary, group, "en-US")
            root = self.read("knowledgearticles", primary["_rootarticleid_value"])
            require(root["isrootarticle"] is True and root["articlepublicnumber"] == group["number"], "Unexpected generated root container.")
            self.journal.save("generated_knowledge_root_recorded", {"native_roots": {index: root["knowledgearticleid"]}})
            arabic_key = "article_" + index + "_ar"
            if arabic_key not in self.state["owned"]:
                require(not self.translations(group), "Unrecorded existing translation; refuse automatic adoption.")
                response = self.request({
                    "kind": "translation", "path": "CreateKnowledgeArticleTranslation", "method": "POST",
                    "body": {
                        "Source": {"@odata.type": "Microsoft.Dynamics.CRM.knowledgearticle", "knowledgearticleid": group["en_id"]},
                        "Language": {"@odata.type": "Microsoft.Dynamics.CRM.languagelocale",
                                     "languagelocaleid": self.knowledge["locales"]["ar-SA"]},
                        "IsMajor": True,
                    }, "group": group["index"],
                })
                self.complete_translation(group, response)
            for language, key in (("en-US", "article_" + index + "_en"), ("ar-SA", arabic_key)):
                row = self.current_owned(key)
                self.check_article_identity(row, group, language, primary if language == "ar-SA" else None)
                for category in (self.knowledge["root_category_id"], group["category_id"]):
                    self.link({"table": "knowledgearticles", "id": row["knowledgearticleid"],
                               "relation": "knowledgearticle_category", "target_table": "categories", "target_id": category})
                self.publish_article(key, group, language)
        self.ensure_knowledge_visibility()
        self.verify_knowledge(search=True)
        self.journal.save("published_knowledge_verified", {"stages": {"knowledge": True}})

    def shared_visibility(self):
        return one(self.api.rows("msdyn_knowledgesearchfilters", {
            "$filter": VISIBILITY_FILTER, "$select": "msdyn_enabled,msdyn_defaultvalues",
        }), "native Knowledge visibility filter")

    def personal_visibility(self, user):
        return self.api.rows("msdyn_knowledgepersonalfilters", {
            "$filter": VISIBILITY_FILTER + " and _ownerid_value eq " + user,
            "$select": "msdyn_knowledgepersonalfilterid,msdyn_enabled,msdyn_preferredvalues",
        })

    def check_visibility_owner(self, item):
        row = self.current_owned(item["key"])
        require(row.get("_ownerid_value") == item["user"],
                f"The personal Visibility filter {item['id']} isn't owned by representative {item['user']}, so it "
                "doesn't apply to them. Stop and ask the owner; nothing else was changed.")

    def ensure_knowledge_visibility(self):
        """Create an approved representative's own Internal filter only when internal articles are hidden."""
        shared = self.shared_visibility()
        hidden = shared.get("msdyn_enabled") is True and "1" not in visibility_values(shared.get("msdyn_defaultvalues"))
        caller = None
        for item in self.plan.get("knowledge_visibility", []):
            if item["key"] in self.state["owned"]:
                self.check_visibility_owner(item)
                continue
            if not hidden or self.personal_visibility(item["user"]):
                continue
            require(self.config["approvals"].get("representative_knowledge_visibility", False),
                    f"Native Knowledge excludes internal articles for representative {item['user']}, who has no personal "
                    "Visibility filter. The plan's representative_knowledge_visibility approval is false, so no filter "
                    "was created. Save Visibility = Internal or Both in that user's Knowledge search filters.")
            if caller is None:
                caller = guid(self.api.get("WhoAmI")["UserId"])
            self.create(item, caller=None if caller == item["user"] else item["user"])
            self.check_visibility_owner(item)

    def verify_knowledge(self, *, search=False, record_search=True):
        if not self.config["enable_knowledge"]:
            require(not self.knowledge["groups"], "Unselected knowledge must not contain article writes.")
            return
        owned_ids = set()
        for group in self.knowledge["groups"]:
            index = str(group["index"])
            english = self.current_owned("article_" + index + "_en")
            arabic = self.current_owned("article_" + index + "_ar")
            require(len(self.translations(group)) == 1, "Duplicate native Arabic translations.")
            for language, row in (("en-US", english), ("ar-SA", arabic)):
                self.check_article_identity(row, group, language, english if language == "ar-SA" else None)
                require((row["statecode"], row["statuscode"]) == (3, 7) and row["islatestversion"],
                        "Knowledge publication/latest-version gate failed.")
                check_payload(row, group["versions"][language], "published " + language)
                owned_ids.add(row["knowledgearticleid"])
                categories = self.api.rows(record_path("knowledgearticles", row["knowledgearticleid"]) +
                                           "/knowledgearticle_category", {"$select": "categoryid"})
                require({entry["categoryid"] for entry in categories} ==
                        {self.knowledge["root_category_id"], group["category_id"]},
                        "Owned knowledge category associations differ from the plan.")
            root_id = english["_rootarticleid_value"]
            require(root_id == self.state["native_roots"][index], "Native root ownership receipt differs.")
            same_number = self.api.rows("knowledgearticles", {
                "$select": "knowledgearticleid,isrootarticle", "$filter": "articlepublicnumber eq " + quote(group["number"]),
            })
            require({row["knowledgearticleid"] for row in same_number} ==
                    {english["knowledgearticleid"], arabic["knowledgearticleid"], root_id},
                    "Unexpected extra knowledge versions/root containers; do not overwrite or delete them.")
            owned_ids.add(root_id)
        current = self.api.rows("knowledgearticles", {
            "$select": "knowledgearticleid,articlepublicnumber,statecode,statuscode,isrootarticle",
        })
        before = article_inventory(self.native["unrelated_knowledge"])
        after = article_inventory([row for row in current if row["knowledgearticleid"] not in owned_ids])
        require(before == after, "Unrelated article inventory/state/ETags changed; preservation cannot be claimed.")
        for item in self.plan.get("knowledge_visibility", []):
            if item["key"] in self.state["owned"]:
                self.check_visibility_owner(item)
        if search:
            self.verify_search(record=record_search)

    def verify_search(self, *, record=True):
        self.verify_knowledge_visibility()
        prefix = self.config["prefix"] + " | "
        pending = {}
        for group in self.knowledge["groups"]:
            for language in ("en-US", "ar-SA"):
                title = group["versions"][language]["title"]
                require(title.startswith(prefix) and title[len(prefix):].strip(),
                        "A native search query needs the approved localized title without its ownership prefix.")
                pending[(group["index"], language)] = title[len(prefix):].strip()

        def indexed():
            for (index, language), term in list(pending.items()):
                group = self.knowledge["groups"][index - 1]
                body = {
                    "SearchText": term, "UseInflection": True, "RemoveDuplicates": False, "StateCode": 3,
                    "QueryExpression": {
                        "@odata.type": "Microsoft.Dynamics.CRM.QueryExpression", "EntityName": "knowledgearticle",
                        "ColumnSet": {"AllColumns": False, "Columns": ["knowledgearticleid", "articlepublicnumber", "languagelocaleid"]},
                        "PageInfo": {"Count": 100, "PageNumber": 1},
                    },
                }
                found_for_all = True
                key = "article_" + str(index) + ("_ar" if language == "ar-SA" else "_en")
                require(key in self.state["owned"], "Native search needs the original localized article ownership receipt.")
                expected_id = self.state["owned"][key]["id"]
                for user in self.config["representative_ids"]:
                    result = self.api.request("FullTextSearchKnowledgeArticle", method="POST", body=body,
                                              headers={"MSCRMCallerID": user})
                    found_for_all = found_for_all and any(
                        row.get("knowledgearticleid") == expected_id and
                        row.get("articlepublicnumber") == group["number"] and
                        row.get("_languagelocaleid_value") == self.knowledge["locales"][language]
                        for row in result["value"]
                    )
                if found_for_all:
                    del pending[(index, language)]
            if record:
                self.journal.save("native_fulltext_readback", evidence={"pending": [
                    {"group": index, "language": language, "term": term} for (index, language), term in pending.items()
                ]})
            return not pending
        self.wait("Native bilingual full-text indexing", indexed)

    def verify_knowledge_visibility(self):
        shared = self.shared_visibility()
        if shared.get("msdyn_enabled") is not True:
            return
        for user in self.config["representative_ids"]:
            personal = self.personal_visibility(user)
            require(len(personal) <= 1, "Ambiguous personal Knowledge visibility filters; review without overwriting them.")
            value = (personal[0].get("msdyn_preferredvalues")
                     if personal and personal[0].get("msdyn_enabled") is True else shared.get("msdyn_defaultvalues"))
            require("1" in visibility_values(value),
                    "Native Knowledge excludes internal articles for representative " + user +
                    ". The knowledge stage creates that user's own Internal filter only with approval "
                    "representative_knowledge_visibility and no existing personal filter. Otherwise select Visibility = "
                    "Internal or Both in that user's Knowledge search and save. Do not make articles public or change "
                    "shared filter defaults. No existing preference was changed by this kit.")

    def shared_snapshot(self, family):
        table, primary = SHARED_TABLES[family]
        return self.read(table, self.native["shared"][family][primary])

    def shared_patch(self, family):
        patch = SUMMARY_PATCHES[family]
        row = self.shared_snapshot(family)
        baseline = self.native["shared"][family]
        require(all(field in row and type(row[field]) is bool for field in patch), "Unsupported native summary/Copilot schema.")
        if family in self.state["shared"]:
            receipt = self.state["shared"][family]
            check_payload(row, receipt["after"], family)
            return
        require(etag(row) == etag(baseline), f"{family} settings changed since planning. Do not overwrite another administrator.")
        changes = {field: value for field, value in patch.items() if row[field] != value}
        if not changes:
            return
        table, primary = SHARED_TABLES[family]
        operation = {
            "kind": "shared_patch", "family": family, "method": "PATCH",
            "path": record_path(table, row[primary]), "body": changes, "headers": {"If-Match": etag(row)},
            "before": {field: row[field] for field in changes},
        }
        self.request(operation)
        after = self.shared_snapshot(family)
        check_payload(after, changes, family)
        self.finish("shared_flags_read_back", {"shared": {family: {
            "before": operation["before"], "after": changes, "after_etag": etag(after),
        }}})

    def copilot_prerequisites(self):
        self.approval("licenses_and_region_reviewed")
        row = self.shared_snapshot("copilot")
        require(row.get("msdyn_copilotenabled") is True and row.get("msdyn_agentassistenabled") is True,
                "Native Copilot isn't opted in, or Ask a question is off. An administrator completes the opt-in in "
                "Copilot Service admin center (workspace-setup.md, Native Copilot opt-in); this kit doesn't accept "
                "terms, buy licenses or change data movement.")
        require(row.get("msdyn_allagentsenabled") is True and row.get("msdyn_allworkstreamsenabled") is True,
                "Native Copilot access is selectively scoped. An administrator must verify/authorize these representatives and workstreams; this recipe does not widen access lists.")
        legacy = self.shared_snapshot("legacy")
        require(legacy.get("msdyn_enabledforagents") is True and legacy.get("msdyn_allagentsenabled") is True,
                "Legacy summary access is not enabled for these users. Resolve through the supported admin UI without widening unrelated access.")

    def verify_copilot(self):
        if not self.config["enable_copilot"]:
            return
        for family, expected in SUMMARY_PATCHES.items():
            row = self.shared_snapshot(family)
            check_payload(row, expected, "aligned " + family)
            baseline = self.native["shared"][family]
            for field, value in baseline.items():
                if type(value) is bool and field not in expected:
                    require(row.get(field) == value, f"Unrelated boolean setting changed: {family}/{field}.")
        row = self.shared_snapshot("copilot")
        require(row.get("msdyn_systemstatus") == COPILOT_READY,
                "Native knowledge source is not Ready. Never PATCH msdyn_systemstatus; use supported native provisioning/status.")

    def copilot_readiness(self):
        row = self.shared_snapshot("copilot")
        try:
            worker = json.loads(row.get("msdyn_dvcopilotstatus") or "{}")
        except (TypeError, ValueError):
            worker = {}
        worker = worker if isinstance(worker, dict) else {}
        return {
            "systemstatus": row.get("msdyn_systemstatus"),
            "knowledge_source_enabled": row.get("msdyn_knowledgearticlesourceenabled"),
            "worker_status": worker.get("status"), "worker_tries": worker.get("tries"),
            "worker_last_updated": worker.get("last_updated_timestamp"), "worker_message": worker.get("message"),
        }

    @staticmethod
    def copilot_ready(state):
        return state["systemstatus"] == COPILOT_READY and str(state["worker_status"]).lower() == "success"

    def provision_copilot(self):
        if not self.config["enable_copilot"]:
            self.journal.save("copilot_not_selected", evidence={"configured": False, "shared_settings_changed": False})
            return
        self.approval("shared_copilot_settings")
        require(self.state["stages"].get("knowledge"), "Publish and verify native knowledge before enabling its Copilot source.")
        self.copilot_prerequisites()
        for family in SUMMARY_PATCHES:
            self.shared_patch(family)
        observed = {"state": self.copilot_readiness()}
        if not self.copilot_ready(observed["state"]):
            if self.config["approvals"]["native_copilot_status_check"] and not self.state.get("status_checked"):
                actions = self.api.rows("workflows", {
                    "$select": "workflowid,name",
                    "$filter": "name eq 'Check Dataverse Copilot Status' and category eq 3 and type eq 1 and statecode eq 1 and ismanaged eq true",
                })
                one(actions, "installed native Check Dataverse Copilot Status action")
                self.request({"kind": "status_check", "path": "msdyn_CheckDVCopilotStatus", "method": "POST", "body": {}})
                self.finish("native_status_check_requested", {"status_checked": True})
            observed["saved"] = None

            def ready():
                state = self.read_fresh(self.copilot_readiness)
                observed["state"] = state
                if state != observed["saved"]:
                    self.journal.save("native_copilot_status_readback", evidence=state)
                    observed["saved"] = state
                return self.copilot_ready(state)

            def timeout():
                state = observed["state"]
                return (f"Native Copilot knowledge isn't Ready after {self.copilot_wait_seconds // 60} minutes: status "
                        f"{state['systemstatus']} (Ready is {COPILOT_READY}), worker {state['worker_status']} after "
                        f"{state['worker_tries']} tries, last update {state['worker_last_updated']}. Check "
                        f"{COPILOT_ADMIN_PAGES}. Then rerun the copilot stage with the same output directory, or wait "
                        "longer with --copilot-wait-minutes. Don't turn on the managed Check DV Copilot Sync Status "
                        "Flow or edit the status.")

            self.wait("Native Copilot knowledge readiness", ready, seconds=self.copilot_wait_seconds, timeout=timeout)
        self.verify_copilot()
        self.journal.save("native_copilot_ready", {"stages": {"copilot": True}},
                          {"native_systemstatus": COPILOT_READY, "worker": observed["state"],
                           "live_case_querying_configured": False})

    def representative_done(self, op):
        if op["kind"] in ("role", "membership"):
            return digest(op["link"]) in self.state["links"]
        return op["item"]["key"] in self.state["owned"]

    def representative_expected(self):
        plan = self.plan["representative"]
        state = copy.deepcopy(plan["before"])
        for op in plan["operations"]:
            if not self.representative_done(op):
                continue
            if op["kind"] == "role":
                state["direct_roles"].append(op["role"])
            elif op["kind"] == "resource":
                state["resources"].append({"bookableresourceid": op["item"]["id"], "_userid_value": plan["user_id"],
                                           "resourcetype": 3, "statecode": 0})
            elif op["kind"] == "capacity":
                state["capacities"].append({
                    routing.CAPACITY_ID: op["item"]["id"], "_msdyn_bookableresourceid_value": plan["resource_id"],
                    "_msdyn_capacityprofileid_value": plan["capacity_profile_id"], "msdyn_maxunits": None,
                    "statecode": 0, "statuscode": 1,
                })
            else:
                state["member"] = True
        return state

    def representative_unchanged(self):
        live = representative_state(self.api, self.config)
        actual, expected = representative_view(live), representative_view(self.representative_expected())
        changed = sorted(key for key in expected if actual[key] != expected[key])
        require(not changed, "The representative's " + ", ".join(changed) + " changed since planning (or since this "
                "kit's last recorded write). Nothing more was written. Ask the owner what changed before continuing.")
        return live

    def provision_representative(self):
        plan = self.plan.get("representative")
        if not plan:
            self.journal.save("representative_setup_not_selected", evidence={"configured": False})
            return None
        self.approval("setup_representative")
        require(not plan["gates"], "Representative setup was gated at planning: " + " ".join(plan["gates"]) +
                " Resolve these, then plan again in a new output directory.")
        self.idle()
        for op in plan["operations"]:
            if self.representative_done(op):
                continue
            self.representative_unchanged()
            if op["kind"] in ("role", "membership"):
                self.link(op["link"])
            else:
                self.create(op["item"])
        summary = self.verify_representative()
        self.journal.save("representative_setup_verified", {"stages": {"representative": True}}, summary)
        return summary

    def verify_representative(self):
        plan = self.plan.get("representative")
        if not plan:
            return None
        require(not plan["gates"], "Representative setup was gated at planning: " + " ".join(plan["gates"]))
        require(all(self.representative_done(op) for op in plan["operations"]),
                "Representative setup is incomplete. Run the representative stage.")
        live = self.representative_unchanged()
        for op in plan["operations"]:
            if op["kind"] in ("resource", "capacity"):
                self.current_owned(op["item"]["key"])
        names = {row["name"].strip().casefold(): row["name"].strip() for row in routing.effective_roles(live).values()}
        missing = [name for name in REPRESENTATIVE_ROLES if name.casefold() not in names]
        require(not missing, "The representative still lacks: " + ", ".join(missing) + ".")
        resources = [row for row in live["resources"] if row.get("statecode") == 0 and row.get("resourcetype") == 3]
        require(len(resources) == 1 and guid(resources[0]["bookableresourceid"]) == plan["resource_id"],
                "Expected exactly one active User bookable resource for the representative.")
        pairs = [row for row in live["capacities"] if row.get("_msdyn_bookableresourceid_value") == plan["resource_id"]
                 and row.get("_msdyn_capacityprofileid_value") == plan["capacity_profile_id"]]
        require(len(pairs) == 1 and pairs[0].get("statecode") == 0 and pairs[0].get("statuscode") == 1,
                "Expected exactly one active association with the inbound capacity profile.")
        require(live["member"], "The representative isn't a member of the fallback queue.")
        return {
            "roles": [names[name.casefold()] for name in REPRESENTATIVE_ROLES],
            "bookable_resource_id": plan["resource_id"], "capacity_profile_id": plan["capacity_profile_id"],
            "capacity_association_id": guid(pairs[0][routing.CAPACITY_ID]), "queue_id": plan["queue_id"],
            "queue_member": True,
            "added_by_this_kit": sorted({op["kind"] for op in plan["operations"]}),
        }

    def binding_expectation(self, *, bound):
        memberships = profile_memberships(self.native["profiles"])
        profile = self.workspace["ids"]["profile"]
        memberships[profile] = sorted(self.config["representative_ids"]) if bound else []
        if bound:
            for user, identifier in self.native["source_assignments"].items():
                if identifier:
                    memberships[identifier].remove(user)
        workstreams = {row["msdyn_liveworkstreamid"]: (
            self.config["prefix"] + "_voice" if bound else row["msdyn_sessiontemplate_default"]
        ) for row in self.native["workstreams"]}
        return memberships, workstreams

    def verify_bindings(self, *, bound):
        memberships, streams = self.binding_expectation(bound=bound)
        require(profile_memberships(all_profiles(self.api)) == memberships,
                "Profile membership drift (including unapproved users). No broad reassignment is allowed.")
        for identifier, expected in streams.items():
            row = self.read("msdyn_liveworkstreams", identifier)
            require(row.get("msdyn_sessiontemplate_default") == expected, "Selected workstream binding drift.")
        if bound:
            for user in self.config["representative_ids"]:
                result = self.api.get("msdyn_getAppConfigByAgent(UserId=" + quote(user) +
                                      ",AppUniqueName=" + quote(self.native["app"]["uniquename"]) + ")")
                resolved = json.loads(result["AppConfigurations"])
                require([row["AppConfigId"] for row in resolved] == [self.workspace["ids"]["profile"]],
                        "Native effective profile is not exactly the dedicated profile. Check competing role/default mappings.")

    def binding_operations(self, *, bound):
        before_memberships, before_streams = self.binding_expectation(bound=not bound)
        after_memberships, after_streams = self.binding_expectation(bound=bound)
        operations = []
        for identifier, value in after_streams.items():
            row = self.read("msdyn_liveworkstreams", identifier)
            require(row["msdyn_sessiontemplate_default"] == before_streams[identifier], "Workstream changed before binding.")
            operations.append({"method": "PATCH", "path": record_path("msdyn_liveworkstreams", identifier),
                               "etag": etag(row), "body": {"msdyn_sessiontemplate_default": value}})
        for identifier, users in after_memberships.items():
            removed = set(before_memberships[identifier]) - set(users)
            added = set(users) - set(before_memberships[identifier])
            if not removed and not added:
                continue
            row = self.read("msdyn_appconfigurations", identifier)
            for user in sorted(removed):
                operations.append({
                    "method": "DELETE", "path": record_path("msdyn_appconfigurations", identifier) +
                    "/" + PROFILE_USERS + "(" + user + ")/$ref", "etag": etag(row),
                })
            for user in sorted(added):
                operations.append({
                    "method": "POST", "path": record_path("msdyn_appconfigurations", identifier) + "/" + PROFILE_USERS + "/$ref",
                    "etag": etag(row),
                    "body": {"@odata.id": self.config["org_url"] + "/api/data/v9.2/systemusers(" + user + ")"},
                })
        return operations

    def batch(self, operations, *, kind, extra=None):
        boundary, changeset = "batch_" + uuid4().hex, "changeset_" + uuid4().hex
        lines = [f"--{boundary}", f"Content-Type: multipart/mixed; boundary={changeset}", ""]
        for index, operation in enumerate(operations, 1):
            etag({"@odata.etag": operation["etag"]})
            lines.extend([
                f"--{changeset}", "Content-Type: application/http", "Content-Transfer-Encoding: binary",
                f"Content-ID: {index}", "",
                operation["method"] + " " + self.config["org_url"] + "/api/data/v9.2/" + operation["path"] + " HTTP/1.1",
                "If-Match: " + operation["etag"],
            ])
            if "body" in operation:
                lines.extend(["Content-Type: application/json", "", canonical_json(operation["body"]), ""])
            else:
                lines.extend(["", ""])
        lines.extend([f"--{changeset}--", f"--{boundary}--", ""])
        request = {
            "kind": kind, "path": "$batch", "method": "POST", "body": "\r\n".join(lines),
            "headers": {"Content-Type": f"multipart/mixed; boundary={boundary}"}, "raw": True,
            "operations": operations, **(extra or {}),
        }
        response = self.request(request)
        codes = [int(value) for value in re.findall(r"HTTP/1\.1 (\d{3})", response)]
        require(len(codes) == len(operations) and all(code in {200, 201, 204} for code in codes),
                "Atomic binding was not confirmed. Inspect the batch receipt before recovery/retry.")

    def bind(self):
        self.approval("bind_representatives_and_workstreams")
        prerequisites = ["workspace"]
        if self.plan.get("representative"):
            prerequisites.append("representative")
        if self.config["enable_knowledge"]:
            prerequisites.append("knowledge")
        if self.config["enable_copilot"]:
            prerequisites.append("copilot")
        require(all(self.state["stages"].get(stage) for stage in prerequisites),
                "Workspace and every selected representative/knowledge/Copilot stage must verify before user/workstream binding.")
        self.verify_workspace()
        self.verify_copilot()
        if self.state["bound"]:
            self.verify_bindings(bound=True)
            return
        self.verify_bindings(bound=False)
        self.idle()
        operations = self.binding_operations(bound=True)
        self.idle()
        self.batch(operations, kind="binding", extra={"bound": True})
        self.verify_bindings(bound=True)
        self.finish("atomic_binding_read_back", {"bound": True, "stages": {"bind": True}})

    def verify(self, *, record=True):
        require(not self.state["pending"], "Resolve the pending mutation before verification.")
        self.verify_workspace()
        self.verify_knowledge(search=True, record_search=record)
        self.verify_copilot()
        self.verify_bindings(bound=True)
        representative = self.verify_representative()
        self.user_settings_unchanged()
        if record:
            self.journal.save("configuration_verified", {"stages": {"verify": True}}, {
                **{label: sum(row["table"] == table for row in self.plan["workspace"]["records"]) for label, table in (
                    ("profile", "msdyn_appconfigurations"), ("sessions", "msdyn_sessiontemplates"),
                    ("tabs", "msdyn_applicationtabtemplates"), ("scripts", "msdyn_productivityagentscripts"),
                    ("steps", "msdyn_productivityagentscriptsteps"), ("activated_macros", "workflows"),
                )},
                "published_articles": self.knowledge["article_count"], "translation_pairs": len(self.knowledge["groups"]),
                "copilot_selected": self.config["enable_copilot"], "acceptance": self.plan["acceptance"],
                "extension": self.state.get("extension"), "representative_setup": representative,
            })

    def recover(self):
        operation = self.state["pending"]
        require(operation, "No pending mutation needs recovery.")
        kind = operation["kind"]
        failed = operation.get("http_status") in {400, 401, 403, 404, 409, 412, 429}
        if kind == "create":
            item = operation["item"]
            if failed:
                require(self.optional(item["table"], item["id"]) is None,
                        "A failed create now collides with an existing record; no ownership adoption is allowed.")
                self.finish("failed_create_confirmed_absent")
            else:
                self.complete_create(item)
        elif kind == "owned_patch":
            item = self.state["owned"][operation["key"]]
            row = self.read(item["table"], item["id"])
            if failed:
                require(etag(row) == operation["headers"]["If-Match"],
                        "Failed patch raced another writer; inspect the drift before recovery.")
                check_payload(row, item["expected"], operation["key"])
                self.finish("failed_patch_confirmed_unapplied")
            else:
                self.complete_owned_patch(operation)
        elif kind == "link":
            if self.link_present(operation["link"]):
                require(not failed, "A rejected association now exists without confirmed ownership; inspect the concurrent writer.")
                self.finish("association_recovered", {"links": {digest(operation["link"]): operation["link"]}})
            else:
                require(failed, "Association outcome unknown; inspect the native request before a new attempt.")
                self.finish("failed_association_confirmed_absent")
        elif kind == "translation":
            group = self.knowledge["groups"][operation["group"] - 1]
            if failed:
                require(not self.translations(group), "A rejected translation now exists; no automatic adoption.")
                self.finish("failed_translation_confirmed_absent")
            else:
                self.complete_translation(group, operation.get("response"))
        elif kind == "language":
            response = operation.get("response") or {}
            if response.get("AsyncOperationId"):
                self.finish("language_job_recovered", {"language_job": guid(response["AsyncOperationId"])})
            elif 1025 in self.languages():
                self.user_settings_unchanged()
                self.finish("language_availability_recovered", {"stages": {"language": True}})
            elif failed:
                self.finish("failed_language_request_recorded")
            else:
                raise GateError("ProvisionLanguageAsync outcome is unknown. Inspect System Jobs; wait for its result. Do not submit a duplicate language job.")
        elif kind in ("shared_patch", "shared_restore"):
            family = operation["family"]
            row = self.shared_snapshot(family)
            if failed:
                require(etag(row) == operation["headers"]["If-Match"],
                        "Rejected shared-setting write raced another administrator; do not adopt their change.")
                self.finish("shared_patch_confirmed_unapplied")
            else:
                require("response" in operation, "Shared-setting ownership needs a confirmed native response; inspect native audit history after a lost response.")
                check_payload(row, operation["body"], family)
                if kind == "shared_restore":
                    receipt = {**self.state["shared"][family], "restored": True}
                else:
                    receipt = {"before": operation["before"], "after": operation["body"], "after_etag": etag(row)}
                self.finish("shared_patch_recovered", {"shared": {family: receipt}})
        elif kind == "status_check":
            if failed:
                self.finish("native_status_check_rejected")
            else:
                require("response" in operation or self.shared_snapshot("copilot").get("msdyn_systemstatus") == 100230003,
                        "Native status-check outcome not confirmed; inspect the admin page without forcing Ready.")
                self.finish("native_status_check_recovered", {"status_checked": True})
        elif kind in ("app_add", "app_remove", "app_publish") and failed:
            require(operation.get("app_before") is not None,
                    "Rejected app action has no recorded pre-action state; inspect native audit history.")
            require(self.app_action_state() == operation["app_before"],
                    "Rejected app action's pre-action app/component state drifted; no recovery or ownership adoption.")
            self.finish("failed_app_action_confirmed_unapplied")
        elif kind in ("app_add", "app_remove"):
            self.validate_staged_app(removing=kind == "app_remove")
            self.finish("app_registration_recovered", {"app_registered": kind == "app_add",
                                                     "app_removal_staged": kind == "app_remove"})
        elif kind == "app_publish":
            removing = operation.get("removing", False)
            self.published_app_ok(removing=removing)
            self.finish("app_publication_recovered", {"app_published": not removing,
                                                    "app_removal_staged": False})
        elif kind == "binding":
            codes = [int(value) for value in re.findall(r"HTTP/1\.1 (\d{3})", operation.get("response") or "")]
            if failed or any(code >= 400 for code in codes):
                self.verify_bindings(bound=not operation["bound"])
                self.finish("failed_atomic_binding_confirmed_unapplied")
            else:
                self.verify_bindings(bound=operation["bound"])
                self.finish("binding_recovered", {"bound": operation["bound"],
                                                 "stages": {"bind": operation["bound"]}})
        else:
            raise GateError("This uncertain action requires native administrator investigation; no automatic retry is safe.")

    def rollback_inventory(self):
        return {
            "mode": "detach-and-retain, not destructive purge",
            "bound": self.state["bound"],
            "restore_workstreams_and_only_recorded_user_memberships": self.binding_expectation(bound=False),
            "remove_exact_app_component": self.app_component_body() if self.state["app_registered"] else None,
            "app_only_publication": self.publication_body(),
            "restore_only_kit_changed_shared_flags": self.state["shared"],
            "archive_only_owned_published_articles": [
                {"key": key, "id": item["id"]} for key, item in self.state["owned"].items()
                if item["table"] == "knowledgearticles" and item["expected"].get("statecode") == 3
            ],
            "deactivate_owned_macros": [item["id"] for item in self.state["owned"].values() if item["table"] == "workflows"],
            "retained": "Owned records/categories/roots and nondefault form for audit; Arabic organization availability; all unrelated records.",
            "retained_native_channel_profile_links": self.native_channel_links(),
            "retained_representative_setup": [
                op.get("link") or {"table": op["item"]["table"], "id": op["item"]["id"]}
                for op in (self.plan.get("representative") or {}).get("operations", []) if self.representative_done(op)
            ],
            "retained_personal_visibility_filters": [
                item["id"] for item in self.plan.get("knowledge_visibility", []) if item["key"] in self.state["owned"]
            ],
            "implementation_evidence": "Offline-tested; this inventory alone does not establish that rollback executed.",
        }

    def rollback(self):
        require(not self.state["pending"], "Recover the pending action before rollback.")
        if self.state.get("extension"):
            require(self.state["extension"].get("status") == "released",
                    "Release the owned supervision addon and record release before base rollback.")
            self.extension_links()
        for session in ("voice_session", "case_session"):
            if session in self.state["owned"]:
                identifier = self.workspace["ids"][session]
                expected = {link["target_id"] for link in self.workspace["links"]
                            if link["id"] == identifier and link["relation"] == EXTENSION_RELATION}
                require(set(membership(self.api, identifier)) <= expected,
                        "Unregistered session-tab delta blocks base rollback; the addon owner must release it first.")
        if self.state["rolled_back"]:
            if "profile" in self.state["owned"]:
                self.verify_bindings(bound=False)
            else:
                require(profile_memberships(all_profiles(self.api)) == profile_memberships(self.native["profiles"]),
                        "Unrelated profile membership changed after partial rollback.")
            if "contact_form" in self.state["owned"]:
                self.published_app_ok(removing=True)
            return
        self.idle()
        if self.state["bound"]:
            self.verify_bindings(bound=True)
            self.batch(self.binding_operations(bound=False), kind="binding", extra={"bound": False})
            self.verify_bindings(bound=False)
            self.finish("bindings_restored", {"bound": False, "stages": {"bind": False}})
        elif "profile" in self.state["owned"]:
            self.verify_bindings(bound=False)
        for family, receipt in list(self.state["shared"].items()):
            if receipt.get("restored"):
                check_payload(self.shared_snapshot(family), receipt["before"], family)
                continue
            row = self.shared_snapshot(family)
            check_payload(row, receipt["after"], family)
            table, primary = SHARED_TABLES[family]
            self.request({
                "kind": "shared_restore", "family": family, "method": "PATCH",
                "path": record_path(table, row[primary]), "body": receipt["before"],
                "headers": {"If-Match": etag(row)},
            })
            check_payload(self.shared_snapshot(family), receipt["before"], family)
            self.finish("shared_flags_restored", {"shared": {family: {**receipt, "restored": True}}})
        for key, item in list(self.state["owned"].items()):
            if item["table"] == "knowledgearticles":
                row = self.current_owned(key)
                if row["statecode"] == 3:
                    self.patch_owned(key, {"statecode": 5, "statuscode": 12})
            if item["table"] == "workflows":
                self.patch_owned(key, {"statecode": 0, "statuscode": 1})
        if self.state["app_registered"]:
            self.approval("publish_app")
            if self.state["app_published"]:
                self.published_app_ok()
            else:
                self.validate_staged_app()
            self.idle()
            self.request({"kind": "app_remove", "path": "RemoveAppComponents", "method": "POST",
                          "body": self.app_component_body()})
            self.validate_staged_app(removing=True)
            self.finish("owned_app_component_removed", {"app_registered": False, "app_removal_staged": True})
        if self.state.get("app_removal_staged"):
            _, before = self.validate_staged_app(removing=True)
            _, after = self.validate_staged_app(removing=True)
            require(etag(before) == etag(after), "App draft changed during rollback.")
            self.request({"kind": "app_publish", "path": "PublishXml", "method": "POST",
                          "body": self.publication_body(), "removing": True})
            self.published_app_ok(removing=True)
            self.finish("app_only_rollback_published", {"app_published": False, "app_removal_staged": False})
        self.user_settings_unchanged()
        self.journal.save("scoped_rollback_verified", {"rolled_back": True, "stages": {
            "workspace": False, "knowledge": False, "copilot": False, "bind": False, "verify": False,
        }}, self.rollback_inventory())


EXTENSION_KIND = "native-pause-ai-session-tab-v1"
LEGACY_KIND = "observed-legacy-workspace-baseline-v1"
RELEASE_KIND = "native-pause-ai-session-tab-release-v1"
EXTENSION_RELATION = "msdyn_sessiontemplate_applicationtab"
EXTENSION_COMMANDS = {"capture-legacy-baseline", "accept-extension", "verify-extension", "release-extension"}
COMPOSITION_LIMITS = {
    "configuration_only": True, "configured_selector_only": True, "rendered_host_verified": False,
    "ui_acceptance": "pending",
    "acoustic_acceptance_verified": False, "live_pause_test_performed": False,
}


def checked_json(path, expected=None):
    result = json.loads(read_private(path))
    require(expected is None or digest(result) == expected, "Private receipt fingerprint changed.")
    return result


def receipt_directory(value):
    path = private_path(value)
    require(path.is_dir() and (os.name == "nt" or path.stat().st_mode & 0o077 == 0),
            "Use an owner-only private receipt directory.")
    return path


def checkpoint(events, expected=None, *, supervision=False):
    require(events and all(event["sequence"] == index + 1 for index, event in enumerate(events)),
            "Receipt checkpoint requires a complete sequential journal.")
    index = expected["sequence"] - 1 if expected else len(events) - 1
    require(0 <= index < len(events), "Receipt checkpoint is missing.")
    actual = {"sequence": events[index]["sequence"],
              "sha256": events[index]["hash"] if supervision else digest(events[index])}
    require(expected is None or actual == expected, "Receipt checkpoint hash differs.")
    return actual


def portable_base(directory, pinned=None, *, released=False, recipe_update_candidate=None):
    directory = receipt_directory(directory)
    plan = checked_json(directory / "plan.json")
    require(plan.get("schema") == SCHEMA and validate_config(plan["config"]) == plan["config"],
            "Base recipe/configuration changed; do not rewrite its immutable plan.")
    journal = Journal(directory, plan)
    require_recipe(plan, journal, recipe_update_candidate)
    require(plan["workspace"] == render_workspace(plan["config"], plan["native"]),
            "Portable base payloads are not the pinned deterministic recipe.")
    require(not journal.state["pending"] and (released or (
            not journal.state["rolled_back"] and journal.state["bound"] and journal.state["stages"].get("verify"))),
            "A portable base needs completed, unblocked configuration verification.")
    events = [checked_json(path) for path in sorted(directory.glob("event-*.json"))]
    verified = [event for event in events if event["label"] == "configuration_verified"]
    require(verified, "Base has no actual configuration-verification checkpoint.")
    point = pinned or {"sequence": verified[-1]["sequence"], "sha256": digest(verified[-1])}
    checkpoint(events, point)
    require(events[point["sequence"] - 1]["label"] == "configuration_verified",
            "Base checkpoint is not a configuration-verification result.")
    reference = {"kind": "portable", "directory": str(directory), "plan_sha256": digest(plan), "checkpoint": point}
    return reference, plan, journal


def supervision_receipt(directory, api, *, pinned=None, released=False):
    import setup_native_supervision as supervision

    directory = receipt_directory(directory)
    plan = checked_json(directory / "plan.json")
    config = supervision.validate_config(plan["config"])
    require(plan.get("version") == 1 and plan["config_hash"] == supervision.digest(config) and
            plan["creation_targets"] == supervision.creation_targets(config),
            "Unsupported or changed supervision plan.")
    require(config["organization_url"] == api.org and config["tenant_id"] == api.tenant,
            "Supervision organization/tenant differs from the authenticated base scope.")
    require(len(config["session_template_ids"]) == 1 and config["session_binding_reviewed"],
            "This composition contract permits exactly one explicitly reviewed voice-session template.")
    require(set(plan["templates"]) == set(config["session_template_ids"]) and
            set(plan["source_hashes"]) == {"native-supervision.html", "native-supervision.js"},
            "Unexpected supervision template/resource scope.")
    journal = supervision.Journal(directory, plan)
    point = checkpoint(journal.events, pinned, supervision=True)
    for key in {event["key"] for event in journal.events if event["kind"] == "intent"}:
        intent, done, rejected = journal.last("intent", key), journal.last("done", key), journal.last("rejected", key)
        require(done and intent["sequence"] < done["sequence"] <= point["sequence"] and
                (not rejected or rejected["sequence"] < intent["sequence"]),
                "Supervision checkpoint has an unresolved/rejected intent or predates its verified completion: " + key)
    if not released:
        require(not any(event["kind"] in {"rollback_intent", "rolled_back", "release_intent", "released"}
                        for event in journal.events), "Supervision addon has pending or completed release/rollback.")
    identifier = config["session_template_ids"][0]
    tab = supervision.owned(config, "tab")
    session_key = "session_" + identifier
    keys = ("tab", "tab_parameter", "resource_native-supervision.html", "resource_native-supervision.js", "publish", session_key)
    for key in keys:
        intent, done = journal.last("intent", key), journal.last("done", key)
        rejected = journal.last("rejected", key)
        require(intent and done and done["sequence"] > intent["sequence"] and
                done["sequence"] <= point["sequence"] and
                (not rejected or rejected["sequence"] < intent["sequence"]),
                "Supervision creation/binding/publication lacks unrejected intent and completion: " + key)
        if key == "tab_parameter" and intent.get("operation") == "generated_parameter_update":
            supervision.check_generated_parameter(journal.last("done", "tab"), intent["before"])
            parameter_id = guid(intent["before"]["msdyn_templateparameterid"])
            require(intent.get("path") == done.get("path") == record_path("msdyn_templateparameters", parameter_id)
                    and intent.get("body") == done.get("body") == {
                        "msdyn_name": "webresourceName", "msdyn_value": "moacc_/native-supervision.html",
                        "msdyn_applicationtabtemplateid@odata.bind": "/msdyn_applicationtabtemplates(" + tab + ")",
                    } and intent.get("parent") == done.get("parent") == tab,
                    "Supervision parameter does not prove the exact native-generated child update.")
        elif key in plan["creation_targets"]:
            require(intent.get("operation") == "create" and "before" in intent and intent["before"] is None and
                    intent.get("path") == done.get("path") == plan["creation_targets"][key] and
                    intent.get("body") == done.get("body"),
                    "Supervision creation does not prove the exact newly owned record: " + key)
        elif key == "publish":
            xml = "<importexportxml><entities><entity>" + supervision.ENTITY + "</entity></entities><webresources>" + "".join(
                "<webresource>" + supervision.owned(config, "resource_" + name) + "</webresource>"
                for name in plan["source_hashes"]
            ) + "</webresources></importexportxml>"
            require(intent.get("operation") == "publication" and intent.get("body") == done.get("body") and
                    intent.get("body") == {"ParameterXml": xml},
                    "Supervision publication is not the exact owned entity/webresource scope.")
        else:
            require(intent.get("operation") == "associate" and intent.get("before") == [] and
                    all(intent.get(field) == done.get(field) for field in ("path", "target", "primary")),
                    "Supervision binding lacks its exact absent-before association intent.")
        if not released:
            require(not any(journal.last(kind, key) for kind in ("rollback_intent", "rolled_back", "release_intent", "released")),
                    "Supervision addon has pending or completed release/rollback.")
    path = record_path("msdyn_sessiontemplates", identifier) + "/" + EXTENSION_RELATION
    binding = journal.last("done", session_key)
    require(binding.get("path") == path and binding.get("target") == tab and
            binding.get("primary") == "msdyn_applicationtabtemplateid",
            "Supervision receipt does not own the exact permitted session-tab edge.")
    setup = supervision.Setup(api, plan, journal)
    if released:
        require(callable(getattr(setup, "verify_unbound", None)), "The supervision helper needs its verified unbind API before release.")
        setup.verify_unbound()
        require(journal.last("released", session_key)["sequence"] <= point["sequence"],
                "Release checkpoint predates the completed release evidence.")
    else:
        setup.verify()
    edge = {"table": "msdyn_sessiontemplates", "id": identifier, "relation": EXTENSION_RELATION,
            "target_table": "msdyn_applicationtabtemplates", "target_id": tab}
    before = sorted(row["msdyn_applicationtabtemplateid"] for row in plan["templates"][identifier]["tabs"])
    require(len(before) == len(set(before)) and tab not in before, "Supervision before-set is ambiguous or already contains its addon.")
    reference = {"directory": str(directory), "plan_sha256": digest(plan), "checkpoint": point}
    return reference, config, edge, before, journal


def membership(api, session):
    rows = api.rows(record_path("msdyn_sessiontemplates", session) + "/" + EXTENSION_RELATION,
                    {"$select": "msdyn_applicationtabtemplateid"})
    values = [row["msdyn_applicationtabtemplateid"] for row in rows]
    require(len(values) == len(set(values)), "Duplicate session-tab membership.")
    return sorted(values)


def configured_selector(api, config, session, expected_name, *, check_workstreams=True):
    row = api.get(record_path("msdyn_sessiontemplates", session), {"$select": "msdyn_uniquename,ismanaged"})
    require(row.get("ismanaged") is False and row["msdyn_uniquename"] == expected_name,
            "The reviewed session identity changed or is a managed default.")
    for identifier in config["workstream_ids"] if check_workstreams else ():
        stream = api.get(record_path("msdyn_liveworkstreams", identifier), {"$select": "msdyn_sessiontemplate_default"})
        require(stream.get("msdyn_sessiontemplate_default") == expected_name,
                "A selected workstream no longer selects the reviewed voice template.")


def legacy_snapshot(api, config, profile, app, session):
    application = api.get(record_path("appmodules", app))
    require(application["uniquename"] == "msdyn_CustomerServiceWorkspace", "Select the native Customer Service workspace app.")
    profile_row = api.get(record_path("msdyn_appconfigurations", profile), {
        "$select": "msdyn_uniquename,msdyn_appmoduleuniquename,ismanaged",
    })
    require(profile_row.get("ismanaged") is False and not profile_row["msdyn_uniquename"].startswith(("msdyn_", "d365")),
            "Legacy composition requires the explicitly selected nondefault unmanaged profile.")
    users = api.rows(record_path("msdyn_appconfigurations", profile) + "/" + PROFILE_USERS, {"$select": "systemuserid"})
    members = sorted(row["systemuserid"] for row in users)
    require(set(config["approved_actor_ids"]) <= set(members), "Approved supervision actors are not assigned to the selected legacy profile.")
    for user in config["approved_actor_ids"]:
        resolved = api.get("msdyn_getAppConfigByAgent(UserId=" + quote(user) +
                           ",AppUniqueName=" + quote(application["uniquename"]) + ")")
        require([row["AppConfigId"] for row in json.loads(resolved["AppConfigurations"])] == [profile],
                "The actor's effective legacy profile differs.")
    template = api.get(record_path("msdyn_sessiontemplates", session), {"$select": "msdyn_uniquename,ismanaged"})
    configured_selector(api, config, session, template["msdyn_uniquename"])
    components = api.rows("appmodulecomponents", {"$filter": "_appmoduleidunique_value eq " + application["appmoduleidunique"]})
    return {
        "profile_id": profile, "app_id": app, "profile_name": profile_row["msdyn_uniquename"], "members": members,
        "session_id": session, "template_name": template["msdyn_uniquename"],
        "app_authored": app_authored(application), "app_components": component_defaults(components),
    }


def capture_legacy_baseline(api, supervision_directory, profile, app, legacy_evidence, selector_evidence):
    import setup_native_supervision as supervision

    directory = receipt_directory(supervision_directory)
    plan = checked_json(directory / "plan.json")
    config = supervision.validate_config(plan["config"])
    require(plan.get("version") == 1 and plan["config_hash"] == supervision.digest(config) and
            plan["creation_targets"] == supervision.creation_targets(config) and
            len(config["session_template_ids"]) == 1 and set(plan["templates"]) == set(config["session_template_ids"]),
            "Use an unchanged single-session supervision plan.")
    proof = checked_json(selector_evidence)
    session = config["session_template_ids"][0]
    require(proof.get("kind") == "native-monitor-selector-evidence-v1" and
            proof.get("configured_selector_proven") is True and proof.get("rendered_host_proven") is False,
            "Provide the separately labelled configured Monitor-selector evidence, not a screenshot or fabricated UI acceptance.")
    source = proof.get("source", {})
    require(source.get("selector") == "TemplateManager.getSessionTemplateForWorkStream" and
            isinstance(source.get("sha256"), str) and re.fullmatch(r"[a-f0-9]{64}", source["sha256"]) and
            isinstance(source.get("url"), str) and source["url"].startswith(
                "https://oc-cdn-public-che.azureedge.net/convcontrol/"),
            "Selector evidence must identify the reviewed native source/version; configuration flags alone are insufficient.")
    require((proof["organization_url"], proof["tenant_id"], proof["profile_id"], proof["app_id"], proof["session_template_id"]) ==
            (config["organization_url"], config["tenant_id"], profile, app, session) and
            set(proof["workstream_ids"]) == set(config["workstream_ids"]),
            "Selector evidence belongs to another organization/profile/template/workstream scope.")
    snapshot = legacy_snapshot(api, config, profile, app, session)
    require(snapshot["template_name"] == proof["template_name"], "Configured selector evidence is stale.")
    before = membership(api, session)
    planned = sorted(row["msdyn_applicationtabtemplateid"] for row in plan["templates"][session]["tabs"])
    require(before == planned and supervision.owned(config, "tab") not in before,
            "Capture legacy baseline before binding; current membership must equal the actual supervision-plan before-set.")
    return {
        "kind": LEGACY_KIND, "captured_at": utc(), "organization_url": config["organization_url"],
        "tenant_id": config["tenant_id"], "snapshot": snapshot, "before": before,
        "legacy_evidence": {"path": str(Path(legacy_evidence).resolve()), "sha256": digest(read_private(legacy_evidence))},
        "selector_evidence": {"path": str(Path(selector_evidence).resolve()), "sha256": digest(proof)},
        "portable_ownership_claimed": False, "acceptance": COMPOSITION_LIMITS,
    }


def extension_base(api, manifest, config, edge, before, *, released=False, recipe_update_candidate=None):
    reference = manifest["base"]
    if reference["kind"] == "portable":
        actual, plan, journal = portable_base(reference["directory"], reference["checkpoint"], released=released,
                                            recipe_update_candidate=recipe_update_candidate)
        require(actual == reference, "Base plan/checkpoint differs.")
        cfg, workspace = plan["config"], plan["workspace"]
        require(config["organization_url"] == cfg["org_url"] and config["tenant_id"] == cfg["tenant_id"],
                "Base and supervision organization/tenant differ.")
        require(edge["id"] == workspace["ids"]["voice_session"] and
                set(config["workstream_ids"]) <= set(cfg["workstream_ids"]) and
                set(config["approved_actor_ids"]) <= set(cfg["representative_ids"]),
                "Addon targets an unowned session, workstream or actor.")
        expected = sorted(link["target_id"] for link in workspace["links"] if
                          link["id"] == edge["id"] and link["relation"] == EXTENSION_RELATION)
        configured_selector(api, config, edge["id"], cfg["prefix"] + "_voice",
                            check_workstreams=not released or journal.state["bound"])
        result = (plan, journal)
    else:
        require(reference["kind"] == "legacy", "Unsupported base kind; no implicit legacy adoption.")
        baseline = checked_json(reference["receipt"], reference["sha256"])
        require(baseline.get("kind") == LEGACY_KIND and baseline["portable_ownership_claimed"] is False and
                baseline.get("acceptance") == COMPOSITION_LIMITS and
                (baseline["organization_url"], baseline["tenant_id"]) ==
                (config["organization_url"], config["tenant_id"]), "Wrong legacy baseline scope/type.")
        evidence = baseline["legacy_evidence"]
        require(digest(read_private(evidence["path"])) == evidence["sha256"], "Original legacy evidence changed.")
        checked_json(baseline["selector_evidence"]["path"], baseline["selector_evidence"]["sha256"])
        saved = baseline["snapshot"]
        require(saved["session_id"] == edge["id"] and
                legacy_snapshot(api, config, saved["profile_id"], saved["app_id"], edge["id"]) == saved,
                "Preserved legacy profile/app/template configuration drifted.")
        expected, result = baseline["before"], None
    require(before == expected and manifest["before"] == expected, "Supervision before-set is not the exact referenced base membership.")
    return result


def extension_shape(manifest):
    require(set(manifest) == {"kind", "accepted_at", "organization_url", "tenant_id", "base", "supervision",
                              "edge", "before", "acceptance"} and
            manifest.get("kind") == EXTENSION_KIND and manifest.get("acceptance") == COMPOSITION_LIMITS,
            "Unsupported extension receipt or claimed UI/acoustic acceptance.")


def verify_extension(api, manifest, *, recipe_update_candidate=None):
    extension_shape(manifest)
    ref = manifest["supervision"]
    actual, config, edge, before, _ = supervision_receipt(ref["directory"], api, pinned=ref["checkpoint"])
    require(actual == ref and manifest["edge"] == edge and
            (manifest["organization_url"], manifest["tenant_id"]) == (config["organization_url"], config["tenant_id"]),
            "Extension plan/head/organization/owned edge differs.")
    base = extension_base(api, manifest, config, edge, before, recipe_update_candidate=recipe_update_candidate)
    require(membership(api, edge["id"]) == sorted(before + [edge["target_id"]]),
            "Live membership is not exactly baseline plus the one owned supervision tab.")
    return base


def verify_extension_release(api, manifest, release):
    extension_shape(manifest)
    require(set(release) == {"kind", "released_at", "acceptance_sha256", "supervision"} and
            release.get("kind") == RELEASE_KIND and release["acceptance_sha256"] == digest(manifest),
            "Release receipt belongs to another acceptance.")
    ref = release["supervision"]
    actual, config, edge, before, journal = supervision_receipt(ref["directory"], api, pinned=ref["checkpoint"], released=True)
    require(actual == ref and ref["directory"] == manifest["supervision"]["directory"] and
            ref["plan_sha256"] == manifest["supervision"]["plan_sha256"] and edge == manifest["edge"],
            "Released addon plan/head/edge changed.")
    checkpoint(journal.events, manifest["supervision"]["checkpoint"], supervision=True)
    base = extension_base(api, manifest, config, edge, before, released=True)
    require(membership(api, edge["id"]) == before, "Addon reference remains, or unrelated membership changed.")
    return base


def immutable_json(path, value):
    path = private_path(path)
    if path.exists():
        require(checked_json(path) == value, "Never overwrite or adopt a different immutable receipt.")
    else:
        write_private(path, json.dumps(value, ensure_ascii=False, indent=2))
    return path


@contextmanager
def composition_lock(directory):
    if directory is None:
        yield
        return
    lock = Path(directory) / "workspace.lock"
    write_private(lock, canonical_json({"pid": os.getpid(), "at": utc(), "scope": "composition"}))
    try:
        yield
    finally:
        lock.unlink()


def require_composition_approval(args, org):
    require(args.approve_extension and args.confirm_organization == org,
            "Local acceptance/release requires --approve-extension and the exact --confirm-organization.")


def register_extension(journal, receipt, manifest):
    desired = {"status": "accepted", "receipt": str(receipt), "sha256": digest(manifest)}
    current = journal.state.get("extension")
    if current:
        require(current == desired, "A different or released addon is already registered; no silent replacement.")
    else:
        journal.save("extension_accepted", {"extension": desired}, {
            "edge": manifest["edge"], "base_checkpoint": manifest["base"]["checkpoint"],
            "supervision_checkpoint": manifest["supervision"]["checkpoint"],
        })


def composition_main(args):
    require(not args.apply, "Composition commands are read-only in Dataverse; --apply is never accepted.")
    require(not args.config and not args.output_dir,
            "Use --base-receipt or --legacy-baseline for composition, not a fabricated base configuration.")
    if args.command == "capture-legacy-baseline":
        require(all((args.supervision_receipt, args.profile_id, args.app_id, args.legacy_evidence,
                     args.selector_evidence, args.output)), "Legacy capture needs the documented explicit scope/evidence/output arguments.")
        source = checked_json(receipt_directory(args.supervision_receipt) / "plan.json")
        config = source["config"]
        api = Dataverse(organization(config["organization_url"]), guid(config["tenant_id"]))
        baseline = capture_legacy_baseline(api, args.supervision_receipt, guid(args.profile_id), guid(args.app_id),
                                           args.legacy_evidence, args.selector_evidence)
        if private_path(args.output).exists():
            baseline["captured_at"] = checked_json(args.output)["captured_at"]
        path = immutable_json(args.output, baseline)
        print(json.dumps({"legacy_baseline": str(path), "dataverse_writes": False, "portable_ownership_claimed": False,
                          "acceptance": COMPOSITION_LIMITS}))
        return 0
    if args.command == "accept-extension":
        require(args.supervision_receipt and args.output and bool(args.base_receipt) != bool(args.legacy_baseline),
                "Acceptance needs exactly one real portable base directory or labelled legacy baseline, plus supervision receipt/output.")
        destination = private_path(args.output)
        existing = checked_json(destination) if destination.exists() else None
        if args.base_receipt:
            require(not existing or existing["base"]["kind"] == "portable", "Existing acceptance has another base kind.")
            reference, base_plan, _ = portable_base(args.base_receipt, existing["base"]["checkpoint"] if existing else None)
            org, tenant = base_plan["config"]["org_url"], base_plan["config"]["tenant_id"]
            directory = reference["directory"]
        else:
            path = private_path(args.legacy_baseline)
            baseline = checked_json(path)
            require(baseline.get("kind") == LEGACY_KIND, "Not a labelled legacy baseline.")
            reference = {"kind": "legacy", "receipt": str(path), "sha256": digest(baseline)}
            org, tenant, directory = baseline["organization_url"], baseline["tenant_id"], None
        require_composition_approval(args, org)
        api = Dataverse(org, tenant)
        with composition_lock(directory):
            if destination.exists():
                manifest = checked_json(destination)
                require(manifest["base"] == reference and manifest["supervision"]["directory"] ==
                        str(receipt_directory(args.supervision_receipt)), "Existing acceptance references another base/addon.")
            else:
                sup, config, edge, before, _ = supervision_receipt(args.supervision_receipt, api)
                manifest = {
                    "kind": EXTENSION_KIND, "accepted_at": utc(), "organization_url": org, "tenant_id": tenant,
                    "base": reference, "supervision": sup, "edge": edge, "before": before, "acceptance": COMPOSITION_LIMITS,
                }
            base = verify_extension(api, manifest)
            if base:
                plan, journal = base
                require(not journal.state.get("extension") or journal.state["extension"] ==
                        {"status": "accepted", "receipt": str(destination), "sha256": digest(manifest)},
                        "Different/released extension already registered.")
                Operator(api, plan, journal, extension=manifest).verify(record=False)
            immutable_json(destination, manifest)
            if base:
                register_extension(journal, destination, manifest)
        print(json.dumps({"extension_acceptance": str(destination), "dataverse_writes": False, "edge": manifest["edge"],
                          "acceptance": COMPOSITION_LIMITS}))
        return 0
    require(args.extension_receipt, "Supply the exact private --extension-receipt.")
    acceptance_path = private_path(args.extension_receipt)
    manifest = checked_json(acceptance_path)
    api = Dataverse(organization(manifest["organization_url"]), guid(manifest["tenant_id"]))
    directory = manifest["base"].get("directory")
    if args.command == "verify-extension":
        if args.release_receipt:
            verify_extension_release(api, manifest, checked_json(args.release_receipt))
            status = "released"
        else:
            base = verify_extension(api, manifest)
            if base:
                plan, journal = base
                require(journal.state.get("extension") == {
                    "status": "accepted", "receipt": str(acceptance_path), "sha256": digest(manifest),
                }, "Portable addon was not registered through explicit acceptance.")
                Operator(api, plan, journal).verify(record=False)
            status = "accepted"
        print(json.dumps({"status": status, "dataverse_writes": False, "acceptance": COMPOSITION_LIMITS}))
        return 0
    require(args.command == "release-extension" and args.output, "Release needs a new private --output receipt.")
    require_composition_approval(args, manifest["organization_url"])
    destination = private_path(args.output)
    with composition_lock(directory):
        if destination.exists():
            release = checked_json(destination)
        else:
            sup, _, _, _, _ = supervision_receipt(manifest["supervision"]["directory"], api, released=True)
            release = {"kind": RELEASE_KIND, "released_at": utc(), "acceptance_sha256": digest(manifest), "supervision": sup}
        base = verify_extension_release(api, manifest, release)
        if base:
            _, journal = base
            current = journal.state.get("extension")
            require(current and current["receipt"] == str(acceptance_path) and current["sha256"] == digest(manifest),
                    "Release does not own this base's registered addon.")
        immutable_json(destination, release)
        if base:
            updated = {**current, "status": "released", "release_receipt": str(destination), "release_sha256": digest(release)}
            if current.get("status") == "released":
                require(current == updated, "Another release receipt is already registered.")
            else:
                journal.save("extension_released", {"extension": updated}, {"edge": manifest["edge"]})
    print(json.dumps({"extension_release": str(destination), "dataverse_writes": False, "command_history_deleted": False}))
    return 0


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("command", nargs="?", default="plan", choices=(
        "plan", "language", "representative", "workspace", "knowledge", "copilot", "bind", "verify", "recover",
        "rollback", "all", "accept-recipe-update",
        *sorted(EXTENSION_COMMANDS),
    ))
    result.add_argument("--config", help="Private base configuration; required for ordinary base stages.")
    result.add_argument("--output-dir", help="Private base receipt directory; required for ordinary base stages.")
    result.add_argument("--base-receipt", help="Real verified portable base directory, only for extension acceptance.")
    result.add_argument("--legacy-baseline", help="Explicitly labelled observed legacy baseline, never a portable plan.")
    result.add_argument("--supervision-receipt", help="Private native-supervision plan/journal directory.")
    result.add_argument("--extension-receipt", help="Exact approved addon receipt; must match the base registration.")
    result.add_argument("--release-receipt", help="Exact release receipt for a read-only release check.")
    result.add_argument("--previous-script", help="Owner-only exact original source backup, only for recipe acceptance.")
    result.add_argument("--approve-recipe-update", action="store_true", help="Explicit script-only version acceptance; never rewrites an existing plan.")
    result.add_argument("--output", help="New owner-only local baseline/acceptance/release JSON file.")
    result.add_argument("--legacy-evidence", help="Original private legacy deployment evidence; retained unchanged.")
    result.add_argument("--selector-evidence", help="Scoped configured-Monitor-selector proof; not browser acceptance.")
    result.add_argument("--profile-id", help="Explicit legacy profile for baseline capture.")
    result.add_argument("--app-id", help="Explicit legacy app for baseline capture.")
    result.add_argument("--approve-extension", action="store_true", help="Explicit local approval; never grants Dataverse mutations.")
    result.add_argument("--apply", action="store_true", help="Explicitly allow only the selected stage's documented mutations.")
    result.add_argument("--confirm-organization", help="Required with --apply; must exactly equal config org_url.")
    result.add_argument("--wait-seconds", type=int, default=900)
    result.add_argument("--poll-seconds", type=int, default=15)
    result.add_argument("--copilot-wait-minutes", type=int, default=60,
                        help="Wait for native Copilot knowledge readiness (1-240 minutes; it has taken about 39).")
    return result


def main(argv=None):
    args = parser().parse_args(argv)
    require(args.command == "accept-recipe-update" or not (args.previous_script or args.approve_recipe_update),
            "Recipe-update arguments require accept-recipe-update.")
    if args.command in EXTENSION_COMMANDS:
        return composition_main(args)
    require(args.config and args.output_dir, "Base stages require --config and --output-dir.")
    require(not any((args.base_receipt, args.legacy_baseline, args.supervision_receipt, args.output,
                     args.legacy_evidence, args.selector_evidence, args.profile_id, args.app_id,
                     args.approve_extension, args.release_receipt)), "Composition arguments require an explicit composition command.")
    config = validate_config(json.loads(read_private(args.config)))
    require(0 <= args.wait_seconds <= 3600 and 1 <= args.poll_seconds <= 60, "Wait must be 0–3600 seconds; polling must be 1–60 seconds.")
    require(1 <= args.copilot_wait_minutes <= 240, "--copilot-wait-minutes must be 1–240.")
    require(not args.apply or args.confirm_organization == config["org_url"], "--apply requires --confirm-organization matching the private configuration.")
    require(not args.apply or args.command not in ("plan", "verify", "recover", "accept-recipe-update"),
            "Plan, verify, recover and recipe acceptance are read-only; omit --apply.")
    output = private_path(args.output_dir)
    if not output.exists():
        output.mkdir(mode=0o700)
    require(output.is_dir() and (os.name == "nt" or output.stat().st_mode & 0o077 == 0),
            "Receipt directory must be owner-only (chmod 700).")
    lock = output / "workspace.lock"
    write_private(lock, canonical_json({"pid": os.getpid(), "at": utc()}))
    try:
        plan_path = output / "plan.json"
        if plan_path.exists():
            plan = json.loads(read_private(plan_path))
            require(plan["schema"] == SCHEMA and plan["config"] == config,
                    "Configuration/source changed since this plan. Do not mix receipts or adopt records into a replacement plan.")
        else:
            require(args.command == "plan", "Create and review a plan in this directory before selecting any stage.")
            api = Dataverse(config["org_url"], config["tenant_id"])
            plan = build_plan(api, config)
            write_private(plan_path, json.dumps(plan, ensure_ascii=False, indent=2))
        journal = Journal(output, plan)
        if args.command != "accept-recipe-update":
            require_recipe(plan, journal)
        if args.extension_receipt:
            registration = journal.state.get("extension")
            path = private_path(args.extension_receipt)
            require(registration and registration["receipt"] == str(path) and
                    registration["sha256"] == digest(checked_json(path)),
                    "Base verification requires the exact locally registered addon receipt.")
        if args.command == "plan":
            print(json.dumps({
                "plan": str(plan_path), "dataverse_writes": False, "gates": plan["gates"],
                "workspace_records": len(plan["workspace"]["records"]), "published_articles_planned": plan["knowledge"]["article_count"],
                "representative_setup_writes_planned": len((plan.get("representative") or {}).get("operations", [])),
                "acceptance": plan["acceptance"],
            }))
            return 0
        api = Dataverse(config["org_url"], config["tenant_id"])
        operator = Operator(api, plan, journal, wait_seconds=args.wait_seconds, poll_seconds=args.poll_seconds,
                            copilot_wait_seconds=args.copilot_wait_minutes * 60,
                            reconnect=lambda: Dataverse(config["org_url"], config["tenant_id"]))
        if args.command == "accept-recipe-update":
            accept_recipe_update(operator, args.previous_script, approved=args.approve_recipe_update,
                                 confirmed_organization=args.confirm_organization)
            print(json.dumps({"recipe_update_receipts": str(output), "dataverse_writes": False,
                              "original_plan_and_events_unchanged": True}))
            return 0
        if args.command == "rollback" and not args.apply:
            path = journal.save("rollback_plan", evidence=operator.rollback_inventory())
            print(json.dumps({"receipt": str(path), "dataverse_writes": False, "destructive_purge": False}))
            return 0
        if args.command not in ("verify", "recover") and not args.apply:
            path = journal.save("stage_dry_run", evidence={"stage": args.command, "requires_explicit_apply": True})
            print(json.dumps({"receipt": str(path), "stage": args.command, "dataverse_writes": False}))
            return 0
        if args.command != "recover":
            require(not journal.state["pending"], "A pending mutation needs recover before any further stage.")
        stages = ("language", "representative", "workspace", "knowledge", "copilot", "bind", "verify"
                  ) if args.command == "all" else (args.command,)
        methods = {
            "language": operator.provision_language, "representative": operator.provision_representative,
            "workspace": operator.provision_workspace,
            "knowledge": operator.provision_knowledge, "copilot": operator.provision_copilot,
            "bind": operator.bind, "verify": operator.verify, "recover": operator.recover, "rollback": operator.rollback,
        }
        readback = None
        try:
            for stage in stages:
                result = methods[stage]()
                if stage == "representative":
                    readback = result
        except Exception as error:
            journal.save("stage_stopped", evidence={"stage": stage, "error_type": type(error).__name__,
                                                  "reason": str(error) if isinstance(error, GateError) else "Inspect native status and the pending receipt."})
            raise
        summary = {"stage": args.command, "verified_receipts": str(output), "acceptance": plan["acceptance"]}
        if readback:
            # verify_representative raised unless every item below was read back.
            summary["representative_readback"] = {"roles": readback["roles"], "bookable_resource": True,
                                                   "capacity_profile_associated": True, "queue_member": True}
        print(json.dumps(summary))
        return 0
    finally:
        lock.unlink()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (GateError, ValueError, OSError) as exc:
        print("Workspace setup stopped: " + str(exc), file=sys.stderr)
        raise SystemExit(2)
