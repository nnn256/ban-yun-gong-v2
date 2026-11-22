from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sqlite3
import threading
import time
import uuid
from collections.abc import Callable
from functools import partial
from pathlib import Path
from datetime import datetime, timezone
from tempfile import TemporaryDirectory
from typing import Any, ClassVar
from urllib.parse import urlsplit

from astrbot import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star, StarTools, register

from .bridge_core import (
    DEFAULT_MANAGED_PLATFORMS,
    BridgeRouter,
    SeenEvents,
    SentMessageIds,
    component_content_type,
    filter_message_chain,
    make_event_key,
    route_event_key,
)
from .larkx_bridge import LarkAgentXBridge
from .larkx_inbox import RECOVERY_EPOCH
from .event_log import install_loguru_compat
from .runtime_status import RuntimeStatus
from .larkx_media import NativeMediaDeliveryUncertain, attachment_bytes, attachment_filename, is_image_message, attachment_kind
from .larkx_merge import is_merged_forward, is_system_message, merged_forward_parts
from .larkx_richtext import has_inline_emotion, is_rich_post, rich_post_parts, rich_text_content
from .qq_transport import (QQDeliveryUncertain, expand_forward_segments, has_forward_segments,
                           install_onebot_result_guard, qq_send_gate, send_qq)
from .qq_emojis import find_face_image
from .build_identity import BRIDGE_VERSION, capture_module_build, loaded_modules


MODULE_BUILD = capture_module_build(__file__)
_LOADED_MODULES = loaded_modules(__package__, MODULE_BUILD)


QQ_ALBUM_MAX_IMAGES = 8
QQ_ALBUM_MAX_BYTES = 16 * 1024 * 1024
RECEIPT_RETRY_SECONDS = 1.0


def _emoji_image_component(component: Any) -> Any:
    """Retain an emoji hint while using AstrBot's validated Image fields."""
    if hasattr(component, "model_dump"):
        fields = component.model_dump()
    elif hasattr(component, "dict"):
        fields = component.dict()
    else:
        fields = vars(component)

    class EmojiImage(type(component)):
        mover_content_type: ClassVar[str] = "emoji"

    return EmojiImage(**fields)


