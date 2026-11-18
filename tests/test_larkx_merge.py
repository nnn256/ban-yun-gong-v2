import importlib
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from astrbot_plugin_message_bridge import larkx_media, larkx_merge


PROJECT = Path(__file__).resolve().parents[1]
LARKX_SOURCE = PROJECT / "runtime" / "LarkAgentX-runtime" / "LarkAgentX-d288ef4df8a42f6286c5506b7185f64430071490"


@unittest.skipUnless((LARKX_SOURCE / "larkx" / "proto" / "gateway.py").is_file(), "Pinned LarkAgentX source is not installed")
class MergedForwardProtobufTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        source = str(LARKX_SOURCE)
        if source not in sys.path:
            sys.path.insert(0, source)
            cls.addClassCleanup(sys.path.remove, source)
        cls.P = importlib.import_module("larkx.proto.proto_pb2")
        cls.E = importlib.import_module("larkx.proto.gateway")._lark_all().entities.RichTextElement
        cls.decoders = importlib.import_module("larkx.proto.decoders")

    def push_merge(self, merged):
        request = self.P.PushMessagesRequest()
        entity = request.messages["outer-record"]
        entity.id, entity.type, entity.fromId = "outer-record", 11, "logged-in-account"
        entity.chatId, entity.chatType = "current-chat", 1
        entity.content = merged.SerializeToString()
        return self.decoders.push_messages(request.SerializeToString())[0]

    def add_child(self, merged, kind, body):
        item = merged.messages.add(id="child", type=kind, fromId="hidden-sender", chatId="old-chat")
        item.content = body.SerializeToString()

    def test_push_record_keeps_original_text_and_downloads_image_from_visible_copy(self):
        record = self.P.MergeForwardContent(groupChatName="record title")
        image = self.P.ImageContent()
        image.imageV2.imageKey = "fixture-image-key"
        self.add_child(record, 4, self.P.TextContent(text="first message"))
        self.add_child(record, 4, self.P.TextContent(text="second message"))
        self.add_child(record, 5, image)
        message = self.push_merge(record)
        self.assertIn("hidden-sender", message["content"])
        self.assertIn("fixture-image-key", message["content"])

        parts = larkx_merge.merged_forward_parts(message)
        self.assertEqual([part["msg_type"] for part in parts], [4, 4, 5])
        self.assertEqual([part["content"] for part in parts], ["first message", "second message", ""])
        self.assertEqual({(part["chat_id"], part["msg_id"]) for part in parts}, {("current-chat", "outer-record")})
        media = importlib.import_module("larkx.media")
        client = SimpleNamespace(auth=SimpleNamespace(region=SimpleNamespace(file_host="fixture.invalid")))
        image_bytes = b"\xff\xd8\xfffixture-image"
        with patch.object(media, "message_resource_url", return_value="image-resource") as url, patch.object(
            media, "download", return_value=(image_bytes, "image/jpeg")
        ) as fetch:
            self.assertEqual(larkx_media.download_image(client, parts[2]), image_bytes)
        url.assert_called_once_with("outer-record", "fixture-image-key", "current-chat", host="fixture.invalid")
        fetch.assert_called_once_with(client.auth, "image-resource", max_bytes=larkx_media.MAX_IMAGE_BYTES)

    def test_nested_record_expands_post_video_with_outer_download_address(self):
        record = self.P.MergeForwardContent(groupChatName="outer record")
        nested = self.P.MergeForwardContent(groupChatName="nested record")
        post = self.P.PostContent()
        post.richText.elementIds.append("1")
        nodes = post.richText.elements.dictionary
        nodes["1"].tag = 22
        nodes["1"].childIds.extend(["2", "3"])
        nodes["2"].tag = 1
        nodes["2"].property = self.E.TextProperty(content="caption").SerializeToString()
        nodes["3"].tag = 18
        nodes["3"].property = self.E.MediaProperty(key="fixture-video", name="clip.mp4", duration=8000).SerializeToString()
        self.add_child(nested, 2, post)
        self.add_child(record, 11, nested)
        self.add_child(record, 4, self.P.TextContent(text="after video"))

        parts = larkx_merge.merged_forward_parts(self.push_merge(record))
        self.assertEqual([part["msg_type"] for part in parts], [4, 15, 4])
        self.assertEqual([part["content"] for part in parts], ["caption", "", "after video"])
        self.assertEqual(parts[1]["content_data"]["key"], "fixture-video")
        self.assertEqual((parts[1]["chat_id"], parts[1]["msg_id"]), ("current-chat", "outer-record"))
