"""Exercise clean package extraction without launching SnowLuma or QQ."""
import hashlib
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'install_snowluma.ps1'
ARCHIVE = ROOT / 'runtime/SnowLuma/SnowLuma-v2.4.2-win-x64.zip'
SHA256 = '0912926c5e73260f35b000278a8658493fc9452f36782c32fa86f0e825bad81a'


@unittest.skipUnless(os.name == 'nt', 'Windows PowerShell bootstrap')
class SnowLumaBootstrapTests(unittest.TestCase):
    def run_installer(self, root, archive):
        powershell = Path(os.environ['SystemRoot']) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
        return subprocess.run([str(powershell), '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
                               '-File', str(SCRIPT), '-AstrBotRoot', str(root), '-ArchivePath', str(archive)],
                              capture_output=True, encoding='utf-8', errors='replace', timeout=90)

    def test_wrong_archive_fails_before_extraction_or_target_creation(self):
        with tempfile.TemporaryDirectory(prefix='mover-snow-hash-') as directory:
            folder = Path(directory)
            archive = folder / 'bad.zip'
            archive.write_bytes(b'not the pinned official archive')
            target_root = folder / 'AstrBot'
            result = self.run_installer(target_root, archive)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('SHA-256 mismatch', result.stderr)
            self.assertFalse((target_root / 'data/plugin_data/pulids_snowluma_go_to_astrbot/snowluma').exists())

    def test_incomplete_existing_target_is_never_overwritten(self):
        with tempfile.TemporaryDirectory(prefix='mover-snow-existing-') as directory:
            folder = Path(directory)
            target = folder / 'AstrBot/data/plugin_data/pulids_snowluma_go_to_astrbot/snowluma'
            target.mkdir(parents=True)
            marker = target / 'config.json'
            marker.write_bytes(b'preserve-existing-configuration')
            result = self.run_installer(folder / 'AstrBot', folder / 'unused.zip')
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(marker.read_bytes(), b'preserve-existing-configuration')
            self.assertEqual(list(target.iterdir()), [marker])

    @unittest.skipUnless(ARCHIVE.is_file(), 'Pinned official SnowLuma archive is not cached')
    def test_real_clean_extraction_uses_manager_path_and_second_run_preserves_data(self):
        self.assertEqual(hashlib.sha256(ARCHIVE.read_bytes()).hexdigest(), SHA256)
        with tempfile.TemporaryDirectory(prefix='mover-snow-clean-') as directory:
            root = Path(directory) / 'AstrBot'
            result = self.run_installer(root, ARCHIVE)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            target = root / 'data/plugin_data/pulids_snowluma_go_to_astrbot/snowluma'
            for relative in ('node.exe', 'index.mjs', 'release.json', 'native/snowluma-win32-x64.dll'):
                self.assertGreater((target / relative).stat().st_size, 0)
            config = target / 'config'
            config.mkdir(exist_ok=True)
            marker = config / 'account-preservation.fixture'
            marker.write_bytes(b'fixture-only-no-credentials')
            original = marker.stat().st_mtime_ns
            second = self.run_installer(root, Path(directory) / 'nonexistent-archive.zip')
            self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
            self.assertIn('already complete', second.stdout)
            self.assertEqual(marker.read_bytes(), b'fixture-only-no-credentials')
            self.assertEqual(marker.stat().st_mtime_ns, original)
            self.assertFalse((root / 'runtime/SnowLuma/app').exists())


if __name__ == '__main__':
    unittest.main()
