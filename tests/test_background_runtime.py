import json
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch
import urllib.request

import psutil

import background_runtime as runtime
import background_supervisor as watchdog


class BackgroundRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="mover-background-test-")
        self.root = Path(self.temporary.name)

    def tearDown(self):
        for logger in list(watchdog.logging.Logger.manager.loggerDict.values()):
            if isinstance(logger, watchdog.logging.Logger) and logger.name.startswith("mover-supervisor-"):
                for handler in logger.handlers[:]:
                    handler.close()
                    logger.removeHandler(handler)
        self.temporary.cleanup()

    def live_status(self, running=True):
        return {"workbench_root": str(self.root), "workbench_pid": os.getpid(),
                "astrbot": {"running": running, "pid": None}}

    def create_supervisor(self):
        runtime.set_desired(self.root, enabled=True, keep_awake=False)
        return watchdog.Supervisor(self.root)

    def test_preferences_are_separate_from_saved_platform_login(self):
        credentials = self.root / "runtime/LarkAgentX-data/accounts/default/credentials.json"
        credentials.parent.mkdir(parents=True)
        credentials.write_bytes(b'private-fixture-bytes')
        runtime.set_desired(self.root, enabled=True, astrbot_desired=False, port=19333)
        result = runtime.settings(self.root)
        self.assertTrue(result["enabled"])
        self.assertFalse(result["astrbot_desired"])
        self.assertEqual(result["port"], 19333)
        self.assertEqual(credentials.read_bytes(), b'private-fixture-bytes')
        self.assertTrue(runtime.settings(self.root / "other-install")["enabled"])
        self.assertEqual(runtime.settings(self.root / "other-install")["keep_awake"], True)

    def test_fresh_settings_are_enabled_once_and_existing_user_opt_out_is_preserved(self):
        first = runtime.ensure_settings(self.root)
        self.assertTrue(first["enabled"])
        self.assertTrue(first["keep_awake"])
        runtime.set_desired(self.root, enabled=False, keep_awake=False, autostart=False)
        second = runtime.ensure_settings(self.root)
        self.assertFalse(second["enabled"])
        self.assertFalse(second["keep_awake"])

    def test_live_supervisor_with_stale_heartbeat_is_reported_unresponsive(self):
        live = {"pid": os.getpid(), "process_started_at": runtime.process_started_at(os.getpid()),
                "state": "running", "keep_awake_active": True,
                "heartbeat_at": (datetime.now(timezone.utc) - timedelta(minutes=3)).isoformat()}
        runtime._atomic_json(self.root / "runtime/background.status.json", live)
        value = runtime.status(self.root)
        self.assertTrue(value["running"])  # Never spawn a duplicate of a hung process.
        self.assertEqual(value["state"], "not_responding")
        self.assertFalse(value["heartbeat_fresh"])
        self.assertFalse(value["keep_awake_active"])
        live["heartbeat_at"] = datetime.now(timezone.utc).isoformat()
        runtime._atomic_json(self.root / "runtime/background.status.json", live)
        value = runtime.status(self.root)
        self.assertEqual(value["state"], "running")
        self.assertTrue(value["heartbeat_fresh"])
        self.assertTrue(value["keep_awake_active"])

    def test_settings_reject_bad_switch_and_port_without_replacing_valid_file(self):
        runtime.set_desired(self.root, enabled=True)
        before = (self.root / "runtime/background.settings.json").read_bytes()
        for changes in ({"enabled": "false"}, {"port": True}, {"port": 80}, {"token": "secret"}):
            with self.assertRaises(ValueError):
                runtime.set_desired(self.root, **changes)
        self.assertEqual((self.root / "runtime/background.settings.json").read_bytes(), before)

    def test_pause_requires_process_creation_time_to_prevent_stale_pid_match(self):
        runtime._atomic_json(self.root / "runtime/update-in-progress.json", {"pid": os.getpid(), "process_started_at": 1})
        self.assertEqual(runtime.paused(self.root), "")
        actual = runtime.process_started_at(os.getpid())
        self.assertIsNotNone(actual)
        runtime._atomic_json(self.root / "runtime/update-in-progress.json", {"pid": os.getpid(), "process_started_at": actual})
        self.assertEqual(runtime.paused(self.root), "update")

    def test_autostart_command_has_no_browser_or_qr_launch_and_is_install_specific(self):
        command = runtime._startup_command(self.root / "中文 路径")
        self.assertIn('start-workbench.ps1"', command)
        self.assertIn("-ResumeBackground", command)
        self.assertIn("-NoBrowser", command)
        self.assertIn("-SkipDesktopActions", command)
        self.assertNotEqual(runtime._install_key(self.root), runtime._install_key(self.root / "second"))

    def test_installed_native_launcher_is_used_for_login_without_a_powershell_window(self):
        folder = self.root / "中文 路径"
        folder.mkdir()
        (folder / "mover-v2-launcher.exe").write_bytes(b"native-launcher-fixture")
        command = runtime._startup_command(folder)
        self.assertIn('mover-v2-launcher.exe"', command)
        self.assertIn("--resume-background", command)
        self.assertNotIn("powershell", command.lower())

    def test_user_stop_disables_recovery_without_stopping_unrelated_programs(self):
        supervisor = self.create_supervisor()
        runtime.set_desired(self.root, enabled=False)
        with patch.object(watchdog, "_request") as request, patch.object(supervisor, "_start_workbench") as start:
            self.assertFalse(supervisor.tick())
        request.assert_not_called()
        start.assert_not_called()

    def test_update_pause_prevents_all_restart_requests(self):
        supervisor = self.create_supervisor()
        with patch.object(runtime, "paused", return_value="update"), patch.object(watchdog, "_request") as request:
            self.assertTrue(supervisor.tick())
        request.assert_not_called()
        self.assertEqual(runtime._read(self.root / "runtime/background.status.json")["state"], "paused")

    def test_foreign_workbench_on_same_port_is_never_controlled(self):
        supervisor = self.create_supervisor()
        with patch.object(watchdog, "_request", return_value={"workbench_root": str(self.root / "other")}) as request, patch.object(supervisor, "_start_workbench") as start:
            supervisor.tick()
        self.assertEqual(request.call_count, 1)
        start.assert_not_called()
        self.assertEqual(runtime._read(self.root / "runtime/background.status.json")["state"], "port_conflict")

    def test_scan_login_and_disconnected_platforms_do_not_restart_astrbot(self):
        supervisor = self.create_supervisor()
        value = self.live_status()
        value.update({"runtime": {"qq": {"connected": False}}, "larkagentx": {"bridge_state": "waiting_login"}})
        with patch.object(watchdog, "_request", return_value=value) as request:
            supervisor.tick()
        self.assertEqual(request.call_count, 1)
        self.assertEqual(runtime._read(self.root / "runtime/background.status.json")["astrbot_state"], "running")

    def test_manually_stopped_astrbot_stays_stopped(self):
        supervisor = self.create_supervisor()
        runtime.set_desired(self.root, astrbot_desired=False)
        with patch.object(watchdog, "_request", return_value=self.live_status(False)) as request:
            supervisor.tick()
        self.assertEqual(request.call_count, 1)
        self.assertEqual(runtime._read(self.root / "runtime/background.status.json")["astrbot_state"], "manually_stopped")

    def test_dead_astrbot_restarts_with_preserved_login_and_backoff(self):
        supervisor = self.create_supervisor()
        def request(port, path, body=None):
            return self.live_status(False) if body is None else {"started": True}
        with patch.object(watchdog, "_request", side_effect=request) as requests:
            supervisor.tick()
            supervisor.tick()
        starts = [call for call in requests.call_args_list if call.args[1] == "/api/astrbot/start"]
        self.assertEqual(len(starts), 1)
        self.assertEqual(starts[0].args[2], {"reset_password": False, "preserve_background": True})
        self.assertGreater(supervisor.next_try["astrbot"], time.monotonic())

    def test_live_unresponsive_workbench_is_not_duplicated(self):
        supervisor = self.create_supervisor()
        supervisor.workbench_pid = os.getpid()
        supervisor.workbench_started_at = runtime.process_started_at(os.getpid())
        with patch.object(watchdog, "_request", side_effect=OSError), patch.object(watchdog, "_occupied", return_value=False), patch.object(supervisor, "_start_workbench") as start:
            supervisor.tick()
        start.assert_not_called()

    def test_exited_workbench_recovers_but_respects_exponential_retry(self):
        supervisor = self.create_supervisor()
        child = Mock(pid=123456)
        child.poll.return_value = 1
        with patch.object(watchdog, "_request", side_effect=OSError), patch.object(watchdog, "_occupied", return_value=False), patch.object(supervisor, "_start_workbench", return_value=child) as start:
            supervisor.tick()
            supervisor.tick()
        self.assertEqual(start.call_count, 1)
        self.assertEqual(supervisor.restart_count, 1)

    def test_backoff_increases_until_capped_and_resets_only_after_stable_run(self):
        supervisor = self.create_supervisor()
        for _ in range(12):
            supervisor._backoff("astrbot", "fixture")
        delay = supervisor.next_try["astrbot"] - time.monotonic()
        self.assertGreater(delay, 295)
        self.assertLessEqual(delay, 300)
        supervisor._healthy("astrbot")
        self.assertGreater(supervisor.failures["astrbot"], 0)
        supervisor.healthy_since["astrbot"] = time.monotonic() - 61
        supervisor._healthy("astrbot")
        self.assertEqual(supervisor.failures["astrbot"], 0)

    def test_requested_supervisor_exit_retains_preferences(self):
        supervisor = self.create_supervisor()
        runtime._atomic_json(self.root / "runtime/supervisor.stop.json", {"pid": os.getpid(), "process_started_at": supervisor.started_at})
        self.assertFalse(supervisor.tick())
        self.assertTrue(runtime.settings(self.root)["enabled"])


