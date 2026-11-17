"""Opt-in, bounded HUMAIN media pilot controlled by a retained Copilot voice topic."""

import asyncio
import hmac
import json
import logging
import os
import re
import secrets
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

from aiohttp import ClientError, ClientSession, ClientTimeout, DummyCookieJar, web
from aiohttp.web_log import AccessLogger
from azure.communication.callautomation import (
    AudioFormat,
    CallConnectionState,
    CommunicationUserIdentifier,
    MediaStreamingAudioChannelType,
    MediaStreamingContentType,
    MediaStreamingOptions,
    PhoneNumberIdentifier,
    StreamingTransportType,
)
from azure.communication.callautomation.aio import CallAutomationClient, CallConnectionClient
from azure.core.exceptions import AzureError, HttpResponseError

from backend.dataverse import DataverseClient, DataverseError
from backend.crm_context import CRMContextError, CustomerContext, apply_customer_context, empty_case_snapshot
from backend.humain import HumainConfig, HumainError
from backend.latency import LatencyTrace
from backend.native_call_probe import NativeCallProbe
from backend.native_notice import NativeNoticeError, NativeStartupNotice, notice_asset
from backend.native_voip import IDENTITY_RETENTION_SECONDS, NativeVoipError, NativeVoipGateway, NativeVoipSession
from backend.native_supervision import CONTRACT, FRESH_SECONDS, NativeCommandConsumer, SupervisionConfig
from backend.rtmt import RTMiddleTier, SpeakerRole
from backend.tools.tools import CURRENT_SESSION, Tool, ToolResult, ToolResultDirection

logger = logging.getLogger("native_media")
PREFIX = "/dynamics/native"
PILOT_SECONDS = 1200
SETUP_SECONDS = 30
CONTEXT_SECONDS = 10
CONTEXT_RESPONSE_SECONDS = CONTEXT_SECONDS + 2
ACS_CONNECT_SECONDS = 10
WAIT_SECONDS = 20
MAX_REQUEST_BYTES = 2048
LANGUAGE_ACK_SECONDS = 8
STARTUP_RESPONSE_SECONDS = SETUP_SECONDS - LANGUAGE_ACK_SECONDS
CRM_CONTEXT_SECONDS = 8


class NativeMediaError(RuntimeError):
    def __init__(self, code: str, message: str, status: int = 409):
        super().__init__(message)
        self.code = code
        self.status = status


@dataclass
class NativeSpeechControl:
    language: str
    revision: int = 0
    acknowledged_revision: int = -1
    changed: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    last_request_item_id: str | None = field(default=None, repr=False)

    @property
    def ready(self):
        return self.acknowledged_revision == self.revision

    def acknowledge(self, language, revision):
        if (
            not isinstance(language, str) or language not in {"ar", "en"}
            or not isinstance(revision, int) or isinstance(revision, bool) or revision < 0
        ):
            raise NativeMediaError("invalid_speech_turn", "Provide a supported language and a non-negative integer revision.", 400)
        if revision > self.revision or (revision == self.revision and language != self.language):
            raise NativeMediaError("speech_turn_mismatch", "The listening turn does not match the requested language revision.")
        # A turn already in flight can finish after a language request. It cannot acknowledge the new revision.
        if revision == self.revision:
            self.acknowledged_revision = revision
            self.changed.set()


def _id(value: Any, field_name: str = "id") -> str:
    if not isinstance(value, str) or not re.fullmatch(
        r"[0-9a-fA-F]{8}(-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", value
    ):
        raise NativeMediaError(f"invalid_{field_name}", f"A valid {field_name} UUID is required.", 400)
    identifier = UUID(value)
    if identifier.int == 0:
        raise NativeMediaError(f"invalid_{field_name}", f"An empty UUID cannot identify {field_name}.", 400)
    return str(identifier)


@dataclass(frozen=True)
class NativeMediaConfig:
    enabled: bool = False
    workstream_id: str = ""
    api_key: str = field(default="", repr=False)
    acs_connection_string: str = field(default="", repr=False)
    public_url: str = ""
    transport: str = "callautomation"
    fast_start: bool = False
    additional_workstream_ids: tuple[str, ...] = ()
    supervision: SupervisionConfig = field(default_factory=SupervisionConfig)

    def __post_init__(self):
        if self.transport not in {"callautomation", "voip"}:
            raise ValueError("DYNAMICS_NATIVE_MEDIA_TRANSPORT must be callautomation or voip.")
        if not isinstance(self.fast_start, bool) or (self.fast_start and self.transport != "voip"):
            raise ValueError("Native fast start requires the VoIP transport and a boolean setting.")
        if self.workstream_id:
            _id(self.workstream_id)
        if not isinstance(self.additional_workstream_ids, tuple):
            raise ValueError("Additional native workstreams must be a tuple of UUIDs.")
        if self.additional_workstream_ids and not self.workstream_id:
            raise ValueError("Configure the primary native workstream before adding another entry.")
        if len(set(self.workstream_ids)) != len(self.workstream_ids):
            raise ValueError("Native workstream IDs must be distinct.")
        if self.api_key and (len(self.api_key) < 32 or "\r" in self.api_key or "\n" in self.api_key):
            raise ValueError("DYNAMICS_NATIVE_MEDIA_API_KEY needs at least 32 characters and no line breaks.")
        if self.public_url:
            url = urlsplit(self.public_url)
            if (
                url.scheme != "https" or not url.hostname or url.username or url.password
                or url.path or url.query or url.fragment
            ):
                raise ValueError("Native media requires the HTTPS voice service origin without a path.")
        if self.enabled and not self.configured:
            raise ValueError("Native media needs a workstream, private API key, native ACS connection and callback origin.")
        if self.supervision.enabled and (not self.enabled or self.transport != "voip"):
            raise ValueError("Native supervision requires an enabled native VoIP pilot.")

    @property
    def configured(self):
        return bool(self.workstream_id and self.api_key and self.acs_connection_string and self.public_url)

    @property
    def workstream_ids(self):
        return tuple(_id(value) for value in (self.workstream_id, *self.additional_workstream_ids)) if self.workstream_id else ()

    @classmethod
    def from_env(cls):
        enabled = os.environ.get("DYNAMICS_NATIVE_MEDIA_ENABLED", "false").lower()
        if enabled not in {"true", "false"}:
            raise ValueError("DYNAMICS_NATIVE_MEDIA_ENABLED must be true or false.")
        if enabled == "true" and os.environ.get("ACS_AUTO_CONFERENCE_ENABLED", "true").lower() != "false":
            raise ValueError("Disable ACS_AUTO_CONFERENCE_ENABLED before enabling native media.")
        callback = os.environ.get("ACS_CALLBACK_PATH", "")
        fast_start = os.environ.get("DYNAMICS_NATIVE_MEDIA_FAST_START", "false").lower()
        if fast_start not in {"true", "false"}:
            raise ValueError("DYNAMICS_NATIVE_MEDIA_FAST_START must be true or false.")
        additional = os.environ.get("DYNAMICS_NATIVE_MEDIA_ADDITIONAL_WORKSTREAM_IDS", "").strip()
        origin = urlsplit(callback)
        return cls(
            enabled=enabled == "true",
            workstream_id=os.environ.get("DYNAMICS_NATIVE_MEDIA_WORKSTREAM_ID", "").strip().lower(),
            api_key=os.environ.get("DYNAMICS_NATIVE_MEDIA_API_KEY", ""),
            acs_connection_string=os.environ.get("DYNAMICS_NATIVE_MEDIA_ACS_CONNECTION_STRING", ""),
            public_url=f"{origin.scheme}://{origin.netloc}" if callback else "",
            transport=os.environ.get("DYNAMICS_NATIVE_MEDIA_TRANSPORT", "callautomation").lower(),
            fast_start=fast_start == "true",
            additional_workstream_ids=tuple(value.strip().lower() for value in additional.split(",")) if additional else (),
            supervision=SupervisionConfig.from_env(),
        )


