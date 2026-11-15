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

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if "id" in attrs:
            self.ids.append(attrs["id"])
        if tag == "html":
            self.ui_version = attrs.get("data-workbench-ui")
        if tag in {"script", "link"}:
            self.assets.append(attrs.get("src") or attrs.get("href") or "")


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
        self.assertIn("function buildRouteFromEndpoints(source, target, includeSelf = false)", self.javascript)

    def test_generic_endpoints_filter_accounts_search_names_and_build_all_platform_pairs(self):
        node = shutil.which("node")
        if node is None:
            self.skipTest("Node.js is not installed")
        script = Path(__file__).with_name("workbench_dom.test.cjs")
        result = subprocess.run([node, str(script)], capture_output=True, text=True, encoding="utf-8", errors="replace")
        self.assertEqual(result.returncode, 0, (result.stdout or "") + (result.stderr or ""))


if __name__ == "__main__":
    unittest.main()