@unittest.skipUnless(os.name == "nt", "Windows detached watchdog integration")
class DetachedWatchdogIntegrationTests(unittest.TestCase):
    def test_workbench_process_crash_recovers_and_manual_stop_exits_watchdog(self):
        root_source = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(prefix="mover-watchdog-process-") as directory:
            root = Path(directory)
            for name in ("background_runtime.py", "background_supervisor.py"):
                shutil.copy2(root_source / name, root / name)
            with socket.socket() as socket_fixture:
                socket_fixture.bind(("127.0.0.1", 0))
                port = socket_fixture.getsockname()[1]
            (root / "workbench_server.py").write_text('''import json, os, threading
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
root=Path(__file__).resolve().parent
class H(BaseHTTPRequestHandler):
 def do_GET(self):
  data=json.dumps({"workbench_root":str(root),"workbench_pid":os.getpid(),"astrbot":{"running":True}}).encode()
  self.send_response(200);self.send_header("Content-Length",str(len(data)));self.end_headers();self.wfile.write(data)
 def do_POST(self):
  self.send_response(200);self.send_header("Content-Length","2");self.end_headers();self.wfile.write(b'{}')
  threading.Thread(target=self.server.shutdown,daemon=True).start()
 def log_message(self,*args):pass
ThreadingHTTPServer(("127.0.0.1",int(os.environ["MOVER_WORKBENCH_PORT"])),H).serve_forever()
''', encoding="utf-8")
            runtime.set_desired(root, enabled=True, keep_awake=False, port=port)
            process = subprocess.Popen([sys.executable, str(root / "background_supervisor.py"), "--interval", "0.2"], creationflags=subprocess.CREATE_NO_WINDOW)
            scripts = {(root / name).resolve() for name in ("background_supervisor.py", "workbench_server.py")}

            def fixture_processes():
                # A Windows virtualenv python.exe can be a launcher which owns
                # a different interpreter PID. Track both by the exact copied
                # script, never by process name or an unverified reused PID.
                found = []
                for candidate in psutil.process_iter(["pid", "exe", "cmdline", "create_time"]):
                    try:
                        if Path(candidate.info.get("exe") or "").name.lower() not in {"python.exe", "pythonw.exe"}:
                            continue
                        if any(Path(arg).resolve() in scripts for arg in (candidate.info.get("cmdline") or [])[1:] if arg and not arg.startswith("-")):
                            found.append(candidate)
                    except (psutil.Error, OSError, ValueError):
                        continue
                return found

            def local_request(path, body=None):
                data = None if body is None else json.dumps(body).encode("utf-8")
                request = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data,
                                                 headers={"Content-Type": "application/json"})
                # The polling deadline must bound the probe as well as the
                # interval. The production helper permits an 8s response while
                # a Windows refused connection can itself consume ~2s.
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                with opener.open(request, timeout=0.5) as response:
                    return json.load(response)

            try:
                def ready(excluded=None):
                    deadline = time.monotonic() + 15
                    last_error = "no response"
                    while time.monotonic() < deadline:
                        try:
                            value = local_request("/api/status")
                            self.assertEqual(Path(value["workbench_root"]).resolve(), root.resolve())
                            identity = (value["workbench_pid"], runtime.process_started_at(value["workbench_pid"]))
                            if identity[1] is not None and identity != excluded:
                                return value
                        except Exception as exc:
                            last_error = type(exc).__name__
                        time.sleep(0.15)
                    status = runtime._read(root / "runtime/background.status.json")
                    logs = {path.name: path.read_text(encoding="utf-8", errors="replace")[-2000:]
                            for path in (root / "runtime/logs").glob("*.log")}
                    self.fail(f"Detached watchdog did not start/recover its fixture workbench; "
                              f"last_probe={last_error}; status={status}; logs={logs}")
                first = ready()
                child = psutil.Process(first["workbench_pid"])
                first_identity = (child.pid, child.create_time())
                self.assertIn(child.pid, {candidate.pid for candidate in fixture_processes()})
                child.kill()
                # TerminateProcess is asynchronous. Observe the verified real
                # interpreter exit before measuring watchdog recovery, including
                # when a virtualenv launcher owns a different PID.
                child.wait(timeout=5)
                second = ready(excluded=first_identity)
                second_identity = (second["workbench_pid"], runtime.process_started_at(second["workbench_pid"]))
                self.assertNotEqual(first_identity, second_identity)
                runtime.set_desired(root, enabled=False)
                self.assertEqual(process.wait(timeout=10), 0)
            finally:
                runtime.set_desired(root, enabled=False)
                try:
                    local_request("/stop", {})
                except Exception:
                    pass
                # An open TCP port is not a process-lifetime fence: the venv
                # launcher may still own redirected output handles after the
                # interpreter closes its listener. Wait for all exact-fixture
                # identities, then terminate only surviving verified fixtures.
                _, remaining = psutil.wait_procs(fixture_processes(), timeout=3)
                verified = {candidate.pid: candidate.create_time() for candidate in fixture_processes()}
                for candidate in remaining:
                    if verified.get(candidate.pid) == candidate.create_time():
                        try:
                            candidate.terminate()
                        except psutil.NoSuchProcess:
                            pass
                _, remaining = psutil.wait_procs(remaining, timeout=3)
                self.assertFalse(remaining, "Exact fixture processes did not exit; refusing unrelated process cleanup")
                process.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
