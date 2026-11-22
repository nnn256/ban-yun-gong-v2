import os
import ctypes
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import unittest
import time


ROOT = Path(__file__).resolve().parents[1]
CSC = Path(r"C:/Windows/Microsoft.NET/Framework64/v4.0.30319/csc.exe")


class PortableLauncherRuntimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not CSC.is_file():
            raise unittest.SkipTest("Windows C# compiler is unavailable")
        cls.build_temp = tempfile.TemporaryDirectory(prefix="mover-launcher-build-")
        cls.build_root = Path(cls.build_temp.name)
        shutil.copy2(ROOT / "portable_launcher.cs", cls.build_root / "portable_launcher.cs")
        cls.compiled = cls.build_root / "mover-v2-launcher.exe"
        result = subprocess.run([
            str(CSC), "/nologo", "/target:winexe", "/out:" + str(cls.compiled),
            "/r:System.Windows.Forms.dll", str(cls.build_root / "portable_launcher.cs"),
        ], capture_output=True, text=True, encoding="utf-8", errors="replace")
        if result.returncode:
            raise AssertionError(result.stdout + result.stderr)

    @classmethod
    def tearDownClass(cls):
        cls.build_temp.cleanup()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="mover-launcher-runtime-")
        self.root = Path(self.temp.name)
        (self.root / "tools").mkdir()
        (self.root / "tools" / "uv.exe").write_bytes(b"fixture")
        (self.root / "astrbot_plugin_message_bridge").mkdir()
        (self.root / "astrbot_plugin_message_bridge" / "metadata.yaml").write_text("name: astrbot_plugin_message_bridge\n", encoding="utf-8")
        for name in ("updater.py", "workbench_server.py", "python-runtime.ps1"):
            (self.root / name).write_text("# local dependency fixture\n", encoding="utf-8")
        (self.root / "start-workbench.ps1").write_text("exit 0", encoding="utf-8-sig")
        self.compiler = self.root / "mover-v2-launcher.exe"
        shutil.copy2(self.compiled, self.compiler)

    def tearDown(self):
        self.temp.cleanup()

    def make_stub(self, body, name="update-and-start.ps1"):
        script = self.root / name
        script.write_text(body, encoding="utf-8-sig")
        return script

    def run_launcher(self, root=None, args=()):
        return subprocess.run([str(self.compiler), *args], cwd=str(root or self.root),
                              capture_output=True, text=True, timeout=30)

    def flag_probe(self):
        self.make_stub('''param([switch]$NoBrowser,[switch]$SkipDesktopActions,[switch]$ResumeBackground,[switch]$StartHere)
Add-Type -TypeDefinition 'using System; using System.Runtime.InteropServices; public static class ConsoleProbe { [DllImport("kernel32.dll")] public static extern IntPtr GetConsoleWindow(); [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr hWnd); }'
$window=[ConsoleProbe]::GetConsoleWindow()
@{no_browser=[bool]$NoBrowser;no_desktop=[bool]$SkipDesktopActions;resume=[bool]$ResumeBackground;start_here=[bool]$StartHere;console_visible=($window -ne [IntPtr]::Zero -and [ConsoleProbe]::IsWindowVisible($window))} | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $PSScriptRoot 'flags.json') -Encoding UTF8
''')

    def read_flags(self):
        marker = self.root / "flags.json"
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            try:
                return json.loads(marker.read_text(encoding="utf-8-sig"))
            except (FileNotFoundError, PermissionError, json.JSONDecodeError):
                time.sleep(.05)
        self.fail("The detached launcher did not finish its flag probe")

    def test_default_and_login_resume_are_silent_and_do_not_open_desktop_apps(self):
        self.flag_probe()
        for args, resume in (((), False), (("--background",), False), (("--resume-background",), True)):
            with self.subTest(args=args):
                self.assertEqual(self.run_launcher(args=args).returncode, 0)
                self.assertEqual(self.read_flags(), {"no_browser": True, "no_desktop": True,
                                                     "resume": resume, "start_here": False, "console_visible": False})

    def test_open_workbench_explicitly_allows_browser_and_login_steps_without_console(self):
        self.flag_probe()
        self.assertEqual(self.run_launcher(args=("--open", "--start-here")).returncode, 0)
        self.assertEqual(self.read_flags(), {"no_browser": False, "no_desktop": False,
                                             "resume": False, "start_here": True, "console_visible": False})

    def test_background_failure_exits_and_logs_without_blocking_on_a_dialog(self):
        self.make_stub("Write-Output 'expected-fixture-failure'; exit 7")
        result = self.run_launcher(args=("--background",))
        self.assertEqual(result.returncode, 7)
        logs = list((self.root / "runtime/logs").glob("launcher-*.log"))
        self.assertTrue(any("expected-fixture-failure" in p.read_text(encoding="utf-8") for p in logs))

    def test_vbs_and_cmd_handoff_are_detached_and_keep_the_hidden_flags(self):
        self.flag_probe()
        shutil.copy2(self.compiled, self.root / "mover-v2-launcher.exe")
        for name, host, expected_open in (("后台运行.vbs", "wscript.exe", False),
                                           ("打开工作台.vbs", "wscript.exe", True),
                                           ("一键启动.cmd", "cmd.exe", False)):
            with self.subTest(entry=name):
                (self.root / "flags.json").unlink(missing_ok=True)
                target = self.root / name
                shutil.copy2(ROOT / name, target)
                command = ([host, "//B", "//Nologo", str(target)] if host == "wscript.exe"
                           else [host, "/d", "/c", str(target)])
                result = subprocess.run(command, cwd=self.root, capture_output=True, timeout=20,
                                        creationflags=subprocess.CREATE_NO_WINDOW)
                self.assertEqual(result.returncode, 0)
                self.assertEqual(self.read_flags(), {"no_browser": not expected_open, "no_desktop": not expected_open,
                                                     "resume": False, "start_here": False, "console_visible": False})
                # flags.json is written just before process exit and mutex release.
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    try:
                        logs = list((self.root / "runtime/logs").glob("launcher-*.log"))
                        if len(logs) >= 1:
                            time.sleep(.15)
                            break
                    except OSError:
                        pass

    def test_check_has_no_side_effect_and_accepts_complete_payload(self):
        self.make_stub("Set-Content -LiteralPath (Join-Path $PSScriptRoot 'unexpected-bootstrap.txt') -Value 'ran'; exit 7")
        result = self.run_launcher(args=("--check",))
        self.assertEqual(result.returncode, 0)
        self.assertFalse((self.root / "unexpected-bootstrap.txt").exists())
        self.assertEqual(list((self.root / "runtime").glob("**/*")) if (self.root / "runtime").exists() else [], [])

    def test_check_rejects_missing_uv_without_creating_logs(self):
        self.make_stub("exit 0")
        (self.root / "tools" / "uv.exe").unlink()
        result = self.run_launcher(args=("--check",))
        self.assertEqual(result.returncode, 2)
        self.assertFalse((self.root / "runtime").exists())

    def test_check_rejects_missing_update_dependencies_without_creating_logs(self):
        self.make_stub("exit 0")
        for name in ("update-and-start.ps1", "updater.py", "start-workbench.ps1",
                     "python-runtime.ps1", "workbench_server.py", "astrbot_plugin_message_bridge/metadata.yaml"):
            with self.subTest(dependency=name):
                path = self.root / name
                previous = path.read_bytes()
                path.unlink()
                try:
                    self.assertEqual(self.run_launcher(args=("--check",)).returncode, 2)
                    self.assertFalse((self.root / "runtime").exists())
                finally:
                    path.write_bytes(previous)

    def test_normal_launch_uses_update_entry_and_does_not_bypass_it_for_start_script(self):
        self.make_stub("Set-Content -LiteralPath (Join-Path $PSScriptRoot 'update-entry.txt') -Value 'verified-update'")
        self.make_stub("Set-Content -LiteralPath (Join-Path $PSScriptRoot 'legacy-entry.txt') -Value 'bypassed-update'", name="start-workbench.ps1")
        result = self.run_launcher()
        self.assertEqual(result.returncode, 0)
        self.assertTrue((self.root / "update-entry.txt").is_file())
        self.assertFalse((self.root / "legacy-entry.txt").exists())
        for name in ("一键启动.cmd", "launch-workbench.cmd", "更新并启动.cmd"):
            self.assertIn("update-and-start.ps1", (ROOT / name).read_text(encoding="utf-8"))

    def test_same_install_repeated_launches_use_unique_logs_and_do_not_share_launcher_log(self):
        self.make_stub("Start-Sleep -Milliseconds 120; Set-Content -LiteralPath (Join-Path $PSScriptRoot 'runtime/stub-ran.txt') -Value 'ok' -Encoding utf8")
        first = self.run_launcher()
        second = self.run_launcher()
        self.assertEqual(first.returncode, 0)
        self.assertEqual(second.returncode, 0)
        logs = list((self.root / "runtime" / "logs").glob("launcher-*.log"))
        self.assertGreaterEqual(len(logs), 2)
        self.assertFalse((self.root / "runtime" / "logs" / "launcher.log").exists())
        self.assertTrue((self.root / "runtime" / "stub-ran.txt").is_file())

    def test_concurrent_same_install_bootstrap_has_one_script_execution_and_no_log_collision(self):
        marker = self.root / "runtime" / "stub-count.txt"
        self.make_stub("$p=Join-Path $PSScriptRoot 'runtime/stub-count.txt'; $n=0; if(Test-Path $p){$n=[int](Get-Content $p)}; Set-Content $p ($n+1); Start-Sleep -Milliseconds 400")
        results, failures = [], []
        def launch():
            try:
                results.append(self.run_launcher())
            except BaseException as exc:
                failures.append(exc)
        threads = [threading.Thread(target=launch) for _ in range(2)]
        for thread in threads: thread.start()
        for thread in threads: thread.join()
        self.assertEqual(failures, [])
        self.assertEqual([result.returncode for result in results], [0, 0])
        self.assertEqual(int(marker.read_text().strip()), 1)
        self.assertGreaterEqual(len(list((self.root / "runtime" / "logs").glob("launcher-*.log"))), 2)

    def test_distinct_install_roots_can_bootstrap_concurrently(self):
        self.make_stub("Start-Sleep -Milliseconds 250; Set-Content -LiteralPath (Join-Path $PSScriptRoot 'runtime/stub-ran.txt') -Value 'ok'")
        other = self.root / "second-install"
        (other / "tools").mkdir(parents=True)
        shutil.copy2(self.compiled, other / "mover-v2-launcher.exe")
        for name in ("update-and-start.ps1", "updater.py", "start-workbench.ps1",
                     "workbench_server.py", "astrbot_plugin_message_bridge/metadata.yaml"):
            target = other / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(self.root / name, target)
        (other / "tools" / "uv.exe").write_bytes(b"fixture")
        (other / "python-runtime.ps1").write_text("# fixture\n", encoding="utf-8-sig")
        first = subprocess.Popen([str(self.compiler)], cwd=self.root)
        second = subprocess.Popen([str(other / "mover-v2-launcher.exe")], cwd=other)
        self.assertEqual(first.wait(timeout=30), 0)
        self.assertEqual(second.wait(timeout=30), 0)
        self.assertTrue((self.root / "runtime" / "stub-ran.txt").is_file())
        self.assertTrue((other / "runtime" / "stub-ran.txt").is_file())

    def test_legacy_exclusive_launcher_log_does_not_block_new_run(self):
        self.make_stub("Set-Content -LiteralPath (Join-Path $PSScriptRoot 'runtime/stub-ran.txt') -Value 'ok'")
        logs = self.root / "runtime" / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        legacy = logs / "launcher.log"
        legacy.touch()
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateFileW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p]
        kernel.CreateFileW.restype = ctypes.c_void_p
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        held = kernel.CreateFileW(str(legacy), 0x40000000, 0, None, 3, 0, None)
        self.assertNotEqual(held, ctypes.c_void_p(-1).value)
        try:
            result = self.run_launcher()
        finally:
            kernel.CloseHandle(held)
        self.assertEqual(result.returncode, 0)
        self.assertTrue((self.root / "runtime" / "stub-ran.txt").is_file())


if __name__ == "__main__":
    unittest.main()
