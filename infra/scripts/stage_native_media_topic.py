"""Stage unused Copilot media/control topics. Never publishes or changes call entry."""

import argparse
import copy
import hashlib
import json
import os
import re
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from uuid import UUID, uuid4
from xml.etree import ElementTree
from xml.sax.saxutils import quoteattr

import yaml


ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = ROOT / "apps/voice/dynamics/native-media-pilot.topic.yaml"
NOTICE_AUDIO = ROOT / "apps/voice/src/app/backend/native_startup_notice.wav"
NOTICE_SOURCE = ROOT / "apps/voice/dynamics/startup-notice.ssml"
TOPIC = "NativeMediaPilot"
CONTEXT_TOPIC = "NativeMediaContext"
CONTEXT = "msdyn_ConversationId"
CASE_VARIABLE = "CaseNumber"
CASE_CONTEXT_FIELDS = {
    "selected_case_id": "String", "selected_case_number": "String", "case_link_status": "String",
    "case_link_error": "String", "case_link_http_status": "Number", "case_link_stage": "String",
    "case_link_verified_at": "String", "case_link_already_linked": "Boolean",
}
CONTROLLER_SCHEMA = r"[A-Za-z0-9_]+\.topic\.Native(?:MediaPilot|SpeechTurnPilot)"


class CopilotDumper(yaml.SafeDumper):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Keep Power Fx scalars on one physical line for the native topic parser.
        self.best_width = 2**31 - 1

    def increase_indent(self, flow=False, indentless=False):
        return super().increase_indent(flow, False)


class CopilotLoader(yaml.SafeLoader):
    def construct_mapping(self, node, deep=False):
        mapping = super().construct_mapping(node, deep)
        for key, value in node.value:
            if (
                isinstance(key, yaml.ScalarNode) and key.value == "phoneNumber"
                and isinstance(value, yaml.ScalarNode)
            ):
                mapping["phoneNumber"] = value.value
        return mapping


def load_topic(source):
    document = yaml.load(source, Loader=CopilotLoader)
    if not isinstance(document, dict):
        raise ValueError("Expected a Copilot YAML object.")
    return document


def validate_initializer(initializer, *, existing_external_global=False):
    if type(existing_external_global) is not bool:
        raise ValueError("The existing external-global contract must be a boolean.")
    variables = {f"init:Global.{CONTEXT}"}
    if existing_external_global:
        variables.add(f"Global.{CONTEXT}")
    if (
        not isinstance(initializer, dict) or initializer.get("kind") != "SetVariable"
        or initializer.get("variable") not in variables
        or initializer.get("value") not in {'=""', '=Text("")'}
        or not isinstance(initializer.get("id"), str) or not initializer["id"]
    ):
        raise ValueError("The topic needs the saved external conversation-ID initializer.")


def render_context_topic(initializer, *, existing_external_global=False):
    validate_initializer(initializer, existing_external_global=existing_external_global)
    document = {
        "kind": "AdaptiveDialog",
        "beginDialog": {
            "kind": "OnRedirect", "id": "main", "actions": [copy.deepcopy(initializer)],
        },
        "inputType": {}, "outputType": {},
    }
    return yaml.dump(
        document, Dumper=CopilotDumper, sort_keys=False,
        allow_unicode=True, indent=2, line_break="\r\n",
    )


def render_topic(initializer, voice_origin, api_key, *, existing_external_global=False):
    validate_initializer(initializer, existing_external_global=existing_external_global)
    url = urllib.parse.urlsplit(voice_origin)
    if (
        url.scheme != "https" or not url.hostname or not url.hostname.endswith(".azurecontainerapps.io")
        or url.path or url.query or url.fragment or url.username or url.password or url.port
    ):
        raise ValueError("Use the HTTPS Azure Container Apps voice origin without a path.")
    if not isinstance(api_key, str) or len(api_key) < 32 or "\r" in api_key or "\n" in api_key:
        raise ValueError("A dedicated native media key of at least 32 characters is required.")
    document = load_topic(TEMPLATE.read_text())

    def replace(value):
        if isinstance(value, str):
            return value.replace("__VOICE_ORIGIN__", voice_origin).replace("__NATIVE_MEDIA_API_KEY__", api_key)
        if isinstance(value, list):
            return [replace(item) for item in value]
        if isinstance(value, dict):
            return {key: replace(item) for key, item in value.items()}
        return value

    return yaml.dump(
        replace(document), Dumper=CopilotDumper, sort_keys=False,
        allow_unicode=True, width=180, indent=2, line_break="\r\n",
    )


def render_test_entry(existing, pilot_schema, test_caller):
    if not isinstance(test_caller, str) or not re.fullmatch(r"\+[1-9]\d{6,14}", test_caller):
        raise ValueError("The test caller must be an explicit E.164 number.")
    if not isinstance(pilot_schema, str) or not re.fullmatch(CONTROLLER_SCHEMA, pilot_schema):
        raise ValueError("Select the exact NativeMediaPilot or NativeSpeechTurnPilot topic schema.")
    document = copy.deepcopy(existing)
    begin = document.get("beginDialog", {})
    actions = begin.get("actions")
    if (
        document.get("kind") != "AdaptiveDialog" or begin.get("kind") != "OnConversationStart"
        or not isinstance(actions, list) or len(actions) != 1
        or actions[0].get("kind") != "TransferConversationV2"
        or actions[0].get("transferType", {}).get("kind") != "TransferToPhoneNumber"
    ):
        raise ValueError("Expected the existing single external-transfer call entry; do not overwrite another flow.")
    phone = actions[0]["transferType"].get("phoneNumber")
    if not isinstance(phone, str) or not re.fullmatch(r"\+[1-9]\d{6,14}", phone):
        raise ValueError("Preserve phoneNumber as an E.164 string, including '+'. Load native YAML with load_topic.")
    begin["actions"] = [{
        "kind": "ConditionGroup",
        "id": "native_pilot_test_entry",
        "conditions": [{
            "id": "native_pilot_test_caller",
            "condition": f"=System.Activity.From.Name = {json.dumps(test_caller)}",
            "actions": [{
                "kind": "BeginDialog",
                "id": "native_pilot_begin",
                "dialog": pilot_schema,
            }],
        }],
        "elseActions": actions,
    }]
    return yaml.dump(
        document, Dumper=CopilotDumper, sort_keys=False,
        allow_unicode=True, width=180, indent=2, line_break="\r\n",
    )


