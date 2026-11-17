# Deploy the contact center

This guide is for the AI coding agent that deploys the template and for the
person it works with, called the owner here. The agent interviews the owner
once, proposes a configuration, prepares a private workspace and runs the
template's tools: Bicep and azd for Azure, guarded Python scripts for
Dynamics 365, and the Azure CLI, Power Platform CLI and Dataverse Web API. It
stops where a person has to decide or act.

The result is a **private prepared workspace** and the resources deployed from
it. Profiles, content, credentials and receipts stay in that workspace. Nothing
customer-specific goes back into this template's Git repository.

When `native-voice` is selected, the
[native sub-workflow](dynamics-native-voice/deploy-with-copilot.md) continues
from section 7.

## Working rules

1. **CLI first.** Use the template's scripts, azd and Bicep, the Power Platform
   CLI (PAC), the Azure CLI and the Dataverse Web API. Call an undocumented
   Dataverse action only where these guides give the exact request. Never
   automate a browser or call endpoints copied from browser traffic.
2. **Read back after every write.** Server state decides whether a step worked,
   not the exit code. PAC can exit 0 after a failure and 1 after a success; see
   [Power Platform CLI results](dynamics-native-voice/troubleshooting.md#power-platform-cli-results).
3. **Discover before asking.** Use read-only commands for anything they can
   answer: tenants, subscriptions, environments, licenses, region options and
   existing resources. Ask the owner only for a sign-in, a permission, a file
   or a UI step, and only when a concrete result shows it's needed.
4. **Give UI steps one screen at a time.** Where no CLI path exists, or it
   failed, give the smallest UI step with every value filled in. Portal steps
   carry a "Last verified" date. If a screen differs, ask for a screenshot that
   shows the left navigation and no secrets.
5. **Ask once for routine work.** Get one reviewable approval for the agreed
   scope: named resources, roles and setup steps. Ask again only for a new
   consequence: a cost, a purchase, publication, consent, a live call or a
   destructive change. Batch those questions.
6. **Stop after the second failure.** When a step fails, read back first,
   because the write may have partly succeeded. Retry once, and only with a
   concrete change. If it fails again, stop and ask the owner: show the
   command, the error, the readback and the permission or UI step that would
   unblock it. Don't try workarounds or experiments while you wait.
7. **Report three levels.** For each component, say whether it is connected,
   configured, or accepted on a real call. Connected and configured don't mean
   that calls work.
8. **Get HUMAIN facts from HUMAIN.** This repository holds no HUMAIN pricing,
   data-flow, processing-location or retention facts, so don't answer those
   questions from it. Research HUMAIN's current published documentation,
   starting with the
   [connection contract](https://humain-voice-agent.share.sanad.ink/docs/connect.html),
   cite the page, and refer the owner to HUMAIN for anything it doesn't settle.
9. **Price from the live price list.** Don't quote Azure prices from memory or
   from this repository. Use `template_setup.py scope --prices`, which reads
   the Azure Retail Prices API and dates the result.

## 1. Set up the private workspace

Use Linux, WSL or a dev container with Python 3.12, Node 22, the Azure CLI and
azd. Dynamics modules also need the Power Platform CLI
([section 3](#3-choose-or-create-the-dynamics-environment)).

Create an owner-only directory outside the checkout and a Python environment
for the deployment tools:

```bash
umask 077
export SOURCE_ROOT="$PWD"
export PRIVATE="$(mktemp -d "$HOME/contact-center-deployment.XXXXXX")"
mkdir "$PRIVATE/inputs"
python3 -m venv "$PRIVATE/venv"
export PYTHON="$PRIVATE/venv/bin/python"
"$PYTHON" -m pip install -r infra/scripts/requirements.txt
"$PYTHON" infra/scripts/template_setup.py init --output "$PRIVATE/profile.json"
```

Each agent tool call starts a new shell, so exported variables don't carry
over. Keep the non-secret values (`PRIVATE`, `PYTHON`, tenant, subscription and
environment names) in a private file such as `$PRIVATE/session.env`, and load
it at the start of each command. Once section 7 runs `azd env set PYTHON`, the
azd hooks find the interpreter on their own.

Keep this deployment's sign-in separate from the owner's other work. In every
shell, point the Azure CLI and azd at the private directory:

```bash
export AZURE_CONFIG_DIR="$PRIVATE/azure-cli"
export AZD_CONFIG_DIR="$PRIVATE/azd"
az login
az account list --query "[].{name:name, subscription:id, tenant:tenantId}" --output table
azd auth login --tenant-id "$TENANT_ID"
```

The owner completes each sign-in. Ask which tenant and subscription to use
only when more than one is eligible.

The agent edits the private profile, not
[the neutral example](../infra/template-profile.example.json). File references
are absolute paths to owner-only private inputs. The source checkout stays free
of deployment data and credentials.

`validate` checks structure; `plan` also lists missing inputs, dependencies and
human gates. Neither signs in or changes cloud resources.

```bash
"$PYTHON" infra/scripts/template_setup.py validate --profile "$PRIVATE/profile.json"
"$PYTHON" infra/scripts/template_setup.py plan --profile "$PRIVATE/profile.json"
```

A valid draft isn't ready to prepare until `missingInputs` is empty.

## 2. One brief, then one proposal

Use the chat client's structured question tool, and ask once, in one
interaction:

| Input | Why it matters |
|---|---|
| Organization, use case and audience | Keeps the demonstration focused |
| Channels: chat, browser voice, phone, email, representative desktop | Decide licenses, geography, cost and tests |
| Existing systems: Dynamics, Azure, phone numbers, email | Mark what must not be reused or written to |
| Approved sources: web pages or documents | Bound what the assistants can say |
| Brand guidelines | Set colors, logo and tone. A guidelines document gives better results than a website alone |

Ask **one question about additional tools**. Don't ask for Bicep parameters,
every color or every line of UI copy.

Give the owner the full path of `$PRIVATE/inputs` for brand guidelines, logos
and source documents. Files saved in the checkout fail the template check and
risk an accidental commit. Move any you find there before preparation;
`check_template.py` lists them.

Then propose:

- Modules, each marked include, defer or exclude, with the reason for every
  inferred exclusion. `plan` prints each module's purpose, dependencies,
  licenses, permissions and gates.
- Brand and assistant names, palette, logo, layout, language and welcome
  experience.
- Assistant behavior, approved source facts and the business actions it can
  take.
- Whether private fictional CRM records would help, and the presenter mode
  ([section 5](#presenter-mode)).
- Existing email or knowledge integrations and any additional tools.
- The Azure scope and cost and, for Dynamics voice, the region. Both are
  described below.

Infer sensible choices, show the proposal and let the owner approve or correct
it. Don't invent business policies, fees, requirements, service deadlines or
external integrations. After approval, ask only about consequential gaps: a
missing credential, a region or license conflict, an external write, a
specific recipient or a UI step.

Several Dynamics-related names sound alike. Describe them by what they do:

| Name | What it does |
|---|---|
| Native Copilot (`native-copilot`) | Microsoft's assistant for representatives in Dynamics. It answers from approved knowledge and writes summaries |
| Routing agent (part of `native-voice`) | A thin Copilot Studio agent that controls the phone call and hands the audio to HUMAIN. Callers don't talk to it |
| Assist API (`crm`, `knowledge`) | This template's service. It reads and writes Dataverse customers and cases and searches approved knowledge |
| Custom console (`agent-console`) | This template's own representative page for web chat handover. It isn't the Dynamics workspace |
| Agent assist (`agent-assist`) | Live insights and supervisor views for this template's own channels. It isn't Dynamics analytics |

### Azure scope and cost

```bash
"$PYTHON" infra/scripts/template_setup.py scope --profile "$PRIVATE/profile.json" --prices
```

`scope` lists the Azure resources, identities, role assignments, models with
SKU and capacity, regions and warm replicas for the selection. `--prices` adds
a dated warm-replica estimate from the public Azure Retail Prices API. Present
usage-based charges such as model tokens, Search, calls and numbers as rates,
with the estimate's date and horizon. Three points are easy to misread:

- Every deployed app keeps at least one replica running, so the Container Apps
  environment costs money even when idle.
- GlobalStandard model capacity is a rate limit, not a provisioned-throughput
  purchase.
- The `insights` model parameters also configure the retrieval model that
  Foundry IQ needs, so selecting `knowledge` deploys that model.

The `azd provision --preview` table in section 7 is partial: it omits
Communication Services, Email, identities and role assignments. Review the
`scope` output alongside it.

### Region for Dynamics voice

Native voice needs a Dynamics environment whose voice conversations run on the
`Elastic` pipeline. On the older `CTQM3` pipeline, HUMAIN's speech is missing
from the saved Dynamics transcript. The environment's region decides the
pipeline, so the default is Switzerland (`crm17`), which runs Elastic. Tell the
owner the consequences before they approve:

- Dynamics data is stored in Switzerland.
- Native Copilot offers case summaries and Ask a question in Switzerland, but
  not conversation summaries; see
  [Copilot feature availability by region](https://learn.microsoft.com/en-us/dynamics365/customer-service/administer/cs-region-availability-service-limits).
- The native ACS resource is created with data location Switzerland.

Section 3 checks the pipeline in every environment, Switzerland included.

## Module boundaries

| Selection | Application services | Additional Azure resources / prerequisites |
|---|---|---|
| `web-chat` | Orchestrator + customer UI | Foundry chat model |
| `browser-voice` | Voice + customer UI | HUMAIN authorization |
| `application-phone` | Voice + customer UI | Application ACS; incoming-call event subscription; approved numbers |
| `native-voice` | Voice | Separately connected native ACS and Dynamics voice; no application-owned ACS number required by the runtime |
| `crm` | Assist API | Isolated CRM managed identity and Dataverse setup |
| `knowledge` | Assist API | Foundry IQ/Search and approved indexed or web sources |
| `email` | No new application by itself | ACS Email; a selected chat/voice channel invokes it |
| `agent-console` | Custom representative UI | Adds `web-chat`; not the Dynamics representative desktop |
| `agent-assist` | Assist API/UI | Cosmos, SignalR, its private-network resources and insight models |
| `native-workspace` | No additional Azure app | Adds `native-voice` and `crm`; scoped native forms/scripts/macros |
| `native-knowledge` | No additional Azure app | Adds `native-workspace`; approved canonical and Arabic knowledge |
| `native-copilot` | No additional Azure app | Adds `native-knowledge`; explicit licensing and shared-settings approval |
| `pause-ai` | No additional Azure app | Adds `native-workspace`; separate schema/roles/controller pins and UI acceptance |

The platform foundation is shared: resource group, registry, Container Apps
environment, logging and monitoring. Leaving optional modules out doesn't
remove its cost.

The custom agent-assist stream isn't a Dynamics analytics adapter. Native
sessions don't mirror their conversation into that console; use the selected
native workspace for native calls.

`knowledge` means Azure Foundry IQ retrieval. It is separate from embedded
assistant knowledge and from native Dynamics article publication. An approved
`knowledge.md` provides embedded facts without creating Search.

CRM-only and knowledge-only Assist deployments don't need Cosmos or SignalR.
Inbound application telephony doesn't need CRM or fictional profiles. Outbound
dialing needs its own exact destination policy; approving generated records
doesn't authorize it.

HUMAIN-only phone and voice selections don't provision an unused Foundry
account. Application ACS speech and transcription permissions are added only
when both application telephony and agent-assist are selected.

## 3. Choose or create the Dynamics environment

Skip this section unless `crm`, `native-voice` or a module built on them is
selected. To use an existing environment, read its org URL and run the
voice-channel and pipeline steps below for native voice; don't create another.

### Install the Power Platform CLI

PAC discovers and creates environments here, and publishes the routing agent
in the native sub-workflow. Install the tested version, 2.14.1, as a .NET tool
inside the private workspace. It needs the .NET SDK; Microsoft recommends
.NET 10.

```bash
dotnet tool install Microsoft.PowerApps.CLI.Tool --version 2.14.1 --tool-path "$PRIVATE/tools"
export PATH="$PRIVATE/tools:$PATH"
pac help
```

If .NET itself is installed in a private directory, also set `DOTNET_ROOT` to
that directory.

Create a named auth profile for the target tenant, and select it by name at
the start of each session:

```bash
pac auth create --name "$PAC_PROFILE" --deviceCode
pac auth select --name "$PAC_PROFILE"
pac auth list
```

`pac auth create --managedIdentity` uses the default Azure identity instead. In
a dev container, that's the Azure CLI sign-in from section 1.

### Discover

These commands change nothing:

```bash
pac admin list --json
pac admin list-tenant-settings --settings-file "$PRIVATE/tenant-settings.json"
pac admin list-app-templates --region europe-uk
```

Read the existing environments, the creation policy
(`disableEnvironmentCreationByNonAdminUsers`) and whether
`D365_CustomerService` is offered. `list-app-templates` takes a macro-region
ID such as `europe-uk`; a country name such as `unitedkingdom` is rejected.

A new environment with a database needs at least 1 GB of available Dataverse
database capacity. PAC can't read capacity. If creation fails for capacity, ask
the owner to check Power Platform admin center > Licensing > Capacity add-ons.
Last verified: November 2026.

### Create the environment

**Human stop:** the owner approves the environment's name, type, region,
currency, language and security group.

Create it with the Customer Service app only. Dynamics 365 apps can't be added
to an environment created without them, and `D365_CustomerService` and
`D365_ContactCenter` can't be provisioned in the same create.

```bash
pac admin create --name "$ENVIRONMENT_NAME" --type Sandbox \
  --macro-region europe-uk --currency "$CURRENCY" --language English \
  --domain "$DOMAIN" --templates D365_CustomerService \
  --security-group-id "$SECURITY_GROUP_ID" --max-async-wait-time 90
```

`--macro-region` places the environment in the tenant's datacenter for that
macro-region. `europe-uk`, `eu-efta` and `emea` include Switzerland. With
Advanced Data Residency, use `--region switzerland` instead.

`pac admin create` can exit 1 with a `linkedEnvironmentMetadata` parse error
after creating the environment. Before any retry, run `pac admin list --json`
and look for it. Identify it by domain and creation time, never by display
name alone.

Read back the org URL and the environment ID. The host shows the region:
`crm17.dynamics.com` is Switzerland. Write the org URL to
`dynamics.organizationUrl` in the private profile, and keep the environment ID
with the receipts.

### Provision the voice channel

For `native-voice` only. Read first, then apply:

```bash
"$PYTHON" infra/scripts/contact_center_setup.py voice-channel \
  --tenant-id "$TENANT_ID" --org-url "$ORG_URL" --output-dir "$PRIVATE/voice-channel"
"$PYTHON" infra/scripts/contact_center_setup.py voice-channel \
  --tenant-id "$TENANT_ID" --org-url "$ORG_URL" --output-dir "$PRIVATE/voice-channel" \
  --apply --confirm-organization "$ORG_URL"
```

The first command reads the provisioning settings and prints the one request
it would send. The second sends that request once and polls until voice is
ready; voice-only provisioning takes about 9 minutes. The two Contact Center
actions it calls ship with Dynamics but aren't documented on Learn. If the
request fails, provisioning fails or voice isn't ready after 30 minutes, the
script stops and prints Microsoft's documented path: Copilot Service admin
center > Customer Support > Channels > Manage channels > Voice. Last verified:
November 2026. A rerun with the same output directory only reads back.

Provisioning adds a trial ACS resource and a US toll-free trial number.

### Gate the voice pipeline

Place one call to the trial number, shown in Copilot Service admin center >
Customer Support > Channels > Phone numbers (last verified: November 2026).
Then run:

```bash
"$PYTHON" infra/scripts/contact_center_setup.py pipeline \
  --tenant-id "$TENANT_ID" --org-url "$ORG_URL"
```

Continue only when it reports `Elastic`. On any other value, stop: this
environment won't keep HUMAIN's speech in the saved transcript.

The trial number can only be called from within the US. If nobody there can
call it, run the gate on the first call to the native number instead, before
Stage 8 of the native sub-workflow turns on native media.

## 4. Check telephony eligibility and choose numbers

Skip this section unless `application-phone` or `native-voice` is selected.

Microsoft is retiring standalone ACS capabilities, including phone numbers and
email; see the
[notice in the README](../README.md#notice-azure-communication-services-retirement).
A customer without an ACS resource created before the cutoff can't acquire
numbers. Check before proposing phone modules:

```bash
"$PYTHON" infra/scripts/contact_center_setup.py eligibility \
  --tenant-id "$TENANT_ID" --subscription-id "$SUBSCRIPTION_ID"
```

It prints the billing agreement type, status and sold-to country, and every ACS
resource in the subscription with its creation time. Compare them with the
current
[number eligibility table](https://learn.microsoft.com/en-us/azure/communication-services/concepts/numbers/sub-eligibility-number-capability)
and the
[retirement guide](https://learn.microsoft.com/en-us/azure/communication-services/acs-retirement-and-breaking-changes-guide).
If the subscription isn't eligible, the phone modules can't be deployed. Stop
and tell the owner. Don't substitute Teams Phone, an SBC or an
application-owned call; Teams Phone support in this template is work in
progress.

### Choose and buy numbers

Recommend UK local (geographic) numbers that can make and receive calls. In
past deployments they were the most flexible and reliable choice. Local numbers
in the owner's own country are desirable, but often hard to obtain. A number's
country says nothing about where data is stored.

Use [acs_numbers.py](../infra/scripts/acs_numbers.py) against the ACS resource
that will own the number, once that resource exists: the application ACS
resource after provisioning (section 7) for `application-phone`, or the native
ACS resource in Stage 4 of the native sub-workflow for `native-voice` (see
[native runbook 4.1](dynamics-native-voice/README.md#41-buy-the-native-number)).

```bash
mkdir -p "$PRIVATE/numbers"
"$PYTHON" infra/scripts/acs_numbers.py purchased \
  --endpoint "$ACS_ENDPOINT" --tenant-id "$TENANT_ID"
"$PYTHON" infra/scripts/acs_numbers.py browse \
  --endpoint "$ACS_ENDPOINT" --tenant-id "$TENANT_ID" \
  --output "$PRIVATE/numbers/browse.json"
"$PYTHON" infra/scripts/acs_numbers.py reserve \
  --endpoint "$ACS_ENDPOINT" --tenant-id "$TENANT_ID" \
  --browse-file "$PRIVATE/numbers/browse.json" --number "$CHOSEN_NUMBER" \
  --output-dir "$PRIVATE/numbers/reservation" --apply
"$PYTHON" infra/scripts/acs_numbers.py purchase \
  --endpoint "$ACS_ENDPOINT" --tenant-id "$TENANT_ID" \
  --reservation-dir "$PRIVATE/numbers/reservation"
```

`browse` defaults to GB geographic numbers with calling in and out, application
assignment and no SMS, and lists the easiest to remember first. Show the owner
the candidates. `reserve` costs nothing and checks the readback. Without
approval flags, `purchase` only prints the flags that approve the purchase:
`--confirm-number` and `--confirm-monthly-cost`, repeating the reserved number
and its monthly cost.

**Human stop:** buy only after the owner approves that monthly cost.

Purchase limits apply per Azure tenant. Release numbers you no longer use
before buying more. If a reservation fails with
`MaxAllowedNumbersPerReservationExceeded`, ask Azure Support for a higher
limit. If no UK local number is available, stop and agree an alternative with
the owner.

## 5. Approve content and optional data

### Brand and behavior

The profile controls localized names, colors, a layout starting point, welcome
copy and suggested prompts. Logos may be approved HTTPS URLs or private
PNG/JPEG/WebP files up to 2 MiB. Active SVG/HTML uploads are not accepted.

`systemPromptFile` supplies approved persona instructions. Backend authorization,
identity, recipient, CRM, native ownership and handover guards remain mandatory;
a persona can't switch them off.

The supplied layouts are starting points, not a limit on agent-assisted design.
If the brief needs a different composition, make that change in the private
prepared frontend, build it, review screenshots and keep the exact source and
test evidence. Changing a name isn't a custom design.

The UI and runtime support English and Arabic. The native workspace recipe is
bilingual. Another language needs an implemented and tested extension, not just
a new locale string.

### Approved knowledge

The repository ships an unconfigured document and an empty corpus.

For embedded knowledge, provide an approved Markdown file with:

- Heading `# Organization service knowledge`.
- A unique `Revision:` value.
- Numbered `### 01. ...` topics.
- Citations such as `[S01]` in the topics.
- A `## Source registry` heading after the last topic, followed by one
  `[S01]: https://... "Title"` line per source. Without that heading, the
  registry becomes part of the last topic; see the
  [knowledge input format](dynamics-native-voice/workspace-setup.md#knowledge-input-format).
- At most 64 KiB of UTF-8 content.

Use only authorized source material. Summarize relevant facts and keep source
attribution; don't copy private customer records or secrets into knowledge.
This file is shared with the customer-facing assistants, so use customer-safe
facts only. Representative-only procedures belong in the scoped agent scripts
or a separately reviewed native-only extension.

If a source website blocks retrieval, for example with HTTP 403, ask the owner
for an approved export or a saved copy. Report it as a blocked source,
separately from having no approved knowledge, and don't swap in another source.

For `knowledge`, provide approved source domains and either:

- `indexed`: a nonempty private corpus; or
- `web`: domain-restricted retrieval, optionally supplemented by that corpus.

Each corpus document contains exactly `id`, `title`, `content`, `sourceUrl`,
`approvalStatus`, `language` and `retrievedAt`. Approval must be explicit,
source URLs must match the domain policy, and IDs must be unique.

For `native-knowledge`, also provide `knowledgeArabicFile` with the same
revision, topic IDs and citations, plus `Canonical-SHA256:` of the exact
canonical file. There is no fixed industry taxonomy, topic count or bundled
business package.

### Optional fictional data

Default: no business records. CRM schema and identity setup work without a
seed.

If the owner wants records, the agent generates a small, coherent dataset
**privately** from the approved scenario. Use plausible names and descriptions,
not labels such as "Demo Customer 001". Don't impersonate real customers or
invent service facts.

Preview the proposed account, contacts, cases and updates with the owner. Set
`demo.approved` only after that review. The
[CRM setup tool](../infra/scripts/setup_dynamics_crm.py) enforces the schema;
the [seed example](dynamics-native-voice/examples/crm-seed.example.json) is
deliberately empty.

Private profile hints use `id`, `display_name`, `display_name_ar`, `phone`,
`email` and `context`. Each must map to an approved seed contact. They select a
customer context for the demonstration; they don't verify identity and aren't
the CRM's source of truth.

Keep synthetic provenance in the private ledger and technical metadata.
Generated business descriptions stay natural.

### Presenter mode

Offer the owner two modes:

- **Reserved only.** Every fictional contact uses reserved, non-routable phone
  numbers and email addresses. Nothing can reach a real person.
- **With a presenter.** The fictional contacts plus one contact that carries
  the presenter's own phone number and email, entered privately.

A presenter contact has four separate parts:

1. The Dataverse contact, holding the presenter's phone and email.
2. A private profile hint, so web chat can start as that customer.
3. Caller context: on a phone call, the number Dynamics reports for the caller
   matches the contact, so the assistant recognizes the presenter.
4. Delivery approval: the email address in `emailPolicy.allowedRecipients`,
   and for outbound calls the number in `phonePolicy.allowedOutboundNumbers`
   with `approvals.outboundDestinations`.

It isn't a Dynamics login or license. For example, the presenter calls, the
assistant greets them by name, and "send me an email" goes to their approved
address.

Data approval, delivery approval and call consent stay separate. Never test a
plausible generated number by dialing it. Every charged or recorded test call
needs fresh consent and caller readiness.

## 6. Prepare the selected deployment

Record content and module approval in the private profile, fill in the
verified target, then prepare a new destination:

```bash
"$PYTHON" infra/scripts/template_setup.py prepare \
  --profile "$PRIVATE/profile.json" --output-dir "$PRIVATE/workspace"
cd "$PRIVATE/workspace"
export REPO_ROOT="$PWD"
```

The command:

1. Validates content and data before creating the destination.
2. Checks neutral source defaults, then copies reviewed source without Git
   history, existing Azure environments or dependency and build directories.
3. Snapshots approved private inputs and renders public UI and private runtime
   configuration separately.
4. Selects only the required services in the private `azure.yaml`.
5. Generates an ingest credential only when the shared API needs it.
6. Writes owner-only selection, preparation and provenance records.

It doesn't create Azure resources, buy numbers, seed Dataverse, call HUMAIN or
place calls. Public UI configuration contains no prompts, full knowledge,
delivery lists, credential values or customer records.

Use a reviewed commit. To include legitimate uncommitted source changes,
inspect them first and pass `--include-reviewed-worktree`. Never reset
unrelated work or silently include arbitrary untracked files.

### Additional tools

`additionalTools` lists requirements (`name` and `purpose`), which are
not executable integrations. Preparation is allowed, but packaging and
deployment stay blocked until an implementation, tests and review exist.

The agent must:

1. Establish the API contract, authentication, permissions, data policy and
   side effects. Stop if access or scope is unclear.
2. Implement the adapter and register it in the selected existing tool
   registries. Keep credentials server-side. Don't add a generic arbitrary-URL
   executor.
3. Test registry exposure, authorization, valid and invalid inputs, failure
   handling, idempotency where needed, and disabled-module behavior.
4. Get approval for the actual integration and its side effects.
5. Save an owner-only `extension-review.json` in the private workspace with
   `approved`, `approvalReference`, the exact `toolNames`, `sourceFiles`
   path/SHA256 pairs and separate `testEvidence` path/SHA256 pairs.

Those paths are inside the private workspace. Record implementation **and
registry** files, plus actual passing test output. The gate checks coverage and
that the evidence is unchanged; it doesn't prove that an adapter works or
replace human review. Changing a reviewed file invalidates the gate.

## 7. Authenticate, preview, approve, deploy

**Human stop:** the owner approves the tenant and subscription, regions,
quotas, licenses, charges, data handling and any reuse of existing resources.

Native voice and the managed-identity CRM recipe need the Azure subscription
and the Dynamics environment in the same Microsoft Entra tenant. For telephony,
finish [section 4](#4-check-telephony-eligibility-and-choose-numbers) first;
another tenant's existing deployment says nothing about this tenant's ability
to buy numbers.

Create a **new** azd environment with the name in the profile; don't reuse
another deployment's environment. Then point the hooks at the deployment
Python, so they work from any shell:

```bash
azd env new "$ENVIRONMENT" --subscription "$SUBSCRIPTION_ID" --location "$LOCATION"
azd env set PYTHON "$PYTHON" --environment "$ENVIRONMENT"
"$PYTHON" infra/scripts/template_setup.py configure
"$PYTHON" infra/scripts/template_setup.py preview
```

Take these values from the approved private profile, not from another
deployment's handoff. `configure` imports the private selection without shell
evaluation and without putting secret values in process arguments. It refuses
an apparent existing deployment and keeps runtime overrides on a workspace
that is already configured.

Ask HUMAIN for the Authorization value issued for this deployment, and agree
who rotates it. Check the
[live contract](https://humain-voice-agent.share.sanad.ink/docs/connect.html)
for the current endpoints and request shape. The owner enters the value through
the private editor or secret store, never chat or shell history: as
`HUMAIN_AUTHORIZATION` in the private selection before `configure`, or in the
named azd environment afterwards. Entering the credential doesn't approve
HUMAIN's data handling; that's the owner's separate decision (working rule 8).
For a demonstration, the owner can record a demo-only acceptance that lists
the open questions, and you don't ask again.

The accepted defaults are:

```dotenv
HUMAIN_LLM_PROVIDER=allam
HUMAIN_TURN_TAKING_STRATEGY=fast_vad
HUMAIN_INTERRUPTION_POLICY_MODE=off
```

For `application-phone`, provisioning creates the application ACS resource but
buys no number. Buy it afterwards (section 4) and push it into the running app
(follow-on phases below). It's separate from the Dynamics-owned number and
isn't needed for native-only voice.

`preview` runs `azd provision --preview` and keeps its output privately. It
doesn't record approval. Review costs, changes, regions, quotas and resource
reuse together with the `scope` output. After the owner decides, record
`approvals.azurePreview` and, where needed, `approvals.serviceEligibility`.

```bash
"$PYTHON" infra/scripts/template_setup.py gate --phase apply
azd provision --environment "$ENVIRONMENT"
azd deploy --environment "$ENVIRONMENT"
```

For a resource-only selection such as standalone email, skip `azd deploy`. The
hooks block unprepared, mismatched or unapproved runs. Don't set the internal
read-only-preview bypass for a write command.

If a new Foundry account's model deployment fails with `RequestConflict`, run
`azd provision` again in the same azd environment. Don't rename or delete
anything; azd's summary may blame a name or soft-delete conflict, which is
misleading here. The template deploys its models one at a time to avoid this.

The profile's primary region sets the application, Search and Cosmos regions.
If service availability needs an override, get explicit region approval and
keep the exact azd parameter snapshot before provisioning.
`azure.communicationsDataLocation` (`ACS_DATA_LOCATION`) sets the data location
of the application ACS and Email resources; the default is Europe. Native ACS
is created separately with data location Switzerland, subject to the native
runbook's
[Elastic gate](dynamics-native-voice/README.md#region-the-elastic-voice-pipeline).
A valid Azure region name doesn't mean communications services are available
there.

### Selected follow-on phases

- **Application phone:** buy the number on the application ACS resource
  ([section 4](#choose-and-buy-numbers)), then push it into the running voice
  app. Changing the azd value alone doesn't reach the running app.

  ```bash
  "$PYTHON" infra/scripts/native_runtime_settings.py \
    --subscription-id "$SUBSCRIPTION_ID" --resource-group "$RESOURCE_GROUP" \
    --set ACS_SOURCE_NUMBER="$APP_NUMBER" --output-dir "$PRIVATE/app-number" \
    --azd-environment "$ENVIRONMENT"
  ```

  Review the plan, then rerun with `--apply --confirm-app ca-voice`. It changes
  only the voice app, creates one revision, reads it back and prints the
  `azd env set` command that keeps the value for the next provision; see
  [native runtime settings](dynamics-native-voice/operations.md#native-runtime-settings).
  Then connect incoming calls with
  [connect_phone_events.py](../infra/scripts/connect_phone_events.py). It
  serves only the application ACS resource; never point it at the
  Dynamics-owned number.
- **CRM:** apply the reviewed [CRM recipe](dynamics-native-voice/crm-setup.md).
  Omit `--seed` for schema-only setup. Writing generated data also needs
  `--approve-seed`. With `native-voice`, first create the
  [shell queue and voice workstream](dynamics-native-voice/README.md#51-create-the-minimal-shell-one-queue-and-one-voice-workstream):
  native-mode CRM needs the workstream ID. A new Customer Service environment
  already contains Microsoft's workstreams, queues and bots; never adopt one by
  name.
- **Native voice:** continue with the
  [native sub-workflow](dynamics-native-voice/deploy-with-copilot.md). It covers
  the dedicated ACS connection, event delivery, the routing agent, identities,
  topic publication, the caller gate and the observer.
- **Native workspace, knowledge and Copilot:** copy the selected booleans from
  the private `native-options.json` into the reviewed
  [workspace configuration](dynamics-native-voice/examples/workspace.example.json)
  and follow the [workspace recipe](dynamics-native-voice/workspace-setup.md).
  Its `representative` stage sets up the first representative; the Copilot
  stage waits up to `--copilot-wait-minutes` (default 60) for native Copilot
  readiness. Skip unselected content and features, and don't turn on shared
  Copilot settings by default.
- **Pause AI:** follow the separate
  [supervision recipe](dynamics-native-voice/supervision-setup.md), using the
  real base receipts and the deployed-identity preflight.

Don't enable native media or Pause AI before their prerequisites verify. When
changing HUMAIN settings, check the live contract and verify the requested and
effective room metadata.

## Resume without replaying writes

Run this from the prepared workspace:

```bash
"$PYTHON" infra/scripts/template_setup.py status
"$PYTHON" infra/scripts/template_setup.py record \
  --stage azure-preview --status approved \
  --evidence "$PRIVATE/approved-preview-evidence.json" \
  --approval-reference "$APPROVAL_REFERENCE" \
  --note "Approved the reviewed Azure scope."
```

Create that evidence from the actual review; don't fabricate it to satisfy a
gate. For `verified` records, attach the real readback or test evidence. A
blocked record describes what is missing. Evidence is owner-only and hash
checked; large media belongs in private storage with a small receipt.

These are coordination records, **not replacements for component ownership
journals**. On resume, inspect current state and the original component
receipts. Use that component's recovery operation for an uncertain write; never
adopt a record by display name or create a replacement ID to hide uncertainty.

Prepared inputs and rendered configurations are pinned. Changing them needs a
new reviewed preparation. If the target has already been deployed, stop and
plan a scoped upgrade that keeps the original environment and receipts; a fresh
preparation isn't authority to overwrite it.

## Verification and handoff

Run only the checks relevant to the selected modules:

```bash
python -m unittest discover -s infra/tests -p 'test_template_setup.py'
python -m unittest discover -s infra/tests -p 'test_embedded_knowledge.py'
az bicep build --file infra/main.bicep --outfile "$PRIVATE/main.template.json"
TEMPLATE_COMPILED_ARM="$PRIVATE/main.template.json" \
  python -m unittest discover -s infra/tests -p 'test_template_infrastructure.py'
```

From the neutral source checkout, also run
`python infra/scripts/check_template.py`. It reports customer-specific
defaults, owner files, identifiers, phone numbers, email addresses, host names,
secrets and private terms in tracked files. It doesn't replace a secret scanner.

For changed applications, use their existing Python unittest, npm test,
typecheck and build commands. Don't start provider sessions or place calls as
part of a build or test. Additional tools need their own integration tests with
mocked transports before any approved real side effects.

Check the customer UI on a phone-sized screen without live voice requests:

```bash
cd apps/chat/ui_v2
npx playwright test tests/browser/ui-evidence.spec.ts --output "$PRIVATE/ui-evidence"
```

It fails if the voice orb and the Start button aren't fully visible together
at 390×844 or 1440×1000, in English and Arabic. It saves `mobile-voice.png`,
`mobile-voice-listening.png` and `mobile-chat.png` after the animations settle.

Verify **both selected and unselected behavior**:

- Only selected optional resources and services are deployed.
- Disabled features expose no tools or hidden UI requests.
- Public assets and configuration contain no private inputs.
- CRM-only works without native voice or agent-assist storage.
- Inbound application telephony works without fictional contacts.
- Generated contacts can't authorize outbound calls or email on their own.
- Knowledge sources, case categories, layouts, languages and branding match the
  approved brief.
- Runtime permissions are verified as the deployed identity, not only as an
  administrator.

For native voice, follow the
[acceptance checklist](dynamics-native-voice/acceptance-checklist.md). Allow at
least **15 minutes** after the native workstream configuration readback before
caller acceptance, and restart the wait after any further change. Get fresh
consent for every charged or recorded test.

Report each component as connected, configured or accepted on a real call.
Keep configuration readback, automated tests, UI checks and caller acceptance
separate. State the limits: one AI call at a time, a 20-minute AI phase, and
the recording notice 5-7 seconds after the call connects, which is work in
progress and on hold. A healthy endpoint doesn't prove audio, handover or
native Copilot answers.

Hand over the source revision and hashes, the private profile and workspace
location, selected modules, built image references, resource and native IDs,
component receipts, secret-store locations without values, acceptance
evidence, pending items and scoped rollback instructions. Don't commit the
generated customer environment back into this template.
