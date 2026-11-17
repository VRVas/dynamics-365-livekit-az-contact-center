import importlib.util
import copy
import hashlib
import unittest
from pathlib import Path
from xml.etree import ElementTree

import yaml


ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("stage_native_media_topic", ROOT / "infra/scripts/stage_native_media_topic.py")
stage = importlib.util.module_from_spec(spec)
spec.loader.exec_module(stage)

KEY = "synthetic-native-controller-key-for-tests"
ORIGIN = "https://voice.test.azurecontainerapps.io"
INITIALIZER = {
    "kind": "SetVariable", "id": "saved_variable_node",
    "variable": "init:Global.msdyn_ConversationId", "value": '=""',
}


def all_actions(value):
    if isinstance(value, dict):
        if "kind" in value and "id" in value:
            yield value
        for child in value.values():
            yield from all_actions(child)
    elif isinstance(value, list):
        for child in value:
            yield from all_actions(child)


class NativeTopicTests(unittest.TestCase):
    def setUp(self):
        self.rendered = stage.render_topic(INITIALIZER, ORIGIN, KEY)
        self.document = yaml.safe_load(self.rendered)
        self.actions = list(all_actions(self.document))

    def test_context_initializer_is_defined_only_in_an_unused_topic(self):
        self.assertEqual(self.document["beginDialog"]["kind"], "OnRedirect")
        context = yaml.safe_load(stage.render_context_topic(INITIALIZER))
        self.assertEqual(context["beginDialog"]["kind"], "OnRedirect")
        self.assertEqual(context["beginDialog"]["actions"], [INITIALIZER])
        self.assertEqual(self.document["beginDialog"]["actions"][0]["id"], "native_capture_context")
        self.assertNotIn("init:Global.msdyn_ConversationId", self.rendered)
        self.assertNotIn("NativeMediaContext", self.rendered)
        self.assertNotIn("OnConversationStart", self.rendered)
        self.assertNotIn("triggerQueries", self.rendered)

    def test_output_matches_native_indented_action_lists(self):
        self.assertIn("\r\n  actions:\r\n    - kind: SetVariable", self.rendered)
        self.assertIn("\r\n      conditions:\r\n        - id:", self.rendered)
        self.assertNotIn("\r\n  - kind:", self.rendered)
        self.assertEqual(self.document["beginDialog"]["actions"][0]["variable"], "Topic.NativeConversationId")

    def test_long_power_fx_expressions_are_never_folded_by_the_yaml_writer(self):
        rendered = stage.render_topic(
            INITIALIZER,
            "https://ca-voice.test-environment-01234567.swedencentral.azurecontainerapps.io",
            KEY,
        )

        def check(node):
            if isinstance(node, yaml.ScalarNode):
                if node.value.startswith("="):
                    self.assertEqual(
                        node.start_mark.line, node.end_mark.line,
                        f"Power Fx formula was folded: {node.value[:80]}",
                    )
            elif isinstance(node, yaml.MappingNode):
                for key, value in node.value:
                    check(key)
                    check(value)
            elif isinstance(node, yaml.SequenceNode):
                for child in node.value:
                    check(child)

        check(yaml.compose(rendered))
        polling_url = next(
            node["url"] for node in all_actions(yaml.safe_load(rendered))
            if node["id"] == "native_wait"
        )
        self.assertGreater(len(polling_url), 180)
        self.assertIn(polling_url, rendered)

    def test_supplied_native_id_is_never_overwritten_by_the_runtime_topic(self):
        assignments = [node for node in self.actions if node["kind"] == "SetVariable"]
        self.assertFalse(any("Global.msdyn_ConversationId" == node.get("variable", "").removeprefix("init:") for node in assignments))
        capture = assignments[0]
        self.assertEqual(capture["value"], "=Lower(Global.msdyn_ConversationId)")

    def test_all_branch_targets_exist_and_action_ids_are_unique(self):
        identifiers = [node["id"] for node in self.actions]
        self.assertEqual(len(identifiers), len(set(identifiers)))
        for node in self.actions:
            if node["kind"] == "GotoAction":
                self.assertIn(node["actionId"], identifiers)

    def test_all_http_requests_are_authenticated_and_errors_are_explicit(self):
        requests = [node for node in self.actions if node["kind"] == "HttpRequestAction"]
        self.assertEqual([node["method"] for node in requests], ["Post", "Get", "Post"])
        for node in requests:
            self.assertEqual(node["headers"], {"X-API-Key": KEY})
            self.assertIn(ORIGIN + "/dynamics/native/", node["url"])
            self.assertEqual(node["errorHandling"]["kind"], "ContinueOnErrorBehavior")
            self.assertIn("statusCode", node["errorHandling"])
            self.assertIn("errorResponseBody", node["errorHandling"])
        self.assertNotIn("body", requests[-1])

    def test_http_error_status_variables_are_not_treated_as_success_status_outputs(self):
        def check(value):
            if isinstance(value, dict):
                if "condition" in value:
                    self.assertNotIn("NativeHttpStatus", value["condition"])
                    self.assertNotIn("NativeStopStatus", value["condition"])
                for child in value.values():
                    check(child)
            elif isinstance(value, list):
                for child in value:
                    check(child)

        check(self.document)
        ids = [node["id"] for node in self.actions]
        self.assertLess(ids.index("native_reset_start_result"), ids.index("native_start"))
        self.assertLess(ids.index("native_reset_stop_result"), ids.index("native_cleanup"))
        self.assertLess(ids.index("native_reset_wait_result"), ids.index("native_wait"))
        self.assertLess(ids.index("native_reset_loop_result"), ids.index("native_poll_again"))

    def test_voice_turn_has_a_real_response_before_silent_http_control(self):
        ids = [node["id"] for node in self.actions]
        self.assertLess(ids.index("native_connecting_notice"), ids.index("native_silence_controller"))
        self.assertLess(ids.index("native_connecting_notice"), ids.index("native_start"))
        notice = next(node for node in self.actions if node["id"] == "native_connecting_notice")
        self.assertTrue(notice["activity"]["speak"][0].strip())

    def test_ai_text_is_forwarded_with_an_explicit_silent_speech_override(self):
        node = next(node for node in self.actions if node["id"] == "native_publish_humain_text")
        self.assertEqual(node["activity"]["text"], ["{Topic.NativeResult.utterance_text}"])
        self.assertEqual(len(node["activity"]["speak"]), 1)
        speech = ElementTree.fromstring(node["activity"]["speak"][0])
        self.assertEqual(speech.tag, "break")
        self.assertEqual(speech.attrib, {"time": "20ms"})
        self.assertFalse(speech.text)
        ids = [node["id"] for node in self.actions]
        self.assertLess(ids.index("native_transcript_message_mode"), ids.index("native_publish_humain_text"))
        self.assertLess(ids.index("native_publish_humain_text"), ids.index("native_acknowledge_transcript"))
        self.assertLess(ids.index("native_acknowledge_transcript"), ids.index("native_wait_after_transcript"))
        modes = {node["id"]: node.get("value") for node in self.actions if node["kind"] == "SetVariable"}
        self.assertIs(modes["native_transcript_message_mode"], False)
        self.assertIs(modes["native_transcript_restore_control"], True)

    def test_transcript_cursor_and_typed_fields_are_present_on_every_response(self):
        for node in self.actions:
            if node["kind"] == "HttpRequestAction":
                properties = node["responseSchema"]["properties"]
                self.assertEqual(properties["utterance_sequence"], "Number")
                for key in ("utterance_text", "utterance_id", "utterance_timestamp"):
                    self.assertEqual(properties[key], "String")
        wait = next(node for node in self.actions if node["id"] == "native_wait")
        self.assertIn("transcript_after=", wait["url"])
        self.assertIn("Topic.NativeTranscriptCursor", wait["url"])
        count = next(node for node in self.actions if node["id"] == "native_count_poll")
        self.assertEqual(count["value"], "=If(IsBlank(Topic.NativeResult.utterance_text), Topic.NativePollCount + 1, Topic.NativePollCount)")

    def test_last_ai_transcript_is_processed_before_handover(self):
        dispatch = next(node for node in self.actions if node["id"] == "native_dispatch_result")
        ids = [condition["id"] for condition in dispatch["conditions"]]
        self.assertLess(ids.index("native_result_invalid"), ids.index("native_transcript_received"))
        self.assertLess(ids.index("native_transcript_received"), ids.index("native_handover_ready"))
        invalid = dispatch["conditions"][0]["condition"]
        self.assertIn("Topic.NativeTranscriptCursor + 1", invalid)
        self.assertIn("IsBlank(Topic.NativeResult.utterance_id)", invalid)

    def test_poll_is_bounded_and_preserves_one_captured_conversation(self):
        capture = next(node for node in self.actions if node["id"] == "native_capture_context")
        self.assertEqual(capture["value"], "=Lower(Global.msdyn_ConversationId)")
        wait = next(node for node in self.actions if node["id"] == "native_wait")
        self.assertIn("?wait=true", wait["url"])
        start = next(node for node in self.actions if node["id"] == "native_start")
        self.assertIn("conversation_id: Topic.NativeConversationId", start["body"]["content"])
        self.assertIn("retain_native_controller: true", start["body"]["content"])
        self.assertIn("Topic.NativePollCount < 10", self.rendered)

    def test_missing_id_exits_locally_before_speech_mode_or_http_initialization(self):
        root_actions = self.document["beginDialog"]["actions"]
        context_check = next(node for node in root_actions if node["id"] == "native_require_context")
        branch = context_check["conditions"][0]["actions"]
        self.assertEqual([node["kind"] for node in branch], ["SendActivity", "EndDialog"])
        self.assertNotIn("GotoAction", yaml.safe_dump(branch))
        self.assertNotIn("HttpRequestAction", yaml.safe_dump(branch))
        self.assertNotIn("OnlyAllowDTMF", yaml.safe_dump(branch))
        self.assertLess(
            next(index for index, node in enumerate(root_actions) if node["id"] == "native_require_context"),
            next(index for index, node in enumerate(root_actions) if node["id"] == "native_silence_controller"),
        )

    def test_handover_is_native_and_requires_cleanup_confirmation(self):
        transfer = [node for node in self.actions if node["kind"] == "TransferConversationV2"]
        self.assertEqual(len(transfer), 1)
        self.assertEqual(transfer[0]["transferType"]["kind"], "TransferToAgent")
        self.assertNotIn("TransferToPhoneNumber", self.rendered)
        self.assertNotIn("EndConversation", self.rendered)
        for target in ("native_handover_ready", "native_completed_handover_recovered"):
            conditions = []

            def collect(value):
                if isinstance(value, dict):
                    if value.get("id") == target:
                        conditions.append(value["condition"])
                    for child in value.values():
                        collect(child)
                elif isinstance(value, list):
                    for child in value:
                        collect(child)

            collect(self.document)
            self.assertEqual(len(conditions), 1)
            self.assertIn("azure_leg_detached", conditions[0])
            self.assertIn('"handover_ready"', conditions[0])

    def test_failure_restores_speech_and_does_not_claim_a_human_connected(self):
        restore = next(node for node in self.actions if node["id"] == "native_report_failure")
        self.assertEqual(restore["variable"], "System.Conversation.OnlyAllowDTMF")
        self.assertIs(restore["value"], False)
        failure = next(node for node in self.actions if node["id"] == "native_connection_error")
        self.assertIn("could not continue", failure["activity"]["speak"][0])
        self.assertNotIn("human connected", self.rendered.lower())

    def test_invalid_origin_key_or_initializer_cannot_be_staged(self):
        for origin in ("http://voice.test.azurecontainerapps.io", "https://untrusted.example", ORIGIN + "/path", ORIGIN + "?q=1"):
            with self.subTest(origin=origin), self.assertRaises(ValueError):
                stage.render_topic(INITIALIZER, origin, KEY)
        for key in ("short", KEY + "\n"):
            with self.assertRaises(ValueError):
                stage.render_topic(INITIALIZER, ORIGIN, key)
        for initializer in ({}, {**INITIALIZER, "variable": "Global.msdyn_ConversationId"}):
            with self.assertRaises(ValueError):
                stage.render_topic(initializer, ORIGIN, KEY)

    def test_repo_template_contains_no_deployed_credentials(self):
        source = stage.TEMPLATE.read_text()
        self.assertIn("__NATIVE_MEDIA_API_KEY__", source)
        self.assertIn("__VOICE_ORIGIN__", source)
        self.assertNotIn(KEY, source)
        self.assertNotIn(KEY, repr(INITIALIZER))

    def test_existing_external_global_reference_requires_explicit_verified_contract(self):
        reference = {**INITIALIZER, "variable": "Global.msdyn_ConversationId"}
        with self.assertRaises(ValueError):
            stage.render_context_topic(reference)
        context = stage.load_topic(stage.render_context_topic(reference, existing_external_global=True))
        self.assertEqual(context["beginDialog"]["kind"], "OnRedirect")
        self.assertEqual(context["beginDialog"]["actions"], [reference])
        self.assertEqual(
            stage.render_topic(reference, ORIGIN, KEY, existing_external_global=True),
            stage.render_topic(INITIALIZER, ORIGIN, KEY),
        )
        for invalid in (
            {**reference, "variable": "Global.anotherId"},
            {**reference, "kind": "Question"},
            {**reference, "value": "=1"},
        ):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                stage.render_context_topic(invalid, existing_external_global=True)
        with self.assertRaises(ValueError):
            stage.render_context_topic(reference, existing_external_global="true")


