"""Import-time code identities; no account, message, credential or path export."""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import re
import sys
import time
import uuid
from typing import Any


BRIDGE_VERSION = "2.0.16"
CODE_FILES = (
    "build_identity.py", "bridge_core.py", "event_log.py", "larkx_bridge.py",
    "larkx_history.py", "larkx_inbox.py", "larkx_media.py", "larkx_merge.py", "larkx_receive.py",
    "larkx_richtext.py", "main.py", "qq_emojis.py", "qq_transport.py",
    "runtime_status.py", "snowluma_watchdog.py",
)
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_VERSION = re.compile(r"\d{1,4}\.\d{1,4}\.\d{1,4}\Z")


def capture_module_build(filename: str | Path) -> str:
    """Call while the module is importing, never to describe a running module later."""
    try:
        return hashlib.sha256(Path(filename).read_bytes()).hexdigest()
    except OSError:
        return ""


def fingerprint(modules: dict[str, str]) -> str:
    encoded = json.dumps(modules, sort_keys=True, separators=(",", ":")).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


MODULE_BUILD = capture_module_build(__file__)


def loaded_modules(package: str, main_build: str) -> dict[str, str]:
    """Snapshot dependencies already imported by main, including retained old modules."""
    result = {}
    for filename in CODE_FILES:
        module = sys.modules.get(f"{package}.{filename[:-3]}")
        result[filename] = str(getattr(module, "MODULE_BUILD", ""))
    result["main.py"] = main_build
    return result


def expected_modules(directory: Path, filenames=CODE_FILES) -> dict[str, str]:
    return {name: capture_module_build(directory / name) for name in filenames}


def runtime_identity(modules: dict[str, str] | None = None) -> dict[str, Any]:
    import psutil

    try:
        created = psutil.Process().create_time()
    except (psutil.Error, OSError):
        created = 0.0
    snapshot = dict(modules or {})
    return {"schema_version": 1, "version": BRIDGE_VERSION, "process_id": os.getpid(),
            "process_started_at": created, "run_id": uuid.uuid4().hex,
            "loaded_at": time.time(), "running": True, "modules": snapshot,
            "modules_fingerprint": fingerprint(snapshot)}


def safe_runtime_identity(value: Any) -> dict[str, Any] | None:
    """Strictly bound file input and retain only fields produced by this module."""
    if not isinstance(value, dict) or type(value.get("schema_version")) is not int or value.get("schema_version") != 1:
        return None
    version = value.get("version")
    pid = value.get("process_id")
    run_id = value.get("run_id")
    modules = value.get("modules")
    if (not isinstance(version, str) or not _VERSION.fullmatch(version)
            or type(pid) is not int or not 0 < pid < 2**32
            or not isinstance(run_id, str) or not re.fullmatch(r"[0-9a-f]{32}", run_id)
            or not isinstance(modules, dict) or set(modules) != set(CODE_FILES)
            or any(not isinstance(code_hash, str) or not _HASH.fullmatch(code_hash)
                   for code_hash in modules.values())):
        return None
    for key in ("process_started_at", "loaded_at"):
        item = value.get(key)
        if type(item) not in (int, float) or not 0 < item < 100_000_000_000 or not math.isfinite(item):
            return None
    digest = fingerprint(modules)
    if value.get("modules_fingerprint") != digest:
        return None
    return {"schema_version": 1, "version": version, "process_id": pid,
            "process_started_at": value["process_started_at"], "run_id": run_id,
            "loaded_at": value["loaded_at"], "running": value.get("running") is True,
            "modules": dict(modules), "modules_fingerprint": digest}


def process_matches(identity: dict[str, Any], root: Path) -> bool:
    import psutil

    try:
        process = psutil.Process(identity["process_id"])
        return (process.is_running()
                and abs(process.create_time() - identity["process_started_at"]) < 0.1
                and Path(process.cwd()).resolve() == root.resolve())
    except (psutil.Error, OSError, ValueError, TypeError, KeyError):
        return False
