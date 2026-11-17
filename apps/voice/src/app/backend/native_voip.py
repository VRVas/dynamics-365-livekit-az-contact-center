"""Private browser-backed ACS audio participant for the native Dynamics pilot."""

import asyncio
import base64
import binascii
import json
import logging
import math
import re
import secrets
import socket
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any, Literal

from aiohttp import WSMsgType, web
from azure.communication.callautomation import CommunicationUserIdentifier
from azure.communication.callautomation.aio import CallConnectionClient
from azure.communication.identity import CommunicationTokenScope, CommunicationUserIdentifier as IdentityUser
from azure.communication.identity.aio import CommunicationIdentityClient
from azure.core.exceptions import AzureError, HttpResponseError
from playwright.async_api import Browser, BrowserContext, Error as PlaywrightError, Page, Playwright, async_playwright
from backend.latency import LatencyTrace, measure

logger = logging.getLogger("native_voip")
IDENTITY_RETENTION_SECONDS = 15 * 60


class NativeVoipError(RuntimeError):
    pass


@dataclass
class NativeVoipSession:
    conversation_id: str
    correlation_id: str
    user: IdentityUser = field(repr=False)
    capability: str = field(default_factory=lambda: secrets.token_urlsafe(32), repr=False)
    registered: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    media_ready: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    failed: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    context: BrowserContext | None = field(default=None, repr=False)
    page: Page | None = field(default=None, repr=False)
    ws: web.WebSocketResponse | None = field(default=None, repr=False)
    error_code: str | None = None
    call_id: str | None = None
    authorized: bool = False
    closing: bool = False
    sent_frames: int = 0
    input_level_peak: float = 0
    latency: LatencyTrace | None = field(default=None, repr=False)
    identity_cleanup_status: Literal["active", "retained", "deleting", "deleted", "failed"] = "active"
    pause_requested: bool = False
    pause_command: str | None = None
    pause_revision: int = 0
    pause_ack: asyncio.Future | None = field(default=None, repr=False)
    send_lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    def fail(self, code: str):
        if not self.closing and not self.failed.is_set():
            self.error_code = code
            self.failed.set()
            logger.error("Native VoIP participant failed conversation=%s code=%s", self.conversation_id, code)

    def check(self):
        if self.failed.is_set():
            raise NativeVoipError(f"Native VoIP participant failed: {self.error_code}.")
        if self.closing:
            raise NativeVoipError("Native VoIP participant is closing.")

    async def wait(self, signal: asyncio.Event, timeout: float):
        self.check()
        tasks = [asyncio.create_task(signal.wait()), asyncio.create_task(self.failed.wait())]
        try:
            completed, _ = await asyncio.wait(tasks, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
            if not completed:
                raise NativeVoipError("Native VoIP participant did not become ready in time.")
            self.check()
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def send_audio(self, message: dict[str, Any]):
        async with self.send_lock:
            await self._send_audio(message)

    async def _send_audio(self, message: dict[str, Any]):
        if self.pause_requested and message.get("type") == "response.output_audio.delta":
            return
        if message.get("type") == "input_audio_buffer.speech_started" and self.closing:
            # Handover can race the tool's final flush; release already clears the browser queue.
            return
        self.check()
        if not self.authorized or not self.media_ready.is_set() or self.ws is None or self.ws.closed:
            raise NativeVoipError("Native VoIP media is not ready for audio.")
        if message.get("type") == "input_audio_buffer.speech_started":
            await self.ws.send_json({"kind": "flush"})
            return
        if message.get("type") != "response.output_audio.delta":
            raise NativeVoipError("Unsupported native VoIP audio message.")
        encoded = message.get("delta")
        if not isinstance(encoded, str):
            raise NativeVoipError("Native VoIP audio must be base64 PCM16.")
        try:
            pcm = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error) as error:
            raise NativeVoipError("Invalid native VoIP PCM encoding.") from error
        if not pcm or len(pcm) % 2 or len(pcm) > 48000:
            raise NativeVoipError("Invalid native VoIP PCM frame size.")
        await self.ws.send_bytes(pcm)
        self.sent_frames += 1

    async def pause(self, command_id: str, revision: int):
        if not re.fullmatch(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", command_id) or type(revision) is not int or revision < 1:
            raise NativeVoipError("Invalid native pause correlation.")
        self.check()
        if not self.authorized or not self.media_ready.is_set() or self.ws is None or self.ws.closed:
            raise NativeVoipError("Native pause requires verified media.")
        self.pause_requested = True
        self.pause_command, self.pause_revision = command_id, revision
        self.pause_ack = asyncio.get_running_loop().create_future()
        try:
            async with asyncio.timeout(6):
                async with self.send_lock:
                    await self.ws.send_json({"kind": "pause", "command_id": command_id, "revision": revision})
                await asyncio.shield(self.pause_ack)
        except TimeoutError as error:
            raise NativeVoipError("Native pause playback acknowledgement was not received.") from error
        finally:
            if self.pause_ack is not None and not self.pause_ack.done():
                self.pause_ack.cancel()

    def acknowledge_pause(self, value):
        if (
            self.pause_ack is not None and not self.pause_ack.done()
            and value.get("command_id") == self.pause_command
            and type(value.get("revision")) is int and value["revision"] == self.pause_revision
            and value.get("muted") is True
            and type(value.get("queued_frames")) is int and value["queued_frames"] == 0
        ):
            self.pause_ack.set_result(True)


class NativeVoipGateway:
    def __init__(self, connection_string: str, *, prewarm=False):
        self._connection_string = connection_string
        self._identity: CommunicationIdentityClient | None = None
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._runner: web.AppRunner | None = None
        self._origin = ""
        self._sessions: dict[str, NativeVoipSession] = {}
        self._lock = asyncio.Lock()
        self._closing = False
        self._prewarm = prewarm
        self._page_lock = asyncio.Lock()
        self._warm_context: BrowserContext | None = None
        self._warm_page: Page | None = None
        self._identity_cleanup_now = asyncio.Event()
        self._identity_cleanups: dict[str, tuple[NativeVoipSession, asyncio.Task[None]]] = {}
        self._identity_cleanup_failures = 0

    @property
    def ready(self):
        return self._browser is not None and self._browser.is_connected() and not self._closing

    @property
    def warm_page_ready(self):
        return self.ready and self._warm_page is not None and not self._warm_page.is_closed()

    @property
    def retained_identity_count(self):
        return len(self._identity_cleanups)

    @property
    def identity_cleanup_failures(self):
        return self._identity_cleanup_failures

    def identity_cleanup_pending(self, session: NativeVoipSession):
        return session.capability in self._identity_cleanups

    async def _new_page(self, latency):
        assert self._browser is not None
        with measure(latency, "voip_browser_context"):
            context = await self._browser.new_context(permissions=["microphone"])
        try:
            with measure(latency, "voip_browser_page"):
                page = await context.new_page()
            with measure(latency, "voip_browser_bundle"):
                await page.goto(self._origin, wait_until="load", timeout=10000)
            return context, page
        except (PlaywrightError, TimeoutError, asyncio.CancelledError):
            async with asyncio.timeout(5):
                await context.close()
            raise

    async def warm(self):
        if not self._prewarm:
            return
        trace = LatencyTrace()
        with trace.span("worker_browser_start"):
            await self.start()
        async with self._page_lock:
            if self._closing or self._sessions or self.warm_page_ready:
                return
            if self._warm_context is not None:
                await self._warm_context.close()
                self._warm_context = None
                self._warm_page = None
            with trace.span("worker_page_warmup"):
                self._warm_context, self._warm_page = await self._new_page(trace)
            trace.mark("worker_warm", warm=True)

    async def _take_page(self, session):
        async with self._page_lock:
            if self.warm_page_ready:
                session.context, session.page = self._warm_context, self._warm_page
                self._warm_context = self._warm_page = None
                if session.latency is not None:
                    session.latency.mark("voip_page_acquired", warm=True)
                return
            if self._closing:
                raise NativeVoipError("The native browser is closing.")
            session.context, session.page = await self._new_page(session.latency)
            if session.latency is not None:
                session.latency.mark("voip_page_acquired", warm=False)

    async def _allocate_identity(self, latency):
        assert self._identity is not None
        operation = asyncio.create_task(self._identity.create_user_and_token(
            scopes=[CommunicationTokenScope.VOIP], token_expires_in=timedelta(hours=1),
        ))
        try:
            with measure(latency, "voip_identity_and_token"):
                return await asyncio.shield(operation)
        except asyncio.CancelledError:
            # Let this one bounded allocation finish so a late-created identity can be revoked.
            try:
                async with asyncio.timeout(12):
                    user, _ = await operation
                    await self._identity.delete_user(user)
            except (AzureError, TimeoutError):
                logger.error("Cancelled native identity allocation cleanup could not be confirmed.")
                raise NativeVoipError("Cancelled native identity allocation has an unconfirmed outcome.")
            raise

    async def start(self):
        async with self._lock:
            if self.ready:
                return
            if self._closing:
                raise NativeVoipError("Native VoIP gateway is closing.")
            if self._browser is not None or self._runner is not None:
                raise NativeVoipError("Native VoIP runtime was lost; restart the voice service before retrying.")
            asset = Path(__file__).parent / "native_rtc/dist/gateway.bundle.js"
            if not asset.is_file():
                raise NativeVoipError("Build the native Calling SDK browser asset before enabling VoIP media.")
            app = web.Application()

            async def index(request):
                return web.Response(
                    text='<!doctype html><meta charset="utf-8"><title>Native voice transport</title><script src="/gateway.js"></script>',
                    content_type="text/html",
                )

            async def script(request):
                return web.FileResponse(asset)

            app.router.add_get("/", index)
            app.router.add_get("/gateway.js", script)
            app.router.add_get("/audio/{capability}", self._socket)
            self._runner = web.AppRunner(app, access_log=None)
            await self._runner.setup()
            sock = socket.socket()
            sock.bind(("127.0.0.1", 0))
            sock.listen()
            sock.setblocking(False)
            self._origin = f"http://localhost:{sock.getsockname()[1]}"
            try:
                await web.SockSite(self._runner, sock).start()
                self._playwright = await async_playwright().start()
                self._browser = await self._playwright.chromium.launch(
                    headless=True,
                    args=[
                        "--autoplay-policy=no-user-gesture-required",
                        "--use-fake-device-for-media-stream",
                        "--use-fake-ui-for-media-stream",
                    ],
                )
                self._identity = CommunicationIdentityClient.from_connection_string(
                    self._connection_string, retry_total=0, connection_timeout=5, read_timeout=5,
                )
            except (PlaywrightError, OSError, AzureError) as error:
                await self.close()
                raise NativeVoipError("Native VoIP browser initialization failed.") from error

    async def _socket(self, request):
        session = self._sessions.get(request.match_info["capability"])
        if session is None or session.closing or session.ws is not None or request.headers.get("Origin") != self._origin:
            return web.Response(status=403)
        ws = web.WebSocketResponse(max_msg_size=4096)
        session.ws = ws
        await ws.prepare(request)
        try:
            async for message in ws:
                if message.type != WSMsgType.TEXT:
                    raise NativeVoipError("Native VoIP status must be JSON text.")
                try:
                    value = json.loads(message.data)
                except (ValueError, UnicodeDecodeError) as error:
                    raise NativeVoipError("Native VoIP status is malformed.") from error
                if not isinstance(value, dict):
                    raise NativeVoipError("Native VoIP status must be an object.")
                kind = value.get("kind")
                if kind == "registered":
                    session.registered.set()
                    if session.latency is not None:
                        session.latency.mark("voip_registered")
                elif kind in {"connected", "media_ready"}:
                    if value.get("call_id") != session.correlation_id:
                        raise NativeVoipError("The SDK participant connected to another call.")
                    session.call_id = value["call_id"]
                    if kind == "media_ready":
                        session.media_ready.set()
                    if session.latency is not None:
                        session.latency.mark("voip_" + kind, once=True)
                elif kind == "phase":
                    stage = value.get("stage")
                    if stage not in {"devices_ready", "invitation_received", "invitation_accepted"}:
                        raise NativeVoipError("Unknown native VoIP timing stage.")
                    if session.latency is not None:
                        session.latency.mark("voip_" + stage, once=True)
                elif kind == "metrics":
                    level = value.get("input_level")
                    if not isinstance(level, (int, float)) or isinstance(level, bool) or not math.isfinite(level):
                        raise NativeVoipError("Invalid native audio metric.")
                    session.input_level_peak = max(session.input_level_peak, level)
                elif kind == "disconnected":
                    session.fail("sdk_disconnected")
                elif kind == "paused":
                    session.acknowledge_pause(value)
                elif kind == "error":
                    session.fail("sdk_reported_error")
                elif kind != "flushed":
                    raise NativeVoipError("Unknown native VoIP status event.")
        except NativeVoipError:
            session.fail("invalid_gateway_status")
            await ws.close(code=1008)
        finally:
            if not session.closing:
                session.fail("gateway_socket_closed")
        return ws

    async def prepare(
        self, conversation_id: str, correlation_id: str, *, latency: LatencyTrace | None = None,
    ) -> NativeVoipSession:
        with measure(latency, "voip_runtime_start"):
            await self.start()
        assert self._identity is not None and self._browser is not None
        token = None
        if self._prewarm:
            user, token = await self._allocate_identity(latency)
        else:
            with measure(latency, "voip_identity_create"):
                user = await self._identity.create_user()
        session = NativeVoipSession(conversation_id, correlation_id, user, latency=latency)
        self._sessions[session.capability] = session
        try:
            if token is None:
                with measure(latency, "voip_identity_token"):
                    token = await self._identity.get_token(
                        user, scopes=[CommunicationTokenScope.VOIP], token_expires_in=timedelta(hours=1),
                    )
            await self._take_page(session)
            assert session.page is not None
            session.page.on("crash", lambda: session.fail("browser_crashed"))
            session.page.on("pageerror", lambda error: session.fail("browser_script_error"))
            with measure(latency, "voip_sdk_registration"):
                await session.page.evaluate(
                    "(settings) => window.nativeVoip.start(settings)",
                    {
                        "token": token.token,
                        "websocketUrl": self._origin.replace("http:", "ws:", 1) + "/audio/" + session.capability,
                        "expectedCallId": correlation_id,
                    },
                )
            await session.wait(session.registered, 12)
            return session
        except (PlaywrightError, AzureError, TimeoutError, NativeVoipError) as error:
            await self.release(session, None)
            raise NativeVoipError("Native VoIP participant registration failed.") from error
        except asyncio.CancelledError:
            await self.release(session, None)
            raise

    async def _delete_identity(self, session: NativeVoipSession):
        assert self._identity is not None
        session.identity_cleanup_status = "deleting"
        try:
            with measure(session.latency, "voip_identity_delete"):
                try:
                    async with asyncio.timeout(5):
                        await self._identity.delete_user(session.user)
                except HttpResponseError as error:
                    if error.status_code != 404:
                        raise
        except (AzureError, TimeoutError):
            session.identity_cleanup_status = "failed"
            raise
        session.identity_cleanup_status = "deleted"

    async def _delete_identity_after_grace(self, session: NativeVoipSession):
        try:
            await asyncio.wait_for(self._identity_cleanup_now.wait(), IDENTITY_RETENTION_SECONDS)
        except TimeoutError:
            pass  # The diagnostic grace period elapsed.
        await self._delete_identity(session)

    async def _retain_identity(self, session: NativeVoipSession):
        if self._identity_cleanups:
            raise NativeVoipError("Only one retained-identity diagnostic is permitted at a time.")
        assert self._identity is not None
        with measure(session.latency, "voip_identity_revoke"):
            async with asyncio.timeout(5):
                await self._identity.revoke_tokens(session.user)
        if self._closing:
            await self._delete_identity(session)
            return
        self._identity_cleanup_now.clear()
        session.identity_cleanup_status = "retained"
        if session.latency is not None:
            session.latency.mark("voip_identity_retained", status="revocation_requested")
        task = asyncio.create_task(self._delete_identity_after_grace(session))
        self._identity_cleanups[session.capability] = (session, task)

        def completed(cleanup_task: asyncio.Task[None]):
            error = None if cleanup_task.cancelled() else cleanup_task.exception()
            if cleanup_task.cancelled() or error is not None:
                session.identity_cleanup_status = "failed"
                self._identity_cleanup_failures += 1
                logger.error(
                    "Deferred native identity cleanup failed conversation=%s type=%s",
                    session.conversation_id,
                    "CancelledError" if cleanup_task.cancelled() else type(error).__name__,
                )
            else:
                self._identity_cleanups.pop(session.capability, None)

        task.add_done_callback(completed)

    async def finish_identity_cleanup(self, session: NativeVoipSession):
        cleanup = self._identity_cleanups.get(session.capability)
        if cleanup is None:
            return
        self._identity_cleanup_now.set()
        try:
            await asyncio.shield(cleanup[1])
        except (AzureError, TimeoutError) as error:
            raise NativeVoipError("Retained identity cleanup could not be confirmed.") from error
        self._identity_cleanups.pop(session.capability, None)

    async def release(
        self, session: NativeVoipSession, connection: CallConnectionClient | None, *, retain_identity: bool = False,
    ):
        session.closing = True
        session.authorized = False
        cleanup_error = False
        try:
            if session.page is not None and not session.page.is_closed():
                try:
                    async with asyncio.timeout(6):
                        await session.page.evaluate("() => window.nativeVoip.stop()")
                except (PlaywrightError, TimeoutError):
                    logger.warning("Native VoIP browser cleanup needs fallback conversation=%s", session.conversation_id)
            if session.context is not None:
                async with asyncio.timeout(5):
                    await session.context.close()
            if connection is not None:
                participant = CommunicationUserIdentifier(session.user.raw_id)
                try:
                    async with asyncio.timeout(12):
                        await connection.get_participant(participant)
                        await connection.remove_participant(participant)
                        for attempt in range(6):
                            try:
                                await connection.get_participant(participant)
                            except HttpResponseError as error:
                                if error.status_code == 404:
                                    break
                                raise
                            if attempt == 5:
                                raise NativeVoipError("The native microphone participant did not leave.")
                            await asyncio.sleep(0.25)
                except HttpResponseError as error:
                    if error.status_code != 404:
                        raise
        except (AzureError, PlaywrightError, NativeVoipError, TimeoutError) as error:
            cleanup_error = True
            logger.error("Native VoIP call cleanup failed conversation=%s type=%s", session.conversation_id, type(error).__name__)
        finally:
            if session.ws is not None and not session.ws.closed:
                try:
                    async with asyncio.timeout(3):
                        await session.ws.close()
                except TimeoutError:
                    cleanup_error = True
                    logger.error("Native VoIP private socket cleanup timed out conversation=%s", session.conversation_id)
            if self._identity is not None and not self.identity_cleanup_pending(session):
                try:
                    if retain_identity and connection is not None and session.media_ready.is_set() and not cleanup_error and not self._closing:
                        try:
                            await self._retain_identity(session)
                        except (AzureError, TimeoutError, NativeVoipError) as error:
                            cleanup_error = True
                            logger.error(
                                "Native identity retention failed conversation=%s type=%s; deleting immediately",
                                session.conversation_id, type(error).__name__,
                            )
                            await self._delete_identity(session)
                    else:
                        await self._delete_identity(session)
                except (AzureError, TimeoutError) as error:
                    cleanup_error = True
                    self._identity_cleanup_failures += 1
                    logger.error("Native VoIP identity cleanup failed conversation=%s type=%s", session.conversation_id, type(error).__name__)
            self._sessions.pop(session.capability, None)
        if cleanup_error:
            raise NativeVoipError("Could not confirm native VoIP participant and identity cleanup.")

    async def close(self):
        self._closing = True
        self._identity_cleanup_now.set()
        for session in list(self._sessions.values()):
            try:
                await self.release(session, None)
            except NativeVoipError:
                logger.error("Native VoIP session cleanup incomplete during shutdown.")
        tasks = [task for _, task in self._identity_cleanups.values()]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if self._identity is not None:
            for capability, (session, _) in list(self._identity_cleanups.items()):
                try:
                    await self._delete_identity(session)
                except (AzureError, TimeoutError) as error:
                    logger.error(
                        "Retained identity cleanup incomplete during shutdown conversation=%s type=%s",
                        session.conversation_id, type(error).__name__,
                    )
                else:
                    self._identity_cleanups.pop(capability, None)
        if self._identity is not None:
            await self._identity.close()
            self._identity = None
        async with self._page_lock:
            if self._warm_context is not None:
                await self._warm_context.close()
                self._warm_context = self._warm_page = None
        if self._browser is not None:
            await self._browser.close()
        if self._playwright is not None:
            await self._playwright.stop()
        if self._runner is not None:
            await self._runner.cleanup()
