"""Prepare a private, selected-module deployment; Azure and Dynamics keep their existing executors."""

import argparse
import copy
import functools
import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urlencode, urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from dotenv import dotenv_values

from native_voice_setup import env_file, guid, organization, private_path, read_private, write_private

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / "infra/template-profile.example.json"
MODULES = {
    "web-chat": (),
    "browser-voice": (),
    "application-phone": (),
    "native-voice": (),
    "crm": (),
    "knowledge": (),
    "email": (),
    "agent-console": ("web-chat",),
    "agent-assist": (),
    "native-workspace": ("native-voice", "crm"),
    "native-knowledge": ("native-workspace",),
    "native-copilot": ("native-knowledge",),
    "pause-ai": ("native-workspace",),
}
FLAGS = {
    "web-chat": "DEPLOY_WEB_CHAT",
    "browser-voice": "DEPLOY_BROWSER_VOICE",
    "application-phone": "DEPLOY_APPLICATION_PHONE",
    "native-voice": "DEPLOY_NATIVE_VOICE",
    "crm": "DEPLOY_CRM",
    "knowledge": "ENABLE_FOUNDRY_IQ",
    "email": "DEPLOY_EMAIL",
    "agent-console": "DEPLOY_AGENT_CONSOLE",
    "agent-assist": "DEPLOY_AGENT_ASSIST",
}
SERVICE_PROJECTS = {
    "orchestrator-api": "apps/chat/orchestrator",
    "ui-v2": "apps/chat/ui_v2",
    "agent-console": "apps/chat/agent_console",
    "assist": "apps/assist",
    "voice": "apps/voice/src/app",
}
UI_CONFIGS = (
    "apps/chat/ui_v2/src/config/template.json",
    "apps/chat/agent_console/src/config/template.json",
    "apps/assist/apps/console/src/config/template.json",
)
RUNTIME_CONFIGS = (
    "apps/voice/src/app/deployment.json",
    "apps/chat/orchestrator/deployment.json",
    "apps/assist/apps/console/src/config/deployment.json",
)
CONTENT_ROOTS = ("apps/voice/src/app", "apps/chat/orchestrator")
SENSITIVE_PATHS = {".git", ".azure", "untracked", "node_modules", ".next", "__pycache__", "dist", "build"}
ENVIRONMENT_EXAMPLES = {".env.example", ".env.template", ".env.local.template"}
PRIVATE_INPUTS = (
    ("brand", "logoFile"), ("assistant", "systemPromptFile"), ("assistant", "knowledgeFile"),
    ("assistant", "knowledgeArabicFile"), ("knowledge", "corpusFile"), ("demo", "seedFile"),
    ("demo", "profilesFile"),
)
HOOK_WRAPPER = Path("infra/scripts/hook_python.sh")

# Human gates that a module adds to the plan, in plan order.
MODULE_GATES = {
    "service-eligibility": ({"native-voice", "application-phone", "email"},
                            "Approve communications eligibility, licensing, data geography and charges."),
    "provider-secret": ({"browser-voice", "native-voice", "application-phone"},
                        "Enter HUMAIN authorization in the private store, never in chat or Git."),
    "crm": ({"crm"}, "Approve the Dataverse environment, isolated role/schema plan and any generated seed."),
    "native-ui": ({"native-voice"}, "Connect native ACS, obtain/sync numbers, create bot/workstream shells and review publication."),
    "workspace": ({"native-workspace"}, "Approve representative/profile/forms/knowledge bindings and verify the actual UI."),
    "native-copilot": ({"native-copilot"}, "Approve Copilot licensing and organization-wide settings; knowledge Q&A is not live-case querying."),
    "supervision": ({"pause-ai"}, "Approve actors, secured command schema/controller pins and the deployed-identity preflight."),
}
# One business line per module plus the licenses and permissions it needs beyond the Azure deployer role.
MODULE_GUIDE = {
    "web-chat": (
        "Customers chat with the AI assistant on your branded web page.",
        ("Foundry quota for the chat model (GlobalStandard) in the subscription.",),
    ),
    "browser-voice": (
        "Customers talk to the HUMAIN voice assistant in the browser, without a phone number.",
        ("A HUMAIN account and its issued Authorization value.",),
    ),
    "application-phone": (
        "Customers call a number that the app owns in Azure Communication Services; the voice app answers.",
        ("A HUMAIN Authorization value.",
         "Phone-number eligibility, purchase and call charges in Azure Communication Services.",
         "Permission to create the incoming-call Event Grid subscription."),
    ),
    "native-voice": (
        "Dynamics 365 Contact Center owns the call: the voice controller keeps control while the Azure voice "
        "app runs the AI conversation, then hands the same call to a native queue.",
        ("Dynamics 365 Contact Center or Customer Service voice entitlements, the voice channel and unified routing.",
         "Copilot Studio maker access in the same Power Platform environment.",
         "Azure and Dataverse in the same Entra tenant.",
         "An Entra application administrator for the Event Grid delivery app.",
         "A separate native Azure Communication Services resource and phone-number eligibility.",
         "A HUMAIN Authorization value."),
    ),
    "crm": (
        "The assistant reads customer context and creates or updates Dataverse cases through the Assist API.",
        ("A Dataverse environment with Dynamics 365 Customer Service.",
         "A Dynamics administrator to create the isolated security role and the managed-identity application user."),
    ),
    "knowledge": (
        "The assistant answers from approved web or indexed sources through Foundry IQ (Azure AI Search), via the Assist API.",
        ("Foundry quota for the retrieval model (the 'insights' deployment) and an Azure AI Search service.",
         "Approved source domains."),
    ),
    "email": (
        "The assistant sends messages through Azure Communication Services Email to approved recipients.",
        ("Azure Communication Services Email eligibility for the communications data geography.",
         "Approved recipients or recipient domains."),
    ),
    "agent-console": (
        "The custom console: your staff take over web-chat conversations in a separate web app, not in Dynamics.",
        ("No Dynamics license; staff use the custom console.",),
    ),
    "agent-assist": (
        "Agent-assist analytics: live transcript, sentiment and suggestions for app-owned voice and chat "
        "sessions in the custom Assist UI; not native Dynamics analytics.",
        ("Foundry quota for the assist and insights models.",
         "Cosmos DB, SignalR and private networking in the hosting region."),
    ),
    "native-workspace": (
        "Sets up the Dynamics representative workspace for handed-over calls: profile, forms, tabs, scripts and macros.",
        ("Licensed representatives with Customer Service or Contact Center and voice entitlements.",
         "System Administrator or System Customizer rights to publish forms, scripts, macros and profiles."),
    ),
    "native-knowledge": (
        "Publishes the approved English and Arabic knowledge as native Dynamics knowledge articles for representatives.",
        ("Knowledge approval and publication rights.",
         "Approved knowledge content and its reviewed Arabic rendering."),
    ),
    "native-copilot": (
        "Native Copilot: the representative's assistant in Dynamics answers questions from the published "
        "articles; it doesn't query live cases.",
        ("Copilot licensing and region availability for the Dynamics environment.",
         "Tenant administrator approval of organization-wide Copilot and summary settings."),
    ),
    "pause-ai": (
        "Adds a Pause AI control for supervisors in the native conversation, backed by secured Dataverse commands.",
        ("The Omnichannel supervisor role and voice entitlement for each approved supervisor.",
         "Dataverse customization rights for the secured command table and roles."),
    ),
}
TERMS = {
    "native Copilot": "The representative's assistant inside Dynamics (native-copilot). It answers from published "
                      "knowledge articles; it isn't the voice AI and doesn't query live cases.",
    "voice controller": "The Copilot Studio topic in the native routing agent that keeps control of each Dynamics call "
                        "and drives the Azure voice app (native-voice).",
    "Assist API": "The shared service API in the assist container app. CRM, knowledge and agent-assist calls go "
                  "through it (crm, knowledge, agent-assist).",
    "custom console": "The web-chat takeover console (agent-console). It isn't the Dynamics representative workspace.",
    "agent-assist analytics": "The custom transcript, sentiment and suggestion stream with Cosmos DB, SignalR and "
                              "insight models (agent-assist). Native calls aren't mirrored into it.",
}

