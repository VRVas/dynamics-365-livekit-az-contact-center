# HUMAIN voice service

This service connects callers to HUMAIN Voice Agent (`hv-agent-flex`) through a
server-side LiveKit bridge. It handles browser voice, application-owned Azure
Communication Services (ACS) phone calls and the media adapter for Dynamics 365
Contact Center voice. HUMAIN credentials and LiveKit room tokens never reach the
browser.

Deploy it with the [modular deployment guide](../../docs/deployment.md). For
Dynamics-owned calls, follow the
[native voice runbook](../../docs/dynamics-native-voice/README.md); the
[native reference](../../docs/dynamics-native-voice/reference.md) has the full
control API, settings and limits.

## Endpoints

| Path | Module | Purpose |
|---|---|---|
| `GET /health` | all | Configuration summary |
| `GET /config` | all | Public organization details and feature flags |
| `GET /` | all | Standalone voice page that embeds `CUSTOMER_UI_URL` |
| `GET /realtime` (WebSocket) | `browser-voice` | Browser microphone session |
| `POST /call` | `application-phone` | Outbound call to an approved number |
| `POST /acs/incoming-call`, `POST /acs/call-events`, `POST /acs` | `application-phone` | Event Grid and Call Automation callbacks; `/acs` is the legacy callback route |
| `GET /realtime-acs` (WebSocket) | `application-phone` | ACS call audio |
| `GET /transcription-acs` (WebSocket) | `application-phone` + `agent-assist` | Transcription after a phone handover |
| `/dynamics/native/...` | `native-voice` | Native media adapter |
| `/dynamics/handover/probe/events` | `native-voice` | Passive call-metadata observer |
| `/dynamics/outbound/...` | `native-voice` | Native proactive (outbound) voice requests |

Unselected or unconfigured features refuse requests. `/health` reports the
service status, requested HUMAIN settings, selected features, registered tools
and the SHA-256 and size of the embedded knowledge. It doesn't prove that audio,
knowledge or email works.

## Configuration

`infra/scripts/template_setup.py prepare` writes the approved versions of these
`src/app` files into the private prepared workspace. The checked-in copies are
neutral: every feature is off and the content files are empty.

| File | Contents |
|---|---|
| `deployment.json` | Organization and assistant names, default language (`en` or `ar`), time zone, greetings, features, case categories, email and outbound-phone policies |
| `system_prompt.md` | Approved persona instructions, added to the backend rules in `runtime_rules.py` |
| `knowledge.md` | Approved embedded knowledge, at most 64 KiB |
| `profiles.json` | Optional approved profile hints that map a phone number to a CRM lookup |

[commands.md](src/app/commands.md) lists the validation rules.
`template_setup.py configure` writes the environment settings to the azd
environment. For local runs, copy [.env.example](.env.example) to `.env`. Never
commit credentials.

| Setting | Purpose |
|---|---|
| `HUMAIN_ENABLED` | `false` keeps health and the UI available and refuses calls until credentials exist |
| `HUMAIN_AUTHORIZATION` | Complete server-side Authorization header value from HUMAIN |
| `HUMAIN_TOKEN_ENDPOINT` | HUMAIN token service; default `https://livekit-admin.sanad.ink/api/tokens/client` |
| `HUMAIN_VOICE` | Default voice, `mul_nourah` |
| `HUMAIN_ASR_MODEL` | `bayan_playground`; `bayan_api` and `bayan_realtime` are also accepted |
| `HUMAIN_LLM_PROVIDER` | `allam`, set by template preparation; the code and Bicep fallback is `gemma4` |
| `HUMAIN_TURN_TAKING_STRATEGY` | `fast_vad` (default) or `livekit_v1_mini` |
| `HUMAIN_INTERRUPTION_POLICY_MODE` | `off` (default), `control` or `shadow` |
| `AGENT_ASSIST_BASE_URL`, `AGENT_ASSIST_API_KEY` | Shared Assist API for CRM and knowledge tools and transcript ingestion |
| `AGENT_ASSIST_ENABLED` | `false` turns off transcript ingestion for `agent-assist` |
| `CUSTOMER_UI_URL` | Customer UI shown on the standalone voice page |
| `CORS_ALLOWED_ORIGINS` | Optional comma-separated browser origins; when unset, the caller's origin is allowed |
| `ACS_EMAIL_CONNECTION_STRING`, `ACS_EMAIL_SENDER` | ACS Email for the `email` module |

