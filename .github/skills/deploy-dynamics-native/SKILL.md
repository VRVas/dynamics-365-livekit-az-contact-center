---
name: deploy-dynamics-native
description: "Deploy or hand over the Dynamics-native voice modules of this contact center template. Use when setting up native telephony, resuming its guarded stages, or preparing a native deployment handover after modular preparation."
---

# Deploy the Dynamics-native voice modules

Follow the [native sub-workflow](../../../docs/dynamics-native-voice/deploy-with-copilot.md).
First complete [modular selection and private preparation](../../../docs/deployment.md).
This skill covers a selected sub-workflow; it isn't an instruction to enable
every module. Use the [native runbook](../../../docs/dynamics-native-voice/README.md)
for portal paths and complete commands, and read the relevant chapter before
running its tool.

## Target

- Dynamics owns the number, call, recording, transcript and representative
  queue.
- HUMAIN supplies the conversational AI; the routing agent keeps call and
  language control.
- Use `HUMAIN_TURN_TAKING_STRATEGY=fast_vad` and
  `HUMAIN_INTERRUPTION_POLICY_MODE=off`, the accepted defaults.
  `livekit_v1_mini` + `control` is an explicit rollback, not the default.
- The application alone plays the packaged 1.3x recording notice. Callers hear
  it 5-7 seconds after the call connects; shortening that is work in progress
  and on hold. Don't reintroduce the reverted latency optimization, IVR music
  trial, native-only WAV playback or a timed greeting.
- Voice conversations must run on the `Elastic` pipeline. Switzerland
  (`crm17`) is the default region, and `contact_center_setup.py pipeline` is
  the gate in every environment.
- When selected, voice and custom text chat share the Dataverse CRM API.
- Native workspace, native knowledge and native Copilot are separate
  selections. Don't publish placeholder articles or require Copilot for the
  basic workspace.
- Explicit Pause AI uses its own schema, roles, controller pins and tab. It is
  never included automatically.
- Native-only handover needs no application-owned phone number.

## Working rules

The deployment guide's working rules apply:

- CLI first. Where no CLI path exists, give one UI screen at a time with every
  value filled in. No browser automation.
- Read back after every write. PAC exit codes are hints.
- Get one approval for the agreed routine scope; ask again only for new
  consequences.
- After a failure, read back and retry once with a concrete change. After the
  second failure, stop and ask the owner.
- Report each component as connected, configured or accepted on a real call,
  explained as the caller's journey.

## Before running commands

1. Confirm this is the intended repository and read local instructions.
2. Inspect Git status. Preserve unrelated work and record the reviewed source
   revision; never reset the worktree.
3. Reuse the approved private profile. Ask only for consequential missing
   inputs, and discover IDs after sign-in.
4. Confirm the pipeline gate and number eligibility before creating telephony
   resources. Don't assume a new organization can acquire ACS numbers.
5. Use the private directory outside the checkout and the one azd environment.
   Secrets go in the private editor or store, never chat or Git.
6. For an existing environment, inspect the original component receipts and
   current native state before proposing a change. Never adopt records by
   name; `discover` marks Microsoft's own records `owned: false`.

## Execute by stage

The sub-workflow defines stages 0-12. For each stage:

1. Read its inputs and the preceding exit checks.
2. Record planned commands and affected resources in the private ledger. Use
   argv-based execution or the documented shell blocks; never build `eval`
   commands from configuration.
3. Run read-only discovery, plan or preview first.
4. Stop at the stated human gate. Approval for one stage doesn't authorize
   phone purchases, broader roles, destructive rollback or unrelated features.
5. Apply only the reviewed scope through the repository's tool.
6. Read back the exact records and runtime outputs, and keep the component
   receipts.
7. Mark the stage complete only when its required evidence exists.

Tools by stage:

- Stage 3: `native_event_delivery.py bootstrap`, the native ACS resource, then
  `native_voice_setup.py copy-sheet` and `connection`.
- Stage 4: `acs_numbers.py`, Sync from Azure,
  `native_event_delivery.py subscriptions` and `verify`, the shell queue and
  workstream, the routing agent and the wizard values.
- Stage 5: `native_voice_setup.py reader-role` and
  `check_native_reader_runtime.py`; `setup_dynamics_crm.py` when CRM is
  selected.
- Stages 6 and 7: `native_voice_setup.py new-keys`, `bundle` and `stage`, then
  `pac_publish.py`.
- Stage 8: runtime settings, `native_voice_setup.py startup-audio` and the
  observer; `native_runtime_settings.py` for later settings-only changes.
- Stage 9: `setup_dynamics_workspace.py`, including the `representative` stage
  and `--copilot-wait-minutes`.
- Stage 10: `setup_native_supervision.py` and
  `check_native_supervision_runtime.py`.

Historical `upgrade-*` and `--legacy-baseline` operations aren't prerequisites
for a fresh deployment.

Run one azd deployment per environment at a time. Native stages that share a
journal run one after another. Parallelize only independent read-only discovery
or checks.

## Human gates

Stop for:

- Licensing, terms, service eligibility, regions, charges and data policy.
- Azure, Entra and Dynamics administrative consent and role-plan approval.
- Provider authorization entry and phone number purchase.
- The native ACS connection, phone sync, the Event Grid banner check, and the
  routing agent, workstream and topic shells.
- The Copilot Topic checker, publication and review of unrelated saved drafts.
- The Copilot opt-in, and the representative's browser, microphone, presence
  and actual forms and knowledge.
- Fresh consent and readiness for each recorded or charged test call, after
  giving the operator the workspace URL and the time cap.

The owner may authorize the agent to run a supported administrative command;
the agent still records the approval and checks the result. Never substitute a
private API, another telephony architecture or administrator access when a
documented step is blocked.

## Verification rules

- Keep native and CRM identities separate. Verify the deployed managed
  identity from inside the container, not only the administrator's access.
- Don't enable Pause AI until its schema, published controller pins and
  deployed-identity preflight pass.
- Allow at least 15 minutes after a native workstream configuration readback
  before caller acceptance. Restart the wait after another change.
- A saved value, a successful publication or an HTTP health response doesn't
  prove audio playback, UI behavior or a working call.
- No made-up conversation IDs for live startup. No automatic test calls.
- Never weaken caller, ownership, workstream, ETag, cancellation or Pause AI
  guards to get a green result.
- On uncertainty, keep the original operation and receipt and use the
  documented recovery. Never create a replacement ID or blindly retry a write.

## Handoff

Provide:

- Source revision, reviewed differences and built image references.
- Target resource and native IDs, configuration files and immutable receipt
  paths.
- Secret-store locations and rotation owners, without secret values.
- Publication, runtime identity, observer, CRM and workspace verification.
- Separate UI and audio acceptance, and explicit pending items.
- Scoped rollback instructions and the 5-7 second notice delay that is on
  hold.

Use direct wording and exact commands. Report a blocker with its evidence and
the decision needed next. Don't describe a deployment as further along than
its evidence shows.
