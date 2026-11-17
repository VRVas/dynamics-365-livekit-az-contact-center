"""Startup-only playback on the application's own native ACS connection."""

import asyncio
import hashlib
import io
import logging
import wave
from functools import lru_cache
from pathlib import Path

from azure.communication.callautomation import FileSource, PhoneNumberIdentifier
from azure.communication.callautomation.aio import CallConnectionClient
from azure.core.exceptions import AzureError

from backend.latency import LatencyTrace

logger = logging.getLogger("native_notice")
NOTICE_FILE = Path(__file__).with_name("native_startup_notice.wav")
NOTICE_STOP_SECONDS = 5


class NativeNoticeError(RuntimeError):
    def __init__(self, code: str):
        super().__init__(f"The startup notice could not be confirmed ({code}).")
        self.code = code


@lru_cache(maxsize=1)
def notice_asset() -> tuple[bytes, str]:
    try:
        content = NOTICE_FILE.read_bytes()
        if len(content) > 2 * 1024 * 1024:
            raise ValueError("The notice exceeds its size limit.")
        with wave.open(io.BytesIO(content), "rb") as audio:
            if (
                (audio.getnchannels(), audio.getsampwidth(), audio.getframerate(), audio.getcomptype())
                != (1, 2, 16000, "NONE") or not 1 <= audio.getnframes() / 16000 <= 30
                or len(audio.readframes(audio.getnframes())) != audio.getnframes() * 2
            ):
                raise ValueError("The notice must be a complete 1-30 second mono PCM16 16 kHz WAV.")
    except (OSError, EOFError, wave.Error, ValueError) as error:
        raise NativeNoticeError("startup_notice_asset_invalid") from error
    return content, hashlib.sha256(content).hexdigest()


class NativeStartupNotice:
    def __init__(
        self, connection: CallConnectionClient, connection_id: str, correlation_id: str,
        latency: LatencyTrace, operation_context: str,
    ):
        self.connection = connection
        self.connection_id = connection_id
        self.correlation_id = correlation_id
        self.latency = latency
        self.operation_context = operation_context
        self.submitted = False
        self.error_code: str | None = None
        self.terminal = asyncio.Event()
        self.terminal_kind = ""
        self._stop_task: asyncio.Task[None] | None = None

    @property
    def state(self):
        if self.error_code:
            return "failed"
        if self.terminal.is_set():
            return "stopped"
        if self._stop_task is not None:
            return "stopping"
        return "submitted" if self.submitted else "not_started"

    async def start(self, phone: str, source_url: str, callback_url: str, cache_id: str):
        if self.submitted:
            raise NativeNoticeError("startup_notice_already_submitted")
        self.submitted = True
        try:
            with self.latency.span("startup_notice_submit"):
                async with asyncio.timeout(5):
                    await self.connection.play_media(
                        FileSource(source_url, play_source_cache_id=cache_id),
                        play_to=[PhoneNumberIdentifier(phone)], loop=True,
                        operation_context=self.operation_context, operation_callback_url=callback_url,
                    )
        except (AzureError, TimeoutError) as error:
            self.error_code = "startup_notice_start_failed"
            raise NativeNoticeError(self.error_code) from error
        if self.error_code:
            raise NativeNoticeError(self.error_code)

    def observe(self, event):
        data = event.get("data") if isinstance(event, dict) else None
        kind = event.get("type") if isinstance(event, dict) else None
        if (
            not isinstance(data, dict)
            or data.get("callConnectionId") != self.connection_id
            or data.get("correlationId") != self.correlation_id
            or data.get("operationContext") != self.operation_context
            or kind not in {
                "Microsoft.Communication.PlayStarted", "Microsoft.Communication.PlayCompleted",
                "Microsoft.Communication.PlayCanceled", "Microsoft.Communication.PlayFailed",
            }
        ):
            raise ValueError("The notice event does not match the verified playback operation.")
        if self.terminal.is_set():
            return
        if kind == "Microsoft.Communication.PlayStarted":
            self.latency.mark("startup_notice_started", once=True)
            return
        self.terminal_kind = kind.rsplit(".", 1)[-1]
        if self.terminal_kind == "PlayFailed":
            self.error_code = "startup_notice_play_failed"
        elif self.terminal_kind == "PlayCanceled" and self._stop_task is None:
            self.error_code = "startup_notice_cancelled_early"
        self.terminal.set()
        self.latency.mark("startup_notice_terminal", status="error" if self.error_code else "ok")

    async def _stop(self):
        try:
            with self.latency.span("startup_notice_stop"):
                async with asyncio.timeout(NOTICE_STOP_SECONDS):
                    if not self.terminal.is_set():
                        # The queue belongs to our connection, not Dynamics' separate native leg.
                        await self.connection.cancel_all_media_operations()
                        await self.terminal.wait()
                    if self.error_code:
                        raise NativeNoticeError(self.error_code)
        except (AzureError, TimeoutError) as error:
            self.error_code = "startup_notice_stop_unconfirmed"
            raise NativeNoticeError(self.error_code) from error

    async def stop(self):
        if not self.submitted:
            return
        if self._stop_task is None:
            self._stop_task = asyncio.create_task(self._stop())

            def completed(task):
                error = None if task.cancelled() else task.exception()
                if task.cancelled() or error is not None:
                    if self.error_code is None:
                        self.error_code = "startup_notice_stop_cancelled" if task.cancelled() else "startup_notice_stop_failed"
                    logger.error("Native startup notice cleanup failed code=%s", self.error_code or "cancelled")

            self._stop_task.add_done_callback(completed)
        await asyncio.shield(self._stop_task)