@dataclass
class NativeMediaSession:
    conversation_id: str
    language: str
    effective_initial_language: str = field(init=False)
    capability: str = field(default_factory=lambda: secrets.token_urlsafe(32), repr=False)
    phase: str = "starting"
    reason: str | None = None
    action: str = "wait"
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    done: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    release: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    bound: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    context_ready: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    startup_prepared: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    runner: asyncio.Task | None = field(default=None, repr=False)
    media_task: asyncio.Task | None = field(default=None, repr=False)
    ws: web.WebSocketResponse | None = field(default=None, repr=False)
    voice: RTMiddleTier | None = field(default=None, repr=False)
    connection: CallConnectionClient | None = field(default=None, repr=False)
    connection_id: str | None = None
    controller_session_id: str | None = None
    correlation_id: str | None = None
    server_call_id: str | None = field(default=None, repr=False)
    connect_requested: bool = False
    detached: bool = False
    voip: NativeVoipSession | None = field(default=None, repr=False)
    transport: str = "callautomation"
    speech_control: NativeSpeechControl | None = field(default=None, repr=False)
    latency: LatencyTrace = field(init=False, repr=False)
    retain_identity_for_metrics: bool = False
    play_startup_notice: bool = False
    native_startup_notice: bool = False
    prepare_startup: bool = False
    notice: NativeStartupNotice | None = field(default=None, repr=False)
    supervision_contract: int = 0
    intervention_latched: bool = False
    ai_state: str = "active"
    pause_acknowledged: bool = False
    pause_revision: int = 0
    control_lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)
    provider_lost: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    detach_attempted: bool = False
    was_active: bool = False
    failure_notice_suppressed: bool = False

    def __post_init__(self):
        self.effective_initial_language = self.language
        self.latency = LatencyTrace(self.conversation_id)
        self.latency.mark("session_created", transport=self.transport)

    @property
    def prepared_startup_requested(self):
        return self.native_startup_notice or self.prepare_startup

    def snapshot(self, transcript_after: int | None = None):
        result = {
            "conversation_id": self.conversation_id,
            "phase": self.phase,
            "action": self.action,
            "reason": self.reason or "",
            "azure_leg_detached": self.detached,
            "provider_ready": self.voice is not None and self.voice.ready,
            "customer_audio_frames": self.voice.input_audio_frames if self.voice else 0,
            "customer_transcript_count": sum(
                segment.speaker_role == SpeakerRole.CUSTOMER for segment in self.voice.transcript_buffer.segments
            ) if self.voice else 0,
            "ai_audio_frames": self.voice.output_audio_frames if self.voice else 0,
            "human_acceptance": "not_observed_by_media_adapter",
            "pilot_limit_seconds": PILOT_SECONDS,
            "audio_transport": self.transport,
            "ai_participant_media_ready": self.voip is not None and self.voip.authorized and not self.voip.closing,
            "native_speaker_label": "resolved_by_dynamics_not_by_this_adapter",
            "control_language": self.speech_control.language if self.speech_control else "",
            "control_revision": self.speech_control.revision if self.speech_control else 0,
            "control_ready": self.speech_control is not None and self.speech_control.ready,
            "utterance_sequence": 0,
            "utterance_id": "",
            "utterance_text": "",
            "utterance_timestamp": "",
            "supervision_contract": self.supervision_contract,
            "ai_state": self.ai_state,
            "ai_pause_acknowledged": self.pause_acknowledged,
            "native_startup_prepared": self.startup_prepared.is_set(),
            "startup_notice_owner": "native" if self.native_startup_notice else "application" if self.play_startup_notice else "none",
            "startup_failed_unpaused": (
                self.done.is_set() and self.phase == "stopped" and self.action == "stop" and self.detached
                and not self.was_active and not self.failure_notice_suppressed
                and not self.intervention_latched and not self.pause_acknowledged
                and self.ai_state == "active" and bool(self.reason)
                and self.reason not in {
                    "control_requested_stop", "service_shutdown", "native_call_not_active",
                    "human_already_assigned", "controller_changed", "controller_not_retained",
                }
            ),
        }
        selected = getattr(self.voice, "selected_case", None)
        result.update(
            selected.snapshot() if selected is not None and selected.native_conversation_id == self.conversation_id
            else empty_case_snapshot()
        )
        if self.retain_identity_for_metrics:
            result.update({
                "identity_retention_requested": True,
                "identity_retention_seconds": IDENTITY_RETENTION_SECONDS,
                "identity_cleanup_status": self.voip.identity_cleanup_status if self.voip else "not_allocated",
            })
        if self.play_startup_notice:
            result.update({
                "startup_notice_requested": True,
                "startup_notice_state": self.notice.state if self.notice else "not_started",
            })
        if transcript_after is None or self.intervention_latched:
            return result
        turns = [
            segment for segment in self.voice.transcript_buffer.segments
            if segment.speaker_role == SpeakerRole.AI_AGENT
        ] if self.voice is not None else []
        if not 0 <= transcript_after <= len(turns):
            raise NativeMediaError("invalid_transcript_cursor", "The transcript cursor is outside this native session.", 400)
        result["utterance_sequence"] = transcript_after
        if transcript_after < len(turns):
            turn = turns[transcript_after]
            if len(turn.text.encode("utf-8")) > 16 * 1024:
                raise NativeMediaError("transcript_too_large", "The provider utterance exceeds the native transcript delivery limit.", 413)
            stamp = turn.timestamp
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=timezone.utc)
            result.update({
                "utterance_sequence": transcript_after + 1,
                "utterance_id": turn.item_id,
                "utterance_text": turn.text,
                "utterance_timestamp": stamp.astimezone(timezone.utc).isoformat(),
            })
        return result


class NativeMediaAccessLogger(AccessLogger):
    def log(self, request, response, time):
        if request.path.startswith(f"{PREFIX}/sessions/") and request.path.endswith(("/media", "/notice-events")):
            endpoint = request.path.rsplit("/", 1)[-1]
            self.logger.info(
                '%s "%s /dynamics/native/sessions/[redacted]/%s" %s %.3fs',
                request.remote, request.method, endpoint, response.status, time,
            )
        else:
            super().log(request, response, time)


