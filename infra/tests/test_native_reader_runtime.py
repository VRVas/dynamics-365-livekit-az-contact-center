import base64
import contextlib
import io
import json
import re
import shlex
import stat
import sys
import tempfile
import types
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

import check_native_reader_runtime as reader
import stage_native_media_topic as topics


USER = "11111111-1111-4111-8111-111111111111"
BUSINESS_UNIT = "22222222-2222-4222-8222-222222222222"
CLIENT = "33333333-3333-4333-8333-333333333333"
OBJECT = "44444444-4444-4444-8444-444444444444"
TENANT = "55555555-5555-4555-8555-555555555555"
ADMIN = "66666666-6666-4666-8666-666666666666"
SUBSCRIPTION = "77777777-7777-4777-8777-777777777777"
ORG = "https://example.crm17.dynamics.com"


def token(appid=CLIENT):
    claims = base64.urlsafe_b64encode(json.dumps({"appid": appid, "oid": OBJECT, "tid": TENANT}).encode()).decode().rstrip("=")
    return f"eyJ0eXAiOiJKV1QifQ.{claims}.synthetic-signature"


def role_denial(user=USER):
    return {"error": {"code": "0x80040220", "message": (
        f"Principal user (Id={user}, type=8, roleCount=1) is missing prvReadRole privilege for entity 'role'. context.Caller={user}."
    )}}


class Response:
    def __init__(self, body):
        self.status, self.body = 200, json.dumps(body).encode()

    def read(self, limit):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def dataverse(overrides=None):
    responses = {
        "WhoAmI": (200, {"UserId": USER, "BusinessUnitId": BUSINESS_UNIT, "OrganizationId": ADMIN}),
        "systemusers": (200, {"applicationid": CLIENT, "azureactivedirectoryobjectid": OBJECT, "isdisabled": False}),
        "msdyn_ocphonenumbers": (200, {"value": [{"msdyn_ocphonenumberid": ADMIN}]}),
        "msdyn_ocphonecallengagementctxes": (200, {"value": []}),
        "roles": (403, role_denial()),
        **(overrides or {}),
    }
    requests = []

    def urlopen(request, timeout):
        requests.append(request)
        path = request.full_url.removeprefix(ORG + "/api/data/v9.2/")
        status, body = responses[path.split("(", 1)[0].split("?", 1)[0]]
        if status != 200:
            raise urllib.error.HTTPError(request.full_url, status, "error", {}, io.BytesIO(json.dumps(body).encode()))
        return Response(body)

    return urlopen, requests


