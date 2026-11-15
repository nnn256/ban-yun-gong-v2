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
from unittest.mock import patch


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


class Video(Record):
    pass


class File(Record):
    def __init__(self, name, file):
        super().__init__(file)
        self.name = name


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
        self.message_obj = SimpleNamespace(self_id="bot", message_id="m-1", timestamp=1)
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
                            "include_self": True,
                        },
                        {
                            "name": "QQ to LarkAgentX",
                            "source_platform": "aiocqhttp",
                            "source_platform_id": "aiocqhttp_main",
                            "source_message_type": "GroupMessage",
                            "source_session_id": "10001",
                            "target_umo": "larkx:GroupMessage:oc_1",
                            "include_self": True,
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
        self.assertEqual([type(item).__name__ for item in chain.chain], ["Plain", "Image", "Plain"])
        self.assertEqual(chain.chain[1].url, "https://fixture.invalid/sticker.png")

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

    async def test_legacy_include_self_cannot_enable_own_messages_for_either_platform(self):
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
        self.assertEqual(self.lark_deliveries, [])
        self.assertEqual(self.context.sent, [])

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

    async def test_qq_video_and_audio_upload_real_attachments(self):
        event = FakeEvent(text="")
        event.get_messages = lambda: [Video("clip.mp4"), Record("voice.silk")]
        with patch("astrbot_plugin_message_bridge.larkx_media.send_attachment", side_effect=["video-id", "audio-id"]) as send:
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
        forward.assert_not_called()
        download.assert_called_once()
        upload.assert_called_once()
        self.assertEqual(upload.call_args.args[1], "oc_2")
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
