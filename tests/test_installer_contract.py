import unittest
import xml.etree.ElementTree as ET
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class InstallerLaunchContractTests(unittest.TestCase):
    def test_installer_launch_targets_use_the_native_launcher(self):
        bundle = (ROOT / "installer" / "Bundle.wxs").read_text(encoding="utf-8")
        product = (ROOT / "installer" / "Product.wxs").read_text(encoding="utf-8")
        build = (ROOT / "installer" / "build-installer.ps1").read_text(encoding="utf-8")

        self.assertIn('LaunchTarget="[InstallFolder]mover-v2-launcher.exe"', bundle)
        self.assertNotIn('LaunchTarget="[InstallFolder]launch-workbench.cmd"', bundle)
        shortcuts = {node.attrib["Id"]: node.attrib for node in ET.fromstring(product).iter()
                     if node.tag.endswith("}Shortcut")}
        self.assertEqual(len(shortcuts), 3)
        for shortcut in shortcuts.values():
            self.assertEqual(shortcut["Target"], "[INSTALLFOLDER]mover-v2-launcher.exe")
        self.assertNotIn("Arguments", shortcuts["StartWorkbenchShortcut"])
        self.assertEqual(shortcuts["OpenWorkbenchShortcut"]["Arguments"], "--open")
        self.assertEqual(shortcuts["DesktopWorkbenchShortcut"]["Arguments"], "--open")
        self.assertIn('LaunchArguments="--open"', bundle)
        self.assertIn("Copy-Item -LiteralPath $portableLauncher -Destination (Join-Path $payloadRoot 'mover-v2-launcher.exe')", build)
        for required in ("updater.py", "update-and-start.ps1", "log_store.py"):
            self.assertIn("'" + required + "'", build)
        self.assertIn("manifest $payloadRoot --version 2.0.9", build)
        self.assertIn("Get-FileHash -LiteralPath $portableUv -Algorithm SHA256", build)
        self.assertIn("Copy-Item -LiteralPath $portableUv -Destination $bundledUv", build)
        self.assertIn("03FA174894B9D0CBE08762D40D1C2C64D10E875BD56F1A1E334EE1E00D60CDBE", build)

    def test_one_click_cmd_verifies_and_applies_updates_before_starting(self):
        root = Path(__file__).resolve().parents[1]
        for name in ("一键启动.cmd", "launch-workbench.cmd"):
            text = (root / name).read_text(encoding="utf-8")
            self.assertIn("update-and-start.ps1", text)
            self.assertNotIn('"%~dp0start-workbench.ps1"', text)
            self.assertIn('start "" /b "%~dp0mover-v2-launcher.exe" --background', text)
            self.assertNotIn("pause", text)
        updater = (root / "update-and-start.ps1").read_text(encoding="utf-8-sig")
        self.assertIn("preserve_background", updater)
        self.assertIn("updater apply", updater)
        self.assertIn("ResumeBackground = $true", updater)
        self.assertIn("update-backups", (root / "updater.py").read_text(encoding="utf-8"))

    def test_packaged_powershell_scripts_have_utf8_bom_for_windows_powershell(self):
        scripts = [
            *(ROOT.glob("*.ps1")),
            *(ROOT / "installer").glob("*.ps1"),
        ]
        self.assertGreater(len(scripts), 0)
        for script in scripts:
            with self.subTest(script=script.name):
                self.assertTrue(script.read_bytes().startswith(b"\xef\xbb\xbf"))

    def test_installer_defaults_to_the_per_user_programs_folder(self):
        bundle = (ROOT / "installer" / "Bundle.wxs").read_text(encoding="utf-8")
        product = (ROOT / "installer" / "Product.wxs").read_text(encoding="utf-8")

        self.assertIn('Value="[LocalAppDataFolder]Programs\\搬运工V2"', bundle)
        self.assertIn('<StandardDirectory Id="PerUserProgramFilesFolder">', product)
        self.assertNotIn('Value="D:\\搬运工V2"', product)


if __name__ == "__main__":
    unittest.main()
