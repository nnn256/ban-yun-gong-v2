import unittest

from astrbot_plugin_message_bridge.bridge_core import (
    BRIDGE_MARKER,
    BridgeRouter,
    SeenEvents,
    SentMessageIds,
    component_content_type,
    filter_message_chain,
    make_event_key,
    route_event_key,
    valid_umo,
)


class RouterTests(unittest.TestCase):
    def setUp(self):
        self.config = {
            "managed_platforms": ["aiocqhttp", "larkx"],
            "rules": [
                {
                    "name": "QQ to Feishu",
                    "source_platform": "aiocqhttp",
                    "source_platform_id": "qq_main",
                    "source_message_type": "GroupMessage",
                    "source_session_id": "10001",
                    "target_umo": "larkx:GroupMessage:oc_123",
                },
                {
                    "name": "Feishu to QQ",
                    "source_platform": "larkx",
                    "source_platform_id": "default",
                    "source_message_type": "GroupMessage",
                    "source_session_id": "oc_123",
                    "target_umo": "qq_main:GroupMessage:10001",
                },
            ],
        }

    def test_routes_both_directions_by_platform_instance_and_group(self):
        router = BridgeRouter(self.config)
        self.assertEqual(
            router.find_route("aiocqhttp", "qq_main", "GroupMessage", "10001").name,
            "QQ to Feishu",
        )
        self.assertEqual(
            router.find_route("larkx", "default", "GroupMessage", "oc_123").name,
            "Feishu to QQ",
        )

    def test_route_does_not_match_other_instances_or_sessions(self):
        router = BridgeRouter(self.config)
        self.assertIsNone(
            router.find_route("aiocqhttp", "other_bot", "GroupMessage", "10001")
        )
        self.assertIsNone(
            router.find_route("aiocqhttp", "qq_main", "GroupMessage", "10002")
        )

    def test_one_feishu_source_matches_multiple_qq_destinations_in_configuration_order(self):
        self.config["rules"][1]["target_account_id"] = "10000001"
        self.config["rules"].append({
            **self.config["rules"][1],
            "name": "Feishu to second QQ",
            "target_umo": "qq_main:GroupMessage:10002",
            "target_account_id": "10000002",
        })
        router = BridgeRouter(self.config)
        routes = router.find_routes("larkx", "default", "GroupMessage", "oc_123")
        self.assertEqual([route.target_umo for route in routes], [
            "qq_main:GroupMessage:10001", "qq_main:GroupMessage:10002",
        ])
        self.assertEqual([route.target_account_id for route in routes], ["10000001", "10000002"])
        self.assertIs(router.find_route("larkx", "default", "GroupMessage", "oc_123"), routes[0])

    def test_shared_qq_destination_keeps_independent_target_accounts(self):
        self.config["rules"][1]["target_account_id"] = "10000001"
        self.config["rules"].append({
            **self.config["rules"][1],
            "name": "Second account in same QQ group",
            "target_account_id": "10000002",
        })
        routes = BridgeRouter(self.config).find_routes("larkx", "default", "GroupMessage", "oc_123")
        self.assertEqual(len(routes), 2)
        self.assertEqual({route.target_account_id for route in routes}, {"10000001", "10000002"})

    def test_multiple_destinations_still_filter_the_source_qq_account(self):
        self.config["rules"][0]["source_account_id"] = "10000001"
        self.config["rules"].append({
            **self.config["rules"][0],
            "name": "First account to another Feishu chat",
            "target_umo": "larkx:GroupMessage:oc_second",
        })
        self.config["rules"].append({
            **self.config["rules"][0],
            "name": "Second account route",
            "source_account_id": "10000002",
            "target_umo": "larkx:GroupMessage:oc_third",
        })
        router = BridgeRouter(self.config)
        first = router.find_routes("aiocqhttp", "qq_main", "GroupMessage", "10001", "10000001")
        second = router.find_routes("aiocqhttp", "qq_main", "GroupMessage", "10001", "10000002")
        self.assertEqual([route.target_umo for route in first], ["larkx:GroupMessage:oc_123", "larkx:GroupMessage:oc_second"])
        self.assertEqual([route.target_umo for route in second], ["larkx:GroupMessage:oc_third"])
        self.assertEqual(router.find_routes("aiocqhttp", "qq_main", "GroupMessage", "10001", "10000003"), [])
        self.assertEqual(router.find_routes("larkx", "wrong-instance", "GroupMessage", "oc_123"), [])

    def test_invalid_or_disabled_routes_are_ignored(self):
        self.config["rules"].append(
            {
                "name": "bad",
                "source_platform": "larkx",
                "source_session_id": "oc_bad",
                "target_umo": "not-a-session",
            }
        )
        self.config["rules"].append(
            {
                "name": "disabled",
                "enabled": False,
                "source_platform": "larkx",
                "source_session_id": "oc_123",
                "target_umo": "qq_main:GroupMessage:10001",
            }
        )
        self.assertEqual(len(BridgeRouter(self.config).routes), 2)

    def test_umo_validation(self):
        self.assertTrue(valid_umo("larkx:GroupMessage:oc_123"))
        self.assertFalse(valid_umo("larkx:GroupMessage:"))
        self.assertFalse(valid_umo("larkx:Unknown:oc_123"))

    def test_route_content_types_default_to_all_and_custom_selection_roundtrips(self):
        self.assertEqual(BridgeRouter(self.config).routes[0].content_types, ("text", "emoji", "image", "video", "audio", "file"))
        self.config["rules"][0]["content_types"] = ["audio", "text"]
        route = BridgeRouter(self.config).routes[0]
        self.assertEqual(route.content_types, ("text", "audio"))

    def test_content_filter_keeps_only_selected_component_types(self):
        class Plain:
            pass

        class Face:
            pass

        class Record:
            pass

        class Video:
            pass

        class File:
            name = "notes.txt"

        class Chain:
            chain = [Plain(), Face(), Record(), Video(), File()]

        filtered = filter_message_chain(Chain(), ("text", "audio", "video"))
        self.assertEqual([component_content_type(item) for item in filtered.chain], ["text", "audio", "video"])
        self.assertEqual(len(Chain.chain), 5)

    def test_route_event_keys_are_independent_per_destination(self):
        router = BridgeRouter(self.config)
        first, second = router.routes
        key = make_event_key("qq", "10001", "message", "member", "hello", 1)
        self.assertNotEqual(route_event_key(key, first), route_event_key(key, second))


