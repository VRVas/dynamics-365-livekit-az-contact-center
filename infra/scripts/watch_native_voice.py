"""Arm a read-only monitor for one new native voice call. Never dials, joins or hangs up."""

import argparse
import base64
from collections import Counter
import json
import math
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from native_voice_setup import Dataverse, guid, organization, private_path, secret_value, voice_origin, write_private


IDENTITY_RETENTION_SECONDS = 900
IDENTITY_FOLLOWUP_SECONDS = IDENTITY_RETENTION_SECONDS + 60
METRIC_FIELDS = (
    "callDuration", "customerLongestMonologueInMs", "avgPauseBeforeSpeakingInMs",
    "switchCount", "talkingSpeedWordsPerMin", "talkToListenRatio",
)
CASE_LINK_FIELDS = ("_msdyn_customer_value", "_msdyn_issueid_value", "_regardingobjectid_value")


def observe_case_links(report, item, media=None):
    if not report.get("observe_case_links"):
        return
    if any(field not in item for field in CASE_LINK_FIELDS):
        raise ValueError("The native response omitted the requested customer/case association fields.")
    values = [guid(item[field]) if item[field] is not None else None for field in CASE_LINK_FIELDS]
    regarding_type = item.get("_regardingobjectid_value@Microsoft.Dynamics.CRM.lookuplogicalname") if values[2] is not None else None
    if values[2] is not None and (not isinstance(regarding_type, str) or not regarding_type):
        raise ValueError("The Regarding association has no record-type annotation.")
    snapshot = {
        "native_customer_id": values[0], "native_case_id": values[1],
        "regarding_record_id": values[2], "regarding_record_type": regarding_type,
        "native_statuscode": report.get("native_statuscode"),
        "media_phase": report.get("media_phase"),
        "representative_accepted": bool(report.get("representative_accepted_at")),
    }
    if media is not None and ("selected_case_id" in media or "selected_case_number" in media):
        identifier, number = media.get("selected_case_id"), media.get("selected_case_number")
        if not isinstance(identifier, str) or not isinstance(number, str) or len(number) > 80:
            raise ValueError("The adapter selected-case snapshot has an invalid shape.")
        if bool(identifier) != bool(number):
            raise ValueError("The adapter selected-case ID and number must be supplied together.")
        selected_id = guid(identifier) if identifier else None
        status = media.get("case_link_status")
        if status not in {"not_selected", "not_requested", "linked", "failed"}:
            raise ValueError("The adapter case-link receipt has an unknown status.")
        snapshot.update({
            "selected_case_id": selected_id, "selected_case_number": number,
            "adapter_link_status": status,
            "adapter_link_error": media.get("case_link_error", ""),
            "adapter_link_http_status": media.get("case_link_http_status", 0),
            "adapter_link_stage": media.get("case_link_stage", ""),
            "adapter_link_verified_at": media.get("case_link_verified_at", ""),
            "selected_case_matches_native": values[1] == selected_id if selected_id else None,
        })
    history = report.setdefault("case_link_timeline", [])
    if not history or any(history[-1].get(key) != value for key, value in snapshot.items()):
        if len(history) >= 32:
            raise ValueError("The bounded case-link transition limit was reached.")
        history.append({**snapshot, "observed_at": datetime.now(timezone.utc).isoformat()})