# Azure scope. Conditions list module sets; any fully selected set enables the entry. Sizes, SKUs and models
# are read from the Bicep, and test_template_setup.py checks this table against the compiled ARM template.
ALWAYS = ((),)
FOUNDRY = (("web-chat",), ("knowledge",), ("agent-assist",))
SERVICE_API = (("crm",), ("knowledge",), ("agent-assist",))
VOICE = (("browser-voice",), ("application-phone",), ("native-voice",))
CUSTOMER_UI = (("web-chat",), ("browser-voice",), ("application-phone",))
COMMUNICATIONS = (("application-phone",), ("email",))
BICEP = {
    "main": "infra/main.bicep", "resources": "infra/resources.bicep", "foundry": "infra/foundry/foundry.bicep",
    "knowledge": "infra/knowledge.bicep", "dataplane": "infra/dataplane.bicep",
}
LOCATION_SETTINGS = {
    "location": "AZURE_LOCATION", "hostingLocation": "APP_LOCATION", "cosmosLocation": "COSMOS_LOCATION",
    "searchLocation": "SEARCH_LOCATION",
}
# (label, ARM type, Bicep file and symbol, condition, location parameter, count)
AZURE_RESOURCES = (
    ("Resource group", "Microsoft.Resources/resourceGroups", ("main", "rg"), ALWAYS, "location", 1),
    ("Log Analytics workspace", "Microsoft.OperationalInsights/workspaces", ("resources", "logAnalytics"), ALWAYS, "location", 1),
    ("Application Insights", "Microsoft.Insights/components", ("resources", "appInsights"), ALWAYS, "location", 1),
    ("Shared runtime identity", "Microsoft.ManagedIdentity/userAssignedIdentities", ("resources", "uami"), ALWAYS, "location", 1),
    ("Container registry", "Microsoft.ContainerRegistry/registries", ("resources", "registry"), ALWAYS, "location", 1),
    ("Key Vault", "Microsoft.KeyVault/vaults", ("resources", "keyVault"), ALWAYS, "location", 1),
    ("Container Apps environment", "Microsoft.App/managedEnvironments", ("resources", "acaEnv"), ALWAYS, "hostingLocation", 1),
    ("Container Apps log export", "Microsoft.Insights/diagnosticSettings", ("resources", "runtimeDiagnostics"), ALWAYS, None, 1),
    ("Foundry account", "Microsoft.CognitiveServices/accounts", ("foundry", "account"), FOUNDRY, "location", 1),
    ("Foundry project", "Microsoft.CognitiveServices/accounts/projects", ("foundry", "project"), FOUNDRY, "location", 1),
    ("Azure AI Search", "Microsoft.Search/searchServices", ("knowledge", "search"), (("knowledge",),), "searchLocation", 1),
    ("Foundry connection to Search", "Microsoft.CognitiveServices/accounts/projects/connections",
     ("resources", "knowledgeConnection"), (("knowledge",),), None, 1),
    ("Assist API container app", "Microsoft.App/containerApps", ("dataplane", "assistApp"), SERVICE_API, "hostingLocation", 1),
    ("CRM identity", "Microsoft.ManagedIdentity/userAssignedIdentities", ("dataplane", "crmIdentity"), (("crm",),), "hostingLocation", 1),
    ("Virtual network", "Microsoft.Network/virtualNetworks", ("dataplane", "vnet"), (("agent-assist",),), "hostingLocation", 1),
    ("Cosmos DB account", "Microsoft.DocumentDB/databaseAccounts", ("dataplane", "cosmos"), (("agent-assist",),), "cosmosLocation", 1),
    ("Cosmos DB database", "Microsoft.DocumentDB/databaseAccounts/sqlDatabases", ("dataplane", "cosmosDb"), (("agent-assist",),), None, 1),
    ("Cosmos DB containers", "Microsoft.DocumentDB/databaseAccounts/sqlDatabases/containers",
     ("dataplane", "containers"), (("agent-assist",),), None, 3),
    ("Cosmos DB private endpoint", "Microsoft.Network/privateEndpoints", ("dataplane", "cosmosPe"), (("agent-assist",),), "hostingLocation", 1),
    ("Private DNS zone", "Microsoft.Network/privateDnsZones", ("dataplane", "documentsDnsZone"), (("agent-assist",),), "global", 1),
    ("Private DNS zone link", "Microsoft.Network/privateDnsZones/virtualNetworkLinks",
     ("dataplane", "documentsDnsLink"), (("agent-assist",),), "global", 1),
    ("Private DNS zone group", "Microsoft.Network/privateEndpoints/privateDnsZoneGroups",
     ("dataplane", "cosmosPeDns"), (("agent-assist",),), None, 1),
    ("Private Container Apps environment", "Microsoft.App/managedEnvironments", ("dataplane", "acaEnvVnet"),
     (("agent-assist",),), "hostingLocation", 1),
    ("SignalR", "Microsoft.SignalRService/signalR", ("dataplane", "signalr"), (("agent-assist",),), "hostingLocation", 1),
    ("Communication Services", "Microsoft.Communication/communicationServices", ("resources", "acsEmail"), COMMUNICATIONS, "global", 1),
    ("Email service", "Microsoft.Communication/emailServices", ("resources", "emailService"), (("email",),), "global", 1),
    ("Azure-managed email domain", "Microsoft.Communication/emailServices/domains", ("resources", "emailDomain"), (("email",),), "global", 1),
    ("Email delivery logs", "Microsoft.Insights/diagnosticSettings", ("resources", "emailDiagnostics"), (("email",),), None, 1),
    ("Call logs", "Microsoft.Insights/diagnosticSettings", ("resources", "callDiagnostics"), (("application-phone",),), None, 1),
    ("Voice app", "Microsoft.App/containerApps", ("resources", "voiceApp"), VOICE, "hostingLocation", 1),
    ("Orchestrator API", "Microsoft.App/containerApps", ("resources", "orchestratorApp"), (("web-chat",),), "hostingLocation", 1),
    ("Customer UI", "Microsoft.App/containerApps", ("resources", "uiApp"), CUSTOMER_UI, "hostingLocation", 1),
    ("Custom console", "Microsoft.App/containerApps", ("resources", "agentConsoleApp"), (("agent-console",),), "hostingLocation", 1),
)
# Resources whose data location is the communications geography, not the Azure region.
COMMUNICATIONS_DATA = {"acsEmail", "emailService"}
# (Foundry deployment symbol, deployment-name parameter, condition, purpose)
MODEL_DEPLOYMENTS = (
    ("gpt54mini", "modelDeploymentName", (("web-chat",),), "web-chat: the assistant's chat model."),
    ("assistModel", "assistModelDeploymentName", (("agent-assist",),),
     "agent-assist: sentiment, classifiers and knowledge in the Assist UI."),
    ("insightsModel", "insightsModelDeploymentName", (("knowledge",), ("agent-assist",)),
     "The retrieval model. The parameters call it 'insights', but Foundry IQ (knowledge) needs it too; "
     "agent-assist uses it for insights."),
)
MODEL_PROCESSING = {
    "GlobalStandard": "Global deployment: inference can run in any Azure region; stored data stays in the account's geography.",
    "DataZoneStandard": "Data zone deployment: inference stays within the account's data zone.",
    "Standard": "Regional deployment: inference runs in the account's region.",
}
# Container Apps (identity used by all apps: the shared runtime identity).
CONTAINER_APPS = (("resources", "orchestratorApp"), ("resources", "uiApp"), ("resources", "agentConsoleApp"),
                  ("dataplane", "assistApp"), ("resources", "voiceApp"))
# (principal, role, role definition as written in the Bicep, target, condition)
ROLE_ASSIGNMENTS = (
    ("Shared runtime identity", "AcrPull", "AcrPull", "Container registry", ALWAYS),
    ("Shared runtime identity", "Key Vault Secrets User", "Key Vault Secrets User", "Key Vault", ALWAYS),
    ("Shared runtime identity", "Cognitive Services OpenAI User", "5e0bd9bd-7b93-4f28-af87-19fc36ad61bd", "Foundry account", FOUNDRY),
    ("Shared runtime identity", "Azure AI Developer", "64702f94-c441-49e6-a78b-ef80e0188fee", "Foundry account", FOUNDRY),
    ("Shared runtime identity", "Azure AI User", "53ca6127-db72-4b80-b1b0-d745d6d5456d", "Foundry project", FOUNDRY),
    ("Deploying user", "Azure AI User", "53ca6127-db72-4b80-b1b0-d745d6d5456d", "Foundry project", FOUNDRY),
    ("Deploying user", "Cognitive Services OpenAI User", "5e0bd9bd-7b93-4f28-af87-19fc36ad61bd", "Foundry account", FOUNDRY),
    ("Deploying user", "Azure AI Developer", "64702f94-c441-49e6-a78b-ef80e0188fee", "Foundry account", FOUNDRY),
    ("Shared runtime identity", "Search Index Data Reader", "1407120a-92aa-4202-b7e9-c0e197c71c8f", "Azure AI Search", (("knowledge",),)),
    ("Foundry project identity", "Search Index Data Reader", "1407120a-92aa-4202-b7e9-c0e197c71c8f", "Azure AI Search", (("knowledge",),)),
    ("Deploying user", "Search Index Data Contributor", "8ebe5a00-799e-43f5-93ac-243d3dce84a7", "Azure AI Search", (("knowledge",),)),
    ("Deploying user", "Search Service Contributor", "7ca78c08-252a-4471-8644-bb5ff32d4ba0", "Azure AI Search", (("knowledge",),)),
    ("Search identity", "Cognitive Services OpenAI User", "5e0bd9bd-7b93-4f28-af87-19fc36ad61bd", "Foundry account", (("knowledge",),)),
    ("Communication Services identity", "Cognitive Services User", "a97b65f3-24c7-4388-baec-2e87135dc908", "Foundry account",
     (("application-phone", "agent-assist"),)),
    ("Shared runtime identity", "Cosmos DB Built-in Data Contributor (data plane)", "00000000-0000-0000-0000-000000000002",
     "Cosmos DB account", (("agent-assist",),)),
)
# (identity, kind, condition, what it does)
IDENTITIES = (
    ("Shared runtime identity", "user-assigned", ALWAYS,
     "Every container app runs as it: image pulls, Key Vault secrets and calls to Foundry, Search and Cosmos DB."),
    ("CRM identity", "user-assigned", (("crm",),),
     "The Assist API's Dataverse identity; CRM setup makes it a Dataverse application user."),
    ("Foundry account identity", "system-assigned", FOUNDRY, "Created with the Foundry account; no role assignments here."),
    ("Foundry project identity", "system-assigned", FOUNDRY, "Reads the Search index when knowledge is selected."),
    ("Search identity", "system-assigned", (("knowledge",),), "Calls the retrieval model for Foundry IQ."),
    ("Communication Services identity", "system-assigned", COMMUNICATIONS,
     "Uses Foundry speech for call transcription when application-phone and agent-assist are both selected."),
    ("Deploying user", "signed-in user", ALWAYS,
     "The person who runs azd (AZURE_PRINCIPAL_ID); gets Foundry and Search administration roles."),
)
OUTSIDE_BICEP = {
    "application-phone": "The phone number purchase and the incoming-call Event Grid subscription (connect_phone_events.py).",
    "native-voice": "The native Communication Services resource, its Event Grid topic and subscriptions, the Event Grid "
                    "delivery app, the voice app's Dataverse application user, the Dynamics voice channel, workstream "
                    "and queue, and the Copilot Studio routing agent (native runbook).",
    "crm": "The Dataverse security role, application user and schema for the CRM identity (setup_dynamics_crm.py).",
    "knowledge": "The Search index, knowledge sources and knowledge bases (postprovision hook, setup_knowledge.py).",
    "native-workspace": "Dataverse workspace configuration only (setup_dynamics_workspace.py).",
    "native-knowledge": "Dynamics knowledge articles only (setup_dynamics_workspace.py).",
    "native-copilot": "Dynamics Copilot settings only (setup_dynamics_workspace.py).",
    "pause-ai": "Dataverse command table, roles and controller pins only (setup_native_supervision.py).",
}
AZD_PREVIEW_NOTE = ("The azd preview table is partial: it omits Communication Services, Email, identities and role "
                    "assignments. Review this scope as well.")
