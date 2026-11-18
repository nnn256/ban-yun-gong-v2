"""Per-install background preferences; contains no platform credentials."""
from __future__ import annotations

import argparse
import contextlib
import ctypes
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Any

DEFAULTS = {"enabled": True, "astrbot_desired": True, "keep_awake": True, "autostart": False, "port": 18766}


def _runtime(root: Path) -> Path:
    return Path(root).resolve() / "runtime"


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".{os.getpid()}.{time.time_ns()}.tmp")
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _read(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


@contextlib.contextmanager
def _settings_lock(root: Path):
    directory = _runtime(root)
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / "background.settings.lock").open("a+b") as lock:
        if os.name == "nt":
            import msvcrt
            if lock.tell() == 0:
                lock.write(b" ")
                lock.flush()
            deadline = time.monotonic() + 10
            while True:
                try:
                    lock.seek(0)
                    msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    if time.monotonic() > deadline:
                        raise TimeoutError("后台设置正在写入，请稍后重试")
                    time.sleep(0.05)
        else:
            import fcntl
            fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            if os.name == "nt":
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(lock, fcntl.LOCK_UN)


def settings(root: Path) -> dict[str, Any]:
    stored = _read(_runtime(root) / "background.settings.json")
    result = {**DEFAULTS, **{key: stored[key] for key in DEFAULTS if key in stored}}
    for key in ("enabled", "astrbot_desired", "keep_awake", "autostart"):
        result[key] = result[key] is True
    if isinstance(result["port"], bool) or not isinstance(result["port"], int) or not 1024 <= result["port"] <= 65535:
        result["port"] = DEFAULTS["port"]
    return result


def ensure_settings(root: Path) -> dict[str, Any]:
    """Persist fresh-install defaults once; never override an existing opt-out."""
    path = _runtime(root) / "background.settings.json"
    with _settings_lock(root):
        if not path.exists():
            _atomic_json(path, DEFAULTS)
            return dict(DEFAULTS)
    return settings(root)


def set_desired(root: Path, **changes: Any) -> dict[str, Any]:
    for key, value in changes.items():
        if key not in DEFAULTS:
            raise ValueError("不支持的后台设置")
        if key == "port":
            if isinstance(value, bool) or not isinstance(value, int) or not 1024 <= value <= 65535:
                raise ValueError("端口范围必须在 1024 到 65535 之间")
        elif not isinstance(value, bool):
            raise ValueError("后台开关必须是布尔值")
    with _settings_lock(root):
        result = {**settings(root), **changes}
        _atomic_json(_runtime(root) / "background.settings.json", result)
    return result


def process_started_at(pid: int) -> float | None:
    """Get process identity without trusting a reusable PID alone."""
    if os.name != "nt":
        try:
            import psutil
            process = psutil.Process(pid)
            return process.create_time() if process.is_running() else None
        except Exception:
            return None
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    handle = kernel.OpenProcess(0x1000, False, int(pid))
    if not handle:
        return None
    try:
        creation, exit_time, kernel_time, user_time = [wintypes.FILETIME() for _ in range(4)]
        code = wintypes.DWORD()
        if not kernel.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value != 259:
            return None
        if not kernel.GetProcessTimes(handle, ctypes.byref(creation), ctypes.byref(exit_time), ctypes.byref(kernel_time), ctypes.byref(user_time)):
            return None
        return ((creation.dwHighDateTime << 32) | creation.dwLowDateTime) / 10_000_000 - 11644473600
    finally:
        kernel.CloseHandle(handle)


def is_same_process(pid: Any, started_at: Any) -> bool:
    try:
        actual = process_started_at(int(pid))
        return actual is not None and abs(actual - float(started_at)) < 1.1
    except (TypeError, ValueError, OverflowError):
        return False


def paused(root: Path) -> str:
    for name in ("update", "setup"):
        value = _read(_runtime(root) / f"{name}-in-progress.json")
        if value and is_same_process(value.get("pid"), value.get("process_started_at")):
            return name
    return ""


def _install_key(root: Path) -> str:
    return hashlib.sha256(str(Path(root).resolve()).lower().encode("utf-8")).hexdigest()[:20]


def _startup_command(root: Path) -> str:
    for name in ("mover-v2-launcher.exe", "搬运工V2.exe"):
        launcher = Path(root).resolve() / name
        if launcher.is_file():
            return subprocess.list2cmdline([str(launcher), "--resume-background"])
    system_root = Path(os.environ.get("SystemRoot", r"C:\Windows"))
    powershell = system_root / "System32/WindowsPowerShell/v1.0/powershell.exe"
    return subprocess.list2cmdline([str(powershell), "-NoLogo", "-NoProfile", "-WindowStyle", "Hidden", "-ExecutionPolicy", "Bypass", "-File", str(Path(root).resolve() / "start-workbench.ps1"), "-NoBrowser", "-SkipDesktopActions", "-ResumeBackground"])


def configure_autostart(root: Path, enabled: bool) -> None:
    if os.name != "nt":
        if enabled:
            raise ValueError("仅 Windows 支持当前用户登录启动")
        return
    import winreg
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run") as key:
        name = "MoverV2-" + _install_key(root)
        if enabled:
            winreg.SetValueEx(key, name, 0, winreg.REG_SZ, _startup_command(root))
        else:
            try:
                winreg.DeleteValue(key, name)
            except FileNotFoundError:
                pass


