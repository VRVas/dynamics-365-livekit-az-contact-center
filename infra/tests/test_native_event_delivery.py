import contextlib
import copy
import io
import json
import os
import re
import shlex
import stat
import subprocess
import tempfile
import unittest
import urllib.error
import urllib.parse
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import native_event_delivery as delivery
import stage_native_media_topic as topics


TENANT = "11111111-1111-4111-8111-111111111111"
OTHER_TENANT = "22222222-2222-4222-8222-222222222222"
WRITER = "33333333-3333-4333-8333-333333333333"
EVENT_GRID_SP = "44444444-4444-4444-8444-444444444444"
SUBSCRIPTION = "55555555-5555-4555-8555-555555555555"
DELIVERY_APP = "66666666-6666-4666-8666-666666666666"
APP_OBJECT = "77777777-7777-4777-8777-777777777777"
DELIVERY_SP = "88888888-8888-4888-8888-888888888888"
OTHER_PRINCIPAL = "99999999-9999-4999-8999-999999999999"
ACS_ID = f"/subscriptions/{SUBSCRIPTION}/resourceGroups/native-rg/providers/Microsoft.Communication/communicationServices/native-acs"
INCOMING_URL = "https://contact-center.example.com/voice/incoming?organization=example&acsResourceName=native-acs"
RECORDING_URL = "https://contact-center.example.com/voice/recording?organization=example&acsResourceName=native-acs"


GUIDE = Path(__file__).resolve().parents[2] / "docs/dynamics-native-voice/operations.md"


def documented(script, values):
    source = GUIDE.read_text().replace("\\\n", " ")
    commands = re.findall(rf"(?m)^python infra/scripts/{re.escape(script)} ([^\n#]+)", source)
    return [[re.sub(r"\$(\w+)", lambda match: values[match[1]], token) for token in shlex.split(command)] for command in commands]


class Crash(BaseException):
    """Models the process stopping between the journaled intent and the write outcome."""


@contextlib.contextmanager
def private_test_directory():
    # Temporary folders stay outside the checkout; a fake checkout models the private-output boundary.
    with tempfile.TemporaryDirectory(prefix="native-event-delivery-test-") as name:
        directory = Path(name).resolve()
        checkout = directory / "checkout"
        checkout.mkdir(mode=0o700)
        with patch.object(topics, "ROOT", checkout), patch.object(delivery, "READBACK_INTERVAL", 0):
            yield directory


def http_error(code, message="Synthetic Graph error", graph_code="Request_Denied"):
    body = json.dumps({"error": {"code": graph_code, "message": message}}).encode()
    return urllib.error.HTTPError("https://graph.microsoft.com/v1.0/synthetic", code, "error", {}, io.BytesIO(body))


def output_of(function, *args):
    stream = io.StringIO()
    with contextlib.redirect_stdout(stream):
        function(*args)
    return json.loads(stream.getvalue())


