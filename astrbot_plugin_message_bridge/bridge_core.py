"""Small, AstrBot-independent routing helpers for the message bridge."""

from __future__ import annotations

from collections import OrderedDict
from copy import copy
from dataclasses import dataclass
from hashlib import sha256
from time import monotonic
from typing import Any, Mapping


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
    """Classify AstrBot components for per-route content filtering."""
    hint = getattr(component, "_mover_content_type", "")
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
        filename = str(getattr(component, "name", "") or getattr(component, "file", "") or "")
        filename = filename.split("?", 1)[0].split("#", 1)[0].lower()
        suffix = filename.rsplit(".", 1)[-1] if "." in filename else ""
        if suffix in {"mp4", "m4v", "mov", "webm", "mkv", "avi", "3gp", "flv", "wmv"}:
            return "video"
        if suffix in {"silk", "wav", "mp3", "m4a", "ogg", "opus", "amr", "flac", "aac"}:
            return "audio"
        return "file"
    return "text"


def filter_message_chain(chain: Any, content_types: tuple[str, ...] | list[str] | set[str]) -> Any:
    allowed = frozenset(content_types)
    filtered = copy(chain)
    filtered.chain = [
        component for component in getattr(chain, "chain", [])
        if component_content_type(component) in allowed
    ]
    return filtered


def route_event_key(event_key: str, route: Route) -> str:
    """Deduplicate each destination independently so one failed route can retry."""
    material = "\0".join((
        event_key,
        route.source_platform,
        route.source_platform_id,
        route.source_account_id,
        route.source_message_type,
        route.source_session_id,
        route.target_account_id,
        route.target_umo,
    ))
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

    def __init__(self, ttl_seconds: float = 600, max_entries: int = 5000) -> None:
        self.ttl_seconds = ttl_seconds
        self.max_entries = max_entries
        self._ids: OrderedDict[str, float] = OrderedDict()

    def _prune(self) -> None:
        cutoff = monotonic() - self.ttl_seconds
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
            for key in keys:
                self._ids[key] = monotonic()
                self._ids.move_to_end(key)
            self._prune()

    def contains(self, platform_id: str, message_id: str, account_id: str = "", session_id: str = "") -> bool:
        self._prune()
        key = f"{platform_id}:{account_id}:{session_id}:{message_id}"
        cross_account_key = f"{platform_id}:*:{session_id}:{message_id}"
        return bool(message_id) and (key in self._ids or (bool(session_id) and cross_account_key in self._ids))


class SeenEvents:
    """Bounded in-memory event de-duplication with a monotonic TTL."""

    def __init__(self, ttl_seconds: float = 600, max_entries: int = 5000) -> None:
        self.ttl_seconds = ttl_seconds
        self.max_entries = max_entries
        self._seen: OrderedDict[str, float] = OrderedDict()

    def accept(self, key: str, now: float | None = None) -> bool:
        current = monotonic() if now is None else now
        cutoff = current - self.ttl_seconds
        while self._seen:
            oldest_key, oldest_at = next(iter(self._seen.items()))
            if oldest_at > cutoff:
                break
            self._seen.pop(oldest_key)
        if key in self._seen:
            self._seen.move_to_end(key)
            return False
        self._seen[key] = current
        while len(self._seen) > self.max_entries:
            self._seen.popitem(last=False)
        return True

    def discard(self, key: str) -> None:
        self._seen.pop(key, None)


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