def autostart_registered(root: Path) -> bool:
    if os.name != "nt":
        return False
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run") as key:
            return winreg.QueryValueEx(key, "MoverV2-" + _install_key(root))[0] == _startup_command(root)
    except OSError:
        return False


def status(root: Path) -> dict[str, Any]:
    result = settings(root)
    live = _read(_runtime(root) / "background.status.json")
    alive = is_same_process(live.get("pid"), live.get("process_started_at"))
    try:
        heartbeat = datetime.fromisoformat(str(live.get("heartbeat_at", "")).replace("Z", "+00:00"))
        heartbeat_age = (datetime.now(timezone.utc) - heartbeat.astimezone(timezone.utc)).total_seconds()
        fresh = -5 <= heartbeat_age <= 60
    except (ValueError, TypeError, OverflowError):
        fresh = False
    result.update({"running": alive, "heartbeat_at": live.get("heartbeat_at", ""),
                   "heartbeat_fresh": alive and fresh,
                   "pid": live.get("pid") if alive else None,
                   "state": (live.get("state", "starting") if fresh else "not_responding") if alive else "stopped",
                   "workbench_state": live.get("workbench_state", "unknown") if alive else "unknown",
                   "astrbot_state": live.get("astrbot_state", "unknown") if alive else "unknown",
                   "keep_awake_active": alive and fresh and live.get("keep_awake_active") is True,
                   "restart_count": live.get("restart_count", 0),
                   "autostart_registered": autostart_registered(root),
                   "pause_reason": paused(root),
                   "sleep_note": "支持息屏后台运行；睡眠、休眠、关机或退出 Windows 用户后无法继续转发。"})
    return result


def ensure_supervisor(root: Path, port: int | None = None) -> dict[str, Any]:
    if port is not None and port != settings(root)["port"]:
        set_desired(root, port=port)
    current = status(root)
    if current["autostart"] and not current["autostart_registered"]:
        configure_autostart(root, True)
    if not current["running"] and current["enabled"]:
        python = Path(root) / "runtime/.venv/Scripts/python.exe"
        if not python.is_file():
            python = Path(sys.executable)
        kwargs: dict[str, Any] = {"cwd": str(root), "stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS
        subprocess.Popen([str(python), str(Path(root) / "background_supervisor.py")], **kwargs)
    return status(root)


def launch_workbench(root: Path, port: int, log_stem: str) -> subprocess.Popen:
    root = root.resolve()
    directory = _runtime(root) / "logs"
    directory.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "MOVER_WORKBENCH_PORT": str(port), "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    with (directory / f"{log_stem}.stdout.log").open("ab") as out, (directory / f"{log_stem}.stderr.log").open("ab") as err:
        # Pass only these standard handles. Inheriting the launcher's pipes would
        # keep its console/log reader and single-instance lock alive after setup.
        return subprocess.Popen([sys.executable, str(root / "workbench_server.py")], cwd=root, env=env,
                                stdin=subprocess.DEVNULL, stdout=out, stderr=err, close_fds=True, creationflags=flags)


def request_stop_supervisor(root: Path, timeout: float = 12) -> bool:
    """Cooperatively stop only this install's watchdog, retaining preferences."""
    current = _read(_runtime(root) / "background.status.json")
    if not is_same_process(current.get("pid"), current.get("process_started_at")):
        return False
    _atomic_json(_runtime(root) / "supervisor.stop.json", {
        "pid": current["pid"], "process_started_at": current["process_started_at"],
    })
    deadline = time.monotonic() + timeout
    while is_same_process(current["pid"], current["process_started_at"]):
        if time.monotonic() >= deadline:
            raise TimeoutError("后台守护尚未退出，更新已停止；请稍后重试")
        time.sleep(0.15)
    return True


def configure(root: Path, values: dict[str, Any]) -> dict[str, Any]:
    allowed = {"enabled", "keep_awake", "autostart"}
    if set(values) - allowed:
        raise ValueError("后台设置包含不支持的字段")
    for value in values.values():
        if not isinstance(value, bool):
            raise ValueError("后台开关必须是布尔值")
    if "autostart" in values:
        configure_autostart(root, values["autostart"])
    set_desired(root, **values)
    if settings(root)["enabled"]:
        ensure_supervisor(root)
    return status(root)


def main() -> None:
    parser = argparse.ArgumentParser(description="Manage this installation's background supervisor.")
    parser.add_argument("action", choices=("stop-for-update", "ensure", "disable", "launch-workbench"))
    parser.add_argument("root", type=Path)
    parser.add_argument("--port", type=int)
    parser.add_argument("--run-id")
    args = parser.parse_args()
    if args.action == "stop-for-update":
        if paused(args.root) != "update":
            parser.exit(2, "An active update marker is required.\n")
        if request_stop_supervisor(args.root):
            print("stopped")
    elif args.action == "disable":
        set_desired(args.root, enabled=False, astrbot_desired=False)
    elif args.action == "launch-workbench":
        if args.port is None or not 1024 <= args.port <= 65535:
            parser.error("launch-workbench requires a valid --port")
        if not args.run_id or not args.run_id.replace("-", "").isalnum():
            parser.error("launch-workbench requires a valid --run-id")
        print(launch_workbench(args.root, args.port, f"workbench-{args.run_id}").pid)
    else:
        ensure_settings(args.root)
        ensure_supervisor(args.root, args.port)


if __name__ == "__main__":
    main()
