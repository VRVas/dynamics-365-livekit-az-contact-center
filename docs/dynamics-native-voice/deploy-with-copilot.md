# Deploy the selected Dynamics-native modules

Use this sub-workflow when `native-voice` is selected, after
[the deployment guide](../deployment.md) has prepared the private workspace.
Run it from the private prepared workspace. Don't copy the full source service
catalog over its selected `azure.yaml`.

Bicep, azd and the guarded native setup tools do the work. The
[native runbook](README.md) has the portal paths and complete commands; read
the relevant chapter before running a stage. The deployment guide's
[working rules](../deployment.md#working-rules) apply throughout: CLI first,
read back after every write, one approval for routine work, and stop after the
second failure. In the commands, `python` means the deployment interpreter
(`$PYTHON`).

The accepted response settings are `HUMAIN_TURN_TAKING_STRATEGY=fast_vad` and
`HUMAIN_INTERRUPTION_POLICY_MODE=off`. The application alone plays the packaged
1.3x recording notice. Callers hear it 5-7 seconds after the call connects;
shortening that **5-7 second delay is work in progress and on hold**. Don't
reintroduce the reverted startup or waiting-music experiments.

Native voice handles one AI call at a time, with a 20-minute AI phase and a
3,000-cycle controller watchdog.

## How a native call works

Explain progress to the owner with the caller's journey:

1. A caller dials the Dynamics number. The workstream binds the number, the
   languages, the routing agent and the fallback queue.
2. The routing agent, a thin Copilot Studio agent, controls the call and hands
   its audio to the voice app's controller. Callers don't talk to it.
3. The controller bridges the call to HUMAIN, which talks with the caller.
4. On handover, the controller transfers the same Dynamics conversation to a
   representative from the fallback queue.

Report each component at one of three levels: connected, configured, or
accepted on a real call. Offline tests, configuration readback and caller
acceptance are different kinds of evidence; never report one as another.

## What is selected

| Module | Native follow-on |
|---|---|
| `native-voice` | Dedicated native ACS, Dynamics connection and number, thin routing agent, reader identity, observer and same-conversation transfer |
| `crm` | Separate CRM identity and schema and the shared API; no records unless privately generated and approved |
| `native-workspace` | Scoped forms, tabs, scripts, macros and representative bindings |
| `native-knowledge` | Approved private bilingual article publication |
| `native-copilot` | Licensed native knowledge Q&A and summary configuration |
| `pause-ai` | Secured command table and roles, compatible controller pins and native control tab |

Custom text chat, browser voice, email and either custom agent desktop are
independent selections. Native calling needs neither the custom console nor an
application-owned ACS number.

## Stage 0: confirm eligibility and scope

Reuse the approved profile. Ask only for missing decisions.

**Human gate:** one approval for the target tenant, subscription and
organization, regions and data policy, licenses, number eligibility, charges,
approved test callers and licensed representatives. The Azure and Dataverse
targets must use the **same Entra** tenant.

Confirm the deployment guide's checks passed:

- the [voice pipeline gate](../deployment.md#gate-the-voice-pipeline) reports
  `Elastic`. If nobody in the US could call the trial number, run it on the
  first call to the native number instead, before Stage 8;
- the subscription [can acquire numbers](../deployment.md#4-check-telephony-eligibility-and-choose-numbers).

Recheck the runbook's
[lifecycle and eligibility section](README.md#first-confirm-service-lifecycle-and-eligibility).
Don't substitute Teams Phone, an SBC or an application-owned call when the
native ACS path is unavailable; those are different designs.

## Stage 1: keep the prepared workspace and private evidence

Use the prepared `azure.yaml`, [root Bicep](../../infra/main.bicep), private
profile and named azd environment. Don't create a second environment or load
another deployment's values.

Keep native component receipts in a private sibling directory outside the
prepared source root:

```bash
umask 077
export WORK="$PRIVATE/native-receipts"
mkdir -m 700 "$WORK"
python infra/scripts/native_voice_setup.py --help
python infra/scripts/template_setup.py status
```

Record source hashes, target selections and approval references. Never put
passwords, provider authorization, keys, private topic bundles or caller data
into chat, Git, screenshots or public logs.

## Stage 2: provision only the selected Azure services

If the deployment guide already provisioned the selection, keep its receipts
and skip this stage. Otherwise use its `configure`, `preview` and approval
flow:

```bash
python infra/scripts/template_setup.py preview
```

**Human gate:** the owner reviews the exact Azure changes, charges, geography
and resource reuse before approval is recorded.

```bash
python infra/scripts/template_setup.py gate --phase apply
azd provision --environment "$ENV"
azd deploy --environment "$ENV"
```

Native voice alone needs the voice service, not every model, database or UI.
The Assist backend is added only for selected CRM, knowledge or agent-assist.
Never change an existing production environment as a deployment test.

Keep `HUMAIN_AUTHORIZATION` private. Check the
[live HUMAIN contract](https://humain-voice-agent.share.sanad.ink/docs/connect.html)
and verify requested and effective metadata before changing integration
settings.

## Stage 3: create and connect the native ACS resource

Follow runbook
[section 3](README.md#3-create-and-connect-the-dedicated-native-acs-resource).
Dynamics asks for the Event Grid delivery app's ID when you connect the
resource, so create that app first:

```bash
python infra/scripts/native_event_delivery.py bootstrap \
  --tenant-id "$TENANT_ID" --output-dir "$WORK/event-delivery-app"
```

Review the plan, then rerun with `--apply --confirm-tenant "$TENANT_ID"`. It
creates only the delivery app, its service principal, the owner and two
app-role assignments; no secret, API permission or consent.

Create the native ACS resource with
[dynamics-voice-poc.bicep](../../infra/dynamics-voice-poc.bicep), in its own
resource group and with data location Switzerland. Then write the four
connection values to a private copy sheet:

```bash
python infra/scripts/native_voice_setup.py copy-sheet \
  --tenant-id "$TENANT_ID" --native-acs-resource-id "$NATIVE_ACS_ID" \
  --event-grid-app-id "$EVENTGRID_APP_ID" \
  --output "$WORK/native-acs-copy-sheet.txt"
```

Warn the owner first that an editor selection can be attached to a chat
message. After copying the connection string, they clear the selection or
switch to a file without secrets before sending anything.

**Human gate:** the owner connects the resource one screen at a time, as in
runbook [3.4](README.md#34-connect-in-the-copilot-service-admin-center): end
the voice trial if it's offered, open the Azure Communication Services tab,
choose **Use existing resource**, enter the four values and select
**Connect**. Never use the application ACS resource here.

Read the connection back, then delete the copy sheet:

```bash
python infra/scripts/native_voice_setup.py connection \
  --tenant-id "$TENANT_ID" --org-url "$ORG_URL" \
  --native-acs-resource-id "$NATIVE_ACS_ID" \
  --event-grid-app-id "$EVENTGRID_APP_ID"
rm "$WORK/native-acs-copy-sheet.txt"
```

Continue when it reports `connected` with `is_trial: false`. To rotate the
connection string later, follow runbook
[3.6](README.md#36-rotate-the-connection-string); never disconnect just to
reveal the field.

## Stage 4: native number, event delivery and routing

Follow runbook
[section 4](README.md#4-configure-native-event-delivery-and-numbers) and
[section 5](README.md#5-create-the-routing-agent-and-native-inbound-workstream).

**Number.** Buy the native number on the native ACS resource with the
deployment guide's [number recipe](../deployment.md#choose-and-buy-numbers).
UK local numbers that make and receive calls are the recommended default. The
owner then syncs it in **Phone numbers > Advanced > Sync from Azure > Sync**.
**Add number** would buy another number, so don't use it.

**Event delivery.** The owner copies the incoming-call and recording webhook
URLs from the same Advanced page into the private endpoints file
([runbook 4.3](README.md#43-copy-the-two-webhook-urls)). Never construct the
URLs. Then plan the system topic and both subscriptions:

```bash
python infra/scripts/native_event_delivery.py subscriptions \
  --tenant-id "$TENANT_ID" --native-acs-resource-id "$NATIVE_ACS_ID" \
  --system-topic-name "$SYSTEM_TOPIC" --delivery-app-id "$EVENTGRID_APP_ID" \
  --endpoints-file "$WORK/dynamics-webhooks.env" \
  --output-dir "$WORK/event-delivery-subscriptions"
```

Review the plan, rerun it with `--apply --confirm-acs-name "$NATIVE_ACS_NAME"`,
then read everything back:

```bash
python infra/scripts/native_event_delivery.py verify \
  --tenant-id "$TENANT_ID" --native-acs-resource-id "$NATIVE_ACS_ID" \
  --system-topic-name "$SYSTEM_TOPIC" --delivery-app-id "$EVENTGRID_APP_ID" \
  --endpoints-file "$WORK/dynamics-webhooks.env" \
  --receipt-file "$WORK/event-delivery-verify-01.json"
```

**Exit gate:** the owner refreshes Manage telephony, and both "Your Event Grid
is unconfigured" banners are gone. See
[native event delivery](operations.md#native-event-delivery).

**Shell queue and workstream.** Before CRM, create one fallback queue and one
voice workstream ([runbook 5.1](README.md#51-create-the-minimal-shell-one-queue-and-one-voice-workstream)).
A new Customer Service environment already contains Microsoft's workstreams,
queues and bots, and `discover` marks them `owned: false`. Never configure or
adopt them, or any record by display name.

**Routing agent.** Create the thin voice-enabled agent
([runbook 5.2](README.md#52-create-a-thin-voice-enabled-routing-agent)). Two
steps have no CLI path. **Enable voice** is saved only by the **Save** button
inside **Phone setup > Select voice**. The telephony channel uses the panel's
**Turn on Telephony channel** flow, then **Connect**.

**Workstream.** Give the owner every voice-wizard value before they start,
including the channel name and the Behaviors page
([wizard values](README.md#provide-the-wizard-values-before-asking-the-operator-to-start)).
External phone consult stays on. Configure the Escalate topic and the
bot-failure action as in
[runbook 5.3](README.md#53-finish-the-inbound-workstream).

**Human gates:** number purchase and charges, phone sync, the voice-enabled
routing agent, and the queue and workstream shells. Verify the actual IDs,
languages, recording/transcription settings and representative entitlements.

Allow **15 minutes after native workstream configuration readback** before
caller acceptance, and restart that wait after another change. A successful
publication or saved flag doesn't prove that telephony has picked up the
change.

## Stage 5: identities and optional CRM

Create the native reader role and the application user for the voice app's
managed identity
([runbook 7.2](README.md#72-create-the-reader-role-and-application-user)):

```bash
python infra/scripts/native_voice_setup.py reader-role \
  --tenant-id "$TENANT_ID" --org-url "$ORG_URL" \
  --business-unit-id "$BUSINESS_UNIT_ID" --client-id "$UAMI_CLIENT_ID" \
  --principal-id "$UAMI_PRINCIPAL_ID" --output-dir "$WORK/reader-role"
```

Review the plan, then rerun with `--apply --confirm-organization "$ORG_URL"`.
It removes the new role's seeded defaults, adds the six reads and re-audits
after every write. It journals user creation and role association separately,
so an interrupted run resumes without a duplicate user.

Then prove the identity from inside the running voice container
([reader runtime check](operations.md#native-reader-runtime-check)):

```bash
python infra/scripts/check_native_reader_runtime.py \
  --subscription-id "$SUBSCRIPTION_ID" --resource-group "$APP_RG" \
  --expected-runtime-user "$READER_USER_ID" \
  --expected-business-unit "$BUSINESS_UNIT_ID" \
  --receipt-file "$WORK/reader-runtime-01.json"
```

When `crm` is selected, follow
[setup_dynamics_crm.py](../../infra/scripts/setup_dynamics_crm.py) and the
[CRM chapter](crm-setup.md). Native-mode CRM needs the workstream ID from
Stage 4. Copy the matching case categories, location policy and `native_voice`
choice from the prepared configuration.

Default to schema and identity only. A nonempty generated seed needs a private
preview, owner approval and `--approve-seed`; it doesn't authorize email or
calls. Verify the deployed managed identity, not admin impersonation. Native
case linking also needs Global `prvReadPluginAssembly`; CRM without native
voice doesn't.

## Stage 6: generate and review the controller

Create the `NativeMediaContext` and `NativeSpeechTurnPilot` shells in the
native UI
([runbook 8](README.md#8-create-the-two-topic-shells-and-external-context-variable)).
The external context-definition topic must never run as a call step.

Generate new control keys only for a new target:

```bash
python infra/scripts/native_voice_setup.py new-keys \
  --output "$WORK/control-keys.env"
```

Don't import that whole file into azd: an observer key without its resource
and lifetime settings is an invalid partial configuration. Don't replace the
ingest key that preparation generated.

Bundle with the real target IDs and the explicitly approved caller:

```bash
python infra/scripts/native_voice_setup.py bundle \
  --tenant-id "$TENANT_ID" --org-url "$ORG_URL" \
  --bot-id "$BOT_ID" --voice-origin "$VOICE_ORIGIN" \
  --test-caller "$TEST_CALLER" \
  --secrets-file "$WORK/control-keys.env" \
  --startup-notice --output-dir "$WORK/topic-bundle-01"
```

For selected CRM, add `--crm-case-context`; that variant uses
`--startup-notice --crm-case-context`. Don't add a CRM controller path when its
runtime module is absent.

Keep `play_startup_notice: true`, `wait_for_startup=true`, native language
control, bounded polling and the departure-before-`TransferToAgent` sequence.
The bundle contains credentials and caller IDs and stays private.

## Stage 7: stage and publish

Preview [atomic topic staging](README.md#93-preview-then-stage-atomically),
then get exact organization and scope approval before `--apply` and
`--confirm-organization`. Keep ETags, saved definitions and publication state.

**Human gate:** the Copilot Topic checker, review of unrelated saved drafts,
and publication.

Publish with [pac_publish.py](../../infra/scripts/pac_publish.py), which runs
`pac copilot publish` once and proves the result by Dataverse readback:

```bash
python infra/scripts/pac_publish.py --tenant-id "$TENANT_ID" --org-url "$ORG_URL" \
  --bot-id "$BOT_ID" --receipt-dir "$WORK/publish-01"
```

Review the preview, then rerun with `--apply --confirm-organization "$ORG_URL"`.
PAC's exit code is a hint; the result counts as published only after the
readback. After a failure, fix the identified defect and retry once with
`--retry-change`. Direct Dataverse `PvaPublish` is internal-only and isn't a
fallback.

Don't let a stale browser editor overwrite the staged topic. Verify the
published definition, not the authoring draft. The Copilot Studio test pane
has no phone context, so it shows "Only approved test callers can use this
line." and can't test the native path.

## Stage 8: runtime settings, notice policy and observer

Follow [runtime and observer setup](README.md#10-configure-the-native-runtime-and-passive-observer)
and [dynamics-handover-probe.bicep](../../infra/dynamics-handover-probe.bicep).
Choose a bounded observation lease or explicit permanent mode; a temporary
test doesn't grant permission for permanent observation.

The accepted permanent fast-start selection is `--permanent --fast-start`. Use
an expiry instead when that is the approved policy. Runtime settings use the
native resource's credentials; the optional `--app-number` isn't a native
handover prerequisite.

Use `native_voice_setup.py startup-audio` for the reviewed workstream flags.
Apply needs its saved plan, the exact organization and
`--confirm-notice-policy`. Keep `not_before_call_acceptance`; readback isn't
propagation.

Apply the complete observer resource, key and lifetime set together. Don't
silently renew expired observation or disable caller or ownership guards to get
audio. For later settings-only changes, use
[native runtime settings](operations.md#native-runtime-settings), which updates
only the voice app.

### Apply the accepted native audio settings

Plan first, keeping the exact workstream snapshot and ETags:

```bash
python infra/scripts/native_voice_setup.py startup-audio \
  --tenant-id "$TENANT_ID" --org-url "$ORG_URL" \
  --workstream-id "$WORKSTREAM_ID" --output-dir "$WORK/startup-audio"
```

**Human gate:** review the application-owned notice and the two native
notification flags. This isn't a legal determination, and it doesn't authorize
changes to recording, consent requirements or music assets.

```bash
python infra/scripts/native_voice_setup.py startup-audio \
  --tenant-id "$TENANT_ID" --org-url "$ORG_URL" \
  --workstream-id "$WORKSTREAM_ID" --output-dir "$WORK/startup-audio" \
  --apply --confirm-organization "$ORG_URL" --confirm-notice-policy
```

Reusing that directory after a timeout only reconciles the recorded intent; it
never submits another uncertain write. Use its recorded
`not_before_call_acceptance` and wait the full propagation interval.

## Stage 9: selected representative workspace and knowledge

If `native-workspace` is absent, skip this stage.

Use [setup_dynamics_workspace.py](../../infra/scripts/setup_dynamics_workspace.py)
and the [workspace chapter](workspace-setup.md). Start with a read-only plan,
review ownership and the actual managed defaults, then apply the approved
stages in order: `language`, `representative`, `workspace`, `knowledge`,
`copilot`, `bind`, `verify`.

- `representative` gives the first representative the three required roles, a
  bookable resource, the inbound capacity profile and fallback queue
  membership ([single representative setup](workspace-setup.md#single-representative-setup)).
  It creates no users and assigns no licenses.
- `knowledge` publishes the approved bilingual articles and gives the
  representative a personal filter that shows internal articles
  ([internal knowledge visibility](workspace-setup.md#internal-knowledge-visibility)).
- `copilot` waits up to `--copilot-wait-minutes` (default 60) for native
  Copilot to reach Ready. An administrator completes the
  [opt-in page](workspace-setup.md#native-copilot-opt-in) first. Don't turn on
  managed flows or patch the status.

A basic workspace doesn't need knowledge publication or Copilot licensing;
select those separately. Native knowledge needs the reviewed canonical and
Arabic inputs; never publish placeholder articles to make a status look ready.
If a stage stops after a successful write, resume as described in
[native behaviors the kit resumes from](workspace-setup.md#native-behaviors-the-kit-resumes-from).

**Human gates:** language availability, scoped form and profile changes,
publication, bindings, the Copilot opt-in and the representative's actual UI.
App or profile ownership and customer or case association are different
things. An open case tab isn't a saved native link.

## Stage 10: selected explicit Pause AI

If `pause-ai` is absent, skip this stage.

Follow [setup_native_supervision.py](../../infra/scripts/setup_native_supervision.py)
and the [supervision chapter](supervision-setup.md). Compose it with the real
portable workspace using `--base-receipt` and the base's `--approve-extension`
registration, keeping immutable receipts and the exact verified controller
pins. If a Dataverse request fails partway through schema creation, rerun the
same stage; the error record keeps the HTTP status, error code and request IDs.

Run [check_native_supervision_runtime.py](../../infra/scripts/check_native_supervision_runtime.py)
against the deployed identity. Global `prvReadEntity`, `prvReadAttribute` and
`prvReadRelationship` are preflight requirements. Don't substitute admin
access or enable the feature before readiness passes.

Pause is explicit, authenticated and latched, with no automatic resume. Native
Monitor and Join alone don't pause HUMAIN.

## Stage 11: no-call verification and caller acceptance

First verify publication, routing, runtime identity, observer configuration,
selected CRM and workspace readiness, and disabled-module behavior. Never use
a made-up conversation ID to start a live session.

Use the [acceptance checklist](acceptance-checklist.md) for the selected
capabilities. Check the 15-minute propagation boundary, and get **fresh**
caller readiness, recording/transcription consent, charges approval and a time
cap for each call. Don't dial automatically.

Before asking for readiness, prepare the operator:

- Send the Dynamics workspace URL. The representative accepts the call there,
  not in the custom console or Teams.
- The representative is **Available**, with the microphone allowed.
- The caller runs a visible timer and hangs up at the agreed cap. The agent
  can't end a call, and its monitor only proves control flow.

The operator must see the monitor's `ARMED` state before the caller dials.
Then verify audio and the notice, language, transcript, interruption,
same-conversation handover, and the optional CRM link and Pause AI. HUMAIN's
first turn must be the approved greeting, after which it waits for the caller.
Finish wrap-up before inspecting the final artifacts.

If HUMAIN is audible but missing from the transcript, rerun the voice pipeline
gate first, then follow
[native voice diagnostics](troubleshooting.md#native-voice-diagnostics).

Report configuration, UI, audio and live-case results separately. Knowledge
Q&A doesn't query live cases. A healthy endpoint isn't a passed call.

## Stage 12: hand over or stop with an explicit blocker

Keep source and profile hashes, images, targets, original component receipts,
secret-store locations without values, actual acceptance and pending gates.
Record coordination progress with `template_setup.py record` and `status`.

Report every component as connected, configured or accepted on a real call,
using the caller's journey above. Never rerun an uncertain write, fabricate an
ownership receipt, or repair a past call to claim acceptance. Use the scoped
rollback and recovery from the component chapter. Leave unrelated users,
roles, queues, drafts and business records intact.

The handover states which modules weren't selected, which checks are still
open, the one-call and 20-minute limits, and the 5-7 second notice delay that
is on hold.
