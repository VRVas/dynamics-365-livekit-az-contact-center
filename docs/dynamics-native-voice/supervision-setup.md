# Native Pause AI — opt-in first slice

This adds a deliberate **Pause AI** control to a native Dynamics conversation.
Native Monitor and Join remain native actions; neither triggers pause automatically.
Pause does not claim that a human joined, transfer ownership, queue another
representative, end the call, or resume when someone leaves.

The feature is **off by default**. No shared API key, service URL, delegated-token
shortcut, anonymous wake endpoint, new MSAL registration, or external agent console
is used by the browser. The existing private controller key stays server-side/in
the retained Copilot topic. Numerical metrics and friendly speaker names remain
outside this slice.

## Trust and runtime contract

The HTML application tab uses the documented
[`Microsoft.Omnichannel.getConversationId()`](https://learn.microsoft.com/en-us/dynamics365/customer-service/develop/reference/methods/getconversationid)
and checks the focused APM session before/after asynchronous calls. A session ID,
case, phone number, latest call, or tab URL is never used as a substitute.
The API can still return a closed conversation; the backend independently checks
the exact active native conversation, controller and allowlisted workstream.

Authenticated `Xrm.WebApi.online.createRecord` creates one user-owned
`moacc_nativeaicommand` row with a stable request UUID, `moacc_conversationid` and
`moacc_action=pause`. Native `CreatedBy` and `CreatedOn` are authoritative.
Only explicitly approved, enabled, licensed interactive human IDs are accepted.
Application users, impersonated/imported requests, other workstreams, stale
sessions, unsupported actions and requests older than 30 seconds are rejected.
The runtime uses an ETag claim before executing; a stale `processing` receipt is
marked unconfirmed rather than executed again.

The requester role grants Create and user-level Read only. It grants no
Write/Delete/Assign/Share. Custom result fields (`moacc_outcome`, `moacc_reason`,
`moacc_ackrevision`, `moacc_ackon`, `moacc_inflight`) have column security:
requesters may read, but cannot create or update them. The runtime application
receives Read/Write on this custom table and the necessary existing-user/bot
reads, plus organization-level `prvReadEntity`, `prvReadAttribute` and
`prvReadRelationship` for the
runtime table/secured-column metadata preflight,
not metadata writes, business-record writes or command Create. Existing roles
are preserved. An administrative schema check is not proof that this runtime
identity can perform the same read.
The two request columns are also field-secured: humans can Create/Read them,
the runtime can Read them, and neither role can Update them after creation.
Native privilege names retain the custom table's schema capitalization. The
setup resolves those names case-insensitively while requiring a unique exact
privilege set and the reviewed depths. New roles can also receive native
SDK/plug-in read and SharePoint defaults. The kit removes only the explicitly
recognized defaults, only from its own still-unassigned roles, and verifies the
exact narrow grants before assignment. Unexpected privileges or later drift
stop setup; existing user roles are never rewritten.

**System Administrators bypass column security.** An existing administrator used
as an approved tester is a trusted infrastructure operator, not a negative
permission test. Set `trust_system_administrators=true` only deliberately.
Do not silently remove roles or create/invent another licensed user.
[Microsoft column-security limitations](https://learn.microsoft.com/en-us/power-platform/admin/field-level-security)
apply. A real, approved non-admin supervisor still must verify denied forged
outcome Create/Update and denied request Update/Delete/Assign/Share.

### Documented table-then-column provisioning

New plans pin both the final `schema` and the exact `schema_requests` sequence.
The first `POST EntityDefinitions` contains **only the primary-name column**, with
Description, Text format and the full RequiredLevel managed property (`Value=None`,
`CanBeChanged=true`, `ManagedPropertyLogicalName=canmodifyrequirementlevelsettings`).
Creation does not set `EntitySetName` or `IsOptimisticConcurrencyEnabled`; their
expected native defaults are verified from readback before continuing.

The seven secured columns are then created individually through
`EntityDefinitions(<verified MetadataId>)/Attributes`, with the owned solution
header and per-column intent/readback. The final result is unchanged: primary name,
platform-generated ID and seven secured command columns, with the same permissions.
After a successful column POST, up to five metadata reads (500ms apart) tolerate
the observed expanded-parent cache lag; the create request is never repeated by
that readback loop. Continued absence stops the stage as unconfirmed.
Setup metadata reads use Microsoft's documented `Consistency: Strong` header to
obtain current definitions rather than the up-to-30-second metadata cache.
[Microsoft's documented metadata contract](https://learn.microsoft.com/en-us/power-apps/developer/data-platform/webapi/create-update-entity-definitions-using-web-api)
is the basis for this request sequence.

The journal records `schema_table` and `schema_column_<logical name>` separately;
`schema` is complete only after every column is verified. Recovery checks the
recorded parent/column identities and exact definitions rather than recreating
successful writes or adopting a matching column after a definite rejection.
Access and publication require the complete schema. If a request fails partway,
see [When a Dataverse request fails](#when-a-dataverse-request-fails).

An earlier all-columns-in-one request returned generic `400 / 0x80040216` without
diagnostic detail. That does **not** prove which request difference caused it.
This is a documented API-contract adaptation; native acceptance must still be
observed. Never edit the old immutable plan to use the new sequence. If its
rollback rejects an unresolved schema intent, stop before deleting anything and
obtain an explicitly reviewed recovery path.

The runtime polls at approximately one second with an active session and five
seconds when idle, eight rows per batch. Idle sweeps explicitly reject closed or
no-AI requests. Throttling uses bounded backoff. The UI polls for at most 45
seconds, then shows **unconfirmed**, never success. A lost create response is
reconciled using the same request ID, not a replacement.
If a later explicit check confirms that the record does not exist, the control
rechecks conversation focus and may retry Create once with that **same UUID**.
Network errors, timeouts and access-denied reads do not authorize a create retry;
an ambiguous/duplicate create response is followed by another read of the same row.

**Requested** means only that a command was submitted. **Pausing** means the
backend claimed it. **Paused** requires a correlated private-worker receipt after
the AI microphone is muted and scheduled PCM is cleared. Input buffers and
in-flight captures are fenced; subsequent provider output is discarded at both
server and browser. New tools are blocked. Up to eight previously admitted tools
may finish under their existing, maximum-30-second deadlines. Their external
writes are not cancelled, rolled back or blindly retried. The UI separately
warns about operations still in flight.

Pause is latched separately from native handover. There is no resume action in
this slice. At the configured 20-minute AI limit, or on provider loss, only the
application's media resources are released. The retained controller keeps a
quiet supervised-conversation path until native end/ownership change. After
five failed native context checks it falls back to a silent native question
hold, without further external retry, generic failure speech or TransferToAgent.
This keeps resources bounded while the native platform owns the call lifetime.
Normal, unpaused handover keeps its existing verified-detach gate.
The extended limit requires the updated voice image; earlier calls used 120
seconds. Pause still gates AI audio/tools immediately and does not reset the
deadline. Full-duration paused-call acceptance remains a separate check.

## Prerequisites and ownership boundary

1. Deploy the reviewed voice code with supervision **disabled**. Keep the existing
   native observer, VoIP, speech-turn, recording notice, language and CaseNumber
   configuration. Verify `/health` reports
   `native_media_pilot.supervision_enabled=false`; the authenticated
   `/dynamics/native/readiness` response exposes `supervision_enabled` directly.
2. Use the existing isolated Azure CLI profile and pinned tenant. The setup identity
   needs metadata/security/webresource privileges; the runtime identity does not.
   This first slice requires the approved human accounts and runtime application
   user in the configured business unit. It never moves users between units.
   Human testers need their existing native workspace/app-profile access,
   Omnichannel supervisor role and voice entitlement. The kit neither licenses
   users nor substitutes its command role for native Contact Center permissions.
3. Finish every native call and wrap-up in the selected workstreams.
   English (1033) and Arabic (1025) must already be provisioned by the existing
   language/workspace recipe; this kit never changes environment languages.
4. Copy [the placeholder configuration](examples/supervision.example.json) into a
   private owner-only file outside the checkout. Use existing IDs only, and a
   new namespace UUID for ownership. Keep the original receipt directory for
   every retry, verification and rollback.
5. Review the native tab binding separately. The portable workspace kit verifies
   **exact** session/profile/app associations. Do **not** append to its owned
   session template without a coordinated migration and updated owner receipt.
   The default supervision configuration leaves `session_template_ids` empty:
   it stages an independently owned tab but does not silently change a workspace.
   `bind` requires explicit IDs of reviewed unmanaged session templates and
   `session_binding_reviewed=true`. No app/profile/form is cloned or modified here.
   Configured-selector proof can authorize staging; label it
   **configured-selector-only / rendered-host-unproven** until the post-provision
   APM context and acoustic checks confirm the actual host, including fallback/reuse.

The setup uses `native_voice_setup.Dataverse` and its private-file helpers.
Metadata and webresources use an owned unmanaged solution and publisher with the
`moacc` customization prefix; their IDs/names derive from the private namespace.
These are Dataverse customization containers, not identities or app registrations.
Native tab type is Web Resource (`509180002`); its `webresourceName` is a child
`msdyn_templateparameters` record. Binding adds only the owned
`msdyn_sessiontemplate_applicationtab` reference and preserves every existing tab.
Dynamics normally generates a blank `webresourceName` child while creating the
tab. The kit verifies that child's parent, generated unique name, creator and
creation interval, then conditionally updates it instead of creating a duplicate.
If the platform does not generate it, explicit creation includes the required
`msdyn_uniquename`. Readback accepts the native aggregate's `{value: ...}` shape;
the child record and exact receipt remain authoritative.

## Plan, apply, publication and verification

Commands below use the repository's existing Python environment and dependencies.
No command deploys Azure, calls a phone, or publishes a bot.

```bash
python infra/scripts/setup_native_supervision.py \
  --config /PRIVATE/supervision.json --output-dir /PRIVATE/supervision-receipt
```

Default `plan` performs authenticated reads only. It saves the exact before images,
controller definition, desired hash and source hashes privately. Receipt events
are immutable and hash-linked. Backups can contain the existing private controller
key: never commit, paste or publish them.

After review, apply individual stages or `all`:

```bash
python infra/scripts/setup_native_supervision.py all \
  --config /PRIVATE/supervision.json --output-dir /PRIVATE/supervision-receipt \
  --apply --confirm-organization https://YOUR-ORGANIZATION.crm.dynamics.com
```

Stages: `schema`, `access`, `controller`, `webresource`, `bind`, `publish`, `verify`,
`unbind`, `verify-unbound`, `reconcile-rejected-schema`,
`upgrade-runtime-metadata`, `upgrade-startup-notice`, `upgrade-startup-playback`,
`upgrade-native-startup`, `rollback-native-startup`,
`upgrade-application-startup`, `rollback-application-startup`, `rollback`. Except for
read-only `verify` and `verify-unbound`, omitting `--apply` only shows the plan.
`all` stages schema/access/controller/webresources and publishes only the owned
custom table and two webresources. It binds only when explicitly configured.
It never invokes PublishAll, republishes an app or changes routing.

The controller transformation preserves the existing speech-turn, notice and
CaseNumber actions. The generic native bundle exporter refuses to overwrite a
supervision controller: use its owned rollback/rebase receipt first. The isolated
placeholder snapshot is reproducible with:

```bash
python infra/scripts/sync_native_voice_topic.py --supervision --check
```

In Copilot Studio, check the exact staged controller in the selected bot, review
the quiet branch, and explicitly publish that bot after approval. Then:

```bash
python infra/scripts/setup_native_supervision.py verify \
  --config /PRIVATE/supervision.json --output-dir /PRIVATE/supervision-receipt
```

Verification requires the explicitly reviewed native tab binding, then checks
schema, owned rows, role/FLS references, native tab parameter
aggregation, exact controller hash and publication time. It emits a private,
secret-free `supervision.runtime.env` containing:

- `DYNAMICS_NATIVE_SUPERVISION_ENABLED=true`
- `DYNAMICS_NATIVE_SUPERVISION_ACTOR_IDS` — explicit human UUID allowlist
- `DYNAMICS_NATIVE_SUPERVISION_CONTROLLERS` — JSON `id`/`sha256` pins

Only the separate, reviewed deployment enables these settings. Runtime startup
rechecks the secured schema and published pins. A native start without
`supervision_contract: 1` is refused when supervision is enabled. Thus an old
controller cannot silently run with the new pause feature.

### Actual runtime preflight before activation

First apply the reviewed actor IDs and controller pins to the compatible voice
container **with `DYNAMICS_NATIVE_SUPERVISION_ENABLED=false`**. Keep the existing
image, identity, secrets and unrelated environment settings unchanged. Copy only
the reviewed nonsecret actor/pin values from `supervision.runtime.env` into the
variables below; never paste the controller's private API key:

```bash
# APPROVED_ACTOR_IDS: the verified comma-separated Dataverse user IDs.
# CONTROLLER_PINS_JSON: the exact verified compact JSON id/sha256 array.
az containerapp update --subscription "$AZURE_SUBSCRIPTION_ID" \
  --resource-group "$AZURE_RESOURCE_GROUP" --name "$VOICE_APP_NAME" \
  --set-env-vars \
    DYNAMICS_NATIVE_SUPERVISION_ENABLED=false \
    DYNAMICS_NATIVE_SUPERVISION_ACTOR_IDS="$APPROVED_ACTOR_IDS" \
    DYNAMICS_NATIVE_SUPERVISION_CONTROLLERS="$CONTROLLER_PINS_JSON"
```

Wait for the intended revision and actual feature-off HTTP health before running:

```bash
python infra/scripts/check_native_supervision_runtime.py \
  --subscription-id "$AZURE_SUBSCRIPTION_ID" \
  --resource-group "$AZURE_RESOURCE_GROUP" --app-name "$VOICE_APP_NAME" \
  --expected-runtime-user "$VOICE_APPLICATION_USER_ID" \
  --receipt-file "$PRIVATE_PARENT/supervision-runtime-preflight.json"
```

This Linux/WSL/dev-container utility runs the existing supervision verifier
inside the deployed container through Azure CLI. It checks `WhoAmI`, the
managed-identity application mapping, the secured schema and the actual
published controller pins. It does not start command polling, modify native
records, connect to HUMAIN or place a call. A failed/missing result exits
nonzero and keeps the private terminal receipt for diagnosis.

For an explicit secret-preservation check, optionally add
`--expected-env-hashes /PRIVATE/environment-secret-hashes.json`. The owner-only
JSON maps each reviewed environment variable name to its original lowercase
SHA256 value hash. The deployed program checks the actual environment values
without printing them; missing or changed values fail before readiness. Pass
hashes only, never raw credentials. This can distinguish a harmless API
null/empty `value` representation on an unchanged `secretRef` from an actual
changed secret; do not assume equivalence from the resource response alone.

Require `runtime_identity_verified=true`, `schema_and_controller_ready=true`
and `supervision_enabled=false` before changing the enable flag. The real
native metadata query requires all three metadata-read privileges listed
above; a successful administrator check or just the first permission does not
prove the runtime can expand the secured-column metadata.

Only then enable the feature and persist the three reviewed settings in the
deployment environment:

```bash
az containerapp update --subscription "$AZURE_SUBSCRIPTION_ID" \
  --resource-group "$AZURE_RESOURCE_GROUP" --name "$VOICE_APP_NAME" \
  --set-env-vars DYNAMICS_NATIVE_SUPERVISION_ENABLED=true

# After actual enabled HTTP health and native readiness have passed:
azd env set DYNAMICS_NATIVE_SUPERVISION_ACTOR_IDS "$APPROVED_ACTOR_IDS" \
  --environment "$AZD_ENV_NAME"
azd env set DYNAMICS_NATIVE_SUPERVISION_CONTROLLERS "$CONTROLLER_PINS_JSON" \
  --environment "$AZD_ENV_NAME"
azd env set DYNAMICS_NATIVE_SUPERVISION_ENABLED true --environment "$AZD_ENV_NAME"
```

Confirm actual HTTP 200 health with
`native_media_pilot.supervision_enabled=true` and `supervision_ready=true`, plus
authenticated native readiness. A revision marked Ready is insufficient.
If startup fails, restore the previous feature-off settings first and verify
healthy service before another attempt. Browser/acoustic acceptance remains
separate from these no-call checks.

Run setup stages **sequentially within one receipt directory**. They share an
exclusive, hash-linked journal even when their native targets differ. A refused
journal collision must be inspected and resumed from the latest intact chain;
never delete an event or run competing stage processes to force progress.

## Recovery and rollback

### When a Dataverse request fails

The stage stops and keeps the evidence. The raised error, the JSON line the
command prints and a journal event in the receipt directory all carry:

- the HTTP status and the Dataverse error code;
- the request IDs from the `x-ms-service-request-id` and `REQ_ID` response
  headers, which Microsoft support can trace;
- up to 2,000 characters of the response body.

Keys, tokens, signatures and email addresses are replaced before anything is
printed or saved.

| Response | Journal event | What a rerun does |
|---|---|---|
| 4xx except 408 and 429: a definite rejection | `rejected`, under the write's key | Repeats its ownership and ETag checks, records a new intent and sends that write again. It never adopts a record that appeared after the rejection. |
| 408, 429, 5xx, or a failed read | `request_failed`, under the write's key or the stage name | Reads back first and adopts only what the recorded intent created. |

**Partial schema:** recovery means rerunning the same stage with the same
configuration and receipt directory. In a test deployment, the second column's
`POST` returned a definite 404 after the table and first column were created.
Rerunning `schema` kept both, created the remaining six columns and verified the
schema. Don't delete the table, edit the journal or start a new namespace.

A rerun is the readback. If it fails on the same request, don't run it again
unchanged: retry once only with a concrete change, such as waiting for a
reported import to finish. If that fails too, stop and ask the owner. Share the
command, the JSON failure line and the journal event number.

### Release only the workspace edges

Use `unbind` before rolling back a composed workspace when command history must
remain. This operation removes **only** the receipt-owned
`msdyn_sessiontemplate_applicationtab` references. It preserves command records,
schema, roles, controller definitions, the tab and both webresources.

Disable the runtime, finish calls/wrap-up, then preview or apply:

```bash
python infra/scripts/setup_native_supervision.py unbind \
  --config /PRIVATE/supervision.json --output-dir /PRIVATE/supervision-receipt

python infra/scripts/setup_native_supervision.py unbind \
  --config /PRIVATE/supervision.json --output-dir /PRIVATE/supervision-receipt \
  --apply --confirm-organization https://YOUR-ORGANIZATION.crm.dynamics.com

python infra/scripts/setup_native_supervision.py verify-unbound \
  --config /PRIVATE/supervision.json --output-dir /PRIVATE/supervision-receipt
```

A fresh release requires each live membership set to equal its real captured
`plan.templates[id].tabs` baseline plus the owned tab. The original trusted binding
intent and successful readback are required; no portable-base receipt is fabricated
for an older private-recipe workspace.

The journal appends `release_intent`, then `released`, keyed by `session_<id>`.
Both include `path`, `target`, `primary`, and sorted complete UUID arrays in
`before`/`after`. Intent `after` is the planned baseline; released `after` is the
observed exact baseline. Only a recorded release intent permits resuming from an
already-restored baseline after a lost delete response. Unknown membership changes
or a subsequently revived edge are refused, not repaired.

Once release starts, `bind`, normal `verify`, and installation `all` cannot
reactivate that receipt. Previously emitted enable files are stale for activation.
`Setup.verify_unbound()` is a read-only composition interface returning
`{"status":"unbound","bindings":[...]}` with the same edge/before/after descriptors.
It requires all selected bindings to have completed release evidence and the live
exact baseline. It never emits runtime enable configuration or modifies receipts.
Workspace acceptance/release records can pin journal prefixes/checkpoints; appending
release events must not invalidate an earlier, correctly pinned prefix.

### Full owned rollback

The narrowly scoped `reconcile-rejected-schema` path is available only for the
legacy one-step schema request with its recorded **HTTP 400 / `0x80040216`**.
It requires the original paired private `*-intent.json`/`*-result.json` files,
matching request body/solution header/timestamp interval, and an explicit
operator approval reference. It pins the original journal entries and exact
request/response hashes. Current metadata must be absent; imports and calls
must be idle; supervision must be off; only the two exactly owned empty
publisher/solution records may exist. It also checks the captured protected
read sets and original user roles.

```bash
python infra/scripts/setup_native_supervision.py reconcile-rejected-schema \
  --config /PRIVATE/old-supervision.json --output-dir /PRIVATE/old-receipt \
  --request-receipt /PRIVATE/old-receipt/ORIGINAL-intent.json \
  --response-receipt /PRIVATE/old-receipt/ORIGINAL-result.json \
  --approval-reference APPROVED-RECOVERY-REFERENCE \
  --apply --confirm-organization https://YOUR-ORGANIZATION.crm.dynamics.com
```

This appends `rejected_unapplied_resolved`, **not** created/success/owned-table
evidence. It does not mutate native records or rewrite old receipts.
Unknown, timeout, throttled, 5xx, different rejection, changed payload, ownership
drift or foreign references cannot be cleared merely because metadata is absent.
The existing conditional `rollback` then revalidates the resolution and live
guards, deletes only the owned empty solution/publisher, and records verified
absence plus unchanged protected state. A new namespace/config/plan is created
only after that cleanup is verified; the old plan is never rebased.

Retry an interrupted stage with the **same configuration and receipt directory**.
Namespace-derived IDs, exact readback and ETags reconcile a lost response without
creating replacements or adopting unknown records. Never edit receipt events.
Resolve an unconfirmed stage before rollback. Concurrent edits cause refusal,
not overwrite.
An external solution import can reject metadata creation with HTTP 429 /
`0x80071151`. Keep the same plan and confirmed owned records, wait only within
a bounded observation window, and resume the unchanged stage after the import
and native-call idle gates clear. Never cancel another administrator's import
or change namespaces to bypass the lock; see the
[recorded customization-lock lesson](troubleshooting.md#custom-table-creation-returns-429--0x80071151).

For an already-applied older supervision plan whose verified runtime role lacks
any of those metadata-read privileges, do not edit the plan or replace the role. The explicit narrow
upgrade retains the original grants, appends an immutable permission delta and
removes only known defaults newly seeded by that grant:

```bash
python infra/scripts/setup_native_supervision.py upgrade-runtime-metadata \
  --config /PRIVATE/supervision.json --output-dir /PRIVATE/supervision-receipt \
  --apply --confirm-organization https://YOUR-ORGANIZATION.crm.dynamics.com
```

The feature must be disabled and calls/wrap-up idle. Unexpected grants, foreign
role members or an unverified legacy role stop the upgrade. Rerun verification
and test the actual runtime identity before enabling; a Container Apps revision
marked Ready does not prove that the voice application answered its health check.
Each conditional controller restore or owned-record deletion revalidates the
final record snapshot used for its ETag, so an intervening edit is preserved.

### Upgrading an existing startup notice

Older owned controllers can have the approved fast audio in
`latencyMessage.speak` but normal-speed prose in `latencyMessage.text`. Native
runtime evidence showed that the latter was synthesized as a separate preface.
The intermediate `upgrade-startup-notice` revision put audio markup in both
fields, but subsequent real calls emitted an empty latency activity and remained
silent until application playback. Its receipts remain historical evidence;
it is **not the current playback fix**.

The subsequent `upgrade-startup-playback` regular-message attempt also synthesized
the parallel text and delayed the application clip. It is historical rollback
evidence, not the current recommendation.

The `upgrade-native-startup` revision introduced parallel prepared startup,
initial language handoff and guarded unpaused-startup failure handling. Its
native-only WAV playback failed caller acceptance even with the editor-authored
speech-only definition. Preserve that revision as the recorded predecessor,
not as a proven announcement fix.

Use **`upgrade-application-startup`** with the compatible backend. It changes
only the prepared request's announcement owner, disables the competing native
latency announcement, and requires the response owner to be `application`.
It preserves the prepared-start endpoint, language handoff, buffered AI output
and failure/Pause AI paths. Do not overwrite the controller or edit old hashes.

1. Finish calls and wrap-up. Disable supervision and verify actual feature-off
   HTTP health. Preserve the current image, pins, actor allowlist and native
   before images privately.
2. Preview the narrow upgrade with the **original configuration and receipt
   directory**, then review the startup-only changes and preserved handover,
   CaseNumber and paused-call paths:

```bash
python infra/scripts/setup_native_supervision.py upgrade-application-startup \
  --config /PRIVATE/supervision.json --output-dir /PRIVATE/supervision-receipt
```

3. Apply only that reviewed upgrade:

```bash
python infra/scripts/setup_native_supervision.py upgrade-application-startup \
  --config /PRIVATE/supervision.json --output-dir /PRIVATE/supervision-receipt \
  --apply --confirm-organization https://YOUR-ORGANIZATION.crm.dynamics.com
```

4. Review the saved controller and explicitly publish the selected bot. Run
   `verify` with the same configuration/directory. The revision is an immutable
   journal event; the original plan and prior publication evidence remain intact.
5. Use the newly verified
   `supervision.runtime.application-startup-<event>.env`, not an older runtime
   file's old controller pin. Apply the new pin while the feature is **off**,
   run the actual deployed-identity preflight above, then enable and verify
   HTTP/native readiness. Persist the reviewed settings in the deployment
   environment.
6. Refresh the workspace's supervision extension receipt in a **new output
   file** against the latest verified checkpoint. Do not rewrite an old
   extension's journal hash. A separate approved workspace app-form addition
   needs an explicitly reviewed successor app baseline; this notice-only
   revision does not authorize unrelated app or tab changes.

The upgrade checks ownership, ETags, idle state and exact controller shape.
It preserves the CaseNumber, acknowledged Pause AI and human-handover paths. A current
fresh plan needs no notice migration. An interrupted attempt resumes with the
same owned IDs and immutable evidence; controller drift is a refusal, not an
invitation to force the update. The new stage refuses a backend that does not
advertise both `application_startup_preparation_supported` and
`startup_failure_notice_supported`. It appends to the owned prepared native
startup revision. For older receipts, first apply `upgrade-native-startup`
while the feature remains off, then this application revision; publish only
the final reviewed controller. Fresh application-only plans need neither
historical upgrade. Each immutable before image and runtime pin remains available.

For an already-applied application-startup revision, a **controller-only** restoration
is available while supervision is disabled and calls/wrap-up are idle:

```bash
python infra/scripts/setup_native_supervision.py rollback-application-startup \
  --config /PRIVATE/supervision.json --output-dir /PRIVATE/supervision-receipt \
  --apply --confirm-organization https://YOUR-ORGANIZATION.crm.dynamics.com
```

This restores only the revision's recorded prior controller. It does not delete
command history, tables, roles or workspace bindings. Explicitly republish,
verify the restored pin, run the real runtime preflight, and coordinate runtime
settings before re-enabling. This restores the recorded native-only predecessor,
whose WAV playback failed acceptance; restoration is not an audible-notice fix.
Restore the newest application revision before using `rollback-native-startup`
for its predecessor. Neither narrow restoration deletes command history,
schema, roles or workspace bindings.

Application-only source, early verified-caller playback, bounded runtime
buffering, input/tool fences, confirmed notice cancellation, ownership
revalidation, publication pins and narrow/full rollback have automated tests.
The source environment's preceding native-owner test on 1 November failed
notice audibility despite successful publication and managed-identity checks.
Do not carry that configuration-only success forward as application audio proof.
The subsequent application-only startup test was accepted for audible playback
and transition, while its approximately six-second initial delay remains a
separate limitation. It was not the Monitor/Join/Pause AI acceptance call.
Publication and actual caller sound remain separate acceptance gates.
The next authorized call must check time from answer to the 1.3x notice, absence
of a normal-speed preface, overlap during setup and clean first HUMAIN speech.

### Applying the reviewed rollback

Disable the runtime feature and verify health; finish calls/wrap-up. Preview:

```bash
python infra/scripts/setup_native_supervision.py rollback \
  --config /PRIVATE/supervision.json --output-dir /PRIVATE/supervision-receipt
```

Add `--apply --confirm-organization ...` only after review. Rollback restores the
saved controller data with If-Match, removes only owned associations/records, and
refuses drift, foreign members and a table containing command history. Archive
history only through a separately approved retention action; this tool never
deletes commands. Review and explicitly republish the restored bot if needed.
Existing user roles, unrelated tabs, workspace profiles, CRM and routing remain
unchanged. A rolled-back receipt cannot be reapplied as a fresh deployment.

## Live acceptance gates

The backend distinguishes a confirmed startup failure from a supervised quiet
hold. It sets `startup_failed_unpaused` only for a completed, detached stop
before any active AI phase, with no intervention or acknowledged pause and no
moved-owner, shutdown or explicit-stop reason. The controller requires that
positive field from the matching conversation's stop response before speaking
the existing connection-error message. Missing, stale or uncertain replies,
cleanup failures and actual supervisor intervention still take the quiet path.
An uncertain final ownership check suppresses the failure notice too.

Configuration readback and no-call checks don't prove acoustic or browser
behavior. Diagnostic command rows are retained for audit; they are not silently
deleted to make rollback possible.

No mocked test proves native audio quality or the deployed UI library context.
After a reviewed rollout, use consenting testers and **90-second maximum calls**:

- Confirm the tab gets the exact focused voice conversation; switching focus
  during ID resolution must create no command. Test English and Arabic.
- Monitor/Join alone must not issue a pause. Explicitly pause while HUMAIN speaks,
  with the caller microphone muted to exclude supervisor echo. Verify both input
  and output stop, no queued/stale speech returns, and human audio continues.
- Check Requested/Pausing/Paused against the actual worker acknowledgment;
  dropped/stale acknowledgments must remain unconfirmed/failed.
- Test unauthorized, disabled/app, stale, wrong-conversation and duplicate commands,
  including actual non-admin negative permissions.
- Test a started write, language transition, provider loss and the AI deadline:
  no blind retry, new tool, generic failure announcement, requeue or auto-resume.
- Confirm native conversation ID, human audio, recording/transcript, notice,
  response-language and CaseNumber paths remain intact. Parked metrics are not
  an acceptance substitute.
