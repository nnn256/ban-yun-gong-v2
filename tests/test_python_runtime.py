"""Use real uv Windows launchers with missing Python targets, without account access."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest

import updater


ROOT = Path(__file__).resolve().parents[1]
UV = ROOT / "tools/uv.exe"
POWERSHELL = Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"


@unittest.skipUnless(os.name == "nt" and UV.is_file(), "Windows with bundled uv required")
class PythonRuntimeRecoveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # The application already pins Python 3.12; use its actual base distribution
        # only as an isolated fixture. Never mutate a global interpreter or login.
        found = subprocess.run([str(UV), "python", "find", "--system", "--managed-python",
                                "--no-python-downloads", "--resolve-links", "3.12.13"],
                               capture_output=True, encoding="utf-8", timeout=20)
        if found.returncode:
            raise AssertionError("The runtime regression requires cached CPython 3.12.13")
        cls.base = Path(found.stdout.strip()).parent

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="mover-python-repair-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "安装目录 with spaces"
        self.root.mkdir()
        shutil.copy2(ROOT / "python-runtime.ps1", self.root / "python-runtime.ps1")
        (self.root / "tools").mkdir()
        shutil.copy2(UV, self.root / "tools/uv.exe")
        self.env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
        # Prove recovery does not depend on py.exe, the app execution aliases,
        # an activated venv, or this test's Python being discoverable on PATH.
        self.env["PATH"] = str(POWERSHELL.parent) + os.pathsep + str(POWERSHELL.parents[2])
        self.env.pop("VIRTUAL_ENV", None)
        self.env.pop("UV_PYTHON", None)
        self.env["UV_OFFLINE"] = "1"
        self.protected = {}
        for name in ("runtime/LarkAgentX-data/credentials.json", "workbench.settings.json",
                     "runtime/AstrBot/data/plugin_data/astrbot_plugin_message_bridge/bridge_rules.json",
                     "runtime/AstrBot/data/qq-session.db", "runtime/delivery.sqlite"):
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"fixture-private-state\x00\xff")
            self.protected[path] = hashlib.sha256(path.read_bytes()).hexdigest()

    def assert_protected(self):
        self.assertEqual(self.protected, {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in self.protected})

    def seed_managed_base(self):
        destination = self.root / "runtime/python/cpython-3.12.13-windows-x86_64-none"
        shutil.copytree(self.base, destination, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        return destination / "python.exe"

    def create_venv(self, *, broken=False):
        venv = self.root / "runtime/.venv"
        result = subprocess.run([str(UV), "venv", "--python", str(self.base / "python.exe"), str(venv)],
                                env=self.env, capture_output=True, encoding="utf-8", timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        python = venv / "Scripts/python.exe"
        if broken:
            # uv embeds the base path in PE resources, independently of pyvenv.cfg.
            # Replace with an equally long nonexistent path in this COPY only.
            executable = python.read_bytes()
            probe = subprocess.run([str(python), "-I", "-B", "-c", "import sys; print(sys._base_executable)"],
                                   env=self.env, capture_output=True, encoding="utf-8", timeout=10)
            self.assertEqual(probe.returncode, 0, probe.stderr)
            base_path = probe.stdout.strip().encode()
            self.assertEqual(executable.count(base_path), 1)
            missing = base_path.replace(b"python.exe", b"pythoX.exe")
            self.assertEqual(len(missing), len(base_path))
            python.write_bytes(executable.replace(base_path, missing))
            failed = subprocess.run([str(python), "-I", "-B", "-c", "print('unexpected')"],
                                    env=self.env, capture_output=True, encoding="utf-8", timeout=10)
            self.assertNotEqual(failed.returncode, 0)
            self.assertIn("uv trampoline failed to spawn Python child process", failed.stderr)
        return python

    def run_helper(self, code, *, timeout=45):
        script = self.root / "probe.ps1"
        script.write_text("$ErrorActionPreference='Stop'\n. (Join-Path $PSScriptRoot 'python-runtime.ps1')\n" + code,
                          encoding="utf-8-sig")
        return subprocess.run([str(POWERSHELL), "-NoLogo", "-NoProfile", "-ExecutionPolicy", "Bypass",
                               "-File", str(script)], cwd=Path(self.temp.name), env=self.env,
                              capture_output=True, encoding="utf-8", errors="replace", timeout=timeout)

    def test_probe_detects_the_exact_missing_child_error_without_leaking_it_to_pipeline(self):
        self.create_venv(broken=True)
        result = self.run_helper("Test-MoverPython -Executable (Join-Path $PSScriptRoot 'runtime/.venv/Scripts/python.exe')")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout.strip(), "False")
        self.assertEqual(result.stderr.strip(), "")
        self.assert_protected()

    def test_verification_falls_back_offline_with_no_system_python_and_leaves_broken_venv_untouched(self):
        python = self.create_venv(broken=True)
        original = python.read_bytes()
        self.seed_managed_base()
        result = self.run_helper("$p=Get-MoverVerificationPython $PSScriptRoot $PSScriptRoot; $p | ConvertTo-Json -Compress")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        selected = json.loads(result.stdout.splitlines()[-1])
        self.assertTrue(Path(selected["Executable"]).is_relative_to(self.root / "runtime/python"))
        self.assertEqual(selected["Arguments"], [])
        self.assertEqual(python.read_bytes(), original)
        self.assert_protected()

    def test_real_repair_rewrites_trampoline_preserves_packages_and_is_idempotent(self):
        python = self.create_venv(broken=True)
        sentinel = self.root / "runtime/.venv/Lib/site-packages/fixture_retained.py"
        sentinel.write_text("VALUE = 42\n", encoding="utf-8")
        self.seed_managed_base()
        self.env["UV_VENV_CLEAR"] = "1"
        code = ("$p=Initialize-MoverApplicationPython $PSScriptRoot; "
                "& $p -I -B -c 'import fixture_retained; assert fixture_retained.VALUE==42; print(123)'; exit $LASTEXITCODE")
        result = self.run_helper(code)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout.splitlines()[-1], "123")
        before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in (python, python.parents[1] / "pyvenv.cfg", sentinel)}
        second = self.run_helper(code)
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        self.assertEqual(before, {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in before})
        self.assert_protected()

    def test_healthy_environment_does_not_require_download_or_rewrite(self):
        python = self.create_venv()
        before = python.read_bytes()
        result = self.run_helper("$p=Initialize-MoverApplicationPython $PSScriptRoot; Write-Output $p")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(Path(result.stdout.strip()), python)
        self.assertFalse((self.root / "runtime/python").exists())
        self.assertEqual(python.read_bytes(), before)
        self.assert_protected()

    def test_offline_unavailable_base_fails_before_touching_broken_environment(self):
        python = self.create_venv(broken=True)
        before = python.read_bytes()
        result = self.run_helper("Initialize-MoverApplicationPython $PSScriptRoot")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("saved logins and rules", result.stderr.lower())
        self.assertEqual(python.read_bytes(), before)
        self.assert_protected()

    def test_actual_update_entry_recovers_its_verifier_without_a_system_python(self):
        self.create_venv(broken=True)
        self.seed_managed_base()
        for name in ("updater.py", "update-and-start.ps1"):
            shutil.copy2(ROOT / name, self.root / name)
        script = self.root / "update-and-start.ps1"
        key_name = rf"Software\InformationMoverV2-Fixture-{self.root.parent.name}"
        registration = r"$registration = 'HKCU:\Software\InformationMoverV2'"
        source = script.read_text(encoding="utf-8-sig")
        self.assertIn(registration, source)
        script.write_text(source.replace(registration, rf"$registration = 'HKCU:\{key_name}'", 1),
                          encoding="utf-8-sig")
        (self.root / "astrbot_plugin_message_bridge").mkdir()
        (self.root / "astrbot_plugin_message_bridge/metadata.yaml").write_text(
            "name: astrbot_plugin_message_bridge\nversion: v2.0.16\n", encoding="utf-8")
        (self.root / "workbench_server.py").write_text("# fixture\n", encoding="utf-8")
        (self.root / "start-workbench.ps1").write_text("throw 'NoStart must not bootstrap services'\n", encoding="utf-8-sig")
        manifest = updater.build_manifest(self.root, "2.0.16")
        (self.root / updater.MANIFEST_NAME).write_text(json.dumps(manifest), encoding="utf-8")
        with socket.socket() as port:
            port.bind(("127.0.0.1", 0))
            self.env["MOVER_WORKBENCH_PORT"] = str(port.getsockname()[1])
        # Use the fixture's registration and do not start platform services.
        import winreg
        try:
            result = subprocess.run([str(POWERSHELL), "-NoLogo", "-NoProfile", "-ExecutionPolicy", "Bypass",
                                     "-File", str(self.root / "update-and-start.ps1"), "-TargetRoot", str(self.root), "-NoStart"],
                                    cwd=Path(self.temp.name), env=self.env, capture_output=True,
                                    encoding="utf-8", errors="replace", timeout=45)
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_name) as key:
                self.assertEqual(Path(winreg.QueryValueEx(key, "InstallRoot")[0]), self.root)
        finally:
            try:
                winreg.DeleteKey(winreg.HKEY_CURRENT_USER, key_name)
            except FileNotFoundError:
                pass
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Verified local release 2.0.16", result.stdout)
        self.assertFalse((self.root / "runtime/update-in-progress.json").exists())
        self.assert_protected()

    def test_native_argument_quoting_preserves_unicode_spaces_quotes_and_trailing_backslashes(self):
        code = r"""
