"""Exhaustive feature projection against a freshly compiled, offline ARM graph."""

import itertools
import json
import os
from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[2]
FLAGS = (
    "deployWebChat", "deployBrowserVoice", "deployApplicationPhone", "deployNativeVoice",
    "deployCrm", "enableFoundryIq", "deployEmail", "deployAgentConsole", "deployAgentAssist",
)


class Context:
    def __init__(self, template, bindings=None, parent=None):
        self.template, self.bindings, self.parent = template, bindings or {}, parent

    def parameter(self, name):
        if name in self.bindings:
            value = self.bindings[name]
            return self.parent.evaluate(value["value"]) if self.parent else value
        return self.template["parameters"][name].get("defaultValue")

    def evaluate(self, value):
        if not isinstance(value, str) or not value.startswith("["):
            return value
        expression = value[1:-1]
        tokens = re.findall(r"'(?:[^']|'')*'|[A-Za-z_][A-Za-z_0-9]*|[(),]", expression)
        if "".join(tokens) != re.sub(r"\s+", "", expression):
            raise AssertionError("Unsupported ARM condition: " + expression)
        position = 0

        def parse():
            nonlocal position
            token = tokens[position]
            position += 1
            if token.startswith("'"):
                return token[1:-1].replace("''", "'")
            if tokens[position] != "(":
                raise AssertionError("Expected ARM function call.")
            position += 1
            arguments = []
            while tokens[position] != ")":
                arguments.append(parse())
                if tokens[position] == ",":
                    position += 1
            position += 1
            if token == "parameters":
                return self.parameter(arguments[0])
            if token == "variables":
                return self.evaluate(self.template["variables"][arguments[0]])
            operations = {
                "and": lambda: all(arguments), "or": lambda: any(arguments),
                "not": lambda: not arguments[0], "empty": lambda: not arguments[0],
                "equals": lambda: arguments[0] == arguments[1],
            }
            if token not in operations:
                raise AssertionError("Unsupported module condition function: " + token)
            return operations[token]()

        result = parse()
        if position != len(tokens):
            raise AssertionError("Unconsumed ARM condition.")
        return result

    def active(self, resource):
        return not resource.get("existing", False) and self.evaluate(resource.get("condition", True))

    def child(self, resource):
        properties = resource["properties"]
        return Context(properties["template"], properties.get("parameters"), self)


class InfrastructureMatrixTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        name = os.environ.get("TEMPLATE_COMPILED_ARM")
        if not name:
            raise unittest.SkipTest("Set TEMPLATE_COMPILED_ARM to the fresh az bicep build output for the resource matrix.")
        path = Path(name)
        sources = (ROOT / "infra/main.bicep", ROOT / "infra/resources.bicep",
                   ROOT / "infra/dataplane.bicep", ROOT / "infra/foundry/foundry.bicep")
        if any(source.stat().st_mtime_ns > path.stat().st_mtime_ns for source in sources):
            raise AssertionError("Recompile Bicep before testing the module matrix.")
        cls.template = json.loads(path.read_text())
        cls.resources_module = next(resource for resource in cls.template["resources"]
                                    if resource["type"] == "Microsoft.Resources/deployments")

    def test_all_512_compiled_resource_feature_combinations(self):
        for choices in itertools.product((False, True), repeat=len(FLAGS)):
            flags = dict(zip(FLAGS, choices))
            root = Context(self.template, flags)
            context = root.child(self.resources_module)
            resources = context.template["resources"]
            active = {name for name, resource in resources.items() if context.active(resource)}
            chat, browser, phone, native, crm, knowledge, email, console, assist = choices
            expected = {
                "voiceApp": browser or phone or native,
                "orchestratorApp": chat,
                "uiApp": chat or browser or phone,
                "agentConsoleApp": console,
                "foundry": chat or knowledge or assist,
                "dataplane": crm or knowledge or assist,
                "knowledge": knowledge,
                "knowledgeConnection": knowledge,
                "acsEmail": phone or email,
                "emailService": email,
                "emailDomain": email,
                "emailDiagnostics": email,
                "callDiagnostics": phone,
                "acsSpeechAccess": phone and assist,
            }
            with self.subTest(flags=flags):
                for name, enabled in expected.items():
                    self.assertEqual(name in active, enabled, name)
                self.assertNotIn("speechAccount", active)
                self.assertNotIn("foundryProject", active)
                if "dataplane" in active:
                    data = context.child(resources["dataplane"])
                    for resource in data.template["resources"]:
                        kind = resource["type"]
                        if kind.startswith(("Microsoft.DocumentDB/", "Microsoft.Network/", "Microsoft.SignalRService/")) or kind == "Microsoft.App/managedEnvironments":
                            self.assertEqual(data.active(resource), assist, kind)
                        elif kind == "Microsoft.ManagedIdentity/userAssignedIdentities":
                            self.assertEqual(data.active(resource), crm)
                        elif kind == "Microsoft.App/containerApps":
                            self.assertTrue(data.active(resource))
                if "foundry" in active:
                    foundry = context.child(resources["foundry"])
                    models = [resource for resource in foundry.template["resources"]
                              if resource["type"] == "Microsoft.CognitiveServices/accounts/deployments"]
                    self.assertEqual([foundry.active(resource) for resource in models],
                                     [chat, assist, assist or knowledge])

    def test_model_writes_are_serialized_on_the_shared_account(self):
        resources = Context(self.template).child(self.resources_module)
        foundry = resources.child(resources.template["resources"]["foundry"])
        models = [resource for resource in foundry.template["resources"]
                  if resource["type"] == "Microsoft.CognitiveServices/accounts/deployments"]
        self.assertEqual(len(models), 3)
        first, assist, insights = (json.dumps(model["dependsOn"]) for model in models)
        self.assertNotIn("parameters('assistModelDeploymentName')", first)
        self.assertNotIn("parameters('insightsModelDeploymentName')", first)
        self.assertIn("parameters('modelDeploymentName')", assist)
        self.assertIn("parameters('modelDeploymentName')", insights)
        self.assertIn("parameters('assistModelDeploymentName')", insights)

    def test_native_only_has_no_application_acs_or_foundry(self):
        flags = {name: name == "deployNativeVoice" for name in FLAGS}
        context = Context(self.template, flags).child(self.resources_module)
        active = {name for name, resource in context.template["resources"].items() if context.active(resource)}
        self.assertIn("voiceApp", active)
        self.assertFalse(active & {"acsEmail", "emailService", "foundry", "knowledge", "dataplane", "uiApp"})


if __name__ == "__main__":
    unittest.main()
