import asyncio
import importlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from astrbot_plugin_message_bridge.runtime_status import RuntimeStatus


class FakeBot:
    def __init__(self, identity=None, groups=None):
        self._wsr_api_clients = {"12345": object()}
        self.identity = identity if identity is not None else {"user_id": 12345, "nickname": "Fixture QQ", "token": "NEVER-EXPORT"}
        self.groups = groups if groups is not None else [{"group_id": 10001, "group_name": "Fixture Group", "cookie": "NEVER-EXPORT"}]
        self.calls = []

    async def call_action(self, action, **params):
        self.calls.append((action, params))
        return self.identity if action == "get_login_info" else self.groups


def fake_context(bot=None, snow=None):
    adapters = [] if bot is None else [SimpleNamespace(
        meta=lambda: SimpleNamespace(name="aiocqhttp", id="qq_bridge"),
        get_client=lambda: bot,
    )]
    metadata = None if snow is None else SimpleNamespace(star_cls=SimpleNamespace(manager=snow))
    return SimpleNamespace(platform_manager=SimpleNamespace(get_insts=lambda: adapters),
                           get_registered_star=lambda name: metadata)


class RuntimeStatusTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.lark = SimpleNamespace(_last_state="waiting_login", client=None, user_id="", account_id="default")
        self.instances = []

    async def asyncTearDown(self):
        for instance in self.instances:
            await instance.stop()
        self.temp.cleanup()

    def monitor(self, context=None, **options):
        result = RuntimeStatus(context or fake_context(), self.directory, self.lark, **options)
        self.instances.append(result)
        return result

    def exported(self):
        return json.loads((self.directory / "current_runtime_status.json").read_text(encoding="utf-8"))

    async def test_qq_identity_and_groups_require_live_onebot_responses_and_are_allowlisted(self):
        bot = FakeBot()
        monitor = self.monitor(fake_context(bot))
        state = await monitor.refresh()
        account = state["qq"]["accounts"][0]
        self.assertTrue(state["qq"]["identity_verified"])
        self.assertEqual(account["user_id"], "12345")
        self.assertEqual(account["groups_status"], "verified")
        self.assertEqual(account["groups"], [{"group_id": "10001", "group_name": "Fixture Group"}])
        self.assertEqual(bot.calls, [("get_login_info", {"self_id": "12345"}), ("get_group_list", {"self_id": "12345"})])
        self.assertNotIn("NEVER-EXPORT", json.dumps(state))
        self.assertTrue(self.exported()["updated_at"])

    async def test_listening_adapter_without_reverse_ws_never_counts_as_qq_login(self):
        bot = FakeBot()
        bot._wsr_api_clients.clear()
        state = await self.monitor(fake_context(bot)).refresh()
        self.assertFalse(state["qq"]["connected"])
        self.assertFalse(state["qq"]["identity_verified"])
        self.assertEqual(bot.calls, [])

    async def test_identity_mismatch_never_loads_groups_or_claims_verified(self):
        bot = FakeBot(identity={"user_id": "different-account"})
        state = await self.monitor(fake_context(bot)).refresh()
        self.assertTrue(state["qq"]["connected"])
        self.assertFalse(state["qq"]["identity_verified"])
        self.assertEqual(len(bot.calls), 1)

    async def test_failed_group_sync_does_not_reuse_a_previous_verified_list(self):
        bot = FakeBot()
        monitor = self.monitor(fake_context(bot))
        self.assertEqual((await monitor.refresh())["qq"]["accounts"][0]["groups_status"], "verified")
        bot.groups = {"error": "NEVER-EXPORT"}
        monitor._refreshed_at -= 3
        account = (await monitor.refresh())["qq"]["accounts"][0]
        self.assertTrue(account["identity_verified"])
        self.assertEqual(account["groups_status"], "unavailable")
        self.assertEqual(account["groups"], [])

    async def test_disconnect_during_probe_clears_identity_and_groups(self):
        bot = FakeBot()
        original = bot.call_action

        async def disconnect(action, **params):
            result = await original(action, **params)
            if action == "get_group_list":
                bot._wsr_api_clients.clear()
            return result

        bot.call_action = disconnect
        account = (await self.monitor(fake_context(bot)).refresh())["qq"]["accounts"][0]
        self.assertEqual(account["state"], "disconnected")
        self.assertFalse(account["identity_verified"])
        self.assertEqual(account["groups"], [])

    async def test_timeout_exports_no_exception_body_and_does_not_hang(self):
        bot = FakeBot()

        async def slow(*args, **kwargs):
            await asyncio.Event().wait()

        bot.call_action = slow
        state = await asyncio.wait_for(self.monitor(fake_context(bot), timeout=0.01).refresh(), 0.2)
        self.assertFalse(state["qq"]["identity_verified"])
        self.assertEqual(state["qq"]["accounts"][0]["state"], "identity_unverified")

    async def test_snowluma_manager_status_is_filtered_without_credentials_or_config(self):
        async def status():
            return {"installed": True, "running": True, "auth_ok": True, "consent_required": False,
                    "accounts": [{"uin": "12345", "nickname": "Fixture QQ", "token": "NEVER-EXPORT"}],
                    "webui_port": 5100, "webui_url": "https://evil.example/NEVER-EXPORT",
                    "config": {"password": "NEVER-EXPORT"}, "last_error": "NEVER-EXPORT"}

        async def probe(method, path, **kwargs):
            self.assertEqual(method, "GET")
            return 200, {"/api/agreements": {"consentRequired": False},
                         "/api/auth/state": {"mustChangePassword": False}}[path]

        snow = SimpleNamespace(status=status, api=SimpleNamespace(token="fixture", raw_request=probe))
        state = await self.monitor(fake_context(snow=snow)).refresh()
        self.assertGreater(state["snowluma"].pop("send_checked_at"), 0)
        self.assertEqual(state["snowluma"], {"state": "known", "installed": True, "running": True, "auth_ok": True,
                                            "consent_required": False, "accounts": [{"uin": "12345", "nickname": "Fixture QQ"}],
                                            "webui_port": 5100, "webui_url": "http://127.0.0.1:5100/",
                                            "send_ready": True, "send_state": "ready", "must_change_password": False})
        self.assertNotIn("NEVER-EXPORT", json.dumps(state))

    async def test_missing_snowluma_manager_remains_explicitly_unknown(self):
        state = await self.monitor().refresh()
        self.assertEqual(state["snowluma"]["state"], "unknown")
        self.assertIsNone(state["snowluma"]["installed"])
        self.assertIsNone(state["snowluma"]["running"])

    async def test_feishu_state_and_both_receipt_counters_share_authoritative_export(self):
        monitor = self.monitor()
        self.lark._last_state = "running"
        self.lark.client = object()
        self.lark.user_id = "fixture-user"
        monitor.update_feishu()
        monitor.record_receive("qq", "10001")
        monitor.record_receive("feishu", "fixture-chat")
        monitor.record_receive("feishu", "fixture-chat-2")
        state = self.exported()
        self.assertTrue(state["feishu"]["identity_verified"])
        self.assertEqual(state["feishu"]["user_id"], "fixture-user")
        self.assertEqual(state["qq"]["received_count"], 1)
        self.assertEqual(state["feishu"]["received_count"], 2)
        self.assertEqual(state["feishu"]["last_chat_id"], "fixture-chat-2")
        self.assertTrue(state["feishu"]["last_received_at"])
        monitor.record_delivery("feishu", True, "fixture-msg-id")
        monitor.record_delivery("qq", False)
        state = self.exported()
        self.assertEqual(state["feishu"]["sent_count"], 1)
        self.assertEqual(state["feishu"]["last_send_status"], "accepted")
        self.assertEqual(state["feishu"]["last_message_id"], "fixture-msg-id")
        self.assertEqual(state["qq"]["failed_count"], 1)
        self.assertEqual(state["qq"]["last_send_status"], "failed")
        self.lark._last_state = "reconnecting"
        monitor.update_feishu()
        self.assertFalse(self.exported()["feishu"]["connected"])

    async def test_restart_does_not_inherit_prior_process_activity_or_verified_identity(self):
        first = self.monitor()
        first.record_receive("qq", "10001")
        first.record_receive("feishu", "fixture-chat")
        second = self.monitor()
        self.assertEqual(self.exported()["qq"]["received_count"], 0)
        self.assertEqual(self.exported()["feishu"]["received_count"], 0)
        self.assertFalse(self.exported()["qq"]["identity_verified"])

    async def test_background_refresh_is_cancelled_and_stopped_is_written_on_terminate(self):
        bot = FakeBot()
        called = asyncio.Event()
        original_call = bot.call_action

        async def observe_call(action, **params):
            called.set()
            return await original_call(action, **params)

        bot.call_action = observe_call
        monitor = self.monitor(fake_context(bot), interval=0.01)
        monitor.start()
        task = monitor._task
        monitor.start()
        self.assertIs(task, monitor._task)
        await asyncio.wait_for(called.wait(), 1)
        self.assertTrue(bot.calls)
        await monitor.stop()
        count = len(bot.calls)
        await asyncio.sleep(0.03)
        self.assertEqual(len(bot.calls), count)
        self.assertIsNone(monitor._task)
        self.assertEqual(self.exported()["qq"]["state"], "stopped")
        self.assertFalse(self.exported()["qq"]["connected"])


class PluginHotReloadTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # Reuse the existing transport-only AstrBot fixture, never an installed
        # account or a real platform send operation.
        fixture = sys.modules.get("test_plugin") or sys.modules.get("tests.test_plugin")
        if fixture is None:
            fixture = importlib.import_module("tests.test_plugin" if __package__ else "test_plugin")
        self.fixture = fixture
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        fixture.FakeStarTools.data_dir = self.directory
        self.rules = self.directory / "bridge_rules.json"
        self.rules.write_text('{"rules":[]}', encoding="utf-8")
        self.bridge = fixture.plugin.MessageBridge(fixture.FakeContext())
        self.sent = []

        def send_msg(message, chat_id):
            self.sent.append((message, chat_id))
            return "fixture-message"

        self.bridge.larkx_bridge.client = SimpleNamespace(send_msg=send_msg)

    async def asyncTearDown(self):
        await self.bridge.terminate()
        self.temp.cleanup()

    def save(self, destination="fixture-new-chat"):
        self.rules.write_text(json.dumps({"rules": [{
            "name": "new fixture route", "source_platform": "aiocqhttp", "source_platform_id": "aiocqhttp_main",
            "source_message_type": "GroupMessage", "source_session_id": "10001",
            "target_umo": f"larkx:GroupMessage:{destination}",
        }]}), encoding="utf-8")

    async def test_saved_mapping_is_applied_by_the_same_running_plugin_on_next_message(self):
        await self.bridge.on_platform_message(self.fixture.FakeEvent())
        self.assertEqual(self.sent, [])
        self.save()
        await self.bridge.on_platform_message(self.fixture.FakeEvent())
        self.assertEqual(self.sent[0][1], "fixture-new-chat")
        state = json.loads((self.directory / "current_runtime_status.json").read_text(encoding="utf-8"))
        self.assertEqual(state["qq"]["received_count"], 2)

    async def test_invalid_or_deleted_mapping_stops_previous_route_without_restart(self):
        self.save()
        await self.bridge.on_platform_message(self.fixture.FakeEvent())
        self.assertEqual(len(self.sent), 1)
        self.rules.write_text('{"rules":', encoding="utf-8")
        with self.assertLogs("test.astrbot", level="ERROR"):
            await self.bridge.on_platform_message(self.fixture.FakeEvent(text="different message"))
        self.assertEqual(len(self.sent), 1)
        self.rules.unlink()
        await self.bridge.on_platform_message(self.fixture.FakeEvent(text="third message"))
        self.assertEqual(len(self.sent), 1)
        self.assertFalse(self.rules.exists())

    async def test_chat_refresh_without_name_preserves_previous_metadata(self):
        chat_file = self.directory / "larkx_chats.json"
        chat_file.write_text(json.dumps({"chats": [{
            "chat_id": "fixture-chat", "name": "Useful group name", "message_type": "GroupMessage",
            "source": "fixture-source", "custom_metadata": "keep-me",
        }]}), encoding="utf-8")
        self.bridge._remember_larkx_chat({"chat_id": "fixture-chat"}, "fixture-chat")
        saved = json.loads((self.directory / "larkx_chats.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["chats"], [{
            "chat_id": "fixture-chat", "name": "Useful group name", "message_type": "GroupMessage",
            "source": "fixture-source", "custom_metadata": "keep-me",
            "last_seen": saved["chats"][0]["last_seen"],
        }])

    async def test_real_send_acceptance_counts_target_and_failure_without_content_or_loop(self):
        self.save()
        await self.bridge.on_platform_message(self.fixture.FakeEvent())
        state = json.loads((self.directory / "current_runtime_status.json").read_text(encoding="utf-8"))
        self.assertEqual(state["feishu"]["sent_count"], 1)
        self.assertEqual(state["feishu"]["last_send_status"], "accepted")
        self.assertNotIn("hello", json.dumps(state))

        self.bridge.larkx_bridge.client = SimpleNamespace(send_msg=lambda *_: False)
        failing_event = self.fixture.FakeEvent(text="new fixture message")
        failing_event.message_obj.message_id = "fixture-message-2"
        await self.bridge.on_platform_message(failing_event)
        state = json.loads((self.directory / "current_runtime_status.json").read_text(encoding="utf-8"))
        self.assertEqual(state["feishu"]["sent_count"], 1)
        self.assertEqual(state["feishu"]["failed_count"], 1)
        self.assertEqual(state["feishu"]["last_send_status"], "failed")
        self.assertNotIn("new fixture message", json.dumps(state))

        before = state["feishu"]["sent_count"]
        self.bridge.larkx_bridge.client = SimpleNamespace(send_msg=lambda *_: "fixture-message-receipt-3")
        echo_event = self.fixture.FakeEvent(text="[搬运工V2] prior echo")
        echo_event.message_obj.message_id = "fixture-message-3"
        await self.bridge.on_platform_message(echo_event)
        state = json.loads((self.directory / "current_runtime_status.json").read_text(encoding="utf-8"))
        self.assertEqual(state["feishu"]["sent_count"], before + 1)
        self.assertEqual(state["feishu"]["failed_count"], 1)

    async def test_feishu_to_qq_send_failure_is_recorded_on_qq_destination(self):
        self.rules.write_text(json.dumps({"rules": [{
            "name": "fixture Feishu to QQ", "source_platform": "larkx", "source_platform_id": "default",
            "source_message_type": "GroupMessage", "source_session_id": "oc_1",
            "target_umo": "aiocqhttp_main:GroupMessage:10001",
        }]}), encoding="utf-8")
        with patch.object(self.fixture.plugin, "send_qq", side_effect=RuntimeError("fixture transport unavailable")) as send:
            await self.bridge.on_larkx_message({
                "chat_id": "oc_1", "chat_type": 2, "from_id": "fixture-member",
                "msg_id": "fixture-lark-message", "content": "never export this body",
            }, "default", "fixture-self")
        send.assert_called_once()
        state = json.loads((self.directory / "current_runtime_status.json").read_text(encoding="utf-8"))
        self.assertEqual(state["qq"]["failed_count"], 1)
        self.assertEqual(state["qq"]["last_send_status"], "failed")
        self.assertNotIn("never export this body", json.dumps(state))


async def async_false(*_args, **_kwargs):
    return False


if __name__ == "__main__":
    unittest.main()
