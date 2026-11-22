"""Isolated regression tests. No accounts, network or live database access."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from astrbot_plugin_message_bridge import qq_transport as transport
from astrbot_plugin_message_bridge.bridge_core import BridgeRouter, SeenEvents
from astrbot_plugin_message_bridge.larkx_inbox import LarkXInbox
from astrbot_plugin_message_bridge.larkx_bridge import LarkAgentXBridge, _SendGatePending
from astrbot_plugin_message_bridge.runtime_status import RuntimeStatus


class FakeAPI:
    def __init__(self, consent=False, password=False):
        self.token = "fixture-only"
        self.consent = consent
        self.password = password
        self.calls = []
        self.unauthorized_once = False

    async def raw_request(self, method, path, **kwargs):
        self.calls.append((method, path))
        await asyncio.sleep(0)
        if self.unauthorized_once:
            self.unauthorized_once = False
            return 401, {"message": "fixture secret must never escape"}
        if path == "/api/agreements":
            return 200, {"consentRequired": self.consent, "version": "fixture-version"}
        if path == "/api/auth/state":
            return 200, {"mustChangePassword": self.password}
        raise AssertionError("No send actions are allowed in gate tests")


def manager(api, *, auto=False):
    owner = SimpleNamespace(api=api, plugin=SimpleNamespace(config={"auto_accept_agreements": auto}))

    async def ensure_api(**kwargs):
        return owner.api

    async def accept():
        api.consent = False
        return {"ok": True}

    owner.ensure_api = AsyncMock(side_effect=ensure_api)
    owner.accept_agreements = AsyncMock(side_effect=accept)
    return owner


class GateTests(unittest.IsolatedAsyncioTestCase):
    async def test_consent_blocks_even_when_status_is_online(self):
        api = FakeAPI(consent=True)
        owner = manager(api)
        result = await transport.snowluma_send_gate(owner)
        self.assertFalse(result["send_ready"])
        self.assertEqual(result["send_state"], "consent_required")
        owner.accept_agreements.assert_not_awaited()
        self.assertEqual(api.calls, [("GET", "/api/agreements")])

    async def test_explicit_auto_preference_accepts_then_rechecks(self):
        api = FakeAPI(consent=True)
        owner = manager(api, auto=True)
        result = await transport.snowluma_send_gate(owner)
        self.assertTrue(result["send_ready"])
        owner.accept_agreements.assert_awaited_once()
        self.assertEqual(api.calls.count(("GET", "/api/agreements")), 2)

    async def test_failed_auto_accept_never_reports_ready(self):
        owner = manager(FakeAPI(consent=True), auto=True)
        owner.accept_agreements = AsyncMock(return_value={"ok": False})
        result = await transport.snowluma_send_gate(owner)
        self.assertEqual(result["send_state"], "consent_required")

    async def test_auto_requires_boolean_true(self):
        owner = manager(FakeAPI(consent=True), auto="false")
        self.assertEqual((await transport.snowluma_send_gate(owner))["send_state"], "consent_required")
        owner.accept_agreements.assert_not_awaited()

    async def test_password_change_gate(self):
        owner = manager(FakeAPI(password=True))
        self.assertEqual((await transport.snowluma_send_gate(owner))["send_state"], "password_change_required")

    async def test_ready_requires_both_readonly_checks(self):
        api = FakeAPI()
        self.assertTrue((await transport.snowluma_send_gate(manager(api)))["send_ready"])
        self.assertEqual(api.calls, [("GET", "/api/agreements"), ("GET", "/api/auth/state")])

    async def test_invalid_auth_metadata_fails_closed(self):
        api = FakeAPI()
        api.raw_request = AsyncMock(return_value=(200, {}))
        self.assertFalse((await transport.snowluma_send_gate(manager(api)))["send_ready"])

    async def test_transport_errors_are_safe_codes(self):
        api = FakeAPI()
        api.raw_request = AsyncMock(side_effect=RuntimeError("private credential body"))
        result = await transport.snowluma_send_gate(manager(api))
        self.assertEqual(result["send_state"], "unavailable")
        self.assertNotIn("private", json.dumps(result))

    async def test_expired_auth_refreshes_before_any_send(self):
        api = FakeAPI()
        api.unauthorized_once = True
        owner = manager(api)
        result = await transport.snowluma_send_gate(owner)
        self.assertTrue(result["send_ready"])
        owner.ensure_api.assert_awaited_once()

    async def test_simultaneous_checks_are_coalesced_and_cached(self):
        api = FakeAPI()
        owner = manager(api)
        results = await asyncio.gather(*(transport.snowluma_send_gate(owner) for _ in range(30)))
        self.assertTrue(all(x["send_ready"] for x in results))
        self.assertEqual(len(api.calls), 2)
        await transport.snowluma_send_gate(owner)
        self.assertEqual(len(api.calls), 2)

    async def test_force_check_recovers_after_consent(self):
        api = FakeAPI(consent=True)
        owner = manager(api)
        self.assertFalse((await transport.snowluma_send_gate(owner))["send_ready"])
        api.consent = False
        self.assertTrue((await transport.snowluma_send_gate(owner, force=True))["send_ready"])

    async def test_replaced_api_invalidates_cache(self):
        owner = manager(FakeAPI())
        self.assertTrue((await transport.snowluma_send_gate(owner))["send_ready"])
        owner.api = FakeAPI(consent=True)
        self.assertFalse((await transport.snowluma_send_gate(owner))["send_ready"])

    async def test_context_blocks_without_fallback(self):
        owner = manager(FakeAPI(consent=True))
        context = SimpleNamespace(get_registered_star=lambda name: SimpleNamespace(star_cls=SimpleNamespace(manager=owner)))
        with self.assertRaises(transport.QQSendBlocked) as result:
            await transport._snowluma_api(context)
        self.assertEqual(result.exception.code, "consent_required")

    async def test_no_manager_keeps_onebot_path(self):
        self.assertIsNone(await transport._snowluma_api(SimpleNamespace()))

    async def test_actual_send_rejection_retains_safe_reason(self):
        api = SimpleNamespace(raw_request=AsyncMock(return_value=(403, {"consentRequired": True, "message": "secret"})))
        with self.assertRaises(transport.QQSendBlocked) as error:
            await transport._send_through_snowluma(api, "10001", "send_group_msg", {})
        self.assertEqual(error.exception.code, "consent_required")
        self.assertEqual(error.exception.http_status, 403)
        self.assertNotIn("secret", str(error.exception))

    async def test_timeout_stays_uncertain_and_not_retried(self):
        api = SimpleNamespace(raw_request=AsyncMock(side_effect=TimeoutError()))
        with self.assertRaises(transport.QQDeliveryUncertain):
            await transport._send_through_snowluma(api, "10001", "send_group_msg", {})
        self.assertEqual(api.raw_request.await_count, 1)

    async def test_accepted_receipt_is_retained(self):
        api = SimpleNamespace(raw_request=AsyncMock(return_value=(200, {"status": "ok", "retcode": 0, "data": {"message_id": -123}})))
        self.assertEqual((await transport._send_through_snowluma(api, "10001", "send_group_msg", {}))["message_id"], -123)


class ResultGuardTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from aiocqhttp.api_impl import ResultStore
        self.store = ResultStore
        self.previous = ResultStore.__dict__["add"]
        ResultStore._futures.clear()
        transport.install_onebot_result_guard()

    async def asyncTearDown(self):
        self.store.add = self.previous
        self.store._futures.clear()

    async def test_first_response_wins_duplicate_ignored(self):
        f = asyncio.get_running_loop().create_future()
        self.store._futures[777] = f
        self.store.add({"echo": {"seq": 777}, "data": "first"})
        self.store.add({"echo": {"seq": 777}, "data": "duplicate"})
        self.assertEqual(f.result()["data"], "first")

    async def test_cancelled_late_response_is_ignored(self):
        f = asyncio.get_running_loop().create_future()
        self.store._futures[778] = f
        f.cancel()
        self.store.add({"echo": {"seq": 778}})
        self.assertTrue(f.cancelled())

    async def test_unknown_response_and_malformed_are_ignored(self):
        for payload in [None, [], {}, {"echo": "bad"}, {"echo": {"seq": 999}}]:
            self.store.add(payload)

    async def test_normal_fetch_still_settles_and_cleans(self):
        task = asyncio.create_task(self.store.fetch(779, 1))
        await asyncio.sleep(0)
        self.store.add({"echo": {"seq": 779}, "status": "ok"})
        self.assertEqual((await task)["status"], "ok")
        self.assertNotIn(779, self.store._futures)

    async def test_timeout_then_late_reply_does_not_raise(self):
        from aiocqhttp.exceptions import NetworkError
        with self.assertRaises(NetworkError):
            await self.store.fetch(780, 0.001)
        self.store.add({"echo": {"seq": 780}})
        self.assertNotIn(780, self.store._futures)

    async def test_guard_install_is_idempotent(self):
        first = self.store.add.__func__
        transport.install_onebot_result_guard()
        self.assertIs(first, self.store.add.__func__)


class QueueTests(unittest.IsolatedAsyncioTestCase):
    async def test_gate_does_not_exhaust_retry_budget_or_drop_body(self):
        with tempfile.TemporaryDirectory() as directory:
            box = LarkXInbox(Path(directory) / "inbox.sqlite3")
            payload = {"msg_id": "fixture-1", "chat_id": "fixture-chat", "content": "fixture body"}
            self.assertTrue(box.enqueue("message", payload, "default", "fixture-user", now=0))
            for index in range(20):
                item = box.claim(account_id="default", user_id="fixture-user", now=index * 10)
                self.assertEqual(item["attempts"], 1)
                self.assertEqual(item["payload"], payload)
                box.defer(item["id"], "consent_required", now=index * 10)
            self.assertEqual(box.stats(), {"pending": 1, "processing": 0, "failed": 0, "done": 0})
            item = box.claim(account_id="default", user_id="fixture-user", now=1000)
            box.complete(item["id"])
            self.assertEqual(box.stats()["done"], 1)
            box.close()

    async def test_gate_keeps_other_accounts_isolated(self):
        with tempfile.TemporaryDirectory() as directory:
            box = LarkXInbox(Path(directory) / "inbox.sqlite3")
            box.enqueue("message", {"msg_id": "a"}, "default", "user-a", now=0)
            self.assertIsNone(box.claim(account_id="default", user_id="user-b", now=100))
            box.close()

    async def test_known_gate_precedes_hydrate_and_delivery(self):
        bridge = object.__new__(LarkAgentXBridge)
        bridge.account_id = "default"
        bridge.user_id = "fixture-user"
        bridge.plugin = SimpleNamespace(larkx_send_gate=AsyncMock(return_value="consent_required"), on_larkx_message=AsyncMock())
        bridge.client = SimpleNamespace(messages_by_ids=lambda ids: self.fail("Must not hydrate behind gate"))
        with self.assertRaises(_SendGatePending):
            await bridge._process_inbox({"kind": "message", "account_id": "default", "user_id": "fixture-user", "payload": {"msg_id": "fixture", "msg_type": 2, "is_truncated": True}})
        bridge.plugin.on_larkx_message.assert_not_awaited()

    async def test_real_consumer_defers_then_completes_without_spending_attempts(self):
        with tempfile.TemporaryDirectory() as directory:
            box = LarkXInbox(Path(directory) / "inbox.sqlite3")
            payload = {"msg_id": "fixture-consumer", "chat_id": "fixture-chat", "from_id": "other", "msg_type": 2,
                       "content": "fixture", "is_truncated": False}
            box.enqueue("message", payload, "default", "fixture-user")
            bridge = object.__new__(LarkAgentXBridge)
            bridge.account_id = "default"
            bridge.user_id = "fixture-user"
            bridge.client = object()
            bridge._inbox = box
            bridge._inbox_wakeup = asyncio.Event()
            bridge._stop = asyncio.Event()
            bridge._connection_workers = set()
            bridge.recovery_message_current = lambda message: True
            blocked = True
            async def gate(*args, **kwargs):
                return "consent_required" if blocked else ""
            delivered = asyncio.Event()
            async def send(*args):
                delivered.set()
                return True
            bridge.plugin = SimpleNamespace(larkx_send_gate=gate, on_larkx_message=send, log_larkx_state=lambda *args: None)
            deferred = asyncio.Event()
            actual_defer = box.defer
            def mark_defer(*args, **kwargs):
                actual_defer(*args, **kwargs, delay=1)
                loop.call_soon_threadsafe(deferred.set)
            loop = asyncio.get_running_loop()
            box.defer = mark_defer
            task = asyncio.create_task(bridge._receive_one_worker())
            try:
                await asyncio.wait_for(deferred.wait(), 3)
                self.assertFalse(delivered.is_set())
                self.assertEqual(box.stats()["failed"], 0)
                blocked = False
                bridge._inbox_wakeup.set()
                await asyncio.wait_for(delivered.wait(), 4)
                deadline = loop.time() + 3
                while box.stats()["done"] != 1 and loop.time() < deadline:
                    await asyncio.sleep(0.01)
                self.assertEqual(box.stats(), {"pending": 0, "processing": 0, "failed": 0, "done": 1})
            finally:
                bridge._stop.set()
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                box.close()


class StatusTests(unittest.IsolatedAsyncioTestCase):
    async def test_concurrent_refresh_does_not_flood_onebot(self):
        with tempfile.TemporaryDirectory() as directory:
            status = RuntimeStatus(SimpleNamespace(), Path(directory), None)
            status._qq = AsyncMock(return_value={"state": "connected"})
            status._snowluma = AsyncMock(return_value={"state": "known"})
            await asyncio.gather(*(status.refresh() for _ in range(40)))
            self.assertEqual(status._qq.await_count, 1)
            self.assertEqual(status._snowluma.await_count, 1)

    async def test_raw_online_state_does_not_override_real_gate(self):
        api = FakeAPI(consent=True)
        owner = manager(api)
        owner.status = AsyncMock(return_value={"running": True, "auth_ok": True, "consent_required": False, "webui_port": 5099})
        context = SimpleNamespace(get_registered_star=lambda name: SimpleNamespace(star_cls=SimpleNamespace(manager=owner)))
        with tempfile.TemporaryDirectory() as directory:
            status = RuntimeStatus(context, Path(directory), None)
            result = await status._snowluma()
            self.assertTrue(result["consent_required"])
            self.assertFalse(result["send_ready"])


class PluginRegressionTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        from astrbot_plugin_message_bridge.main import MessageBridge
        from astrbot.api.event import MessageChain
        cls.MessageBridge = MessageBridge
        cls.MessageChain = MessageChain

    def fixture(self, directory):
        plugin = object.__new__(self.MessageBridge)
        plugin.data_dir = Path(directory)
        plugin.context = SimpleNamespace()
        plugin._chat_cache_lock = threading.RLock()
        plugin.log_larkx_error = lambda *args: None
        plugin._reload_router_if_changed = lambda: None
        plugin.larkx_bridge = SimpleNamespace(user_id="fixture-user")
        plugin.router = BridgeRouter({"rules": [{"name": "fixture", "source_platform": "larkx",
            "source_session_id": "fixture-chat", "source_platform_id": "default", "include_self": True,
            "target_umo": "qq_bridge:GroupMessage:100001"}]})
        return plugin

    async def test_qq_gate_does_not_block_unmapped_message(self):
        with tempfile.TemporaryDirectory() as directory:
            plugin = self.fixture(directory)
            with patch("astrbot_plugin_message_bridge.main.qq_send_gate", new=AsyncMock()) as probe:
                self.assertEqual(await plugin.larkx_send_gate({"chat_id": "other-chat"}, "default"), "")
                probe.assert_not_awaited()

    async def test_qq_gate_does_not_block_feishu_only_route(self):
        with tempfile.TemporaryDirectory() as directory:
            plugin = self.fixture(directory)
            plugin.router = BridgeRouter({"rules": [{"source_platform": "larkx", "source_session_id": "fixture-chat",
                "target_umo": "larkx:GroupMessage:other-chat"}]})
            with patch("astrbot_plugin_message_bridge.main.qq_send_gate", new=AsyncMock()) as probe:
                self.assertEqual(await plugin.larkx_send_gate({"chat_id": "fixture-chat", "chat_type": 2}, "default"), "")
                probe.assert_not_awaited()

    async def test_qq_gate_reports_block_for_matching_route(self):
        with tempfile.TemporaryDirectory() as directory:
            plugin = self.fixture(directory)
            with patch("astrbot_plugin_message_bridge.main.qq_send_gate", new=AsyncMock(return_value={"send_ready": False, "send_state": "consent_required"})):
                self.assertEqual(await plugin.larkx_send_gate({"chat_id": "fixture-chat", "chat_type": 2}, "default"), "consent_required")

    async def test_qq_gate_allows_independent_feishu_route_before_deferral(self):
        with tempfile.TemporaryDirectory() as directory:
            plugin = self.fixture(directory)
            plugin.router = BridgeRouter({"rules": [
                {"source_platform": "larkx", "source_session_id": "fixture-chat", "target_umo": "qq_bridge:GroupMessage:100001"},
                {"source_platform": "larkx", "source_session_id": "fixture-chat", "target_umo": "larkx:GroupMessage:other-chat"}]})
            with patch("astrbot_plugin_message_bridge.main.qq_send_gate", new=AsyncMock(return_value={"send_ready": False, "send_state": "consent_required"})) as probe:
                message = {"chat_id": "fixture-chat", "chat_type": 2}
                self.assertEqual(await plugin.larkx_send_gate(message, "default"), "")
                probe.assert_not_awaited()
                self.assertEqual(await plugin.larkx_send_gate(message, "default", include_mixed=True), "consent_required")

    async def test_self_message_optout_still_applies_before_gate(self):
        with tempfile.TemporaryDirectory() as directory:
            plugin = self.fixture(directory)
            plugin.router = BridgeRouter({"rules": [{"source_platform": "larkx", "source_session_id": "fixture-chat",
                "target_umo": "qq_bridge:GroupMessage:100001", "include_self": False}]})
            with patch("astrbot_plugin_message_bridge.main.qq_send_gate", new=AsyncMock()) as probe:
                self.assertEqual(await plugin.larkx_send_gate({"chat_id": "fixture-chat", "chat_type": 2, "from_id": "fixture-user"}, "default"), "")
                probe.assert_not_awaited()

    async def test_chat_cache_keeps_all_parallel_updates(self):
        with tempfile.TemporaryDirectory() as directory:
            plugin = self.fixture(directory)
            await asyncio.gather(*(asyncio.to_thread(plugin._remember_larkx_chat, {"chat_type": 2, "chat_name": f"fixture {i}"}, f"chat-{i}") for i in range(35)))
            chats = json.loads((Path(directory) / "larkx_chats.json").read_text(encoding="utf-8"))["chats"]
            self.assertEqual(len(chats), 35)
            self.assertFalse(list(Path(directory).glob("*.tmp")))

    async def test_blocked_reservation_is_released_not_silently_confirmed(self):
        from astrbot_plugin_message_bridge.bridge_core import Route
        with tempfile.TemporaryDirectory() as directory:
            plugin = self.fixture(directory)
            plugin.seen = SeenEvents(path=Path(directory) / "seen.sqlite3")
            plugin._qq_delivery_lock = asyncio.Lock()
            plugin.trace_larkx = AsyncMock()
            plugin._deliver_chain = AsyncMock(return_value=False)
            route = Route(name="fixture", source_platform="larkx", source_session_id="fixture-chat", target_umo="qq_bridge:GroupMessage:100001")
            result = await plugin._deliver_reserved("fixture-key", route, self.MessageChain().message("fixture"))
            self.assertFalse(result)
            self.assertFalse(plugin.seen.contains("fixture-key"))

    async def test_received_message_reaches_sender_after_gate_recovery(self):
        with tempfile.TemporaryDirectory() as directory:
            bridge = object.__new__(LarkAgentXBridge)
            bridge.account_id = "default"
            bridge.user_id = "fixture-user"
            bridge.plugin = SimpleNamespace(larkx_send_gate=AsyncMock(return_value=""), on_larkx_message=AsyncMock(return_value=True))
            bridge.recovery_message_current = lambda message: True
            item = {"kind": "message", "account_id": "default", "user_id": "fixture-user", "payload": {
                "msg_id": "fixture-1", "chat_id": "fixture-chat", "from_id": "fixture-sender", "msg_type": 2,
                "content": "fixture", "is_truncated": False}}
            self.assertTrue(await bridge._process_inbox(item))
            bridge.plugin.on_larkx_message.assert_awaited_once_with(item["payload"], "default", "fixture-user")


if __name__ == "__main__":
    unittest.main(verbosity=2)
