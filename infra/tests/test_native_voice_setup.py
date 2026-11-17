import copy
import hashlib
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, mock_open, patch
from uuid import UUID


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "infra/scripts"))
import native_voice_setup as setup
import stage_native_media_topic as topics
import sync_native_voice_topic as snapshot
import watch_native_voice as watch


BOT = "11111111-1111-4111-8111-111111111111"
WORKSTREAM = "22222222-2222-4222-8222-222222222222"
QUEUE = "33333333-3333-4333-8333-333333333333"
CALL = "44444444-4444-4444-8444-444444444444"
KEY = "synthetic-native-api-key-for-unit-tests"
ORIGIN = "https://voice.example.azurecontainerapps.io"


def basic_voice_configuration():
    return json.dumps({
        "isTelephonyEnabled": True,
        "settings": {"TelephonyInitialized": True, "GenerativeActionsEnabled": False},
        "voiceProcessingMode": {"$kind": "ClassicVoiceProcessingMode"},
    })


@contextmanager
def private_test_directory():
    # A private folder outside the real checkout, with a stand-in checkout inside it for the boundary checks.
    with tempfile.TemporaryDirectory(prefix="native-voice-setup-test-") as name:
        directory = Path(name).resolve()
        checkout = directory / "checkout"
        checkout.mkdir(mode=0o700)
        with patch.object(topics, "ROOT", checkout):
            yield directory


def records(*, crm_case_context=False):
    definition = {
        "kind": "Variable", "name": "msdyn_ConversationId", "scope": "Conversation",
        "isExternalInitializationAllowed": True, "isOutputToExternalCallers": False,
    }
    initializer = {
        "kind": "SetVariable", "id": "saved_initializer", "variable": "init:Global.msdyn_ConversationId", "value": '=""',
    }
    entry = {
        "kind": "AdaptiveDialog", "beginDialog": {
            "kind": "OnConversationStart", "id": "main",
            "actions": [{"kind": "EndConversation", "id": "unchanged_non_test_route"}],
        },
    }
    documents = {
        "msdyn_ConversationId": definition,
        "NativeMediaContext": topics.load_topic(topics.render_context_topic(initializer)),
        "NativeSpeechTurnPilot": {"kind": "AdaptiveDialog", "beginDialog": {"kind": "OnRedirect", "id": "main", "actions": []}},
        "Conversation Start": entry,
    }
    if crm_case_context:
        documents["CaseNumber"] = {
            "kind": "Variable", "name": "CaseNumber", "scope": "Conversation",
            "isExternalInitializationAllowed": False, "isOutputToExternalCallers": True,
        }
    result = {}
    for index, (name, doc) in enumerate(documents.items(), 1):
        result[name] = {
            "name": name, "componenttype": 12 if doc["kind"] == "Variable" else 9, "data": setup.dump(doc),
            "botcomponentid": f"00000000-0000-4000-8000-{index:012d}",
            "schemaname": "sample_agent." + ("GlobalVariableComponent." if doc["kind"] == "Variable" else "topic.") + name.replace(" ", ""),
            "language": None if doc["kind"] == "Variable" else 1033, "@odata.etag": 'W/"100"',
        }
    return result


class BundleTests(unittest.TestCase):
    def test_bundle_threads_explicit_notice_option_without_changing_caller_gate(self):
        source = records()
        normal = setup.bundle_documents(source, ORIGIN, KEY, ["+12025550101"])
        enabled = setup.bundle_documents(source, ORIGIN, KEY, ["+12025550101"], startup_notice=True)
        self.assertEqual(enabled["context"], normal["context"])
        self.assertEqual(enabled["entry"], normal["entry"])
        self.assertNotIn("play_startup_notice", normal["controller"])
        self.assertIn("play_startup_notice: true", enabled["controller"])
        self.assertNotIn("native_startup_notice", enabled["controller"])

    def test_winning_snapshot_is_generated_without_real_credentials(self):
        self.assertEqual(snapshot.OUTPUT.read_bytes(), snapshot.generated().encode())
        source = snapshot.OUTPUT.read_text()
        self.assertIn("__NATIVE_MEDIA_API_KEY__", source)
        self.assertIn("__VOICE_ORIGIN__", source)
        self.assertNotIn(KEY, source)
        self.assertIn("speech_turn_control: true", source)
        self.assertNotIn('variable: init:Global.msdyn_ConversationId', source)

    def test_bundle_uses_the_working_renderer_and_preserves_initializer_and_other_callers(self):
        source = records()
        original = copy.deepcopy(source)
        files = setup.bundle_documents(source, ORIGIN, KEY, ["+12025550101", "+12025550102"])
        controller = topics.load_topic(files["controller"])
        nodes = {node["id"]: node for node in controller["beginDialog"]["actions"]}
        self.assertIn("speech_turn_control: true", nodes["native_start"]["body"]["content"])
        self.assertEqual(nodes["native_wait"]["method"], "Post")
        self.assertEqual(nodes["native_listen_turn"]["voiceInputSettings"]["speechRecognitionTimeoutInMilliseconds"], 1000)
        self.assertEqual(nodes["native_start"]["headers"]["X-API-Key"], KEY)
        context = topics.load_topic(files["context"])
        self.assertEqual(context["beginDialog"]["actions"][0]["id"], "saved_initializer")
        entry = topics.load_topic(files["entry"])["beginDialog"]["actions"][0]
        self.assertEqual(entry["elseActions"], topics.load_topic(source["Conversation Start"]["data"])["beginDialog"]["actions"])
        self.assertEqual(entry["conditions"][0]["condition"], '=System.Activity.From.Name = "+12025550101" || System.Activity.From.Name = "+12025550102"')
        self.assertEqual(source, original)

    def test_existing_controller_edits_require_explicit_replace(self):
        source = records()
        source["NativeSpeechTurnPilot"]["data"] = setup.dump({
            "kind": "AdaptiveDialog", "beginDialog": {"kind": "OnRedirect", "actions": [{"kind": "EndDialog", "id": "user_edit"}]},
        })
        with self.assertRaisesRegex(ValueError, "already has actions"):
            setup.bundle_documents(source, ORIGIN, KEY, ["+12025550101"])
        setup.bundle_documents(source, ORIGIN, KEY, ["+12025550101"], replace_controller=True)

    def test_context_must_not_have_executable_runtime_work(self):
        source = records()
        doc = topics.load_topic(source["NativeMediaContext"]["data"])
        doc["beginDialog"]["actions"].append({"kind": "EndDialog", "id": "extra"})
        source["NativeMediaContext"]["data"] = setup.dump(doc)
        with self.assertRaises(ValueError):
            setup.bundle_documents(source, ORIGIN, KEY, ["+12025550101"])

    def test_existing_external_context_reference_preserves_controller_and_caller_gate(self):
        source = records()
        original = setup.bundle_documents(source, ORIGIN, KEY, ["+12025550101"])
        context = topics.load_topic(source[setup.CONTEXT]["data"])
        context["beginDialog"]["actions"][0]["variable"] = "Global.msdyn_ConversationId"
        source[setup.CONTEXT]["data"] = setup.dump(context)
        generated = setup.bundle_documents(source, ORIGIN, KEY, ["+12025550101"])
        self.assertEqual(generated["controller"], original["controller"])
        self.assertEqual(generated["entry"], original["entry"])
        self.assertEqual(
            topics.load_topic(generated["context"])["beginDialog"]["actions"][0]["variable"],
            "Global.msdyn_ConversationId",
        )
        self.assertNotIn("init:Global.msdyn_ConversationId", generated["context"])

    def test_reference_form_requires_the_same_external_input_only_global(self):
        for field, value in (("scope", "User"), ("isOutputToExternalCallers", True), ("isExternalInitializationAllowed", False)):
            source = records()
            context = topics.load_topic(source[setup.CONTEXT]["data"])
            context["beginDialog"]["actions"][0]["variable"] = "Global.msdyn_ConversationId"
            source[setup.CONTEXT]["data"] = setup.dump(context)
            variable = topics.load_topic(source[setup.VARIABLE]["data"])
            variable[field] = value
            source[setup.VARIABLE]["data"] = setup.dump(variable)
            with self.subTest(field=field), self.assertRaises(ValueError):
                setup.bundle_documents(source, ORIGIN, KEY, ["+12025550101"])

    def test_variable_must_be_input_only_and_externally_initialized(self):
        for field, value in (("scope", "User"), ("isOutputToExternalCallers", True), ("isExternalInitializationAllowed", False)):
            source = records()
            variable = topics.load_topic(source["msdyn_ConversationId"]["data"])
            variable[field] = value
            source["msdyn_ConversationId"]["data"] = setup.dump(variable)
            with self.subTest(field=field), self.assertRaises(ValueError):
                setup.bundle_documents(source, ORIGIN, KEY, ["+12025550101"])

    def test_unsafe_callers_and_experimental_entry_are_refused(self):
        original = topics.load_topic(records()["Conversation Start"]["data"])
        for callers in ([], ["+12025550101"] * 2, ["123"], ["+12025550101"] * 4):
            with self.subTest(callers=callers), self.assertRaises(ValueError):
                setup.caller_entry(original, "sample_agent.topic.NativeSpeechTurnPilot", callers)
        original["beginDialog"]["actions"][0]["id"] = "native_transcript_probe_entry"
        with self.assertRaises(ValueError):
            setup.caller_entry(original, "sample_agent.topic.NativeSpeechTurnPilot", ["+12025550101"])

    def test_regenerating_our_gate_does_not_nest_or_change_the_fallback(self):
        original = topics.load_topic(records()["Conversation Start"]["data"])
        first = setup.caller_entry(original, "sample_agent.topic.NativeSpeechTurnPilot", ["+12025550101"])
        second = setup.caller_entry(first, "sample_agent.topic.NativeSpeechTurnPilot", ["+12025550102"])
        self.assertEqual(second["beginDialog"]["actions"][0]["elseActions"], original["beginDialog"]["actions"])

    def test_no_generation_into_repository_and_no_overwriting_private_files(self):
        with self.assertRaises(ValueError):
            setup.private_path(ROOT / "secret-output.json")
        with private_test_directory() as directory:
            path = Path(directory) / "private.env"
            setup.write_private(path, 'DYNAMICS_NATIVE_MEDIA_API_KEY="' + KEY + '"\n')
            self.assertEqual(setup.secret_value(path, "DYNAMICS_NATIVE_MEDIA_API_KEY"), KEY)
            with self.assertRaises(FileExistsError):
                setup.write_private(path, "overwrite")
            if os.name != "nt":
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                path.chmod(0o644)
                with self.assertRaises(ValueError):
                    setup.read_private(path)

    def test_observer_parameters_have_explicit_expiry_and_private_key(self):
        with private_test_directory() as directory:
            secret = Path(directory) / "secrets.env"
            output = Path(directory) / "observer.json"
            setup.write_private(secret, 'DYNAMICS_NATIVE_PROBE_API_KEY="' + KEY + '"\n')
            args = SimpleNamespace(
                voice_origin=ORIGIN, system_topic_name="native-events", expires_at="2099-01-01T12:00:00Z", permanent=False,
                secrets_file=secret, output=output,
            )
            setup.observer_parameters(args)
            doc = json.loads(output.read_text())
            self.assertEqual(doc["parameters"]["apiKey"]["value"], KEY)
            self.assertFalse(doc["parameters"]["permanent"]["value"])
            self.assertEqual(doc["parameters"]["webhookUrl"]["value"], ORIGIN + "/dynamics/handover/probe/events")
            args.expires_at = "2000-01-01T00:00:00Z"
            with self.assertRaises(ValueError):
                setup.observer_parameters(args)

    def test_permanent_observer_parameters_have_no_expiry(self):
        with private_test_directory() as directory:
            secret, output = Path(directory) / "keys.env", Path(directory) / "observer.json"
            setup.write_private(secret, setup.env_file({"DYNAMICS_NATIVE_PROBE_API_KEY": KEY}))
            setup.observer_parameters(SimpleNamespace(
                voice_origin=ORIGIN, system_topic_name="native-events", expires_at=None, permanent=True,
                secrets_file=secret, output=output,
            ))
            parameters = json.loads(output.read_text())["parameters"]
            self.assertTrue(parameters["permanent"]["value"])
            self.assertEqual(parameters["expiresAt"]["value"], "")
        with self.assertRaisesRegex(ValueError, "must not also specify"):
            setup.observer_expiry("2099-01-01T00:00:00Z", True)
        with self.assertRaisesRegex(ValueError, "Choose a future"):
            setup.observer_expiry(None, False)

    def test_bootstrap_never_applies_partial_observer_credentials(self):
        with private_test_directory() as directory:
            keys, output = Path(directory) / "keys.env", Path(directory) / "bootstrap.env"
            setup.write_private(keys, setup.env_file({
                "INGEST_API_KEY": KEY, "DYNAMICS_NATIVE_PROBE_API_KEY": KEY,
                "DYNAMICS_NATIVE_MEDIA_API_KEY": KEY, "HUMAIN_AUTHORIZATION": "Synthetic credential",
            }))
            setup.bootstrap_env(SimpleNamespace(secrets_file=keys, output=output))
            values = setup.dotenv_values(output, interpolate=False)
            self.assertEqual(values["HUMAIN_LLM_PROVIDER"], "allam")
            self.assertEqual(values["HUMAIN_INTERRUPTION_POLICY_MODE"], "off")
            self.assertEqual(values["HUMAIN_TURN_TAKING_STRATEGY"], "fast_vad")
            self.assertEqual(values["ACS_AUTO_CONFERENCE_ENABLED"], "false")
            self.assertNotIn("DYNAMICS_NATIVE_PROBE_API_KEY", values)
            self.assertNotIn("DYNAMICS_NATIVE_MEDIA_API_KEY", values)

    def test_runtime_file_uses_native_resource_keys_and_rejects_a_different_tenant(self):
        with private_test_directory() as directory:
            keys, output = Path(directory) / "keys.env", Path(directory) / "native.env"
            setup.write_private(keys, setup.env_file({"DYNAMICS_NATIVE_MEDIA_API_KEY": KEY, "DYNAMICS_NATIVE_PROBE_API_KEY": KEY}))
            args = SimpleNamespace(
                tenant_id=BOT, org_url="https://example.crm17.dynamics.com",
                workstream_id=WORKSTREAM, additional_workstream_id=[], native_acs_resource_id=f"/subscriptions/{BOT}/resourceGroups/native/providers/Microsoft.Communication/communicationServices/native-acs",
                app_number="+12025550101", expires_at="2099-01-01T00:00:00Z", permanent=False, fast_start=True,
                secrets_file=keys, output=output,
            )
            connection = "endpoint=https://native.example;accesskey=synthetic"
            with patch.object(setup.subprocess, "run", side_effect=[
                SimpleNamespace(stdout=json.dumps({"tenantId": BOT})),
                SimpleNamespace(stdout=json.dumps({"primaryConnectionString": connection})),
            ]) as run:
                setup.runtime_env(args)
            values = setup.dotenv_values(output, interpolate=False)
            self.assertEqual(values["DYNAMICS_NATIVE_MEDIA_ACS_CONNECTION_STRING"], connection)
            self.assertEqual(values["DYNAMICS_NATIVE_PROBE_RESOURCE_ID"], args.native_acs_resource_id)
            self.assertEqual(values["DYNAMICS_NATIVE_MEDIA_TRANSPORT"], "voip")
            self.assertEqual(values["DYNAMICS_NATIVE_MEDIA_FAST_START"], "true")
            self.assertEqual(values["DYNAMICS_NATIVE_PROBE_PERMANENT"], "false")
            self.assertTrue(all(connection not in str(call) for call in run.call_args_list))
            args.output = Path(directory) / "other.env"
            with patch.object(setup.subprocess, "run", return_value=SimpleNamespace(stdout=json.dumps({"tenantId": CALL}))) as run:
                with self.assertRaisesRegex(ValueError, "not in the Dynamics tenant"):
                    setup.runtime_env(args)
                run.assert_called_once()

    def test_runtime_exports_permanent_observation_and_additional_entry(self):
        with private_test_directory() as directory:
            keys, output = Path(directory) / "keys.env", Path(directory) / "native.env"
            setup.write_private(keys, setup.env_file({"DYNAMICS_NATIVE_MEDIA_API_KEY": KEY, "DYNAMICS_NATIVE_PROBE_API_KEY": KEY}))
            args = SimpleNamespace(
                tenant_id=BOT, org_url="https://example.crm17.dynamics.com", workstream_id=WORKSTREAM,
                additional_workstream_id=[CALL],
                native_acs_resource_id=f"/subscriptions/{BOT}/resourceGroups/native/providers/Microsoft.Communication/communicationServices/native-acs",
                app_number="", expires_at=None, permanent=True, fast_start=True,
                secrets_file=keys, output=output,
            )
            with patch.object(setup.subprocess, "run", side_effect=[
                SimpleNamespace(stdout=json.dumps({"tenantId": BOT})),
                SimpleNamespace(stdout=json.dumps({"primaryConnectionString": "endpoint=https://native.example;accesskey=synthetic"})),
            ]):
                setup.runtime_env(args)
            values = setup.dotenv_values(output, interpolate=False)
            self.assertEqual(values["DYNAMICS_NATIVE_PROBE_PERMANENT"], "true")
            self.assertEqual(values["DYNAMICS_NATIVE_PROBE_EXPIRES_AT"], "")
            self.assertNotIn("ACS_SOURCE_NUMBER", values)
            self.assertEqual(values["DYNAMICS_NATIVE_MEDIA_ADDITIONAL_WORKSTREAM_IDS"], CALL)
            args.additional_workstream_id = [WORKSTREAM]
            with patch.object(setup.subprocess, "run") as run:
                with self.assertRaisesRegex(ValueError, "must be distinct"):
                    setup.runtime_env(args)
                run.assert_not_called()