def observe_identity(report, media):
    mode = report.get("identity_mode")
    if mode is None:
        return
    expected = mode == "retained"
    if media.get("identity_retention_requested", False) is not expected:
        raise ValueError("The call's identity-retention mode does not match the planned diagnostic.")
    if not expected:
        report["immediate_cleanup_mode_checks"] = report.get("immediate_cleanup_mode_checks", 0) + 1
        return
    if (
        type(media.get("identity_retention_seconds")) is not int
        or media["identity_retention_seconds"] != IDENTITY_RETENTION_SECONDS
    ):
        raise ValueError("The retained-identity grace period differs from the reviewed 900 seconds.")
    state = media.get("identity_cleanup_status")
    if state not in {"not_allocated", "active", "retained", "deleting", "deleted", "failed"}:
        raise ValueError("The retained identity has an unknown cleanup state.")
    report["identity_cleanup_status"] = state
    states = report.setdefault("identity_states", [])
    if not states or states[-1]["state"] != state:
        if len(states) >= 16:
            raise ValueError("Too many identity-state transitions for one diagnostic call.")
        states.append({"state": state, "observed_at": datetime.now(timezone.utc).isoformat()})
    if state == "failed":
        raise ValueError("Identity cleanup failed; inspect the service before another diagnostic.")
    if report.get("native_statuscode") == 4 and state == "retained":
        report["identity_retained_at_native_closure"] = True


def observe(report, item, media, session):
    identifier = item["msdyn_ocliveworkitemid"]
    if report.get("conversation_id") not in (None, identifier):
        raise ValueError("More than one call appeared in the armed window; stop rather than guess.")
    report["conversation_id"] = identifier
    report["native_statuscode"] = item.get("statuscode")
    report["native_state"] = item.get("statuscode@OData.Community.Display.V1.FormattedValue")
    if media:
        if media.get("conversation_id") != identifier or media.get("audio_transport") != "voip":
            raise ValueError("The native media response belongs to another conversation or transport.")
        if media.get("control_language") not in {"ar", "en"} or type(media.get("control_revision")) is not int:
            raise ValueError("The call is not using the expected speech-turn controller.")
        observe_identity(report, media)
        if media.get("control_ready"):
            report.setdefault("languages", {})[str(media["control_revision"])] = media["control_language"]
        report["microphone_ready_seen"] |= media.get("ai_participant_media_ready") is True
        report["audio_frames_seen"] |= media.get("customer_audio_frames", 0) > 0 and media.get("ai_audio_frames", 0) > 0
        report["handover_ready_seen"] |= (
            media.get("phase") == "handover_ready" and media.get("action") == "handover"
            and media.get("azure_leg_detached") is True
        )
        report["media_phase"] = media.get("phase")
        report["media_reason"] = media.get("reason")
    if session:
        if session.get("_msdyn_liveworkitemid_value") != identifier:
            raise ValueError("The native session belongs to another conversation.")
        if item.get("_msdyn_activeagentid_value") == report["representative_id"] and session.get("msdyn_agentacceptedon"):
            report["representative_accepted_at"] = session["msdyn_agentacceptedon"]
    observe_case_links(report, item, media)


