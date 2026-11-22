"""Small, AstrBot-independent routing helpers for the message bridge."""

from __future__ import annotations

from .build_identity import capture_module_build

MODULE_BUILD = capture_module_build(__file__)

from collections import OrderedDict
from contextlib import closing, contextmanager
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
import sqlite3
from threading import RLock
from time import monotonic, time as wall_time
from typing import Any, Iterable, Mapping


PLUGIN_ID = "astrbot_plugin_message_bridge"
BRIDGE_MARKER = "[搬运工V2]"
DEFAULT_MANAGED_PLATFORMS = ("aiocqhttp", "larkx")
VALID_MESSAGE_TYPES = frozenset(("GroupMessage", "FriendMessage", "OtherMessage"))
CONTENT_TYPE_ORDER = ("text", "emoji", "image", "video", "audio", "file")
DEFAULT_CONTENT_TYPES = CONTENT_TYPE_ORDER


@dataclass(frozen=True)
class Route:
    name: str
    source_platform: str
    source_session_id: str
    target_umo: str
    source_platform_id: str = ""
    source_message_type: str = "GroupMessage"
    source_account_id: str = ""
    target_account_id: str = ""
    include_self: bool = False
    content_types: tuple[str, ...] = DEFAULT_CONTENT_TYPES

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "Route | None":
        name = str(value.get("name", "unnamed route")).strip()
        source_platform = str(value.get("source_platform", "")).strip()
        source_session_id = str(value.get("source_session_id", "")).strip()
        source_platform_id = str(value.get("source_platform_id", "")).strip()
        source_message_type = str(value.get("source_message_type", "GroupMessage")).strip()
        source_account_id = str(value.get("source_account_id", "")).strip()
        target_account_id = str(value.get("target_account_id", "")).strip()
        target_umo = str(value.get("target_umo", "")).strip()
        raw_content_types = value.get("content_types")
        content_types = (
            DEFAULT_CONTENT_TYPES
            if raw_content_types is None
            else tuple(kind for kind in CONTENT_TYPE_ORDER if kind in raw_content_types)
            if isinstance(raw_content_types, (list, tuple, set, frozenset))
            else ()
        )
        if not value.get("enabled", True):
            return None
        if source_platform not in DEFAULT_MANAGED_PLATFORMS:
            return None
        if not source_session_id or source_message_type not in VALID_MESSAGE_TYPES:
            return None
        if not valid_umo(target_umo):
            return None
        return cls(
            name=name,
            source_platform=source_platform,
            source_session_id=source_session_id,
            target_umo=target_umo,
            source_platform_id=source_platform_id,
            source_message_type=source_message_type,
            source_account_id=source_account_id,
            target_account_id=target_account_id,
            include_self=bool(value.get("include_self", False)),
            content_types=content_types,
        )

    def matches(
        self,
        platform: str,
        platform_id: str,
        message_type: str,
        session_id: str,
        account_id: str = "",
    ) -> bool:
        return (
            self.source_platform == platform
            and (not self.source_platform_id or self.source_platform_id == platform_id)
            and self.source_message_type == message_type
            and self.source_session_id == session_id
            and (not self.source_account_id or self.source_account_id == account_id)
        )


def valid_umo(value: str) -> bool:
    """Check AstrBot's platform_id:message_type:session_id session format."""
    if not value or value.count(":") < 2:
        return False
    platform_id, message_type, session_id = value.split(":", 2)
    return bool(platform_id and session_id and message_type in VALID_MESSAGE_TYPES)


def component_content_type(component: Any) -> str:
    hint = getattr(component, "mover_content_type", "") or getattr(component, "_mover_content_type", "")
    if hint in CONTENT_TYPE_ORDER:
        return hint
    name = type(component).__name__.lower()
    if name in {"face", "mface", "sticker"}:
        return "emoji"
    if name in {"plain", "at", "text", "reply"}:
        return "text"
    if name in {"record", "audio", "voice"}:
        return "audio"
    if name == "video":
        return "video"
    if name == "image":
        return "image"
    if name == "file":
        filename = str(getattr(component, "name", "") or getattr(component, "file", "") or "").split("?", 1)[0].lower()
        suffix = filename.rsplit(".", 1)[-1] if "." in filename else ""
        if suffix in {"mp4", "m4v", "mov", "webm", "mkv", "avi", "3gp", "flv", "wmv"}:
            return "video"
        if suffix in {"silk", "wav", "mp3", "m4a", "ogg", "opus", "amr", "flac", "aac"}:
            return "audio"
        return "file"
    return "text"


