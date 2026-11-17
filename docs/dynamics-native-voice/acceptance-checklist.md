# Comprehensive acceptance checklist

**7 November 2026 - accepted voice settings, restored startup, CRM, workspace and Pause AI**

This is a test procedure, not a record of passed tests. Start with the core run:
offline workspace/chat checks, **three separate calls of at most 90 seconds
from dialing**, then offline artifact checks. Most testing does not require
keeping a paid call connected.

[Deployment guide](README.md) | [Operations](operations.md) |
[Supervision contract](supervision-setup.md) | [Routing scope](routing-setup.md)

## 1. What should work, and what must not be assumed

| Area | Acceptance boundary |
|---|---|
| Customer context | Eligible Dynamics customer/account/case context; name and configured initial voice language |
| CRM tools | Shared voice/chat implementation; confirmed case creation and append-only follow-up notes |
| Related Cases | Current customer's active and resolved cases, including application-owned cases |
| Normal handover | AI media departs; native representative accepts the same conversation |
| Voice interruption | Accepted `fast_vad` + `off` settings; measure cutoff, backchannels and mid-sentence pauses. User web acceptance is not a general latency SLA or a substitute for native phone acceptance |
| Case Details | Three-field association/readback and scoped case navigation are implemented; actual post-fix panel rendering and prefetched selection still require observation |
| Native Monitor/Join | Monitor is silent; Join makes the operator audible. Neither automatically pauses HUMAIN |
| Explicit Pause AI | Blocks AI input, output and new tools after confirmed acknowledgment; preserves human audio |
| Pause versus transfer | Pause does not join, assign, transfer, resolve a case or end the native call |
| Resume | No resume action and no automatic resume when the operator leaves |
| Native Copilot | Knowledge Q&A and configured summaries; **live-case querying is not configured** |
| Routing | Guarded multi-operator setup tooling exists; live expansion needs another approved eligible operator |
| Other channels | Existing custom text chat uses CRM. Native Dynamics website chat/email are separate backlog work |

Use **PASS / FAIL / BLOCKED / NOT TESTED**. Record a known gap as a gap, not PASS
because it is already known. Do not equate saved settings, a successful API
request or an open record tab with the corresponding UI/audio behavior.

## 2. Prepare once, then reset between calls

### People and equipment

- Caller: an approved test phone whose synthetic Dynamics contact is enabled.
- Operator: the approved workspace account, with a headset and browser microphone
  permission. Prefer separate caller/operator people to avoid acoustic feedback.
- AI-phase supervision can use the approved operator while the AI owns the call.
  Human-phase supervision needs a **different approved supervisor account**;
  a person cannot monitor a conversation assigned to that same account.
- Confirm recording/transcription consent, destination and international charges.
  Start a visible **90-second timer when dialing**, not when HUMAIN speaks.

### Test data and private worksheet

Use only authorized synthetic records. Record these privately:

| Label | Value to collect |
|---|---|
| `RUN_TAG` | Unique test label, for example `acceptance-<UTC-date>-<initials>` |
| `CUSTOMER_A` | Caller/chat profile and its expected CRM first name |
| `CASE_A` | One active case belonging to that customer, preferably the case created in C2 |
| `CASE_R` | One existing resolved case for the same customer |
| `CASE_B` | Another approved synthetic customer's case, for the access-isolation test only |
| Baseline | Current case statuses, latest approved customer-update notes and case count |
| Each voice test | UTC start/end, native conversation ID, selected case and screenshots |

Record counts before creating anything; do not keep expecting the original seed
count after a test legitimately adds a case. Case lists can be bounded to recent
records; a short list is not proof that older cases were deleted.

### Before each call

1. Finish the previous call **and its Dynamics wrap-up**. The conversation must
   reach Closed, not just disappear from the phone.