class NativeEntryTests(unittest.TestCase):
    def test_speech_turn_controller_uses_the_same_caller_and_test_panel_guards(self):
        original = {
            "kind": "AdaptiveDialog",
            "beginDialog": {
                "kind": "OnConversationStart", "id": "main", "actions": [{
                    "kind": "TransferConversationV2", "id": "original_transfer",
                    "transferType": {"kind": "TransferToPhoneNumber", "phoneNumber": "+12025550101"},
                }],
            },
        }
        schema = "test_agent.topic.NativeSpeechTurnPilot"
        entry = stage.load_topic(stage.render_test_entry(original, schema, "+12025550102"))
        gate = entry["beginDialog"]["actions"][0]
        self.assertEqual(gate["conditions"][0]["actions"][0]["dialog"], schema)
        self.assertEqual(gate["elseActions"], original["beginDialog"]["actions"])
        test = stage.load_topic(stage.render_test_panel_entry(
            entry, schema, "11111111-1111-4111-8111-111111111111",
        ))
        self.assertEqual(
            test["beginDialog"]["actions"][0]["conditions"][0]["condition"],
            "=System.Conversation.InTestMode = true",
        )
        self.assertEqual(test["beginDialog"]["actions"][0]["elseActions"], entry["beginDialog"]["actions"])

    def test_test_panel_override_is_guarded_and_real_entry_is_unchanged(self):
        original = {
            "kind": "AdaptiveDialog",
            "beginDialog": {
                "kind": "OnConversationStart", "id": "main",
                "actions": [{"kind": "EndDialog", "id": "original"}],
            },
        }
        snapshot = yaml.safe_load(yaml.safe_dump(original))
        rendered = stage.render_test_panel_entry(
            original, "test_agent.topic.NativeMediaPilot", "11111111-1111-4111-8111-111111111111"
        )
        doc = yaml.safe_load(rendered)
        gate = doc["beginDialog"]["actions"][0]
        self.assertEqual(gate["conditions"][0]["condition"], "=System.Conversation.InTestMode = true")
        self.assertEqual(gate["elseActions"], snapshot["beginDialog"]["actions"])
        self.assertEqual(original, snapshot)
        self.assertEqual(gate["conditions"][0]["actions"][0]["value"], "11111111-1111-4111-8111-111111111111")
        with self.assertRaises(ValueError):
            stage.render_test_panel_entry(doc, "test_agent.topic.NativeMediaPilot", "11111111-1111-4111-8111-111111111111")

    def test_native_unquoted_e164_value_round_trips_without_losing_plus(self):
        source = """kind: AdaptiveDialog
beginDialog:
  kind: OnConversationStart
  id: main
  actions:
    - kind: TransferConversationV2
      id: original_transfer
      transferType:
        kind: TransferToPhoneNumber
        phoneNumber: +12025550101
"""
        original = stage.load_topic(source)
        phone = original["beginDialog"]["actions"][0]["transferType"]["phoneNumber"]
        self.assertIsInstance(phone, str)
        self.assertEqual(phone, "+12025550101")
        rendered = stage.render_test_entry(original, "test_agent.topic.NativeMediaPilot", "+12025550102")
        after = yaml.safe_load(rendered)
        restored = after["beginDialog"]["actions"][0]["elseActions"][0]["transferType"]["phoneNumber"]
        self.assertIsInstance(restored, str)
        self.assertEqual(restored, "+12025550101")
        with self.assertRaisesRegex(ValueError, "E.164 string"):
            stage.render_test_entry(yaml.safe_load(source), "test_agent.topic.NativeMediaPilot", "+12025550102")

    def test_schema_loader_preserves_other_numeric_and_boolean_values(self):
        document = stage.load_topic("phoneNumber: +12025550101\ncounter: 0\nenabled: true\n")
        self.assertEqual(document, {"phoneNumber": "+12025550101", "counter": 0, "enabled": True})
        self.assertIsInstance(document["counter"], int)
        self.assertIs(document["enabled"], True)

    def test_only_selected_caller_is_redirected_and_original_route_is_preserved(self):
        original = {
            "kind": "AdaptiveDialog",
            "beginDialog": {
                "kind": "OnConversationStart", "id": "main", "actions": [{
                    "kind": "TransferConversationV2", "id": "original_transfer",
                    "transferType": {"kind": "TransferToPhoneNumber", "phoneNumber": "+12025550101"},
                }],
            },
        }
        original_actions = original["beginDialog"]["actions"]
        result = yaml.safe_load(stage.render_test_entry(
            original, "test_agent.topic.NativeMediaPilot", "+12025550102"
        ))
        actions = result["beginDialog"]["actions"]
        self.assertEqual(len(actions), 1)
        gate = actions[0]
        self.assertEqual(gate["conditions"][0]["condition"], '=System.Activity.From.Name = "+12025550102"')
        self.assertEqual(gate["conditions"][0]["actions"], [{
            "kind": "BeginDialog", "id": "native_pilot_begin", "dialog": "test_agent.topic.NativeMediaPilot",
        }])
        self.assertEqual(gate["elseActions"], original_actions)
        self.assertEqual(original["beginDialog"]["actions"], original_actions)

    def test_other_entry_flows_and_unsafe_selectors_are_rejected(self):
        for arguments in (
            ({}, "test_agent.topic.NativeMediaPilot", "+12025550102"),
            ({}, "test_agent.topic.Other", "+12025550102"),
            ({}, "test_agent.topic.NativeMediaPilot", '"+12025550102"'),
        ):
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                stage.render_test_entry(*arguments)


