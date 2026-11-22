"""Exercise the delivered update script under real Windows PowerShell 5.1."""
from contextlib import contextmanager
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from urllib.request import build_opener, ProxyHandler, Request
import venv
import zipfile

import psutil

import background_runtime
import updater

ROOT = Path(__file__).resolve().parents[1]
CSC = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "Microsoft.NET/Framework64/v4.0.30319/csc.exe"
START_STUB = '''param([switch]$NoBrowser,[switch]$SkipDesktopActions,[switch]$PrepareOnly,[switch]$ResumeBackground)
$ErrorActionPreference = 'Stop'
@{started=$true; resumed=[bool]$ResumeBackground; no_browser=[bool]$NoBrowser; no_desktop=[bool]$SkipDesktopActions} |
    ConvertTo-Json | Set-Content -LiteralPath (Join-Path $PSScriptRoot 'runtime/fixture-started.json') -Encoding UTF8
exit 0
'''
LEGACY_STOP = '''param([switch]$ForUpdate)
$ErrorActionPreference = 'Stop'
Set-Content -LiteralPath (Join-Path $PSScriptRoot 'runtime/legacy-stop-called.txt') -Value 'called'
py -3 -c 'import pathlib,background_runtime as b; root=pathlib.Path.cwd(); assert b.paused(root)=="update", "An active update marker is required"; b.request_stop_supervisor(root)'
exit $LASTEXITCODE
'''
SMOKE_WORKBENCH = '''import json, os, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

root = Path(__file__).resolve().parent

def astrbot_root():
    return root / "runtime/AstrBot"

def prepare_local_setup():
    return {"astrbot_root": str(astrbot_root()), "requires_restart": False}

class Handler(BaseHTTPRequestHandler):
    def answer(self, data):
        payload = json.dumps(data).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        self.answer({"workbench_root": str(root), "workbench_pid": os.getpid(),
                     "version": "2.0.9", "astrbot": {"running": False}})

    def do_POST(self):
        if self.path != "/api/workbench/stop":
            self.send_error(400)
            return
        self.answer({"stopping": True})
        threading.Thread(target=self.server.shutdown, daemon=True).start()

if __name__ == "__main__":
    with ThreadingHTTPServer(("127.0.0.1", int(os.environ["MOVER_WORKBENCH_PORT"])), Handler) as server:
        server.serve_forever()
'''


