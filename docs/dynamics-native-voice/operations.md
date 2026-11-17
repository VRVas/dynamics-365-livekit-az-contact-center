# Native voice: acceptance and operations

[Deployment tutorial](README.md) | [Reference](reference.md) | [Troubleshooting](troubleshooting.md)

For the current CRM, bilingual workspace and Pause AI package, start with the
[comprehensive acceptance checklist](acceptance-checklist.md). It separates
short-call tests, offline checks, known gaps and gated engineering scenarios.

Use the same private `WORK`, `ENV` and selected IDs as the deployment tutorial.
All commands below are run from the repository root with the setup virtual
environment active. Never paste actual control keys into these examples.

## Native event delivery

Dynamics answers native calls only after Event Grid delivers
`Microsoft.Communication.IncomingCall` and
`Microsoft.Communication.RecordingFileStatusUpdated` to its two webhooks with
Microsoft Entra authentication. Until then, Manage telephony shows "Your Event
Grid is unconfigured for incoming calls" and "Your Event Grid is unconfigured
for call recording". [native_event_delivery.py](../../infra/scripts/native_event_delivery.py)
scripts this with Microsoft Graph REST calls and Azure CLI. It needs no Graph
PowerShell, browser automation or secrets.

| Command | When to run it |
|---|---|
| `bootstrap` | Before connecting the native ACS resource in Dynamics, which asks for the app ID |
| `subscriptions` | Right after the native ACS connection and number sync |
| `verify` | After `subscriptions`, and again before any call |

Every write is journaled in the private `--output-dir` before it is sent, and
read back afterwards. Rerun the same command after an interruption: confirmed
steps are skipped, and an unconfirmed step is never resent automatically. After
fixing the reported cause, `--retry-step <step>` resends that one step once. A
second failure stops for a human decision.

### 1. Create the delivery app

Sign in to Azure CLI, in the Dynamics tenant, as the user who will create the
event subscriptions. That user needs an Entra role that can create applications
and assign app roles, such as Cloud Application Administrator.

```bash
python infra/scripts/native_event_delivery.py bootstrap \
  --tenant-id "$TENANT_ID" --output-dir "$WORK/event-delivery-app"

# After reviewing the plan:
python infra/scripts/native_event_delivery.py bootstrap \
  --tenant-id "$TENANT_ID" --output-dir "$WORK/event-delivery-app" \
  --apply --confirm-tenant "$TENANT_ID"
```

The plan stops unless the tenant's Microsoft.EventGrid service principal is
found exactly once, by both its application ID and its name. Apply creates only:

- a single-tenant application, `Native Voice Events` by default, with one app
  role, `AzureEventGridSecureWebhookSubscriber`, for applications and users;
- its service principal;
- the signed-in user as application owner;
- app-role assignments for Microsoft.EventGrid and for the signed-in user.

It adds no secret, API permission or consent. The readback checks owners, app
roles, `appRoleAssignedTo`, the principal's own app roles and delegated grants,
and fails on anything unexpected. Record `delivery_app_id` as `EVENTGRID_APP_ID`.
In Dynamics it is the **Event grid app id**; `TENANT_ID` is the **Event grid app
tenant id**.

