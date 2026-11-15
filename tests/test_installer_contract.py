import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class InstallerLaunchContractTests(unittest.TestCase):
    def test_installer_launch_targets_use_the_native_launcher(self):
        bundle = (ROOT / "installer" / "Bundle.wxs").read_text(encoding="utf-8")
        product = (ROOT / "installer" / "Product.wxs").read_text(encoding="utf-8")
        build = (ROOT / "installer" / "build-installer.ps1").read_text(encoding="utf-8")

        self.assertIn('LaunchTarget="[InstallFolder]mover-v2-launcher.exe"', bundle)
        self.assertNotIn('LaunchTarget="[InstallFolder]launch-workbench.cmd"', bundle)
        self.assertEqual(product.count('Target="[INSTALLFOLDER]mover-v2-launcher.exe"'), 2)
        self.assertIn("Copy-Item -LiteralPath $portableLauncher -Destination (Join-Path $payloadRoot 'mover-v2-launcher.exe')", build)

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
