# Voice runtime configuration

Stage validated `deployment.json`, approved `system_prompt.md` and `knowledge.md` beside
`app.py`. The checked-in files contain neutral defaults, no selected modules and no knowledge.
Nonempty knowledge must start with `# Organization service knowledge`, include a `Revision:`
line and fit in 64 KiB. Private persona content cannot override runtime safety rules.

Optional `profiles.json` contains approved selector/phone mappings only. It is empty by
default; it never replaces Dynamics customer context. Application `/call` requires its exact
destination in the private `phonePolicy.allowedOutboundNumbers`, not a profile mapping.
Inbound application calls work with no profiles, CRM or outbound allowlist.
Email requires both the selected module and an explicit recipient/domain policy;
an empty policy denies sending. A synthetic CRM recipient requires an exact `allowedRecipients`
entry; allowing its domain alone is insufficient.

`browserVoice` gates `/realtime` independently of native media. `nativeVoice` selects the native
voice module; its existing environment authorization, controller and workstream gates still apply.
Native CRM context
verification is retained when `crm` is selected, and omitted when it is not. `applicationTelephony`
alone requires application ACS calling environment variables. `agentAssist` controls transcript
ingestion independently of CRM and knowledge. `/config` exposes only organization presentation
and feature flags, never private mappings or email policy.

Automatic application-phone conferencing is off by default. To enable it, explicitly set
`ACS_AUTO_CONFERENCE_ENABLED=true` and an approved E.164 `ACS_AUTO_CONFERENCE_NUMBER` distinct
from `ACS_SOURCE_NUMBER`. That destination and every application operator transfer must also
appear in `phonePolicy.allowedOutboundNumbers`. There is no bundled conference destination;
native transfer is separate and needs no application-owned ACS number.

The accepted HUMAIN defaults remain `fast_vad` and `off`. Native media ownership, recording
notice, pause controls and same-conversation transfer are unchanged.