class FakeGraph(delivery.Graph):
    def __init__(self):
        self.tenant = TENANT
        self.event_grid = [{"id": EVENT_GRID_SP, "appId": delivery.EVENT_GRID_APP_ID, "displayName": "Microsoft.EventGrid"}]
        self.named_event_grid = None
        self.me = {"id": WRITER, "displayName": "Deployment Operator"}
        self.applications, self.principals = [], []
        self.owners, self.principal_owners, self.assigned, self.client_roles, self.consents = {}, {}, {}, {}, {}
        self.writes, self.failures = [], {}
        self.extra_assignment = None

    def configured(self, role_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"):
        role = delivery.app_role(role_id)
        self.applications.append({
            "id": APP_OBJECT, "appId": DELIVERY_APP, "displayName": "Native Voice Events", "uniqueName": "native-voice-events-test",
            "signInAudience": "AzureADMyOrg", "appRoles": [role], "passwordCredentials": [], "keyCredentials": [],
            "requiredResourceAccess": [],
        })
        self.principals.append({"id": DELIVERY_SP, "appId": DELIVERY_APP, "appRoles": [role], "passwordCredentials": [], "keyCredentials": []})
        self.owners[APP_OBJECT] = [WRITER]
        self.assigned[DELIVERY_SP] = [
            {"id": "a1", "principalId": EVENT_GRID_SP, "principalType": "ServicePrincipal", "appRoleId": role_id, "resourceId": DELIVERY_SP},
            {"id": "a2", "principalId": WRITER, "principalType": "User", "appRoleId": role_id, "resourceId": DELIVERY_SP},
        ]
        return self

    def request(self, path, *, method="GET", body=None, headers=None):
        split = urllib.parse.urlsplit(path)
        route, query = urllib.parse.unquote(split.path), dict(urllib.parse.parse_qsl(split.query))
        if method == "GET":
            return self.read(route, query)
        label = self.label(method, route, body)
        self.writes.append({"label": label, "route": route, "body": copy.deepcopy(body), "headers": dict(headers or {})})
        queued = self.failures.get(label) or []
        failure, landed = queued.pop(0) if queued else (None, False)
        if failure is not None and not landed:
            raise failure
        result = self.apply(label, route, body)
        if failure is not None:
            raise failure
        return result

    @staticmethod
    def label(method, route, body):
        if method == "PATCH" and route.startswith("applications(uniqueName="):
            return "application"
        if method == "PATCH" and route.startswith("servicePrincipals(appId="):
            return "service-principal"
        if method == "POST" and route.endswith("/owners/$ref"):
            return "owner"
        if method == "POST" and route.endswith("/appRoleAssignedTo"):
            return "assignment:" + body["principalId"]
        raise AssertionError(f"Unexpected Graph write {method} {route}")

    def apply(self, label, route, body):
        if label == "application":
            unique = re.search(r"uniqueName='([^']+)'", route)[1]
            if any(app["uniqueName"] == unique for app in self.applications):
                return {}
            app = {
                "id": APP_OBJECT, "appId": DELIVERY_APP, "uniqueName": unique, "displayName": body["displayName"],
                "signInAudience": body["signInAudience"], "appRoles": copy.deepcopy(body["appRoles"]),
                "passwordCredentials": [], "keyCredentials": [], "requiredResourceAccess": [],
            }
            self.applications.append(app)
            return copy.deepcopy(app)
        if label == "service-principal":
            app_id = re.search(r"appId='([^']+)'", route)[1]
            if any(row["appId"] == app_id for row in self.principals):
                return {}
            app = next(row for row in self.applications if row["appId"] == app_id)
            self.principals.append({
                "id": DELIVERY_SP, "appId": app_id, "displayName": body["displayName"],
                "appRoles": copy.deepcopy(app["appRoles"]), "passwordCredentials": [], "keyCredentials": [],
            })
            if self.extra_assignment:
                self.assigned.setdefault(DELIVERY_SP, []).append(dict(self.extra_assignment))
            return {}
        if label == "owner":
            application = route.split("/")[1]
            user = body["@odata.id"].rsplit("/", 1)[1]
            if user in self.owners.get(application, []):
                raise http_error(400, "One or more added object references already exist.")
            self.owners.setdefault(application, []).append(user)
            return {}
        principal = route.split("/")[1]
        rows = self.assigned.setdefault(principal, [])
        if any(row["principalId"] == body["principalId"] and row["appRoleId"] == body["appRoleId"] for row in rows):
            raise http_error(400, "Permission being assigned already exists on the object.")
        rows.append({
            "id": f"assignment-{len(rows) + 1}", "principalId": body["principalId"], "appRoleId": body["appRoleId"],
            "principalType": "ServicePrincipal" if body["principalId"] == EVENT_GRID_SP else "User", "resourceId": principal,
        })
        return {}

    def read(self, route, query):
        if route == "me":
            return dict(self.me)
        match = re.fullmatch(r"(\w+) eq '(.*)'", query.get("$filter", ""))
        if route == "servicePrincipals":
            field, value = match.groups()
            if field == "appId" and value == delivery.EVENT_GRID_APP_ID:
                rows = self.event_grid
            elif field == "displayName" and value == "Microsoft.EventGrid":
                rows = self.event_grid if self.named_event_grid is None else self.named_event_grid
            else:
                rows = [row for row in self.principals if row.get(field) == value]
            return {"value": copy.deepcopy(rows)}
        if route == "applications":
            field, value = match.groups()
            return {"value": copy.deepcopy([row for row in self.applications if row.get(field) == value.replace("''", "'")])}
        parts = route.split("/")
        if parts[0] == "applications" and parts[2] == "owners":
            return {"value": [{"id": value} for value in self.owners.get(parts[1], [])]}
        collections = {
            "owners": self.principal_owners, "appRoleAssignedTo": self.assigned,
            "appRoleAssignments": self.client_roles, "oauth2PermissionGrants": self.consents,
        }
        if parts[0] == "servicePrincipals" and parts[2] in collections:
            stored = collections[parts[2]].get(parts[1], [])
            return {"value": copy.deepcopy([{"id": value} for value in stored] if parts[2] == "owners" else stored)}
        raise AssertionError(f"Unexpected Graph read {route}")


def bootstrap_args(directory, *extra):
    return delivery.parser().parse_args(["bootstrap", "--tenant-id", TENANT, "--output-dir", str(directory / "bootstrap"), *extra])


APPLY = ("--apply", "--confirm-tenant", TENANT)


class BootstrapTests(unittest.TestCase):
    def test_plan_writes_nothing_then_apply_creates_only_the_delivery_app_and_two_grants(self):
        with private_test_directory() as directory:
            graph = FakeGraph()
            planned = output_of(delivery.bootstrap, bootstrap_args(directory), graph)
            self.assertEqual(planned["status"], "planned")
            self.assertEqual(graph.writes, [])
            self.assertEqual(stat.S_IMODE((directory / "bootstrap").stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE((directory / "bootstrap/plan.json").stat().st_mode), 0o600)
            with self.assertRaisesRegex(ValueError, "--confirm-tenant"):
                delivery.bootstrap(bootstrap_args(directory, "--apply"), graph)
            self.assertEqual(graph.writes, [])

            result = output_of(delivery.bootstrap, bootstrap_args(directory, *APPLY), graph)
            self.assertEqual([write["label"] for write in graph.writes], [
                "application", "service-principal", "owner", "assignment:" + EVENT_GRID_SP, "assignment:" + WRITER,
            ])
            application = graph.writes[0]
            self.assertEqual(application["headers"], {"Prefer": "create-if-missing"})
            self.assertEqual(application["body"]["signInAudience"], "AzureADMyOrg")
            self.assertEqual([role["value"] for role in application["body"]["appRoles"]], [delivery.ROLE])
            self.assertEqual(application["body"]["appRoles"][0]["allowedMemberTypes"], ["Application", "User"])
            for write in graph.writes:
                self.assertFalse({"passwordCredentials", "keyCredentials", "requiredResourceAccess"} & set(write["body"]))
            self.assertEqual(result["status"], "configured")
            self.assertEqual((result["delivery_app_id"], result["owners"]), (DELIVERY_APP, [WRITER]))
            self.assertEqual((result["secrets_created"], result["consent_grants"]), (0, 0))
            self.assertTrue(Path(result["private_receipt"]).is_file())

            rerun = output_of(delivery.bootstrap, bootstrap_args(directory, *APPLY), graph)
            self.assertEqual(set(rerun["steps"].values()), {"unchanged"})
            self.assertEqual(len(graph.writes), 5)

    def test_event_grid_principal_must_exist_once_with_documented_id_and_name(self):
        cases = (
            ("event_grid", [], "No Microsoft.EventGrid"),
            ("named_event_grid", [{"id": EVENT_GRID_SP, "appId": OTHER_PRINCIPAL, "displayName": "Microsoft.EventGrid"}], "rerun the plan with --event-grid-app-id"),
            ("named_event_grid", [{"id": EVENT_GRID_SP}, {"id": OTHER_PRINCIPAL}], "exactly one"),
            ("event_grid", [{"id": EVENT_GRID_SP, "appId": delivery.EVENT_GRID_APP_ID, "displayName": "Event Grid copy"}], "different principals"),
        )
        for field, value, message in cases:
            with self.subTest(field=field, message=message), private_test_directory() as directory:
                graph = FakeGraph()
                setattr(graph, field, value)
                if message.startswith("rerun"):
                    graph.event_grid = []
                with self.assertRaisesRegex(ValueError, message):
                    delivery.bootstrap(bootstrap_args(directory), graph)
                self.assertFalse((directory / "bootstrap").exists())
                self.assertEqual(graph.writes, [])

    def test_existing_display_name_or_different_writer_is_refused(self):
        with private_test_directory() as directory:
            graph = FakeGraph().configured()
            with self.assertRaisesRegex(ValueError, "already exists"):
                delivery.bootstrap(bootstrap_args(directory), graph)
            graph = FakeGraph()
            output_of(delivery.bootstrap, bootstrap_args(directory), graph)
            graph.me = {"id": OTHER_PRINCIPAL, "displayName": "Another administrator"}
            with self.assertRaisesRegex(ValueError, "subscription writer"):
                delivery.bootstrap(bootstrap_args(directory, *APPLY), graph)
            self.assertEqual(graph.writes, [])

    def test_unexpected_grants_owners_or_consent_fail_the_readback(self):
        def extra_assignment(graph, plan):
            graph.extra_assignment = {"id": "extra", "principalId": OTHER_PRINCIPAL, "principalType": "Group",
                                      "appRoleId": plan["app_role"]["id"], "resourceId": DELIVERY_SP}

        def extra_owner(graph, plan):
            graph.owners[APP_OBJECT] = [OTHER_PRINCIPAL]

        def consent(graph, plan):
            graph.consents[DELIVERY_SP] = [{"id": "grant", "scope": "User.Read"}]

        for change, message in ((extra_assignment, "unexpected grants"), (extra_owner, "owners"), (consent, "consent")):
            with self.subTest(message=message), private_test_directory() as directory:
                graph = FakeGraph()
                output_of(delivery.bootstrap, bootstrap_args(directory), graph)
                change(graph, json.loads((directory / "bootstrap/plan.json").read_text()))
                with self.assertRaisesRegex(RuntimeError, message):
                    delivery.bootstrap(bootstrap_args(directory, *APPLY), graph)
                self.assertFalse(list((directory / "bootstrap").glob("after-*.json")))

    def test_lost_response_is_reconciled_by_readback_without_a_second_write(self):
        with private_test_directory() as directory:
            graph = FakeGraph()
            output_of(delivery.bootstrap, bootstrap_args(directory), graph)
            graph.failures["assignment:" + EVENT_GRID_SP] = [(urllib.error.URLError("connection reset"), True)]
            result = output_of(delivery.bootstrap, bootstrap_args(directory, *APPLY), graph)
            self.assertEqual(result["steps"]["event-grid-assignment"], "reconciled")
            self.assertEqual([write["label"] for write in graph.writes].count("assignment:" + EVENT_GRID_SP), 1)

    def test_resume_after_partial_bootstrap_never_duplicates_or_resends_silently(self):
        with private_test_directory() as directory:
            graph = FakeGraph()
            output_of(delivery.bootstrap, bootstrap_args(directory), graph)
            graph.failures["assignment:" + EVENT_GRID_SP] = [(Crash(), False)]
            with self.assertRaises(Crash):
                delivery.bootstrap(bootstrap_args(directory, *APPLY), graph)
            journal = directory / "bootstrap"
            self.assertTrue((journal / "event-grid-assignment.intent-1.json").is_file())
            self.assertFalse((journal / "event-grid-assignment.done.json").exists())
            writes = len(graph.writes)

            with self.assertRaisesRegex(RuntimeError, "No write was resent.*--retry-step event-grid-assignment"):
                delivery.bootstrap(bootstrap_args(directory, *APPLY), graph)
            self.assertEqual(len(graph.writes), writes)

            result = output_of(delivery.bootstrap, bootstrap_args(directory, *APPLY, "--retry-step", "event-grid-assignment"), graph)
            self.assertEqual(result["status"], "configured")
            self.assertEqual(result["steps"]["application"], "unchanged")
            labels = [write["label"] for write in graph.writes]
            self.assertEqual(labels.count("application"), 1)
            self.assertEqual(len(graph.applications), 1)
            self.assertEqual(labels.count("assignment:" + EVENT_GRID_SP), 2)
            self.assertEqual(len(graph.assigned[DELIVERY_SP]), 2)

    def test_a_rejected_step_allows_one_explicit_retry_then_stops(self):
        with private_test_directory() as directory:
            graph = FakeGraph()
            output_of(delivery.bootstrap, bootstrap_args(directory), graph)
            denied = lambda: (http_error(403, "Insufficient privileges to complete the operation.", "Authorization_RequestDenied"), False)
            graph.failures["owner"] = [denied(), denied()]
            with self.assertRaisesRegex(RuntimeError, "HTTP 403.*Authorization_RequestDenied.*--retry-step owner"):
                delivery.bootstrap(bootstrap_args(directory, *APPLY), graph)
            failure = json.loads((directory / "bootstrap/owner.failed-1.json").read_text())
            self.assertTrue(failure["definite"])
            with self.assertRaisesRegex(RuntimeError, "HTTP 403.*last attempt"):
                delivery.bootstrap(bootstrap_args(directory, *APPLY, "--retry-step", "owner"), graph)
            writes = len(graph.writes)
            with self.assertRaisesRegex(RuntimeError, "2 attempts failed.*ask the human"):
                delivery.bootstrap(bootstrap_args(directory, *APPLY, "--retry-step", "owner"), graph)
            self.assertEqual(len(graph.writes), writes)

    def test_graph_token_for_another_tenant_is_refused(self):
        for identity in ({"accessToken": "synthetic", "tenant": OTHER_TENANT}, {"accessToken": "synthetic"}):
            with self.subTest(identity=identity), patch.object(
                delivery.subprocess, "run", return_value=SimpleNamespace(stdout=json.dumps(identity)),
            ):
                with self.assertRaisesRegex(ValueError, "another tenant"):
                    delivery.Graph(TENANT)


class FakeAzure:
    def __init__(self):
        self.tenant = TENANT
        self.topics, self.subscriptions, self.calls = [], {}, []
        self.create_failures, self.create_state = {}, "Succeeded"

    def __call__(self, arguments, *, timeout=120, secrets=()):
        arguments = list(arguments)
        self.calls.append(arguments)

        def option(name):
            return arguments[arguments.index(name) + 1]

        if arguments[:2] == ["account", "show"]:
            return {"tenantId": self.tenant}
        if arguments[:2] == ["resource", "show"]:
            return {"id": option("--ids"), "type": "Microsoft.Communication/CommunicationServices", "location": "global"}
        if arguments[:3] == ["eventgrid", "system-topic", "list"]:
            return copy.deepcopy(self.topics)
        if arguments[:3] == ["eventgrid", "system-topic", "create"]:
            topic = {
                "id": f"/subscriptions/{SUBSCRIPTION}/resourceGroups/{option('--resource-group')}/providers/Microsoft.EventGrid/systemTopics/{option('--name')}",
                "name": option("--name"), "source": option("--source"), "topicType": option("--topic-type"),
                "location": option("--location"), "provisioningState": "Succeeded",
            }
            self.topics.append(topic)
            return topic
        command = arguments[:4]
        if command == ["eventgrid", "system-topic", "event-subscription", "list"]:
            return [self.public(row) for row in self.subscriptions.values()]
        if command == ["eventgrid", "system-topic", "event-subscription", "show"]:
            return self.public(self.subscriptions[option("--name")], full="--include-full-endpoint-url" in arguments)
        if command == ["eventgrid", "system-topic", "event-subscription", "create"]:
            queued = self.create_failures.get(option("--name")) or []
            if queued:
                raise queued.pop(0)
            filters = []
            if "--advanced-filter" in arguments:
                start = arguments.index("--advanced-filter")
                key, operator, value = arguments[start + 1:start + 4]
                filters = [{"key": key, "operatorType": operator, "values": [value]}]
            self.subscriptions[option("--name")] = route(
                option("--name"), option("--endpoint"), option("--included-event-types"), filters,
                tenant=option("--azure-active-directory-tenant-id"),
                application=option("--azure-active-directory-application-id-or-uri"), state=self.create_state,
            )
            return self.public(self.subscriptions[option("--name")])
        raise AssertionError(f"Unexpected az call {arguments}")

    @staticmethod
    def public(row, full=False):
        row = copy.deepcopy(row)
        if not full:
            row["destination"]["endpointUrl"] = None
        return row

    def writes(self):
        return [call for call in self.calls if "create" in call[:4]]


def route(name, url, event, filters, *, tenant=TENANT, application=DELIVERY_APP, state="Succeeded", attributes=None):
    split = urllib.parse.urlsplit(url)
    return {
        "name": name, "provisioningState": state, "eventDeliverySchema": "EventGridSchema", "expirationTimeUtc": None,
        "destination": {
            "endpointType": "WebHook", "endpointUrl": url, "endpointBaseUrl": f"{split.scheme}://{split.netloc}{split.path}",
            "azureActiveDirectoryTenantId": tenant, "azureActiveDirectoryApplicationIdOrUri": application,
            "deliveryAttributeMappings": attributes, "maxEventsPerBatch": 1,
        },
        "filter": {"includedEventTypes": [event] if event else None, "advancedFilters": filters,
                   "subjectBeginsWith": "", "subjectEndsWith": "", "isSubjectCaseSensitive": False},
    }


def existing_topic():
    return {
        "id": f"/subscriptions/{SUBSCRIPTION}/resourceGroups/native-rg/providers/Microsoft.EventGrid/systemTopics/native-topic",
        "name": "native-topic", "source": ACS_ID, "topicType": delivery.TOPIC_TYPE, "provisioningState": "Succeeded",
    }


PHONE_FILTER = [{"key": "data.to.kind", "operatorType": "StringIn", "values": ["phoneNumber"]}]


def write_endpoints(directory, **overrides):
    values = {
        "INCOMING_CALL_WEBHOOK_URL": INCOMING_URL, "RECORDING_WEBHOOK_URL": RECORDING_URL,
        "EVENT_GRID_APP_ID": DELIVERY_APP, "EVENT_GRID_APP_TENANT_ID": TENANT, **overrides,
    }
    path = directory / f"dynamics-webhooks-{len(list(directory.glob('dynamics-webhooks-*')))}.env"
    delivery.write_private(path, "".join(f"{key}={value}\n" for key, value in values.items() if value is not None))
    return path


def subscription_args(directory, endpoints, *extra, command="subscriptions"):
    arguments = [
        command, "--tenant-id", TENANT, "--native-acs-resource-id", ACS_ID, "--system-topic-name", "native-topic",
        "--delivery-app-id", DELIVERY_APP, "--endpoints-file", str(endpoints),
    ]
    if command == "subscriptions":
        arguments += ["--output-dir", str(directory / "subscriptions")]
    return delivery.parser().parse_args([*arguments, *extra])


CONFIRM = ("--apply", "--confirm-acs-name", "native-acs")


class SubscriptionTests(unittest.TestCase):
    def run_command(self, azure, function, *args):
        with patch.object(delivery, "az", azure):
            return output_of(function, *args)

    def test_creates_one_topic_and_two_entra_routes_then_verify_prints_the_exit_gate(self):
        with private_test_directory() as directory:
            azure, endpoints = FakeAzure(), write_endpoints(directory)
            planned = self.run_command(azure, delivery.subscriptions, subscription_args(directory, endpoints))
            self.assertEqual(planned["system_topic"]["action"], "create")
            self.assertEqual({row["action"] for row in planned["routes"].values()}, {"create"})
            self.assertTrue(planned["routes"]["incoming"]["acs_name_in_query"])
            self.assertEqual(azure.writes(), [])
            with patch.object(delivery, "az", azure), self.assertRaisesRegex(ValueError, "--confirm-acs-name"):
                delivery.subscriptions(subscription_args(directory, endpoints, "--apply"))
            self.assertEqual(azure.writes(), [])

            applied = self.run_command(azure, delivery.subscriptions, subscription_args(directory, endpoints, *CONFIRM))
            topic, incoming, recording = azure.writes()
            self.assertEqual(topic[:3], ["eventgrid", "system-topic", "create"])
            self.assertEqual(topic[topic.index("--source") + 1], ACS_ID)
            self.assertEqual(topic[topic.index("--location") + 1], "global")
            self.assertEqual(incoming[incoming.index("--included-event-types") + 1], delivery.INCOMING)
            self.assertEqual(incoming[incoming.index("--advanced-filter"):][:4], ["--advanced-filter", "data.to.kind", "StringIn", "phoneNumber"])
            self.assertEqual(recording[recording.index("--included-event-types") + 1], delivery.RECORDING)
            self.assertNotIn("--advanced-filter", recording)
            for call in (incoming, recording):
                self.assertEqual(call[call.index("--azure-active-directory-tenant-id") + 1], TENANT)
                self.assertEqual(call[call.index("--azure-active-directory-application-id-or-uri") + 1], DELIVERY_APP)
            self.assertEqual(applied["status"], "subscriptions_verified")
            self.assertEqual(applied["routes"]["incoming"]["advanced_filters"], PHONE_FILTER)
            self.assertEqual(applied["routes"]["recording"]["advanced_filters"], [])
            for text in [json.dumps(planned), json.dumps(applied), *(path.read_text() for path in (directory / "subscriptions").iterdir())]:
                self.assertNotIn("contact-center.example.com/voice", text)

            with patch.object(delivery, "az", azure):
                verified = output_of(delivery.verify, subscription_args(directory, endpoints, command="verify"), FakeGraph().configured())
            self.assertEqual(verified["status"], "verified")
            self.assertTrue(verified["delivery_app_verified"])
            self.assertIn("Last verified: November 2026", verified["exit_gate"])
            self.assertIn("left navigation", verified["exit_gate"])

    def test_missing_or_unsafe_endpoints_are_refused_before_any_azure_call(self):
        cases = (
            ({"RECORDING_WEBHOOK_URL": None}, "missing"),
            ({"INCOMING_CALL_WEBHOOK_URL": "https://ca-voice.example.azurecontainerapps.io/acs/incoming-call"}, "container app"),
            ({"RECORDING_WEBHOOK_URL": INCOMING_URL}, "identical"),
            ({"INCOMING_CALL_WEBHOOK_URL": "http://contact-center.example.com/voice/incoming"}, "HTTPS"),
            ({"EXTRA_SETTING": "value"}, "unexpected"),
        )
        for overrides, message in cases:
            with self.subTest(message=message), private_test_directory() as directory:
                azure = FakeAzure()
                with patch.object(delivery, "az", azure), self.assertRaisesRegex(ValueError, message):
                    delivery.subscriptions(subscription_args(directory, write_endpoints(directory, **overrides)))
                self.assertEqual(azure.calls, [])
        with private_test_directory() as directory:
            endpoints = write_endpoints(directory)
            os.chmod(endpoints, 0o640)
            with patch.object(delivery, "az", FakeAzure()), self.assertRaisesRegex(ValueError, "chmod 600"):
                delivery.subscriptions(subscription_args(directory, endpoints))

    def test_wrong_tenant_or_app_is_refused(self):
        for overrides, account_tenant, message in (
            ({"EVENT_GRID_APP_TENANT_ID": OTHER_TENANT}, TENANT, "tenant id"),
            ({"EVENT_GRID_APP_ID": OTHER_PRINCIPAL}, TENANT, "app id"),
            ({}, OTHER_TENANT, "not in the Dynamics tenant"),
        ):
            with self.subTest(message=message), private_test_directory() as directory:
                azure = FakeAzure()
                azure.tenant = account_tenant
                with patch.object(delivery, "az", azure), self.assertRaisesRegex(ValueError, message):
                    delivery.subscriptions(subscription_args(directory, write_endpoints(directory, **overrides)))
                self.assertEqual(azure.writes(), [])
                self.assertFalse((directory / "subscriptions").exists())

    def test_duplicate_or_foreign_routes_block_the_plan(self):
        cases = (
            ({"legacy-incoming": route("legacy-incoming", "https://other.example.com/incoming", delivery.INCOMING, PHONE_FILTER)}, "doesn't match the planned route"),
            ({"all-events": route("all-events", "https://other.example.com/all", None, [])}, "event types differ"),
            ({"native-dynamics-incoming": route("native-dynamics-incoming", RECORDING_URL, delivery.RECORDING, [])}, "doesn't match the planned route"),
            ({"no-filter": route("no-filter", INCOMING_URL, delivery.INCOMING, [])}, "advanced filters differ"),
            ({name: route(name, INCOMING_URL, delivery.INCOMING, PHONE_FILTER) for name in ("first-copy", "second-copy")}, "Duplicate"),
        )
        for subscriptions, message in cases:
            with self.subTest(message=message, names=sorted(subscriptions)), private_test_directory() as directory:
                azure = FakeAzure()
                azure.topics, azure.subscriptions = [existing_topic()], subscriptions
                with patch.object(delivery, "az", azure), self.assertRaisesRegex(RuntimeError, message):
                    delivery.subscriptions(subscription_args(directory, write_endpoints(directory)))
                self.assertEqual(azure.writes(), [])
                self.assertFalse((directory / "subscriptions").exists())

    def test_exact_existing_route_is_reused_and_the_passive_observer_is_allowed(self):
        with private_test_directory() as directory:
            azure, endpoints = FakeAzure(), write_endpoints(directory)
            azure.topics = [existing_topic()]
            azure.subscriptions = {
                "manual-incoming": route("manual-incoming", INCOMING_URL, delivery.INCOMING, PHONE_FILTER),
                delivery.OBSERVER: route(delivery.OBSERVER, "https://ca-voice.example.azurecontainerapps.io/dynamics/handover/probe/events",
                                         delivery.INCOMING, [], tenant=None, application=None, attributes=[{"name": "X-API-Key"}]),
                "sms": route("sms", "https://contact-center.example.com/sms", "Microsoft.Communication.SMSReceived", []),
            }
            planned = self.run_command(azure, delivery.subscriptions, subscription_args(directory, endpoints))
            self.assertEqual(planned["system_topic"]["action"], "reuse")
            self.assertEqual(planned["routes"]["incoming"]["name"], "manual-incoming")
            self.assertEqual(planned["routes"]["incoming"]["action"], "none")
            self.assertEqual(planned["routes"]["recording"]["action"], "create")
            self.assertEqual({row["kind"] for row in planned["other_subscriptions_reviewed"]}, {"passive observer", "unrelated event types"})
            applied = self.run_command(azure, delivery.subscriptions, subscription_args(directory, endpoints, *CONFIRM))
            self.assertEqual([call[call.index("--name") + 1] for call in azure.writes()], ["native-dynamics-recordings"])
            self.assertEqual(applied["routes"]["incoming"]["name"], "manual-incoming")

    def test_failed_handshake_stops_and_allows_one_explicit_retry(self):
        with private_test_directory() as directory:
            azure, endpoints = FakeAzure(), write_endpoints(directory)
            self.run_command(azure, delivery.subscriptions, subscription_args(directory, endpoints))
            azure.create_failures["native-dynamics-incoming"] = [delivery.CommandError("az eventgrid failed: Webhook validation handshake failed.")]
            with patch.object(delivery, "az", azure), self.assertRaisesRegex(RuntimeError, "handshake.*--retry-step incoming-subscription"):
                delivery.subscriptions(subscription_args(directory, endpoints, *CONFIRM))
            failure = json.loads((directory / "subscriptions/incoming-subscription.failed-1.json").read_text())
            self.assertFalse(failure["definite"])
            writes = len(azure.writes())
            with patch.object(delivery, "az", azure), self.assertRaisesRegex(RuntimeError, "No write was resent"):
                delivery.subscriptions(subscription_args(directory, endpoints, *CONFIRM))
            self.assertEqual(len(azure.writes()), writes)
            applied = self.run_command(
                azure, delivery.subscriptions, subscription_args(directory, endpoints, *CONFIRM, "--retry-step", "incoming-subscription"),
            )
            self.assertEqual(applied["steps"], {"system-topic": "unchanged", "incoming-subscription": "written", "recording-subscription": "written"})

    def test_verify_refuses_unfinished_routes_or_a_missing_event_grid_grant(self):
        with private_test_directory() as directory:
            azure, endpoints = FakeAzure(), write_endpoints(directory)
            azure.topics = [existing_topic()]
            azure.subscriptions = {
                "native-dynamics-incoming": route("native-dynamics-incoming", INCOMING_URL, delivery.INCOMING, PHONE_FILTER),
                "native-dynamics-recordings": route("native-dynamics-recordings", RECORDING_URL, delivery.RECORDING, [], state="Failed"),
            }
            arguments = subscription_args(directory, endpoints, command="verify")
            with patch.object(delivery, "az", azure), self.assertRaisesRegex(RuntimeError, "not Succeeded"):
                delivery.verify(arguments, FakeGraph().configured())
            azure.subscriptions["native-dynamics-recordings"]["provisioningState"] = "Succeeded"
            graph = FakeGraph().configured()
            graph.assigned[DELIVERY_SP] = graph.assigned[DELIVERY_SP][1:]
            with patch.object(delivery, "az", azure), self.assertRaisesRegex(RuntimeError, "Microsoft.EventGrid isn't assigned"):
                delivery.verify(arguments, graph)

    def test_documented_commands_parse_with_this_cli(self):
        values = {"TENANT_ID": TENANT, "NATIVE_ACS_ID": ACS_ID, "SYSTEM_TOPIC": "native-topic",
                  "EVENTGRID_APP_ID": DELIVERY_APP, "NATIVE_ACS_NAME": "native-acs", "WORK": "/private/work"}
        parsed = [delivery.parser().parse_args(command) for command in documented("native_event_delivery.py", values)]
        self.assertEqual({args.command for args in parsed}, {"bootstrap", "subscriptions", "verify"})
        for args in parsed:
            if getattr(args, "apply", False):
                self.assertTrue(getattr(args, "confirm_tenant", None) == TENANT or getattr(args, "confirm_acs_name", None) == "native-acs")

    def test_azure_cli_errors_never_echo_webhook_urls(self):
        error = subprocess.CalledProcessError(1, ["az", "--endpoint", INCOMING_URL], stderr=f"Webhook validation handshake failed for {INCOMING_URL}")
        with patch.object(delivery.subprocess, "run", side_effect=error):
            with self.assertRaises(delivery.CommandError) as raised:
                delivery.az(["eventgrid", "system-topic", "event-subscription", "create", "--endpoint", INCOMING_URL], secrets=(INCOMING_URL,))
        self.assertIn("handshake failed", str(raised.exception))
        self.assertNotIn("contact-center.example.com", str(raised.exception))
        self.assertNotIn("organization=example", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