@register(
    "astrbot_plugin_message_bridge",
    "搬运工V2",
    "在 AstrBot 的 QQ/OneBot 与飞书会话之间双向转发；不直接调用 AI。",
    BRIDGE_VERSION,
)
class MessageBridge(Star):
    def __init__(self, context: Context) -> None:
        super().__init__(context)
        install_loguru_compat()
        install_onebot_result_guard()
        self._chat_cache_lock = threading.RLock()
        self.data_dir = Path(StarTools.get_data_dir("astrbot_plugin_message_bridge"))
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.config_path = self.data_dir / "bridge_rules.json"
        self.router = self._load_router()
        self._rules_signature = self._config_signature()
        self.seen = SeenEvents(
            ttl_seconds=7 * 86400, max_entries=100000,
            path=self.data_dir / "delivery_seen.sqlite3",
        )
        self.sent_qq = SentMessageIds(
            ttl_seconds=7 * 86400, max_entries=100000,
            path=self.data_dir / "qq_receipts.sqlite3",
        )
        self._qq_delivery_lock = asyncio.Lock()
        self._qq_self_event_handlers: dict[int, tuple[Any, Any, Any]] = {}
        self._larkx_envelopes_in_flight: set[str] = set()
        self._reported_sessions: set[str] = set()
        self.larkx_bridge = LarkAgentXBridge(self)
        self.runtime_status = RuntimeStatus(context, self.data_dir, self.larkx_bridge, build=_LOADED_MODULES)
        self._publish_routing_status()
        logger.info(
            "搬运工V2 已加载；配置文件: %s；受管平台: %s；有效规则: %s",
            self.config_path,
            ", ".join(sorted(self.router.managed_platforms)),
            len(self.router.routes),
        )

    async def initialize(self) -> None:
        await self.start_personal_feishu_listener()

    @filter.on_astrbot_loaded()
    async def start_personal_feishu_listener(self) -> None:
        self._install_qq_self_message_handlers()
        if self.larkx_bridge:
            self.larkx_bridge.start()
        self.runtime_status.start()

    def _install_qq_self_message_handlers(self) -> None:
        manager = getattr(self.context, "platform_manager", None)
        getter = getattr(manager, "get_insts", None)
        if not callable(getter):
            return
        for adapter in getter():
            metadata = adapter.meta()
            if metadata.name != "aiocqhttp":
                continue
            bot = adapter.get_client()
            original = getattr(bot, "_handle_event", None)
            if not callable(original) or id(bot) in self._qq_self_event_handlers:
                continue

            async def receive(payload, _original=original):
                # aiocqhttp rejects message_sent before its event bus sees it.
                # Reuse its normal parser and dispatch, preserving source data.
                if isinstance(payload, dict) and payload.get("post_type") == "message_sent":
                    sender = payload.get("sender")
                    sender_id = sender.get("user_id") if isinstance(sender, dict) else None
                    self_id = payload.get("self_id")
                    if (payload.get("message_type") in {"group", "private"}
                            and self_id is not None and str(self_id)
                            and sender_id is not None and str(sender_id) == str(self_id)):
                        payload = dict(payload)
                        payload["post_type"] = "message"
                return await _original(payload)

            bot._handle_event = receive
            self._qq_self_event_handlers[id(bot)] = (bot, original, receive)

    def _restore_qq_self_message_handlers(self) -> None:
        for bot, original, receive in self._qq_self_event_handlers.values():
            if getattr(bot, "_handle_event", None) is receive:
                bot._handle_event = original
        self._qq_self_event_handlers.clear()

    def _load_router(self) -> BridgeRouter:
        if not self.config_path.exists():
            template = Path(__file__).with_name("bridge_rules.example.json")
            self.config_path.write_text(
                template.read_text(encoding="utf-8"), encoding="utf-8"
            )
        try:
            data = json.loads(self.config_path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("配置根节点必须是 JSON 对象")
            if "managed_platforms" not in data:
                data["managed_platforms"] = list(DEFAULT_MANAGED_PLATFORMS)
            data.setdefault("bridge_enabled", True)
            return BridgeRouter(data)
        except Exception:
            logger.exception("搬运工V2 配置无效；当前不会转发消息")
            return BridgeRouter({"managed_platforms": DEFAULT_MANAGED_PLATFORMS, "bridge_enabled": False})

    def _publish_routing_status(self) -> None:
        if hasattr(self, "runtime_status") and hasattr(self, "router"):
            self.runtime_status.update_routing(self.router.enabled, len(self.router.routes), len(self.router.routes))

    def _config_signature(self) -> tuple[int, int] | None:
        try:
            stat = self.config_path.stat()
            return stat.st_mtime_ns, stat.st_size
        except OSError:
            return None

    def _reload_router_if_changed(self) -> None:
        signature = self._config_signature()
        if signature == self._rules_signature:
            return
        self._rules_signature = signature
        # A deleted or partially written configuration disables forwarding;
        # it must not keep the last good rules sending to an old destination.
        self.router = (self._load_router() if signature is not None else
                       BridgeRouter({"managed_platforms": DEFAULT_MANAGED_PLATFORMS, "bridge_enabled": False}))
        self._publish_routing_status()

    def larkx_recovery_chats(self, account_id: str) -> dict[str, str]:
        self._reload_router_if_changed()
        if not self.router.enabled:
            return {}
        grouped: dict[str, set[tuple]] = {}
        for route in self.router.routes:
            chat = route.source_session_id
            if (not route.matches("larkx", account_id, route.source_message_type, chat)
                    or not route.content_types
                    or (route.target_umo.startswith("larkx:") and route.target_umo.split(":", 2)[-1] == chat)):
                continue
            grouped.setdefault(chat, set()).add((route.source_platform_id, route.source_message_type,
                                                  route.target_umo, route.target_account_id,
                                                  route.include_self, route.content_types))
        return {chat: hashlib.sha256(json.dumps(sorted(rules), ensure_ascii=False).encode("utf-8")).hexdigest()
                for chat, rules in grouped.items()}

    async def larkx_send_gate(self, message: dict, account_id: str, *, include_mixed: bool = False) -> str:
        """Do not spend finite retries on a known QQ permission gate."""
        self._reload_router_if_changed()
        if not self.router.enabled:
            return ""
        chat = str(message.get("chat_id") or "")
        raw_type = message.get("chat_type")
        kind = "GroupMessage" if raw_type in (2, 3, "2", "3") else "FriendMessage" if raw_type in (1, "1") else ""
        own = str(message.get("from_id") or "") == str(self.larkx_bridge.user_id or "")
        routes = [route for route in self.router.routes
                  if route.source_platform == "larkx" and route.source_session_id == chat
                  and (not route.source_platform_id or route.source_platform_id == account_id)
                  and (not kind or route.source_message_type == kind)
                  and (not own or route.include_self) and route.content_types
                  and not (route.target_umo.startswith("larkx:") and route.target_umo.split(":", 2)[-1] == chat)]
        if (not any(not route.target_umo.startswith("larkx:") for route in routes)
                or not include_mixed and any(route.target_umo.startswith("larkx:") for route in routes)):
            # A blocked QQ destination must not pause the same message's
            # independent Feishu destination. Confirm it first, then defer
            # only remaining QQ work under the original receipt identities.
            return ""
        gate = await qq_send_gate(self.context)
        return "" if gate["send_ready"] else gate["send_state"]

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_platform_message(self, event: AstrMessageEvent) -> None:
        self._reload_router_if_changed()
        platform = event.get_platform_name()
        if platform not in self.router.managed_platforms:
            return

        # The bridge is a transport-only consumer; managed traffic must never
        # fall through to AstrBot's conversational or AI response handlers.
        event.stop_event()
        sender_id = event.get_sender_id()
        message_obj = getattr(event, "message_obj", None)
        source_account_id = str(getattr(message_obj, "self_id", ""))
        own_message = bool(sender_id and str(sender_id) == source_account_id)
        message_type = getattr(event.get_message_type(), "value", "")
        platform_id = str(event.get_platform_id())
        session_id = str(event.get_session_id())
        if platform == "aiocqhttp" and own_message and message_type == "FriendMessage":
            # AstrBot derives a private session from sender.user_id. For a
            # message_sent event that is our own account, not its recipient.
            # Preserve the verified sender and route against the reported peer.
            raw = getattr(message_obj, "raw_message", None)
            if isinstance(raw, dict):
                for name in ("target_id", "peer_id", "user_id"):
                    peer = str(raw.get(name) or "")
                    if peer and peer != source_account_id:
                        session_id = peer
                        break
            if session_id == source_account_id:
                # No identifiable recipient: do not misroute into a rule for
                # the logged-in account's own private session.
                return
        if platform == "aiocqhttp":
            self.runtime_status.record_receive("qq", session_id)
        if not self.router.enabled:
            return

        if platform == "aiocqhttp":
            async with self._qq_delivery_lock:
                if await asyncio.to_thread(
                    self.sent_qq.contains, platform_id, str(getattr(message_obj, "message_id", "")),
                    source_account_id, session_id,
                ):
                    return

        message_text = event.get_message_str() or ""
        routes = self.router.find_routes(
            platform, platform_id, message_type, session_id,
            source_account_id,
        )
        routes = [route for route in routes if not own_message or route.include_self]
        if not routes:
            origin = str(event.unified_msg_origin)
            if origin not in self._reported_sessions and len(self._reported_sessions) < 500:
                self._reported_sessions.add(origin)
                logger.info(
                    "搬运工V2 会话尚未配置；平台=%s；会话=%s",
                    platform,
                    origin,
                )
            return

        event_id = str(getattr(message_obj, "message_id", "") or "")
        timestamp = getattr(message_obj, "timestamp", "")
        dedup_platform_id = platform_id if message_type == "GroupMessage" else f"{platform_id}:{source_account_id}"
        dedup_key = make_event_key(
            dedup_platform_id,
            session_id,
            event_id,
            sender_id,
            message_text,
            timestamp,
        )
        raw = getattr(message_obj, "raw_message", None)
        segments = raw.get("message") if isinstance(raw, dict) else None
        if platform == "aiocqhttp" and has_forward_segments(segments):
            try:
                expanded = await expand_forward_segments(segments, getattr(event, "bot", None), source_account_id)
            except Exception as exc:
                for route in routes:
                    destination = "feishu" if route.target_umo.startswith("larkx:") else "qq"
                    self.runtime_status.record_delivery(destination, False)
                logger.warning("搬运工V2 读取 QQ 合并转发失败 (%s)", type(exc).__name__)
                return
            chain = self._build_forward_chain(event, platform, expanded)
        else:
            chain = self._build_forward_chain(event, platform)
        requested_types = {kind for route in routes for kind in route.content_types}
        chain = filter_message_chain(chain, requested_types)
        if not chain.chain:
            return
        for route in routes:
            route_chain = filter_message_chain(chain, route.content_types)
            if not route_chain.chain:
                continue
            with TemporaryDirectory(prefix="mover-qq-source-") as directory:
                if platform == "aiocqhttp":
                    try:
                        route_chain = await self._materialize_qq_media(event, route_chain, directory)
                    except Exception as exc:
                        target = route.target_umo.split(":", 1)[0]
                        delivery_platform = "feishu" if target == "larkx" else "qq"
                        self.runtime_status.record_delivery(delivery_platform, False)
                        self.log_larkx_error("下载 QQ 音视频失败", exc)
                        continue
                await self._deliver_route_parts(route, route_chain, dedup_key)

    async def _deliver_route_parts(self, route, chain: MessageChain, event_key: str, *, trace_source: str = "",
                                   can_submit: Callable[[], bool] | None = None) -> bool:
        route_key = route_event_key(event_key, route)
        completed = True
        for index, component in enumerate(chain.chain):
            part_key = f"{route_key}:part:{index}"
            part = MessageChain()
            part.chain.append(component)
            result = await self._deliver_reserved(part_key, route, part, can_submit=can_submit, trace_source=trace_source)
            if result is False:
                completed = False
            # An unavailable part must not discard all subsequent pictures.
            # Unknown receipts stay reserved and are never retried blindly.
        return completed

    async def _deliver_reserved(
        self, key: str, route, chain: MessageChain, native_larkx_message_id: str = "", *,
        can_submit: Callable[[], bool] | None = None, trace_source: str = "",
    ) -> bool | None:
        return await self._deliver_with_reservation([key], route, chain, native_larkx_message_id,
                                                    can_submit=can_submit, trace_source=trace_source)

    async def _has_delivery_receipt(self, key: str) -> bool:
        # A read can wait behind a disk write in another worker. Keep it off
        # the event loop that receives and acknowledges Feishu messages.
        return await asyncio.to_thread(self.seen.contains, key)

    async def _deliver_reserved_many(self, keys: list[str], route, chain: MessageChain, *,
                                     can_submit: Callable[[], bool] | None = None) -> bool | None:
        """Send an album once while retaining the original leaf identities."""
        if len(keys) != len(chain.chain) or len(set(keys)) != len(keys):
            raise ValueError("Album receipt keys must match its components")
        return await self._deliver_with_reservation(keys, route, chain, can_submit=can_submit)

    @staticmethod
    async def _wait_delivery_task(task: asyncio.Task) -> tuple[Any, bool]:
        """Let a running side effect settle, even after repeated caller cancellation."""
        cancelled = False
        while True:
            try:
                return await asyncio.shield(task), cancelled
            except asyncio.CancelledError:
                if task.cancelled():
                    raise
                cancelled = True

    async def _attempt_reserved_delivery(
        self, keys: list[str], route, chain: MessageChain, native_larkx_message_id: str,
        can_submit: Callable[[], bool] | None,
    ) -> tuple[list[str], bool | None, bool, str]:
        if can_submit is not None and not can_submit():
            return [], True, False, "filtered"
        reservation = asyncio.create_task(asyncio.to_thread(self.seen.reserve_many, keys))
        accepted, cancelled = await self._wait_delivery_task(reservation)
        if not accepted:
            return [], True, cancelled, "duplicate"
        if cancelled:
            # A caller cancelled during a disk write has not submitted to QQ.
            # Wait for ownership to be known and release the unsent reservation.
            return accepted, False, True, "rejected"

        if len(accepted) != len(keys):
            # An overlapping album may have already sent some of these leaves.
            owned = set(accepted)
            selected = MessageChain()
            selected.chain.extend(component for key, component in zip(keys, chain.chain) if key in owned)
        else:
            selected = chain
        async def submit() -> tuple[bool, bool | None]:
            # Rule and account changes can happen during download, while
            # waiting in the QQ lane, or during the SQLite reservation.
            if can_submit is not None and not can_submit():
                return False, False
            return True, await self._deliver_chain(route, selected, native_larkx_message_id=native_larkx_message_id)

        delivery = asyncio.create_task(submit())
        try:
            (attempted, result), cancelled = await self._wait_delivery_task(delivery)
        except asyncio.CancelledError:
            # The transport task itself was interrupted; its remote outcome
            # cannot be inferred from the cancellation.
            return accepted, None, True, "uncertain"
        state = "filtered" if not attempted else "confirmed" if result is True else "rejected" if result is False else "uncertain"
        return accepted, result, cancelled, state

    async def _deliver_with_reservation(
        self, keys: list[str], route, chain: MessageChain, native_larkx_message_id: str = "", *,
        can_submit: Callable[[], bool] | None = None, trace_source: str = "",
    ) -> bool | None:
        if not keys:
            return True
        if not chain.chain and not native_larkx_message_id:
            return False
        if route.target_umo.startswith("larkx:"):
            accepted, result, cancelled, state = await self._attempt_reserved_delivery(
                keys, route, chain, native_larkx_message_id, can_submit,
            )
        else:
            # Waiting in line is cancellable and does not create a receipt.
            # Hold the lane through the remote call and QQ echo registration.
            async with self._qq_delivery_lock:
                accepted, result, cancelled, state = await self._attempt_reserved_delivery(
                    keys, route, chain, native_larkx_message_id, can_submit,
                )
        if accepted:
            cancelled = await self._commit_delivery_outcome(accepted, result) or cancelled
        if cancelled:
            raise asyncio.CancelledError
        if trace_source:
            target_ref = hashlib.sha256(keys[0].encode("utf-8")).hexdigest()[:16]
            await self.trace_larkx("larkx_qq_delivery", state, source_ref=trace_source, target_ref=target_ref)
        return True if state == "filtered" else result

    async def _commit_delivery_outcome(self, keys: list[str], result: bool | None) -> bool:
        """Settle a known remote result before completing its inbox item.

        In particular, a transient disk error while releasing a *rejected*
        send must not leave reserved keys that the next attempt mistakes for
        accepted pictures. Retry local storage only; never repeat the send.
        """
        async def persist() -> None:
            warned = False
            while True:
                try:
                    if result is False:
                        await asyncio.to_thread(self.seen.discard_many, keys)
                    else:
                        await asyncio.to_thread(self.seen.finish_many, keys, uncertain=result is None)
                    return
                except (sqlite3.Error, OSError) as exc:
                    if not warned:
                        try:
                            self.log_larkx_error("本地转发回执暂时无法保存，等待存储恢复；未重新发送", exc)
                        except Exception:
                            pass
                        warned = True
                    await asyncio.sleep(RECEIPT_RETRY_SECONDS)

        _, cancelled = await self._wait_delivery_task(asyncio.create_task(persist()))
        return cancelled

    async def _deliver_chain(
        self, route, chain: MessageChain, native_larkx_message_id: str = ""
    ) -> bool | None:
        delivery_platform = "feishu" if route.target_umo.split(":", 2)[0] == "larkx" else "qq"
        if not chain.chain and not native_larkx_message_id:
            return False
        try:
            target_platform = route.target_umo.split(":", 2)[0]
            if target_platform == "larkx":
                target_chat_id = route.target_umo.split(":", 2)[-1]
                if native_larkx_message_id:
                    sent = bool(
                        self.larkx_bridge
                        and await asyncio.to_thread(
                            self.larkx_bridge.forward_message,
                            target_chat_id,
                            native_larkx_message_id,
                        )
                    )
                else:
                    sent = bool(
                        self.larkx_bridge
                        and await asyncio.to_thread(
                            self.larkx_bridge.send_chain, target_chat_id, chain
                        )
                    )
            else:
                sent = await self._send_to_qq(route, chain)
        except (NativeMediaDeliveryUncertain, QQDeliveryUncertain) as exc:
            try:
                self.runtime_status.record_delivery(delivery_platform, False)
                self.runtime_status.events.write("send_uncertain", delivery_platform, False,
                                                 error_type=type(exc).__name__)
                self.log_larkx_error("发送结果未知；已保留去重记录，请核对目标会话", exc)
            except Exception:
                pass
            return None
        except Exception as exc:
            try:
                self.runtime_status.record_delivery(delivery_platform, False)
                from .qq_transport import QQSendError
                if isinstance(exc, QQSendError):
                    self.runtime_status.events.write("send_rejected", delivery_platform, False,
                                                     error_type=type(exc).__name__, error_code=exc.code,
                                                     http_status=exc.http_status)
                self.log_larkx_error("消息转发失败", exc)
            except Exception:
                pass
            return False
        # Diagnostic storage is separate from the delivery receipt. A status
        # write failure after acceptance must never release the dedup key.
        try:
            self.runtime_status.record_delivery(delivery_platform, bool(sent), str(sent) if isinstance(sent, str) else "")
            if not sent:
                logger.error(
                    "搬运工V2 发送失败；规则=%s；目标会话=%s",
                    route.name,
                    route.target_umo,
                )
        except Exception:
            pass
        return bool(sent)

    async def _send_to_qq(self, route, chain: MessageChain) -> str:
        # Caller owns the QQ lane until the echo receipt is visible.
        account_id, message_id = await send_qq(
            self.context, route.target_umo, chain, self.runtime_status,
            account_id=route.target_account_id, return_account=True,
        )
        session_id = route.target_umo.split(":", 2)[2]
        try:
            await asyncio.to_thread(
                self.sent_qq.add, route.target_umo.split(":", 1)[0], message_id, account_id, session_id,
            )
        except Exception as exc:
            # The remote send has already been accepted. A local disk
            # error cannot turn it into a safe-to-repeat send failure.
            raise QQDeliveryUncertain("QQ 已接受消息，但本地回执未保存") from exc
        return message_id

    async def _download_qq_record(self, event: AstrMessageEvent, component: Any) -> bytes:
        message_obj = getattr(event, "message_obj", None)
        raw_message = getattr(message_obj, "raw_message", None)
        segments = raw_message.get("message", []) if isinstance(raw_message, dict) else []
        segment = next((
            item for item in segments
            if isinstance(item, dict) and str(item.get("type", "")).lower() == "record"
        ), {})
        data = segment.get("data") if isinstance(segment.get("data"), dict) else {}
        file_ref = str(getattr(component, "file", "") or data.get("file") or "").strip()
        bot = getattr(event, "bot", None)
        caller = getattr(bot, "call_action", None)
        if not file_ref or not callable(caller):
            raise ValueError("QQ 语音缺少可读取的 URL、文件或 OneBot record 标识")
        result = await caller(
            "get_record",
            file=file_ref,
            out_format="wav",
            self_id=str(getattr(message_obj, "self_id", "") or ""),
        )
        if isinstance(result, dict) and isinstance(result.get("data"), dict):
            result = result["data"]
        file_path = result.get("file") if isinstance(result, dict) else result
        if not file_path:
            raise ValueError("OneBot get_record 没有返回可读取文件")
        return await asyncio.to_thread(attachment_bytes, str(file_path))

    async def _materialize_qq_media(
        self, event: AstrMessageEvent, chain: MessageChain, directory: str
    ) -> MessageChain:
        from astrbot.api.message_components import Record, Video

        materialized = MessageChain()
        for index, component in enumerate(chain.chain):
            media_kind = component_content_type(component)
            if media_kind not in {"audio", "video"}:
                materialized.chain.append(component)
                continue
            try:
                data = await asyncio.to_thread(attachment_bytes, component)
            except Exception:
                if media_kind != "audio":
                    raise
                data = await self._download_qq_record(event, component)

            reference = str(
                getattr(component, "name", "")
                or getattr(component, "path", "")
                or getattr(component, "file", "")
                or getattr(component, "url", "")
            )
            if reference.startswith(("base64://", "data:")):
                reference = ""
            if reference.startswith(("http://", "https://", "file:")):
                reference = urlsplit(reference).path
            filename = attachment_filename(reference, data, media_kind)
            path = Path(directory) / f"{index}-{filename}"
            await asyncio.to_thread(path.write_bytes, data)
            if media_kind == "audio":
                materialized.chain.append(Record.fromFileSystem(str(path)))
            else:
                materialized.chain.append(Video.fromFileSystem(str(path)))
        return materialized

    def log_larkx_state(self, message: str) -> None:
        logger.info("搬运工V2 LarkAgentX: %s", message)

    def log_larkx_error(self, message: str, error: Exception) -> None:
        from .qq_transport import QQSendError
        if isinstance(error, QQSendError):
            logger.warning("搬运工V2 LarkAgentX: %s (%s, code=%s, HTTP=%s)",
                           message, type(error).__name__, error.code, error.http_status)
        else:
            logger.warning("搬运工V2 LarkAgentX: %s (%s)", message, type(error).__name__)

    async def trace_larkx(self, action: str, state: str, message: dict | None = None, account_id: str = "", *,
                          source_ref: str = "", target_ref: str = "", count: int = 1,
                          content_type: str = "image") -> None:
        try:
            if message is not None:
                source_ref = self._larkx_source_ref(message, account_id)
            await self.runtime_status.record_larkx_trace(action, state=state, source_ref=source_ref,
                                                         target_ref=target_ref, count=count, content_type=content_type)
        except Exception:
            pass

    def _larkx_source_ref(self, message: dict, account_id: str) -> str:
        return hashlib.sha256(self._larkx_event_key(message, account_id).encode("utf-8")).hexdigest()[:16]

    async def _download_larkx_media(self, method, message: dict) -> Any:
        from requests.exceptions import ConnectionError, HTTPError, Timeout

        delays = (0.3, 0.7, 1.2, 2.5, 5.0)
        for attempt in range(len(delays) + 1):
            try:
                return await asyncio.to_thread(method, message)
            except (HTTPError, ConnectionError, Timeout) as exc:
                response = getattr(exc, "response", None)
                status = getattr(response, "status_code", None)
                transient = (not isinstance(exc, HTTPError) or status in {404, 408, 425, 429}
                             or isinstance(status, int) and 500 <= status <= 599)
                if not transient or attempt == len(delays):
                    raise
                delay = delays[attempt]
                # Honour bounded Retry-After seconds without trusting response
                # bodies or unbounded server-supplied delays.
                try:
                    delay = max(delay, min(30.0, float(getattr(response, "headers", {}).get("Retry-After", 0))))
                except (TypeError, ValueError):
                    pass
                self.runtime_status.events.write(
                    "download_retry", "feishu", attempt=attempt + 1,
                    error_type=type(exc).__name__, http_status=status or "",
                    source_message_id=message.get("msg_id") or message.get("message_id") or "",
                    chat_id=message.get("chat_id") or "",
                )
                await asyncio.sleep(delay)

    @staticmethod
    def _larkx_event_key(message: dict, account_id: str) -> str:
        message_id = str(message.get("msg_id") or message.get("message_id") or "")
        content = str(message.get("content") or "")
        if not message_id:
            # A batch can have identical placeholder text and timestamps. Its
            # resource keys, rather than the shared placeholder, identify each
            # separate image. Keep metadata only in a one-way hash.
            media_identity = {
                key: message[key] for key in ("msg_type", "content_data", "image_key", "file_key", "key")
                if key in message
            }
            content += "\0" + hashlib.sha256(json.dumps(
                media_identity, sort_keys=True, ensure_ascii=False, default=lambda value: repr(value),
            ).encode("utf-8")).hexdigest()
        return make_event_key(
            f"larkx:{account_id}", str(message.get("chat_id") or ""), message_id,
            str(message.get("from_id") or ""), content,
            message.get("create_time") or message.get("timestamp") or "",
        )

    @staticmethod
    def _larkx_content_type(message: dict) -> str:
        kind = attachment_kind(message)
        if kind == "sticker":
            return "emoji"
        if is_image_message(message):
            return "image"
        return kind if kind in {"audio", "video", "file"} else "text"

    async def _larkx_part_chain(self, part: dict, directory: str, index: int) -> MessageChain:
        from astrbot.api.message_components import Image, Record, Video, File

        chain = MessageChain()
        kind = self._larkx_content_type(part)
        if kind == "text":
            value = part.get("content")
            if isinstance(value, str) and value.strip():
                chain.message(value)
        elif kind == "image":
            data = await self._download_larkx_media(self.larkx_bridge.download_image, part)
            chain.chain.append(Image.fromBytes(data))
        else:
            data, filename, downloaded_kind = await self._download_larkx_media(
                self.larkx_bridge.download_attachment, part
            )
            if downloaded_kind == "sticker":
                chain.chain.append(_emoji_image_component(Image.fromBytes(data)))
            else:
                path = Path(directory) / f"{index}-{filename}"
                await asyncio.to_thread(path.write_bytes, data)
                if downloaded_kind == "audio":
                    chain.chain.append(Record.fromFileSystem(str(path)))
                elif downloaded_kind == "video":
                    chain.chain.append(Video.fromFileSystem(str(path)))
                else:
                    chain.chain.append(File(name=filename, file=str(path)))
        return chain

    async def _forward_larkx_parts(self, message: dict, routes: list, event_key: str, parse, label: str, *,
                                   can_submit: Callable[[], bool] | None = None) -> bool:
        # POST and MERGE_FORWARD are envelopes. Materialize the selected leaves
        # for both destinations so a video arrives as a native player message.
        try:
            parts = parse(message)
        except Exception as exc:
            for route in routes:
                platform = "feishu" if route.target_umo.startswith("larkx:") else "qq"
                self.runtime_status.record_delivery(platform, False)
            self.log_larkx_error(f"解析飞书{label}失败", exc)
            return False
        chat_id = str(message.get("chat_id") or "")
        active = [route for route in routes if not (
            route.target_umo.startswith("larkx:") and route.target_umo.split(":", 2)[-1] == chat_id
        )]
        completed = True
        index = 0
        while index < len(parts):
            part = parts[index]
            # Album boundaries come from the original tree, before filtering
            # by route or old receipts. Real merged chat records retain their
            # individual child-message semantics.
            if not is_merged_forward(message) and self._larkx_content_type(part) == "image":
                end = index + 1
                while end < len(parts) and self._larkx_content_type(parts[end]) == "image":
                    end += 1
                for offset in range(index, end, QQ_ALBUM_MAX_IMAGES):
                    group = list(enumerate(parts[offset:min(offset + QQ_ALBUM_MAX_IMAGES, end)], offset))
                    if not await self._forward_larkx_album_images(group, active, event_key, label, can_submit=can_submit):
                        completed = False
                index = end
                continue
            kind = self._larkx_content_type(part)
            rendered = ({route: rich_text_content(part, route.content_types) for route in active}
                        if kind == "text" else {})
            selected = [route for route in active if (rendered.get(route, "").strip() if kind == "text"
                                                     else kind in route.content_types)]
            if not selected:
                index += 1
                continue
            part_key = f"{event_key}:envelope:{index}:{part.get('_rich_part_id', index)}"
            with TemporaryDirectory(prefix="mover-lark-part-") as directory:
                chain = None
                for route in selected:
                    key = route_event_key(part_key, route)
                    if await self._has_delivery_receipt(key):
                        continue
                    try:
                        if kind == "text":
                            chain = MessageChain().message(rendered[route])
                        elif chain is None:
                            chain = await self._larkx_part_chain(part, directory, index)
                        # Reserve only once download/validation has succeeded;
                        # a crash during a read cannot suppress an unsent image.
                        sent = await self._deliver_reserved(key, route, chain, can_submit=can_submit) if chain.chain else True
                    except Exception as exc:
                        platform = "feishu" if route.target_umo.startswith("larkx:") else "qq"
                        self.runtime_status.record_delivery(platform, False)
                        self.log_larkx_error(f"飞书{label}内容转发失败", exc)
                        sent = False
                    if not sent:
                        if sent is False:
                            completed = False
                        # Keep processing this batch's remaining pictures.
                        # The inbox will retry only the failed leaves later.
            index += 1
        return completed

    async def _forward_larkx_album_images(self, group: list[tuple[int, dict]], routes: list,
                                         event_key: str, label: str, *,
                                         can_submit: Callable[[], bool] | None = None) -> bool:
        from astrbot.api.message_components import Image

        selected = [route for route in routes if "image" in route.content_types]
        if not selected:
            return True
        completed = True
        pending: list[tuple[str, Any]] = []
        pending_bytes = 0

        async def flush() -> None:
            nonlocal completed, pending_bytes
            if not pending:
                return
            for route in selected:
                leaves = []
                for part_key, component in pending:
                    key = route_event_key(part_key, route)
                    if not await self._has_delivery_receipt(key):
                        leaves.append((key, component))
                if not leaves:
                    continue
                try:
                    if route.target_umo.startswith("larkx:"):
                        for key, component in leaves:
                            chain = MessageChain()
                            chain.chain.append(component)
                            if await self._deliver_reserved(key, route, chain, can_submit=can_submit) is False:
                                completed = False
                    else:
                        chain = MessageChain()
                        chain.chain.extend(component for _, component in leaves)
                        if await self._deliver_reserved_many([key for key, _ in leaves], route, chain, can_submit=can_submit) is False:
                            completed = False
                except Exception as exc:
                    platform = "feishu" if route.target_umo.startswith("larkx:") else "qq"
                    self.runtime_status.record_delivery(platform, False)
                    self.log_larkx_error(f"飞书{label}多图转发失败", exc)
                    completed = False
            pending.clear()
            pending_bytes = 0

        for index, part in group:
            part_key = f"{event_key}:envelope:{index}:{part.get('_rich_part_id', index)}"
            needed = [route for route in selected
                      if not await self._has_delivery_receipt(route_event_key(part_key, route))]
            if not needed:
                continue
            try:
                data = await self._download_larkx_media(self.larkx_bridge.download_image, part)
                # At most one extra validated image is in memory while deciding
                # a byte boundary. A legal large single image stays sendable.
                if pending and pending_bytes + len(data) > QQ_ALBUM_MAX_BYTES:
                    await flush()
                component = Image.fromBytes(data)
                pending.append((part_key, component))
                pending_bytes += len(data)
                if pending_bytes >= QQ_ALBUM_MAX_BYTES:
                    await flush()
            except Exception as exc:
                for platform in {"feishu" if route.target_umo.startswith("larkx:") else "qq" for route in needed}:
                    self.runtime_status.record_delivery(platform, False)
                self.log_larkx_error(f"下载飞书{label}图片失败", exc)
                completed = False
                # Continue later leaves; no reservation exists for this failed
                # download, so inbox recovery can retry just the missing image.
        await flush()
        return completed

    async def on_larkx_message(
        self, message: dict, account_id: str, user_id: str
    ) -> bool:
        if is_system_message(message):
            return True
        routes = []
        image_message = is_image_message(message)
        source_ref = ""
        try:
            self._reload_router_if_changed()
            chat_id = str(message.get("chat_id") or "")
            if chat_id:
                self._record_larkx_receive(chat_id, message)
            if image_message:
                source_ref = self._larkx_source_ref(message, account_id)
            if not self.router.enabled:
                if source_ref:
                    await self.trace_larkx("larkx_qq_route", "filtered", source_ref=source_ref)
                return True
            message_id = str(message.get("msg_id") or message.get("message_id") or "")
            if await asyncio.to_thread(self.larkx_bridge.is_outgoing, chat_id, message_id):
                if source_ref:
                    await self.trace_larkx("larkx_qq_route", "echo", source_ref=source_ref)
                return True
            # The lookup above yields. Apply current routing, and reject an
            # old recovery epoch before resolving any new destination.
            self._reload_router_if_changed()
            if not self.router.enabled or not self.larkx_bridge.recovery_message_current(message):
                return True
            can_submit = (partial(self.larkx_bridge.recovery_message_current, message)
                          if message.get(RECOVERY_EPOCH) else None)
            content = str(message.get("content") or "")
            media_kind = attachment_kind(message)
            sticker_message = media_kind == "sticker"
            rich_post = is_rich_post(message)
            merged = is_merged_forward(message)
            inline_emotion = not rich_post and not merged and has_inline_emotion(message)
            if (not content.strip() and not image_message and not media_kind and not rich_post and not merged
                    and not inline_emotion):
                return True
            if not chat_id:
                return True
            self._remember_larkx_chat(message, chat_id)
            raw_chat_type = message.get("chat_type", 0)
            if raw_chat_type in (2, 3, "2", "3"):
                message_type = "GroupMessage"
            elif raw_chat_type in (1, "1"):
                message_type = "FriendMessage"
            else:
                configured = {route.source_message_type for route in self.router.routes
                              if route.source_platform == "larkx" and route.source_session_id == chat_id
                              and (not route.source_platform_id or route.source_platform_id == account_id)}
                if len(configured) != 1:
                    return True
                message_type = configured.pop()
            routes = self.router.find_routes(
                "larkx", account_id, message_type, chat_id
            )
            own_message = str(message.get("from_id") or "") == str(user_id or "")
            had_qq_route = any(not route.target_umo.startswith("larkx:") for route in routes)
            routes = [route for route in routes if not own_message or route.include_self]
            if inline_emotion:
                routes = [route for route in routes if {"text", "emoji"}.intersection(route.content_types)
                          and not (route.target_umo.startswith("larkx:") and route.target_umo.split(":", 2)[-1] == chat_id)]
                if not routes:
                    return True
                parts = rich_post_parts(message)
                event_key = self._larkx_event_key(message, account_id)
                complete = True
                for route in routes:
                    value = "".join(rich_text_content(part, route.content_types) for part in parts)
                    if not value.strip():
                        continue
                    # Keep the same receipt identity as a normal TEXT message
                    # across an upgrade or a repeated receive.
                    key = f"{route_event_key(event_key, route)}:part:0"
                    if await self._deliver_reserved(key, route, MessageChain().message(value), can_submit=can_submit) is False:
                        complete = False
                return complete
            if merged or rich_post:
                if routes:
                    dedup_key = self._larkx_event_key(message, account_id)
                    if dedup_key not in self._larkx_envelopes_in_flight:
                        parse, label = (merged_forward_parts, "合并转发") if merged else (rich_post_parts, "富文本")
                        self._larkx_envelopes_in_flight.add(dedup_key)
                        try:
                            return await self._forward_larkx_parts(message, routes, dedup_key, parse, label, can_submit=can_submit)
                        finally:
                            self._larkx_envelopes_in_flight.discard(dedup_key)
                return True
            source_content_type = (
                "emoji" if sticker_message else
                "image" if image_message else
                media_kind if media_kind in {"audio", "video", "file"} else
                "text"
            )
            routes = [route for route in routes if source_content_type in route.content_types]
            if source_ref:
                qq_routes = sum(not route.target_umo.startswith("larkx:") for route in routes)
                await self.trace_larkx("larkx_qq_route", "matched" if qq_routes else
                                       "filtered" if had_qq_route else "no_route",
                                       source_ref=source_ref, count=qq_routes or 1)
            if not routes:
                return True
            dedup_key = self._larkx_event_key(message, account_id)
            native_routes = []
            media_routes = []
            for route in routes:
                target_platform, _, target_chat = route.target_umo.split(":", 2)
                if target_platform == "larkx" and target_chat == chat_id:
                    continue
                if target_platform == "larkx" and source_content_type not in {"text", "video"} and message_id:
                    native_routes.append(route)
                else:
                    media_routes.append(route)
            completed = True
            for route in native_routes:
                route_key = route_event_key(dedup_key, route)
                sent = await self._deliver_reserved(
                    route_key, route, MessageChain(), native_larkx_message_id=message_id, can_submit=can_submit
                )
                if sent is False:
                    completed = False
            if not media_routes:
                return completed
            # Ordinary Feishu messages produce a single component. A repeated
            # push or recovered inbox item need not download accepted media.
            media_routes = [route for route in media_routes if not await self._has_delivery_receipt(
                f"{route_event_key(dedup_key, route)}:part:0"
            )]
            if not media_routes:
                return completed
            with TemporaryDirectory(prefix="mover-receive-") as directory:
                chain = MessageChain()
                if image_message or sticker_message:
                    from astrbot.api.message_components import Image
                    data = (await self._download_larkx_media(self.larkx_bridge.download_image, message) if image_message
                            else (await self._download_larkx_media(self.larkx_bridge.download_attachment, message))[0])
                    component = Image.fromBytes(data)
                    if sticker_message:
                        component = _emoji_image_component(component)
                    chain.chain.append(component)
                elif media_kind:
                    from astrbot.api.message_components import Record, Video, File
                    data, filename, kind = await self._download_larkx_media(self.larkx_bridge.download_attachment, message)
                    path = Path(directory) / filename
                    await asyncio.to_thread(path.write_bytes, data)
                    if kind == "audio":
                        chain.chain.append(Record.fromFileSystem(str(path)))
                    elif kind == "video":
                        chain.chain.append(Video.fromFileSystem(str(path)))
                    else:
                        chain.chain.append(File(name=filename, file=str(path)))
                else:
                    chain.message(content)
                for route in media_routes:
                    target_platform, _, target_chat = route.target_umo.split(":", 2)
                    if target_platform == "larkx" and target_chat == chat_id:
                        continue
                    route_chain = filter_message_chain(chain, route.content_types)
                    if not route_chain.chain:
                        continue
                    if not await self._deliver_route_parts(route, route_chain, dedup_key,
                                                           trace_source=source_ref if target_platform != "larkx" else "",
                                                           can_submit=can_submit):
                        completed = False
                return completed
        except Exception as exc:
            destinations = {"feishu" if route.target_umo.startswith("larkx:") else "qq" for route in routes}
            for destination in destinations:
                self.runtime_status.record_delivery(destination, False)
            if source_ref and "qq" in destinations:
                await self.trace_larkx("larkx_qq_delivery", "prep_failed", source_ref=source_ref)
            self.runtime_status.events.write(
                "receive_failed", "feishu", False, error_type=type(exc).__name__,
                source_message_id=message.get("msg_id") or message.get("message_id") or "",
                chat_id=message.get("chat_id") or "",
            )
            self.log_larkx_error("接收消息转发失败", exc)
            return False

    def _record_larkx_receive(self, chat_id: str, message: dict) -> None:
        self.runtime_status.record_receive("feishu", chat_id)

    def _remember_larkx_chat(self, message: dict, chat_id: str) -> None:
        with self._chat_cache_lock:
            self._write_larkx_chat(message, chat_id)

    def _write_larkx_chat(self, message: dict, chat_id: str) -> None:
        path = self.data_dir / "larkx_chats.json"
        temp = path.with_name(path.name + f".{os.getpid()}.{uuid.uuid4().hex}.tmp")
        try:
            data = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {"chats": []}
            chats = data.get("chats", []) if isinstance(data, dict) else []
            if not isinstance(chats, list):
                chats = []
            previous = next((item for item in chats if isinstance(item, dict) and str(item.get("chat_id")) == chat_id), {})
            chat_type = message.get("chat_type")
            supplied_name = str(message.get("chat_name") or message.get("group_name") or "").strip()[:120]
            entry = {
                **previous,
                "chat_id": chat_id,
                "name": supplied_name or str(previous.get("name") or "")[:120],
                "message_type": ("GroupMessage" if chat_type in (2, 3, "2", "3") else "FriendMessage") if chat_type is not None else previous.get("message_type", "FriendMessage"),
                "last_seen": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            }
            chats = [item for item in chats if not isinstance(item, dict) or item.get("chat_id") != chat_id]
            chats.append(entry)
            self.data_dir.mkdir(parents=True, exist_ok=True)
            temp.write_text(json.dumps({"chats": chats[-500:]}, ensure_ascii=False, indent=2), encoding="utf-8")
            for attempt in range(4):
                try:
                    temp.replace(path)
                    break
                except PermissionError:
                    if attempt == 3:
                        raise
                    time.sleep(0.025 * (attempt + 1))
        except Exception as exc:
            self.log_larkx_error("保存会话列表失败", exc)
        finally:
            temp.unlink(missing_ok=True)

    async def terminate(self) -> None:
        self._restore_qq_self_message_handlers()
        if self.larkx_bridge:
            await self.larkx_bridge.stop()
        await self.runtime_status.stop()

    @staticmethod
    def _build_forward_chain(event: AstrMessageEvent, platform: str, expanded_segments: list | None = None) -> MessageChain:
        chain = MessageChain()
        components = list(event.get_messages()) if expanded_segments is None else []
        by_type: dict[str, list[Any]] = {}
        for component in components:
            by_type.setdefault(type(component).__name__.lower(), []).append(component)

        def take(kind: str) -> Any | None:
            values = by_type.get(kind, [])
            return values.pop(0) if values else None

        raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
        segments = expanded_segments if expanded_segments is not None else (raw.get("message") if isinstance(raw, dict) else None)
        if isinstance(segments, list):
            for segment in segments:
                if not isinstance(segment, dict):
                    continue
                kind = str(segment.get("type") or "").lower()
                data = segment.get("data") if isinstance(segment.get("data"), dict) else {}
                if kind == "text":
                    value = data.get("text")
                    if isinstance(value, str):
                        chain.message(value)
                elif kind == "mface":
                    url = data.get("url")
                    if url:
                        from astrbot.api.message_components import Image
                        chain.chain.append(_emoji_image_component(Image.fromURL(str(url))))
                    elif data.get("text"):
                        chain.message(str(data["text"]))
                elif kind == "face":
                    component = take("face")
                    if component is None:
                        try:
                            from astrbot.api.message_components import Face
                            component = Face(id=int(data.get("id")))
                        except (TypeError, ValueError):
                            continue
                    chain.chain.append(component)
                elif kind in {"image", "record", "video", "file", "at"}:
                    component = take(kind)
                    if component is None and kind in {"image", "record", "video", "file"}:
                        component = MessageBridge._component_from_raw_segment(kind, data)
                    if component is not None:
                        chain.chain.append(component)
        else:
            chain.chain.extend(
                component for component in components
                if type(component).__name__.lower() in {"plain", "image", "record", "video", "file", "at", "face"}
            )
        if expanded_segments is None and not chain.chain and event.get_message_str():
            fallback = str(event.get_message_str())
            # AstrBot's string rendering may put a component-class label before
            # reply text when raw OneBot segments are unavailable.
            for label in ("[ComponentType.Reply]", "[ComponentType.At]", "[ComponentType.Face]"):
                fallback = fallback.replace(label, "")
            if fallback.strip():
                chain.message(fallback.strip())
        return chain

    @staticmethod
    def _component_from_raw_segment(kind: str, data: dict[str, Any]) -> Any | None:
        """Build supported components when an adapter only exposes raw OneBot JSON."""
        source = str(data.get("url") or data.get("file") or data.get("path") or "").strip()
        if not source:
            return None
        from astrbot.api.message_components import File, Image, Record, Video

        component_type = {"image": Image, "record": Record, "video": Video}.get(kind)
        if kind == "file":
            name = str(data.get("name") or data.get("file_name") or Path(source).name or "attachment")
            url = source if source.startswith(("http://", "https://", "file:")) else ""
            return File(name=name, file=source, url=url)
        if component_type is None:
            return None
        if source.startswith(("http://", "https://")):
            return component_type.fromURL(source)
        if source.startswith(("file:", "base64://", "data:")):
            return component_type(file=source)
        candidate = Path(source)
        if candidate.is_file():
            return component_type.fromFileSystem(str(candidate))
        return component_type(file=source)