class TranscriptBoundaryProbeTests(unittest.TestCase):
    def setUp(self):
        self.topic = stage.load_topic(
            (ROOT / "apps/voice/dynamics/native-transcript-boundary-probe.topic.yaml").read_text()
        )
        self.actions = self.topic["beginDialog"]["actions"]

    def test_diagnostic_is_self_ending_and_never_calls_an_api_or_transfers(self):
        self.assertEqual(self.topic["beginDialog"]["kind"], "OnRedirect")
        self.assertEqual(self.actions[-1]["kind"], "EndConversation")
        kinds = {node["kind"] for node in all_actions(self.topic)}
        self.assertNotIn("HttpRequestAction", kinds)
        self.assertNotIn("TransferConversationV2", kinds)
        self.assertNotIn("GotoAction", kinds)

    def test_voice_participant_has_a_bounded_silent_test_window(self):
        node = next(n for n in self.actions if n["id"] == "transcript_probe_microphone_window")
        speech = ElementTree.fromstring(node["activity"]["speak"][0])
        self.assertEqual(speech.tag, "break")
        self.assertEqual(speech.attrib["time"], "20000ms")
        self.assertLess(self.actions.index(node), len(self.actions) - 1)

    def test_diagnostic_separates_display_dynamic_speech_and_silent_speech(self):
        nodes = {node["id"]: node for node in self.actions}
        different = nodes["transcript_probe_different_speech"]["activity"]
        self.assertEqual(different["text"], ["Display alpha."])
        self.assertEqual(different["speak"], ["Spoken bravo."])
        dynamic = nodes["transcript_probe_dynamic_speech"]["activity"]
        self.assertEqual(dynamic["text"], dynamic["speak"])
        silent = nodes["transcript_probe_display_only"]["activity"]
        self.assertEqual(silent["text"], ["Display delta."])
        self.assertEqual(ElementTree.fromstring(silent["speak"][0]).tag, "break")
        muted = nodes["transcript_probe_muted_words"]["activity"]
        xml = ElementTree.fromstring(muted["speak"][0])
        self.assertEqual(xml.attrib["volume"], "silent")
        self.assertEqual(xml.text, muted["text"][0])

    def test_owned_caller_gate_preserves_the_existing_real_caller_entry(self):
        previous = {
            "kind": "AdaptiveDialog",
            "beginDialog": {
                "kind": "OnConversationStart", "id": "main",
                "actions": [{"kind": "ConditionGroup", "id": "native_pilot_test_entry", "conditions": []}],
            },
        }
        original = yaml.safe_load(yaml.safe_dump(previous))
        result = stage.load_topic(stage.render_transcript_probe_entry(
            previous, "test_agent.topic.NativeTranscriptBoundaryProbe", "+12025550101"
        ))
        gate = result["beginDialog"]["actions"][0]
        self.assertEqual(gate["elseActions"], previous["beginDialog"]["actions"])
        self.assertEqual(gate["conditions"][0]["condition"], '=System.Activity.From.Name = "+12025550101"')
        self.assertEqual(previous, original)
        with self.assertRaises(ValueError):
            stage.render_transcript_probe_entry(result, "test_agent.topic.NativeTranscriptBoundaryProbe", "+12025550101")