def render_test_panel_entry(existing, pilot_schema, closed_conversation_id):
    identifier = str(UUID(closed_conversation_id))
    if not isinstance(pilot_schema, str) or not re.fullmatch(CONTROLLER_SCHEMA, pilot_schema):
        raise ValueError("Select the exact NativeMediaPilot or NativeSpeechTurnPilot topic schema.")
    document = copy.deepcopy(existing)
    begin = document.get("beginDialog", {})
    actions = begin.get("actions")
    if begin.get("kind") != "OnConversationStart" or not isinstance(actions, list) or not actions:
        raise ValueError("Expected the existing conversation-start entry.")
    if any(action.get("id") == "native_contract_test_gate" for action in actions):
        raise ValueError("The test-panel entry is already staged.")
    begin["actions"] = [{
        "kind": "ConditionGroup", "id": "native_contract_test_gate",
        "conditions": [{
            "id": "native_contract_test_mode",
            "condition": "=System.Conversation.InTestMode = true",
            "actions": [
                {
                    "kind": "SetVariable", "id": "native_contract_closed_context",
                    "variable": "Global.msdyn_ConversationId", "value": identifier,
                },
                {
                    "kind": "BeginDialog", "id": "native_contract_run_pilot",
                    "dialog": pilot_schema,
                },
                {
                    "kind": "SendActivity", "id": "native_contract_return",
                    "activity": {
                        "text": ["The test flow returned. This check does not place telephone calls."],
                        "speak": ["The test flow returned. This check does not place telephone calls."],
                    },
                },
                {"kind": "EndDialog", "id": "native_contract_end"},
            ],
        }],
        "elseActions": actions,
    }]
    return yaml.dump(
        document, Dumper=CopilotDumper, sort_keys=False,
        allow_unicode=True, width=180, indent=2, line_break="\r\n",
    )


def render_transcript_probe_entry(existing, probe_schema, owned_caller):
    if not isinstance(owned_caller, str) or not re.fullmatch(r"\+[1-9]\d{6,14}", owned_caller):
        raise ValueError("Provide the owned diagnostic caller in E.164 format.")
    if not re.fullmatch(r"[A-Za-z0-9_]+\.topic\.NativeTranscriptBoundaryProbe", probe_schema):
        raise ValueError("Select the exact transcript boundary probe topic.")
    document = copy.deepcopy(existing)
    begin = document.get("beginDialog", {})
    actions = begin.get("actions")
    if (
        begin.get("kind") != "OnConversationStart" or not isinstance(actions, list) or len(actions) != 1
        or actions[0].get("id") != "native_pilot_test_entry"
    ):
        raise ValueError("Expected the existing caller-scoped native pilot entry; refusing to replace another flow.")
    begin["actions"] = [{
        "kind": "ConditionGroup",
        "id": "native_transcript_probe_entry",
        "conditions": [{
            "id": "native_transcript_probe_owned_caller",
            "condition": f"=System.Activity.From.Name = {json.dumps(owned_caller)}",
            "actions": [{
                "kind": "BeginDialog", "id": "native_transcript_probe_begin",
                "dialog": probe_schema,
            }],
        }],
        "elseActions": actions,
    }]
    return yaml.dump(
        document, Dumper=CopilotDumper, sort_keys=False,
        allow_unicode=True, indent=2, line_break="\r\n",
    )


def startup_notice_message(voice_origin):
    digest = hashlib.sha256(NOTICE_AUDIO.read_bytes()).hexdigest()
    document = ElementTree.parse(NOTICE_SOURCE).getroot()
    text = " ".join(" ".join(document.itertext()).split())
    if not text:
        raise ValueError("The startup notice source must contain the approved recording statement.")
    audio_url = f"{voice_origin}/dynamics/native/startup-notice/{digest}.wav"
    return {"text": [text], "speak": [f"<audio src={quoteattr(audio_url)} />"]}


def startup_notice_latency_settings(voice_origin):
    """Historical receipt contract; audio-only latency messages were silent in native calls."""
    audio = startup_notice_message(voice_origin)["speak"][0]
    return {
        "kind": "LatencyMessageSettings",
        "allowLatencyMessage": True,
        "latencyMessage": {
            "kind": "Message", "text": [audio], "speak": [audio],
        },
        "latencyMessageDelayInMilliseconds": 0,
        "minimumPlaybackInMilliseconds": 0,
    }


def native_startup_latency_settings(voice_origin):
    return {
        "allowLatencyMessage": True,
        "latencyMessage": {"speak": startup_notice_message(voice_origin)["speak"]},
        "latencyMessageDelayInMilliseconds": 0,
        "minimumPlaybackInMilliseconds": 0,
    }


