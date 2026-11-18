"""Detached AstrBot owner and output collector, independent from the workbench."""
from __future__ import annotations

import argparse
import contextlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

import psutil

from log_store import append_log


def atomic_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
    try:
        temporary.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def identity(process: psutil.Process) -> dict:
    return {"pid": process.pid, "process_started_at": process.create_time()}


def same_process(record: dict) -> bool:
    try:
        process = psutil.Process(int(record["pid"]))
        return process.is_running() and abs(process.create_time() - float(record["process_started_at"])) < 1.1
    except (psutil.Error, OSError, ValueError, KeyError, TypeError):
        return False


def launched_by(pid: int, launcher_pid: int) -> bool:
    """Windows virtualenv launchers can own a separate Python interpreter PID."""
    if pid == launcher_pid:
        return True
    try:
        return any(parent.pid == launcher_pid for parent in psutil.Process(pid).parents())
    except (psutil.Error, OSError, ValueError):
        return False


def is_astrbot_command(command: list[str], executable: str | Path, expected_exe: str | Path) -> bool:
    """Recognize both the generated AstrBot launcher and python -m AstrBot."""
    if not command or "run" not in [part.lower() for part in command]:
        return False
    exe_name = Path(str(executable)).name.lower()
    if exe_name == "astrbot.exe":
        return Path(executable).resolve() == Path(expected_exe).resolve()
    if exe_name not in {"python.exe", "pythonw.exe"}:
        return False
    # The Windows console-script launcher passes its own absolute path to
    # the real interpreter, rather than using python -m astrbot.
    if len(command) > 1 and Path(command[1]).resolve() == Path(expected_exe).resolve():
        return True
    lowered = [part.lower().replace("/", "\\") for part in command]
    for index, part in enumerate(lowered[:-1]):
        if part == "-m" and lowered[index + 1] in {"astrbot", "astrbot.__main__"}:
            return True
    return any("\\site-packages\\astrbot\\__main__.py" in part for part in lowered)


def owned_runner(marker: dict, root: Path, script: Path) -> int | None:
    """Verify recorded PID, creation time, exact script and AstrBot working dir."""
    record = marker.get("runner", {})
    if not isinstance(record, dict) or not same_process(record):
        return None
    try:
        process = psutil.Process(int(record["pid"]))
        command = process.cmdline()
        if Path(process.cwd()).resolve() != Path(root).resolve():
            return None
        if not any(Path(part).resolve() == script.resolve() for part in command[1:] if part and not part.startswith("-")):
            return None
        return process.pid
    except (psutil.Error, OSError, ValueError):
        return None


def stop_owned_tree(pid: int, created_at: float, timeout: float = 10) -> None:
    if not same_process({"pid": pid, "process_started_at": created_at}):
        return
    try:
        process = psutil.Process(pid)
        children = process.children(recursive=True)
    except psutil.Error:
        return
    records = []
    for child in children:
        if child.pid == os.getpid():
            continue
        try:
            records.append((child, child.create_time()))
        except psutil.Error:
            continue
    records.append((process, created_at))
    for child, started_at in reversed(records):
        if not same_process({"pid": child.pid, "process_started_at": started_at}):
            continue
        with contextlib.suppress(psutil.Error):
            child.terminate()
    _, alive = psutil.wait_procs([child for child, _ in records], timeout=timeout)
    for child in alive:
        record = next((stamp for candidate, stamp in records if candidate.pid == child.pid), None)
        if same_process({"pid": child.pid, "process_started_at": record}):
            with contextlib.suppress(psutil.Error):
                child.kill()
    psutil.wait_procs(alive, timeout=5)


def capture(stream, log_path: Path, secret_path: Path | None = None) -> None:
    pattern = re.compile(r"Initial password:\s*(\S+)", re.IGNORECASE)
    for line in stream:
        line = line.rstrip("\r\n")
        match = pattern.search(line)
        if match:
            if secret_path is not None:
                try:
                    import win32crypt
                    encrypted = win32crypt.CryptProtectData(match.group(1).encode("utf-8"), "MoverV2 AstrBot bootstrap", None, None, None, 0)
                    secret_path.parent.mkdir(parents=True, exist_ok=True)
                    secret_path.write_bytes(encrypted)
                except Exception:
                    pass
            line = pattern.sub("Initial password: [在工作台本地显示]", line)
        append_log(log_path, line)


def run(root: Path, command: list[str], pid_file: Path, log_path: Path, secret_path: Path | None = None) -> int:
    root = root.resolve()
    own = identity(psutil.Process())
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    child = subprocess.Popen(command, cwd=root, stdin=subprocess.DEVNULL,
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             text=True, encoding="utf-8", errors="replace", bufsize=1,
                             creationflags=flags)
    child_record = identity(psutil.Process(child.pid))
    marker = {**child_record, "runner": own, "root": str(root), "state": "running"}
    atomic_json(pid_file, marker)
    try:
        capture(child.stdout, log_path, secret_path)
        code = child.wait()
        # The Windows console launcher normally waits for its Python child.
        # Explicitly remove any remaining descendants on unexpected exit.
        marker["state"] = "stopped"
        marker["exit_code"] = code
        atomic_json(pid_file, marker)
        return code
    finally:
        if child.poll() is None:
            stop_owned_tree(child.pid, child_record["process_started_at"], timeout=5)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--pid-file", type=Path, required=True)
    parser.add_argument("--log", type=Path, required=True)
    parser.add_argument("--secret", type=Path)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("Missing AstrBot command")
    try:
        return run(args.root, command, args.pid_file, args.log, args.secret)
    except Exception as exc:
        append_log(args.log, "ERROR AstrBot runner failed (" + type(exc).__name__ + ")")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
