import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import zipfile

import updater


class SafeUpdaterTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="mover-updater-test-")
        self.root = Path(self.temporary.name)
        self.source = self.root / "release"
        self.target = self.root / "installed"
        for root in (self.source, self.target):
            (root / "astrbot_plugin_message_bridge").mkdir(parents=True)
            (root / "astrbot_plugin_message_bridge/metadata.yaml").write_text(
                "name: astrbot_plugin_message_bridge\nversion: v2.0.6\n", encoding="utf-8")
            (root / "start-workbench.ps1").write_text("# start\n", encoding="utf-8")
            (root / "workbench_server.py").write_text("VERSION = 'old'\n", encoding="utf-8")
        (self.source / "workbench_server.py").write_text("VERSION = 'new'\n", encoding="utf-8")
        (self.source / "astrbot_plugin_message_bridge/larkx_richtext.py").write_text("# new media parser\n")
        self.user_files = [
            self.target / "runtime/LarkAgentX-data/credentials.json",
            self.target / "runtime/AstrBot/data/plugin_data/astrbot_plugin_message_bridge/bridge_rules.json",
            self.target / "runtime/AstrBot/data/plugin_data/pulids_snowluma_go_to_astrbot/snowluma/qq-session.db",
            self.target / "runtime/AstrBot/data/cmd_config.json",
            self.target / "workbench.settings.json",
            self.target / ".workbench-bootstrap.dpapi",
        ]
        for index, path in enumerate(self.user_files):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(bytes([0, 255, index]) + "用户的登录数据和群规则".encode())
        self.before = self.user_hashes()
        self.make_manifest()

    def tearDown(self):
        self.temporary.cleanup()

    def user_hashes(self):
        return {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in self.user_files}

    def make_manifest(self):
        manifest = updater.build_manifest(self.source, "2.0.6")
        (self.source / updater.MANIFEST_NAME).write_text(json.dumps(manifest), encoding="utf-8")
        return manifest

    def make_zip(self, extras=None):
        package = self.root / "release.zip"
        with zipfile.ZipFile(package, "w") as archive:
            for path in self.source.rglob("*"):
                if path.is_file():
                    archive.write(path, path.relative_to(self.source).as_posix())
            for name, data in (extras or {}).items():
                archive.writestr(name, data)
        return package

    def test_real_zip_update_retains_every_login_and_mapping_byte(self):
        result = updater.apply_release(self.make_zip(), self.target)
        self.assertEqual((self.target / "workbench_server.py").read_text(), "VERSION = 'new'\n")
        self.assertTrue((self.target / "astrbot_plugin_message_bridge/larkx_richtext.py").is_file())
        self.assertEqual(self.before, self.user_hashes())
        backup = Path(result["backup"])
        self.assertEqual((backup / "files/workbench_server.py").read_text(), "VERSION = 'old'\n")
        self.assertEqual(json.loads((backup / "journal.json").read_text())["state"], "complete")

    def test_running_cli_installs_unicode_directory_and_preserves_data(self):
        process = subprocess.run([sys.executable, "-B", str(Path(updater.__file__)), "apply",
                                  str(self.make_zip()), str(self.target)], capture_output=True, text=True)
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertTrue(json.loads(process.stdout)["updated"])
        self.assertEqual(self.before, self.user_hashes())

    def test_hash_mismatch_is_rejected_before_a_single_replacement(self):
        (self.source / "workbench_server.py").write_text("tampered")
        with self.assertRaisesRegex(updater.UpdateError, "checksum|size"):
            updater.apply_release(self.source, self.target)
        self.assertEqual((self.target / "workbench_server.py").read_text(), "VERSION = 'old'\n")
        self.assertFalse((self.target / "runtime/update-backups").exists())
        self.assertEqual(self.before, self.user_hashes())

    def test_write_failure_rolls_back_replaced_and_added_code(self):
        def fail_on_later_file(name):
            if name == "workbench_server.py":
                raise OSError("simulated locked destination")

        with self.assertRaises(OSError):
            updater.apply_release(self.source, self.target, _before_replace=fail_on_later_file)
        self.assertFalse((self.target / "astrbot_plugin_message_bridge/larkx_richtext.py").exists())
        self.assertEqual((self.target / "workbench_server.py").read_text(), "VERSION = 'old'\n")
        self.assertEqual(self.before, self.user_hashes())
        journals = list((self.target / "runtime/update-backups").glob("*/journal.json"))
        self.assertEqual(json.loads(journals[0].read_text())["state"], "rolled_back")

    def test_release_cannot_overwrite_runtime_or_account_settings(self):
        for name in ["runtime/LarkAgentX-data/credentials.json", "workbench.settings.json",
                     "astrbot_plugin_message_bridge/bridge_rules.json", "runtime/AstrBot/data/cmd_config.json",
                     "../credentials.json", "/tmp/workbench_server.py", "C:/workbench_server.py",
                     "workbench/../../workbench.settings.json", "workbench/CON.html", "workbench/app.js ",
                     "workbench\\app.js", "workbench/./app.js"]:
            with self.subTest(name=name), self.assertRaises(updater.UpdateError):
                updater.safe_payload_name(name)

    def test_extra_zip_data_or_traversal_entries_are_rejected(self):
        for name in ["../outside.py", "runtime/session.db", "workbench.settings.json", "workbench/unknown.js"]:
            with self.subTest(name=name), self.assertRaises(updater.UpdateError):
                updater.apply_release(self.make_zip({name: b"forbidden"}), self.target)
        self.assertEqual(self.before, self.user_hashes())
        self.assertFalse((self.root / "outside.py").exists())

    def test_duplicate_case_paths_are_rejected(self):
        package = self.make_zip({"workbench_SERVER.py": b"other"})
        with self.assertRaises(updater.UpdateError):
            updater.apply_release(package, self.target)

    def test_missing_or_wrong_application_identity_is_rejected(self):
        wrong = self.root / "other-project"
        wrong.mkdir()
        with self.assertRaises(updater.UpdateError):
            updater.apply_release(self.source, wrong)
        (self.target / "astrbot_plugin_message_bridge/metadata.yaml").write_text("name: other_project")
        with self.assertRaises(updater.UpdateError):
            updater.apply_release(self.source, self.target)
        self.assertEqual(self.before, self.user_hashes())

    def test_zip_symlink_is_rejected(self):
        package = self.make_zip()
        with zipfile.ZipFile(package, "a") as archive:
            info = zipfile.ZipInfo("workbench/linked.js")
            info.create_system = 3
            info.external_attr = 0o120777 << 16
            archive.writestr(info, "../../workbench.settings.json")
        with self.assertRaises(updater.UpdateError):
            updater.apply_release(package, self.target)

    def test_target_junction_does_not_write_outside_installation(self):
        if os.name != "nt":
            self.skipTest("Windows junction test")
        outside = self.root / "outside"
        outside.mkdir()
        junction = self.target / "workbench"
        script = self.root / "create-junction.ps1"
        script.write_text('param([string]$Link,[string]$Destination)\n$ErrorActionPreference="Stop"\nNew-Item -ItemType Junction -Path $Link -Target $Destination | Out-Null\n', encoding="utf-8-sig")
        result = subprocess.run(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script),
                                 str(junction), str(outside)], capture_output=True)
        if result.returncode:
            self.fail("Could not create the Windows junction fixture: " + result.stderr.decode(errors="replace"))
        try:
            (self.source / "workbench").mkdir()
            (self.source / "workbench/app.js").write_text("code")
            self.make_manifest()
            with self.assertRaises(updater.UpdateError):
                updater.apply_release(self.source, self.target)
            self.assertFalse((outside / "app.js").exists())
        finally:
            junction.rmdir()

    def test_sealer_hashes_the_actual_zip_payload_and_rejects_old_unsealed_package(self):
        package = self.root / "unsigned.zip"
        with zipfile.ZipFile(package, "w") as archive:
            for path in self.source.rglob("*"):
                if path.is_file() and path.name != updater.MANIFEST_NAME:
                    archive.write(path, path.relative_to(self.source).as_posix())
        with self.assertRaisesRegex(updater.UpdateError, "manifest"):
            updater.read_release(package)
        updater.seal_zip(package, "2.0.6")
        manifest, payload = updater.read_release(package)
        self.assertEqual(manifest["version"], "2.0.6")
        self.assertEqual(manifest["files"]["workbench_server.py"]["sha256"],
                         hashlib.sha256(payload["workbench_server.py"]).hexdigest())


if __name__ == "__main__":
    unittest.main()