def startup_prepared_condition(owner):
    if owner not in {"native", "application"}:
        raise ValueError("Prepared startup requires one recognized announcement owner.")
    return (
        f'=!Topic.NativeResult.native_startup_prepared || Topic.NativeResult.startup_notice_owner <> "{owner}" || '
        '(Topic.NativeResult.control_language <> "ar" && Topic.NativeResult.control_language <> "en") || '
        'Topic.NativeResult.control_revision < 0'
    )


def startup_failure_branch(failure_actions):
    return {
        "kind": "ConditionGroup", "id": "native_supervision_check_startup_failure",
        "conditions": [{
            "id": "native_startup_failure_confirmed",
            "condition": (
                '=Lower(Topic.NativeStopResult.conversation_id) = Topic.NativeConversationId && '
                'Topic.NativeStopResult.supervision_contract = 1 && '
                'Topic.NativeStopResult.action = "stop" && Topic.NativeStopResult.phase = "stopped" && '
                'Topic.NativeStopResult.azure_leg_detached && Topic.NativeStopResult.startup_failed_unpaused && '
                'Topic.NativeStopResult.ai_state = "active" && !Topic.NativeStopResult.ai_pause_acknowledged'
            ),
            "actions": copy.deepcopy(failure_actions),
        }],
        "elseActions": [{
            "kind": "GotoAction", "id": "native_supervision_cleanup_hold", "actionId": "native_supervision_hold",
        }],
    }


def native_startup_controller(document, origin, *, failure_actions=None):
    document = copy.deepcopy(document)
    actions = document["beginDialog"]["actions"]

    def walk(value):
        if isinstance(value, dict):
            if "id" in value:
                yield value
            for child in value.values():
                yield from walk(child)
        elif isinstance(value, list):
            for child in value:
                yield from walk(child)

    nodes = {node["id"]: node for node in walk(document)}
    required = {"native_start", "native_connecting_notice", "native_check_start", "native_dispatch_result",
                "native_reset_wait_result", "native_listen_turn"}
    if not required.issubset(nodes):
        raise ValueError("Native announcement ownership requires the existing speech-turn controller.")
    if "native_supervision_hold" in nodes and "native_supervision_check_startup_failure" not in nodes:
        if failure_actions is None or [node["id"] for node in failure_actions] != [
            "native_report_failure", "native_connection_error", "native_end_failed_topic",
        ]:
            raise ValueError("The supervised startup repair needs the recorded original failure actions.")
        hold = nodes.get("native_supervision_cleanup_hold")
        if hold not in actions or hold != {
            "kind": "GotoAction", "id": "native_supervision_cleanup_hold", "actionId": "native_supervision_hold",
        }:
            raise ValueError("The legacy quiet-cleanup path changed; do not overwrite it.")
        actions[actions.index(hold)] = startup_failure_branch(failure_actions)
    fields = {
        "native_startup_prepared": ("Boolean", "false"),
        "startup_notice_owner": ("String", 'Text("")'),
        "startup_failed_unpaused": ("Boolean", "false"),
    }
    for node in walk(document):
        if node.get("kind") == "HttpRequestAction":
            properties = node["responseSchema"]["properties"]
            for name, (kind, _) in fields.items():
                if name in properties and properties[name] != kind:
                    raise ValueError("The native startup response schema changed.")
                properties[name] = kind
        if node.get("kind") == "SetVariable" and node.get("variable") in {"Topic.NativeResult", "Topic.NativeStopResult"}:
            value = node.get("value")
            if not isinstance(value, str) or not value.endswith("}"):
                raise ValueError("The native startup response initializer is not recognized.")
            for name, (_, default) in fields.items():
                matches = re.findall(r"\b" + name + r"\s*:", value)
                if matches and (len(matches) != 1 or not re.search(r"\b" + name + r"\s*:\s*" + re.escape(default), value)):
                    raise ValueError("Native startup initializer must retain a single fail-closed default.")
                if not matches:
                    value = value[:-1] + ", " + name + ": " + default + "}"
            node["value"] = value
    start = nodes["native_start"]
    endpoint = origin + "/dynamics/native/sessions"
    if start.get("url") not in (endpoint, endpoint + "?wait_for_context=true", endpoint + "?wait_for_startup=true"):
        raise ValueError("The native startup endpoint changed.")
    body = start["body"]["content"]
    if body.count("speech_turn_control: true") != 1:
        raise ValueError("Native startup requires the existing speech-turn contract.")
    body = re.sub(r", play_startup_notice: true\b", "", body)
    if "play_startup_notice" in body:
        raise ValueError("The application notice must be absent in native-only startup.")
    if "native_startup_notice" not in body:
        body = body.replace("speech_turn_control: true", "speech_turn_control: true, native_startup_notice: true")
    elif not re.search(r"\bnative_startup_notice: true\b", body):
        raise ValueError("The native announcement option is not recognized.")
    start["body"]["content"] = body
    start["url"] = endpoint + "?wait_for_startup=true"
    start["latencyMessageSettings"] = native_startup_latency_settings(origin)
    nodes["native_connecting_notice"]["activity"] = {"speak": ['<break time="20ms" />']}
    extra_conditions = [
        {
            "id": "native_start_terminal",
            "condition": '=Topic.NativeResult.action <> "wait"',
            "actions": [{"kind": "GotoAction", "id": "native_start_to_dispatch", "actionId": "native_dispatch_result"}],
        },
        {
            "id": "native_start_not_prepared",
            "condition": startup_prepared_condition("native"),
            "actions": [{"kind": "GotoAction", "id": "native_start_unprepared_cleanup", "actionId": "native_cleanup"}],
        },
    ]
    conditions = nodes["native_check_start"]["conditions"]
    for condition in extra_conditions:
        present = [node for node in conditions if node.get("id") == condition["id"]]
        if present and present != [condition]:
            raise ValueError("The native prepared-start guard changed.")
        if not present:
            conditions.append(condition)
    initialization = [
        {"kind": "SetVariable", "id": "native_initial_requested_language",
         "variable": "Topic.NativeListenLanguage", "value": "=Topic.NativeResult.control_language"},
        {"kind": "SetVariable", "id": "native_initial_requested_revision",
         "variable": "Topic.NativeLanguageRevision", "value": "=Topic.NativeResult.control_revision"},
    ]
    present = [node for node in actions if node.get("id") in {item["id"] for item in initialization}]
    if present and present != initialization:
        raise ValueError("The initial native language acknowledgment contract changed.")
    if not present:
        index = actions.index(nodes["native_reset_wait_result"])
        actions[index:index] = initialization
    return document


