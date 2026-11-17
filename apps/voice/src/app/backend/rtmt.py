import asyncio
from collections import deque
from collections.abc import Awaitable, Callable
import base64
import binascii
import json
import logging
import uuid
from collections.abc import Coroutine
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

import aiohttp
from aiohttp import web
from livekit import rtc

from backend.agent_assist import AgentAssistClient, AgentAssistSession
from backend.crm_context import CustomerContext, SelectedCaseReceipt
from backend.helpers import transform_openai_to_acs_format
from backend.humain import HumainConfig, HumainError, ignored_metadata_fields, mint_token, room_metadata, validate_voice
from backend.latency import LatencyTrace, measure
from backend.tools.case_tools import CaseServiceClient
from backend.tools.tools import CURRENT_SESSION, Tool, ToolResult, ToolResultDirection
from runtime_config import DEPLOYMENT, Deployment

logger = logging.getLogger("rtmt")

SAMPLE_RATE = 24000
FRAME_SAMPLES = SAMPLE_RATE // 50
FRAME_BYTES = FRAME_SAMPLES * 2
RPC_MAX_BYTES = 15 * 1024
NATIVE_STARTUP_BUFFER_BYTES = SAMPLE_RATE * 2 * 10


class SpeakerRole:
    CUSTOMER = "customer"
    AI_AGENT = "ai_agent"
    OPERATOR = "operator"
    UNKNOWN = "unknown"


@dataclass
class TranscriptSegment:
    item_id: str
    speaker_raw: Optional[str]
    speaker_role: str
    start: Optional[float]
    end: Optional[float]
    text: str
    timestamp: datetime = field(default_factory=datetime.utcnow)


class TranscriptBuffer:
    """Per-call transcripts, also used by the ACS human-handover tools."""

    def __init__(self):
        self.segments: list[TranscriptSegment] = []
        self.pending_deltas: dict[str, str] = {}
        self._speaker_map: dict[str, str] = {}
        self._handover_occurred = False
        self._customer_speaker: Optional[str] = None
        self._agent_speaker: Optional[str] = None

    def mark_handover(self):
        self._handover_occurred = True
        if self._agent_speaker and self._agent_speaker in self._speaker_map:
            self._speaker_map[self._agent_speaker] = SpeakerRole.OPERATOR

    def _map_speaker(self, raw_speaker: Optional[str]) -> str:
        if not raw_speaker:
            return SpeakerRole.UNKNOWN
        if raw_speaker in self._speaker_map:
            return self._speaker_map[raw_speaker]
        if self._agent_speaker is None:
            self._agent_speaker = raw_speaker
            role = self.get_current_agent_role()
        elif self._customer_speaker is None:
            self._customer_speaker = raw_speaker
            role = SpeakerRole.CUSTOMER
        elif self._handover_occurred:
            role = SpeakerRole.OPERATOR
        else:
            logger.warning("Unknown diarized speaker: %s", raw_speaker)
            return SpeakerRole.UNKNOWN
        self._speaker_map[raw_speaker] = role
        return role

    def add_delta(self, item_id: str, delta: str):
        self.pending_deltas[item_id] = self.pending_deltas.get(item_id, "") + delta

    def add_segment(self, item_id, speaker, start, end, text):
        self.segments.append(TranscriptSegment(
            item_id, speaker, self._map_speaker(speaker), start, end, text
        ))

    def mark_completed(self, item_id: str, transcript: str):
        self.pending_deltas.pop(item_id, None)
        if transcript:
            self.segments.append(TranscriptSegment(
                item_id, self._customer_speaker, SpeakerRole.CUSTOMER, None, None, transcript
            ))

    def get_current_agent_role(self) -> str:
        return SpeakerRole.OPERATOR if self._handover_occurred else SpeakerRole.AI_AGENT

    def add_agent_transcript(self, item_id: str, transcript: str, role: str):
        self.segments.append(TranscriptSegment(
            item_id, self._agent_speaker, role, None, None, transcript
        ))

    def get_full_transcript(self) -> str:
        return "\n".join(f"[{s.speaker_role}]: {s.text}" for s in self.segments)

    def get_segments_json(self) -> list[dict]:
        return [{
            "item_id": s.item_id,
            "speaker_raw": s.speaker_raw,
            "speaker_role": s.speaker_role,
            "start": s.start,
            "end": s.end,
            "text": s.text,
            "timestamp": s.timestamp.isoformat(),
        } for s in self.segments]

    def get_conversation_summary(self) -> dict:
        return {
            "total_segments": len(self.segments),
            "customer_segments": sum(s.speaker_role == SpeakerRole.CUSTOMER for s in self.segments),
            "ai_agent_segments": sum(s.speaker_role == SpeakerRole.AI_AGENT for s in self.segments),
            "operator_segments": sum(s.speaker_role == SpeakerRole.OPERATOR for s in self.segments),
            "handover_occurred": self._handover_occurred,
            "speaker_mapping": dict(self._speaker_map),
        }