class NativeLanguageProbeTests(unittest.TestCase):
    def test_language_switches_use_the_supported_user_variable_and_end_the_call(self):
        document = stage.load_topic(
            (ROOT / "apps/voice/dynamics/native-language-probe.topic.yaml").read_text()
        )
        actions = document["beginDialog"]["actions"]
        self.assertEqual(document["beginDialog"]["kind"], "OnRedirect")
        self.assertEqual([node["kind"] for node in actions], [
            "SetVariable", "Question", "SetVariable", "Question",
            "SetVariable", "Question", "EndConversation",
        ])
        language_nodes = [node for node in actions if node["kind"] == "SetVariable"]
        self.assertEqual([node["value"]["value"] for node in language_nodes], ["Arabic", "English", "Arabic"])
        for node in language_nodes:
            self.assertEqual(node["variable"], "System.User.Language")
            self.assertEqual(node["value"]["kind"], "OptionDataValue")
            self.assertEqual(node["value"]["type"], {"kind": "SystemOptionSet", "name": "Locale"})
        for index in (1, 3, 5):
            question = actions[index]
            message = question["prompt"]
            self.assertEqual(message["text"], message["speak"])
            self.assertTrue(message["speak"][0].strip())
            self.assertEqual(question["entity"], {
                "kind": "BooleanPrebuiltEntity",
                "dtmfMultipleChoiceOptions": {"generateMapping": True},
            })
            self.assertTrue(question["alwaysPrompt"])
            self.assertFalse(question["interruptionPolicy"]["allowInterruption"])
            expected = {
                "silenceDetectionTimeoutInMilliseconds": 500 if index == 1 else 2000,
                "repeatCountOnSilence": 0,
                "defaultValueMissingAction": "SetEmpty",
                "defaultValueOnSilence": "=Blank()",
            }
            if index == 1:
                expected.update({"utteranceEndTimeoutInMilliseconds": 300, "speechRecognitionTimeoutInMilliseconds": 1000})
            self.assertEqual(question["voiceInputSettings"], expected)
        for index in (1, 5):
            self.assertTrue(any("\u0600" <= char <= "\u06ff" for char in actions[index]["prompt"]["text"][0]))

    def test_silence_continuation_preserves_native_choice_types_when_serialized(self):
        document = stage.load_topic(
            (ROOT / "apps/voice/dynamics/native-language-probe.topic.yaml").read_text()
        )
        rendered = yaml.dump(document, Dumper=stage.CopilotDumper, sort_keys=False, line_break="\r\n")
        restored = stage.load_topic(rendered)
        self.assertEqual(restored, document)
        for node in restored["beginDialog"]["actions"]:
            if node["kind"] == "Question":
                self.assertEqual(node["voiceInputSettings"]["defaultValueOnSilence"], "=Blank()")
                self.assertEqual(node["voiceInputSettings"]["defaultValueMissingAction"], "SetEmpty")