def application_startup_controller(document, origin):
    document = copy.deepcopy(document)
    actions = document.get("beginDialog", {}).get("actions", [])
    nodes = {node.get("id"): node for node in actions}
    required = {"native_start", "native_connecting_notice", "native_check_start"}
    if not required.issubset(nodes) or any(
        sum(node.get("id") == identifier for node in actions) != 1 for identifier in required
    ):
        raise ValueError("Application startup requires the exact prepared-start actions.")
    start = nodes["native_start"]
    body = start.get("body", {}).get("content", "")
    disabled = {"kind": "LatencyMessageSettings", "allowLatencyMessage": False}
    guards = [condition for condition in nodes["native_check_start"].get("conditions", [])
              if condition.get("id") == "native_start_not_prepared"]
    if (
        start.get("kind") != "HttpRequestAction"
        or start.get("url") != origin + "/dynamics/native/sessions?wait_for_startup=true"
        or nodes["native_connecting_notice"].get("activity") != {"speak": ['<break time="20ms" />']}
        or body.count("speech_turn_control: true") != 1 or len(guards) != 1
    ):
        raise ValueError("Application startup must preserve the prepared endpoint, silent handshake and speech control.")
    guard = guards[0]
    if "native_startup_notice" not in body and body.count("play_startup_notice: true") == 1:
        if start.get("latencyMessageSettings") != disabled or guard.get("condition") != startup_prepared_condition("application"):
            raise ValueError("The application-only startup settings drifted.")
        return document
    if (
        body.count("native_startup_notice: true") != 1 or "play_startup_notice" in body
        or start.get("latencyMessageSettings") != native_startup_latency_settings(origin)
        or guard.get("condition") != startup_prepared_condition("native")
    ):
        raise ValueError("Replace only the reviewed native-only prepared-start announcement.")
    start["body"]["content"] = body.replace("native_startup_notice: true", "play_startup_notice: true")
    start["latencyMessageSettings"] = disabled
    guard["condition"] = startup_prepared_condition("application")
    return document


def case_number_expression(record):
    if record not in {"NativeResult", "NativeStopResult"}:
        raise ValueError("Use the authoritative normal or recovered native response.")
    number = f"Topic.{record}.selected_case_number"
    identifier = f"Topic.{record}.selected_case_id"
    uuid = "[0-9A-Fa-f]{8}(-[0-9A-Fa-f]{4}){3}-[0-9A-Fa-f]{12}"
    return (
        f'=If(IsMatch({identifier}, "{uuid}") && {identifier} <> "00000000-0000-0000-0000-000000000000" && '
        f'IsMatch({number}, "[A-Za-z0-9][A-Za-z0-9\\-]{{2,79}}") && '
        f'!StartsWith(Upper({number}), "OP-") && '
        f'!IsMatch({number}, "[0-9A-Fa-f]{{32}}|{uuid}"), {number}, Text(""))'
    )


def add_case_handoff(document):
    actions = document["beginDialog"]["actions"]

    def walk(value):
        if isinstance(value, dict):
            if isinstance(value.get("id"), str):
                yield value
            for child in value.values():
                yield from walk(child)
        elif isinstance(value, list):
            for child in value:
                yield from walk(child)

    nodes = {node["id"]: node for node in walk(document)}
    if "native_set_case_number" in nodes:
        raise ValueError("The controller already includes CRM case context.")
    for name in ("native_start", "native_wait", "native_cleanup"):
        nodes[name]["responseSchema"]["properties"].update(CASE_CONTEXT_FIELDS)
    defaults = {
        name: 'Text("")' if kind == "String" else "0" if kind == "Number" else "false"
        for name, kind in CASE_CONTEXT_FIELDS.items()
    }
    for node in nodes.values():
        if node.get("kind") == "SetVariable" and node.get("variable") in {"Topic.NativeResult", "Topic.NativeStopResult"}:
            if not isinstance(node.get("value"), str) or not node["value"].endswith("}"):
                raise ValueError("Expected typed native response reset records.")
            node["value"] = node["value"][:-1] + ", " + ", ".join(f"{name}: {value}" for name, value in defaults.items()) + "}"
    for condition, record, identifier in (
        ("native_handover_ready", "NativeResult", "native_case_from_status"),
        ("native_completed_handover_recovered", "NativeStopResult", "native_case_from_stop"),
    ):
        nodes[condition]["actions"].insert(0, {
            "kind": "SetVariable", "id": identifier,
            "variable": "Topic.NativeHandoverCaseNumber", "value": case_number_expression(record),
        })
    actions[1:1] = [
        {
            "kind": "SetVariable", "id": "native_initialize_case_number",
            "variable": f"init:Global.{CASE_VARIABLE}", "value": '=Text("")',
        },
        {
            "kind": "SetVariable", "id": "native_initialize_handover_case",
            "variable": "init:Topic.NativeHandoverCaseNumber", "value": '=Text("")',
        },
    ]
    index = next(index for index, node in enumerate(actions) if node["id"] == "native_transfer_to_agent")
    actions.insert(index, {
        "kind": "SetVariable", "id": "native_set_case_number",
        "variable": f"Global.{CASE_VARIABLE}", "value": "=Topic.NativeHandoverCaseNumber",
    })