class RTMiddleTier:
    """Bridge the existing browser/ACS PCM protocol to one HUMAIN LiveKit room.

    The startup instance is a template. Every call gets a clone with its own room,
    participant identity, transcripts, RPC tasks, and handover state.
    """

    def __init__(self, config: HumainConfig, deployment: Deployment | None = None):
        self.config = config
        self.deployment = deployment or DEPLOYMENT
        self.selected_voice = validate_voice(config.voice)
        self.tools: dict[str, Tool] = {}
        self.system_message = ""
        self.intro_message = ""
        self.acs_caller: Any = None
        self.agent_assist_client: Optional[AgentAssistClient] = None
        self.agent_assist_session: Optional[AgentAssistSession] = None
        self.current_call_connection_id: Optional[str] = None
        self.customer_name: Optional[str] = None
        self.customer_first_name: str | None = None
        self.customer_email: Optional[str] = None
        self.customer_phone: Optional[str] = None
        self.native_conversation_id: str | None = None
        self.customer_context: CustomerContext | None = None
        self.selected_case: SelectedCaseReceipt | None = None
        self._case_selection_revision: int = 0
        self.customer_context_required: bool = False
        self._crm_base_system_message: str | None = None
        self._crm_base_intro_message: str | None = None
        self.language = self.deployment.organization.defaultLanguage
        self.operator_transfer_number: Optional[str] = None
        self.transfer_enabled = False
        self.transfer_active = False
        self.native_pause_enabled = False
        self.native_startup_pending = False
        self._native_startup_output: deque[tuple[dict, int]] = deque()
        self._native_startup_output_bytes = 0
        self._native_startup_draining = False
        self.ai_paused = False
        self._capture_lock = asyncio.Lock()
        self._output_lock = asyncio.Lock()
        self._tool_operations: set[asyncio.Task] = set()
        self._native_tool_uncertain = 0
        self._transferred_calls: set[str] = set()
        self._participant_map: dict[str, str] = {}
        self._customer_participant_id: Optional[str] = None
        self._operator_participant_id: Optional[str] = None
        self._current_audio_speaker: Optional[str] = None
        self.transcript_buffer = TranscriptBuffer()

        session_id = uuid.uuid4().hex
        self._identity = f"moacc-{session_id}"
        self._room_name = f"moacc-session-{session_id}"
        self._room: Optional[rtc.Room] = None
        self._source: Optional[rtc.AudioSource] = None
        self._mic_track_sid: Optional[str] = None
        self._client_ws: Optional[web.WebSocketResponse] = None
        self._is_acs = False
        self._ready = False
        self._closing = False
        self._agents: set[str] = set()
        self._agent_joined = asyncio.Event()
        self._failure: Optional[asyncio.Future[str]] = None
        self._tasks: set[asyncio.Task] = set()
        self._rpc_tasks: set[asyncio.Task] = set()
        self._transcript_tasks: set[asyncio.Task] = set()
        self._transcript_lock = asyncio.Lock()
        self._seen_transcripts: set[tuple[str, str]] = set()
        self._ignored_metadata_fields: tuple[str, ...] = ()
        self._input_buffer = bytearray()
        self.acs_input_participant_id: Optional[str] = None
        self.input_audio_frames = 0
        self.output_audio_frames = 0
        self.transcript_updated = asyncio.Event()
        self.customer_transcript_updated = asyncio.Event()
        self.native_audio_sink: Optional[Callable[[dict], Awaitable[None]]] = None
        self.latency: LatencyTrace | None = None
        self.token_http: aiohttp.ClientSession | None = None

    def clone_for_session(self) -> "RTMiddleTier":
        clone = RTMiddleTier(self.config, self.deployment)
        clone.selected_voice = self.selected_voice
        clone.tools = self.tools
        clone.system_message = self.system_message if self._crm_base_system_message is None else self._crm_base_system_message
        clone.intro_message = self.intro_message if self._crm_base_intro_message is None else self._crm_base_intro_message
        clone.language = self.language
        clone.acs_caller = self.acs_caller
        clone.agent_assist_client = self.agent_assist_client
        return clone

    @property
    def ready(self) -> bool:
        return self._ready and not self._closing

    def _fail(self, message: str) -> None:
        if not self._closing and self._failure is not None and not self._failure.done():
            self._failure.set_result(message)

    def _start_task(self, coroutine: Coroutine, *, transcript: bool = False) -> asyncio.Task:
        tasks = self._transcript_tasks if transcript else self._tasks
        task = asyncio.create_task(coroutine)
        tasks.add(task)

        def finished(completed: asyncio.Task) -> None:
            tasks.discard(completed)
            if completed.cancelled():
                return
            error = completed.exception()
            if error is not None:
                logger.error("HUMAIN session task failed (%s)", type(error).__name__)
                if not transcript:
                    self._fail(
                        str(error) if isinstance(error, HumainError)
                        else "The HUMAIN voice connection failed. Please restart the session."
                    )

        task.add_done_callback(finished)
        return task

    async def _send_to_client(self, message: dict) -> None:
        async with self._output_lock:
            await self._send_to_client_unlocked(message)

    async def _send_to_client_unlocked(self, message: dict) -> None:
        if self._client_ws is None or self._client_ws.closed:
            return
        if (self.transfer_active or self.ai_paused) and message.get("type") == "response.output_audio.delta":
            return
        if message.get("type") == "input_audio_buffer.speech_started":
            self._clear_native_startup_output()
        if message.get("type") == "response.output_audio.delta" and (
            self.native_startup_pending or self._native_startup_draining
        ):
            encoded = message.get("delta")
            if not isinstance(encoded, str):
                raise HumainError("Native startup audio must contain base64 PCM16.")
            try:
                pcm = base64.b64decode(encoded, validate=True)
            except (ValueError, binascii.Error) as error:
                raise HumainError("Native startup audio has invalid encoding.") from error
            if not pcm or len(pcm) % 2 or len(pcm) > SAMPLE_RATE * 2:
                raise HumainError("Native startup audio frame size is invalid.")
            if self._native_startup_output_bytes + len(pcm) > NATIVE_STARTUP_BUFFER_BYTES:
                raise HumainError("Native startup audio exceeded its bounded buffer; refusing to discard speech.")
            self._native_startup_output.append((dict(message), len(pcm)))
            self._native_startup_output_bytes += len(pcm)
            return
        await self._deliver_to_client_unlocked(message)

    async def _deliver_to_client_unlocked(self, message: dict) -> None:
        is_audio = message.get("type") == "response.output_audio.delta"
        if self._is_acs and self.native_audio_sink is not None and (
            is_audio or message.get("type") == "input_audio_buffer.speech_started"
        ):
            await self.native_audio_sink(message)
            if is_audio:
                self.output_audio_frames += 1
            return
        # These event names are the existing client wire contract, not an OpenAI connection.
        if self._is_acs:
            message = transform_openai_to_acs_format(message)
        if message is not None:
            await self._client_ws.send_json(message)
            if is_audio:
                self.output_audio_frames += 1

    def _clear_native_startup_output(self):
        self._native_startup_output.clear()
        self._native_startup_output_bytes = 0

    async def release_native_startup(self):
        async with self._capture_lock:
            async with self._output_lock:
                if self._closing or self.transfer_active or self.ai_paused or not self.ready:
                    self._clear_native_startup_output()
                    raise HumainError("Native startup ended before audio release was confirmed.")
                if not self._is_acs or self.native_audio_sink is None:
                    raise HumainError("Native startup release requires its verified native audio sink.")
                self._input_buffer.clear()
                self.native_startup_pending = False
                if self.latency is not None:
                    self.latency.mark("native_startup_audio_released")
                if self._native_startup_output and not self._native_startup_draining:
                    self._native_startup_draining = True
                    self._start_task(self._drain_native_startup_output())

    async def _drain_native_startup_output(self):
        loop = asyncio.get_running_loop()
        next_frame_at = loop.time()
        try:
            while True:
                async with self._output_lock:
                    if self._closing or self.transfer_active or self.ai_paused:
                        self._clear_native_startup_output()
                    if not self._native_startup_output:
                        self._native_startup_draining = False
                        return
                    message, size = self._native_startup_output.popleft()
                    self._native_startup_output_bytes -= size
                    if self._client_ws is None or self._client_ws.closed:
                        raise HumainError("Native startup media closed before buffered speech was delivered.")
                    await self._deliver_to_client_unlocked(message)
                # Pace retained frames so the native microphone's 500 ms queue cannot overflow.
                next_frame_at = max(next_frame_at + size / (SAMPLE_RATE * 2), loop.time())
                await asyncio.sleep(max(0, next_frame_at - loop.time()))
        finally:
            self._native_startup_draining = False
            self._clear_native_startup_output()

    def _agent_connected(self, participant: rtc.RemoteParticipant) -> None:
        if participant.kind == rtc.ParticipantKind.PARTICIPANT_KIND_AGENT:
            self._agents.add(participant.identity)
            self._agent_joined.set()
            if self.latency is not None:
                self.latency.mark("humain_agent_seen", once=True)

    def _register_room_handlers(self) -> None:
        room = self._room
        assert room is not None
        room.on("participant_connected", self._agent_connected)

        @room.on("participant_disconnected")
        def participant_disconnected(participant):
            if participant.identity in self._agents:
                self._agents.discard(participant.identity)
                self._fail("The HUMAIN agent left the session. Please reconnect.")

        @room.on("disconnected")
        def disconnected(reason):
            self._fail("The HUMAIN LiveKit connection was lost. Please reconnect.")

        @room.on("track_subscribed")
        def track_subscribed(track, publication, participant):
            self._agent_connected(participant)
            if participant.identity in self._agents and track.kind == rtc.TrackKind.KIND_AUDIO:
                self._start_task(self._relay_audio(track))

        @room.on("track_subscription_failed")
        def track_subscription_failed(participant, track_sid, error):
            if participant.identity in self._agents:
                self._fail("Could not subscribe to the HUMAIN agent's audio.")

        @room.on("room_metadata_changed")
        def metadata_changed(metadata):
            self._check_metadata(metadata)

        # HUMAIN emits the deprecated event too, with different segment IDs.
        # Use one transport so a spoken turn is not ingested twice.
        room.register_text_stream_handler(
            "lk.transcription",
            lambda reader, identity: self._start_task(
                self._read_transcription(reader, identity), transcript=True
            ),
        )

        @room.on("data_received")
        def data_received(packet):
            if (
                packet.topic not in {"tool", "tool_status", "tool_result"}
                or packet.participant is None
                or packet.participant.identity not in self._agents
            ):
                return
            try:
                event = json.loads(packet.data.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                logger.warning("Invalid HUMAIN %s lifecycle event", packet.topic)
                return
            if not isinstance(event, dict):
                logger.warning("HUMAIN %s lifecycle event must be an object", packet.topic)
                return
            self._start_task(self._send_to_client({
                "type": "extension.humain.tool",
                "topic": packet.topic,
                "event": event,
            }))

    def _check_metadata(self, metadata: str) -> None:
        if not metadata or self._closing:
            return
        try:
            document = json.loads(metadata)
        except json.JSONDecodeError:
            logger.warning("HUMAIN room metadata is not valid JSON")
            return
        effective = document.get("effective") if isinstance(document, dict) else None
        if not isinstance(effective, dict):
            return
        fields = ignored_metadata_fields(effective.get("ignored_metadata"))
        if fields == self._ignored_metadata_fields:
            return
        self._ignored_metadata_fields = fields
        warning = None
        if fields:
            logger.warning(
                "HUMAIN ignored optional metadata fields=%s in room %s; continuing the session",
                ",".join(fields), self._room_name,
            )
            warning = {
                "code": "humain_ignored_metadata",
                "message": "HUMAIN did not apply some optional settings. The conversation can continue.",
                "ignored_fields": list(fields),
            }
        else:
            logger.info("HUMAIN metadata warning cleared in room %s", self._room_name)
        self._start_task(self._send_to_client({"type": "session.warning", "warning": warning}))

    async def _connect(self) -> None:
        assert self._room is not None
        if self.customer_context_required and self.customer_context is None:
            raise HumainError("Dynamics customer context must be ready before starting native voice.")
        try:
            metadata = room_metadata(
                self.config, self._identity, self.selected_voice,
                self.system_message, self.intro_message, self.tools,
            )
        except ValueError as exc:
            raise HumainError(str(exc)) from exc
        if self.latency is not None:
            self.latency.mark(
                "humain_context_ready", prompt_bytes=len(self.system_message.encode()),
                llm=self.config.llm_provider, asr=self.config.asr_model,
            )
        with measure(self.latency, "humain_token"):
            options = {"http_session": self.token_http} if self.token_http is not None else {}
            credentials = await mint_token(self.config, self._identity, self._room_name, metadata, **options)
        try:
            with measure(self.latency, "humain_room_connect"):
                async with asyncio.timeout(30):
                    await self._room.connect(credentials.url, credentials.token)
        except (rtc.ConnectError, TimeoutError) as exc:
            raise HumainError("Could not connect to HUMAIN's LiveKit media server.") from exc

        self._room.local_participant.register_rpc_method("client_tool_call", self._handle_tool_call)
        for participant in self._room.remote_participants.values():
            self._agent_connected(participant)
        self._check_metadata(self._room.metadata)
        if self._failure is not None and self._failure.done():
            raise HumainError(self._failure.result())

        self._source = rtc.AudioSource(SAMPLE_RATE, 1, queue_size_ms=200)
        track = rtc.LocalAudioTrack.create_audio_track("customer-microphone", self._source)
        options = rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE)
        try:
            with measure(self.latency, "humain_microphone_publish"):
                async with asyncio.timeout(15):
                    publication = await self._room.local_participant.publish_track(track, options)
        except (RuntimeError, TimeoutError) as exc:
            raise HumainError("Could not publish the microphone to HUMAIN.") from exc
        self._mic_track_sid = publication.sid
        try:
            with measure(self.latency, "humain_agent_wait"):
                async with asyncio.timeout(30):
                    await self._agent_joined.wait()
        except TimeoutError as exc:
            raise HumainError("HUMAIN's hv-agent-flex agent did not join the room within 30 seconds.") from exc
        self._ready = True
        if self.latency is not None:
            self.latency.mark("humain_ready")
        await self._send_to_client({
            "type": "session.updated",
            "session": {"provider": "humain", "voice": self.selected_voice},
        })
        logger.info("HUMAIN session ready: %s (ACS: %s)", self._room_name, self._is_acs)

    async def _relay_audio(self, track: rtc.RemoteAudioTrack) -> None:
        stream = rtc.AudioStream(
            track, sample_rate=SAMPLE_RATE, num_channels=1, frame_size_ms=20, capacity=10
        )
        try:
            async for event in stream:
                if not self.transfer_active and not self.ai_paused and any(event.frame.data):
                    if self.latency is not None:
                        self.latency.audio("output", event.frame.data)
                    await self._send_to_client({
                        "type": "response.output_audio.delta",
                        "delta": base64.b64encode(event.frame.data).decode("ascii"),
                    })
        finally:
            await stream.aclose()

    def _speaker_role(self, identity: str, attributes: dict[str, str]) -> Optional[str]:
        if identity == self._identity or (
            self._mic_track_sid
            and attributes.get("lk.transcribed_track_id") == self._mic_track_sid
        ):
            return SpeakerRole.CUSTOMER
        if identity in self._agents:
            return SpeakerRole.AI_AGENT
        logger.warning("Ignoring transcription from an unrecognized HUMAIN participant")
        return None

    async def _read_transcription(self, reader: rtc.TextStreamReader, identity: str) -> None:
        try:
            text = await reader.read_all()
        except rtc.StreamError:
            logger.warning("HUMAIN transcription stream was aborted before completion")
            return
        # Finality/segment attributes can arrive in the stream trailer, so read them after EOF.
        attributes = reader.info.attributes or {}
        if attributes.get("lk.transcription_final") == "false":
            return
        role = self._speaker_role(identity, attributes)
        if role is not None:
            item_id = attributes.get("lk.segment_id") or reader.info.stream_id
            await self._record_transcript(role, item_id, text)

    async def _record_transcript(self, role: str, item_id: str, text: str) -> None:
        async with self._transcript_lock:
            key = (role, item_id)
            if self.transfer_active or self.ai_paused or not text.strip() or key in self._seen_transcripts:
                return
            self._seen_transcripts.add(key)
            if self.latency is not None:
                self.latency.mark(
                    "customer_transcript_final" if role == SpeakerRole.CUSTOMER else "ai_transcript_final",
                    count=len(self.transcript_buffer.segments) + 1,
                )
            if role == SpeakerRole.CUSTOMER:
                self.transcript_buffer.mark_completed(item_id, text)
                self.customer_transcript_updated.set()
                event_type = "conversation.item.input_audio_transcription.completed"
            else:
                self.transcript_buffer.add_agent_transcript(item_id, text, SpeakerRole.AI_AGENT)
                self.transcript_updated.set()
                event_type = "response.output_audio_transcript.done"
            await self._send_to_client({
                "type": event_type, "item_id": item_id, "transcript": text,
            })
            if self.agent_assist_client and self.agent_assist_session:
                try:
                    async with asyncio.timeout(10):
                        await self.agent_assist_client.send_message(self.agent_assist_session, role, text)
                except TimeoutError:
                    logger.error("Timed out forwarding a HUMAIN transcript to the assist console")

    def _tool_error(self, message: str) -> str:
        logger.warning("HUMAIN tool call failed: %s", message)
        return json.dumps({"ok": False, "error": message})

    async def _handle_tool_call(self, invocation: rtc.RpcInvocationData) -> str:
        if self._closing or invocation.caller_identity not in self._agents:
            return self._tool_error("Tool caller is not the active HUMAIN agent.")
        try:
            payload = json.loads(invocation.payload)
        except json.JSONDecodeError:
            return self._tool_error("Tool payload must be valid JSON.")
        if not isinstance(payload, dict) or not isinstance(payload.get("tool"), str):
            return self._tool_error("Tool payload must contain a tool name.")
        name = payload["tool"]
        arguments = payload.get("arguments")
        if name not in self.tools or not isinstance(arguments, dict):
            return self._tool_error("Unknown tool or invalid arguments object.")
        if self.transfer_active or self.ai_paused:
            return self._tool_error("AI tools are paused. No new operation was started.")
        if self.native_startup_pending:
            return self._tool_error("Native voice startup is not acknowledged. No tool operation was started.")
        if self.native_pause_enabled and len(self._tool_operations) >= 8:
            return self._tool_error("The native tool limit is reached. No new operation was started.")

        task = asyncio.current_task()
        if task is not None:
            self._rpc_tasks.add(task)
        token = CURRENT_SESSION.set(self)
        try:
            logger.info("HUMAIN tool started: %s (room=%s request=%s)", name, self._room_name, invocation.request_id)
            if self.native_pause_enabled:
                operation = asyncio.create_task(self._execute_admitted_tool(name, arguments, invocation.response_timeout))
                self._tool_operations.add(operation)
                operation.add_done_callback(self._tool_operation_finished)
                result = await asyncio.shield(operation)
            else:
                with measure(self.latency, "tool_execution", tool=name):
                    async with asyncio.timeout(invocation.response_timeout):
                        result = await self.tools[name].target(arguments)
            if not isinstance(result, ToolResult):
                return self._tool_error("Tool returned an invalid result.")
            text = result.to_text()
            if self.transfer_active:
                self._input_buffer.clear()
                if self._source is not None:
                    self._source.clear_queue()
                await self._send_to_client({"type": "input_audio_buffer.speech_started"})
            if result.destination == ToolResultDirection.TO_CLIENT:
                if self.ai_paused:
                    return json.dumps({"result": text})
                await self._send_to_client({
                    "type": "extension.middle_tier_tool_response",
                    "tool_name": name, "tool_result": text, "previous_item_id": invocation.request_id,
                })
                text = ""
            try:
                structured = json.loads(text)
            except json.JSONDecodeError:
                structured = text
            if isinstance(structured, dict) and (
                structured.get("success") is False or structured.get("ok") is False
            ):
                response = {key: value for key, value in structured.items() if key not in {"success", "ok"}}
                response.update({"ok": False, "error": structured.get("error") or "Tool failed."})
            else:
                response = {"result": structured}
            logger.info(
                "HUMAIN tool finished: %s (room=%s request=%s success=%s)",
                name, self._room_name, invocation.request_id, response.get("ok") is not False,
            )
            encoded = json.dumps(response, ensure_ascii=False)
            if len(encoded.encode("utf-8")) > RPC_MAX_BYTES:
                return self._tool_error("Tool result exceeds the 15 KiB RPC limit. Request fewer results.")
            return encoded
        except (TimeoutError, aiohttp.ClientError, ValueError, TypeError, KeyError, RuntimeError) as exc:
            logger.error("HUMAIN tool %s failed (%s)", name, type(exc).__name__)
            if name == "create_case":
                lookup = CaseServiceClient.creation_lookup_reference(self, arguments)
                if lookup is not None:
                    return json.dumps({
                        "ok": False, "lookup_reference": lookup,
                        "error": "The service could not confirm the outcome. Check status before repeating the action.",
                    })
            return self._tool_error("Tool execution failed or timed out. Do not report success.")
        finally:
            CURRENT_SESSION.reset(token)
            if task is not None:
                self._rpc_tasks.discard(task)

    @property
    def inflight_tools(self):
        return min(8, len(self._tool_operations) + self._native_tool_uncertain) if self.native_pause_enabled else len(self._rpc_tasks)

    async def _execute_admitted_tool(self, name, arguments, timeout):
        with measure(self.latency, "tool_execution", tool=name):
            async with asyncio.timeout(min(max(float(timeout), 0.1), 30)):
                return await self.tools[name].target(arguments)

    def _tool_operation_finished(self, task):
        self._tool_operations.discard(task)
        if not task.cancelled() and task.exception() is not None:
            self._native_tool_uncertain = min(8, self._native_tool_uncertain + 1)
            logger.warning("An admitted native tool completed with an error; its outcome must not be blindly retried")

    async def drain_native_tools(self):
        if self._tool_operations:
            # Each admitted operation has its own <=30s deadline. Provider loss is not rollback.
            await asyncio.gather(*(asyncio.shield(task) for task in tuple(self._tool_operations)), return_exceptions=True)

    async def pause_audio(self, pause_sink):
        if not self.native_pause_enabled:
            raise ValueError("Native pause is not enabled for this session.")
        self.ai_paused = True
        async with asyncio.timeout(8):
            async with self._capture_lock:
                self._input_buffer.clear()
                if self._source is not None:
                    self._source.clear_queue()
            async with self._output_lock:
                self._clear_native_startup_output()
                await pause_sink()

    async def _capture_audio(self, data: dict) -> None:
        async with self._capture_lock:
            await self._capture_audio_unlocked(data)

    async def _capture_audio_unlocked(self, data: dict) -> None:
        audio = data.get("audio")
        if not isinstance(audio, str):
            raise HumainError("Audio frames must contain base64-encoded PCM16.")
        try:
            pcm = base64.b64decode(audio, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise HumainError("Received an invalid base64 audio frame.") from exc
        if len(pcm) % 2:
            raise HumainError("PCM16 audio frames must contain complete 16-bit samples.")
        if len(pcm) > SAMPLE_RATE * 2:
            raise HumainError("Audio frames must not exceed one second of PCM16.")
        if not self._ready or self.transfer_active or self.ai_paused or self.native_startup_pending or self._source is None:
            return
        self._input_buffer.extend(pcm)
        while len(self._input_buffer) >= FRAME_BYTES:
            if self.transfer_active or self.ai_paused or self.native_startup_pending:
                self._input_buffer.clear()
                return
            frame = bytes(self._input_buffer[:FRAME_BYTES])
            del self._input_buffer[:FRAME_BYTES]
            await self._source.capture_frame(rtc.AudioFrame(frame, SAMPLE_RATE, 1, FRAME_SAMPLES))
            if self.latency is not None:
                self.latency.audio("input", frame)
            self.input_audio_frames += 1

    async def _request_language(self, language) -> None:
        error_message = "Could not update the voice language. Please ask the assistant to switch language or reconnect."
        if language not in ("ar", "en") or not self._ready or self._room is None or self.transfer_active or self.ai_paused:
            logger.warning("Voice language selection refused: invalid language or inactive AI session.")
            await self._send_to_client({"type": "session.warning", "warning": {
                "code": "language_update_failed", "message": error_message,
            }})
            return
        language_name = "Arabic" if language == "ar" else "English"
        request = (
            f"I explicitly selected {language_name} using the language button. "
            f"Please respond in {language_name} from now on until I explicitly request a different language."
        )
        try:
            # LiveKit's linked-participant text input carries an explicit user choice,
            # without recreating the room or discarding conversation history.
            async with asyncio.timeout(5):
                await self._room.local_participant.send_text(request, topic="lk.chat")
        except (rtc.StreamError, TimeoutError, ConnectionError) as error:
            logger.error("Voice language selection failed (%s)", type(error).__name__)
            await self._send_to_client({"type": "session.warning", "warning": {
                "code": "language_update_failed", "message": error_message,
            }})
            return
        self.language = language
        await self._send_to_client({"type": "session.language_requested", "language": language})

    async def _receive_messages(self) -> None:
        assert self._client_ws is not None
        async for message in self._client_ws:
            if message.type == aiohttp.WSMsgType.ERROR:
                raise HumainError("The client audio connection failed.")
            if message.type != aiohttp.WSMsgType.TEXT:
                raise HumainError("The voice connection expects JSON text messages.")
            try:
                data = json.loads(message.data)
            except json.JSONDecodeError as exc:
                raise HumainError("Invalid JSON in the voice connection.") from exc
            if not isinstance(data, dict):
                raise HumainError("Voice messages must be JSON objects.")
            if self._is_acs:
                if data.get("kind") != "AudioData":
                    logger.debug("Ignoring non-audio ACS message")
                    continue
                audio = data.get("audioData")
                if not isinstance(audio, dict):
                    raise HumainError("Invalid ACS audioData object.")
                participant_id = audio.get("participantRawID")
                if self.acs_input_participant_id is not None and participant_id != self.acs_input_participant_id:
                    continue
                if participant_id:
                    if self._customer_participant_id is None:
                        self._customer_participant_id = participant_id
                        self._participant_map[participant_id] = SpeakerRole.CUSTOMER
                    elif self.transfer_active and participant_id != self._customer_participant_id:
                        self._operator_participant_id = participant_id
                        self._participant_map[participant_id] = SpeakerRole.OPERATOR
                    self._current_audio_speaker = self._participant_map.get(participant_id)
                data = {"type": "input_audio_buffer.append", "audio": audio.get("data")}
            if data.get("type") == "input_audio_buffer.append":
                await self._capture_audio(data)
            elif data.get("type") == "session.update":
                raise HumainError("HUMAIN session settings are fixed at room creation. Restart to change voice.")
            elif data.get("type") == "session.language":
                await self._request_language(data.get("language"))
            else:
                logger.warning("Ignoring unsupported realtime client event")

    async def _receive_handshake(self) -> bool:
        assert self._client_ws is not None
        message = await self._client_ws.receive(timeout=15)
        if message.type in {aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.CLOSING}:
            return False
        if message.type != aiohttp.WSMsgType.TEXT:
            raise HumainError("Expected a voice session setup message.")
        data = json.loads(message.data)
        if not isinstance(data, dict):
            raise HumainError("Voice session setup must be a JSON object.")
        if self._is_acs:
            metadata = data.get("audioMetadata")
            if (
                data.get("kind") != "AudioMetadata"
                or not isinstance(metadata, dict)
                or metadata.get("sampleRate") != SAMPLE_RATE
                or metadata.get("channels") != 1
                or metadata.get("encoding", "PCM") != "PCM"
            ):
                raise HumainError("ACS must stream PCM16 audio at 24 kHz, mono.")
        else:
            session = data.get("session")
            if data.get("type") != "session.update" or not isinstance(session, dict):
                raise HumainError("Expected session.update before sending audio.")
            if session.get("voice") is not None:
                self.selected_voice = validate_voice(session["voice"])
            language = session.get("language", self.deployment.organization.defaultLanguage)
            if language not in ("en", "ar"):
                raise HumainError("Choose English or Arabic for the conversation.")
            self.language = language
            self.intro_message = self.deployment.organization.greeting[language]
        initial_language = "العربية" if self.language == "ar" else "الإنجليزية"
        self.system_message += f"""

## Final response-language policy
Initial response language selected by the interface: {self.language}.
لغة الرد عند بدء هذه المحادثة هي {initial_language}.
أنت مساعد خدمة العملاء. لا تستنتج لغة الرد من لغة السؤال.
إذا كانت لغة الرد العربية وسأل المستخدم بالإنجليزية، أجب بالعربية.
لا تنتقل للإنجليزية إلا إذا طلب المستخدم ذلك صراحة أو اختارها بزر اللغة.
بعد طلب تغيير اللغة، استمر باللغة المختارة في جميع الردود التالية حتى طلب صريح جديد.
الكلمات الإنجليزية وأسماء الخدمات والنصوص المقتبسة ليست طلباً لتغيير اللغة.
على سبيل المثال: السؤال "How can I get help?" وحده لا يغيّر العربية إلى الإنجليزية.
أما "Please speak English from now on" فهو طلب صريح ويغيّر لغة الرد للإنجليزية.
An English-language question is NOT permission to switch from Arabic. Honor only
an explicit language request or UI choice, and retain the last explicit choice.
These response-language rules apply equally to embedded-knowledge answers, tool
acknowledgments and follow-up questions, regardless of the source text's language.
"""
        if self.native_audio_sink is not None and "select_response_language" in self.tools:
            self.system_message += """
## Native response-language changes
The initial language above is only the starting language, not a permanent restriction.
Before the caller speaks, greet in the initial language and do not request a language change.
Call select_response_language for every explicit Arabic/English change and wait for success.
The successful tool result's response_language is then the current response language.
If it is English, answer in English even when the caller requested the change in Arabic.
If it is Arabic, answer in Arabic even when the caller requested the change in English.
Answer any question bundled with the change in the NEW language, not the old language.
Do not speak a new-language acknowledgement before the tool succeeds.
"""
        return True

    async def forward_messages(self, ws: web.WebSocketResponse, is_acs_audio_stream: bool):
        self._client_ws = ws
        self._is_acs = is_acs_audio_stream
        self._failure = asyncio.get_running_loop().create_future()
        if self.latency is not None:
            self.latency.mark("acs_media_websocket_open")
        try:
            if not self.config.enabled:
                raise HumainError("HUMAIN voice is not enabled yet. Configure the server's HUMAIN credentials first.")
            if not await self._receive_handshake():
                return
            if self.latency is not None:
                self.latency.mark("acs_media_metadata_received")
            if self.agent_assist_client and self.agent_assist_client.enabled:
                self.agent_assist_session = self.agent_assist_client.create_session()
                self.agent_assist_session.channel = "voice"
            self._room = rtc.Room()
            self._register_room_handlers()
            receiver = self._start_task(self._receive_messages())
            self._start_task(self._connect())
            await asyncio.wait({receiver, self._failure}, return_when=asyncio.FIRST_COMPLETED)
            if self._failure.done():
                raise HumainError(self._failure.result())
        except (HumainError, ValueError, TimeoutError, ConnectionError) as exc:
            message = str(exc) if isinstance(exc, (HumainError, ValueError)) else "Voice session setup failed."
            logger.error("HUMAIN session ended: %s", message)
            if not ws.closed and not self._is_acs:
                await ws.send_json({"type": "error", "error": {"message": message}, "fatal": True})
            if not ws.closed:
                await ws.close(code=1011)
        finally:
            self._closing = True
            self._ready = False
            pending = list(self._tasks | self._rpc_tasks)
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            self._clear_native_startup_output()
            await self.drain_native_tools()
            if self._room is not None:
                try:
                    async with asyncio.timeout(10):
                        await self._room.disconnect()
                except (RuntimeError, TimeoutError):
                    logger.error("HUMAIN room disconnect failed")
            if self._source is not None:
                try:
                    await self._source.aclose()
                except RuntimeError:
                    logger.error("HUMAIN audio source cleanup failed")
            if self._transcript_tasks:
                try:
                    async with asyncio.timeout(5):
                        await asyncio.gather(*list(self._transcript_tasks))
                except TimeoutError:
                    logger.warning("Timed out draining HUMAIN transcript delivery on disconnect")
            if self.agent_assist_client and self.agent_assist_session:
                try:
                    async with asyncio.timeout(5):
                        await self.agent_assist_client.complete_session(self.agent_assist_session)
                except TimeoutError:
                    logger.error("Timed out completing the assist session after HUMAIN disconnected")
            if not self._failure.done():
                self._failure.cancel()
            if not ws.closed:
                await ws.close()
            self._room = None
            self._source = None
            self._client_ws = None
