# Repeatable Dynamics CRM setup

This guide covers the shared CRM data plane: custom columns and alternate
keys, an isolated application user/security role, and optional privately
generated account/contact/case history. The repository-owned entry point is
[`setup_dynamics_crm.py`](../../infra/scripts/setup_dynamics_crm.py). It imports
the existing `native_voice_setup.Dataverse` transport and private-file helpers.
It does **not** depend on an operator's former session scripts or seed files.

**Evidence boundary:** offline contract tests and a successful plan don't prove
a deployment. Verify each deployment as its deployed identity, as described in
[section 5](#5-runtime-configuration-and-actual-verification). This guide
doesn't establish production readiness.

## What this kit does—and does not do

| Mode | Network access | Changes |
|---|---|---|
| Default, no mode flag | None | Validates private inputs and writes a private plan |
| `--apply --confirm-organization URL` | Azure identity read and Dataverse | Creates missing owned CRM schema/identity and documented narrow role changes; generated records additionally require `--approve-seed` |
| `--verify` | Azure identity read and Dataverse | Read-only checks, including administrator-mediated runtime impersonation |
| `--rollback-plan-from RECEIPT` | None | Produces a reviewed rollback inventory, **not** a delete script |

The kit never creates an Azure managed identity, business unit, queue or
workstream; changes routing; links a live conversation; places calls; sends
emails; deploys/restarts apps; or touches Cosmos. The CRM user is **not** given
System Administrator, System Customizer, delete, resolve-case or role-management
privileges. Administrative schema/seed work and runtime identity are separate.

The voice/chat case pathway uses Dynamics; its former Cosmos case pathway is
disabled. Unrelated Cosmos resources and functionality remain intact.
Do not delete Cosmos infrastructure as part of this procedure.

Workspace languages, forms, views, knowledge publication, agent bindings and
supervision are separate concerns; see [workspace setup](workspace-setup.md).
**Case Details remains an end-to-end acceptance gate.** Bounded read-only
reconciliation now covers uncertain creates, notes and native links. A result
absent at its deadline remains unknown, not a proven failure. A verified
Customer/IssueId/case Regarding association or a `CaseNumber` context value does
not prove that the agent's panel renders the right record or that a prefetched
answer invoked case selection. See the [runtime contract](operations.md#native-case-identification-at-handover).

## 1. Prerequisites and identity distinction

1. A licensed Dynamics Customer Service/Omnichannel environment, its exact HTTPS
   organization origin, and the correct Entra tenant. The supported origin
   format is `https://<organization>.crm<region>.dynamics.com`, with no path or
   trailing slash; sovereign-cloud origins are not implemented by the shared
   transport. English metadata labels use LCID 1033. Arabic workspace language
   provisioning is a separate step.
2. An existing enabled Dataverse business unit. Only when `native_voice: true`,
   supply explicit active **inbound native voice** workstream IDs. The kit verifies
   their activity/direction/type without changing routing. Use the workstream
   created for this deployment in the runbook's
   [minimal shell step](README.md#51-create-the-minimal-shell-one-queue-and-one-voice-workstream),
   never a platform workstream chosen by name. CRM-only setup uses
   `native_voice: false`, an empty reader ID and an empty workstream list.
3. An approved **administrative operator** signed into Azure CLI. The operator
   needs schema customization/publishing, role/application-user administration,
   seed record creation/assignment and native `CloseIncident` permissions.
   Runtime probes use `CallerObjectId`; the operator also needs Dataverse
   impersonation (`prvActOnBehalfOfAnotherUser`). An administrator may do setup;
   those privileges must not be assigned to the runtime identity.
4. The **separate CRM user-assigned managed identity (UAMI)** already declared in
   `infra/dataplane.bicep` and exposed through `infra/resources.bicep` and the
   root deployment outputs. Prefer the nonsecret `MOACC_CRM_UAMI_*` outputs
   after provisioning; use existing-resource discovery only when those outputs
   are unavailable. Do not run `az identity create`, duplicate the Bicep
   resource, or repurpose the existing native reader identity.
5. When native linking is selected, the existing native reader's
   **client/application ID**, supplied separately so accidental reuse is rejected.
   A UAMI client ID, its principal/object
   ID, a Dataverse `systemuserid`, and a Dataverse `roleid` are four different
   identifiers. They are not interchangeable.
6. Python 3.10+ and the repository's existing setup dependencies, Azure CLI,
   Azure Developer CLI (`azd`) for deployment-output lookup, and access to the
   repository. No additional library or paid service is introduced.

From the repository root:

```bash
python3 -m venv .venv-crm-setup
.venv-crm-setup/bin/python -m pip install -r infra/scripts/requirements.txt
.venv-crm-setup/bin/python infra/scripts/setup_dynamics_crm.py --help

umask 077
mkdir -m 700 ../moacc-private-crm
cp docs/dynamics-native-voice/examples/crm-config.example.json ../moacc-private-crm/config.json
cp docs/dynamics-native-voice/examples/crm-seed.example.json ../moacc-private-crm/seed.json
chmod 600 ../moacc-private-crm/config.json ../moacc-private-crm/seed.json
```

Create a new private parent directory; do not reuse an unrelated operator's
directory. Generated files must be outside the checkout, owner-only, and never
committed. The helper creates each run directory with mode `0700` and files
with exclusive creation/mode `0600`; it will not overwrite a prior run. On
non-POSIX hosts, enforce equivalent owner-only ACLs.

Fill the private config with your deployment values. The committed examples are
deliberately invalid until their placeholders are replaced. After the normal,
approved infrastructure provisioning has completed, select the matching azd
environment and read its outputs:

```bash
azd env select "$AZURE_ENV_NAME"
CRM_IDENTITY_CLIENT_ID="$(azd env get-value MOACC_CRM_UAMI_CLIENT_ID)"
CRM_IDENTITY_PRINCIPAL_ID="$(azd env get-value MOACC_CRM_UAMI_PRINCIPAL_ID)"
CRM_IDENTITY_RESOURCE_ID="$(azd env get-value MOACC_CRM_UAMI_RESOURCE_ID)"
```

Copy those values into the existing private configuration keys:

| Nonsecret deployment output | Private `config.json` key |
|---|---|
| `MOACC_CRM_UAMI_CLIENT_ID` | `managed_identity.client_id` |
| `MOACC_CRM_UAMI_PRINCIPAL_ID` | `managed_identity.principal_id` |
| `MOACC_CRM_UAMI_RESOURCE_ID` | `managed_identity.resource_id` |

These are **outputs**, not azd input overrides. `tenant_id`,
`native_reader_client_id`, the organization URL, business unit and workstreams
remain separately supplied and validated. Do not substitute the principal ID
for an application/client ID or derive a Dataverse user ID from either.

**Fallback for older deployments without these outputs:** if an output is
unavailable or empty, do not invent it with `azd env set`. Inspect the
**existing** assist container app's `identity.userAssignedIdentities` and
`CRM_CLIENT_ID`, select its matching CRM identity resource ID, then read that
identity. This is targeted discovery, not provisioning:

```bash
# Supply these from your own deployment, not from someone else's receipts.
az login --tenant "$AZURE_TENANT_ID"
az account set --subscription "$AZURE_SUBSCRIPTION_ID"
az containerapp show --resource-group "$AZURE_RESOURCE_GROUP" --name "$ASSIST_APP_NAME" \
  --query '{identities:identity.userAssignedIdentities,environment:properties.template.containers[0].env[?name==`CRM_CLIENT_ID`]}' \
  -o json > ../moacc-private-crm/assist-identity.json
az identity show --ids "$CRM_IDENTITY_RESOURCE_ID" \
  --query '{resource_id:id,client_id:clientId,principal_id:principalId,tenant_id:tenantId}' \
  -o json > ../moacc-private-crm/existing-crm-identity.json
chmod 600 ../moacc-private-crm/assist-identity.json ../moacc-private-crm/existing-crm-identity.json
```

The CLI validates the supplied resource/client/principal/tenant tuple against
`az identity show`, then uses the shared Dataverse helper to obtain a short-lived
operator token for the exact organization and tenant. Tokens, authorization
headers and Azure CLI diagnostics are not printed or saved in receipts. It does
not create a client secret or impersonate the operator in the deployed service.

## 2. Prepare a private, explicitly synthetic seed

The [seed example](examples/crm-seed.example.json) is empty. Omit `--seed` for
schema/identity-only setup. No contacts or cases are created by default.
If the owner requests fictional records, generate and review them privately with
stable unique keys:

- `synthetic_only: true` and `synthetic: true` on **every** contact are mandatory.
  This is operator attestation, not an automated proof that input data is fake.
- Use a distinct, controlled **synthetic**
  profile identifier in normalized E.164 form. Never use an uninvolved person's
  number. No call is placed. Leave `email` null or use a nonpersonal test address;
  no email is sent.
- Preferred language is exactly `"ar"` or `"en"` and follows the approved scenario.
- Use natural-looking fictional names and descriptions, not visible demo labels.
  Supply a district and street/landmark only when `location_required` is true;
  otherwise empty location fields are valid. Do not invent external submission,
  dispatch, approval or completion-date claims.
- Cases support `active` or `resolved`; resolved cases require a synthetic
  `resolution` text. Priorities are Dynamics values 1, 2 or 3. Supported
  categories come from the approved configuration's `case_categories` and must
  match the runtime profile. The neutral default is `general`.
- Limits are 10 contacts, 30 cases, 10 notes per case, 10 workstreams, and 2 MiB
  per config/seed input. Prior receipts have a separate 32 MiB cap; at most 20
  original receipts can be supplied for one resume. This is a bounded
  demonstration kit, not a bulk migrator.

The approved knowledge input is
[`infra/data/knowledge.md`](../../infra/data/knowledge.md) in the private prepared
workspace. The source template contains no service facts. Knowledge is not
personal history or proof of jurisdiction.

The kit preserves the reviewed natural business values. Synthetic provenance
belongs in private seed hashes/receipts and technical eligibility fields; it is
not prepended to descriptions, notes or resolutions. Data approval does not
authorize real email, outbound calls or external business actions.

### Actor and idempotency contracts

The eligible contact gate is:

```text
moacc_actorid == "profile:" + lowercase_hex(SHA256(normalized_E164_UTF8))
AND moacc_demoenabled == true
AND statecode == 0
```

The voice adapter hashes the bound E.164 number. Chat hashes its selected
`profile_id`; to share this demonstration contact, that profile identifier must
be the same normalized value. An anonymous `session:` actor has no automatically
created contact. **Caller-ID/profile matching is not authentication.** The
runtime explicitly returns `identityVerified: false` and
`dataScope: synthetic_profiles_only`. Hashes are linkable private identifiers,
not secrets or an authorization credential.

Account/contact/role/user IDs derive deterministically from the organization,
`deployment_key`, resource type and stable seed key. Cases and notes use the
same operation-GUID algorithm as `case-service.ts`; request keys and payload
hashes use SHA-256. Case payload hashing preserves the service's JSON field order
(`category`, `description`, then `location` with `district`, `street`, `landmark`)
and UTF-8/compact JSON behavior. Notes use subject `Customer update`,
which is the runtime's customer-visible-note filter.

Do not change `deployment_key`, seed keys, phone identifiers or immutable seed
contents to “fix” an interrupted run. That changes identity or conflicts with
existing data. The kit skips exact matches, rejects differing deterministic
records, and refuses natural/alternate-key collisions belonging to other IDs.
Existing matches are **not** claimed as created by the current run.

## 3. Review the offline plan

```bash
.venv-crm-setup/bin/python infra/scripts/setup_dynamics_crm.py \
  --config ../moacc-private-crm/config.json \
  --seed ../moacc-private-crm/seed.json \
  --output-dir ../moacc-private-crm/plan-01
```

No Azure login or network is needed for this step. Privately inspect
`plan-01/plan.json`, `runtime.env` and `receipt.json`. The plan includes candidate
payloads, derived identifiers, columns/keys and the privilege contract.
It cannot tell whether records/roles already exist, whether licensing or
operator privileges suffice, or whether a plug-in will succeed.

### Schema contract

| Table | Custom column | New column type/length |
|---|---|---|
| Contact | `moacc_actorid` | String, 100 |
| Contact | `moacc_demoenabled` | Boolean, **default false** |
| Contact | `moacc_preferredlanguage` | String, 2; seed/runtime values `ar` or `en` |
| Incident | `moacc_requestkey` | String, 64 |
| Incident | `moacc_payloadhash` | String, 64 |
| Incident | `moacc_category` | String, 32 |
| Incident | `moacc_district` | String, 120 |
| Incident | `moacc_street` | String, 160 |
| Incident | `moacc_landmark` | String, 200 |

These are optional, non-field-secured custom columns. Compatible pre-existing
strings with greater capacity are preserved, not narrowed. Incompatible
types, too-short strings, non-Text formats, required-field settings, field
security or a true default gate cause failure.
Preferred language is a string contract, not a choice/option-set integer; the
kit validates `ar/en` in seed input rather than pretending Dataverse enforces
that enumeration on every external writer.

The two alternate keys are:

- Contact `moacc_contact_actorid` on `moacc_actorid`.
- Incident `moacc_incident_requestkey` on `moacc_requestkey`.

Only missing columns/keys are created. `PublishXml` targets **contact and
incident only**, never `PublishAllXml`. Coordinate with other makers: publishing
an entity can also publish their pending edits on that same entity. Run in an
approved change window with no concurrent schema/seed/role editors.
Both indexes must read back **Active** before seeding. A Failed index is not
“fixed” by deleting customer data, dropping an existing key or endlessly retrying.

### Runtime role: the verified minimum

The dedicated unmanaged role is named `Contact Center CRM <deployment_key>`.
Privilege IDs are resolved by name in the target environment; none are tenant
constants.

| Depth | Exact privileges granted by this kit |
|---|---|
| Local / business unit | `prvReadContact`, `prvAppendToContact`, `prvReadAccount` |
| Local / business unit | `prvCreateIncident`, `prvReadIncident`, `prvWriteIncident`, `prvAppendIncident`, `prvAppendToIncident` |
| Local / business unit | `prvCreateNote`, `prvReadNote`, `prvAppendNote` |
| Local / business unit | `prvReadActivity`, `prvWriteActivity`, `prvAppendActivity` |
| Local / business unit | `prvReadmsdyn_ocphonecallengagementctx`, `prvReadUser` |
| Global / organization | `prvReadmsdyn_liveworkstream`, `prvReadQueue`, `prvReadSubject`, **`prvReadPluginAssembly`** |
| Basic / user | `prvReadAsyncOperation` |

The full native-linked configuration requires these 21 privileges. CRM-only
uses 17: it omits `prvReadmsdyn_ocphonecallengagementctx`,
`prvReadmsdyn_liveworkstream`, `prvReadQueue` and `prvReadPluginAssembly`,
and does not query native workstreams or the native plug-in assembly.
Neither mode is a System Administrator workaround.

For native linking, `prvReadPluginAssembly` at Global depth is essential: source-environment ETag
link probing returned HTTP 400 while resolving
`Microsoft.Dynamics.OmnichannelPrime.Plugins` before this privilege was restored,
then HTTP 204 with readback afterward. Do not misdiagnose that as an ETag problem
or grant broad administration to bypass it.

The kit uses `AddPrivilegesRole`, never wholesale `ReplacePrivilegesRole`.
It removes **only** these four exact SharePoint privileges when recorded as
platform-added defaults on a kit-owned role:

```text
prvReadSharePointDocument
prvReadSharePointData
prvWriteSharePointData
prvCreateSharePointData
```

For a partial-run resume they must still be present in the last recorded
privilege snapshot. SharePoint privileges that reappear after a successful
cleanup are preserved as a new, unexplained change, not automatically stripped
using an old creating receipt.

Some environments also add Global read-only SDK dependencies
`prvReadSdkMessage`, `prvReadSdkMessageProcessingStep`,
`prvReadSdkMessageProcessingStepImage`, and `prvReadPluginType`. These are
preserved and explicitly reported, not silently deleted or counted as kit
grants. An unfamiliar extra privilege or a broader-than-required depth is
preserved **and causes verification to fail**. Existing unexpected user roles
are likewise preserved, not removed. The kit also inventories the user's team
memberships and refuses inherited team-assigned roles; it does not strip team
roles or claim least privilege from direct membership alone. There is no
automatic broadening/fallback role when checks fail.
It also refuses to modify a CRM role used by another user or any team, even
when a former receipt proves that the kit originally created that role.

Microsoft documents that
[`RetrieveUserPrivileges`](https://learn.microsoft.com/en-us/power-apps/developer/data-platform/webapi/reference/retrieveuserprivileges)
reports inherited team privileges at Basic depth, not necessarily their actual
effective depth. The kit therefore requires no team-assigned role rather than
treating that function's depth as a least-privilege certificate.

Local scope is **not** per-contact row isolation. Seed records are owned by the
isolated application user in the configured business unit; application code
still enforces contact eligibility, customer/case ownership, confirmation,
allowlisted workstreams, caller matching and live-link state/ETag checks.
`prvWriteActivity` is not a field-level allowlist. Do not grant these privileges
to a shared runtime identity or expose its token to clients.

## 4. Explicitly apply

After private review and approved operator sign-in:

```bash
# Read this exact organization URL from your private configuration.
export CRM_DATAVERSE_ORG_URL='<your configured HTTPS organization origin>'

.venv-crm-setup/bin/python infra/scripts/setup_dynamics_crm.py \
  --config ../moacc-private-crm/config.json \
  --seed ../moacc-private-crm/seed.json \
  --output-dir ../moacc-private-crm/apply-01 \
  --apply --approve-seed --confirm-organization "$CRM_DATAVERSE_ORG_URL"
```

For schema-only setup omit both `--seed` and `--approve-seed`. The empty example
is valid but creates no business records. An owner-approved generated dataset,
not the presence of a file, is the prerequisite for nonempty seed writes.

This is the first command in the procedure that changes Dataverse. It:

1. Checks the existing Azure identity, operator/runtime separation, business
   unit, workstreams, privilege definitions and identity/name collisions.
2. Creates missing compatible schema and verifies targeted publication/key
   activation.
3. Creates/verifies the receipt-owned role/application user and its single
   intended role association. Existing unfamiliar application users are not
   adopted; create a dedicated identity through the normal infrastructure
   process instead.
4. Verifies the impersonated execution identity, custom-column reads, the
   expected `prvReadRole` denial and, only for native linking, the native plug-in
   assembly. Normally `WhoAmI` must match the runtime user and business unit.
   If it instead identifies the exact administrator established in this run's
   preflight, the helper requires a bounded Dataverse 403/`0x80040220` denial
   whose unique principal-user, AAD-object and execution-caller fields all
   match the pinned runtime identity. This proof method and the differing
   diagnostic are journaled explicitly. A generic 403, foreign or ambiguous
   identity, ignored impersonation, or allowed role read is never accepted.
   This remains administrator-mediated evidence; verify the deployed UAMI
   separately as described below.
5. Seeds account/contacts as administrator, with ownership assigned to the CRM
   user. Creates cases/customer notes under `CallerObjectId`, verifies their
   `createdby` attribution, and reads back exact payload/lookup values.
6. Resolves only unchanged receipt-owned synthetic cases as administrator using
   `CloseIncident`, status 5. It does **not** grant runtime resolve rights or
   manufacture historical timestamps.

Conditional seed creates use `PATCH <table>(<deterministic-id>)` with
`If-None-Match: *`. An HTTP 412/conflict is recorded, not converted into an
update. No seed row is overwritten to make a run pass.

`CloseIncident` can generate its own resolution activity ID. The kit sends no
invented `activityid` in that action, reads the actual `incidentresolutions`
relationship, and saves the real ID and before/after case state. Missing,
multiple or mismatched resolution activities cause a bounded failure; the kit
does not fabricate a second activity or blindly resubmit closure. This action
does not offer the same row-ETag contract as a normal record PATCH, so keep the
synthetic seed isolated from concurrent editors during setup.

### Receipts and interrupted runs

Every mode writes `plan.json`, `runtime.env`, `owned-records.json` and a final
`receipt.json` in its new private run directory. Live modes also write numbered
observations and `*-before.json`/`*-after.json` mutation journals:

- The before file is persisted **before** the request, with the exact request
  payload, scoped noncredential headers and observed previous state.
- The after file records readback, failure status, and whether a write response
  was received. Full seed/identity snapshots remain private.
- Ownership requires a confirmed create and readback. A timeout is not silently
  promoted to “created by this run”; its observed/candidate outcome stays in the
  failure event for operator reconciliation.
- On a failure, exit status is nonzero and no automatic rollback occurs. Prior
  successful changes remain journaled. If a process is killed before final
  receipt writing, its immutable per-request files are the recovery evidence.
  Never invent an ownership receipt for a user-owned record.

Retain every run directory. For an interrupted run, inspect its exact
before/after events first, confirm actual current state, then use a **new**
output directory and the original apply receipt where ownership is required:

```bash
.venv-crm-setup/bin/python infra/scripts/setup_dynamics_crm.py \
  --config ../moacc-private-crm/config.json \
  --seed ../moacc-private-crm/seed.json \
  --output-dir ../moacc-private-crm/apply-02 \
  --previous-receipt ../moacc-private-crm/apply-01/receipt.json \
  --apply --approve-seed --confirm-organization "$CRM_DATAVERSE_ORG_URL"
```

A successful unchanged replay makes no seed/role writes and does not claim
ownership of the existing records. Preserve the **original creating receipt**,
not only that later no-op receipt. Repeat `--previous-receipt` when separate
partial runs created different required resources. Receipts are bound to the
exact configuration hash. If an interruption left unpublished schema, review the contact/incident
change window and add `--publish-schema` to an explicit apply. This republish
flag is rejected in plan/verify modes.

Metadata/key/closure polling defaults to 20 reads with 3 seconds between reads;
`--wait-attempts` is bounded to 1–30 and `--wait-seconds` to 0–10. An individual
Dataverse/Azure CLI request has the shared 45-second timeout. Total wall time
can exceed the sleep budget because requests themselves take time; failure
reconciliation may do a second bounded read loop. There are no unlimited
retries, automatic privilege escalations or blind write retries.

## 5. Runtime configuration and actual verification

The private generated `runtime.env` contains these **nonsecret** assist-service
settings:

```dotenv
CRM_DATAVERSE_ORG_URL=<organization HTTPS origin>
CRM_CLIENT_ID=<isolated CRM UAMI client/application UUID>
CRM_NATIVE_WORKSTREAM_IDS=<comma-separated allowlisted workstream UUIDs>
```

These are **container runtime names, not azd input names**. The current wiring is:

| `infra/main.parameters.json` azd input | Root Bicep parameter | Assist runtime result |
|---|---|---|
| `DYNAMICS_ORG_URL` | `dynamicsOrgUrl` | `CRM_DATAVERSE_ORG_URL` via dataplane `crmDataverseOrgUrl` |
| `DYNAMICS_NATIVE_MEDIA_WORKSTREAM_ID` | `dynamicsNativeMediaWorkstreamId` | First ID in `CRM_NATIVE_WORKSTREAM_IDS` |
| `DYNAMICS_NATIVE_MEDIA_ADDITIONAL_WORKSTREAM_IDS` | `dynamicsNativeMediaAdditionalWorkstreamIds` | Additional comma-separated IDs in `CRM_NATIVE_WORKSTREAM_IDS` via dataplane `crmNativeWorkstreamIds` |
| No caller-supplied azd CRM client ID | Existing dataplane `crmIdentity` resource | `CRM_CLIENT_ID` from `crmIdentity.properties.clientId` |

Setting an arbitrary `CRM_*` value with `azd env set` does not change those
templates. In particular, do not set `CRM_CLIENT_ID` as though it overrides
Bicep's resource reference. The root deployment exports
`MOACC_CRM_UAMI_CLIENT_ID`, `MOACC_CRM_UAMI_PRINCIPAL_ID` and
`MOACC_CRM_UAMI_RESOURCE_ID` for the existing identity; retrieve them with
`azd env get-value` and map them to the private config as shown in section 1.
They expose provisioned resource values, not additional provisioning switches.
Use section 1's targeted existing-resource discovery only for older deployments
without those outputs.
Keep the private setup config and actual deployed settings consistent.
Use normalized lowercase workstream GUIDs and comma separators without spaces;
the application compares native workstream IDs exactly. The generated
`runtime.env` is a comparison/reference file, not an azd parameter file.
Dataverse GUIDs need not satisfy RFC UUID version/variant bits. Runtime GUID
validation must accept actual server-created identifiers without changing the
exact workstream, caller/customer, record-ownership or ETag checks. Never create
a replacement workstream merely to obtain an RFC-shaped identifier.

The kit consumes this infrastructure; it does not deploy it or apply environment
variables. Attach only the intended identity through the normal deployment
process. Do not replace native media's separate Dataverse reader configuration
with the CRM identity.

Keep the existing authenticated case API wiring:

| Process | Existing settings |
|---|---|
| Assist API | `API_KEY` and the three `CRM_*` values above |
| Voice | `AGENT_ASSIST_BASE_URL`, `AGENT_ASSIST_API_KEY` (legacy aliases `CCAGENT_ASSIST_URL`, `CCAGENT_ASSIST_API_KEY`) |
| Chat orchestrator | `ASSIST_BASE_URL`, `ASSIST_INGEST_KEY` |

The client keys must match the assist API's existing credential. They are
separate from native-media control keys and managed-identity credentials. No
secret values belong in this guide, seed, command-line argument or committed
env file. Use the existing secret deployment mechanism.

Run the read-only CRM verifier after apply:

```bash
.venv-crm-setup/bin/python infra/scripts/setup_dynamics_crm.py \
  --config ../moacc-private-crm/config.json \
  --seed ../moacc-private-crm/seed.json \
  --output-dir ../moacc-private-crm/verify-01 \
  --verify
```

Inspect its private checks for schema/key readiness, exact required privilege
depths, retained platform reads, intended user role and absence of inherited
team roles, runtime impersonation,
plug-in access, `prvReadRole` denial, eligible contacts, record relationships,
and resolved-state/activity readbacks.

**This is administrator-mediated impersonation**, not acquisition of the
deployed UAMI's own token. Verify the actual deployed service separately after
the normal deployment process. With `ASSIST_BASE_URL` and `API_KEY` already
securely loaded into your operator environment, this example makes only a
readiness GET and avoids putting the key in process arguments:

```bash
.venv-crm-setup/bin/python - <<'PY'
import json
import os
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

origin = os.environ["ASSIST_BASE_URL"].rstrip("/")
parts = urlsplit(origin)
if parts.scheme != "https" or not parts.hostname or parts.path or parts.query or parts.fragment or parts.username or parts.password:
    raise SystemExit("Use the trusted deployed HTTPS assist origin, without a path or credentials.")

class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None

url = origin + "/api/cases"
request = Request(url, headers={"X-API-Key": os.environ["API_KEY"], "Accept": "application/json"})
with build_opener(NoRedirect).open(request, timeout=15) as response:
    result = json.load(response)
assert result["source"] == "dynamics" and result["crmReady"] is True
assert result["cosmosCasePathEnabled"] is False
assert result["dataScope"] == "synthetic_profiles_only"
print("Deployed managed-identity CRM readiness verified; no case write or call was made.")
PY
```

Do not use a redirecting/untrusted origin for a credentialed check. Readiness
does not perform case/note writes, authenticate a person, prove the UI panel
selection, validate supervision, or settle timeout outcomes. Later authorized
synthetic end-to-end testing must explicitly cover those still-open cases.

### Validation and evidence checklist

- **Offline repository tests:** run
  `.venv-crm-setup/bin/python -m unittest discover -s infra/tests -p 'test_dynamics_crm_setup.py' -v`.
  Tests use an in-memory Dataverse contract and mocked private output/auth;
  they do not contact Azure/Dynamics or write private seed data to disk.
- **Field evidence:** in test deployments, runtime case and note creation, case
  update, role-read denial, exact-operation case and note replay, a real chat
  prepare/confirm/write/receipt and the public three-field native association
  all worked under the CRM application user. The native plug-in read and ETag
  probe established the Global plug-in-assembly read requirement.
- **Check in each deployment:** the deployed managed identity's tokens, the
  role-consumer and team-role isolation checks, plug-in and solution differences
  in the target environment, customer-facing bilingual journeys and Case
  Details rendering after handover. Bounded timeout reconciliation has contract
  tests; a delayed commit can't be produced on demand, so check the receipts
  after any timeout. Do not present offline tests or earlier receipts as these
  acceptance results.

## 6. Failure handling and targeted rollback

| Failure | Safe next step |
|---|---|
| Invalid config, synthetic flag, phone or language | Correct private inputs; plan again. No cloud calls occurred. |
| Wrong tenant, identity tuple, foreign app user/role | Inspect deployment identity outputs. Do not adopt a shared user or grant it administration. |
| Metadata incompatible/secured or key Failed | Review exact metadata and duplicate values privately; preserve existing fields/data. |
| Unexpected/wider privileges or user roles | Preserve them, stop and obtain an explicit isolated-identity decision outside the kit. |
| Plug-in HTTP 400/403 or assembly missing | Verify Global `prvReadPluginAssembly` and installed native solution; do not mask failure with System Administrator. |
| Impersonation fails or role-read denial differs | Check the operator's impersonation rights and actual user mapping. A generic 403 is not successful verification. |
| Conditional-create conflict or seed drift | Read the exact deterministic record and receipt; never overwrite or generate a new identity merely to retry. |
| Close timeout / missing resolution | Read case state plus its actual resolution relationship; do not fabricate or duplicate an activity. |
| Metadata visibility delay | Generic and typed attribute 404s are retried only inside the existing bounded visibility wait; the create POST is never repeated. On timeout, reconcile the original write and resume with its receipt. |
| Timeout, 429 or other service error | Stop, inspect outcome and private journals. No automatic alternate write or privilege fallback is submitted. |

Start rollback with a private **offline inventory**:

```bash
.venv-crm-setup/bin/python infra/scripts/setup_dynamics_crm.py \
  --config ../moacc-private-crm/config.json \
  --seed ../moacc-private-crm/seed.json \
  --output-dir ../moacc-private-crm/rollback-review-01 \
  --rollback-plan-from ../moacc-private-crm/apply-01/receipt.json
```

This emits `rollback-plan.json` with only that receipt's confirmed created
records, exact snapshots/IDs, role-membership/privilege change events, and
unknown outcomes that require separate reconciliation. Generate one inventory
per relevant apply receipt and review in reverse run order. It executes nothing.

Rollback is an administrator's targeted operation, not a broad delete-by-name,
demo-marker search, whole-role replacement or database reset:

1. Stop using the synthetic data through your normal operational procedure;
   this kit does not pause routing. Read every candidate again and compare the
   saved snapshot and ETag. Preserve anything edited since setup.
2. Enumerate relationships before deletion: case notes/activities/resolutions
   and native-conversation links; contact cases; account contacts/cases; other
   custom relationships and solution dependencies. **Any unowned dependent or
   user-edited data blocks cascading cleanup.** A saved list of created IDs is
   not proof that deleting a parent is harmless now.
3. Review owned annotations first. For unchanged owned resolved cases, an
   administrator may reopen the exact case (`statecode: 0`, `statuscode: 1`)
   before reviewing its resolution activity and incident deletion. Never
   reopen or delete someone else's case. Use the **actual** saved resolution
   activity ID, not an originally proposed ID.
4. Delete only unchanged, unreferenced owned incidents, then contacts, then
   account. Keep private before/after readbacks for rollback itself.
5. Remove only the `systemuserroles_association` added by the receipt, and only
   after confirming no live consumer depends on it. Preserve all other roles.
   Disable rather than delete a kit-owned application user after its data and
   consumers have been reviewed. Delete a kit-owned role only if no other
   user/team uses it. For narrow additions to a previously owned role, reverse
   only that event's still-unchanged privilege delta; never replace the entire
   current privilege set.
6. Only after data/consumers are removed, use Dataverse solution dependency
   inspection to review removal of receipt-owned alternate keys, then columns.
   Never remove pre-existing compatible schema or use a dependency override to
   force deletion. Any required republish must still target contact/incident.
7. Leave the existing Azure UAMI, business unit, native reader role, workstreams,
   queues and unrelated Cosmos resources alone.

For a **single already-reviewed row**, the native admin commands have this
shape. Set `REVIEWED_ODATA_PATH` only to an exact path from the private inventory
and obtain `REVIEWED_ETAG` from its just-compared current read; these commands
are intentionally not run by the rollback-plan mode:

```bash
az rest --resource "$CRM_DATAVERSE_ORG_URL" --method get \
  --url "$CRM_DATAVERSE_ORG_URL/api/data/v9.2/$REVIEWED_ODATA_PATH" \
  > ../moacc-private-crm/rollback-row-before.json

# Only after relationship/snapshot review; HTTP 412 means stop, not force.
az rest --resource "$CRM_DATAVERSE_ORG_URL" --method delete \
  --url "$CRM_DATAVERSE_ORG_URL/api/data/v9.2/$REVIEWED_ODATA_PATH" \
  --headers "If-Match=$REVIEWED_ETAG" \
  > ../moacc-private-crm/rollback-row-response.json
```

Do not apply this DELETE example to app users, role associations or metadata:
those have their own supported APIs/dependency checks. Unknown write outcomes
and kills before receipt completion require manual reconciliation from the
numbered journals; this kit intentionally does not guess ownership to make
automatic cleanup appear convenient.