def filter_message_chain(chain: Any, content_types: tuple[str, ...] | list[str] | set[str]) -> Any:
    from copy import copy
    filtered = copy(chain)
    filtered.chain = [component for component in getattr(chain, "chain", []) if component_content_type(component) in frozenset(content_types)]
    return filtered


def route_event_key(event_key: str, route: Route) -> str:
    material = "\0".join((event_key, route.source_platform, route.source_platform_id, route.source_account_id, route.source_message_type, route.source_session_id, route.target_account_id, route.target_umo))
    return sha256(material.encode("utf-8", errors="replace")).hexdigest()


class BridgeRouter:
    def __init__(self, config: Mapping[str, Any]) -> None:
        configured_platforms = config.get("managed_platforms", DEFAULT_MANAGED_PLATFORMS)
        if not isinstance(configured_platforms, (list, tuple, set)):
            configured_platforms = DEFAULT_MANAGED_PLATFORMS
        self.managed_platforms = frozenset(
            str(name).strip() for name in configured_platforms if str(name).strip()
        )
        self.enabled = bool(config.get("bridge_enabled", True))
        parsed_routes = []
        configured_routes = config.get("rules", [])
        if not isinstance(configured_routes, list):
            configured_routes = []
        for raw_route in configured_routes:
            if isinstance(raw_route, Mapping):
                route = Route.from_mapping(raw_route)
                if route:
                    parsed_routes.append(route)
        self.routes = tuple(parsed_routes)

    def find_route(
        self,
        platform: str,
        platform_id: str,
        message_type: str,
        session_id: str,
        account_id: str = "",
    ) -> Route | None:
        for route in self.routes:
            if route.matches(platform, platform_id, message_type, session_id, account_id):
                return route
        return None

    def find_routes(
        self,
        platform: str,
        platform_id: str,
        message_type: str,
        session_id: str,
        account_id: str = "",
    ) -> list[Route]:
        """Return each configured destination for the current source account."""
        return [
            route for route in self.routes
            if route.matches(platform, platform_id, message_type, session_id, account_id)
        ]


class SentMessageIds:
    """Track transport receipts so forwarded content needs no visible marker."""

    def __init__(self, ttl_seconds: float = 600, max_entries: int = 5000, path: str | Path | None = None) -> None:
        self.ttl_seconds = ttl_seconds
        self.max_entries = max_entries
        self._ids: OrderedDict[str, float] = OrderedDict()
        self._path = Path(path) if path is not None else None
        self._lock = RLock()
        if self._path is not None:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._store() as connection:
                connection.execute("CREATE TABLE IF NOT EXISTS sent_receipts (receipt_key TEXT PRIMARY KEY, sent_at REAL NOT NULL)")
                connection.execute("CREATE INDEX IF NOT EXISTS sent_receipts_sent_at ON sent_receipts (sent_at)")
                self._prune_store(connection, wall_time())
                self._ids.update(connection.execute("SELECT receipt_key,sent_at FROM sent_receipts ORDER BY sent_at,rowid"))

    @contextmanager
    def _store(self):
        connection = sqlite3.connect(self._path, timeout=10)
        try:
            with connection:
                connection.execute("BEGIN IMMEDIATE")
                yield connection
        finally:
            connection.close()

    def _prune_store(self, connection: sqlite3.Connection, current: float) -> None:
        connection.execute("DELETE FROM sent_receipts WHERE sent_at <= ?", (current - self.ttl_seconds,))
        excess = connection.execute("SELECT COUNT(*) FROM sent_receipts").fetchone()[0] - self.max_entries
        if excess > 0:
            connection.execute(
                "DELETE FROM sent_receipts WHERE receipt_key IN (SELECT receipt_key FROM sent_receipts ORDER BY sent_at,rowid LIMIT ?)",
                (excess,),
            )

    def _prune(self) -> None:
        cutoff = (wall_time() if self._path is not None else monotonic()) - self.ttl_seconds
        while self._ids:
            key, at = next(iter(self._ids.items()))
            if at > cutoff and len(self._ids) <= self.max_entries:
                break
            self._ids.pop(key)

    def add(self, platform_id: str, message_id: str, account_id: str = "", session_id: str = "") -> None:
        if message_id:
            keys = [f"{platform_id}:{account_id}:{session_id}:{message_id}"]
            if session_id:
                keys.append(f"{platform_id}:*:{session_id}:{message_id}")
            current = wall_time() if self._path is not None else monotonic()
            with self._lock:
                if self._path is not None:
                    with self._store() as connection:
                        connection.executemany("INSERT OR REPLACE INTO sent_receipts(receipt_key,sent_at) VALUES (?,?)",
                                               [(key, current) for key in keys])
                        self._prune_store(connection, current)
                for key in keys:
                    self._ids[key] = current
                    self._ids.move_to_end(key)
                self._prune()

    def contains(self, platform_id: str, message_id: str, account_id: str = "", session_id: str = "") -> bool:
        if not message_id:
            return False
        key = f"{platform_id}:{account_id}:{session_id}:{message_id}"
        cross_account_key = f"{platform_id}:*:{session_id}:{message_id}"
        with self._lock:
            if self._path is not None:
                with closing(sqlite3.connect(self._path, timeout=10)) as connection:
                    keys = (key, cross_account_key) if session_id else (key, key)
                    return connection.execute(
                        "SELECT 1 FROM sent_receipts WHERE receipt_key IN (?,?) AND sent_at > ? LIMIT 1",
                        (*keys, wall_time() - self.ttl_seconds),
                    ).fetchone() is not None
            self._prune()
            return key in self._ids or (bool(session_id) and cross_account_key in self._ids)