2. Reload the workspace; use a fresh voice session rather than a restored tab.
3. For V1, set the representative Available with a free inbound slot.
4. Have the test observer perform the [no-call checks](operations.md#no-call-checks).
   Voice, native observer, Dataverse and VoIP must be ready; Pause AI must be
   enabled/ready for V3. Shared CRM readiness must pass.
5. Agree this call's scope/cap, arm a **fresh** read-only monitor, and wait for
   `ARMED`. One call per monitor window; no overlapping native AI calls.
6. If a required control is absent or a call fails, capture the symptom and hang
   up. Do not spend paid minutes debugging or extend the cap to finish a checklist.

This document does not arm a call. The monitor cannot dial or hang up.
An explicitly agreed 120-second caller cap is separate from the configured
20-minute **active AI phase** safety deadline. If that AI deadline expires,
the pilot hands over even without a human request. Record
`pilot_duration_limit` separately from `customer_requested_human`.
The extended source limit requires the updated voice image and published
3,000-cycle controller. Earlier calls used a 120-second AI deadline; short-call
monitor runs do not establish full-duration acceptance of the extension.

After the 31 October silence regression, verify startup before continuing V3:
record phone answer/timer-start, first audible notice and first HUMAIN speech
separately. The current application-only prepared-start route must be verified
by its playback/cancellation callbacks, emitted native activity and caller sound.
A passing configuration check is not a measured answer-to-notice result.
The first regular-route native-only test on 1 November **failed notice
audibility**: HUMAIN connected in approximately 13 seconds, but no recording
notice was heard. The application-only correction preserves the parallel
preparation but changes the announcement owner. A subsequent authorized
startup-only call confirmed audible playback and a smooth transition, with
approximately six seconds before the notice. This does not pass all of V1-V3
or eliminate the initial delay. Confirm the application owner in status and
notice cancellation before AI audio release on each new deployment.
Stop and report another silent startup rather than repeatedly dialing.

The latency optimization and native-music trials have been reverted to the
accepted snapshot. The **5-7 second startup delay is work in progress and on
hold**. First deployment acceptance must disclose that limitation rather than
run the parked experiments or claim a one-to-two-second result. The ordinary
recording, HUMAIN, language, CRM and native handover checks still apply.

## 3. W1 - Customer forms and case visibility, without a call

1. Open `CUSTOMER_A` through the dedicated voice/case session's Customer tab.
   The scoped case-list fix is on that form; unrelated stock Contact forms were
   deliberately left unchanged.
2. Inspect **Customer > Related Cases**. Open an active case and `CASE_R`.
3. Compare titles, numbers, customer links, states and latest timeline notes with
   the worksheet. Check that application-owned cases are visible.
4. Open another approved synthetic customer in a separate session. Its related
   cases must not be replaced with `CUSTOMER_A`'s records.
5. Reopen the Customer tab after a refresh; repeat the case-list check.
6. Open a case through the scoped case route. Confirm District, Street,
   Landmark and Category are visible and match the actual case fields. These
   are the incident location, not the contact's postal address.

**PASS:** the contact-scoped **All Cases** view includes active/resolved records,
does not depend on the representative owning them, and excludes other customers.

**Important distinction:** Recent Cases/Related Cases list customer records.
Conversation **Case Details** displays the one associated case. The latter is
tested during V1, not proven by this list.

## 4. C1-C4 - Shared text-chat CRM checks, without a phone call

Use the existing customer chat UI, not representative Copilot on the right.
Select the same synthetic customer as the caller. Selecting a demo profile is
not production identity authentication.

### C1. Context and current reads

Send:

> Please speak English. What cases do I have? Keep the answer brief.

Then:

> Fetch the current status and most recent customer update for case [CASE_A].

**PASS:** correct customer/cases, real Dynamics case numbers and current approved
updates. No invented dispatch, approval or completion promise. A greeting uses
the CRM name where appropriate; it must not substitute another profile.

### C2. Confirmation and one real case

Send:

> Prepare a synthetic service request: [RUN_TAG], a test issue needs
> inspection in Test District, on Test Street near Test Landmark. Show the
> details, but do not save anything until I confirm.

- Check Dynamics: there must be **no new case yet**.
- Confirm once: **"Yes, record those details as a test case."**
- Save the returned Dynamics number as `CASE_A`.
- Refresh Dynamics and find exactly one matching new case linked to the customer.
- On the custom case form, check **Test District / Test Street / Test Landmark**
  and the selected category separately from the description.

**PASS:** no write before confirmation; one case afterward; correct location,
description and customer; real Dynamics number, not an `OP-` receipt.
No email, call, dispatch or case resolution is implied.

If the result is uncertain, check the operation/reference before resubmitting.
Do not treat paraphrasing the request as an idempotency test: exact operation-ID
replay is an engineering check, not an arbitrary repeated natural-language turn.

### C3. Follow-up and read-after-write

Send:

> Prepare a follow-up for [CASE_A]: "[RUN_TAG] - the test issue is still unresolved."
> Do not save it yet.

Check no note was added. Then confirm once and ask for the latest case update.

**PASS:** one customer-update note appears on the same case, is returned by a
fresh lookup, and the case state/priority/customer have not been changed.
This tests append-only notes, not automatic task creation or case resolution.

### C4. Language and profile isolation

- Ask to continue in Arabic; ask about the same case. Return to English.
  **PASS:** language changes without losing the customer/case context.
- Try switching profiles in that conversation through the supported UI.
  **PASS:** it requires a new conversation or refuses the switch; no silent
  replacement of the bound customer.
- Start a new chat for another approved profile and request `CASE_A`.
  **PASS:** no cross-customer case details are disclosed.
- Start a Guest chat. Public knowledge guidance may work; private cases and
  anonymous/substitute case creation must not.

Use authorized synthetic fixtures only. Do not probe arbitrary real customers.
Test a CRM outage only in controlled engineering validation; never break live
credentials to simulate one.

## 5. V1 - Personalized startup, fresh case read and ordinary handover

**One fresh call, maximum 90 seconds from dialing. No Join or Pause AI in V1.**

| Step | Caller/operator action | What to check |
|---|---|---|
| 1 | Call the Dynamics number from the approved phone | Recording-only Arabic/English notice; no initial representative acceptance |
| 2 | Listen to the first HUMAIN greeting | Correct CRM name and configured initial language; no overlap or clipped first word |
| 3 | "Fetch the latest status and most recent update for case [CASE_A]. One sentence, please." | Same new case/update created in chat; fresh read, not a stale startup guess |
| 4 | "Please connect me to a human." Say it once | Native notification appears; accept it normally |
| 5 | Inspect Customer Summary > Case Details | Correct customer **and that specific case**, not just an opened case tab |
| 6 | Caller: "Caller audio check." Operator: "Representative audio check." | Both directions clear; AI no longer answers |
| 7 | Capture the native parent ID and visible transcript, then hang up | Same conversation through transfer; distinct caller/AI/representative speakers |

The recording announcement is at **1.3x**, and may be interrupted when HUMAIN
is ready. Finishing the entire clip is not required. Measure answer-to-notice
and first-AI timing; **instant-on-answer is not established**.

Do not manually link a case before capturing the automatic Case Details result.
If it is empty, record the failure, selected reference, UTC time and screenshot.
The observer compares the actual case-tool receipt and before/after native
associations. Do not assume handover erased a link unless the timeline proves it.

**V1 pass requires actual Case Details and same-conversation human audio.**
The broader automatic selection/timeout reliability item remains open until its
regressions are also accepted, even if this explicit-reference path passes.

## 6. V2 - Arabic/English control and native transcription

**Separate fresh call, maximum 90 seconds. No Pause AI; no human transfer needed.**

1. Verify the configured initial greeting again.
2. Say: **"Please speak Arabic. Explain how you can help in one sentence."**
3. Confirm an Arabic answer. Then say:
   **"أريد المساعدة في طلب خدمة."**
4. Say: **"Please switch to English. Repeat that in one sentence."**
5. Confirm an English answer. Hang up within the cap.

**PASS:** native dialog/transcription language-change evidence, Arabic text for
the Arabic turn, English for the English turn, separate speakers, no duplicated
AI voice or cross-call identity leakage.

The representative's UI language is a different setting. Do not require automatic
language recognition after human handover or perfect recognition of a compound
code-switching phrase; those are not established capabilities.

## 7. V3 - Monitor, Join and explicit Pause AI

**Separate fresh call, maximum 90 seconds. This is not normal Transfer to agent.**

Keep the operator in the native **Ongoing Conversations** experience. Use the
approved account; do not assign the call to that same account merely to monitor it.

### A. Establish the baseline

1. Caller talks briefly with HUMAIN.
2. Operator selects the call and **Monitor**.
   - Operator hears the call; caller must not hear the monitoring operator.
   - Merely opening Monitor or the Pause AI tab must not mute the bot.
3. Confirm the new voice session contains **Pause AI**. If missing/unavailable,
   capture the screen and stop this test; do not substitute a different/latest
   call or edit template settings during the paid call.
4. Operator selects native **Join call**.
   - Caller and operator can hear one another.
   - Join itself does **not** pause HUMAIN.
   - With the caller microphone muted, operator speech should not become AI input.

### B. Pause while AI is speaking

5. Caller unmutes and says:

   > Explain the steps for submitting a service request in some detail.

6. Once HUMAIN is speaking, **mute the caller microphone again**. This prevents
   echo or caller interruption from masquerading as a successful supervisor pause.
7. Operator opens **Pause AI**, presses **Pause AI / check request**, and confirms.
   Keep the same native conversation focused.
8. Observe Requested, possibly Pausing, then:

   > AI paused — input, output and new tools are blocked.

Fast transitions can skip a visible intermediate message. **Requested/Pausing
are not success.** Failed, Rejected, Unconfirmed or Ended are not a new successful
pause either. Do not wait beyond the call cap for the UI's 45-second polling limit.

Record click-to-confirmation and click-to-last-AI-audio separately. There is no
claimed zero-millisecond/instant cancellation SLA; note any short network audio
tail rather than hiding it.

### C. Prove both directions are blocked

9. After confirmed Paused, caller unmutes and says:

   > Are you still there? Tell me the status of my case.

10. Caller and operator exchange two short sentences and observe for roughly
    ten seconds, if the call budget permits.

**PASS:**

- No continued/restarted HUMAIN answer and no new response to the caller.
- Caller/operator two-way audio continues.
- No new AI tools are started after the acknowledged pause.
- No new queue notification, transfer, disconnect or different native conversation.
- A previously admitted operation may finish; that must not trigger resumed speech
  or a duplicate write. Audit this separately if it was actually exercised.

If time permits, press **check request** again: it must check the same request,
not create another command or reactivate AI. Then hang up.

### D. What not to infer

- The AI participant may still be listed while muted. Disappearance is not the
  immediate pause criterion.
- Pause does not change the case, assign the operator or end the native call.
- There is no Resume button. Do not test normal handover by asking the muted AI
  afterward; use a fresh call.
- For a repeat V3, check that the new call starts normally and is not paused by
  a previous call's cached request.
- Operator leave/no-auto-resume can be checked in a separate short repeat:
  after confirmed pause, the operator leaves, caller asks once, AI stays silent,
  caller hangs up. Do not intentionally abandon the caller.

## 8. W2-W5 - Workspace checks after hanging up

Keep the session open briefly during wrap-up for summary/script inspection;
then close the actual Dynamics phone session and verify Closed. Do not keep
the PSTN call connected for these checks.

### W2. Scripts and all nine macros

Open **Agent scripts** in the appropriate voice or case session. English UI
should offer voice handover, case handling and wrap-up workflows. They are
15, 9 and 4 steps respectively; Arabic has separate counterparts.

| Macro | Expected result |
|---|---|
| Review linked customer | Correct contact form and its Related Cases |
| Open linked case | The conversation's actual associated case; no invented selection when absent |
| Find existing cases | Current customer's All Cases grid, not Owner: Me; opening a result alone must not claim association |
| New case form - review then save | Unsaved native form; closing it creates no case |
| Link confirmed saved case (live call only) | **Live conversation only**, correct saved focused case, explicit association |
| Follow-up task form for focused case | Unsaved task linked to the focused case; not an email or automatic save |
| Refresh saved case/tab | Refreshes the intended record, not another session; this is the visible label |
| Open native knowledge search | Opens Knowledge with Published and Internal/Both visibility for the internal knowledge articles |
| Refresh case-session context | Refreshes the current case context without overwriting another session |

**Exception:** test the explicit link macro during V1 only **after** recording
automatic linking's result and only if time permits. Otherwise use a separately
bounded session. A manual repair must not convert the automatic-link result to
PASS. Missing/unsaved/wrong contexts must not link a guessed or unrelated case.
Do not test this macro after hanging up or during wrap-up and interpret its
refusal as a failed link write.

### W3. Knowledge Q&A, not private case querying

In representative Copilot, ask a question that one approved, published article
answers, phrased as a customer would ask it.

Inspect the answer's citations and open the cited published article.

**PASS:** relevant approved guidance and supporting article/source
links; no invented submission, dispatch, case state or service promise.

Search Knowledge directly too. Verify the English article's Arabic translation
is available and published. The expected package is the approved set of
translation pairs, not every unrelated draft in the tenant.
Keep **Published** selected and inspect **Visibility: Internal or Both**.
Search for a term from that article's title. If the language filter is English,
switch that search filter deliberately to inspect the Arabic translation;
do not make internal articles public to get results.

As a boundary check, ask for a customer's latest case update in the Copilot pane.
**Do not expect this to work yet:** live-case query extensibility is separate.
It must not fabricate private facts. Use the actual case form/timeline or the
customer-facing CRM tools to establish the record's real status.

### W4. Actual summaries and persisted artifacts

1. Open **Activities > Conversations**, find this run's conversation and open
   **Summary**. This is the historical conversation summary, not Customer
   Summary in the live workspace.
2. Open `CASE_A` separately and inspect its case summary and timeline.
3. Use any captured join/handover summary as a separate observation; a saved
   end summary does not prove a join summary was shown.

Compare case, join/handover, on-demand and end summaries against source records.
Check issue, location, case number, actions already taken, unresolved work and
who said what.

- No invented resolution or claimed action that was only discussed.
- Inspect whether AI-phase content is actually included; an omitted hybrid-AI
  segment is a coverage gap, not silently accepted as a complete summary.
- Listen to native recordings, including the moment of Pause AI.
- Confirm post-pause caller/operator audio still exists in recording/transcript.
- Confirm speaker distinction and preservation after final closure.
- Case state must remain unchanged merely because the conversation closed.

Generation can be asynchronous and needs sufficient source content. If absent,
record whether pending, permission-blocked or unavailable; do not call it PASS
because a configuration switch is enabled. Investigate offline, not on another
paid call.
A very short new case without notes or associated activities can legitimately
have insufficient source content. The custom case location fields are not
automatically included in its summary mapping. Never add invented detail just
to make a summary appear.

### W5. Arabic representative UI

With no active call or wrap-up, deliberately switch the representative's personal
Dynamics UI language to Arabic and reload.

- Arabic script variants appear, with Arabic wrap-up redirects.
- Check actual Arabic labels/RTL visually; execute safe navigation macros again.
- If native knowledge/Copilot is selected, ask an Arabic question whose answer
  exists in the approved private content. Inspect the answer and its supporting
  citations. Do not invent a service question for an unconfigured package.
- Restore the preferred English UI afterward if desired.

The Pause AI control's own Arabic/English button changes its labels only; it is
not the representative-language setting or a caller voice-language switch.
Document deliberate UI changes: preserved-baseline setup verification can report
them as drift and must not silently overwrite the user's preference.

## 9. K1-K2 - Reproduce the remaining Case Details gaps

These are **regression/acceptance gates**, not promises from a configuration
readback. Bounded uncertain-write reconciliation now has automated and live
API verification; a caller-facing panel still needs its own observation.

| Test | Procedure and evidence |
|---|---|
| K1: prefetched selection | Optional fresh bounded call: "Tell me about my latest case", then request a human. Record the discussed case, whether a fresh lookup/selection tool ran, and actual Case Details. Empty association remains FAIL for the desired experience |
| K2: uncertain link write | Observer inspects any naturally occurring link timeout and reads the actual association afterward. Preserve original receipt and timing; do not assume timeout means no write, or assume a later link came from a particular fallback |

Do not deliberately throttle or break production Dataverse to trigger K2.
Fault-injection belongs in a controlled environment. A V1 explicit-reference
success does not close K1/K2, and manually opening/linking the case does not prove
automatic behavior.

## 10. Advanced tests - gated, not part of the three-call core run

| Test | Prerequisite | What must be proved |
|---|---|---|
| Human-phase supervisor join | Distinct approved representative and supervisor with entitlement/access | After ordinary AI handover, Monitor/private consultation/Join preserve privacy, three-way audio and the same conversation. Private messaging is not proof of audio whisper |
| Paused AI deadline | **Separate explicit approval** for a longer bounded call and charges | Cross the 20-minute AI-active deadline while paused; humans continue, no generic failure prompt, no automatic requeue/resume. The timer starts after setup, not dialing |
| Provider loss while paused | Controlled engineering window/staging, not arbitrary live restarts | Only AI resources are released; no renewed AI speech or unintended transfer |
| In-flight tool at pause | Approved synthetic write and observer correlation | Already-submitted write settles once; no new tools after pause, duplicate action or spoken follow-up |
| Wrong focus/stale request | Controlled UI/engineering test on authorized fixtures | No command for another focused conversation; closed/no-AI/expired/unauthorized commands are explicit rejections |
| Non-admin permissions | Approved non-admin account; do not remove the tester's admin roles | Cannot forge outcome fields or change/delete/reassign command requests; runtime cannot create commands. An admin account is not a valid FLS denial test |
| Network/retry recovery | Controlled browser/API fault simulation | Same request UUID reused; a confirmed missing row can retry once per click; unknown response cannot create replacement commands or fake Paused |
| Duplicate business writes | Authenticated engineering fixture with exact operation IDs | Same logical create/follow-up produces one record; conflicting payload rejected; no blind retry or Cosmos fallback |
| Multi-operator routing | Second individually approved eligible human, reviewed capacities/roles/license/policy and an applied plan | Native assignment follows the reviewed strategy/presence/capacity; busy/full operators are not unexpectedly selected. Use sequential AI calls, not unsupported AI concurrency |
| Setup reproducibility | Separate eligible test environment, budgets and admin authorization | Follow repository guides without private old-session implementation; plan/apply/verify/recovery/rollback and real identities work there |

For the deadline test, an explicitly agreed 180-second dialing cap may be needed
to cross the active-phase timer; still hang up at the agreed cap if setup leaves
insufficient time. **The ordinary 90-second tests cannot prove this behavior.**
No longer test is authorized merely by this checklist.

Do not run schema rollback, remove roles, force provider failure or rewrite old
call records in the working environment just to complete the matrix.

Still parked: numerical Call Metrics, friendly HUMAIN speaker naming, automatic
human-phase language recognition, outbound calling and optional later channels.
Inbound acceptance on another country's domestic number is a separate approved
number/provider test.

## 11. Observer notes and evidence

The test observer can use the existing read-only monitor for each approved call:

```bash
# Run only after fresh caller readiness, consent and the agreed cap.
# TEST_REPORT must be a NEW private filename for this one call.
python infra/scripts/watch_native_voice.py \
  --tenant-id "$TENANT_ID" --org-url "$ORG_URL" \
  --voice-origin "$VOICE_ORIGIN" \
  --workstream-id "$WORKSTREAM_ID" --queue-id "$QUEUE_ID" \
  --representative-id "$REPRESENTATIVE_ID" \
  --secrets-file "$WORK/control-keys.env" \
  --output "$TEST_REPORT" --case-links --caller-limit-seconds 90 --arm
```

The monitor's aggregate success criterion expects **ordinary handover and
representative acceptance**. V2 and V3 intentionally may not do that, so a
nonzero exit or `control_proof_observed=false` is not by itself their failure.
Inspect the test-specific evidence. The monitor is not a complete Pause AI
verifier; capture its command outcome and actual audio separately.

For V3, the observer correlates the exact conversation with the native command
row, `moacc_outcome`, `moacc_ackrevision`, `moacc_ackon`, `moacc_inflight`, and the
`supervisor_pause_requested` / `supervisor_pause_acknowledged` timing events.
Preserve failed/unconfirmed records; do not reset them to simulate success.

Record privately for every call:

- Test ID, UTC start/end, conversation ID and case reference.
- What you said, what you heard and what the UI showed.
- Pause click, last AI audio and acknowledgment times when applicable.
- Screenshots before manual fixes, especially Case Details and Pause status.
- Transcript/recording and source case/note comparison.
- Final Closed state and observer cleanup/readiness checks.

Do not share control keys, raw private controller exports or unredacted citizen
data in public issues. Keep failed attempts and later successes as separate runs.

## 12. Return this scorecard

```text
Run tag:
W1 Customer/Related Cases: PASS / FAIL / BLOCKED / NOT TESTED
C1 CRM reads:
C2 Confirmed case creation: case reference =
C3 Follow-up/read-after-write:
C4 Language/profile/guest isolation:
V1 Normal call/handover: conversation = ; Case Details =
V2 Arabic/English: conversation =
V3 Monitor/Join/Pause: conversation = ; UI result = ; audio stopped =
V3 caller trigger after Paused: AI replied YES / NO / NOT TESTED
V3 human two-way audio preserved:
W2 Scripts/macros: missing or failing action =
W3 Knowledge/citations:
W4 Summaries/recording/transcript:
W5 Arabic representative UI:
K1/K2 known case-link regressions:
Advanced tests actually exercised:
Errors and UTC timestamps:
```

Acceptance means **observed behavior**, not a large test count. Do not mark the
overall experience fully accepted while case linking, required generated content
or a mandatory access/audio check is failed, blocked or untested.
