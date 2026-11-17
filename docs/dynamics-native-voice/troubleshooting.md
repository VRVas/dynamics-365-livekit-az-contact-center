# Native voice: troubleshooting

[Deployment tutorial](README.md) | [Operations](operations.md) | [Reference](reference.md)

Use this page when a deployment step fails. It records the symptoms, causes and
fixes from the integration tests; the runbook already uses the tested approach.

## Rules before investigating

1. Have the caller hang up. Do not keep an international call open while reading
   logs or waiting on chat.
2. Record the exact native conversation ID and UTC time.
3. Check whether the app, original Copilot session and customer call are still
   active before issuing any cleanup.
4. Inspect the narrow failing boundary. A good recording does not prove good
   transcription; a successful tool does not prove a human answered.
5. Preserve failed test reports. Do not replace them with the next successful run.
6. Keep the original nonpilot caller route and before-bundle until validation.
7. Do not disable tenant security policy, rotate every key, add broad roles, or
   invite another telephone participant as a generic workaround.
8. Treat a CLI exit code as a hint. Read back the server state, retry once only
   with a concrete change, then stop and ask the owner. See
   [Power Platform CLI results](#power-platform-cli-results).

## Azure and tenant setup

### A new tenant cannot acquire or port the expected ACS numbers

Check eligibility before debugging the application. ACS telephony onboarding
restrictions and a 2028 transition have been announced. The ACS and Dynamics
guides currently state different new-customer cutoff dates; the
[deployment lifecycle gate](README.md#first-confirm-service-lifecycle-and-eligibility)
records both.

Confirm the tenant/resource history, number geography and supported scenario
with Microsoft. Successful creation of an ACS resource does not prove
phone-number acquisition is available. A Dynamics license, fresh Azure resource
or different application key is not a workaround.

If the intended services are unavailable, stop this deployment path and plan the
supported replacement. Do not silently substitute Teams Phone extensibility:
the custom native adapter has not been ported/validated against that path.

### Wrong subscription/tenant or unexpected application registration

**Symptom:** resources or Entra apps appear in the wrong tenant; Dynamics calls
fail despite Azure login succeeding.

**Check**

- `az account show` tenant/subscription.
- `azd` selected environment and its private environment file.
- Dynamics browser `WhoAmI`.
- Which identity requested the Graph/Dataverse token.

**Fix:** use the Dynamics tenant for the new Azure deployment and log both CLIs
into it explicitly. For non-ARM audiences, request a token with an explicit
tenant/resource. `az rest --subscription` is not proof that a Graph token used
the desired tenant.

The successful solution moved the application deployment into the Dynamics
tenant while preserving the old environment. It did not depend on the discarded
cross-tenant federation prototype.

### Region, model or Search creation fails

**Symptom:** subscription not allowed, region capacity exhausted, or model quota
unavailable.

**Fix:** check the actual error and choose supported regions before creating
resources. The tested deployment used Sweden Central for hosting and France
Central for Search because earlier regions were unavailable. Do not copy those
choices without checking today's quota/capacity, or modify an existing resource's
region expecting migration.

Do not re-enable local keys on protected AI/storage resources to bypass policy.

### First app deployment fails even though native media is disabled

**Cause to check:** an observer API key was imported without its resource ID and
either an expiry or explicit permanent mode. `NativeCallProbe` rejects partial
configuration. Permanent mode also rejects a nonempty expiry.

**Fix:** use `bootstrap-env` for the first deployment; use `runtime-env` only
after all native configuration inputs are available. Do not put the entire
generated control-key file directly into azd at the bootstrap phase.

### Provision succeeds but the service is still a hello-world page

Provisioning initially uses placeholder images. Run root `azd deploy` and verify
the actual voice `/health` response. `azd package` or an accepted ARM deployment
alone does not establish the running application.

Preserve azd's recorded image variables before subsequent provisioning.

## Power Platform CLI results

A Power Platform CLI (PAC) exit code is a hint, not the result. Each item below
is what PAC did in testing (version 2.14.1), followed by the readback that tells
the truth. After any PAC command that changes the cloud, read back the server
state before you retry. Retry once only with a concrete change; if it fails
again, stop and ask the owner. Command references:
[`pac copilot`](https://learn.microsoft.com/en-us/power-platform/developer/cli/reference/copilot),
[`pac admin`](https://learn.microsoft.com/en-us/power-platform/developer/cli/reference/admin),
[`pac application`](https://learn.microsoft.com/en-us/power-platform/developer/cli/reference/application).

- **`pac copilot publish`** exited 0 while printing "Failed to publish"; the
  agent's `synchronizationstatus` was Failed.
  **Readback:** use [`pac_publish.py`](#publish-an-agent-and-prove-the-result),
  which reads the agent row before and after.
- **`pac copilot push`** exited 0 but left the root settings unchanged.
  **Readback:** `native_voice_setup.py agent-baseline` patches the agent row
  with its ETag and reads it back.
- **`pac copilot pack`** succeeded while settings were missing or encoded
  differently from the public API.
  **Readback:** don't take settings from a packed solution. Apply them with
  `agent-baseline` and read the agent row back.
- **`pac admin assign-user --application-user`** failed with "The given key
  'name' was not present in the dictionary" after it had created the
  application user, so the role stayed unassigned.
  **Readback:** look the user up by its application ID
  (`systemusers?$filter=applicationid eq <client-id>`) before any retry. Use
  `native_voice_setup.py reader-role` (see `--help`) to create or find the
  application user and associate the role; it reads both back.
- **`pac admin create`** exited 1 with a `linkedEnvironmentMetadata` parse
  error after it had created the environment.
  **Readback:** `pac admin list` shows the environment and its URL; open the
  org URL. Don't create it again.
- **`pac admin create --templates D365_CustomerService,D365_ContactCenter`** is
  rejected: the two templates are exclusive.
  **Readback:** `pac admin list` shows that nothing was created. Create with
  `D365_CustomerService` only.
- **`pac admin list-app-templates --region unitedkingdom`** is rejected.
  **Readback:** use the tenant's macro-region values from environment
  discovery. Learn lists the known IDs under `pac admin create --macro-region`.
- **`pac copilot init --environment`** imports a solution and creates an agent.
  It's a cloud change, not local scaffolding.
  **Readback:** `pac copilot list --environment "$ORG_URL"` shows the new
  agent. For local files only, leave out `--environment`.
- **`pac application install`** for `mspva_TelephonyExtension` failed with
  "Cab file required".
  **Readback:** voice works without it, so don't retry. Check the agent's saved
  voice setting with `native_voice_setup.py verify`, not the catalog's
  Installed label.

### Publish an agent and prove the result

`pac copilot publish` is the supported publication path. The internal Dataverse
`PvaPublish` action is not a fallback. Run PAC through the wrapper, which reads
the agent row before and after:

```bash
python infra/scripts/pac_publish.py --tenant-id "$TENANT_ID" --org-url "$ORG_URL" \
  --bot-id "$BOT_ID" --receipt-dir "$WORK/publish-receipt"

python infra/scripts/pac_publish.py --tenant-id "$TENANT_ID" --org-url "$ORG_URL" \
  --bot-id "$BOT_ID" --receipt-dir "$WORK/publish-receipt" \
  --apply --confirm-organization "$ORG_URL"
```

The first command only reads. The second runs `pac copilot publish` once, then
reads the agent back for up to `--timeout-seconds` (default 300). It prints one
JSON line and keeps owner-only events in the receipt directory.

| `status` | What the readback showed | Next step |
|---|---|---|
| `published` (exit 0) | `publishedon` advanced, and the newest `lastFinishedPublishOperation` in `synchronizationstatus` is `Succeeded` | None, whatever PAC's exit code was. |
| `failed` | That newest operation is `Failed`. Its diagnostics name the component and error code. | Fix that cause, then retry once with `--retry-change "<what you changed>"`. After a second failure the script stops: ask the owner. |
| `unconfirmed` | No new result within the wait, a status value the script doesn't know, or the agent couldn't be read | Rerun the same command without `--retry-change`; it only reads back. If it stays unconfirmed, ask the owner. |

Learn documents `publishedon` and `synchronizationstatus` on the
[bot table](https://learn.microsoft.com/en-us/power-apps/developer/data-platform/reference/entities/bot),
but not the JSON inside `synchronizationstatus`. The script reads
`lastFinishedPublishOperation.status`, the shape seen in tested environments,
and shows any other shape as it read it instead of guessing.

## Native ACS connection and events

### `PhoneNumberControlProxy-getSubscriptionsFailed`

The original failure occurred while using an app designer/preview rather than
the actual Dynamics runtime. The installed control depended on its origin.

**Fix:** open the real admin app's `main.aspx?appid=...` URL. Verify Azure
subscription access independently before changing permissions. The exact
original host-versus-cookie cause was not captured, so do not diagnose every
instance of this error as the same issue.

### Native resource connects but webhooks/phone sync fail

Check these exact values:

- Native ACS resource name has no whitespace.
- Native ACS ARM resource ID is correct.
- Connection string belongs to that resource.
- Event Grid app client ID is the delivery app, not an object ID or UAMI.
- Tenant is the same tenant selected in Dynamics.
- Incoming/recording endpoints came from the current Dynamics connection and
  include all query parameters.
- Delivery app-role grants exist for Microsoft.EventGrid and the subscription
  writer.

A leading space in the saved ACS resource name caused a real failure. Fix an
incorrect connection through the supported disconnect/reconnect workflow, not
an isolated name-field patch that leaves webhook URLs inconsistent.

Disconnecting native ACS can require removing/recreating its subscriptions.
Plan that as a maintenance operation, retain exact definitions, and explicitly
verify restoration. Do not let intentional subscription deletion look like an
unexplained Azure failure.

### Event Grid webhook validation handshake failed

Do not remove Entra authentication. Check the audience/tenant/app-role bindings,
the exact endpoint and the writer's destination authorization. Propagation can
take up to 15 minutes.

Use one event subscription per type. Wait for **Succeeded**, then verify actual
phone/recording behavior. A configured connection does not create event delivery
automatically.

### Number sync says success but the number is missing

A sync response can mean accepted work rather than completed import. Inspect the
actual Phone numbers list for an **Active** record.

Do not use the generic Import status page as the native phone sync truth source.
Check resource connection, lease state/capabilities, native events and background
provisioning before repeating purchase or import.

### No exact native call locator

The observer must see **IncomingCall** on the native resource. The earlier
CallStarted/CallEnded observer was wrong for this PSTN/bot path.

Check:

- Observer enabled with matching lifetime modes: either an unexpired lease on
  both sides or permanent mode with no expiry.
- Dedicated secret delivery key matches the backend.
- Source resource ID matches exactly.
- Event correlation equals the phone context's `msdyn_callsid`.
- The event is still within the observer's 15-minute retention window.

Never decode an opaque locator, use the originating synthetic caller's unrelated
ACS correlation, or select the latest call globally.

## Copilot authoring

### Only the trigger appears, or a saved topic renders incompletely

The platform did not reliably render some generically serialized YAML.

The generator uses native-style indented action sequences and CRLF. It also
prevents line wrapping of Power Fx expressions. Use the generated files instead
of reformatting through an arbitrary YAML tool.
Repository attributes preserve the topic bytes during checkout, including CRLF
in the generated controller.

### `PowerFxError`, `Unexpected character`, missing HTTP response variable

A long dynamic URL was physically folded across lines by the YAML writer.
Generic YAML reassembled a valid value, but Copilot's authoring parser failed.
The resulting broken node also produced a misleading cleanup-reference error.

**Fix:** keep each formula on one physical line. Regenerate through
`CopilotDumper`, reopen the topic, and check again. Do not delete downstream
cleanup/handover nodes merely because the parser did not reach them.

### `Cannot reference variable from component native_cleanup`

First resolve any earlier syntax/schema error. A malformed HTTP node can create
secondary reference errors.

For a genuinely invalid branch, do not jump from a missing-context path into
actions requiring variables initialized later. The speech-turn controller reports
the missing ID locally and ends that topic before beginning HTTP work.

### `InvalidTransferPhoneNumber` after an unrelated edit

A generic YAML loader parsed `+441...` as an integer and discarded `+`.

Use strings for E.164 values and the schema-aware loader. The setup kit preserves
an original nonpilot external-transfer action as a string. The native
controller itself uses **Transfer to agent**, not a telephone number.

### Conversation ID is empty or invalid

The only valid parent ID is Dynamics' externally supplied
`msdyn_ConversationId`. Not a LiveKit room, ACS connection ID, Copilot conversation
ID, case ID or last-seen call.

Check that the global:

- Has the exact case-sensitive name.
- Is String, conversation scoped, external-input enabled, not an output.
- Is defined by a saved initializer in unused NativeMediaContext.
- Is not initialized again during Conversation Start or the media topic.

**Known root cause:** an executed `SetVariable` with `init:Global...` and `=""`
overwrote a correct externally supplied ID. Move it out of runtime.

Do not depend on a global-variable timeout setting for IVR synchronization.
Read the actual context; a missing value is an explicit error.

### Correct ID supplied, but Dataverse returns 404 / no open session

Dynamics can provide the context before its conversation/session/phone rows are
readable by the application.

The backend retries only recognized pending-visibility reasons within a bounded
window. It queries the unique open session by exact parent ID, not an
asynchronously updated last-session pointer.

Do not retry permissions failures, ambiguous results or changed ownership as if
they were normal eventual consistency. The 10-second context bound still shares
the 30-second overall setup deadline.

### Start returns 202 but the topic immediately stops

Do not treat `ContinueOnErrorBehavior.statusCode` as a success status output.
It can be unset on success.

The controller initializes a fresh typed result and checks the returned
conversation ID/action. It does not infer failure from a missing error variable.

### `ValueParsingError`

Compare the actual JSON against the node's declared Record schema. A missing
reason is `""`, not JSON null. All response reset records and schemas must include
the same control fields.

Do not return a success-shaped default when parsing fails. Preserve the failure
and clean up explicitly.

### `TelephonyBotNotResponseWithMessageBack` or generic spoken error

The voice protocol expects a valid response for the turn. The controller
includes a real setup announcement before native asynchronous HTTP work.

Inspect saved HTTP latency settings. Do not leave "Send a message" selected with
an empty message. Conversely, an empty latency protocol activity in a trace does
**not** prove that checkbox is still enabled. Check the saved definition and
reopen the editor before changing it again.

### Language variable rejects `ar-SA`

`System.User.Language` is a native **choice**. Select Arabic or English.
Generated YAML uses `OptionDataValue` with `SystemOptionSet: Locale`.

The channel still uses `ar-SA`/`en-US` locale codes. Variable type and channel
locale are different layers.

### A timeout value appears entered but did not save

The native UI once failed to persist the speech recognition timeout until the
field lost focus and the topic was saved again.

Reopen and inspect **saved YAML**. Expected field:
`speechRecognitionTimeoutInMilliseconds: 1000`. Do not assume the canvas
display proves persistence.

### A silent listener hangs up the caller

Do not combine a short silence timeout with the default **Hang Up** fallback.
Use zero reprompts and **Set variable to empty**, with `defaultValueOnSilence:
=Blank()`, as in the generated node.

Do not add a Boolean yes/no question to the live controller: it can speak a
menu. The generated node uses `StringPrebuiltEntity`, a silent speech prompt,
and disables switching to other topics.

### Stale editor / ETag conflict

Stop editing from the stale canvas. Close it, refresh and reopen before saving.
Regenerate a bundle against the new saved state.

Do not remove concurrency guards, force the old file over someone else's edits,
or publish partially updated topics. A bundle's `before.json` is private recovery
material, not permission to revert all future changes.

## Native voice diagnostics

Use these steps in order when AI speech is missing from Dynamics transcripts or
a native call misbehaves. Each step proves one boundary; don't skip ahead.

Portal steps below were last verified in November 2026. If a screen differs,
stop and ask the owner for a screenshot that includes the left navigation, with
no secrets showing.

### 1. Gate the voice pipeline

```bash
python infra/scripts/contact_center_setup.py pipeline --tenant-id "$TENANT_ID" --org-url "$ORG_URL"
```

The gate reads `msdyn_infracreatedby` on the newest voice conversations in the
[Conversation table](https://learn.microsoft.com/en-us/dynamics365/developer/reference/entities/msdyn_ocliveworkitem);
`--help` explains it. Continue only when it exits 0 with `"status": "elastic"`.
Stop on any other result; with no voice conversation yet, place the one call
its output asks for, then rerun. In testing, HUMAIN's speech was saved in 47 of
48 transcripts on `Elastic` and in none of 14 on `CTQM3`, and matching the
voice-channel settings didn't move a `CTQM3` environment to `Elastic`. Learn
marks the column as internal, so treat its value as an observation, not a
contract.

### 2. Set up conversation diagnostics

Conversation diagnostics export each conversation's lifecycle events to Azure
Application Insights. Learn:
[configure conversation diagnostics](https://learn.microsoft.com/en-us/dynamics365/customer-service/administer/configure-conversation-diagnostics),
[export to Application Insights](https://learn.microsoft.com/en-us/power-platform/admin/conversation-diagnostics-application-insights),
[export prerequisites](https://learn.microsoft.com/en-us/power-platform/admin/set-up-export-application-insights).
Do the six steps in order; each has its own check.

**Step 1: Managed Environment.** The export works only from a
[Managed Environment](https://learn.microsoft.com/en-us/power-platform/admin/managed-environment-enable),
which needs premium licenses. With the owner's approval:

```bash
pac admin set-governance-config --environment "$ORG_URL" --protection-level Standard
```

Check: in the Power Platform admin center, open **Manage** > **Environments**
and the environment's **...** menu. It offers **Edit Managed Environments**,
not **Enable Managed Environments**. *Last verified: November 2026.*

**Step 2: Application Insights resource.** Use one resource for this
environment only. Learn says the export needs local authentication on it, so
don't disable local authentication there; keep it separate from the voice
app's own telemetry.

```bash
az monitor app-insights component create --app "$DIAGNOSTICS_APP_INSIGHTS" \
  --resource-group "$AZURE_RESOURCE_GROUP" --location "$AZURE_LOCATION" \
  --kind web --application-type web --workspace "$LOG_ANALYTICS_WORKSPACE_ID"
az monitor app-insights component show --app "$DIAGNOSTICS_APP_INSIGHTS" \
  --resource-group "$AZURE_RESOURCE_GROUP" --query "{appId: appId, id: id}"
```

Check: `show` returns the `appId` (step 4 needs it) and the resource `id`.
Learn: [`az monitor app-insights`](https://learn.microsoft.com/en-us/cli/azure/monitor/app-insights).

**Step 3: Data export.** In the Power Platform admin center, open **Manage** >
**Data export** > **App Insights** > **New data export**. Name the package,
select **Dynamics Customer Service** as the source, select the managed
environment, then the subscription, resource group and resource from step 2.
Review, then select **Create**. Check: the export is listed on the
**App Insights** tab. An environment can have only one Customer Service export.
*Last verified: November 2026.*

**Step 4: Query identity and permissions.** In the Copilot Service admin
center, open **Operations** > **Insights** > **Observability settings** >
**Conversation lifecycle diagnostics** > **Manage**. Enter the `appId` from
step 2 in **Application Insights ID**, select **Provision**, and copy the
third-party application name it shows. That application reads the telemetry.
In Microsoft Entra ID, its Application Insights read permission must show
**Granted**; an administrator grants consent if not. *Last verified: November
2026.*

Disabled sample GUIDs and the prerequisite checkboxes on this page look like
completed setup, but they aren't. A sample GUID is a placeholder, not a saved
value, and a checkbox only records your confirmation. Only steps 5 and 6 prove
the setup.

With approval, give that application **Monitoring Reader** on the resource,
then read the assignment back:

```bash
DIAGNOSTICS_QUERY_APP_ID=$(az ad sp list --display-name "$DIAGNOSTICS_QUERY_APP_NAME" --query "[0].appId" -o tsv)
az role assignment create --assignee "$DIAGNOSTICS_QUERY_APP_ID" --role "Monitoring Reader" \
  --scope "$DIAGNOSTICS_APP_INSIGHTS_ID"
az role assignment list --assignee "$DIAGNOSTICS_QUERY_APP_ID" --scope "$DIAGNOSTICS_APP_INSIGHTS_ID" \
  --query "[].roleDefinitionName" -o tsv
```

**Step 5: Connection test.** On the same page, select **Test connection**.
Check: a message says the service can read telemetry from Application
Insights. If it fails, fix the cause it shows and test once more; then ask the
owner. *Last verified: November 2026.*

**Step 6: Events.** Place one test call, or use a recent conversation. Learn
allows up to 24 hours for delivery. Then count the diagnostics events:

```bash
az monitor app-insights query --apps "$DIAGNOSTICS_APP_INSIGHTS" \
  --resource-group "$AZURE_RESOURCE_GROUP" --offset 1d --analytics-query \
  "traces | extend cd = parse_json(customDimensions) | where tostring(cd['powerplatform.analytics.scenario']) == 'ConversationDiagnosticsScenario' | summarize events = count() by subscenario = tostring(cd['powerplatform.analytics.subscenario'])"
```

Check: rows appear per subscenario. Then, in Copilot Service workspace, open
**Conversations**, select the conversation and select **Diagnose**.
*Last verified: November 2026.* More queries:
[sample queries](https://learn.microsoft.com/en-us/dynamics365/guidance/resources/conversation-diagnostics-sample-queries).

### Contingency: known-clip microphone diagnostic (not shipped)

This diagnostic is designed, not built. Build it only if the pipeline gate
reports `Elastic` and AI speech is still missing from saved transcripts.

In one consenting test call, it plays a known clip (fixed words, language,
length and checksum) through the native microphone path HUMAIN uses: the ACS
Calling SDK microphone participant, with HUMAIN not connected. Then it compares
the call recording with the saved transcript.

| Clip in the recording | Clip words in the saved transcript | Meaning |
|---|---|---|
| Yes | Yes | The microphone path and transcription work. Look at HUMAIN's audio and timing. |
| Yes | No | Audio present, transcript missing: native transcription dropped it. Take the conversation ID to Microsoft support. |
| No | Either | The clip never reached the call. Check the media path: device, stream selection and media readiness. |

Before it may place a call, it needs:

- caller checks: an approved test caller who consented, and a call-length cap;
- tenant and resource ownership checks: the organization, workstream, native
  ACS resource and recording belong to this deployment's tenant and receipts;
- a restore plan: record every setting it changes, restore each one after the
  call, and read them back.

Related lesson: [HUMAIN is audible in the recording but absent from the
transcript](#humain-is-audible-in-the-recording-but-absent-from-the-transcript).

## Audio, transcription and handover

### HUMAIN is audible in the recording but absent from the transcript

Application-injected Call Automation audio was audible and recorded but not
included as expected in native transcript text. Forwarding actual provider text
into silent Copilot Message nodes did not repair it.

**Fix:** publish HUMAIN PCM as a separate **ACS Calling SDK
microphone participant** on the native call. `voip` is the required transport
for this runbook.

Verify HUMAIN's words in the native transcript during the call and after
closure. Recordings, provider text and Copilot messages test different outputs.

### `Acs_Unknown` appears

The workspace is showing a distinct ACS identity but not the supplied friendly
name. A display name set in Calling SDK did not automatically become the native
transcript header.

This is accepted while caller, AI and representative remain distinct.
Do not write fake native participant records or rewrite transcript speaker IDs.
Raw Customer-role classification remains an analytics caveat.

### Raw transcript participant IDs or roles appear merged

The formatted export did not always represent the same distinctions as the
workspace. Some raw views label both external speakers Customer.

Inspect actual visible headers and audio in the native workspace. Do not infer
successful or failed diarization solely from one repeated `ParticipantId`.

### Arabic becomes English-looking phonetic text

Changing the workspace display language or supplying Arabic text is not enough.
The native transcription language must change.

Failed approaches in this topology:

- Only changing Copilot speech-language selection before a long HTTP loop.
- Disabling DTMF-only mode without native listening turns.
- Separate ACS captions `setSpokenLanguage`; captions reported Arabic but the
  native Dynamics transcript stayed English.

**Fix:** a silent native Question turn applies the dialog/transcription
language. The controller acknowledges that completed turn to the backend, and
the trusted HUMAIN tool waits before using the new language.

The native workspace showed language-alignment notifications, Arabic words and
distinct speaker headers during the acceptance calls.

### English request causes an old-language reply

A compound spoken request can create another provider turn while a language
tool waits. A text-only simulation missed this race.

The current controller uses short native housekeeping turns and an explicit
tool result naming the response language. It also requires new finalized input
from the verified caller stream before changing language.

Keep the initial-language policy and post-tool policy aligned. Do not shorten
HUMAIN's input window or drop audio to hide the race.

### The quiet Question captures HUMAIN's own speech

The recognizer can hear both caller and HUMAIN. The controller discards that
mixed result. `/turn` accepts only language/revision and rejects text/intent
fields. Keep handover, topic interruption and language decisions off this path.

### Handover adds another telephone or creates a second conversation

You are probably using the legacy ACS handover or external Transfer to phone
number path.

The native implementation:

1. Signals the retained controller.
2. Pauses AI output/tools.
3. Removes only its microphone/control legs.
4. Confirms departure.
5. Returns `handover_ready`.
6. Lets Copilot execute **Transfer to agent**.

No public operator phone number is supplied to this tool. A conference participant
invitation is not an equivalent native queue handover.

### External transfer works, but returning to a human fails

The old external transfer ended the Copilot session (`BLIND_TRANSFER`).
Calling native assignment/supervisor-transfer APIs afterwards produced:

- "No active agent found for the conversation."
- "Item is not available for picking."

Do not grant the app broad assignment privileges to work around an ended
controller. Keep Copilot alive and use its native transfer node.

`BotTransferSession` can also describe normal bot-to-human handover. It is not
proof that an external telephone transfer occurred.

### AddParticipant returns 403 / 7602, identity belongs to another resource

Create the disposable VoIP user on the **native Dynamics ACS resource**, not a
separate diagnostic ACS.

Keep the native incoming Event Grid phoneNumber filter so Dynamics does not
answer that user invitation. The successful fix required no new number, Teams
service or carrier.

### SDK is Connected but no speech reaches Dynamics

Signaling readiness is not media readiness. An early prototype reported
Connected, one local stream and completed source playback without transmitting
useful audio.

The working browser initializes software devices, selects the custom audio
stream, confirms the exact call and waits for RTP/media readiness before audio.
First samples sent too early were clipped in the experiments. Do not remove
those gates to improve a benchmark.

### Setup randomly fails before HUMAIN starts

Use `/timings` to locate the stage. Earlier fixed eight-poll ACS and five-read
context limits sometimes expired too soon. The current implementation uses
bounded wall-clock windows inside the original overall setup limit.

Do not count a timeout ceiling as fixed latency, and do not silently retry a
new call. Confirm that the previous one is closed before re-arming.

### Long silence after asking for a human

Measure the entire chain:

1. End of the actual spoken request.
2. Provider tool invocation.
3. Application media departure.
4. Native transfer session creation.
5. Queue assignment.
6. Representative acceptance.

The original long silence followed a first interrupting request that the provider
did not act on;
the repeated request was handled quickly. It was not a hidden 20-second wait
after the tool. Later tests handled one interrupting request in about 4 seconds
to native transfer.

Locate the delayed stage before changing polling. Measure representative
acceptance separately from the handover request.

### Native transcript missing after hanging up

Close the Dynamics phone session's wrap-up tab. Wait for the final file to be
generated. `msdyn_commontranscriptmessages` can be empty while voice transcript
files exist elsewhere.

Files are on native `msdyn_transcripts` / `msdyn_ocrecordings`; a Copilot execution
trace is a different artifact. Never edit final transcript files to fabricate
coverage.

### A normal-speed recording message plays before the fast one

Compare the native HTTP action's saved `latencyMessage.text` and
`latencyMessage.speak`, then the actual emitted voice activity. In the reproduced
case, native latency playback synthesized the text at normal speed despite an
audio reference in `speak`. The application subsequently played the correct
1.3x clip during provider preparation.

Read-only comparison with the first post-1.3x call on 29 October showed that
the normal-speed native speech was already generated then. The interval from
that latency activity to the next native listening message was approximately
0.65 seconds, compared with 8.45 seconds in the doubled-notice call on
31 October. This is consistent with a short wait masking the unwanted native
speech and a longer wait exposing it; the trace alone does not establish how
much of either announcement the caller heard. There is no evidence that a
second, normal-speed WAV was introduced.

The first attempted correction supplied audio-only SSML to both latency fields.
It passed publication/configuration checks, but later real calls emitted empty
latency activities with no audio attachment. That correction regressed audible
startup; do not report it as successful based on publication alone.

The later regular-message attempt also emitted the text as normal-speed TTS.
The user-created native Speech & DTMF sample instead saved a latency message
with **only `speak`**, and no parallel `text` field. The preceding native-owned
renderer follows that exact shape, retains the ordinary silent handshake and
waits for provider preparation, intending native playback to cover startup.
The subsequent regular-route test failed notice acceptance: HUMAIN connected
in approximately 13 seconds, but the caller heard no notice and the native
latency activity had empty speech. Application notice playback is not active
in that mode. The earlier application's successful WAV playback is not proof
that native-only WAV playback ever worked.

The approved correction restores the application as the sole player of the
unchanged 1.3x WAV while retaining prepared parallel startup. It disables the
native HTTP latency announcement, starts the static clip only after exact
caller/call validation, and still waits for CRM before inviting HUMAIN.
Native language acknowledgment and confirmed application-notice cancellation
precede fresh ownership checks and AI audio release. Use the narrow
`upgrade-application-startup` migration; do not roll back CRM, Pause AI or the
parallel-preparation work. The speed adjustment was not the duplicate-notice
cause. A subsequent authorized call confirmed audible playback and a smooth
transition; its native trace had no recording prose/audio tag or MessageBack
error. The caller still reported approximately six seconds before the notice.
Do not treat successful playback as proof that the initial delay is eliminated.

Anonymous GET and HEAD requests to the existing content-addressed URL returned
HTTP 200 and `audio/wav`, without redirects. This verifies access from the
checking client, not from the native renderer; it does not establish a need
for a different host. Do not change storage, authentication or networking
solely because native playback is silent. Do not remove human wait/hold music,
weaken context validation or add a serial full-clip wait. Changing announcement
ownership is a separate, explicit deployment decision, not a silent fallback.
The source environment's application-only change was explicitly approved.

For an existing supervision-owned controller, use the
[narrow notice upgrade](supervision-setup.md#upgrading-an-existing-startup-notice),
publish the exact revision and verify the actual runtime identity/pins before
re-enabling. Publication/configuration readback is not a listening test.

### The phone is connected but silent before the notice

Separate answer-to-notice from dialing/ringback and notice-to-HUMAIN. In the
two reported calls, the native latency activity contained empty text/speech and
only timing metadata. The first call exhausted the ten-second verified-context
window with `native_session_pending`; no application audio leg was established.
The second resolved context in 6.51 seconds, began the application clip 8.806
seconds after backend startup, and reached active AI at 14.045 seconds. The
caller reported 12-13 seconds of silence after their phone timer started.

Do not treat a healthy `/readiness` response or a later correct transcript as
proof that startup audio works. Preserve both failed and successful attempts.
The current application-owned prepared-start route passed playback/transition
acceptance, but first-notice latency remains a separate measurement. The
underlying native-session visibility deadline still needs observation and must
not be bypassed by choosing another conversation.

The existing ACS summary export later supplied the three calls' PSTN participant
timestamps. Query by `CallStartTime` as well as an adequate `TimeGenerated`
window: the observed summaries arrived hours after their calls. They showed
approximately 1.8-3.6 seconds from the positive-duration **Application Inbound**
PSTN participant's start to the bot starting, before application setup. The
participant timestamp is only precise to a second and is not the handset answer
clock. Do not claim that a faster application eliminates this native interval,
or enable unrelated analytics experiments to fill the missing measurement.

### The application notice works but consistently begins after about six seconds

Do not assume a hidden six-second timer, WAV speed problem or scale-to-zero.
Two post-fix calls showed this breakdown, measured from the first native bot
activity rather than handset answer:

| Stage | Earlier accepted call | Later captured call |
|---|---:|---:|
| Native activity to backend invocation | 1.400 s | 1.686 s |
| Backend to verified native context | 1.250 s | 2.721 s |
| Context to verified ACS caller | 1.475 s | 0.900 s |
| Verified caller to playback-start callback | 0.742 s | 0.456 s |
| Total from first native activity | 4.867 s | 5.763 s |

The later call initially returned HTTP 404 for the exact conversation, then no
open native controller twice. The accepted implementation retried every 500 ms:
its context resolver spent approximately 0.800 seconds on eleven reads and
1.505 seconds between them. This establishes API visibility waits, not whether
the underlying row was uncommitted, not yet visible, or awaiting access changes.
Do not infer early readability from a later successful read or `CreatedOn`.

The recording contained sustained energy from about 0.1 seconds, both calls
used warm SDK pages, and the application already kept a replica running.
Connection callbacks arrived roughly 0.22 seconds before the polling loop
detected readiness. Those are optimization candidates; not all retry time is
necessarily reclaimable, and removing database waits alone does not establish
a two-second answer-to-audio result.

Keep all caller, workstream, controller uniqueness, ownership and cancellation
guards. Optimize independent read scheduling and readiness detection, not the
authorization requirements. Follow the
[working-baseline procedure](operations.md#preserving-a-working-startup-before-optimization)
and verify that provider preparation is not made slower.

A system connection greeting is not a no-delay substitute: Microsoft's
[AI-agent guidance](https://learn.microsoft.com/en-us/dynamics365/customer-service/administer/configure-bot-best-practices)
says the voice agent is engaged after that uninterruptible greeting plays.
The [native wait-music facility](https://learn.microsoft.com/en-us/dynamics365/customer-service/administer/voice-channel-music)
is a different coverage candidate, but its exact start/stop boundaries on this
hybrid route need separate measurement. Do not introduce a timed greeting or
change workstream-wide music settings as part of an image-only optimization.

### A native audio-only probe immediately plays an error

The captured error was `TelephonyBotNotResponseWithMessageBack`, not an HTTP
permission error or a proven bad WAV. The isolated topic had only a latency
action and then ended its dialog without an ordinary voice response or a
conversation-ending/handover action. Native traces recorded the latency protocol
activity, then the platform error. That test did not establish audio acceptance.

Keep the regular route's known ordinary speech handshake and native listening
turns. Do not copy the isolated topic's termination structure into the live
controller. A saved editor definition is evidence of serialization, not proof
that a complete voice conversation meets the native response protocol.

### A stopped startup leaves the caller in silence

The first supervision implementation replaced the general failure tail with
unconditional quiet hold. That protected paused calls but also hid a known
`native_session_pending` stop before AI had ever become active.

The corrected backend returns the positive `startup_failed_unpaused` signal only
after a completed, detached, pre-active stop with no intervention and no
moved/unconfirmed ownership. The native controller checks that flag and the exact
conversation/contract before speaking the original failure message. Missing
responses, active/past AI phases, actual pause and uncertain ownership remain
quiet. Never replace the quiet guard with unconditional error speech.

### A case question unexpectedly transfers to a human

Read the exact backend stop reason before changing model prompts. In the
reported case it was `pilot_duration_limit`, not `customer_requested_human`.
The original 120-second timer caused this handover after AI became active.
The approved source limit is now 1,200 seconds (20 minutes), with the separate
controller watchdog raised from 300 to 3,000 cycles. Expiry still performs normal
native handover; elapsed caller speech, silence and tool waits all count.
Deploy the updated voice image and publish the matching controller composition.
Confirm `pilot_limit_seconds: 1200` in deployed health/status and the published
3,000-cycle condition; source edits alone do not change live calls.
The monitor's 120-second caller cap does not change that policy or automatically
hang up. Keep the safety bound unless a different deadline/expiry experience is
explicitly approved.

## CRM and representative workspace

### Chat reports "not recorded", but a Dynamics case exists

Compare the real `ticketnumber`, deterministic request key and customer-bound
operation lookup before retrying or changing the data. In an acceptance test,
the native case existed with the correct category, district, street and landmark,
but the original create response exceeded the transport deadline. An immediate
lookup was still missing while the commit outcome was unconfirmed. A later
specialist retry hit the model's token rate limit rather than issuing another
Dataverse write.

The shared service now performs bounded read-only reconciliation of case/note
creates and native links. It never substitutes a new ID or retries an uncertain
write. A result still absent at the deadline remains explicitly unknown.
Voice/chat tools retain the same operation reference, and their instructions
forbid calling that "not recorded." The chat coordinator calls the shared CRM
tools directly instead of requiring an additional model hop for transactions.

Incident location is stored in the custom case columns `moacc_district`,
`moacc_street` and `moacc_landmark`, with `moacc_category` for the category.
A stock case form not displaying those columns does not prove they were omitted
from the write. Inspect the actual record and the selected form separately.
The workspace recipe clones a nondefault case form with all four controls,
pins the owned case routes and registers only that new form with the app.
Reload/reopen the scoped tab; an already-open stock form will not gain controls
just because the columns exist.

### Custom table creation returns 429 / `0x80071151`

Inspect the native error before treating every HTTP 429 as ordinary request-rate
throttling. During supervision setup, `EntityCustomization` was blocked by a
separate solution Import. Read-only `importjobs` observations showed a progressing
Power Pages update; the new command table had not been created.

Do not cancel another import, bypass the customization lock or start a replacement
deployment namespace. Retain the immutable plan and its real completed records.
Observe the import with a bounded wait, then repeat the call/wrap-up idle check
before resuming the same unchanged stage. If the lock remains, stop explicitly
with the current state and safe resume point.

The source attempt created only its owned publisher and solution before stopping.
No controller, routing, existing role or workspace binding was changed, and
supervision stayed disabled. A lock rejection is not permission to assume that
all later stages completed or to discard ownership receipts.

### Case lookup succeeds, but the native case link returns HTTP 400

Do not assume the case disappeared or that handover removed an existing link.
Compare the case receipt's `conversationLink` stage/status with the actual
conversation lookups and timestamps.

One reproduced failure was native code `0x80040224` while loading
`Microsoft.Dynamics.OmnichannelPrime.Plugins`. The CRM app user could read the
case and write activities, but lacked organization-level
`prvReadPluginAssembly`. Reading that assembly's metadata as the same user
returned `0x80040220`, naming the missing privilege.

Adding only the assembly-read privilege made the same ETag-protected update
succeed on an isolated non-telephony activity; customer/case readback was
verified and the temporary activity removed. No plug-in write or administrator
permission was needed. Role changes can seed unrelated SharePoint defaults:
compare the effective role before/after and remove only reviewed new extras.
The [CRM setup kit](crm-setup.md) and shared-service readiness check include
this prerequisite.

### Case Details is empty even though HUMAIN described a case

The customer relationship, the list of customer cases and the conversation's
one selected case are different things. Inspect `_msdyn_issueid_value` separately
from `_msdyn_customer_value` and generic `_regardingobjectid_value`.

The adapter selects a case from a validated successful case action. An answer
drawn only from prefetched context can omit that action and leave the native
case association empty. A timed-out association PATCH is also uncertain: a
test returned HTTP 504 while its final native record later contained the link.
Bounded read-only reconciliation now handles that outcome, while a result still
absent at the deadline remains explicitly unconfirmed. Prefetched selection and
actual UI association still require acceptance; the earlier permission repair
does not prove every handover is linked correctly.

A later test provided stronger evidence: the backend read back the correct
IssueId before transfer, and the actual native handoff carried the correct
`CaseNumber` and selected-case receipt, yet the representative reported an empty
Case Details panel. The installed enhanced form has a visibility branch that
shows its IssueId-bound card only when Regarding is nonempty and differs from
Customer. Native template `caseId` hydration also uses native context items, not
the handoff case number alone.

The shared service now keeps the public case-typed Regarding association aligned
with IssueId and preserves the contact in Customer, using the same conditional
write and verified-caller guards. Unrelated Regarding records or concurrent
human case selections are not overwritten. The public navigation contract was
verified on a temporary non-telephony activity under the CRM application user.
The internal `CreateUpdateLWIContextItems` custom API is private and is not
invoked as an application workaround. Actual future panel rendering remains a
UI acceptance observation; do not claim that a previously verified link was
erased at handover without before/after evidence.

Use the read-only monitor's `--case-links` timeline and compare the selected
receipt, actual native lookups and representative acceptance. Never infer
linking from an opened case tab, and never alter a closed test conversation to
manufacture a passing result.

### Recent Cases has records, but the contact's Related Cases grid is empty

Inspect the form's actual saved view and relationship query under the
representative identity. The stock **My Active Cases** view includes
`ownerid = current-user`; application-owned cases therefore disappear even when
the representative has Read access. Its active-state filter also excludes
resolved cases.

The successful fix was **All Cases** with the existing current-contact
`customerid` relationship filter. It used a nondefault cloned contact form,
scoped template/macro pins and one app form-component registration, preserving
shared views, case owners and other users' defaults. Do not populate unrelated
primary-contact fields or reassign all cases to disguise the view filter.

The workspace app explicitly allowlists forms. Creating a clone alone is not
enough: register it with `AddAppComponents`, publish only the reviewed app
change, then reload the workspace and reopen existing Customer tabs.
See the [workspace setup chapter](workspace-setup.md).

### Find existing cases is empty, or New case shows a red X

An unconfigured native incident-list tab can fall back to **My Active Cases**,
with Owner: Me. Use the owned route to the current customer's corrected
**All Cases** subgrid; do not broaden a shared view or change case ownership.

The observed New case runs resolved customer ID/type/name correctly and passed
their guard, but failed in native `Create_New` navigation. No precise browser
exception was retained, so missing permissions or bad customer parsing were not
established causes. The replacement uses supported Entity Record new-form
templates, `createFromEntity`, an empty data object and the scoped form pin.
Native `InitializeFrom` returned the correct customer binding without saving a
record. Opening, abandoning and deliberately saving the browser form remain
separate acceptance observations.

### Link confirmed saved case shows a red X after hanging up

Inspect the native macro's guard and action statuses. In the reported run the
guard was false and the link action was **skipped**; a later fallback lookup
failed. That does not establish a rejected Dataverse link write or identify
which guard clause failed.

Linking is live-conversation-only and requires the intended saved, focused
case. Test it before hanging up, after capturing automatic linking's result.
Do not remove those guards to repair historical conversations. The refresh
step's visible label is **Refresh saved case/tab**; "Refresh focused record"
is the internal macro name, not a second missing command.

### Native Knowledge search is empty, but Copilot citations work

Check the native search's **Visibility** preference. All provisioned articles
are internal; the native External-only default excludes them. The investigation
verified real English/Arabic search results under the representative identity,
so missing indexing or Read access was not the cause.

Set Internal or Both only for the approved representative. Keep Published and
the existing language preference. Do not make the articles public, publish
unrelated drafts, or change global defaults to hide a personal filter problem.

### A conversation summary seems absent, or a case has too little information

Open the completed record through **Activities > Conversations > Summary**.
The inspected modern summary was stored in
`msdyn_conversationinsight.msdyn_copilotsummary` and included both AI and human
content even though the legacy conversation summary field was null.

Case summaries are a different surface. The reported new case had a five-word
description, no annotations and no regarding activities. "Not enough
information" was consistent with those sources; the existing custom location
columns were not in the native case-summary mapping. Do not invent description
text or silently change shared mappings. See
[summary inspection](operations.md#where-to-inspect-summaries).

### Representative Copilot answers a case question from the knowledge base

Published knowledge is not the customer/case database. The native representative
Copilot does not automatically inherit HUMAIN's shared CRM tools. Case form
summaries and timeline data are also distinct from free-form case querying in
the side pane.

Confirm the intended feature/source configuration. The tested workspace enables
knowledge Q&A and case summaries; live-case query extensibility remains separate.
Do not publish private case histories as general knowledge to work around that
boundary, and do not enable unrelated consent or cross-region flags.

### Arabic articles exist, but Arabic agent scripts cannot be created or selected

An installed language pack is not necessarily provisioned. LCID 1025 script
creation failed with HTTP 400 / `0x8004432f` before Arabic was available.
The successful sequence used documented `ProvisionLanguageAsync`, waited for
the native operation to succeed, and verified both LCIDs 1033/1025 before
creating the Arabic variants.

Native script selection follows the representative's UI language. Bilingual
text on an English-LCID script is not an Arabic script variant. Preserve the
representative's current UI/help language and use separate localized redirects.
Do not disable Activity Feeds or use private/synchronous language APIs as a
shortcut.

### The Copilot knowledge source stays Configuring

Inspect native provisioning status and its exact error. In the source
environment, an existing managed status-check flow was off with an unbound
Dataverse connection. The native `msdyn_CheckDVCopilotStatus` action confirmed
that the knowledge source was actually ready.

Do not assign a Ready value manually, repeatedly toggle the source, or reset
unrelated analytics flags. A successful current status check is not a repair of
disabled periodic status automation; record that maintenance separately.

### The CRM controller fails native Power Fx validation

Native Power Fx rejected an unescaped trailing hyphen in a case-number regex
character class, although Python/JavaScript accepted it. The renderer now emits
the escaped literal hyphen. Regenerate with `--crm-case-context`, run the native
topic checker and inspect the actual publication result; a local regex test or
successful draft save is not proof of a valid native publication.

### Supervisor Join works, but HUMAIN keeps answering the customer

This was observed in the AI phase. The human joined the native conference while
the verified customer-only stream continued reaching HUMAIN. Operator speech
remained excluded, and joining did not update HUMAIN's human-presence state.

Monitor and Join are not the same intervention. Even Monitor can have a native
participant `joinedon`, so do not trigger AI takeover from participant counts or
that timestamp alone. Pausing needs input/output/new-tool gates, acknowledged
queue flushing and a separate lifecycle state. The generic stop/transfer path
can detach media or requeue a human who is already present; the 20-minute AI
deadline also needs explicit paused-state handling.

### Pause commands stay Processing despite successful native writes

Dataverse normalizes an empty text value to null. The command claim clears its
optional reason field; comparing an empty string request with a null native
readback previously left the outcome unconfirmed even though the claim persisted.
The native PATCH representation also omits null-valued fields even when a
subsequent GET includes them. The consumer explicitly writes null for an empty
reason and performs a selected-field GET when the PATCH result is partial.
Every requested field must then be present and match; omission is not assumed
to mean null. No replacement write is submitted. Verify command processing
against the real service, not only mocks that preserve empty strings or return
complete PATCH records.

### Enabling supervision makes the voice worker fail startup

The feature deliberately verifies its schema and pinned native controller before
starting the consumer. Those checks run as the voice managed identity, not the
setup administrator. The actual metadata query requires organization-level
`prvReadEntity`, `prvReadAttribute` and `prvReadRelationship`; table-row Read and
Write alone are insufficient. Native denials name the missing privilege.

Restore feature-off settings and confirm actual HTTP health first. Apply only
the documented read-only permission upgrade, then run the
[in-container preflight](supervision-setup.md#actual-runtime-preflight-before-activation).
Do not grant System Administrator, weaken the schema check or infer application
health from the Container Apps revision's Ready status. Persist enable settings
only after the real HTTP/native readiness checks pass.

## Dashboard says you need the Supervisor role

1. Confirm the browser identity via `$ORG_URL/api/data/v9.2/WhoAmI`.
2. Check direct/inherited roles for that exact user and business unit.
3. Assign the existing built-in **Omnichannel supervisor** role if absent.
4. Preserve the user's other roles.
5. Refresh the whole Dynamics page, not just the dashboard's Refresh All.
6. If still denied, test a private browser session with the same user.

The source environment initially lacked the explicit role. Adding it was
necessary, but the old session continued to show the error. A fresh private
session worked. No broader permission reset or extra application role was needed.

Use the working window for the call, and close the competing old workspace.

## Telemetry is empty

- Query the correct workspace and the resource-specific
  `ContainerAppConsoleLogs` / `ContainerAppSystemLogs` tables.
- Verify real rows, not just table schema.
- Check event time and ingestion time; several minutes' lag is expected.
- Verify the voice environment uses Azure Monitor plus its diagnostic setting.
- Keep workspace local authentication disabled. The older shared-key
  Log Analytics destination produced no retained rows in this environment.
- Do not enable HTTP diagnostics or verbose payload capture on media URLs.

The repair was validated by a harmless health marker **and** real
`VOICE_LATENCY` stage records. It did not weaken authentication policy.

## Outbound remains disabled

The native proactive outbound workstream rejected ACS Direct Offer numbers.
An application `/call` request is ordinary ACS calling, not native Dynamics
proactive calling.

Teams Phone / Direct Routing / carrier choices have their own licenses,
eligibility and infrastructure. They are outside this template's scope.
Do not add an SBC, purchase a carrier plan, or enable outbound as an incidental
"fix" for inbound deployment.

## How to add a new lesson

For every new failure, record:

- Exact symptom and failing boundary.
- Safe, bounded reproduction with call/cost limits.
- Evidence distinguishing competing explanations.
- Minimal change and preserved behaviors.
- Unit/contract check plus native acceptance result.
- Known limitations and rollback.

Update the runbook, relevant setup helper/example and regression tests in the
same change. Distinguish **implemented and tested**, **configured and read
back**, and **accepted in a real UI or call**. A private session receipt
is evidence, not a prerequisite another team must somehow recover.
