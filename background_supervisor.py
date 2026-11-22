"""A per-install background watchdog; never replaces or terminates foreign processes."""
from __future__ import annotations

import argparse
import ctypes
from datetime import datetime, timezone
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import socket
import time
import urllib.error
import urllib.request

import background_runtime as runtime

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001


def _set_execution_state(flags: int) -> tuple[bool, int | None]:
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    function = kernel.SetThreadExecutionState
    function.argtypes = [ctypes.c_uint32]
    function.restype = ctypes.c_uint32
    ctypes.set_last_error(0)
    success = bool(function(flags))
    return success, None if success else ctypes.get_last_error()


def _request(port: int, path: str, body: dict | None = None):
    data = None if body is None else json.dumps(body).encode("utf-8")
    request = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data, headers={"Content-Type": "application/json"})
    # Bypass machine proxy settings for this private local control channel.
    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=8) as response:
        return json.load(response)


def _occupied(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except OSError:
        return False


class Supervisor:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.pid = os.getpid()
        self.started_at = runtime.process_started_at(self.pid)
        self.workbench_pid = None
        self.workbench_started_at = None
        self.workbench_child = None
        self.restart_count = 0
        self.failures = {"workbench": 0, "astrbot": 0}
        self.next_try = {"workbench": 0.0, "astrbot": 0.0}
        self.healthy_since = {"workbench": None, "astrbot": None}
        self.awake = False
        self.power_state = "pending"
        self.power_error_code = None
        self._ready_reported = False
        directory = self.root / "runtime/logs"
        directory.mkdir(parents=True, exist_ok=True)
        self.logger = logging.getLogger("mover-supervisor-" + runtime._install_key(self.root))
        self.logger.setLevel(logging.INFO)
        self.logger.propagate = False
        if not self.logger.handlers:
            handler = RotatingFileHandler(directory / "supervisor.log", maxBytes=4 * 1024 * 1024, backupCount=5, encoding="utf-8")
            handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
            self.logger.addHandler(handler)

    def _power(self, enabled: bool):
        previous_state, previous_error = self.power_state, self.power_error_code
        self.awake = False
        self.power_error_code = None
        if os.name == "nt":
            # ES_SYSTEM_REQUIRED deliberately allows Windows to turn off the display.
            try:
                success, error = _set_execution_state(ES_CONTINUOUS | (ES_SYSTEM_REQUIRED if enabled else 0))
            except (OSError, AttributeError) as exc:
                success, error = False, getattr(exc, "winerror", 0)
            self.awake = success and enabled
            self.power_state = "active" if self.awake else "failed" if enabled else "disabled"
            if enabled and not success:
                self.power_error_code = error if type(error) is int and 0 <= error <= 0xFFFFFFFF else 0
        else:
            self.power_state = "unsupported" if enabled else "disabled"
        if self.power_state == "failed" and (previous_state, previous_error) != (self.power_state, self.power_error_code):
            self.logger.warning("Idle sleep prevention request failed (Win32 error %s); automatic recovery remains active", self.power_error_code)
        elif self.power_state == "active" and previous_state == "failed":
            self.logger.info("Idle sleep prevention request recovered; display may turn off")

    def _state(self, state: str, workbench: str, astrbot: str):
        runtime._atomic_json(self.root / "runtime/background.status.json", {
            "install_key": runtime._install_key(self.root),
            "pid": self.pid, "process_started_at": self.started_at,
            "heartbeat_at": datetime.now(timezone.utc).isoformat(), "state": state,
            "workbench_state": workbench, "astrbot_state": astrbot,
            "keep_awake_active": self.awake, "keep_awake_state": self.power_state,
            "keep_awake_error_code": self.power_error_code, "restart_count": self.restart_count,
        })

    def _backoff(self, name: str, error: str):
        self.failures[name] += 1
        delay = min(300, 5 * 2 ** min(self.failures[name] - 1, 6))
        self.next_try[name] = time.monotonic() + delay
        self.healthy_since[name] = None
        self.logger.warning("%s recovery attempt deferred for %ss (%s)", name, delay, error)

    def _healthy(self, name: str):
        now = time.monotonic()
        if self.healthy_since[name] is None:
            self.healthy_since[name] = now
        if now - self.healthy_since[name] >= 60:
            self.failures[name] = 0
            self.next_try[name] = 0

    def tick(self) -> bool:
        stop = runtime._read(self.root / "runtime/supervisor.stop.json")
        if stop.get("pid") == self.pid and runtime.is_same_process(stop.get("pid"), stop.get("process_started_at")):
            self._power(False)
            self._state("stopped", "unmanaged", "unmanaged")
            return False
        config = runtime.settings(self.root)
        if not config["enabled"]:
            self._power(False)
            self._state("stopped", "unmanaged", "unmanaged")
            return False
        self._power(config["keep_awake"])
        if not self._ready_reported:
            # Publish ownership and the actual power result before any network
            # probe (which may block while the workbench is still starting).
            self._state("starting", "unknown", "unknown")
            self._ready_reported = True
        if runtime.paused(self.root):
            self._state("paused", "updating", "updating")
            return True
        port = config["port"]
        current = None
        try:
            current = _request(port, "/api/status")
        except (OSError, ValueError, urllib.error.URLError):
            pass
        if current:
            try:
                same = Path(current.get("workbench_root", "")).resolve() == self.root
            except (ValueError, OSError, TypeError):
                same = False
            if not same:
                self._state("port_conflict", "another_install", "unmanaged")
                return True
            self.workbench_pid = current.get("workbench_pid") or self.workbench_pid
            if self.workbench_pid:
                self.workbench_started_at = runtime.process_started_at(int(self.workbench_pid))
            self._healthy("workbench")
        if not current:
            self.healthy_since["workbench"] = None
            still_alive = runtime.is_same_process(self.workbench_pid, self.workbench_started_at)
            if not still_alive and self.workbench_child is not None and self.workbench_child.poll() is None:
                still_alive = True
            if _occupied(port) or still_alive:
                self._state("waiting", "not_responding", "unknown")
                return True
            self._state("recovering", "stopped", "unknown")
            if time.monotonic() < self.next_try["workbench"]:
                return True
            # Re-read after probing: a user may have pressed Stop in the meantime.
            if not runtime.settings(self.root)["enabled"] or runtime.paused(self.root):
                return True
            try:
                self.workbench_child = self._start_workbench(port)
                self.workbench_pid = self.workbench_child.pid
                self.workbench_started_at = runtime.process_started_at(self.workbench_pid)
                self.restart_count += 1
                self.logger.info("Workbench started in background (pid %s)", self.workbench_pid)
                self._backoff("workbench", "waiting for startup")
            except OSError as exc:
                self._backoff("workbench", type(exc).__name__)
            return True
        astrbot = current.get("astrbot", {})
        desired = runtime.settings(self.root)
        if not desired["astrbot_desired"]:
            self._state("running", "running", "manually_stopped")
            return True
        if astrbot.get("running"):
            self._healthy("astrbot")
            self._state("running", "running", "running")
        elif astrbot.get("pid") or astrbot.get("starting"):
            # A process that is still starting or blocked is not another launch slot.
            self._state("waiting", "running", "starting")
        elif time.monotonic() < self.next_try["astrbot"]:
            self._state("recovering", "running", "retry_wait")
        else:
            latest = runtime.settings(self.root)
            if not latest["enabled"] or runtime.paused(self.root):
                return True
            if not latest["astrbot_desired"]:
                self._state("running", "running", "manually_stopped")
                return True
            self._state("recovering", "running", "restarting")
            try:
                result = _request(port, "/api/astrbot/start", {"reset_password": False, "preserve_background": True, "automatic_recovery": True})
                if result.get("skipped") is True and result.get("started") is False:
                    if result.get("reason") == "disabled":
                        self._power(False)
                        self._state("stopped", "unmanaged", "unmanaged")
                        return False
                    if result.get("reason") == "paused":
                        self._state("paused", "updating", "updating")
                        return True
                    if result.get("reason") == "manually_stopped":
                        self._state("running", "running", "manually_stopped")
                        return True
                if not result.get("started"):
                    raise ValueError("not_started")
                self.restart_count += 1
                self.logger.info("AstrBot restart requested; saved platform login and rules retained")
                self._backoff("astrbot", "waiting for startup")
            except (OSError, ValueError, urllib.error.URLError) as exc:
                self._backoff("astrbot", type(exc).__name__)
        return True

    def _start_workbench(self, port):
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + str(time.time_ns())
        return runtime.launch_workbench(self.root, port, f"workbench-supervised-{stamp}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--interval", type=float, default=3)
    args = parser.parse_args()
    root = Path(__file__).resolve().parent
    handle = None
    if os.name == "nt":
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.SetLastError(0)
        kernel.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
        kernel.CreateMutexW.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.CreateMutexW(None, False, "Local\\MoverV2-Supervisor-" + runtime._install_key(root))
        if not handle:
            raise OSError(ctypes.get_last_error(), "Cannot create supervisor mutex")
        if ctypes.get_last_error() == 183:
            kernel.CloseHandle(handle)
            return
    supervisor = Supervisor(root)
    supervisor.logger.info("Background supervisor started; requesting idle-sleep prevention when enabled; display may turn off")
    try:
        while True:
            try:
                if not supervisor.tick():
                    break
            except Exception as exc:
                # A temporarily unreadable status file or a local endpoint error
                # must not take the recovery process itself down. Log only the
                # exception type: platform responses can contain private content.
                supervisor.logger.error("Supervisor check failed (%s); retrying", type(exc).__name__)
            time.sleep(max(0.2, min(args.interval, 30)))
    finally:
        supervisor._power(False)
        supervisor.logger.info("Background supervisor stopped")
        if handle:
            kernel.CloseHandle(handle)


if __name__ == "__main__":
    main()
