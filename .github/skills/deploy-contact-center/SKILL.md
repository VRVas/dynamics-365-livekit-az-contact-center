---
name: deploy-contact-center
description: "Deploy, customize or resume this modular contact center template. Use when setting up a new tenant, choosing Azure or Dynamics channels, preparing a branded demo, generating private fictional data, or handing over a deployment to another agent."
---

# Deploy the modular contact center

Follow [the deployment guide](../../../docs/deployment.md). Use the
[profile example](../../../infra/template-profile.example.json) and the
[preparation CLI](../../../infra/scripts/template_setup.py); don't build a new
deployment engine. Bicep, azd and the guarded setup tools are the executors.

## Working rules

- CLI first: the template's scripts, azd and Bicep, PAC, the Azure CLI and the
  Dataverse Web API. No browser automation and no endpoints copied from browser
  traffic. Call an undocumented Dataverse action only where the guides give the
  exact request.
- Read back server state after every write. PAC exit codes are hints.
- Discover with read-only commands before asking the owner anything.
- Where no CLI path exists, or it failed, give one UI screen at a time with
  every value filled in.
- Get one approval for the agreed routine scope. Ask again only for a cost, a
  purchase, publication, consent, a live call or a destructive change.
- When a step fails, read back, then retry once with a concrete change. If it
  fails again, stop and ask the owner, showing the command, the error, the
  readback and what would unblock it. No workarounds while you wait.
- Report each component as connected, configured or accepted on a real call.
- Don't answer HUMAIN pricing or data-handling questions from this repository.
  Research HUMAIN's current documentation and cite it.
- Don't quote Azure prices from memory; use `template_setup.py scope --prices`.

## Conversation

1. Use structured questions/options for one concise brief: organization and
   use case, audience, channels, existing systems, approved sources and brand
   guidelines. Say why each input matters, and give the owner the private
   folder for their files.
2. Ask once about additional tools. Keep the existing email and Foundry IQ
   capabilities when selected; don't invent an integration from its name.
3. Propose modules marked include, defer or exclude with a reason for each
   exclusion, plus design, language, persona, tools, content, the Azure scope
   and cost, and for Dynamics voice the Switzerland region and its
   consequences. Offer the presenter modes for fictional data.
4. Show the proposal and get approval. Don't ask about every color or
   low-level parameter.

## Privacy and customization

- Keep all generated profiles, sources, branding, datasets, credentials and
  deployment evidence in an owner-only directory outside the source checkout.
- Don't collect secrets through chat. Use the private editor or store.
- Default to no records and no business facts. Generate fictional records only
  after approval, with natural names and coherent values, not obvious demo
  labels.
- Keep synthetic provenance in private receipts and technical metadata. Don't
  invent policies, costs, requirements, delivery promises or external receipts.
- Data approval isn't delivery approval. Outbound numbers and email recipients
  have separate policies, and each live test call needs fresh consent.
- Use the public configuration only for approved presentation. Never include a
  system prompt, full knowledge, contact records, credentials or a delivery
  allowlist.
- Presets are starting points. Build requested bespoke UI or integrations in
  the private workspace, test them and keep the reviewed source and evidence.

## Execution

1. Inspect source status and preserve unrelated changes. Record the reviewed
   revision.
2. Create the private workspace and Python environment. Set `AZURE_CONFIG_DIR`
   and `AZD_CONFIG_DIR` inside it before anyone signs in. Every tool call is a
   fresh shell, so reload the private session values each time.
3. Initialize the private profile. `validate`, `plan` and `scope` show missing
   inputs, dependencies and the Azure footprint. A valid draft isn't
   deployment readiness.
4. For Dynamics modules, install PAC 2.14.1 in the private workspace and
   discover first. Create a new environment with Customer Service only, then
   run `contact_center_setup.py voice-channel` and gate on
   `contact_center_setup.py pipeline`, which must report Elastic.
5. For phone modules, run `contact_center_setup.py eligibility`, then
   `acs_numbers.py`. Recommend UK local numbers that make and receive calls,
   and buy only after the owner approves the monthly cost.
6. Review content and data, then `prepare` a new private workspace.
7. Build any requested additional tools through the existing registries, and
   record tests and approval in `extension-review.json`.
8. Create the new azd environment, run `azd env set PYTHON` with the private
   interpreter, then `configure` and `preview`. Stop for Azure scope and cost
   approval. Never use the internal preview flag for writes.
9. Provision, deploy and run only the selected follow-on phases. Push a
   purchased application number with `native_runtime_settings.py`.
10. Use `status` and `record` for coordination, and the component journals for
    ownership and recovery. Never blindly replay a timed-out write.

Use [the native sub-workflow](../../../docs/dynamics-native-voice/deploy-with-copilot.md)
only when native voice is selected. It adds `native_event_delivery.py`, the
`copy-sheet`, `connection` and `reader-role` steps,
`check_native_reader_runtime.py`, `pac_publish.py` and
`native_runtime_settings.py`. CRM alone doesn't need voice workstreams. Native
workspace, with its `representative` stage and `--copilot-wait-minutes`,
knowledge, Copilot and Pause AI are distinct selections.

## Human stops and verification

Stop for licensing, terms and charges; sign-in and consent; permissions;
environment creation; number purchase; the native connection and the agent and
workstream UI steps; publication; recipients; and actual UI and audio
acceptance. Approving the brief isn't blanket authority for these.

Keep the native caller, ownership, ETag, cancellation, transcript and handover
guards. Keep `fast_vad` + `off`; provider changes need the live HUMAIN contract
and the effective metadata. Native Pause AI is explicitly latched and never
inferred from Monitor or Join. Wait at least 15 minutes after workstream
configuration changes.

Verify selected features and the absence of unselected side effects, including
the phone-size UI evidence check. Keep private evidence, and keep tests,
readback, UI checks and caller acceptance separate. State the limits: one AI
call at a time, a 20-minute AI phase, and the recording notice 5-7 seconds
after the call connects, which is work in progress and on hold.

Finish with a concise handover: selected modules, source and images, private
receipts, secret locations without values, verified outcomes, pending gates and
scoped rollback. Don't describe a component as further along than its evidence
shows.
