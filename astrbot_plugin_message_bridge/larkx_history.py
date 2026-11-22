"""Read-only reconciliation of positions for Feishu conversations in active routes."""
from __future__ import annotations

import asyncio
import time
from typing import Any, Callable

from .build_identity import capture_module_build
from .larkx_inbox import LarkXInbox, RecoveryCheckpoint
from .larkx_merge import is_system_message
from .larkx_receive import normalize_entity


MODULE_BUILD = capture_module_build(__file__)
POLL_SECONDS = 20
BUSY_SECONDS = 2
CHAT_BATCH = 8
PAGE_SIZE = 50
FRESH_POSITIONS = 40  # leave capacity for holes in older pages even under sustained traffic


class PositionReadUnavailable(RuntimeError):
    pass


def _position(value: Any) -> int | None:
    if isinstance(value, str) and 0 < len(value) <= 10 and value.isascii() and value.isdecimal():
        value = int(value)
    return value if type(value) is int and 0 <= value <= 2**31 - 1 else None


def read_chat_positions(client: Any, chats: list[str]) -> dict[str, int]:
    raw = client.api("chats.PullChatsByIdsRequest", {"chatIds": chats})
    infos = raw.get("chats") if isinstance(raw, dict) else None
    if not isinstance(infos, dict):
        raise PositionReadUnavailable()
    result = {}
    for chat in chats:
        info = infos.get(chat)
        if not isinstance(info, dict) or str(info.get("id") or chat) != chat:
            continue
        position = _position(info.get("lastMessagePosition"))
        if position is not None:
            result[chat] = position
    return result


def read_message_positions(client: Any, chat: str, positions: list[int]) -> dict[int, dict | None]:
    raw = client.api("messages.PullMessagesByPositionsRequest", {"chatId": chat, "positions": positions})
    entities = raw.get("messages") if isinstance(raw, dict) else None
    if not isinstance(entities, dict):
        raise PositionReadUnavailable()
    found: dict[int, dict | None] = {}
    wanted = set(positions)
    seen_positions: set[int] = set()
    ids: dict[str, int] = {}
    for key, entity in entities.items():
        position = _position(key)
        if position is None or position not in wanted:
            continue
        if position in seen_positions:
            found.pop(position, None)
            continue
        seen_positions.add(position)
        if not isinstance(entity, dict):
            continue
        # Some entities omit chatId or position in a keyed response. A
        # conflicting explicit identity is never substituted with our request.
        reported_chat = entity.get("chatId") or entity.get("channelId")
        if reported_chat and str(reported_chat) != chat:
            continue
        if "position" in entity and _position(entity["position"]) != position:
            continue
        if (entity.get("isRemoved") is True or entity.get("isNoTraceRemoved") is True
                or entity.get("isVisibleV2") is False):
            found[position] = None
            continue
        try:
            message = normalize_entity(entity)
        except (TypeError, ValueError):
            continue
        if is_system_message(message):
            found[position] = None
            continue
        message_id = message.get("msg_id")
        if not isinstance(message_id, str) or not message_id or not message.get("from_id"):
            continue
        if message_id in ids:
            found.pop(ids[message_id], None)
            continue  # neither slot is safe to confirm
        ids[message_id] = position
        found[position] = {**message, "chat_id": chat, "position": position}
    return found


class LarkXHistory:
    def __init__(self) -> None:
        self.scopes: dict[str, RecoveryCheckpoint] = {}
        self._routes: dict[str, str] | None = None
        self._offset = 0
        self.recovered = 0

    async def sync(self, inbox: LarkXInbox, call: Callable, account: str, user: str,
                   routes: dict[str, str]) -> None:
        if routes == self._routes:
            return
        self.scopes = await call(inbox.sync_recovery_chats, account, user, routes)
        self._routes = dict(routes)
        self._offset = 0

    async def poll(self, inbox: LarkXInbox, client: Any, call: Callable, ensure_current: Callable,
                   account: str, user: str, *, now: float | None = None,
                   reconnect: bool = False) -> tuple[dict, bool]:
        timestamp = time.time() if now is None else now
        errors = 0
        busy = False
        chats = sorted(self.scopes)
        start = self._offset % max(1, len(chats))
        selected = (chats + chats)[start:start + min(len(chats), CHAT_BATCH)]
        self._offset = start + len(selected)
        if selected:
            scopes = [self.scopes[chat] for chat in selected]
            try:
                positions = await asyncio.to_thread(read_chat_positions, client, selected)
            except Exception:
                positions = {}
            ensure_current()
            for scope in scopes:
                chat = scope.chat_id
                if self.scopes.get(chat) != scope:
                    continue
                newest = positions.get(chat)
                if newest is None or (scope.position is not None and newest < scope.position):
                    errors += 1
                    continue
                if scope.position is None:
                    baselined = await call(inbox.recovery_baseline, scope, newest)
                    ensure_current()
                    if self.scopes.get(chat) == scope:
                        if baselined:
                            self.scopes[chat] = RecoveryCheckpoint(chat, scope.signature, scope.epoch, newest)
                        else:
                            self._routes = None  # refresh the stored checkpoint before the next read
                            errors += 1
                    continue
                through = min(newest, scope.position + FRESH_POSITIONS)
                busy |= through < newest
                requested = await call(inbox.recovery_plan, scope, through, limit=PAGE_SIZE,
                                       now=float("inf") if reconnect else timestamp)
                ensure_current()
                if self.scopes.get(chat) != scope or (not requested and through == scope.position):
                    continue
                try:
                    found = await asyncio.to_thread(read_message_positions, client, chat, requested) if requested else {}
                except Exception:
                    errors += 1
                    continue
                ensure_current()
                if self.scopes.get(chat) != scope:
                    continue
                stored = await call(inbox.commit_recovery_page, scope, through, requested, found, now=timestamp)
                ensure_current()
                if self.scopes.get(chat) != scope:
                    continue
                if stored is None:
                    self._routes = None
                    errors += 1
                    continue
                self.recovered += stored
                self.scopes[chat] = RecoveryCheckpoint(chat, scope.signature, scope.epoch, through)
        baselined, missing = await call(inbox.recovery_counts, account, user)
        ensure_current()
        state = ("no_routes" if not chats else "degraded" if errors else "catching_up" if busy
                 else "waiting" if missing or baselined < len(chats) else "ready")
        return {"state": state, "baselined": baselined, "missing": missing, "recovered": self.recovered,
                "errors": errors, "checked_at": int(timestamp)}, busy and not errors
