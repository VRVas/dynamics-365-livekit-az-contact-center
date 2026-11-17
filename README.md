# Dynamics 365 + LiveKit Contact Center on Azure

A modular contact center for Azure: customer chat, HUMAIN voice agents over
LiveKit, and Dynamics 365 Contact Center for phone calls, cases and handover to
people. You choose the capabilities. An AI coding agent prepares a private copy
of this template and deploys it with Bicep, azd and guarded setup scripts. It
stops for the steps a person has to do: sign-in, licenses, costs, approvals, a
few portal screens and test calls.

**Start with [the deployment guide](docs/deployment.md).** In GitHub Copilot,
run `/deploy-contact-center`. With another coding agent, ask it to follow
[the deployment skill](.github/skills/deploy-contact-center/SKILL.md).

## What you can deploy

| Capability | Module |
|---|---|
| Customer text chat | `web-chat` |
| Browser HUMAIN voice | `browser-voice` |
| Application-owned ACS telephone calls | `application-phone` |
| Dynamics-owned telephone calls and native queue handover | `native-voice` |
| Shared Dataverse customer and case API | `crm` |
| Approved-source Foundry IQ retrieval | `knowledge` |
| ACS Email with recipient restrictions | `email` |
| Custom-chat representative console | `agent-console` |
| Agent-assist sessions, insights and supervisor views | `agent-assist` |
| Scoped Dynamics representative forms, scripts and macros | `native-workspace` |
| Approved bilingual knowledge articles in Dynamics | `native-knowledge` |
| Dynamics Copilot knowledge answers and summaries | `native-copilot` |
| Explicit, authenticated Pause AI for supervisors | `pause-ai` |