class CopySheetTests(unittest.TestCase):
    RESOURCE = f"/subscriptions/{BOT}/resourceGroups/native/providers/Microsoft.Communication/communicationServices/native-acs"
    SECRET = "endpoint=https://example.communication.azure.com/;accesskey=synthetic-unit-test-value"

    def args(self, output):
        return SimpleNamespace(tenant_id=BOT, native_acs_resource_id=self.RESOURCE, event_grid_app_id=QUEUE, output=output)

    def test_sheet_is_private_and_the_secret_is_never_printed(self):
        with private_test_directory() as directory:
            output = Path(directory) / "copy-sheet.txt"
            with patch.object(setup.subprocess, "run", side_effect=[
                SimpleNamespace(stdout=json.dumps({"tenantId": BOT.upper()})),
                SimpleNamespace(stdout=json.dumps({"primaryConnectionString": self.SECRET})),
            ]) as run, patch("sys.stdout", new_callable=io.StringIO) as printed:
                setup.copy_sheet(self.args(output))
            self.assertEqual(output.stat().st_mode & 0o777, 0o600)
            sheet = output.read_text()
            for line in ("ACS Resource Name: native-acs", f"ACS Resource ID: {self.RESOURCE}",
                         f"Connection string: {self.SECRET}", f"Event grid app id: {QUEUE}", BOT):
                self.assertIn(line, sheet)
            self.assertNotIn("synthetic-unit-test-value", printed.getvalue())
            self.assertFalse(json.loads(printed.getvalue())["connection_string_printed"])
            self.assertIn("listKeys", run.call_args_list[1].args[0][-4])
            with self.assertRaisesRegex(ValueError, "new copy-sheet filename"):
                setup.copy_sheet(self.args(output))

    def test_foreign_tenant_or_checkout_path_reads_no_key(self):
        with private_test_directory() as directory:
            with patch.object(setup.subprocess, "run", return_value=SimpleNamespace(stdout=json.dumps({"tenantId": CALL}))) as run:
                with self.assertRaisesRegex(ValueError, "not in the Dynamics tenant"):
                    setup.copy_sheet(self.args(Path(directory) / "copy-sheet.txt"))
            run.assert_called_once()
            self.assertFalse((Path(directory) / "copy-sheet.txt").exists())
            with patch.object(setup.subprocess, "run") as run, self.assertRaisesRegex(ValueError, "outside the repository"):
                setup.copy_sheet(self.args(setup.topics.ROOT / "copy-sheet.txt"))
            run.assert_not_called()


class StagingTests(unittest.TestCase):
    def bundle(self, directory):
        source = records()
        output = setup.bundle_documents(source, ORIGIN, KEY, ["+12025550101"])
        changes = []
        for kind, name in (("controller", "NativeSpeechTurnPilot"), ("context", "NativeMediaContext"), ("entry", "Conversation Start")):
            setup.write_private(directory / setup.BUNDLE_FILES[kind], output[kind])
            changes.append({
                "name": name, "id": source[name]["botcomponentid"], "etag": source[name]["@odata.etag"],
                "file": setup.BUNDLE_FILES[kind], "sha256": setup.digest(output[kind]),
            })
        manifest = {"version": 1, "org_url": "https://example.crm17.dynamics.com", "tenant_id": BOT, "bot_id": BOT, "updates": changes}
        setup.write_private(directory / "bundle.json", json.dumps(manifest))
        setup.write_private(directory / "before.json", json.dumps({"records": source, "publishedon": None}))
        return source

    def test_dry_run_does_not_write_and_apply_is_idempotent(self):
        with private_test_directory() as directory:
            path = Path(directory)
            state = self.bundle(path)
            api = MagicMock()
            api.get.side_effect = lambda target, query: {"publishedon": None} if target.startswith("bots(") else {
                "data": next(row for row in state.values() if row["botcomponentid"] in target)["data"]
            }

            def patch_many(updates):
                for row in updates:
                    state[row["name"]]["data"] = row["data"]
                    state[row["name"]]["@odata.etag"] = 'W/"101"'

            api.patch_many.side_effect = patch_many
            with patch.object(setup, "Dataverse", return_value=api), patch.object(setup, "components", side_effect=lambda *args, **kwargs: state):
                setup.stage_bundle(SimpleNamespace(bundle_dir=path, apply=False))
                api.patch_many.assert_not_called()
                setup.stage_bundle(SimpleNamespace(bundle_dir=path, apply=True))
                api.patch_many.assert_called_once()
                setup.stage_bundle(SimpleNamespace(bundle_dir=path, apply=True))
                api.patch_many.assert_called_once()

    def test_stale_draft_and_modified_generated_yaml_are_refused(self):
        with private_test_directory() as directory:
            path = Path(directory)
            state = self.bundle(path)
            state["Conversation Start"]["@odata.etag"] = 'W/"999"'
            api = MagicMock()
            api.get.return_value = {"publishedon": None}
            with patch.object(setup, "Dataverse", return_value=api), patch.object(setup, "components", return_value=state):
                with self.assertRaisesRegex(ValueError, "changed since export"):
                    setup.stage_bundle(SimpleNamespace(bundle_dir=path, apply=True))
            api.patch_many.assert_not_called()
            with (path / setup.BUNDLE_FILES["controller"]).open("a") as output:
                output.write("\n# manual edit\n")
            with self.assertRaisesRegex(ValueError, "changed after export"):
                setup.load_bundle(path)

    def test_http_changeset_is_atomic_and_conditional(self):
        api = object.__new__(setup.Dataverse)
        api.org = "https://example.crm17.dynamics.com"
        api.request = MagicMock(return_value="HTTP/1.1 204 No Content\r\nHTTP/1.1 204 No Content\r\n")
        updates = [{"id": BOT, "etag": 'W/"10"', "data": "kind: AdaptiveDialog"}, {"id": WORKSTREAM, "etag": 'W/"11"', "data": "kind: AdaptiveDialog"}]
        api.patch_many(updates)
        args = api.request.call_args
        payload = args.kwargs["body"].decode()
        self.assertEqual(args.args[0], "$batch")
        self.assertEqual(payload.count("If-Match:"), 2)
        self.assertEqual(payload.count("Content-Type: multipart/mixed; boundary=changeset_"), 1)
        self.assertNotIn("publish", payload.lower())
        api.request.return_value = "HTTP/1.1 412 Precondition Failed"
        with self.assertRaises(RuntimeError):
            api.patch_many(updates)


