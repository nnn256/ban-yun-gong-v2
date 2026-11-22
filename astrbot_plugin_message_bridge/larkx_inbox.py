"""Durable, local receive work for Feishu push messages and read-only pulls.

The database belongs to plugin runtime data, never a distributable package.
Completed work keeps only an opaque receipt; failed work remains available for
explicit retry instead of disappearing after the websocket has acknowledged it.
"""
from __future__ import annotations

from .build_identity import capture_module_build

MODULE_BUILD = capture_module_build(__file__)

import base64
from contextlib import closing
from dataclasses import dataclass
import hashlib
import json
import sqlite3
import threading
import time
from uuid import uuid4
from pathlib import Path
from typing import Any


RETRY_DELAYS = (2, 5, 15, 30, 60)
RECEIPT_TTL = 24 * 60 * 60
RECOVERY_EPOCH = "_mover_recovery_epoch"
RECOVERY_RETRY_DELAYS = (10, 30, 120, 600, 3600)


@dataclass(frozen=True)
class RecoveryCheckpoint:
    chat_id: str
    signature: str
    epoch: str
    position: int | None


def _encode(payload: Any) -> str:
    def binary(value: Any) -> dict[str, str]:
        if isinstance(value, (bytes, bytearray, memoryview)):
            return {"__mover_binary__": base64.b64encode(bytes(value)).decode("ascii")}
        raise TypeError("Unsupported Feishu inbox value")

    return json.dumps(payload, default=binary, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _decode(payload: str) -> Any:
    def binary(value: dict[str, Any]) -> Any:
        if set(value) == {"__mover_binary__"} and isinstance(value["__mover_binary__"], str):
            return base64.b64decode(value["__mover_binary__"], validate=True)
        return value

    return json.loads(payload, object_hook=binary)


class LarkXInbox:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(path, check_same_thread=False, timeout=5)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA synchronous=FULL")
        with self._connection:
            self._connection.execute("""
                CREATE TABLE IF NOT EXISTS inbox (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    receipt TEXT NOT NULL UNIQUE,
                    kind TEXT NOT NULL,
                    account_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    payload TEXT,
                    status TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    available_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    error_code TEXT NOT NULL DEFAULT ''
                )
            """)
            self._connection.execute("CREATE INDEX IF NOT EXISTS inbox_pending ON inbox(status, available_at, id)")
            self._connection.execute("CREATE INDEX IF NOT EXISTS inbox_done_age ON inbox(updated_at) WHERE status='done'")
            self._connection.execute("""
                CREATE TABLE IF NOT EXISTS recovery_chats (
                    account_id TEXT NOT NULL, user_id TEXT NOT NULL, chat_id TEXT NOT NULL,
                    signature TEXT NOT NULL, epoch TEXT NOT NULL UNIQUE, position INTEGER,
                    PRIMARY KEY (account_id, user_id, chat_id)
                )
            """)
            self._connection.execute("""
                CREATE TABLE IF NOT EXISTS recovery_missing (
                    epoch TEXT NOT NULL, position INTEGER NOT NULL,
                    attempts INTEGER NOT NULL, retry_at REAL NOT NULL,
                    PRIMARY KEY (epoch, position)
                )
            """)
            self._connection.execute("CREATE INDEX IF NOT EXISTS recovery_due ON recovery_missing(epoch,retry_at,position)")
            self._connection.execute("""
                CREATE TABLE IF NOT EXISTS recovery_observed (
                    epoch TEXT NOT NULL, position INTEGER NOT NULL,
                    PRIMARY KEY (epoch, position)
                )
            """)
            self._connection.execute("UPDATE inbox SET status='pending' WHERE status='processing'")

    def _insert(self, kind: str, payload: dict[str, Any], account_id: str, user_id: str, timestamp: float) -> bool:
        encoded = _encode(payload)
        if kind == "message" and (payload.get("msg_id") or payload.get("message_id")):
            identity = _encode([payload.get("chat_id"), payload.get("msg_id") or payload.get("message_id")])
        else:
            identity = encoded
        receipt = hashlib.sha256((str(account_id) + "\0" + str(user_id) + "\0" + kind + "\0" + identity).encode("utf-8")).hexdigest()
        cursor = self._connection.execute(
            "INSERT OR IGNORE INTO inbox(receipt,kind,account_id,user_id,payload,available_at,updated_at) VALUES(?,?,?,?,?,?,?)",
            (receipt, kind, str(account_id), str(user_id), encoded, timestamp, timestamp),
        )
        return cursor.rowcount == 1

    def enqueue(self, kind: str, payload: dict[str, Any], account_id: str, user_id: str,
                *, now: float | None = None) -> bool:
        timestamp = time.time() if now is None else now
        with self._lock, self._connection:
            self._connection.execute("DELETE FROM inbox WHERE status='done' AND updated_at < ?", (timestamp - RECEIPT_TTL,))
            inserted = self._insert(kind, payload, account_id, user_id, timestamp)
            position = payload.get("position")
            if (kind == "message" and type(position) is int and position > 0 and payload.get("chat_id")
                    and (payload.get("msg_id") or payload.get("message_id"))):
                scope = self._connection.execute(
                    "SELECT epoch,position FROM recovery_chats WHERE account_id=? AND user_id=? AND chat_id=?",
                    (str(account_id), str(user_id), str(payload["chat_id"])),
                ).fetchone()
                if scope and scope["position"] is not None:
                    if position > scope["position"]:
                        self._connection.execute("INSERT OR IGNORE INTO recovery_observed VALUES(?,?)", (scope["epoch"], position))
                    else:
                        self._connection.execute("DELETE FROM recovery_missing WHERE epoch=? AND position=?", (scope["epoch"], position))
            return inserted

    def sync_recovery_chats(self, account_id: str, user_id: str,
                            routes: dict[str, str]) -> dict[str, RecoveryCheckpoint]:
        """Discard disabled/changed scopes; a re-enabled route needs a fresh baseline."""
        with self._lock, self._connection:
            rows = self._connection.execute(
                "SELECT user_id,chat_id,signature,epoch,position FROM recovery_chats WHERE account_id=?",
                (account_id,),
            ).fetchall()
            result = {}
            for row in rows:
                chat = row["chat_id"]
                if row["user_id"] == user_id and routes.get(chat) == row["signature"]:
                    result[chat] = RecoveryCheckpoint(chat, row["signature"], row["epoch"], row["position"])
                else:
                    self._connection.execute("DELETE FROM recovery_chats WHERE epoch=?", (row["epoch"],))
                    self._connection.execute("DELETE FROM recovery_missing WHERE epoch=?", (row["epoch"],))
                    self._connection.execute("DELETE FROM recovery_observed WHERE epoch=?", (row["epoch"],))
            for chat, signature in routes.items():
                if chat not in result:
                    epoch = uuid4().hex
                    self._connection.execute("INSERT INTO recovery_chats VALUES(?,?,?,?,?,NULL)",
                                             (account_id, user_id, chat, signature, epoch))
                    result[chat] = RecoveryCheckpoint(chat, signature, epoch, None)
            return result

    def recovery_baseline(self, scope: RecoveryCheckpoint, position: int) -> bool:
        with self._lock, self._connection:
            cursor = self._connection.execute("UPDATE recovery_chats SET position=? WHERE epoch=? AND position IS NULL",
                                              (position, scope.epoch))
            return cursor.rowcount == 1

    def recovery_plan(self, scope: RecoveryCheckpoint, through: int, *, limit: int, now: float) -> list[int]:
        assert scope.position is not None
        with self._lock:
            received = {row[0] for row in self._connection.execute(
                "SELECT position FROM recovery_observed WHERE epoch=? AND position>? AND position<=?",
                (scope.epoch, scope.position, through),
            )}
            requested = [position for position in range(scope.position + 1, through + 1) if position not in received]
            gaps = self._connection.execute(
                "SELECT position FROM recovery_missing WHERE epoch=? AND retry_at<=? ORDER BY retry_at,position LIMIT ?",
                (scope.epoch, now, max(0, limit - len(requested))),
            )
            return sorted([*requested, *(row[0] for row in gaps)])

    def commit_recovery_page(self, scope: RecoveryCheckpoint, through: int, requested: list[int],
                             found: dict[int, dict | None], *, now: float) -> int | None:
        """Move the scan boundary only with an inbox record or a durable gap for every slot."""
        assert scope.position is not None
        with self._lock, self._connection:
            # Acquire the write transaction before using the scope. A second
            # connection may have advanced or replaced it during the API read.
            cursor = self._connection.execute("UPDATE recovery_chats SET position=? WHERE epoch=? AND position=?",
                                              (through, scope.epoch, scope.position))
            if cursor.rowcount != 1:
                return None
            row = self._connection.execute("SELECT * FROM recovery_chats WHERE epoch=?", (scope.epoch,)).fetchone()
            stored = 0
            for position in requested:
                if position in found:
                    message = found[position]
                    if message is not None:
                        stored += self._insert("message", {**message, RECOVERY_EPOCH: scope.epoch},
                                               row["account_id"], row["user_id"], now)
                    self._connection.execute("DELETE FROM recovery_missing WHERE epoch=? AND position=?",
                                             (scope.epoch, position))
                    continue
                if position <= scope.position:
                    gap = self._connection.execute("SELECT attempts FROM recovery_missing WHERE epoch=? AND position=?",
                                                   (scope.epoch, position)).fetchone()
                    if gap is None:
                        continue  # a live push filled this gap during the read
                    attempt = gap[0] + 1
                else:
                    if self._connection.execute("SELECT 1 FROM recovery_observed WHERE epoch=? AND position=?",
                                                (scope.epoch, position)).fetchone():
                        continue
                    attempt = 1
                delay = RECOVERY_RETRY_DELAYS[min(attempt, len(RECOVERY_RETRY_DELAYS)) - 1]
                self._connection.execute("INSERT OR REPLACE INTO recovery_missing VALUES(?,?,?,?)",
                                         (scope.epoch, position, attempt, now + delay))
            if through > scope.position:
                self._connection.execute("DELETE FROM recovery_observed WHERE epoch=? AND position<=?", (scope.epoch, through))
            return stored

    def recovery_counts(self, account_id: str, user_id: str) -> tuple[int, int]:
        with self._lock:
            baselined = self._connection.execute(
                "SELECT COUNT(*) FROM recovery_chats WHERE account_id=? AND user_id=? AND position IS NOT NULL",
                (account_id, user_id),
            ).fetchone()[0]
            missing = self._connection.execute(
                "SELECT COUNT(*) FROM recovery_missing JOIN recovery_chats USING(epoch) WHERE account_id=? AND user_id=?",
                (account_id, user_id),
            ).fetchone()[0]
            return baselined, missing

    def claim(self, *, account_id: str | None = None, user_id: str | None = None,
              now: float | None = None) -> dict[str, Any] | None:
        timestamp = time.time() if now is None else now
        with self._lock, self._connection:
            identity = ""
            values: list[Any] = [timestamp]
            if account_id is not None:
                identity += " AND account_id=?"
                values.append(account_id)
            if user_id is not None:
                identity += " AND user_id=?"
                values.append(user_id)
            row = self._connection.execute(
                "SELECT * FROM inbox WHERE status='pending' AND available_at <= ?" + identity + " ORDER BY id LIMIT 1", values,
            ).fetchone()
            if row is None:
                return None
            self._connection.execute(
                "UPDATE inbox SET status='processing',attempts=attempts+1,updated_at=? WHERE id=?", (timestamp, row["id"]),
            )
            result = dict(row)
            result["attempts"] += 1
            result["payload"] = _decode(result["payload"])
            return result

    def complete(self, item_id: int) -> None:
        with self._lock, self._connection:
            self._connection.execute("UPDATE inbox SET status='done',payload=NULL,error_code='',updated_at=? WHERE id=?",
                                     (time.time(), item_id))

    def update_payload(self, item_id: int, payload: dict[str, Any]) -> None:
        with self._lock, self._connection:
            self._connection.execute("UPDATE inbox SET payload=?,updated_at=? WHERE id=?", (_encode(payload), time.time(), item_id))

    def retry(self, item: dict[str, Any], error_code: str, *, now: float | None = None) -> bool:
        """Schedule later work without keeping subsequent messages behind it."""
        timestamp = time.time() if now is None else now
        attempt = item["attempts"]
        pending = attempt <= len(RETRY_DELAYS)
        delay = RETRY_DELAYS[attempt - 1] if pending else 0
        with self._lock, self._connection:
            self._connection.execute(
                "UPDATE inbox SET status=?,available_at=?,updated_at=?,error_code=? WHERE id=?",
                ("pending" if pending else "failed", timestamp + delay, timestamp, error_code, item["id"]),
            )
        return pending

    def release(self, item_id: int, *, undo_attempt: bool = False) -> None:
        with self._lock, self._connection:
            self._connection.execute("UPDATE inbox SET status='pending',attempts=MAX(0,attempts-?) WHERE id=? AND status='processing'",
                                     (1 if undo_attempt else 0, item_id))

    def defer(self, item_id: int, error_code: str, *, delay: float = 5, now: float | None = None) -> None:
        """Keep known pre-send gates out of the finite delivery retry budget."""
        timestamp = time.time() if now is None else now
        if error_code not in {"consent_required", "password_change_required", "auth_required", "unavailable"}:
            raise ValueError("Unsupported send gate")
        with self._lock, self._connection:
            self._connection.execute(
                "UPDATE inbox SET status='pending',attempts=MAX(0,attempts-1),available_at=?,updated_at=?,error_code=? "
                "WHERE id=? AND status='processing'",
                (timestamp + min(60, max(1, delay)), timestamp, error_code, item_id),
            )

    def retry_failed(self) -> int:
        with self._lock, self._connection:
            cursor = self._connection.execute(
                "UPDATE inbox SET status='pending',attempts=0,available_at=?,error_code='' WHERE status='failed'", (time.time(),),
            )
            return cursor.rowcount

    def stats(self) -> dict[str, int]:
        with self._lock:
            result = {"pending": 0, "processing": 0, "failed": 0, "done": 0}
            result.update({row[0]: row[1] for row in self._connection.execute("SELECT status,COUNT(*) FROM inbox GROUP BY status")})
            return result

    def close(self) -> None:
        with self._lock:
            self._connection.close()


def read_inbox_stats(path: Path) -> dict[str, int]:
    """Read from a workbench process without resetting a running consumer."""
    result = {"pending": 0, "processing": 0, "failed": 0, "done": 0}
    if not path.is_file():
        return result
    with closing(sqlite3.connect(path, timeout=5)) as connection:
        result.update(dict(connection.execute("SELECT status,COUNT(*) FROM inbox GROUP BY status")))
    return result


def retry_failed_inbox(path: Path) -> int:
    if not path.is_file():
        return 0
    with closing(sqlite3.connect(path, timeout=5)) as connection:
        with connection:
            cursor = connection.execute(
                "UPDATE inbox SET status='pending',attempts=0,available_at=?,error_code='' WHERE status='failed'", (time.time(),),
            )
            return cursor.rowcount
