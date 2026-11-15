import tempfile
import unittest
from pathlib import Path

from astrbot_plugin_message_bridge import qq_emojis


class QQEmojiLookupTests(unittest.TestCase):
    def setUp(self):
        qq_emojis._FACE_CACHE.clear()
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        qq_emojis._FACE_CACHE.clear()
        self.temp.cleanup()

    def add_asset(self, version: str, face_id: str = "311") -> Path:
        path = self.root / "versions" / version / "resources" / "app" / "resource" / "default-emojis" / f"{face_id}.png"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"png-fixture")
        return path

    def test_selects_highest_natural_version_without_pinning_a_version(self):
        self.add_asset("9.9.9")
        newest = self.add_asset("9.9.10")
        self.assertEqual(qq_emojis.find_face_image(311, install_roots=[self.root]), newest)

    def test_cache_returns_only_existing_assets_and_rechecks_after_removal(self):
        cached = self.add_asset("9.9.10")
        older = self.add_asset("9.9.9")
        self.assertEqual(qq_emojis.find_face_image("311", install_roots=[self.root]), cached)
        cached.unlink()
        self.assertEqual(qq_emojis.find_face_image("311", install_roots=[self.root]), older)

    def test_missing_id_or_asset_returns_none(self):
        self.add_asset("9.9.10", "5")
        self.assertIsNone(qq_emojis.find_face_image(311, install_roots=[self.root]))
        self.assertIsNone(qq_emojis.find_face_image("not-an-id", install_roots=[self.root]))
        self.assertIsNone(qq_emojis.find_face_image("../311", install_roots=[self.root]))
        self.assertIsNone(qq_emojis.find_face_image(True, install_roots=[self.root]))

    def test_supports_a_flat_portable_install_root(self):
        flat = self.root / "portable"
        image = flat / "resources" / "app" / "resource" / "default-emojis" / "14.png"
        image.parent.mkdir(parents=True)
        image.write_bytes(b"png-fixture")
        self.assertEqual(qq_emojis.find_face_image("14", install_roots=[flat]), image)


if __name__ == "__main__":
    unittest.main()