class StartupAudioTests(unittest.TestCase):
    VOICE = "55555555-5555-4555-8555-555555555555"
    WAIT = "88888888-8888-4888-8888-888888888888"
    HOLD = "99999999-9999-4999-8999-999999999999"
    FLAGS = {
        "msdyn_skipwaitmusicforivr": True,
        "msdyn_playrecordingtranscriptionnotifications": False,
    }

    def setUp(self):
        manager = private_test_directory()
        self.root = manager.__enter__()
        self.addCleanup(manager.__exit__, None, None, None)
        self.directory = self.root / "startup-audio"
        self.args = SimpleNamespace(
            org_url="https://example.crm17.dynamics.com", tenant_id=BOT, workstream_id=WORKSTREAM,
            output_dir=self.directory, apply=False, confirm_organization=None, confirm_notice_policy=False,
        )
        self.workstream = {
            "msdyn_liveworkstreamid": WORKSTREAM, "statecode": 0, "msdyn_direction": 0,
            "msdyn_streamsource": 192440000, "_msdyn_defaultqueue_value": QUEUE, "_msdyn_bot_user_value": BOT,
            "msdyn_name": "Synthetic private workstream", "msdyn_capacityrequired": 1, "@odata.etag": 'W/"10"',
        }
        self.voice = {
            "msdyn_ocvoicechannelsettingid": self.VOICE, "_msdyn_liveworkstreamid_value": WORKSTREAM,
            "statecode": 0, "statuscode": 1, "msdyn_skipwaitmusicforivr": False,
            "msdyn_playrecordingtranscriptionnotifications": True,
            "msdyn_recordingenabled": True, "msdyn_requestuserconsentforrecording": True,
            "msdyn_transcriptionenabled": True, "_ownerid_value": BOT,
            "modifiedon": "2026-10-01T10:00:00Z", "versionnumber": 20, "_modifiedby_value": BOT,
            "@odata.etag": 'W/"20"',
        }
        self.voices = [self.voice]
        self.languages = [{
            "msdyn_ocvoicechannellanguagesettingid": f"00000000-0000-4000-8000-{index:012d}",
            "_msdyn_ocvoicechannelsettingid_value": self.VOICE, "_msdyn_languageid_value": identifier,
            "_msdyn_waitmusicid_value": self.WAIT, "_msdyn_holdmusicid_value": self.HOLD,
            "msdyn_isprimarylanguage": index == 1, "statecode": 0, "@odata.etag": f'W/"{30 + index}"',
        } for index, identifier in enumerate((BOT, CALL), 1)]
        self.music = {
            key: {
                "msdyn_ocphonemusicid": key, "msdyn_name": name,
                "msdyn_musicfile": identifier, "msdyn_musicfile_name": name + ".wav", "@odata.etag": f'W/"{index}"',
            } for index, (key, name, identifier) in enumerate(((self.WAIT, "native-wait", BOT), (self.HOLD, "native-hold", CALL)), 40)
        }
        self.open_calls = []
        self.api = MagicMock(org=self.args.org_url, tenant=BOT)
        self.api.get.side_effect = self.get_record
        self.api.rows.side_effect = self.get_rows
        self.api.request.side_effect = self.patch_voice

    def get_record(self, path, query=None):
        self.assertIsNone(query)
        if path == f"msdyn_liveworkstreams({WORKSTREAM})":
            return copy.deepcopy(self.workstream)
        for key, row in self.music.items():
            if path == f"msdyn_ocphonemusics({key})":
                return copy.deepcopy(row)
        self.fail("Unexpected native read: " + path)

    def get_rows(self, table, query):
        if table == "msdyn_ocvoicechannelsettings":
            self.assertEqual(query, {"$filter": f"_msdyn_liveworkstreamid_value eq {WORKSTREAM}", "$top": "2"})
            return copy.deepcopy(self.voices)
        if table == "msdyn_ocvoicechannellanguagesettings":
            self.assertEqual(query, {"$filter": f"_msdyn_ocvoicechannelsettingid_value eq {self.VOICE}"})
            return copy.deepcopy(self.languages)
        if table == "msdyn_ocliveworkitems":
            self.assertEqual(query, {
                "$select": "msdyn_ocliveworkitemid", "$top": "1",
                "$filter": f"_msdyn_liveworkstreamid_value eq {WORKSTREAM} and statuscode ne 4",
            })
            return copy.deepcopy(self.open_calls)
        self.fail("Unexpected native table: " + table)

    def patch_voice(self, path, *, method, body, headers):
        self.assertEqual(path, f"msdyn_ocvoicechannelsettings({self.VOICE})")
        self.assertEqual(method, "PATCH")
        self.assertEqual(body, self.FLAGS)
        self.assertEqual(headers, {"If-Match": 'W/"20"'})
        self.assertTrue((self.directory / "intent.json").is_file())
        self.voice.update(body)
        self.voice.update({
            "@odata.etag": 'W/"21"', "modifiedon": "2026-11-02T08:00:00Z", "versionnumber": 21,
            "_modifiedby_value": CALL, "_modifiedonbehalfby_value": None,
        })
        return {}

    def invoke(self, *, apply=False):
        args = SimpleNamespace(**vars(self.args))
        args.apply = apply
        if apply:
            args.confirm_organization, args.confirm_notice_policy = args.org_url, True
        with patch("sys.stdout", new_callable=io.StringIO) as output:
            setup.startup_audio(args, self.api)
        return json.loads(output.getvalue())

    def test_parser_defaults_to_plan_and_dispatches_without_mutation_flags(self):
        argv = [
            "native_voice_setup.py", "startup-audio", "--tenant-id", BOT, "--org-url", self.args.org_url,
            "--workstream-id", WORKSTREAM, "--output-dir", str(self.directory),
        ]
        parsed = setup.parser().parse_args(argv[1:])
        self.assertFalse(parsed.apply)
        self.assertFalse(parsed.confirm_notice_policy)
        self.assertIsNone(parsed.confirm_organization)
        with patch.object(sys, "argv", argv), patch.object(setup, "Dataverse", return_value=self.api), patch.object(
            setup, "startup_audio",
        ) as handler:
            setup.main()
        handler.assert_called_once_with(parsed, self.api)
        self.api.request.assert_not_called()

    def test_plan_has_no_remote_writes_and_keeps_complete_private_before_images(self):
        before = copy.deepcopy(self.voice)
        result = self.invoke()
        self.assertEqual(
            {key: result[key] for key in ("plan_only", "configuration_verified", "native_changes", "calls_placed")},
            {"plan_only": True, "configuration_verified": False, "native_changes": 0, "calls_placed": 0},
        )
        self.assertFalse(result["runtime_audio_verified"])
        self.assertIsNone(result["not_before_call_acceptance"])
        self.assertNotIn("Synthetic private workstream", json.dumps(result))
        plan = json.loads((self.directory / "plan.json").read_text())
        self.assertEqual(plan["desired"], self.FLAGS)
        self.assertEqual(set(plan["changes"]), set(self.FLAGS))
        self.assertEqual(plan["before"]["voice"], before)
        self.assertEqual(plan["before"]["workstream"], self.workstream)
        self.assertEqual(plan["before"]["languages"], self.languages)
        self.assertEqual(plan["before"]["music"], self.music)
        self.assertEqual(self.voice, before)
        self.assertFalse((self.directory / "intent.json").exists())
        self.api.request.assert_not_called()
        self.api.patch_many.assert_not_called()
        if os.name != "nt":
            self.assertEqual(self.directory.stat().st_mode & 0o777, 0o700)
            for path in self.directory.iterdir():
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_apply_requires_both_human_confirmations_before_native_reads(self):
        self.invoke()
        self.api.reset_mock()
        for org, policy in ((None, False), (self.args.org_url, False), (None, True), ("https://other.crm17.dynamics.com", True)):
            args = SimpleNamespace(**{**vars(self.args), "apply": True, "confirm_organization": org, "confirm_notice_policy": policy})
            with self.subTest(org=org, policy=policy), self.assertRaisesRegex(ValueError, "requires --confirm-organization"):
                setup.startup_audio(args, self.api)
        self.api.get.assert_not_called()
        self.api.rows.assert_not_called()
        self.api.request.assert_not_called()
        self.assertFalse((self.directory / "intent.json").exists())

    def test_confirmations_without_apply_and_apply_without_saved_plan_are_refused(self):
        self.args.confirm_notice_policy = True
        with self.assertRaisesRegex(ValueError, "only with --apply"):
            self.invoke()
        self.args.confirm_notice_policy = False
        with self.assertRaisesRegex(ValueError, "without --apply first"):
            self.invoke(apply=True)
        self.api.get.assert_not_called()
        self.api.request.assert_not_called()

    def test_private_paths_permissions_and_symlinked_plan_are_enforced(self):
        self.args.output_dir = topics.ROOT / "not-private"
        with self.assertRaisesRegex(ValueError, "outside the repository"):
            self.invoke()
        self.args.output_dir = self.directory
        self.invoke()
        self.api.reset_mock()
        if os.name != "nt":
            self.directory.chmod(0o755)
            with self.assertRaisesRegex(ValueError, "owner-only"):
                self.invoke(apply=True)
            self.directory.chmod(0o700)
            (self.directory / "plan.json").chmod(0o644)
            with self.assertRaisesRegex(ValueError, "Restrict the input file"):
                self.invoke(apply=True)
            (self.directory / "plan.json").chmod(0o600)
        plan = self.directory / "plan.json"
        saved = self.root / "saved-plan.json"
        plan.rename(saved)
        plan.symlink_to(saved)
        with self.assertRaisesRegex(ValueError, "not symlinks"):
            self.invoke(apply=True)
        self.api.get.assert_not_called()
        self.api.request.assert_not_called()

    def test_wrong_workstream_or_authentication_scope_is_refused(self):
        original = copy.deepcopy(self.workstream)
        for field, value in (
            ("msdyn_liveworkstreamid", CALL), ("statecode", 1), ("msdyn_direction", 1),
            ("msdyn_streamsource", 1), ("_msdyn_defaultqueue_value", None), ("_msdyn_bot_user_value", None),
        ):
            with self.subTest(field=field):
                self.workstream.update(original)
                self.workstream[field] = value
                with self.assertRaises(ValueError):
                    self.invoke()
                self.assertFalse(self.directory.exists())
        self.workstream.update(original)
        self.api.tenant = CALL
        with self.assertRaisesRegex(ValueError, "organization and tenant"):
            self.invoke()
        self.api.tenant, self.api.org = BOT, "https://other.crm17.dynamics.com"
        with self.assertRaisesRegex(ValueError, "organization and tenant"):
            self.invoke()
        self.api.request.assert_not_called()

    def test_ambiguous_inactive_or_foreign_voice_rows_are_refused(self):
        for rows in ([], [self.voice, copy.deepcopy(self.voice)]):
            self.voices = rows
            with self.subTest(count=len(rows)), self.assertRaisesRegex(ValueError, "exactly one"):
                self.invoke()
        self.voices = [self.voice]
        self.voice["_msdyn_liveworkstreamid_value"] = CALL
        with self.assertRaisesRegex(ValueError, "another workstream"):
            self.invoke()
        self.voice["_msdyn_liveworkstreamid_value"], self.voice["statecode"] = WORKSTREAM, 1
        with self.assertRaisesRegex(ValueError, "active native voice-settings"):
            self.invoke()
        self.api.request.assert_not_called()

    def test_incomplete_booleans_etags_and_language_music_ownership_are_refused(self):
        original_voice, original_languages = copy.deepcopy(self.voice), copy.deepcopy(self.languages)
        for field, value in (
            ("msdyn_recordingenabled", None), ("msdyn_requestuserconsentforrecording", None),
            ("msdyn_skipwaitmusicforivr", 1), ("@odata.etag", "*"),
        ):
            self.voice.update(original_voice)
            self.voice[field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.invoke()
        self.voice.update(original_voice)
        for field, value in (
            ("_msdyn_ocvoicechannelsettingid_value", CALL),
            ("msdyn_ocvoicechannellanguagesettingid", self.languages[1]["msdyn_ocvoicechannellanguagesettingid"]),
        ):
            self.languages = copy.deepcopy(original_languages)
            self.languages[0][field] = value
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, "ambiguous or belong"):
                self.invoke()
        self.languages = copy.deepcopy(original_languages)
        del self.languages[0]["_msdyn_holdmusicid_value"]
        with self.assertRaisesRegex(ValueError, "omitted a wait/hold"):
            self.invoke()
        self.languages = copy.deepcopy(original_languages)
        self.music[self.HOLD]["msdyn_ocphonemusicid"] = CALL
        with self.assertRaisesRegex(ValueError, "music records"):
            self.invoke()
        self.api.request.assert_not_called()

    def test_open_calls_including_wrapup_block_only_writes_for_the_entire_workstream(self):
        self.open_calls = [{"msdyn_ocliveworkitemid": CALL, "statuscode": 3}]
        self.invoke()
        for status in (1, 2, 3, 5, 6):
            self.open_calls[0]["statuscode"] = status
            with self.subTest(status=status), self.assertRaisesRegex(ValueError, "all selected-workstream calls and Dynamics wrap-up"):
                self.invoke(apply=True)
        self.api.request.assert_not_called()
        self.assertFalse((self.directory / "intent.json").exists())

    def test_apply_patches_only_two_flags_and_preserves_recording_consent_routing_and_music(self):
        self.invoke()
        plan_bytes = (self.directory / "plan.json").read_bytes()
        before = json.loads(plan_bytes)["before"]
        result = self.invoke(apply=True)
        self.api.request.assert_called_once_with(
            f"msdyn_ocvoicechannelsettings({self.VOICE})", method="PATCH",
            body=self.FLAGS, headers={"If-Match": 'W/"20"'},
        )
        self.assertEqual(result["status"], "configured")
        self.assertFalse(result["plan_only"])
        self.assertTrue(result["configuration_verified"])
        self.assertEqual(result["native_changes"], 1)
        self.assertEqual(result["calls_placed"], 0)
        self.assertFalse(result["runtime_audio_verified"])
        self.assertEqual((self.directory / "plan.json").read_bytes(), plan_bytes)
        after = json.loads(Path(result["after_file"]).read_text())
        for key in ("workstream", "languages", "music"):
            self.assertEqual(after["after"][key], before[key])
        for field in ("msdyn_recordingenabled", "msdyn_requestuserconsentforrecording", "msdyn_transcriptionenabled", "_ownerid_value"):
            self.assertEqual(self.voice[field], before["voice"][field])
        intent = json.loads((self.directory / "intent.json").read_text())
        self.assertTrue(intent["notice_policy_reviewed"])
        self.assertEqual(intent["body"], self.FLAGS)
        self.assertEqual(intent["plan_sha256"], setup.startup_audio_hash(json.loads(plan_bytes)))
        self.assertEqual(after["intent_sha256"], setup.startup_audio_hash(intent))

    def test_partial_delta_keeps_the_exact_two_flag_payload_and_disabled_recording_unchanged(self):
        self.voice.update({"msdyn_skipwaitmusicforivr": True, "msdyn_recordingenabled": False, "msdyn_requestuserconsentforrecording": False})
        self.invoke()
        plan = json.loads((self.directory / "plan.json").read_text())
        self.assertEqual(set(plan["changes"]), {"msdyn_playrecordingtranscriptionnotifications"})
        self.invoke(apply=True)
        self.assertEqual(self.api.request.call_args.kwargs["body"], self.FLAGS)
        self.assertIs(self.voice["msdyn_recordingenabled"], False)
        self.assertIs(self.voice["msdyn_requestuserconsentforrecording"], False)

    def test_noop_is_truthful_idempotent_and_needs_no_idle_gate_or_patch(self):
        self.voice.update(self.FLAGS)
        self.open_calls = [{"msdyn_ocliveworkitemid": CALL, "statuscode": 3}]
        planned = self.invoke()
        original = {path.name: path.read_bytes() for path in self.directory.iterdir()}
        applied, repeated = self.invoke(apply=True), self.invoke(apply=True)
        for result in (planned, applied, repeated):
            self.assertEqual(result["status"], "already_configured")
            self.assertTrue(result["configuration_verified"])
            self.assertEqual(result["native_changes"], 0)
            self.assertEqual(result["planned_native_changes"], 0)
            self.assertEqual(result["calls_placed"], 0)
        self.assertEqual(len({result["after_file"] for result in (planned, applied, repeated)}), 3)
        for name, content in original.items():
            self.assertEqual((self.directory / name).read_bytes(), content)
        self.assertFalse((self.directory / "intent.json").exists())
        self.api.request.assert_not_called()
        self.assertNotIn("msdyn_ocliveworkitems", [call.args[0] for call in self.api.rows.call_args_list])

    def test_stale_etag_is_refused_without_rebasing_the_plan(self):
        self.invoke()
        before = (self.directory / "plan.json").read_bytes()
        self.voice["@odata.etag"] = 'W/"999"'
        with self.assertRaisesRegex(ValueError, "ETag changed since planning"):
            self.invoke(apply=True)
        self.assertEqual((self.directory / "plan.json").read_bytes(), before)
        self.assertFalse((self.directory / "intent.json").exists())
        self.api.request.assert_not_called()

    def test_all_unowned_field_drift_is_refused_even_when_etags_are_unchanged(self):
        self.invoke()
        for record, field, value in (
            (self.voice, "msdyn_recordingenabled", False),
            (self.voice, "msdyn_requestuserconsentforrecording", False),
            (self.voice, "msdyn_transcriptionenabled", 1),
            (self.voice, "_ownerid_value", CALL),
            (self.workstream, "_msdyn_defaultqueue_value", CALL),
            (self.workstream, "msdyn_capacityrequired", 2),
            (self.languages[0], "_msdyn_holdmusicid_value", self.WAIT),
            (self.languages[0], "_msdyn_waitmusicid_value", self.HOLD),
            (self.languages[0], "_msdyn_languageid_value", QUEUE),
            (self.languages[0], "msdyn_isprimarylanguage", False),
            (self.music[self.HOLD], "msdyn_musicfile", QUEUE),
            (self.music[self.WAIT], "@odata.etag", 'W/"999"'),
        ):
            original = record[field]
            record[field] = value
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, "changed since planning"):
                self.invoke(apply=True)
            record[field] = original
        self.api.request.assert_not_called()
        self.assertFalse((self.directory / "intent.json").exists())

    def test_external_change_to_desired_flags_is_not_claimed_without_an_owned_intent(self):
        self.invoke()
        self.voice.update(self.FLAGS)
        self.voice["@odata.etag"] = 'W/"21"'
        with self.assertRaisesRegex(ValueError, "changed since planning"):
            self.invoke(apply=True)
        self.api.request.assert_not_called()

    def test_not_before_is_exactly_900_seconds_after_verified_readback(self):
        self.invoke()
        fixed = datetime(2026, 11, 2, 8, 5, 9, 123456, tzinfo=timezone.utc)
        with patch.object(setup, "datetime", wraps=datetime) as clock:
            clock.now.return_value = fixed
            result = self.invoke(apply=True)
        self.assertEqual(datetime.fromisoformat(result["readback_at"].replace("Z", "+00:00")), fixed)
        self.assertEqual(datetime.fromisoformat(result["not_before_call_acceptance"].replace("Z", "+00:00")), fixed + timedelta(seconds=900))
        receipt = json.loads(Path(result["after_file"]).read_text())
        self.assertEqual(receipt["not_before_call_acceptance"], result["not_before_call_acceptance"])

    def test_server_audit_and_formatted_annotations_are_not_configuration_drift(self):
        self.voice["_modifiedby_value@Microsoft.Dynamics.CRM.lookuplogicalname"] = "systemuser"
        self.workstream["msdyn_streamsource@OData.Community.Display.V1.FormattedValue"] = "Voice"
        self.invoke()
        self.workstream["msdyn_streamsource@OData.Community.Display.V1.FormattedValue"] = "Voz"
        result = self.invoke(apply=True)
        self.assertTrue(result["configuration_verified"])
        self.assertEqual(self.voice["_modifiedby_value"], CALL)

    def test_readback_drift_leaves_an_immutable_failure_receipt_and_never_retries(self):
        self.invoke()

        def drift(path, **kwargs):
            self.patch_voice(path, **kwargs)
            self.voice["msdyn_requestuserconsentforrecording"] = False
            self.music[self.HOLD]["msdyn_musicfile"] = QUEUE

        self.api.request.side_effect = drift
        with self.assertRaisesRegex(RuntimeError, "readback differed"):
            self.invoke(apply=True)
        observations = [json.loads(path.read_text()) for path in self.directory.glob("after-*.json")]
        failed = next(row for row in observations if row["status"] == "readback_drift")
        self.assertFalse(failed["configuration_verified"])
        self.assertIsNone(failed["not_before_call_acceptance"])
        self.assertEqual(failed["native_changes"], 1)
        self.assertEqual(failed["after"]["music"][self.HOLD]["msdyn_musicfile"], QUEUE)
        with self.assertRaisesRegex(RuntimeError, "requires recovery; no PATCH was retried"):
            self.invoke(apply=True)
        self.api.request.assert_called_once()

    def test_uncertain_saved_write_reconciles_exact_owned_after_without_replay(self):
        self.invoke()
        before = (self.directory / "plan.json").read_bytes()

        def uncertain(path, **kwargs):
            self.patch_voice(path, **kwargs)
            raise TimeoutError("Synthetic private network details must not be printed")

        self.api.request.side_effect = uncertain
        with self.assertRaisesRegex(RuntimeError, "write is unconfirmed") as failed:
            self.invoke(apply=True)
        self.assertNotIn("private network", str(failed.exception))
        intent = (self.directory / "intent.json").read_bytes()
        result = self.invoke(apply=True)
        self.assertEqual(result["status"], "intent_readback_verified")
        self.assertTrue(result["configuration_verified"])
        self.assertEqual(result["native_changes"], 0)
        self.assertEqual((self.directory / "plan.json").read_bytes(), before)
        self.assertEqual((self.directory / "intent.json").read_bytes(), intent)
        self.api.request.assert_called_once()
        readonly = self.invoke()
        self.assertTrue(readonly["plan_only"])
        self.assertTrue(readonly["configuration_verified"])
        self.api.request.assert_called_once()

    def test_uncertain_unsaved_write_requires_recovery_not_a_blind_patch_retry(self):
        self.invoke()
        self.api.request.side_effect = TimeoutError("Synthetic timeout")
        with self.assertRaisesRegex(RuntimeError, "write is unconfirmed"):
            self.invoke(apply=True)
        for apply in (False, True):
            with self.subTest(apply=apply), self.assertRaisesRegex(RuntimeError, "requires recovery; no PATCH was retried"):
                self.invoke(apply=apply)
        self.api.request.assert_called_once()
        observations = [json.loads(path.read_text()) for path in self.directory.glob("after-*.json")]
        self.assertTrue(any(row["status"] == "pending_intent_matches_before" and not row["configuration_verified"] for row in observations))

    def test_http_412_is_recorded_and_later_unowned_desired_values_do_not_reconcile_it(self):
        self.invoke()
        self.api.request.side_effect = setup.urllib.error.HTTPError(self.args.org_url, 412, "private failure detail", None, None)
        with self.assertRaisesRegex(RuntimeError, "HTTP 412; no retry") as failed:
            self.invoke(apply=True)
        self.assertNotIn("private failure detail", str(failed.exception))
        rejection = json.loads((self.directory / "rejected.json").read_text())
        self.assertEqual(rejection["http_status"], 412)
        self.voice.update({**self.FLAGS, "@odata.etag": 'W/"21"'})
        with self.assertRaisesRegex(RuntimeError, "requires recovery"):
            self.invoke(apply=True)
        self.api.request.assert_called_once()

    def test_failed_postwrite_readback_is_reconciled_without_a_second_write(self):
        self.invoke()

        def read(path, query=None):
            if self.api.request.called:
                raise TimeoutError("Synthetic failed readback")
            return self.get_record(path, query)

        self.api.get.side_effect = read
        with self.assertRaisesRegex(RuntimeError, "readback is unconfirmed; no second PATCH"):
            self.invoke(apply=True)
        self.api.get.side_effect = self.get_record
        result = self.invoke(apply=True)
        self.assertTrue(result["configuration_verified"])
        self.assertEqual(result["native_changes"], 0)
        self.api.request.assert_called_once()

    def test_successful_apply_recheck_preserves_every_original_receipt(self):
        self.invoke()
        applied = self.invoke(apply=True)
        previous = {path.name: path.read_bytes() for path in self.directory.iterdir()}
        repeated = self.invoke(apply=True)
        self.assertEqual(repeated["native_changes"], 0)
        self.assertTrue(repeated["configuration_verified"])
        self.assertNotEqual(applied["after_file"], repeated["after_file"])
        self.api.request.assert_called_once()
        for name, content in previous.items():
            self.assertEqual((self.directory / name).read_bytes(), content)

    def test_modified_plan_scope_or_intent_body_is_never_used_for_writes(self):
        self.invoke()
        plan_path = self.directory / "plan.json"
        original = plan_path.read_text()
        changed = json.loads(original)
        changed["desired"]["msdyn_requestuserconsentforrecording"] = False
        plan_path.write_text(json.dumps(changed))
        with self.assertRaisesRegex(ValueError, "exact two-flag scope"):
            self.invoke(apply=True)
        self.api.request.assert_not_called()
        plan_path.write_text(original)
        self.api.request.side_effect = TimeoutError("Synthetic timeout")
        with self.assertRaises(RuntimeError):
            self.invoke(apply=True)
        intent_path = self.directory / "intent.json"
        intent = json.loads(intent_path.read_text())
        intent["body"]["msdyn_requestuserconsentforrecording"] = False
        intent_path.write_text(json.dumps(intent))
        with self.assertRaisesRegex(ValueError, "not owned by this exact immutable"):
            self.invoke(apply=True)
        self.api.request.assert_called_once()