def read_native_metrics(api, conversation_id):
    identifier = guid(conversation_id)
    result = {"checked_at": datetime.now(timezone.utc).isoformat()}
    document = api.get("msdyn_conversationinsights", {
        "$select": "msdyn_conversationinsightid",
        "$filter": f"_msdyn_conversationid_value eq {identifier}",
        "$top": "2",
    })
    rows = document["value"]
    if (
        not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows)
        or len(rows) > 1 or document.get("@odata.nextLink")
    ):
        raise ValueError("The native insight record is ambiguous; no row was selected.")
    if not rows:
        return {**result, "status": "no_insight_record"}
    insight_id = guid(rows[0]["msdyn_conversationinsightid"])
    request = urllib.request.Request(
        f"{api.org}/api/data/v9.0/msdyn_conversationinsights({insight_id})/msdyn_insights/$value",
        headers=api.headers,
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            content = response.read(4 * 1024 * 1024 + 1)
            if response.status != 200:
                raise ValueError("Unexpected native metrics-file response status.")
    except urllib.error.HTTPError as error:
        if error.code != 404:
            raise
        return {**result, "status": "file_absent", "http_status": 404, "insight_id": insight_id}
    if len(content) > 4 * 1024 * 1024:
        raise ValueError("The native metrics file exceeds the diagnostic size limit.")
    data = json.loads(content)
    if not isinstance(data, dict) or not isinstance(data.get("insights"), dict):
        raise ValueError("The native metrics file has an unexpected schema.")
    metrics = {field: data["insights"].get(field) for field in METRIC_FIELDS}
    if any(type(value) not in (int, float) or not math.isfinite(value) for value in metrics.values()):
        raise ValueError("Native numerical metrics are missing, invalid or non-finite.")
    return {
        **result, "status": "file_available", "http_status": 200,
        "insight_id": insight_id, "metrics": metrics,
        "positive_analyzed_duration": metrics["callDuration"] > 0,
    }


def read_speech_role_tags(api, conversation_id):
    identifier = guid(conversation_id)
    document = api.get("msdyn_transcripts", {
        "$select": "msdyn_transcriptid",
        "$filter": f"_msdyn_liveworkitemidid_value eq {identifier}",
        "$top": "2",
    })
    rows = document["value"]
    if (
        not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows)
        or len(rows) > 1 or document.get("@odata.nextLink")
    ):
        raise ValueError("The saved native transcript is ambiguous.")
    if not rows:
        return {"status": "transcript_pending"}
    transcript_id = guid(rows[0]["msdyn_transcriptid"])
    document = api.get("annotations", {
        "$select": "documentbody",
        "$filter": f"_objectid_value eq {transcript_id} and filename eq 'Messages_file.txt'",
        "$top": "2",
    })
    rows = document["value"]
    if (
        not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows)
        or len(rows) > 1 or document.get("@odata.nextLink")
    ):
        raise ValueError("The original speech-message attachment is ambiguous.")
    if not rows:
        return {"status": "source_messages_pending"}
    if not isinstance(rows[0].get("documentbody"), str):
        raise ValueError("The source speech-message attachment has invalid content.")
    envelope = json.loads(base64.b64decode(rows[0]["documentbody"], validate=True).decode("utf-8-sig"))
    if (
        not isinstance(envelope, list) or len(envelope) != 1 or not isinstance(envelope[0], dict)
        or not isinstance(envelope[0].get("TranscriptMetadata"), dict)
    ):
        raise ValueError("The source speech-message attachment does not match this conversation.")
    owner = envelope[0]["TranscriptMetadata"].get("LiveWorkItemId")
    if not isinstance(owner, str) or owner.lower() != identifier or not isinstance(envelope[0].get("Content"), str):
        raise ValueError("The source speech-message attachment does not match this conversation.")
    messages = json.loads(envelope[0]["Content"])
    if not isinstance(messages, list) or any(not isinstance(message, dict) for message in messages):
        raise ValueError("The original speech-message list has an unexpected schema.")
    counts = Counter()
    for message in messages:
        tags = message.get("tags", "")
        if not isinstance(tags, str):
            raise ValueError("A native speech message has invalid tags.")
        if not tags.startswith("SpeechToText,"):
            continue
        source = message.get("fromUserId", "")
        if not isinstance(source, str):
            raise ValueError("A native speech message has an invalid source identifier.")
        kind = "acs" if source.startswith("8:acs:") else "phone" if re.fullmatch(r"\+?\d{7,15}", source) else "other"
        roles = tuple(token for token in tags.split(",") if re.fullmatch(r"From[A-Za-z]{1,30}", token))
        counts[kind, roles] += 1
    return {
        "status": "available" if counts else "no_speech_messages",
        "groups": [
            {"source_kind": kind, "role_tokens": list(roles), "messages": count}
            for (kind, roles), count in sorted(counts.items())
        ],
    }


