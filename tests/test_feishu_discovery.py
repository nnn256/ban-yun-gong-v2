import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import workbench_server as server


class FeishuDiscoveryTests(unittest.TestCase):
    def test_group_name_is_actual_metadata_and_never_sender_name(self):
        client = SimpleNamespace(get_group_name=Mock(return_value="Fixture Group"),
                                 get_user_name=Mock(side_effect=AssertionError("no sender lookup for group")))
        self.assertEqual(server._chat_display_name(client, {"type": "GROUP", "name": "Real Group", "sender_name": "Wrong Person"}, "chat"), "Real Group")
        self.assertEqual(server._chat_display_name(client, {"type": 2, "sender_name": "Wrong Person"}, "chat"), "Fixture Group")
        client.get_group_name.assert_called_once_with("chat")

    def test_private_chat_uses_peer_profile_and_excludes_own_name(self):
        client = SimpleNamespace(auth=SimpleNamespace(user_id="self"),
                                 api=Mock(return_value={"chatters": {"self": {"name": "Self"}, "peer": {"name": "Ignored Raw Name"}}}),
                                 get_user_name=Mock(return_value="Peer Profile Name"))
        self.assertEqual(server._chat_display_name(client, {"type": "P2P", "name": ""}, "chat"), "Peer Profile Name")
        client.get_user_name.assert_called_once_with("peer", "chat")
        client.api.assert_called_once_with("chats.PullChatChattersRequest", {"chatId": "chat", "needChatters": True, "limit": 20})

    def test_self_chat_uses_current_profile_and_missing_names_stay_unknown(self):
        client = SimpleNamespace(auth=SimpleNamespace(user_id="self"), api=Mock(return_value={"chatters": {"self": {}}}), get_user_name=Mock(return_value="My Profile"))
        self.assertEqual(server._chat_display_name(client, {"type": 1}, "chat"), "My Profile")
        client.get_user_name.assert_called_once_with("self", "chat")
        client.get_user_name.return_value = None
        self.assertEqual(server._chat_display_name(client, {"type": 1, "sender_name": "Misleading"}, "chat"), "")

    def test_cached_get_does_not_make_network_requests_or_trust_event_group_names(self):
        chats = [
            {"chat_id": "group", "name": "Sender Accidentally Stored Here", "message_type": "GroupMessage", "source": "received_event"},
            {"chat_id": "verified", "name": "Real Group", "message_type": "GroupMessage", "source": "account_metadata"},
        ]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "data").mkdir()
            (root / "data" / "cmd_config.json").write_text('{"platform":[]}', encoding="utf-8")
            with patch.object(server, "astrbot_root", return_value=root), patch.object(server, "ROOT", root), patch.object(server, "read_runtime_status", return_value={"qq": {}}), patch.object(server, "larkx_status", return_value={"chats": chats}):
                result = server.mapping_discovery()
        by_id = {chat["chat_id"]: chat for chat in result["feishu_chats"]}
        self.assertEqual(by_id["group"]["name"], "群聊（名称待刷新）")
        self.assertFalse(by_id["group"]["name_verified"])
        self.assertEqual(by_id["verified"]["name"], "Real Group")
        self.assertTrue(by_id["verified"]["name_verified"])

    def test_explicit_refresh_resolves_names_prunes_inaccessible_chats_and_preserves_rules(self):
        info = {
            "group": {"id": "group", "type": 2, "name": "Actual Group", "status": 0},
            "dm": {"id": "dm", "type": 1, "name": "", "status": 0},
            "gone": {"id": "gone", "type": 2, "name": "Gone Group", "isDissolved": True},
            "deleted": {"id": "deleted", "type": 2, "name": "Deleted Group", "status": "DELETED"},
            "cached": {"id": "cached", "type": "TOPIC_GROUP", "name": "Earlier Verified Group"},
            "no-access": None,
        }
        client = SimpleNamespace(auth=SimpleNamespace(user_id="self"), feed_chats=Mock(return_value=["group", "dm", "gone", "deleted", "no-access"]),
                                 get_chat_info=Mock(side_effect=lambda chat_id: info[chat_id]),
                                 api=Mock(return_value={"chatters": {"self": {}, "peer": {}}}), get_user_name=Mock(return_value="Peer"), close=Mock())
        auth_module = types.ModuleType("larkx.auth")
        auth_module.LarkAuth = Mock(return_value=client.auth)
        client_module = types.ModuleType("larkx.client")
        client_module.LarkClient = Mock(return_value=client)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            rules = root / "bridge_rules.json"
            original_rules = '{"rules":[{"name":"Keep existing rule"}]}'
            rules.write_text(original_rules, encoding="utf-8")
            (root / "larkx_chats.json").write_text(json.dumps({"chats": [{"chat_id": "cached", "name": "Stale Name"}]}), encoding="utf-8")
            with patch.dict(sys.modules, {"larkx.auth": auth_module, "larkx.client": client_module}), patch.object(server, "rules_path", return_value=rules), patch.object(server, "astrbot_root", return_value=root), patch.object(server, "mapping_discovery", return_value={"result": "cached"}):
                self.assertEqual(server.discover_feishu_chats(), {"result": "cached"})
            cached = json.loads((root / "larkx_chats.json").read_text(encoding="utf-8"))["chats"]
            self.assertEqual(rules.read_text(encoding="utf-8"), original_rules)
        by_id = {chat["chat_id"]: chat for chat in cached}
        self.assertEqual(set(by_id), {"group", "dm", "cached"})
        self.assertEqual(by_id["group"]["name"], "Actual Group")
        self.assertEqual(by_id["dm"]["name"], "Peer")
        self.assertEqual(by_id["dm"]["message_type"], "FriendMessage")
        self.assertEqual(by_id["cached"]["message_type"], "GroupMessage")
        self.assertTrue(all(chat["source"] == "account_metadata" for chat in cached))
        self.assertTrue(all(chat["metadata_checked_at"] for chat in cached))
        client.close.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
