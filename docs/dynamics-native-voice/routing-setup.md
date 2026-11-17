# Native multi-operator queue and capacity onboarding

This kit adds already-approved human representatives to **one existing native
voice queue** and associates their existing User bookable resources with the
**existing inbound capacity profiles** already bound to the selected
workstreams. It isn't a routing engine: Dynamics continues to make routing
decisions.

The implementation is
[`setup_dynamics_routing.py`](../../infra/scripts/setup_dynamics_routing.py).
It defaults to read-only discovery and planning, and has offline safety and
payload-contract tests. Plan it read-only in your tenant first, then verify the
applied result there. It doesn't place test calls. Readbacks show that the
configuration persisted, not actual distribution, presence, licensing or
service-level performance.

**Activation remains blocked without at least two individually approved,
eligible existing humans.** Do not reuse another account to satisfy that count.
Multi-representative human routing does **not** enable multi-AI scaling: the
existing AI integration remains single-concurrent-call.

For the first representative, use the workspace kit's
[single representative setup](workspace-setup.md#single-representative-setup).
It shares this kit's queue, capacity-profile and resource checks and the same
capacity association body, and it also adds missing roles and creates the
bookable resource when the user has none.

## What stays untouched

- Queue assignment strategy, priority, operating hours, overflow, SLA/abandonment
  thresholds, existing members, and routing/assignment rules.
- Existing capacity profiles, their limits, blocking/reset policies, workstream
  bindings, existing resource associations and custom limits.
- Identities, licenses, roles, persona mappings, bookable resources, presence,
  skills, calendars, application access, runtime configuration and deployments.
- Dynamic `msdyn_agentcapacityprofileunits` consumed/available counters. These
  are observed only; the kit never creates, patches or resets them.

No skills, business-hours/calendar policy, overflow policy, queue-priority
changes or SLA targets have been supplied. They remain **explicit later policy
requirements**, with separate review and acceptance. An existing queue with no
hours is effectively available around the clock in native Dynamics; preserving
that setting does not approve it as the intended business policy.

## Prerequisites and exact identities

Use the [main prerequisites](README.md), then collect identifiers privately from
the native admin experience. There is no tenant-wide search for substitute
operators and no identity creation:

1. Supply the exact organization origin, Entra tenant, Dataverse organization ID,
   interactive setup administrator's Dataverse user ID **and** Entra object ID.
   The signed-in Azure CLI identity must match. Never use a runtime application
   identity for setup.
2. Select one active Omnichannel **Voice** queue. Each selected workstream must
   be active, native inbound voice, use that queue as its default queue, and
   already have exactly one active, published binding to the specified capacity
   profile. Custom route-to-queue branches are not evaluated by this kit.
3. For each human, supply their Dataverse `systemuserid`, matching Entra object
   ID, and the ID of their **one active User bookable resource**. Enabled,
   licensed, read/write access mode and absence of application identifiers are
   necessary, not sufficient, eligibility checks. Missing/ambiguous resources,
   inactive users, application users and conflicting associations stop apply.
4. Supply exact effective security-role IDs. This bounded kit requires
   **Basic User**, **Omnichannel agent**, and **App Profile User** among those
   IDs; it reads both direct and team-inherited assignments. Additional required
   roles can be listed. **Customer Service Representative** is needed for case
   work; productivity/workspace features have their own prerequisites. Confirm
   native persona mapping, workspace access and relevant record privileges
   separately, using `native_access_admin_confirmed`.
5. **Omnichannel API access must not be assigned to interactive representatives**,
   directly or through teams. The kit rejects it; it never removes roles. Custom,
   renamed or localized replacements for the required native role names need
   administrator reconciliation, not automatic equivalence or privilege grants.
6. A tenant administrator must verify each human's actual Customer Service /
   Contact Center **voice entitlement and provisioned service plans**, recording
   the approval reference and setting that human's
   `voice_license_admin_confirmed` only after verification. Dataverse
   **`islicensed=true` is not proof of a voice SKU**. No license API, purchasing,
   user provisioning or license assignment is performed here.
7. Review the discovered queue policy and **all workstreams sharing the selected
   profiles**. A resource/profile association is user-wide, not queue-local.
   Set `shared_capacity_and_queue_policy_reviewed` only after approving that
   impact. For every representative, explicitly list each distinct selected
   profile once. `max_units: null` means inherit its current default; a positive
   integer means an explicitly approved per-representative limit. Existing
   associations must match exactly, including null versus a custom limit.
   Conflicts are not overwritten and unrelated capacities are preserved.
8. Use a drained maintenance window with no concurrent queue/capacity/user/role
   editors. The guard deliberately requires **no nonclosed conversations anywhere
   in the organization**, not just on the chosen workstreams, because capacities
   can be shared. It also checks observed representative capacity consumption.
   Missing dynamic rows do not prove availability. A guard is not an atomic lock
   against a call arriving afterwards.

Native UI references:
[queues](https://learn.microsoft.com/en-us/dynamics365/customer-service/administer/queues-omnichannel),
[capacity profiles](https://learn.microsoft.com/en-us/dynamics365/customer-service/administer/capacity-profiles),
[user/bookable-resource setup](https://learn.microsoft.com/en-us/dynamics365/customer-service/administer/users-user-profiles),
and [security roles](https://learn.microsoft.com/en-us/dynamics365/customer-service/implement/add-users-assign-roles).
Use **Queues → selected queue → Add users** and **User management → Capacity
profiles → selected existing profile → Users → Add user** for supported manual
administration. Do not create a new profile merely to bypass a conflict.

## Private configuration and read-only review

Use the existing Python environment with `python-dotenv` and `PyYAML`, and an
authorized Azure CLI login. See [workspace tooling](workspace-setup.md) for
dependency restoration if those existing dependencies are unavailable.

Create a restricted private parent outside the checkout and copy
[`examples/routing-config.example.json`](examples/routing-config.example.json)
there. Fill placeholders privately. The example's approvals are deliberately
false. Never commit the filled configuration, discovery, plans or receipts.
Input files must be owner-only; output directories are mode 700 and new files
mode 600. Commands below use environment variables, not deployment identifiers:

```bash
# CONFIG: owner-only filled configuration outside the checkout.
# ROUTING_DIRECTORY: a NEW path in an existing private parent outside the checkout.
# PYTHON: the existing Python interpreter with the repository's dependencies.
"$PYTHON" infra/scripts/setup_dynamics_routing.py \
  --config "$CONFIG" --directory "$ROUTING_DIRECTORY"
```

Default planning performs GETs only. It writes immutable `plan.json` and a
hash-linked review receipt containing selected identities, direct/team roles and
their privilege definitions, native relationship metadata, queue policy, complete selected workstream/profile
records, members, resource associations, profile consumers and idle observations.
A one-person configuration is allowed **for audit only** and reports the missing
second-operator gate. Unknown IDs, inaccessible tables and unexpected schemas
are hard failures; the kit never treats an unreadable collection as empty.
An initially gated plan is audit-only and contains no mutations. Resolve its
prerequisites, including any initial idle-call gate, then create a new plan.

Review the private plan and receipt, not just the CLI summary. Each future write
has an exact target, body and before-image. The CLI reports both digests. A review
expires after **15 minutes**, must be the latest receipt, and must match a fresh
readback. Any identity, role, queue policy, workstream/profile, member or resource
association drift stops apply. A fresh review can renew an unchanged or partially
completed plan without discarding its ownership history:

```bash
"$PYTHON" infra/scripts/setup_dynamics_routing.py \
  --directory "$ROUTING_DIRECTORY" --review
```

A changed desired configuration or newly approved operator requires a new plan;
never edit `plan.json` or an existing receipt. Review does not silently rebase
unrelated changes into an old plan.

## Explicit additive apply and verification

Only run this after all prerequisites and administrative approvals are satisfied:

```bash
"$PYTHON" infra/scripts/setup_dynamics_routing.py \
  --directory "$ROUTING_DIRECTORY" --apply \
  --confirm-organization "$ORG_URL" \
  --reviewed-plan-sha256 "$PLAN_SHA256" \
  --reviewed-receipt-sha256 "$RECEIPT_SHA256" \
  --maintenance-window-confirmed

"$PYTHON" infra/scripts/setup_dynamics_routing.py \
  --directory "$ROUTING_DIRECTORY" --verify
```

The only write contracts are:

| Purpose | Native contract |
|---|---|
| Add one human to the selected queue | `POST queues(<queue-id>)/queuemembership_association/$ref`, body `{"@odata.id":"<organization-origin>/api/data/v9.2/systemusers(<user-id>)"}` |
| Add one missing resource/profile association | `POST msdyn_bookableresourcecapacityprofiles`, with a plan-reserved primary ID, `msdyn_name`, `msdyn_bookableresourceid@odata.bind`, `msdyn_capacityprofileid@odata.bind`, explicit nullable `msdyn_maxunits`, active state and status |

The capacity navigation properties bind to `/bookableresources(<resource-id>)`
and `/msdyn_capacityprofiles(<profile-id>)`. Readback uses
`_msdyn_bookableresourceid_value` and `_msdyn_capacityprofileid_value`; workstream
bindings instead use `_msdyn_workstream_id_value` and
`_msdyn_capacityprofile_id_value`. They are different contracts. The installed
relationship metadata is checked before planning; see the Microsoft
[association table reference](https://learn.microsoft.com/en-us/dynamics365/developer/reference/entities/msdyn_bookableresourcecapacityprofile).

Capacity associations are completed **before** adding queue memberships.
Existing matching associations/members are no-ops, not adopted as owned.
Every write is preceded by a durable intent receipt and fresh configuration/idle
checks; acknowledgement and persisted readback are separate receipts.
Verification checks every approved human's membership and exact active resource
capacity outcomes while confirming preserved configuration. Re-reviewing and
reapplying a completed plan makes no additional writes.
Incomplete verification and in-doubt recovery return a nonzero exit status;
read-only audit plans may complete successfully while explicitly listing gates.

Native capacity updates can take **up to 15 minutes** to synchronize. Have
representatives refresh their browser and verify native presence/capacity in the
admin UI. Configuration success is not a claim that the next call will route to
either representative. Actual routing, busy/no-answer/overflow behavior, skills,
hours and SLA acceptance require separately approved policies and test calls.

## Interrupted writes, ownership and rollback planning

Keep the original plan directory. Never resolve a timeout by generating new
association IDs or adopting matching display names.

```bash
"$PYTHON" infra/scripts/setup_dynamics_routing.py \
  --directory "$ROUTING_DIRECTORY" --recover

"$PYTHON" infra/scripts/setup_dynamics_routing.py \
  --directory "$ROUTING_DIRECTORY" --rollback-plan
```

Both commands are **read-only against Dataverse**. Recovery reconciles an
outstanding intent with current records. A matching reserved capacity row must
also have the expected creator and a creation time consistent with the intent.
A successful but unacknowledged queue addition can be recorded as present, never
as exclusively owned. A definitive rejected request with unchanged absent state
can be cleared for a fresh review and retry. A timeout/transport error with no
persisted result remains **in doubt**: absence is not proof that a delayed request
will never commit. No automatic replay is allowed; retain the receipts and
resolve the request with the administrator before proceeding.

Rollback planning includes exact original and observed outcomes:

- Pre-existing queue memberships and capacity associations are retained.
- A confirmed new capacity row is only a **manual conditional-delete candidate**
  when its exact readback/ETag, identity, surrounding state and idle guards still
  match. The plan includes its `If-Match` condition, not an executable rollback
  command. Recheck all shared-workstream impacts and drain again before a human
  performs any change.
- **Queue links have no per-link creator/ETag ownership evidence. They are always
  retained by this kit**, even when a before-absent/after-present receipt exists.
  Concurrent administrators can add the same link. A human must establish that
  removal is authorized and coordinate it with capacity rollback; the kit never
  silently disassociates a shared member.
- Pending requests, changed records, active conversations or eligibility drift
  block deletion candidates. There is no automatic rollback apply.

Receipts are exclusive, append-only and hash-linked to the immutable plan.
Broken internal links, missing sequence entries or mismatched plans stop replay;
the explicitly reviewed receipt digest also pins the latest review. These are
local integrity/ownership evidence, not signed authorization or proof of exclusive
tenancy. Keep the directory private and backed up.

## Remaining activation inputs and acceptance

- At least two approved existing human identities, verified entitlements,
  correct native roles/app access and unambiguous active User bookable resources.
- Exact queue/workstream/profile IDs, explicitly approved per-human limits,
  shared-profile impact approval, fresh reviewed state and a drained window.
- Explicit skills/proficiency, calendars/business hours, queue strategy/priority,
  overflow and SLA decisions if the business requires changes. None are guessed.
- Separately authorized native routing acceptance with two humans, followed by
  documented evidence. No live calls, purchases or mutations were used to build
  this slice; AI concurrency and supervision remain separate work.

Focused offline validation:

```bash
PYTHONPATH=infra/scripts "$PYTHON" -m unittest discover \
  -s infra/tests -p 'test_dynamics_routing_setup.py'
```