def follow_identity_cleanup(report, api, media, output):
    identifier = guid(report["conversation_id"])
    report["call_window_status"] = report["status"]
    report["call_window_finished_at"] = datetime.now(timezone.utc).isoformat()
    report["identity_followup_status"] = "observing"
    report["identity_trial_valid"] = False
    deadline = time.monotonic() + IDENTITY_FOLLOWUP_SECONDS
    next_metrics = 0.0
    snapshots = report.setdefault("post_call_metrics", [])
    while time.monotonic() < deadline:
        item = api.get(f"msdyn_ocliveworkitems({identifier})", {
            "$select": "statuscode,_msdyn_liveworkstreamid_value,_msdyn_cdsqueueid_value",
        })
        if (
            item.get("_msdyn_liveworkstreamid_value") != report["workstream_id"]
            or item.get("_msdyn_cdsqueueid_value") not in (None, report["queue_id"])
        ):
            raise ValueError("The diagnostic call left its expected workstream or queue.")
        current = media(f"/dynamics/native/sessions/{identifier}", missing=True)
        if current is None:
            raise ValueError("The adapter session was lost before identity cleanup could be verified.")
        if current.get("conversation_id") != identifier:
            raise ValueError("Identity follow-up returned another conversation.")
        report["native_statuscode"] = item["statuscode"]
        report["native_state"] = item.get("statuscode@OData.Community.Display.V1.FormattedValue")
        observe_identity(report, current)
        state = report["identity_cleanup_status"]
        closed = item["statuscode"] == 4
        if closed and (time.monotonic() >= next_metrics or state == "deleted"):
            if len(snapshots) >= 20:
                raise ValueError("The bounded metric-check limit was reached.")
            snapshot = {
                **read_native_metrics(api, identifier),
                "identity_state_at_read": state,
            }
            if report.get("speech_role_tags", {}).get("status") != "available":
                report["speech_role_tags"] = {
                    **read_speech_role_tags(api, identifier),
                    "checked_at": datetime.now(timezone.utc).isoformat(),
                    "identity_state_at_read": state,
                }
            snapshots.append(snapshot)
            checkpoint = output.with_name(f"{output.stem}.snapshot-{len(snapshots):02d}{output.suffix}")
            write_private(checkpoint, json.dumps(report, indent=2) + "\n")
            next_metrics = time.monotonic() + 60
        if state == "deleted":
            timings = media(f"/dynamics/native/sessions/{identifier}/timings")
            if (
                timings.get("conversation_id") != identifier
                or not isinstance(timings.get("events"), list)
                or any(not isinstance(event, dict) for event in timings["events"])
            ):
                raise ValueError("Identity cleanup timing evidence is missing or belongs to another call.")
            report["identity_timing_events"] = [
                event for event in timings["events"]
                if event.get("stage") in {
                    "voip_identity_revoke", "voip_identity_retained", "voip_identity_delete",
                    "voip_release", "session_released",
                }
            ]
            report["identity_timing_origin_utc"] = timings.get("origin_utc")
            stages = {
                event["stage"] for event in report["identity_timing_events"] if event.get("status") == "ok"
            }
            retained = [event for event in report["identity_timing_events"] if event["stage"] == "voip_identity_retained"]
            deleted = [
                event for event in report["identity_timing_events"]
                if event["stage"] == "voip_identity_delete" and event.get("status") == "ok"
            ]
            if len(retained) == len(deleted) == 1:
                values = (deleted[0].get("elapsed_ms"), deleted[0].get("duration_ms"), retained[0].get("elapsed_ms"))
                if any(type(value) not in (int, float) or not math.isfinite(value) or value < 0 for value in values):
                    raise ValueError("Identity timing evidence contains an invalid duration.")
                report["observed_identity_grace_ms"] = round(
                    deleted[0]["elapsed_ms"] - deleted[0]["duration_ms"] - retained[0]["elapsed_ms"], 3,
                )
            report["identity_followup_status"] = "deletion_confirmed"
            report["identity_trial_valid"] = (
                closed and report.get("identity_retained_at_native_closure") is True
                and {"voip_identity_revoke", "voip_identity_delete"}.issubset(stages)
                and report.get("observed_identity_grace_ms", 0) >= IDENTITY_RETENTION_SECONDS * 1000
                and not any(
                    event.get("status") in {"error", "cancelled"} for event in report["identity_timing_events"]
                )
                and report.get("handover_ready_seen") is True
                and report.get("microphone_ready_seen") is True
                and report.get("audio_frames_seen") is True
                and bool(report.get("representative_accepted_at"))
            )
            return
        if closed and state in {"not_allocated", "active"} and current.get("phase") in {"stopped", "handover_ready"}:
            raise ValueError("The call ended without entering the expected retained-identity state.")
        time.sleep(10)
    raise ValueError("Identity deletion was not confirmed within the bounded follow-up window.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tenant-id", required=True, type=guid)
    parser.add_argument("--org-url", required=True, type=organization)
    parser.add_argument("--voice-origin", required=True, type=voice_origin)
    parser.add_argument("--workstream-id", required=True, type=guid)
    parser.add_argument("--queue-id", required=True, type=guid)
    parser.add_argument("--representative-id", required=True, type=guid)
    parser.add_argument("--secrets-file", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--caller-limit-seconds", type=int, choices=(60, 90, 120, 150, 180), default=90)
    parser.add_argument("--dial-window-seconds", type=int, choices=(210, 600), default=210)
    parser.add_argument("--identity-mode", choices=("immediate", "retained"))
    parser.add_argument("--follow-identity-cleanup", action="store_true")
    parser.add_argument("--case-links", action="store_true", help="Record native customer/case/Regarding associations before and after acceptance.")
    parser.add_argument("--arm", action="store_true")
    args = parser.parse_args()
    if args.follow_identity_cleanup and args.identity_mode != "retained":
        parser.error("--follow-identity-cleanup requires --identity-mode retained.")
    output = private_path(args.output)
    checkpoint = output.with_name(output.stem + ".call-window.json")
    if output.exists() or (args.follow_identity_cleanup and checkpoint.exists()):
        raise ValueError("Use a new report filename; previous evidence is never overwritten.")
    if not args.arm:
        print("Dry run. Add --arm immediately before the agreed caller test. No calls or mutations.")
        return
    api = Dataverse(args.org_url, args.tenant_id)
    if args.case_links:
        api.headers["Prefer"] = 'odata.include-annotations="OData.Community.Display.V1.FormattedValue,Microsoft.Dynamics.CRM.lookuplogicalname"'
    key = secret_value(args.secrets_file, "DYNAMICS_NATIVE_MEDIA_API_KEY")

    def media(path, missing=False):
        request = urllib.request.Request(args.voice_origin + path, headers={"X-API-Key": key, "Accept": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=40 if path.endswith("/readiness") else 8) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            if missing and error.code == 404:
                return None
            raise

    readiness = media("/dynamics/native/readiness")
    if not all(readiness.get(key) is True for key in ("enabled", "observer_enabled", "dataverse_connected", "voip_gateway_ready")) or readiness.get("transport") != "voip":
        raise ValueError("The voice pilot is not ready; no monitor armed.")
    idle = api.rows("msdyn_ocliveworkitems", {
        "$select": "msdyn_ocliveworkitemid", "$top": "2",
        "$filter": f"_msdyn_liveworkstreamid_value eq {args.workstream_id} and statuscode ne 4",
    })
    if idle:
        raise ValueError("Close any prior call and its Dynamics wrap-up session before arming.")
    now = datetime.now(timezone.utc)
    report = {
        "armed_at": now.isoformat(), "representative_id": args.representative_id,
        "workstream_id": args.workstream_id, "queue_id": args.queue_id,
        "caller_hangup_limit_seconds": args.caller_limit_seconds,
        "dialing_window_seconds": args.dial_window_seconds,
        "identity_mode": args.identity_mode,
        "observe_case_links": args.case_links,
        "microphone_ready_seen": False, "audio_frames_seen": False, "handover_ready_seen": False,
        "control_operations": 0, "live_ui_and_audio_confirmation": "required_separately",
    }
    print(json.dumps({
        "state": "ARMED", "window_seconds": args.dial_window_seconds,
        "caller_hangup_limit_seconds": args.caller_limit_seconds,
        "identity_mode": args.identity_mode,
    }), flush=True)
    deadline = time.monotonic() + args.dial_window_seconds
    last = None
    select = "msdyn_ocliveworkitemid,createdon,statuscode,_msdyn_cdsqueueid_value,_msdyn_lastsessionid_value,_msdyn_activeagentid_value"
    if args.case_links:
        select += "," + ",".join(CASE_LINK_FIELDS)
    try:
        while time.monotonic() < deadline:
            items = api.rows("msdyn_ocliveworkitems", {
                "$select": select,
                "$filter": f"_msdyn_liveworkstreamid_value eq {args.workstream_id} and createdon ge {now.isoformat().replace('+00:00', 'Z')}",
                "$top": "2",
            })
            if len(items) > 1:
                raise ValueError("Multiple calls appeared. Stop and re-arm a new window; do not guess.")
            if not items:
                time.sleep(2)
                continue
            item = items[0]
            if item.get("_msdyn_cdsqueueid_value") not in (None, args.queue_id):
                raise ValueError("The call left the intended native queue.")
            identifier = guid(item["msdyn_ocliveworkitemid"])
            current = media(f"/dynamics/native/sessions/{identifier}", missing=True)
            session_id = item.get("_msdyn_lastsessionid_value")
            session = api.get(f"msdyn_ocsessions({guid(session_id)})", {
                "$select": "_msdyn_liveworkitemid_value,msdyn_agentacceptedon",
            }) if session_id else None
            if report.get("conversation_id") is None:
                deadline = time.monotonic() + args.caller_limit_seconds + 5
            observe(report, item, current, session)
            marker = (report.get("native_state"), report.get("media_phase"), report.get("media_reason"), report.get("representative_accepted_at"))
            if marker != last:
                print(json.dumps({"at": datetime.now(timezone.utc).isoformat(), "state": marker}), flush=True)
                last = marker
            age = (datetime.now(timezone.utc) - datetime.fromisoformat(item["createdon"].replace("Z", "+00:00"))).total_seconds()
            if report["native_statuscode"] == 4 or age >= args.caller_limit_seconds + 5:
                report["status"] = "call_closed" if report["native_statuscode"] == 4 else "observation_limit_reached"
                break
            time.sleep(2)
        else:
            report["status"] = "armed_window_expired"
        if args.follow_identity_cleanup and report.get("conversation_id"):
            write_private(checkpoint, json.dumps(report, indent=2) + "\n")
            print(json.dumps({
                "state": "CALL_WINDOW_FINISHED", "status": report["status"],
                "native_state": report.get("native_state"),
                "next": "Read-only identity cleanup follow-up; caller limit is unchanged.",
            }), flush=True)
            follow_identity_cleanup(report, api, media, output)
    except (ValueError, KeyError, RuntimeError, urllib.error.URLError, TimeoutError) as error:
        report["status"] = "monitor_stopped"
        report["error"] = str(error) if isinstance(error, (ValueError, RuntimeError)) else type(error).__name__
    report["control_proof_observed"] = all(report[key] for key in (
        "microphone_ready_seen", "audio_frames_seen", "handover_ready_seen",
    )) and bool(report.get("representative_accepted_at"))
    report["finished_at"] = datetime.now(timezone.utc).isoformat()
    write_private(output, json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
    success = report["control_proof_observed"] and report["status"] != "monitor_stopped"
    if args.follow_identity_cleanup:
        success = success and report.get("identity_trial_valid") is True
    raise SystemExit(0 if success else 1)


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError) as error:
        print(json.dumps({"status": "not_armed", "error": str(error) if isinstance(error, ValueError) else type(error).__name__}))
        raise SystemExit(1)