The tool performs Microsoft's [secure webhook delivery with a Microsoft Entra user](https://learn.microsoft.com/en-us/azure/event-grid/secure-webhook-delivery)
procedure with Graph [application upsert](https://learn.microsoft.com/en-us/graph/api/application-upsert),
[owner](https://learn.microsoft.com/en-us/graph/api/application-post-owners) and
[app-role assignment](https://learn.microsoft.com/en-us/graph/api/serviceprincipal-post-approleassignedto)
requests instead of Graph PowerShell. It defaults to the public-cloud
Microsoft.EventGrid application ID from earlier versions of Microsoft's script;
current guidance reads the ID from the tenant. If the plan reports another
application ID for the tenant's Microsoft.EventGrid principal, for example in
another cloud, rerun the plan with `--event-grid-app-id` set to it.

### 2. Copy the Dynamics values privately

After the connection and number sync, open **Copilot Service admin center ->
Channels -> Phone numbers -> Advanced -> Manage Azure Communication Services**.
Copy these values exactly into `$WORK/dynamics-webhooks.env`, then run
`chmod 600` on it. Never construct the URLs.

```text
INCOMING_CALL_WEBHOOK_URL=<Incoming call Web Hook Endpoint>
RECORDING_WEBHOOK_URL=<Recording Web Hook Endpoint>
EVENT_GRID_APP_ID=<Event grid app id>
EVENT_GRID_APP_TENANT_ID=<Event grid app tenant id>
```

Last verified: November 2026. If the screen differs, ask the owner for a
screenshot that includes the left navigation, with no secrets showing.

The tool refuses a file others can read, URLs on `*.azurecontainerapps.io`, and
an app or tenant ID that differs from `--delivery-app-id` or `--tenant-id`.
Output and journals hold only SHA-256 fingerprints of the URLs.

### 3. Create the topic and both subscriptions

```bash
python infra/scripts/native_event_delivery.py subscriptions \
  --tenant-id "$TENANT_ID" --native-acs-resource-id "$NATIVE_ACS_ID" \
  --system-topic-name "$SYSTEM_TOPIC" --delivery-app-id "$EVENTGRID_APP_ID" \
  --endpoints-file "$WORK/dynamics-webhooks.env" \
  --output-dir "$WORK/event-delivery-subscriptions"

# After reviewing the plan:
python infra/scripts/native_event_delivery.py subscriptions \
  --tenant-id "$TENANT_ID" --native-acs-resource-id "$NATIVE_ACS_ID" \
  --system-topic-name "$SYSTEM_TOPIC" --delivery-app-id "$EVENTGRID_APP_ID" \
  --endpoints-file "$WORK/dynamics-webhooks.env" \
  --output-dir "$WORK/event-delivery-subscriptions" \
  --apply --confirm-acs-name "$NATIVE_ACS_NAME"
```

It creates each item, or reuses it when it already matches exactly:

| Item | Configuration |
|---|---|
| System topic | One per ACS resource; Event Grid [allows one system topic per source](https://learn.microsoft.com/en-us/azure/event-grid/system-topics). If the resource already has one, the plan names it; pass that name as `--system-topic-name` |
| `native-dynamics-incoming` | `Microsoft.Communication.IncomingCall` to the incoming URL, advanced filter `data.to.kind` StringIn `phoneNumber` |
| `native-dynamics-recordings` | `Microsoft.Communication.RecordingFileStatusUpdated` to the recording URL, no filter |
| Both subscriptions | WebHook, Event Grid schema, Entra tenant `TENANT_ID` and application `EVENTGRID_APP_ID`, no expiry |

The filter stops Dynamics from answering the VoIP invitation that the
application sends to HUMAIN's ACS user. The plan stops when any other
subscription on the topic receives either event type, except the passive
observer `moacc-native-handover-probe`. It never overwrites or deletes a
subscription.

Event Grid validates each webhook while creating it, and Microsoft notes that
the Entra settings can take [up to 15 minutes to synchronize](https://learn.microsoft.com/en-us/dynamics365/customer-service/administer/voice-channel-configure-services).
If creation fails, for example with "Webhook validation handshake failed", the
tool reads back, records the error and stops. Check the app ID, tenant and both
role assignments, wait, then rerun once with
`--retry-step incoming-subscription` or `--retry-step recording-subscription`.
Don't remove Entra authentication to get past the handshake.

### 4. Verify, then close the gate

```bash
python infra/scripts/native_event_delivery.py verify \
  --tenant-id "$TENANT_ID" --native-acs-resource-id "$NATIVE_ACS_ID" \
  --system-topic-name "$SYSTEM_TOPIC" --delivery-app-id "$EVENTGRID_APP_ID" \
  --endpoints-file "$WORK/dynamics-webhooks.env" \
  --receipt-file "$WORK/event-delivery-verify-01.json"
```

`verify` writes nothing in Azure or Entra. It passes only when the topic and
both subscriptions are `Succeeded`, their full endpoints match the copied URLs,
filters and Entra settings are exact, no other route receives either event, and
the delivery app has its role with Microsoft.EventGrid assigned.

**Exit gate before any call:** refresh **Copilot Service admin center ->
Channels -> Phone numbers -> Manage** (Manage telephony). Both "Your Event Grid
is unconfigured..." banners, for incoming calls and for call recording, must be
gone. Last verified: November 2026. If the screen differs, ask the owner for a
screenshot that includes the left navigation, with no secrets showing.

## Native runtime settings

[native_runtime_settings.py](../../infra/scripts/native_runtime_settings.py)
changes allowlisted runtime settings on the running voice app only. A root
`azd provision` would preview changes to every app, the Container Apps
environment and Foundry metadata. Use it:

- for native-only changes after deployment: observer lease or permanent mode,
  fast start, an additional workstream or a HUMAIN behaviour setting;
- after buying an application number: set `ACS_SOURCE_NUMBER` before
  application telephony is used. Provisioning rendered the old value, and
  `azd env set` alone doesn't change the running app.

Allowed keys are `DYNAMICS_ORG_URL`, `ACS_SOURCE_NUMBER`,
`ACS_AUTO_CONFERENCE_ENABLED`, `DYNAMICS_OUTBOUND_ENABLED`, the non-secret
`DYNAMICS_NATIVE_MEDIA_*` and `DYNAMICS_NATIVE_PROBE_*` settings from
`runtime-env`, and the non-secret `HUMAIN_*` settings from `bootstrap-env`. The
tool refuses any other key, every secret (API keys, connection strings,
`HUMAIN_AUTHORIZATION`) and any variable the app holds as a secret reference.
Change secrets with `azd provision` so they stay Container Apps secrets.

```bash
python infra/scripts/native_runtime_settings.py \
  --subscription-id "$SUBSCRIPTION_ID" --resource-group "$APP_RG" \
  --set "ACS_SOURCE_NUMBER=$APP_NUMBER" \
  --azd-environment "$ENV" --output-dir "$WORK/runtime-settings-01"

# After reviewing the diff, while the line is idle:
python infra/scripts/native_runtime_settings.py \
  --subscription-id "$SUBSCRIPTION_ID" --resource-group "$APP_RG" \
  --set "ACS_SOURCE_NUMBER=$APP_NUMBER" \
  --azd-environment "$ENV" --output-dir "$WORK/runtime-settings-01" \
  --apply --confirm-app ca-voice
```

The plan reads the app with `az containerapp show`, shows each change with
credential-like values masked, checks related settings together and prints the
`azd env set` commands. Apply sends one
[`az containerapp update --set-env-vars`](https://learn.microsoft.com/en-us/cli/azure/containerapp#az-containerapp-update)
with a named revision suffix. It never builds a PATCH from the GET output; a
hand-built PATCH that copied read-only fields such as `imageType` was rejected
with HTTP 400. Apply passes only when:

- exactly one new revision exists and is the latest, provisioned and healthy;
- the image, secrets, resources and scale are unchanged;
- only the planned variables changed, on the app and in the new revision;
- every other container app in the resource group kept its latest revision.

Then run the printed `azd env set` commands so a later `azd provision` keeps
the values. If the update fails, the tool reads back and stops without a second
update. Fix the cause and plan once more in a new directory. Rerunning with the
same directory only re-verifies.

## Native reader runtime check

Run [check_native_reader_runtime.py](../../infra/scripts/check_native_reader_runtime.py)
after the native reader application user exists and the voice app is deployed
with `DYNAMICS_ORG_URL`. Run it again after changing that identity
or its role. It proves which Dataverse identity the running container uses. A
check from the deployer's machine can't: `WhoAmI` with an administrator token
and `CallerObjectId` impersonation returned the administrator, not the app user.

```bash
python infra/scripts/check_native_reader_runtime.py \
  --subscription-id "$SUBSCRIPTION_ID" --resource-group "$APP_RG" \
  --expected-runtime-user "$READER_USER_ID" \
  --expected-business-unit "$READER_BUSINESS_UNIT_ID" \
  --receipt-file "$WORK/native-reader-runtime-01.json"
```

`READER_USER_ID` is the application user's `systemuserid`, not the managed
identity's client or object ID. `READER_BUSINESS_UNIT_ID` is the
business unit chosen for that user.

The tool opens `az containerapp exec` into the `voice` container, so run it from
a Linux, macOS or WSL terminal. Inside, a read-only Python probe gets a
Dataverse token with `ManagedIdentityCredential` and the container's
`AZURE_CLIENT_ID`, the same identity the voice app's Dataverse client uses. The
check passes only when:

- the token belongs to `AZURE_CLIENT_ID`, and `WhoAmI` returns the expected
  application user and business unit;
- that user is enabled and its application ID is `AZURE_CLIENT_ID`;
- reads of the native phone tables `msdyn_ocphonenumbers` and
  `msdyn_ocphonecallengagementctxes` succeed;
- a read of `roles` is denied with 403 `prvReadRole` for that user.

Anything else fails closed. The owner-only receipt keeps the observations and
the terminal transcript, never the token. The probe writes nothing and places
no call.

## No-call checks

Run the published configuration check:

```bash
python infra/scripts/native_voice_setup.py verify \
  --tenant-id "$TENANT_ID" --org-url "$ORG_URL" \
  --bot-id "$BOT_ID" --workstream-id "$WORKSTREAM_ID" \
  --voice-origin "$VOICE_ORIGIN" --secrets-file "$WORK/control-keys.env"
```

This makes authenticated readiness calls. It can warm an unused software
browser page, but it does not create a customer conversation, invite a
participant or dial a number.

It also requires the agent's saved Basic-voice initialization. Telephony
channel registration, callable topics and healthy media alone are insufficient:
an unsaved **Enable voice** toggle can leave the agent in the wrong modality.
Use the supported Voice settings, save (in some UI versions under **Phone
setup -> Select voice**) and publish; do not manufacture readiness by writing
setup flags directly. Recheck real live and saved AI speech after a correction.

Also verify:

- The Azure subscription and Dynamics browser `WhoAmI` belong to the expected
  tenant/user.
- The native observer is enabled; any configured lease covers the planned test.
- Both native answering and recording subscriptions still target Dynamics, and
  native incoming delivery still filters `data.to.kind = phoneNumber`:
  [`native_event_delivery.py verify`](#native-event-delivery) checks both.
- Only the passive observer targets `/dynamics/handover/probe/events`.
- The container still runs as the native reader application user:
  [native reader runtime check](#native-reader-runtime-check).
- Representative queue membership, bookable resource and inbound capacity match.
- The standard **Escalate** topic contains a native **Transfer to agent**, not
  the starter "not currently configured" message. Check the standard Fallback
  topic's redirect too; the custom controller's transfer does not validate
  those separate paths.
- The workstream's AI-agent failure action is explicitly configured. The
  accepted baseline uses **Wait Music and Escalate**, not an empty binding.

The command above checks the custom controller contract; retain separate
readback for these system-topic and workstream failure paths. A rejected
Copilot Studio web-test session is not a phone-readiness test: the caller gate
can deliberately reject it because it lacks the approved native caller context.
Keep that guard and use accurate rejection wording. Do not copy a previous
agent's external-phone fallback merely to reproduce its test-pane result.
- `DYNAMICS_OUTBOUND_ENABLED=false` and `ACS_AUTO_CONFERENCE_ENABLED=false`.

### Optional closed-call contract check

Use this only with a **real, verified Closed** conversation from this environment.
Never use an active conversation, a guessed UUID, the most recent call, or another
tenant's example.

The expected sequence with the dedicated media key is:

1. `POST /dynamics/native/sessions` using the closed conversation ID, language
   `ar`, `retain_native_controller: true`, `speech_turn_control: true`.
2. The HTTP 202 acknowledges the background request. Audio must remain stopped.
3. `GET /dynamics/native/sessions/{id}?wait=true` returns an explicit stopped
   result, typically `reason: "native_call_not_active"`.
4. Customer/AI frame counters remain zero and provider readiness remains false.

This confirms the refusal path. It is not a positive media/recording test.
Negative authentication checks should return 401 without the correct key.

## Controlled live acceptance

### A. Arm before asking anyone to dial

1. Obtain informed recording/transcription consent from the participants and
   confirm the test phone's international/per-minute charges.
2. Open the representative workspace in one browser session. Use a headset.
3. Set the representative Available and confirm one free voice inbound slot.
4. Close any previous active call **and its wrap-up session**.
5. Agree a maximum **90 seconds from dialing**, with immediate hangup on error.
6. Start the monitor:

```bash
python infra/scripts/watch_native_voice.py \
  --tenant-id "$TENANT_ID" --org-url "$ORG_URL" \
  --voice-origin "$VOICE_ORIGIN" \
  --workstream-id "$WORKSTREAM_ID" --queue-id "$QUEUE_ID" \
  --representative-id "$REPRESENTATIVE_ID" \
  --secrets-file "$WORK/control-keys.env" \
  --output "$WORK/acceptance-01.json" --arm
```

Wait for `ARMED` output. Stop the test if the observer has expired, the line is
not idle or a readiness check fails.

The monitor waits up to 210 seconds for the test and stops observing a call after
about 95 seconds. The caller's hangup limit remains 90 seconds from dialing.
The monitor cannot dial or hang up.

For a separately agreed short startup-only test, use
`--caller-limit-seconds 60` and hang up within 60 seconds from dialing.
This does not change the default 90-second limit or provide automatic hangup.
Do not silently expand the agreed caller allowance to fit the monitoring tool.

### B. One call, short turns

Call `DYNAMICS_NUMBER` from the explicitly allowed `TEST_CALLER`:

1. Hear the setup announcement and Arabic HUMAIN greeting.
2. Ask for English and a brief answer.
3. Ask for Arabic and a brief answer.
4. Return to English before the human portion if the representative will speak
   English.
5. While HUMAIN is answering, request a human **once**.
6. Accept the notification in Dynamics.
7. Speak one short sentence from the caller's phone.
8. Speak another from the representative headset.
9. **Before ending the call**, inspect the native transcript.
10. End the call within the agreed limit.

Do not redial in the same monitor window. It refuses multiple conversations
because it cannot identify which one you intended. Stop, verify cleanup, and start a new
monitor/report for another authorized attempt.

### C. Record these pass/fail results

| Requirement | Evidence required | Insufficient proxy |
|---|---|---|
| AI starts automatically | Caller hears HUMAIN without a representative accepting first | Backend enabled |
| Two-way audio | Caller request produces a relevant HUMAIN response, no duplicated voice | Frame counters alone |
| Arabic/English | Actual native transcript contains both scripts/languages after explicit switches | Copilot variable changed |
| Native language alignment | Dynamics reports dialog/transcription change; native words match spoken language | Export `Locale` field alone |
| Speaker separation | Caller number, `Acs_Unknown` and representative name / `You` stay distinct | Raw `Customer said` labels |
| Live availability | AI history visible while the human call is still open | Final transcript only |
| Handover on first request | One request, safe AI departure, native transfer notification | Tool returns success |
| Same conversation | Native parent conversation ID unchanged before/after transfer | Two linked call records |
| Human connection | Actual representative acceptance and two-way human audio | Queue assignment only |
| Recording | Listen to native recording; it includes AI and expected human/caller portions | Recording row exists |
| Persistence | Closed call retains readable transcript and recording | Wrap-up view before completion |
| Cleanup | Own microphone/control leg gone; no live call/worker/identity leak | CLI exit code 0 |

Accept `Acs_Unknown` temporarily only if it stays distinct from the caller and
representative. It identifies the ACS user, not a new AI telephone number.
The raw transcript may classify both PSTN and ACS external speakers as Customer.
Do not infer correct AI/customer analytics classification from speaker headers.

### D. Finish wrap-up

After hanging up, close the **phone session tab inside Dynamics** using its small
`x`. Do not close only the browser tab.

Verify the conversation reaches **Closed**, not merely Wrap-up. Its final
transcript/insights may be produced asynchronously. In the completed record's
Details area, inspect the native transcript and recording. Avoid repeatedly
starting new calls while waiting for post-call files.

The representative may need permissions to view recordings/transcripts distinct
from participation in the queue.

## Evidence pack

The opening must greet the caller and wait, not treat prefetched CRM context as
a spoken request. The provider adapter sends the approved greeting through
`intro_message` with an explicit instruction to speak it once, then wait.
Keep that text, tools and language policy separate from the caller's first
turn. Test actual opening behavior: request acceptance or an effective model
setting alone does not establish what the caller heard. A provider-only
synthetic check does not prove native transcript, phone audio or startup latency.

Keep a private, per-test record containing:

- Test date/time with **UTC** and the UI time-zone offset.
- Native parent conversation ID and workstream/queue IDs.
- Copilot trace ID and final outcome.
- Native child session creation, assignment and acceptance times.
- Native recording, transcript and insight record IDs, when generated.
- Read-only monitor report.
- Voice `/timings` result if measuring latency.
- Screenshots of **visible speaker headers**, not just plain text.
- Human observations: first speech, audio quality, handover wait, live visibility.
- Errors/limits, including failed runs; never replace failed evidence with a
  later successful result.

The monitor records call-control events. Assess audio quality and recognition
separately. Its `control_proof_observed` can be false if the test ran
beyond its observation limit even though a later handover succeeded. Add a
separate timestamped note; do not rewrite the original report.

## Dynamics customer context and cases

Voice and text chat use the same authenticated `/api/cases` service. Its
customer and case operations use Dataverse only. Agent-assist keeps its own
conversation, transcript and insight storage.

### Service configuration and access

The shared service requires:

| Setting | Purpose |
|---|---|
| `CRM_DATAVERSE_ORG_URL` | HTTPS Dataverse organization origin |
| `CRM_CLIENT_ID` | Dedicated CRM managed identity attached to the shared service |
| `CRM_NATIVE_WORKSTREAM_IDS` | Comma-separated native workstream UUIDs eligible for customer/case linking |

Keep `AZURE_CLIENT_ID` unchanged for the service's existing Cosmos and other
Azure access. Register the CRM identity as its own Dataverse application user.
Its role needs customer/account reads, case creation/reads/approved writes,
customer-update note creation/reads and the necessary Append/Append To
privileges. Native association also requires the relevant conversation and
phone-context reads, conversation writes and organization-level
`prvReadPluginAssembly`. The native conversation update pipeline reads
`Microsoft.Dynamics.OmnichannelPrime.Plugins` under the application user.
Missing assembly-read permission can surface as HTTP 400 / `0x80040224` even
when the customer/case read succeeds and the application can write activities.
Do not grant plug-in creation/writes, schema administration, case deletion,
case resolution or System Administrator to the runtime. After changing a role,
verify the exact effective privilege set; the platform can reintroduce unrelated
default SharePoint privileges.

The current integration is limited to synthetic test profiles. Contacts must
have `moacc_demoenabled=true` and a `moacc_actorid` equal to
`profile:` followed by the SHA-256 of their E.164 mobile number. Keep customer
phone numbers and email addresses in the approved Dataverse seed material,
not new repository fixtures.
The optional contact field `moacc_preferredlanguage` contains `ar` or `en`.
It selects the initial native voice language; null preserves the caller
controller's default. The backend aligns the native listening language before
HUMAIN speaks. A later explicit in-call language choice still takes precedence.

Required case columns are `moacc_requestkey`, `moacc_payloadhash`,
`moacc_category`, `moacc_district`, `moacc_street` and `moacc_landmark`.
Create an alternate key for the contact actor ID and case request key, and
confirm their indexes are Active. Use standard contacts, accounts, cases and
notes rather than separate per-channel case tables.

An authenticated `GET /api/cases` verifies the actual application identity
and required CRM fields without changing data. When native workstreams are
configured, it also verifies access to the native conversation plug-in assembly
metadata, without retrieving its binary content. A successful response reports
`source: "dynamics"`, `crmReady: true` and `cosmosCasePathEnabled: false`.
The ordinary process-health endpoint reports configuration only.

### Context and tool behavior

- Customer lookup starts after native caller/context verification and overlaps
  the existing ACS/VoIP preparation. The CRM context and personalized greeting
  are prepared before the HUMAIN microphone invitation and provider token
  metadata. Measure `crm_lookup_start`, `crm_lookup` and `crm_context_ready`
  separately from total call startup.
- The initial context contains the eligible customer, linked account and up to
  five current case records. Truncation is explicit; detailed customer updates
  are retrieved when a case is opened. A status question still needs a fresh
  tool result, not an inference from the startup snapshot.
- Greet by the known CRM first name, or name when no first name is present,
  in the selected response language. Unknown callers get no invented identity.
  A caller-ID match or selected chat profile is not authentication.
- Both channels expose customer/context and case-list tools plus the existing
  create, status and confirmed-follow-up tools. Business authorization and
  duplicate prevention live in the shared service, not the adapters.
- Creates return the actual Dynamics case number. `OP-` references are only
  deterministic lookups for uncertain write outcomes. Retries use the same
  operation ID and create-only conditional writes; they cannot overwrite an
  existing case with different details.
- Uncertain case/note creation is reconciled with read-only checks of the same
  deterministic record, for at most six seconds and five attempts. A missing
  `OP-` lookup remains unconfirmed during that bound; it is not proof the write
  never committed. The UI must not describe an unknown result as "not recorded."
- Native writes have a ten-second response window; ordinary reads keep their
  four-second limit. Each shared operation has a sixteen-second overall budget,
  including reconciliation, below the channel adapters' eighteen-second timeout.
  Remaining budget caps each subsequent request and readback delay.
- Native association writes use the same bounded readback after an uncertain
  response. Only explicit version conflicts retry an ETag-protected PATCH;
  transport timeouts never trigger another association write.
- Chat invokes the shared CRM tools directly from the coordinator. It does not
  need a second model transaction specialist just to save, read or append a note.
- Follow-ups append customer-update notes. They do not resolve, reopen, delete
  or reprioritize cases. Neither channel falls back to Cosmos when CRM fails.
- Native case linking validates the actual caller and allowed workstream before
  changing the customer, native IssueId and case Regarding associations. The
  customer remains the contact; Regarding points to the selected case. Only an
  empty Regarding, that same customer, or the already-linked case can be changed;
  unrelated records and a concurrently selected different case are preserved.
  A failed link is reported
  separately from a successful case operation. Closing a conversation does not
  close its case, and the existing human-handover mechanism remains unchanged.

Validate lookup latency through the deployed API, recording cold and warm
samples, percentile timings and returned-context size. A sub-second CRM lookup
is not a sub-second end-to-end telephone connection guarantee. Do not claim
workspace linking from a case-creation receipt alone: verify the native
customer/case panels during a controlled handover.

For that acceptance call, add `--case-links` to the read-only call monitor.
It records bounded, timestamped changes to the customer, native case and
Regarding associations, including the Regarding record type, media phase and
representative acceptance. It does not confuse an opened case tab with a saved
association, or infer why an association changed. Compare the actual case-tool
link receipt with these before/after observations. This option does not enable
the parked identity-retention or numerical-metrics experiments.

### Native case identification at handover

The native handover uses the case-sensitive **CaseNumber** output context,
containing the actual Dynamics `ticketnumber`. A case GUID or an `OP-`
reconciliation reference is not a valid substitute.

Prepare its conversation-scoped output variable:

```bash
python infra/scripts/native_voice_setup.py case-context-variable \
  --tenant-id "$TENANT_ID" --org-url "$ORG_URL" --bot-id "$BOT_ID" \
  --receipt-file "$WORK/case-number-variable.json" --apply
```

Use a new private receipt filename. Add `--crm-case-context` when generating
the reviewed bundle and when running `verify`. Deploy the compatible backend
before publishing this controller. Preserve existing custom caller gates when
updating an already deployed topic.

The adapter selects a case only from a validated successful create, read or
follow-up receipt, not from a list of possible customer cases. Its authenticated
snapshot exposes `selected_case_id`, `selected_case_number` and bounded
`case_link_*` evidence. The controller sets `Global.CaseNumber` immediately before
transfer. Normal handover reads the current status result; recovery reads the
stop result. A new conversation starts with no selected case.

A failed direct conversation-link operation does not erase the actual retrieved
case number. It remains available for native case identification, while the
failure is reported separately. Direct linking uses conditional writes, at most
three version-conflict attempts with ownership revalidation, and a readback
before reporting success. It does not create links on closed/wrap-up calls.
Successful readback requires both IssueId and the case-typed Regarding lookup,
not IssueId alone. An installed enhanced native Case Details control can keep its
card hidden while Regarding still equals Customer. Scoped case navigation must
read the actual conversation lookup when native session `caseId` is stale or
absent; neither a case number nor a matching customer list is a substitute.

## Representative workspace

Use a dedicated experience profile rather than modifying the default profile
for every representative. This profile provides native voice and case
sessions, linked-customer/case tabs, case lookup, the timeline, Knowledge,
Copilot and Agent scripts. Assign it only to the intended representatives.
Workstreams must reference the reviewed voice-session template.

The three guided workflows cover:

1. **Voice handover:** confirm the linked customer and case, review the issue,
   location and handover information.
2. **Case handling:** inspect current status, use published guidance, collect
   confirmed details and prepare the appropriate case or follow-up.
3. **Wrap-up:** record the outcome and remaining work, then end the phone
   session. Do not equate closing the conversation with resolving its case.

Provide a script variant for each supported representative UI LCID. Native
script selection filters by UI language; bilingual text on an English-only
script does not make that script selectable in Arabic UI. The workspace setup
has six script records (three per language), with Arabic redirects staying in
the Arabic variants and all variants reusing the same macros.

The native macro shortcuts support reviewing the customer, opening/finding a
case, opening new case/task forms for review, explicitly linking the focused
case, refreshing the record/session context and opening knowledge search.
They do not place calls, send emails or automatically resolve cases.

If workspace setup stops after a successful write, don't recreate anything.
Resume as described in
[native behaviors the kit resumes from](workspace-setup.md#native-behaviors-the-kit-resumes-from).

### Customer case lists versus conversation association

The contact's **Related Cases** grid and the conversation's **Case Details**
serve different purposes. The grid lists cases for that contact; Case Details
requires one saved conversation-to-case association. Neither a populated recent
case list nor opening a case tab establishes that association.

Do not use **My Active Cases** as the default customer subgrid view: its
current-owner filter hides cases owned by the CRM application user, and its
active-state filter hides resolved cases. Use **All Cases** while retaining the
current-contact `customerid` relationship filter. Scope changes through a
nondefault cloned contact form pinned to the dedicated profile's contact tabs
and contact-navigation macro; register that form with the workspace app without
changing shared forms, views, case owners or other users' defaults.

The scoped **Find existing cases** route uses this same current-customer form
and its All Cases grid, rather than an unfiltered incident list that defaults
back to My Active Cases. Opening a result still does not associate it with the
phone conversation.

### Case location and safe form actions

A case's service location is separate from the contact's address.
District, Street, Landmark and Category are existing custom incident columns;
use the nondefault custom case form to display them. Pin that clone in the
owned case routes and register it with the app. Do not copy location into an
unrelated standard field or change a shared form to make the data appear.

New-case navigation uses a registered native Entity Record application template,
with `createFromEntity` when customer context is available and an empty data
object. It opens an **unsaved** form; only a deliberate native Save creates a
case. The case tab and Open linked case action read the current conversation's
persisted IssueId rather than guessing from the newest case or a stale session
`caseId`.

The explicit Link confirmed saved case action requires a live conversation and
the correct saved, focused case. It is not a post-hang-up repair action. A
skipped link after wrap-up does not prove a link write failed. The visible
refresh step is **Refresh saved case/tab**, not its internal macro name
"Refresh focused record." Reload the workspace and reopen existing tabs after
changing form/template definitions.

### Knowledge and Copilot

When native knowledge is selected, publish reviewed private knowledge in both
English and Arabic using native translation relationships. The renderer derives
groups from the supplied topics and retains their approved source references.
The neutral template has no business knowledge or fixed article count.
Do not bulk-publish unrelated existing drafts or archived content.

Inspect native Knowledge search's **Visibility** filter separately from Copilot
Q&A. Template-published articles are internal; the native External-only default
excludes them even when the index and representative Read access work. Use
Internal or Both for the approved representative, retaining the Published
filter and that user's language choice. A personal search preference must not
make the articles public or change other representatives' defaults.

Enable the **knowledge article source** for representative Copilot and verify
its native provisioning state, not just the saved checkbox. Ask-a-question,
answer drafting and deprecated knowledge flags are different capabilities;
do not enable unrelated fields to make them look consistent.

Copilot grounds answers in articles tagged with the representative's **UI
language**, which is separate from the caller's preferred voice language.
Adding Arabic article translations does not itself enable Arabic UI language
availability. Keep the user's existing UI choice unchanged while preparing the
additional language, then verify native Q&A in the intended UI language.

Knowledge Q&A is not automatically a query interface for live customer cases.
HUMAIN's shared CRM tools are separate from the representative Copilot's tools.
Use the case form's summary and timeline for record-specific updates. Free-form
case-data questions in the Copilot pane require the corresponding supported
case-data/extensibility capability and permissions; publishing knowledge
articles or enabling case summaries alone does not provide it. See
[case-data questions](https://learn.microsoft.com/en-us/dynamics365/contact-center/use/use-ask-a-question#summarize-cases-and-ask-about-case-data)
and [Copilot Studio extensibility](https://learn.microsoft.com/en-us/dynamics365/customer-service/administer/extend-copilot-with-copilot-studio).
This workspace's knowledge configuration does not enable that
extensibility or create case-query tools.

Case, join/handover, on-demand and end-of-conversation summary controls must be
checked across the installed settings families. Review the generated summaries
against the source records: an enabled control does not prove complete coverage
of the hybrid AI conversation. Native summary content and language quality are
part of acceptance, not configuration-only proof.

### Where to inspect summaries

- **Conversation summary:** open **Activities > Conversations**, select the
  completed phone conversation and open its native **Summary** surface. This
  historical conversation form is not the active session's Customer Summary.
  The modern stored output is `msdyn_conversationinsight.msdyn_copilotsummary`;
  a null legacy `msdyn_conversationsummaryfield` does not prove it is absent.
- **Case summary:** open the associated saved case and inspect its Copilot
  summary and timeline. It summarizes available case information, not every
  article the representative has searched. A five-word description without
  notes or associated activities can legitimately yield "not enough
  information." Do not pad records or invent actions to force generation.
- **Join/handover summary:** capture the actual join experience separately.
  Finding an end-of-conversation summary does not prove that a separate join
  summary was generated or displayed.

The inspected acceptance conversation's stored summary covered the AI
case/status discussion and the subsequent human join/audio checks. This is
one observed output, not proof of exhaustive details or accurate speaker labels
in every export. Compare the summary with the transcript and actual case
fields; the custom location fields are not automatically included in the native
case-summary mapping.

### One combined acceptance session

Reload the native workspace after profile/configuration changes, with no active
call or wrap-up. Use the controlled call procedure with `--case-links`:

- Hear the CRM-backed name and preferred initial language.
- Ask for the current status of one specific active case, then request a human.
- Verify the same customer and case are selected in the native conversation.
- Hang up within the agreed limit; perform the remaining workspace checks
  without keeping the telephone connected.
- On the synthetic case, exercise the scripts, navigation/refresh macros and
  reviewed case/task forms. Check that wrap-up leaves the case unresolved.
- Ask a knowledge question in each configured UI language, inspect citations,
  and compare case/join/end summaries with their actual source information.

Do not use a manually opened case tab, a saved setting, or a successful API write
as a substitute for the corresponding native UI observation.

Keep the prefetched-selection and uncertain-write regression checks. HUMAIN can
describe a prefetched case without invoking a fresh case-selection action,
leaving Case Details empty. An earlier link exceeded the old four-second write
deadline and was later present in Dynamics. The current implementation gives
writes ten seconds and performs bounded read-only reconciliation within the
whole operation budget. Absence at that deadline is still unconfirmed, not
proof that no write occurred. Do not blindly repeat writes or rewrite completed
conversations, and do not infer UI acceptance from the reconciliation tests.

### Native supervisor monitoring and joining

Use the native **Ongoing Conversations** report with **Supervisor Monitor**
enabled and an authorized supervisor account. Voice monitoring requires an
active conversation; a supervisor cannot monitor a conversation assigned to
that same account. Test human-phase supervision with a representative and a
separately signed-in supervisor, not two browser tabs for one identity.

Native **Monitor** is silent listening. Private consultation messages are
hidden from the customer; do not describe them as proven audio whisper.
**Join call** makes the supervisor audible, but does not promise to stop
HUMAIN's separate microphone participant. AI-phase Join has been demonstrated
in the source environment by the user, with the human audible while HUMAIN
remained active. That is not evidence that every role/environment supports the
same action, or that AI input/output is paused.

The application forwards only verified customer audio to HUMAIN. Supervisor
speech is excluded, and no supervisor-join event currently cancels AI output.
Native ownership changes can trigger asynchronous safety cleanup, which is not
an instant supervisor-interruption contract. A deterministic AI takeover would
need explicit authorization, output suppression, PCM flush, verified removal
and native handover coordination; the generic stop endpoint is not equivalent.

The separate [Pause AI setup](supervision-setup.md) now provides an opt-in
implementation for an explicit authenticated intervention. It is disabled by
default and requires reviewed schema/permissions, a compatible pinned controller
and an actual native control-tab binding. Do not mistake code availability or
an unbound tab template for an enabled workspace feature.

The control uses native `Xrm.WebApi` command creation, not a browser copy of the
private controller key. Requested/Pausing are not Paused: the latter requires
the runtime's correlated microphone/PCM acknowledgment. Native Join still does
not trigger this action automatically, and pause is not native reassignment.

For an authorized AI-phase test, first check whether native Monitor/Join is
available. Keep the caller microphone muted while the supervisor speaks during
an AI answer, so acoustic echo cannot masquerade as customer interruption.
Measure whether AI output actually stops and stays stopped. If the action is
unavailable, record that limitation rather than substituting a conference
invite. Separately test Monitor/private messages/Join after ordinary human
handover. Use the same consent and 90-second caller hangup limit; no supervision
capability is accepted solely from configuration or a successful join.

[Native voice monitoring](https://learn.microsoft.com/en-us/dynamics365/customer-service/use/voice-channel-monitor-calls)
and [eligibility restrictions](https://learn.microsoft.com/en-us/dynamics365/customer-service/use/monitor-conversations).

## Optional startup-only bilingual notice

The generated controller uses the authenticated
`POST /dynamics/native/sessions?wait_for_startup=true` request to opt into a
prerecorded Arabic then English notice, played only by the application on the
existing Dynamics call:

```json
{
  "conversation_id": "<actual-native-conversation-UUID>",
  "language": "ar",
  "retain_native_controller": true,
  "speech_turn_control": true,
  "play_startup_notice": true
}
```

Omitting the flag preserves the existing behavior. The option requires native
VoIP speech-turn control and cannot be changed on an existing session. The
caller cannot supply an audio URL or arbitrary text.
The prepared-start query selects parallel preparation and acknowledgment-gated
AI release. The older immediate/context-wait application modes remain for
compatibility; omitting the query is not equivalent to this contract.
The historical `native_startup_notice` flag remains for receipt compatibility
and controlled rollback, but cannot be combined with application playback.
Supervision-enabled controllers also carry their existing versioned contract.

The packaged notice contains only the recording, quality and training statement;
it has no welcome or organization name. Its editable synthesis source is
[startup-notice.ssml](../../apps/voice/dynamics/startup-notice.ssml); the runtime
asset is
[native_startup_notice.wav](../../apps/voice/src/app/backend/native_startup_notice.wav).
Review the wording for the intended deployment. Regenerate the asset with
Azure Speech as **mono, 16-bit PCM, 16 kHz WAV**, between 1 and 30 seconds and at
most 2 MiB. Speech synthesis happens during preparation, not during a call.
The static, non-confidential notice has a public, content-addressed download URL
so ACS can fetch it. Never put customer information or secrets in this asset.
The packaged recording plays at **1.3x speed** with its pitch preserved
(approximately 7.54 seconds rather than 9.83). After synthesizing the base SSML,
apply FFmpeg's `atempo=1.3` filter and retain mono PCM16 at 16 kHz. Audio
processing is an offline preparation step, not an application dependency.

### Playback boundaries

**Source-environment history, 1 November:** the preceding native-only route
reached HUMAIN in approximately 13 seconds, but the caller heard no recording
notice. A subsequent application-only startup call had audible playback and a
caller-accepted smooth transition to HUMAIN, with approximately six seconds
before the notice. This is one accepted playback test, not proof of instant
answer-to-audio or all cold-start behavior.

- Keep the ordinary 20 ms silent speech handshake before the HTTP action. The
  HTTP action's native latency announcement is disabled: no parallel native
  recording prose or WAV reference.
- The backend validates the exact call and caller's ACS membership before
  submitting the packaged clip to that caller through its own ACS connection.
  CRM lookup and VoIP preparation run concurrently. The static notice does not
  wait for CRM preference resolution or the first native language acknowledgment;
  it contains no customer-specific information.
- CRM context must still be verified before HUMAIN's microphone is invited.
  Provider preparation continues while the notice plays, not after a full-clip
  wait. No caller/identity validation deadline is removed or extended.
- The response waits for provider preparation or a terminal failure, with a
  22-second bound inside the existing 30-second setup limit. The ten-second
  exact-context visibility limit remains unchanged; do not bypass it by selecting
  another call. This is not a promise that every cold startup succeeds.
- On HTTP completion, the controller adopts the
  prepared response's requested language/revision and completes the existing
  silent language question. The backend reserves up to eight seconds for that
  acknowledgment. It cancels only its own notice queue and requires the exact
  matching terminal callback before revalidating the native owner/controller and
  releasing AI audio. Unconfirmed cancellation fails closed.
- Until acknowledgment, customer audio is not submitted to HUMAIN, new tools
  are rejected explicitly, and output is retained in a bounded ten-second PCM
  buffer. On release, buffered frames are paced rather than burst into the
  native microphone's 500 ms queue. Overflow fails explicitly; no first words
  are silently discarded. Pause, barge-in flush and shutdown clear pending audio.
- The callback is protected by a per-call capability and exact
  connection/correlation/operation matching. It is not evidence that a human
  has joined. Pause, shutdown and teardown retain their existing quiet/output
  cleanup rules.
- Time before Copilot starts remains a separate native-platform interval.
  Server incoming-call, PSTN participant and handset-answer timestamps are
  different; instant handset playback has not been established.

### Native configuration

Disable the native recording-announcement path:

```yaml
latencyMessageSettings:
  kind: LatencyMessageSettings
  allowLatencyMessage: false
```

The backend supplies the exact packaged asset URL, not a caller-provided URL.
Do not add native recording prose or an audio fallback: the observed native
route synthesized prose at normal speed, while speech-only audio was silent.
Publication/schema checks alone did not catch the difference. Preserve the
ordinary silent speech response and native listening turns; this is not an
audio-only topic that ends without a voice response.

Add `--startup-notice` to the reviewed `native_voice_setup.py bundle` command
when generating a compatible controller. Existing controllers still require
explicit `--replace-controller`; preserve any custom caller gates. The option
uses the application-only prepared-start contract and disables native notice
playback on that path. It preserves human-transfer actions, the AI deadline
and caller allowlist; normal native language turns still acknowledge output
release and later language changes.

For an approved startup-only rollout, the workstream's **Skip wait music for
workstream IVR** setting (`msdyn_skipwaitmusicforivr`) suppresses the initial
native music. This is a workstream-level setting, so review all of its entry
routes before changing it. Preserve its previous value for rollback.
**Do not replace the per-language Wait Music or Hold Music assignments**:
those belong to the native waiting/hold experience and must remain unchanged.
Deploy the compatible backend before publishing an opted-in controller.
Its authenticated readiness response must report `startup_notice_available:
true`, `application_startup_preparation_supported: true`,
`startup_failure_notice_supported: true` and the exact `startup_notice_sha256`.
The verifier checks the silent handshake, prepared-start URL, application-owner
response guard and disabled native latency announcement. It refuses an old or
mismatched backend/controller/asset.

The API and renderer opt-in do not themselves change Dynamics music settings.
Record both changes separately, use idle-call and ETag guards, and verify
publication and saved configuration before a caller test.
For a fresh deployment, use
[`native_voice_setup.py startup-audio`](deploy-with-copilot.md#apply-the-accepted-native-audio-settings).
It saves an immutable plan, requires notice-policy approval for writes, and
changes only the two accepted startup-audio flags. Its readback reports the
earliest caller-acceptance time; it does not prove runtime playback.

An already-owned Pause AI controller needs the narrow
[application-startup revision](supervision-setup.md#upgrading-an-existing-startup-notice),
not a generic controller overwrite or edits to old receipt hashes.

### Acceptance

Status must expose `startup_notice_owner: "application"`,
`startup_notice_requested: true` and `native_startup_prepared`. The timing feed
records `startup_notice_started`, `startup_notice_stop`,
`startup_notice_terminal`, `native_startup_response_wait`,
`initial_language_ack_wait` and `native_startup_audio_released`. Require notice
cancellation before AI audio release, as well as a matching native language
acknowledgment. Neither provider-ready nor generated PCM proves audible greeting
or notice onset; capture the native activity, application callbacks and caller
result. No duplicate announcement is not a pass when no notice is audible.

In the accepted source-environment call, application playback started 3.468
seconds after the backend request began. Native language acknowledgment preceded
the correlated notice stop, and first AI output energy followed the notice
terminal callback by 0.407 seconds. The native execution trace contained no
recording prose or audio tag and no MessageBack error. These server measurements
do not replace the caller's approximately six-second answer-to-notice estimate;
an exact handset-answer timestamp was not captured.

### Preserving a working startup before optimization

Treat accepted audio behavior as a baseline, not just a successful deployment.
Before changing the startup path:

1. Finish calls and wrap-up. Capture the immutable image digest, live revision,
   native publication time, exact controller/caller-entry hashes, runtime pins
   and native workstream/language/music settings.
2. Save an owner-only source archive and per-file hashes, including uncommitted
   implementation files. Preserve the deployment environment privately; native
   topic sources and environment snapshots can contain credentials. Never commit
   them or print them in a shared report.
3. Keep the successful caller feedback, exact call IDs and timing/recording
   evidence. Verify the archive contents and keep an integrity manifest for the
   source and configuration snapshots.
   Preserve raw native readbacks. Compare configuration separately from
   metadata-confirmed read-only rollup timestamps: the observed
   `msdyn_skillattachmentrulescount_date` advanced hourly without an ETag
   change. Do not ignore its source count, routing fields or arbitrary
   timestamp-shaped configuration.
4. Change one independently measurable surface at a time. An image-only startup
   optimization must not silently republish the bot, enable IVR music, replace
   the recording, change CRM/routing, or alter the context/setup/active-AI limits.
5. Keep the accepted image available for a narrow restore. With calls idle,
   disable supervision, restore only that immutable image, verify the unchanged
   controller and configuration, run the actual deployed-identity preflight,
   then coordinate re-enable and persist the restored image/settings. Do not
   reset the dirty worktree or roll back native schema/workspace history.

The first-audio goal is independent of HUMAIN's preparation time. Do not add a
sleep, minimum playback floor, full-clip wait or music-finished prerequisite
before provider preparation. Neither synthetic concurrency tests nor healthy
configuration prove a one-to-two-second handset result. Keep native waiting
music as a separate coverage experiment with its own baseline and consent.

### Prepared-start latency optimization

This experiment is **reverted and on hold**. On 2 November, the source files and
live voice image were restored to the accepted pre-optimization snapshot. Native
settings, published topics and the application-owned WAV also match that
baseline. The optimization-specific tests were removed; the accepted runtime,
CRM, notice, supervision and transport suites passed after restoration.

The **5-7 second first-notice delay remains work in progress**. No one-to-two-second
result was established. The source baseline uses the existing
`native_context_resolved` timing; optimization-only readiness flags and telemetry
must not be required by a deployment checklist.

Keep the experimental receipts for diagnosis. Do not deploy the reverted image,
rerun music trials or add a timer as part of the current
[AI-led deployment](deploy-with-copilot.md).

### Measuring an optimization

Use the controlled call procedure above to verify:

0. Measure answer/timer-start to first audible notice separately from dialing
   and first HUMAIN speech. Preserve the native emitted speech activity and
   backend timing evidence; neither alone proves handset audibility.
1. Arabic precedes English and the initial music is absent.
2. The notice ends when HUMAIN is ready, without waiting for its full length.
3. There is no overlap or clipped first HUMAIN word; caller audio still reaches
   HUMAIN after cancellation.
4. Native transcript/recording and same-conversation human handover still work.
5. Human-queue and hold music retain their previous behavior.
6. Compare native-event-to-backend, backend-to-verified-context,
   context-to-verified-ACS-caller, notice onset, provider readiness and first
   audible AI output separately. Require no new audio-completion dependency
   before HUMAIN preparation; do not hide slower preparation behind filler.
7. Record the change from the accepted image, not just an absolute number.
   The initial sample's approximately six-second delay is a measured limitation,
   not an SLA or evidence that every later call has the same critical path.

Compare first-speech timings as well as listening: cancellation confirmation is
an asynchronous network operation, not a guarantee of zero added latency.
An interrupted recording notice is not affirmative consent and may not deliver
the entire notice. Confirm the applicable notice/consent requirement before
enabling interruption.

### Controlled native IVR waiting-music coverage test

This section records a **reverted experiment**, not a deployment procedure.
Both native workstreams use the accepted skip-IVR-music configuration. The
application, source and live image were restored to the sealed pre-optimization
baseline on 2 November. Further latency/music work is on hold.

After the image-only optimization still produced about six seconds before the
notice, the source environment separately enabled the existing native IVR
waiting music on the **UK workstream only**. The sole conditional native update
was `msdyn_skipwaitmusicforivr: true -> false`, with an exact before image,
ETag, intent and readback receipt. The second workstream's settings, per-language wait/hold assets,
recording notifications, the application image/revision, caller entry and bot
publication were verified unchanged.

This is not a three-second timer or a new message before application startup.
The native platform controls the music's natural start/stop boundaries.
Configuration verification does not establish early audibility, full-gap
coverage or no additional HUMAIN latency; those require a separately authorized
startup-only call.

**Source-environment result:** the authorized caller heard no music and reported
an experience identical to the preceding tests. The original UK skip-music
value was restored to `true`, with all other configuration and the optimized
application unchanged. However, the change was saved at 20:41:15 UTC, the call
started at 20:45:47 UTC, and restoration was saved at 20:50:10 UTC.

Microsoft's [Contact Center FAQ](https://learn.microsoft.com/en-us/dynamics365/customer-service/administer/faqs#how-long-does-a-configuration-change-to-the-dynamics-365-contact-center-and-unified-routing-settings-take-to-update)
states that workstream and other configuration changes can take up to
**15 minutes** to reflect. Testing after about 4.5 minutes and restoring within
nine minutes did not allow that propagation window. The caller result was
negative, but the experiment is **inconclusive about native music support or
effectiveness**. A successful Dataverse readback or `Published` component state
does not prove the telephony service has consumed the change.

Before any separately approved retry, capture a fresh baseline and receipt,
allow at least 15 minutes after successful configuration readback, and only then
obtain fresh caller readiness and arm one new observation. This is a one-time
deployment wait, not a delay inserted into calls. Restoration also has a
propagation window. Do not repeat the earlier conclusion that native waiting
music cannot cover the gap based on this premature trial.

The selected English/Arabic stock music files existed, were active, and returned
readable MP3 headers. The call's native bot sent its ordinary 20 ms silent speech
response about 0.233 seconds after its first activity. That response may end a
native waiting phase before application preparation, but no available trace
proved a music-start/stop decision. Neither that hypothesis nor asset readback
establishes the playback root cause.

**Propagated retry, 2 November:** a fresh UK-only trial was allowed the full
15-minute window and left unchanged overnight. The next two calls could not
be accepted as a successful startup-music comparison:

- The first exhausted the existing ten-second native-context deadline with
  `native_session_pending`. The conversation was initially unreadable, then
  no usable controller was returned. Native error speech matched that outcome.
- The second reached ACS connection readiness, but the participant list did not
  match the exact expected caller (`customer_not_in_native_call`). The
  application stopped before notice playback or HUMAIN readiness and returned
  its native handover fallback. A native handoff and `BotTransferSession`
  explain why the queue/workspace was called without a requested human transfer.

Neither call submitted the application's recording notice. The caller's reported
song on the second call was therefore not that WAV; queue/wait music is
consistent with the handoff but its exact native source is not proven.
The one-shot caller-membership check also exists in the accepted pre-optimization
source. Faster readiness polling may expose a race sooner, but no captured
participant response or later in-call read proves that hypothesis.

After both conversations were Closed, only the UK skip-music flag was restored
to `true`, with verified readback at 06:02:12 UTC. Allow the restoration's full
propagation window before any new acceptance call. The optimized image, native
controller/publication, music assets, the second workstream's settings and original sealed
source/image rollback baseline were unchanged. Do not call this a passed
coverage or no-added-delay test, bypass caller validation, or assume the music
flag caused either readiness failure.

For this specific experiment, record first music, notice and HUMAIN onset
separately instead of asserting that initial music is absent. If the music ends
when the native bot starts and leaves another silent interval, record partial
coverage, not a fully bridged gap. If it persists over speech or delays
application/provider startup, close the call/wrap-up and conditionally restore
only the captured UK skip-music value. Do not overwrite shared music assets or
change the second workstream.

Restore this experiment's music flag before using an image-only rollback guard
that expects the original music configuration. Preserve both the accepted
source/image baseline and the separate music-change receipts; do not rewrite
either to make a later comparison pass.

## Telemetry

### Experimental identity-lifetime diagnostic

This is an **off-by-default diagnostic, not a confirmed Call Metrics fix**.
It tests whether deleting the external microphone's ACS identity during
handover affects later native analytics. Microsoft documents that identity
deletion removes the identity, revokes its tokens and deletes associated data;
it does not document whether Dynamics' numerical processor needs that data.

For a separately approved, caller-scoped experiment, the authenticated native
session-start request can include:

```json
{
  "conversation_id": "<actual-native-conversation-UUID>",
  "language": "ar",
  "retain_native_controller": true,
  "speech_turn_control": true,
  "retain_identity_for_metrics": true
}
```

Omitting `retain_identity_for_metrics` preserves immediate identity deletion.
The option requires VoIP speech-turn control and cannot be changed on an
existing session. Use a reviewed temporary caller-specific controller; do not
enable it for every caller by changing the shared production topic.

On a clean, customer-requested human handover:

- The browser, microphone participant and application call leg still leave.
  Their existing departure checks remain required before native handover.
- Token revocation is requested after microphone departure. The identity is
  retained for **900 seconds from media release**, then deletion is attempted.
  Revocation is not an assertion of instantaneous token invalidation.
- A failed setup, failed departure or application shutdown uses immediate
  cleanup instead. A failed revocation attempts immediate deletion and reports
  failure; it does not claim the retention experiment succeeded.
- Only one retained-identity diagnostic may await cleanup at a time. Normal
  non-diagnostic calls remain available.

Authenticated session status adds `identity_retention_requested`,
`identity_retention_seconds`, and `identity_cleanup_status` for the opted-in
call. Cleanup status progresses through `active`, `retained`, `deleting` and
`deleted`, or reports `failed`; it is `not_allocated` before an identity exists.
No ACS identity or token is exposed. Readiness reports
`retained_identity_count` and `identity_cleanup_failures`.
Pending identity cleanup keeps that call's status available beyond the normal
history expiry; the existing bounded history limit still applies.

The timing feed records `voip_identity_revoke`, `voip_identity_retained` and
`voip_identity_delete`. Failed deferred cleanup is logged and remains tracked
for a shutdown cleanup attempt. Graceful shutdown ends the grace period early;
abrupt process termination can prevent deferred deletion. Keep the tested
revision running until cleanup is confirmed, and treat a restart or unconfirmed
cleanup as an invalid retention experiment requiring operational follow-up.

The 15-minute grace is an experiment bound, **not an analytics SLA**. Verify
that the actual conversation closed while the identity was retained, compare
the native metrics-file result with a matched immediate-deletion call, and
record the eventual deletion. Do not infer causation from one successful call
or infer a processor failure from an early missing file. Retaining an identity
does not keep the microphone connected or change native speaker roles.

For an approved retained-identity call, the read-only monitor can collect the
remaining evidence without keeping the caller on the phone:

```bash
python infra/scripts/watch_native_voice.py \
  --tenant-id "$TENANT_ID" --org-url "$ORG_URL" \
  --voice-origin "$VOICE_ORIGIN" \
  --workstream-id "$WORKSTREAM_ID" --queue-id "$QUEUE_ID" \
  --representative-id "$REPRESENTATIVE_ID" \
  --secrets-file "$WORK/control-keys.env" \
  --output "$WORK/identity-retention-call.json" \
  --caller-limit-seconds 150 --identity-mode retained \
  --follow-identity-cleanup --arm
```

These flags **observe** the option; they do not enable it or change the bot.
Agree the caller limit before using a value other than the default 90 seconds.
The monitor accepts explicitly agreed caps of 60, 90, 120, 150 or 180 seconds;
choosing a longer cap does not change the backend's AI-phase deadline.
The default 210-second dialing window is separate from the call observation
window, which ends five seconds after the agreed caller limit.
`--dial-window-seconds 600` gives the tester ten minutes to start the one call;
it does not extend the agreed caller hangup cap or the backend AI-phase limit.

With `--follow-identity-cleanup`, the monitor saves the original call-window
report separately, then observes cleanup with a 16-minute deadline.
In-flight requests retain their own bounded timeouts.
Each metrics checkpoint has its own numbered `.snapshot-NN.json` file;
the final report is written once. Earlier evidence is never overwritten.
It reads the native metrics file no more often than once a minute while the
conversation is Closed, plus a final check when identity deletion is observed.
The caller still hangs up at the agreed limit; this is not an extension.
When the original saved speech-message attachment becomes available, the
report also records its role-token counts and whether the identity was still
retained when read. Raw speech, phone numbers and ACS identities are omitted.
Missing role tags are recorded as an input difference, not rewritten or
treated as proof of the numerical worker's behavior.

`identity_trial_valid` requires observed retention while the conversation is
Closed, successful revocation and deletion timing, and the full 900-second
grace before deletion begins. A restart, lost adapter session, early shutdown
cleanup or unconfirmed handover does not pass those checks. File absence,
zero-duration output and meaningful generated metrics remain distinct
observations; an otherwise valid trial is not automatically a successful fix.
Authorization failures and ambiguous insight rows stop the diagnostic rather
than being reported as missing metrics. The monitor never deletes identities,
starts calculations, changes participants, publishes a bot or hangs up a call.

[ACS identity deletion reference](https://learn.microsoft.com/en-us/rest/api/communication/identity/communication-identity/delete)

### Immediate per-call timing

The protected endpoint is:

```text
GET /dynamics/native/sessions/{actual-native-conversation-id}/timings
X-API-Key: <dedicated media control key>
```

Read it from a server-side tool that obtains the key from a private file. Do not
put the key into a URL, public browser application or shell history.

The timeline includes:

- Native context reads/wait.
- ACS connect request, state and readiness.
- Fresh identity/token allocation and browser-page acquisition.
- VoIP SDK registration, invitation, native membership and RTP readiness.
- Initial/native language acknowledgements.
- HUMAIN context byte count, token, room, microphone, agent readiness.
- First PCM and sustained audio-energy milestones.
- Transcript-final counts and tool execution spans.
- Handover request and resource release.

Times are monotonic relative to one backend session origin. The UTC origin
enables correlation with native artifacts; it is not a packet-synchronized
global clock.

`first_output_energy` is 60 ms of PCM energy above -40 dBFS. It is not a
recognized word, first syllable guarantee or handset playout timestamp. Silence
and comfort noise can produce PCM frames before speech.

### Retained logs

Run in the target Log Analytics workspace:

```kusto
ContainerAppConsoleLogs
| where TimeGenerated > ago(1h) and ContainerAppName == "ca-voice"
| where Log contains "VOICE_LATENCY "
| extend timing = parse_json(extract(@"VOICE_LATENCY (.*)", 1, Log))
| project TimeGenerated, conversation=tostring(timing.conversation_id),
    stage=tostring(timing.stage), elapsed_ms=todouble(timing.elapsed_ms),
    duration_ms=todouble(timing.duration_ms), status=tostring(timing.status),
    fast_start=tobool(timing.fast_start)
| order by TimeGenerated asc
```

For one selected call, add:

```kusto
| where conversation == "<native-conversation-id>"
```

Check ingestion itself without fetching conversation content:

```kusto
ContainerAppConsoleLogs
| where TimeGenerated > ago(1h) and ContainerAppName == "ca-voice"
| summarize Rows=count(), LatestEvent=max(TimeGenerated),
    LatestIngestion=max(ingestion_time())
```

The tested table is `ContainerAppConsoleLogs`, not the legacy
`ContainerAppConsoleLogs_CL`. A schema existing in the workspace is not evidence
that rows are arriving.

Root Bicep configures the voice environment with:

- `appLogsConfiguration.destination = azure-monitor`.
- `runtime-logs` diagnostic setting -> existing workspace.
- Console and system categories only.
- No enabling of Log Analytics local/shared-key authentication.
- No HTTP diagnostics exposing per-call media capability URLs.

Allow normal ingestion lag; observed batches were about 6-7 minutes behind event
time. For immediate debugging use the protected timeline or bounded live logs.
The separate VNet environment's logging configuration was not changed by the
latency work.

### Compare startup configurations

Use the same image, topic, caller topology, model, ASR and knowledge context.
Change only `DYNAMICS_NATIVE_MEDIA_FAST_START`.

Record **both** successful and failed calls. Exclude failures from a successful
latency median, but report their number and reasons separately.

Measure:

1. Native recording/call start -> first actual AI speech.
2. Setup announcement end -> first actual AI speech.
3. Backend start -> verified microphone ready.
4. Backend start -> HUMAIN ready.
5. First PCM versus first sustained energy.

An empty-prompt local LiveKit test and a full-context PSTN call measure different
workloads. Benchmark the same workload before attributing a change to deployment.

The historical two-versus-two Azure comparison is in the
[reference results](reference.md#measured-results). It is small-sample evidence,
not a p95/SLA claim. Fast startup leaves native language and ownership gates
intact.

## Permanent observer

Deploy the version that supports permanent mode before changing these flags,
and make the change while the line is idle. Keep the resource ID and observer
key unchanged. Plan, review and apply the runtime change with
[native runtime settings](#native-runtime-settings), then run its printed
`azd env set` commands:

```bash
python infra/scripts/native_runtime_settings.py \
  --subscription-id "$SUBSCRIPTION_ID" --resource-group "$APP_RG" \
  --set DYNAMICS_NATIVE_PROBE_PERMANENT=true --set DYNAMICS_NATIVE_PROBE_EXPIRES_AT= \
  --azd-environment "$ENV" --output-dir "$WORK/runtime-permanent-observer"
# Review the diff, then rerun the same command with --apply --confirm-app ca-voice.

python infra/scripts/native_voice_setup.py observer-parameters \
  --system-topic-name "$SYSTEM_TOPIC" --voice-origin "$VOICE_ORIGIN" --permanent \
  --secrets-file "$WORK/control-keys.env" \
  --output "$WORK/observer-permanent.parameters.json"

az deployment group create --name native-call-observer \
  --resource-group "$NATIVE_RG" --subscription "$SUBSCRIPTION_ID" \
  --template-file infra/dynamics-handover-probe.bicep \
  --parameters "@$WORK/observer-permanent.parameters.json" --output none
```

Verify `native_call_probe.permanent=true`, native readiness reports the observer
enabled, and the Event Grid subscription has **no expiration time**. No far-future
date is used. The two original Dynamics subscriptions must be unchanged.

The 15-minute lookback, bounded buffer and all authentication/source checks still
apply. Permanent mode does not preserve buffered events or live sessions through
a process restart.

## Observer lease renewal

The backend refuses new native sessions when the observer is disabled or its
lease has expired. This section applies to expiring installations
(`DYNAMICS_NATIVE_PROBE_PERMANENT=false`), not permanent observation.
Stored events have a 15-minute lookback in both modes.

Renew while the line is idle, before the lease ends:

```bash
export PROBE_EXPIRES_AT="$(date -u -d '+24 hours' '+%Y-%m-%dT%H:%M:%SZ')"

python infra/scripts/native_voice_setup.py observer-parameters \
  --system-topic-name "$SYSTEM_TOPIC" \
  --voice-origin "$VOICE_ORIGIN" --expires-at "$PROBE_EXPIRES_AT" \
  --secrets-file "$WORK/control-keys.env" \
  --output "$WORK/observer-renewal-02.parameters.json"

az deployment group create --name native-call-observer \
  --resource-group "$NATIVE_RG" --subscription "$SUBSCRIPTION_ID" \
  --template-file infra/dynamics-handover-probe.bicep \
  --parameters "@$WORK/observer-renewal-02.parameters.json" \
  --output none

python infra/scripts/native_runtime_settings.py \
  --subscription-id "$SUBSCRIPTION_ID" --resource-group "$APP_RG" \
  --set "DYNAMICS_NATIVE_PROBE_EXPIRES_AT=$PROBE_EXPIRES_AT" \
  --azd-environment "$ENV" --output-dir "$WORK/runtime-lease-02"
# Review the diff, then rerun the same command with --apply --confirm-app ca-voice.
```

Only the expiry value changes on the Container App, through
[native runtime settings](#native-runtime-settings); no secret is on the command
line. Run its printed `azd env set` command so a later provision doesn't restore
the old value.

Verify:

- The latest revision is ready and `/health` responds.
- Authenticated native readiness reports observer enabled.
- Event Grid subscription `expirationTimeUtc` matches the runtime value.
- Incoming/recording subscriptions are unchanged: `native_event_delivery.py verify`
  passes.

An expiry-only Event Grid PATCH failed in the source environment; redeploying the
complete existing observer template worked. Do not delete the entire system
topic to renew one lease.

## Additional inbound numbers

Keep each existing number and its workstream unchanged. For another entry:

1. Acquire/import the additional number into the same native ACS/Dynamics
   environment.
2. Configure an inbound voice workstream for it, using the same published
   Copilot agent, fallback queue, capacity profile and representative pool.
3. Match the primary and secondary languages, voices, hold/wait music,
   recording/transcription settings, transfer controls, notifications and
   bot-failure behavior. Keep per-workstream generated identities, mutable voice
   profiles and routing records separate so editing one entry cannot change another.
4. Keep `DYNAMICS_NATIVE_MEDIA_WORKSTREAM_ID` as the original entry. Add the
   new UUID to `DYNAMICS_NATIVE_MEDIA_ADDITIONAL_WORKSTREAM_IDS`, persist it in
   azd and apply the live setting while idle.
5. Run `native_voice_setup.py verify` for **each** workstream and check that
   readiness lists both exact IDs. Repeat the controlled call acceptance on
   each number before claiming carrier/audio parity.

`runtime-env --additional-workstream-id` can generate the extra setting; repeat
that option for multiple additional entries. Blank or duplicate IDs fail rather
than widening the allowed scope. The backend resolves each conversation's own
workstream, queue and bot and rejects a change of owner during the call.

The published caller-selection policy is shared unchanged. Another number does
not add concurrent call capacity: the adapter still permits **one native pilot
call at a time**. Increasing that limit is separate work.

## Applying future changes

| Change | Required action |
|---|---|
| Python, browser code or dependencies | Targeted tests; `azd deploy voice --environment "$ENV"` |
| Bicep parameters/secrets/env values | Persist azd settings; review `azd provision --preview`; provision. For allowlisted non-secret native settings only, use [native runtime settings](#native-runtime-settings) |
| Copilot YAML / language settings | Save, reopen, Topic checker; publish the **agent once** |
| Dataverse human security role | Assign/review role, refresh user session; no Copilot publish |
| Observer lifetime mode | Update Event Grid **and** runtime; persist either the lease or permanent mode |
| Additional inbound number | Configure the entry, extend the explicit workstream allowlist, verify both routes |
| Knowledge content | Regenerate packaged copies, run knowledge checks, deploy affected services |
| Fast-start flag | [Native runtime settings](#native-runtime-settings) while idle; run its `azd env set` commands; warm readiness before a call |
| Purchased application number | Set `ACS_SOURCE_NUMBER` with [native runtime settings](#native-runtime-settings) before application telephony is used; persist it in azd |
| HUMAIN interruption policy | Accepted value `HUMAIN_INTERRUPTION_POLICY_MODE=off`; persist/apply with compatible code while idle. No native publication or workstream change |
| HUMAIN turn detector | Accepted value `HUMAIN_TURN_TAKING_STRATEGY=fast_vad`, paired with `off`; preserve both unless an explicit alternative or rollback is approved |

For an image-only change that needs inspection before promotion, split the
supported azd workflow:

```bash
azd publish voice --environment "$ENV" --to "$REVIEWED_VOICE_IMAGE"
# Inspect this image and record its digest before updating the service.
azd deploy voice --environment "$ENV" --from-package "$REVIEWED_VOICE_IMAGE"
```

With `docker.remoteBuild: true`, `azd package` alone can report no artifacts;
`azd publish` builds and pushes the image without updating the running app.
Compare the artifact with the running image, including resolved dependencies
and the native SDK bundle. An unchanged requirements file can still resolve a
new transitive package. Preserve the deployed version when an unrelated update
is outside the reviewed change. After promotion, verify the actual image digest,
deployed code, identity, secret values and native readiness; retain the image
reference in private azd settings. These are not caller/audio acceptance.

The [accepted responsiveness configuration](../../apps/voice/README.md#accepted-voice-responsiveness)
is `fast_vad` + `off`, approved by the user on 7 November 2026. Preserve the
tested image and explicit live/azd values; promotion does not require restarting
an already matching runtime. For future changes, snapshot the current image
and environment, keep ASR/voice/native startup/routing unchanged, and verify
requested versus provider-effective settings and actual audio separately.
An explicit conservative rollback sets both `livekit_v1_mini` and `control`;
it must not silently become the default again. The first-notice delay work
remains parked.

### Preserve deployed image names

After an azd deployment, preserve the `SERVICE_*_IMAGE_NAME` values in the
environment. A root provision using stale placeholder images can replace real
services. Always preview existing-environment changes.

Do not run two independent azd provisioning/deployment processes on the same
environment concurrently.

### Code-validation commands

Use the voice dependency environment for backend tests:

```bash
python -m pip install -r apps/voice/src/app/requirements.txt
PYTHONPATH=apps/voice/src/app:apps/voice/tests:infra/tests:infra/scripts \
  python -m unittest \
  test_latency test_native_media test_native_voip test_native_call_probe \
  test_humain test_acs test_profiles test_dynamics_outbound \
  test_dynamics_call_control_probe test_native_media_topic test_native_voice_setup \
  test_native_voice_docs -q

npm --prefix apps/voice/src/app/backend/native_rtc ci --ignore-scripts
npm --prefix apps/voice/src/app/backend/native_rtc run build
npm --prefix apps/voice/src/app/backend/native_rtc test

python infra/scripts/sync_native_voice_topic.py --check
az bicep build --file infra/main.bicep --outfile "$WORK/main-check.json"
```

These tests use mocks; no unit test places a call or sends email. `runTests`
discovery in the original editor did not find this unittest suite, so the
explicit runner above is the reproducible validation command.

Keep the checked-in snapshot synchronized:

```bash
python infra/scripts/sync_native_voice_topic.py
python infra/scripts/sync_native_voice_topic.py --check
```

The snapshot is generated from the legacy base template plus the speech-turn
renderer. Do not manually copy a tenant-specific rendered topic into the repo.

## Secret rotation

There are different credentials with different consumers:

| Secret | Consumers to update |
|---|---|
| HUMAIN authorization | Private azd settings / Container App secret |
| Native media control key | Container App secret **and every HTTP node in the published Copilot controller** |
| Native probe key | Container App secret **and Event Grid observer's secret delivery property** |
| Native ACS connection string | Dynamics native ACS connection and the Azure adapter using it; coordinate ACS key rotation |
| Ingestion key | Shared Assist case/knowledge backend and its voice/chat clients |

For the native ACS credential, follow the scoped procedure in
[runbook section 3.6](README.md#36-rotate-the-connection-string). Do not assume
the healthy **Manage telephony** screen exposes an editable connection string.
The current native UI shows resource details and Event Grid settings while the
connection is healthy.
Microsoft's [same-resource recovery guidance](https://learn.microsoft.com/en-us/dynamics365/customer-service/administer/voice-channel-connect-existing-resource)
documents the connection-string repair field after regeneration invalidates the
stored credential. Coordinate an idle maintenance window and an operator ready
to enter the fresh **primary connection string**; do not invalidate the active
key without that coordinated recovery.

Do not use **Disconnect** merely to make a field editable. Microsoft documents
that [disconnecting removes Dynamics number associations](https://learn.microsoft.com/en-us/dynamics365/customer-service/administer/voice-channel-disconnect-from-acs)
and can require voice-channel reconfiguration. Leave working Event Grid IDs and
routes unchanged. Regenerate only the intended dedicated-resource key once,
preserve the other key and original outcome, and verify actual connection repair
and no-call access afterwards. If the documented repair field is unavailable,
stop with the original state rather than retrying rotation or using a private API.
A healthy screenshot doesn't prove that the rotated credential works; check it
with a no-call readback and then a controlled call.

For a media-key rotation:

1. Ensure no pilot call is active; arrange a maintenance window.
2. Create a new random key in the private secret store. Do not print it.
3. Generate a new topic bundle with the updated private key. For an existing
   controller, explicitly review `--replace-controller`.
4. Apply the updated backend secret/settings through the approved infrastructure
   process, without overwriting unrelated secrets.
5. Stage, check and publish the new topic bundle while callers are held off.
6. Verify new-key readiness, old-key rejection and a no-call closed-record guard.
7. Store rollback material privately; retire the old key only after all consumers
   are updated.

This procedure requires a maintenance window; zero-downtime multi-key rotation
isn't supported. Exported topic YAML contains the live key. Rotate that key if
the YAML leaks.

## Rollback

### Fast-start rollback

While the line is idle:

```bash
python infra/scripts/native_runtime_settings.py \
  --subscription-id "$SUBSCRIPTION_ID" --resource-group "$APP_RG" \
  --set DYNAMICS_NATIVE_MEDIA_FAST_START=false \
  --azd-environment "$ENV" --output-dir "$WORK/runtime-fast-start-rollback"
# Review the diff, then rerun the same command with --apply --confirm-app ca-voice.
```

Run the printed `azd env set` command. The apply readback verifies the new ready
revision; then check native readiness. This returns to serial
preparation; it does not change the Copilot language/transfer controller.

### Topic rollback

1. Stop new test calls.
2. Open the private bundle's `before.json`.
3. Restore the previous `data` for the relevant topic through the native code
   editor, preserving newer unrelated changes.
4. Save, reopen and check all affected topics.
5. Publish the agent once.
6. Verify the intended caller gate and runtime pairing.

Keep the global initializer out of live entry topics. Restoring an external
phone transfer also changes how handover works. `NativeMediaPilot` can restore
the historical long-poll control flow, but lacks the speech-turn controller's
multilingual transcript behavior.

### Disable the pilot

Restore an approved nonpilot Conversation Start route first and publish it.
Then disable `DYNAMICS_NATIVE_MEDIA_ENABLED` in the live app and persisted
environment while idle.

If the caller gate still points to a disabled backend, new calls fail.

### Cleanup

Remove only explicitly identified test resources or subscriptions after checking
their consumers. Do not delete a resource group, entire ACS resource, native
number, or Dynamics incoming/recording subscriptions to remove the passive probe.

The application removes its per-call VoIP identity and browser context.
The fast-start spare page is software only and holds no ACS identity/token.
If cleanup is unconfirmed, investigate the exact call and identity. Avoid
cleanup commands that affect other calls or processes.

## Handoff checklist

- [ ] Repository revision recorded; required uncommitted source changes identified.
- [ ] Root deployment used; selected subscription/tenant verified.
- [ ] Current ACS eligibility/lifecycle guidance reviewed; actual tenant eligibility and migration owner recorded.
- [ ] Resource names, UAMI client/principal IDs and app user recorded.
- [ ] Native/app ACS resources and their different numbers recorded.
- [ ] Event Grid delivery app/client/object IDs and role assignments recorded.
- [ ] `native_event_delivery.py verify` receipt recorded; both Manage telephony banners gone.
- [ ] Native reader runtime check receipt recorded.
- [ ] Workstream, queue, bot, channel and representative IDs recorded.
- [ ] Human roles, bookable resource, capacity profile and app access verified.
- [ ] Application-role effective privilege report reviewed, including seeded extras.
- [ ] Private rendered topic bundle and rollback definitions stored securely.
- [ ] Published topic/language revision recorded.
- [ ] Runtime is `voip`; fast-start setting and rollback documented.
- [ ] Observer lifetime mode recorded; leased installations have a renewal owner and reminder.
- [ ] Durable stage log query works without enabling local authentication.
- [ ] Live bilingual audio, separate speaker headers and human acceptance tested.
- [ ] Closed native recording/transcript verified, not merely a success toast.
- [ ] Limitations and data-processing boundaries accepted by the receiving team.
- [ ] No active call, abandoned watcher or leaked diagnostic identity remains.

## Before production

- Plan the Dynamics Teams Phone extensibility transition and the separate
  application ACS number/Email dependencies before 30 September 2028. Recheck
  the [service lifecycle gate](README.md#first-confirm-service-lifecycle-and-eligibility);
  do not assume a custom ACS participant is automatically grandfathered or
  that the current media adapter can be moved without engineering/revalidation.
- Design durable event/correlation ownership and restart recovery; permanent
  observation alone only removes the scheduled expiry.
- Externalize session state before introducing multiple replicas/workers.
- Define restart recovery, abandoned-caller cleanup, high availability and load
  limits for the browser/media worker and provider room quota.
- Validate no-agent/busy/queue-timeout behavior, compliance, retention and auditing.
- Replace caller-ID/profile matching with the required customer authentication.
- Resolve friendly AI naming and analytics-role classification if those matter
  beyond a demonstration.
- Establish native automatic human-phase multilingual recognition if required.
- Validate word-level/mid-sentence code-switching limits and speech overlap.
- Benchmark representative workloads and failure rates; small test medians are
  not p95 guarantees.
- Outbound remains disabled/parked. The direct application `/call` endpoint is
  not a native Dynamics proactive calling implementation.
