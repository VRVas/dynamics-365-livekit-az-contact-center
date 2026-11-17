import asyncio
import base64
import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from livekit import rtc

from backend.agent_assist import AgentAssistSession
from backend.humain import AGENT_NAME, HumainConfig, HumainError, HumainToken, ignored_metadata_fields, mint_token, room_metadata
from backend.rtmt import FRAME_BYTES, RPC_MAX_BYTES, RTMiddleTier, SpeakerRole
from backend.tools.tools import CURRENT_SESSION, Tool, ToolResult, ToolResultDirection


def config(**overrides):
    return HumainConfig(authorization="TestScheme synthetic-test-credential", **overrides)


def tool(name="lookup", target=None):
    return Tool(
        target=target or AsyncMock(return_value=ToolResult('{"success": true}', ToolResultDirection.TO_SERVER)),
        schema={
            "type": "function",
            "name": name,
            "description": "Look up a test item.",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    )


def invocation(payload, caller="agent", timeout=1):
    return rtc.RpcInvocationData(
        request_id="rpc-1", caller_identity=caller,
        payload=json.dumps(payload), response_timeout=timeout,
    )


class ConfigTests(unittest.TestCase):
    def test_ignored_metadata_diagnostics_never_expose_values(self):
        for value, expected in (
            ({}, ()),
            ([], ()),
            ({"voice": "private-value", "system_prompt": "private-prompt"}, ("system_prompt", "voice")),
            (["language", "target_dialect"], ("language", "target_dialect")),
            ([{"key": "voice", "value": "private-value"}], ("voice",)),
            ({"client_tools.participant_identity": "private-identity"}, ("client_tools",)),
            ({"private-key-content": "private-value"}, ("other",)),
            ("private-freeform-report", ("other",)),
            ([{"unexpected": "private-value"}], ("other",)),
        ):
            with self.subTest(value=value):
                self.assertEqual(ignored_metadata_fields(value), expected)

    def test_explicitly_disabled_voice_can_start_without_credentials(self):
        with patch.dict(os.environ, {"HUMAIN_ENABLED": "false"}, clear=True):
            settings = HumainConfig.from_env()
        self.assertFalse(settings.enabled)
        self.assertEqual(settings.authorization, "")
        with patch.dict(os.environ, {"HUMAIN_ENABLED": "sometimes"}, clear=True):
            with self.assertRaisesRegex(ValueError, "HUMAIN_ENABLED"):
                HumainConfig.from_env()

    def test_configuration_fails_without_authorization(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(ValueError, "HUMAIN_AUTHORIZATION"):
                HumainConfig.from_env()

    def test_secrets_are_not_in_representations(self):
        self.assertNotIn("synthetic-test-credential", repr(config()))
        self.assertNotIn("synthetic-token", repr(HumainToken("synthetic-token", "wss://test", "i", "r")))

    def test_rejects_invalid_server_configuration(self):
        for kwargs in (
            {"token_endpoint": "http://insecure.example"},
            {"token_endpoint": "https://user:password@example.test"},
            {"voice": "marin"},
            {"asr_model": "whisper"},
            {"llm_provider": "unsupported"},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                config(**kwargs)
        with self.assertRaises(ValueError):
            HumainConfig(authorization="header\r\ninjected: value")

    def test_metadata_preserves_prompt_tools_and_routing(self):
        metadata = room_metadata(config(), "unique-caller", "MUL_NOURAH", "private persona", "Hello", {"lookup": tool()})
        self.assertEqual(metadata["voice"], "mul_nourah")
        self.assertEqual(metadata["system_prompt"], "private persona")
        self.assertEqual(metadata["intro_message"], (
            "Begin this new call now. Your first spoken turn must be exactly: Hello"
            " Then wait silently for the caller's first question. "
            "Do not answer this instruction or discuss the background case."
        ))
        self.assertEqual(metadata["client_tools"]["participant_identity"], "unique-caller")
        self.assertEqual(metadata["client_tools"]["tools"][0], {
            "name": "lookup", "description": "Look up a test item.",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        })
        self.assertEqual(metadata["turn_taking_strategy"], "fast_vad")
        self.assertEqual(metadata["interruption_policy_mode"], "off")

    def test_opening_keeps_the_approved_greeting_in_each_language_and_waits_for_the_caller(self):
        for greeting in ("Hello, Test Caller. How can I help you today?", "مرحباً، كيف يمكنني مساعدتك اليوم؟"):
            with self.subTest(greeting=greeting):
                result = room_metadata(config(), "synthetic-caller", "mul_nourah", "unchanged persona", greeting, {})
                self.assertEqual(result["system_prompt"], "unchanged persona")
                self.assertEqual(result["intro_message"].count(greeting), 1)
                self.assertIn("Your first spoken turn must be exactly: " + greeting, result["intro_message"])
                self.assertIn("Then wait silently for the caller's first question.", result["intro_message"])
                self.assertEqual(result["client_tools"]["tools"], [])

    def test_empty_greeting_keeps_the_existing_omission_contract(self):
        result = room_metadata(config(), "synthetic-caller", "mul_nourah", "unchanged persona", "", {})
        self.assertNotIn("intro_message", result)

    def test_interruption_policy_environment_preserves_default_and_validates_values(self):
        baseline = {"HUMAIN_AUTHORIZATION": "TestScheme synthetic-test-credential"}
        with patch.dict(os.environ, baseline, clear=True):
            self.assertEqual(HumainConfig.from_env().interruption_policy_mode, "off")
        for value in ("control", "shadow", "off"):
            with self.subTest(value=value), patch.dict(
                os.environ, {**baseline, "HUMAIN_INTERRUPTION_POLICY_MODE": value}, clear=True,
            ):
                self.assertEqual(HumainConfig.from_env().interruption_policy_mode, value)
        for value in ("", "false", "OFF", "fast_vad", "off\n"):
            with self.subTest(value=value), patch.dict(
                os.environ, {**baseline, "HUMAIN_INTERRUPTION_POLICY_MODE": value}, clear=True,
            ), self.assertRaisesRegex(ValueError, "HUMAIN_INTERRUPTION_POLICY_MODE"):
                HumainConfig.from_env()
        for value in (None, False, []):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "HUMAIN_INTERRUPTION_POLICY_MODE"):
                config(interruption_policy_mode=value)

    def test_policy_b_changes_only_interruption_metadata_and_survives_session_cloning(self):
        arguments = ("unique-caller", "mul_nourah", "private persona", "Hello", {"lookup": tool()})
        baseline = room_metadata(config(interruption_policy_mode="control", turn_taking_strategy="livekit_v1_mini"), *arguments)
        template = RTMiddleTier(config(interruption_policy_mode="off", turn_taking_strategy="livekit_v1_mini"))
        for session in (template, template.clone_for_session(), template.clone_for_session()):
            actual = room_metadata(session.config, *arguments)
            self.assertEqual(actual, {**baseline, "interruption_policy_mode": "off"})
            self.assertEqual(actual["turn_taking_strategy"], "livekit_v1_mini")
        self.assertEqual(config().interruption_policy_mode, "off")

    def test_turn_strategy_environment_preserves_default_and_validates_values(self):
        baseline = {"HUMAIN_AUTHORIZATION": "TestScheme synthetic-test-credential"}
        with patch.dict(os.environ, baseline, clear=True):
            self.assertEqual(HumainConfig.from_env().turn_taking_strategy, "fast_vad")
        for value in ("livekit_v1_mini", "fast_vad"):
            with self.subTest(value=value), patch.dict(
                os.environ, {**baseline, "HUMAIN_TURN_TAKING_STRATEGY": value}, clear=True,
            ):
                self.assertEqual(HumainConfig.from_env().turn_taking_strategy, value)
        for value in ("", "off", "FAST_VAD", "server_vad", "fast_vad\n"):
            with self.subTest(value=value), patch.dict(
                os.environ, {**baseline, "HUMAIN_TURN_TAKING_STRATEGY": value}, clear=True,
            ), self.assertRaisesRegex(ValueError, "HUMAIN_TURN_TAKING_STRATEGY"):
                HumainConfig.from_env()
        for value in (None, False, []):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "HUMAIN_TURN_TAKING_STRATEGY"):
                config(turn_taking_strategy=value)

    def test_accepted_pair_changes_only_turn_policy_and_survives_cloning(self):
        arguments = ("unique-caller", "mul_nourah", "private persona", "Hello", {"lookup": tool()})
        baseline = room_metadata(config(interruption_policy_mode="control", turn_taking_strategy="livekit_v1_mini"), *arguments)
        template = RTMiddleTier(config())
        for session in (template, template.clone_for_session(), template.clone_for_session()):
            actual = room_metadata(session.config, *arguments)
            self.assertEqual(actual, {
                **baseline, "interruption_policy_mode": "off", "turn_taking_strategy": "fast_vad",
            })
        self.assertEqual(config().turn_taking_strategy, "fast_vad")

    def test_explicit_legacy_pair_remains_available_for_rollback(self):
        with patch.dict(os.environ, {
            "HUMAIN_AUTHORIZATION": "TestScheme synthetic-test-credential",
            "HUMAIN_INTERRUPTION_POLICY_MODE": "control",
            "HUMAIN_TURN_TAKING_STRATEGY": "livekit_v1_mini",
        }, clear=True):
            settings = HumainConfig.from_env()
        metadata = room_metadata(settings, "rollback-caller", settings.voice, "", "", {})
        self.assertEqual(metadata["interruption_policy_mode"], "control")
        self.assertEqual(metadata["turn_taking_strategy"], "livekit_v1_mini")

    def test_tool_contract_limits(self):
        valid = {f"tool_{i}": tool(f"tool_{i}") for i in range(16)}
        self.assertEqual(len(room_metadata(config(), "i", "mul_sara", "", "", valid)["client_tools"]["tools"]), 16)
        valid["extra"] = tool("extra")
        with self.assertRaisesRegex(ValueError, "16"):
            room_metadata(config(), "i", "mul_sara", "", "", valid)
        for name in ("client_tool_call", "bad name", "n" * 65):
            with self.subTest(name=name), self.assertRaises(ValueError):
                room_metadata(config(), "i", "mul_sara", "", "", {name: tool(name)})
        with self.assertRaisesRegex(ValueError, "mismatch"):
            room_metadata(config(), "i", "mul_sara", "", "", {"wrong": tool()})
        declaration = tool()
        declaration.schema["description"] = "x" * 2001
        with self.assertRaises(ValueError):
            room_metadata(config(), "i", "mul_sara", "", "", {"lookup": declaration})

    def test_clones_never_reuse_room_or_mutable_state(self):
        template = RTMiddleTier(config())
        template.system_message = "persona"
        template.tools = {"lookup": tool()}
        first, second = template.clone_for_session(), template.clone_for_session()
        self.assertNotEqual(first._identity, second._identity)
        self.assertNotEqual(first._room_name, second._room_name)
        self.assertIs(first.tools, template.tools)
        self.assertEqual(second.system_message, "persona")
        first.transfer_active = True
        first._transferred_calls.add("call-1")
        first.transcript_buffer.mark_completed("one", "hello")
        self.assertFalse(second.transfer_active)
        self.assertFalse(second._transferred_calls)
        self.assertFalse(second.transcript_buffer.segments)


class TokenTests(unittest.IsolatedAsyncioTestCase):
    async def test_shared_token_transport_preserves_headers_and_is_not_closed_per_call(self):
        response = SimpleNamespace(status=200, json=AsyncMock(return_value={
            "token": "synthetic-token", "url": "wss://livekit.example",
            "identity": "caller", "roomName": "fresh-room",
        }))
        http = MagicMock(closed=False)
        http.post.return_value.__aenter__ = AsyncMock(return_value=response)
        with patch("backend.humain.aiohttp.ClientSession") as factory:
            result = await mint_token(config(), "caller", "fresh-room", {}, http_session=http)
        self.assertEqual(result.identity, "caller")
        factory.assert_not_called()
        http.close.assert_not_called()
        self.assertEqual(http.post.call_args.kwargs["headers"]["Authorization"], config().authorization)
        self.assertFalse(http.post.call_args.kwargs["allow_redirects"])

    async def test_closed_shared_token_transport_fails_explicitly(self):
        http = MagicMock(closed=True)
        with self.assertRaises(HumainError):
            await mint_token(config(), "caller", "fresh-room", {}, http_session=http)
        http.post.assert_not_called()

    async def request_token(self, status=200, payload=None, json_error=None):
        response = SimpleNamespace(
            status=status,
            json=AsyncMock(
                return_value=payload if payload is not None else {
                    "token": "synthetic-token", "url": "wss://livekit.example",
                    "identity": "caller", "roomName": "fresh-room",
                },
                side_effect=json_error,
            ),
        )
        http = MagicMock()
        http.post.return_value.__aenter__ = AsyncMock(return_value=response)
        with patch("backend.humain.aiohttp.ClientSession") as factory:
            factory.return_value.__aenter__.return_value = http
            result = await mint_token(config(), "caller", "fresh-room", {"voice": "mul_nourah"})
        return result, http

    async def test_server_side_token_request(self):
        credentials, http = await self.request_token()
        self.assertEqual(credentials.url, "wss://livekit.example")
        kwargs = http.post.call_args.kwargs
        self.assertFalse(kwargs["allow_redirects"])
        self.assertEqual(kwargs["headers"]["Authorization"], config().authorization)
        self.assertEqual(kwargs["json"], {
            "identity": "caller", "roomName": "fresh-room", "agentName": AGENT_NAME,
            "roomMetadata": {"voice": "mul_nourah"},
        })
        created, _ = await self.request_token(status=201)
        self.assertEqual(created.identity, "caller")

    async def test_accepts_the_live_service_https_media_url(self):
        credentials, _ = await self.request_token(payload={
            "token": "synthetic-token", "url": "https://livekit.sanad.ink",
            "identity": "caller", "roomName": "fresh-room",
        })
        self.assertEqual(credentials.url, "https://livekit.sanad.ink")

    async def test_upstream_errors_are_explicit(self):
        for status in (401, 403, 429, 500, 302):
            with self.subTest(status=status), self.assertRaises(HumainError):
                await self.request_token(status=status)
        with self.assertRaises(HumainError):
            await self.request_token(json_error=ValueError("bad JSON"))

    async def test_rejects_invalid_or_misrouted_credentials(self):
        valid = {"token": "t", "url": "wss://test.example", "identity": "caller", "roomName": "fresh-room"}
        for payload in (
            [], {}, {**valid, "token": ""}, {**valid, "url": "ws://insecure.example"},
            {**valid, "identity": "wrong"}, {**valid, "roomName": "reused"},
        ):
            with self.subTest(payload=payload), self.assertRaises(HumainError):
                await self.request_token(payload=payload)


class SessionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.session = RTMiddleTier(config())
        self.session._agents.add("agent")
        self.session._client_ws = SimpleNamespace(closed=False, send_json=AsyncMock())
        self.session._source = SimpleNamespace(
            capture_frame=AsyncMock(), clear_queue=MagicMock(), aclose=AsyncMock(),
        )
        self.session._ready = True

    async def test_pcm16_frames_preserve_samples_and_carry(self):
        pcm = b"\x12\x34" * 4096
        await self.session._capture_audio({"audio": base64.b64encode(pcm).decode()})
        frames = [call.args[0] for call in self.session._source.capture_frame.call_args_list]
        self.assertEqual(len(frames), 8)
        self.assertEqual(b"".join(frame.data.tobytes() for frame in frames), pcm[:8 * FRAME_BYTES])
        self.assertTrue(all(frame.sample_rate == 24000 and frame.num_channels == 1 for frame in frames))
        self.assertTrue(all(frame.samples_per_channel == 480 for frame in frames))
        self.assertEqual(bytes(self.session._input_buffer), pcm[8 * FRAME_BYTES:])
        await self.session._capture_audio({"audio": base64.b64encode(b"\0\0" * 224).decode()})
        self.assertEqual(self.session._source.capture_frame.await_count, 9)
        self.assertEqual(self.session._input_buffer, b"")

    async def test_rejects_invalid_audio(self):
        for audio in ("not base64!", "AA==", None, 12, base64.b64encode(b"\0" * 48002).decode()):
            with self.subTest(audio=audio), self.assertRaises(HumainError):
                await self.session._capture_audio({"audio": audio})

    async def test_native_livekit_audio_source_accepts_bridge_frames(self):
        source = rtc.AudioSource(24000, 1, queue_size_ms=200)
        try:
            await source.capture_frame(rtc.AudioFrame(b"\1\0" * 480, 24000, 1, 480))
            source.clear_queue()
            self.assertEqual(source.queued_duration, 0)
        finally:
            await source.aclose()

    async def test_audio_not_published_before_ready_or_after_handover(self):
        audio = {"audio": base64.b64encode(b"\1\0" * 480).decode()}
        self.session._ready = False
        await self.session._capture_audio(audio)
        self.session._ready = True
        self.session.transfer_active = True
        await self.session._capture_audio(audio)
        self.session._source.capture_frame.assert_not_awaited()

    async def test_acs_metadata_warning_preserves_the_media_stream(self):
        self.session._is_acs = True
        self.session._failure = asyncio.get_running_loop().create_future()
        with self.assertLogs("rtmt", level="WARNING") as captured:
            self.session._check_metadata(json.dumps({
                "effective": {"ignored_metadata": {"target_dialect": "private-value"}},
            }))
        await asyncio.gather(*list(self.session._tasks))
        self.assertFalse(self.session._failure.done())
        self.assertTrue(self.session._ready)
        self.assertIn("target_dialect", "\n".join(captured.output))
        self.assertNotIn("private-value", "\n".join(captured.output))
        self.session._client_ws.send_json.assert_not_awaited()
        await self.session._capture_audio({"audio": base64.b64encode(b"\1\0" * 480).decode()})
        self.session._source.capture_frame.assert_awaited_once()
        self.session._failure.cancel()

    async def test_server_tool_results_and_validation(self):
        target = AsyncMock(return_value=ToolResult('{"success": true, "value": 42}', ToolResultDirection.TO_SERVER))
        self.session.tools = {"lookup": tool(target=target)}
        result = json.loads(await self.session._handle_tool_call(invocation({"tool": "lookup", "arguments": {}})))
        self.assertEqual(result, {"result": {"success": True, "value": 42}})
        for payload, caller in (
            ({"tool": "lookup", "arguments": {}}, "stranger"),
            ({"tool": "absent", "arguments": {}}, "agent"),
            ({"tool": "lookup", "arguments": []}, "agent"),
            ([], "agent"),
        ):
            with self.subTest(payload=payload, caller=caller):
                self.assertFalse(json.loads(await self.session._handle_tool_call(invocation(payload, caller)))["ok"])
        malformed = invocation({})
        malformed.payload = "{"
        self.assertFalse(json.loads(await self.session._handle_tool_call(malformed))["ok"])
        self.assertEqual(target.await_count, 1)

    async def test_tool_failure_timeout_and_utf8_limit(self):
        target = AsyncMock(return_value=ToolResult('{"success": false, "error": "unavailable"}', ToolResultDirection.TO_SERVER))
        self.session.tools = {"lookup": tool(target=target)}
        call = invocation({"tool": "lookup", "arguments": {}})
        failure = json.loads(await self.session._handle_tool_call(call))
        self.assertEqual(failure, {"ok": False, "error": "unavailable"})
        target.side_effect = RuntimeError("internal detail")
        self.assertFalse(json.loads(await self.session._handle_tool_call(call))["ok"])
        target.side_effect = None
        # The limit is on UTF-8 bytes of the whole envelope, not Python characters.
        overhead = len(json.dumps({"result": ""}).encode())
        target.return_value = ToolResult("x" * (RPC_MAX_BYTES - overhead), ToolResultDirection.TO_SERVER)
        response = await self.session._handle_tool_call(call)
        self.assertEqual(len(response.encode()), RPC_MAX_BYTES)
        target.return_value = ToolResult("\u0646" * (RPC_MAX_BYTES // 2), ToolResultDirection.TO_SERVER)
        self.assertFalse(json.loads(await self.session._handle_tool_call(call))["ok"])

        async def slow(args):
            await asyncio.Event().wait()

        target.side_effect = slow
        self.assertFalse(json.loads(await self.session._handle_tool_call(
            invocation({"tool": "lookup", "arguments": {}}, timeout=0.01)
        ))["ok"])
        self.assertIsNone(CURRENT_SESSION.get())

    async def test_concurrent_rpc_context_and_handover_mute_are_isolated(self):
        second = self.session.clone_for_session()
        second._agents.add("agent")
        self.session.current_call_connection_id = "first"
        second.current_call_connection_id = "second"
        arrived = asyncio.Event()

        async def handover(args):
            current = CURRENT_SESSION.get()
            if current.current_call_connection_id == "first":
                await arrived.wait()
                current.transfer_active = True
            else:
                arrived.set()
                await asyncio.sleep(0)
            return ToolResult(json.dumps({"call": CURRENT_SESSION.get().current_call_connection_id}), ToolResultDirection.TO_SERVER)

        self.session.tools = second.tools = {"lookup": tool(target=handover)}
        self.session._is_acs = True
        self.session._input_buffer.extend(b"\0\0")
        call = invocation({"tool": "lookup", "arguments": {}})
        responses = await asyncio.gather(
            self.session._handle_tool_call(call), second._handle_tool_call(call)
        )
        self.assertEqual([json.loads(r)["result"]["call"] for r in responses], ["first", "second"])
        self.assertTrue(self.session.transfer_active)
        self.assertFalse(second.transfer_active)
        self.session._source.clear_queue.assert_called_once()
        self.assertFalse(self.session._input_buffer)
        self.session._client_ws.send_json.assert_awaited_with({"kind": "StopAudio", "audioData": None, "stopAudio": {}})
        await self.session._send_to_client({"type": "response.output_audio.delta", "delta": "AAAA"})
        self.assertEqual(self.session._client_ws.send_json.await_count, 1)
        self.assertIsNone(CURRENT_SESSION.get())

    async def test_transcripts_are_final_deduplicated_and_attributed(self):
        assist = SimpleNamespace(send_message=AsyncMock())
        self.session.agent_assist_client = assist
        self.session.agent_assist_session = AgentAssistSession("call-test", "voice")
        self.session._mic_track_sid = "mic"
        reader = SimpleNamespace(
            info=SimpleNamespace(attributes={"lk.segment_id": "user-1", "lk.transcription_final": "false"}, stream_id="stream"),
            read_all=AsyncMock(return_value="hello"),
        )
        await self.session._read_transcription(reader, self.session._identity)
        assist.send_message.assert_not_awaited()
        reader.info.attributes["lk.transcription_final"] = "true"
        await self.session._read_transcription(reader, self.session._identity)
        await self.session._record_transcript(SpeakerRole.CUSTOMER, "user-1", "hello")
        await self.session._record_transcript(SpeakerRole.AI_AGENT, "agent-1", "welcome")
        self.assertEqual(assist.send_message.await_count, 2)
        self.assertEqual([s.speaker_role for s in self.session.transcript_buffer.segments], ["customer", "ai_agent"])
        self.assertEqual(self.session._speaker_role("agent", {"lk.transcribed_track_id": "mic"}), "customer")
        self.session.transfer_active = True
        await self.session._record_transcript(SpeakerRole.AI_AGENT, "muted", "not spoken")
        self.assertEqual(assist.send_message.await_count, 2)

    async def test_paced_output_ignores_silence_and_uses_acs_envelope(self):
        pcm = b"\1\0" * 480

        class Stream:
            aclose = AsyncMock()

            async def __aiter__(self):
                for data in (b"\0\0" * 480, pcm):
                    yield SimpleNamespace(frame=rtc.AudioFrame(data, 24000, 1, 480))

        stream = Stream()
        self.session._is_acs = True
        with patch("backend.rtmt.rtc.AudioStream", return_value=stream) as factory:
            await self.session._relay_audio(MagicMock())
        self.assertEqual(factory.call_args.kwargs["sample_rate"], 24000)
        self.assertEqual(factory.call_args.kwargs["capacity"], 10)
        self.session._client_ws.send_json.assert_awaited_once_with({
            "kind": "AudioData", "audioData": {"data": base64.b64encode(pcm).decode()},
        })
        stream.aclose.assert_awaited_once()


class FakeRoom:
    def __init__(self):
        self.handlers = {}
        self.text_handlers = {}
        self.metadata = ""
        self.remote_participants = {
            "agent": SimpleNamespace(identity="agent", kind=rtc.ParticipantKind.PARTICIPANT_KIND_AGENT)
        }
        self.local_participant = SimpleNamespace(
            register_rpc_method=MagicMock(),
            publish_track=AsyncMock(return_value=SimpleNamespace(sid="mic")),
            send_text=AsyncMock(),
        )
        self.connect = AsyncMock()
        self.disconnect = AsyncMock()

    def on(self, event, callback=None):
        def register(handler):
            self.handlers[event] = handler
            return handler
        return register(callback) if callback is not None else register

    def register_text_stream_handler(self, topic, callback):
        self.text_handlers[topic] = callback


class WebSocketTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from runtime_fixtures import enabled_deployment
        self.room = FakeRoom()
        self.source = SimpleNamespace(
            capture_frame=AsyncMock(), aclose=AsyncMock(), clear_queue=MagicMock()
        )
        self.session = RTMiddleTier(config(), enabled_deployment())
        self.session.system_message = "server persona"
        self.done = asyncio.Event()
        self.tokens = []

        async def mint(settings, identity, name, metadata):
            self.tokens.append((identity, name, metadata))
            return HumainToken("synthetic-token", "wss://media.example", identity, name)

        for target, replacement in (
            ("backend.rtmt.rtc.Room", MagicMock(return_value=self.room)),
            ("backend.rtmt.rtc.AudioSource", MagicMock(return_value=self.source)),
            ("backend.rtmt.rtc.LocalAudioTrack.create_audio_track", MagicMock(return_value=MagicMock())),
            ("backend.rtmt.mint_token", mint),
        ):
            patcher = patch(target, replacement)
            patcher.start()
            self.addCleanup(patcher.stop)

        async def websocket(request):
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            try:
                await self.session.forward_messages(ws, request.path.endswith("acs"))
            finally:
                self.done.set()
            return ws

        app = web.Application()
        app.router.add_get("/realtime", websocket)
        app.router.add_get("/realtime-acs", websocket)
        self.client = TestClient(TestServer(app))
        await self.client.start_server()
        self.addAsyncCleanup(self.client.close)

    async def test_browser_protocol_and_disconnect_cleanup(self):
        ws = await self.client.ws_connect("/realtime")
        await ws.send_json({"type": "session.update", "session": {"voice": "mul_sara", "instructions": "untrusted"}})
        ready = await ws.receive_json(timeout=2)
        self.assertEqual(ready, {"type": "session.updated", "session": {"provider": "humain", "voice": "mul_sara"}})
        identity, name, metadata = self.tokens[0]
        self.assertEqual(metadata["client_tools"]["participant_identity"], identity)
        self.assertTrue(metadata["system_prompt"].startswith("server persona"))
        self.assertIn("selected by the interface: ar", metadata["system_prompt"])
        self.assertNotIn("untrusted", metadata["system_prompt"])
        self.assertEqual(metadata["intro_message"], (
            "Begin this new call now. Your first spoken turn must be exactly: "
            + self.session.deployment.organization.greeting["ar"]
            + " Then wait silently for the caller's first question. "
            "Do not answer this instruction or discuss the background case."
        ))
        self.assertTrue(name.startswith("moacc-session-"))
        self.assertNotIn("token", ready["session"])
        self.room.connect.assert_awaited_once_with("wss://media.example", "synthetic-token")
        self.room.local_participant.register_rpc_method.assert_called_once()
        self.assertIn("lk.transcription", self.room.text_handlers)
        await ws.close()
        await asyncio.wait_for(self.done.wait(), 2)
        self.room.disconnect.assert_awaited_once()
        self.source.aclose.assert_awaited_once()
        self.assertFalse(self.session._tasks)

    async def test_explicit_english_handshake_changes_greeting_and_initial_policy(self):
        ws = await self.client.ws_connect("/realtime")
        await ws.send_json({"type": "session.update", "session": {"language": "en"}})
        await ws.receive_json(timeout=2)
        self.assertEqual(self.session.language, "en")
        self.assertIn("selected by the interface: en", self.tokens[0][2]["system_prompt"])
        self.assertIn("Your first spoken turn must be exactly: Hello", self.tokens[0][2]["intro_message"])
        await ws.close()
        await asyncio.wait_for(self.done.wait(), 2)

    async def test_ui_language_choice_is_forwarded_without_restarting_the_call(self):
        ws = await self.client.ws_connect("/realtime")
        await ws.send_json({"type": "session.update", "session": {}})
        await ws.receive_json(timeout=2)
        for language, name in (("en", "English"), ("ar", "Arabic")):
            await ws.send_json({"type": "session.language", "language": language})
            response = await ws.receive_json(timeout=2)
            self.assertEqual(response, {"type": "session.language_requested", "language": language})
            call = self.room.local_participant.send_text.call_args
            self.assertIn(f"explicitly selected {name}", call.args[0])
            self.assertEqual(call.kwargs, {"topic": "lk.chat"})
            self.assertEqual(self.session.language, language)
        self.assertEqual(len(self.tokens), 1)
        self.room.disconnect.assert_not_awaited()
        await ws.close()
        await asyncio.wait_for(self.done.wait(), 2)

    async def test_failed_language_switch_warns_without_losing_the_active_call(self):
        self.room.local_participant.send_text.side_effect = ConnectionError("Provider unavailable")
        ws = await self.client.ws_connect("/realtime")
        await ws.send_json({"type": "session.update", "session": {}})
        await ws.receive_json(timeout=2)
        await ws.send_json({"type": "session.language", "language": "en"})
        notice = await ws.receive_json(timeout=2)
        self.assertEqual(notice["warning"]["code"], "language_update_failed")
        self.assertEqual(self.session.language, "ar")
        self.room.disconnect.assert_not_awaited()
        self.assertFalse(ws.closed)
        await ws.close()
        await asyncio.wait_for(self.done.wait(), 2)

    async def test_invalid_voice_fails_without_creating_room(self):
        ws = await self.client.ws_connect("/realtime")
        await ws.send_json({"type": "session.update", "session": {"voice": "marin"}})
        error = await ws.receive_json(timeout=2)
        self.assertEqual(error["type"], "error")
        self.assertTrue(error["fatal"])
        self.assertFalse(self.tokens)
        await ws.close()

    async def test_acs_metadata_uses_server_voice(self):
        self.session.selected_voice = "mul_abdullah"
        ws = await self.client.ws_connect("/realtime-acs")
        await ws.send_json({"kind": "AudioMetadata", "audioMetadata": {"encoding": "PCM", "sampleRate": 24000, "channels": 1}})
        frame_received = asyncio.Event()
        self.source.capture_frame.side_effect = lambda frame: frame_received.set()
        # The room publication completes before the test sends its first PCM frame.
        async with asyncio.timeout(2):
            while not self.session._ready:
                await asyncio.sleep(0.001)
        self.assertEqual(self.tokens[0][2]["voice"], "mul_abdullah")
        await ws.send_json({
            "kind": "AudioData",
            "audioData": {"data": base64.b64encode(b"\1\0" * 480).decode(), "participantRawID": "customer"},
        })
        await asyncio.wait_for(frame_received.wait(), 2)
        self.assertEqual(self.session._participant_map, {"customer": "customer"})
        await ws.close()
        await asyncio.wait_for(self.done.wait(), 2)

    async def test_mid_call_language_metadata_warning_does_not_disconnect(self):
        ws = await self.client.ws_connect("/realtime")
        await ws.send_json({"type": "session.update", "session": {}})
        await ws.receive_json(timeout=2)
        self.room.handlers["room_metadata_changed"](json.dumps({
            "effective": {"ignored_metadata": {"target_dialect": "requested-dialect"}},
        }))
        notice = await ws.receive_json(timeout=2)
        self.assertEqual(notice["type"], "session.warning")
        self.assertEqual(notice["warning"]["ignored_fields"], ["target_dialect"])
        self.assertNotIn("requested-dialect", json.dumps(notice))
        self.assertFalse(self.session._failure.done())
        self.assertFalse(ws.closed)
        received = asyncio.Event()
        self.source.capture_frame.side_effect = lambda frame: received.set()
        await ws.send_json({
            "type": "input_audio_buffer.append", "audio": base64.b64encode(b"\1\0" * 480).decode(),
        })
        await asyncio.wait_for(received.wait(), 2)
        await self.session._send_to_client({"type": "response.output_audio.delta", "delta": "AQABAA=="})
        self.assertEqual((await ws.receive_json(timeout=2))["type"], "response.output_audio.delta")
        self.room.disconnect.assert_not_awaited()
        await ws.close()
        await asyncio.wait_for(self.done.wait(), 2)

    async def test_ignored_initial_settings_warn_without_preventing_connection(self):
        self.room.metadata = json.dumps({"effective": {"ignored_metadata": ["client_tools"]}})
        ws = await self.client.ws_connect("/realtime")
        await ws.send_json({"type": "session.update", "session": {}})
        messages = [await ws.receive_json(timeout=2), await ws.receive_json(timeout=2)]
        by_type = {message["type"]: message for message in messages}
        self.assertEqual(set(by_type), {"session.warning", "session.updated"})
        self.assertEqual(by_type["session.warning"]["warning"]["ignored_fields"], ["client_tools"])
        self.assertTrue(self.session._ready)
        self.assertFalse(self.session._failure.done())
        self.room.local_participant.publish_track.assert_awaited_once()
        self.room.disconnect.assert_not_awaited()
        await ws.close()
        await asyncio.wait_for(self.done.wait(), 2)

    async def test_metadata_warnings_are_sanitized_deduplicated_and_cleared(self):
        ws = await self.client.ws_connect("/realtime")
        await ws.send_json({"type": "session.update", "session": {}})
        await ws.receive_json(timeout=2)
        metadata = json.dumps({
            "requested": {"system_prompt": "private customer context"},
            "effective": {"ignored_metadata": {"system_prompt": "private customer context", "private-diagnostic": "secret"}},
        })
        with self.assertLogs("rtmt", level="WARNING") as captured:
            self.room.handlers["room_metadata_changed"](metadata)
            self.room.handlers["room_metadata_changed"](metadata)
        notice = await ws.receive_json(timeout=2)
        self.assertEqual(notice["warning"]["ignored_fields"], ["other", "system_prompt"])
        self.assertEqual(len(captured.output), 1)
        for private in ("private customer context", "private-diagnostic", "secret"):
            self.assertNotIn(private, json.dumps(notice))
            self.assertNotIn(private, "\n".join(captured.output))
        self.room.handlers["room_metadata_changed"](json.dumps({"effective": {"ignored_metadata": []}}))
        self.assertEqual(await ws.receive_json(timeout=2), {"type": "session.warning", "warning": None})
        self.assertFalse(self.session._failure.done())
        await ws.close()
        await asyncio.wait_for(self.done.wait(), 2)

    async def test_tool_lifecycle_and_one_transcription_transport(self):
        ws = await self.client.ws_connect("/realtime")
        await ws.send_json({"type": "session.update", "session": {}})
        await ws.receive_json(timeout=2)
        agent = self.room.remote_participants["agent"]
        self.room.handlers["data_received"](SimpleNamespace(topic="future-topic", data=b"not JSON", participant=agent))
        snapshot = {"type": "tool_status_snapshot", "jobs": [{"tool": "lookup", "status": "done"}]}
        self.room.handlers["data_received"](SimpleNamespace(
            topic="tool_status", data=json.dumps(snapshot).encode(), participant=agent,
        ))
        self.assertEqual(await ws.receive_json(timeout=2), {
            "type": "extension.humain.tool", "topic": "tool_status", "event": snapshot,
        })
        self.assertNotIn("transcription_received", self.room.handlers)
        self.room.text_handlers["lk.transcription"](
            SimpleNamespace(
                info=SimpleNamespace(
                    attributes={"lk.segment_id": "final", "lk.transcription_final": "true"},
                    stream_id="stream-final",
                ),
                read_all=AsyncMock(return_value="welcome"),
            ),
            agent.identity,
        )
        transcript = await ws.receive_json(timeout=2)
        self.assertEqual(transcript["type"], "response.output_audio_transcript.done")
        self.assertEqual(transcript["transcript"], "welcome")
        self.assertEqual(len(self.session.transcript_buffer.segments), 1)
        await ws.close()
        await asyncio.wait_for(self.done.wait(), 2)

    async def test_agent_loss_is_reported_and_releases_media(self):
        ws = await self.client.ws_connect("/realtime")
        await ws.send_json({"type": "session.update", "session": {}})
        await ws.receive_json(timeout=2)
        self.room.handlers["participant_disconnected"](self.room.remote_participants["agent"])
        error = await ws.receive_json(timeout=2)
        self.assertIn("agent left", error["error"]["message"])
        await ws.close()
        await asyncio.wait_for(self.done.wait(), 2)
        self.source.aclose.assert_awaited_once()
        self.assertNotIn("agent", self.session._agents)

    async def test_stop_during_connect_cancels_upstream_work(self):
        connecting = asyncio.Event()

        async def connect(url, token):
            connecting.set()
            await asyncio.Event().wait()

        self.room.connect.side_effect = connect
        ws = await self.client.ws_connect("/realtime")
        await ws.send_json({"type": "session.update", "session": {}})
        await asyncio.wait_for(connecting.wait(), 2)
        await ws.close()
        await asyncio.wait_for(self.done.wait(), 2)
        self.room.disconnect.assert_awaited_once()
        self.assertFalse(self.session._tasks)

    async def test_token_failure_is_visible_and_completes_assist_session(self):
        assist_session = AgentAssistSession("call", "voice")
        assist = SimpleNamespace(
            enabled=True, create_session=MagicMock(return_value=assist_session),
            complete_session=AsyncMock(),
        )
        self.session.agent_assist_client = assist
        with patch("backend.rtmt.mint_token", side_effect=HumainError("HUMAIN authorization was rejected.")):
            ws = await self.client.ws_connect("/realtime")
            await ws.send_json({"type": "session.update", "session": {}})
            error = await ws.receive_json(timeout=2)
            self.assertIn("authorization", error["error"]["message"])
            await ws.close()
            await asyncio.wait_for(self.done.wait(), 2)
        assist.complete_session.assert_awaited_once_with(assist_session)
        self.assertFalse(self.session._tasks)


class AppTests(unittest.IsolatedAsyncioTestCase):
    async def test_staged_deployment_reports_disabled_voice_and_never_dials(self):
        from app import create_app
        from runtime_fixtures import enabled_deployment
        with patch.dict(os.environ, {"HUMAIN_ENABLED": "false"}, clear=True), patch("app.load_dotenv"):
            client = TestClient(TestServer(await create_app(enabled_deployment(applicationTelephony=False))))
        await client.start_server()
        try:
            response = await client.get("/health")
            self.assertEqual(response.status, 200)
            health = await response.json()
            self.assertEqual(health["status"], "awaiting_humain_credentials")
            self.assertFalse(health["voice_enabled"])
            self.assertEqual(health["interruption_policy_mode"], "off")
            self.assertEqual(health["turn_taking_strategy"], "fast_vad")
            response = await client.post("/call", json={"number": "+10000000000"})
            self.assertEqual(response.status, 503)
            with patch("backend.rtmt.mint_token") as token_request:
                ws = await client.ws_connect("/realtime")
                error = await ws.receive_json(timeout=2)
                self.assertEqual(error["type"], "error")
                self.assertIn("not enabled", error["error"]["message"])
                token_request.assert_not_called()
                await ws.close()
        finally:
            await client.close()

    async def test_app_starts_without_azure_openai_configuration(self):
        from app import create_app
        from runtime_fixtures import enabled_deployment
        env = {"HUMAIN_AUTHORIZATION": "TestScheme synthetic-test-credential", "AGENT_PHONE_NUMBER": ""}
        with patch.dict(os.environ, env, clear=True), patch("app.load_dotenv"):
            app = await create_app(enabled_deployment(applicationTelephony=False))
        paths = {route.resource.canonical for route in app.router.routes()}
        self.assertTrue({"/realtime", "/realtime-acs", "/transcription-acs", "/call"}.issubset(paths))
        await app.cleanup()

    async def test_health_reports_requested_policy_without_connecting_to_provider(self):
        from app import create_app
        from runtime_fixtures import enabled_deployment
        env = {
            "HUMAIN_AUTHORIZATION": "TestScheme synthetic-test-credential",
            "HUMAIN_INTERRUPTION_POLICY_MODE": "off",
            "HUMAIN_TURN_TAKING_STRATEGY": "fast_vad",
        }
        with patch.dict(os.environ, env, clear=True), patch("app.load_dotenv"):
            client = TestClient(TestServer(await create_app(enabled_deployment(applicationTelephony=False))))
        await client.start_server()
        try:
            with patch("backend.rtmt.mint_token") as token_request:
                response = await client.get("/health")
                self.assertEqual(response.status, 200)
                health = await response.json()
                self.assertEqual(health["interruption_policy_mode"], "off")
                self.assertEqual(health["turn_taking_strategy"], "fast_vad")
                token_request.assert_not_called()
        finally:
            await client.close()

    async def test_new_phone_call_does_not_reuse_an_active_room(self):
        from app import create_app
        from backend.session_registry import SessionRegistry
        from runtime_fixtures import enabled_deployment

        registry = SessionRegistry()
        first, second = RTMiddleTier(config()), RTMiddleTier(config())
        registry.register("first", first)
        registry.register("second", second)
        env = {
            "HUMAIN_AUTHORIZATION": "TestScheme synthetic-test-credential",
            "ACS_SOURCE_NUMBER": "+12025550102", "ACS_CONNECTION_STRING": "synthetic",
            "ACS_CALLBACK_PATH": "https://voice.example/acs", "ACS_MEDIA_STREAMING_WEBSOCKET_PATH": "wss://voice.example/realtime-acs",
        }
        with (
            patch.dict(os.environ, env, clear=True), patch("app.load_dotenv"),
            patch("app.get_session_registry", return_value=registry),
            patch.object(RTMiddleTier, "forward_messages", autospec=True),
            patch("app.AcsCaller", return_value=SimpleNamespace(
                close=AsyncMock(), outbound_call_handler=AsyncMock(return_value=web.Response()),
            )),
        ):
            client = TestClient(TestServer(await create_app(enabled_deployment())))
            await client.start_server()
            try:
                ws = await client.ws_connect("/realtime-acs", headers={"x-ms-call-connection-id": "new-call"})
                await ws.close()
                third = registry.get("new-call")
                self.assertIsNotNone(third)
                self.assertIsNot(third, first)
                self.assertIsNot(third, second)
                self.assertNotIn(third._room_name, {first._room_name, second._room_name})
            finally:
                await client.close()


if __name__ == "__main__":
    unittest.main()
