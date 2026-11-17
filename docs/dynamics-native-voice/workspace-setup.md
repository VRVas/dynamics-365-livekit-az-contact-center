# Reproduce the bilingual native representative workspace

This is the repository-owned setup for the **native Dynamics representative
experience**, with separately selected knowledge and Copilot configuration,
not a replacement console. Run it after
[CRM setup](crm-setup.md) and the [main deployment prerequisites](README.md).
It does not deploy the voice application, publish a bot, place calls, send email,
change case/customer data, or implement routing, supervisor controls or HUMAIN
pause/resume.

The implementation is
[`setup_dynamics_workspace.py`](../../infra/scripts/setup_dynamics_workspace.py)
and the reviewed content is in
[`infra/data/dynamics-workspace`](../../infra/data/dynamics-workspace/).
The command defaults to **read-only planning**. A mutation requires a selected
stage, `--apply`, an exact `--confirm-organization`, and the corresponding approval
in the private configuration.

> **Evidence boundary:** the kit's tests and server readbacks don't establish
> native generated answers, summary quality or browser rendering. Check those in
> the representative's UI ([section 12](#12-native-ui-acceptance-checklist)).
> Supervisor Join doesn't pause the AI, and this kit doesn't add an automatic
> pause.

## 1. What the kit owns