class ReaderProgramTests(unittest.TestCase):
    def run_program(self, overrides=None, *, environment=None, appid=CLIENT):
        credentials = []

        class Credential:
            def __init__(self, client_id):
                credentials.append(client_id)

            def get_token(self, scope):
                credentials.append(scope)
                return types.SimpleNamespace(token=token(appid))

            def close(self):
                credentials.append("closed")

        urlopen, requests = dataverse(overrides)
        output = io.StringIO()
        variables = {"DYNAMICS_ORG_URL": ORG, "AZURE_CLIENT_ID": CLIENT, "AZURE_TENANT_ID": TENANT} if environment is None else environment
        with patch.dict(sys.modules, {"azure.identity": types.SimpleNamespace(ManagedIdentityCredential=Credential)}), \
                patch.dict("os.environ", variables, clear=True), patch("urllib.request.urlopen", urlopen), \
                contextlib.redirect_stdout(output):
            exec(compile(reader.PROGRAM, "native-reader-probe", "exec"), {})
        text = output.getvalue()
        self.assertNotIn("synthetic-signature", text)
        self.assertTrue(all(request.get_method() == "GET" for request in requests))
        result = json.loads(text.split(reader.MARKER, 1)[1])
        return result, credentials, requests

    def test_container_identity_reads_native_phone_tables_and_is_denied_roles(self):
        result, credentials, requests = self.run_program()
        self.assertEqual(credentials, [CLIENT, ORG + "/.default", "closed"])
        self.assertEqual(reader.evaluate(result, USER, BUSINESS_UNIT), [])
        self.assertEqual(result["role_read"]["principal_user_id"], USER)
        self.assertEqual(
            [request.full_url.removeprefix(ORG + "/api/data/v9.2/").split("?")[0] for request in requests],
            ["WhoAmI", f"systemusers({USER})", "msdyn_ocphonenumbers", "msdyn_ocphonecallengagementctxes", "roles"],
        )

    def test_wrong_user_or_business_unit_fails_closed(self):
        result, _, _ = self.run_program({"WhoAmI": (200, {"UserId": ADMIN, "BusinessUnitId": ADMIN})})
        failures = " ".join(reader.evaluate(result, USER, BUSINESS_UNIT))
        self.assertIn("another user", failures)
        self.assertIn("another business unit", failures)

    def test_allowed_role_read_fails_closed(self):
        result, _, _ = self.run_program({"roles": (200, {"value": [{"roleid": ADMIN}]})})
        self.assertRegex(" ".join(reader.evaluate(result, USER, BUSINESS_UNIT)), "HTTP 200 instead of a 403 prvReadRole")

    def test_role_denial_for_another_user_or_reason_fails_closed(self):
        for overrides in ({"roles": (403, role_denial(ADMIN))},
                          {"roles": (403, {"error": {"code": "0x80072560", "message": "The user is not a member of the organization."}})}):
            with self.subTest(overrides=overrides):
                result, _, _ = self.run_program(overrides)
                self.assertTrue(reader.evaluate(result, USER, BUSINESS_UNIT))

    def test_failed_native_read_fails_closed(self):
        denied = {"error": {"code": "0x80040220", "message": "missing prvReadmsdyn_ocphonenumber privilege"}}
        result, _, _ = self.run_program({"msdyn_ocphonenumbers": (403, denied)})
        self.assertRegex(" ".join(reader.evaluate(result, USER, BUSINESS_UNIT)), "Reading msdyn_ocphonenumbers returned HTTP 403")

    def test_token_for_another_identity_or_missing_configuration_fails_closed(self):
        result, _, _ = self.run_program(appid=ADMIN)
        self.assertRegex(" ".join(reader.evaluate(result, USER, BUSINESS_UNIT)), "container's managed identity")
        result, credentials, requests = self.run_program(environment={"DYNAMICS_ORG_URL": ORG})
        self.assertEqual((credentials, requests), ([], []))
        self.assertRegex(" ".join(reader.evaluate(result, USER, BUSINESS_UNIT)), "AZURE_CLIENT_ID")


@contextlib.contextmanager
def private_test_directory():
    # Temporary folders stay outside the checkout; a fake checkout models the private-output boundary.
    with tempfile.TemporaryDirectory(prefix="native-reader-runtime-test-") as name:
        directory = Path(name).resolve()
        checkout = directory / "checkout"
        checkout.mkdir(mode=0o700)
        with patch.object(topics, "ROOT", checkout):
            yield directory


def passing_result(**changes):
    result = {
        "client_id": CLIENT, "tenant_id": TENANT, "token": {"appid": CLIENT, "azp": "", "oid": OBJECT, "tid": TENANT},
        "who_am_i": {"status": 200, "user_id": USER, "business_unit_id": BUSINESS_UNIT},
        "application_user": {"status": 200, "application_id": CLIENT, "object_id": OBJECT, "disabled": False},
        "native_reads": {table: {"status": 200, "rows": 0, "error_code": None} for table in reader.NATIVE_TABLES},
        "role_read": {"status": 403, "error_code": "0x80040220", "missing_prvReadRole": True, "principal_user_id": USER, "caller_user_id": USER},
        "remote_writes": 0, "calls_placed": 0,
    }
    result.update(changes)
    return result


