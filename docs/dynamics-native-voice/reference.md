# Native voice: architecture and reference

[Deployment tutorial](README.md) | [Operations](operations.md) | [Troubleshooting](troubleshooting.md)

This page describes the accepted configuration as of 7 November 2026.
Untested Dynamics features are listed under [acceptance boundaries](#acceptance-boundaries).

## Data and control paths

Open the [call architecture and handover diagrams](native-call-flow.html).
They show the media paths separately from the transfer sequence.

Dynamics answers the PSTN call on its native ACS resource. Copilot stays active
as the routing controller. A passive `IncomingCall` observer supplies the Azure
app with correlation metadata; it never answers the call.

Call Automation attaches to the existing call and receives unmixed 24-kHz audio.
The app forwards only the verified PSTN caller's stream to the per-call
HUMAIN/ALLAM LiveKit room. HUMAIN handles speech recognition, responses and tools.
An Azure-hosted Calling SDK browser then publishes HUMAIN PCM as a custom
microphone on the same call, using a fresh user from the native ACS resource.

The browser initializes software audio devices and publishes only that custom
stream. It captures no physical microphone and needs no representative sign-in.

On a human request, the app stops AI media, removes its microphone/control legs
and confirms their departure. Copilot then transfers the same conversation to
the native queue, where a representative receives and accepts the notification.

### What Dynamics sees

| Party | Identity |
|---|---|
| Caller | Original PSTN caller number |
| HUMAIN | Separate ACS VoIP user; native UI currently renders `Acs_Unknown` |
| Routing shell | Copilot/IVR identity for setup/control messages |
| Representative | Normal Dynamics user, shown as their name / `You` |

The native Calling SDK `call.id` matched `msdyn_callsid` in the phone engagement
context. Opaque server-call locator strings from two APIs were not necessarily
equal; comparing those strings alone falsely rejected one diagnostic.

The native adapter verifies:

1. The exact Dynamics parent conversation and allowed inbound workstream.
2. The uniquely open original bot session, with no already accepted human.
3. The exact phone engagement context and observed incoming-call correlation.
4. Call Automation connection readiness and the expected customer in its roster.
5. The new SDK participant's call ID and actual native membership.
6. Native language acknowledgement before HUMAIN audio starts.

It never chooses a "latest active call" or decodes an opaque server-call ID to
guess ownership.

## Identity dictionary

Use each ID only for the field listed:

| Name | Origin/use |
|---|---|
| Entra tenant ID | Tenant boundary for Azure and Dynamics authentication |
| Azure subscription ID | Resource billing/management scope |
| Power Platform environment ID | Environment selection in Copilot Studio / Power Platform |
| Dataverse organization ID | Native Dynamics organization and generated webhook context |
| Runtime app module ID | `main.aspx?appid=...` for admin/workspace |
| Copilot bot ID | Dataverse `bots` record |
| Bot schema prefix | Namespace used by `BeginDialog`, from actual `schemaname` |
| Copilot topic ID | `botcomponentid`; topic YAML is in `data`, not `content` |
| Native bot user ID | Workstream's bot system-user lookup |
| Native conversation ID | `msdyn_ocliveworkitemid`; supplied as `Global.msdyn_ConversationId` |
| Native session ID | `msdyn_ocsessions.activityid`; one parent can have bot and human child sessions |
| Native call correlation ID | `msdyn_ocphonecallengagementctx.msdyn_callsid`, matching native incoming event correlation / SDK call ID |
| Server-call locator | Opaque ACS value from the native IncomingCall observer |
| ACS connection ID | Application's own Call Automation leg |
| UAMI client ID | Application ID used when adding the Dataverse managed-identity app user |
| UAMI principal ID | Entra service-principal/object ID; role-assignment target |
| Dataverse application-user ID | `systemuserid` for that UAMI in this environment |
| CRM managed identity / app user | Separate assist-service identity for customer/case operations; not the native telephony reader |
| Dynamics case number | `incident.ticketnumber`, carried by the `CaseNumber` output global |
| Dynamics case ID | `incident.incidentid`, used for record relationships; not a substitute for `CaseNumber` |
| Event Grid app client ID | Dedicated webhook audience; supplied to Dynamics and native subscriptions |
| Event Grid app object ID | App registration object used to configure app roles |
| Microsoft.EventGrid principal ID | Tenant-local enterprise application receiving delivery permission |
| Per-call ACS user ID | Disposable VoIP speaker identity; never a purchased phone number |
| LiveKit room/participant ID | Per-call HUMAIN provider context, not a Dynamics record ID |

The server captures the native parent ID once. Executing the global initializer
again would replace it with an empty value. The initializer belongs only in the
unused context-definition topic.

## Topic package

### Required

| Component | Purpose | How created |
|---|---|---|
| `Conversation Start` | Caller gate -> speech-turn controller; preserve approved nonpilot fallback | Existing native system topic, then private generated entry |
| `NativeSpeechTurnPilot` | Silent native speech-turn control, language changes and guarded human transfer | UI-created empty topic, populated by setup kit |
| `NativeMediaContext` | Defines externally initialized global String | UI-created variable node, preserved as unused topic |
| `msdyn_ConversationId` | Conversation-scoped, external-input-only global | UI variable properties |
| `CaseNumber` (CRM option) | Conversation-scoped, external-output String for a validated selected case | `case-context-variable` setup command |
| Native voice system topics | Platform silence/unrecognized/error/DTMF/handoff infrastructure | Keep the shipped topics; don't delete merely because they seem unused |

### Historical or diagnostic, not prerequisites

- `NativeMediaPilot`: previous retained long-poll controller; base for the renderer.
- `NativeLanguageProbe`: language/silence semantics diagnostic.
- `NativeTranscriptBoundaryProbe`: display-text versus spoken-audio diagnostic.
- `NativeSpeechLocaleProbe`: earlier isolated language experiment.

Skip those diagnostic topics in a new tenant. The renderer uses the checked-in
base template offline.

### Exact language/silence settings

The native language choice is:

```yaml
variable: System.User.Language
value:
  kind: OptionDataValue
  type:
    kind: SystemOptionSet
    name: Locale
  value: Arabic
```

Use `English` for the other branch. `ar-SA` and `en-US` describe speech-channel
locales; they are not plain-string substitutes for this choice-typed variable.

The quiet listening node is a whole-response Question:

```yaml
kind: Question
id: native_listen_turn
interruptionPolicy:
  allowInterruption: false
alwaysPrompt: true
variable: init:Topic.NativeObservedSpeech
prompt:
  speak:
    - <break time="20ms" />
entity: StringPrebuiltEntity
voiceInputSettings:
  silenceDetectionTimeoutInMilliseconds: 500
  repeatCountOnSilence: 0
  defaultValueMissingAction: SetEmpty
  defaultValueOnSilence: =Blank()
  utteranceEndTimeoutInMilliseconds: 300
  speechRecognitionTimeoutInMilliseconds: 1000
```

The complete generated controller is authoritative; do not assemble it by
copying these isolated excerpts.

`NativeObservedSpeech` may contain HUMAIN's speech because the native recognizer
can hear other call participants. The controller neither forwards it to the application nor uses it for
intent/language/handover decisions. Only HUMAIN's trusted tools, with
the verified customer PCM path, request those actions.

## Native control API

All control endpoints use the dedicated `DYNAMICS_NATIVE_MEDIA_API_KEY` in
`X-API-Key`. No endpoint below is a public browser API.

| Method/path | Contract |
|---|---|
| `GET /dynamics/native/readiness` | Checks real application Dataverse reads and VoIP readiness; can warm unused page |
| `POST /dynamics/native/sessions` | Accepts exact native ID/language/retained-controller flag; starts guarded background setup |
| `POST /dynamics/native/sessions/{id}/turn` | Completes one native listening acknowledgement; accepts only language/revision |
| `GET /dynamics/native/sessions/{id}` | Current lifecycle, frame counts, language-control state; no transcript by default |
| `GET /dynamics/native/sessions/{id}?wait=true` | Legacy status long poll, at most 20 seconds |
| `GET /dynamics/native/sessions/{id}?transcript_after=N` | Optional authenticated finalized AI-turn feed |
| `GET /dynamics/native/sessions/{id}/timings` | Bounded monotonic timing events, no speech/audio |
| `POST /dynamics/native/sessions/{id}/stop` | Request release of application's own media, not caller hangup |

Speech-turn start body:

```json
{
  "conversation_id": "<actual native Dynamics conversation UUID>",
  "language": "ar",
  "retain_native_controller": true,
  "speech_turn_control": true
}
```

`speech_turn_control` defaults off for older callers; it requires `voip`.

Listening acknowledgement:

```json
{"language": "ar", "revision": 0}
```

The response always uses typed fields, including:

```json
{
  "conversation_id": "<same UUID>",
  "phase": "active",
  "action": "wait",
  "reason": "",
  "azure_leg_detached": false,
  "control_language": "ar",
  "control_revision": 0,
  "control_ready": true
}
```

The server rejects text/intent fields in `/turn`. A stale acknowledgement cannot
confirm a newer language revision. The trusted `select_response_language` tool
requires a new finalized customer transcript, waits for native acknowledgement,
then explicitly tells HUMAIN the new response language. Silence is not
interpreted as a request, and a failed switch is not reported as success.

Handover gate:

```json
{
  "action": "handover",
  "phase": "handover_ready",
  "azure_leg_detached": true,
  "reason": "customer_requested_human"
}
```

Copilot may request native transfer after this response. Human acceptance is
still separate; the backend reports it as
`not_observed_by_media_adapter`.

HTTP 202 from start is asynchronous acceptance. HTTP 200 status alone does not
establish audio. Strings declared by the topic, particularly `reason`, must be
strings, not JSON null.

### Media and observer endpoints

- `/dynamics/native/sessions/{id}/media` is the ACS input WebSocket. It needs a
  per-session capability and exact `x-ms-call-connection-id`, not the general
  control key. It is not advertised to callers.
- The browser's private PCM/control WebSocket is bound to localhost and
  capability/origin protected.
- `/dynamics/handover/probe/events` is the passive observer with its own key.
  It never answers an IncomingCall or stores its answer token.

## Environment settings

Use root Bicep/azd for Azure, not the standalone app manifest.

| Variable | Deployment value / purpose |
|---|---|
| `AZURE_SUBSCRIPTION_ID`, `AZURE_RESOURCE_GROUP` | Explicit target deployment |
| `AZURE_LOCATION`, `APP_LOCATION`, `COSMOS_LOCATION`, `SEARCH_LOCATION` | Approved resource placement; don't relocate existing resources casually |
| `AZURE_PRINCIPAL_ID` | Deploying user object ID for the root bootstrap |
| `INGEST_API_KEY` | Shared Assist case/knowledge backend authentication |
| `CRM_DATAVERSE_ORG_URL` | Assist runtime's Dataverse origin; use the deployment bindings documented in the CRM setup chapter |
| `CRM_CLIENT_ID` | Assist runtime's separate CRM managed identity client ID |
| `CRM_NATIVE_WORKSTREAM_IDS` | Exact native workstreams eligible for verified customer/case association |
| `HUMAIN_AUTHORIZATION` | Complete private provider authorization |
| `HUMAIN_LLM_PROVIDER` | `allam`; override the root historical `gemma4` default explicitly |
| `HUMAIN_ASR_MODEL` | `bayan_playground` |
| `HUMAIN_VOICE` | `mul_nourah`, bilingual voice used in tests |
| `HUMAIN_INTERRUPTION_POLICY_MODE` | Accepted default **`off`**, paired with `fast_vad`; accepted for ongoing use. [Acceptance and history](../../apps/voice/README.md#accepted-voice-responsiveness); compatible health reports the requested value |
| `HUMAIN_TURN_TAKING_STRATEGY` | Accepted default **`fast_vad`**, paired with `off`; `livekit_v1_mini` + `control` is an explicit rollback option, not the deployment default |
| `HUMAIN_TOKEN_ENDPOINT` | Provider-issued endpoint; source deployment used `https://livekit-admin.sanad.ink/api/tokens/client` |
| `ACS_SOURCE_NUMBER` | Application ACS number, not a destination to call during native attachment |
| `ACS_CONNECTION_STRING` | Optional root override for application ACS; never put native ACS here to merge resources |
| `ACS_AUTO_CONFERENCE_ENABLED` | **false** |
| `DYNAMICS_ORG_URL` | Exact Dataverse origin |
| `DYNAMICS_NATIVE_MEDIA_ENABLED` | true only after configuration is complete |
| `DYNAMICS_NATIVE_MEDIA_TRANSPORT` | **voip** |
| `DYNAMICS_NATIVE_MEDIA_FAST_START` | true for tested warm/parallel path; false for serial baseline |
| `DYNAMICS_NATIVE_MEDIA_WORKSTREAM_ID` | Primary allowed native inbound workstream |
| `DYNAMICS_NATIVE_MEDIA_ADDITIONAL_WORKSTREAM_IDS` | Optional comma-separated UUIDs for additional inbound entries; no duplicates or wildcard |
| `DYNAMICS_NATIVE_MEDIA_API_KEY` | Dedicated server-to-server Copilot control key |
| `DYNAMICS_NATIVE_MEDIA_ACS_CONNECTION_STRING` | Native Dynamics ACS connection, separate from application ACS |
| `DYNAMICS_NATIVE_PROBE_RESOURCE_ID` | Full ARM ID of native ACS, not a call ID |
| `DYNAMICS_NATIVE_PROBE_API_KEY` | Dedicated observer Event Grid delivery key |
| `DYNAMICS_NATIVE_PROBE_EXPIRES_AT` | Future UTC lease matching Event Grid; empty in permanent mode |
| `DYNAMICS_NATIVE_PROBE_PERMANENT` | Default false; explicit no-expiry mode with the same authentication and metadata lookback |
| `DYNAMICS_OUTBOUND_ENABLED` | **false** |
| `DYNAMICS_OUTBOUND_CONFIG_ID` | Empty for this inbound recipe |
| `DYNAMICS_NATIVE_SUPERVISION_ENABLED` | Default false; enable only after the separate native Pause AI setup and verification |
| `DYNAMICS_NATIVE_SUPERVISION_ACTOR_IDS` | Explicit approved human Dataverse user IDs, not caller profiles or Entra object IDs |
| `DYNAMICS_NATIVE_SUPERVISION_CONTROLLERS` | Reviewed native controller component IDs/SHA-256 pins emitted by the supervision setup verifier |

Bicep supplies `AZURE_CLIENT_ID` from the application UAMI, `AZURE_TENANT_ID` from
the deployment tenant, callback/media URLs from the voice origin, and the
Assist/Cosmos/email/Foundry bindings. Do not copy these from another environment.

## Limits

| Limit | Current behavior |
|---|---|
| Concurrent native pilot sessions | One |
| Application process/replicas | One in-memory owner; min=max one replica |
| Voice resources | 2 vCPU, 4 GiB in tested deployment |
| Overall setup | 30 seconds |
| Pending native context | 10-second wall-clock bound, within overall setup |
| ACS connection readiness | 10-second wall-clock bound, within overall setup |
| AI phase | 1,200 seconds (20 minutes) after setup |
| Native language acknowledgement | 8-second server wait |
| Speech-turn controller cycle cap | 3,000 |
| Observer metadata | 128 events, 15-minute lookback; expiring lease or explicit permanent mode |
| Recent native sessions | 16 records, approximately 15-minute retention |
| Timing events | 256 per call, explicit dropped-event count |
| PCM | 24-kHz mono PCM16, 20-ms frames |
| Browser scheduling | 60-ms base buffer, explicit 500-ms maximum queue |
| Final AI transcript API item | 16 KiB maximum, explicit error rather than truncation |
| Provider tool output | Below 15 KiB RPC limit |
| Native CRM prefetch | 8-second bound, before HUMAIN microphone invitation |
| Shared case-service HTTP client | 18-second request bound |
| Individual Dataverse request | Reads 4 seconds, writes 10 seconds, capped by the 16-second shared operation budget; a timeout is an uncertain outcome, not proof no write occurred |

The AI deadline ships in the voice image and the controller cap in the
generated controller. An existing deployment that still uses 120 seconds and
300 cycles needs the updated voice image and a republished controller that
keeps its CRM and Pause AI options.

Timeout/cap expiry does not necessarily hang up the caller; the server can offer
native handover after safely releasing its media. The test participant must
still hang up at the agreed cutoff.

## Measured results

These measurements come from development calls in one tenant.

| Milestone | Evidence |
|---|---|
| Initial retained-controller human handover | Real caller, same native parent, notification/acceptance and clear human audio |
| Native AI transcript via a real microphone | Synthetic clip and then actual HUMAIN speech persisted under a distinct ACS speaker |
| Bilingual native control | Owned and real calls with explicit AR/EN/AR changes; native system notifications confirm transcription updates |
| Final combined real call | Arabic -> English -> Arabic -> English, one interrupting human request, native transfer, representative acceptance and live three-speaker view |
| Final real handover | About 1.6 s request-end -> tool; 4.3 s request-end -> native transfer; user perceived about 5-6 s |
| Faster startup | Two-versus-two controlled Azure calls: microphone-ready median 7.18 -> 5.38 s; first native Arabic speech 18.46 -> 16.29 s from recording start |
| Telemetry | Real stage events retained in `ContainerAppConsoleLogs` while workspace local auth stayed disabled |
| CRM context from the deployed voice container | 30 warm samples: median 128.121 ms, p95 154.153 ms, maximum 188.170 ms; not end-to-end phone startup |
| Initial API request after a deployment | 4.055 s in the separate Codespace measurement; cold requests are not uniformly sub-second |
| Isolated CRM case/note operations | Real application-identity reads and idempotent writes; unauthorized and cross-customer requests rejected |
| Real chat confirmation after write correction | Prepare created no case; one confirmation created one case with all location fields and returned a real Dynamics receipt; sample prepare/confirm times about 6.7/6.6 s, not latency guarantees |
| Public native case association | Customer, IssueId and case Regarding verified under the CRM identity on one temporary non-telephony activity; the activity was removed afterward |
| Bilingual startup notice | Recording-only clip at 1.3x, 7.537 s; user reported about 4 s initial silence, so instant-on-answer remains open |
| Startup latency regression | Native text produced normal-rate TTS; native-only WAV playback also failed caller acceptance. Application-only WAV playback with parallel preparation, native ACK and confirmed cancellation passed one startup call with a smooth transition. Native recording-announcement latency is disabled; the caller's approximately six-second initial delay remains unresolved |
| Prepared-start optimization | Reverted on 2 November to the sealed accepted source and image. The trial did not meet the two-second first-audio target. The 5-7 second delay remains work in progress, with investigation on hold |
| Native IVR music coverage trial | The first trial was premature relative to the 15-minute propagation window. A later propagated retry had two startup failures: missing native controller, then unverified ACS caller membership with fallback handover. Neither reached app notice/HUMAIN; UK music was restored after closure. Coverage and no-added-delay remain unaccepted; music causality is not established |
| Native pre-bot timing | Three matched ACS/native traces: about 1.8-3.6 s from the inbound PSTN participant's start to Copilot start, and 2.8-4.6 s to the first generated message. Participant timestamps have one-second precision and are not handset answer timestamps; this is separate from application preparation |
| Customer Related Cases | Scoped All Cases form verified and user-confirmed; includes active and resolved cases without changing owners |
| Native Knowledge visibility | Personal Internal/External visibility applied for the approved representative; native searches returned published internal articles for English and Arabic terms. Shared filters and user language were unchanged; browser reload/acceptance remains separate |
| Native navigation correction | Three navigation macros, unsaved-case templates, fresh conversation-case lookup and scoped case-form registration deployed. Exact native readbacks, 73 installed-client contract checks and the preserved Pause AI composition passed; browser execution is a separate check |
| Agent-script wording | Twenty English/Arabic step records updated and read back, including the explicit live-call-only link title; unselected steps and macro definitions unchanged |
| Stored conversation summary | One actual modern summary included the AI case discussion and human join/audio checks; not proof of every summary type, complete detail or export speaker attribution |
| Supervisor Join during AI phase | User-confirmed human participation; AI continued responding to caller audio, so Join is not an AI pause guarantee |
| Explicit native Pause AI control | Caller confirmed basic live Pause AI on 2 November; deployed runtime-identity/controller checks also passed. Non-admin permissions and the remaining failure/continuity acceptance checks remain separate |

The fast-start comparison kept ALLAM, knowledge, greeting, ASR and the published
controller unchanged. It used two successful calls per configuration. One fast
run was slower than the fastest serial run; the median improvement does not
predict an individual call.

Eight no-PSTN provider/context/greeting experiments did not justify production
changes. A "short greeting" instruction was inconsistent. A construction-only
context narrowed coverage without a reliable startup win. `bayan_realtime`
failed to establish the provider agent twice in that environment. Those
experiments are not deployment prerequisites.

## Acceptance boundaries

These limits apply, and these features are outside the tested scope:

- Live calls showed distinct speakers and Arabic/English text. The AI label
  remains `Acs_Unknown`.
- Raw exports can call the ACS speaker `Customer`. AI/customer classification in
  sentiment, summaries, agent analytics and compliance must be evaluated.
- The native conversation inherits the selected language at human handover.
  Automatic human-phase language detection and mixed-language phrases are not
  established. Brief Arabic speech after an English handover was rendered
  phonetically; separate captions or translated text are not a native fix.
- Explicit AI-phase language changes work. The trailing clause of a compound
  switch request can be partially recognized during the transition. This is not
  a word-perfect code-switching claim.
- Live history was visible during the call. Per-word caption latency was not measured.
- Recording/transcript existence does not prove complete summaries, sentiment,
  historical analytics or live supervisor feature parity.
- The case store is Dynamics Dataverse for eligible synthetic profiles.
  It is not an external system submission, dispatch or business approval.
  The former Cosmos case path and legacy-reference lookup are disabled.
- Prefetched case descriptions do not by themselves establish selected-case
  state. Case Details association remains an open end-to-end reliability gate;
  a successful read or an opened case tab is insufficient proof.
- A timed-out association PATCH was later present in native records. Bounded
  read-only reconciliation is now implemented and tested; absence at the
  deadline still means uncertain, not proven noncommit. It never blindly repeats
  a write or repairs a completed conversation.
- Caller-ID matching and static profile selection are not customer authentication.
- Native Copilot knowledge Q&A and case summaries do not automatically expose
  HUMAIN's live CRM tools. Live-case query extensibility is separate.
- Published bilingual knowledge, scripts/macros and summary settings are
  configured; actual generated content and both UI-language experiences still
  require focused acceptance.
- Supervisor monitoring/joining does not automatically pause the external AI.
  A human joined during a test while HUMAIN continued to answer customer speech.
  The supervisor's audio remains excluded from the customer-only input stream.
- Startup still has provider/media variability. Increasing replica count breaks
  current in-memory ownership unless the architecture changes.
- The 20-minute AI-phase pilot deadline automatically hands over even without
  a caller request. Earlier calls confirmed `pilot_duration_limit` under the
  original 120-second limit; they do not prove 20-minute live acceptance.
  The caller-hangup/monitor caps remain separate and unchanged. Any further
  safety-policy change requires an explicit reviewed decision.
- Permanent observation removes scheduled expiry, but the observer and browser
  worker still keep per-call state in memory. Restart recovery and durable
  orchestration remain separate work.
- Native proactive outbound remains parked. Buying another ACS Direct Offer
  number does not solve its incompatible workstream requirements.
- This is the existing ACS-based voice path, not a future Teams Phone
  extensibility implementation. New-customer acquisition restrictions and the
  30 September 2028 transition apply to relevant dependencies; see the
  [lifecycle gate and conflicting published cutoff dates](README.md#first-confirm-service-lifecycle-and-eligibility).
  This template establishes no future support exemption or automatic migration.

## Files to change when the implementation evolves

| File | Responsibility |
|---|---|
| [native_media.py](../../apps/voice/src/app/backend/native_media.py) | Native ownership, lifecycle, speech acknowledgements, per-call tools |
| [native_voip.py](../../apps/voice/src/app/backend/native_voip.py) | Private Calling SDK worker, identities, warmup and cleanup |
| [gateway.ts](../../apps/voice/src/app/backend/native_rtc/gateway.ts) | Browser media readiness, call binding and PCM delivery |
| [pcm.ts](../../apps/voice/src/app/backend/native_rtc/pcm.ts) | Audio scheduling and queue bounds |
| [rtmt.py](../../apps/voice/src/app/backend/rtmt.py) | HUMAIN LiveKit media, trusted speaker sources, tools and final transcripts |
| [latency.py](../../apps/voice/src/app/backend/latency.py) | Bounded private-safe monotonic measurements |
| [native_call_probe.py](../../apps/voice/src/app/backend/native_call_probe.py) | Passive event metadata, lifetime mode and short lookback |
| [dataverse.py](../../apps/voice/src/app/backend/dataverse.py) | Application-identity reads and explicit transport errors |
| [stage_native_media_topic.py](../../infra/scripts/stage_native_media_topic.py) | Native YAML serialization and speech-turn renderer |
| [native_voice_setup.py](../../infra/scripts/native_voice_setup.py) | Repeatable private bundle/configuration/preflight workflow |
| [setup_dynamics_crm.py](../../infra/scripts/setup_dynamics_crm.py) | CRM schema, isolated application role and private synthetic seed setup |
| [setup_dynamics_workspace.py](../../infra/scripts/setup_dynamics_workspace.py) | Bilingual knowledge, representative profile, forms, scripts and macros |
| [setup_dynamics_routing.py](../../infra/scripts/setup_dynamics_routing.py) | Guarded native queue/capacity onboarding; no implicit operator or policy changes |
| [setup_native_supervision.py](../../infra/scripts/setup_native_supervision.py) | Opt-in control schema, FLS/roles, owned native UI/controller and rollback |
| [native_supervision.py](../../apps/voice/src/app/backend/native_supervision.py) | Authenticated command provenance, claims and bounded polling |
| [pause.ts](../../apps/voice/src/app/backend/native_rtc/pause.ts) | Private worker pause latch, PCM flush and mute acknowledgment |
| [case-service.ts](../../apps/assist/apps/console/src/lib/case-service.ts) | Shared CRM business rules and native association checks |
| [CRM Dataverse transport](../../apps/assist/apps/console/src/lib/dataverse.ts) | Isolated identity, bounded requests and safe native error codes |
| [dynamics-handover-probe.bicep](../../infra/dynamics-handover-probe.bicep) | Complete observer subscription definition |
| [resources.bicep](../../infra/resources.bicep) | Runtime bindings, secrets, single-replica resources and telemetry |

Do not edit the generated public snapshot alone. Change the source template or
renderer, regenerate it, and keep tests and deployment instructions aligned.