The deployment plan shows each module's dependencies before you approve it.
Selecting Dynamics voice doesn't add CRM, a custom agent desktop, knowledge,
email or the application phone conference. See the
[module boundaries](docs/deployment.md#module-boundaries).

## Notice: Azure Communication Services retirement

> [!IMPORTANT]
> Microsoft is retiring standalone Azure Communication Services (ACS) capabilities
> on **30 September 2028**. This template's telephony and email modules use them.
> Check eligibility before you select those modules.

- **Phone numbers.** Customers without an existing ACS resource can no longer
  acquire or port ACS phone numbers. The
  [Dynamics 365 guide](https://learn.microsoft.com/en-us/dynamics365/contact-center/administer/migrate-from-azure-communication-services)
  dates this from 30 September 2026; the
  [ACS guide](https://learn.microsoft.com/en-us/azure/communication-services/acs-retirement-and-breaking-changes-guide)
  applies it to customers without an ACS resource created before 23 October 2026.
  For those customers, this blocks `application-phone`, `native-voice` and the
  modules built on it (`native-workspace`, `native-knowledge`, `native-copilot`,
  `pause-ai`). Existing ACS customers can still buy numbers for now, but
  Microsoft may add restrictions during the transition.
- **Dynamics 365 voice.** Dynamics voice that runs on ACS is supported until
  30 September 2028 and has to move to Teams Phone extensibility before then.
- **Calling and media APIs.** After the retirement date, ACS Call Automation,
  the Calling SDK, audio streaming and call recording are supported only in
  Teams scenarios such as Teams Phone extensibility. Both HUMAIN phone adapters
  in this template use these APIs.
- **Email.** ACS Email retires on the same date. It still accepts new
  resources, but Microsoft says that can change at any time. The `email` module
  supports only ACS Email; Microsoft lists Exchange Online and Marketplace
  providers as alternatives for external email.

Microsoft's replacement voice path for Dynamics 365 Contact Center is
[Teams Phone extensibility](https://learn.microsoft.com/en-us/dynamics365/contact-center/administer/configure-teams-phone-in-voice-channel),
with Teams Calling Plan, Operator Connect or Direct Routing providing the phone
line. **Teams Phone support in this template is work in progress.** Running
HUMAIN on Teams Phone calls isn't implemented or verified yet.

`web-chat`, `browser-voice`, `crm`, `knowledge`, `agent-console` and
`agent-assist` don't use ACS and aren't affected.

## What ships in the template

- No customer branding, profiles, service facts or records. The runtime defaults
  are neutral.
- Branding, welcome text, quick actions, languages, persona, approved sources and
  case categories come from a private deployment profile.
- Optional fictional records are generated and reviewed during deployment. Their
  values look natural; their origin is recorded in private receipts and
  technical fields.
- Approving generated data doesn't authorize email delivery or phone calls.
- Test fixtures are technical, not a deployable dataset.

## Components

| Component | Purpose |
|---|---|
| [Customer UI](apps/chat/ui_v2) | Chat, browser voice and approved outbound-phone controls |
| [Voice](apps/voice) | HUMAIN bridge, application telephony and the native media adapter |
| [Chat orchestrator](apps/chat/orchestrator) | Chat and the selected business tools |
| [Assist](apps/assist) | Shared CRM and knowledge API; optional agent-assist views |
| [Custom representative console](apps/chat/agent_console) | Handover for custom chat, separate from the Dynamics workspace |
| [Infrastructure](infra) | Conditional Azure resources and guarded Dynamics setup tools |

The backend, not the prompts, enforces CRM identity, case ownership, idempotency
and handling of uncertain writes.

## Dynamics-native voice

Dynamics owns the number, conversation, recording, transcript and representative
queue. HUMAIN supplies the conversational AI. On handover, the application's
media and control legs leave the call before the native controller runs
`TransferToAgent`, so the representative joins the original Dynamics
conversation. No extra phone leg is dialed.

Read only the parts you selected: the
[native deployment runbook](docs/dynamics-native-voice/README.md),
[call-flow diagram](docs/dynamics-native-voice/native-call-flow.html),
[CRM recipe](docs/dynamics-native-voice/crm-setup.md),
[workspace recipe](docs/dynamics-native-voice/workspace-setup.md) and
[Pause AI recipe](docs/dynamics-native-voice/supervision-setup.md).

## Limits

- Telephony depends on what your tenant can buy. Check licenses and
  phone-number eligibility first; see the ACS notice above.
- Native voice handles one AI call at a time, with up to 20 minutes of AI
  conversation before the call moves to a representative.
- Callers hear the recording notice about 5–7 seconds after the call connects.
- The HUMAIN defaults (`fast_vad` turn detection, interruption policy `off`) are
  the tested settings, not an end-to-end latency guarantee.
- Native voice and the managed-identity CRM recipe need the Azure subscription
  and the Dynamics environment in the same Microsoft Entra tenant. Basic CRM
  runs without native voice.
- A case recorded in Dynamics doesn't prove that an external action, approval,
  dispatch or payment happened.
- A successful deployment isn't acceptance. Check the UI and place a real test
  call before anyone relies on it.

## Development and verification

Use the application lockfiles and the
[infrastructure dependencies](infra/scripts/requirements.txt). The
[verification section](docs/deployment.md#verification-and-handoff) lists the
checks and test runners for each module.

Don't run `azd up` from the unprepared checkout: the preparation hooks stop it
before provisioning. Don't apply these module defaults to an existing deployment
without a reviewed change plan and its original receipts.

## Authors

Created by [VRVas](https://github.com/VRVas) (vvasilescu@microsoft.com).

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). This project has adopted the
[Microsoft Open Source Code of Conduct](https://opensource.microsoft.com/codeofconduct/).
Report security vulnerabilities as described in [SECURITY.md](SECURITY.md).

## Trademarks

This project may contain trademarks or logos for projects, products, or services. Authorized use of Microsoft
trademarks or logos is subject to and must follow
[Microsoft's Trademark & Brand Guidelines](https://www.microsoft.com/legal/intellectualproperty/trademarks/usage/general).
Use of Microsoft trademarks or logos in modified versions of this project must not cause confusion or imply Microsoft sponsorship.
Any use of third-party trademarks or logos are subject to those third-party's policies.

## License

MIT. See [LICENSE.md](LICENSE.md).
