import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

import psutil

import astrbot_runner
import workbench_server


class AstrBotRunnerTests(unittest.TestCase):
    def test_runner_collector_survives_workbench_parent_exit_and_rotates_logs(self):
        source_root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(prefix="mover-astrbot-runner-") as temporary:
            root = Path(temporary)
            fake = root / "fake_astrbot.py"
            fake.write_text(
                "import time\nfor i in range(18):\n print(f'astrbot-line-{i}', flush=True)\n time.sleep(.18)\n",
                encoding="utf-8",
            )
            pid_file = root / "runtime/AstrBot/.mover-v2-astrbot.pid"
            log_path = root / "runtime/AstrBot/data/logs/workbench-astrbot.log"
            runner = source_root / "astrbot_runner.py"
            command = [sys.executable, str(fake)]
            wrapper = root / "workbench_parent.py"
            wrapper.write_text(
                "import os,subprocess,sys,time\n"
                "root,runner,pidfile,log,*command=sys.argv[1:]\n"
                "flags=getattr(subprocess,'DETACHED_PROCESS',0)|getattr(subprocess,'CREATE_NO_WINDOW',0)\n"
                "p=subprocess.Popen([sys.executable,runner,'--root',root,'--pid-file',pidfile,'--log',log,'--',*command],"
                "cwd=root,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,creationflags=flags)\n"
                "deadline=time.time()+5\n"
                "while time.time()<deadline and not os.path.exists(pidfile): time.sleep(.02)\n"
                "if not os.path.exists(pidfile): raise SystemExit(5)\n",
                encoding="utf-8",
            )
            result = subprocess.run(
                [sys.executable, str(wrapper), str(root), str(runner), str(pid_file), str(log_path), *command],
                cwd=root, capture_output=True, text=True, timeout=15,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertTrue(pid_file.is_file())
            marker = json.loads(pid_file.read_text(encoding="utf-8"))
            self.assertTrue(astrbot_runner.same_process(marker["runner"]))
            # The creating workbench-like process has exited. The independent
            # runner still drains output and replaces credentials before logging.
            deadline = time.monotonic() + 12
            while time.monotonic() < deadline:
                content = log_path.read_text(encoding="utf-8") if log_path.exists() else ""
                if "astrbot-line-17" in content:
                    break
                time.sleep(0.1)
            self.assertIn("astrbot-line-0", content)
            self.assertIn("astrbot-line-17", content)
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and astrbot_runner.same_process(marker["runner"]):
                time.sleep(0.1)
            self.assertFalse(astrbot_runner.same_process(marker["runner"]))
            self.assertEqual(json.loads(pid_file.read_text(encoding="utf-8"))["state"], "stopped")

    def test_owner_validation_rejects_reused_or_wrong_working_directory_process(self):
        current = psutil.Process()
        script = Path(__file__).resolve().parents[1] / "astrbot_runner.py"
        valid_shape = {"runner": {"pid": current.pid, "process_started_at": current.create_time()}}
        self.assertIsNone(astrbot_runner.owned_runner(valid_shape, Path(tempfile.gettempdir()), script))
        stale = {"runner": {"pid": current.pid, "process_started_at": current.create_time() - 100}}
        self.assertFalse(astrbot_runner.same_process(stale["runner"]))

    def test_runner_child_tree_stop_requires_matching_creation_time(self):
        current = psutil.Process()
        astrbot_runner.stop_owned_tree(current.pid, current.create_time() - 300, timeout=0.1)
        self.assertTrue(current.is_running())

    def test_runner_child_tree_stop_terminates_only_the_recorded_runner_tree(self):
        with tempfile.TemporaryDirectory(prefix="mover-stop-tree-") as temporary:
            child_pid_file = Path(temporary) / "child.pid"
            child_code = "import os,subprocess,sys,time; p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)']); open(sys.argv[1],'w').write(str(p.pid)); time.sleep(60)"
            parent = subprocess.Popen([sys.executable, "-c", child_code, str(child_pid_file)],
                                      stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline and not child_pid_file.exists():
                    time.sleep(0.05)
                self.assertTrue(child_pid_file.exists())
                parent_identity = {"pid": parent.pid, "process_started_at": psutil.Process(parent.pid).create_time()}
                child_pid = int(child_pid_file.read_text())
                child_identity = {"pid": child_pid, "process_started_at": psutil.Process(child_pid).create_time()}
                self.assertTrue(astrbot_runner.launched_by(child_pid, parent.pid))
                self.assertTrue(astrbot_runner.launched_by(parent.pid, parent.pid))
                self.assertFalse(astrbot_runner.launched_by(os.getpid(), parent.pid))
                astrbot_runner.stop_owned_tree(parent.pid, parent_identity["process_started_at"], timeout=2)
                parent.wait(timeout=5)
                self.assertFalse(astrbot_runner.same_process(parent_identity))
                self.assertFalse(astrbot_runner.same_process(child_identity))
            finally:
                if parent.poll() is None:
                    parent.kill()
                    parent.wait(timeout=5)

    def test_workbench_stop_uses_marker_creation_time_not_pid_alone(self):
        with tempfile.TemporaryDirectory(prefix="mover-stop-marker-") as temporary:
            marker_path = Path(temporary) / "owned.json"
            marker_path.write_text(json.dumps({"pid": 41, "process_started_at": 100.0,
                                               "runner": {"pid": 42, "process_started_at": 200.0}}), encoding="utf-8")
            with (
                mock.patch.object(workbench_server, "ASTRBOT_PID_FILE", marker_path),
                mock.patch.object(workbench_server, "_managed_astrbot_pid", return_value=42),
                mock.patch("astrbot_runner.same_process", side_effect=[True, False]) as same,
                mock.patch("astrbot_runner.stop_owned_tree") as stop,
            ):
                result = workbench_server.stop_astrbot()
            self.assertTrue(result["stopped"])
            same.assert_any_call({"pid": 42, "process_started_at": 200.0})
            stop.assert_called_once_with(42, 200.0)

    def test_workbench_stop_keeps_marker_when_owned_process_survives(self):
        with tempfile.TemporaryDirectory(prefix="mover-stop-survivor-") as temporary:
            marker_path = Path(temporary) / "owned.json"
            marker_path.write_text(json.dumps({"pid": 41, "process_started_at": 100.0,
                                               "runner": {"pid": 42, "process_started_at": 200.0}}), encoding="utf-8")
            with (
                mock.patch.object(workbench_server, "ASTRBOT_PID_FILE", marker_path),
                mock.patch.object(workbench_server, "_managed_astrbot_pid", return_value=42),
                mock.patch("astrbot_runner.same_process", return_value=True),
                mock.patch("astrbot_runner.stop_owned_tree") as stop,
            ):
                with self.assertRaises(ValueError):
                    workbench_server.stop_astrbot()
            stop.assert_called_once_with(42, 200.0)
            self.assertTrue(marker_path.is_file())

    def test_astrbot_fallback_recognizes_direct_launcher_and_python_module_entry(self):
        root = Path("C:/InformationMover/runtime/.venv/Scripts")
        exe = root / "astrbot.exe"
        self.assertTrue(astrbot_runner.is_astrbot_command([str(exe), "run", "-p", "6185"], exe, exe))
        python = root / "python.exe"
        self.assertTrue(astrbot_runner.is_astrbot_command([str(python), "-m", "astrbot", "run", "-p", "6185"], python, exe))
        self.assertTrue(astrbot_runner.is_astrbot_command([str(python), "-B", "-m", "astrbot.cli", "run", "-p", "6185"], python, exe))
        other = root / "other/python.exe"
        self.assertFalse(astrbot_runner.is_astrbot_command([str(other), "-m", "astrbot.cli", "run"], other, exe))
        self.assertTrue(astrbot_runner.is_astrbot_command([str(python), str(exe), "run", "-p", "6185"], python, exe))
        self.assertFalse(astrbot_runner.is_astrbot_command([str(python), str(root / "other" / "astrbot.exe"), "run"], python, exe))
        self.assertFalse(astrbot_runner.is_astrbot_command([str(python), "other.py", "run"], python, exe))

    def test_orphaned_windows_launcher_requires_an_exact_astrbot_descendant_in_its_root(self):
        with tempfile.TemporaryDirectory(prefix="mover-orphan-owner-") as temporary:
            root = Path(temporary).resolve()
            exe = root / "venv/Scripts/astrbot.exe"
            python = root / "venv/Scripts/python.exe"
            marker = root / "owned.json"
            marker.write_text(json.dumps({"pid": 41, "process_started_at": 100.0}), encoding="utf-8")
            launcher = mock.Mock(pid=41)
            launcher.exe.return_value = str(exe)
            launcher.cmdline.return_value = [str(exe), "run", "-p", "6185"]
            launcher.cwd.return_value = str(root / "temporary-launcher-cwd")
            child = mock.Mock(pid=42)
            child.exe.return_value = str(python)
            child.cmdline.return_value = [str(python), str(exe), "run", "-p", "6185"]
            child.cwd.return_value = str(root)
            cases = [(True, True, 41), (False, True, None), (True, False, None)]
            for correct_cwd, correct_exe, expected in cases:
                with self.subTest(correct_cwd=correct_cwd, correct_exe=correct_exe):
                    child.cwd.return_value = str(root if correct_cwd else root / "another-install")
                    child.cmdline.return_value = [str(python), str(exe if correct_exe else root / "another/astrbot.exe"), "run"]
                    launcher.children.return_value = [child]
                    with (
                        mock.patch.object(workbench_server, "ASTRBOT_PID_FILE", marker),
                        mock.patch.object(workbench_server, "ASTRBOT_EXE", exe),
                        mock.patch.object(workbench_server, "astrbot_root", return_value=root),
                        mock.patch("astrbot_runner.owned_runner", return_value=None),
                        mock.patch("astrbot_runner.same_process", return_value=True),
                        mock.patch("psutil.Process", return_value=launcher),
                    ):
                        self.assertEqual(workbench_server._managed_astrbot_pid(), expected)


if __name__ == "__main__":
    unittest.main()
