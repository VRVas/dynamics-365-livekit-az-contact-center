import asyncio
import base64
import json
import unittest
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from aiohttp import WSServerHandshakeError, web
from aiohttp.test_utils import TestClient, TestServer
from azure.communication.callautomation import CommunicationUserIdentifier as CallUser
from azure.communication.identity import CommunicationTokenScope, CommunicationUserIdentifier
from azure.core.credentials import AccessToken
from azure.core.exceptions import HttpResponseError
from playwright.async_api import Error as PlaywrightError

from backend.native_voip import IDENTITY_RETENTION_SECONDS, NativeVoipError, NativeVoipGateway, NativeVoipSession


CONVERSATION = "11111111-1111-4111-8111-111111111111"
CORRELATION = "22222222-2222-4222-8222-222222222222"
USER_ID = "8:acs:synthetic-identity"


def session():
    return NativeVoipSession(CONVERSATION, CORRELATION, CommunicationUserIdentifier(USER_ID))


def not_found():
    error = HttpResponseError("Synthetic missing participant")
    error.status_code = 404
    return error


class VoipSessionTests(unittest.IsolatedAsyncioTestCase):
    async def test_private_identity_and_capability_are_not_represented(self):
        current = session()
        self.assertNotIn(current.capability, repr(current))
        self.assertNotIn(USER_ID, repr(current))

    async def test_audio_requires_both_native_authorization_and_real_media_readiness(self):
        current = session()
        current.ws = SimpleNamespace(closed=False, send_bytes=AsyncMock(), send_json=AsyncMock())
        audio = {"type": "response.output_audio.delta", "delta": base64.b64encode(b"\1\0" * 480).decode()}
        with self.assertRaises(NativeVoipError):
            await current.send_audio(audio)
        current.authorized = True
        with self.assertRaises(NativeVoipError):
            await current.send_audio(audio)
        current.media_ready.set()
        await current.send_audio(audio)
        current.ws.send_bytes.assert_awaited_once_with(b"\1\0" * 480)
        self.assertEqual(current.sent_frames, 1)
        await current.send_audio({"type": "input_audio_buffer.speech_started"})
        current.ws.send_json.assert_awaited_once_with({"kind": "flush"})

    async def test_invalid_frames_do_not_enter_audio_transport(self):
        current = session()
        current.authorized = True
        current.media_ready.set()
        current.ws = SimpleNamespace(closed=False, send_bytes=AsyncMock())
        for value in (None, "invalid!", "", "AA==", base64.b64encode(b"\0" * 48002).decode()):
            with self.subTest(value=value), self.assertRaises(NativeVoipError):
                await current.send_audio({"type": "response.output_audio.delta", "delta": value})
        current.ws.send_bytes.assert_not_awaited()

    async def test_final_handover_flush_is_idempotent_during_release(self):
        current = session()
        current.closing = True
        await current.send_audio({"type": "input_audio_buffer.speech_started"})
        with self.assertRaises(NativeVoipError):
            await current.send_audio({"type": "response.output_audio.delta", "delta": "AAAA"})

    async def test_failure_and_timeout_are_explicit(self):
        current = session()
        with self.assertRaises(NativeVoipError):
            await current.wait(current.registered, 0)
        waiter = asyncio.create_task(current.wait(current.media_ready, 1))
        await asyncio.sleep(0)
        current.fail("wrong_native_call")
        with self.assertRaisesRegex(NativeVoipError, "wrong_native_call"):
            await waiter
        current.registered.set()
        with self.assertRaises(NativeVoipError):
            await current.wait(current.registered, 1)

    async def test_wait_cancellation_leaves_no_running_waiters(self):
        current = session()
        task = asyncio.create_task(current.wait(current.media_ready, 20))
        await asyncio.sleep(0)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task


class VoipGatewayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.gateway = NativeVoipGateway("synthetic-connection")
        self.gateway._origin = "http://localhost:12345"
        self.identity = SimpleNamespace(
            create_user=AsyncMock(return_value=CommunicationUserIdentifier(USER_ID)),
            get_token=AsyncMock(return_value=AccessToken("synthetic-token", 2000000000)),
            delete_user=AsyncMock(), revoke_tokens=AsyncMock(), close=AsyncMock(),
        )
        self.gateway._identity = self.identity
        self.page = SimpleNamespace(
            goto=AsyncMock(), evaluate=AsyncMock(), on=MagicMock(), is_closed=MagicMock(return_value=False),
        )
        self.context = SimpleNamespace(new_page=AsyncMock(return_value=self.page), close=AsyncMock())
        self.browser = SimpleNamespace(
            is_connected=MagicMock(return_value=True),
            new_context=AsyncMock(return_value=self.context),
            close=AsyncMock(),
        )
        self.gateway._browser = self.browser
        self.addAsyncCleanup(self.gateway.close)

    async def test_prepare_uses_a_disposable_scoped_identity_and_private_control_socket(self):
        async def register(*args):
            next(iter(self.gateway._sessions.values())).registered.set()

        self.page.evaluate.side_effect = register
        current = await self.gateway.prepare(CONVERSATION, CORRELATION)
        self.assertEqual(current.user.raw_id, USER_ID)
        self.identity.get_token.assert_awaited_once()
        call = self.identity.get_token.call_args
        self.assertEqual(call.kwargs["scopes"], [CommunicationTokenScope.VOIP])
        self.assertEqual(call.kwargs["token_expires_in"], timedelta(hours=1))
        settings = self.page.evaluate.call_args.args[1]
        self.assertEqual(settings["expectedCallId"], CORRELATION)
        self.assertIn(current.capability, settings["websocketUrl"])
        self.assertFalse(current.authorized)
        self.assertFalse(current.media_ready.is_set())

    async def test_warmup_keeps_one_unused_page_and_allocates_no_identity(self):
        self.gateway._prewarm = True
        await self.gateway.warm()
        await self.gateway.warm()
        self.assertTrue(self.gateway.warm_page_ready)
        self.browser.new_context.assert_awaited_once()
        self.page.goto.assert_awaited_once()
        self.page.evaluate.assert_not_awaited()
        self.identity.create_user.assert_not_awaited()
        self.identity.get_token.assert_not_awaited()
        self.assertEqual(self.gateway._sessions, {})

    async def test_fast_prepare_consumes_warm_page_and_uses_one_identity_request(self):
        self.gateway._prewarm = True
        self.identity.create_user_and_token = AsyncMock(return_value=(
            CommunicationUserIdentifier(USER_ID), AccessToken("synthetic-token", 2000000000),
        ))
        await self.gateway.warm()

        async def register(*args):
            next(iter(self.gateway._sessions.values())).registered.set()

        self.page.evaluate.side_effect = register
        current = await self.gateway.prepare(CONVERSATION, CORRELATION)
        self.assertFalse(self.gateway.warm_page_ready)
        self.assertIs(current.page, self.page)
        self.browser.new_context.assert_awaited_once()
        self.identity.create_user.assert_not_awaited()
        self.identity.get_token.assert_not_awaited()
        self.identity.create_user_and_token.assert_awaited_once_with(
            scopes=[CommunicationTokenScope.VOIP], token_expires_in=timedelta(hours=1),
        )
        await self.gateway.release(current, None)
        self.context.close.assert_awaited_once()
        self.identity.delete_user.assert_awaited_once_with(current.user)
        self.assertIsNone(self.gateway._warm_context)

    async def test_cancelled_fast_allocation_finishes_and_revokes_late_identity(self):
        self.gateway._prewarm = True
        started, complete = asyncio.Event(), asyncio.Event()

        async def allocate(**kwargs):
            started.set()
            await complete.wait()
            return CommunicationUserIdentifier(USER_ID), AccessToken("synthetic-token", 2000000000)

        self.identity.create_user_and_token = AsyncMock(side_effect=allocate)
        preparing = asyncio.create_task(self.gateway.prepare(CONVERSATION, CORRELATION))
        await asyncio.wait_for(started.wait(), 1)
        preparing.cancel()
        await asyncio.sleep(0)
        complete.set()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(preparing, 1)
        self.identity.delete_user.assert_awaited_once()
        self.browser.new_context.assert_not_awaited()
        self.assertEqual(self.gateway._sessions, {})

    async def test_warmup_does_not_allocate_another_page_during_a_call(self):
        self.gateway._prewarm = True
        current = session()
        self.gateway._sessions[current.capability] = current
        await self.gateway.warm()
        self.browser.new_context.assert_not_awaited()

    async def test_shutdown_closes_unused_warm_page(self):
        self.gateway._prewarm = True
        await self.gateway.warm()
        await self.gateway.close()
        self.context.close.assert_awaited_once()
        self.assertIsNone(self.gateway._warm_context)
        self.assertFalse(self.gateway.warm_page_ready)

    async def test_prepare_failure_releases_identity_and_context(self):
        self.page.goto.side_effect = PlaywrightError("Synthetic page failure")
        with self.assertRaises(NativeVoipError):
            await self.gateway.prepare(CONVERSATION, CORRELATION)
        self.identity.delete_user.assert_awaited_once()
        self.context.close.assert_awaited_once()
        self.assertEqual(self.gateway._sessions, {})

    async def test_prepare_cancellation_releases_allocated_identity(self):
        self.page.goto.side_effect = asyncio.CancelledError
        with self.assertRaises(asyncio.CancelledError):
            await self.gateway.prepare(CONVERSATION, CORRELATION)
        self.identity.delete_user.assert_awaited_once()

    async def test_release_removes_only_the_known_microphone_when_browser_cleanup_fails(self):
        current = session()
        current.page = self.page
        current.context = self.context
        self.gateway._sessions[current.capability] = current
        self.page.evaluate.side_effect = PlaywrightError("Synthetic browser error")
        connection = SimpleNamespace(
            get_participant=AsyncMock(side_effect=[object(), not_found()]),
            remove_participant=AsyncMock(),
        )
        await self.gateway.release(current, connection)
        removed = connection.remove_participant.call_args.args[0]
        self.assertIsInstance(removed, CallUser)
        self.assertEqual(removed.raw_id, USER_ID)
        self.identity.delete_user.assert_awaited_once_with(current.user)
        self.assertTrue(current.closing)
        self.assertNotIn(current.capability, self.gateway._sessions)

    async def test_already_removed_participant_does_not_trigger_another_removal(self):
        current = session()
        connection = SimpleNamespace(
            get_participant=AsyncMock(side_effect=not_found()), remove_participant=AsyncMock(),
        )
        await self.gateway.release(current, connection)
        connection.remove_participant.assert_not_awaited()
        self.identity.delete_user.assert_awaited_once()

    def ready_participant(self):
        current = session()
        current.page = self.page
        current.context = self.context
        current.media_ready.set()
        current.authorized = True
        self.gateway._sessions[current.capability] = current
        connection = SimpleNamespace(
            get_participant=AsyncMock(side_effect=not_found()), remove_participant=AsyncMock(),
        )
        return current, connection

    async def test_default_ready_participant_cleanup_is_still_immediate(self):
        current, connection = self.ready_participant()
        await self.gateway.release(current, connection)
        self.identity.delete_user.assert_awaited_once_with(current.user)
        self.identity.revoke_tokens.assert_not_awaited()
        self.assertEqual(current.identity_cleanup_status, "deleted")
        self.assertEqual(self.gateway.retained_identity_count, 0)

    async def test_retention_revokes_only_after_media_departure_and_does_not_delay_release(self):
        current, connection = self.ready_participant()
        events = []

        async def closed():
            events.append("context_closed")

        async def absent(_):
            events.append("participant_absent")
            raise not_found()

        async def revoked(_):
            events.append("revocation_requested")

        self.context.close.side_effect = closed
        connection.get_participant.side_effect = absent
        self.identity.revoke_tokens.side_effect = revoked
        await self.gateway.release(current, connection, retain_identity=True)
        self.assertEqual(events, ["context_closed", "participant_absent", "revocation_requested"])
        self.page.evaluate.assert_awaited_once_with("() => window.nativeVoip.stop()")
        self.assertTrue(current.closing)
        self.assertFalse(current.authorized)
        self.assertNotIn(current.capability, self.gateway._sessions)
        self.assertEqual(current.identity_cleanup_status, "retained")
        self.assertEqual(self.gateway.retained_identity_count, 1)
        self.identity.delete_user.assert_not_awaited()
        await self.gateway.finish_identity_cleanup(current)
        self.identity.delete_user.assert_awaited_once_with(current.user)
        self.assertEqual(current.identity_cleanup_status, "deleted")
        self.assertEqual(self.gateway.retained_identity_count, 0)

    async def test_identity_grace_requests_exactly_fifteen_minutes_then_deletes(self):
        current, connection = self.ready_participant()
        wait_for = asyncio.wait_for
        timeouts = []
        deleted = asyncio.Event()

        async def fast_wait(awaitable, timeout):
            timeouts.append(timeout)
            return await wait_for(awaitable, 0.001 if timeout == IDENTITY_RETENTION_SECONDS else timeout)

        async def delete(_):
            deleted.set()

        self.identity.delete_user.side_effect = delete
        with patch("backend.native_voip.asyncio.wait_for", side_effect=fast_wait):
            await self.gateway.release(current, connection, retain_identity=True)
            self.identity.delete_user.assert_not_awaited()
            await wait_for(deleted.wait(), 1)
            await self.gateway.finish_identity_cleanup(current)
        self.assertEqual(IDENTITY_RETENTION_SECONDS, 900)
        self.assertIn(900, timeouts)
        self.assertEqual(current.identity_cleanup_status, "deleted")
        self.assertEqual(self.gateway.retained_identity_count, 0)

    async def test_shutdown_deletes_a_retained_identity_even_before_timer_task_starts(self):
        current, connection = self.ready_participant()
        await self.gateway.release(current, connection, retain_identity=True)
        self.identity.delete_user.assert_not_awaited()
        await self.gateway.close()
        self.identity.delete_user.assert_awaited_once_with(current.user)
        self.identity.close.assert_awaited_once()
        self.assertEqual(current.identity_cleanup_status, "deleted")
        self.assertEqual(self.gateway.retained_identity_count, 0)

    async def test_repeated_release_does_not_duplicate_or_cancel_retention(self):
        current, connection = self.ready_participant()
        await self.gateway.release(current, connection, retain_identity=True)
        await self.gateway.release(current, connection, retain_identity=True)
        self.identity.revoke_tokens.assert_awaited_once_with(current.user)
        self.identity.delete_user.assert_not_awaited()
        self.assertEqual(self.gateway.retained_identity_count, 1)
        await self.gateway.finish_identity_cleanup(current)
        self.identity.delete_user.assert_awaited_once_with(current.user)

    async def test_no_retention_without_a_verified_media_connection(self):
        current, _ = self.ready_participant()
        await self.gateway.release(current, None, retain_identity=True)
        self.identity.revoke_tokens.assert_not_awaited()
        self.identity.delete_user.assert_awaited_once_with(current.user)
        self.assertEqual(self.gateway.retained_identity_count, 0)

    async def test_no_retention_before_media_readiness(self):
        current, connection = self.ready_participant()
        current.media_ready.clear()
        await self.gateway.release(current, connection, retain_identity=True)
        self.identity.revoke_tokens.assert_not_awaited()
        self.identity.delete_user.assert_awaited_once_with(current.user)

    async def test_failed_participant_removal_deletes_immediately_even_when_retention_requested(self):
        current, connection = self.ready_participant()
        connection.get_participant.side_effect = HttpResponseError("Synthetic lookup failure")
        with self.assertLogs("native_voip", level="ERROR"), self.assertRaises(NativeVoipError):
            await self.gateway.release(current, connection, retain_identity=True)
        self.identity.revoke_tokens.assert_not_awaited()
        self.identity.delete_user.assert_awaited_once_with(current.user)
        self.assertEqual(self.gateway.retained_identity_count, 0)

    async def test_failed_revocation_deletes_immediately_and_does_not_claim_retention_success(self):
        current, connection = self.ready_participant()
        self.identity.revoke_tokens.side_effect = HttpResponseError("Synthetic revoke failure")
        with self.assertLogs("native_voip", level="ERROR"), self.assertRaises(NativeVoipError):
            await self.gateway.release(current, connection, retain_identity=True)
        self.identity.delete_user.assert_awaited_once_with(current.user)
        self.assertEqual(current.identity_cleanup_status, "deleted")
        self.assertEqual(self.gateway.retained_identity_count, 0)

    async def test_deferred_deletion_failure_is_visible_and_retried_at_shutdown(self):
        current, connection = self.ready_participant()
        self.identity.delete_user.side_effect = HttpResponseError("Synthetic deferred deletion failure")
        await self.gateway.release(current, connection, retain_identity=True)
        with self.assertLogs("native_voip", level="ERROR"), self.assertRaises(NativeVoipError):
            await self.gateway.finish_identity_cleanup(current)
        self.assertEqual(current.identity_cleanup_status, "failed")
        self.assertEqual(self.gateway.retained_identity_count, 1)
        self.assertEqual(self.gateway.identity_cleanup_failures, 1)
        self.identity.delete_user.side_effect = None
        await self.gateway.close()
        self.assertEqual(self.identity.delete_user.await_count, 2)
        self.assertEqual(self.gateway.retained_identity_count, 0)
        self.assertEqual(current.identity_cleanup_status, "deleted")

    async def test_cancelling_early_cleanup_wait_does_not_cancel_identity_deletion(self):
        current, connection = self.ready_participant()
        started, finish = asyncio.Event(), asyncio.Event()

        async def delete(_):
            started.set()
            await finish.wait()

        self.identity.delete_user.side_effect = delete
        await self.gateway.release(current, connection, retain_identity=True)
        waiting = asyncio.create_task(self.gateway.finish_identity_cleanup(current))
        await asyncio.wait_for(started.wait(), 1)
        waiting.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await waiting
        finish.set()
        await self.gateway.finish_identity_cleanup(current)
        self.assertEqual(current.identity_cleanup_status, "deleted")
        self.assertEqual(self.gateway.identity_cleanup_failures, 0)

    async def test_identity_cleanup_failure_is_not_reported_as_success(self):
        current = session()
        self.identity.delete_user.side_effect = HttpResponseError("Synthetic deletion failure")
        with self.assertRaises(NativeVoipError):
            await self.gateway.release(current, None)

    async def test_participant_cleanup_failure_still_attempts_identity_cleanup(self):
        current = session()
        connection = SimpleNamespace(
            get_participant=AsyncMock(side_effect=HttpResponseError("Synthetic denial")),
            remove_participant=AsyncMock(),
        )
        with self.assertRaises(NativeVoipError):
            await self.gateway.release(current, connection)
        self.identity.delete_user.assert_awaited_once()


class VoipSocketTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.gateway = NativeVoipGateway("synthetic")
        self.gateway._origin = "http://localhost:12345"
        self.session = session()
        self.gateway._sessions[self.session.capability] = self.session
        app = web.Application()
        app.router.add_get("/audio/{capability}", self.gateway._socket)
        self.client = TestClient(TestServer(app))
        await self.client.start_server()
        self.addAsyncCleanup(self.client.close)

    async def connect(self, origin=None):
        return await self.client.ws_connect(
            "/audio/" + self.session.capability,
            headers={"Origin": origin or self.gateway._origin},
        )

    async def test_unknown_capability_and_wrong_origin_are_rejected(self):
        with self.assertRaises(WSServerHandshakeError):
            await self.client.ws_connect("/audio/wrong", headers={"Origin": self.gateway._origin})
        with self.assertRaises(WSServerHandshakeError):
            await self.connect("http://other.example")

    async def test_call_id_binding_precedes_media_readiness(self):
        ws = await self.connect()
        await ws.send_json({"kind": "registered"})
        await asyncio.wait_for(self.session.registered.wait(), 1)
        await ws.send_json({"kind": "media_ready", "call_id": CONVERSATION})
        await asyncio.wait_for(self.session.failed.wait(), 1)
        self.assertFalse(self.session.media_ready.is_set())
        self.assertFalse(self.session.authorized)
        await ws.close()

    async def test_correct_sdk_call_id_sets_readiness_but_not_native_authorization(self):
        ws = await self.connect()
        await ws.send_json({"kind": "connected", "call_id": CORRELATION})
        await ws.send_json({"kind": "media_ready", "call_id": CORRELATION})
        await asyncio.wait_for(self.session.media_ready.wait(), 1)
        self.assertEqual(self.session.call_id, CORRELATION)
        self.assertFalse(self.session.authorized)
        self.session.closing = True
        await ws.close()
        self.assertFalse(self.session.failed.is_set())

    async def test_duplicate_socket_cannot_take_over_a_session(self):
        ws = await self.connect()
        with self.assertRaises(WSServerHandshakeError):
            await self.connect()
        self.session.closing = True
        await ws.close()

    async def test_lost_socket_is_an_explicit_session_failure(self):
        ws = await self.connect()
        await ws.close()
        await asyncio.wait_for(self.session.failed.wait(), 1)
        self.assertEqual(self.session.error_code, "gateway_socket_closed")


if __name__ == "__main__":
    unittest.main()
