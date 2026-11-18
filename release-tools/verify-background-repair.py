"""Run the shipping EXE against an isolated Windows installation with a 404 save route."""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.request import build_opener, ProxyHandler, Request
import venv
import winreg
import zipfile

import psutil

ROOT = Path(__file__).resolve().parent.parent
RELEASE = ROOT / "artifacts/release-v2.0.9"
ARCHIVE = RELEASE / "搬运工V2-2.0.9.zip"
REPAIR = RELEASE / "搬运工V2-修复后台保存.exe"
LARK_DIR = "runtime/LarkAgentX-runtime/LarkAgentX-d288ef4df8a42f6286c5506b7185f64430071490/larkx"
OPENER = build_opener(ProxyHandler({}))
LEGACY = '''import json, os, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

root = Path(__file__).resolve().parent

class Handler(BaseHTTPRequestHandler):
    def reply(self, status, value):
        body = json.dumps(value).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        background = json.loads((root / "runtime/background.settings.json").read_text(encoding="utf-8"))
        self.reply(200, {"workbench_root": str(root), "workbench_pid": os.getpid(),
                        "version": os.environ["FIXTURE_WORKBENCH_VERSION"],
                        "background": background, "astrbot": {"running": False}})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if self.path == "/api/workbench/stop":
            if body.get("preserve_background") is True:
                self.reply(200, {"stopping": True})
                threading.Thread(target=self.server.shutdown, daemon=True).start()
                return
        self.reply(404, {"error": "Not found"})

with ThreadingHTTPServer(("127.0.0.1", int(os.environ["MOVER_WORKBENCH_PORT"])), Handler) as server:
    server.serve_forever()
'''


@contextmanager
def preserve_install_registration():
    key_path = r"Software\InformationMoverV2"
    present = True
    existing = None
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as key:
            try:
                existing = winreg.QueryValueEx(key, "InstallRoot")
            except FileNotFoundError:
                pass
    except FileNotFoundError:
        present = False
    try:
        yield
    finally:
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, key_path) as key:
            if existing:
                winreg.SetValueEx(key, "InstallRoot", 0, existing[1], existing[0])
            else:
                try:
                    winreg.DeleteValue(key, "InstallRoot")
                except FileNotFoundError:
                    pass
        if not present:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, key_path)


def unused_port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def ask(port, path, values=None):
    body = json.dumps(values).encode() if values is not None else None
    request = Request(f"http://127.0.0.1:{port}{path}", body, {"Content-Type": "application/json"})
    with OPENER.open(request, timeout=4) as response:
        return response.status, json.load(response)


def write(root, file, data):
    target = root / file
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    return target


def seed(target, port):
    with zipfile.ZipFile(ARCHIVE) as release:
        for name in ("start-workbench.ps1", "astrbot_plugin_message_bridge/metadata.yaml", "workbench/app.js"):
            write(target, name, release.read(name))
    write(target, "workbench_server.py", LEGACY.encode())
    pkg = target / "runtime/.venv"
    venv.EnvBuilder(with_pip=False).create(pkg)
    write(pkg, "Lib/site-packages/fixture-dependencies.pth",
          f"import site; site.addsitedir({ascii(str(ROOT / 'runtime/.venv/Lib/site-packages'))})\n".encode())
    # Startup checks the command exists; the saved state below keeps AstrBot stopped.
    write(pkg, "Scripts/astrbot.exe", b"fixture (not run)")
    shutil.copytree(ROOT / LARK_DIR, target / LARK_DIR, ignore=shutil.ignore_patterns("__pycache__"))
    bot = target / "runtime/AstrBot"
    write(bot, ".astrbot", b"")
    (bot / "data/plugins").mkdir(parents=True)
    workbench_config = {"astrbot_root": str(bot), "astrbot_port": unused_port()}
    write(target, "workbench.settings.json", json.dumps(workbench_config).encode())
    snow = bot / "data/plugin_data/pulids_snowluma_go_to_astrbot/snowluma"
    for name in ("node.exe", "index.mjs", "launcher.bat", "native/snowluma-win32-x64.dll",
                 "native/snowluma-win32-x64.node", "native/websocket-win32-x64.node", "native/ffmpeg/ffmpegAddon.win32.x64.node"):
        write(snow, name, b"isolated fixture file; not executed")
    write(snow, "release.json", b'{"version":"2.4.2","target":"win32-x64"}')

    prefs = {"enabled": False, "astrbot_desired": False, "keep_awake": False, "autostart": False, "port": port}
    local_files = {
        write(target, "runtime/background.settings.json", json.dumps(prefs, indent=2).replace("\n", "\r\n").encode()),
        write(target, "runtime/LarkAgentX-data/credentials.json", b'{"test":"not logged in"}\n'),
        write(bot, "data/plugin_data/astrbot_plugin_message_bridge/bridge_rules.json", b'{"rules":[],"bridge_enabled":true}\n'),
        write(snow, "account/fixture.db", b"fake QQ session\x00\xff"),
        target / "workbench.settings.json",
    }
    return prefs, {f: f.read_bytes() for f in local_files}