class SeenEvents:
    """Bounded event de-duplication, optionally retained across restarts.

    In-memory instances use monotonic time. A persistent instance uses wall
    time because a monotonic timestamp cannot be compared across reboots.
    Store failures propagate instead of silently losing duplicate protection.
    """

    def __init__(self, ttl_seconds: float = 600, max_entries: int = 5000, path: str | Path | None = None) -> None:
        self.ttl_seconds = ttl_seconds
        self.max_entries = max_entries
        self._seen: OrderedDict[str, float] = OrderedDict()
        self._path = Path(path) if path is not None else None
        self._lock = RLock()
        if self._path is not None:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._store() as connection:
                connection.execute("CREATE TABLE IF NOT EXISTS seen_events (event_key TEXT PRIMARY KEY, accepted_at REAL NOT NULL, state TEXT NOT NULL DEFAULT 'sent')")
                columns = {row[1] for row in connection.execute("PRAGMA table_info(seen_events)")}
                if "state" not in columns:
                    # Earlier versions only persisted a key after a send
                    # attempt. Its result cannot safely be inferred on upgrade.
                    connection.execute("ALTER TABLE seen_events ADD COLUMN state TEXT NOT NULL DEFAULT 'uncertain'")
                # A process may stop between reserving a key and receiving a
                # send response. Surface this condition for manual checking.
                connection.execute("UPDATE seen_events SET state='uncertain' WHERE state='reserved'")
                connection.execute("CREATE INDEX IF NOT EXISTS seen_events_accepted_at ON seen_events (accepted_at)")
                self._expire_store(connection, wall_time())
                self._prune_store_capacity(connection)

    @contextmanager
    def _store(self):
        # Do not keep file handles open: workbench upgrades and Windows
        # backups can copy this small database when no operation is running.
        connection = sqlite3.connect(self._path, timeout=10)
        try:
            with connection:
                connection.execute("BEGIN IMMEDIATE")
                yield connection
        finally:
            connection.close()

    def _expire_store(self, connection: sqlite3.Connection, current: float) -> None:
        connection.execute("DELETE FROM seen_events WHERE accepted_at <= ?", (current - self.ttl_seconds,))

    def _prune_store_capacity(self, connection: sqlite3.Connection,
                              protected_keys: frozenset[str] = frozenset()) -> None:
        count = connection.execute("SELECT COUNT(*) FROM seen_events").fetchone()[0]
        excess = count - self.max_entries
        if excess > 0:
            # A single outbound message may own several reservations. Never
            # report those as reserved and then evict the beginning of that
            # same message while bounding the receipt cache.
            candidates = connection.execute("SELECT event_key FROM seen_events ORDER BY accepted_at, rowid")
            removed = []
            for (key,) in candidates:
                if key not in protected_keys:
                    removed.append((key,))
                    if len(removed) == excess:
                        break
            connection.executemany("DELETE FROM seen_events WHERE event_key = ?", removed)

    def _prune_memory(self, current: float) -> None:
        cutoff = current - self.ttl_seconds
        while self._seen:
            oldest_key, oldest_at = next(iter(self._seen.items()))
            if oldest_at > cutoff:
                break
            self._seen.pop(oldest_key)

    def accept(self, key: str, now: float | None = None) -> bool:
        """Reserve one event, retaining the original single-key interface."""
        return bool(self.reserve_many([key], now=now))

    def reserve_many(self, keys: Iterable[str], now: float | None = None) -> list[str]:
        """Atomically reserve missing leaves of one outbound message.

        Existing sent, uncertain and active reservations all remain protected.
        The returned keys are unique and retain input order. A caller must send
        only those returned leaves and finish or release that entire set.
        The database is the only authority for persistent instances. Database
        failures roll back every insert and prune in the batch.
        A batch larger than the cache capacity is rejected rather than partly
        reserving it or returning keys that have already been evicted.
        """
        ordered = list(dict.fromkeys(keys))
        if not ordered:
            return []
        current = (wall_time() if self._path is not None else monotonic()) if now is None else now
        with self._lock:
            if self._path is not None:
                with self._store() as connection:
                    self._expire_store(connection, current)
                    accepted = []
                    for key in ordered:
                        if connection.execute(
                            "INSERT OR IGNORE INTO seen_events (event_key, accepted_at, state) VALUES (?, ?, 'reserved')", (key, current)
                        ).rowcount == 1:
                            accepted.append(key)
                    if len(accepted) > self.max_entries:
                        raise ValueError("Receipt batch exceeds cache capacity")
                    if accepted:
                        self._prune_store_capacity(connection, frozenset(accepted))
                return accepted
            else:
                cutoff = current - self.ttl_seconds
                updated = OrderedDict((key, at) for key, at in self._seen.items() if at > cutoff)
                accepted = [key for key in ordered if key not in updated]
                if len(accepted) > self.max_entries:
                    raise ValueError("Receipt batch exceeds cache capacity")
                updated.update((key, current) for key in accepted)
                protected = frozenset(accepted)
                for key in list(updated):
                    if len(updated) <= self.max_entries:
                        break
                    if key not in protected:
                        updated.pop(key)
            self._seen = updated
            return accepted

    def contains(self, key: str, now: float | None = None) -> bool:
        """Check a receipt without reserving it or extending its lifetime.

        Callers can avoid downloading an already delivered attachment before
        using accept() to reserve it immediately before a real send attempt.
        """
        current = (wall_time() if self._path is not None else monotonic()) if now is None else now
        with self._lock:
            if self._path is not None:
                connection = sqlite3.connect(self._path, timeout=10)
                try:
                    return connection.execute(
                        "SELECT 1 FROM seen_events WHERE event_key = ? AND accepted_at > ?",
                        (key, current - self.ttl_seconds),
                    ).fetchone() is not None
                finally:
                    connection.close()
            self._prune_memory(current)
            return key in self._seen

    def finish(self, key: str, *, uncertain: bool = False) -> None:
        """Record a send result without releasing its duplicate protection.

        A reservation that never reaches finish() is reported as uncertain
        after a crash. Failures known to be unsent should use discard().
        """
        self.finish_many([key], uncertain=uncertain)

    def finish_many(self, keys: Iterable[str], *, uncertain: bool = False) -> None:
        """Commit one send result for all batch members, without new receipts."""
        ordered = list(dict.fromkeys(keys))
        if ordered and self._path is not None:
            with self._lock, self._store() as connection:
                connection.executemany("UPDATE seen_events SET state = ? WHERE event_key = ?",
                                       [("uncertain" if uncertain else "sent", key) for key in ordered])

    def discard(self, key: str) -> None:
        self.discard_many([key])

    def discard_many(self, keys: Iterable[str]) -> None:
        """Release an explicitly unsent batch atomically for a later retry."""
        ordered = list(dict.fromkeys(keys))
        if not ordered:
            return
        with self._lock:
            if self._path is not None:
                with self._store() as connection:
                    connection.executemany("DELETE FROM seen_events WHERE event_key = ?", [(key,) for key in ordered])
            else:
                for key in ordered:
                    self._seen.pop(key, None)


