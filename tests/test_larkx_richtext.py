import copy
import importlib
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from astrbot_plugin_message_bridge import larkx_richtext as rich


PROJECT = Path(__file__).resolve().parents[1]
LARKX_SOURCE = PROJECT / "runtime" / "LarkAgentX-runtime" / "LarkAgentX-d288ef4df8a42f6286c5506b7185f64430071490"


class RichPostEnvelopeTests(unittest.TestCase):
    def test_detection_and_plain_post_never_use_generated_summary(self):
        for value in (2, "2", "post", "POST"):
            self.assertTrue(rich.is_rich_post({"msg_type": value}))
        self.assertFalse(rich.is_rich_post({"msg_type": 15}))
        self.assertFalse(rich.is_rich_post({"msg_type": 4, "content": "[富文本]\n[视频]"}))
        self.assertFalse(rich.is_rich_post({"msg_type": 4, "content_data": {"text": "ordinary", "richText": {}}}))
        self.assertFalse(rich.is_rich_post({"msg_type": 4, "content_data": {"richText": {
            "elements": {"dictionary": {"1": {"tag": 1}, "2": {"tag": 10}}}}}}))
        for value in (4, "4", "text", "TEXT"):
            for tag in (2, "IMG", 18, "MEDIA"):
                self.assertTrue(rich.is_rich_post({"msg_type": value, "content_data": {"richText": {
                    "elements": {"dictionary": {"1": {"tag": tag}}}}}}))
        with patch.object(rich, "_element_types", side_effect=AssertionError("unnecessary protobuf import")):
            with self.assertRaises(rich.RichPostError):
                rich.rich_post_parts({"msg_type": 2, "content": "[富文本]\n[视频]"})
            result = rich.rich_post_parts({"msg_type": 2, "content_data": {"text": "real text"}})
        self.assertEqual(result[0]["content"], "real text")

    def test_malformed_envelopes_and_dangling_roots_fail_explicitly(self):
        cases = [{"msg_type": 2}, {"msg_type": 2, "content_data": {}},
                 {"msg_type": 2, "content_data": {"richText": {}}},
                 {"msg_type": 2, "content_data": []},
                 {"msg_type": 2, "content_data": {"richText": []}},
                 {"msg_type": 2, "content_data": {"richText": {"elements": []}}},
                 {"msg_type": 2, "content_data": {"richText": {"elementIds": ["missing"]}}}]
        for case in cases:
            with self.assertRaises(rich.RichPostError):
                rich.rich_post_parts(case)