def run_repair(environment, expected):
    log_dir = Path(os.environ["LOCALAPPDATA"]) / "InformationMoverV2/RepairLogs"
    previous = set(log_dir.glob("background-*.log"))
    result = subprocess.run([str(REPAIR), "--headless"], env=environment, timeout=100)
    logs = set(log_dir.glob("background-*.log")) - previous
    details = "\n".join(p.read_text(encoding="utf-8") for p in sorted(logs))
    assert result.returncode == expected, (result.returncode, details)
    assert details, "Missing the repair log"
    return details


def stop_fixture(port, target):
    try:
        _, live = ask(port, "/api/status")
    except OSError:
        return
    if Path(live["workbench_root"]).resolve() != target.resolve():
        return
    worker = psutil.Process(live["workbench_pid"])
    parent = worker.parent()
    processes = [worker]
    if parent and str(target / "workbench_server.py") in parent.cmdline():
        processes.append(parent)
    ask(port, "/api/workbench/stop", {"preserve_background": True})
    _, remaining = psutil.wait_procs(processes, timeout=7)
    assert not remaining, "Fixture server did not exit"


@contextmanager
def legacy_workbench(target, environment, port, version):
    script = write(target, "workbench_server.py", LEGACY.encode())
    with subprocess.Popen([sys.executable, "-B", str(script)], cwd=target,
                          env={**environment, "FIXTURE_WORKBENCH_VERSION": version},
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                          creationflags=subprocess.CREATE_NO_WINDOW) as legacy:
        try:
            deadline = time.monotonic() + 10
            while True:
                try:
                    _, old = ask(port, "/api/status")
                    break
                except URLError:
                    if time.monotonic() >= deadline or legacy.poll() is not None:
                        raise
                    time.sleep(0.1)
            assert Path(old["workbench_root"]) == target and old["version"] == version
            try:
                ask(port, "/api/background/config", {"enabled": False})
            except HTTPError as error:
                assert error.code == 404 and json.load(error) == {"error": "Not found"}
            else:
                raise AssertionError("Fixture should reproduce Not found")
            yield legacy
        finally:
            try:
                stop_fixture(port, target)
            finally:
                if legacy.poll() is None:
                    legacy.terminate()


def verify():
    with preserve_install_registration(), tempfile.TemporaryDirectory(prefix="verify-bg-repair-", dir=ROOT / "artifacts") as folder:
        target = Path(folder).resolve() / "existing 已登录"
        assert target.is_relative_to(ROOT / "artifacts")
        port = unused_port()
        environment = {**os.environ, "MOVER_WORKBENCH_PORT": str(port), "PYTHONUTF8": "1"}
        prefs, before = seed(target, port)
        missing_log = run_repair(environment, 1)
        assert "Repair stopped:" in missing_log
        assert not (target / "runtime/update-backups").exists()
        with legacy_workbench(target, environment, port, "2.0.8") as legacy:
            log = run_repair(environment, 0)
            assert "Verified local release 2.0.9" in log and "HTTP 200" in log, log
            assert legacy.wait(timeout=5) == 0
            _, live = ask(port, "/api/status")
            assert Path(live["workbench_root"]) == target and live["version"] == "2.0.9"
            assert all(f.read_bytes() == expected for f, expected in before.items())
            assert not live["astrbot"]["running"] and not live["background"]["running"]
            again = run_repair(environment, 0)
            assert "Verified local release" not in again and "HTTP 200" in again, again
            _, repeated = ask(port, "/api/status")
            assert repeated["workbench_pid"] == live["workbench_pid"]
            code, saved = ask(port, "/api/background/config", {"enabled": False, "keep_awake": True, "autostart": False})
            _, checked = ask(port, "/api/status")
            assert prefs["keep_awake"] is False
            assert code == 200 and saved["keep_awake"] and checked["background"]["keep_awake"]
        with legacy_workbench(target, environment, port, "2.0.9") as legacy:
            log = run_repair(environment, 0)
            assert "reports 2.0.9 but the save route returns HTTP 404" in log and "HTTP 200" in log, log
            assert legacy.wait(timeout=5) == 0
            _, live = ask(port, "/api/status")
            assert live["version"] == "2.0.9" and live["background"]["keep_awake"] is True
    return {
        "legacyVersion": "2.0.8", "legacySaveHttpStatus": 404, "repairedVersion": "2.0.9",
        "savedSettingsHttpStatus": 200, "savedChangedValuePersisted": True,
        "loginRulesAndPreferencesBytesPreservedBeforeUserChange": True,
        "sameVersionWithMissingEndpointRepaired": True,
        "noRunningWorkbenchHandled": True, "healthyRerunKeptProcess": True,
        "astrbotOrPlatformAccountsStarted": False, "repairSha256": hashlib.sha256(REPAIR.read_bytes()).hexdigest(),
    }


if __name__ == "__main__":
    report = json.dumps(verify(), indent=2)
    (RELEASE / "background-repair-verification.json").write_text(report + "\n", encoding="utf-8")
    print(report)