class AgentBaselineTests(unittest.TestCase):
    def setUp(self):
        self.api = MagicMock(org="https://example.crm17.dynamics.com", tenant=BOT)
        self.bot = {
            "botid": BOT, "ismanaged": False, "statecode": 0, "language": 1033,
            "supportedlanguages": None, "authenticationmode": 2, "authenticationtrigger": 1,
            "accesscontrolpolicy": 2, "publishedon": "2026-10-01T12:00:00Z", "@odata.etag": 'W/"100"',
            "configuration": json.dumps({
                "settings": {"GenerativeActionsEnabled": True, "UnrelatedSetting": True},
                "aISettings": {
                    "useModelKnowledge": True, "isFileAnalysisEnabled": True, "isSemanticSearchEnabled": True,
                    "optInUseLatestModels": False,
                }, "recognizer": {"$kind": "GenerativeAIRecognizer"},
            }),
        }
        self.api.get.return_value = copy.deepcopy(self.bot)

    def args(self, output, **changes):
        return SimpleNamespace(
            bot_id=BOT, org_url=self.api.org, tenant_id=BOT, output_dir=output,
            **{"apply": False, "confirm_organization": None, **changes},
        )

    def preview(self, directory):
        args = self.args(directory / "agent-baseline")
        with patch("sys.stdout", new_callable=io.StringIO):
            setup.agent_baseline(args, self.api)
        return args, json.loads((args.output_dir / "plan.json").read_text())

    def test_preview_uses_public_auth_values_and_preserves_unrelated_configuration(self):
        with private_test_directory() as directory:
            args, plan = self.preview(directory)
            self.assertEqual(plan["desired"]["authenticationmode"], 1)
            self.assertEqual(plan["desired"]["authenticationtrigger"], 0)
            self.assertEqual(plan["desired"]["accesscontrolpolicy"], 0)
            self.assertEqual(plan["desired"]["supportedlanguages"], "1025")
            configuration = json.loads(plan["desired"]["configuration"])
            self.assertFalse(configuration["settings"]["GenerativeActionsEnabled"])
            self.assertTrue(configuration["settings"]["UnrelatedSetting"])
            self.assertEqual(configuration["recognizer"], {"$kind": "PVARecognizer"})
            self.assertEqual(plan["if_match"], 'W/"100"')
        self.api.request.assert_not_called()

    def test_apply_requires_original_preview_and_exact_organization(self):
        with private_test_directory() as directory:
            args = self.args(directory / "agent", apply=True)
            with self.assertRaisesRegex(ValueError, "confirm-organization"):
                setup.agent_baseline(args, self.api)
            args.confirm_organization = self.api.org
            with self.assertRaisesRegex(ValueError, "Preview"):
                setup.agent_baseline(args, self.api)
        self.api.request.assert_not_called()

    def test_exact_conditional_apply_persists_intent_and_reconciles_without_second_patch(self):
        with private_test_directory() as directory:
            args, plan = self.preview(directory)
            args.apply, args.confirm_organization = True, self.api.org
            expected = {**self.bot, **plan["desired"], "@odata.etag": 'W/"101"', "modifiedon": "2026-10-02"}

            def mutate(path, **request):
                self.assertTrue((args.output_dir / "intent.json").exists())
                self.assertEqual(request["headers"], {"If-Match": 'W/"100"'})
                self.assertEqual(request["body"], plan["desired"])
                self.api.get.return_value = expected

            self.api.request.side_effect = mutate
            with patch("sys.stdout", new_callable=io.StringIO) as output:
                setup.agent_baseline(args, self.api)
                setup.agent_baseline(args, self.api)
            self.api.request.assert_called_once()
            receipts = [json.loads(path.read_text()) for path in args.output_dir.glob("after-*.json")]
            self.assertEqual({row["status"] for row in receipts}, {"verified", "reconciled"})
            self.assertTrue(all(row["voice_channel_ready"] is False for row in receipts))
            self.assertNotIn("UnrelatedSetting", output.getvalue())

    def test_pending_intent_with_unconfirmed_state_never_retries(self):
        with private_test_directory() as directory:
            args, plan = self.preview(directory)
            args.apply, args.confirm_organization = True, self.api.org
            (args.output_dir / "intent.json").write_text(json.dumps({
                "plan_hash": setup.digest(json.dumps(plan, sort_keys=True, separators=(",", ":"))),
            }))
            (args.output_dir / "intent.json").chmod(0o600)
            with self.assertRaisesRegex(RuntimeError, "do not retry"):
                setup.agent_baseline(args, self.api)
        self.api.request.assert_not_called()

    def test_tampered_plan_or_foreign_target_does_not_mutate(self):
        with private_test_directory() as directory:
            args, plan = self.preview(directory)
            args.apply, args.confirm_organization = True, self.api.org
            plan["desired"]["authenticationmode"] = 3
            (args.output_dir / "plan.json").write_text(json.dumps(plan))
            with self.assertRaisesRegex(ValueError, "plan or target differs"):
                setup.agent_baseline(args, self.api)
        self.api.request.assert_not_called()

    def test_changed_etag_stops_before_conditional_patch(self):
        with private_test_directory() as directory:
            args, _ = self.preview(directory)
            args.apply, args.confirm_organization = True, self.api.org
            self.api.get.return_value = {**self.bot, "@odata.etag": 'W/"101"'}
            with self.assertRaisesRegex(ValueError, "changed since preview"):
                setup.agent_baseline(args, self.api)
        self.api.request.assert_not_called()

    def test_concurrent_publication_is_not_accepted_as_our_success(self):
        with private_test_directory() as directory:
            args, plan = self.preview(directory)
            args.apply, args.confirm_organization = True, self.api.org
            self.api.request.side_effect = lambda *a, **k: setattr(
                self.api.get, "return_value", {**self.bot, **plan["desired"], "publishedon": "2026-10-03"},
            )
            with self.assertRaisesRegex(RuntimeError, "not confirmed"):
                setup.agent_baseline(args, self.api)
            self.assertEqual(list(args.output_dir.glob("after-*.json")), [])
        self.api.request.assert_called_once()

    def test_managed_foreign_language_or_custom_auth_agent_is_preserved(self):
        for change in (
            {"ismanaged": True}, {"language": 1025}, {"supportedlanguages": "1031"},
            {"authenticationmode": 3}, {"accesscontrolpolicy": 3}, {"botid": CALL},
        ):
            with self.subTest(change=change), self.assertRaises(ValueError):
                setup.agent_baseline_plan({**self.bot, **change}, self.api.org, BOT, BOT)
        self.api.request.assert_not_called()

    def test_voice_saved_without_recognizer_is_not_given_one_again(self):
        configuration = json.loads(self.bot["configuration"])
        configuration.pop("recognizer")
        configuration["isTelephonyEnabled"] = True
        plan = setup.agent_baseline_plan({**self.bot, "configuration": json.dumps(configuration)}, self.api.org, BOT, BOT)
        desired = json.loads(plan["desired"]["configuration"])
        self.assertNotIn("recognizer", desired)
        self.assertFalse(desired["settings"]["GenerativeActionsEnabled"])
        configuration.pop("isTelephonyEnabled")
        with self.assertRaisesRegex(ValueError, "supported agent configuration"):
            setup.agent_baseline_plan({**self.bot, "configuration": json.dumps(configuration)}, self.api.org, BOT, BOT)

    def test_malformed_configuration_or_coerced_metadata_is_rejected(self):
        for change in (
            {"configuration": "[]"}, {"@odata.etag": None}, {"language": True},
            {"authenticationmode": True}, {"authenticationtrigger": False}, {"statecode": False},
        ):
            with self.subTest(change=change), self.assertRaises(ValueError):
                setup.agent_baseline_plan({**self.bot, **change}, self.api.org, BOT, BOT)
        self.api.request.assert_not_called()


class ContextVariableTests(unittest.TestCase):
    def setUp(self):
        self.api = MagicMock(org="https://example.crm17.dynamics.com", tenant=BOT)
        self.bot = {"botid": BOT, "schemaname": "sample_agent", "language": 1033, "publishedon": None}
        self.api.get.return_value = self.bot
        self.api.rows.return_value = []
        self.args = SimpleNamespace(
            bot_id=BOT, org_url=self.api.org, apply=False, confirm_organization=None, receipt_file=None,
        )

    def test_preview_uses_actual_bot_schema_and_never_mutates(self):
        with patch("sys.stdout", new_callable=io.StringIO) as output:
            setup.context_variable(self.args, self.api)
        result = json.loads(output.getvalue())
        self.assertEqual(result["schemaname"], "sample_agent.GlobalVariableComponent.msdyn_ConversationId")
        self.assertEqual(result["definition"], {
            "kind": "Variable", "name": "msdyn_ConversationId", "scope": "Conversation",
            "isExternalInitializationAllowed": True, "isOutputToExternalCallers": False,
            "initializationTimeoutInMilliseconds": 0,
        })
        self.assertFalse(result["remote_changes"])
        self.api.request.assert_not_called()

    def test_apply_requires_exact_organization_before_read_or_write(self):
        self.args.apply = True
        for value in (None, "https://another.crm17.dynamics.com"):
            self.args.confirm_organization = value
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "confirm-organization"):
                setup.context_variable(self.args, self.api)
        self.api.get.assert_not_called()
        self.api.request.assert_not_called()

    def test_existing_input_global_is_verified_without_overwrite_or_ownership_claim(self):
        record = {**records()[setup.VARIABLE], "_parentbotid_value": BOT}
        self.api.rows.return_value = [record]
        with patch("sys.stdout", new_callable=io.StringIO) as output:
            setup.context_variable(self.args, self.api)
        self.assertEqual(json.loads(output.getvalue())["status"], "already_configured")
        self.api.request.assert_not_called()

    def test_foreign_ambiguous_or_output_global_is_preserved(self):
        record = {**records()[setup.VARIABLE], "_parentbotid_value": BOT}
        invalid = copy.deepcopy(record)
        definition = topics.load_topic(invalid["data"])
        definition["isOutputToExternalCallers"] = True
        invalid["data"] = setup.dump(definition)
        for rows in (
            [record, record], [{**record, "_parentbotid_value": CALL}],
            [{**record, "schemaname": "other.GlobalVariableComponent.msdyn_ConversationId"}],
            [{**record, "componenttype": 9}], [invalid],
        ):
            self.api.rows.return_value = rows
            with self.subTest(rows=len(rows)), self.assertRaises(ValueError):
                setup.context_variable(self.args, self.api)
        self.api.request.assert_not_called()

    def test_foreign_bot_or_invalid_namespace_never_creates_a_global(self):
        for change in ({"botid": CALL}, {"schemaname": "another.agent"}, {"language": 1025}):
            self.api.get.return_value = {**self.bot, **change}
            with self.subTest(change=change), self.assertRaises(ValueError):
                setup.context_variable(self.args, self.api)
        self.api.request.assert_not_called()

    def test_apply_persists_intent_and_creates_only_exact_input_global(self):
        definition = {
            "kind": "Variable", "name": "msdyn_ConversationId", "scope": "Conversation",
            "isExternalInitializationAllowed": True, "isOutputToExternalCallers": False,
            "initializationTimeoutInMilliseconds": 0,
        }
        saved = {
            **records()[setup.VARIABLE], "botcomponentid": CALL, "_parentbotid_value": BOT,
            "language": 1033, "data": setup.dump(definition),
        }
        self.api.rows.side_effect = [[], [saved]]
        self.api.get.side_effect = [self.bot, {"publishedon": None}, saved, {"publishedon": None}]
        self.args.apply = True
        self.args.confirm_organization = self.api.org
        self.args.receipt_file = Path.cwd().parent / "synthetic-context-receipt.json"
        sequence = []
        self.api.request.side_effect = lambda *args, **kwargs: sequence.append("post")
        opened = mock_open()
        with patch.object(setup, "uuid4", return_value=UUID(CALL)), patch.object(
            setup, "write_private", side_effect=lambda *args: sequence.append("receipt"),
        ) as receipt, patch.object(Path, "open", opened), patch("sys.stdout", new_callable=io.StringIO) as output:
            setup.context_variable(self.args, self.api)
        self.assertEqual(sequence, ["receipt", "post"])
        self.api.request.assert_called_once()
        body = self.api.request.call_args.kwargs["body"]
        self.assertEqual(body["name"], setup.VARIABLE)
        self.assertEqual(body["componenttype"], 12)
        self.assertEqual(body["parentbotid@odata.bind"], f"/bots({BOT})")
        self.assertEqual(body["language"], 1033)
        self.assertEqual(topics.load_topic(body["data"]), definition)
        self.assertEqual(json.loads(receipt.call_args.args[1])["status"], "creation_pending")
        self.assertFalse(json.loads(output.getvalue())["published"])

    def test_receipt_failure_prevents_context_creation(self):
        self.args.apply = True
        self.args.confirm_organization = self.api.org
        self.args.receipt_file = Path.cwd().parent / "existing-context-receipt.json"
        with patch.object(setup, "write_private", side_effect=FileExistsError), self.assertRaises(FileExistsError):
            setup.context_variable(self.args, self.api)
        self.api.request.assert_not_called()

    def test_missing_receipt_prevents_context_creation(self):
        self.args.apply = True
        self.args.confirm_organization = self.api.org
        with self.assertRaisesRegex(ValueError, "receipt"):
            setup.context_variable(self.args, self.api)
        self.api.request.assert_not_called()

    def test_context_receipt_must_be_outside_the_checkout(self):
        self.args.apply = True
        self.args.confirm_organization = self.api.org
        self.args.receipt_file = ROOT / "synthetic-context-receipt.json"
        with self.assertRaisesRegex(ValueError, "outside the repository"):
            setup.context_variable(self.args, self.api)
        self.api.get.assert_not_called()
        self.api.request.assert_not_called()


