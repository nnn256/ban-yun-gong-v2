import json
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch

import workbench_server as server


def route(source, source_id, source_session, target_id, target_session, name):
    return {
        "name": name,
        "enabled": True,
        "source_platform": source,
        "source_platform_id": source_id,
        "source_message_type": "GroupMessage",
        "source_session_id": source_session,
        "target_umo": f"{target_id}:GroupMessage:{target_session}",
    }


class WorkbenchConfigTests(unittest.TestCase):
    def test_fresh_plugin_runtime_overrides_stale_larkx_bridge_file(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            (home / "bridge-status.json").write_text(
                json.dumps({"state": "stopped", "message": "old stop state"}),
                encoding="utf-8",
            )
            with (
                patch.object(server, "LARKX_HOME", home),
                patch.object(server, "read_runtime_status", return_value={
                    "available": True,
                    "stale": False,
                    "feishu": {"state": "running", "connected": True},
                }),
                patch.object(server, "rules_path", return_value=home / "bridge_rules.json"),
                patch.object(server, "local_feishu_desktop_status", return_value={"running": True, "process_count": 1}),
                patch.object(server, "larkx_monitor_status", return_value={"received_count": 0}),
            ):
                result = server.larkx_status()

        self.assertEqual(result["bridge_state"], "running")
        self.assertEqual(result["bridge_message"], "飞书个人账号已验证，消息监听已启动")

    def test_integration_status_is_a_secret_free_allowlist(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            plugin = root / "plugin"
            plugin.mkdir()
            for name in server.PLUGIN_FILES:
                (plugin / name).touch()
            lark_status = {
                "has_credentials": True,
                "user_id": "user-safe-id",
                "bridge_state": "running",
                "bridge_message": "connected",
                "agent_backend": "none",
                "monitor": {"received_count": 4, "last_received_at": "now", "last_chat_id": "oc_group"},
                "chats": [{"chat_id": "oc_group", "name": "Team", "message_type": "GroupMessage", "last_seen": "now", "content": "must not escape"}],
                "cookies": "secret-cookie",
                "image_data": "secret-qr",
            }
            with (
                patch.object(server, "load_settings", return_value={"astrbot_root": str(root), "astrbot_port": 6185}),
                patch.object(server, "astrbot_root", return_value=root),
                patch.object(server, "plugin_destination", return_value=plugin),
                patch.object(server, "port_open", side_effect=lambda port, **_kwargs: port in {6185, 6199}),
                patch.object(server, "read_rules", return_value={"rules": [{"enabled": True}, {"enabled": False}]}),
                patch.object(server, "larkx_status", return_value=lark_status),
                patch.object(server, "snowluma_onebot_configured", return_value=True),
                patch.object(server, "_snowluma_first_run_pending", return_value=False),
                patch.object(server, "read_runtime_status", return_value={"available": True, "qq": {"connected": True}, "feishu": {"state": "running", "received_count": 4, "last_chat_id": "oc_group"}, "snowluma": {"running": True, "webui_port": 5100}}),
            ):
                result = server.integration_status()

        self.assertTrue(result["astrbot"]["running"])
        self.assertEqual(result["routing"], {"total": 2, "enabled": 1, "bridge_enabled": True, "ai_disabled": True})
        self.assertEqual(result["feishu"]["monitor"]["last_chat_id"], "oc_group")
        self.assertEqual(result["feishu"]["chats"][0]["chat_id"], "oc_group")
        encoded = json.dumps(result)
        self.assertNotIn("must not escape", encoded)
        self.assertNotIn("secret-cookie", encoded)
        self.assertNotIn("secret-qr", encoded)
        self.assertNotIn("password", encoded)

    def test_valid_bidirectional_rules_are_normalized(self):
        data = {
            "rules": [
                route("aiocqhttp", "qq", "12345", "larkx", "oc_1", "QQ to Feishu"),
                route("larkx", "default", "oc_1", "qq", "12345", "Feishu to QQ"),
            ]
        }
        result = server.validate_rules(data)
        self.assertEqual(len(result["rules"]), 2)
        self.assertEqual(result["managed_platforms"], ["aiocqhttp", "larkx"])

    def test_qq_account_selection_survives_mapping_validation(self):
        outbound = route("aiocqhttp", "qq_bridge", "12345", "larkx", "chat", "QQ to Feishu")
        inbound = route("larkx", "default", "chat", "qq_bridge", "12345", "Feishu to QQ")
        outbound["source_account_id"] = "10000001"
        inbound["target_account_id"] = "10000001"
        result = server.validate_rules({"rules": [outbound, inbound]})
        self.assertFalse(result["rules"][0]["include_self"])
        self.assertFalse(result["rules"][1]["include_self"])
        self.assertEqual(result["rules"][0]["source_account_id"], "10000001")
        self.assertEqual(result["rules"][1]["target_account_id"], "10000001")

    def test_disabled_route_is_persistable(self):
        value = route("aiocqhttp", "qq", "12345", "larkx", "77000001", "paused")
        value["enabled"] = False
        self.assertFalse(server.validate_rules({"rules": [value]})["rules"][0]["enabled"])

    def test_enabling_routes_requires_both_current_account_targets(self):
        discovery = {"qq_instances": [{"id": "qq", "accounts": [{"identity_verified": True, "connected": True, "groups": [{"group_id": "12345"}]}]}], "feishu_chats": [{"chat_id": "77000001"}]}
        with patch.object(server, "mapping_discovery", return_value=discovery):
            valid = route("aiocqhttp", "qq", "12345", "larkx", "77000001", "valid")
            self.assertEqual(server.verify_rule_targets({"rules": [valid]})["rules"][0]["target_umo"], "larkx:GroupMessage:77000001")
            invalid = {**valid, "source_session_id": "98765"}
            with self.assertRaisesRegex(ValueError, "来源 QQ 群"):
                server.verify_rule_targets({"rules": [invalid]})
            invalid["enabled"] = False
            self.assertFalse(server.verify_rule_targets({"rules": [invalid]})["rules"][0]["enabled"])

    def test_global_bridge_pause_and_resume_preserve_all_mapping_rules(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / ".astrbot").touch()
            plugin = root / "plugin"
            plugin.mkdir()
            path = root / "bridge_rules.json"
            rules = [route("aiocqhttp", "qq", "12345", "larkx", "77000001", "fixture route")]
            path.write_text(json.dumps({"rules": rules, "bridge_enabled": True}), encoding="utf-8")
            with (
                patch.object(server, "astrbot_root", return_value=root),
                patch.object(server, "plugin_destination", return_value=plugin),
                patch.object(server, "rules_path", return_value=path),
            ):
                paused = server.set_bridge_enabled(False)
                self.assertFalse(paused["enabled"])
                self.assertEqual(server.read_rules(root)["rules"][0]["name"], "fixture route")
                resumed = server.set_bridge_enabled(True)
                self.assertTrue(resumed["enabled"])
                self.assertEqual(len(server.read_rules(root)["rules"]), 1)

    def test_qq_to_qq_and_feishu_to_feishu_routes_are_verified(self):
        discovery = {
            "qq_instances": [{"id": "qq_bridge", "accounts": [
                {"identity_verified": True, "connected": True, "user_id": "111", "groups": [{"group_id": "10001"}]},
                {"identity_verified": True, "connected": True, "user_id": "222", "groups": [{"group_id": "20002"}]},
            ]}],
            "feishu_chats": [{"chat_id": "chat-a"}, {"chat_id": "chat-b"}],
        }
        with patch.object(server, "mapping_discovery", return_value=discovery):
            qq_to_qq = route("aiocqhttp", "qq_bridge", "10001", "qq_bridge", "20002", "same platform")
            qq_to_qq["source_account_id"] = "111"
            qq_to_qq["target_account_id"] = "222"
            self.assertEqual(server.verify_rule_targets({"rules": [qq_to_qq]})["rules"][0]["target_umo"], "qq_bridge:GroupMessage:20002")
            feishu_to_feishu = route("larkx", "default", "chat-a", "larkx", "chat-b", "same platform")
            self.assertEqual(server.verify_rule_targets({"rules": [feishu_to_feishu]})["rules"][0]["target_umo"], "larkx:GroupMessage:chat-b")
            same_chat = route("larkx", "default", "chat-a", "larkx", "chat-a", "self")
            with self.assertRaisesRegex(ValueError, "同一个账号会话"):
                server.verify_rule_targets({"rules": [same_chat]})

    def test_private_qq_route_requires_explicit_account_when_two_are_online(self):
        discovery = {"qq_instances": [{"id": "qq_bridge", "accounts": [
            {"identity_verified": True, "connected": True, "user_id": "111", "groups": []},
            {"identity_verified": True, "connected": True, "user_id": "222", "groups": []},
        ]}], "feishu_chats": [{"chat_id": "chat-a"}]}
        with patch.object(server, "mapping_discovery", return_value=discovery):
            rule = route("larkx", "default", "chat-a", "qq_bridge", "333", "DM")
            rule["source_message_type"] = "GroupMessage"
            rule["target_umo"] = "qq_bridge:FriendMessage:333"
            with self.assertRaisesRegex(ValueError, "私聊关联多个账号"):
                server.verify_rule_targets({"rules": [rule]})
            rule["target_account_id"] = "222"
            self.assertEqual(server.verify_rule_targets({"rules": [rule]})["rules"][0]["target_account_id"], "222")

    def test_runtime_snapshot_expires_and_filters_credentials(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "current_runtime_status.json"
            raw = {"updated_at": datetime.now(timezone.utc).isoformat(), "qq": {"connected": True, "identity_verified": True, "accounts": [{"platform_id": "qq", "user_id": "12345", "identity_verified": True, "cookie": "private", "groups": [{"group_id": "123", "group_name": "Team", "content": "private"}]}]}, "feishu": {"connected": True}, "snowluma": {"running": True, "webui_port": 5100, "password": "private"}, "token": "private"}
            path.write_text(json.dumps(raw), encoding="utf-8")
            with patch.object(server, "rules_path", return_value=Path(temp) / "bridge_rules.json"):
                fresh = server.read_runtime_status()
                self.assertTrue(fresh["qq"]["identity_verified"])
                self.assertEqual(fresh["snowluma"]["webui_port"], 5100)
                self.assertNotIn("private", json.dumps(fresh))
                raw["updated_at"] = (datetime.now(timezone.utc)-timedelta(seconds=50)).isoformat()
                path.write_text(json.dumps(raw), encoding="utf-8")
                expired = server.read_runtime_status()
                self.assertTrue(expired["stale"])
                self.assertFalse(expired["qq"]["connected"])
                self.assertFalse(expired["qq"]["identity_verified"])
                self.assertIsNone(expired["snowluma"]["running"])

    def test_bad_destination_is_rejected(self):
        data = {"rules": [route("aiocqhttp", "qq", "12345", "larkx", "", "bad")]}
        with self.assertRaisesRegex(ValueError, "目标会话 ID"):
            server.validate_rules(data)

    def test_unknown_source_platform_is_rejected(self):
        data = {"rules": [route("discord", "d1", "room", "lark", "oc_1", "bad")]}
        with self.assertRaisesRegex(ValueError, "平台必须是 QQ"):
            server.validate_rules(data)

    def test_install_copies_plugin_and_initializes_empty_runtime_rules(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / ".astrbot").touch()
            (root / "data" / "plugins").mkdir(parents=True)
            installed = server.install_plugin(root)
            config = json.loads(server.rules_path(root).read_text(encoding="utf-8"))
            self.assertTrue((installed / "main.py").is_file())
            self.assertEqual(config["rules"], [])

    def test_distributed_plugin_rule_template_is_empty(self):
        template = json.loads((server.PLUGIN_SOURCE / "bridge_rules.example.json").read_text(encoding="utf-8"))
        self.assertEqual(template["rules"], [])
        self.assertTrue(template["bridge_enabled"])

    def test_dashboard_listener_is_forced_to_loopback_and_keeps_other_options(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "data").mkdir()
            config_path = root / "data" / "cmd_config.json"
            config_path.write_text(
                json.dumps({"dashboard": {"username": "operator", "port": 7000}}),
                encoding="utf-8",
            )
            server._write_local_dashboard_config(root, 6185)
            result = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertEqual(result["dashboard"]["host"], "127.0.0.1")
            self.assertEqual(result["dashboard"]["port"], 6185)
            self.assertEqual(result["dashboard"]["username"], "operator")

    def test_dashboard_config_reader_accepts_windows_utf8_bom(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "data").mkdir()
            config_path = root / "data" / "cmd_config.json"
            config_path.write_text(
                "\ufeff" + json.dumps({"dashboard": {"username": "operator"}}),
                encoding="utf-8",
            )
            server._write_local_dashboard_config(root, 6185)
            result = json.loads(config_path.read_text(encoding="utf-8-sig"))
            self.assertEqual(result["dashboard"]["host"], "127.0.0.1")
            self.assertEqual(result["dashboard"]["username"], "operator")

    def test_missing_runtime_rules_default_to_empty(self):
        with tempfile.TemporaryDirectory() as temp:
            self.assertEqual(server.read_rules(Path(temp))["rules"], [])

    def test_local_feishu_desktop_detection_reports_process_without_message_data(self):
        import psutil
        fake_process = type("FakeProcess", (), {"info": {"name": "Feishu.exe", "exe": r"D:\Feishu\app\Feishu.exe"}})()
        with patch.object(psutil, "process_iter", return_value=[fake_process]):
            result = server.local_feishu_desktop_status()
        self.assertTrue(result["running"])
        self.assertEqual(result["process_count"], 1)
        self.assertNotIn("messages", result)

    def test_larkx_monitor_status_returns_only_activity_metadata(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "bridge-status.json"
            path.write_text(json.dumps({"state": "running", "received_count": 7, "last_received_at": "2026-11-15T12:00:00+00:00", "last_chat_id": "oc_group"}), encoding="utf-8")
            with patch.object(server, "rules_path", return_value=Path(temp) / "bridge_rules.json"), patch.object(server, "read_runtime_status", return_value={"available": False}):
                path.rename(Path(temp) / "larkx_monitor_status.json")
                result = server.larkx_monitor_status()
        self.assertEqual(result["received_count"], 7)
        self.assertEqual(result["last_chat_id"], "oc_group")
        self.assertNotIn("content", result)

    def test_admin_password_strength_is_checked(self):
        with self.assertRaisesRegex(ValueError, "至少 8 位"):
            server.save_admin_password("short")

    def test_admin_password_is_stored_with_current_user_dpapi(self):
        try:
            import win32crypt  # noqa: F401
        except ImportError:
            self.skipTest("Windows DPAPI is unavailable")
        previous = server._initial_password
        try:
            with tempfile.TemporaryDirectory() as temp:
                secret_file = Path(temp) / "bootstrap.dpapi"
                with patch.object(server, "BOOTSTRAP_SECRET_FILE", secret_file):
                    server.save_admin_password("LocalAdmin9-Xy")
                    self.assertTrue(secret_file.is_file())
                    self.assertEqual(server._load_bootstrap_password(), "LocalAdmin9-Xy")
        finally:
            server._initial_password = previous

    def test_snowluma_client_is_configured_without_token_and_preserves_other_networks(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            app = root / "app"
            app.mkdir()
            (app / "node.exe").touch()
            config_path = app / "config" / "onebot.json"
            config_path.parent.mkdir()
            original = {
                "networks": {
                    "httpServers": [{"name": "local-api", "port": 3000}],
                    "wsClients": [{
                        "name": "other-client",
                        "enabled": True,
                        "url": "ws://127.0.0.1:9000/ws",
                        "role": "Event",
                        "accessToken": "preserve-this-existing-value",
                    }],
                },
                "historySync": {"enabled": False},
            }
            config_path.write_text(json.dumps(original), encoding="utf-8")
            with (
                patch.object(server, "SNOWLUMA_APP", app),
                patch.object(server, "SNOWLUMA_CONFIG", config_path),
                patch.object(server, "port_open", return_value=False),
            ):
                result = server.configure_snowluma_onebot()
                updated = json.loads(config_path.read_text(encoding="utf-8"))
                self.assertTrue(server.snowluma_onebot_configured())
            self.assertTrue(result["configured"])
            self.assertFalse(result["token_required"])
            self.assertEqual(updated["networks"]["httpServers"], original["networks"]["httpServers"])
            self.assertEqual(updated["historySync"], original["historySync"])
            self.assertEqual(updated["networks"]["wsClients"][0]["accessToken"], "preserve-this-existing-value")
            bridge = updated["networks"]["wsClients"][1]
            self.assertEqual(bridge["url"], "ws://127.0.0.1:6199/ws")
            self.assertNotIn("accessToken", bridge)
            self.assertTrue(Path(result["backup"]).is_file())

    def test_invalid_snowluma_config_is_left_unchanged(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            app = root / "app"
            app.mkdir()
            (app / "node.exe").touch()
            config_path = app / "config" / "onebot.json"
            config_path.parent.mkdir()
            original = b"{invalid json"
            config_path.write_bytes(original)
            with (
                patch.object(server, "SNOWLUMA_APP", app),
                patch.object(server, "SNOWLUMA_CONFIG", config_path),
            ):
                with self.assertRaisesRegex(ValueError, "JSON 无效"):
                    server.configure_snowluma_onebot()
            self.assertEqual(config_path.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