PRICES_API = "https://prices.azure.com/api/retail/prices"
CONTAINER_APPS_METERS = {
    "Standard vCPU Active Usage": "1 Second", "Standard vCPU Idle Usage": "1 Second",
    "Standard Memory Active Usage": "1 GiB Second", "Standard Memory Idle Usage": "1 GiB Second",
}
HOURS_PER_MONTH = 730


def shape(value, keys, label):
    if not isinstance(value, dict) or set(value) != set(keys):
        raise ValueError(f"{label} must contain exactly the documented fields.")


def text(value, maximum, label, *, empty=False):
    if not isinstance(value, str) or "\x00" in value or len(value) > maximum or value != value.strip() or (not value and not empty):
        raise ValueError(f"{label} must be a trimmed string of at most {maximum} characters.")
    return value


def localized(value, label, *, empty=False):
    shape(value, {"en", "ar"}, label)
    for language, item in value.items():
        text(item, 1000, f"{label}.{language}", empty=empty)


def domains(values, label):
    if not isinstance(values, list) or len(values) > 30 or any(not isinstance(value, str) for value in values) or len(set(values)) != len(values):
        raise ValueError(f"{label} must be a bounded list of distinct DNS names.")
    for value in values:
        if not isinstance(value, str) or not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?\.[a-z]{2,63}", value):
            raise ValueError(f"{label} must contain lowercase DNS names, not URLs or wildcards.")


def optional_private_file(value, label, *, allow_packaged=False, binary=False):
    if value is not None:
        text(value, 4096, label)
        path = Path(value).expanduser().resolve()
        if path.is_relative_to(ROOT) and not (allow_packaged and path.is_relative_to(ROOT / ".deployment-inputs")):
            raise ValueError(f"{label} belongs in the private deployment workspace, not the repository.")
        if not path.is_file():
            raise ValueError(f"{label} must identify an existing private file.")
        if path.stat().st_size > 8 * 1024 * 1024:
            raise ValueError(f"{label} exceeds the private input size limit.")
        if binary:
            if os.name != "nt" and path.stat().st_mode & 0o077:
                raise ValueError(f"{label} must be owner-only (chmod 600).")
        else:
            read_private(path)


def logo_extension(path):
    source = Path(path)
    if source.stat().st_size > 2 * 1024 * 1024:
        raise ValueError("Brand logos must be at most 2 MiB.")
    with source.open("rb") as image:
        header = image.read(32)
    if header.startswith(b"\x89PNG\r\n\x1a\n") and len(header) >= 24:
        return "png"
    if header.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if header.startswith(b"RIFF") and header[8:12] == b"WEBP":
        return "webp"
    raise ValueError("Use a PNG, JPEG or WebP logo; active SVG/HTML uploads are not accepted.")


def resolve_modules(selected):
    if not isinstance(selected, list) or not selected or any(not isinstance(item, str) or item not in MODULES for item in selected):
        raise ValueError("Select one or more documented modules.")
    if len(set(selected)) != len(selected):
        raise ValueError("Module selections must be unique.")
    resolved = set(selected)
    while True:
        expanded = resolved | {dependency for item in resolved for dependency in MODULES[item]}
        if expanded == resolved:
            return sorted(resolved)
        resolved = expanded


def services_for(modules):
    selected = set(modules)
    services = set()
    if "web-chat" in selected:
        services.add("orchestrator-api")
    if selected & {"web-chat", "browser-voice", "application-phone"}:
        services.add("ui-v2")
    if "agent-console" in selected:
        services.add("agent-console")
    if selected & {"crm", "knowledge", "agent-assist"}:
        services.add("assist")
    if selected & {"browser-voice", "application-phone", "native-voice"}:
        services.add("voice")
    return [name for name in SERVICE_PROJECTS if name in services]