class CaseVariableTests(unittest.TestCase):
    def setUp(self):
        self.source = records()
        self.api = MagicMock(org="https://example.crm17.dynamics.com", tenant=BOT)
        self.api.rows.return_value = [self.source[setup.VARIABLE]]
        self.api.get.return_value = {"publishedon": None}
        self.args = SimpleNamespace(bot_id=BOT, apply=False, receipt_file=None)

    def test_dry_run_verifies_standard_schema_without_modifying_any_component(self):
        with patch("sys.stdout", new_callable=io.StringIO) as output:
            setup.case_context_variable(self.args, self.api)
        result = json.loads(output.getvalue())
        self.assertEqual(result["componenttype"], 12)
        self.assertEqual(result["schemaname"], "sample_agent.GlobalVariableComponent.CaseNumber")
        self.assertEqual(result["definition"], {
            "kind": "Variable", "name": "CaseNumber", "scope": "Conversation",
            "isExternalInitializationAllowed": False, "isOutputToExternalCallers": True,
        })
        self.assertFalse(result["remote_changes"])
        self.api.request.assert_not_called()
        self.api.get.assert_not_called()

    def test_existing_output_global_is_preserved_and_private_extra_fields_are_not_printed(self):
        case = records(crm_case_context=True)[setup.CASE_VARIABLE]
        data = topics.load_topic(case["data"])
        data["description"] = "synthetic-private-variable-description"
        case["data"] = setup.dump(data)
        self.api.rows.return_value.append(case)
        self.args.apply = True
        with patch("sys.stdout", new_callable=io.StringIO) as output:
            setup.case_context_variable(self.args, self.api)
        self.assertEqual(json.loads(output.getvalue())["component_id"], case["botcomponentid"])
        self.assertNotIn("synthetic-private-variable-description", output.getvalue())
        self.api.request.assert_not_called()

    def test_ambiguous_or_nonstandard_globals_are_never_replaced(self):
        case = records(crm_case_context=True)[setup.CASE_VARIABLE]
        for rows in (
            [], [self.source[setup.VARIABLE]] * 2,
            [self.source[setup.VARIABLE], {**case, "name": "casenumber"}],
            [self.source[setup.VARIABLE], {**case, "componenttype": 9}],
            [self.source[setup.VARIABLE], {**case, "data": setup.dump({
                "kind": "Variable", "name": "CaseNumber", "scope": "User",
                "isExternalInitializationAllowed": False, "isOutputToExternalCallers": True,
            })}],
        ):
            self.api.rows.return_value = rows
            with self.subTest(rows=len(rows)), self.assertRaises(ValueError):
                setup.case_context_variable(self.args, self.api)
            self.api.request.assert_not_called()

    def test_apply_creates_only_the_unused_global_with_readback_and_rollback_evidence(self):
        case = records(crm_case_context=True)[setup.CASE_VARIABLE]
        case.update(botcomponentid=CALL, _parentbotid_value=BOT)
        self.api.rows.side_effect = [[self.source[setup.VARIABLE]], [self.source[setup.VARIABLE], case]]
        self.api.get.side_effect = [
            {"publishedon": "2026-10-01T12:00:00Z"}, case,
            {"publishedon": "2026-10-01T12:00:00Z"},
        ]
        self.args.apply = True
        self.args.receipt_file = Path("untracked/synthetic-case-rollback.json")
        sequence = []
        self.api.request.side_effect = lambda *args, **kwargs: sequence.append("post")
        opened = mock_open()
        with patch.object(setup, "uuid4", return_value=UUID(CALL)), patch.object(
            setup, "write_private", side_effect=lambda *args: sequence.append("receipt"),
        ) as receipt, patch.object(Path, "open", opened), patch("sys.stdout", new_callable=io.StringIO) as output:
            setup.case_context_variable(self.args, self.api)
        self.assertEqual(sequence, ["receipt", "post"])
        call = self.api.request.call_args
        self.assertEqual(call.args, ("botcomponents",))
        self.assertEqual(call.kwargs["method"], "POST")
        body = call.kwargs["body"]
        self.assertEqual(body["botcomponentid"], CALL)
        self.assertEqual(body["componenttype"], 12)
        self.assertEqual(body["parentbotid@odata.bind"], f"/bots({BOT})")
        self.assertNotIn("language", body)
        self.assertEqual(topics.load_topic(body["data"])["name"], "CaseNumber")
        self.assertNotIn("TransferConversation", body["data"])
        pending = json.loads(receipt.call_args.args[1])
        self.assertEqual(pending["status"], "creation_pending")
        self.assertIsNone(pending["previous_component"])
        saved = json.loads("".join(call.args[0] for call in opened().write.call_args_list))
        self.assertEqual(saved["rollback"]["path"], f"botcomponents({CALL})")
        self.assertEqual(saved["rollback"]["if_match"], case["@odata.etag"])
        result = json.loads(output.getvalue())
        self.assertFalse(result["published"])
        self.assertFalse(result["existing_components_modified"])
        self.api.request.assert_called_once()

    def test_apply_requires_a_new_receipt_before_any_remote_write(self):
        self.args.apply = True
        with self.assertRaisesRegex(ValueError, "receipt"):
            setup.case_context_variable(self.args, self.api)
        self.args.receipt_file = Path("untracked/synthetic-existing-receipt.json")
        with patch.object(setup, "write_private", side_effect=FileExistsError), self.assertRaises(FileExistsError):
            setup.case_context_variable(self.args, self.api)
        self.api.request.assert_not_called()


class CaseContextBundleTests(unittest.TestCase):
    def test_export_records_case_optin_and_preserves_the_global_as_a_prerequisite(self):
        source = records(crm_case_context=True)
        api = MagicMock(org="https://example.crm17.dynamics.com", tenant=BOT)
        api.get.return_value = {
            "language": 1033, "supportedlanguages": "1025", "publishedon": "2026-10-29T12:00:00Z",
        }
        destination = MagicMock()
        destination.exists.return_value = False
        destination.__truediv__.side_effect = lambda name: Path("untracked/not-created-case-bundle") / name
        args = SimpleNamespace(
            output_dir=Path("untracked/not-created-case-bundle"), bot_id=BOT, secrets_file=Path("not-opened.env"),
            voice_origin=ORIGIN, test_caller=["+12025550101"], replace_controller=False,
            startup_notice=True, crm_case_context=True,
        )
        saved = {}
        with (
            patch.object(setup, "private_path", return_value=destination),
            patch.object(setup, "components", return_value=source) as components,
            patch.object(setup, "secret_value", return_value=KEY),
            patch.object(setup, "write_private", side_effect=lambda path, text: saved.update({path.name: text})),
        ):
            setup.export_bundle(args, api)
        manifest = json.loads(saved["bundle.json"])
        self.assertTrue(manifest["crm_case_context"])
        self.assertEqual(len(manifest["updates"]), 3)
        self.assertNotIn(setup.CASE_VARIABLE, [row["name"] for row in manifest["updates"]])
        self.assertIn(setup.CASE_VARIABLE, json.loads(saved["before.json"])["records"])
        components.assert_called_once_with(api, BOT, crm_case_context=True)
        api.request.assert_not_called()
        topics.validate_case_handoff(topics.load_topic(saved[setup.BUNDLE_FILES["controller"]]))

    def test_case_optin_requires_an_existing_output_global_and_preserves_other_topics(self):
        with self.assertRaisesRegex(ValueError, "CaseNumber"):
            setup.bundle_documents(records(), ORIGIN, KEY, ["+12025550101"], crm_case_context=True)
        source = records(crm_case_context=True)
        before = copy.deepcopy(source)
        regular = setup.bundle_documents(source, ORIGIN, KEY, ["+12025550101"], startup_notice=True)
        case = setup.bundle_documents(source, ORIGIN, KEY, ["+12025550101"], startup_notice=True, crm_case_context=True)
        self.assertEqual(regular["entry"], case["entry"])
        self.assertEqual(regular["context"], case["context"])
        self.assertEqual(source, before)
        topics.validate_case_handoff(topics.load_topic(case["controller"]))
        self.assertIn("play_startup_notice: true", case["controller"])

    def test_existing_case_and_notice_configuration_cannot_be_silently_downgraded(self):
        source = records(crm_case_context=True)
        source[setup.CONTROLLER]["data"] = setup.bundle_documents(
            source, ORIGIN, KEY, ["+12025550101"], crm_case_context=True, startup_notice=True,
        )["controller"]
        with self.assertRaisesRegex(ValueError, "crm-case-context"):
            setup.bundle_documents(source, ORIGIN, KEY, ["+12025550101"], replace_controller=True, startup_notice=True)
        with self.assertRaisesRegex(ValueError, "startup-notice"):
            setup.bundle_documents(source, ORIGIN, KEY, ["+12025550101"], replace_controller=True, crm_case_context=True)

    def test_optin_flags_are_explicit_and_bad_variable_direction_is_rejected(self):
        options = setup.parser().parse_args([
            "bundle", "--org-url", "https://example.crm17.dynamics.com", "--tenant-id", BOT,
            "--bot-id", BOT, "--voice-origin", ORIGIN, "--test-caller", "+12025550101",
            "--secrets-file", "not-opened.env", "--output-dir", "not-created",
            "--crm-case-context", "--startup-notice",
        ])
        self.assertTrue(options.crm_case_context)
        self.assertTrue(options.startup_notice)
        source = records(crm_case_context=True)
        definition = topics.load_topic(source[setup.CASE_VARIABLE]["data"])
        definition["isOutputToExternalCallers"] = False
        source[setup.CASE_VARIABLE]["data"] = setup.dump(definition)
        with self.assertRaisesRegex(ValueError, "external output"):
            setup.bundle_documents(source, ORIGIN, KEY, ["+12025550101"], crm_case_context=True)

    def test_case_snapshot_is_separate_from_the_legacy_default_and_contains_no_credentials(self):
        crm = snapshot.generated(crm_case_context=True)
        self.assertIn("Global.CaseNumber", crm)
        self.assertIn("__NATIVE_MEDIA_API_KEY__", crm)
        self.assertNotIn(KEY, crm)
        self.assertNotIn("CaseNumber", snapshot.generated())
        self.assertEqual(snapshot.CRM_OUTPUT.read_bytes(), crm.encode())
        topics.validate_case_handoff(topics.load_topic(crm))

    def test_staging_refuses_a_case_global_changed_since_export(self):
        source = records(crm_case_context=True)
        documents = setup.bundle_documents(source, ORIGIN, KEY, ["+12025550101"], crm_case_context=True)
        updates = [{
            "name": name, "id": source[name]["botcomponentid"], "etag": source[name]["@odata.etag"],
            "data": documents[kind],
        } for kind, name in (("controller", setup.CONTROLLER), ("context", setup.CONTEXT), ("entry", "Conversation Start"))]
        before = {"records": copy.deepcopy(source), "publishedon": None}
        manifest = {
            "version": 1, "org_url": "https://example.crm17.dynamics.com", "tenant_id": BOT,
            "bot_id": BOT, "crm_case_context": True,
        }
        source[setup.CASE_VARIABLE]["botcomponentid"] = CALL
        api = MagicMock()
        api.get.return_value = {"publishedon": None}
        with patch.object(setup, "load_bundle", return_value=(manifest, before, updates)), patch.object(
            setup, "components", return_value=source,
        ), patch.object(setup, "Dataverse", return_value=api), self.assertRaisesRegex(ValueError, "CaseNumber output global changed"):
            setup.stage_bundle(SimpleNamespace(bundle_dir=Path("not-opened"), apply=True))
        api.patch_many.assert_not_called()

    def test_bundle_rejects_a_non_boolean_case_context_manifest(self):
        manifest = {"version": 1, "updates": [{}, {}, {}], "crm_case_context": "true"}
        with patch.object(setup, "read_private", side_effect=[json.dumps(manifest), "{}"]), self.assertRaisesRegex(ValueError, "must be a boolean"):
            setup.load_bundle(Path("not-opened"))

    def test_bundle_cannot_hide_case_wiring_behind_a_disabled_flag(self):
        source = records(crm_case_context=True)
        documents = setup.bundle_documents(source, ORIGIN, KEY, ["+12025550101"], crm_case_context=True)
        updates = [{
            "name": name, "id": source[name]["botcomponentid"], "file": setup.BUNDLE_FILES[kind],
            "sha256": setup.digest(documents[kind]),
        } for kind, name in (("controller", setup.CONTROLLER), ("context", setup.CONTEXT), ("entry", "Conversation Start"))]
        manifest = {"version": 1, "updates": updates, "crm_case_context": False}
        contents = [json.dumps(manifest), "{}"] + [documents[kind] for kind in ("controller", "context", "entry")]
        with patch.object(setup, "read_private", side_effect=contents), self.assertRaisesRegex(ValueError, "does not match its controller"):
            setup.load_bundle(Path("not-opened"))


def skip_native_configuration_checks(test):
    # These tests cover publication/runtime checks; NativeConfigurationTests covers the Dataverse preflight.
    patcher = patch.object(setup, "native_configuration_checks", return_value={"checked": True})
    patcher.start()
    test.addCleanup(patcher.stop)


class CaseContextVerificationTests(unittest.TestCase):
    def setUp(self):
        skip_native_configuration_checks(self)
        self.current = records(crm_case_context=True)
        documents = setup.bundle_documents(self.current, ORIGIN, KEY, ["+12025550101"], crm_case_context=True)
        for kind, name in (("controller", setup.CONTROLLER), ("context", setup.CONTEXT), ("entry", "Conversation Start")):
            self.current[name]["data"] = documents[kind]
        self.workstream = {
            "msdyn_name": "Native voice", "statecode": 0, "msdyn_direction": 0,
            "msdyn_streamsource": 192440000, "_msdyn_bot_user_value": BOT, "_msdyn_defaultqueue_value": QUEUE,
        }
        self.bot = {
            "name": "Routing agent", "language": 1033, "supportedlanguages": "1025",
            "publishedon": "2026-10-29T12:00:00Z",
            "configuration": basic_voice_configuration(),
        }
        self.ready = {
            "enabled": True, "observer_enabled": True, "dataverse_connected": True,
            "voip_gateway_ready": True, "transport": "voip", "workstream": "Native voice",
            "crm_case_context_supported": True,
        }
        self.health = {
            "voice_enabled": True, "automatic_phone_conference_enabled": False,
            "dynamics_outbound": {"submission_enabled": False},
        }
        self.args = SimpleNamespace(
            workstream_id=WORKSTREAM, bot_id=BOT, secrets_file=Path("not-opened.env"),
            voice_origin=ORIGIN, crm_case_context=True,
        )

    def test_case_verification_checks_output_global_publication_and_backend_support(self):
        for supported in (True, False):
            api = MagicMock()
            api.get.side_effect = [self.workstream, self.bot] + [{"modifiedon": "2026-10-29T11:00:00Z"}] * 4
            with (
                self.subTest(supported=supported),
                patch.object(setup, "components", return_value=self.current) as components,
                patch.object(setup, "secret_value", return_value=KEY),
                patch.object(setup.urllib.request, "urlopen", side_effect=[
                    io.BytesIO(json.dumps({**self.ready, "crm_case_context_supported": supported}).encode()),
                    io.BytesIO(json.dumps(self.health).encode()),
                ]),
            ):
                if supported:
                    setup.verify(self.args, api)
                    components.assert_called_with(api, BOT, crm_case_context=True)
                else:
                    with self.assertRaisesRegex(ValueError, "snapshot support"):
                        setup.verify(self.args, api)
            api.request.assert_not_called()
            api.patch_many.assert_not_called()

    def test_unpublished_output_global_is_not_reported_as_ready(self):
        api = MagicMock()
        api.get.side_effect = [self.workstream, self.bot] + [{"modifiedon": "2026-10-29T11:00:00Z"}] * 3 + [
            {"modifiedon": "2026-10-29T13:00:00Z"},
        ]
        with (
            patch.object(setup, "components", return_value=self.current),
            patch.object(setup.urllib.request, "urlopen") as http,
            self.assertRaisesRegex(ValueError, "CaseNumber global has unpublished edits"),
        ):
            setup.verify(self.args, api)
        http.assert_not_called()

    def test_explicit_case_requirement_rejects_a_legacy_controller(self):
        self.current[setup.CONTROLLER]["data"] = setup.bundle_documents(
            records(), ORIGIN, KEY, ["+12025550101"],
        )["controller"]
        api = MagicMock()
        api.get.side_effect = [self.workstream, self.bot] + [{"modifiedon": "2026-10-29T11:00:00Z"}] * 3
        with (
            patch.object(setup, "components", return_value=self.current),
            patch.object(setup.urllib.request, "urlopen") as http,
            self.assertRaisesRegex(ValueError, "missing the requested CRM"),
        ):
            setup.verify(self.args, api)
        http.assert_not_called()


