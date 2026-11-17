# Channel integration

Use [modular preparation](deployment.md) to select services and generate private
configuration. Do not copy URLs, keys or records from a reference deployment.

## Shared CRM and knowledge API

The existing Assist service hosts `POST /api/cases`, the shared CRM and
knowledge API for voice and chat.

- `crm` enables eligible customer context, case reads, confirmed case creation
  and confirmed append-only follow-up.
- `knowledge` enables approved-source Foundry IQ retrieval.
- Disabled modules reject the corresponding operations explicitly.
- Server-side voice/chat clients authenticate with the private ingest credential.
  Never place that credential in public UI configuration.
- Identity, ownership, idempotency, confirmation and uncertain-write handling
  remain backend responsibilities. A private profile hint is not authentication.

See the [shared API implementation](../apps/assist/apps/console/src/lib/case-service.ts),
[voice client](../apps/voice/src/app/backend/tools/case_tools.py) and
[chat client](../apps/chat/orchestrator/case_tools.py) for exact request/response
schemas. CRM-only deployments do not require Cosmos, SignalR or native voice.

The current CRM scenario is gated to explicitly eligible synthetic profiles.
Real-customer authentication/onboarding and production authorization are not
established by selecting a profile or matching caller ID.

## Optional custom agent-assist stream

`agent-assist` enables the existing session/transcript/insight UI and its Cosmos,
SignalR and model dependencies. The voice adapter uses
`AGENT_ASSIST_BASE_URL`, `AGENT_ASSIST_API_KEY` and `AGENT_ASSIST_ENABLED`.
Use values from this deployment, not a hardcoded example endpoint.

The [client](../apps/voice/src/app/backend/agent_assist.py) retains stable session
and message identifiers, speaker labels, escalation and completion operations.
The [API routes](../apps/assist/apps/console/src/app/api) are the contract for
consumers. Check authentication and current feature availability before writing
another client; do not use sample credentials or assume every UI endpoint is a
production identity boundary.

Native Dynamics sessions deliberately do not create a second Assist transcript
session. Selecting this module is not a native Dynamics analytics integration.
Use the native workspace and its separately verified platform capabilities for
native conversations.

## Email and phone

Email is real ACS Email, not a fake success fallback. The selected channel must
confirm a recipient and satisfy its delivery policy. Synthetic contexts require
an exact approved recipient; dataset approval is not delivery approval.
An accepted send operation does not prove delivery or reading.

Application-owned incoming phone calls need no CRM/profile dataset. Outbound
calls and operator/conference destinations need the separate exact dialing
policy. Native Dynamics handover is a different mechanism and does not use this
phone-number allowlist.

## Private configuration and evidence

Keep system prompts, full knowledge, profile records, delivery policies,
credentials and generated native bundles out of browser configuration.
Only approved presentation enters the public frontend files.

Distinguish source checks, target readiness, actual UI behavior and a completed
caller test. Read back the actual runtime identity and retain private component
receipts; an administrator's successful request is not application readiness.