def validate(value, *, require_target=False, allow_packaged=False):
    defaults = json.loads(EXAMPLE.read_text())
    shape(value, defaults, "profile")
    if type(value["schemaVersion"]) is not int or value["schemaVersion"] != 1:
        raise ValueError("Unsupported profile schemaVersion.")
    if not isinstance(value["name"], str) or not re.fullmatch(r"[a-z][a-z0-9-]{2,31}", value["name"]):
        raise ValueError("Use a 3-32 character lowercase deployment name.")
    modules = resolve_modules(value["modules"])
    shape(value["azure"], defaults["azure"], "azure")
    for key in ("tenantId", "subscriptionId"):
        text(value["azure"][key], 36, "azure." + key, empty=True)
        if value["azure"][key] or require_target:
            guid(value["azure"][key])
    text(value["azure"]["location"], 50, "azure.location")
    text(value["azure"]["communicationsDataLocation"], 60, "azure.communicationsDataLocation")
    if not re.fullmatch(r"[A-Za-z][A-Za-z ]{1,59}", value["azure"]["communicationsDataLocation"]):
        raise ValueError("Use the provider's communications geography name; confirm its availability before provisioning.")
    if not re.fullmatch(r"[a-z0-9]+", value["azure"]["location"]):
        raise ValueError("Use the Azure region identifier.")
    group = text(value["azure"]["resourceGroup"], 90, "azure.resourceGroup", empty=True)
    if group and not re.fullmatch(r"[A-Za-z0-9_.()-]+", group):
        raise ValueError("Use an explicit Azure resource group name.")
    shape(value["dynamics"], defaults["dynamics"], "dynamics")
    org = text(value["dynamics"]["organizationUrl"], 300, "dynamics.organizationUrl", empty=True)
    if org or require_target and set(modules) & {"native-voice", "crm"}:
        organization(org)
    shape(value["brand"], defaults["brand"], "brand")
    localized(value["brand"]["name"], "brand.name")
    localized(value["brand"]["assistantName"], "brand.assistantName")
    shape(value["brand"]["colors"], defaults["brand"]["colors"], "brand.colors")
    for color in value["brand"]["colors"].values():
        if not isinstance(color, str) or not re.fullmatch(r"#[0-9a-fA-F]{6}", color):
            raise ValueError("Brand colors must be six-digit hexadecimal CSS colors.")
    if not isinstance(value["brand"]["layout"], str) or value["brand"]["layout"] not in {"split", "chat-first", "voice-first"}:
        raise ValueError("Choose split, chat-first or voice-first layout.")
    logo = value["brand"]["logoUrl"]
    optional_private_file(value["brand"]["logoFile"], "brand.logoFile", allow_packaged=allow_packaged, binary=True)
    if logo and value["brand"]["logoFile"]:
        raise ValueError("Choose either an approved logo URL or a private logo file.")
    if value["brand"]["logoFile"]:
        logo_extension(value["brand"]["logoFile"])
    if logo is not None:
        url = urlsplit(text(logo, 2000, "brand.logoUrl"))
        if url.scheme != "https" or not url.hostname or url.username or url.password or url.query or url.fragment:
            raise ValueError("Use a public query-free HTTPS logo URL, or privately stage the image file; never expose a signed URL.")
    shape(value["locale"], defaults["locale"], "locale")
    languages = value["locale"]["supported"]
    if not isinstance(languages, list) or not languages or any(not isinstance(item, str) for item in languages) or set(languages) - {"en", "ar"} or len(set(languages)) != len(languages):
        raise ValueError("The current runtime supports distinct en/ar language selections.")
    if value["locale"]["default"] not in languages:
        raise ValueError("Default language must be selected.")
    code = value["locale"]["dialingCountryCode"]
    if not isinstance(code, str) or code and not re.fullmatch(r"\+[1-9]\d{0,3}", code):
        raise ValueError("Dialing country code must be empty or a +country-code value.")
    try:
        ZoneInfo(value["locale"]["timeZone"])
    except (ZoneInfoNotFoundError, TypeError):
        raise ValueError("Use an installed IANA time zone.") from None
    shape(value["welcome"], defaults["welcome"], "welcome")
    localized(value["welcome"]["title"], "welcome.title")
    localized(value["welcome"]["description"], "welcome.description", empty=True)
    if not isinstance(value["quickActions"], list) or len(value["quickActions"]) > 12:
        raise ValueError("Supply at most twelve approved suggested prompts.")
    keys = set()
    for action in value["quickActions"]:
        shape(action, {"id", "label", "prompt"}, "quickAction")
        key = text(action["id"], 40, "quickAction.id")
        if not re.fullmatch(r"[a-z][a-z0-9-]*", key) or key in keys:
            raise ValueError("Suggested prompt IDs must be unique slugs.")
        keys.add(key)
        localized(action["label"], "quickAction.label")
        localized(action["prompt"], "quickAction.prompt")
    shape(value["assistant"], defaults["assistant"], "assistant")
    localized(value["assistant"]["greeting"], "assistant.greeting")
    for key in ("systemPromptFile", "knowledgeFile", "knowledgeArabicFile"):
        optional_private_file(value["assistant"][key], "assistant." + key, allow_packaged=allow_packaged)
    if require_target and "native-knowledge" in modules and not all(value["assistant"][key] for key in ("knowledgeFile", "knowledgeArabicFile")):
        raise ValueError("Native knowledge requires the approved canonical content and its reviewed Arabic rendering.")
    shape(value["knowledge"], defaults["knowledge"], "knowledge")
    if not isinstance(value["knowledge"]["mode"], str) or value["knowledge"]["mode"] not in {"web", "indexed"}:
        raise ValueError("Knowledge mode must be web or indexed.")
    domains(value["knowledge"]["allowedDomains"], "knowledge.allowedDomains")
    optional_private_file(value["knowledge"]["corpusFile"], "knowledge.corpusFile", allow_packaged=allow_packaged)
    if require_target and "knowledge" in modules and not value["knowledge"]["allowedDomains"]:
        raise ValueError("Knowledge retrieval requires deployment-approved source domains.")
    shape(value["business"], defaults["business"], "business")
    categories = value["business"]["caseCategories"]
    if not isinstance(categories, list) or not 1 <= len(categories) <= 30 or any(not isinstance(item, str) for item in categories) or len(set(categories)) != len(categories):
        raise ValueError("Provide 1-30 distinct service category identifiers.")
    if any(not isinstance(item, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", item) for item in categories):
        raise ValueError("Service category identifiers must be short lowercase slugs.")
    if type(value["business"]["locationRequired"]) is not bool:
        raise ValueError("business.locationRequired must be a boolean.")
    text(value["business"]["city"], 120, "business.city", empty=True)
    shape(value["emailPolicy"], defaults["emailPolicy"], "emailPolicy")
    domains(value["emailPolicy"]["allowedDomains"], "emailPolicy.allowedDomains")
    recipients = value["emailPolicy"]["allowedRecipients"]
    if not isinstance(recipients, list) or len(recipients) > 50 or any(
        not isinstance(item, str) or not re.fullmatch(r"[^\s@<>]+@[^\s@<>]+\.[^\s@<>]+", item) for item in recipients
    ):
        raise ValueError("Email recipients must be a bounded list of explicitly approved addresses.")
    if require_target and "email" in modules and not recipients and not value["emailPolicy"]["allowedDomains"]:
        raise ValueError("Email requires an approved recipient or recipient domain; generated contacts are not delivery approval.")
    shape(value["phonePolicy"], defaults["phonePolicy"], "phonePolicy")
    numbers = value["phonePolicy"]["allowedOutboundNumbers"]
    if (not isinstance(numbers, list) or len(numbers) > 20
            or any(not isinstance(number, str) or not re.fullmatch(r"\+[1-9][0-9]{6,14}", number) for number in numbers)
            or len(set(numbers)) != len(numbers)):
        raise ValueError("Outbound phone policy must contain distinct normalized E.164 destinations.")
    if numbers and "application-phone" not in modules:
        raise ValueError("Outbound phone destinations require the application-phone module.")
    shape(value["demo"], defaults["demo"], "demo")
    if not isinstance(value["demo"]["mode"], str) or value["demo"]["mode"] not in {"none", "generate"} or type(value["demo"]["approved"]) is not bool:
        raise ValueError("Demo mode must be none/generate with a boolean approval.")
    for key in ("seedFile", "profilesFile"):
        optional_private_file(value["demo"][key], "demo." + key, allow_packaged=allow_packaged)
    if value["demo"]["mode"] == "none" and any(value["demo"][key] for key in ("seedFile", "profilesFile")):
        raise ValueError("Private seed/profile inputs require the explicit generate mode.")
    if value["demo"]["approved"] and (value["demo"]["mode"] != "generate" or not value["demo"]["seedFile"]):
        raise ValueError("Demo approval must reference the actual generated private seed.")
    if value["demo"]["mode"] == "generate" and "crm" not in modules:
        raise ValueError("Generated business records require the CRM module; do not fake a non-CRM case backend.")
    tools = value["additionalTools"]
    if not isinstance(tools, list) or len(tools) > 8:
        raise ValueError("List at most eight additional tool requirements.")
    tool_names = set()
    for tool in tools:
        shape(tool, {"name", "purpose"}, "additionalTool")
        name = text(tool["name"], 64, "additionalTool.name")
        if not re.fullmatch(r"[a-z][a-z0-9_]{1,63}", name) or name in tool_names:
            raise ValueError("Additional tool requirements need distinct lowercase tool identifiers.")
        tool_names.add(name)
        text(tool["purpose"], 2000, "additionalTool.purpose")
    shape(value["approvals"], defaults["approvals"], "approvals")
    if any(type(approved) is not bool for approved in value["approvals"].values()):
        raise ValueError("Approval fields must be explicit booleans.")
    if value["approvals"]["outboundDestinations"] and not numbers:
        raise ValueError("Outbound approval must identify the actual destination allowlist.")
    return copy.deepcopy(value)


def public_config(profile):
    selected = set(resolve_modules(profile["modules"]))
    brand = {key: copy.deepcopy(value) for key, value in profile["brand"].items() if key != "logoFile"}
    if profile["brand"]["logoFile"]:
        brand["logoUrl"] = "/brand-logo." + logo_extension(profile["brand"]["logoFile"])
    return {
        "schemaVersion": 1, "brand": brand,
        "locale": {key: copy.deepcopy(profile["locale"][key]) for key in ("default", "supported", "dialingCountryCode")},
        "features": {
            "chat": "web-chat" in selected, "voice": "browser-voice" in selected,
            "phone": "application-phone" in selected and bool(profile["phonePolicy"]["allowedOutboundNumbers"])
                     and profile["approvals"]["outboundDestinations"],
            "agentConsole": "agent-console" in selected, "agentAssist": "agent-assist" in selected,
            "profiles": "crm" in selected and bool(profile["demo"]["approved"] and profile["demo"]["profilesFile"]),
        },
        "welcome": copy.deepcopy(profile["welcome"]), "quickActions": copy.deepcopy(profile["quickActions"]),
    }


def runtime_config(profile):
    selected = set(resolve_modules(profile["modules"]))
    language = profile["locale"]["default"]
    return {
        "schemaVersion": 1,
        "organization": {
            "name": profile["brand"]["name"][language], "assistantName": profile["brand"]["assistantName"][language],
            "defaultLanguage": language, "timeZone": profile["locale"]["timeZone"],
            "greeting": copy.deepcopy(profile["assistant"]["greeting"]),
        },
        "features": {
            "crm": "crm" in selected, "knowledge": "knowledge" in selected, "email": "email" in selected,
            "agentAssist": "agent-assist" in selected, "applicationTelephony": "application-phone" in selected,
            "browserVoice": "browser-voice" in selected, "agentConsole": "agent-console" in selected,
            "nativeVoice": "native-voice" in selected,
        },
        "business": copy.deepcopy(profile["business"]),
        "knowledge": {"allowedDomains": list(profile["knowledge"]["allowedDomains"])},
        "emailPolicy": copy.deepcopy(profile["emailPolicy"]),
        "phonePolicy": copy.deepcopy(profile["phonePolicy"]),
    }


def deployment_values(profile):
    selected = set(resolve_modules(profile["modules"]))
    return {
        "AZURE_ENV_NAME": profile["name"],
        "AZURE_TENANT_ID": profile["azure"]["tenantId"],
        "AZURE_SUBSCRIPTION_ID": profile["azure"]["subscriptionId"],
        "AZURE_LOCATION": profile["azure"]["location"],
        "APP_LOCATION": profile["azure"]["location"],
        "COSMOS_LOCATION": profile["azure"]["location"],
        "SEARCH_LOCATION": profile["azure"]["location"],
        "ACS_DATA_LOCATION": profile["azure"]["communicationsDataLocation"],
        "AZURE_RESOURCE_GROUP": profile["azure"]["resourceGroup"] or "rg-" + profile["name"],
        "FOUNDRY_ACCOUNT_BASE_NAME": "aif-" + profile["name"],
        "FOUNDRY_PROJECT_NAME": profile["name"],
        "FOUNDRY_AGENT_NAME": profile["name"] + "-assistant",
        **{key: str(module in selected).lower() for module, key in FLAGS.items()},
        "FOUNDRY_IQ_MODE": profile["knowledge"]["mode"],
        "DYNAMICS_ORG_URL": profile["dynamics"]["organizationUrl"],
        "HUMAIN_LLM_PROVIDER": "allam", "HUMAIN_VOICE": "mul_nourah", "HUMAIN_ASR_MODEL": "bayan_playground",
        "HUMAIN_TURN_TAKING_STRATEGY": "fast_vad", "HUMAIN_INTERRUPTION_POLICY_MODE": "off",
        "ACS_AUTO_CONFERENCE_ENABLED": "false", "DYNAMICS_OUTBOUND_ENABLED": "false",
        "DYNAMICS_NATIVE_MEDIA_ENABLED": "false", "DYNAMICS_NATIVE_SUPERVISION_ENABLED": "false",
        "DYNAMICS_NATIVE_MEDIA_TRANSPORT": "voip" if "native-voice" in selected else "callautomation",
    }


def plan(profile):
    resolved = resolve_modules(profile["modules"])
    gates = [
        {"id": "content", "human": True, "purpose": "Approve the inferred brand, UI, persona, source facts, modules and additional-tool plan."},
        {"id": "azure-access", "human": True, "purpose": "Authenticate the target tenant/subscription and approve regions, quotas and resource reuse."},
        {"id": "azure-preview", "human": True, "purpose": "Review Bicep changes, costs and identities before provisioning."},
    ]
    gates.extend({"id": gate, "human": True, "purpose": purpose}
                 for gate, (modules, purpose) in MODULE_GATES.items() if modules & set(resolved))
    if profile["additionalTools"]:
        gates.append({"id": "additional-tools", "human": True, "purpose": "Review generated adapters, tests, permissions, credential references and external side effects."})
    if profile["demo"]["mode"] == "generate":
        gates.append({"id": "generated-data", "human": True, "purpose": "Approve natural-looking fictional records privately; verify all contact destinations are safe."})
    if profile["phonePolicy"]["allowedOutboundNumbers"]:
        gates.append({"id": "outbound-destinations", "human": True, "purpose": "Approve these exact dialing destinations separately from generated records; every charged test call still needs fresh consent."})
    gates.append({"id": "acceptance", "human": True, "purpose": "Verify selected UI/audio/business actions. Each charged or recorded call needs fresh consent."})
    return {
        "schemaVersion": 1, "requestedModules": profile["modules"], "resolvedModules": resolved,
        "addedDependencies": sorted(set(resolved) - set(profile["modules"])), "services": services_for(resolved),
        "humanGates": gates, "configurationOnly": True, "cloudChanges": False,
        "missingInputs": missing_inputs(profile),
        "modules": module_catalog(profile["modules"]),
        "commonGates": ["content", "azure-access", "azure-preview", "acceptance"],
        "terms": TERMS,
        "limitations": [
            "The native voice and managed-identity CRM recipes require aligned Azure/Dataverse tenants; no silent cross-tenant authentication substitution.",
            "The complete clean-tenant replay remains unverified.",
            "One native AI call at a time; the current media process uses a 20-minute active phase.",
            "First-notice delay remains work in progress and on hold.",
        ],
    }


def module_catalog(requested):
    """Every module, selected or not, so the agent can explain each option and each exclusion."""
    resolved = set(resolve_modules(requested))
    catalog = []
    for module, (summary, needs) in MODULE_GUIDE.items():
        entry = {"module": module}
        if module in requested:
            entry.update(decision="include", reason="Requested.")
        elif module in resolved:
            dependents = sorted(item for item in resolved if module in resolve_modules([item]) and item != module)
            entry.update(decision="include", reason="Required by " + ", ".join(dependents) + ".")
        else:
            entry.update(decision="exclude", reason="Not requested. Tell the owner why it is left out.")
        entry.update(summary=summary, dependsOn=list(MODULES[module]), services=services_for([module]),
                     licensesAndPermissions=list(needs),
                     humanGates=[gate for gate, (modules, _) in MODULE_GATES.items() if module in modules])
        if entry["decision"] == "exclude":
            added = set(resolve_modules(sorted(resolved | {module})))
            entry["addsIfSelected"] = {"modules": sorted(added - resolved),
                                       "services": sorted(set(services_for(sorted(added))) - set(services_for(sorted(resolved))))}
        catalog.append(entry)
    return catalog


def selected_by(condition, modules):
    """Return the selected modules that enable a scope entry, or None when it is not deployed."""
    matched = [alternative for alternative in condition if set(alternative) <= modules]
    if not matched:
        return None
    return sorted({module for alternative in matched for module in alternative}) or ["platform"]


@functools.lru_cache(maxsize=None)
def bicep_declaration(path, symbol):
    match = re.search(rf"^(?:resource|module) {symbol} '[^']+' = .*?^[}}\]]$", Path(path).read_text(), re.M | re.S)
    return match.group(0) if match else None


def bicep_block(source, symbol):
    block = bicep_declaration(str(ROOT / BICEP[source]), symbol)
    if block is None:
        raise ValueError(f"{BICEP[source]} no longer declares {symbol}. Update the scope table in template_setup.py.")
    return block


def bicep_value(source, symbol, pattern, label):
    match = re.search(pattern, bicep_block(source, symbol), re.S)
    if not match:
        raise ValueError(f"Can't read {label} of {symbol} in {BICEP[source]}. Update the scope reader in template_setup.py.")
    return match.group(1)


def bicep_sku(source, symbol):
    block = bicep_block(source, symbol)
    for pattern in (r"\bacrSku:\s*'([^']+)'", r"\bskuName:\s*'([^']+)'", r"\bsku:\s*'([^']+)'",
                    r"\bsku:\s*\{\s*name:\s*'([^']+)'", r"\bworkloadProfileType:\s*'([^']+)'"):
        match = re.search(pattern, block)
        if match:
            return match.group(1)
    return "Serverless" if "'EnableServerless'" in block else None


def parameter_default(name):
    """Return the azd setting and default value that main.parameters.json maps to a main.bicep parameter."""
    value = json.loads((ROOT / "infra/main.parameters.json").read_text())["parameters"][name]["value"]
    match = re.fullmatch(r"\$\{([A-Z0-9_]+)=([^}]*)\}", value)
    if not match:
        raise ValueError(f"infra/main.parameters.json maps {name} without a default. Update the scope reader.")
    return match.group(1), match.group(2)


def container_app(source, symbol):
    read = functools.partial(bicep_value, source, symbol)
    return {
        "app": read(r"\bname:\s*'(ca-[a-z0-9-]+)'", "the app name"),
        "service": read(r"'azd-service-name':\s*'([^']+)'", "the azd service name"),
        "minReplicas": int(read(r"\bminReplicas:\s*(\d+)", "minReplicas")),
        "maxReplicas": int(read(r"\bmaxReplicas:\s*(\d+)", "maxReplicas")),
        "cpu": float(read(r"\bcpu:\s*json\('([0-9.]+)'\)", "cpu")),
        "memoryGiB": float(read(r"\bmemory:\s*'([0-9.]+)Gi'", "memory")),
    }


def model_deployment(symbol, parameter):
    read = functools.partial(bicep_value, "foundry", symbol)
    capacity = read(r"\bsku:\s*\{\s*name:\s*'[^']+'\s*capacity:\s*(\w+)", "capacity")
    if not capacity.isdigit():
        capacity = bicep_value("resources", "foundry", rf"\b{capacity}:\s*(\d+)", capacity)
    setting, default = parameter_default(parameter)
    sku = read(r"\bsku:\s*\{\s*name:\s*'([^']+)'", "the SKU")
    tier = re.search(r"\bserviceTier:\s*'([^']+)'", bicep_block("foundry", symbol))
    return {
        "deployment": default, "deploymentSetting": setting,
        "model": read(r"\bmodel:\s*\{[^}]*?\bname:\s*'([^']+)'", "the model name"),
        "version": read(r"\bmodel:\s*\{[^}]*?\bversion:\s*'([^']+)'", "the model version"),
        "sku": sku, "capacity": int(capacity),
        "capacityMeaning": "Rate limit in thousands of tokens per minute, billed per token; not provisioned throughput.",
        "serviceTier": tier.group(1) if tier else "Standard",
        "processing": MODEL_PROCESSING.get(sku, "Check this deployment type's data processing terms."),
        **({"serviceTierNote": "Priority processing is billed at its own per-token rate."} if tier and tier.group(1) == "Priority" else {}),
    }


def scope(profile, *, prices=False):
    """List the Azure resources, identities, roles, models, geography and warm replicas for the selected modules."""
    modules = set(resolve_modules(profile["modules"]))
    values = deployment_values(profile)
    regions = {name: values[setting] for name, setting in LOCATION_SETTINGS.items()}
    regions["searchLocation"] = regions["searchLocation"] or regions["hostingLocation"]
    resources, apps = [], []
    for label, kind, (source, symbol), condition, location, count in AZURE_RESOURCES:
        enabled = selected_by(condition, modules)
        if not enabled:
            continue
        entry = {"resource": label, "type": kind, "count": count, "selectedBy": enabled}
        sku = bicep_sku(source, symbol)
        if sku:
            entry["sku"] = sku
        if location is None:
            entry["location"] = "same as its parent"
        elif location == "global":
            entry["location"] = "global"
        else:
            entry.update(location=regions[location], locationSetting=LOCATION_SETTINGS[location])
        if symbol in COMMUNICATIONS_DATA:
            entry.update(dataLocation=values["ACS_DATA_LOCATION"], dataLocationSetting="ACS_DATA_LOCATION")
        resources.append(entry)
        if (source, symbol) in CONTAINER_APPS:
            apps.append({**container_app(source, symbol), "location": entry["location"]})
    models = []
    for symbol, parameter, condition, purpose in MODEL_DEPLOYMENTS:
        enabled = selected_by(condition, modules)
        if enabled:
            models.append({**model_deployment(symbol, parameter), "selectedBy": enabled, "purpose": purpose,
                           "accountLocation": values["AZURE_LOCATION"]})
    warm = [app for app in apps if app["minReplicas"]]
    result = {
        "schemaVersion": 1, "resolvedModules": sorted(modules), "azdPreview": AZD_PREVIEW_NOTE,
        "resources": resources,
        "identities": [{"identity": name, "kind": kind, "role": purpose} for name, kind, condition, purpose in IDENTITIES
                       if selected_by(condition, modules)],
        "roleAssignments": [{"principal": principal, "role": role, "roleDefinition": definition, "scope": target,
                             "selectedBy": selected_by(condition, modules)}
                            for principal, role, definition, target, condition in ROLE_ASSIGNMENTS
                            if selected_by(condition, modules)],
        "models": models,
        "containerApps": apps,
        "warmReplicas": {
            "replicas": sum(app["minReplicas"] for app in warm),
            "vCpu": sum(app["minReplicas"] * app["cpu"] for app in warm),
            "memoryGiB": sum(app["minReplicas"] * app["memoryGiB"] for app in warm),
            "note": "These replicas run with no traffic. Container Apps bills them at the idle rate, so idle cost isn't zero.",
        },
        "outsideBicep": [{"module": module, "creates": OUTSIDE_BICEP[module]} for module in sorted(modules & set(OUTSIDE_BICEP))],
        "geographyNotes": [
            "Services outside Azure, such as Dynamics 365 and the voice provider, aren't listed here; review their terms separately.",
            *(["Knowledge web mode also retrieves from the public web for the approved domains; review the web "
               "knowledge source's data handling before approval."]
              if "knowledge" in modules and profile["knowledge"]["mode"] == "web" else []),
        ],
        "usageCosts": "Models, Search, Cosmos DB, SignalR, Communication Services, Email, registry, Key Vault and logs "
                      "are billed by usage or tier. Price them for the approved region before approval.",
        "cloudChanges": False,
    }
    result["prices"] = container_app_prices(values["APP_LOCATION"], result["warmReplicas"]) if prices else (
        "Not queried. Add --prices for a dated warm-replica estimate from the Azure Retail Prices API.")
    return result


def container_app_prices(region, warm):
    query = (f"serviceName eq 'Azure Container Apps' and armRegionName eq '{region}' "
             "and priceType eq 'Consumption' and skuName eq 'Standard'")
    url = PRICES_API + "?" + urlencode({"$filter": query}, quote_via=quote)
    items, page_url = [], url
    try:
        for _ in range(10):
            with urllib.request.urlopen(page_url, timeout=30) as response:
                page = json.load(response)
            items.extend(page.get("Items") or [])
            page_url = page.get("NextPageLink")
            if not page_url:
                break
            link = urlsplit(page_url)
            if link.scheme != "https" or link.hostname != "prices.azure.com":
                raise ValueError("The price list pointed to an unexpected next page.")
        else:
            raise ValueError("The price list has more pages than expected.")
    except (OSError, json.JSONDecodeError, AttributeError) as error:
        raise ValueError(f"Can't read Azure Retail Prices ({error}). Retry, or run scope without --prices.") from None
    meters = {}
    for item in items:
        if not isinstance(item, dict) or item.get("meterName") not in CONTAINER_APPS_METERS:
            continue
        if item.get("type") != "Consumption" or item.get("armRegionName") != region or item.get("tierMinimumUnits") != 0:
            continue
        if item.get("isPrimaryMeterRegion") is False or item.get("currencyCode") != "USD":
            continue
        if item["meterName"] in meters:
            raise ValueError(f"The price list has more than one '{item['meterName']}' price for {region}; check {url}.")
        meters[item["meterName"]] = item
    for name, unit in CONTAINER_APPS_METERS.items():
        item = meters.get(name)
        if not item:
            raise ValueError(f"The price list has no '{name}' price for {region}; check {url}.")
        if item.get("unitOfMeasure") != unit or not isinstance(item.get("retailPrice"), (int, float)) or item["retailPrice"] < 0:
            raise ValueError(f"'{name}' isn't priced per '{unit}' for {region}; check {url}.")
    seconds = HOURS_PER_MONTH * 3600

    def monthly(state):
        cost = warm["vCpu"] * meters[f"Standard vCPU {state} Usage"]["retailPrice"]
        cost += warm["memoryGiB"] * meters[f"Standard Memory {state} Usage"]["retailPrice"]
        return round(cost * seconds, 2)

    return {
        "source": "Azure Retail Prices API (public; no sign-in)", "queryUrl": url,
        "queriedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "currency": "USD", "region": region,
        "meters": [{"meter": name, "retailPrice": meters[name]["retailPrice"], "unit": meters[name]["unitOfMeasure"],
                    "effectiveStartDate": meters[name].get("effectiveStartDate")} for name in CONTAINER_APPS_METERS],
        "warmReplicaEstimate": {
            "horizon": f"{HOURS_PER_MONTH} hours (one month)", "vCpu": warm["vCpu"], "memoryGiB": warm["memoryGiB"],
            "allIdle": monthly("Idle"), "allActive": monthly("Active"),
        },
        "excludes": "The monthly free grant, requests, environment meters, usage-billed services and anything outside Azure.",
    }


def missing_inputs(profile):
    modules = set(resolve_modules(profile["modules"]))
    missing = [f"azure.{key}" for key in ("tenantId", "subscriptionId") if not profile["azure"][key]]
    if modules & {"native-voice", "crm"} and not profile["dynamics"]["organizationUrl"]:
        missing.append("dynamics.organizationUrl")
    if "knowledge" in modules:
        if not profile["knowledge"]["allowedDomains"]:
            missing.append("knowledge.allowedDomains")
        if profile["knowledge"]["mode"] == "indexed" and not profile["knowledge"]["corpusFile"]:
            missing.append("knowledge.corpusFile")
    if "native-knowledge" in modules:
        missing.extend("assistant." + key for key in ("knowledgeFile", "knowledgeArabicFile") if not profile["assistant"][key])
    if "email" in modules and not any(profile["emailPolicy"].values()):
        missing.append("emailPolicy.approvedRecipientsOrDomains")
    if profile["demo"]["mode"] == "generate" and not profile["demo"]["approved"]:
        missing.append("demo.approvedPrivateSeed")
    if profile["phonePolicy"]["allowedOutboundNumbers"] and not profile["approvals"]["outboundDestinations"]:
        missing.append("approvals.outboundDestinations")
    if not profile["approvals"]["content"]:
        missing.append("approvals.content")
    return missing


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def private_digest(path):
    if not path.is_file() or os.name != "nt" and path.stat().st_mode & 0o077:
        raise ValueError("Evidence must be an owner-only file (chmod 600).")
    if path.stat().st_size > 32 * 1024 * 1024:
        raise ValueError("Keep evidence files below 32 MiB; retain larger media separately and reference its receipt.")
    return digest(path)


def profile_fingerprint(profile):
    content = {key: value for key, value in profile.items() if key != "approvals"}
    return hashlib.sha256(json.dumps(content, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def validate_payloads(profile):
    from setup_dynamics_crm import validate_seed
    from setup_knowledge import validate_inputs
    from sync_prompt_knowledge import validate as validate_knowledge

    content = {}
    for field, maximum in (("systemPromptFile", 32 * 1024), ("knowledgeFile", 64 * 1024), ("knowledgeArabicFile", 64 * 1024)):
        if profile["assistant"][field]:
            value = read_private(profile["assistant"][field])
            if not value.strip() or len(value.encode()) > maximum:
                raise ValueError(f"Approved {field} is empty or exceeds its packaged context budget.")
            if field == "knowledgeFile":
                validate_knowledge(value.encode())
            content[field] = value
    modules = resolve_modules(profile["modules"])
    knowledge = {"schemaVersion": 1, **{key: profile["knowledge"][key] for key in ("mode", "allowedDomains")},
                 "approved": profile["approvals"]["content"]}
    documents = json.loads(read_private(profile["knowledge"]["corpusFile"])) if profile["knowledge"]["corpusFile"] else []
    if "knowledge" in modules:
        validate_inputs(knowledge, documents)
    elif documents:
        raise ValueError("A retrieval corpus was supplied without selecting knowledge retrieval.")
    if "native-knowledge" in modules:
        from setup_dynamics_workspace import GateError, render_knowledge
        try:
            render_knowledge({"enable_knowledge": True, "org_url": profile["dynamics"]["organizationUrl"],
                              "namespace": profile["name"], "prefix": "service", "label": profile["brand"]["name"]["en"]},
                             {}, content["knowledgeFile"], content["knowledgeArabicFile"])
        except GateError as error:
            raise ValueError(str(error)) from None
    seed = None
    if profile["demo"]["seedFile"]:
        seed = validate_seed(json.loads(read_private(profile["demo"]["seedFile"])),
                             categories=profile["business"]["caseCategories"],
                             location_required=profile["business"]["locationRequired"])
    if profile["demo"]["mode"] == "generate" and (not seed or seed["account"] is None):
        raise ValueError("Generated-data mode requires actual approved fictional records; use none for schema-only setup.")
    if profile["demo"]["profilesFile"]:
        profiles = json.loads(read_private(profile["demo"]["profilesFile"]))
        if not isinstance(profiles, list) or not 1 <= len(profiles) <= 10 or not seed or not seed["contacts"]:
            raise ValueError("Private profile hints require 1-10 approved seeded contacts.")
        ids, phones = {"no_profile"}, set()
        contacts = {item["phone"]: item for item in seed["contacts"]}
        for item in profiles:
            shape(item, {"id", "display_name", "display_name_ar", "phone", "email", "context"}, "privateProfile")
            for key, maximum in (("id", 100), ("display_name", 160), ("display_name_ar", 160), ("context", 2000)):
                text(item[key], maximum, "privateProfile." + key, empty=key in {"context", "display_name_ar"})
            if item["id"] in ids or not isinstance(item["phone"], str) or item["phone"] not in contacts or item["phone"] in phones:
                raise ValueError("Profile IDs must be unique and their phones must map to approved seed contacts.")
            if not isinstance(item["email"], str) or item["email"] != (contacts[item["phone"]]["email"] or ""):
                raise ValueError("A profile must not introduce an unreviewed delivery address.")
            ids.add(item["id"])
            phones.add(item["phone"])
    return content, knowledge, documents


def source_paths(*, include_reviewed_worktree=False):
    status = subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT)
    if status.strip() and not include_reviewed_worktree:
        raise ValueError("The source worktree has changes. Review them and use --include-reviewed-worktree, or use a reviewed commit; never reset unrelated work.")
    command = ["git", "ls-files", "--cached", "--exclude-standard", "-z"]
    if include_reviewed_worktree:
        command.append("--others")
    listing = subprocess.check_output(command, cwd=ROOT)
    result = []
    for raw in listing.decode().split("\0"):
        if not raw:
            continue
        path = Path(raw)
        if SENSITIVE_PATHS.intersection(path.parts) or path.name.startswith(".env") and path.name not in ENVIRONMENT_EXAMPLES:
            continue
        source = ROOT / path
        if source.is_symlink() or not source.resolve().is_relative_to(ROOT.resolve()):
            raise ValueError("Reviewed source copies must not follow symlinks.")
        if source.is_file():
            result.append(path)
    return sorted(set(result))


def prepare(profile, destination, profile_path, *, include_reviewed_worktree=False):
    destination = private_path(destination)
    if destination.exists():
        raise ValueError("Use a new private workspace; never overwrite another deployment.")
    if not profile["approvals"]["content"]:
        raise ValueError("Approve the proposed content/module profile before preparing its deployment workspace.")
    if profile["demo"]["mode"] == "generate" and not profile["demo"]["approved"]:
        raise ValueError("Generated data must be previewed and explicitly approved, or set demo mode to none.")
    if profile["phonePolicy"]["allowedOutboundNumbers"] and not profile["approvals"]["outboundDestinations"]:
        raise ValueError("Generated data approval is not delivery approval. Explicitly approve outbound destinations before staging.")
    approved_content, knowledge, documents = validate_payloads(profile)
    from check_template import check as verify_neutral_source
    verify_neutral_source()
    paths = source_paths(include_reviewed_worktree=include_reviewed_worktree)
    if Path("azure.yaml") not in paths:
        raise ValueError("The reviewed source does not contain its azd service catalog.")
    if HOOK_WRAPPER not in paths:
        raise ValueError(f"The reviewed source does not contain {HOOK_WRAPPER}, which the azd hooks run.")
    selected = plan(profile)
    profile = copy.deepcopy(profile)
    destination.mkdir(mode=0o700)
    manifest = {}
    for relative in paths:
        source, target = ROOT / relative, destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        manifest[str(relative)] = digest(target)
    inputs = destination / ".deployment-inputs"
    inputs.mkdir(mode=0o700)
    for section, field in PRIVATE_INPUTS:
        if profile[section][field]:
            source = Path(profile[section][field]).expanduser().resolve()
            extension = "." + logo_extension(source) if field == "logoFile" else ".json" if field in {"corpusFile", "seedFile", "profilesFile"} else ".md"
            target = inputs / (section + "-" + field + extension)
            with os.fdopen(os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as output:
                output.write(source.read_bytes())
            profile[section][field] = str(target)
    for relative in UI_CONFIGS:
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(public_config(profile), ensure_ascii=False, indent=2) + "\n")
        if profile["brand"]["logoFile"]:
            logo = target.parents[2] / "public" / ("brand-logo." + logo_extension(profile["brand"]["logoFile"]))
            logo.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(profile["brand"]["logoFile"], logo)
    runtime = runtime_config(profile)
    for relative in RUNTIME_CONFIGS:
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(runtime, ensure_ascii=False, indent=2) + "\n")
        target.chmod(0o600)
    for root in CONTENT_ROOTS:
        for field, name, maximum in (
            ("systemPromptFile", "system_prompt.md", 32 * 1024),
            ("knowledgeFile", "knowledge.md", 64 * 1024),
        ):
            source = profile["assistant"][field]
            if source:
                target = destination / root / name
                target.write_text(approved_content[field])
                target.chmod(0o600)
        if profile["demo"]["profilesFile"]:
            target = destination / root / "profiles.json"
            target.write_text(read_private(profile["demo"]["profilesFile"]))
            target.chmod(0o600)
    if profile["assistant"]["knowledgeFile"]:
        (destination / "infra/data/knowledge.md").write_text(read_private(profile["assistant"]["knowledgeFile"]))
    if profile["assistant"]["knowledgeArabicFile"]:
        (destination / "infra/data/dynamics-workspace/knowledge-ar.md").write_text(read_private(profile["assistant"]["knowledgeArabicFile"]))
    write_private(destination / "native-options.json", json.dumps({
        "enable_knowledge": "native-knowledge" in selected["resolvedModules"],
        "enable_copilot": "native-copilot" in selected["resolvedModules"],
        "crm": {
            "native_voice": "native-voice" in selected["resolvedModules"],
            "case_categories": profile["business"]["caseCategories"],
            "location_required": profile["business"]["locationRequired"],
        },
    }, indent=2) + "\n")
    (destination / "infra/data/knowledge.json").write_text(json.dumps(documents, ensure_ascii=False, indent=2) + "\n")
    (destination / "infra/data/knowledge-config.json").write_text(json.dumps(knowledge, indent=2) + "\n")
    azure = yaml.safe_load((destination / "azure.yaml").read_text())
    azure["name"] = profile["name"]
    azure["services"] = {name: service for name, service in azure["services"].items() if name in selected["services"]}
    if "knowledge" not in selected["resolvedModules"]:
        azure["hooks"].pop("postprovision", None)
    (destination / "azure.yaml").write_text(yaml.safe_dump(azure, sort_keys=False))
    values = deployment_values(profile)
    if set(selected["resolvedModules"]) & {"crm", "knowledge", "agent-assist"}:
        values["INGEST_API_KEY"] = secrets.token_urlsafe(32)
    write_private(destination / "selection.env", env_file(values))
    write_private(destination / "deployment-profile.json", json.dumps(profile, ensure_ascii=False, indent=2) + "\n")
    rendered = [*UI_CONFIGS, *RUNTIME_CONFIGS, "native-options.json",
                "infra/data/knowledge.md", "infra/data/knowledge.json", "infra/data/knowledge-config.json",
                "infra/data/dynamics-workspace/knowledge-ar.md",
                *(root + "/" + name for root in CONTENT_ROOTS for name in ("system_prompt.md", "knowledge.md", "profiles.json"))]
    if profile["brand"]["logoFile"]:
        rendered.extend(str(Path(relative).parents[2] / "public" / ("brand-logo." + logo_extension(profile["brand"]["logoFile"])))
                        for relative in UI_CONFIGS)
    for relative in rendered:
        target = destination / relative
        if target.is_file() and relative not in UI_CONFIGS:
            target.chmod(0o600)
    save = {
        **{key: value for key, value in selected.items() if key not in {"modules", "commonGates", "terms"}},
        "status": "awaiting-human", "sourceCommit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
        "sourceFiles": manifest, "sourceProfileSha256": digest(profile_path),
        "preparedProfileSha256": profile_fingerprint(profile), "preparationComplete": True,
        "inputFiles": {str(path.relative_to(destination)): digest(path) for path in inputs.iterdir()},
        "renderedFiles": {relative: digest(destination / relative) for relative in rendered if (destination / relative).is_file()},
        "createdAt": datetime.now(timezone.utc).isoformat(), "completedStages": [], "componentReceipts": {},
        "dataProvenance": "synthetic-generated-private" if profile["demo"]["mode"] == "generate" else "no-generated-data",
        "additionalTools": [{"name": tool["name"], "status": "needs-private-integration-and-review"}
                            for tool in profile["additionalTools"]],
        "secretValuesIncluded": False, "callerAcceptance": "not-tested",
    }
    write_private(destination / "deployment-ledger.json", json.dumps(save, indent=2) + "\n")
    return {"privateWorkspace": str(destination), "services": selected["services"], "resolvedModules": selected["resolvedModules"],
            "status": "awaiting-human", "cloudChanges": False}


def prepared_workspace():
    profile_path = ROOT / "deployment-profile.json"
    if not profile_path.is_file():
        raise ValueError("This is an unprepared template. Run template_setup.py prepare into a private workspace before azd.")
    ledger_path = ROOT / "deployment-ledger.json"
    if not ledger_path.is_file():
        raise ValueError("Preparation did not finish. Do not deploy a partial workspace.")
    profile = validate(json.loads(read_private(profile_path)), require_target=True, allow_packaged=True)
    ledger = json.loads(read_private(ledger_path))
    if not isinstance(ledger, dict) or ledger.get("preparationComplete") is not True or ledger.get("preparedProfileSha256") != profile_fingerprint(profile):
        raise ValueError("The selected configuration changed after preparation. Prepare a new reviewed workspace.")
    for field in ("inputFiles", "renderedFiles"):
        entries = ledger.get(field)
        if not isinstance(entries, dict):
            raise ValueError("The preparation ledger is incomplete.")
        for relative, expected in entries.items():
            if not isinstance(relative, str):
                raise ValueError("Invalid prepared file reference.")
            target = (ROOT / relative).resolve()
            boundary = ROOT / ".deployment-inputs" if field == "inputFiles" else ROOT
            if not target.is_relative_to(boundary) or not target.is_file() or digest(target) != expected:
                raise ValueError("Approved private content or generated module configuration changed after preparation.")
    return profile, ledger


def gate(phase):
    profile, ledger = prepared_workspace()
    selected = plan(profile)
    azure = yaml.safe_load((ROOT / "azure.yaml").read_text())
    if set(azure["services"]) != set(selected["services"]):
        raise ValueError("The azd service list differs from the reviewed module selection.")
    if not profile["approvals"]["content"]:
        raise ValueError("Content/module approval is missing.")
    if profile["phonePolicy"]["allowedOutboundNumbers"] and not profile["approvals"]["outboundDestinations"]:
        raise ValueError("Outbound destination approval is missing or was revoked. Do not deploy the staged dialing policy.")
    environment = ROOT / ".azure" / profile["name"] / ".env"
    if not environment.is_file():
        raise ValueError("Create the new azd environment and run template_setup.py configure first.")
    values = dotenv_values(environment, interpolate=False)
    if values.get("TEMPLATE_PREPARED_PROFILE_SHA256") != ledger["preparedProfileSha256"]:
        raise ValueError("The azd environment was not initialized from this prepared profile.")
    expected = deployment_values(profile)
    for key in (*FLAGS.values(), "AZURE_TENANT_ID", "AZURE_SUBSCRIPTION_ID", "AZURE_LOCATION", "AZURE_RESOURCE_GROUP", "ACS_DATA_LOCATION"):
        if values.get(key) != expected[key]:
            raise ValueError(f"{key} differs from the reviewed target/module selection.")
    applying = phase in {"apply", "deploy"} or phase == "provision" and os.environ.get("TEMPLATE_READ_ONLY_PREVIEW") != "true"
    if applying:
        if not profile["approvals"]["azurePreview"]:
            raise ValueError("Review the Azure preview and record approval before applying/deploying.")
        if set(selected["resolvedModules"]) & {"email", "native-voice", "application-phone"} and not profile["approvals"]["serviceEligibility"]:
            raise ValueError("Communications eligibility, charges and policy require explicit approval.")
    if phase in {"package", "deploy"}:
        modules = set(selected["resolvedModules"])
        if modules & {"browser-voice", "application-phone", "native-voice"} and len(values.get("HUMAIN_AUTHORIZATION") or "") < 32:
            raise ValueError("Enter HUMAIN_AUTHORIZATION securely before packaging/deploying voice; do not paste it into chat.")
        if modules & {"crm", "knowledge", "agent-assist"} and len(values.get("INGEST_API_KEY") or "") < 32:
            raise ValueError("The selected shared API requires its generated private INGEST_API_KEY.")
        if "application-phone" in modules and not re.fullmatch(r"\+[1-9][0-9]{6,14}", values.get("ACS_SOURCE_NUMBER") or ""):
            raise ValueError("Acquire/configure the approved application ACS source number before deploying application telephony.")
    if phase in {"package", "deploy"} and profile["additionalTools"]:
        review_path = ROOT / "extension-review.json"
        if not review_path.is_file():
            raise ValueError("Additional tools are requirements, not installed features. Implement/test/review them privately first.")
        review = json.loads(read_private(review_path))
        if not isinstance(review, dict) or review.get("approved") is not True or not review.get("approvalReference"):
            raise ValueError("Additional-tool review needs explicit human approval.")
        names = review.get("toolNames")
        if not isinstance(names, list) or any(not isinstance(name, str) for name in names) or set(names) != {tool["name"] for tool in profile["additionalTools"]}:
            raise ValueError("Additional-tool review does not cover the requested tools.")
        files = review.get("sourceFiles")
        evidence = review.get("testEvidence")
        if not isinstance(files, dict) or not files or not isinstance(evidence, dict) or not evidence:
            raise ValueError("Record reviewed source hashes and passing-test evidence hashes for additional tools.")
        if files.keys() & evidence.keys():
            raise ValueError("Test execution evidence must be separate from implementation source files.")
        for path, expected in {**files, **evidence}.items():
            if not isinstance(path, str) or not isinstance(expected, str):
                raise ValueError("Additional-tool review must contain path/hash pairs.")
            target = (ROOT / path).resolve()
            if not target.is_relative_to(ROOT) or not target.is_file() or digest(target) != expected:
                raise ValueError("Additional-tool source or test evidence changed after review.")
    return {"gate": phase, "status": "verified", "services": selected["services"], "cloudChanges": False}


def event_directory():
    directory = ROOT / ".deployment-events"
    directory.mkdir(mode=0o700, exist_ok=True)
    return directory


def event_name(stage, suffix):
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ-") + stage + "-" + secrets.token_hex(3) + suffix


def configure():
    profile, ledger = prepared_workspace()
    environment = ROOT / ".azure" / profile["name"] / ".env"
    if not environment.is_file():
        raise ValueError("First create the named azd environment with azd env new; do not reuse a production environment.")
    current = dotenv_values(environment, interpolate=False)
    existing = current.get("TEMPLATE_PREPARED_PROFILE_SHA256")
    if existing:
        if existing != ledger["preparedProfileSha256"]:
            raise ValueError("This azd environment belongs to a different preparation; its values were preserved.")
        return {"environment": profile["name"], "status": "already-configured", "runtimeOverridesPreserved": True}
    if any(key.startswith("SERVICE_") and value for key, value in current.items()):
        raise ValueError("This appears to be an existing deployment. Inspect its original receipts; do not overwrite its settings.")
    for key, expected in (("AZURE_TENANT_ID", profile["azure"]["tenantId"]), ("AZURE_SUBSCRIPTION_ID", profile["azure"]["subscriptionId"])):
        if current.get(key) and current[key] != expected:
            raise ValueError("The existing azd target differs from the approved profile.")
    read_private(ROOT / "selection.env")
    values = dotenv_values(ROOT / "selection.env", interpolate=False)
    values["TEMPLATE_PREPARED_PROFILE_SHA256"] = ledger["preparedProfileSha256"]
    if any(not re.fullmatch(r"[A-Z][A-Z0-9_]*", key) or not isinstance(value, str)
           for key, value in {**current, **values}.items()):
        raise ValueError("The environment contains an unset value or invalid key.")
    content = env_file({**current, **values})
    temporary = environment.with_name(".template-env-" + secrets.token_hex(8))
    try:
        write_private(temporary, content)
        os.replace(temporary, environment)
    finally:
        if temporary.exists():
            temporary.unlink()
    return {"environment": profile["name"], "status": "configured", "cloudChanges": False}


def preview():
    gate("preview")
    profile, _ = prepared_workspace()
    log = event_directory() / event_name("azure-preview", ".txt")
    with os.fdopen(os.open(log, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as output:
        result = subprocess.run(["azd", "provision", "--preview", "--environment", profile["name"], "--no-prompt"],
                                cwd=ROOT, stdout=output, stderr=subprocess.STDOUT,
                                env={**os.environ, "TEMPLATE_READ_ONLY_PREVIEW": "true"})
    if result.returncode:
        raise ValueError(f"Azure preview failed. Inspect {log}; no approval was recorded.")
    return {"status": "previewed", "privateEvidence": str(log), "approvalRecorded": False, "resourcesDeployed": False,
            "note": AZD_PREVIEW_NOTE.replace("this scope", "template_setup.py scope")}


def stages_for(profile):
    selected = plan(profile)
    return [gate["id"] for gate in selected["humanGates"]] + ["azure-provision"] + (
        ["application-deploy"] if selected["services"] else []
    ) + (["native-control"] if "native-voice" in selected["resolvedModules"] else [])


def record_stage(args):
    profile, ledger = prepared_workspace()
    if args.stage not in stages_for(profile):
        raise ValueError("Record only a stage selected by this deployment profile.")
    text(args.note, 2000, "stage note")
    if args.status in {"approved", "verified"} and not args.evidence:
        raise ValueError("Approval/verification records require private evidence references.")
    if args.status == "approved" or args.stage == "acceptance" and args.status == "verified":
        text(args.approval_reference, 1000, "human approval reference")
    evidence = {}
    for path in args.evidence:
        path = path.expanduser().resolve()
        evidence[str(path)] = private_digest(path)
    document = {"schemaVersion": 1, "profileSha256": ledger["preparedProfileSha256"],
                "stage": args.stage, "status": args.status, "note": args.note,
                "approvalReference": args.approval_reference, "evidence": evidence,
                "recordedAt": datetime.now(timezone.utc).isoformat(),
                "componentJournalsRemainAuthoritative": True}
    output = event_directory() / event_name(args.stage, ".json")
    write_private(output, json.dumps(document, indent=2) + "\n")
    return {"record": str(output), "stage": args.stage, "recordedStatus": args.status, "cloudChanges": False}


def status():
    profile, ledger = prepared_workspace()
    latest = {}
    directory = ROOT / ".deployment-events"
    for path in sorted(directory.glob("*.json")) if directory.is_dir() else ():
        event = json.loads(read_private(path))
        if not isinstance(event, dict) or event.get("profileSha256") != ledger["preparedProfileSha256"]:
            raise ValueError("A stage record belongs to another preparation.")
        for evidence, expected in event["evidence"].items():
            target = Path(evidence)
            if private_digest(target) != expected:
                raise ValueError("Recorded evidence changed. Re-verify the stage instead of trusting an old status.")
        latest[event["stage"]] = {"status": event["status"], "record": str(path)}
    return {"environment": profile["name"], "modules": ledger["resolvedModules"],
            "stages": {stage: latest.get(stage, {"status": "pending"}) for stage in stages_for(profile)},
            "callerAcceptance": latest.get("acceptance", {"status": "not-tested"}),
            "resumePolicy": "Read the latest evidence and original component journal; re-check current state before any write.",
            "cloudChanges": False}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init", help="Write a new private profile from the neutral example.")
    init.add_argument("--output", type=private_path, required=True)
    for name, summary in (
        ("validate", "Check the profile's structure and list missing inputs."),
        ("plan", "List every module with its purpose, dependencies, licenses and gates, plus the selected services."),
        ("scope", "List the Azure resources, identities, roles, models, regions and warm replicas for the selection."),
        ("prepare", "Copy the reviewed source and private inputs into a new private workspace."),
    ):
        command = sub.add_parser(name, help=summary)
        command.add_argument("--profile", type=private_path, required=True)
        if name == "prepare":
            command.add_argument("--output-dir", type=private_path, required=True)
            command.add_argument("--include-reviewed-worktree", action="store_true")
        if name == "scope":
            command.add_argument("--prices", action="store_true",
                                 help="Add a dated Container Apps warm-replica estimate from the public Azure Retail Prices API.")
    check = sub.add_parser("gate", help="Check the prepared workspace before an azd phase.")
    check.add_argument("--phase", choices=("provision", "package", "apply", "deploy"), required=True)
    for name, summary in (
        ("configure", "Import the private selection into the new azd environment."),
        ("preview", "Run azd provision --preview and keep its output privately."),
        ("status", "Show recorded stages for this preparation."),
    ):
        sub.add_parser(name, help=summary)
    record = sub.add_parser("record", help="Record a stage decision with private evidence.")
    record.add_argument("--stage", required=True)
    record.add_argument("--status", choices=("approved", "verified", "blocked"), required=True)
    record.add_argument("--evidence", type=Path, action="append", default=[])
    record.add_argument("--note", required=True)
    record.add_argument("--approval-reference", default="")
    args = parser.parse_args(argv)
    if args.command == "init":
        write_private(args.output, EXAMPLE.read_text())
        print(json.dumps({"profile": str(args.output), "status": "needs-brief-and-target", "cloudChanges": False}))
        return 0
    try:
        if args.command in {"configure", "preview", "status", "record"}:
            operation = {"configure": configure, "preview": preview, "status": status, "record": lambda: record_stage(args)}
            print(json.dumps(operation[args.command](), indent=2))
            return 0
        if args.command == "gate":
            print(json.dumps(gate(args.phase), indent=2))
            return 0
        profile = validate(json.loads(read_private(args.profile)), require_target=args.command == "prepare")
        if args.command == "prepare":
            result = prepare(profile, args.output_dir, args.profile, include_reviewed_worktree=args.include_reviewed_worktree)
        elif args.command == "plan":
            result = plan(profile)
        elif args.command == "scope":
            result = scope(profile, prices=args.prices)
        else:
            result = {
                "valid": True, "resolvedModules": resolve_modules(profile["modules"]),
                "readyForPreparation": not missing_inputs(profile), "missingInputs": missing_inputs(profile),
                "cloudChanges": False,
            }
    except (ValueError, OSError, json.JSONDecodeError) as error:
        print(json.dumps({"status": "blocked", "error": str(error), "cloudChanges": False}), file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