ORG = "https://example.crm17.dynamics.com"
UNIT = "66666666-6666-4666-8666-666666666666"
CLIENT = "77777777-7777-4777-8777-777777777777"
PRINCIPAL = "88888888-8888-4888-8888-888888888888"
SHAREPOINT = ("prvCreateSharePointData", "prvReadSharePointData", "prvWriteSharePointData", "prvReadSharePointDocument")
SEEDED = SHAREPOINT + (
    "prvReadPluginAssembly", "prvReadPluginType", "prvReadSdkMessage",
    "prvReadSdkMessageProcessingStep", "prvReadSdkMessageProcessingStepImage",
)


class FakeReaderDataverse:
    """Stateful Dataverse double for the reader bootstrap; it records every call in order."""

    def __init__(self):
        self.org, self.tenant = ORG, BOT
        names = [*setup.REQUIRED_READS, *SEEDED, "prvReadAccount"]
        self.ids = {name: f"00000000-0000-4000-8000-{index:012d}" for index, name in enumerate(names, 100)}
        self.names = {value: key for key, value in self.ids.items()}
        self.roles, self.users, self.user_roles = {}, {}, {}
        self.readd_after_add, self.sticky = set(), set()
        self.fail = {}
        self.calls = []
        self.journal = None

    def http_error(self, path, code):
        return setup.urllib.error.HTTPError(ORG + "/api/data/v9.2/" + path, code, "synthetic", {}, None)

    def get(self, path, query=None):
        self.calls.append(("get", path))
        if path == f"businessunits({UNIT})":
            return {"businessunitid": UNIT, "isdisabled": False}
        if path.startswith("roles("):
            role = self.roles.get(path[6:-1])
            if role is None:
                raise self.http_error(path, 404)
            return {key: value for key, value in role.items() if key != "privileges"}
        if path.startswith("RetrieveRolePrivilegesRole(RoleId="):
            role = self.roles[path[len("RetrieveRolePrivilegesRole(RoleId="):-1]]
            return {"RolePrivileges": [
                {"PrivilegeId": self.ids[name].upper(), "Depth": depth} for name, depth in role["privileges"].items()
            ]}
        raise AssertionError("Unexpected read: " + path)

    def rows(self, table, query):
        self.calls.append(("rows", table))
        if table == "privileges":
            names = setup.re.findall(r"name eq '([^']+)'", query["$filter"])
            ids = setup.re.findall(r"privilegeid eq ([0-9a-f-]{36})", query["$filter"])
            return [{"privilegeid": self.ids[name], "name": name} for name in names if name in self.ids] + [
                {"privilegeid": key, "name": self.names[key]} for key in ids
            ]
        if table == "roles":
            name, unit = setup.re.fullmatch(r"name eq '([^']+)' and _businessunitid_value eq (\S+)", query["$filter"]).groups()
            return [{"roleid": key, "name": row["name"]} for key, row in self.roles.items()
                    if row["name"] == name and row["_businessunitid_value"] == unit]
        if table == "systemusers":
            client = setup.re.fullmatch(r"applicationid eq (\S+)", query["$filter"])[1]
            return [copy.deepcopy(row) for row in self.users.values() if row["applicationid"] == client]
        owner, relation = setup.re.fullmatch(r"(\w+\([^)]+\))/(\w+)", table).groups()
        identifier = owner[owner.index("(") + 1:-1]
        if relation == "systemuserroles_association" and owner.startswith("roles("):
            return [{"systemuserid": user} for user, roles in self.user_roles.items() if identifier in roles]
        if relation == "systemuserroles_association":
            return [{"roleid": role, "name": self.roles.get(role, {}).get("name", "System Administrator")}
                    for role in sorted(self.user_roles.get(identifier, ()))]
        if relation in {"teamroles_association", "teammembership_association"}:
            return []
        raise AssertionError("Unexpected table: " + table)

    def request(self, path, *, method, body=None, headers=None):
        self.calls.append(("request", path))
        if self.journal is not None:
            intent = sorted(self.journal.glob("*-intent.json"))[-1]
            assert json.loads(intent.read_text())["request"]["path"] == path, "write sent before its journal intent"
            assert not intent.with_name(intent.name.replace("-intent", "-result")).exists()
        if path == "roles":
            self.roles[body["roleid"]] = {
                "roleid": body["roleid"], "name": body["name"], "ismanaged": False,
                "_businessunitid_value": body["businessunitid@odata.bind"][15:-1],
                "privileges": {name: "Global" for name in SEEDED},
            }
        elif path.endswith("RemovePrivilegeRole"):
            privileges = self.roles[path[6:path.index(")")]]["privileges"]
            removed = self.names[body["Privilege"]["privilegeid"]]
            privileges.pop(removed)
            privileges.update({name: "Global" for name in self.sticky if name != removed})
        elif path.endswith("AddPrivilegesRole"):
            privileges = self.roles[path[6:path.index(")")]]["privileges"]
            privileges.update({self.names[row["PrivilegeId"]]: row["Depth"] for row in body["Privileges"]})
            privileges.update({name: "Global" for name in self.readd_after_add})
            self.readd_after_add = set()
        elif path == "systemusers":
            self.users[body["systemuserid"]] = {
                "systemuserid": body["systemuserid"], "applicationid": body["applicationid"],
                "azureactivedirectoryobjectid": body["azureactivedirectoryobjectid"], "accessmode": 4,
                "_businessunitid_value": body["businessunitid@odata.bind"][15:-1], "isdisabled": False,
            }
        elif path.endswith("/$ref"):
            user = path[len("systemusers("):path.index(")")]
            if self.fail.get("$ref") == "rejected":
                raise self.http_error(path, 400)
            self.user_roles.setdefault(user, set()).add(body["@odata.id"].rsplit("(", 1)[1][:-1])
        failure = self.fail.pop(path, None)
        if failure is not None:
            raise failure


class ReaderRoleTests(unittest.TestCase):
    def setUp(self):
        manager = private_test_directory()
        self.root = manager.__enter__()
        self.addCleanup(manager.__exit__, None, None, None)
        self.api = FakeReaderDataverse()
        self.output = self.root / "reader-identity"

    def args(self, **changes):
        return SimpleNamespace(**{
            "org_url": ORG, "tenant_id": BOT, "business_unit_id": UNIT, "client_id": CLIENT,
            "principal_id": PRINCIPAL, "role_name": setup.READER_ROLE_NAME, "output_dir": self.output,
            "apply": False, "confirm_organization": None, **changes,
        })

    def run_reader(self, **changes):
        with patch("sys.stdout", new_callable=io.StringIO) as output:
            setup.reader_role(self.args(**changes), self.api)
        return json.loads(output.getvalue())

    def plan_and_apply(self):
        self.assertEqual(self.run_reader()["status"], "planned")
        self.api.journal = self.output / "journal"
        return self.run_reader(apply=True, confirm_organization=ORG)

    def writes(self, suffix=""):
        return [path for kind, path in self.api.calls if kind == "request" and path.endswith(suffix)]

    def role(self):
        return setup.reader_target(self.args(), self.api)["role_id"]

    def test_new_role_drops_seeded_and_reintroduced_defaults_then_assigns_only_that_role(self):
        self.api.readd_after_add = set(SHAREPOINT)
        receipt = self.plan_and_apply()
        role = self.api.roles[self.role()]
        self.assertEqual(role["privileges"], setup.REQUIRED_READS)
        self.assertEqual(receipt["status"], "configured")
        self.assertEqual(receipt["privileges_that_returned_after_removal"], sorted(SHAREPOINT))
        self.assertEqual(len(self.writes("RemovePrivilegeRole")), len(SEEDED) + len(SHAREPOINT))
        self.assertEqual(len(self.writes("AddPrivilegesRole")), 1)
        self.assertEqual(len(self.writes("systemusers")), 1)
        self.assertEqual(self.api.user_roles, {receipt["application_user_id"]: {self.role()}})
        self.assertTrue(receipt["application_user_created_by_this_tool"])
        for index, (kind, path) in enumerate(self.api.calls):
            if kind == "request" and "PrivilegeRole" in path or kind == "request" and "PrivilegesRole" in path:
                self.assertTrue(self.api.calls[index + 1][1].startswith("RetrieveRolePrivilegesRole"), "No re-audit after a write")
        self.assertEqual(self.run_reader(apply=True, confirm_organization=ORG)["status"], "already_configured")
        self.assertEqual(len(self.writes()), len(SEEDED) + len(SHAREPOINT) + 4)

    def test_a_privilege_that_returns_after_two_removals_stops_for_the_owner(self):
        self.api.sticky = {"prvCreateSharePointData"}
        self.assertEqual(self.run_reader()["status"], "planned")
        with self.assertRaisesRegex(RuntimeError, "prvCreateSharePointData came back after being removed 2 times"):
            self.run_reader(apply=True, confirm_organization=ORG)
        journal = self.output / "journal"
        removed = [
            json.loads(path.with_name(path.name.replace("-result", "-intent")).read_text())["privilege"]
            for path in sorted(journal.glob("*remove-privilege-result.json"))
        ]
        self.assertEqual(removed.count("prvCreateSharePointData"), 2)
        self.assertEqual(self.writes("AddPrivilegesRole"), [])
        self.assertEqual(self.writes("systemusers"), [])

    def test_interrupted_user_creation_resumes_without_a_second_user(self):
        self.api.fail["systemusers"] = KeyboardInterrupt()
        self.assertEqual(self.run_reader()["status"], "planned")
        self.api.journal = self.output / "journal"
        with self.assertRaises(KeyboardInterrupt):
            self.run_reader(apply=True, confirm_organization=ORG)
        self.assertEqual(len(self.api.users), 1)
        self.assertEqual(self.api.user_roles, {})
        pending = [path.name for path in (self.output / "journal").glob("*create-application-user-*.json")]
        self.assertEqual([name.rsplit("-", 1)[1] for name in pending], ["intent.json"])
        receipt = self.run_reader(apply=True, confirm_organization=ORG)
        self.assertEqual(receipt["journal_steps_reconciled"], 1)
        self.assertEqual(len(self.writes("systemusers")), 1)
        self.assertEqual(self.api.user_roles, {receipt["application_user_id"]: {self.role()}})

    def test_user_created_despite_an_error_response_is_confirmed_by_readback(self):
        self.api.fail["systemusers"] = self.api.http_error("systemusers", 500)
        receipt = self.plan_and_apply()
        self.assertEqual(receipt["status"], "configured")
        self.assertEqual(len(self.writes("systemusers")), 1)
        result = next((self.output / "journal").glob("*create-application-user-result.json"))
        self.assertTrue(json.loads(result.read_text())["after_error"])

    def test_rejected_role_assignment_is_not_resent_on_resume(self):
        self.api.fail["$ref"] = "rejected"
        with self.assertRaisesRegex(RuntimeError, "No retry was sent"):
            self.plan_and_apply()
        with self.assertRaisesRegex(RuntimeError, "isn't applied on the server"):
            self.run_reader(apply=True, confirm_organization=ORG)
        self.assertEqual(len(self.writes("/$ref")), 1)
        self.assertEqual(len(self.api.users), 1)

    def test_role_with_the_reader_name_is_never_adopted(self):
        self.api.roles["99999999-9999-4999-8999-999999999999"] = {
            "roleid": "99999999-9999-4999-8999-999999999999", "name": setup.READER_ROLE_NAME,
            "ismanaged": False, "_businessunitid_value": UNIT, "privileges": {},
        }
        with self.assertRaisesRegex(ValueError, "wasn't created by this tool"):
            self.run_reader()
        self.assertFalse(self.output.exists())
        self.assertEqual(self.writes(), [])

    def test_existing_app_user_with_other_roles_is_preserved(self):
        user = "55555555-5555-4555-8555-555555555555"
        self.api.users[user] = {
            "systemuserid": user, "applicationid": CLIENT, "azureactivedirectoryobjectid": PRINCIPAL,
            "accessmode": 4, "_businessunitid_value": UNIT, "isdisabled": False,
        }
        self.api.user_roles[user] = {"44444444-4444-4444-8444-444444444444"}
        with self.assertRaisesRegex(ValueError, "already has other roles"):
            self.run_reader()
        self.api.user_roles[user] = set()
        self.api.users[user]["_businessunitid_value"] = QUEUE
        with self.assertRaisesRegex(ValueError, "another object ID, business unit"):
            self.run_reader()
        self.assertEqual(self.writes(), [])

    def test_existing_user_is_found_by_identity_and_only_the_role_is_added(self):
        user = "55555555-5555-4555-8555-555555555555"
        self.api.users[user] = {
            "systemuserid": user, "applicationid": CLIENT, "azureactivedirectoryobjectid": PRINCIPAL,
            "accessmode": 4, "_businessunitid_value": UNIT, "isdisabled": False,
        }
        receipt = self.plan_and_apply()
        self.assertEqual(self.writes("systemusers"), [])
        self.assertEqual(receipt["application_user_id"], user)
        self.assertFalse(receipt["application_user_created_by_this_tool"])
        self.assertEqual(self.api.user_roles, {user: {self.role()}})

    def test_plan_and_audit_are_read_only_and_apply_needs_the_reviewed_plan(self):
        with self.assertRaisesRegex(ValueError, "confirm-organization"):
            self.run_reader(apply=True)
        with self.assertRaisesRegex(ValueError, "Plan the reader identity"):
            self.run_reader(apply=True, confirm_organization=ORG)
        with self.assertRaisesRegex(ValueError, "only with --apply"):
            self.run_reader(confirm_organization=ORG)
        planned = self.run_reader()
        self.assertEqual(planned["missing_required_reads"], sorted(setup.REQUIRED_READS))
        self.assertEqual(self.run_reader()["status"], "audit")
        with self.assertRaisesRegex(ValueError, "targets another"):
            self.run_reader(apply=True, confirm_organization=ORG, client_id=QUEUE)
        self.assertEqual(self.writes(), [])