def read_delivery_stats(path: str | Path, ttl_seconds: float = 7 * 86400) -> dict[str, int]:
    """Read privacy-safe receipt counts without modifying a live database."""
    path = Path(path).resolve()
    result = {"sent": 0, "uncertain": 0}
    if not path.is_file():
        return result
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=5)) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(seen_events)")}
        if not columns:
            return result
        if "state" not in columns:
            # Legacy receipts did not distinguish success from an unfinished
            # attempt; do not label these as confirmed success.
            result["uncertain"] = connection.execute(
                "SELECT COUNT(*) FROM seen_events WHERE accepted_at > ?", (wall_time() - ttl_seconds,)
            ).fetchone()[0]
            return result
        for state, count in connection.execute(
                "SELECT state, COUNT(*) FROM seen_events WHERE accepted_at > ? GROUP BY state", (wall_time() - ttl_seconds,)):
            result["sent" if state == "sent" else "uncertain"] += count
    return result


def make_event_key(
    platform_id: str,
    session_id: str,
    message_id: str,
    sender_id: str,
    message: str,
    timestamp: Any,
) -> str:
    if message_id:
        return f"{platform_id}:{session_id}:{message_id}"
    material = "\0".join(
        (platform_id, session_id, sender_id, str(timestamp or ""), message)
    )
    return sha256(material.encode("utf-8", errors="replace")).hexdigest()