def validate_case_handoff(document):
    actions = document.get("beginDialog", {}).get("actions", [])
    top = {node.get("id"): node for node in actions}
    expected = load_topic(render_speech_turn_pilot(load_topic(TEMPLATE.read_text()), crm_case_context=True))
    expected_top = {node["id"]: node for node in expected["beginDialog"]["actions"]}
    for identifier in ("native_initialize_case_number", "native_initialize_handover_case", "native_set_case_number"):
        if top.get(identifier) != expected_top[identifier]:
            raise ValueError("The native CaseNumber declaration or assignment is invalid.")
    ids = [node.get("id") for node in actions]
    if (
        ids.index("native_set_case_number") + 1 != ids.index("native_transfer_to_agent")
        or ids.index("native_initialize_case_number") >= ids.index("native_start")
        or top["native_transfer_to_agent"].get("transferType", {}).get("kind") != "TransferToAgent"
    ):
        raise ValueError("Set CaseNumber immediately before native agent transfer.")
    for parent, condition in (
        ("native_dispatch_result", "native_handover_ready"),
        ("native_check_cleanup", "native_completed_handover_recovered"),
    ):
        actual = next((node for node in top.get(parent, {}).get("conditions", []) if node.get("id") == condition), None)
        wanted = next(node for node in expected_top[parent]["conditions"] if node["id"] == condition)
        if actual != wanted:
            raise ValueError("Both native handover paths must capture their own authoritative case response.")
    for name in ("native_start", "native_wait", "native_cleanup"):
        properties = top.get(name, {}).get("responseSchema", {}).get("properties", {})
        if any(properties.get(field) != kind for field, kind in CASE_CONTEXT_FIELDS.items()):
            raise ValueError("Every native response needs the typed CRM case context fields.")


def add_supervision(document):
    """Add the versioned, silent supervised-call path without changing handover or case actions."""
    document = copy.deepcopy(document)
    actions = document["beginDialog"]["actions"]

    def walk(value):
        if isinstance(value, dict):
            if "id" in value:
                yield value
            for child in value.values():
                yield from walk(child)
        elif isinstance(value, list):
            for child in value:
                yield from walk(child)

    nodes = {node["id"]: node for node in walk(document)}
    if "native_supervision_hold" in nodes:
        raise ValueError("Supervision is already rendered; verify its exact reviewed snapshot instead of layering changes.")
    if not {"native_listen_turn", "native_wait", "native_dispatch_result", "native_check_cleanup"}.issubset(nodes):
        raise ValueError("Supervision requires the native speech-turn controller.")
    start = nodes["native_start"]["body"]["content"]
    if "supervision_contract" in start or start.count("speech_turn_control: true") != 1:
        raise ValueError("The controller start contract is not recognized.")
    nodes["native_start"]["body"]["content"] = start.replace(
        "speech_turn_control: true", "speech_turn_control: true, supervision_contract: 1",
    )
    fields = {
        "supervision_contract": "Number", "ai_state": "String",
        "ai_pause_acknowledged": "Boolean", "startup_failed_unpaused": "Boolean",
    }
    for node in nodes.values():
        if node.get("kind") == "HttpRequestAction":
            node["responseSchema"]["properties"].update(fields)
        if node.get("kind") == "SetVariable" and node.get("variable") in {"Topic.NativeResult", "Topic.NativeStopResult"}:
            extra = ', supervision_contract: 0, ai_state: Text(""), ai_pause_acknowledged: false'
            if not re.search(r"\bstartup_failed_unpaused\s*:", node["value"]):
                extra += ', startup_failed_unpaused: false'
            node["value"] = node["value"][:-1] + extra + "}"
    def goto(identifier, target):
        return {"kind": "GotoAction", "id": identifier, "actionId": target}

    nodes["native_dispatch_result"]["conditions"][:0] = [
        {
            "id": "native_supervision_ended",
            "condition": '=Lower(Topic.NativeResult.conversation_id) = Topic.NativeConversationId && Topic.NativeResult.supervision_contract = 1 && Topic.NativeResult.action = "quiet_end"',
            "actions": [goto("native_supervision_to_end", "native_supervision_quiet_end")],
        },
        {
            "id": "native_supervision_paused",
            "condition": '=Lower(Topic.NativeResult.conversation_id) = Topic.NativeConversationId && Topic.NativeResult.supervision_contract = 1 && Topic.NativeResult.action = "supervised"',
            "actions": [goto("native_supervision_to_listen", "native_supervision_listen")],
        },
        {
            "id": "native_supervision_unconfirmed",
            "condition": '=Lower(Topic.NativeResult.conversation_id) = Topic.NativeConversationId && Topic.NativeResult.supervision_contract = 1 && Topic.NativeResult.action = "quiet_hold"',
            "actions": [goto("native_supervision_to_hold", "native_supervision_hold")],
        },
    ]
    # Unknown outcomes stay quiet; only a verified failure before any active AI or intervention may speak.
    failure_index = next(i for i, node in enumerate(actions) if node["id"] == "native_report_failure")
    if [node["id"] for node in actions[failure_index:]] != [
        "native_report_failure", "native_connection_error", "native_end_failed_topic",
    ]:
        raise ValueError("Unrecognized controller tail; preserve and reconcile its edits before supervision.")
    failure_actions = copy.deepcopy(actions[failure_index:])
    actions[failure_index:] = [startup_failure_branch(failure_actions)]
    quiet_listen = copy.deepcopy(nodes["native_listen_turn"])
    quiet_listen.update({"id": "native_supervision_listen", "variable": "init:Topic.NativeSupervisedSpeech"})
    poll = copy.deepcopy(nodes["native_wait"])
    if poll.get("method") != "Post" or not poll.get("url", "").endswith(', "/turn")'):
        raise ValueError("The controller turn endpoint is not recognized.")
    poll.update({
        "id": "native_supervision_poll", "method": "Get",
        "url": poll["url"].replace(', "/turn")', ")"),
        "latencyMessageSettings": {"kind": "LatencyMessageSettings", "allowLatencyMessage": False},
    })
    poll.pop("body", None)
    reset = copy.deepcopy(nodes["native_reset_loop_result"])
    reset["id"] = "native_supervision_reset_result"
    hold = copy.deepcopy(quiet_listen)
    hold.update({"id": "native_supervision_hold", "variable": "init:Topic.NativeSupervisionHeldSpeech"})
    actions.extend([
        quiet_listen, reset, poll,
        {
            "kind": "ConditionGroup", "id": "native_supervision_poll_result",
            "conditions": [
                {
                    "id": "native_supervision_poll_ended",
                    "condition": '=Lower(Topic.NativeResult.conversation_id) = Topic.NativeConversationId && Topic.NativeResult.supervision_contract = 1 && Topic.NativeResult.action = "quiet_end"',
                    "actions": [goto("native_supervision_poll_to_end", "native_supervision_quiet_end")],
                },
                {
                    "id": "native_supervision_poll_active",
                    "condition": '=Lower(Topic.NativeResult.conversation_id) = Topic.NativeConversationId && Topic.NativeResult.supervision_contract = 1 && Topic.NativeResult.action = "supervised"',
                    "actions": [goto("native_supervision_poll_again", "native_supervision_listen")],
                },
            ],
            "elseActions": [goto("native_supervision_poll_uncertain", "native_supervision_hold")],
        },
        hold, goto("native_supervision_hold_again", "native_supervision_hold"),
        {"kind": "EndDialog", "id": "native_supervision_quiet_end"},
    ])
    return document