$p=@{Executable=$env:MOVER_TEST_EXECUTABLE; Arguments=@('-I','-B','-c','import sys,json; print(json.dumps(sys.argv[1:],ensure_ascii=True))','中文 空格','quote"inside','trailing space\','')}
$r=Invoke-MoverRuntimeCommand @p
if($r.ExitCode -ne 0){throw 'argument fixture failed'}
Write-Output $r.Output
"""
        self.env["MOVER_TEST_EXECUTABLE"] = sys.executable
        result = self.run_helper(code)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(json.loads(result.stdout), ["中文 空格", 'quote"inside', "trailing space\\", ""])

    def test_probe_timeout_does_not_block_for_a_hung_interpreter(self):
        self.env["MOVER_TEST_EXECUTABLE"] = sys.executable
        result = self.run_helper("$r=Invoke-MoverRuntimeCommand -Executable $env:MOVER_TEST_EXECUTABLE -Arguments @('-I','-B','-c','import time; time.sleep(30)') -TimeoutMilliseconds 100; $r.ExitCode")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout.strip(), "-1")

    def test_astrbot_installer_repairs_python_and_uses_module_instead_of_stale_console_exe(self):
        self.create_venv(broken=True)
        self.seed_managed_base()
        packages = self.root / "runtime/.venv/Lib/site-packages"
        (packages / "astrbot/cli").mkdir(parents=True)
        (packages / "astrbot/__init__.py").write_text("# fixture\n", encoding="utf-8")
        (packages / "astrbot/cli/__init__.py").write_text("# fixture\n", encoding="utf-8")
        (packages / "astrbot/cli/__main__.py").write_text(
            "from pathlib import Path\nimport sys\ncli = object()\nif __name__ == '__main__':\n"
            "    assert sys.argv[1:]==['init','--yes']\n"
            "    Path('.astrbot').touch()\n    Path('fixture-init-used-module').touch()\n", encoding="utf-8")
        for module in ("psutil", "aiohttp", "cryptography", "win32crypt"):
            (packages / f"{module}.py").write_text("# dependency fixture\n", encoding="utf-8")
        shutil.copy2(ROOT / "install_astrbot.ps1", self.root / "install_astrbot.ps1")
        (self.root / "install.ps1").write_text(
            "param([string]$AstrBotRoot,[switch]$Replace,[string]$ResultPath)\n"
            "Set-Content -LiteralPath (Join-Path $AstrBotRoot 'fixture-plugin-installed') -Value ok\n",
            encoding="utf-8-sig")
        self.assertFalse((self.root / "runtime/.venv/Scripts/astrbot.exe").exists())
        result = subprocess.run([str(POWERSHELL), "-NoLogo", "-NoProfile", "-ExecutionPolicy", "Bypass",
                                 "-File", str(self.root / "install_astrbot.ps1")], cwd=Path(self.temp.name),
                                env=self.env, capture_output=True, encoding="utf-8", errors="replace", timeout=45)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue((self.root / "runtime/AstrBot/fixture-init-used-module").is_file())
        self.assertTrue((self.root / "runtime/AstrBot/fixture-plugin-installed").is_file())
        # Initialized setups run only PowerShell commands and captured probes.
        # Their success must not depend on a stale native exit code in the caller.
        repaired = self.run_helper("$global:LASTEXITCODE=17; & (Join-Path $PSScriptRoot 'install_astrbot.ps1'); exit $LASTEXITCODE")
        self.assertEqual(repaired.returncode, 0, repaired.stdout + repaired.stderr)
        self.assertIn("are ready under", repaired.stdout)
        self.assert_protected()


if __name__ == "__main__":
    unittest.main()
