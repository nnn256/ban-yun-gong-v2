import re
import shutil
import subprocess
import unittest
from html.parser import HTMLParser
from pathlib import Path


class WorkbenchMarkup(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids = []
        self.ui_version = None
        self.assets = []
        self.content_types = []
        self.content_labels = []
        self._current_content_type = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if "id" in attrs:
            self.ids.append(attrs["id"])
        if tag == "html":
            self.ui_version = attrs.get("data-workbench-ui")
        if tag in {"script", "link"}:
            self.assets.append(attrs.get("src") or attrs.get("href") or "")
        if tag == "input" and attrs.get("name") == "contentType":
            self.content_types.append(attrs)
            self._current_content_type = attrs.get("value")

    def handle_data(self, data):
        if self._current_content_type and data.strip():
            self.content_labels.append((self._current_content_type, data.strip()))

    def handle_endtag(self, tag):
        if tag == "label":
            self._current_content_type = None


class WorkbenchDomContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        root = Path(__file__).resolve().parents[1]
        cls.javascript = (root / "workbench" / "app.js").read_text(encoding="utf-8")
        cls.html = (root / "workbench" / "index.html").read_text(encoding="utf-8")
        cls.markup = WorkbenchMarkup()
        cls.markup.feed(cls.html)

    def test_status_fields_and_action_bindings_exist_in_page(self):
        references = set(re.findall(r"\$\('#([A-Za-z0-9_-]+)'\)", self.javascript))
        # These selectors are constructed from runtime platform/step names.
        references.update({"qqState", "qqDot", "feishuState", "feishuDot",
                           "wizardRuntimeState", "wizardQqState", "wizardFeishuState", "wizardMappingState"})
        references.update(side + suffix for side in ("source", "target") for suffix in (
            "Platform", "Instance", "InstanceOptions", "Account", "AccountLabel", "AccountHelp",
            "MessageType", "Session", "SessionLabel", "SessionHelp", "SessionSearch", "SessionPicker",
        ))
        self.assertEqual(references - set(self.markup.ids), set(), "JavaScript references a missing DOM field")
        self.assertEqual(len(self.markup.ids), len(set(self.markup.ids)), "Duplicate IDs make status rendering ambiguous")

    def test_html_and_assets_share_current_javascript_version(self):
        version = re.search(r"const UI_VERSION = '([^']+)'", self.javascript).group(1)
        self.assertEqual(self.markup.ui_version, version)
        for asset in self.markup.assets:
            self.assertIn(f"?v={version}", asset)
        self.assertIn("mappingIncludeSelf", self.markup.ids)
        self.assertIn('id="mappingIncludeSelf" type="checkbox"', self.html)
        self.assertIn("本人手动发送的消息（默认关闭）", self.html)
        self.assertIn("data-toggle-self", self.javascript)

    def test_content_type_controls_have_supported_labels_and_all_defaults(self):
        expected = [("text", "文字"), ("emoji", "表情包"), ("image", "图片"),
                    ("video", "视频"), ("audio", "语音"), ("file", "文件")]
        self.assertIn("contentTypeOptions", self.markup.ids)
        self.assertEqual(self.markup.content_labels, expected)
        self.assertEqual([field.get("value") for field in self.markup.content_types],
                         [kind for kind, _ in expected])
        self.assertTrue(all(field.get("type") == "checkbox" and "checked" in field
                            for field in self.markup.content_types))
        self.assertIn("至少选择一种转发内容类型。", self.javascript)

    def test_endpoint_selection_content_filters_and_editor_state(self):
        node = shutil.which("node")
        if node is None:
            self.skipTest("Node.js is not installed")
        script = Path(__file__).with_name("workbench_dom.test.cjs")
        result = subprocess.run([node, str(script)], capture_output=True, text=True, encoding="utf-8", errors="replace")
        self.assertEqual(result.returncode, 0, (result.stdout or "") + (result.stderr or ""))


if __name__ == "__main__":
    unittest.main()