class SpeechTurnPilotTests(unittest.TestCase):
    def test_case_number_pattern_escapes_literal_hyphen_for_native_power_fx(self):
        for record in ("NativeResult", "NativeStopResult"):
            expression = stage.case_number_expression(record)
            self.assertIn('"[A-Za-z0-9][A-Za-z0-9\\-]{2,79}"', expression)
            self.assertNotIn("[A-Za-z0-9-]", expression)

    def setUp(self):
        self.original = stage.load_topic(stage.TEMPLATE.read_text())
        self.before = copy.deepcopy(self.original)
        self.document = stage.load_topic(stage.render_speech_turn_pilot(self.original))
        self.nodes = {node["id"]: node for node in all_actions(self.document)}

    def test_startup_notice_uses_only_application_audio_during_preparation(self):
        self.assertNotIn("play_startup_notice", self.nodes["native_start"]["body"]["content"])
        enabled = stage.load_topic(stage.render_speech_turn_pilot(self.original, startup_notice=True))
        nodes = {node["id"]: node for node in all_actions(enabled)}
        self.assertIn("play_startup_notice: true", nodes["native_start"]["body"]["content"])
        self.assertNotIn("native_startup_notice", nodes["native_start"]["body"]["content"])
        message = nodes["native_connecting_notice"]["activity"]
        self.assertEqual(message, {"speak": ['<break time="20ms" />']})
        self.assertTrue(nodes["native_start"]["url"].endswith("/dynamics/native/sessions?wait_for_startup=true"))
        self.assertNotIn("wait_for_context", nodes["native_start"]["url"])
        latency = nodes["native_start"]["latencyMessageSettings"]
        self.assertEqual(latency, {"kind": "LatencyMessageSettings", "allowLatencyMessage": False})
        guard = next(node for node in nodes["native_check_start"]["conditions"] if node["id"] == "native_start_not_prepared")
        self.assertEqual(guard["condition"], stage.startup_prepared_condition("application"))
        self.assertNotIn("<audio", stage.yaml.dump(enabled))
        self.assertNotIn("This call is recorded", stage.yaml.dump(enabled))
        order = [node.get("id") for node in enabled["beginDialog"]["actions"]]
        self.assertLess(order.index("native_connecting_notice"), order.index("native_start"))
        self.assertLess(order.index("native_start"), order.index("native_select_listening_language"))
        initial = {"native_initial_requested_language", "native_initial_requested_revision"}
        self.assertEqual([identifier for identifier in order if identifier not in initial],
                         [node.get("id") for node in self.document["beginDialog"]["actions"]])
        self.assertLess(order.index("native_initial_requested_language"), order.index("native_reset_wait_result"))
        for name in ("native_listen_turn", "native_transfer_to_agent"):
            self.assertEqual(nodes[name], self.nodes[name])
        self.assertEqual(self.original, self.before)
        for invalid in ("true", 1, None):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                stage.render_speech_turn_pilot(self.original, startup_notice=invalid)

    def test_application_owner_transform_preserves_native_history_and_rejects_drift(self):
        native = stage.native_startup_controller(self.document, "__VOICE_ORIGIN__")
        before = copy.deepcopy(native)
        application = stage.application_startup_controller(native, "__VOICE_ORIGIN__")
        self.assertEqual(native, before)
        self.assertEqual(stage.application_startup_controller(application, "__VOICE_ORIGIN__"), application)
        for field in ("url", "body", "latencyMessageSettings", "condition"):
            changed = copy.deepcopy(native)
            nodes = {node["id"]: node for node in all_actions(changed)}
            if field == "condition":
                next(node for node in nodes["native_check_start"]["conditions"]
                     if node["id"] == "native_start_not_prepared")["condition"] = "=true"
            elif field == "body":
                nodes["native_start"]["body"]["content"] += ", play_startup_notice: true"
            else:
                nodes["native_start"][field] = "unreviewed"
            with self.subTest(field=field), self.assertRaises(ValueError):
                stage.application_startup_controller(changed, "__VOICE_ORIGIN__")
        original_nodes = {node["id"]: node for node in all_actions(native)}
        revised_nodes = {node["id"]: node for node in all_actions(application)}
        for field in ("body", "latencyMessageSettings"):
            revised_nodes["native_start"][field] = original_nodes["native_start"][field]
        next(node for node in revised_nodes["native_check_start"]["conditions"]
             if node["id"] == "native_start_not_prepared")["condition"] = stage.startup_prepared_condition("native")
        self.assertEqual(application, native)

    def test_native_speech_message_uses_the_approved_audio_and_meaningful_display_text(self):
        message = stage.startup_notice_message(ORIGIN)
        digest = hashlib.sha256(stage.NOTICE_AUDIO.read_bytes()).hexdigest()
        source = f"{ORIGIN}/dynamics/native/startup-notice/{digest}.wav"
        self.assertEqual(len(message["speak"]), 1)
        audio = ElementTree.fromstring(message["speak"][0])
        self.assertEqual(audio.tag, "audio")
        self.assertEqual(audio.attrib, {"src": source})
        self.assertIsNone(audio.text)
        self.assertEqual(list(audio), [])
        self.assertEqual(len(message["text"]), 1)
        self.assertIn("This call is recorded", message["text"][0])
        self.assertNotIn("<audio", message["text"][0])

    def test_notice_only_changes_startup_fields_with_crm_and_pause_ai_enabled(self):
        baseline = stage.load_topic(stage.render_speech_turn_pilot(
            self.original, crm_case_context=True, supervision=True,
        ))
        enabled = stage.load_topic(stage.render_speech_turn_pilot(
            self.original, startup_notice=True, crm_case_context=True, supervision=True,
        ))
        before = {node["id"]: node for node in all_actions(baseline)}
        after = {node["id"]: node for node in all_actions(enabled)}
        start = after["native_start"]
        self.assertIn("supervision_contract: 1", start["body"]["content"])
        self.assertEqual(start["latencyMessageSettings"], {"kind": "LatencyMessageSettings", "allowLatencyMessage": False})
        self.assertEqual(after["native_connecting_notice"]["activity"], {"speak": ['<break time="20ms" />']})
        for field in ("body", "url"):
            start[field] = copy.deepcopy(before["native_start"][field])
        del start["latencyMessageSettings"]
        after["native_connecting_notice"]["activity"] = copy.deepcopy(before["native_connecting_notice"]["activity"])
        after["native_check_start"]["conditions"] = copy.deepcopy(before["native_check_start"]["conditions"])
        enabled["beginDialog"]["actions"] = [
            node for node in enabled["beginDialog"]["actions"]
            if node.get("id") not in {"native_initial_requested_language", "native_initial_requested_revision"}
        ]
        for identifier, original in before.items():
            revised = after[identifier]
            if original.get("kind") == "HttpRequestAction":
                for field in ("native_startup_prepared", "startup_notice_owner", "startup_failed_unpaused"):
                    if field not in original["responseSchema"]["properties"]:
                        revised["responseSchema"]["properties"].pop(field)
            if original.get("kind") == "SetVariable" and original.get("variable") in {"Topic.NativeResult", "Topic.NativeStopResult"}:
                revised["value"] = original["value"]
        self.assertEqual(enabled, baseline)

    def test_question_is_silent_and_continues_without_fabricating_caller_input(self):
        question = self.nodes["native_listen_turn"]
        self.assertEqual(question["kind"], "Question")
        self.assertEqual(question["entity"], "StringPrebuiltEntity")
        self.assertEqual(question["prompt"], {"speak": ['<break time="20ms" />']})
        self.assertTrue(question["alwaysPrompt"])
        self.assertFalse(question["interruptionPolicy"]["allowInterruption"])
        self.assertEqual(question["voiceInputSettings"], {
            "silenceDetectionTimeoutInMilliseconds": 500,
            "repeatCountOnSilence": 0,
            "defaultValueMissingAction": "SetEmpty",
            "defaultValueOnSilence": "=Blank()",
            "utteranceEndTimeoutInMilliseconds": 300,
            "speechRecognitionTimeoutInMilliseconds": 1000,
        })

    def test_each_poll_returns_through_listening_and_retains_bounded_lifecycle(self):
        ids = [node["id"] for node in self.document["beginDialog"]["actions"]]
        conditions = {node["id"]: node for node in self.nodes["native_dispatch_result"]["conditions"]}
        self.assertEqual(ids.index("native_wait"), ids.index("native_listen_turn") + 1)
        self.assertEqual(ids.index("native_listen_turn"), ids.index("native_select_listening_language") + 1)
        self.assertEqual(self.nodes["native_wait"]["method"], "Post")
        self.assertIn('"/turn"', self.nodes["native_wait"]["url"])
        self.assertEqual(
            self.nodes["native_wait"]["body"]["content"],
            "=JSON({language: Topic.NativeListenLanguage, revision: Topic.NativeLanguageRevision})",
        )
        self.assertEqual(
            conditions["native_keep_waiting"]["condition"],
            '=Topic.NativeResult.action = "wait" && Topic.NativePollCount < 3000',
        )
        self.assertEqual(self.nodes["native_count_poll"]["value"], "=Topic.NativePollCount + 1")
        for node in self.nodes.values():
            if node["kind"] == "GotoAction":
                self.assertIn(node["actionId"], self.nodes)
                self.assertNotEqual(node["actionId"], "native_wait")
            if node["kind"] == "SetVariable" and node.get("variable") == "System.Conversation.OnlyAllowDTMF":
                self.assertIs(node["value"], False)
        previous = {node["id"]: node for node in all_actions(self.original)}
        self.assertEqual(self.nodes["native_transfer_to_agent"], previous["native_transfer_to_agent"])
        for name in ("native_start", "native_cleanup", "native_wait"):
            self.assertEqual(self.nodes[name]["headers"], previous[name]["headers"])
            self.assertEqual(self.nodes[name]["errorHandling"], previous[name]["errorHandling"])
        self.assertIn("speech_turn_control: true", self.nodes["native_start"]["body"]["content"])
        before = next(node for node in previous["native_dispatch_result"]["conditions"] if node["id"] == "native_handover_ready")
        self.assertEqual(conditions["native_handover_ready"], before)
        self.assertEqual(self.original, self.before)

    def test_initial_language_uses_the_real_choice_type_and_repeated_conversion_is_refused(self):
        self.assertEqual(self.nodes["native_initialize_listening_language"]["value"], "ar")
        self.assertEqual(self.nodes["native_initialize_language_revision"]["value"], 0)
        for code, label in (("ar", "Arabic"), ("en", "English")):
            locale = self.nodes[f"native_listen_in_{code}"]
            self.assertEqual(locale["variable"], "System.User.Language")
            self.assertEqual(locale["value"], {
                "kind": "OptionDataValue",
                "type": {"kind": "SystemOptionSet", "name": "Locale"},
                "value": label,
            })
        with self.assertRaises(ValueError):
            stage.render_speech_turn_pilot(self.document)
        with self.assertRaises(ValueError):
            stage.render_speech_turn_pilot({"beginDialog": {"kind": "OnConversationStart", "actions": []}})

    def test_mixed_recognition_is_not_mirrored_or_used_to_choose_language_or_handover(self):
        self.assertNotIn("native_publish_humain_text", self.nodes)
        question = self.nodes["native_listen_turn"]
        self.assertEqual(question["variable"], "init:Topic.NativeObservedSpeech")
        serialized = yaml.safe_dump(self.document)
        self.assertEqual(serialized.count("NativeObservedSpeech"), 1)
        self.assertIn("Topic.NativeResult.control_language", self.nodes["native_capture_requested_language"]["value"])
        self.assertIn("Topic.NativeResult.control_revision", self.nodes["native_capture_requested_revision"]["value"])
        for node in self.nodes.values():
            if node["kind"] == "HttpRequestAction":
                properties = node["responseSchema"]["properties"]
                self.assertEqual(properties["control_language"], "String")
                self.assertEqual(properties["control_revision"], "Number")
                self.assertEqual(properties["control_ready"], "Boolean")