| Owned result | Boundaries |
|---|---|
| One dedicated, nondefault experience profile | Direct assignment to the approved Dataverse user IDs only; no default-profile or role-mapping edits |
| Two session templates | Native Active Conversation voice anchor; dedicated case entity session |
| Six application tabs | Voice customer, linked case, case customer, current-customer case lookup, two unsaved new-case variants |
| Selected productivity tools | Agent scripts always; owned Knowledge/Copilot pane tabs only when selected |
| Six script records and 56 steps | Voice/case/wrap-up per UI LCID: English 1033 and Arabic 1025; 15/9/4 steps per language |
| Nine activated shared macros | Human initiated; no automatic save, case resolution, email or call action |
| Two cloned main forms | Contact: current-customer **All Cases**; Case: existing service fields and native controls; both nondefault/nonfallback |
| Exactly two app component additions | The two cloned forms only, followed by app-only `PublishXml` |
| Optional knowledge categories | One root plus groups derived from the approved private topics |
| Optional published articles | One English/Arabic **native translation pair** and platform-generated root per group |
| Minimal shared configuration differences | Knowledge-source flag and aligned legacy/modern summary switches, only with explicit approval |
| Selected workstream session bindings | Only the supplied workstreams, only when they still use the managed voice session |
| Optional first-representative setup | Only missing roles, one User bookable resource, one inbound capacity association and fallback-queue membership for one approved user; see [Single representative setup](#single-representative-setup) |
| Optional personal Visibility filter | Only an approved representative's own Internal filter, when internal articles are hidden; see [Internal knowledge visibility](#internal-knowledge-visibility) |

`enable_knowledge` and `enable_copilot` default to false. Copy the selected values
from the private preparation's `native-options.json`. Basic workspace binding
does not require a published knowledge package or licensed Copilot setup.
Unselected stages record that they were skipped, without publishing placeholders,
enabling profile Copilot features or changing organization-wide settings.

When selected, knowledge is rendered from privately supplied
[`knowledge.md`](../../infra/data/knowledge.md) and its reviewed Arabic rendering.
The template's files are unconfigured. Topic IDs, citations, revision and canonical
**SHA-256** must agree; every source must be used. Groups contain up to six topics,
with no fixed industry taxonomy or topic count. The current bounded recipe
supports at most 99 topics. Source changes require a newly reviewed translation,
not an automatic assertion that service facts remain current. End the last topic
with a `## Source registry` heading; see [Knowledge input format](#knowledge-input-format).

**Important scope distinction:** enabling the native Knowledge article source
does **not** configure Copilot to query live cases or execute CRM business actions.
Keep live-case Q&A/integration, native record permissions and knowledge grounding
as separate capabilities with separate acceptance evidence.

## 2. Prerequisites and authorization

Complete these before creating the final plan:

1. Select the correct **Dataverse organization origin, Entra tenant, existing
   Customer Service workspace app, approved native voice workstreams and
   representative Dataverse `systemuserid` values**. An Entra object ID is not a
   Dataverse user ID. This bounded recipe expects app unique name
   `msdyn_CustomerServiceWorkspace`.
2. Verify the environment has the Microsoft Customer Service/Contact Center,
   multisession productivity tools, native knowledge and Copilot solutions
   installed. Missing or ambiguous managed defaults are a hard stop, not an
   instruction to fabricate records with guessed IDs.
3. Use an authorized, licensed interactive administration identity in Azure CLI.
   The identity needs the applicable System Administrator/System Customizer,
   productivity-profile, workflow activation, language-provisioning,
   knowledge approval/publication, customization publication and app-management
   privileges. Grant permissions through your normal administration process,
   not by giving the runtime application broad administration roles.
4. Verify each representative is enabled, has read/write access mode, the
   appropriate Customer Service/Contact Center and voice entitlements, app access
   and native record privileges. A working profile does not grant case, contact,
   timeline or task access. See the [CRM guide](crm-setup.md) for the application
   identity, case/customer permissions and synthetic acceptance fixtures. For the
   first representative, the optional `representative` stage adds the missing
   native roles, bookable resource, capacity association and queue membership; see
   [Single representative setup](#single-representative-setup).
5. **Only for `enable_copilot: true`:** in the supported native admin experience,
   finish the organization's licensed
   **Copilot / Ask a question** setup and applicable summary access; see
   [Native Copilot opt-in](#native-copilot-opt-in). Review actual
   service-plan provisioning, region availability, required data movement and
   consent with the tenant administrator. This kit neither buys licenses nor
   enables cross-region consent, Bing, external sources or translation.
6. **Only for `enable_copilot: true`:** the recipe requires existing native Copilot and legacy summary access to
   already include the selected representatives/workstreams. If native access is
   selectively scoped rather than enabled for all, the stage stops for explicit
   administrator review; it does not widen agent/workstream lists.
7. Drain active/open/waiting/wrap-up conversations on the selected workstreams.
   Choose a customization maintenance window with no other app editors.
   `AddAppComponents` and `PublishXml` are unbound native actions, not ETag-atomic
   transactions. The kit checks published/draft authored content, exact component
   deltas and defaults before/after; a maintenance window is still required.
8. The selected workstreams must still use `msdyn_voicecall_session`. Existing
   custom session choices are deliberately not replaced. A workstream template
   affects **all representatives using that workstream**, even though the
   dedicated profile itself is assigned only to the approved users.
9. CRM setup must already expose the string columns `moacc_district`,
   `moacc_street`, `moacc_landmark` and `moacc_category`. The kit validates their
   native read/create/update metadata, and the existing contact/account → case
   `customerid` mappings used by `createFromEntity` / `InitializeFrom`. It neither
   creates substitute address notes nor changes these mappings or stored values.

Do not use the already-deployed environment as an implicit adoption manifest.
New deterministic IDs, a prefix collision or matching display names do not prove
ownership. Existing deployments need their original portable receipts; importing
old environment snapshots or silently taking over another profile/article is not
implemented.

## 3. Prepare the private configuration and tools

Use the repository's existing Python environment (Python 3.10+ and the existing
`python-dotenv`/`PyYAML` dependencies), Azure CLI and Node for client-contract tests.
No npm package installation is necessary.

```bash
PYTHON="${PYTHON:-python3}"
"$PYTHON" -c 'import dotenv, yaml'
az version
node --version
```

Only if the Python import check fails, restore those already-declared
dependencies in the selected Python environment:

```bash
"$PYTHON" -m pip install 'python-dotenv==1.0.1' 'PyYAML>=6,<7'
```

Create a private output parent **outside the checkout**. The output parent must
exist; the kit creates its deployment directory with mode 700 and new receipts
with mode 600. Receipts contain environment identifiers and before-images; never
commit them, attach them to a public issue or put them in the example directory.

```bash
PRIVATE_PARENT="$HOME/.local/share/dynamics-native-workspace"
mkdir -p "$PRIVATE_PARENT"
chmod 700 "$PRIVATE_PARENT"
cp docs/dynamics-native-voice/examples/workspace.example.json \
  "$PRIVATE_PARENT/workspace.json"
chmod 600 "$PRIVATE_PARENT/workspace.json"
CONFIG="$PRIVATE_PARENT/workspace.json"
OUTPUT="$PRIVATE_PARENT/workspace-v1"
```

Edit the copied JSON, not the repository example:

- Replace every angle-bracket placeholder with a caller-selected value.
- Set a stable nonpersonal `namespace` slug and unique 3–24 character publisher
  `prefix`. Do not use `msdyn`/`d365` names.
- Owned record IDs derive from `uuid5(NAMESPACE_URL, portable namespace/prefix)`
  plus the organization and semantic record key. Reusing the same receipt and
  configuration reuses those IDs. A new namespace is a new deployment, not a way
  to repair ownership.
- List only approved users and workstreams (1–10 distinct IDs per list).
- Review the scope above before setting individual approvals to `true`.
  `licenses_and_region_reviewed` records an operator acknowledgment; it is not
  an automated license, consent or region-capability attestation.
- `native_copilot_status_check` permits one call to the installed native
  `msdyn_CheckDVCopilotStatus` action if the source is not already Ready.
  Leave it false if your administrator will drive provisioning from the native UI.
- Optional, added later: `setup_representative` (with a `representative_setup`
  section, see [Single representative setup](#single-representative-setup)) and
  `representative_knowledge_visibility` (see
  [Internal knowledge visibility](#internal-knowledge-visibility)). Leave them
  false if you don't use those steps, and delete `representative_setup` when the
  representative is already set up.

Finish these choices **before planning**. Configuration and source hashes are
bound into the immutable plan; changing approvals later invalidates it.

```bash
TENANT_ID="$("$PYTHON" -c 'import json,sys; print(json.load(open(sys.argv[1]))["tenant_id"])' "$CONFIG")"
ORG_URL="$("$PYTHON" -c 'import json,sys; print(json.load(open(sys.argv[1]))["org_url"])' "$CONFIG")"
az login --tenant "$TENANT_ID" --allow-no-subscriptions
"$PYTHON" infra/scripts/setup_dynamics_workspace.py --help
```

Do not print or save access tokens in receipts. The shared `Dataverse` client
obtains a tenant/resource-specific token using Azure CLI and checks its tenant;
the kit does not store Authorization headers.

## 4. Create and review the plan

```bash
"$PYTHON" infra/scripts/setup_dynamics_workspace.py \
  --config "$CONFIG" --output-dir "$OUTPUT"
```

This performs authenticated **reads**, discovers native records by stable
names/schema, checks for ownership collisions and writes `plan.json` privately.
It is not a live provisioning command and does not publish anything.

Review the private JSON for:

- exact organization/app/workstream/user selection;
- native managed `msdyn_csw_app_configuration` and its installed extensions,
  channel providers and tool presentation;
- managed `msdyn_voicecall_session` and its actual anchor lookup;
- `Enhanced full case form`, `Contact for Multisession experience`, `All Cases`
  and the `incident_customer_contacts` relationship;
- before-images of published/draft app configuration, component defaults,
  contact form order/defaults, profile memberships, UI settings, shared flags
  and unrelated knowledge article inventory;
- six localized scripts, 56 ordered steps, nine macro definitions, the two
  conditional contact form pins and the twenty-four article payloads;
- if selected, the `representative` section: the user's roles, bookable
  resources, capacity associations, fallback queue and route as read, the exact
  writes and any gates;
- the `knowledge_visibility` filter payloads, created only when needed;
- language and external prerequisite gates.

Opening a plan again reuses it; it does **not** silently refresh the before-images.
If external prerequisites require changes, resolve them before owned-resource
creation, then use a **new output directory** for a fresh plan. Never discard an
output directory after it has recorded a successful/uncertain mutation.

No mutation is authorized merely by choosing a stage:

```bash
"$PYTHON" infra/scripts/setup_dynamics_workspace.py workspace \
  --config "$CONFIG" --output-dir "$OUTPUT"
```

That command records a stage dry-run. It does not create workspace rows.

## 5. Provision Arabic availability before LCID 1025 scripts

Arabic article locale records are not proof that the organization's Arabic
**UI language** is provisioned. The gate is the actual result of
`RetrieveProvisionedLanguages()`.

```bash
"$PYTHON" infra/scripts/setup_dynamics_workspace.py language \
  --config "$CONFIG" --output-dir "$OUTPUT" \
  --apply --confirm-organization "$ORG_URL" \
  --wait-seconds 900 --poll-seconds 15
```

If 1025 is absent and approved, the kit invokes the documented
**`ProvisionLanguageAsync`** action with `{"Language": 1025}`. It records
`AsyncOperationId`, polls that exact System Job and requires both:

- job state `Completed` / status `Succeeded` (state 3, status 30);
- 1025 present in `RetrieveProvisionedLanguages()`.

The wait is bounded (default 900 seconds, maximum 3600). A timeout stops this
stage; rerunning the same stage waits on the **recorded job**, not a second job.
A failed/cancelled System Job is a hard stop for administrator investigation.
Do not substitute synchronous `ProvisionLanguage`, suppress failed jobs or
disable **Activity Feeds** to get past a language error.

The kit compares each approved user's UI/help/locale settings before/after.
It **does not change an English representative to Arabic**. Provisioning only
makes Arabic available for an explicitly chosen user UI later.

If language provisioning changed native form/app metadata, the stale snapshot
guard will stop workspace creation. Before any owned records exist, make a fresh
plan in a new private output directory after the language job finishes. Retain the
language-only receipt for audit.

## Single representative setup

Use this once for the first approved representative. The
[multi-operator kit](routing-setup.md) needs at least two people and existing
resources; this stage covers one person. It adds only what is missing:

- the **Omnichannel agent**, **App Profile User** and **Customer Service
  Representative** roles from the user's own business unit;
- one active User **bookable resource**, if the user has none;
- the resource's association with the inbound **capacity profile**, inheriting the
  profile's default limit;
- membership of the workstream's **fallback queue**.

It doesn't create users, assign licenses, edit roles, profiles, queues or capacity
limits, or set presence. Roles the user already has, directly or through a team,
stay as they are.

Add this section to the private configuration and set approval
`setup_representative` before planning:

```json
"representative_setup": {
  "user_id": "<APPROVED_DATAVERSE_SYSTEMUSER_ID>",
  "entra_object_id": "<APPROVED_ENTRA_OBJECT_ID>",
  "queue_id": "<FALLBACK_VOICE_QUEUE_ID>",
  "capacity_profile_id": "<INBOUND_CAPACITY_PROFILE_ID>"
}
```

`user_id` must be one of `representative_ids`. The kit checks that
`entra_object_id` matches the user's `azureactivedirectoryobjectid`. The queue
must be the default queue of a selected inbound voice workstream that has exactly
one active, published binding to this capacity profile.

Planning reads the user, direct and team roles, bookable resources, capacity
associations, the queue and workstream route, and the relationship metadata for
queue membership, capacity associations and the resource's user lookup. It
records the exact writes in `plan.json`. It records a gate instead, and the stage
writes nothing, when:

- the user isn't enabled, licensed and interactive, or has the Omnichannel API
  access role;
- a required role isn't exactly one role in the user's business unit;
- the user has bookable resources, but not exactly one active User resource;
- the queue, capacity profile or route doesn't match;
- an existing association with the capacity profile is inactive or duplicated.

Fix the cause and plan again in a new output directory.

```bash
"$PYTHON" infra/scripts/setup_dynamics_workspace.py representative \
  --config "$CONFIG" --output-dir "$OUTPUT" \
  --apply --confirm-organization "$ORG_URL"
```

| Change | Public operation |
|---|---|
| Add a role | `POST systemusers(<user-id>)/systemuserroles_association/$ref` |
| Create the resource | `POST bookableresources` with `name` (the user's full name), `resourcetype` 3 (User), `timezone` (the user's time-zone code) and `UserId@odata.bind` |
| Associate the capacity profile | `POST msdyn_bookableresourcecapacityprofiles`, the same body as the routing kit, with `msdyn_maxunits` null |
| Join the fallback queue | `POST queues(<queue-id>)/queuemembership_association/$ref` |

The selected workstreams must be idle. Before each write the kit reads the state
again and stops if anything changed since the plan or its last recorded write.
It saves the intent before each write and reads back after it; a rerun skips
recorded writes. The command output lists the roles and confirms the resource,
capacity association and queue membership; the receipt has their IDs. `verify`
repeats this readback, and `bind` waits for this stage when it's selected.
`all` runs it after `language`. Rollback keeps these changes; remove them only
through a separately reviewed admin change.

This doesn't prove the representative can go Available or take a call. Capacity
changes can take up to 15 minutes to apply; then the representative reloads the
workspace and sets presence.

Learn: [associate records](https://learn.microsoft.com/en-us/power-apps/developer/data-platform/webapi/associate-disassociate-entities-using-web-api),
[bookable resource table](https://learn.microsoft.com/en-us/dynamics365/developer/reference/entities/bookableresource),
[capacity association table](https://learn.microsoft.com/en-us/dynamics365/developer/reference/entities/msdyn_bookableresourcecapacityprofile),
[users and bookable resources](https://learn.microsoft.com/en-us/dynamics365/customer-service/administer/users-user-profiles).

## 6. Create the native workspace and scoped forms

```bash
"$PYTHON" infra/scripts/setup_dynamics_workspace.py workspace \
  --config "$CONFIG" --output-dir "$OUTPUT" \
  --apply --confirm-organization "$ORG_URL"
```

This creates unassigned owned configuration, activates the nine macros, creates
the native relationships and writes/polls template-parameter readbacks. It does
not yet move representatives or bind workstreams.

### Why the Related Cases change is precise

The stock multisession contact form's **My Active Cases** view filters by owner.
App-user-owned cases can disappear even when their `customerid` points to the
correct contact. The fix is **not** to reassign cases, populate Primary Contact,
edit the shared view or make a new global default form.

The kit:

1. Clones the discovered managed contact main form.
2. Changes only `RelatedCases` `ViewId`/`ViewIds` to the discovered native
   **All Cases** view, retaining `incident_customer_contacts`.
3. Keeps the clone nondefault and nonfallback; appends its display order after
   the existing contact main forms. Existing form order/default flags and role
   conditions stay intact.
4. Also clones the managed **Enhanced full case form**, retaining its native
   timeline/Copilot controls and adding **Details → Service request details**:
   District, Street, Landmark and Category bound to the four existing CRM fields.
   The clone is nondefault/nonfallback and follows the existing explicit form
   orders. Native forms without optional `DisplayConditions` or an explicit
   `Order` are preserved as-is, not rewritten to zero; at least one real
   numeric order is required before either clone can append. Malformed explicit
   orders remain a hard stop. No shared form or case value is changed.
5. Calls `AddAppComponents` with exactly the **two owned forms** and inspects
   the unpublished `appmoduleidunique`'s **`appmodulecomponents`** rows.
   `RetrieveAppComponents` still shows the published catalog before publication,
   and an unpublished descriptor can have an empty generated component list.
6. Verifies the exact two-form delta, unchanged component defaults and no new
   native app validation errors, then publishes **only**
   `<appmodules><appmodule>…</appmodule></appmodules>` through `PublishXml`.
   It never uses `PublishAllXml` or contact/case entity-wide publication. New
   forms must already be published by native creation; unexpected publication
   state stops the stage rather than broadening its scope.
7. Pins the two customer tabs **and Find existing cases** using native OData `formId` slugs that
   require the current customer entity type to match the contact form.
8. Adds a **contact-only** `FormId` branch to the dedicated review-customer macro.
   Account customers keep their normal form selection; missing customers open
   Contacts for deliberate identification without manufacturing an association.

Resolved cases belong in All Cases too. The relationship, not an owner filter,
restricts the grid to the current contact; it still excludes other customers.
**Find existing cases** opens this current-customer form/grid, not an unfiltered
Cases list. Native Entity List templates support `entityName`/`viewId`, not a
dynamic customer filter; merely choosing a global All Cases view would still
show other customers. Account customers retain their native form; review their
related-case view explicitly rather than claiming the contact-grid acceptance
applies to accounts.

The linked-case tab and both new-case forms explicitly select the custom
case clone. This does not replace the shared Case default: a separately opened
case can still use its existing native form. Select the nondefault custom
case form deliberately if needed.

The source-environment navigation correction was deployed separately from the
remaining script-label changes. Its three navigation macros, two new-case
templates, scoped case pins and one case-form app registration passed native
readback. Installed native client contracts passed with network/mutations
blocked; actual browser execution is still an acceptance step.
The subsequent text-only pass updated and read back 20 English/Arabic script
steps, including the live-call-only link title. Unselected steps and macro
definitions were verified unchanged; no forms or call logic changed in that pass.

Native template synchronization can advance sibling record versions while
leaving their settings unchanged. For reviewed owned records, compare all
settings, identities and ownership against the recorded before image, then use
the freshly read ETag in the conditional PATCH. Do not ignore changed values or
disable `If-Match`. Recheck every child value and the parent parameter aggregate
after the sequence. A partial rollout needs its own explicitly scoped successor
workspace/Pause AI receipt; it must not claim that pending script steps were
applied.

The approved section heading is shared bilingual text:
**Service request details / تفاصيل طلب الخدمة**. The four fields retain
separate English/Arabic labels. This follows the workspace's bilingual captions
without depending on an unverified section-translation identifier.

Native English `systemform.formxml` reads can omit the four Arabic field labels
even when they are stored. Verification checks the exact published layout and
the native full-label `RetrieveUnpublished` view of the **same published form
version/ETag**. The shared heading and all eight field labels must match.
Native inherited captions can be reconstructed differently from the authored
XML; their control layout must be identical and their localized labels must
match the source form's corresponding native view. Missing field
translations or a distinct unpublished revision remain hard stops.

The source-environment attempt to repair a separate section translation created
an unpublished form layer and did not verify the Arabic heading. Adding a
`labelid` also failed an isolated fresh-create probe, which was removed. Do not
repeat that approach or publish all Case customizations to bypass the check.
The user instead approved replacing only the new, unused owned form with this
bilingual-heading version. Such a replacement requires exact creation/edit
receipts, fresh ETags, no app registration or external references, and retained
before/deletion/replacement evidence. Never recreate an unconfirmed or foreign
record, rewrite an old plan, or change the user's UI language.

Native publication regenerates descriptor status/version, solution/publisher
layer identifiers and unordered entity enumeration. The comparison normalizes
those generated differences, not navigation, actual defaults or authored
configuration. Localized `RetrieveUnpublished` form labels can be enriched by the
platform without a new pending customization; do not broaden publication just
because raw XML strings from different retrieval modes differ.

The clone is not made a new default, but authorized users may still see a
nondefault form in a manual form picker. The kit does not make role changes to hide
it. Existing open tabs must be closed/reopened after a workspace reload.

### Script and macro semantics

- The native script selector filters on the representative's **UI LCID**. Arabic
  text in an English script alone is not Arabic UI provisioning.
- Voice/case sessions each have an English and an Arabic initial script. The
  wrap-up route points to the same-language wrap-up script.
- Macros are shared between the two languages; there are nine definitions, not
  eighteen.
- New case uses a registered native Entity Record application template, not
  lower-level `Create_New` navigation or a `Custom_Array`. The customer variant
  supplies `createFromEntity` with the current saved customer's ID/type; native
  mappings initialize Customer without saving. The missing-customer variant is
  blank. Neither supplies a record `entityId`; both explicitly set `data={}`,
  rather than inheriting the voice anchor's `ocContext`. Free-text customer names
  are not interpolated into JSON. Task preparation still uses native `Create_New`.
- A live `InitializeFrom` read can verify the customer's case defaults without
  creating a case. Client-schema tests do not establish browser navigation:
  open the unsaved form and verify Customer before accepting the change.
- The link macro is the one **explicit association change**: only a saved,
  focused case in a live conversation is eligible. Opening a case tab is not
  proof of linkage. Do not switch sessions during execution. Use it **before
  hanging up**, not during wrap-up or on historical/closed conversations. A failed
  guard can skip the link action; inspect native macro run history rather than
  assuming a red X means a permission failure or retrying a historical write.
- The voice linked-case template and **Open linked case** read
  `msdyn_ocliveworkitem._msdyn_issueid_value` using the current `LiveWorkItemId`.
  A simple `{caseId}` slug is a session-context value, not a fresh database read;
  passing a `CaseNumber` during handover does not itself populate that slug.
  The read-through route honors the actually saved association, including a
  deliberate manual selection; it never chooses a customer's “latest” case,
  writes a link, or overwrites history. No associated case means current-customer
  lookup, not a manufactured association. Outside conversation context the
  existing cached-case navigation remains available.
- Native link persistence, session hydration and card visibility are different
  contracts. The inspected native conversation client populates `caseId` from
  its returned incident **ContextItems** and updates session context after native
  linking; a handover `CaseNumber` is not that context item. The managed Active
  Conversation case card binds `msdyn_issueid`, but its enhanced-form visibility
  initializer checks that `regardingobjectid` is present and differs from
  Customer. A direct IssueId patch alone therefore does not prove complete native
  UI hydration. The read-through tab does not rewrite either association or
  override managed card logic. Any future backend native-link integration must
  separately verify the supported context-link API and live-session guards;
  never “repair” a completed conversation or set both fields merely for appearance.
- The exact script action is **Refresh saved case/tab**, not “Refresh focused
  record”. It refreshes the focused page, not necessarily cached voice-session
  associations. **Refresh case-session context** is for a case entity session
  only, never the voice anchor.
- Keep `Custom_Array` **after scalar inputs** in macro JSON. The installed native
  client contract exposed an input-order dependency; alphabetical serialization
  left customer/task slugs unresolved. The renderer preserves this ordering and
  tests it. Do not “normalize” clientdata by sorting all object keys.
- Child `msdyn_templateparameters` are the source of the values. The native
  platform synchronizes the parent aggregate; the kit verifies that synchronization
  with a bounded wait, rather than silently forcing a conflicting aggregate.

## 7. Publish the scoped bilingual Knowledge package

Run this stage only with `enable_knowledge: true` and approved private content.
The default workspace-only recipe does not publish articles.

```bash
"$PYTHON" infra/scripts/setup_dynamics_workspace.py knowledge \
  --config "$CONFIG" --output-dir "$OUTPUT" \
  --apply --confirm-organization "$ORG_URL" \
  --wait-seconds 900 --poll-seconds 15
```

For each group, the kit creates the owned English primary article, invokes
`CreateKnowledgeArticleTranslation` for the actual Arabic language-locale record,
records the returned Arabic ID, and verifies the shared native root and English
parent relationship. It does not fake translations by making independent
same-title articles or assigning guessed locale IDs.

It applies the reviewed Arabic content only while the translation is Draft,
associates the root/group categories, approves (state 1/status 5) and publishes
(state 3/status 7) each language version with ETag guards. Readback requires:

- the exact planned Published/latest records, translation pairs and recorded roots;
- semantic HTML equality to reviewed content, retaining all official source links;
- exact owned category relationships and no extra versions under an owned number;
- unchanged unrelated article inventory/state/ETags;
- successful native `FullTextSearchKnowledgeArticle` results for each planned English
  and Arabic topical check, impersonating each approved representative;
- an effective native Knowledge Visibility filter that includes internal articles
  for each approved representative.

The search POST is a **read-only native search action**, not a publication.
Indexing waits are bounded. Searches inspect the first 100 results per topical
query; a larger catalog or delayed indexing can stop the gate. Inspect native
search and deliberately adapt/review the bounded query rather than reporting
success without finding the owned language record.

Queries come from each approved localized article title, without the generated
ownership prefix. Every group and selected representative must return the exact
owned article ID, public number and language. Custom organization content must
not inherit fixed sample search terms.

**Empty native Knowledge results with working Copilot citations:** these are
separate retrieval surfaces. Articles in this package are **internal**.
Knowledge search → **Filters** must include **Published**, **Internal** or
**Both**, and the intended article **Language**. An **External-only** default
excludes this entire package, even when indexing, permissions and Copilot are
working. Start with a concrete term from the approved content; a whole
conversation title is not a useful mandatory query.

The kit reads `msdyn_knowledgesearchfilters` and that representative's
`msdyn_knowledgepersonalfilters`. When internal articles are hidden, it creates
the representative's own filter with approval, or stops; see
[Internal knowledge visibility](#internal-knowledge-visibility). It does **not**
change global defaults, user UI language, article visibility, unrelated drafts or
public sources. Article-language filtering is separate from changing the user's
UI language.

Changing already-Published/Approved content is deliberately not an automatic
update operation. Use a separately reviewed native article versioning workflow
when the canonical knowledge changes; this kit will not overwrite a published
article or a third party's corrections.

### Knowledge input format

`knowledge.md` and its Arabic rendering use numbered `### NN. Title` topics. A
topic ends at the next `## ` heading, so end the last topic with a
`## Source registry` heading and put the registry under it:

```markdown
### 12. Last topic title

Approved text [S03].

## Source registry

[S03]: https://example.org/reference "Reference title"
```

Planning stops if a registry line sits inside a topic, which happens when that
heading is missing. The registry becomes each article's **Official sources** list,
not topic text.

**Blocked sources.** If an official website blocks the fetch tool (for example,
HTTP 403), don't publish the topic without it or swap in another source. Ask the
owner for an approved export or saved copy of the page. Report it as
`source blocked: <URL> (HTTP 403)`, separately from "no approved knowledge".

## Internal knowledge visibility

This package's articles are internal. Representatives can't find them when the
shared Knowledge Visibility filter shows only external articles
(`msdyn_defaultvalues` is `["0"]`) and they have no personal Visibility filter.

With approval `representative_knowledge_visibility`, the `knowledge` stage then
creates that representative's own filter, after checking that none exists:

```json
{
  "msdyn_knowledgepersonalfilterid": "<planned ID>",
  "msdyn_name": "Visibility",
  "msdyn_entityname": "knowledgearticle",
  "msdyn_attributename": "isinternal",
  "msdyn_enabled": true,
  "msdyn_preferredvalues": "[\"1\"]"
}
```

The kit sends the create as the representative (`MSCRMCallerID`
impersonation), so the representative owns the row. The setup account needs the
*Act on Behalf of Another User* privilege, which the knowledge search check uses
too. When the representative runs the kit, it sends no impersonation header.
Readback requires the representative as owner; the knowledge search check then
requires Internal in that user's effective filter. Rollback keeps the filter.

The kit never changes the shared filter, another user's filter, an existing
personal choice or article visibility. Without the approval, or when the
representative already has a filter that excludes Internal, the stage stops.
The representative can then select **Visibility: Internal** (or both values) in
Knowledge search filters and save. Last verified: November 2026. If that screen
differs, ask the owner for a screenshot that includes the left navigation, with
no secrets showing.

Learn: [msdyn_knowledgepersonalfilter](https://learn.microsoft.com/en-us/power-apps/developer/data-platform/webapi/reference/msdyn_knowledgepersonalfilter?view=dataverse-latest),
[impersonate another user](https://learn.microsoft.com/en-us/power-apps/developer/data-platform/webapi/impersonate-another-user-web-api).

## Native Copilot opt-in

Only for `enable_copilot: true`. An administrator does this in the browser; the
kit doesn't automate it. Do it before planning: the plan records a gate while
Copilot or Ask a question is off.

Last verified: November 2026. If a screen differs from this description, ask the
owner for a screenshot that includes the left navigation, with no secrets showing.

1. **Power Platform admin center → Manage → Environments → the environment →
   Generative AI features → Edit.** Review data movement with the owner. Some
   regions need **Move data across regions** before Copilot works. For an
   environment in Switzerland or another EU Data Boundary country, Copilot
   prompts and replies are processed within the EU Data Boundary, which can be
   outside that country. Clearing the box later doesn't undo data that has
   already moved. Leave **Bing search** off; this kit uses no web sources.
2. **Copilot Service admin center → Support experience → Productivity → Copilot
   settings → Manage.** The page shows the data movement status, a notice when
   Copilot isn't processed in your region by default, and a link to the terms.
   Review the terms with the owner, then select **Opt in**.
3. On that page, turn on **Copilot side pane** (shown on some pages as **Copilot
   help pane - Ask a Question**). **Copilot immersive** is optional.
4. **Translation using Copilot → Let representatives translate responses using
   Copilot** translates Copilot-written replies into the representative's
   language. It doesn't translate the caller or the live call. Optional.
5. **Support experience data → Record transcripts of representative interactions
   with Copilot** keeps representatives' Copilot chats for usage analysis. It
   doesn't record calls. Turn it on only if the owner wants that data.
6. **Agents within Copilot → Admin Management (preview)** is an optional agent for
   administrators. Leave it off unless the owner asks for it.
7. Leave **Agents within Copilot → Customer Support → Overview → Knowledge
   sources** to the `copilot` stage. With approval `shared_copilot_settings` it
   turns on the organization's knowledge base as the source.

The plan checks that Copilot and Ask a question are on and open to all
representatives and workstreams. The `copilot` stage then waits until native
knowledge is **Ready** (`msdyn_systemstatus` 100230003) **and** the worker result
in `msdyn_dvcopilotstatus` reports `success`. The default wait is 60 minutes: in
one environment Ready took about 39 minutes, and the earlier 900-second wait
timed out. Change it with `--copilot-wait-minutes` (1-240). Each status change is
saved in the receipt. If the token expires during the wait, the kit gets a new
one for that status read only; it never repeats a write.

On timeout, the message shows the last status and worker result and names the
pages to check: Copilot settings (opt-in, data movement) and Customer Support →
Overview → Knowledge sources. Then rerun `copilot` with the same output
directory; it doesn't send a second status request. Don't turn on the managed
**Check DV Copilot Sync Status Flow** or edit the status: in that environment the
flow stayed Off and the status still reached Ready.

Learn: [manage Copilot features](https://learn.microsoft.com/en-us/dynamics365/customer-service/administer/configure-copilot-features),
[Copilot help pane settings](https://learn.microsoft.com/en-us/dynamics365/contact-center/administer/copilot-enable-help-pane),
[data movement across regions](https://learn.microsoft.com/en-us/power-platform/admin/geographical-availability-copilot).

## 8. Align summaries and verify genuine native Copilot readiness

This is selected separately with `enable_copilot: true` and requires
`enable_knowledge: true`. It is not a prerequisite for the basic workspace.

```bash
"$PYTHON" infra/scripts/setup_dynamics_workspace.py copilot \
  --config "$CONFIG" --output-dir "$OUTPUT" \
  --apply --confirm-organization "$ORG_URL" \
  --copilot-wait-minutes 60 --poll-seconds 15
```

With the explicit shared-settings approval, only necessary differences are
patched:

| Family | Desired fields |
|---|---|
| `msdyn_agentcopilotsettings` | `msdyn_knowledgearticlesourceenabled=true` |
| Legacy `msdyn_conversationsummarysettings` | `msdyn_whenagentjoins=true`, `msdyn_conversationends=true` |
| Modern `msdyn_copilotsummarizationsettings`, summary type 0 | `msdyn_enabled`, `msdyn_casesummaryenabled`, `msdyn_ondemandenabled`, `msdyn_whenagentjoinsenabled`, `msdyn_whenconversationendsenabled` all true |

Align **both** summary families; a modern enabled flag alone is insufficient to
establish join/transfer behavior. Preserve the email-summary row and unrelated
email, analytics, translation, geography, create-case, consent and access-scope
flags. `msdyn_allowtranslation` is not a prerequisite to force on merely because
source text is Arabic and the representative UI is English.

If needed and approved, the runner discovers the installed activated
**Check Dataverse Copilot Status** action and calls the native
`msdyn_CheckDVCopilotStatus` workflow contract once. This can advance the
platform's real readiness workflow; it does not set a fabricated success value.
The gate reads **`msdyn_systemstatus=100230003` (Ready)** and the worker result
from the actual native setting; see [Native Copilot opt-in](#native-copilot-opt-in)
for the wait and timeout. The runner never PATCHes that status or
`msdyn_dvcopilotstatus`. If the native admin status remains Configuring/Processing
or the managed action is missing/ambiguous, stop and resolve provisioning through
the supported admin experience. Do not turn on a disabled managed flow or
overwrite its connections as an undocumented shortcut.

Ready proves control-plane readiness, **not** generated-answer quality, supported
Arabic behavior in a particular representative UI, or access to live cases.
Arabic Ask a question, draft-chat support and automatic knowledge-draft-from-case
are different capabilities; do not assume one implies the others.

## 9. Bind only the approved representatives and workstreams

```bash
"$PYTHON" infra/scripts/setup_dynamics_workspace.py bind \
  --config "$CONFIG" --output-dir "$OUTPUT" \
  --apply --confirm-organization "$ORG_URL"
```

This stage requires verified workspace records, published/searchable knowledge
and native Copilot readiness. It rechecks active conversations, profile membership
and workstream session state, then uses an ETag-guarded Dataverse changeset to:

- change only the supplied workstreams' `msdyn_sessiontemplate_default`;
- remove only recorded **direct** source-profile assignments for approved users;
- add those users to the dedicated profile.

Default/role-based source access is not dismantled. Other users and unrelated
application-profile memberships remain unchanged. The native
`msdyn_getAppConfigByAgent` readback must resolve exactly the dedicated profile
for every approved representative. Competing mappings or any unexpected
membership stop verification; there is no bulk cleanup.

After separately reviewing all stages, `all` is an explicit-apply convenience
that runs language → representative → workspace → knowledge → copilot → bind →
verify, stopping at the first gate. `representative` only records "not selected"
when the configuration has no `representative_setup`. It is not a way to bypass
the prerequisite or maintenance window requirements.

```bash
"$PYTHON" infra/scripts/setup_dynamics_workspace.py all \
  --config "$CONFIG" --output-dir "$OUTPUT" \
  --apply --confirm-organization "$ORG_URL"
```

## 10. Read back, recover interruptions and retain receipts

```bash
"$PYTHON" infra/scripts/setup_dynamics_workspace.py verify \
  --config "$CONFIG" --output-dir "$OUTPUT"
```

No `--apply` is allowed on `plan`, `verify` or `recover`. Verification performs
reads and native full-text search, not configuration writes, macro execution or
test calls.

The output directory contains:

- immutable `plan.json`, including selections, before-images, generated payloads
  and source hashes;
- ordered `event-*.json` files with a hash chain bound to the exact plan;
- intent written **before** each native mutation, its safe response, and verified
  ownership/state readback afterward;
- bounded language/readiness/search status evidence and explicit stage-stop
  receipts;
- a short-lived exclusive `workspace.lock` preventing two processes from using
  the same local receipt directory simultaneously.

Rerun the same stage with the same configuration/output to continue an already
confirmed deployment. Matching owned IDs alone are insufficient: expected fields,
states, relationships and native ETags/readbacks must match the receipt.
Changed or ambiguous records fail closed rather than being overwritten.

If an action's outcome is uncertain:

```bash
"$PYTHON" infra/scripts/setup_dynamics_workspace.py recover \
  --config "$CONFIG" --output-dir "$OUTPUT"
```

Recovery reconciles **reads only** with the recorded intent. It can recognize a
persisted create/patch/translation/binding/app publication or a demonstrably
unapplied HTTP failure. It does not reissue an uncertain native action.
A rejected create/patch is not allowed to adopt a different writer's subsequent
matching record/value. Shared-setting ownership after a lost response requires
native audit investigation, not a guess based on a matching boolean.
For a recorded rejection of `AddAppComponents`, `PublishXml` or
`RemoveAppComponents`, recovery compares both published and unpublished app
ETags/layers, authored configuration and complete component snapshots with the
recorded **pre-action** state. Only an exact unchanged match clears pending,
without claiming registration, publication or removal; then an explicit retry or
rollback can proceed. Missing snapshots, drift and uncertain outcomes stay
blocked rather than being treated as an unapplied rejection.
An unresolved language action without a returned job ID requires System Jobs
inspection and actual language availability, not submission of another job.
If the expected post-state cannot be established, the command stops; retain the
receipt and investigate before retrying or cleanup.

Do not delete or edit the journal to force continuation. A missing event, changed
plan/source/configuration hash, outside edit or stale ETag is a real stop.

When a write fails, run `recover` first: the write may have partly applied. Retry
once, and only with a concrete change. If it fails again, stop and ask the owner;
show the command, the error, the readback and the UI step or permission that
would unblock it. Don't try workarounds while you wait.

### Native behaviors the kit resumes from

These four were seen in one test deployment. None of them leads to a second
create or to adopting drift.

- **A new case form fails its first localization readback, then passes
  unchanged.** The create was sent once. The stage stops and keeps that readback
  in the journal (`create_readback_mismatch`). Wait a few minutes, then run
  `recover` with the same configuration and output directory. It only reads; when
  the form reads back as planned, it records ownership. Then rerun `workspace`.
  Don't resend the create or start a new plan.
- **A new profile stores `CustomerServiceApps`** instead of the requested app
  unique name. The kit accepts that one value only on the profile it just created.
  It then makes one ETag-guarded PATCH of `msdyn_appmoduleuniquename` back to the
  requested name, after checking that the profile has no users, the app's unique
  name is unchanged and the other profiles are unchanged, and reads the whole
  profile back. If that PATCH fails, `recover` confirms it didn't apply and the
  stage stops for the owner; it never sends the correction twice.
- **Updating one generated template parameter advances its siblings' ETags.** The
  kit records ownership of every planned generated parameter before changing any.
  Later checks compare every field except version and modified-time metadata, and
  each PATCH uses the ETag read just before it. A changed value still stops.
- **A planned extension link is already on a new profile** (seen with the
  Productivity Pane Loader). The kit reads the profile's links right after
  creating it and records the planned ones that are present instead of posting
  them. A link that isn't in the plan stops the stage; nothing is removed.

### Explicit script-only recipe correction

Do not edit an applied plan's hashes to accommodate a helper correction.
Preserve an owner-only exact copy of the original script and review/test the
new version first. With no pending mutation or active conversation, explicitly
accept the script-only update:

```bash
"$PYTHON" infra/scripts/setup_dynamics_workspace.py accept-recipe-update \
  --config "$CONFIG" --output-dir "$OUTPUT" \
  --previous-script "$PRIVATE_PARENT/setup_dynamics_workspace.original.py" \
  --approve-recipe-update --confirm-organization "$ORG_URL"
```

This performs native reads/searches, not configuration writes. It requires the
original source backup to match the immutable plan, unchanged config/data and
identical rendered workspace/knowledge payloads. Existing workspace records,
scoped app publication and real localized Knowledge searches must pass first.
It appends a separately approved source-version event; no original plan/event
is rewritten. Ordinary stages and portable composition both enforce the exact
accepted digest and retained original source backup. Further unapproved edits,
data changes, missing backups and pending mutations remain blocked.

For a subsequent separately reviewed script correction, preserve the exact
currently accepted script and pass that backup as `--previous-script`. Each
acceptance appends a version transition; every previous source backup and the
original plan remain required. An older backup is not authority for skipping
an accepted version or changing data.

If a process was killed and left `workspace.lock`, verify its recorded process
has stopped and there is no other operator before removing **that one lock file**.
Never remove the plan/events as a lock workaround.

## 11. Scoped rollback — detach and retain, not destructive purge

Review the rollback inventory first:

```bash
"$PYTHON" infra/scripts/setup_dynamics_workspace.py rollback \
  --config "$CONFIG" --output-dir "$OUTPUT"
```

After draining selected workstreams and approving the maintenance window:

```bash
"$PYTHON" infra/scripts/setup_dynamics_workspace.py rollback \
  --config "$CONFIG" --output-dir "$OUTPUT" \
  --apply --confirm-organization "$ORG_URL"
```

Rollback only uses exact recorded ownership and expected applied state:

1. Restores selected workstream template values and the original approved users'
   direct source-profile memberships; detaches them from the owned profile.
2. Restores only shared flags this run actually changed, with fresh ETags and
   expected-value checks.
3. Archives owned Published knowledge content (state 5/status 12), leaving
   unrelated articles untouched. Draft/Approved partial content remains
   unpublished.
4. Deactivates owned macros.
5. Removes exactly the owned form components with `RemoveAppComponents`, verifies
   the original component/default set, and publishes **only the app**.
6. Retains the owned nondefault forms, profile, sessions, tabs, scripts, categories,
   native root containers and receipts for audit. It does not delete records that
   might have acquired external references.
7. Retains the representative's roles, bookable resource, capacity association,
   queue membership and personal Visibility filter added by this kit. The
   inventory lists them; removing them is a separately reviewed admin change.

Arabic organization availability is intentionally **not unprovisioned**: it is
a shared capability that may now be used by other people. Representative language
settings remain unchanged. A destructive purge/versioning migration is a separate
administrator-reviewed task, not a wildcard rollback feature.

This rollback path has offline state/concurrency coverage, not a live rollback
claim. Native plugin/state-transition differences must stop the operation for
review. A rolled-back deployment is not automatically reactivated by `all`; a
new deployment needs a deliberate new namespace/prefix.

## 12. Native UI acceptance checklist

Use approved synthetic fixtures and a separate, explicitly authorized acceptance
window. This setup task did not make calls, send messages or perform browser
acceptance.

1. Reload the selected workspace as an approved representative. Confirm the
   dedicated effective profile; compare another representative's unchanged
   profile. Confirm the current English UI is still English.
2. Open voice and case sessions. Confirm native anchor/case forms, customer,
   linked case, native timeline, Knowledge, Copilot and Agent scripts. Missing
   customer/case context must stay missing; a lookup list must not claim linkage.
3. Test a contact with active **and resolved** cases, including app-user-owned
   cases. Related Cases must contain that customer's records and exclude another
   customer's cases. Confirm `customerid`, not Primary Contact, drives the grid.
4. Open an account customer and an absent-customer context. Neither should force
   the contact clone; existing account form/default navigation must remain intact.
5. Review the nine macros using synthetic records. New case/task actions must
   open unsaved native forms; new case must show the confirmed Customer and the
   existing custom case fields, including values on saved cases. The explicit link action must require the correct
   saved focused case and live conversation; wrong/unsaved/missing contexts must
   fall back safely. Never resolve a case merely because a call ended.
6. In English UI, confirm 15 voice steps, 9 case steps and a 4-step English
   wrap-up route. After a representative **chooses** the provisioned Arabic UI,
   confirm Arabic-first script selection and the same-language wrap-up route.
   Check RTL layout and actual labels visually; the contract tests do not render
   a browser. Run the preserved-baseline verifier before this deliberate UI
   change: later verification reports an out-of-band UI-language change rather
   than silently resetting the user's choice or rewriting the baseline receipt.
7. Search native Knowledge in both languages and open the true translation
   counterpart. Validate the official source links and internal-only article
   availability.
8. Ask covered English and Arabic service questions in the appropriate UI.
   Inspect actual citations, source language, unsupported-feature behavior and
   grounded answers; Ready/searchable articles alone do not prove these.
9. Exercise native case/on-demand/join/transfer/end summaries where authorized.
   A case summary needs sufficient source content (documented minimum 50 tokens);
   do not pad descriptions or rewrite history to force a result. Case summary uses
   the configured case fields and linked notes/activities, not every open voice
   tab. The four custom case fields are **not automatically added to summary
   mapping** by exposing them on a form.
   For a closed call use **Activities → Conversations → the conversation →
   Summary** inside the native Conversation record. Its stored Copilot output can
   be in `msdyn_conversationinsight.msdyn_copilotsummary` even when
   `msdyn_ocliveworkitem.msdyn_conversationsummaryfield` is empty. Generation and
   transcript persistence are asynchronous; inspect the actual stored result.
   Separate AI-phase, handover/join and human-phase source coverage: some sessions
   do include AI speech, but a merged participant ID does not establish speaker
   identity. Do not infer universal coverage or a distinct join result from a
   single end-summary record.
10. Record actual outcomes privately. Mark unexercised generation, quality,
    browser, call/transfer and live-case querying checks as **not tested**, not
    pass-by-configuration.

## 13. Offline and optional installed-native-client tests

### Native-channel presence prerequisite

A case-only workspace profile can render cases and Copilot while still lacking
the native channel provider. Before a native call, inspect the effective
`msdyn_getAppConfigByAgent` result for the approved user: the intended dedicated
profile must also resolve the installed native Omnichannel provider and its
CIF v2/CCaaS loaders. Do not equate the phone icon, Copilot answers or configured
queue capacity with an Available representative.

Use the managed **Customer Service workspace + channels** profile from the
same organization as the native-component source. Preserve existing
Inbox/Productivity loaders, forms, scripts and user assignments; never copy a
provider URL from another deployment. An already-applied case-only base needs
a separately reviewed additive association upgrade. Its pinned
`native-channel-profile-upgrade-v1` receipt must identify the original base
hash, exact owned profile, installed managed source/component snapshots and
only the three native references. Each association needs original
intent/response/readback in the base journal before registering
`native_channel_upgrade`; matching existing links are not ownership.

Base and portable-addon verification accept only that exact recorded delta.
Unknown extra providers, other profiles, unrecorded associations, changed
managed sources and missing receipts remain stops. Retained-resource base
rollback retains these references on the unassigned owned profile; it does
not modify or delete the Microsoft-managed components. Actual presence,
microphone and browser audio still require owner observation after reload.

```bash
PYTHONPATH=infra/scripts "$PYTHON" -m unittest \
  infra.tests.test_dynamics_workspace_setup -v
```

The always-on tests cover rendering, the exact inventory, current-contact versus
account/missing-context behavior, localization, canonical/source coverage,
native lifecycle mocks, ETags, idempotency, interruption recovery, bounded gates,
scoped app publication and retained-resource rollback.

An optional test executes the **installed Microsoft client runtimes privately**
in Node VM sandboxes with network, Dataverse writes, run-history writes and native
navigation replaced by strict mocks. Do not vendor Microsoft-managed bundles.
Obtain the installed versions through an authorized **read-only** export, retain
them privately, and provide their paths and the installed macro-action catalog:

```bash
export DYNAMICS_NATIVE_APP_RUNTIME="$PRIVATE_PARENT/native-app-runtime.js"
export DYNAMICS_NATIVE_MACRO_RUNTIME="$PRIVATE_PARENT/native-macro-runtime.js"
export DYNAMICS_NATIVE_SCRIPT_RUNTIME="$PRIVATE_PARENT/native-script-runtime.js"
export DYNAMICS_NATIVE_ACTION_CATALOG="$PRIVATE_PARENT/native-macro-actions.json"
PYTHONPATH=infra/scripts "$PYTHON" -m unittest \
  infra.tests.test_dynamics_workspace_setup.ClientContractTests -v
```

The action catalog can be the private setup `plan.json` (`native.macro_actions`)
or a Web API response containing `value` rows with `msdyn_name` and
`msdyn_runtimeapi`. The optional test checks native contracts across the three
customer-template pins, unsaved customer-default parameters, nine macros/safe contexts, native LCID selection, step
parsing and same-language wrap-up routing. Without all four inputs the installed
runtime test is explicitly **skipped**; the authored declarative contracts still
run. Neither test claims live browser rendering or persisted new-kit deployment.

## Native technical references

- [Create experience profiles](https://learn.microsoft.com/en-us/dynamics365/customer-service/administer/create-agent-experience-profile)
- [Session templates](https://learn.microsoft.com/en-us/dynamics365/customer-service/administer/session-templates)
- [ProvisionLanguageAsync](https://learn.microsoft.com/en-us/power-apps/developer/data-platform/webapi/reference/provisionlanguageasync)
- [RetrieveProvisionedLanguages](https://learn.microsoft.com/en-us/power-apps/developer/data-platform/webapi/reference/retrieveprovisionedlanguages)
- [AddAppComponents](https://learn.microsoft.com/en-us/power-apps/developer/data-platform/webapi/reference/addappcomponents)
- [PublishXml](https://learn.microsoft.com/en-us/power-apps/developer/data-platform/webapi/reference/publishxml)
- [CreateKnowledgeArticleTranslation](https://learn.microsoft.com/en-us/power-apps/developer/data-platform/webapi/reference/createknowledgearticletranslation)
- [Knowledge article lifecycle](https://learn.microsoft.com/en-us/dynamics365/customer-service/administer/work-knowledge-articles)
- [Configure Copilot features](https://learn.microsoft.com/en-us/dynamics365/customer-service/administer/configure-copilot-features)
- [Enable native summaries](https://learn.microsoft.com/en-us/dynamics365/contact-center/administer/copilot-enable-summary)
- [Copilot regions, languages and service limits](https://learn.microsoft.com/en-us/dynamics365/customer-service/administer/cs-region-availability-service-limits)

## 14. Exact optional Pause AI composition

The [native supervision kit](supervision-setup.md) owns its command table,
permissions, controller changes, webresources and **one added tab reference**.
The workspace kit does not adopt those records. This integration supports exactly
one explicitly reviewed voice-session template and the supervision-owned tab,
through `msdyn_sessiontemplate_applicationtab` only. It is not a generic extension
framework or permission to accept arbitrary extra tabs, scripts or components.

**Configuration acceptance is not UI acceptance.** A reviewed native configured
selector, exact template ownership and actual before-set are sufficient to stage
the intended configuration. No paid call, open Monitor session or new Join action
is required to capture a configuration baseline or accept this composition.
Every composition receipt retains:

```json
{
  "configuration_only": true,
  "configured_selector_only": true,
  "rendered_host_verified": false,
  "ui_acceptance": "pending",
  "acoustic_acceptance_verified": false,
  "live_pause_test_performed": false
}
```

The inspected native Monitor path uses
`TemplateManager.getSessionTemplateForWorkStream` and the selected workstream's
`msdyn_sessiontemplate_default`. Custom-template failure can fall back to the
managed voice template; existing sessions can also be reused/restored. Join uses
the current conversation/session. These facts establish the configured selector,
not the actual host in a particular browser. If a later, separately authorized
user observation is available, record it separately without rewriting these
configuration-only receipts:

```javascript
const session = Microsoft.Apm.getFocusedSession();
const context = await session.getContext();
console.log(session.sessionId, context.templateName,
            await context.get("LiveWorkItemId"));
```

Use an already-open monitored conversation, confirm unchanged focus and the
correct conversation ID, and retain the real result. Do not fabricate this result
from a workstream setting or start a call solely to obtain it.

### Fresh tenant: real portable base, then independently owned addon

1. Complete sections 1–10 with this version of the workspace helper. Retain its
   actual `plan.json`, successful `configuration_verified` checkpoint and
   complete event chain.
2. Plan the supervision kit separately. Select the base's **owned voice session**
   ID from its real receipt, not a managed default or an unrelated unmanaged
   template. Keep the supervision plan's captured before-membership identical to
   the workspace plan's baseline.
3. Perform the supervision guide's separately approved disabled/idle native
   provisioning, publication and `bind` steps, then its read-only `verify`.
   The workspace composition command does not perform these native changes.
4. Explicitly accept the already-proven delta locally:

```bash
BASE_OUTPUT="$OUTPUT"
SUP_OUTPUT="$PRIVATE_PARENT/native-supervision"
EXTENSION="$PRIVATE_PARENT/pause-ai-extension.json"
"$PYTHON" infra/scripts/setup_dynamics_workspace.py accept-extension \
  --base-receipt "$BASE_OUTPUT" --supervision-receipt "$SUP_OUTPUT" \
  --output "$EXTENSION" --approve-extension \
  --confirm-organization "$ORG_URL"
```

Acceptance validates both private receipt chains and pins their plan hashes and
verified **checkpoints**, not mutable “latest” filenames. Later valid journal
continuation is allowed; the pinned checkpoint must still exist unchanged.
It requires:

- identical organization and tenant;
- the exact base-owned voice session, approved workstream/actor subsets and
  current configured workstream selector;
- supervision's `plan.templates[session].tabs` equal to the complete base set;
- exact creation intents with absent-before state, successful unrejected
  create/bind/publication evidence and current supervision verification;
- only the expected owned entity/webresource publication, not an app-wide publish;
- live membership **equal to baseline plus the one receipt-owned addon tab**.

The helper rechecks the other normal portable-base guards, writes the private
acceptance file exclusively, and appends an `extension_accepted` event to the base
journal. It never edits the old base plan/events or changes the baseline's owned
records/links. A repeated acceptance reuses the same file/registration and
revalidates it without issuing native mutations.

```bash
"$PYTHON" infra/scripts/setup_dynamics_workspace.py verify-extension \
  --extension-receipt "$EXTENSION"
"$PYTHON" infra/scripts/setup_dynamics_workspace.py verify \
  --config "$CONFIG" --output-dir "$BASE_OUTPUT" \
  --extension-receipt "$EXTENSION"
```

Normal base verification/replay also discovers the registered acceptance
automatically. Supplying a file explicitly requires the same recorded
fingerprint. It compares exact baseline-plus-approved membership; an unknown
extra reference, changed base record, wrong plan/checkpoint, rejected intent or
started addon release remains a hard stop.

**Version boundary:** the existing base script/data hash guard is unchanged.
Create new portable base plans with the composition-capable helper before
provisioning. An older, already-applied portable plan requires a separate explicit
recipe-version migration, not editing `input_hashes` or fabricating a replacement
ownership journal. The legacy flow below is not that migration.

### Existing private-recipe deployment: labelled observed legacy baseline

An older deployed workspace can have genuine historical evidence but **no
portable-kit plan/journal**. Do not synthesize one. Retain its original evidence,
the independently reviewed configured-selector proof, and the supervision
read-only plan. Before binding the addon, capture an explicitly labelled
`observed-legacy-workspace-baseline-v1`:

```bash
LEGACY_EVIDENCE="$PRIVATE_PARENT/original-workspace-evidence.json"
SELECTOR_EVIDENCE="$PRIVATE_PARENT/native-monitor-selector-evidence.json"
LEGACY_BASELINE="$PRIVATE_PARENT/legacy-workspace-baseline.json"
LEGACY_PROFILE_ID="<REVIEWED_EXISTING_PROFILE_ID>"
WORKSPACE_APP_ID="<REVIEWED_EXISTING_WORKSPACE_APP_ID>"
"$PYTHON" infra/scripts/setup_dynamics_workspace.py capture-legacy-baseline \
  --supervision-receipt "$SUP_OUTPUT" \
  --profile-id "$LEGACY_PROFILE_ID" --app-id "$WORKSPACE_APP_ID" \
  --legacy-evidence "$LEGACY_EVIDENCE" --selector-evidence "$SELECTOR_EVIDENCE" \
  --output "$LEGACY_BASELINE"
```

This performs configuration reads only. It verifies the actual nondefault
unmanaged profile, effective approved-user assignments, app/component baseline,
unmanaged template identity, selected workstreams and exact before-set. The addon
must not already be present. The receipt pins the original legacy evidence and
selector proof and states `portable_ownership_claimed=false`.

The selector evidence must be an actual reviewed
`native-monitor-selector-evidence-v1` record, with matching
`organization_url`, `tenant_id`, `profile_id`, `app_id`, `session_template_id`,
`template_name` and `workstream_ids`; `configured_selector_proven=true`;
`rendered_host_proven=false`; and `source.url`, `source.sha256` and
`source.selector=TemplateManager.getSessionTemplateForWorkStream` identifying the
reviewed versioned native conversation-control bundle. Do not turn a screenshot,
guessed target or lone workstream flag into this evidence. The parent/operator
supplies the actual private proof; repository examples contain no deployed IDs.

After the separately approved supervision provisioning/binding verifies:

```bash
"$PYTHON" infra/scripts/setup_dynamics_workspace.py accept-extension \
  --legacy-baseline "$LEGACY_BASELINE" --supervision-receipt "$SUP_OUTPUT" \
  --output "$EXTENSION" --approve-extension \
  --confirm-organization "$ORG_URL"
"$PYTHON" infra/scripts/setup_dynamics_workspace.py verify-extension \
  --extension-receipt "$EXTENSION"
```

This verifies the exact observed legacy baseline plus the independently owned
delta. It does **not** certify all historical workspace/CRM changes or give the
portable workspace rollback ownership of legacy records. The original legacy
operator remains responsible for those operations.

Adding a separately approved scoped form later changes the legacy app-component
baseline. **Do not edit old receipts, recapture a pre-binding baseline with the
addon already present, silently adopt the legacy workspace into a portable
journal, or remove Pause AI merely to make verification pass.** The original
operator must prepare an explicit baseline migration: pin the old extension and
legacy manifest hashes, retain exact before/after app snapshots and patch receipt,
allow only the reviewed nondefault form-component delta, and prove that profile
memberships, session selector, all preexisting components/defaults and the exact
Pause AI edge are unchanged. Record the observed new baseline and successor
extension in **new immutable receipts**, referencing the original evidence and
patch. The original strict verifier must pass against that exact successor;
unreviewed drift remains a stop. A proposed migration is not an applied receipt.

### Release the addon before changing/removing the base

Base rollback refuses active registered extensions. It also detects unexplained
extra session-tab references even if an operator forgot to register the addon.
Removing a reference by hand is not a release receipt.

Do not use a destructive supervision rollback to evade command-history
retention. Use the supervision owner's minimal `unbind`, which retains command
history, schema, roles, controllers, tab and webresources:

```bash
"$PYTHON" infra/scripts/setup_native_supervision.py unbind \
  --config "$PRIVATE_PARENT/supervision.json" --output-dir "$SUP_OUTPUT" \
  --apply --confirm-organization "$ORG_URL"
"$PYTHON" infra/scripts/setup_native_supervision.py verify-unbound \
  --config "$PRIVATE_PARENT/supervision.json" --output-dir "$SUP_OUTPUT"
RELEASE="$PRIVATE_PARENT/pause-ai-extension-release.json"
"$PYTHON" infra/scripts/setup_dynamics_workspace.py release-extension \
  --extension-receipt "$EXTENSION" --output "$RELEASE" \
  --approve-extension --confirm-organization "$ORG_URL"
"$PYTHON" infra/scripts/setup_dynamics_workspace.py verify-extension \
  --extension-receipt "$EXTENSION" --release-receipt "$RELEASE"
```

`unbind` requires the supervision guide's disabled/idle and maintenance-window
guards. Only the original owned reference is removed. `release_intent`/`released`
evidence and live exact restored baseline membership are required; an interrupted
release, revived reference or foreign edge cannot be silently accepted/deleted.
Workspace release writes another immutable private file and appends
`extension_released` to a real portable base journal. Only then may its normal
rollback proceed. For legacy deployments, the parent/operator must require the
verified release before its separately owned legacy rollback.

All four workspace composition commands are read-only in Dataverse and reject
`--apply`. `--approve-extension` authorizes only local evidence/registration.
Neither acceptance nor release upgrades pending rendered-host, browser, acoustic
or live-pause acceptance to “passed.”