@unittest.skipUnless((LARKX_SOURCE / "larkx" / "proto" / "gateway.py").is_file(), "Pinned LarkAgentX source is not installed")
class RichPostProtobufTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.inserted_source = str(LARKX_SOURCE) not in sys.path
        if cls.inserted_source:
            sys.path.insert(0, str(LARKX_SOURCE))
        cls.E = importlib.import_module("larkx.proto.gateway")._lark_all().entities.RichTextElement

    @classmethod
    def tearDownClass(cls):
        if cls.inserted_source:
            sys.path.remove(str(LARKX_SOURCE))

    def node(self, tag, prop=None, children=()):
        return {"tag": tag, "property": prop.SerializeToString() if prop is not None else b"", "childIds": list(children)}

    def video(self, key="fixture-video"):
        return self.node(18, self.E.MediaProperty(key=key, name="clip.mov", size=1234, mime="video/quicktime", duration=13000, fsUnit="fixture-unit"))

    def text(self, value):
        return self.node(1, self.E.TextProperty(content=value))

    def post(self, nodes, roots=("1",), title=""):
        return {"msg_type": 2, "msg_id": "original-post", "chat_id": "original-chat", "sender_id": "sender",
                "content": "[富文本]\n[视频]", "content_data": {"title": title, "text": "summary must not win",
                "richText": {"elementIds": list(roots), "elements": {"dictionary": nodes}}}}

    def test_nested_video_preserves_download_envelope_and_resource_fields(self):
        original = self.post({"3": self.video(), "1": self.node(22, children=["2"]), "2": self.node(4, children=["3"])})
        before = copy.deepcopy(original)
        parts = rich.rich_post_parts(original)
        self.assertEqual(original, before)
        self.assertEqual(len(parts), 1)
        part = parts[0]
        self.assertEqual((part["msg_type"], part["content"], part["_rich_part_id"]), (15, "", "3"))
        self.assertEqual((part["msg_id"], part["chat_id"], part["sender_id"]), ("original-post", "original-chat", "sender"))
        self.assertEqual(part["content_data"]["key"], "fixture-video")
        self.assertEqual(part["content_data"]["duration"], 13000)

    def test_text_envelope_uses_structured_media_instead_of_summary(self):
        message = self.post({"1": self.node(22, children=["2", "3"]),
                             "2": self.text("caption"), "3": self.video()})
        message.update(msg_type=4, chat_type=1, from_id="account-self")
        self.assertTrue(rich.is_rich_post(message))
        parts = rich.rich_post_parts(message)
        self.assertEqual([part["msg_type"] for part in parts], [4, 15])
        self.assertEqual([part["content"] for part in parts], ["caption", ""])
        self.assertEqual(parts[1]["content_data"]["key"], "fixture-video")
        self.assertEqual((parts[1]["chat_type"], parts[1]["from_id"]), (1, "account-self"))

    def test_mixed_order_shared_nodes_and_unreachable_metadata(self):
        message = self.post({"4": self.text("after"), "3": self.video(), "2": self.text("before"),
                             "1": self.node(22, children=["2", "3", "4", "3"]), "99": self.video("metadata")})
        parts = rich.rich_post_parts(message)
        self.assertEqual([part["msg_type"] for part in parts], [4, 15, 4])
        self.assertEqual([part["content"] for part in parts], ["before", "", "after"])
        self.assertEqual([part["_rich_part_id"] for part in parts], [part["_rich_part_id"] for part in rich.rich_post_parts(message)])

    def test_actual_bracket_labels_remain_text(self):
        parts = rich.rich_post_parts(self.post({"1": self.text("[富文本]\n[视频]")}))
        self.assertEqual(parts[0]["content"], "[富文本]\n[视频]")

    def test_title_and_paragraphs_preserve_order(self):
        parts = rich.rich_post_parts(self.post({"1": self.node(22, children=["2", "3"]),
              "2": self.node(3, children=["4", "5"]), "3": self.node(3, self.E.TextProperty(content="second")),
              "4": self.text("first "), "5": self.text("line")}, title="actual title"))
        self.assertEqual([part["content"] for part in parts], ["actual title", "first line\nsecond"])

    def test_image_v2_and_origin_key_normalize_for_existing_downloader(self):
        v2 = self.E.ImageProperty()
        v2.image.imageKey = "fixture-v2"
        legacy = self.E.ImageProperty(originKey="fixture-origin", fsUnit="fixture-unit")
        parts = rich.rich_post_parts(self.post({"1": self.node(2, v2), "2": self.node(2, legacy)}, roots=["1", "2"]))
        extract = importlib.import_module("larkx.media").extract_resource_key
        self.assertEqual([extract(part["msg_type"], part["content_data"]) for part in parts], ["fixture-v2", "fixture-origin"])

    def test_missing_media_key_or_corrupt_property_is_not_placeholder_success(self):
        for node in (self.video(""), {"tag": 18, "property": b"\xff"}, {"tag": 18, "property": "not bytes"}):
            with self.subTest(node_type=type(node["property"])):
                with self.assertRaises(rich.RichPostError):
                    rich.rich_post_parts(self.post({"1": node}))

    def test_flat_legacy_layout_sorts_ids_without_adding_unreachable_nodes_to_trees(self):
        parts = rich.rich_post_parts(self.post({"10": self.video(), "2": self.text("first")}, roots=[]))
        self.assertEqual([part["msg_type"] for part in parts], [4, 15])
        with self.assertRaises(rich.RichPostError):
            rich.rich_post_parts(self.post({"1": self.node(22, children=["2"]), "2": self.text("text")}, roots=[]))

    def test_cycles_missing_children_and_oversized_properties_fail_with_typed_error(self):
        cases = [
            self.post({"1": self.node(22, children=["2"]), "2": self.node(3, children=["1"])}),
            self.post({"1": self.node(22, children=["missing"])}),
            self.post({"1": {"tag": 18, "property": b"x" * (rich.MAX_PROPERTY_BYTES + 1)}}),
        ]
        for case in cases:
            with self.assertRaises(rich.RichPostError):
                rich.rich_post_parts(case)

    def test_depth_text_parts_and_total_property_budgets(self):
        deep = {str(i): self.node(22, children=[str(i + 1)]) for i in range(1, rich.MAX_DEPTH + 3)}
        deep[str(rich.MAX_DEPTH + 3)] = self.text("end")
        with self.assertRaises(rich.RichPostError):
            rich.rich_post_parts(self.post(deep))
        with patch.object(rich, "MAX_TEXT_CHARS", 3), self.assertRaises(rich.RichPostError):
            rich.rich_post_parts(self.post({"1": self.text("longer")}))
        with patch.object(rich, "MAX_PARTS", 1), self.assertRaises(rich.RichPostError):
            rich.rich_post_parts(self.post({"1": self.video(), "2": self.video()}, roots=["1", "2"]))
        with patch.object(rich, "MAX_TOTAL_PROPERTY_BYTES", 1), self.assertRaises(rich.RichPostError):
            rich.rich_post_parts(self.post({"1": self.video()}))


if __name__ == "__main__":
    unittest.main()