class CaseHandoffTests(unittest.TestCase):
    def setUp(self):
        self.original = stage.load_topic(stage.TEMPLATE.read_text())
        self.normal = stage.load_topic(stage.render_speech_turn_pilot(self.original))
        self.document = stage.load_topic(stage.render_speech_turn_pilot(self.original, crm_case_context=True))
        self.nodes = {node["id"]: node for node in all_actions(self.document)}

    def test_case_context_is_opt_in_and_does_not_change_transfer_or_language_control(self):
        self.assertNotIn("CaseNumber", stage.render_speech_turn_pilot(self.original))
        previous = {node["id"]: node for node in all_actions(self.normal)}
        for identifier in ("native_transfer_to_agent", "native_restore_speech", "native_listen_turn", "native_select_listening_language"):
            self.assertEqual(self.nodes[identifier], previous[identifier])
        self.assertEqual(self.nodes["native_wait"]["body"], previous["native_wait"]["body"])
        self.assertEqual(self.nodes["native_initialize_case_number"]["variable"], "init:Global.CaseNumber")
        self.assertEqual(self.nodes["native_initialize_case_number"]["value"], '=Text("")')
        for invalid in (1, "true", None):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                stage.render_speech_turn_pilot(self.original, crm_case_context=invalid)

    def test_every_response_and_reset_has_bounded_typed_case_fields(self):
        for identifier in ("native_start", "native_wait", "native_cleanup"):
            properties = self.nodes[identifier]["responseSchema"]["properties"]
            for field, kind in stage.CASE_CONTEXT_FIELDS.items():
                self.assertEqual(properties[field], kind)
            self.assertEqual(self.nodes[identifier]["headers"], {"X-API-Key": "__NATIVE_MEDIA_API_KEY__"})
        for node in self.nodes.values():
            if node.get("kind") == "SetVariable" and node.get("variable") in {"Topic.NativeResult", "Topic.NativeStopResult"}:
                for field in stage.CASE_CONTEXT_FIELDS:
                    self.assertIn(field + ":", node["value"])
        self.assertFalse({"phone", "email", "notes", "description"} & stage.CASE_CONTEXT_FIELDS.keys())

    def test_normal_and_recovered_handover_use_their_own_authoritative_result(self):
        branches = {
            branch["id"]: branch for node in self.nodes.values() if node.get("kind") == "ConditionGroup"
            for branch in node.get("conditions", [])
        }
        for condition, capture, source, other in (
            ("native_handover_ready", "native_case_from_status", "NativeResult", "NativeStopResult"),
            ("native_completed_handover_recovered", "native_case_from_stop", "NativeStopResult", "NativeResult"),
        ):
            branch = branches[condition]
            self.assertEqual(branch["actions"][0], self.nodes[capture])
            self.assertEqual(self.nodes[capture]["value"], stage.case_number_expression(source))
            self.assertNotIn(f"Topic.{other}.", self.nodes[capture]["value"])
            self.assertEqual(branch["actions"][-1]["actionId"], "native_restore_speech")
            self.assertIn("azure_leg_detached", branch["condition"])
            self.assertIn("handover_ready", branch["condition"])
        identifiers = [node["id"] for node in self.document["beginDialog"]["actions"]]
        self.assertEqual(identifiers.index("native_transfer_to_agent"), identifiers.index("native_set_case_number") + 1)
        self.assertEqual(self.nodes["native_set_case_number"]["variable"], "Global.CaseNumber")
        self.assertEqual(self.nodes["native_set_case_number"]["value"], "=Topic.NativeHandoverCaseNumber")

    def test_no_case_fallback_or_link_success_requirement_is_injected(self):
        for source in ("NativeResult", "NativeStopResult"):
            value = stage.case_number_expression(source)
            self.assertIn(f"Topic.{source}.selected_case_number", value)
            self.assertIn(f"Topic.{source}.selected_case_id", value)
            self.assertIn('"OP-"', value)
            self.assertIn("[0-9A-Fa-f]{32}", value)
            self.assertTrue(value.endswith(', Text(""))'))
            self.assertNotIn("case_link_status", value)
            self.assertNotIn("customer", value.lower())
        with self.assertRaises(ValueError):
            stage.case_number_expression("NativeResult;untrusted")

    def test_readiness_rejects_wrong_recovery_source_and_missing_case_schema(self):
        stage.validate_case_handoff(self.document)
        for change in ("recovery", "schema", "assignment", "placement"):
            document = copy.deepcopy(self.document)
            nodes = {node["id"]: node for node in all_actions(document)}
            if change == "recovery":
                nodes["native_case_from_stop"]["value"] = stage.case_number_expression("NativeResult")
            elif change == "schema":
                del nodes["native_cleanup"]["responseSchema"]["properties"]["selected_case_number"]
            elif change == "assignment":
                nodes["native_set_case_number"]["variable"] = "Global.msdyn_CaseId"
            else:
                actions = document["beginDialog"]["actions"]
                actions.remove(nodes["native_set_case_number"])
                actions.insert(0, nodes["native_set_case_number"])
            with self.subTest(change=change), self.assertRaises(ValueError):
                stage.validate_case_handoff(document)

    def test_notice_case_optins_compose_without_changing_notice_or_speech_ack(self):
        without_case = stage.load_topic(stage.render_speech_turn_pilot(self.original, startup_notice=True))
        with_case = stage.load_topic(stage.render_speech_turn_pilot(
            self.original, startup_notice=True, crm_case_context=True,
        ))
        previous = {node["id"]: node for node in all_actions(without_case)}
        current = {node["id"]: node for node in all_actions(with_case)}
        for identifier in ("native_listen_turn", "native_select_listening_language", "native_connecting_notice", "native_transfer_to_agent"):
            self.assertEqual(current[identifier], previous[identifier])
        self.assertEqual(current["native_start"]["latencyMessageSettings"], previous["native_start"]["latencyMessageSettings"])
        self.assertEqual(current["native_start"]["body"], previous["native_start"]["body"])
        stage.validate_case_handoff(with_case)


if __name__ == "__main__":
    unittest.main()
