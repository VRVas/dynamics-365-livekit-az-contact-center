import asyncio
import hashlib
import io
import tempfile
import unittest
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from xml.etree import ElementTree

from azure.core.exceptions import HttpResponseError

from backend.latency import LatencyTrace
from backend.native_notice import NativeNoticeError, NativeStartupNotice, notice_asset


CONVERSATION = "11111111-1111-4111-8111-111111111111"
CONNECTION = "22222222-2222-4222-8222-222222222222"
CORRELATION = "33333333-3333-4333-8333-333333333333"
PHONE = "+12025550101"


def event(kind="PlayCanceled", **changes):
    return {
        "type": "Microsoft.Communication." + kind,
        "data": {
            "callConnectionId": CONNECTION, "correlationId": CORRELATION,
            "operationContext": "synthetic-notice", **changes,
        },
    }


class NoticeAssetTests(unittest.TestCase):
    def test_packaged_notice_is_complete_telephone_pcm_and_content_addressed(self):
        content, digest = notice_asset()
        self.assertEqual(digest, hashlib.sha256(content).hexdigest())
        self.assertEqual(digest, "832a0365dde484512ea69bea45c406aa5074845c7f4361293ccf5f4dd4c8be14")
        with wave.open(io.BytesIO(content), "rb") as audio:
            self.assertEqual((audio.getnchannels(), audio.getsampwidth(), audio.getframerate()), (1, 2, 16000))
            self.assertGreater(audio.getnframes() / 16000, 1)
            self.assertLessEqual(audio.getnframes() / 16000, 30)
            self.assertAlmostEqual(audio.getnframes() / 16000, 9.825 / 1.3, delta=0.04)
            pcm = audio.readframes(audio.getnframes())
            self.assertEqual(len(pcm), audio.getnframes() * 2)
            self.assertTrue(any(pcm))

    def test_source_has_approved_arabic_then_english_without_a_delay_before_arabic(self):
        root = Path(__file__).resolve().parents[1]
        document = ElementTree.parse(root / "dynamics/startup-notice.ssml").getroot()
        voices = document.findall("{http://www.w3.org/2001/10/synthesis}voice")
        self.assertEqual([voice.attrib["name"] for voice in voices], ["ar-SA-ZariyahNeural", "en-US-JennyNeural"])
        self.assertEqual(voices[0].text.strip(), "يتم تسجيل هذه المكالمة لأغراض الجودة والتدريب.")
        self.assertEqual(voices[1].text.strip(), "This call is recorded for quality and training purposes.")

    def test_missing_or_invalid_asset_is_an_error_not_an_empty_audio_fallback(self):
        self.addCleanup(notice_asset.cache_clear)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "notice.wav"
            for content in (None, b"not a WAV"):
                if content is not None:
                    path.write_bytes(content)
                notice_asset.cache_clear()
                with patch("backend.native_notice.NOTICE_FILE", path), self.assertRaises(NativeNoticeError):
                    notice_asset()

    def test_truncated_pcm_payload_is_rejected(self):
        self.addCleanup(notice_asset.cache_clear)
        data = io.BytesIO()
        with wave.open(data, "wb") as audio:
            audio.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
            audio.writeframes(b"\x01\x00" * 16000)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "truncated.wav"
            path.write_bytes(data.getvalue()[:-100])
            notice_asset.cache_clear()
            with patch("backend.native_notice.NOTICE_FILE", path), self.assertRaises(NativeNoticeError):
                notice_asset()


class NoticeLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.connection = SimpleNamespace(play_media=AsyncMock(), cancel_all_media_operations=AsyncMock())
        self.trace = LatencyTrace(CONVERSATION)
        self.notice = NativeStartupNotice(
            self.connection, CONNECTION, CORRELATION, self.trace, "synthetic-notice",
        )

    async def start(self):
        await self.notice.start(
            PHONE, "https://voice.example/notice.wav",
            "https://voice.example/private-callback?token=synthetic-capability", "synthetic-digest",
        )

    async def test_default_unstarted_cleanup_does_not_cancel_media(self):
        await self.notice.stop()
        self.connection.cancel_all_media_operations.assert_not_awaited()

    async def test_play_targets_only_the_verified_caller_and_uses_its_own_callback(self):
        await self.start()
        arguments = self.connection.play_media.call_args
        self.assertEqual(arguments.args[0].url, "https://voice.example/notice.wav")
        self.assertEqual(arguments.args[0].play_source_cache_id, "synthetic-digest")
        self.assertEqual([target.raw_id for target in arguments.kwargs["play_to"]], ["4:" + PHONE])
        self.assertTrue(arguments.kwargs["loop"])
        self.assertEqual(arguments.kwargs["operation_context"], "synthetic-notice")
        self.assertIn("private-callback", arguments.kwargs["operation_callback_url"])
        self.assertEqual(self.notice.state, "submitted")
        self.notice.observe(event("PlayCompleted"))
        await self.notice.stop()

    async def test_cancel_acceptance_is_not_proof_that_playback_stopped(self):
        await self.start()
        requested = asyncio.Event()
        self.connection.cancel_all_media_operations.side_effect = lambda: requested.set()
        stop = asyncio.create_task(self.notice.stop())
        await requested.wait()
        self.assertFalse(stop.done())
        self.assertEqual(self.notice.state, "stopping")
        self.notice.observe(event())
        await stop
        self.assertEqual(self.notice.state, "stopped")
        self.connection.cancel_all_media_operations.assert_awaited_once()

    async def test_wrong_call_or_operation_cannot_unlock_audio(self):
        await self.start()
        for changes in (
            {"callConnectionId": CONVERSATION}, {"correlationId": CONVERSATION},
            {"operationContext": "another-notice"},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.notice.observe(event(**changes))
            self.assertFalse(self.notice.terminal.is_set())
        self.notice.observe(event("PlayCompleted"))
        await self.notice.stop()

    async def test_concurrent_stop_waiters_share_one_cancellation(self):
        await self.start()
        requested = asyncio.Event()
        self.connection.cancel_all_media_operations.side_effect = lambda: requested.set()
        first = asyncio.create_task(self.notice.stop())
        second = asyncio.create_task(self.notice.stop())
        await requested.wait()
        first.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await first
        self.assertFalse(second.done())
        self.notice.observe(event())
        await second
        await self.notice.stop()
        self.connection.cancel_all_media_operations.assert_awaited_once()

    async def test_stop_has_a_bounded_deadline_and_failure_is_visible(self):
        await self.start()
        with patch("backend.native_notice.NOTICE_STOP_SECONDS", 0.01), self.assertLogs("native_notice", level="ERROR"):
            with self.assertRaises(NativeNoticeError) as caught:
                await self.notice.stop()
        self.assertEqual(caught.exception.code, "startup_notice_stop_unconfirmed")
        self.assertEqual(self.notice.state, "failed")

    async def test_play_failure_is_not_reported_as_successful_startup(self):
        await self.start()
        self.notice.observe(event("PlayFailed"))
        with self.assertLogs("native_notice", level="ERROR"), self.assertRaises(NativeNoticeError):
            await self.notice.stop()
        self.assertEqual(self.notice.state, "failed")

    async def test_submission_failure_still_attempts_owned_queue_cleanup(self):
        self.connection.play_media.side_effect = HttpResponseError("Synthetic play failure")
        with self.assertRaises(NativeNoticeError):
            await self.start()
        self.connection.cancel_all_media_operations.side_effect = lambda: self.notice.observe(event())
        with self.assertLogs("native_notice", level="ERROR"), self.assertRaises(NativeNoticeError):
            await self.notice.stop()
        self.connection.cancel_all_media_operations.assert_awaited_once()
        self.assertTrue(self.notice.terminal.is_set())

    async def test_duplicate_terminal_event_does_not_change_confirmed_outcome(self):
        await self.start()
        self.notice.observe(event("PlayCompleted"))
        self.notice.observe(event("PlayFailed"))
        await self.notice.stop()
        self.assertEqual(self.notice.state, "stopped")
        self.connection.cancel_all_media_operations.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
