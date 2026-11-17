# Deploy HUMAIN voice inside Dynamics 365 Contact Center

**Deployment runbook, version 2.2 - 17 November 2026**

Deploy the Azure voice application alongside Dynamics 365 Contact Center.
HUMAIN/ALLAM handles the AI conversation; Dynamics owns the call from the first
ring through human handover. This is the technical native-voice sub-workflow;
start with the [modular entry point](../deployment.md) to select and prepare the
deployment. CRM, custom chat and the representative workspace are optional modules.
Use repository-owned setup tools and private environment inputs; a previous
operator's session folder is not a deployment prerequisite.

For an AI-led deployment, start with
[Deploy with Copilot](deploy-with-copilot.md) and the repository's
`/deploy-dynamics-native` skill. That guide orders the existing commands and
marks every human gate. This page contains their full technical details.

The accepted response settings are `HUMAIN_TURN_TAKING_STRATEGY=fast_vad` and
`HUMAIN_INTERRUPTION_POLICY_MODE=off`, approved by the user on 7 November for
ongoing use. They are the code, bootstrap and infrastructure defaults for
browser and native phone voice. The documented responsiveness trade-offs and
earlier comparisons remain in the [voice guide](../../apps/voice/README.md#accepted-voice-responsiveness).

The accepted pre-optimization source and live image were restored on 2 November.
The **5-7 second first-notice delay is work in progress, with the investigation
on hold**. Latency experiments and native waiting-music trials are excluded from
the deployment target.

The configuration allows one native AI call at a time, with a 1,200-second
(20-minute) AI phase after setup and a 3,000-cycle controller watchdog. Check
licensing,
regional availability, security policy and the acceptance results in your own
tenant before considering production use. Microsoft certification of this
custom topology has not been established.

> **Before deploying a new tenant:** native voice needs an ACS phone number,
> which Microsoft is retiring, and a Dynamics environment whose voice
> conversations run on the Elastic pipeline. Read the
> [lifecycle and eligibility gate](#first-confirm-service-lifecycle-and-eligibility)
> before provisioning resources or buying numbers. A new organization may be
> unable to buy the numbers this recipe needs.

## Start here

| Read | Purpose |
|---|---|
| [Deploy with Copilot](deploy-with-copilot.md) | AI-led execution order, required inputs, human approvals and resume rules |
| **This page** | New-environment deployment, step by step |
| [Architecture and reference](reference.md) | Identity map, settings, API contracts, verified results and limitations |
| [Operations and acceptance](operations.md) | Live test, monitoring, renewal, rotation, updates, rollback and handoff |
| [Comprehensive acceptance checklist](acceptance-checklist.md) | Exact prompts, short-call scenarios, workspace/chat checks and a pass/fail scorecard |
| [Troubleshooting and lessons](troubleshooting.md) | Actual failure modes and the fixes that worked |
| [CRM setup](crm-setup.md) | Isolated identity/role, schema, synthetic seed, shared API and case handover |
| [Representative workspace setup](workspace-setup.md) | Published bilingual knowledge, Copilot source, profiles, forms, scripts and macros |
| [Multi-operator routing foundation](routing-setup.md) | Reviewed additive queue/capacity onboarding after the baseline works |
| [Native Pause AI setup](supervision-setup.md) | Opt-in authenticated control, secured command records, compatible controller and native UI binding |
| [Runtime supervision preflight](../../infra/scripts/check_native_supervision_runtime.py) | Check the actual deployed managed identity before enabling the control |
| [Controller YAML](../../apps/voice/dynamics/native-speech-turn-pilot.topic.yaml) | Complete credential-free snapshot for inspection |
| [Setup utility](../../infra/scripts/native_voice_setup.py) | Private deployment bundle and guarded configuration helpers |
| [Read-only monitor](../../infra/scripts/watch_native_voice.py) | Portable single-call acceptance monitor |
| [Call architecture and handover diagrams](native-call-flow.html) | Audio paths, retained control and the transfer sequence |

### The call in five steps

1. The caller dials the Dynamics number. Dynamics creates the conversation and
   answers through the Copilot routing agent.
2. Copilot keeps control of the call and its transcription language while Azure
   sends the verified caller's audio to HUMAIN.
3. HUMAIN speaks through a separate ACS VoIP participant. Dynamics transcribes
   the caller and HUMAIN under distinct speaker headers.
4. When the caller requests a person, Azure stops AI media and confirms that
   its own media/control participants have left.
5. Copilot transfers the same conversation to the native Dynamics queue.
   A representative accepts there.

The initial AI call needs no representative acceptance. The tested path uses
neither an external transfer to a bot number nor a Teams Calling Plan or SBC.
Native VoIP does not dial an application-owned ACS number, and native handover
registration doesn't depend on application telephony, so a native-only
deployment needs no application number.

## Tested behavior and limits

Verified on real calls in test deployments:

- Real HUMAIN/ALLAM Arabic and English audio in a Dynamics-owned inbound call.
- Explicit Arabic -> English -> Arabic changes within the same call, with native
  notifications that transcription language changed.
- Live transcript entries and distinct caller, `Acs_Unknown` and representative
  headers across human acceptance.
- Native queue handover on one interrupting request; final real test reached
  native transfer about 4.3 seconds after the request ended.
- Native recording and transcript persisted after wrap-up closed.
- Opt-in faster startup and stage telemetry without speech, audio or secrets.
- Dynamics-backed customer context in native voice and selected-profile text
  chat, shared case tools, personalized greeting and initial language preference.
- Application-owned bilingual recording notice at 1.3x speed, with confirmed
  cancellation before HUMAIN audio. Native startup music/announcements are
  disabled; the first-notice delay remains open.
- Published English/Arabic knowledge, provisioned Arabic UI availability and
  a dedicated representative profile. The corrected customer form lists the
  current customer's active and resolved cases.
- Supervisor Join during the AI phase. Join alone doesn't pause HUMAIN;
  monitoring, joining and taking over are distinct operations, and pausing the
  AI is the separate, explicit `pause-ai` module.

These tests leave several gaps. The AI still appears as `Acs_Unknown`, and raw
exports can classify it as `Customer`. AI/customer analytics classification
needs validation. Automatic Arabic/English detection after the human joins is
unresolved. See [acceptance boundaries](reference.md#acceptance-boundaries).

**Do not turn setup completion into an acceptance claim.** The CRM service
reconciles uncertain writes without repeating them, and keeps Customer, IssueId
and case Regarding consistent. A prefetched answer can still omit explicit case
selection; the actual Case Details panel must be checked after handover.
Representative Copilot knowledge Q&A is not automatically live-case querying.
Check generated summary quality and supervision continuity separately in each
deployment.

### Restored deployment target

Keep the accepted `fast_vad` + `off` response settings alongside the restored
startup-audio path. This acceptance does not reopen the parked startup work.

The application is the sole player of the packaged 1.3x WAV on the Dynamics
call. CRM context and VoIP preparation retain the accepted parallel startup.
Native language acknowledgment, confirmed notice cancellation and current
owner/caller checks gate AI audio. The source environment's caller accepted
the recording and transition; that result does not eliminate the initial delay.

Shared CRM tools use bounded write reconciliation and truthful unknown outcomes.
The corrected customer/case forms, navigation, script wording and native
knowledge search remain deployed. Automatic case selection/linking is still
pending caller acceptance; do not make a historical association to conceal that
gap. See [case identification](operations.md#native-case-identification-at-handover)
and [workspace setup](workspace-setup.md).

Pause AI remains configured with verified runtime identity and published
controller pins. Its remaining UI/audio and non-admin permission checks are
listed in [acceptance](acceptance-checklist.md). Native Join does not automatically
pause HUMAIN. Source-environment summaries and speaker labels are evidence for
the inspected calls, not guarantees for every export.

The 1 November latency optimization and waiting-music experiments were reverted
to the sealed accepted snapshot. Their findings remain in
[troubleshooting](troubleshooting.md); they are not deployment steps.

## Roadmap through this runbook

1. [Prerequisites, roles and private workspace](#1-prerequisites-roles-and-private-workspace)
2. [Deploy the Azure application](#2-deploy-the-azure-application)
3. [Create and connect the dedicated native ACS resource](#3-create-and-connect-the-dedicated-native-acs-resource)
4. [Configure native event delivery and numbers](#4-configure-native-event-delivery-and-numbers)
5. [Create the routing agent and native inbound workstream](#5-create-the-routing-agent-and-native-inbound-workstream)
6. [Prepare the representative](#6-prepare-the-representative)
7. [Give the application its Dataverse identity](#7-give-the-application-its-dataverse-identity)
   and complete the [CRM setup chapter](crm-setup.md)
8. [Create the two topic shells and external context variable](#8-create-the-two-topic-shells-and-external-context-variable)
9. [Generate and stage the controller](#9-generate-and-stage-the-controller)
10. [Configure the native runtime and passive observer](#10-configure-the-native-runtime-and-passive-observer)
11. [Publish once and validate without a call](#11-publish-once-and-validate-without-a-call)
    and complete the [representative workspace chapter](workspace-setup.md)
12. [Run the controlled acceptance call](#12-run-the-controlled-acceptance-call)
13. [Hand the environment to the next team](#13-hand-the-environment-to-the-next-team)

Complete each phase's exit checks before continuing.

### What makes this recipe repeatable

- Every required command, template and nonsecret example must be in the
  repository. Private inputs contain environment IDs, approved test data and
  credentials, not missing implementation steps.
- Keep dry-run plans, exact before/after manifests and rollback receipts in the
  private deployment folder. Do not copy a previous tenant's generated IDs,
  secrets or conditional-update versions.
- Record whether each result is a unit or contract check, a native
  configuration readback, or actual UI or call acceptance. These are not
  interchangeable.
- Update this guide, its linked chapters and troubleshooting lessons whenever
  the implementation changes. Keep unsuccessful experiments and open defects
  out of the mandatory successful path, but retain their lessons and limits.
- A new team's final signoff requires its own service-eligibility and acceptance
  checks. The source environment's success does not waive those prerequisites.
- When a command fails, read back the server state first; a write may have
  partly succeeded. Retry once, and only with a concrete change. If it fails
  again, stop and ask the owner with the command, the error and the readback.

---

## 1. Prerequisites, roles and private workspace

### First: confirm service lifecycle and eligibility

This runbook covers the **ACS-based** Dynamics voice integration. Read the root
[ACS retirement notice](../../README.md#notice-azure-communication-services-retirement)
first. It gives the retirement dates and the number-acquisition restriction;
this section applies them to each selection.

Dynamics voice on ACS stays supported until 30 September 2028 and has to move
to Teams Phone extensibility before then
([Dynamics migration guide](https://learn.microsoft.com/en-us/dynamics365/contact-center/administer/migrate-from-azure-communication-services)).
That move needs changes to this adapter and new media, transcript and handover
tests. Changing a license or carrier alone isn't a tested replacement.

#### What each selection needs from ACS

The root deployment creates the application ACS resource only when `email` or
`application-phone` is selected: [resources.bicep](../../infra/resources.bicep)
sets `deployCommunications = deployEmail || deployApplicationPhone`. You create
the native ACS resource yourself in [section 3](#3-create-and-connect-the-dedicated-native-acs-resource).

| Selection | Application ACS | ACS Email | Application number | Native ACS and Dynamics number |
|---|---|---|---|---|
| `native-voice` only | Not created | Not created | None | Required |
| `application-phone` | Created | Not created | Required | Only with `native-voice` |
| `email` | Created | Created | None | Only with `native-voice` |

- **Native-only** needs one number: the Dynamics number on the native ACS
  resource. Native handover doesn't dial an application number, and
  `runtime-env` treats `--app-number` as optional. Don't buy an application
  number to initialize native voice.
- **Application phone** adds the application ACS resource and its own number.
  Check that number's eligibility separately.
- **Email** adds the application ACS resource and ACS Email, with no number.

A tenant without an existing ACS resource can't buy or port ACS numbers; the
root notice gives the two published cutoff dates. In such a tenant this blocks
`native-voice` and `application-phone`. Number eligibility also depends on the
billing country and agreement type. A valid Azure region proves neither.
`python infra/scripts/contact_center_setup.py eligibility` reads the billing
facts and existing ACS resource creation times; its flags are in `--help`.

#### Region: the Elastic voice pipeline

Native voice works only when the environment's voice conversations run on the
`Elastic` pipeline. In field deployments, a Swiss environment ran on Elastic
and saved HUMAIN's lines in the Dynamics transcript; a Canadian environment ran
on another pipeline and saved none. The region is a default; the pipeline is
the gate.

1. Create the Dynamics environment in Switzerland. Its org URL ends in
   `crm17.dynamics.com`
   ([voice channel regions](https://learn.microsoft.com/en-us/dynamics365/customer-service/administer/voice-channel-region-availability)).
2. Create the native ACS resource with data location `Switzerland`
   ([section 3.1](#31-create-it-in-a-separate-resource-group)).
3. After voice is provisioned, place one call to the automatic trial number.
   Then run `python infra/scripts/contact_center_setup.py pipeline`; its flags
   are in `--help`. Continue only when it reports `Elastic`. Do this before
   you end the trial: ending it retires the trial number.

The trial number is a US toll-free number, and Learn says it can be called only
from within the US
([trial numbers](https://learn.microsoft.com/en-us/dynamics365/customer-service/administer/voice-channel-trial-phone-numbers)).

Another region is acceptable only when this gate reports `Elastic`. Run the
gate in Switzerland too.

Switzerland has consequences the owner must accept:

- Dynamics data resides in Switzerland.
- Learn lists Copilot conversation summaries as unavailable in Switzerland.
  Case summaries and Ask a question are available
  ([Copilot feature regions](https://learn.microsoft.com/en-us/dynamics365/customer-service/administer/cs-region-availability-service-limits),
  checked November 2026). Check the page again before promising a feature.

**Stop/go gate:** every number the selection needs is eligible, the pipeline
gate reports `Elastic`, and a Teams Phone migration owner is named. Record the
evidence in the private handoff. The current working environment doesn't need
to be disconnected to investigate these requirements.

### 1.1 Choose the deployment boundary

Use a **new azd environment and new resource groups in the Dynamics
environment's Microsoft Entra tenant**. Don't retarget an old deployment. The
enhanced voice connection needs the active Azure subscription in the **same
tenant**; this runbook doesn't reproduce the abandoned cross-tenant prototype.

Current defaults:

| Component | Default | Set by |
|---|---|---|
| Dynamics Contact Center and Dataverse | Switzerland (`crm17`) | Environment creation, gated on `Elastic` |
| Native ACS data location | `Switzerland` | `dataLocation` in section 3.1 |
| Application ACS and ACS Email, when selected | `Europe` | `azure.communicationsDataLocation` in the private profile ([example](../../infra/template-profile.example.json)) |
| Application hosting, models and Cosmos | `swedencentral` | `azure.location` in the private profile |

The earlier source deployment used Switzerland for Dataverse and the native
ACS resource, Sweden Central for hosting, France Central for Azure AI Search
(subscription capacity), and the United States for the application ACS under an
older template default. These are history, not defaults. Don't change the
region of existing stateful resources to match them.

Validate data handling for Dynamics, Azure and optional public-web retrieval
with the owner. For HUMAIN, use
[HUMAIN's documentation](https://humain-voice-agent.share.sanad.ink/docs/connect.html);
this repository doesn't record HUMAIN's data handling.

### 1.2 Confirm licenses and install voice

In the target Power Platform environment:

1. Confirm appropriate Dynamics 365 Customer Service / Contact Center and voice
   entitlements. Do not infer working voice from an app icon or a license name.
2. Provision the voice channel and accept its Voice/SMS terms.
3. Enable unified routing.
4. Confirm enhanced voice workstreams and voice-enabled Copilot agents are
   available in the environment's region.
5. Confirm the administrator can open the **runtime** Copilot Service admin
   center and representative workspace.

Current product installation and entitlement references:

- [Install the voice channel](https://learn.microsoft.com/en-us/dynamics365/customer-service/administer/voice-channel-install)
- [Provision channels](https://learn.microsoft.com/en-us/dynamics365/contact-center/implement/provision-channels)
- [Provision unified routing](https://learn.microsoft.com/en-us/dynamics365/customer-service/administer/provision-unified-routing)
- [Connect an existing ACS resource](https://learn.microsoft.com/en-us/dynamics365/customer-service/administer/voice-channel-connect-existing-resource)

The existing-resource workflow appears only after the voice trial ends. Run the
[pipeline gate](#region-the-elastic-voice-pipeline) first, then end the trial
as described in [section 3.4](#34-connect-in-the-copilot-service-admin-center).
Don't disconnect or reconnect a working production resource to make a button
appear.

### 1.3 Separate the permissions

| Actor | Required responsibility |
|---|---|
| Azure deployer | Create resources and role assignments in the selected subscription; Owner, or Contributor plus appropriate role-assignment permission, subject to tenant policy |
| Entra application administrator | Create/own the Event Grid delivery app and configure its application-role assignments |
| Dynamics administrator | Provision voice, workstream, queue, roles, application user and routing agent |
| Copilot maker | Edit/publish this agent in the same Power Platform environment |
| Representative | Licensed, enabled, in the queue, with bookable resource and available voice capacity |
| Supervisor | Explicit **Omnichannel supervisor** role to open Ongoing Conversations Dashboard |
| Application UAMI | Dataverse read access for correlation/ownership checks; no native assignment-write permission (created by `reader-role`, [section 7](#7-give-the-application-its-dataverse-identity)) |

Assign Azure permissions and Dataverse roles separately. The dashboard also
requires the Supervisor role, including for a Dataverse System Administrator.

### 1.4 Tools and versions

Use Linux, WSL or a dev container for the command blocks. No PowerShell is
needed: `native_event_delivery.py` makes the Microsoft Graph and Event Grid
calls.

Tested during the implementation:

| Tool/component | Version |
|---|---|
| Python | 3.12 |
| Azure CLI | 2.90.0 |
| Azure Developer CLI | 1.35.0 |
| Node for the Docker SDK build | 22 |
| ACS Call Automation Python | 1.4.0, **not 1.4.0b1** |
| ACS Identity Python | 1.5.0 |
| Playwright Python / browser package | 1.63.0 |
| ACS Calling JavaScript | 1.46.1 |
| TypeScript | 5.9.3 |

The dependency manifests are authoritative. Do not independently upgrade the
browser, identity, Calling SDK and Call Automation packages during first setup.
The Docker build installs its browser and OS libraries; it does not depend on an
operator's Chrome installation. All five services use `remoteBuild: true` in the
root manifest: deployment builds run in Azure Container Registry. Local Node/
Docker are not required merely to run the deployment steps; local SDK tests or
local image builds have their own development prerequisites.

Use a repository revision containing this runbook, the native renderer, SDK
gateway and infrastructure changes. Record that revision and any required local
changes. The YAML depends on those components.

### 1.5 Create a private deployment folder

First use the [modular preparation workflow](../deployment.md). Work from its
private prepared source directory, not the original checkout. Reuse its Python
environment and create native receipts in a separate private sibling directory:

```bash
umask 077
export REPO_ROOT="$PWD"
export WORK="$PRIVATE/native-receipts"
mkdir -p "$WORK"
chmod 700 "$WORK"

python infra/scripts/native_voice_setup.py --help
```

Use a fresh `WORK` for a different environment. The setup utility refuses to
overwrite generated files and writes them outside the repository with owner-only
permissions. On Windows, use an equivalent private directory ACL; Unix mode bits
alone are not a Windows security boundary.

Record the following non-secret selections in a private text file of your
choice, or use the [selection example](examples/selection.env.example):

```bash
export ENV="native-voice"
export TENANT_ID="<Dynamics-Entra-tenant-UUID>"
export SUBSCRIPTION_ID="<Azure-subscription-UUID-in-that-tenant>"
export ORG_URL="https://<organization>.crm<number>.dynamics.com"
export APP_RG="rg-native-voice"
export NATIVE_RG="rg-native-voice-telephony"
export NATIVE_ACS_NAME="<new-unique-acs-resource-name>"
export SYSTEM_TOPIC="eg-native-voice"
```

Use the already selected azd environment and targets rather than creating a
second environment. Replace every placeholder before executing commands. `ORG_URL` is the Dataverse
organization **origin**, not `make.powerapps.com`, the environment UUID, an
`api.crm...` URL, or a `main.aspx` URL.

### 1.6 Confirm network prerequisites

The tested Azure deployment did not add a custom egress proxy or restrictive
network policy. If yours has one, clear these paths before phone testing:

- Public HTTPS delivery from Copilot/ACS/Event Grid to the required application
  routes; the Call Automation media route also needs WebSocket upgrades.
- Application HTTPS to Entra, Dataverse, the configured HUMAIN token endpoint
  and dependent Azure services.
- Real WebRTC media between the Azure-hosted Calling SDK and ACS. An HTTP
  allowlist alone is insufficient. Follow the current
  [ACS network requirements](https://learn.microsoft.com/en-us/azure/communication-services/concepts/voice-video-calling/network-requirements),
  including the documented media/registration endpoints and UDP/TCP allowances.
- WebSocket signaling and ICE/TURN media to the **actual HUMAIN-provided
  LiveKit deployment**. Obtain its egress requirements from that provider;
  generic self-hosted LiveKit default ports are not proof of its configuration.
- A supported representative browser, microphone permission and an
  unrestricted real-time media path from the representative's network.

This repository is a LiveKit **client**, not a deployment of a LiveKit server.
Do not replace the per-route authentication with a blanket interactive-login
page that prevents service callbacks. Review ingress/security requirements with
the platform owner. Readiness/SDK registration still do not prove RTP or audible
speech; the controlled call remains required.

**Exit gate:** target environment, licenses, tenant, subscription, administrators
and a private deployment folder are known; service eligibility and network paths
are reviewed. No numbers have been dialed.

---

## 2. Deploy the Azure application

### 2.1 Authenticate both CLIs explicitly

```bash
az login --tenant "$TENANT_ID"
az account set --subscription "$SUBSCRIPTION_ID"
az account show --query '{subscription:id,tenant:tenantId,name:name}' --output json
azd auth login --tenant-id "$TENANT_ID"

azd env new "$ENV" --subscription "$SUBSCRIPTION_ID" --location swedencentral
azd env set AZURE_RESOURCE_GROUP "$APP_RG" --environment "$ENV"
azd env set FOUNDRY_ACCOUNT_BASE_NAME "aif-$ENV" --environment "$ENV"
azd env set APP_LOCATION swedencentral --environment "$ENV"
azd env set COSMOS_LOCATION swedencentral --environment "$ENV"
azd env set SEARCH_LOCATION francecentral --environment "$ENV"
```

Choose different regions **before creating resources** if your subscription's
access, model availability or data policy requires them. Merely changing a model
deployment *name* does not change the model/version pinned in Bicep.

Set the deploying user's Entra object ID:

```bash
DEPLOYER_OBJECT_ID="$(az ad signed-in-user show --query id --output tsv)"
azd env set AZURE_PRINCIPAL_ID "$DEPLOYER_OBJECT_ID" --environment "$ENV"
```

In a multi-tenant session, verify the tenant again before Graph/Dataverse work.
`--subscription` on an unrelated `az rest` call does not universally scope a
non-ARM token to the intended tenant. The setup utility explicitly requests its
Dataverse token with `--tenant`.

### 2.2 Register providers and review capacity

These namespaces occur in the current compiled root deployment, including its
nested modules, plus the native Event Grid integration:

```bash
for provider in \
  Microsoft.App Microsoft.Authorization Microsoft.CognitiveServices \
  Microsoft.Communication Microsoft.ContainerRegistry Microsoft.DocumentDB \
  Microsoft.EventGrid Microsoft.Insights Microsoft.KeyVault \
  Microsoft.ManagedIdentity Microsoft.Network Microsoft.OperationalInsights \
  Microsoft.OperationsManagement Microsoft.Resources Microsoft.Search \
  Microsoft.SecurityInsights Microsoft.SignalRService Microsoft.Storage
do
  az provider register --namespace "$provider" --subscription "$SUBSCRIPTION_ID" --wait
done
```

Confirm quota and access for the Foundry models, Search SKU, Cosmos and Container
Apps. The root stack also deploys chat/UI, Assist, private request storage, identity,
networking, knowledge and email.

### 2.3 Generate secrets without putting them in command history

```bash
python infra/scripts/native_voice_setup.py new-keys \
  --output "$WORK/control-keys.env"
```

The control-key helper also generates a legacy ingest key. Do not import that
key over the ingest credential already generated by modular preparation.
Native bundle/runtime stages need the two native control keys from this file.

Enter provider authorization in the **private prepared selection/environment**:

```dotenv
HUMAIN_AUTHORIZATION="<complete single-line value supplied by HUMAIN>"
```

Use the provider-supplied authorization value exactly. Do not infer the scheme,
paste it in chat, print it, or commit it.

```bash
chmod 600 "$WORK/control-keys.env"
python infra/scripts/template_setup.py configure
chmod 600 ".azure/$ENV/.env"
```

The prepared selection chooses **ALLAM**, `bayan_playground` and `mul_nourah`, disables
legacy automatic phone conferencing, and leaves native media disabled. If HUMAIN
authorization is absent, packaging/deployment stops. Obtain it before deploying
the selected voice service. On an already configured workspace, securely update
the named environment rather than rerunning initialization to rotate other keys.

**Do not import the whole control-keys file into azd yet.** The passive observer
validates its resource ID, key and lifetime mode as a set. Supplying only its key is an
invalid partial configuration, even before the native pilot is enabled.

### 2.4 Use only the root deployment

```bash
python infra/scripts/sync_prompt_knowledge.py --check
az bicep build --file infra/main.bicep --outfile "$WORK/main.template.json"
python infra/scripts/template_setup.py preview
# Review: correct tenant/group/regions; no unintended deletions or old-environment changes.
python infra/scripts/template_setup.py gate --phase apply
azd provision --environment "$ENV"
azd deploy --environment "$ENV"
```

Use one azd deployment process per environment. Its own service operations can
run in parallel; competing azd processes can overwrite recorded image settings.

The root post-provision hook configures the knowledge index and knowledge bases.
If it fails after infrastructure succeeded, fix the stated access/capacity
problem and rerun that hook explicitly:

```bash
python infra/scripts/setup_knowledge.py --environment "$ENV"
```

Keep the resource group and tenant policy intact while fixing the hook.
Use the root manifest; the historical app-level manifests omit shared services.

The prepared manifest contains only selected services. `native-voice` alone
deploys `voice`; CRM or Foundry IQ adds the Assist backend. Chat and either
custom agent desktop are separate choices. Do not restore the five-service
catalog over the selected private manifest.

### 2.5 Capture outputs without dumping secrets

```bash
VOICE_ORIGIN="$(azd env get-value SERVICE_VOICE_URI --environment "$ENV")"
export VOICE_ORIGIN="${VOICE_ORIGIN%/}"
export UAMI_CLIENT_ID="$(azd env get-value MOACC_UAMI_CLIENT_ID --environment "$ENV")"
export UAMI_PRINCIPAL_ID="$(azd env get-value MOACC_UAMI_PRINCIPAL_ID --environment "$ENV")"
export APP_ACS_NAME="$(azd env get-value MOACC_ACS_NAME --environment "$ENV")"

curl --fail --silent --show-error "$VOICE_ORIGIN/health"
```

Do not run `azd env get-values` in a recorded terminal: it can reveal secrets.

**Exit gate:** the real `/health` responds, not the placeholder hello-world
container. Record the voice origin and managed-identity IDs. Native media is
still disabled. `automatic_phone_conference_enabled` is false.

---

## 3. Create and connect the dedicated native ACS resource

Dynamics answers native calls through its own ACS resource. Never connect the
application ACS resource (`MOACC_ACS_NAME`, created only for `email` or
`application-phone`) to Dynamics, and don't connect one native resource to more
than one Dynamics environment.

### 3.1 Create it in a separate resource group

```bash
az group create --name "$NATIVE_RG" --location swedencentral \
  --subscription "$SUBSCRIPTION_ID" --output none

az deployment group create --name native-voice-resource \
  --resource-group "$NATIVE_RG" --subscription "$SUBSCRIPTION_ID" \
  --template-file infra/dynamics-voice-poc.bicep \
  --parameters acsName="$NATIVE_ACS_NAME" dataLocation=Switzerland \
  --output none

export NATIVE_ACS_ID="$(az deployment group show \
  --name native-voice-resource --resource-group "$NATIVE_RG" \
  --subscription "$SUBSCRIPTION_ID" \
  --query properties.outputs.acsResourceId.value --output tsv)"
```

`dataLocation` is the ACS data geography, not the phone-number country or the
resource group's location. Keep `Switzerland` unless the
[pipeline gate](#region-the-elastic-voice-pipeline) passed in another region and
the owner approved that data location.

### 3.2 Create the Event Grid delivery app

Dynamics asks for the client ID of an Entra app that authenticates Event Grid
deliveries to its webhooks. Create it with `native_event_delivery.py`, which
uses portable Microsoft Graph calls; no PowerShell is needed. Its subcommands
and flags are in `--help`, and [Native event delivery](operations.md#native-event-delivery)
describes each step and its readback.

```bash
python infra/scripts/native_event_delivery.py --help
```

Run its `bootstrap` subcommand now: plan, review, then apply. It creates only
the dedicated app, its service principal and the
`AzureEventGridSecureWebhookSubscriber` role assignments, then reads back the
owners and grants. It needs no secret and no broad consent. Record the reported
`delivery_app_id` as `EVENTGRID_APP_ID`. It isn't the voice UAMI, the Copilot
bot ID or the Microsoft.EventGrid principal. Background:
[secure webhook delivery](https://learn.microsoft.com/en-us/azure/event-grid/secure-webhook-delivery).

### 3.3 Prepare the four connection values

Dynamics asks for four values. Prepare all of them before the owner opens the
form:

| Field in Dynamics | Value | Hand over |
|---|---|---|
| ACS Resource Name | `NATIVE_ACS_NAME` | Directly |
| ACS Resource ID | `NATIVE_ACS_ID` | Directly |
| Event grid app id | `EVENTGRID_APP_ID` | Directly |
| Connection String | Primary connection string of the native ACS resource | Private copy sheet only |

**Event grid app tenant id** is prefilled; it must equal `TENANT_ID`.

Write the copy sheet. The command prints no secret and writes an owner-only file
outside the checkout:

```bash
python infra/scripts/native_voice_setup.py copy-sheet \
  --tenant-id "$TENANT_ID" --native-acs-resource-id "$NATIVE_ACS_ID" \
  --event-grid-app-id "$EVENTGRID_APP_ID" \
  --output "$WORK/native-acs-copy-sheet.txt"
```

> [!WARNING]
> A coding-agent chat can attach the current editor selection to a message.
> After copying the connection string, clear the selection or switch to a
> non-secret file before you send anything. Never paste the string into chat,
> a terminal command or a screenshot.

Don't use the application ACS resource's name, ID or connection string here.

### 3.4 Connect in the Copilot Service admin center

Last verified: November 2026. If a screen differs, ask the owner for a
screenshot that includes the left navigation, with no secrets showing.

Give the owner one screen at a time:

1. Open the **runtime** Copilot Service admin center
   (`https://<organization>.crm<number>.dynamics.com/main.aspx?appid=<admin-app-id>`;
   `discover` lists the app ID). Select **Customer support > Channels**, then
   **Manage** for **Phone numbers**.
2. If a US toll-free trial number and **End trial** appear, confirm that the
   pipeline gate passed, then select **End trial**
   ([end the trial](https://learn.microsoft.com/en-us/dynamics365/customer-service/administer/voice-channel-trial-phone-numbers#end-the-trial)).
3. Select **Get started**. On the **Azure Communication Services** tab, select
   **Use existing resource**. Don't select **Create new resource**, and don't
   use the **Teams phone system** tab.
4. Enter the values from section 3.3, with the connection string from the copy
   sheet. Check that **Event grid app tenant id** shows `TENANT_ID`, and select
   the checkbox confirming that this resource serves only this environment.
5. Select **Connect**.

Microsoft's procedure:
[connect an existing resource](https://learn.microsoft.com/en-us/dynamics365/customer-service/administer/voice-channel-connect-existing-resource).

### 3.5 Read back the connection

```bash
python infra/scripts/native_voice_setup.py connection \
  --tenant-id "$TENANT_ID" --org-url "$ORG_URL" \
  --native-acs-resource-id "$NATIVE_ACS_ID" \
  --event-grid-app-id "$EVENTGRID_APP_ID"
```

The command reads the Dynamics communication provider records. It reads the
table metadata first and stops if expected columns are missing. It passes only
when exactly one active record holds the native resource ID, the Event Grid app
ID and the tenant ID across its entries, and `msdyn_istrial` is false. It
prints entry keys, never values, and writes nothing.

Delete the copy sheet once the readback passes:

```bash
rm "$WORK/native-acs-copy-sheet.txt"
```

**Exit gate:** `connection` reports `connected` with `is_trial: false`.

### 3.6 Rotate the connection string

Rotate only when the string was exposed, and only on the native resource.
Microsoft's [existing-resource guidance](https://learn.microsoft.com/en-us/dynamics365/customer-service/administer/voice-channel-connect-existing-resource)
says that after the key is regenerated, the Connection string field under
**Channels > Phone numbers > Advanced > Manage Azure Communication Services**
shows an error, and you paste the new string there. A healthy connection shows
no such field. Never select **Disconnect** to make it appear.

1. Agree a window with no native calls. Calls fail until Dynamics and the voice
   app both have the new string.
2. Regenerate the native resource's primary key
   ([Regenerate Key](https://learn.microsoft.com/en-us/rest/api/communication/resourcemanager/communication-services/regenerate-key)).
   The response contains the new key, so suppress it:

   ```bash
   az rest --method post \
     --url "https://management.azure.com${NATIVE_ACS_ID}/regenerateKey?api-version=2023-04-01" \
     --body '{"keyType": "Primary"}' --output none
   ```

3. Write a new copy sheet with `copy-sheet` and a new `--output` file.
4. The owner pastes the new string into the Connection string field and waits
   for the success message. Last verified: November 2026; use the screenshot
   fallback from section 3.4 if the page differs.
5. Give the voice app the new string: write a new runtime file with
   `runtime-env` and apply it as in
   [section 10](#10-configure-the-native-runtime-and-passive-observer).
6. Read back: `connection` passes, Manage Azure Communication Services shows no
   error after a refresh, and `verify` reports `voip_gateway_ready`. Then delete
   the copy sheet.

Also see [secret rotation](operations.md#secret-rotation).

---

## 4. Configure native event delivery and numbers

### 4.1 Buy the native number

Recommended: a UK local (geographic) number with both making and receiving
calls enabled. In past deployments these were the most flexible and reliable
option. A local number in the deployer's country is desirable but often hard to
obtain.

Use `acs_numbers.py` on the native resource. Its `browse`, `reserve` and
`purchase` subcommands check a readback after each step; their flags are in
`--help`:

```bash
python infra/scripts/acs_numbers.py --help
```

1. Browse UK geographic numbers that can make and receive calls, and pick one
   that is easy to remember.
2. Reserve it. Check the reservation readback: country, number type,
   assignment, calling capabilities and monthly cost.
3. Buy it only after the owner approves the price. Record it as
   `DYNAMICS_NUMBER`.

If no UK local number is available, stop and propose an alternative to the
owner. A UK number says nothing about where data is stored.

- Buying ACS numbers depends on the tenant already having an ACS resource; see
  the root [ACS retirement notice](../../README.md#notice-azure-communication-services-retirement).
- Purchase limits apply per Azure tenant
  ([ACS service limits](https://learn.microsoft.com/en-us/azure/communication-services/concepts/service-limits)).
  Release numbers you no longer use before buying. If a reservation fails with
  `MaxAllowedNumbersPerReservationExceeded`, release unused numbers and request
  a purchase-limit increase through Azure Support.

| Number | Owned by | Purpose |
|---|---|---|
| `DYNAMICS_NUMBER` | Native ACS resource | The number customers dial; only Dynamics answers it |
| Optional `APP_NUMBER` | Application ACS resource, only with `application-phone` | Application-owned calls, or an approved controlled caller |

[app.py](../../apps/voice/src/app/app.py) registers native handover in its
native-only session factory without an application caller or
`ACS_SOURCE_NUMBER`. Don't buy an application number to initialize native voice.
The one-number target is covered offline; accept it only after a real call.

### 4.2 Sync the number into Dynamics

Last verified: November 2026. If a screen differs, ask the owner for a
screenshot that includes the left navigation, with no secrets showing.

1. Select **Customer support > Channels**, **Manage** for **Phone numbers**,
   then **Advanced**.
2. Under **Sync from Azure**, select **Sync**, then **Sync** in the dialog.
3. Don't select **Add number**: it buys another number.

Confirm that `DYNAMICS_NUMBER` appears on the **Phone numbers** page. A success
toast means only that the sync was accepted. Microsoft's procedure:
[sync from Azure](https://learn.microsoft.com/en-us/dynamics365/customer-service/administer/voice-channel-sync-from-acs).

Don't bind the application number to the Dynamics workstream, and don't point
the native number at the application's `/acs/incoming-call` webhook.
`python infra/scripts/connect_phone_events.py` serves only the application
number: it targets `MOACC_ACS_NAME`, never the native resource.

### 4.3 Copy the two webhook URLs

On the same **Advanced** page, open **Manage Azure Communication Services** and
copy these two values into the owner-only file `$WORK/dynamics-webhooks.env`,
in the format given in [Native event delivery](operations.md#native-event-delivery):

- **Incoming call Web Hook Endpoint**
- **Recording Web Hook Endpoint**

Copy them exactly, with every query parameter. Never construct them from a
pattern or another environment's host name. Last verified: November 2026; use
the screenshot fallback from section 4.2 if the page differs.

### 4.4 Create the native event subscriptions

Run `native_event_delivery.py subscriptions` for the native ACS resource with
the webhook file from 4.3: plan, review, then apply. It creates one Event Grid
system topic (`SYSTEM_TOPIC`) and two subscriptions authenticated with
`EVENTGRID_APP_ID` and `TENANT_ID`:

| Subscription | Event type | Endpoint | Advanced filter |
|---|---|---|---|
| Incoming | `Microsoft.Communication.IncomingCall` | Incoming call URL from 4.3 | `data.to.kind` StringIn `phoneNumber` |
| Recording | `Microsoft.Communication.RecordingFileStatusUpdated` | Recording URL from 4.3 | None |

The application later invites an ACS user as HUMAIN's VoIP participant. The
incoming filter stops Dynamics from answering that invitation as a phone call.

Then run `native_event_delivery.py verify`. It reads back endpoints, filters,
the delivery app's role assignments and the `Succeeded` state, and writes
nothing. If a step fails, read back first, retry once only with a concrete
change, then stop and ask the owner. Removing Entra authentication doesn't fix a
handshake failure; Microsoft allows up to 15 minutes for the app to propagate.
Details: [Native event delivery](operations.md#native-event-delivery).

**Exit gate, before any call:** the owner refreshes **Customer support >
Channels > Phone numbers > Manage** (Manage telephony) and confirms that both
banners are gone: "Your Event Grid is unconfigured for incoming calls" and "Your
Event Grid is unconfigured for call recording". Last verified: November 2026;
use the screenshot fallback if the page differs. Also required: `connection`
passes, `native_event_delivery.py verify` passes, `DYNAMICS_NUMBER` is on the
Phone numbers page, and nothing points the native number at
`/acs/incoming-call`.

---

## 5. Create the routing agent and native inbound workstream

Work through this section in order. Native-mode CRM
([section 7.3](#73-configure-the-separate-crm-identity-schema-and-seed)) needs
the workstream ID from 5.1, so create the shell first.

### 5.1 Create the minimal shell: one queue and one voice workstream

A fresh Customer Service environment already holds about 14 workstreams and 46
bots from Microsoft solutions. Don't reuse any of them, and never pick a record
by its display name. Create one fallback queue and one voice workstream for this
deployment.

Last verified: November 2026. If a screen differs, ask the owner for a
screenshot that includes the left navigation, with no secrets showing.

In the runtime Copilot Service admin center:

1. Select **Customer support > Queues**, **Manage** for **Advanced queues**,
   then **New**. Enter a name such as `<organization> Voice Queue`, choose type
   **Voice** and select **Create**
   ([create a queue](https://learn.microsoft.com/en-us/dynamics365/customer-service/administer/queues-omnichannel)).
2. Select **Customer support > Workstreams**. It has its own site map entry,
   separate from Conversation Orchestration. Select **New workstream**, enter a
   name such as `<organization> Voice`, choose type **Voice** and create it with
   push distribution
   ([create a workstream](https://learn.microsoft.com/en-us/dynamics365/customer-service/administer/create-workstreams)).
   Choose the enhanced voice experience if the tenant offers a choice.

Read the IDs back:

```bash
python infra/scripts/native_voice_setup.py discover \
  --tenant-id "$TENANT_ID" --org-url "$ORG_URL"
```

`discover` classifies every record it lists. `platform` records (managed
solution components and platform defaults) and `not-routable` queues have
`owned: false`: never configure or adopt them. `environment` records were created
in this environment. Ask the owner to confirm the two new IDs from the record
URLs in the admin center, then record them as `QUEUE_ID` and `WORKSTREAM_ID`.

**Exit gate:** `WORKSTREAM_ID` is an active inbound voice workstream classified
`environment`, and `QUEUE_ID` is the new voice queue. Native-mode CRM can now use
`WORKSTREAM_ID`.

### 5.2 Create a thin voice-enabled routing agent

Use the **same Power Platform environment as Dynamics** in Copilot Studio.
Last verified: November 2026. If a screen differs, ask the owner for a
screenshot that includes the left navigation, with no secrets showing.

1. Create a new agent, for example `<organization> Routing`, with primary
   language **English**. Record its ID as `BOT_ID`; `discover` lists it as an
   `environment` bot.
2. Make Conversation Start safe until the caller gate exists: a message
   "Only approved test callers can use this line." followed by
   **End Conversation**. The bundle in section 9 keeps this as the route for
   every caller that isn't approved.
3. Apply the agent baseline **before** you enable voice:

   ```bash
   python infra/scripts/native_voice_setup.py agent-baseline \
     --tenant-id "$TENANT_ID" --org-url "$ORG_URL" --bot-id "$BOT_ID" \
     --output-dir "$WORK/agent-baseline-01"

   python infra/scripts/native_voice_setup.py agent-baseline \
     --tenant-id "$TENANT_ID" --org-url "$ORG_URL" --bot-id "$BOT_ID" \
     --output-dir "$WORK/agent-baseline-01" \
     --apply --confirm-organization "$ORG_URL"
   ```

   Review the saved plan first. It selects classic orchestration and the
   PVARecognizer, turns off generative and model-knowledge defaults, adds
   Arabic as the secondary language and allows anonymous telephone callers on
   this agent only. Apply sends one PATCH with the saved ETag; an existing
   intent is reconciled by readback, never resent.
4. Enable **Voice** with the basic (classic) voice experience and **Speech &
   DTMF** authoring. The **Save** button sits inside **Phone setup > Select
   voice**, not on the top-level Voice panel. Open that section, select
   **Save**, go back to Voice and confirm the toggle stays on. Saving voice drops
   the explicit PVARecognizer object; don't re-add it. `agent-baseline` leaves it
   absent on a voice-enabled agent.
5. Open **Channels** and select the **Dynamics 365 Contact Center** tile
   (**Dynamics 365 Customer Service** in older tenants). Use the panel's
   **Turn on Telephony channel** flow, then **Connect**
   ([hand off to Dynamics](https://learn.microsoft.com/en-us/microsoft-copilot-studio/configuration-hand-off-omnichannel)).
6. If the panel warns about missing extensions, install only the Omnichannel
   Power Virtual Agent Extension (`OmnichannelPrimePVAExtensionAnchor`) when the
   catalog lacks it:

   ```bash
   pac application list --environment "$ORG_URL" --installState All \
     --output "$WORK/applications-available-before.json"
   pac application install --environment "$ORG_URL" \
     --application-name OmnichannelPrimePVAExtensionAnchor
   pac solution list --environment "$ORG_URL"
   ```

   Confirm the install from its status and the solution list, not from the
   catalog's Installed label. Skip `mspva_TelephonyExtension`: its install
   failed with "Cab file required", and voice works without it. Don't download
   a CAB, change licenses or replace the environment to get past an install
   error; stop and ask the owner. Microsoft's
   [missing-extension guidance](https://learn.microsoft.com/en-us/troubleshoot/dynamics-365/customer-service/omnichannel-for-customer-service/agent-transfer-pva-bot-issue)
   lists three extensions; field deployments needed only this one. Once the
   panel shows **Connected** and **Voice On**, ignore the missing-solutions
   warning.
7. Open the standard **Escalate** system topic. Its starter version only says
   that escalation isn't currently configured. Replace that message with a
   **Transfer to agent** node with automatic context (`TransferConversationV2`
   / `TransferToAgent`); don't add an external phone number. **Fallback**
   redirects to Escalate, so a stub there also breaks unrecognized turns. The
   native controller keeps its own transfer.
8. Publish this safe version so the workstream can select the agent. It isn't
   the final publication.

The Copilot Studio test pane has no phone context, so it can't exercise the
native path: rejection with "Only approved test callers can use this line." is
expected there. Never remove the caller gate or copy an external-phone fallback
from a historical agent to make the test pane succeed.

`verify` ([section 11.2](#112-no-call-configuration-check)) later checks, from
the saved configuration:

- voice is saved: `isTelephonyEnabled=true`, `settings.TelephonyInitialized=true`
  and, when present, `voiceProcessingMode` `ClassicVoiceProcessingMode`;
- `OmnichannelPrimePVAExtensionAnchor` is installed, and reports its version;
  `mspva_TelephonyExtension` isn't required;
- Escalate has a Transfer to agent node, and no reachable topic, Fallback
  included, still contains or redirects to the starter message.

A connected Telephony channel and a successful publication don't prove that
voice is saved. Read the saved state; don't PATCH undocumented setup flags.

### 5.3 Finish the inbound workstream

Last verified: November 2026. If a screen differs, ask the owner for a
screenshot that includes the left navigation, with no secrets showing.

Open the workstream from 5.1 in the runtime admin center:

1. **Set up voice** and select `DYNAMICS_NUMBER`.
2. Languages:
   - Primary: **English - United States (`en-US`)**.
   - Secondary: **Arabic - Saudi Arabia (`ar-SA`)**.
   - Choose supported voices; the source environment used
     `en-US-AvaMultilingualNeural` and `ar-SA-HamedNeural`.
3. Recording/transcription: **Transcription and recording**, start
   **Automatic**, subject to the consent/notification policy approved for the test.
4. Preserve approved hold/wait music and recording pause/resume-on-hold behavior.
   Screen recording is not required for this voice integration.
5. Set the workstream's default/fallback queue to `QUEUE_ID`.
6. In the workstream's **Bots / Agents** section, add the published routing agent.
   Set its **Fallback action** to **Wait Music and Escalate**. `verify` fails
   while the AI agent failure action (`msdyn_botfailureactionconfig`) is empty.
   This handles bot failure; it is separate from the application-owned startup
   notice and normal requested human handover.
7. Ensure routing rules don't send the initial call directly to a human or to
   another bot. For the first deployment, keep one deterministic fallback queue.

Keep external phone **Consult** on so representatives can consult an outside
participant; `verify` fails while `msdyn_agentexternalparticipantcontrolenabled`
is false on this workstream's voice channel settings. External phone
**Transfer**, Teams consult and Teams transfer stay off; `verify` reports them.
**Use bridged transfers** was needed only for an earlier external-transfer
experiment; the native path uses **Transfer to agent**.

Retain the before-state and current ETags when correcting existing topics or
workstream bindings. Verify unrelated topics and routing/audio settings remain
unchanged, publish with explicit approval, and observe the native propagation
window. These configuration checks do not prove acoustic handover or native AI
transcription.

#### Provide the wizard values before asking the operator to start

The handoff must distinguish the new **voice channel name** (for example,
`<organization> Voice Support`) from the already-created workstream and queue.
Supply that name, the exact native number, primary/secondary languages and a
complete Behaviors matrix up front; do not make the operator ask for each field.

For the accepted controlled-demo baseline:

| Behaviors control | Selected value |
|---|---|
| Customer wait time | On; position and average-wait notifications checked |
| Channel operating hours | Off for this controlled demo; not an organization-hours policy |
| Transcription and recording | Transcription and recording; Automatic start |
| Representative recording/transcription pause and resume | Yes |
| Automatic recording/transcription pause on hold/unhold | No; preserve the reviewed policy |
| Show transcription by default | Yes |
| Request for User Consent | No only when fresh consent/readiness is obtained separately before each controlled test |
| Custom automated messages | None; do not add another greeting or recording notice |
| External phone consult | **On**; `msdyn_agentexternalparticipantcontrolenabled` reads `true` |
| External phone transfer, Teams consult and Teams transfer | Off for native same-conversation handover |
| Authentication & Anti-Fraud | Off unless separately selected and configured |
| Post-call survey | Off unless separately selected and approved |

These are reviewed demo choices, not unconditional production defaults or legal
consent advice. The pause/resume controls here affect **recording/transcription**,
not the separate explicit Pause AI feature. Preserve hold/wait music and apply
the application-owned startup-notice flags through the guarded CLI later.
After the wizard saves, read back its real settings, native number association,
language/voice records and exact workstream identity. Owner completion, saved
configuration, runtime propagation and actual UI/audio acceptance are different
evidence. Report any difference instead of silently rewriting operator choices.

### 5.4 Record actual IDs

```bash
python infra/scripts/native_voice_setup.py discover \
  --tenant-id "$TENANT_ID" --org-url "$ORG_URL" --bot-id "$BOT_ID"
```

Confirm `BOT_ID`, `WORKSTREAM_ID`, `QUEUE_ID`, and the runtime admin/workspace app
IDs. The output includes no topic content or control keys. Do not confuse:

- Copilot **bot ID** with its underlying native **bot system-user ID**.
- Voice workstream ID with voice channel settings ID.
- App module ID with Power Platform environment ID.

The catalogs contain at most ten **newest-created** records each, not every
record in the tenant. `--bot-id` adds metadata for the four required
topic/variable components, not unrelated topic content. Never select "the
latest conversation" or a record with `owned: false` to configure the
integration.

**Exit gate:** inbound workstream, number, both languages, agent, fallback
action and default queue are saved. Do not test HUMAIN yet.

For the application-owned startup notice, the portable
[`startup-audio` command](deploy-with-copilot.md#apply-the-accepted-native-audio-settings)
plans and conditionally applies the two native audio flags. It leaves recording,
consent and human wait/hold assets unchanged. The topic bundle alone does not
configure those workstream flags. Require the reviewed notice policy and the
reported 15-minute propagation window before caller acceptance.

---

## 6. Prepare the representative

Use one consenting, licensed test representative initially. Set them up with
the [single-representative setup](workspace-setup.md#single-representative-setup):
it adds the roles, the bookable resource, the inbound capacity profile and queue
membership through documented operations, with a readback. It creates no users
and buys no licenses.

This is the initial one-representative recipe. It is not permission to bypass
the separate multi-operator kit's two-approved-human activation gate, add other
people, manufacture availability, or treat configuration as microphone/presence/
handover acceptance.

The readback, or a manual check, must show:

1. In Dynamics user management, confirm the user is enabled and has an appropriate
   read/write access mode.
2. Assign required **Basic User**, **Omnichannel agent**, and applicable Customer
   Service/app-profile roles. Administrator roles are for configuration, not a
   substitute for representative setup.
3. Add the user to the dedicated voice queue.
4. Create/verify the user's active **Bookable Resource**, type **User**, linked
   to that exact Dynamics user.
5. Assign the **Default voice inbound** capacity profile used by the workstream.
   It must have at least one available slot.
6. Confirm the user belongs to the intended app experience/profile.
7. Open the **runtime representative workspace** and set presence **Available**.
8. Grant browser microphone access and select the headset.

The workstream is profile based. A generic capacity number of `100` is not a
replacement for its **voice inbound capacity profile**. A similarly named
"Escalation profile" is not automatically required.

For the supervisor dashboard, assign **Omnichannel supervisor explicitly**.
If the role is present on the correct user but the dashboard still says it is
missing, use a fresh private browser session. This resolved cached access in the
verified environment; see [dashboard troubleshooting](troubleshooting.md#dashboard-says-you-need-the-supervisor-role).

Use **one active representative workspace session** for the test. Close competing
old/private windows so call notifications and audio are not handled by another
browser session.

**Exit gate:** queue membership, bookable resource, voice capacity and live
Available state are all confirmed. Record `REPRESENTATIVE_ID`; use
`$ORG_URL/api/data/v9.2/WhoAmI` in the same browser if testing as that user.
Use the returned **`UserId`**, not the Bookable Resource ID or Entra object ID.

---

## 7. Give the application its Dataverse identity

The voice app reads native conversations with its user-assigned managed
identity (UAMI). `reader-role` creates a dedicated role with only the reads
below, creates or finds the Dataverse application user for that identity, and
assigns the role. It never adopts an existing role by name and never grants a
write.

### 7.1 Required reads

| Privilege | Depth | Purpose |
|---|---|---|
| `prvReadmsdyn_liveworkstream` | Global | Validate inbound voice workstream/bot/queue configuration |
| `prvReadActivity` | Local (business unit) | Read native conversations and sessions |
| `prvReadmsdyn_ocphonecallengagementctx` | Local | Read exact call correlation and phone-participant context |
| `prvReadUser` | Local | User/configuration access |
| `prvReadQueue` | Global | Queue configuration inspection |
| `prvReadmsdyn_ocphonenumber` | Global | Native number configuration inspection |

ReadActivity covers the activity-derived native conversation/session records.
Do not grant assignment, write, transfer or supervisor privileges to this
telephony-reader identity to "make handover work"; **Copilot performs native
transfer**. The separate CRM identity below has a different, explicitly bounded
customer/case role.

### 7.2 Create the reader role and application user

Record the business unit that holds the native conversation records as
`BUSINESS_UNIT_ID`. A new environment has one: the root business unit, which
`discover` prints as `actor.BusinessUnitId` for an administrator in it.

Plan, review the plan, then apply:

```bash
python infra/scripts/native_voice_setup.py reader-role \
  --tenant-id "$TENANT_ID" --org-url "$ORG_URL" \
  --business-unit-id "$BUSINESS_UNIT_ID" \
  --client-id "$UAMI_CLIENT_ID" --principal-id "$UAMI_PRINCIPAL_ID" \
  --output-dir "$WORK/reader-identity-01"

python infra/scripts/native_voice_setup.py reader-role \
  --tenant-id "$TENANT_ID" --org-url "$ORG_URL" \
  --business-unit-id "$BUSINESS_UNIT_ID" \
  --client-id "$UAMI_CLIENT_ID" --principal-id "$UAMI_PRINCIPAL_ID" \
  --output-dir "$WORK/reader-identity-01" \
  --apply --confirm-organization "$ORG_URL"
```

Apply journals each write before sending it and re-reads the role after it:

1. Creates the unmanaged role `Contact Center Native Voice Reader`, with an ID
   derived from the organization, business unit and identity. If a role with
   that name but another ID exists in the business unit, the run stops; it
   never adopts it. Use `--role-name` for another name.
2. Removes every privilege outside the required reads with
   `RemovePrivilegeRole`, one at a time. A new role arrives with about nine
   seeded defaults, including SharePoint create and write.
3. Adds the required reads with `AddPrivilegesRole`. Dataverse can then add
   SharePoint privileges back; the next audit removes them again.
4. Stops and asks the owner if a privilege comes back after two removals.
5. Finds the application user for `UAMI_CLIENT_ID`, or creates it as a
   non-interactive user in `BUSINESS_UNIT_ID`. An existing user with another
   role, another business unit or a disabled state stops the run.
6. Assigns the role with the public `systemuserroles_association/$ref` call.
   Don't use `pac admin assign-user`; in testing it failed after creating the
   user.

If a write fails, the command reads the server state. A write that took effect
anyway is recorded as confirmed; one that didn't stops the run without a retry.
Rerunning `--apply` with the same `--output-dir` resumes: it confirms earlier
steps by readback and never creates a second user. It won't resend a write
whose readback shows it wasn't applied. Fix the cause recorded in the journal,
asking the owner if it's unclear, then plan again in a new `--output-dir`.

Without `--apply`, an existing directory is audited read-only.

| Value | Must match |
|---|---|
| Dataverse application ID | Azure UAMI **client ID** |
| Dataverse Entra object ID, when populated | Azure UAMI **principal/object ID** |
| Business unit | `BUSINESS_UNIT_ID`, so Local-depth reads cover the native records |
| Roles | Only the reader role, holding exactly the six reads |
| Identity secret | None for the same-tenant UAMI |

Microsoft's background:
[application users](https://learn.microsoft.com/en-us/power-platform/admin/manage-application-users).
A deployer's successful `WhoAmI` doesn't prove the application can read records.
The authenticated voice `/dynamics/native/readiness` later tests the **Azure
application identity** from its actual compute.

**Exit gate:** the apply receipt reports exactly the required reads and one role
assignment to the identity's application user. Record its
`application_user_id` as `NATIVE_APP_USER_ID`. No cross-tenant federation or
copied Entra secrets are involved.

### 7.3 Configure the separate CRM identity, schema and seed

Skip this section when `crm` is not selected. Native voice then supplies no CRM
context provider and publishes no CRM tools.

Complete the [CRM setup chapter](crm-setup.md) before enabling the expanded
customer/case experience. This is a separate identity attached to the shared
assist service; do not elevate the telephony reader or replace the service's
existing `AZURE_CLIENT_ID`.

The repeatable sequence is:

1. Capture the CRM managed identity's client/principal IDs from the deployment.
   With native voice, put `WORKSTREAM_ID` from
   [section 5.1](#51-create-the-minimal-shell-one-queue-and-one-voice-workstream)
   in `workstream_ids`.
2. Plan the dedicated Dataverse app user, exact runtime role and schema.
3. Review the private plan, then explicitly apply it.
4. Publish only the affected schema and wait for both alternate keys to become
   Active; provision the optional contact language field as part of this schema.
5. Default to schema/identity only. Seed explicitly approved private fictional
   records only when requested, using `--approve-seed`; never invent a dataset
   simply to make a readiness check look successful.
6. Configure the shared service, deploy its reviewed immutable image and run its
   authenticated readiness check from the real managed identity.
7. Verify scoped read/create/follow-up behavior and no Cosmos case fallback.

The native conversation update pipeline additionally needs organization-level
`prvReadPluginAssembly` on the **CRM** role. Activity write permission alone was
not sufficient: the native plug-in failed to load under the restricted app user.
Only assembly read was added; plug-in writes and runtime administration were not.

**Exit gate:** actual shared-service readiness passes and any selected private
seed/cleanup manifest is recorded. Customer-context acceptance requires an
eligible contact; an empty schema does not establish that scenario.
This does not yet prove that a case is associated with a live conversation.

---

## 8. Create the two topic shells and external context variable

Use **English as the editing language** in Copilot Studio.

### 8.1 NativeMediaContext: define, but never execute, the global

For an explicitly selected new agent, the input global can be created through
the public Dataverse component API using the guarded CLI. Preview first:

```bash
python infra/scripts/native_voice_setup.py context-variable \
  --tenant-id "$TENANT_ID" --org-url "$ORG_URL" --bot-id "$BOT_ID"

python infra/scripts/native_voice_setup.py context-variable \
  --tenant-id "$TENANT_ID" --org-url "$ORG_URL" --bot-id "$BOT_ID" \
  --apply --confirm-organization "$ORG_URL" \
  --receipt-file "$WORK/conversation-context-global-01.json"
```

This creates only the unused input definition, not a topic, channel or
publication. The receipt is written before the request. Reconcile that exact
component after an uncertain outcome; never generate a replacement ID. An
existing matching definition is read-only verified, not overwritten or claimed
as newly owned. The following topic initializer must still refer to that global.

1. Create a blank topic named exactly **NativeMediaContext**.
2. Add **Variable management -> Set a variable value**.
3. Create a **String** variable named exactly **msdyn_ConversationId**.
4. Change scope to **Global / any topic can access** (conversation scoped).
5. Enable **External sources can set values**.
6. Do **not** enable output to external callers.
7. In Reference, use **Get value from this node if empty**, if presented.
8. Use the formula `=""` / `Text("")` as the empty initializer.
9. Save.

The reference global-variable definition should resemble:

```yaml
kind: Variable
name: msdyn_ConversationId
scope: Conversation
isExternalInitializationAllowed: true
isOutputToExternalCallers: false
initializationTimeoutInMilliseconds: 0
```

This is **variable metadata**, not a topic to paste into the topic editor.
The setup utility checks the actual saved variable.

The saved SetVariable node must refer to:

```yaml
variable: init:Global.msdyn_ConversationId
value: =""
```

If the native integration already supplies the external definition and the
compiler reports `DuplicateVariableInitializer`, reference the existing input
instead of redeclaring it: `variable: Global.msdyn_ConversationId`. The native
bundle helper permits this only after verifying the same conversation-scoped,
input-only external global. It preserves the exact empty String assignment
and unused redirect-only context topic; controller/caller guards are unchanged.
Do not delete the global, rename the native contract, create a second initializer
or execute this topic to obtain a conversation ID.

Keep only this initializer in the context topic. Do not call NativeMediaContext
from Conversation Start or any runtime topic. Its generated `OnRedirect`
trigger has no intent phrases.

Executing that initializer cleared the real conversation ID in the initial
trials. `init:` does not protect an executed SetVariable action from overwriting it.
The runtime must only read `Global.msdyn_ConversationId`. Global-variable timeout
settings are not the synchronization mechanism for this IVR path.

### 8.2 NativeSpeechTurnPilot: empty shell

Create another blank topic named exactly **NativeSpeechTurnPilot** and save it
without actions. The setup utility will fill it with the speech-turn
controller and an `OnRedirect` trigger.

The utility expects exactly:

- One `Conversation Start` topic.
- One `NativeMediaContext`.
- One `NativeSpeechTurnPilot`.
- One `msdyn_ConversationId` global variable component.

If the UI does not show the Reference option or the variable type is wrong,
stop and inspect its saved metadata rather than adding a second initializer.
See [context troubleshooting](troubleshooting.md#conversation-id-is-empty-or-invalid).

**Exit gate:** both topic shells and the external String variable are saved.
No runtime call reaches the initializer.

---

### 8.3 Add the selected-case output variable

For the CRM-enabled controller, create the case-sensitive conversation-scoped
**CaseNumber** output global. It carries the real Dynamics `ticketnumber`, not a
case GUID or an `OP-` reconciliation handle.

```bash
python infra/scripts/native_voice_setup.py case-context-variable \
  --tenant-id "$TENANT_ID" --org-url "$ORG_URL" --bot-id "$BOT_ID"

python infra/scripts/native_voice_setup.py case-context-variable \
  --tenant-id "$TENANT_ID" --org-url "$ORG_URL" --bot-id "$BOT_ID" \
  --receipt-file "$WORK/case-number-variable-01.json" --apply
```

The first command inspects without writing; the second creates only the reviewed
unused output definition and a new private rollback receipt. It does not publish.
Deploy a compatible runtime before publishing a controller that consumes its
selected-case fields. Existing normal and cleanup/recovery paths use their
respective authoritative backend snapshots.

## 9. Generate and stage the controller

### 9.1 Export a private deployment bundle

Record the E.164 test caller as `TEST_CALLER`. If using an automated,
owned-number test runner later, add its source number as a second allowed
caller. Do not put arbitrary public callers into this pilot.

```bash
python infra/scripts/native_voice_setup.py bundle \
  --tenant-id "$TENANT_ID" --org-url "$ORG_URL" \
  --bot-id "$BOT_ID" --voice-origin "$VOICE_ORIGIN" \
  --test-caller "$TEST_CALLER" \
  --secrets-file "$WORK/control-keys.env" \
  --startup-notice \
  --output-dir "$WORK/topic-bundle-01"
```

Add `--crm-case-context` only when `crm` is selected; that variant uses
`--startup-notice --crm-case-context`. Do not enable a controller-side CRM path
against an unselected API.
`--startup-notice` uses the packaged recording-only Arabic/English WAV at 1.3x;
it does not require synthesizing speech on every deployment or call. It replaces
only startup waiting audio, not human-queue or hold music. See the
[notice configuration and playback gates](operations.md#optional-startup-only-bilingual-notice).

Add `--test-caller "$APP_NUMBER"` only if your approved synthetic tests need that
route. The generator supports one to three explicitly selected caller numbers.

Generated private files:

| File | Purpose |
|---|---|
| `NativeSpeechTurnPilot.topic.yaml` | Speech-turn controller with your origin/control key |
| `NativeMediaContext.topic.yaml` | Unused external-variable definition topic, preserving its actual node ID |
| `ConversationStart.topic.yaml` | Explicit test-caller gate; original non-test actions preserved in `elseActions` |
| `before.json` | Exact previous definitions and publication timestamp for recovery |
| `bundle.json` | Target IDs, ETags and generated-file checksums |

**The bundle contains credentials and caller IDs.** Keep it private. Never attach
it to an issue, paste it into a chat, or commit it.

For inspection without secrets, use the checked-in
[base controller snapshot](../../apps/voice/dynamics/native-speech-turn-pilot.topic.yaml)
or [CRM controller snapshot](../../apps/voice/dynamics/native-speech-turn-crm-pilot.topic.yaml).
These snapshots use placeholders; the private bundle adds your selected runtime
options and environment values. They are generated by
[sync_native_voice_topic.py](../../infra/scripts/sync_native_voice_topic.py), not
manually maintained:

```bash
python infra/scripts/sync_native_voice_topic.py --check
python infra/scripts/sync_native_voice_topic.py --crm-case-context --check
```

### 9.2 Understand the behavior before staging

The generated topic:

1. Captures the real Dynamics conversation ID once.
2. Selects application-only playback of the approved startup notice when opted
   in; native recording latency announcements stay disabled.
3. Starts `/dynamics/native/sessions?wait_for_startup=true` with
   `speech_turn_control: true` and `play_startup_notice: true` for that option.
4. Uses native **Locale choices**, aligning with a validated CRM initial-language
   preference before HUMAIN starts; Arabic remains the unconfigured default.
5. Executes silent listening turns: 500 ms silence, 300 ms utterance end,
   1000 ms recognition timeout, no reprompts, empty-value continuation.
6. Posts **only `{language, revision}`** to `/turn`.
7. Ignores the mixed recognized speech. It is not trusted caller intent.
8. Switches between Arabic and English only on the backend's trusted language
   request and acknowledgement protocol.
9. Captures a validated selected-case number, when present, for native handover.
10. Executes **native Transfer to agent** only after confirmed media detachment.
11. Cleans up and reports failures explicitly.

HUMAIN supplies the spoken replies. This topic handles native call control;
it leaves the transcript to Dynamics.

### 9.3 Preview, then stage atomically

```bash
python infra/scripts/native_voice_setup.py stage \
  --bundle-dir "$WORK/topic-bundle-01"

# Review the proposed files and affected topic names before this command:
python infra/scripts/native_voice_setup.py stage \
  --bundle-dir "$WORK/topic-bundle-01" --apply
```

The first command is read-only. The second sends one conditional Dataverse
changeset for the changed topics. It refuses stale drafts, changed variables,
new publications and altered generated files. It never publishes.

Do not edit the bot in another browser while staging. After staging, close the
old topic editor, refresh and reopen it; **do not save stale canvas content**.

For a later revision with existing controller actions, `bundle` requires
`--replace-controller`. Review that overwrite proposal against your custom edits and use a new bundle
directory; the utility cannot merge them automatically.

### 9.4 Manual paste alternative

If direct authoring API access is restricted:

1. Generate the private bundle using an authorized export identity.
2. Open each corresponding topic's code editor.
3. Paste its generated YAML in the same topic.
4. Keep the existing external variable metadata.
5. Save all three, reopen, and run Topic checker.

Do not paste a YAML *variable definition* as an adaptive topic. Do not paste the
unresolved public snapshot and forget the origin/key placeholders.

**Exit gate:** saved draft is complete; Topic checker has zero errors. Do not
publish the new caller route until the backend and observer are ready.

---

## 10. Configure the native runtime and passive observer

### 10.1 Select an explicit observation lease

The observer is required for native call correlation. Choose either an expiring
lease or explicit permanent mode. In leased mode, the runtime and Event Grid
subscription must use the same future expiry:

```bash
export PROBE_EXPIRES_AT="$(date -u -d '+24 hours' '+%Y-%m-%dT%H:%M:%SZ')"
```

This syntax is GNU/Linux. On other platforms, calculate an equivalent UTC ISO
timestamp. Record it in the handoff. If setup extends beyond the lease, renew
both sides.

For a continuously operating entry point, use `--permanent` instead of
`--expires-at "$PROBE_EXPIRES_AT"` in **both** commands below. This writes
`DYNAMICS_NATIVE_PROBE_PERMANENT=true`, clears the runtime expiry and omits
the Event Grid expiry. The two modes are mutually exclusive.

Permanent mode keeps the dedicated key, exact resource/call checks and
15-minute metadata lookback. It does not make the in-memory observer durable
across restarts. See [permanent observer operations](operations.md#permanent-observer).

### 10.2 Write the complete runtime settings privately

```bash
python infra/scripts/native_voice_setup.py runtime-env \
  --tenant-id "$TENANT_ID" --org-url "$ORG_URL" \
  --workstream-id "$WORKSTREAM_ID" \
  --native-acs-resource-id "$NATIVE_ACS_ID" \
  --expires-at "$PROBE_EXPIRES_AT" \
  --fast-start \
  --secrets-file "$WORK/control-keys.env" \
  --output "$WORK/native-runtime-01.env"

azd env set --file "$WORK/native-runtime-01.env" --environment "$ENV"
chmod 600 ".azure/$ENV/.env"
```

`runtime-env` reads the native ACS connection string into the private file; it
does not print it or apply the settings to Azure. It verifies the native ACS
subscription belongs to the selected tenant.
`--app-number "$APP_NUMBER"` is optional for a separately selected application
phone path. Omitting it preserves that environment value rather than clearing it.

It enables native media with the `voip` transport and the
complete observer resource/key/lifetime configuration. `--fast-start` enables the tested
parallel setup and one-unused-page warmup by writing
`DYNAMICS_NATIVE_MEDIA_FAST_START=true`. Omit that flag for an instrumented serial
baseline (`false`).

For another inbound entry, add `--additional-workstream-id "$SECOND_WORKSTREAM_ID"`.
This keeps the primary ID and writes an explicit additional allowlist; it does
not accept arbitrary workstreams. Configure that entry with the same agent,
queue, languages and call behavior first. See
[additional inbound numbers](operations.md#additional-inbound-numbers).

### 10.3 Apply infrastructure settings

For this newly created environment:

```bash
azd provision --preview --environment "$ENV"
# Review existing images and stateful resources. Do not accept replacement/deletion surprises.
azd provision --environment "$ENV"
```

This is necessary to apply Bicep environment variables and Container Apps
secrets. **Changing `.azure/<environment>/.env` alone is not a live update.**
The post-provision knowledge hook may run again; its upload is merge-based.

Verify the application still runs the real image. `azd deploy voice` deploys code;
it is not a general substitute for applying changed infrastructure parameters.
See [operations](operations.md#applying-future-changes) for scoped later updates.

### 10.4 Deploy the third subscription: passive native call metadata

```bash
python infra/scripts/native_voice_setup.py observer-parameters \
  --system-topic-name "$SYSTEM_TOPIC" \
  --voice-origin "$VOICE_ORIGIN" --expires-at "$PROBE_EXPIRES_AT" \
  --secrets-file "$WORK/control-keys.env" \
  --output "$WORK/observer-01.parameters.json"

az deployment group create --name native-call-observer \
  --resource-group "$NATIVE_RG" --subscription "$SUBSCRIPTION_ID" \
  --template-file infra/dynamics-handover-probe.bicep \
  --parameters "@$WORK/observer-01.parameters.json" \
  --output none
```

This adds `moacc-native-handover-probe` on the **existing native system topic**:

- Event: `Microsoft.Communication.IncomingCall`.
- Destination: application `/dynamics/handover/probe/events`.
- Dedicated, secret `X-API-Key` delivery property.
- Explicit expiry, or no expiry when `--permanent` was selected.
- **No call answering, redirection, participant invitation or recording action.**

It discards the incoming answer token, numbers and names, retaining only bounded
correlation metadata. Dynamics remains the only subscriber that answers native
phone calls. `CallStarted`/`CallEnded` are not substitutes for this PSTN/bot
correlation path.

Verify three native subscriptions, including the two original answering/
recording subscriptions. Do not overwrite either of those with the probe.

**Exit gate:** the running native runtime is configured; observer is enabled and
its lifetime mode matches Event Grid; all three native subscriptions are `Succeeded`; the two
native answering/recording endpoints and authentication are unchanged.

---

## 11. Publish once and validate without a call

### 11.1 Native topic checker

Refresh/reopen the three topics. Check:

- `NativeSpeechTurnPilot`: no errors.
- `NativeMediaContext`: only the unused initializer; no trigger phrases.
- `Conversation Start`: intended test caller gate and original `elseActions`.
- `User.Language` assignments show **Arabic/English choices**, not raw strings.
- Silent Question has **no spoken yes/no menu**, no topic interruptions and
  **Set variable to empty**, not Hang Up, as its silence fallback.
- The initial HTTP action's native latency recording announcement is disabled;
  the application plays the notice. Preserve the generated polling/listening
  settings rather than adding repeated native speech.
- The CRM controller sets `Global.CaseNumber` from the correct normal/recovery
  result immediately before transfer; the output global is externally visible.

Now select **Publish once at the agent level**. All saved topic/language changes
are included. An unsaved canvas is not included.

With explicit publication approval, publish from the console with
[pac_publish.py](../../infra/scripts/pac_publish.py). It runs
`pac copilot publish` once and proves the result by Dataverse readback:

```bash
python infra/scripts/pac_publish.py --tenant-id "$TENANT_ID" --org-url "$ORG_URL" \
  --bot-id "$BOT_ID" --receipt-dir "$WORK/publish-01"

python infra/scripts/pac_publish.py --tenant-id "$TENANT_ID" --org-url "$ORG_URL" \
  --bot-id "$BOT_ID" --receipt-dir "$WORK/publish-01" \
  --apply --confirm-organization "$ORG_URL"
```

The first command only previews. The result is `published` only when the
agent's `publishedon` advanced and the newest finished publish operation in its
public `synchronizationstatus` is `Succeeded`. A zero PAC exit is not proof, and
a `Failed` result with diagnostics stays a failure even if PAC exits zero. Do
not call the internal Dataverse `PvaPublish` action. Keep the original result,
correct only the identified authoring defect, then retry once in the same
receipt directory with `--retry-change "<what changed>"`. Also confirm that the
required component definitions and modification times match the reviewed
bundle. CLI and server checks don't prove actual Topic Checker rendering or
audio. See [Power Platform CLI results](troubleshooting.md#power-platform-cli-results).

### 11.2 No-call configuration check

```bash
python infra/scripts/native_voice_setup.py verify \
  --tenant-id "$TENANT_ID" --org-url "$ORG_URL" \
  --bot-id "$BOT_ID" --workstream-id "$WORKSTREAM_ID" \
  --voice-origin "$VOICE_ORIGIN" \
  --secrets-file "$WORK/control-keys.env" --crm-case-context
```

The utility checks saved publication, controller URLs/keys, required language
settings, the inbound workstream, Azure VoIP readiness, real application
Dataverse access, and disabled legacy conferencing/outbound submission.
It places no calls. Test event delivery, media and the native UI in phase 12.

Before any runtime check it reads the native configuration and fails when:

- the workstream's AI agent failure action (`msdyn_botfailureactionconfig` on the
  [workstream](https://learn.microsoft.com/en-us/dynamics365/developer/reference/entities/msdyn_liveworkstream))
  is empty;
- the workstream's
  [voice channel settings](https://learn.microsoft.com/en-us/dynamics365/customer-service/develop/reference/entities/msdyn_ocvoicechannelsetting)
  record, linked through `msdyn_liveworkstreamid`, is missing, ambiguous or
  inactive, or external phone consult
  (`msdyn_agentexternalparticipantcontrolenabled`) is off. External phone
  transfer and both Teams controls are reported against their expected off
  state;
- the `OmnichannelPrimePVAExtensionAnchor` solution isn't installed. Its version
  is reported; `mspva_TelephonyExtension` isn't required;
- the Escalate topic lacks Transfer to agent, or any reachable topic, Fallback
  included, contains or redirects to the starter "not currently configured"
  message.

Current readiness includes the allowed workstream IDs and names in `workstreams`;
the checker validates the requested entry against that list. It retains the
primary `workstream` name for older clients. On an older deployment that exposes
only the name, confirm the exact UUID in the private runtime export.
Confirm the selected published agent in each workstream UI.

Expected native readiness:

```json
{
  "enabled": true,
  "observer_enabled": true,
  "dataverse_connected": true,
  "transport": "voip",
  "voip_gateway_ready": true,
  "fast_start": true,
  "warm_page_ready": true
}
```

An unused warm page has no token, ACS identity or caller history.

Do not use a made-up conversation UUID to exercise live setup. After you have a
closed native call, its exact ID can be used for a no-call negative
guard test; see [acceptance](operations.md#no-call-checks).

### 11.3 Check durable telemetry

The current root Bicep uses **Azure Monitor** for the voice environment and
`runtime-logs` diagnostic settings targeting the existing workspace. Console
and system logs are enabled; HTTP diagnostics are not.

An ordinary `/health` request produces a harmless access-log event. Verify it
arrives in `ContainerAppConsoleLogs`; later calls emit `VOICE_LATENCY` records.
Ingestion took roughly 6-7 minutes during testing. Empty immediate queries are
not evidence that the call is delayed. See [telemetry queries](operations.md#telemetry).

**Exit gate:** published checker-clean controller, no-call runtime checks pass,
correct native event subscriptions, telemetry routing, and representative readiness.

### 11.4 Provision and verify the representative experience

Complete the [workspace setup chapter](workspace-setup.md), using only the
intended representative IDs. Its dependency order matters:

1. Verify app/license access and native UI-language availability.
2. Provision Arabic through the documented asynchronous language operation if
   needed; preserve the representative's current UI/help language.
3. Publish reviewed English/Arabic article pairs and verify the actual Copilot
   knowledge-source provisioning state.
4. Create the nondefault experience profile, voice/case sessions, localized
   scripts and shared macros.
5. Use a scoped contact form with **All Cases** filtered to the current
   customer; register the form with the app and pin only the intended templates.
6. Align case/join/end/on-demand summary settings, verify native selectors and
   associations, then reload the representative workspace.

Knowledge Q&A does not inherit HUMAIN's CRM tools. The live-case querying
extension in representative Copilot is a separate capability, not enabled by
publishing knowledge articles. Also keep optional routing/supervisor controls
separate from the initial single-representative baseline.

---

## 12. Run the controlled acceptance call

Use the [complete acceptance procedure](operations.md#controlled-live-acceptance).
Complete it before handing over the deployment.

Before dialing:

1. Confirm no active native call or unfinished wrap-up.
2. Confirm the observer is enabled, voice health, and one free inbound voice slot.
3. Use one representative workspace browser session with headset, Available.
4. Agree caller, recipient number, cost and **90-second hangup limit**.
5. Arm the portable monitor and wait for actual `ARMED` output.

```bash
python infra/scripts/watch_native_voice.py \
  --tenant-id "$TENANT_ID" --org-url "$ORG_URL" \
  --voice-origin "$VOICE_ORIGIN" \
  --workstream-id "$WORKSTREAM_ID" --queue-id "$QUEUE_ID" \
  --representative-id "$REPRESENTATIVE_ID" \
  --secrets-file "$WORK/control-keys.env" \
  --output "$WORK/acceptance-01.json" --case-links --arm
```

Then make **one call** from the allowed test phone to `DYNAMICS_NUMBER`.
Check the recording notice, CRM-backed greeting and intended initial language.
Ask the current status of one specific case, then test explicit language changes
and one human-handover request within the agreed limit. Inspect the actual
customer/case associations, native acceptance, speaker separation and artifacts.
After hanging up, check knowledge citations, scripts/macros, summaries and
resolved as well as active customer cases without keeping the phone connected.

A case mentioned from prefetched context is not necessarily a selected case.
Record any missing Case Details binding or uncertain link receipt as an open
result; do not manually link a historical record to make the test pass.

The monitor is read-only. It cannot end a call. **Hang up by the agreed limit
even if the monitor is still running.** Do not wait on a paid call for someone
to investigate a log, and do not redial inside the same armed window.

**Exit gate:** every acceptance item is recorded with actual evidence. A config
check or a monitor's frame counters alone cannot establish this result.

---

## 13. Hand the environment to the next team

Use the [handoff checklist](operations.md#handoff-checklist).
At minimum deliver:

- Repository revision and any uncommitted changes needed to reproduce the build.
- Azure/Power Platform selections, runtime app links, resource/record IDs.
- Secure secret-store locations, owners and rotation procedure, not the values
  in a general-purpose report.
- Current published topic version and private before/after bundle.
- Observer lifetime mode; expiry and renewal owner if using a lease.
- Acceptance report and native conversation/recording/transcript references.
- Current caller gate, rollback controller/route and fast-start setting.
- CRM app-user/role/schema/key verification, private synthetic seed/cleanup
  manifest and actual shared-service readiness result.
- Workspace app/profile/form/template/script/macro/article manifests, exact
  changed settings and their rollback references.
- Language provisioning and Copilot source readiness, keeping knowledge Q&A
  separate from live-case querying and generated-summary acceptance.
- Service-acquisition eligibility, lifecycle source/date and Teams migration owner.
- Dynamics region and the pipeline gate result (`Elastic`).
- Known limitations: generic label, analytics-role classification, post-handover
  automatic language detection, single-process limits and remaining tests.

When the implementation changes, update the renderer, regenerate the shareable
snapshot and run the kit tests. Update acceptance claims only after testing the
changed behavior.
Commit or otherwise deliver the exact reviewed source before another team
replays it: a base commit hash alone does not include uncommitted setup changes.

### How this runbook was validated

The integration's phone/media claims come from the controlled and human
calls described in [verified results](reference.md#measured-results).
The documentation kit was checked with unit/contract tests,
generated-snapshot comparison, local-link validation and Bash syntax checks.
Its discovery and no-call verification commands were executed read-only against
the working tenant. The `reader-role` bootstrap, the `connection` readback, the
`copy-sheet` command, the ownership classification in `discover` and the
native configuration checks in `verify` have offline tests only; run them in
plan or read-only mode first in a new tenant.

The generated controller was compared with the saved controller used in those calls.
The new atomic staging helper is tested with simulated Dataverse responses;
a second empty-tenant deployment using this kit has not been rerun.
Writing this documentation required no new publication or paid call.
Repeat the exit checks when deploying to the next tenant.

### Expanding beyond the single-representative baseline

Use the [routing foundation](routing-setup.md) to discover and review approved
human identities, native queue membership and existing capacity-profile
associations. Its default mode is read-only and can audit a one-person baseline;
applying a multi-operator plan requires at least two individually approved,
eligible humans, entitlement confirmations and a fresh drained maintenance
window. It does not create users, buy licenses, alter the queue strategy or
write dynamic capacity counters.

This is not full skills/calendar/overflow/SLA configuration, and it does not
raise the application's single-concurrent-AI-call limit. Those policy and
capacity decisions need separate inputs and acceptance. The source environment
has not been silently expanded to another operator.

For supervised intervention, use the separate
[native Pause AI recipe](supervision-setup.md). Deploy compatible code disabled,
review the native schema/permissions and exact control-tab binding, stage and
publish the compatible controller, verify its pins, then explicitly enable the
runtime feature. Native Monitor/Join alone does not issue a pause command.

The pause recipe must be composed with the workspace's ownership/association
receipts; do not append an unreviewed tab or hand-edit old receipts to hide drift.
Keep the same native conversation and distinguish paused AI from a formally
transferred/assigned human call. No real UI/audio acceptance is implied by
configuration or contract tests.