class NativeMediaPilot:
    def __init__(
        self,
        config: NativeMediaConfig,
        dataverse: DataverseClient,
        observer: NativeCallProbe,
        session_factory: Callable[[str, str], RTMiddleTier],
        *,
        customer_context_provider: Callable[[RTMiddleTier], Awaitable[CustomerContext]] | None = None,
    ):
        self.config = config
        self.dataverse = dataverse
        self.observer = observer
        self.session_factory = session_factory
        self.customer_context_provider = customer_context_provider
        self.sessions: dict[str, NativeMediaSession] = {}
        self._automation: CallAutomationClient | None = None
        self._closing = False
        self._gateway = NativeVoipGateway(
            config.acs_connection_string, prewarm=config.fast_start,
        ) if config.transport == "voip" else None
        self._warmup_task: asyncio.Task | None = None
        self._token_http: ClientSession | None = None
        self.supervision = NativeCommandConsumer(
            config.supervision, dataverse, self.pause_command, lambda: self.sessions,
        )
        if config.enabled and not dataverse.org_url:
            raise ValueError("Native media requires DYNAMICS_ORG_URL.")

    def health(self):
        return {
            "configured": self.config.configured,
            "enabled": self.config.enabled,
            "observer_enabled": self.observer.enabled,
            "observer_permanent": self.observer.permanent,
            "pilot_limit_seconds": PILOT_SECONDS,
            "transport": self.config.transport,
            "voip_gateway_ready": self._gateway is not None and self._gateway.ready,
            "fast_start": self.config.fast_start,
            "warm_page_ready": self._gateway is not None and self._gateway.warm_page_ready,
            "retained_identity_count": self._gateway.retained_identity_count if self._gateway else 0,
            "identity_cleanup_failures": self._gateway.identity_cleanup_failures if self._gateway else 0,
            "startup_notice_supported": True,
            "startup_notice_context_wait_supported": True,
            "native_startup_notice_supported": True,
            "application_startup_preparation_supported": True,
            "startup_failure_notice_supported": True,
            "crm_context_prefetch_enabled": self.customer_context_provider is not None,
            "crm_context_timeout_seconds": CRM_CONTEXT_SECONDS,
            "crm_case_context_supported": True,
            "supervision_supported": True,
            "supervision_enabled": self.config.supervision.enabled,
            "supervision_ready": self.supervision.ready,
            "supervision_contract": CONTRACT,
        }

    async def start_supervision(self):
        await self.supervision.start()

    async def pause_command(self, conversation_id, command_id, created):
        session = self.sessions.get(conversation_id)
        if session is None:
            return {"outcome": "rejected", "reason": "no_native_ai_session"}
        async with session.control_lock:
            if (
                not self.config.supervision.enabled or not self.supervision.ready
                or session.supervision_contract != CONTRACT
            ):
                return {"outcome": "rejected", "reason": "supervision_not_ready"}
            if session.done.is_set() or session.release.is_set() or created < session.created_at:
                return {"outcome": "rejected", "reason": "inactive_or_stale_session"}
            if session.pause_acknowledged:
                return {
                    "outcome": "paused", "revision": session.pause_revision,
                    "inflight": session.voice.inflight_tools if session.voice else 0,
                }
            if session.intervention_latched:
                return {"outcome": "failed", "reason": "playback_unconfirmed"}
            if session.phase != "active" or session.voice is None or session.voip is None:
                return {"outcome": "rejected", "reason": "no_ready_native_ai"}
            try:
                item = await self._conversation(conversation_id)
                workstream = await self._workstream(item.get("_msdyn_liveworkstreamid_value"))
                self._check_owner(item, workstream)
                await self._check_controller(session)
            except (NativeMediaError, DataverseError):
                return {"outcome": "rejected", "reason": "native_context_unconfirmed"}
            if session.done.is_set() or session.release.is_set() or session.phase != "active" or session.voice.transfer_active:
                return {"outcome": "rejected", "reason": "handover_or_cleanup_started"}
            if datetime.now(timezone.utc) - created > timedelta(seconds=FRESH_SECONDS):
                return {"outcome": "rejected", "reason": "expired"}
            session.intervention_latched = True
            session.ai_state = "pausing"
            session.action = "supervised"
            session.pause_revision += 1
            session.latency.mark("supervisor_pause_requested")
            try:
                await session.voice.pause_audio(lambda: session.voip.pause(command_id, session.pause_revision))
            except (NativeVoipError, TimeoutError, RuntimeError, ConnectionError):
                session.ai_state = "pause_failed"
                session.provider_lost.set()
                return {"outcome": "failed", "reason": "playback_unconfirmed"}
            session.ai_state = "paused"
            session.pause_acknowledged = True
            session.latency.mark("supervisor_pause_acknowledged")
            return {"outcome": "paused", "revision": session.pause_revision, "inflight": session.voice.inflight_tools}

    async def warmup(self):
        if self.config.enabled and self.config.fast_start and self._gateway is not None and not self._closing:
            await self._gateway.warm()

    def _schedule_warmup(self):
        if not self.config.fast_start or self._closing or (self._warmup_task is not None and not self._warmup_task.done()):
            return
        self._warmup_task = asyncio.create_task(self.warmup())

        def completed(task):
            if not task.cancelled() and task.exception() is not None:
                logger.error("Native transport warmup failed type=%s", type(task.exception()).__name__)

        self._warmup_task.add_done_callback(completed)

    def _authorize(self, request):
        if not self.config.api_key:
            raise NativeMediaError("not_configured", "Native media pilot is not configured.", 503)
        supplied = request.headers.get("X-API-Key", "")
        if not hmac.compare_digest(supplied.encode(), self.config.api_key.encode()):
            raise NativeMediaError("unauthorized", "A valid native media API key is required.", 401)

    async def _workstream(self, identifier=None):
        if not self.config.workstream_id:
            raise NativeMediaError("workstream_not_configured", "Select the native media pilot workstream.", 503)
        identifier = self.config.workstream_ids[0] if identifier is None else _id(identifier)
        if identifier not in self.config.workstream_ids:
            raise NativeMediaError("native_call_not_active", "The selected conversation is not in an allowed native workstream.")
        workstream = await self.dataverse.get(
            f"msdyn_liveworkstreams({identifier})",
            params={"$select": "msdyn_liveworkstreamid,msdyn_name,msdyn_direction,msdyn_streamsource,_msdyn_bot_user_value,_msdyn_defaultqueue_value,statecode"},
        )
        if (
            workstream.get("msdyn_liveworkstreamid") != identifier
            or workstream.get("statecode") != 0
            or workstream.get("msdyn_direction") != 0
            or workstream.get("msdyn_streamsource") != 192440000
            or not workstream.get("_msdyn_bot_user_value")
            or not workstream.get("_msdyn_defaultqueue_value")
        ):
            raise NativeMediaError("invalid_workstream", "Native pilot requires an active inbound voice workstream, AI agent and fallback queue.")
        return workstream

    async def _conversation(self, identifier):
        return await self.dataverse.get(
            f"msdyn_ocliveworkitems({identifier})",
            params={"$select": "msdyn_ocliveworkitemid,statuscode,createdon,_msdyn_liveworkstreamid_value,_msdyn_cdsqueueid_value,_msdyn_activeagentid_value"},
        )

    def _check_owner(self, item, workstream):
        if (
            item.get("_msdyn_liveworkstreamid_value") != workstream.get("msdyn_liveworkstreamid")
            or item.get("_msdyn_liveworkstreamid_value") not in self.config.workstream_ids
            or item.get("_msdyn_cdsqueueid_value") != workstream["_msdyn_defaultqueue_value"]
            or item.get("statuscode@OData.Community.Display.V1.FormattedValue") != "Active"
        ):
            raise NativeMediaError("native_call_not_active", "The selected conversation is not active in the pilot workstream and queue.")
        assigned = item.get("_msdyn_activeagentid_value")
        if assigned and assigned != workstream["_msdyn_bot_user_value"]:
            raise NativeMediaError("human_already_assigned", "A native representative already owns this conversation.")

    async def readiness(self):
        identity = await self.dataverse.get("WhoAmI")
        _id(identity.get("UserId"))
        workstream = await self._workstream()
        workstreams = [workstream]
        for identifier in self.config.workstream_ids[1:]:
            workstreams.append(await self._workstream(identifier))
        for table, identifier in (
            ("msdyn_ocliveworkitems", "msdyn_ocliveworkitemid"),
            ("msdyn_ocsessions", "activityid"),
            ("msdyn_ocphonecallengagementctxes", "msdyn_ocphonecallengagementctxid"),
        ):
            await self.dataverse.get(table, params={"$select": identifier, "$top": "1"})
        if self._gateway is not None and self.config.enabled:
            await self._gateway.start()
            await self.warmup()
        try:
            _, digest = notice_asset()
            notice = {"startup_notice_available": True, "startup_notice_sha256": digest}
        except NativeNoticeError as error:
            logger.error("Native startup notice readiness failed code=%s", error.code)
            notice = {"startup_notice_available": False, "startup_notice_error": error.code}
        return {
            **self.health(),
            **notice,
            "dataverse_connected": True,
            "workstream": workstream["msdyn_name"],
            "workstreams": [
                {"id": row["msdyn_liveworkstreamid"], "name": row["msdyn_name"]}
                for row in workstreams
            ],
            "scope": "Configuration only. Native media, retained Copilot control and human handover require a live pilot.",
        }

    def start(
        self, conversation_id, language, *, speech_turn_control=False,
        retain_identity_for_metrics=False, play_startup_notice=False, native_startup_notice=False, supervision_contract=0,
        prepare_startup=False,
    ):
        identifier = _id(conversation_id)
        if type(supervision_contract) is not int or supervision_contract not in {0, CONTRACT}:
            raise NativeMediaError("unsupported_supervision_contract", "The native supervision controller contract is unsupported.", 400)
        if self.config.supervision.enabled and (
            supervision_contract != CONTRACT or not speech_turn_control or not self.supervision.ready
        ):
            raise NativeMediaError("supervision_controller_required", "A verified, published supervision controller is required.", 503)
        if not isinstance(language, str) or language not in {"ar", "en"}:
            raise NativeMediaError("invalid_language", "Choose ar or en.", 400)
        if not isinstance(speech_turn_control, bool) or (speech_turn_control and self.config.transport != "voip"):
            raise NativeMediaError("invalid_speech_control", "Speech-turn control requires a boolean opt-in and the VoIP transport.", 400)
        if not isinstance(retain_identity_for_metrics, bool) or (
            retain_identity_for_metrics and (not speech_turn_control or self._gateway is None)
        ):
            raise NativeMediaError(
                "invalid_identity_retention",
                "Identity retention requires a boolean opt-in and native VoIP speech-turn control.", 400,
            )
        if not isinstance(play_startup_notice, bool) or (
            play_startup_notice and (not speech_turn_control or self._gateway is None)
        ):
            raise NativeMediaError(
                "invalid_startup_notice",
                "The startup notice requires a boolean opt-in and native VoIP speech-turn control.", 400,
            )
        if not isinstance(native_startup_notice, bool) or (
            native_startup_notice and (play_startup_notice or not speech_turn_control or self._gateway is None)
        ):
            raise NativeMediaError(
                "invalid_startup_notice",
                "Native announcement ownership requires native VoIP speech control and excludes application playback.", 400,
            )
        if not isinstance(prepare_startup, bool) or (
            prepare_startup and not (play_startup_notice or native_startup_notice)
        ):
            raise NativeMediaError(
                "invalid_startup_wait", "Prepared startup requires one explicit announcement owner.", 400,
            )
        existing = self.sessions.get(identifier)
        if existing:
            if (
                existing.language != language or (existing.speech_control is not None) != speech_turn_control
                or existing.retain_identity_for_metrics != retain_identity_for_metrics
                or existing.play_startup_notice != play_startup_notice
                or existing.native_startup_notice != native_startup_notice
                or existing.prepared_startup_requested != (prepare_startup or native_startup_notice)
                or existing.supervision_contract != supervision_contract
            ):
                raise NativeMediaError("session_conflict", "The native session already has different language, controller or diagnostic settings.")
            return existing
        if self._closing or not self.config.enabled or not self.observer.enabled:
            raise NativeMediaError("pilot_disabled", "The native media pilot or its observer is disabled or expired.", 503)
        if play_startup_notice or native_startup_notice:
            try:
                notice_asset()
            except NativeNoticeError as error:
                raise NativeMediaError(error.code, str(error), 503) from error
        if any(not session.done.is_set() for session in self.sessions.values()):
            raise NativeMediaError("pilot_busy", "Only one native media pilot call is permitted at a time.", 409)
        if retain_identity_for_metrics and self._gateway is not None and self._gateway.retained_identity_count:
            raise NativeMediaError(
                "identity_retention_busy", "Finish the previous retained-identity cleanup before another diagnostic call.", 409,
            )
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=15)
        self.sessions = {
            key: value for key, value in self.sessions.items()
            if not value.done.is_set() or value.created_at >= cutoff or (
                value.retain_identity_for_metrics and value.voip is not None
                and self._gateway is not None and self._gateway.identity_cleanup_pending(value.voip)
            )
        }
        if len(self.sessions) >= 16:
            raise NativeMediaError("pilot_capacity", "The pilot history limit was reached; wait for expiry.", 429)
        session = NativeMediaSession(
            identifier, language, transport=self.config.transport,
            speech_control=NativeSpeechControl(language) if speech_turn_control else None,
            retain_identity_for_metrics=retain_identity_for_metrics,
            play_startup_notice=play_startup_notice,
            native_startup_notice=native_startup_notice,
            prepare_startup=prepare_startup,
            supervision_contract=supervision_contract,
        )
        session.latency.mark("setup_mode", fast_start=self.config.fast_start)
        self.sessions[identifier] = session
        session.runner = asyncio.create_task(self._run(session))
        session.runner.add_done_callback(lambda task: self._completed(session, task))
        return session

    @staticmethod
    def _completed(session, task):
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            logger.error("Native media task failed conversation=%s error=%s", session.conversation_id, type(error).__name__)
            session.phase = "failed"
            session.action = "stop"
            session.reason = "unexpected_pilot_failure"
            session.done.set()

    @staticmethod
    def _validate_controller(controller, conversation_id):
        if not isinstance(controller, dict):
            raise NativeMediaError("invalid_native_session", "Dynamics returned an invalid native session.")
        controller_id = _id(controller.get("activityid"), "native_session_id")
        if (
            controller.get("_msdyn_liveworkitemid_value") != conversation_id
            or controller.get("msdyn_sessioncreationreason") != 192350000
            or controller.get("msdyn_sessionclosedon") is not None
            or controller.get("msdyn_agentacceptedon") is not None
        ):
            raise NativeMediaError("controller_not_retained", "The original native AI session is no longer active. Do not use external phone transfer for this pilot.")
        return controller_id

    async def _check_controller(self, session):
        controller = await self.dataverse.get(
            f"msdyn_ocsessions({session.controller_session_id})",
            params={"$select": "activityid,_msdyn_liveworkitemid_value,msdyn_sessioncreationreason,msdyn_sessionclosedon,msdyn_agentacceptedon"},
        )
        if self._validate_controller(controller, session.conversation_id) != session.controller_session_id:
            raise NativeMediaError("controller_changed", "Dynamics returned a different native controller session.")

    async def _wait_for_language(self, session):
        state = session.speech_control
        if state is None:
            raise NativeMediaError("speech_control_disabled", "This native conversation does not use speech-turn control.")
        deadline = asyncio.get_running_loop().time() + LANGUAGE_ACK_SECONDS
        while True:
            state.changed.clear()
            if session.release.is_set() or session.done.is_set():
                raise NativeMediaError("speech_control_stopped", "The native conversation ended before language acknowledgement.")
            if state.ready:
                return
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise NativeMediaError("native_language_unconfirmed", "The controller did not complete a listening turn in the requested language.")
            waiters = [asyncio.create_task(state.changed.wait()), asyncio.create_task(session.release.wait())]
            try:
                await asyncio.wait(waiters, timeout=remaining, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for waiter in waiters:
                    waiter.cancel()
                await asyncio.gather(*waiters, return_exceptions=True)

    async def _select_response_language(self, session, arguments):
        voice, state = session.voice, session.speech_control
        if (
            voice is None or state is None or CURRENT_SESSION.get() is not voice
            or not isinstance(arguments, dict) or set(arguments) != {"language"}
            or not isinstance(arguments["language"], str) or arguments["language"] not in {"ar", "en"}
        ):
            raise ValueError("Language selection requires the exact native voice session and a supported language.")
        if session.phase != "active" or session.release.is_set() or session.intervention_latched or not state.ready:
            raise ValueError("The native session is not ready for another language change.")
        requested = arguments["language"]
        if requested != state.language:
            voice.customer_transcript_updated.clear()
            customer_turns = [
                segment for segment in voice.transcript_buffer.segments if segment.speaker_role == SpeakerRole.CUSTOMER
            ]
            if not customer_turns or customer_turns[-1].item_id == state.last_request_item_id:
                try:
                    await asyncio.wait_for(voice.customer_transcript_updated.wait(), timeout=1)
                except TimeoutError as error:
                    raise ValueError("A language change requires a new finalized utterance from the verified caller.") from error
                customer_turns = [
                    segment for segment in voice.transcript_buffer.segments if segment.speaker_role == SpeakerRole.CUSTOMER
                ]
            if (
                not customer_turns or customer_turns[-1].item_id == state.last_request_item_id
                or session.release.is_set() or session.intervention_latched or not state.ready
            ):
                raise ValueError("No new caller utterance is available for this language change.")
            state.last_request_item_id = customer_turns[-1].item_id
            state.language = requested
            state.revision += 1
            session.latency.mark("language_requested", language=requested, revision=state.revision)
            logger.info(
                "Native language requested conversation=%s language=%s revision=%d",
                session.conversation_id, requested, state.revision,
            )
            try:
                with session.latency.span("language_ack_wait", language=requested, revision=state.revision):
                    await self._wait_for_language(session)
            except (NativeMediaError, asyncio.CancelledError):
                if not session.release.is_set() and not session.intervention_latched:
                    voice.transfer_active = True
                    session.reason = "native_language_unconfirmed"
                    session.release.set()
                raise
        voice.language = requested
        logger.info(
            "Native language acknowledged conversation=%s language=%s revision=%d",
            session.conversation_id, requested, state.revision,
        )
        language_name = "Arabic" if requested == "ar" else "English"
        return ToolResult(json.dumps({
            "success": True, "language": requested, "response_language": language_name,
            "control_revision": state.revision,
            "message": (
                f"The caller explicitly selected {language_name}. The native speech controller is ready for {language_name}. "
                f"Answer the caller's pending question in {language_name} now. This replaces the initial response language. "
                f"Keep responding in {language_name} until another explicit change is confirmed."
            ),
        }), ToolResultDirection.TO_SERVER)

    async def _resolve_call(self, session):
        try:
            with session.latency.span("dataverse_conversation"):
                item = await self._conversation(session.conversation_id)
        except DataverseError as error:
            if error.code != "dynamics_not_found":
                raise
            raise NativeMediaError(
                "native_conversation_pending",
                "Dynamics has not exposed the exact new conversation record yet.",
            ) from error
        if item.get("msdyn_ocliveworkitemid") != session.conversation_id:
            raise NativeMediaError("native_id_mismatch", "Dynamics returned a different conversation.")
        workstream_id = item.get("_msdyn_liveworkstreamid_value")
        if workstream_id not in self.config.workstream_ids:
            raise NativeMediaError("native_call_not_active", "The selected conversation is not in an allowed native workstream.")
        with session.latency.span("dataverse_workstream"):
            workstream = await self._workstream(workstream_id)
        self._check_owner(item, workstream)
        try:
            created = datetime.fromisoformat(item["createdon"])
            if created.tzinfo is None:
                raise ValueError("Native creation time needs a time zone.")
            created = created.astimezone(timezone.utc)
        except (KeyError, ValueError, TypeError, OverflowError) as error:
            raise NativeMediaError("invalid_native_time", "The native conversation has no valid creation time.") from error
        age = datetime.now(timezone.utc) - created
        if not timedelta(seconds=-60) <= age <= timedelta(minutes=10):
            raise NativeMediaError("stale_native_call", "Start the pilot only on a fresh native inbound call.")
        with session.latency.span("dataverse_controller"):
            controllers = await self.dataverse.get(
                "msdyn_ocsessions",
                params={
                    "$select": "activityid,_msdyn_liveworkitemid_value,msdyn_sessioncreationreason,msdyn_sessionclosedon,msdyn_agentacceptedon",
                    "$filter": f"_msdyn_liveworkitemid_value eq {session.conversation_id} and msdyn_sessionclosedon eq null",
                    "$top": "2",
                },
            )
        candidates = controllers.get("value")
        if not isinstance(candidates, list) or len(candidates) > 1 or "@odata.nextLink" in controllers:
            raise NativeMediaError("ambiguous_native_session", "Dynamics did not return one unique open native session.")
        if not candidates:
            raise NativeMediaError("native_session_pending", "The native controller session is not available yet.")
        session.controller_session_id = self._validate_controller(candidates[0], session.conversation_id)
        with session.latency.span("dataverse_phone_context"):
            document = await self.dataverse.get(
                "msdyn_ocphonecallengagementctxes",
                params={
                    "$select": "msdyn_callsid,msdyn_fromphone",
                    "$filter": f"_msdyn_liveworkitemid_value eq {session.conversation_id}",
                    "$top": "2",
                },
            )
        rows = document.get("value")
        if not isinstance(rows, list) or len(rows) > 1 or "@odata.nextLink" in document:
            raise NativeMediaError("ambiguous_native_call", "The native phone context is missing or ambiguous.")
        if not rows:
            raise NativeMediaError("native_phone_context_pending", "The exact native phone context is not available yet.")
        if not isinstance(rows[0], dict):
            raise NativeMediaError("ambiguous_native_call", "The native phone context is invalid.")
        if rows[0].get("msdyn_callsid") in (None, ""):
            raise NativeMediaError("native_phone_context_pending", "The native call identifier is not available yet.")
        session.correlation_id = _id(rows[0].get("msdyn_callsid"), "native_call_id")
        phone = rows[0].get("msdyn_fromphone")
        if not isinstance(phone, str) or not re.fullmatch(r"\+[1-9]\d{6,14}", phone):
            raise NativeMediaError("unknown_customer_media", "The pilot cannot identify the customer's native phone participant.")
        locator = self.observer.find_call(session.correlation_id)
        if locator is None:
            raise NativeMediaError("native_event_missing", "The exact native incoming-call event is not available yet.")
        session.server_call_id = locator
        session.latency.mark("native_context_resolved")
        return workstream, phone

    async def _connect_acs(self, session, phone):
        if session.release.is_set():
            raise NativeMediaError("control_requested_stop", "The pilot was stopped before attachment.")
        if self._automation is None:
            self._automation = CallAutomationClient.from_connection_string(
                self.config.acs_connection_string, retry_total=0, connection_timeout=5, read_timeout=5,
            )
        transport = (
            self.config.public_url.replace("https://", "wss://", 1)
            + f"{PREFIX}/sessions/{session.conversation_id}/media?token={session.capability}"
        )
        options = MediaStreamingOptions(
            transport_url=transport, transport_type=StreamingTransportType.WEBSOCKET,
            content_type=MediaStreamingContentType.AUDIO,
            audio_channel_type=MediaStreamingAudioChannelType.UNMIXED,
            audio_format=AudioFormat.PCM24_K_MONO,
            enable_bidirectional=self._gateway is None, start_media_streaming=False,
        )
        session.connect_requested = True
        with session.latency.span("acs_connect_request"):
            properties = await self._automation.connect_call(
                server_call_id=session.server_call_id,
                callback_url=f"{self.config.public_url}/acs/call-events",
                operation_context=f"native-media-{session.conversation_id}",
                media_streaming=options,
            )
        session.connection_id = _id(properties.call_connection_id, "acs_connection_id")
        session.connection = self._automation.get_call_connection(session.connection_id)
        if session.release.is_set():
            raise NativeMediaError("control_requested_stop", "The pilot was stopped during attachment.")
        attempts = 0
        previous_state = None
        deadline = asyncio.get_running_loop().time() + ACS_CONNECT_SECONDS
        try:
            with session.latency.span("acs_connection_wait"):
                async with asyncio.timeout(ACS_CONNECT_SECONDS):
                    while True:
                        current = await session.connection.get_call_properties()
                        attempts += 1
                        if current.call_connection_state != previous_state:
                            previous_state = current.call_connection_state
                            label = previous_state if previous_state in set(CallConnectionState) else "unexpected"
                            session.latency.mark("acs_connection_state", status=label, count=attempts)
                        if current.call_connection_state == CallConnectionState.CONNECTED:
                            if current.server_call_id != session.server_call_id:
                                raise NativeMediaError("wrong_acs_call", "The ACS connection does not match the native call.")
                            break
                        if current.call_connection_state != CallConnectionState.CONNECTING:
                            raise NativeMediaError("native_connect_failed", "The native call ended before connection readiness.")
                        if asyncio.get_running_loop().time() >= deadline:
                            raise NativeMediaError("native_connect_failed", "ACS did not confirm the native call connection in time.")
                        await asyncio.sleep(0.5)
        except TimeoutError as error:
            raise NativeMediaError("native_connect_failed", "ACS did not confirm the native call connection in time.") from error
        session.latency.mark("acs_connection_ready", count=attempts)
        expected = PhoneNumberIdentifier(phone)
        customer_ids = set()
        async for participant in session.connection.list_participants():
            if isinstance(participant.identifier, PhoneNumberIdentifier) and participant.identifier.raw_id == expected.raw_id:
                customer_ids.add(participant.identifier.raw_id)
        if customer_ids != {expected.raw_id}:
            raise NativeMediaError("customer_not_in_native_call", "The customer phone participant was not found in the native ACS call.")
        session.latency.mark("customer_membership_verified")
        return expected.raw_id

    async def _prepare_voip(self, session):
        assert self._gateway is not None and session.correlation_id is not None
        with session.latency.span("voip_prepare"):
            session.voip = await self._gateway.prepare(
                session.conversation_id, session.correlation_id, latency=session.latency,
            )

    def _create_voice_session(self, session: NativeMediaSession, phone: str) -> RTMiddleTier:
        voice = self.session_factory(phone, session.effective_initial_language)
        voice.native_conversation_id = session.conversation_id
        voice.customer_phone = phone
        voice.customer_context_required = self.customer_context_provider is not None
        return voice

    def _select_initial_language(self, session: NativeMediaSession, context: CustomerContext) -> None:
        preference = context.customer.preferred_language if context.customer is not None else None
        if preference not in (None, "ar", "en"):
            raise CRMContextError("Dynamics returned an invalid initial language preference.")
        language = preference or session.language
        state = session.speech_control
        if state is None and language != session.language:
            raise NativeMediaError(
                "crm_language_control_required",
                "A different CRM initial language requires native speech-turn control.",
            )
        session.effective_initial_language = language
        if state is not None and state.language != language:
            state.language = language
            state.revision += 1
            state.changed.set()
        session.latency.mark(
            "crm_initial_language_selected", language=language,
            revision=state.revision if state is not None else 0,
            status="preferred" if preference is not None else "requested_default",
        )

    async def _prefetch_customer_context(self, session: NativeMediaSession, phone: str) -> RTMiddleTier:
        assert self.customer_context_provider is not None
        session.latency.mark("crm_lookup_start")
        try:
            with session.latency.span("crm_lookup"):
                # Resolve the preference before invoking the configured voice factory; this lookup cannot start media.
                lookup = RTMiddleTier(HumainConfig(authorization="", enabled=False))
                lookup.language = session.language
                lookup.native_conversation_id = session.conversation_id
                lookup.customer_phone = phone
                lookup.customer_context_required = True
                async with asyncio.timeout(CRM_CONTEXT_SECONDS):
                    context = await self.customer_context_provider(lookup)
                if not isinstance(context, CustomerContext):
                    raise CRMContextError("Dynamics returned invalid customer context.")
                self._select_initial_language(session, context)
                voice = self._create_voice_session(session, phone)
                apply_customer_context(voice, context)
        except TimeoutError as error:
            raise NativeMediaError("crm_context_timeout", "Dynamics customer context was not ready within the startup deadline.") from error
        except CRMContextError as error:
            raise NativeMediaError("crm_context_invalid", "Dynamics customer context could not be verified.") from error
        except (ClientError, ValueError) as error:
            raise NativeMediaError("crm_context_unavailable", "Dynamics customer context is currently unavailable.") from error
        session.latency.mark("crm_context_ready", prompt_bytes=len(voice.system_message.encode()))
        return voice

    async def _connect(self, session, phone):
        if session.release.is_set():
            raise NativeMediaError("control_requested_stop", "The pilot was stopped before attachment.")
        voice = None
        crm_lookup = None
        if self.customer_context_provider is not None:
            crm_lookup = asyncio.create_task(self._prefetch_customer_context(session, phone))
        preparation = None
        if self.config.fast_start and self._gateway is not None:
            if session.release.is_set():
                raise NativeMediaError("control_requested_stop", "The pilot was stopped before attachment.")
            preparation = asyncio.create_task(self._prepare_voip(session))
        try:
            customer_id = await self._connect_acs(session, phone)
            if crm_lookup is not None and preparation is None and self._gateway is not None:
                preparation = asyncio.create_task(self._prepare_voip(session))
            if session.play_startup_notice:
                if not session.prepared_startup_requested:
                    if crm_lookup is not None and voice is None:
                        voice = await crm_lookup
                    with session.latency.span("startup_notice_native_handoff_wait"):
                        await self._wait_for_language(session)
                if session.release.is_set():
                    raise NativeMediaError("control_requested_stop", "The pilot was stopped before the startup notice.")
                assert session.connection is not None and session.connection_id is not None and session.correlation_id is not None
                _, digest = notice_asset()
                session.notice = NativeStartupNotice(
                    session.connection, session.connection_id, session.correlation_id, session.latency,
                    f"startup-notice-{session.conversation_id}",
                )
                await session.notice.start(
                    phone, f"{self.config.public_url}{PREFIX}/startup-notice/{digest}.wav",
                    f"{self.config.public_url}{PREFIX}/sessions/{session.conversation_id}/notice-events?token={session.capability}",
                    digest,
                )
            if preparation is not None:
                await preparation
            elif self._gateway is not None:
                await self._prepare_voip(session)
            if crm_lookup is not None:
                voice = await crm_lookup
        finally:
            pending = [task for task in (preparation, crm_lookup) if task is not None]
            for task in pending:
                if not task.done():
                    task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
        if session.release.is_set():
            raise NativeMediaError("control_requested_stop", "The pilot was stopped before participant invitation.")
        if self._gateway is not None:
            assert session.voip is not None and session.connection is not None
            with session.latency.span("voip_invite"):
                await session.connection.add_participant(
                    target_participant=CommunicationUserIdentifier(session.voip.user.raw_id),
                    invitation_timeout=15, operation_context=f"native-humain-{session.conversation_id}",
                )
            with session.latency.span("voip_media_ready_wait"):
                await session.voip.wait(session.voip.media_ready, 15)
            member = await session.connection.get_participant(CommunicationUserIdentifier(session.voip.user.raw_id))
            if member.identifier.raw_id != session.voip.user.raw_id or session.voip.call_id != session.correlation_id:
                raise NativeVoipError("The HUMAIN microphone is not bound to the expected native call.")
            session.voip.authorized = True
            session.latency.mark("voip_membership_verified")
        if session.speech_control is not None and not session.prepared_startup_requested:
            with session.latency.span("initial_language_ack_wait"):
                await self._wait_for_language(session)
        if voice is None:
            voice = self._create_voice_session(session, phone)
        voice.latency = session.latency
        if self.config.fast_start:
            if self._token_http is None:
                self._token_http = ClientSession(timeout=ClientTimeout(total=20), cookie_jar=DummyCookieJar())
            voice.token_http = self._token_http
        session.voice = voice
        voice.native_startup_pending = session.prepared_startup_requested
        voice.current_call_connection_id = session.connection_id
        voice.native_pause_enabled = self.config.supervision.enabled and session.supervision_contract == CONTRACT
        voice.acs_input_participant_id = customer_id
        voice.transfer_enabled = True
        voice.operator_transfer_number = None
        if session.voip is not None:
            voice.native_audio_sink = session.voip.send_audio
            if session.notice is not None:
                notice, voip = session.notice, session.voip

                async def notice_then_audio(message):
                    if message.get("type") == "response.output_audio.delta":
                        await notice.stop()
                        if session.release.is_set():
                            raise NativeNoticeError("startup_notice_session_stopped")
                    await voip.send_audio(message)

                voice.native_audio_sink = notice_then_audio
        voice.tools = dict(voice.tools)
        if "talk_to_human" not in voice.tools:
            raise NativeMediaError("handover_tool_missing", "The native voice session has no human-handover tool.")

        async def handover(arguments):
            if arguments != {} or CURRENT_SESSION.get() is not voice:
                raise ValueError("Native handover requires the exact active voice session.")
            async with session.control_lock:
                if session.done.is_set() or session.phase != "active" or session.intervention_latched:
                    raise ValueError("The native AI session is not accepting a handover request.")
                voice.transfer_active = True
                session.reason = "customer_requested_human"
                session.latency.mark("handover_requested")
                session.phase = "releasing"
                session.release.set()
            return ToolResult(json.dumps({
                "success": True, "status": "handover_requested",
                "message": "Native handover requested. A representative is not connected yet.",
            }), ToolResultDirection.TO_SERVER)

        voice.tools["talk_to_human"] = Tool(schema=voice.tools["talk_to_human"].schema, target=handover)
        if session.speech_control is not None:
            if "select_response_language" in voice.tools:
                raise NativeMediaError("language_tool_conflict", "A response-language tool is already registered.")
            voice.tools["select_response_language"] = Tool(
                schema={
                    "type": "function", "name": "select_response_language",
                    "description": (
                        "Call before changing response language when the caller explicitly asks for Arabic or English. "
                        "Use ar or en. Wait for success before speaking in the new language. "
                        "Do not call just because a question, quotation or service name uses another language."
                    ),
                    "parameters": {
                        "type": "object", "properties": {"language": {"type": "string", "enum": ["ar", "en"]}},
                        "required": ["language"], "additionalProperties": False,
                    },
                },
                target=lambda arguments: self._select_response_language(session, arguments),
            )
            voice.system_message += (
                "\n\nNative conversation control:\n"
                "Only an explicit caller request changes the response language. Before every such change, call "
                "select_response_language and wait for success. Do not say a new-language acknowledgement first. "
                "On success, keep that language until the next explicit request. On failure, do not claim the change succeeded.\n"
                "An explicit request for a human takes priority over continuing an answer. Call talk_to_human immediately, "
                "without asking the caller to repeat or finishing the previous answer. Do not claim a human has joined."
            )
        session.bound.set()
        if session.release.is_set():
            raise NativeMediaError("control_requested_stop", "The pilot was stopped before media streaming.")
        with session.latency.span("acs_start_media"):
            await session.connection.start_media_streaming(operation_context=f"native-media-{session.conversation_id}")

    async def _resolve_pending_context(self, session):
        pending = None
        deadline = asyncio.get_running_loop().time() + CONTEXT_SECONDS
        try:
            with session.latency.span("native_context_wait"):
                async with asyncio.timeout(CONTEXT_SECONDS):
                    while True:
                        try:
                            return await self._resolve_call(session)
                        except NativeMediaError as error:
                            if (
                                error.code not in {
                                    "native_conversation_pending", "native_session_pending",
                                    "native_phone_context_pending", "native_event_missing",
                                } or session.release.is_set()
                            ):
                                raise
                            pending = error
                            if asyncio.get_running_loop().time() >= deadline:
                                raise
                            logger.info("Waiting for native context conversation=%s reason=%s", session.conversation_id, error.code)
                            await asyncio.sleep(0.5)
        except TimeoutError as error:
            if pending is not None:
                raise pending from error
            raise

    async def _hold_supervised(self, session, workstream, deadline):
        failures = 0
        session.phase = "supervised"
        while not session.release.is_set():
            if not session.detach_attempted and (
                asyncio.get_running_loop().time() >= deadline or session.provider_lost.is_set()
                or session.voice is None or not session.voice.ready
                or (session.voip is not None and session.voip.failed.is_set())
            ):
                await self._detach(session)
                session.ai_state = "released" if session.detached else "pause_failed"
            try:
                item = await self._conversation(session.conversation_id)
                self._check_owner(item, workstream)
                await self._check_controller(session)
                failures = 0
            except NativeMediaError as error:
                session.reason = error.code
                return
            except DataverseError:
                failures += 1
                if failures >= 5:
                    session.reason = "supervision_context_unconfirmed"
                    return
            try:
                await asyncio.wait_for(session.release.wait(), timeout=3)
            except TimeoutError:
                pass

    async def _run(self, session):
        workstream = None
        safe_to_transfer = False
        try:
            async with asyncio.timeout(SETUP_SECONDS):
                if self.config.supervision.enabled:
                    await self.supervision.verify()
                workstream, phone = await self._resolve_pending_context(session)
                session.context_ready.set()
                await self._connect(session, phone)
                while not session.release.is_set():
                    if session.voice is not None and session.voice.ready:
                        break
                    await asyncio.sleep(0.1)
                if session.release.is_set():
                    raise NativeMediaError("media_setup_failed", "The native media stream closed before HUMAIN became ready.")
                if session.prepared_startup_requested:
                    session.startup_prepared.set()
                    session.latency.mark("native_startup_prepared")
                    with session.latency.span("initial_language_ack_wait"):
                        await self._wait_for_language(session)
                    if session.notice is not None:
                        await session.notice.stop()
                    if session.release.is_set() or session.voice is None or not session.voice.ready:
                        raise NativeMediaError("media_setup_failed", "Native startup ended before audio release.")
                    self._check_owner(await self._conversation(session.conversation_id), workstream)
                    await self._check_controller(session)
                    if session.voip is not None:
                        session.voip.check()
                    await session.voice.release_native_startup()
                elif session.notice is not None:
                    await session.notice.stop()
            session.phase = "active"
            session.was_active = True
            session.latency.mark("session_active")
            logger.info("Native HUMAIN media active conversation=%s", session.conversation_id)
            deadline = asyncio.get_running_loop().time() + PILOT_SECONDS
            while not session.release.is_set():
                if session.intervention_latched:
                    await self._hold_supervised(session, workstream, deadline)
                    break
                if asyncio.get_running_loop().time() >= deadline:
                    session.reason = "pilot_duration_limit"
                    session.release.set()
                    break
                try:
                    await asyncio.wait_for(session.release.wait(), timeout=3)
                except TimeoutError:
                    item = await self._conversation(session.conversation_id)
                    self._check_owner(item, workstream)
                    await self._check_controller(session)
                    if session.voip is not None:
                        session.voip.check()
                    if session.voice is None or not session.voice.ready:
                        raise NativeMediaError("media_ended", "The HUMAIN native-media connection ended.")
            safe_to_transfer = session.reason in {
                "customer_requested_human", "pilot_duration_limit", "media_stream_closed", "native_language_unconfirmed",
                "startup_notice_failed",
            }
        except (NativeMediaError, DataverseError, NativeVoipError, NativeNoticeError, HumainError, AzureError, TimeoutError, ValueError) as error:
            session.reason = error.code if isinstance(error, (NativeMediaError, DataverseError, NativeNoticeError)) else type(error).__name__
            logger.error("Native media stopped conversation=%s reason=%s", session.conversation_id, session.reason)
            safe_to_transfer = workstream is not None and session.reason not in {
                "native_call_not_active", "human_already_assigned", "controller_changed",
                "control_requested_stop", "service_shutdown",
            }
        finally:
            session.phase = "releasing"
            if not session.detach_attempted:
                await self._detach(session)
            if session.voice is not None:
                await session.voice.drain_native_tools()
            if session.intervention_latched:
                session.action = "quiet_end" if session.reason in {
                    "native_call_not_active", "human_already_assigned", "controller_changed", "controller_not_retained",
                } else "quiet_hold"
                session.phase = "supervised_ended" if session.action == "quiet_end" else "supervised_hold"
                session.done.set()
                session.latency.mark("supervised_media_released", status=session.action)
                return
            if safe_to_transfer and session.detached and not self._closing:
                try:
                    item = await self._conversation(session.conversation_id)
                    self._check_owner(item, workstream)
                    await self._check_controller(session)
                except (NativeMediaError, DataverseError) as error:
                    logger.warning("Native transfer not offered conversation=%s reason=%s", session.conversation_id, error.code)
                    session.reason = error.code
                    session.failure_notice_suppressed = True
                    safe_to_transfer = False
            session.action = "handover" if safe_to_transfer and session.detached and not self._closing else "stop"
            session.phase = "handover_ready" if session.action == "handover" else "stopped"
            if not session.detached:
                session.phase = "failed"
                session.reason = "azure_cleanup_unconfirmed"
            session.done.set()
            session.latency.mark("session_released", status=session.action)
            logger.info("Native media released conversation=%s action=%s", session.conversation_id, session.action)
            if session.detached:
                self._schedule_warmup()

    async def _detach(self, session):
        session.detach_attempted = True
        voip_detached = True
        retain_identity = (
            session.retain_identity_for_metrics and session.reason == "customer_requested_human" and not self._closing
        )
        if session.voice is not None:
            session.voice.transfer_active = True
        if session.notice is not None:
            try:
                await session.notice.stop()
            except NativeNoticeError:
                logger.error("Startup notice cleanup unconfirmed; removing own call leg conversation=%s", session.conversation_id)
        if session.voip is not None and self._gateway is not None:
            try:
                with session.latency.span("voip_release"):
                    if retain_identity:
                        await self._gateway.release(session.voip, session.connection, retain_identity=True)
                    else:
                        await self._gateway.release(session.voip, session.connection)
            except NativeVoipError:
                voip_detached = False
                logger.error("HUMAIN microphone cleanup unconfirmed conversation=%s", session.conversation_id)
        if session.ws is not None and not session.ws.closed:
            try:
                async with asyncio.timeout(5):
                    if self._gateway is None:
                        await session.ws.send_json({"kind": "StopAudio", "audioData": None, "stopAudio": {}})
                    await session.ws.close()
            except (ConnectionError, TimeoutError):
                logger.warning("Native media WebSocket was already lost conversation=%s", session.conversation_id)
        if session.connection is not None:
            try:
                async with asyncio.timeout(10):
                    await session.connection.hang_up(is_for_everyone=False)
                    for attempt in range(5):
                        properties = await session.connection.get_call_properties()
                        if properties.call_connection_state == CallConnectionState.DISCONNECTED:
                            session.detached = voip_detached
                            break
                        if attempt < 4:
                            await asyncio.sleep(0.25)
                if not session.detached:
                    logger.error("Native own-leg disconnect not confirmed conversation=%s", session.conversation_id)
            except HttpResponseError as error:
                if error.status_code == 404:
                    session.detached = voip_detached
                    logger.info("Native media leg already absent conversation=%s", session.conversation_id)
                else:
                    logger.error("Native own-leg cleanup failed conversation=%s HTTP=%s", session.conversation_id, error.status_code)
            except (AzureError, TimeoutError) as error:
                logger.error("Native own-leg cleanup unconfirmed conversation=%s error=%s", session.conversation_id, type(error).__name__)
        elif not session.connect_requested:
            session.detached = voip_detached
        if retain_identity and not session.detached and session.voip is not None and self._gateway is not None:
            try:
                await self._gateway.finish_identity_cleanup(session.voip)
            except NativeVoipError:
                logger.error("Failed native detach also has unconfirmed identity cleanup conversation=%s", session.conversation_id)

    async def close(self):
        self._closing = True
        await self.supervision.close()
        if self._warmup_task is not None:
            self._warmup_task.cancel()
            await asyncio.gather(self._warmup_task, return_exceptions=True)
        for session in self.sessions.values():
            if not session.done.is_set():
                session.reason = "service_shutdown"
                session.release.set()
                if session.runner:
                    session.runner.cancel()
        tasks = [session.runner for session in self.sessions.values() if session.runner is not None]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if self._automation is not None:
            await self._automation.close()
        if self._gateway is not None:
            await self._gateway.close()
        if self._token_http is not None:
            await self._token_http.close()
        await self.dataverse.close()

    async def _wait_for_transcript(self, session, cursor):
        deadline = asyncio.get_running_loop().time() + WAIT_SECONDS
        while True:
            voice = session.voice
            if voice is not None:
                voice.transcript_updated.clear()
            result = session.snapshot(cursor)
            remaining = deadline - asyncio.get_running_loop().time()
            if result["utterance_text"] or session.done.is_set() or remaining <= 0:
                return result
            signal = voice.transcript_updated if voice is not None else session.bound
            waiters = [asyncio.create_task(signal.wait()), asyncio.create_task(session.done.wait())]
            try:
                await asyncio.wait(waiters, timeout=remaining, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for task in waiters:
                    task.cancel()
                await asyncio.gather(*waiters, return_exceptions=True)

    @staticmethod
    async def _request_body(request, *, array=False):
        limit = 16 * 1024 if array else MAX_REQUEST_BYTES
        raw = bytearray()
        async for chunk in request.content.iter_chunked(1024):
            raw.extend(chunk)
            if len(raw) > limit:
                raise NativeMediaError("too_large", f"Native pilot requests must not exceed {limit // 1024} KiB.", 413)
        try:
            body = json.loads(raw)
        except (ValueError, UnicodeDecodeError) as error:
            raise NativeMediaError("invalid_json", "A JSON request object is required.", 400) from error
        if not isinstance(body, list if array else dict):
            raise NativeMediaError("invalid_request", "A JSON request array is required." if array else "A JSON request object is required.", 400)
        return body

    async def _wait_for_context(self, session):
        waiters = [
            asyncio.create_task(session.context_ready.wait()),
            asyncio.create_task(session.done.wait()),
        ]
        try:
            with session.latency.span("native_context_response_wait"):
                completed, _ = await asyncio.wait(
                    waiters, timeout=CONTEXT_RESPONSE_SECONDS, return_when=asyncio.FIRST_COMPLETED,
                )
                if not completed:
                    session.reason = "native_context_unconfirmed"
                    session.release.set()
                    raise NativeMediaError("native_context_unconfirmed", "Native context was not ready within the startup bound.", 504)
        except asyncio.CancelledError:
            logger.warning("Native context wait cancelled; accepted setup remains managed conversation=%s", session.conversation_id)
            raise
        finally:
            for waiter in waiters:
                waiter.cancel()
            await asyncio.gather(*waiters, return_exceptions=True)

    async def _wait_for_startup(self, session):
        waiters = [
            asyncio.create_task(session.startup_prepared.wait()),
            asyncio.create_task(session.done.wait()),
        ]
        try:
            with session.latency.span("native_startup_response_wait"):
                completed, _ = await asyncio.wait(
                    waiters, timeout=STARTUP_RESPONSE_SECONDS, return_when=asyncio.FIRST_COMPLETED,
                )
                if not completed:
                    session.reason = "native_startup_unconfirmed"
                    session.release.set()
                    raise NativeMediaError("native_startup_unconfirmed", "Native startup was not prepared within its bound.", 504)
        except asyncio.CancelledError:
            logger.warning("Native startup waiter cancelled; accepted setup remains managed conversation=%s", session.conversation_id)
            raise
        finally:
            for waiter in waiters:
                waiter.cancel()
            await asyncio.gather(*waiters, return_exceptions=True)

    async def control(self, request):
        try:
            self._authorize(request)
            if request.match_info.route.name == "native-media-readiness":
                return web.json_response(await self.readiness(), headers={"Cache-Control": "no-store"})
            if request.match_info.route.name == "native-media-start":
                body = await self._request_body(request)
                required = {"conversation_id", "language", "retain_native_controller"}
                optional = {
                    "speech_turn_control", "retain_identity_for_metrics", "play_startup_notice",
                    "native_startup_notice", "supervision_contract",
                }
                if (
                    not required.issubset(body) or set(body) - required - optional
                    or body["retain_native_controller"] is not True
                ):
                    raise NativeMediaError("invalid_request", "Provide conversation_id, language and retain_native_controller=true.", 400)
                context_wait = request.query.getall("wait_for_context", [])
                startup_wait = request.query.getall("wait_for_startup", [])
                wait_for_context = context_wait == ["true"]
                wait_for_startup = startup_wait == ["true"]
                if (
                    set(request.query) - {"wait_for_context", "wait_for_startup"} or len(context_wait) > 1
                    or (context_wait and context_wait[0] not in {"true", "false"})
                    or (wait_for_context and body.get("play_startup_notice") is not True)
                    or (context_wait and startup_wait)
                ):
                    raise NativeMediaError("invalid_context_wait", "Context waiting requires wait_for_context=true and the startup notice opt-in.", 400)
                if (len(startup_wait) > 1 or (startup_wait and startup_wait[0] != "true")
                        or (wait_for_startup and (
                            (body.get("native_startup_notice") is True) == (body.get("play_startup_notice") is True)
                        ))
                        or (body.get("native_startup_notice") is True and not wait_for_startup)):
                    raise NativeMediaError("invalid_startup_wait", "Prepared startup requires wait_for_startup=true and exactly one announcement owner.", 400)
                options = {key: body[key] for key in (
                    "speech_turn_control", "retain_identity_for_metrics", "play_startup_notice",
                    "native_startup_notice", "supervision_contract",
                ) if key in body}
                if wait_for_startup:
                    options["prepare_startup"] = True
                session = self.start(body["conversation_id"], body["language"], **options)
                if wait_for_context:
                    await self._wait_for_context(session)
                elif wait_for_startup:
                    await self._wait_for_startup(session)
                return web.json_response(session.snapshot(), status=202, headers={"Cache-Control": "no-store"})
            identifier = _id(request.match_info["conversation_id"])
            session = self.sessions.get(identifier)
            if session is None:
                raise NativeMediaError("session_not_found", "The native pilot session was not found.", 404)
            if request.match_info.route.name == "native-media-timings":
                return web.json_response(session.latency.snapshot(), headers={"Cache-Control": "no-store"})
            if request.match_info.route.name == "native-media-turn":
                body = await self._request_body(request)
                if set(body) != {"language", "revision"} or request.query:
                    raise NativeMediaError("invalid_speech_turn", "A listening turn accepts only language and revision, not transcript text or intents.", 400)
                if session.speech_control is None:
                    raise NativeMediaError("speech_control_disabled", "This conversation does not use speech-turn control.")
                previous = session.speech_control.acknowledged_revision
                session.speech_control.acknowledge(body["language"], body["revision"])
                if session.speech_control.acknowledged_revision != previous:
                    session.latency.mark("native_listening_ack", language=body["language"], revision=body["revision"])
                return web.json_response(session.snapshot(), headers={"Cache-Control": "no-store"})
            cursor = request.query.get("transcript_after")
            if cursor is not None:
                if request.method != "GET" or not re.fullmatch(r"0|[1-9]\d{0,5}", cursor):
                    raise NativeMediaError("invalid_transcript_cursor", "Use a non-negative transcript_after integer on a status GET.", 400)
                after = int(cursor)
                result = (
                    await self._wait_for_transcript(session, after)
                    if request.query.get("wait") == "true" else session.snapshot(after)
                )
                return web.json_response(result, headers={"Cache-Control": "no-store"})
            if request.method == "POST":
                if request.can_read_body:
                    raise NativeMediaError("invalid_request", "Stop does not accept a request body.", 400)
                if not session.done.is_set():
                    session.reason = "control_requested_stop"
                    session.release.set()
            if request.query.get("wait") == "true" or request.method == "POST":
                try:
                    await asyncio.wait_for(session.done.wait(), timeout=WAIT_SECONDS)
                except TimeoutError:
                    pass
            return web.json_response(session.snapshot(), headers={"Cache-Control": "no-store"})
        except NativeVoipError:
            logger.error("Native VoIP readiness failed")
            return web.json_response({"error": {"code": "voip_unavailable", "message": "The native microphone adapter is unavailable."}}, status=503)
        except (NativeMediaError, DataverseError) as error:
            logger.warning("Native media request refused code=%s", error.code)
            return web.json_response({"error": {"code": error.code, "message": str(error)}}, status=error.status)

    async def startup_audio(self, request):
        try:
            content, digest = notice_asset()
        except NativeNoticeError as error:
            logger.error("Native startup notice asset unavailable code=%s", error.code)
            return web.json_response({"error": error.code}, status=503)
        if request.match_info["digest"] != digest:
            raise web.HTTPNotFound()
        return web.Response(
            body=content, content_type="audio/wav",
            headers={"Cache-Control": "public, max-age=31536000, immutable", "ETag": f'"{digest}"'},
        )

    async def notice_events(self, request):
        try:
            identifier = _id(request.match_info["conversation_id"])
            session = self.sessions.get(identifier)
            supplied = request.query.get("token", "")
            if (
                session is None or session.notice is None
                or not hmac.compare_digest(supplied.encode(), session.capability.encode())
            ):
                raise NativeMediaError("unauthorized_notice_event", "The notice callback capability is invalid.", 401)
            events = await self._request_body(request, array=True)
            if not 1 <= len(events) <= 8:
                raise NativeMediaError("invalid_notice_events", "Provide between one and eight notice events.", 400)
            for event in events:
                try:
                    session.notice.observe(event)
                except ValueError as error:
                    raise NativeMediaError("invalid_notice_event", str(error), 400) from error
            if session.notice.error_code and not session.done.is_set():
                logger.error("Native startup notice failed conversation=%s code=%s", identifier, session.notice.error_code)
                session.reason = "startup_notice_failed"
                session.release.set()
            return web.json_response({"accepted": len(events)}, headers={"Cache-Control": "no-store"})
        except NativeMediaError as error:
            logger.warning("Native notice callback refused code=%s", error.code)
            return web.json_response({"error": error.code}, status=error.status)

    async def media(self, request):
        try:
            identifier = _id(request.match_info["conversation_id"])
            session = self.sessions.get(identifier)
            supplied = request.query.get("token", "")
            if session is None or session.done.is_set() or not hmac.compare_digest(supplied.encode(), session.capability.encode()):
                raise NativeMediaError("unauthorized_media", "The native media capability is invalid or expired.", 401)
            try:
                await asyncio.wait_for(session.bound.wait(), timeout=10)
            except TimeoutError as error:
                raise NativeMediaError("connection_not_bound", "Native call connection was not verified in time.", 409) from error
            if session.release.is_set() or session.done.is_set() or session.voice is None:
                raise NativeMediaError("media_stopped", "The native media session is no longer accepting audio.", 409)
            if request.headers.get("x-ms-call-connection-id") != session.connection_id:
                raise NativeMediaError("wrong_connection", "The media stream does not match the verified ACS connection.", 403)
            if session.media_task is not None:
                raise NativeMediaError("duplicate_media", "A native media stream is already attached.", 409)
            ws = web.WebSocketResponse(max_msg_size=128 * 1024)
            session.media_task = asyncio.current_task()
            await ws.prepare(request)
            session.ws = ws
            try:
                await session.voice.forward_messages(ws, True)
            finally:
                if session.intervention_latched:
                    session.provider_lost.set()
                elif not session.release.is_set():
                    session.reason = "media_stream_closed"
                    session.release.set()
            return ws
        except NativeMediaError as error:
            logger.warning("Native media stream refused code=%s", error.code)
            return web.json_response({"error": error.code}, status=error.status)

    def register(self, app):
        app.router.add_get(f"{PREFIX}/startup-notice/{{digest}}.wav", self.startup_audio, name="native-startup-audio")
        app.router.add_get(f"{PREFIX}/readiness", self.control, name="native-media-readiness")
        app.router.add_post(f"{PREFIX}/sessions", self.control, name="native-media-start")
        app.router.add_get(f"{PREFIX}/sessions/{{conversation_id}}", self.control, name="native-media-status")
        app.router.add_get(f"{PREFIX}/sessions/{{conversation_id}}/timings", self.control, name="native-media-timings")
        app.router.add_post(f"{PREFIX}/sessions/{{conversation_id}}/stop", self.control, name="native-media-stop")
        app.router.add_post(f"{PREFIX}/sessions/{{conversation_id}}/turn", self.control, name="native-media-turn")
        app.router.add_get(f"{PREFIX}/sessions/{{conversation_id}}/media", self.media, name="native-media-stream")
        app.router.add_post(f"{PREFIX}/sessions/{{conversation_id}}/notice-events", self.notice_events, name="native-notice-events")