def render_speech_turn_pilot(existing, *, startup_notice=False, crm_case_context=False, supervision=False):
    if not isinstance(startup_notice, bool):
        raise ValueError("The startup notice option must be a boolean.")
    if not isinstance(crm_case_context, bool):
        raise ValueError("The CRM case-context option must be a boolean.")
    if type(supervision) is not bool:
        raise ValueError("The supervision option must be a boolean.")
    document = copy.deepcopy(existing)
    begin = document.get("beginDialog", {})
    actions = begin.get("actions")
    if begin.get("kind") != "OnRedirect" or not isinstance(actions, list):
        raise ValueError("Expected a retained native-media controller topic.")

    def walk(value):
        if isinstance(value, dict):
            if isinstance(value.get("id"), str):
                yield value
            for child in value.values():
                yield from walk(child)
        elif isinstance(value, list):
            for child in value:
                yield from walk(child)

    nodes = {node["id"]: node for node in walk(document)}
    required = {"native_wait", "native_keep_waiting", "native_silence_controller", "native_connecting_notice"}
    if not required.issubset(nodes) or {"native_listen_turn", "native_select_listening_language"} & nodes.keys():
        raise ValueError("The source is not the original retained controller; do not layer another listening loop.")
    wait = nodes["native_wait"]
    if wait.get("kind") != "HttpRequestAction" or wait.get("url", "").count("?wait=true&transcript_after=") != 1:
        raise ValueError("Expected the existing authenticated native transcript status request.")
    condition = nodes["native_keep_waiting"].get("condition", "")
    condition, replacements = re.subn(r"\bTopic\.NativePollCount\s*<\s*10\b", "Topic.NativePollCount < 3000", condition)
    if replacements != 1:
        raise ValueError("Expected the original bounded native polling condition.")
    nodes["native_keep_waiting"]["condition"] = condition
    start_body = nodes["native_start"]["body"]["content"]
    if start_body.count("retain_native_controller: true") != 1 or "speech_turn_control" in start_body:
        raise ValueError("Expected the original native session start body.")
    nodes["native_start"]["body"]["content"] = start_body.replace(
        "retain_native_controller: true",
        "retain_native_controller: true, speech_turn_control: true",
    )
    origin_match = re.search(r'"([^"]+)/dynamics/native/sessions/', wait["url"])
    if origin_match is None:
        raise ValueError("The native status URL does not contain the expected voice origin.")
    origin = origin_match.group(1)
    wait["method"] = "Post"
    wait["url"] = f'=Concatenate("{origin}/dynamics/native/sessions/", Topic.NativeConversationId, "/turn")'
    wait["body"] = {
        "kind": "RawRequestContent", "contentType": "application/json",
        "content": "=JSON({language: Topic.NativeListenLanguage, revision: Topic.NativeLanguageRevision})",
    }

    for node in nodes.values():
        if node.get("kind") == "SetVariable" and node.get("variable") == "System.Conversation.OnlyAllowDTMF":
            node["value"] = False
        if node.get("kind") == "GotoAction" and node.get("actionId") == "native_wait":
            node["actionId"] = "native_select_listening_language"
        if node.get("kind") == "HttpRequestAction":
            node["responseSchema"]["properties"].update({
                "control_language": "String", "control_revision": "Number", "control_ready": "Boolean",
            })
        if node.get("kind") == "SetVariable" and node.get("variable") in {"Topic.NativeResult", "Topic.NativeStopResult"}:
            if not isinstance(node["value"], str) or not node["value"].endswith("}"):
                raise ValueError("Expected the typed native response record initializer.")
            node["value"] = node["value"][:-1] + ', control_language: Text(""), control_revision: 0, control_ready: false}'

    actions.remove(nodes["native_reset_transcript_cursor"])
    nodes["native_dispatch_result"]["conditions"].remove(nodes["native_transcript_received"])
    nodes["native_count_poll"]["value"] = "=Topic.NativePollCount + 1"
    nodes["native_result_invalid"]["condition"] = (
        '=Lower(Topic.NativeResult.conversation_id) <> Topic.NativeConversationId || '
        '(Topic.NativeResult.action = "wait" && '
        '((Topic.NativeResult.control_language <> "ar" && Topic.NativeResult.control_language <> "en") || '
        'Topic.NativeResult.control_revision < Topic.NativeLanguageRevision))'
    )
    nodes["native_keep_waiting"]["actions"][:0] = [
        {
            "kind": "SetVariable", "id": "native_capture_requested_language",
            "variable": "Topic.NativeListenLanguage", "value": "=Topic.NativeResult.control_language",
        },
        {
            "kind": "SetVariable", "id": "native_capture_requested_revision",
            "variable": "Topic.NativeLanguageRevision", "value": "=Topic.NativeResult.control_revision",
        },
    ]
    initialization = [
        {
            "kind": "SetVariable", "id": "native_initialize_listening_language",
            "variable": "Topic.NativeListenLanguage", "value": "ar",
        },
        {
            "kind": "SetVariable", "id": "native_initialize_language_revision",
            "variable": "Topic.NativeLanguageRevision", "value": 0,
        },
    ]
    index = next(index for index, node in enumerate(actions) if node["id"] == "native_connecting_notice")
    actions[index:index] = initialization
    locale = {
        "kind": "ConditionGroup", "id": "native_select_listening_language",
        "conditions": [
            {
                "id": f"native_language_is_{code}", "condition": f'=Topic.NativeListenLanguage = "{code}"',
                "actions": [{
                    "kind": "SetVariable", "id": f"native_listen_in_{code}",
                    "variable": "System.User.Language",
                    "value": {
                        "kind": "OptionDataValue",
                        "type": {"kind": "SystemOptionSet", "name": "Locale"},
                        "value": label,
                    },
                }],
            }
            for code, label in (("ar", "Arabic"), ("en", "English"))
        ],
        "elseActions": [{
            "kind": "GotoAction", "id": "native_invalid_language_cleanup", "actionId": "native_cleanup",
        }],
    }
    listen = {
        "kind": "Question", "id": "native_listen_turn",
        "interruptionPolicy": {"allowInterruption": False},
        "alwaysPrompt": True, "variable": "init:Topic.NativeObservedSpeech",
        "prompt": {"speak": ['<break time="20ms" />']},
        "entity": "StringPrebuiltEntity",
        "voiceInputSettings": {
            "silenceDetectionTimeoutInMilliseconds": 500,
            "repeatCountOnSilence": 0,
            "defaultValueMissingAction": "SetEmpty",
            "defaultValueOnSilence": "=Blank()",
            "utteranceEndTimeoutInMilliseconds": 300,
            "speechRecognitionTimeoutInMilliseconds": 1000,
        },
    }
    index = next(index for index, node in enumerate(actions) if node["id"] == "native_wait")
    actions[index:index] = [locale, listen]
    if crm_case_context:
        add_case_handoff(document)
    if supervision:
        document = add_supervision(document)
    if startup_notice:
        document = native_startup_controller(document, origin)
        document = application_startup_controller(document, origin)
    return yaml.dump(
        document, Dumper=CopilotDumper, sort_keys=False, allow_unicode=True,
        indent=2, line_break="\r\n",
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--org-url", required=True)
    parser.add_argument("--tenant-id", required=True, type=UUID)
    parser.add_argument("--bot-id", required=True, type=UUID)
    parser.add_argument("--voice-origin", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--backup-file", type=Path)
    args = parser.parse_args()
    if not re.fullmatch(r"https://[a-z0-9-]+\.crm\d*\.dynamics\.com", args.org_url):
        raise ValueError("Use the Dataverse organization HTTPS URL without a path.")
    key = os.environ.get("DYNAMICS_NATIVE_MEDIA_API_KEY", "")
    auth = subprocess.run([
        "az", "account", "get-access-token", "--tenant", str(args.tenant_id),
        "--resource", args.org_url, "--output", "json",
    ], check=True, capture_output=True, text=True, timeout=40)
    token = json.loads(auth.stdout)["accessToken"]
    headers = {"Authorization": "Bearer " + token, "Accept": "application/json"}

    def read(path):
        with urllib.request.urlopen(
            urllib.request.Request(args.org_url + "/api/data/v9.2/" + path, headers=headers), timeout=30,
        ) as response:
            return json.load(response)

    query = urllib.parse.urlencode({
        "$select": "botcomponentid,name,componenttype,data,schemaname,language",
        "$filter": (
            f"_parentbotid_value eq {args.bot_id} and "
            f"(name eq '{TOPIC}' or name eq 'Conversation Start' or name eq '{CONTEXT}' or name eq '{CONTEXT_TOPIC}')"
        ),
        "$top": "5",
    })
    records = read("botcomponents?" + query)
    if "@odata.nextLink" in records or len(records["value"]) not in {3, 4}:
        raise ValueError("Expected the saved pilot, context variable, entry and optional unused context topic.")
    by_name = {row["name"]: row for row in records["value"]}
    if set(by_name) not in (
        {TOPIC, CONTEXT, "Conversation Start"},
        {TOPIC, CONTEXT, "Conversation Start", CONTEXT_TOPIC},
    ):
        raise ValueError("The saved pilot topic or context variable is ambiguous.")
    variable = load_topic(by_name[CONTEXT]["data"])
    if (
        variable.get("kind") != "Variable" or variable.get("scope") != "Conversation"
        or variable.get("isExternalInitializationAllowed") is not True
        or variable.get("isOutputToExternalCallers") is not False
    ):
        raise ValueError("Use a conversation-scoped input-only global variable; do not carry it between sessions.")
    topic = by_name[TOPIC]
    existing = load_topic(topic["data"])
    if not isinstance(existing, dict) or not isinstance(existing.get("beginDialog"), dict):
        raise ValueError("The saved pilot topic is not an adaptive dialog.")
    actions = existing["beginDialog"].get("actions")
    if not isinstance(actions, list) or not actions:
        raise ValueError("The saved pilot topic has no conversation-ID initialization node.")
    context_topic = by_name.get(CONTEXT_TOPIC)
    initializer = actions[0] if context_topic is None else load_topic(context_topic["data"])["beginDialog"]["actions"][0]
    context_yaml = render_context_topic(initializer)
    if context_topic is not None and load_topic(context_topic["data"]) != load_topic(context_yaml):
        raise ValueError("The unused context topic has unexpected edits; refusing to overwrite it.")
    rendered = render_topic(initializer, args.voice_origin, key)
    same_definition = existing == load_topic(rendered)
    if same_definition and topic["data"] == rendered and context_topic is not None:
        print(json.dumps({"status": "already_staged", "topic_id": topic["botcomponentid"], "published": False}))
        return
    if not same_definition and (
        topic["componenttype"] != 9 or len(actions) != 1
        or existing["beginDialog"].get("kind") not in {"OnRecognizedIntent", "OnRedirect"}
        or existing["beginDialog"].get("intent", {})
    ):
        raise ValueError("The pilot draft has unexpected edits. Refusing to overwrite it.")
    if not args.apply:
        print(json.dumps({
            "status": "ready_to_stage", "topic_id": topic["botcomponentid"],
            "external_context_verified": True, "context_topic_create": context_topic is None,
            "published": False, "entry_topic_changed": False,
        }, indent=2))
        return
    if args.backup_file is None:
        raise ValueError("--backup-file outside the repository is required before applying.")
    backup = args.backup_file.resolve()
    if backup.is_relative_to(ROOT):
        raise ValueError("Save the private draft backup outside the repository.")
    descriptor = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as output:
        json.dump(records["value"], output, indent=2)
        output.write("\n")
    if context_topic is None:
        context_id = str(uuid4())
        context_record = {
            "botcomponentid": context_id, "name": CONTEXT_TOPIC, "componenttype": 9,
            "schemaname": topic["schemaname"].rsplit(".", 1)[0] + "." + CONTEXT_TOPIC,
            "parentbotid@odata.bind": f"/bots({args.bot_id})",
            "data": context_yaml,
        }
        if topic.get("language") is not None:
            context_record["language"] = topic["language"]
        create = urllib.request.Request(
            args.org_url + "/api/data/v9.2/botcomponents",
            data=json.dumps(context_record).encode(), method="POST",
            headers={**headers, "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(create, timeout=30) as response:
            if response.status not in {200, 201, 204}:
                raise RuntimeError("The unused context topic could not be created.")
        context_topic = read(f"botcomponents({context_id})?$select=data")
        if load_topic(context_topic["data"]) != load_topic(context_yaml):
            raise RuntimeError("The saved context definition differs; stop before publication.")
    request = urllib.request.Request(
        args.org_url + f"/api/data/v9.2/botcomponents({topic['botcomponentid']})",
        data=json.dumps({"data": rendered}).encode(), method="PATCH",
        headers={**headers, "Content-Type": "application/json", "If-Match": topic["@odata.etag"]},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        if response.status not in {200, 204}:
            raise RuntimeError("The draft update did not complete.")
    verified = read(f"botcomponents({topic['botcomponentid']})?$select=data")
    if load_topic(verified["data"]) != load_topic(rendered):
        raise RuntimeError("The saved draft differs from the expected controller topic.")
    entry = by_name["Conversation Start"]
    entry_after = read(f"botcomponents({entry['botcomponentid']})?$select=data")
    if entry_after["data"] != entry["data"]:
        raise RuntimeError("Conversation Start changed during staging. Stop and review before publishing.")
    print(json.dumps({
        "status": "draft_staged", "topic_id": topic["botcomponentid"],
        "trigger": "OnRedirect", "published": False, "entry_topic_changed": False,
        "context_definition_topic": CONTEXT_TOPIC,
        "native_validation": "Refresh and check the topic in Copilot Studio before publication.",
    }, indent=2))


if __name__ == "__main__":
    try:
        main()
    except urllib.error.HTTPError as error:
        print(json.dumps({"status": "failed", "error": f"Dataverse returned HTTP {error.code}; no request values were printed."}))
        raise SystemExit(1)
    except (ValueError, RuntimeError, yaml.YAMLError, subprocess.SubprocessError, urllib.error.URLError, OSError) as error:
        print(json.dumps({"status": "failed", "error_type": type(error).__name__, "error": str(error) if isinstance(error, (ValueError, RuntimeError)) else "The staging operation failed; no credentials were printed."}))
        raise SystemExit(1)
