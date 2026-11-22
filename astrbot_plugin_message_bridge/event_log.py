"""Persistent routing evidence without message bodies or authentication data."""
from __future__ import annotations

from .build_identity import capture_module_build

MODULE_BUILD = capture_module_build(__file__)

import json
import logging
import re
import threading
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path


_compat_logger = None
_compat_lock = threading.RLock()

_TRACE_STATES = {
    "larkx_ingress": ("persisted", "duplicate", "persist_failed"),
    "larkx_qq_route": ("matched", "no_route", "filtered", "echo", "disabled"),
    "larkx_qq_delivery": (
        "confirmed", "rejected", "uncertain", "reused_confirmed", "reused_uncertain", "duplicate", "filtered", "prep_failed",
    ),
    "larkx_history": ("baseline", "recovered", "gap"),
}
_RECOVERY_STATES = (
    "idle", "checking", "baseline", "recovered", "current", "gap", "error",
    "disabled", "unavailable", "waiting", "offline", "no_routes", "degraded", "catching_up", "ready",
)


def _bounded_count(value) -> int:
    return max(0, min(value, 1_000_000)) if type(value) is int else 0


def safe_larkx_recovery_status(raw: dict) -> dict:
    """Copy only aggregate recovery evidence to the local status API."""
    raw = raw if isinstance(raw, dict) else {}
    state = raw.get("state")
    checked = raw.get("checked_at")
    checked_at = ""
    try:
        if type(checked) is int and checked > 0:
            checked_at = datetime.fromtimestamp(checked, timezone.utc).isoformat(timespec="seconds")
        elif isinstance(checked, str) and len(checked) <= 40:
            stamp = datetime.fromisoformat(checked.replace("Z", "+00:00"))
            if stamp.tzinfo is not None:
                checked_at = stamp.astimezone(timezone.utc).isoformat(timespec="seconds")
    except (ValueError, OverflowError, OSError):
        pass
    return {
        "state": state if state in _RECOVERY_STATES else "idle",
        "checked_at": checked_at,
        **{key: _bounded_count(raw.get(key)) for key in ("configured", "baselined", "missing", "recovered", "errors")},
    }


def _compat_log_record(record: dict) -> None:
    """Supply AstrBot's sink fields for third-party raw Loguru records."""
    extra = record["extra"]
    level = record["level"].name
    extra.setdefault("plugin_tag", "[LarkAgentX]" if str(record.get("name", "")).startswith("larkx") else "[Core]")
    extra.setdefault("short_levelname", {"WARNING": "WARN", "CRITICAL": "CRIT"}.get(level, level))
    extra.setdefault("astrbot_version_tag", "")
    extra.setdefault("source_file", record["file"].name)
    extra.setdefault("source_line", record["line"])
    extra.setdefault("trace_tag", "")
    extra.setdefault("is_trace", False)


def install_loguru_compat():
    """Patch future Loguru imports without replacing host sinks or patchers.

    AstrBot patches its own logger wrapper, while LarkAgentX imports the raw
    module logger. Both share the sink collection, whose formats require these
    fields. A public ``patch`` wrapper preserves existing host patchers and
    bound extra fields; assigning it before importing LarkAgentX covers those
    records without changing AstrBot's logger or global core configuration.
    """
    global _compat_logger
    import loguru

    with _compat_lock:
        if loguru.logger is not _compat_logger:
            _compat_logger = loguru.logger.patch(_compat_log_record)
            loguru.logger = _compat_logger
        return _compat_logger


class EventLog:
    def __init__(self, directory: Path) -> None:
        self.handler = RotatingFileHandler(Path(directory) / "events.jsonl", maxBytes=4 * 1024 * 1024, backupCount=7, encoding="utf-8", delay=True)
        self._lock = threading.RLock()

    def write(self, action: str, platform: str = "", success: bool | None = None, **fields) -> None:
        event = {"time": datetime.now(timezone.utc).isoformat(timespec="seconds"), "component": "bridge", "level": "ERROR" if success is False else "INFO", "action": action, "platform": platform}
        if success is not None:
            event["success"] = success
        if action in _TRACE_STATES:
            state = fields.get("state")
            if state not in _TRACE_STATES[action]:
                return
            # The new trace actions never use the generic detail allowlist;
            # each field has a fixed format even if its caller makes a mistake.
            history = action == "larkx_history"
            content_type = fields.get("content_type", "image")
            if not history and content_type != "image":
                return
            event.pop("success", None)
            event.update(platform="qq" if action.startswith("larkx_qq_") else "feishu",
                         state=state, count=_bounded_count(fields.get("count")),
                         level=("ERROR" if state in ("persist_failed", "rejected", "prep_failed") else
                                "WARNING" if state in ("uncertain", "reused_uncertain", "gap") else "INFO"))
            if not history:
                event["content_type"] = content_type
            for key in ("source_ref", "target_ref"):
                value = fields.get(key)
                if isinstance(value, str) and re.fullmatch(r"[a-f0-9]{16}", value):
                    event[key] = value
        else:
            allowed = {"state", "chat_id", "message_id", "enabled", "active", "total",
                       "attempt", "error_type", "error_code", "http_status", "source_message_id",
                       "part_id", "event_id", "target", "target_key", "content_type",
                       "pending", "failed", "completed", "parts", "queue_size"}
            event.update({key: str(value)[:200] for key, value in fields.items() if key in allowed})
        with self._lock:
            try:
                Path(self.handler.baseFilename).parent.mkdir(parents=True, exist_ok=True)
                self.handler.emit(logging.LogRecord("bridge", logging.INFO, "", 0, json.dumps(event, ensure_ascii=False), (), None))
            except OSError:
                # Logging failures cannot stop delivery of an already received event.
                pass
            finally:
                # Status objects are replaced during reload and tests. Do not keep
                # a Windows file handle open outside a write operation.
                self.handler.close()