class DiscoveryTests(unittest.TestCase):
    def test_newest_catalog_entries_and_required_topics_are_explicit(self):
        api = MagicMock()
        api.get.return_value = {"UserId": BOT}
        api.rows.return_value = []
        setup.discover(SimpleNamespace(bot_id=BOT), api)
        catalogs = api.rows.call_args_list[:4]
        self.assertEqual(len(catalogs), 4)
        for call in catalogs:
            self.assertEqual(call.args[1]["$orderby"], "createdon desc")
            self.assertEqual(call.args[1]["$top"], "10")
        topics = api.rows.call_args_list[4]
        self.assertEqual(topics.args[0], "botcomponents")
        for name in ("Conversation Start", setup.CONTROLLER, setup.CONTEXT, setup.VARIABLE):
            self.assertIn(f"name eq '{name}'", topics.args[1]["$filter"])
        self.assertIn(f"_parentbotid_value eq {BOT} and (", topics.args[1]["$filter"])

    def test_platform_records_are_marked_not_owned_and_names_never_select_a_record(self):
        actor = "55555555-5555-4555-8555-555555555555"
        rows = {
            "appmodules": [],
            "bots": [
                {"botid": BOT, "name": "Routing agent", "ismanaged": False, "_createdby_value": actor.upper()},
                {"botid": CALL, "name": "Routing agent", "ismanaged": True, "_createdby_value": QUEUE},
            ],
            "msdyn_liveworkstreams": [
                {"msdyn_liveworkstreamid": WORKSTREAM, "msdyn_name": "Voice", "ismanaged": False,
                 "msdyn_isdefault": False, "_createdby_value": QUEUE},
                {"msdyn_liveworkstreamid": CALL, "msdyn_name": "Voice", "ismanaged": False, "msdyn_isdefault": True},
            ],
            "queues": [
                {"queueid": QUEUE, "name": "Fallback", "msdyn_isdefaultqueue": False, "msdyn_isomnichannelqueue": True},
                {"queueid": CALL, "name": "Default", "msdyn_isdefaultqueue": True, "msdyn_isomnichannelqueue": True},
                {"queueid": BOT, "name": "Personal", "msdyn_isdefaultqueue": False, "msdyn_isomnichannelqueue": False},
            ],
        }
        api = MagicMock()
        api.get.return_value = {"UserId": actor}
        api.rows.side_effect = lambda table, query: copy.deepcopy(rows[table])
        with patch("sys.stdout", new_callable=io.StringIO) as output:
            setup.discover(SimpleNamespace(bot_id=None), api)
        result = json.loads(output.getvalue())
        classes = {
            label: [(row["classification"]["source"], row["classification"]["owned"]) for row in result[label]]
            for label in ("bots", "workstreams", "queues")
        }
        self.assertEqual(classes["bots"], [("environment", None), ("platform", False)])
        self.assertEqual(classes["workstreams"], [("environment", None), ("platform", False)])
        self.assertEqual(classes["queues"], [("environment", None), ("platform", False), ("not-routable", False)])
        self.assertTrue(result["bots"][0]["classification"]["created_by_current_actor"])
        self.assertFalse(result["workstreams"][0]["classification"]["created_by_current_actor"])
        selects = {call.args[0]: call.args[1]["$select"].split(",") for call in api.rows.call_args_list}
        for table in ("appmodules", "bots", "msdyn_liveworkstreams"):
            self.assertIn("ismanaged", selects[table])
        for fields in selects.values():
            self.assertIn("_createdby_value", fields)
        self.assertIn("never a display name", result["ownership"])


def topic_row(name, document, *, state=0):
    return {
        "name": name, "schemaname": "sample_agent.topic." + name.replace(" ", ""), "componenttype": 9,
        "statecode": state, "data": setup.dump(document),
    }


def system_topic(trigger, *actions):
    return {"kind": "AdaptiveDialog", "beginDialog": {"kind": trigger, "id": "main", "actions": list(actions)}}


STARTER = {"kind": "SendActivity", "id": "starter", "activity": "Escalating to a representative is not currently configured for this agent."}
TRANSFER = {
    "kind": "TransferConversationV2", "id": "transfer",
    "transferType": {"kind": "TransferToAgent", "messageToAgent": "The caller asked for a person."},
}


def redirect(name):
    return {"kind": "BeginDialog", "id": "redirect_" + name, "dialog": "sample_agent.topic." + name}


FALLBACK = system_topic("OnUnknownIntent", {
    "kind": "ConditionGroup", "id": "retry",
    "conditions": [{"id": "again", "condition": "=System.FallbackCount < 3",
                    "actions": [{"kind": "SendActivity", "id": "rephrase", "activity": "Please rephrase."}]}],
    "elseActions": [redirect("Escalate")],
})


class EscalationAuditTests(unittest.TestCase):
    def test_starter_escalate_fails_and_names_the_fallback_redirect(self):
        with self.assertRaises(ValueError) as failure:
            setup.audit_topics([topic_row("Escalate", system_topic("OnEscalate", STARTER)), topic_row("Fallback", FALLBACK)])
        self.assertIn("Escalate is the starter escalation stub", str(failure.exception))
        self.assertIn("Fallback redirects to the stub Escalate", str(failure.exception))

    def test_starter_text_or_a_missing_transfer_both_fail(self):
        for actions in ((STARTER, TRANSFER), ({"kind": "SendActivity", "id": "note", "activity": "Please call back later."},)):
            with self.subTest(actions=[action["id"] for action in actions]), self.assertRaisesRegex(ValueError, "starter escalation stub"):
                setup.audit_topics([topic_row("Escalate", system_topic("OnEscalate", *actions))])

    def test_configured_escalate_passes_and_unreachable_or_disabled_stubs_are_ignored(self):
        report = setup.audit_topics([
            topic_row("Escalate", system_topic("OnEscalate", TRANSFER)), topic_row("Fallback", FALLBACK),
            topic_row("Old escalation", system_topic("OnEscalate", STARTER), state=1),
            topic_row("Unused draft", system_topic("OnRedirect", STARTER)),
            {"name": "Variable", "schemaname": "sample_agent.GlobalVariableComponent.Name", "componenttype": 12, "data": "kind: ["},
        ])
        self.assertEqual(report["escalate_transfers_to_agent"], ["Escalate"])
        self.assertEqual(report["fallback_redirects"], ["Escalate"])
        self.assertEqual(report["reachable_topics_audited"], 2)

    def test_fallback_redirect_to_another_reachable_stub_fails(self):
        fallback = copy.deepcopy(FALLBACK)
        fallback["beginDialog"]["actions"][0]["elseActions"] = [redirect("HumanHelp")]
        with self.assertRaisesRegex(ValueError, "Fallback redirects to the stub HumanHelp"):
            setup.audit_topics([
                topic_row("Escalate", system_topic("OnEscalate", TRANSFER)), topic_row("Fallback", fallback),
                topic_row("HumanHelp", system_topic("OnRedirect", STARTER)),
            ])

    def test_missing_escalate_or_unreadable_topic_fails_closed(self):
        with self.assertRaisesRegex(ValueError, "missing or turned off"):
            setup.audit_topics([topic_row("Fallback", FALLBACK)])
        broken = topic_row("Escalate", system_topic("OnEscalate", TRANSFER))
        broken["data"] = "kind: ["
        with self.assertRaisesRegex(ValueError, "'Escalate' could not be read"):
            setup.audit_topics([broken])


class NativeConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.workstream = {
            "_msdyn_botfailureactionconfig_value": QUEUE,
            "_msdyn_botfailureactionconfig_value@OData.Community.Display.V1.FormattedValue": "Wait Music and Escalate",
        }
        self.voice = {
            "msdyn_ocvoicechannelsettingid": CALL, "_msdyn_liveworkstreamid_value": WORKSTREAM.upper(), "statecode": 0,
            setup.CONSULT_CONTROL: True, **{name: False for name in setup.TRANSFER_CONTROLS},
        }
        self.tables = {
            "msdyn_ocvoicechannelsettings": [self.voice],
            "solutions": [{"uniquename": setup.PVA_EXTENSION, "version": "1.0.0.1", "installedon": "2026-11-01T10:00:00Z"}],
            "botcomponents": [topic_row("Escalate", system_topic("OnEscalate", TRANSFER)), topic_row("Fallback", FALLBACK)],
        }
        self.queries = {}
        self.api = MagicMock()
        self.api.rows.side_effect = self.rows

    def rows(self, table, query):
        self.queries[table] = query
        return copy.deepcopy(self.tables[table])

    def check(self):
        return setup.native_configuration_checks(self.api, WORKSTREAM, BOT, self.workstream)

    def test_preflight_reports_extension_controls_failure_action_and_escalation(self):
        report = self.check()
        self.assertEqual(report["omnichannel_pva_extension"], {"installed": True, "version": "1.0.0.1", "installedon": "2026-11-01T10:00:00Z"})
        self.assertFalse(report["telephony_extension_required"])
        self.assertTrue(report["voice_controls"]["external_phone_consult"])
        self.assertTrue(all(report["voice_controls"][name]["as_expected"] for name in setup.TRANSFER_CONTROLS))
        self.assertEqual(report["ai_agent_failure_action"], {"id": QUEUE, "name": "Wait Music and Escalate"})
        self.assertEqual(report["topics"]["escalate_transfers_to_agent"], ["Escalate"])
        self.assertEqual(self.queries["msdyn_ocvoicechannelsettings"]["$filter"], f"_msdyn_liveworkstreamid_value eq {WORKSTREAM}")
        self.assertEqual(self.queries["solutions"]["$filter"], f"uniquename eq '{setup.PVA_EXTENSION}'")
        self.assertEqual(self.queries["botcomponents"]["$filter"], f"_parentbotid_value eq {BOT} and componenttype eq 9")
        self.api.request.assert_not_called()

    def test_empty_ai_agent_failure_action_fails_before_other_reads(self):
        self.workstream["_msdyn_botfailureactionconfig_value"] = None
        with self.assertRaisesRegex(ValueError, "Fallback action is empty"):
            self.check()
        self.api.rows.assert_not_called()

    def test_consult_off_fails_and_transfer_controls_are_reported_not_enforced(self):
        self.voice[setup.CONSULT_CONTROL] = False
        with self.assertRaisesRegex(ValueError, "External phone consult is off"):
            self.check()
        self.voice[setup.CONSULT_CONTROL] = True
        self.voice[setup.TRANSFER_CONTROLS[0]] = True
        report = self.check()["voice_controls"][setup.TRANSFER_CONTROLS[0]]
        self.assertEqual(report, {"value": True, "expected": False, "as_expected": False})

    def test_voice_settings_must_resolve_to_one_active_record_of_this_workstream(self):
        for voices, message in (
            ([], "found 0"), ([self.voice, {**self.voice, "msdyn_ocvoicechannelsettingid": BOT}], "found 2"),
            ([{**self.voice, "_msdyn_liveworkstreamid_value": CALL}], "another workstream"),
            ([{**self.voice, "statecode": 1}], "inactive"),
        ):
            self.tables["msdyn_ocvoicechannelsettings"] = voices
            with self.subTest(message=message), self.assertRaisesRegex(ValueError, message):
                self.check()

    def test_missing_omnichannel_extension_fails_and_telephony_extension_is_not_required(self):
        self.tables["solutions"] = []
        with self.assertRaisesRegex(ValueError, f"{setup.PVA_EXTENSION}.*mspva_TelephonyExtension isn't required"):
            self.check()
        self.assertNotIn("mspva_TelephonyExtension", self.queries["solutions"]["$filter"])

    def test_starter_escalation_fails_the_preflight(self):
        self.tables["botcomponents"] = [topic_row("Escalate", system_topic("OnEscalate", STARTER)), topic_row("Fallback", FALLBACK)]
        with self.assertRaisesRegex(ValueError, "Fallback redirects to the stub Escalate"):
            self.check()

    def test_verify_runs_these_checks_before_topic_and_runtime_checks(self):
        workstream = {
            "msdyn_name": "Native voice", "statecode": 0, "msdyn_direction": 0, "msdyn_streamsource": 192440000,
            "_msdyn_bot_user_value": BOT, "_msdyn_defaultqueue_value": QUEUE, "_msdyn_botfailureactionconfig_value": None,
        }
        bot = {"name": "Routing agent", "language": 1033, "supportedlanguages": "1025",
               "publishedon": "2026-10-19T12:00:00Z", "configuration": basic_voice_configuration()}
        api = MagicMock()
        api.get.side_effect = [workstream, bot]
        with patch.object(setup, "components") as reads, patch.object(setup.urllib.request, "urlopen") as http, \
                self.assertRaisesRegex(ValueError, "Fallback action is empty"):
            setup.verify(SimpleNamespace(workstream_id=WORKSTREAM, bot_id=BOT), api)
        self.assertIn("_msdyn_botfailureactionconfig_value", api.get.call_args_list[0].args[1]["$select"])
        reads.assert_not_called()
        http.assert_not_called()


class ConnectionTests(unittest.TestCase):
    RESOURCE = f"/subscriptions/{BOT}/resourceGroups/native/providers/Microsoft.Communication/communicationServices/native-acs"
    APP_ACS = f"/subscriptions/{BOT}/resourceGroups/app/providers/Microsoft.Communication/communicationServices/app-acs"
    EVENT_APP = "99999999-9999-4999-8999-999999999999"
    SETTING = "55555555-5555-4555-8555-555555555555"
    SECRET = "endpoint=https://example.communication.azure.com/;accesskey=synthetic-unit-test-value"

    def setUp(self):
        self.metadata = {
            setup.PROVIDER_TABLE: ("msdyn_occommunicationprovidersettings", "msdyn_occommunicationprovidersettingid",
                                   ["msdyn_occommunicationprovidersettingid", "msdyn_istrial", "statecode"]),
            setup.PROVIDER_ENTRY_TABLE: ("msdyn_occommunicationprovidersettingentries", "msdyn_occommunicationprovidersettingentryid",
                                         ["msdyn_key", "msdyn_name", "msdyn_value", "msdyn_providersetting", "statecode"]),
        }
        self.relationships = [{"ReferencingAttribute": "msdyn_providersetting", "ReferencedEntity": setup.PROVIDER_TABLE}]
        self.settings = [{"msdyn_occommunicationprovidersettingid": self.SETTING, "msdyn_istrial": False, "statecode": 0}]
        self.entries = [
            self.entry("ResourceIdentifier", self.RESOURCE.upper()), self.entry("AppRegistration", "{" + self.EVENT_APP.upper() + "}"),
            self.entry("DirectoryTenant", BOT), self.entry("Secret", self.SECRET),
        ]
        self.api = MagicMock()
        self.api.get.side_effect = self.get
        self.api.rows.side_effect = self.rows
        self.queries = {}

    def entry(self, key, value, parent=None):
        return {"msdyn_key": key, "msdyn_name": key, "msdyn_value": value,
                "_msdyn_providersetting_value": parent or self.SETTING, "statecode": 0}

    def get(self, path, query=None):
        table, part = setup.re.fullmatch(r"EntityDefinitions\(LogicalName='(\w+)'\)(/\w+)?", path).groups()
        if table not in self.metadata:
            raise setup.urllib.error.HTTPError("https://example.crm17.dynamics.com/" + path, 404, "missing", {}, None)
        set_name, key, columns = self.metadata[table]
        if part is None:
            return {"LogicalName": table, "EntitySetName": set_name, "PrimaryIdAttribute": key}
        if part == "/Attributes":
            return {"value": [{"LogicalName": name} for name in columns]}
        return {"value": self.relationships}

    def rows(self, table, query):
        self.queries[table] = query
        return copy.deepcopy(self.settings if table == "msdyn_occommunicationprovidersettings" else self.entries)

    def run_connection(self):
        args = SimpleNamespace(tenant_id=BOT, native_acs_resource_id=self.RESOURCE, event_grid_app_id=self.EVENT_APP)
        with patch("sys.stdout", new_callable=io.StringIO) as output:
            setup.connection(args, self.api)
        return output.getvalue()

    def test_matches_values_across_entries_and_prints_no_values(self):
        printed = self.run_connection()
        result = json.loads(printed)
        self.assertEqual(result["status"], "connected")
        self.assertEqual(result["matched_entry_keys"], {
            "acs_resource_id": ["ResourceIdentifier"], "event_grid_app_id": ["AppRegistration"],
            "event_grid_app_tenant_id": ["DirectoryTenant"],
        })
        for value in (self.SECRET, "synthetic-unit-test-value", "native-acs", self.EVENT_APP):
            self.assertNotIn(value, printed)
        self.assertIn("_msdyn_providersetting_value", self.queries["msdyn_occommunicationprovidersettingentries"]["$select"])
        self.api.request.assert_not_called()

    def test_trial_or_application_acs_connection_fails(self):
        for trial in (True, None):
            self.settings[0]["msdyn_istrial"] = trial
            with self.subTest(trial=trial), self.assertRaisesRegex(ValueError, "voice trial"):
                self.run_connection()
        self.settings[0]["msdyn_istrial"] = False
        self.entries[0] = self.entry("ResourceIdentifier", self.APP_ACS)
        with self.assertRaisesRegex(ValueError, "No active .* native ACS resource ID.*never the application ACS"):
            self.run_connection()

    def test_missing_event_grid_values_name_keys_not_values(self):
        del self.entries[1]
        with self.assertRaises(ValueError) as failure:
            self.run_connection()
        self.assertIn("event_grid_app_id", str(failure.exception))
        self.assertIn("ResourceIdentifier", str(failure.exception))
        self.assertNotIn("synthetic-unit-test-value", str(failure.exception))

    def test_duplicate_or_inactive_provider_records_are_not_guessed(self):
        other = "66666666-6666-4666-8666-666666666666"
        self.settings.append({"msdyn_occommunicationprovidersettingid": other, "msdyn_istrial": False, "statecode": 0})
        self.entries.append(self.entry("ResourceIdentifier", self.RESOURCE, parent=other))
        with self.assertRaisesRegex(ValueError, "More than one"):
            self.run_connection()
        self.settings[1]["statecode"] = 1
        self.assertEqual(json.loads(self.run_connection())["other_active_provider_records"], [])

    def test_unknown_schema_fails_before_any_row_is_read(self):
        cases = (
            (lambda: self.metadata[setup.PROVIDER_TABLE][2].remove("msdyn_istrial"), "has no msdyn_istrial column"),
            (lambda: self.metadata[setup.PROVIDER_ENTRY_TABLE][2].remove("msdyn_value"), "has no msdyn_value column"),
            (lambda: self.metadata.pop(setup.PROVIDER_TABLE), "has no msdyn_occommunicationprovidersetting table"),
            (lambda: self.relationships.clear(), "no single lookup"),
        )
        for change, message in cases:
            self.setUp()
            change()
            with self.subTest(message=message), self.assertRaisesRegex(ValueError, message):
                self.run_connection()
            self.api.rows.assert_not_called()