@unittest.skipUnless(os.name == "nt", "Windows PowerShell update entry")
class WindowsUpdateLaunchTests(unittest.TestCase):
    def setUp(self):
        import winreg
        self.winreg = winreg
        self.key_name = r"Software\InformationMoverV2"
        self.previous = None
        self.key_existed = False
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, self.key_name) as key:
                self.key_existed = True
                try:
                    self.previous = winreg.QueryValueEx(key, "InstallRoot")
                except FileNotFoundError:
                    pass
        except FileNotFoundError:
            pass
        self.temp = tempfile.TemporaryDirectory(prefix="mover-update-launch-")
        self.root = Path(self.temp.name)
        self.source = self.root / "new release 新版"
        self.target = self.root / "existing 已登录"
        for folder in (self.source, self.target):
            (folder / "astrbot_plugin_message_bridge").mkdir(parents=True)
            (folder / "astrbot_plugin_message_bridge/metadata.yaml").write_text("name: astrbot_plugin_message_bridge\nversion: v2.0.8\n", encoding="utf-8")
            (folder / "workbench_server.py").write_text("VERSION='old'\n", encoding="utf-8")
            (folder / "start-workbench.ps1").write_text(START_STUB, encoding="utf-8-sig")
        (self.target / "stop-workbench.ps1").write_text(LEGACY_STOP, encoding="utf-8-sig")
        for name in ("updater.py", "update-and-start.ps1", "background_runtime.py", "background_supervisor.py", "stop-workbench.ps1"):
            shutil.copy2(ROOT / name, self.source / name)
        (self.source / "workbench_server.py").write_text("VERSION='new'\n", encoding="utf-8")
        self.protected = {}
        for name in ("runtime/LarkAgentX-data/credentials.json", "runtime/AstrBot/data/plugin_data/astrbot_plugin_message_bridge/bridge_rules.json", "runtime/AstrBot/data/plugin_data/pulids_snowluma_go_to_astrbot/snowluma/account/session.db", "workbench.settings.json"):
            path = self.target / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"\x00\xffprivate-login-and-rules\x00")
            self.protected[path] = hashlib.sha256(path.read_bytes()).hexdigest()
        self.environment = os.environ.copy()
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            self.environment["MOVER_WORKBENCH_PORT"] = str(listener.getsockname()[1])
        self.environment["PYTHONUTF8"] = "1"
        self.port = int(self.environment["MOVER_WORKBENCH_PORT"])
        self.run_number = 0
        self.seal()

    def tearDown(self):
        winreg = self.winreg
        try:
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER, self.key_name) as key:
                if self.previous:
                    value, kind = self.previous
                    winreg.SetValueEx(key, "InstallRoot", 0, kind, value)
                else:
                    try:
                        winreg.DeleteValue(key, "InstallRoot")
                    except FileNotFoundError:
                        pass
            if not self.key_existed:
                try:
                    winreg.DeleteKey(winreg.HKEY_CURRENT_USER, self.key_name)
                except OSError:
                    pass
        finally:
            self.temp.cleanup()

    def seal(self):
        manifest = updater.build_manifest(self.source, "2.0.9")
        (self.source / updater.MANIFEST_NAME).write_text(json.dumps(manifest), encoding="utf-8")

    def run_update(self, *arguments):
        return self.run_powershell(self.source / "update-and-start.ps1", *arguments)

    def run_powershell(self, script, *arguments):
        self.run_number += 1
        out = self.root / f"powershell-{self.run_number}.stdout.txt"
        err = self.root / f"powershell-{self.run_number}.stderr.txt"
        # A detached server can inherit the PowerShell output handles on Windows.
        # Wait for the script itself without waiting for a server-held pipe to close.
        with out.open("wb") as stdout, err.open("wb") as stderr:
            result = subprocess.run(["powershell.exe", "-NoLogo", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script), *arguments],
                                    cwd=self.root, env=self.environment, stdout=stdout, stderr=stderr, timeout=40)
        result.stdout = out.read_text(encoding="utf-8", errors="replace")
        result.stderr = err.read_text(encoding="utf-8", errors="replace")
        return result

    def assert_protected(self):
        self.assertEqual(self.protected, {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in self.protected})

    @contextmanager
    def running_workbench(self, installation, version="2.0.8", astrbot_running=False, managed=True):
        posts = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def reply(self, result):
                payload = json.dumps(result).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def do_GET(self):
                if self.path != "/api/status":
                    self.send_error(404)
                    return
                self.reply({"workbench_root": str(installation), "version": version,
                            "astrbot": {"running": astrbot_running, "managed_here": astrbot_running and managed}})

            def do_POST(self):
                posts.append((self.path, json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
                self.reply({"stopping": True})
                if self.path == "/api/workbench/stop":
                    threading.Thread(target=shutdown, daemon=True).start()

        fixture = ThreadingHTTPServer(("127.0.0.1", self.port), Handler)

        def shutdown():
            fixture.shutdown()
            fixture.server_close()

        server = threading.Thread(target=fixture.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        server.start()
        try:
            yield posts
        finally:
            shutdown()
            server.join(timeout=5)

    def wait_for(self, callback, message, timeout=8):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = callback()
            if value:
                return value
            time.sleep(0.1)
        self.fail(message)

    @contextmanager
    def running_supervisor(self):
        for name in ("background_runtime.py", "background_supervisor.py"):
            shutil.copy2(ROOT / name, self.target / name)
        background_runtime.set_desired(self.target, keep_awake=False, autostart=False, port=self.port)
        config = self.target / "runtime/background.settings.json"
        self.protected[config] = hashlib.sha256(config.read_bytes()).hexdigest()
        process = subprocess.Popen([sys.executable, "-B", str(self.target / "background_supervisor.py"), "--interval", "0.2"],
                                   cwd=self.target, creationflags=subprocess.CREATE_NO_WINDOW)
        try:
            old_pid = self.wait_for(lambda: background_runtime.status(self.target)["pid"], "Initial watchdog did not start")
            yield process, old_pid
        finally:
            try:
                background_runtime.request_stop_supervisor(self.target)
            finally:
                if process.poll() is None:
                    process.terminate()
                process.wait(timeout=5)

    def test_cross_directory_updates_running_208_without_using_broken_legacy_stop(self):
        # A stale registry entry pointing at the release must not override the live installation.
        with self.winreg.CreateKey(self.winreg.HKEY_CURRENT_USER, self.key_name) as key:
            self.winreg.SetValueEx(key, "InstallRoot", 0, self.winreg.REG_SZ, str(self.source))
        with self.running_workbench(self.target, astrbot_running=True) as posts, self.running_supervisor() as (watchdog, _):
            process = self.run_update("-NoBrowser", "-SkipDesktopActions")
            self.assertEqual(process.returncode, 0, process.stdout + process.stderr)
            self.assertEqual(watchdog.wait(timeout=5), 0)
            self.assertEqual(posts, [("/api/astrbot/stop", {"preserve_background": True}),
                                     ("/api/workbench/stop", {"preserve_background": True})])
        self.assertEqual((self.target / "workbench_server.py").read_text(), "VERSION='new'\n")
        self.assertFalse((self.target / "runtime/legacy-stop-called.txt").exists())
        flags = json.loads((self.target / "runtime/fixture-started.json").read_text(encoding="utf-8-sig"))
        self.assertEqual(flags, {"started": True, "resumed": False, "no_browser": True, "no_desktop": True})
        self.assertFalse((self.target / "runtime/update-in-progress.json").exists())
        self.assert_protected()

    def test_unregistered_v205_install_is_found_from_its_single_default_port(self):
        with self.winreg.CreateKey(self.winreg.HKEY_CURRENT_USER, self.key_name) as key:
            try:
                self.winreg.DeleteValue(key, "InstallRoot")
            except FileNotFoundError:
                pass
        with self.running_workbench(self.target, "2.0.5"):
            process = self.run_update("-NoStart")
        self.assertEqual(process.returncode, 0, process.stdout + process.stderr)
        self.assertEqual((self.target / "workbench_server.py").read_text(), "VERSION='new'\n")
        self.assertEqual(self.protected, {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in self.protected})
        with self.winreg.OpenKey(self.winreg.HKEY_CURRENT_USER, self.key_name) as key:
            self.assertEqual(Path(self.winreg.QueryValueEx(key, "InstallRoot")[0]), self.target)

    def test_zip_update_nostart_applies_and_preserves_all_login_bytes(self):
        package = self.root / "release.zip"
        with zipfile.ZipFile(package, "w") as archive:
            for path in self.source.rglob("*"):
                if path.is_file():
                    archive.write(path, path.relative_to(self.source).as_posix())
        process = self.run_update("-TargetRoot", str(self.target), "-PackagePath", str(package), "-NoStart")
        self.assertEqual(process.returncode, 0, process.stdout + process.stderr)
        self.assertFalse((self.target / "runtime/fixture-started.json").exists())
        self.assertEqual((self.target / "workbench_server.py").read_text(), "VERSION='new'\n")
        self.assert_protected()

    def test_registered_install_is_selected_automatically_when_release_is_unpacked_elsewhere(self):
        with self.winreg.CreateKey(self.winreg.HKEY_CURRENT_USER, self.key_name) as key:
            self.winreg.SetValueEx(key, "InstallRoot", 0, self.winreg.REG_SZ, str(self.target))
        process = self.run_update("-NoBrowser", "-SkipDesktopActions", "-NoStart")
        self.assertEqual(process.returncode, 0, process.stdout + process.stderr)
        self.assertEqual((self.target / "workbench_server.py").read_text(), "VERSION='new'\n")
        self.assertEqual((self.source / "workbench_server.py").read_text(), "VERSION='new'\n")
        self.assert_protected()

    def test_development_same_directory_without_manifest_starts_locally(self):
        (self.source / updater.MANIFEST_NAME).unlink()
        process = self.run_update("-TargetRoot", str(self.source), "-NoBrowser", "-SkipDesktopActions")
        self.assertEqual(process.returncode, 0, process.stdout + process.stderr)
        flags = json.loads((self.source / "runtime/fixture-started.json").read_text(encoding="utf-8-sig"))
        self.assertFalse(flags["resumed"])
        self.assertTrue(flags["started"])
        self.assert_protected()

    def test_same_directory_overwrite_restarts_workbench_still_running_208(self):
        with self.running_workbench(self.source) as posts:
            process = self.run_update("-NoBrowser", "-SkipDesktopActions")
        self.assertEqual(process.returncode, 0, process.stdout + process.stderr)
        self.assertEqual(posts, [("/api/workbench/stop", {"preserve_background": True})])
        self.assertTrue((self.source / "runtime/fixture-started.json").exists())
        self.assertFalse((self.source / "runtime/update-in-progress.json").exists())
        self.assert_protected()

    def test_same_directory_login_resume_flag_reaches_background_start(self):
        (self.source / updater.MANIFEST_NAME).unlink()
        with self.winreg.CreateKey(self.winreg.HKEY_CURRENT_USER, self.key_name) as key:
            self.winreg.SetValueEx(key, "InstallRoot", 0, self.winreg.REG_SZ, str(self.target))
        process = self.run_update("-NoBrowser", "-SkipDesktopActions", "-ResumeBackground")
        self.assertEqual(process.returncode, 0, process.stdout + process.stderr)
        flags = json.loads((self.source / "runtime/fixture-started.json").read_text(encoding="utf-8-sig"))
        self.assertTrue(flags["resumed"])
        self.assertTrue(flags["no_browser"])
        self.assertTrue(flags["no_desktop"])
        self.assertFalse((self.target / "runtime/fixture-started.json").exists())
        self.assert_protected()

    def test_setup_uses_free_workbench_port_without_touching_other_install(self):
        self.environment.pop("MOVER_WORKBENCH_PORT")
        script = self.source / "update-and-start.ps1"
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            alternate = listener.getsockname()[1]
        script.write_text(script.read_text(encoding="utf-8-sig").replace("18766", str(self.port)).replace(
            "18767..18786", str(alternate)), encoding="utf-8-sig")
        stub = self.source / "start-workbench.ps1"
        stub.write_text(START_STUB.replace("started=$true;", "port=$env:MOVER_WORKBENCH_PORT;started=$true;"),
                        encoding="utf-8-sig")
        self.seal()
        with self.winreg.CreateKey(self.winreg.HKEY_CURRENT_USER, self.key_name) as key:
            self.winreg.SetValueEx(key, "InstallRoot", 0, self.winreg.REG_SZ, str(self.target))
        with self.running_workbench(self.target) as posts:
            process = self.run_update("-StartHere", "-NoBrowser", "-SkipDesktopActions")
        self.assertEqual(process.returncode, 0, process.stdout + process.stderr)
        self.assertEqual(posts, [])
        flags = json.loads((self.source / "runtime/fixture-started.json").read_text(encoding="utf-8-sig"))
        self.assertEqual(flags["port"], str(alternate))
        self.assertFalse((self.target / "runtime/fixture-started.json").exists())
        self.assert_protected()

    def test_setup_preserves_saved_workbench_port_and_rejects_foreign_occupancy(self):
        self.environment.pop("MOVER_WORKBENCH_PORT")
        background_runtime.set_desired(self.source, port=self.port, autostart=False, keep_awake=False)
        config = self.source / "runtime/background.settings.json"
        before = config.read_bytes()
        with self.running_workbench(self.target) as posts:
            process = self.run_update("-StartHere", "-NoBrowser", "-SkipDesktopActions")
        self.assertEqual(process.returncode, 20, process.stdout + process.stderr)
        self.assertEqual(posts, [])
        self.assertEqual(config.read_bytes(), before)
        self.assertFalse((self.source / "runtime/fixture-started.json").exists())

    def test_invalid_distribution_fails_before_program_or_login_changes(self):
        (self.source / "workbench_server.py").write_text("TAMPERED\n")
        process = self.run_update("-TargetRoot", str(self.target), "-NoStart")
        self.assertNotEqual(process.returncode, 0)
        self.assertEqual((self.target / "workbench_server.py").read_text(), "VERSION='old'\n")
        self.assertFalse((self.target / "runtime/update-backups").exists())
        self.assert_protected()

    def test_failed_apply_resumes_previous_start_and_watchdog(self):
        (self.source / "log_store.py").write_text("# an update file\n", encoding="utf-8")
        (self.target / "log_store.py").mkdir()
        self.seal()
        with self.running_workbench(self.target), self.running_supervisor() as (watchdog, old_pid):
            process = self.run_update("-TargetRoot", str(self.target), "-NoBrowser", "-SkipDesktopActions")
            self.assertNotEqual(process.returncode, 0, process.stdout + process.stderr)
            self.assertIn("Original program files were rolled back", process.stdout + process.stderr)
            self.assertEqual(watchdog.wait(timeout=5), 0)

            def restarted():
                pid = background_runtime.status(self.target)["pid"]
                return pid and pid != old_pid

            self.wait_for(restarted, "Watchdog was not restored after the failed update")
            flags = json.loads((self.target / "runtime/fixture-started.json").read_text(encoding="utf-8-sig"))
            self.assertEqual(flags, {"started": True, "resumed": True, "no_browser": True, "no_desktop": True})
        self.assertEqual((self.target / "workbench_server.py").read_text(), "VERSION='old'\n")
        self.assertFalse((self.target / "runtime/update-in-progress.json").exists())
        self.assert_protected()

    @unittest.skipUnless(CSC.is_file(), "Windows C# compiler is unavailable")
    def test_manual_launcher_exits_with_workbench_open_and_background_recovery_disabled(self):
        # Real starter and Python, with a stand-in HTTP workbench and no platform logins.
        shutil.copy2(ROOT / "start-workbench.ps1", self.source / "start-workbench.ps1")
        (self.source / "workbench_server.py").write_text(SMOKE_WORKBENCH, encoding="utf-8")
        for name in ("install_astrbot.ps1", "install_larkagentx.ps1", "install_snowluma.ps1"):
            (self.source / name).write_text('if ($env:MOVER_INSTALL_ROOT -ne $PSScriptRoot) { throw "Incorrect install root" }\nexit 0\n', encoding="utf-8-sig")
        background_runtime.set_desired(self.source, enabled=False, astrbot_desired=False, keep_awake=False, port=self.port)
        config = self.source / "runtime/background.settings.json"
        expected = config.read_bytes()
        venv.EnvBuilder(with_pip=False).create(self.source / "runtime/.venv")
        (self.source / "tools").mkdir()
        (self.source / "tools/uv.exe").write_bytes(b"only the venv is used in this fixture")
        launcher = self.source / "mover-v2-launcher.exe"
        compiled = subprocess.run([str(CSC), "/nologo", "/target:winexe", "/r:System.Windows.Forms.dll",
                                   "/out:" + str(launcher), str(ROOT / "portable_launcher.cs")],
                                  capture_output=True, text=True, timeout=15)
        self.assertEqual(compiled.returncode, 0, compiled.stdout + compiled.stderr)
        self.seal()
        entry = ("-TargetRoot", str(self.source), "-NoBrowser", "-SkipDesktopActions")
        resume = self.run_update(*entry, "-ResumeBackground")
        self.assertEqual(resume.returncode, 0, resume.stdout + resume.stderr)
        self.assertIn("automatic startup was skipped", resume.stdout)
        url = f"http://127.0.0.1:{self.port}"
        opener = build_opener(ProxyHandler({}))
        try:
            process = subprocess.run([str(launcher), "--background", "--start-here"], cwd=self.root, env=self.environment,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
            logs = list((self.source / "runtime/logs").glob("launcher-*.log"))
            details = "\n".join(log.read_text(encoding="utf-8") for log in logs)
            self.assertEqual(process.returncode, 0, details)
            with opener.open(url + "/api/status", timeout=2) as response:
                status = json.load(response)
            self.assertEqual(Path(status["workbench_root"]), self.source)
            self.assertEqual(status["version"], "2.0.9")
            self.assertEqual(config.read_bytes(), expected)
            self.assertFalse(background_runtime.status(self.source)["running"])
        finally:
            try:
                with opener.open(url + "/api/status", timeout=1) as response:
                    live = json.load(response)
                if Path(live["workbench_root"]) == self.source:
                    server = psutil.Process(live["workbench_pid"])
                    parent = server.parent()
                    servers = [server]
                    if parent and str(self.source / "workbench_server.py") in parent.cmdline():
                        servers.append(parent)  # Windows venv launcher also holds the log handles.
                    opener.open(Request(url + "/api/workbench/stop", data=b"{}"), timeout=2).close()
                    _, remaining = psutil.wait_procs(servers, timeout=5)
                    self.assertFalse(remaining, "Fixture workbench did not exit")
            except (OSError, psutil.NoSuchProcess):
                pass


if __name__ == "__main__":
    unittest.main()