class LoopProtectionTests(unittest.TestCase):
    def test_sent_qq_receipt_is_shared_only_within_the_target_group(self):
        sent = SentMessageIds()
        sent.add("qq_bridge", "-123", "account-a", "group-1")
        self.assertTrue(sent.contains("qq_bridge", "-123", "account-b", "group-1"))
        self.assertFalse(sent.contains("qq_bridge", "-123", "account-b", "group-2"))

    def test_seen_events_are_rejected_until_ttl_expires(self):
        seen = SeenEvents(ttl_seconds=5, max_entries=10)
        self.assertTrue(seen.accept("event-1", now=10))
        self.assertFalse(seen.accept("event-1", now=12))
        self.assertTrue(seen.accept("event-1", now=16))

    def test_seen_cache_is_bounded(self):
        seen = SeenEvents(ttl_seconds=30, max_entries=2)
        seen.accept("one", now=1)
        seen.accept("two", now=2)
        seen.accept("three", now=3)
        self.assertEqual(len(seen._seen), 2)
        self.assertTrue(seen.accept("one", now=4))

    def test_failed_delivery_can_release_its_deduplication_key_for_retry(self):
        seen = SeenEvents()
        self.assertTrue(seen.accept("route-event"))
        seen.discard("route-event")
        self.assertTrue(seen.accept("route-event"))

    def test_fallback_event_key_is_stable(self):
        first = make_event_key("qq", "10001", "", "user", "hello", 50)
        second = make_event_key("qq", "10001", "", "user", "hello", 50)
        third = make_event_key("qq", "10001", "", "user", "other", 50)
        self.assertEqual(first, second)
        self.assertNotEqual(first, third)

    def test_bridge_marker_is_explicit(self):
        self.assertEqual(BRIDGE_MARKER, "[搬运工V2]")


if __name__ == "__main__":
    unittest.main()
