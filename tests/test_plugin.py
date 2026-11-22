import importlib
import asyncio
import json
import logging
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch


class Image:
    def __init__(self, data=b"", url=""):
        self.data = data
        self.url = url

    @classmethod
    def fromBytes(cls, data):
        return cls(data=data)

    @classmethod
    def fromURL(cls, url):
        return cls(url=url)


class Plain:
    def __init__(self, text):
        self.text = text


class Face:
    def __init__(self, id):
        self.id = id


class Record:
    def __init__(self, file):
        self.file = file

    @classmethod
    def fromFileSystem(cls, path):
        return cls(file=path)

    @classmethod
    def fromURL(cls, url):
        return cls(file=url, url=url)


class Video(Record):
    pass


class File(Record):
    def __init__(self, name, file, url=""):
        super().__init__(file)
        self.name = name
        self.url = url


class FakeMessageChain:
    def __init__(self):
        self.chain = []

    def message(self, text):
        self.chain.append(Plain(text))
        return self


class FakeStar:
    def __init__(self, context):
        self.context = context


class FakeStarTools:
    data_dir = None

    @classmethod
    def get_data_dir(cls, _plugin_name):
        return cls.data_dir


def install_astrbot_stubs():
    astrbot = types.ModuleType("astrbot")
    astrbot.logger = logging.getLogger("test.astrbot")

    api = types.ModuleType("astrbot.api")
    event = types.ModuleType("astrbot.api.event")
    event.AstrMessageEvent = object
    event.MessageChain = FakeMessageChain
    event.filter = SimpleNamespace(
        EventMessageType=SimpleNamespace(ALL=object()),
        event_message_type=lambda _kind: lambda handler: handler,
        on_astrbot_loaded=lambda: lambda handler: handler,
    )

    star = types.ModuleType("astrbot.api.star")
    star.Context = object
    star.Star = FakeStar
    star.StarTools = FakeStarTools
    star.register = lambda *args: lambda plugin: plugin
    components = types.ModuleType("astrbot.api.message_components")
    components.Image = Image
    components.Record = Record
    components.Video = Video
    components.File = File
    components.Face = Face
    components.Plain = Plain

    astrbot.api = api
    api.event = event
    api.star = star
    sys.modules.update(
        {
            "astrbot": astrbot,
            "astrbot.api": api,
            "astrbot.api.event": event,
            "astrbot.api.star": star,
            "astrbot.api.message_components": components,
        }
    )


install_astrbot_stubs()
plugin = importlib.import_module("astrbot_plugin_message_bridge.main")


class FakeContext:
    def __init__(self):
        self.sent = []

    async def send_message(self, target, chain):
        self.sent.append((target, chain))
        return True


class FakeEvent:
    def __init__(self, platform="aiocqhttp", session_id="10001", text="hello"):
        self.message_obj = SimpleNamespace(self_id="bot", message_id="m-1", timestamp=1, raw_message=None)
        self.bot = None
        self.unified_msg_origin = f"{platform}_main:GroupMessage:{session_id}"
        self.platform = platform
        self.session_id = session_id
        self.text = text
        self.stopped = False

    def get_platform_name(self):
        return self.platform

    def get_platform_id(self):
        return f"{self.platform}_main"

    def get_session_id(self):
        return self.session_id

    def get_message_type(self):
        return SimpleNamespace(value="GroupMessage")

    def get_message_str(self):
        return self.text

    def get_messages(self):
        return []

    def get_sender_id(self):
        return "member-1"

    def get_sender_name(self):
        return "Member"

    def stop_event(self):
        self.stopped = True


class PluginTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        FakeStarTools.data_dir = Path(self.temp_dir.name)
        (FakeStarTools.data_dir / "bridge_rules.json").write_text(
            json.dumps(
                {
                    "managed_platforms": ["aiocqhttp", "larkx"],
                    "rules": [
                        {
                            "name": "LarkAgentX to QQ",
                            "source_platform": "larkx",
                            "source_platform_id": "default",
                            "source_message_type": "GroupMessage",
                            "source_session_id": "oc_1",
                            "target_umo": "aiocqhttp_main:GroupMessage:10001",
                            "include_self": False,
                        },
                        {
                            "name": "QQ to LarkAgentX",
                            "source_platform": "aiocqhttp",
                            "source_platform_id": "aiocqhttp_main",
                            "source_message_type": "GroupMessage",
                            "source_session_id": "10001",
                            "target_umo": "larkx:GroupMessage:oc_1",
                            "include_self": False,
                        },
                    ],
                }
            ),
            encoding="utf-8",
        )
        self.context = FakeContext()
        async def fake_send(context, target, chain, runtime, *, account_id="", return_account=False):
            context.sent.append((target, chain))
            receipt = f"qq-receipt-{len(context.sent)}"
            return (account_id or "fixture-account", receipt) if return_account else receipt
        self.qq_patch = patch.object(plugin, "send_qq", side_effect=fake_send)
        self.qq_patch.start()
        self.bridge = plugin.MessageBridge(self.context)
        self.lark_deliveries = []

        class FakeLarkXClient:
            def send_msg(_self, text, chat_id):
                self.lark_deliveries.append((text, chat_id))
                return "lark-msg"

        self.bridge.larkx_bridge.client = FakeLarkXClient()

    async def asyncTearDown(self):
        await self.bridge.terminate()
        self.qq_patch.stop()
        self.temp_dir.cleanup()

    async def test_forwards_to_configured_destination_without_astrbot_reply_flow(self):
        event = FakeEvent()
        await self.bridge.on_platform_message(event)
        self.assertTrue(event.stopped)
        self.assertEqual(len(self.lark_deliveries), 1)
        text, chat_id = self.lark_deliveries[0]
        self.assertEqual(chat_id, "oc_1")
        self.assertEqual(text, "hello")

    async def test_plugin_reload_initialization_starts_listeners(self):
        with patch.object(self.bridge.larkx_bridge, "start") as lark_start, patch.object(self.bridge.runtime_status, "start") as status_start:
            await self.bridge.initialize()
        lark_start.assert_called_once_with()
        status_start.assert_called_once_with()

    async def test_self_sent_qq_wrapper_normalizes_only_valid_self_events_without_mutation(self):
        received = []

        async def handle(payload):
            received.append(payload)
            return "handled"

        bot = SimpleNamespace(_handle_event=handle)
        adapter = SimpleNamespace(meta=lambda: SimpleNamespace(name="aiocqhttp"), get_client=lambda: bot)
        self.context.platform_manager = SimpleNamespace(get_insts=lambda: [adapter])
        with patch.object(self.bridge.larkx_bridge, "start"), patch.object(self.bridge.runtime_status, "start"):
            await self.bridge.initialize()
            installed = bot._handle_event
            await self.bridge.start_personal_feishu_listener()
        self.assertIs(bot._handle_event, installed, "Repeated startup must not wrap the same bot twice")

        raw = {"post_type": "message_sent", "message_type": "group", "self_id": 111,
               "sender": {"user_id": 111}, "message": [{"type": "text", "data": {"text": "manual"}}]}
        self.assertEqual(await bot._handle_event(raw), "handled")
        self.assertEqual(raw["post_type"], "message_sent")
        self.assertIsNot(received[-1], raw)
        self.assertEqual(received[-1]["post_type"], "message")
        self.assertEqual(received[-1]["message"], raw["message"])
        for payload in (
            {**raw, "post_type": "message"},
            {**raw, "sender": {"user_id": 222}},
            {**raw, "message_type": "notice"},
            {**raw, "sender": {}},
        ):
            await bot._handle_event(payload)
            self.assertIs(received[-1], payload, "Normal or unverified events keep the original path unchanged")

        await self.bridge.terminate()
        self.assertIs(bot._handle_event, handle)
        self.assertEqual(self.bridge._qq_self_event_handlers, {})

    async def test_self_sent_qq_wrapper_preserves_newer_external_handler_on_terminate(self):
        bot = SimpleNamespace(_handle_event=AsyncMock())
        adapter = SimpleNamespace(meta=lambda: SimpleNamespace(name="aiocqhttp"), get_client=lambda: bot)
        self.context.platform_manager = SimpleNamespace(get_insts=lambda: [adapter])
        self.bridge._install_qq_self_message_handlers()
        replacement = AsyncMock()
        bot._handle_event = replacement
        await self.bridge.terminate()
        self.assertIs(bot._handle_event, replacement)

    async def test_self_sent_qq_event_uses_rule_switch_and_receipt_echo_filter(self):
        async def handle(payload):
            if payload.get("post_type") != "message":
                return
            event = FakeEvent(text=payload["message"][0]["data"]["text"])
            event.message_obj.raw_message = payload
            event.message_obj.self_id = str(payload["self_id"])
            event.message_obj.message_id = str(payload["message_id"])
            event.get_sender_id = lambda: str(payload["sender"]["user_id"])
            await self.bridge.on_platform_message(event)

        bot = SimpleNamespace(_handle_event=handle)
        adapter = SimpleNamespace(meta=lambda: SimpleNamespace(name="aiocqhttp"), get_client=lambda: bot)
        self.context.platform_manager = SimpleNamespace(get_insts=lambda: [adapter])
        self.bridge._install_qq_self_message_handlers()
        raw = {"post_type": "message_sent", "message_type": "group", "self_id": "10000001",
               "sender": {"user_id": "10000001"}, "message_id": "self-off",
               "message": [{"type": "text", "data": {"text": "manual self"}}]}
        await bot._handle_event(raw)
        self.assertEqual(self.lark_deliveries, [])

        path = FakeStarTools.data_dir / "bridge_rules.json"
        config = json.loads(path.read_text(encoding="utf-8"))
        config["rules"][1]["include_self"] = True
        path.write_text(json.dumps(config), encoding="utf-8")
        raw["message_id"] = "self-on"
        await bot._handle_event(raw)
        await bot._handle_event(raw)
        self.assertEqual(self.lark_deliveries, [("manual self", "oc_1")])

        raw["message_id"] = "bridge-receipt"
        self.bridge.sent_qq.add("aiocqhttp_main", "bridge-receipt", "10000001", "10001")
        await bot._handle_event(raw)
        self.assertEqual(self.lark_deliveries, [("manual self", "oc_1")])

    async def test_self_sent_qq_private_routes_by_recipient_and_honors_switch_and_echo(self):
        path = FakeStarTools.data_dir / "bridge_rules.json"
        config = json.loads(path.read_text(encoding="utf-8"))
        config["rules"][1].update(source_message_type="FriendMessage",
                                  source_session_id="20002", source_account_id="10000001")
        path.write_text(json.dumps(config), encoding="utf-8")

        async def handle(payload):
            if payload.get("post_type") != "message":
                return
            # This matches AstrBot's private event conversion: the session is
            # sender.user_id even when the sender is the logged-in account.
            event = FakeEvent(session_id=str(payload["sender"]["user_id"]), text="private manual")
            event.get_message_type = lambda: SimpleNamespace(value="FriendMessage")
            event.message_obj.raw_message = payload
            event.message_obj.self_id = str(payload["self_id"])
            event.message_obj.message_id = str(payload["message_id"])
            event.get_sender_id = lambda: str(payload["sender"]["user_id"])
            await self.bridge.on_platform_message(event)

        bot = SimpleNamespace(_handle_event=handle)
        adapter = SimpleNamespace(meta=lambda: SimpleNamespace(name="aiocqhttp"), get_client=lambda: bot)
        self.context.platform_manager = SimpleNamespace(get_insts=lambda: [adapter])
        self.bridge._install_qq_self_message_handlers()
        raw = {"post_type": "message_sent", "message_type": "private", "self_id": "10000001",
               "sender": {"user_id": "10000001"}, "user_id": "10000001", "target_id": "20002",
               "message_id": "private-self-off", "message": []}
        await bot._handle_event(raw)
        self.assertEqual(self.lark_deliveries, [])
        config["rules"][1]["include_self"] = True
        path.write_text(json.dumps(config), encoding="utf-8")
        raw["message_id"] = "private-self-on"
        await bot._handle_event(raw)
        await bot._handle_event(raw)
        self.assertEqual(self.lark_deliveries, [("private manual", "oc_1")])
        self.assertEqual(raw["post_type"], "message_sent")
        raw["message_id"] = "private-bridge-echo"
        self.bridge.sent_qq.add("aiocqhttp_main", raw["message_id"], "10000001", "20002")
        await bot._handle_event(raw)
        self.assertEqual(self.lark_deliveries, [("private manual", "oc_1")])
        config["rules"][1]["include_self"] = False
        path.write_text(json.dumps(config), encoding="utf-8")
        raw["message_id"] = "private-self-disabled-again"
        await bot._handle_event(raw)
        self.assertEqual(self.lark_deliveries, [("private manual", "oc_1")])

    async def test_private_self_message_accepts_reported_peer_variants_but_not_missing_recipient(self):
        path = FakeStarTools.data_dir / "bridge_rules.json"
        config = json.loads(path.read_text(encoding="utf-8"))
        config["rules"][1].update(source_message_type="FriendMessage", source_session_id="20002",
                                  source_account_id="10000001", include_self=True)
        own_route = dict(config["rules"][1], name="own private session", source_session_id="10000001",
                         target_umo="larkx:FriendMessage:wrong-chat")
        config["rules"].append(own_route)
        path.write_text(json.dumps(config), encoding="utf-8")
        for index, fields in enumerate(({"peer_id": "20002"}, {"user_id": "20002"}, {})):
            event = FakeEvent(session_id="10000001", text=f"variant {index}")
            event.get_message_type = lambda: SimpleNamespace(value="FriendMessage")
            event.message_obj.self_id = "10000001"
            event.message_obj.message_id = f"private-variant-{index}"
            event.message_obj.raw_message = {"message_type": "private", **fields}
            event.get_sender_id = lambda: "10000001"
            await self.bridge.on_platform_message(event)
        self.assertEqual(self.lark_deliveries, [("variant 0", "oc_1"), ("variant 1", "oc_1")])

    async def test_feishu_private_self_switch_applies_before_deduplication_and_receipt_filter(self):
        path = FakeStarTools.data_dir / "bridge_rules.json"
        config = json.loads(path.read_text(encoding="utf-8"))
        config["rules"][0].update(source_message_type="FriendMessage",
                                  target_umo="aiocqhttp_main:FriendMessage:20002",
                                  target_account_id="10000001")
        path.write_text(json.dumps(config), encoding="utf-8")
        message = {"chat_id": "oc_1", "chat_type": 1, "from_id": "bot-user",
                   "msg_id": "private-feishu-manual", "content": "private manual"}
        await self.bridge.on_larkx_message(message, "default", "bot-user")
        self.assertEqual(self.context.sent, [])
        config["rules"][0]["include_self"] = True
        path.write_text(json.dumps(config), encoding="utf-8")
        await self.bridge.on_larkx_message(message, "default", "bot-user")
        await self.bridge.on_larkx_message(message, "default", "bot-user")
        self.assertEqual(len(self.context.sent), 1)
        self.assertEqual(self.context.sent[0][0], "aiocqhttp_main:FriendMessage:20002")
        echo = dict(message, msg_id="private-feishu-bridge-echo")
        self.bridge.larkx_bridge._sent_ids.add(self.bridge.larkx_bridge._receipt_scope("oc_1"), echo["msg_id"])
        await self.bridge.on_larkx_message(echo, "default", "bot-user")
        self.assertEqual(len(self.context.sent), 1)
        config["rules"][0]["include_self"] = False
        path.write_text(json.dumps(config), encoding="utf-8")
        await self.bridge.on_larkx_message(dict(message, msg_id="private-feishu-disabled-again"), "default", "bot-user")
        self.assertEqual(len(self.context.sent), 1)

    async def test_larkx_media_download_retries_transient_not_ready_response(self):
        from requests.exceptions import HTTPError

        response = SimpleNamespace(status_code=404)
        missing = HTTPError("resource not ready", response=response)
        download = types.SimpleNamespace(calls=0)

        def fetch(message):
            download.calls += 1
            if download.calls == 1:
                raise missing
            return b"ready-media"

        with patch.object(plugin.asyncio, "sleep", new=AsyncMock()) as sleep:
            result = await self.bridge._download_larkx_media(fetch, {"msg_id": "fixture"})
        self.assertEqual(result, b"ready-media")
        self.assertEqual(download.calls, 2)
        sleep.assert_awaited_once_with(0.3)

    async def test_larkx_media_download_does_not_retry_format_errors(self):
        # This exception is deliberately unrelated to HTTP resource readiness.
        malformed = Mock(side_effect=ValueError("invalid audio bytes"))
        with patch.object(plugin.asyncio, "sleep", new=AsyncMock()) as sleep:
            with self.assertRaisesRegex(ValueError, "invalid audio bytes"):
                await self.bridge._download_larkx_media(malformed, {"msg_id": "fixture"})
        self.assertEqual(malformed.call_count, 1)
        sleep.assert_not_awaited()

    async def test_larkx_media_download_transient_retries_are_bounded(self):
        from requests.exceptions import HTTPError

        fetch = Mock(side_effect=HTTPError("unavailable", response=SimpleNamespace(status_code=503)))
        with patch.object(plugin.asyncio, "sleep", new=AsyncMock()) as sleep:
            with self.assertRaises(HTTPError):
                await self.bridge._download_larkx_media(fetch, {"msg_id": "fixture"})
        self.assertEqual(fetch.call_count, 6)
        self.assertEqual([call.args[0] for call in sleep.await_args_list], [0.3, 0.7, 1.2, 2.5, 5.0])

    async def test_unmapped_managed_message_is_stopped_before_astrbot_ai(self):
        event = FakeEvent(session_id="another-group")
        await self.bridge.on_platform_message(event)
        self.assertTrue(event.stopped)
        self.assertEqual(self.lark_deliveries, [])

    async def test_forwards_from_feishu_to_qq(self):
        await self.bridge.on_larkx_message(
            {"chat_id": "oc_1", "chat_type": 2, "from_id": "member-2", "msg_id": "lark-1", "content": "from Feishu"},
            "default",
            "bot-user",
        )
        self.assertEqual(len(self.context.sent), 1)
        target, chain = self.context.sent[0]
        self.assertEqual(target, "aiocqhttp_main:GroupMessage:10001")
        self.assertEqual(chain.chain[0].text, "from Feishu")

    async def test_forwards_qq_message_to_larkx_without_agent(self):
        delivered = []

        class FakeLarkXClient:
            def send_msg(self, text, chat_id):
                delivered.append((text, chat_id))
                return "lark-msg-1"

        self.bridge.larkx_bridge.client = FakeLarkXClient()
        await self.bridge.on_platform_message(FakeEvent())
        self.assertEqual(len(delivered), 1)
        self.assertEqual(delivered[0][1], "oc_1")
        self.assertEqual(delivered[0][0], "hello")

    async def test_self_authored_feishu_is_filtered_and_marker_is_preserved_for_members(self):
        await self.bridge.on_larkx_message(
            {"chat_id": "oc_1", "chat_type": 2, "from_id": "bot-user", "msg_id": "own-1", "content": "echo"},
            "default",
            "bot-user",
        )
        await self.bridge.on_larkx_message(
            {"chat_id": "oc_1", "chat_type": 2, "from_id": "member-2", "msg_id": "mark-1", "content": "[搬运工V2] QQ | Member: hello"},
            "default",
            "bot-user",
        )
        self.assertEqual(len(self.context.sent), 1)
        self.assertEqual(self.context.sent[0][1].chain[0].text, "[搬运工V2] QQ | Member: hello")

    async def test_bridge_feishu_receipt_echo_is_ignored_without_visible_marker(self):
        await self.bridge.on_platform_message(FakeEvent())
        await self.bridge.on_larkx_message(
            {"chat_id": "oc_1", "chat_type": 2, "from_id": "bot-user", "msg_id": "lark-msg", "content": "hello"},
            "default", "bot-user",
        )
        self.assertEqual(self.context.sent, [])

    async def test_qq_image_is_sent_as_native_image_without_placeholder_or_prefix(self):
        event = FakeEvent(text="")
        event.get_messages = lambda: [Image(url="https://fixture.invalid/image.png")]
        with patch("astrbot_plugin_message_bridge.larkx_media.send_image", return_value="image-receipt") as send:
            await self.bridge.on_platform_message(event)
        send.assert_called_once()
        self.assertEqual(send.call_args.args[1], "oc_1")
        self.assertIsInstance(send.call_args.args[2], Image)
        self.assertEqual(self.lark_deliveries, [])

    async def test_feishu_image_with_empty_plain_text_is_forwarded_as_qq_image(self):
        with patch.object(self.bridge.larkx_bridge, "download_image", return_value=b"fixture-image"):
            await self.bridge.on_larkx_message(
                {"chat_id": "oc_1", "chat_type": 2, "from_id": "fixture-member", "msg_id": "image-1", "msg_type": 5, "content": ""},
                "default", "bot-user",
            )
        self.assertEqual(len(self.context.sent), 1)
        image = self.context.sent[0][1].chain[0]
        self.assertIsInstance(image, Image)
        self.assertEqual(image.data, b"fixture-image")

    async def test_feishu_sticker_message_downloads_image_bytes_instead_of_key_summary(self):
        with patch.object(self.bridge.larkx_bridge, "download_attachment", return_value=(b"sticker-image", "sticker.png", "sticker")):
            await self.bridge.on_larkx_message(
                {"chat_id": "oc_1", "chat_type": 2, "from_id": "member", "msg_id": "sticker-1", "msg_type": 10, "content": "[表情包] key=private-resource"},
                "default", "bot-user",
            )
        image = self.context.sent[-1][1].chain[0]
        self.assertIsInstance(image, Image)
        self.assertEqual(image.data, b"sticker-image")

    async def test_qq_face_is_preserved_for_qq_and_emitted_as_local_image_to_feishu(self):
        face = Face(id=14)
        event = FakeEvent(text="")
        event.get_messages = lambda: [face]
        event.message_obj.raw_message = {"message": [{"type": "face", "data": {"id": "14"}}]}
        chain = plugin.MessageBridge._build_forward_chain(event, "aiocqhttp")
        self.assertIs(chain.chain[0], face)
        with patch("astrbot_plugin_message_bridge.larkx_bridge.find_face_image", return_value=Path("fixture-face.png")) as lookup, patch("astrbot_plugin_message_bridge.larkx_media.send_image", return_value="face-receipt") as send:
            self.bridge.larkx_bridge.client = SimpleNamespace(send_msg=lambda *_: None)
            self.bridge.larkx_bridge.send_chain("oc_1", chain)
        lookup.assert_called_once_with(14)
        send.assert_called_once()
        self.assertEqual(send.call_args.args[2], Path("fixture-face.png"))

    async def test_qq_mface_image_stays_in_original_segment_order(self):
        event = FakeEvent(text="")
        event.get_messages = lambda: [Plain("before"), Plain("after")]
        event.message_obj.raw_message = {"message": [
            {"type": "text", "data": {"text": "before"}},
            {"type": "mface", "data": {"url": "https://fixture.invalid/sticker.png"}},
            {"type": "text", "data": {"text": "after"}},
        ]}
        chain = plugin.MessageBridge._build_forward_chain(event, "aiocqhttp")
        self.assertEqual([type(item).__name__ for item in chain.chain], ["Plain", "EmojiImage", "Plain"])
        self.assertEqual(plugin.component_content_type(chain.chain[1]), "emoji")
        self.assertEqual(chain.chain[1].url, "https://fixture.invalid/sticker.png")

    async def test_raw_onebot_video_segment_is_preserved_without_adapter_component(self):
        with tempfile.TemporaryDirectory() as directory:
            video = Path(directory) / "raw-video.mp4"
            video.write_bytes(b"\x00\x00\x00\x18ftypmp42fixture")
            event = FakeEvent(text="caption")
            event.get_messages = lambda: [Plain("caption")]
            event.message_obj.raw_message = {"message": [
                {"type": "text", "data": {"text": "caption"}},
                {"type": "video", "data": {"file": str(video)}},
            ]}
            chain = plugin.MessageBridge._build_forward_chain(event, "aiocqhttp")
        self.assertEqual([plugin.component_content_type(item) for item in chain.chain], ["text", "video"])
        self.assertIsInstance(chain.chain[1], Video)

    async def test_failed_qq_media_delivery_retries_same_event(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "retry.mp4"
            path.write_bytes(b"\x00\x00\x00\x18ftypmp42fixture")
            event = FakeEvent(text="")
            event.message_obj.message_id = "retry-video-event"
            event.get_messages = lambda: [Video.fromFileSystem(str(path))]
            with patch("astrbot_plugin_message_bridge.larkx_media.send_native_media", side_effect=[None, "video-receipt"]) as send:
                await self.bridge.on_platform_message(event)
                await self.bridge.on_platform_message(event)
        self.assertEqual(send.call_count, 2)
        self.assertEqual(send.call_args.kwargs["kind"], "video")
        self.assertEqual(self.lark_deliveries, [])

    async def test_feishu_media_download_failure_can_retry_same_event(self):
        message = {"chat_id": "oc_1", "chat_type": 2, "from_id": "member", "msg_id": "retry-audio", "msg_type": 7, "content": ""}
        with patch.object(self.bridge.larkx_bridge, "download_attachment", side_effect=[RuntimeError("temporary"), (b"RIFF\x00\x00\x00\x00WAVEfixture", "voice.wav", "audio")]) as download:
            await self.bridge.on_larkx_message(message, "default", "bot-user")
            await self.bridge.on_larkx_message(message, "default", "bot-user")
        self.assertEqual(download.call_count, 2)
        self.assertEqual(len(self.context.sent), 1)
        self.assertIsInstance(self.context.sent[0][1].chain[0], Record)

    async def test_content_types_filter_text_by_route(self):
        path = FakeStarTools.data_dir / "bridge_rules.json"
        config = json.loads(path.read_text(encoding="utf-8"))
        config["rules"][1]["content_types"] = ["video"]
        path.write_text(json.dumps(config), encoding="utf-8")
        await self.bridge.on_platform_message(FakeEvent(text="filtered text"))
        self.assertEqual(self.lark_deliveries, [])

    async def test_retry_does_not_repeat_successful_text_before_failed_media(self):
        chain = FakeMessageChain().message("caption")
        chain.chain.append(Video("fixture.mp4"))
        route = self.bridge.router.routes[1]
        with patch.object(self.bridge, "_deliver_chain", new=AsyncMock(side_effect=[True, False, True])) as deliver:
            self.assertFalse(await self.bridge._deliver_route_parts(route, chain, "multipart"))
            self.assertTrue(await self.bridge._deliver_route_parts(route, chain, "multipart"))
        self.assertEqual(deliver.call_count, 3)
        self.assertEqual([plugin.component_content_type(call.args[1].chain[0]) for call in deliver.call_args_list], ["text", "video", "video"])

    async def test_qq_reply_component_class_label_is_removed_from_text_fallback(self):
        event = FakeEvent(text="[ComponentType.Reply] 原始正文")
        event.get_messages = lambda: []
        chain = plugin.MessageBridge._build_forward_chain(event, "aiocqhttp")
        self.assertEqual(len(chain.chain), 1)
        self.assertEqual(chain.chain[0].text, "原始正文")

    async def test_qq_sent_receipt_echo_is_ignored_and_manual_self_message_uses_rule_switch(self):
        await self.bridge.on_larkx_message(
            {"chat_id": "oc_1", "chat_type": 2, "from_id": "member", "msg_id": "lark-2", "content": "plain body"},
            "default", "bot-user",
        )
        echo = FakeEvent(text="plain body")
        echo.message_obj.message_id = "qq-receipt-1"
        echo.message_obj.self_id = "10000001"
        echo.get_sender_id = lambda: "10000001"
        await self.bridge.on_platform_message(echo)
        self.assertEqual(self.lark_deliveries, [])
        manual = FakeEvent(text="my manual text")
        manual.message_obj.message_id = "manual-1"
        manual.message_obj.self_id = "10000001"
        manual.get_sender_id = lambda: "10000001"
        await self.bridge.on_platform_message(manual)
        self.assertEqual(self.lark_deliveries, [])

    async def test_source_qq_account_filter_prevents_the_other_online_account_from_forwarding(self):
        path = FakeStarTools.data_dir / "bridge_rules.json"
        config = json.loads(path.read_text(encoding="utf-8"))
        config["rules"][1]["source_account_id"] = "10000001"
        path.write_text(json.dumps(config), encoding="utf-8")
        other = FakeEvent()
        other.message_obj.self_id = "10000002"
        await self.bridge.on_platform_message(other)
        self.assertEqual(self.lark_deliveries, [])
        chosen = FakeEvent()
        chosen.message_obj.self_id = "10000001"
        await self.bridge.on_platform_message(chosen)
        self.assertEqual(self.lark_deliveries[0][0], "hello")

    async def test_include_self_can_enable_manual_messages_for_both_platforms(self):
        path = FakeStarTools.data_dir / "bridge_rules.json"
        config = json.loads(path.read_text(encoding="utf-8"))
        for rule in config["rules"]:
            rule["include_self"] = True
        path.write_text(json.dumps(config), encoding="utf-8")
        event = FakeEvent()
        event.get_sender_id = lambda: "bot"
        await self.bridge.on_platform_message(event)
        await self.bridge.on_larkx_message(
            {"chat_id": "oc_1", "chat_type": 2, "from_id": "bot-user", "msg_id": "own-off", "content": "manual"},
            "default", "bot-user",
        )
        self.assertEqual(self.lark_deliveries, [("hello", "oc_1")])
        self.assertEqual(len(self.context.sent), 1)
        self.assertEqual(self.context.sent[0][1].chain[0].text, "manual")

    async def test_feishu_audio_and_video_download_real_bytes_and_never_send_resource_summary(self):
        for msg_type, kind, component in ((7, "audio", Record), (15, "video", Video)):
            with patch.object(self.bridge.larkx_bridge, "download_attachment", return_value=(b"real-media", "clip.bin", kind)) as download:
                await self.bridge.on_larkx_message(
                    {"chat_id": "oc_1", "chat_type": 2, "from_id": "member", "msg_id": f"media-{msg_type}", "msg_type": msg_type, "content": "[video] key=INTERNAL"},
                    "default", "bot-user",
                )
            download.assert_called_once()
            self.assertIsInstance(self.context.sent[-1][1].chain[0], component)
            self.assertFalse(hasattr(self.context.sent[-1][1].chain[0], "text"))

    def rich_post(self, message_id="rich-video"):
        return {"chat_id": "oc_1", "chat_type": 2, "from_id": "member", "msg_id": message_id,
                "msg_type": 2, "content": "[富文本]\n[视频]", "content_data": {"richText": {}}}

    def rich_parts(self, post):
        return [
            {**post, "msg_type": 4, "content": "before", "content_data": {}, "_rich_part_id": "text-1"},
            {**post, "msg_type": 15, "content": "", "content_data": {"key": "fixture-video"}, "_rich_part_id": "media-2"},
            {**post, "msg_type": 4, "content": "after", "content_data": {}, "_rich_part_id": "text-3"},
        ]

    async def test_rich_post_sends_original_text_and_actual_video_in_order(self):
        post = self.rich_post()
        with patch.object(plugin, "rich_post_parts", return_value=self.rich_parts(post)), patch.object(
            self.bridge.larkx_bridge, "download_attachment", return_value=(b"video-bytes", "clip.mov", "video")
        ) as download:
            await self.bridge.on_larkx_message(post, "default", "bot-user")
        self.assertEqual([plugin.component_content_type(chain.chain[0]) for _, chain in self.context.sent], ["text", "video", "text"])
        self.assertEqual(self.context.sent[0][1].chain[0].text, "before")
        self.assertEqual(self.context.sent[2][1].chain[0].text, "after")
        self.assertEqual(download.call_args.args[0]["msg_id"], post["msg_id"])
        self.assertFalse(Path(self.context.sent[1][1].chain[0].file).exists(), "Scoped resource removed after awaited send")

    async def test_text_envelope_rich_video_preserves_private_source_and_qq_target(self):
        path = FakeStarTools.data_dir / "bridge_rules.json"
        config = json.loads(path.read_text(encoding="utf-8"))
        config["rules"][0].update(source_message_type="FriendMessage",
                                  target_umo="aiocqhttp_main:FriendMessage:20002",
                                  target_account_id="10000001", content_types=["video"])
        path.write_text(json.dumps(config), encoding="utf-8")
        post = self.rich_post("private-text-envelope-video")
        post.update(msg_type=4, chat_type=1, content_data={"richText": {
            "elementIds": ["media"], "elements": {"dictionary": {"media": {"tag": 18}}}}})
        with patch.object(plugin, "rich_post_parts", return_value=self.rich_parts(post)), patch.object(
            self.bridge.larkx_bridge, "download_attachment", return_value=(b"video-bytes", "clip.mp4", "video")
        ) as download:
            await self.bridge.on_larkx_message(post, "default", "bot-user")
            await self.bridge.on_larkx_message(post, "default", "bot-user")
        self.assertEqual(len(self.context.sent), 1)
        self.assertEqual(self.context.sent[0][0], "aiocqhttp_main:FriendMessage:20002")
        self.assertIsInstance(self.context.sent[0][1].chain[0], Video)
        self.assertEqual(download.call_args.args[0]["msg_id"], post["msg_id"])
        self.assertEqual(download.call_args.args[0]["chat_type"], 1)

    async def test_self_authored_text_envelope_video_requires_switch_and_deduplicates(self):
        path = FakeStarTools.data_dir / "bridge_rules.json"
        config = json.loads(path.read_text(encoding="utf-8"))
        config["rules"][0]["content_types"] = ["video"]
        path.write_text(json.dumps(config), encoding="utf-8")
        post = self.rich_post("own-text-envelope-video")
        post.update(msg_type=4, from_id="bot-user", content_data={"richText": {
            "elementIds": ["media"], "elements": {"dictionary": {"media": {"tag": 18}}}}})
        with patch.object(plugin, "rich_post_parts", return_value=self.rich_parts(post)), patch.object(
            self.bridge.larkx_bridge, "download_attachment", return_value=(b"video-bytes", "clip.mp4", "video")
        ) as download:
            await self.bridge.on_larkx_message(post, "default", "bot-user")
            download.assert_not_called()
            config["rules"][0]["include_self"] = True
            path.write_text(json.dumps(config), encoding="utf-8")
            await self.bridge.on_larkx_message(post, "default", "bot-user")
            await self.bridge.on_larkx_message(post, "default", "bot-user")
        self.assertEqual(len(self.context.sent), 1)
        self.assertIsInstance(self.context.sent[0][1].chain[0], Video)

    async def test_empty_post_records_failure_without_sending_summary(self):
        post = self.rich_post("empty-post")
        with patch.object(self.bridge.runtime_status, "record_delivery") as record, patch.object(
            self.bridge.larkx_bridge, "download_attachment"
        ) as download:
            await self.bridge.on_larkx_message(post, "default", "bot-user")
        download.assert_not_called()
        self.assertEqual(self.context.sent, [])
        record.assert_called_once_with("qq", False)

    async def test_video_only_rule_accepts_rich_post_outer_type(self):
        path = FakeStarTools.data_dir / "bridge_rules.json"
        config = json.loads(path.read_text(encoding="utf-8"))
        config["rules"][0]["content_types"] = ["video"]
        path.write_text(json.dumps(config), encoding="utf-8")
        post = self.rich_post()
        with patch.object(plugin, "rich_post_parts", return_value=self.rich_parts(post)), patch.object(
            self.bridge.larkx_bridge, "download_attachment", return_value=(b"video", "clip.mov", "video")
        ):
            await self.bridge.on_larkx_message(post, "default", "bot-user")
        self.assertEqual(len(self.context.sent), 1)
        self.assertIsInstance(self.context.sent[0][1].chain[0], Video)

    async def test_rich_post_text_only_rule_does_not_download_filtered_media(self):
        path = FakeStarTools.data_dir / "bridge_rules.json"
        config = json.loads(path.read_text(encoding="utf-8"))
        config["rules"][0].update(target_umo="larkx:GroupMessage:oc_2", content_types=["text"])
        path.write_text(json.dumps(config), encoding="utf-8")
        post = self.rich_post()
        with patch.object(plugin, "rich_post_parts", return_value=self.rich_parts(post)), patch.object(
            self.bridge.larkx_bridge, "download_attachment"
        ) as download, patch.object(self.bridge.larkx_bridge, "forward_message") as forward:
            await self.bridge.on_larkx_message(post, "default", "bot-user")
        download.assert_not_called()
        forward.assert_not_called()
        self.assertEqual(self.lark_deliveries, [("before", "oc_2"), ("after", "oc_2")])

    async def test_rich_post_to_feishu_private_chat_sends_a_native_video_in_order(self):
        path = FakeStarTools.data_dir / "bridge_rules.json"
        config = json.loads(path.read_text(encoding="utf-8"))
        config["rules"][0].update(target_umo="larkx:FriendMessage:oc_2")
        path.write_text(json.dumps(config), encoding="utf-8")
        post = self.rich_post()
        native = []

        def capture_video(_client, target, component, *, kind):
            native.append((target, kind, Path(component.file).read_bytes()))
            return "native-video-receipt"

        with patch.object(plugin, "rich_post_parts", return_value=self.rich_parts(post)), patch.object(
            self.bridge.larkx_bridge, "download_attachment", return_value=(b"video", "clip.mp4", "video")
        ) as download, patch.object(self.bridge.larkx_bridge, "forward_message") as forward, patch(
            "astrbot_plugin_message_bridge.larkx_media.send_native_media", side_effect=capture_video
        ), patch("astrbot_plugin_message_bridge.larkx_media.send_attachment") as send_file:
            await self.bridge.on_larkx_message(post, "default", "bot-user")
            await self.bridge.on_larkx_message(post, "default", "bot-user")
        download.assert_called_once()
        forward.assert_not_called()
        send_file.assert_not_called()
        self.assertEqual(native, [("oc_2", "video", b"video")])
        self.assertEqual(self.lark_deliveries, [("before", "oc_2"), ("after", "oc_2")])

    async def test_qq_forward_expands_image_and_video_without_sending_the_chat_record_summary(self):
        with tempfile.TemporaryDirectory() as directory:
            clip = Path(directory) / "video.mp4"
            clip.write_bytes(b"\x00\x00\x00\x18ftypmp42fixture")
            event = FakeEvent(text="[合并转发] author 123: ignored summary")
            event.message_obj.raw_message = {"message": [{"type": "forward", "data": {"id": "record-1"}}]}
            event.bot = SimpleNamespace(call_action=AsyncMock(return_value={"messages": [
                {"sender": {"user_id": 123}, "message": [{"type": "text", "data": {"text": "before"}}]},
                {"sender": {"user_id": 123}, "message": [{"type": "image", "data": {"url": "https://fixture.invalid/image.png"}}]},
                {"sender": {"user_id": 123}, "message": [{"type": "video", "data": {"file": clip.as_uri()}}]},
            ]}))
            delivered = []

            def capture_image(_client, target, component):
                delivered.append((target, "image", component.url))
                return "image-receipt"

            def capture_video(_client, target, component, *, kind):
                delivered.append((target, kind, Path(component.file).read_bytes()))
                return "video-receipt"

            self.bridge.larkx_bridge.client.send_msg = lambda text, target: delivered.append((target, "text", text)) or "text-receipt"
            with patch("astrbot_plugin_message_bridge.larkx_media.send_image", side_effect=capture_image), patch(
                "astrbot_plugin_message_bridge.larkx_media.send_native_media", side_effect=capture_video
            ):
                await self.bridge.on_platform_message(event)
            event.bot.call_action.assert_awaited_once_with("get_forward_msg", self_id="bot", id="record-1")
            self.assertEqual(delivered, [("oc_1", "text", "before"),
                                        ("oc_1", "image", "https://fixture.invalid/image.png"),
                                        ("oc_1", "video", clip.read_bytes())])

    async def test_qq_forward_fetch_failure_does_not_send_summary(self):
        event = FakeEvent(text="[合并转发] ignored summary")
        event.message_obj.raw_message = {"message": [{"type": "forward", "data": {"id": "record-1"}}]}
        event.bot = SimpleNamespace(call_action=AsyncMock(return_value={"status": "failed"}))
        with patch.object(self.bridge.runtime_status, "record_delivery") as record:
            await self.bridge.on_platform_message(event)
        self.assertEqual(self.lark_deliveries, [])
        record.assert_called_once_with("feishu", False)

    async def test_rich_post_retry_continues_remaining_parts_and_only_retries_the_failure(self):
        post = self.rich_post()
        with patch.object(plugin, "rich_post_parts", return_value=self.rich_parts(post)), patch.object(
            self.bridge.larkx_bridge, "download_attachment", side_effect=[RuntimeError("resource not ready"), (b"video", "clip.mov", "video")]
        ) as download:
            self.assertFalse(await self.bridge.on_larkx_message(post, "default", "bot-user"))
            self.assertEqual([plugin.component_content_type(chain.chain[0]) for _, chain in self.context.sent], ["text", "text"])
            self.assertTrue(await self.bridge.on_larkx_message(post, "default", "bot-user"))
            await self.bridge.on_larkx_message(post, "default", "bot-user")
        self.assertEqual(download.call_count, 2)
        self.assertEqual([plugin.component_content_type(chain.chain[0]) for _, chain in self.context.sent], ["text", "text", "video"])

    async def test_malformed_rich_media_records_failure_without_summary_text(self):
        post = self.rich_post()
        with patch.object(plugin, "rich_post_parts", side_effect=ValueError("media key missing")), patch.object(
            self.bridge.runtime_status, "record_delivery"
        ) as record:
            await self.bridge.on_larkx_message(post, "default", "bot-user")
        self.assertEqual(self.context.sent, [])
        self.assertEqual(self.lark_deliveries, [])
        record.assert_called_once_with("qq", False)

    async def test_qq_video_and_audio_use_native_feishu_media(self):
        event = FakeEvent(text="")
        with tempfile.TemporaryDirectory() as directory:
            video = Path(directory) / "clip.mp4"
            voice = Path(directory) / "voice.silk"
            video.write_bytes(b"\x00\x00\x00\x18ftypmp42fixture")
            voice.write_bytes(b"#!SILK_V3fixture")
            event.get_messages = lambda: [Video.fromFileSystem(str(video)), Record.fromFileSystem(str(voice))]
            with patch("astrbot_plugin_message_bridge.larkx_media.send_native_media", side_effect=["video-id", "audio-id"]) as send:
                await self.bridge.on_platform_message(event)
        self.assertEqual(send.call_count, 2)
        self.assertEqual(send.call_args_list[0].kwargs["kind"], "video")
        self.assertEqual(send.call_args_list[1].kwargs["kind"], "audio")
        self.assertEqual(self.lark_deliveries, [])

    async def test_qq_to_qq_and_one_source_to_multiple_destinations_are_independent(self):
        path = FakeStarTools.data_dir / "bridge_rules.json"
        config = json.loads(path.read_text(encoding="utf-8"))
        extra = dict(config["rules"][1], target_umo="aiocqhttp_main:GroupMessage:20002", target_account_id="10000002")
        config["rules"].append(extra)
        path.write_text(json.dumps(config), encoding="utf-8")
        await self.bridge.on_platform_message(FakeEvent())
        self.assertEqual(self.lark_deliveries[0][0], "hello")
        self.assertEqual(self.context.sent[0][0], "aiocqhttp_main:GroupMessage:20002")
        self.assertEqual(self.context.sent[0][1].chain[0].text, "hello")

    async def test_pause_resume_stops_forwarding_then_applies_new_message_without_restart(self):
        path = FakeStarTools.data_dir / "bridge_rules.json"
        config = json.loads(path.read_text(encoding="utf-8"))
        config["bridge_enabled"] = False
        path.write_text(json.dumps(config), encoding="utf-8")
        paused = FakeEvent(text="paused message")
        paused.message_obj.message_id = "paused-1"
        await self.bridge.on_platform_message(paused)
        self.assertTrue(paused.stopped)
        self.assertEqual(self.lark_deliveries, [])

        config["bridge_enabled"] = True
        path.write_text(json.dumps(config), encoding="utf-8")
        resumed = FakeEvent(text="resumed message")
        resumed.message_obj.message_id = "resumed-1"
        await self.bridge.on_platform_message(resumed)
        self.assertEqual(self.lark_deliveries, [("resumed message", "oc_1")])

    async def test_both_platform_self_authored_messages_are_rejected_when_switch_is_off(self):
        path = FakeStarTools.data_dir / "bridge_rules.json"
        config = json.loads(path.read_text(encoding="utf-8"))
        for rule in config["rules"]:
            rule["include_self"] = False
        path.write_text(json.dumps(config), encoding="utf-8")
        qq = FakeEvent(text="qq self")
        qq.message_obj.self_id = "bot"
        qq.get_sender_id = lambda: "bot"
        await self.bridge.on_platform_message(qq)
        await self.bridge.on_larkx_message(
            {"chat_id": "oc_1", "chat_type": 2, "from_id": "bot-user", "msg_id": "feishu-self", "content": "feishu self"},
            "default", "bot-user",
        )
        self.assertEqual(self.lark_deliveries, [])
        self.assertEqual(self.context.sent, [])

    async def test_same_platform_qq_fanout_keeps_each_destination_independent(self):
        path = FakeStarTools.data_dir / "bridge_rules.json"
        config = json.loads(path.read_text(encoding="utf-8"))
        config["rules"].append(dict(config["rules"][1], name="QQ second group", target_umo="aiocqhttp_main:GroupMessage:20002", target_account_id="10000002"))
        path.write_text(json.dumps(config), encoding="utf-8")
        event = FakeEvent(text="fanout")
        event.message_obj.message_id = "fanout-1"
        await self.bridge.on_platform_message(event)
        targets = [target for target, _chain in self.context.sent]
        self.assertEqual(targets, ["aiocqhttp_main:GroupMessage:20002"])
        self.assertEqual(self.lark_deliveries, [("fanout", "oc_1")])

    async def test_unavailable_video_does_not_block_or_duplicate_text_only_route(self):
        path = FakeStarTools.data_dir / "bridge_rules.json"
        config = json.loads(path.read_text(encoding="utf-8"))
        source = config["rules"][1]
        config["rules"] = [
            dict(source, name="Video route", target_umo="larkx:GroupMessage:oc_video", content_types=["video"]),
            dict(source, name="Text route", target_umo="larkx:GroupMessage:oc_text", content_types=["text"]),
        ]
        path.write_text(json.dumps(config), encoding="utf-8")
        event = FakeEvent(text="keep this text")
        event.message_obj.message_id = "mixed-unavailable-video"
        event.message_obj.raw_message = {"message": [
            {"type": "text", "data": {"text": "keep this text"}},
            {"type": "video", "data": {"file": "opaque-unavailable-video"}},
        ]}
        event.get_messages = lambda: [Video("opaque-unavailable-video")]

        with patch.object(self.bridge.runtime_status, "record_delivery") as deliveries:
            await self.bridge.on_platform_message(event)
            await self.bridge.on_platform_message(event)

        self.assertEqual(self.lark_deliveries, [("keep this text", "oc_text")])
        self.assertEqual(self.context.sent, [])
        failures = [call for call in deliveries.call_args_list if call.args == ("feishu", False)]
        successes = [call for call in deliveries.call_args_list if len(call.args) >= 2 and call.args[1] is True]
        self.assertEqual(len(failures), 2, "Only the failed video destination is retried on the duplicate event")
        self.assertEqual(len(successes), 1, "Successful text parts retain their existing per-route deduplication")

    async def test_feishu_to_feishu_preserves_native_media_and_also_sends_to_qq(self):
        path = FakeStarTools.data_dir / "bridge_rules.json"
        config = json.loads(path.read_text(encoding="utf-8"))
        config["rules"].append(dict(config["rules"][0], target_umo="larkx:GroupMessage:oc_2"))
        path.write_text(json.dumps(config), encoding="utf-8")
        with (
            patch.object(self.bridge.larkx_bridge, "forward_message", return_value=True) as forward,
            patch.object(self.bridge.larkx_bridge, "download_image", return_value=b"image") as download,
            patch("astrbot_plugin_message_bridge.larkx_media.send_image", return_value="native-image-id") as upload,
        ):
            await self.bridge.on_larkx_message(
                {"chat_id": "oc_1", "chat_type": 2, "from_id": "member", "msg_id": "image-fanout", "msg_type": 5, "content": ""},
                "default", "bot-user",
            )
        forward.assert_called_once_with("oc_2", "image-fanout")
        download.assert_called_once()
        upload.assert_not_called()
        self.assertEqual(len(self.context.sent), 1)
        self.assertIsInstance(self.context.sent[0][1].chain[0], Image)
        self.assertEqual(self.context.sent[0][1].chain[0].data, b"image")

    async def test_duplicate_platform_event_is_sent_once(self):
        first = FakeEvent()
        duplicate = FakeEvent()
        await self.bridge.on_platform_message(first)
        await self.bridge.on_platform_message(duplicate)
        self.assertTrue(first.stopped)
        self.assertTrue(duplicate.stopped)
        self.assertEqual(len(self.lark_deliveries), 1)

    async def test_same_group_event_seen_by_two_qq_accounts_forwards_once(self):
        first = FakeEvent()
        first.message_obj.self_id = "10000001"
        second = FakeEvent()
        second.message_obj.self_id = "10000002"
        await self.bridge.on_platform_message(first)
        await self.bridge.on_platform_message(second)
        self.assertEqual(self.lark_deliveries, [("hello", "oc_1")])

    async def test_visible_marker_is_ordinary_member_content_and_does_not_suppress_forwarding(self):
        event = FakeEvent(text="[搬运工V2] forwarded text")
        await self.bridge.on_platform_message(event)
        self.assertTrue(event.stopped)
        self.assertEqual(self.lark_deliveries, [("[搬运工V2] forwarded text", "oc_1")])

    async def test_personal_feishu_listener_starts_after_astrbot_loaded_without_agent(self):
        with tempfile.TemporaryDirectory() as larkx_home:
            old_home = os.environ.get("LARKX_HOME")
            os.environ["LARKX_HOME"] = larkx_home
            try:
                accounts = types.ModuleType("larkx.accounts")
                accounts.credentials_path = lambda _: Path(larkx_home) / "missing-credentials.json"
                auth = types.ModuleType("larkx.auth")
                auth.LarkAuth = object
                client = types.ModuleType("larkx.client")
                client.LarkClient = object
                with patch.dict(sys.modules, {"larkx.accounts": accounts, "larkx.auth": auth, "larkx.client": client}):
                    await self.bridge.start_personal_feishu_listener()
                    await asyncio.sleep(0.1)
                status_path = Path(larkx_home) / "bridge-status.json"
                status = json.loads(status_path.read_text(encoding="utf-8"))
                self.assertIn(status["state"], {"waiting_login", "starting"})
                self.assertEqual(status["agent_backend"], "none")
            finally:
                if old_home is None:
                    os.environ.pop("LARKX_HOME", None)
                else:
                    os.environ["LARKX_HOME"] = old_home

    async def test_unmanaged_platform_is_left_untouched(self):
        event = FakeEvent(platform="telegram")
        await self.bridge.on_platform_message(event)
        self.assertFalse(event.stopped)
        self.assertEqual(self.context.sent, [])


if __name__ == "__main__":
    unittest.main()
