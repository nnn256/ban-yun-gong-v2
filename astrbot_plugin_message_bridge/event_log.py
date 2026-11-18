"""Persistent routing evidence without message bodies or authentication data."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path


class EventLog:
    def __init__(self, directory: Path) -> None:
        self.handler = RotatingFileHandler(Path(directory) / "events.jsonl", maxBytes=4 * 1024 * 1024, backupCount=7, encoding="utf-8", delay=True)

    def write(self, action: str, platform: str = "", success: bool | None = None, **fields) -> None:
        event = {"time": datetime.now(timezone.utc).isoformat(timespec="seconds"), "component": "bridge", "level": "ERROR" if success is False else "INFO", "action": action, "platform": platform}
        if success is not None:
            event["success"] = success
        event.update({key: str(value)[:200] for key, value in fields.items() if key in {"state", "chat_id", "message_id", "enabled", "active", "total"}})
        try:
            self.handler.emit(logging.LogRecord("bridge", logging.INFO, "", 0, json.dumps(event, ensure_ascii=False), (), None))
        except OSError:
            # Logging failures cannot stop delivery of an already received event.
            pass
        finally:
            # Status objects are replaced during reload and tests. Do not keep
            # a Windows file handle open outside a write operation.
            self.handler.close()