Phone and native settings are in their sections below. HUMAIN's
[provider guide](https://humain-voice-agent.share.sanad.ink/docs/connect.html)
is the authority for the provider contract.

## Accepted voice responsiveness

These are the defaults in code, `.env.example`, template preparation and Bicep:

```dotenv
HUMAIN_TURN_TAKING_STRATEGY=fast_vad
HUMAIN_INTERRUPTION_POLICY_MODE=off
```

They apply to new browser and phone sessions. `off` disables HUMAIN's custom
interruption policy, not LiveKit interruption. HUMAIN documents this pair as its
fast mode. `fast_vad` can end a turn on a short pause and treat brief speech
such as "okay" as an interruption, so test mid-sentence pauses and backchannels
as well as deliberate interruptions.

To roll back, set both `HUMAIN_TURN_TAKING_STRATEGY=livekit_v1_mini` and
`HUMAIN_INTERRUPTION_POLICY_MODE=control` in the azd environment and the running
app, then start a new session. Don't change only one of the pair. `/health`
reports the requested values, not what HUMAIN applied; check HUMAIN's effective
room metadata and real audio after a change.

A small synthetic comparison measured the time from the start of a prerecorded
interruption until browser output was quiet (below -50 dBFS for 500 ms), with
three web sessions per configuration:

| Turn taking + interruption policy | Range | Median |
|---|---|---|
| `livekit_v1_mini` + `control` (rollback) | 1.01-1.58 s | 1.50 s |
| `livekit_v1_mini` + `off` | 3.07-3.95 s | 3.12 s |
| `fast_vad` + `off` (default) | 1.23-1.36 s | 1.24 s |

In two probes, a brief "okay" ended an answer mid-sentence with the default pair
but not with the rollback pair. Neither pair started speaking during a 700 ms
pause in the caller's sentence. These aren't phone measurements or a latency
guarantee.

## Language

`organization.defaultLanguage` sets the initial language. The customer UI sends
its selected language when a browser session starts. A language change in the UI
reaches the running session through LiveKit's `lk.chat` input, without a
restart; a failed change shows a warning. A spoken request to switch language
lasts for the rest of the conversation. Speaking another language doesn't change
the response language by itself. The voice is fixed when the session starts.

For native calls with CRM selected, a validated contact's preferred language can
choose the language before the greeting. Later explicit requests take priority.

## Content and profiles

`system_prompt.md` adds persona instructions; it can't override the backend's
identity, recipient, CRM or handover rules. `knowledge.md` is appended to the
system message at startup, so covered questions need no retrieval. Nonempty
knowledge must start with `# Organization service knowledge` and include a
`Revision:` line; see
[approved knowledge](../../docs/deployment.md#approved-knowledge).

`profiles.json` maps approved phone numbers to a CRM lookup. A profile is a hint,
not identity verification, and doesn't allow calls or email. Caller ID isn't
identity verification either; unknown callers never receive another person's
data.

## Tools

- **CRM and knowledge.** The tools in `backend/tools/case_tools.py` call the
  authenticated Assist API at `/api/cases`. Assist owns Foundry IQ retrieval
  and the isolated Dataverse identity. `knowledge` adds
  a fallback search for questions the embedded knowledge doesn't cover. `crm`
  adds customer context, case listing, creation, lookup and follow-up, and a
  location-completeness check. Results stay below LiveKit's 15 KiB RPC limit.
- **Uncertain writes.** When the outcome of a write is unknown, the tool returns
  `outcome: unknown` with a lookup reference; the assistant must check status
  instead of claiming success or failure. A recorded case doesn't prove an
  external submission, dispatch or approval.
- **Email** (`send_customer_email`). Uses the bound CRM contact's address unless
  the caller gives one; callers without CRM context must give one. The recipient
  must match `emailPolicy`; with a bound CRM contact, only an exact
  `allowedRecipients` entry is accepted. Success means ACS returned `Succeeded`
  with an operation ID; it isn't proof of delivery.
- **Handover** (`talk_to_human`). See the phone and native sections below.

## Application phone (`application-phone`)

The module needs `ACS_SOURCE_NUMBER`, `ACS_CONNECTION_STRING`,
`ACS_CALLBACK_PATH` (the callback URL) and `ACS_MEDIA_STREAMING_WEBSOCKET_PATH`;
the service won't start without them. Provisioning creates ACS but doesn't buy a
number. [connect_phone_events.py](../../infra/scripts/connect_phone_events.py)
connects incoming-call events for the application ACS resource only.

- Inbound calls work without CRM or profiles. They can't transfer to a person.
- `POST /call` takes `number`, an optional `operator_number` (both E.164) and
  `language`. Both numbers must be in `phonePolicy.allowedOutboundNumbers`;
  approving profiles or generated records never allows dialing. With CRM
  selected, no call is placed if customer context can't be loaded.
- `talk_to_human` transfers only outbound `/call` sessions, to the request's
  operator number or `AGENT_PHONE_NUMBER`, which must also be approved. Browser
  sessions never transfer. A transfer request isn't proof that a person answered.
- Transcription after a handover needs `agent-assist`,
  `AZURE_COGNITIVE_SERVICES_ENDPOINT` and `ACS_TRANSCRIPTION_WEBSOCKET_PATH`.

Automatic conferencing is off by default. `ACS_AUTO_CONFERENCE_ENABLED=true`
requires `ACS_AUTO_CONFERENCE_NUMBER`, distinct from the source number and listed
in the outbound policy. When a call connects, the service checks the participant
list and invites that number once if it's absent. If the check or invitation
fails, the error is logged and the call continues; the service doesn't redial.
The participant is an audible member of the call, not a silent monitor, so give
any required notice and get consent. Keep it off when native media is enabled;
native media refuses to start otherwise. `/health` reports
`automatic_phone_conference_enabled`.

## Dynamics-native voice (`native-voice`)

Dynamics owns the number, conversation, recording, transcript and queue. The
Copilot Studio controller `NativeSpeechTurnPilot` calls this service with the
conversation's `msdyn_ConversationId`. The service validates the native
conversation, joins the call, sends only the verified caller's audio to HUMAIN
and plays HUMAIN's replies into the call. On handover it leaves the call, and the
controller runs the native **Transfer to agent** node, so the representative
joins the same Dynamics conversation.

[native_voice_setup.py](../../infra/scripts/native_voice_setup.py) renders the
private controller with your service origin and key.
[native-speech-turn-pilot.topic.yaml](dynamics/native-speech-turn-pilot.topic.yaml)
is a credential-free snapshot of it. The
[runbook](../../docs/dynamics-native-voice/README.md#roadmap-through-this-runbook)
lists the stages.

| Setting | Purpose |
|---|---|
| `DYNAMICS_NATIVE_MEDIA_ENABLED` | `false` by default; when `true`, allows one AI call at a time |
| `DYNAMICS_NATIVE_MEDIA_TRANSPORT` | `voip` (set for `native-voice` by template preparation) or `callautomation` (code default, earlier playback path) |
| `DYNAMICS_NATIVE_MEDIA_FAST_START` | `false` by default; `true` (VoIP only) overlaps registration with call setup and keeps one preloaded, unused browser page |
| `DYNAMICS_NATIVE_MEDIA_WORKSTREAM_ID` | Native inbound voice workstream |
| `DYNAMICS_NATIVE_MEDIA_ADDITIONAL_WORKSTREAM_IDS` | Optional comma-separated extra workstreams |
| `DYNAMICS_NATIVE_MEDIA_API_KEY` | Control API key, at least 32 characters |
| `DYNAMICS_NATIVE_MEDIA_ACS_CONNECTION_STRING` | The ACS resource connected to Dynamics, not the application ACS resource |
| `DYNAMICS_ORG_URL` | Dataverse URL, such as `https://example.crm17.dynamics.com` |

The service also needs the HTTPS origin from `ACS_CALLBACK_PATH`, a managed
identity registered as a Dataverse application user with read access to the
workstream, conversations, sessions and phone context, the call-metadata
observer below, and automatic conferencing turned off.

### Control API

Every request needs `X-API-Key: <DYNAMICS_NATIVE_MEDIA_API_KEY>`.

| Method and path | Purpose |
|---|---|
| `GET /dynamics/native/readiness` | Check configuration, Dataverse access and VoIP readiness without joining a call |
| `POST /dynamics/native/sessions` | Start the AI for one native conversation (HTTP 202) |
| `GET /dynamics/native/sessions/{conversation_id}` | Status; `?wait=true` waits up to 20 seconds |
| `POST /dynamics/native/sessions/{conversation_id}/turn` | Controller listening acknowledgement: `language` and `revision` only |
| `GET /dynamics/native/sessions/{conversation_id}/timings` | Startup timing events, without speech or audio |
| `POST /dynamics/native/sessions/{conversation_id}/stop` | Release the service's own call leg; the caller stays on the call |

```json
{
  "conversation_id": "<Dynamics conversation UUID>",
  "language": "en",
  "retain_native_controller": true,
  "speech_turn_control": true
}
```

### Behavior

- **Exact matching.** The service refuses a second concurrent call, other
  workstreams or queues, stale or assigned conversations, and anything it can't
  match exactly: conversation, controller session, ACS correlation ID and caller
  participant. It never picks the most recent call.
- **Audio.** Audio is 24 kHz mono PCM16. Only the verified caller's unmixed
  audio goes to HUMAIN.
- **VoIP transport.** A private headless browser in the container runs the ACS
  Calling SDK with a new ACS identity per call on the Dynamics ACS resource.
  HUMAIN speaks through that participant, so its speech appears in the native
  transcript. The Dynamics incoming-call subscription must keep its
  `data.to.kind = phoneNumber` filter so it doesn't take the SDK invitation.
- **Language changes.** With `speech_turn_control`, the controller acknowledges
  each listening turn. The `select_response_language` tool waits for the
  controller to confirm a new language before HUMAIN switches.
- **Handover.** `talk_to_human` stops AI audio and tools, closes the media stream
  and removes only the service's call leg. The controller transfers after status
  returns `action: handover` with `azure_leg_detached: true`. Status reports
  `human_acceptance: not_observed_by_media_adapter`, because a transfer request
  isn't a connected representative.
- **CRM.** With `crm` selected, the service loads the caller's eligible customer
  and case context before HUMAIN starts and greets by name when known. If the
  lookup fails or takes longer than 8 seconds, the session stops instead of
  using another profile.
- **Limits.** One active AI call; up to 16 recent sessions in memory, lost on
  restart. Setup must finish in 30 seconds, and the AI phase lasts at most 1,200
  seconds (20 minutes). The controller's loop is capped at 3,000 cycles. These
  limits don't end the caller's call.

Known gaps: HUMAIN appears as `Acs_Unknown` in the workspace transcript, and
automatic language detection after handover is unresolved. See the
[native reference](../../docs/dynamics-native-voice/reference.md) and
[operations guide](../../docs/dynamics-native-voice/operations.md) for measured
results, `VOICE_LATENCY` log queries, the optional startup notice and rollback.

### Controller files

| File | Use |
|---|---|
| [native-speech-turn-pilot.topic.yaml](dynamics/native-speech-turn-pilot.topic.yaml) | Credential-free snapshot of the current controller, regenerated by `infra/scripts/sync_native_voice_topic.py` |
| [native-speech-turn-crm-pilot.topic.yaml](dynamics/native-speech-turn-crm-pilot.topic.yaml) | The same snapshot with the CRM `CaseNumber` output |
| [native-supervision-pilot.topic.yaml](dynamics/native-supervision-pilot.topic.yaml) | The same snapshot with the Pause AI contract |
| `native-supervision.html`, `native-supervision.js` | Pause AI tab for the native workspace |
| [native-media-pilot.topic.yaml](dynamics/native-media-pilot.topic.yaml) | Earlier long-poll controller; the renderer's base template and a rollback reference |
| `native-language-probe.topic.yaml`, `native-transcript-boundary-probe.topic.yaml` | Diagnostics for owned test numbers; not deployment prerequisites |
| `startup-notice.ssml` | Source text for the optional recording notice |

### Call-metadata observer

[dynamics-handover-probe.bicep](../../infra/dynamics-handover-probe.bicep) adds
an Event Grid subscription on the Dynamics ACS resource that sends
`IncomingCall` metadata to `/dynamics/handover/probe/events`. The observer never
answers, rejects or redirects a call; the Dynamics subscription remains the only
answering application. It keeps only call identifiers and timestamps for 15
minutes, in memory, and discards the answer token, caller details, speech and
transcripts.

Configure `DYNAMICS_NATIVE_PROBE_RESOURCE_ID`, a dedicated
`DYNAMICS_NATIVE_PROBE_API_KEY`, and either `DYNAMICS_NATIVE_PROBE_EXPIRES_AT`
or `DYNAMICS_NATIVE_PROBE_PERMANENT=true`.

A diagnostic checks whether a second Call Automation connection can join a native
call and leave without ending it. Run it from the repository root with your
Azure CLI sign-in and a private scope file shaped like
[probe-scope.example.json](../../docs/dynamics-native-voice/examples/probe-scope.example.json):

```bash
python infra/scripts/probe_dynamics_call_control.py \
  --config <private-scope.json> --env-file <private-azd-env-file> \
  --conversation-id <native-conversation-UUID>
```

Without `--execute` it only reads the native records and observer metadata;
`--execute` allows one bounded attachment to an agreed test call. It never
dials, transfers or streams audio.

### Pause AI

`DYNAMICS_NATIVE_SUPERVISION_ENABLED`, `DYNAMICS_NATIVE_SUPERVISION_ACTOR_IDS`
and `DYNAMICS_NATIVE_SUPERVISION_CONTROLLERS` enable authenticated pause
commands from the native workspace. They need native media with the VoIP
transport. Follow the
[supervision recipe](../../docs/dynamics-native-voice/supervision-setup.md).

## Native outbound API

Native proactive calls use separate authenticated endpoints. They don't replace
`/call` or change inbound routing.

| Method and path | Operation |
|---|---|
| `GET /dynamics/outbound/readiness` | Read-only Dataverse identity and engagement checks |
| `POST /dynamics/outbound/requests` | Submit one native proactive voice request |
| `GET /dynamics/outbound/tasks/{task_id}` | Read the native task and its attempts |
| `POST /dynamics/outbound/tasks/{task_id}/cancel` | Cancel pending or future attempts; doesn't hang up an active call |

Every request needs `X-API-Key: <DYNAMICS_OUTBOUND_API_KEY>`. Configure
`DYNAMICS_ORG_URL`, `DYNAMICS_OUTBOUND_API_KEY` (at least 32 characters) and
`DYNAMICS_OUTBOUND_CONFIG_ID`. `DYNAMICS_OUTBOUND_ENABLED=false`, the default,
blocks new submissions but keeps status and cancellation available. Submission
also checks that the engagement is active, voice, AI-led, in Copilot dial mode
and linked to a workstream. The service authenticates to Dataverse with its
managed identity, which needs permission to create deliveries.

A submission schedules a real call when enabled:

```json
{
  "request_id": "<UUID generated once per logical callback>",
  "contact_id": "<Dynamics contact UUID>",
  "phone_number": "+12025550101",
  "not_before": "<ISO 8601 timestamp with offset>",
  "expires_at": "<later ISO 8601 timestamp with offset>",
  "consent_confirmed": true,
  "language": "en",
  "context": {
    "CaseReference": "CASE-123"
  }
}
```

- `expires_at` must be in the future, after `not_before`, and at most 30 days
  ahead. Both are converted to UTC. It limits when attempts start, not call
  length.
- `context` allows up to 16 string values of at most 512 characters and 4 KiB in
  total. Names starting with `moacc_` are reserved; the service adds
  `moacc_request_id` and `moacc_language`.
- HTTP 202 returns `request_id`, `delivery_task_id`, `delivery_id` and
  `status_url`. It means Dynamics accepted the delivery, not that anyone
  answered.
- After a timeout, retry with the same `request_id`; Dynamics deduplicates on it.
  HTTP 429 keeps `Retry-After`. The service never retries writes itself and has
  no fallback to direct ACS calls.
- The API only schedules native calls. It doesn't attach HUMAIN to them.

Start with one concurrent call, no automatic reattempts or SMS fallback,
explicit calling hours, and recording and transcription; inbound settings
aren't inherited. Dynamics proactive voice needs a supported outbound number:
ACS Direct Routing or a supported Teams Phone number, not an ordinary ACS Direct
Offer number. Check
[Microsoft's requirements](https://learn.microsoft.com/en-us/azure/communication-services/concepts/interop/tpe/teams-phone-extensibility-connectivity-cost#outbound-calling-prerequisites)
and the ACS retirement notice in the [root README](../../README.md).

## Run and test locally

Use Python 3.12 from this folder:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r src/app/requirements.txt
cp .env.example .env
PORT=8002 python src/app/app.py
```

The neutral `deployment.json` turns every module off. To try a module locally,
run from a prepared workspace, or enable it in a local copy of
`deployment.json` that you don't commit.

```bash
PYTHONPATH=src/app python -m unittest discover -s tests -v
npm --prefix src/app/backend/native_rtc ci
npm --prefix src/app/backend/native_rtc run build
npm --prefix src/app/backend/native_rtc test
```

The tests mock HUMAIN, ACS, Dataverse and email; they don't place calls or send
email. Separately check real credentials, speech in both languages,
interruptions, tool failures, email recipients and the selected handover path.

## Original accelerator contributors

- [Carlos Raul Garcia](https://www.linkedin.com/in/carlosgarcialalicata/)
- [Sasa Juratovic](https://www.linkedin.com/in/sasajuratovic/)
- [Richard Lagrange](https://www.linkedin.com/in/richard-lagrange/)
- [Sanjay Singh](https://www.linkedin.com/in/san360/)
- [Robin-Manuel Thiel](https://www.linkedin.com/in/robinmanuelthiel/)
- [Yimi Wang](https://www.linkedin.com/in/yimiwang/)

Based on [on-the-road copilot](https://github.com/Azure-Samples/on-the-road-copilot).
Use of Microsoft and third-party trademarks remains subject to their respective
brand policies and must not imply sponsorship.
