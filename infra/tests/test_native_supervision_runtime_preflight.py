import contextlib
import hashlib
import io
import json
import sys
import types
import unittest
from unittest.mock import patch
from uuid import NAMESPACE_URL, uuid5

import check_native_supervision_runtime as preflight


USER = str(uuid5(NAMESPACE_URL, "supervision-preflight-runtime-user"))
APP = str(uuid5(NAMESPACE_URL, "supervision-preflight-application"))


class RuntimePreflightTests(unittest.TestCase):
    def run_program(self, *, user=USER, application=APP, denied=False, configured=True,
                    expected_env_hashes=None, environment=None):
        calls = []

        class Client:
            def __init__(self, *args):
                pass

            async def get(self, path, **kwargs):
                calls.append(path)
                if path == "WhoAmI":
                    return {"UserId": user}
                return {"applicationid": application, "isdisabled": False}

            async def close(self):
                calls.append("close")

        class Consumer:
            def __init__(self, settings, client, apply, sessions):
                self.ready = False
                self.client = client
                self.assert_read_only = apply is None and sessions() == {}

            async def verify(self):
                if not self.assert_read_only:
                    raise AssertionError("Preflight must not execute a command.")
                if denied:
                    raise PermissionError("Metadata permission missing")
                calls.append("verify")
                self.ready = True

        settings = types.SimpleNamespace(actors=(USER,) if configured else (), controllers=((USER, "a" * 64),), enabled=False)
        modules = {
            "backend": types.ModuleType("backend"),
            "backend.dataverse": types.SimpleNamespace(DataverseClient=Client),
            "backend.native_supervision": types.SimpleNamespace(
                NativeCommandConsumer=Consumer,
                SupervisionConfig=types.SimpleNamespace(from_env=lambda: settings),
            ),
        }
        output = io.StringIO()
        with patch.dict(sys.modules, modules), patch.dict("os.environ", {
            "DYNAMICS_ORG_URL": "https://example.crm17.dynamics.com", "AZURE_CLIENT_ID": APP,
            **(environment or {}),
        }, clear=True), contextlib.redirect_stdout(output):
            exec(compile(preflight.program(USER, expected_env_hashes), "runtime-preflight", "exec"), {})
        return calls, output.getvalue()

    def test_exact_runtime_identity_is_checked_without_enabling_or_polling(self):
        calls, output = self.run_program()
        result = json.loads(output.split("SUPERVISION_PREFLIGHT_RESULT=", 1)[1])
        self.assertEqual(calls, ["WhoAmI", f"systemusers({USER})", "verify", "close"])
        self.assertTrue(result["schema_and_controller_ready"])
        self.assertFalse(result["supervision_enabled"])
        self.assertFalse(result["command_polling_started"])
        self.assertEqual(result["remote_writes"], 0)

    def test_wrong_user_or_application_is_not_accepted(self):
        with self.assertRaisesRegex(ValueError, "runtime identity"):
            self.run_program(user=APP)
        with self.assertRaisesRegex(ValueError, "managed identity"):
            self.run_program(application=USER)

    def test_missing_permission_or_configuration_is_not_a_success(self):
        with self.assertRaises(PermissionError):
            self.run_program(denied=True)
        with self.assertRaisesRegex(ValueError, "Configure reviewed"):
            self.run_program(configured=False)

    def test_selected_environment_secret_hashes_are_verified_without_printing_values(self):
        value = "offline-only-secret-fixture"
        expected = {"PREFLIGHT_TEST_SECRET": hashlib.sha256(value.encode()).hexdigest()}
        _, output = self.run_program(expected_env_hashes=expected, environment={"PREFLIGHT_TEST_SECRET": value})
        result = json.loads(output.split("SUPERVISION_PREFLIGHT_RESULT=", 1)[1])
        self.assertTrue(result["environment_secret_hashes_verified"])
        self.assertEqual(result["environment_secret_count"], 1)
        self.assertNotIn(value, output)

    def test_missing_or_changed_environment_secret_never_passes(self):
        expected = {"PREFLIGHT_TEST_SECRET": hashlib.sha256(b"original-value").hexdigest()}
        for environment in ({}, {"PREFLIGHT_TEST_SECRET": ""}, {"PREFLIGHT_TEST_SECRET": "different-value"}):
            with self.subTest(environment=environment), self.assertRaisesRegex(ValueError, "secret hash mismatch"):
                self.run_program(expected_env_hashes=expected, environment=environment)

    def test_hash_input_rejects_empty_unknown_shapes_invalid_names_and_non_hash_values(self):
        for value in ({}, [], {"BAD-NAME": "a" * 64}, {"VALID_NAME": "not-a-hash"},
                      {"VALID_NAME": "A" * 64}, {"VALID_NAME": None}):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "SHA256"):
                preflight.program(USER, value)


if __name__ == "__main__":
    unittest.main()