class ReaderCommandTests(unittest.TestCase):
    def arguments(self, receipt):
        return ["--subscription-id", SUBSCRIPTION, "--resource-group", "app-rg", "--expected-runtime-user", USER,
                "--expected-business-unit", BUSINESS_UNIT, "--receipt-file", str(receipt)]

    def test_verified_probe_writes_an_owner_only_receipt(self):
        with private_test_directory() as directory:
            receipt = directory / "reader-runtime.json"
            terminal = ">>> exec('...')\n" + reader.MARKER + json.dumps(passing_result()) + "\n>>> exit()\n"
            with patch.object(reader, "run_in_container", return_value=terminal) as run, contextlib.redirect_stdout(io.StringIO()) as output:
                reader.main(self.arguments(receipt))
            self.assertEqual(run.call_args.args[:4], (SUBSCRIPTION, "app-rg", "ca-voice", "voice"))
            self.assertEqual(json.loads(output.getvalue())["status"], "verified")
            self.assertEqual(stat.S_IMODE(receipt.stat().st_mode), 0o600)
            self.assertEqual(json.loads(receipt.read_text())["status"], "verified")

    def test_failed_or_missing_result_is_recorded_and_raises(self):
        wrong = passing_result(who_am_i={"status": 200, "user_id": ADMIN, "business_unit_id": BUSINESS_UNIT})
        for terminal, status in ((reader.MARKER + json.dumps(wrong) + "\n", "failed"), ("Traceback (most recent call last):\n", "unconfirmed")):
            with self.subTest(status=status), private_test_directory() as directory:
                receipt = directory / "reader-runtime.json"
                with patch.object(reader, "run_in_container", return_value=terminal), self.assertRaisesRegex(RuntimeError, "didn't pass"):
                    reader.main(self.arguments(receipt))
                self.assertEqual(json.loads(receipt.read_text())["status"], status)

    def test_existing_receipt_is_never_overwritten(self):
        with private_test_directory() as directory:
            receipt = directory / "reader-runtime.json"
            receipt.write_text("{}")
            with patch.object(reader, "run_in_container") as run, self.assertRaisesRegex(ValueError, "new private receipt"):
                reader.main(self.arguments(receipt))
            run.assert_not_called()

    def test_documented_command_parses_with_this_cli(self):
        source = (Path(__file__).resolve().parents[2] / "docs/dynamics-native-voice/operations.md").read_text().replace("\\\n", " ")
        [command] = re.findall(r"(?m)^python infra/scripts/check_native_reader_runtime\.py ([^\n#]+)", source)
        with private_test_directory() as directory:
            values = {"SUBSCRIPTION_ID": SUBSCRIPTION, "APP_RG": "app-rg", "READER_USER_ID": USER,
                      "READER_BUSINESS_UNIT_ID": BUSINESS_UNIT, "WORK": str(directory)}
            args = reader.parser().parse_args([re.sub(r"\$(\w+)", lambda match: values[match[1]], token) for token in shlex.split(command)])
        self.assertEqual((args.app_name, args.container_name, args.expected_runtime_user), ("ca-voice", "voice", USER))

    def test_exec_targets_the_voice_container_with_an_interactive_python(self):
        process = types.SimpleNamespace(poll=lambda: 0)
        with patch.object(reader.subprocess, "Popen", return_value=process) as popen, \
                patch.object(reader.select, "select", return_value=([], [], [])):
            self.assertEqual(reader.run_in_container(SUBSCRIPTION, "app-rg", "ca-voice", "voice", "print(1)"), "")
        command = popen.call_args.args[0]
        self.assertEqual(command[:3], ["az", "containerapp", "exec"])
        self.assertEqual(command[command.index("--container") + 1], "voice")
        self.assertEqual(command[command.index("--command") + 1], "python -i")


if __name__ == "__main__":
    unittest.main()