class BasicVoiceConfigurationTests(unittest.TestCase):
    def test_saved_basic_voice_and_initialized_legacy_voice_are_accepted(self):
        setup.validate_basic_voice_agent({"configuration": basic_voice_configuration()})
        legacy = json.loads(basic_voice_configuration())
        legacy.pop("voiceProcessingMode")
        legacy["settings"].pop("GenerativeActionsEnabled")
        setup.validate_basic_voice_agent({"configuration": json.dumps(legacy)})

    def test_connected_channel_without_saved_enabled_voice_is_not_ready(self):
        for value in (None, False, "true", 1):
            configuration = json.loads(basic_voice_configuration())
            configuration["channels"] = [{"channelId": "Telephony"}]
            if value is None:
                configuration.pop("isTelephonyEnabled")
            else:
                configuration["isTelephonyEnabled"] = value
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "connected Telephony"):
                setup.validate_basic_voice_agent({"configuration": json.dumps(configuration)})

    def test_missing_or_uninitialized_voice_settings_are_rejected(self):
        for settings in (None, {}, {"TelephonyInitialized": False}, {"TelephonyInitialized": "true"},
                         {"TelephonyInitialized": 1}):
            configuration = json.loads(basic_voice_configuration())
            configuration["settings"] = settings
            with self.subTest(settings=settings), self.assertRaisesRegex(ValueError, "initialization"):
                setup.validate_basic_voice_agent({"configuration": json.dumps(configuration)})

    def test_wrong_voice_mode_or_generative_orchestration_is_rejected(self):
        for mode in ("ClassicVoiceProcessingMode", {}, {"$kind": "RealtimeVoiceProcessingMode"}):
            configuration = json.loads(basic_voice_configuration())
            configuration["voiceProcessingMode"] = mode
            with self.subTest(mode=mode), self.assertRaisesRegex(ValueError, "Basic/classic"):
                setup.validate_basic_voice_agent({"configuration": json.dumps(configuration)})
        for value in (True, "false", 0):
            configuration = json.loads(basic_voice_configuration())
            configuration["settings"]["GenerativeActionsEnabled"] = value
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "classic topic"):
                setup.validate_basic_voice_agent({"configuration": json.dumps(configuration)})

    def test_invalid_or_missing_config_never_returns_ready(self):
        for value in (None, "", "null", "[]", "true", "{", {}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                setup.validate_basic_voice_agent({"configuration": value})

    def test_disabled_agent_is_rejected_before_backend_checks_or_writes(self):
        api = MagicMock()
        api.get.side_effect = [
            {"msdyn_name": "Native voice", "statecode": 0, "msdyn_direction": 0,
             "msdyn_streamsource": 192440000, "_msdyn_bot_user_value": BOT, "_msdyn_defaultqueue_value": QUEUE},
            {"name": "Routing agent", "language": 1033, "supportedlanguages": "1025",
             "publishedon": "2026-10-19T12:00:00Z", "configuration": '{"channels":[{"channelId":"Telephony"}]}'},
        ]
        with patch.object(setup, "components") as component_reads, \
                patch.object(setup.urllib.request, "urlopen") as http, \
                self.assertRaisesRegex(ValueError, "connected Telephony"):
            setup.verify(SimpleNamespace(workstream_id=WORKSTREAM, bot_id=BOT), api)
        component_reads.assert_not_called()
        http.assert_not_called()
        api.request.assert_not_called()


class VerificationTests(unittest.TestCase):
    def setUp(self):
        skip_native_configuration_checks(self)

    def test_application_owner_verifier_checks_preparation_capability_and_no_native_notice(self):
        source = records()
        documents = setup.bundle_documents(source, ORIGIN, KEY, ["+12025550101"], startup_notice=True)
        for kind, name in (("controller", setup.CONTROLLER), ("context", setup.CONTEXT), ("entry", "Conversation Start")):
            source[name]["data"] = documents[kind]
        workstream = {
            "msdyn_name": "Native voice", "statecode": 0, "msdyn_direction": 0,
            "msdyn_streamsource": 192440000, "_msdyn_bot_user_value": BOT, "_msdyn_defaultqueue_value": QUEUE,
        }
        bot = {"name": "Routing agent", "language": 1033, "supportedlanguages": "1025", "publishedon": "2026-10-19T12:00:00Z",
               "configuration": basic_voice_configuration()}
        ready = {
            "enabled": True, "observer_enabled": True, "dataverse_connected": True,
            "voip_gateway_ready": True, "transport": "voip", "workstream": "Native voice",
            "startup_notice_supported": True, "startup_notice_available": True,
            "application_startup_preparation_supported": True, "startup_failure_notice_supported": True,
            "startup_notice_sha256": hashlib.sha256(topics.NOTICE_AUDIO.read_bytes()).hexdigest(),
        }
        health = {"voice_enabled": True, "automatic_phone_conference_enabled": False, "dynamics_outbound": {"submission_enabled": False}}
        args = SimpleNamespace(workstream_id=WORKSTREAM, bot_id=BOT, secrets_file=Path("unused.env"), voice_origin=ORIGIN)
        for defect in (None, "backend", "latency", "endpoint", "owner_guard"):
            current, actual_ready = copy.deepcopy(source), dict(ready)
            controller = topics.load_topic(current[setup.CONTROLLER]["data"])
            nodes = {node["id"]: node for node in controller["beginDialog"]["actions"]}
            if defect == "backend":
                actual_ready.pop("application_startup_preparation_supported")
            elif defect == "latency":
                nodes["native_start"]["latencyMessageSettings"] = topics.native_startup_latency_settings(ORIGIN)
            elif defect == "endpoint":
                nodes["native_start"]["url"] = ORIGIN + "/dynamics/native/sessions?wait_for_context=true"
            elif defect == "owner_guard":
                next(node for node in nodes["native_check_start"]["conditions"]
                     if node["id"] == "native_start_not_prepared")["condition"] = topics.startup_prepared_condition("native")
            current[setup.CONTROLLER]["data"] = setup.dump(controller)
            api = MagicMock()
            api.get.side_effect = [workstream, bot] + [{"modifiedon": "2026-10-19T11:00:00Z"}] * 3
            with self.subTest(defect=defect), patch.object(setup, "components", return_value=current), \
                    patch.object(setup, "secret_value", return_value=KEY), patch("builtins.print"), \
                    patch.object(setup.urllib.request, "urlopen", side_effect=[
                        io.BytesIO(json.dumps(actual_ready).encode()), io.BytesIO(json.dumps(health).encode()),
                    ]):
                if defect is None:
                    setup.verify(args, api)
                else:
                    with self.assertRaisesRegex(ValueError, "startup notice"):
                        setup.verify(args, api)

    def test_notice_optin_cannot_verify_against_an_old_backend_or_missing_asset(self):
        current = records()
        documents = setup.bundle_documents(current, ORIGIN, KEY, ["+12025550101"], startup_notice=True)
        for kind, name in (("controller", setup.CONTROLLER), ("context", setup.CONTEXT), ("entry", "Conversation Start")):
            current[name]["data"] = documents[kind]
        workstream = {
            "msdyn_name": "Native voice", "statecode": 0, "msdyn_direction": 0,
            "msdyn_streamsource": 192440000, "_msdyn_bot_user_value": BOT, "_msdyn_defaultqueue_value": QUEUE,
        }
        bot = {
            "name": "Routing agent", "language": 1033, "supportedlanguages": "1025",
            "publishedon": "2026-10-19T12:00:00Z",
            "configuration": basic_voice_configuration(),
        }
        ready = {
            "enabled": True, "observer_enabled": True, "dataverse_connected": True,
            "voip_gateway_ready": True, "transport": "voip", "workstream": "Native voice",
        }
        args = SimpleNamespace(workstream_id=WORKSTREAM, bot_id=BOT, secrets_file=Path("unused.env"), voice_origin=ORIGIN)
        for extra in (
            {},
            {"startup_notice_supported": True, "startup_notice_available": False},
            {"startup_notice_supported": True, "startup_notice_available": True},
        ):
            api = MagicMock()
            api.get.side_effect = [workstream, bot] + [{"modifiedon": "2026-10-19T11:00:00Z"}] * 3
            with patch.object(setup, "components", return_value=current), patch.object(
                setup, "secret_value", return_value=KEY,
            ), patch.object(
                setup.urllib.request, "urlopen", return_value=io.BytesIO(json.dumps({**ready, **extra}).encode()),
            ), self.assertRaisesRegex(ValueError, "startup notice"):
                setup.verify(args, api)

    def test_publication_checks_compare_instants_not_timestamp_formatting(self):
        current = records()
        documents = setup.bundle_documents(current, ORIGIN, KEY, ["+15550100001"])
        for kind, name in (("controller", setup.CONTROLLER), ("context", setup.CONTEXT), ("entry", "Conversation Start")):
            current[name]["data"] = documents[kind]
        workstream = {
            "msdyn_name": "Native voice", "statecode": 0, "msdyn_direction": 0,
            "msdyn_streamsource": 192440000, "_msdyn_bot_user_value": BOT, "_msdyn_defaultqueue_value": QUEUE,
        }
        bot = {"name": "Routing agent", "language": 1033, "supportedlanguages": "1025", "publishedon": "2026-10-19T12:00:00.500Z",
               "configuration": basic_voice_configuration()}
        ready = {
            "enabled": True, "observer_enabled": True, "dataverse_connected": True,
            "voip_gateway_ready": True, "transport": "voip", "workstream": "Native voice",
        }
        health = {"voice_enabled": True, "automatic_phone_conference_enabled": False, "dynamics_outbound": {"submission_enabled": False}}
        args = SimpleNamespace(workstream_id=WORKSTREAM, bot_id=BOT, secrets_file=Path("unused.env"), voice_origin=ORIGIN)
        cases = [
            ("2026-10-19T12:00:00Z", True, ready),
            ("2026-10-19T12:00:00.750Z", False, ready),
            ("2026-10-19T12:00:00Z", True, {
                **ready, "workstream": "Primary entry",
                "workstreams": [{"id": CALL, "name": "Primary entry"}, {"id": WORKSTREAM, "name": "Native voice"}],
            }),
        ]
        for modified, should_pass, readiness in cases:
            api = MagicMock()
            api.get.side_effect = [workstream, bot, *[{"modifiedon": modified}] * 3]
            with (
                self.subTest(modified=modified),
                patch.object(setup, "components", return_value=current),
                patch.object(setup, "secret_value", return_value=KEY),
                patch.object(setup.urllib.request, "urlopen", side_effect=[io.BytesIO(json.dumps(data).encode()) for data in (readiness, health)]) as http,
            ):
                if should_pass:
                    setup.verify(args, api)
                    self.assertEqual(http.call_count, 2)
                else:
                    with self.assertRaisesRegex(ValueError, "unpublished edits"):
                        setup.verify(args, api)
                    http.assert_not_called()


class MonitorTests(unittest.TestCase):
    def test_closed_state_uses_the_numeric_code_in_a_non_english_environment(self):
        with private_test_directory() as folder:
            output = Path(folder) / "report.json"
            args = [
                "watch_native_voice.py", "--tenant-id", BOT, "--org-url", "https://example.crm4.dynamics.com",
                "--voice-origin", ORIGIN, "--workstream-id", WORKSTREAM, "--queue-id", QUEUE,
                "--representative-id", BOT, "--secrets-file", str(Path(folder) / "keys.env"),
                "--output", str(output), "--arm",
            ]
            item = {
                "msdyn_ocliveworkitemid": CALL, "statuscode": 4,
                "statuscode@OData.Community.Display.V1.FormattedValue": "Cerrado",
                "createdon": watch.datetime.now(watch.timezone.utc).isoformat(),
            }
            ready = {name: True for name in ("enabled", "observer_enabled", "dataverse_connected", "voip_gateway_ready")}
            ready["transport"] = "voip"
            missing = watch.urllib.error.HTTPError(ORIGIN, 404, "not retained", None, None)
            with (
                patch.object(sys, "argv", args),
                patch.object(watch, "Dataverse") as client,
                patch.object(watch, "secret_value", return_value=KEY),
                patch.object(watch.urllib.request, "urlopen", side_effect=[io.BytesIO(json.dumps(ready).encode()), missing]),
                patch.object(watch.time, "sleep") as sleep,
                self.assertRaises(SystemExit) as stopped,
            ):
                client.return_value.rows.side_effect = [[], [item]]
                watch.main()
            self.assertEqual(stopped.exception.code, 1)
            self.assertEqual(json.loads(output.read_text())["status"], "call_closed")
            sleep.assert_not_called()

    def test_read_only_observation_needs_media_and_native_acceptance(self):
        report = {"representative_id": BOT, "microphone_ready_seen": False, "audio_frames_seen": False, "handover_ready_seen": False}
        item = {"msdyn_ocliveworkitemid": CALL, "_msdyn_activeagentid_value": BOT}
        media = {
            "conversation_id": CALL, "audio_transport": "voip", "control_language": "en", "control_revision": 1,
            "control_ready": True, "ai_participant_media_ready": True, "customer_audio_frames": 1, "ai_audio_frames": 1,
        }
        watch.observe(report, item, media, None)
        self.assertTrue(report["microphone_ready_seen"])
        self.assertNotIn("representative_accepted_at", report)
        watch.observe(report, item, {**media, "phase": "handover_ready", "action": "handover", "azure_leg_detached": True}, {
            "_msdyn_liveworkitemid_value": CALL, "msdyn_agentacceptedon": "2026-10-18T12:00:00Z",
        })
        self.assertTrue(report["handover_ready_seen"])
        self.assertIn("representative_accepted_at", report)
        with self.assertRaises(ValueError):
            watch.observe(report, {**item, "msdyn_ocliveworkitemid": QUEUE}, media, None)


if __name__ == "__main__":
    unittest.main()
