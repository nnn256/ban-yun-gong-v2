from __future__ import annotations

import asyncio
import json
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
from .runtime_status import RuntimeStatus
from .larkx_media import NativeMediaDeliveryUncertain, attachment_bytes, attachment_filename, is_image_message, attachment_kind
from .larkx_merge import is_merged_forward, merged_forward_parts
from .larkx_richtext import is_rich_post, rich_post_parts
from .qq_transport import expand_forward_segments, has_forward_segments, send_qq
from .qq_emojis import find_face_image


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
    "2.0.9",
)
class MessageBridge(Star):
    def __init__(self, context: Context) -> None:
        super().__init__(context)
        self.data_dir = Path(StarTools.get_data_dir("astrbot_plugin_message_bridge"))
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.config_path = self.data_dir / "bridge_rules.json"
        self.router = self._load_router()
        self._rules_signature = self._config_signature()
        self.seen = SeenEvents()
        self.sent_qq = SentMessageIds()
        self._qq_delivery_lock = asyncio.Lock()
        self._qq_self_event_handlers: dict[int, tuple[Any, Any, Any]] = {}
        self._larkx_envelopes_in_flight: set[str] = set()
        self._reported_sessions: set[str] = set()
        self.larkx_bridge = LarkAgentXBridge(self)
        self.runtime_status = RuntimeStatus(context, self.data_dir, self.larkx_bridge)
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

        async with self._qq_delivery_lock:
            if self.sent_qq.contains(platform_id, str(getattr(message_obj, "message_id", "")), source_account_id, session_id):
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

    async def _deliver_route_parts(self, route, chain: MessageChain, event_key: str) -> bool:
        route_key = route_event_key(event_key, route)
        for index, component in enumerate(chain.chain):
            part_key = f"{route_key}:part:{index}"
            if not self.seen.accept(part_key):
                continue
            part = MessageChain()
            part.chain.append(component)
            result = await self._deliver_chain(route, part)
            if not result:
                if result is False:
                    self.seen.discard(part_key)
                return False
        return True

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
            self.runtime_status.record_delivery(delivery_platform, bool(sent), str(sent) if isinstance(sent, str) else "")
            if not sent:
                logger.error(
                    "搬运工V2 发送失败；规则=%s；目标会话=%s",
                    route.name,
                    route.target_umo,
                )
            return bool(sent)
        except NativeMediaDeliveryUncertain as exc:
            self.runtime_status.record_delivery(delivery_platform, False)
            self.log_larkx_error("原生媒体发送结果未知；已保留去重记录以避免重复发送", exc)
            return None
        except Exception as exc:
            self.runtime_status.record_delivery(delivery_platform, False)
            self.log_larkx_error("消息转发失败", exc)
            return False

    async def _send_to_qq(self, route, chain: MessageChain) -> str:
        async with self._qq_delivery_lock:
            account_id, message_id = await send_qq(
                self.context, route.target_umo, chain, self.runtime_status,
                account_id=route.target_account_id, return_account=True,
            )
            session_id = route.target_umo.split(":", 2)[2]
            self.sent_qq.add(route.target_umo.split(":", 1)[0], message_id, account_id, session_id)
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
        logger.warning("搬运工V2 LarkAgentX: %s (%s)", message, type(error).__name__)

    async def _download_larkx_media(self, method, message: dict) -> Any:
        from requests.exceptions import HTTPError

        delays = (0.3, 0.7, 1.2)
        for attempt in range(len(delays) + 1):
            try:
                return await asyncio.to_thread(method, message)
            except HTTPError as exc:
                status = getattr(exc.response, "status_code", None)
                transient = status in {404, 408, 425, 429} or isinstance(status, int) and 500 <= status <= 599
                if not transient or attempt == len(delays):
                    raise
                # A pushed resource can become downloadable just after its event.
                await asyncio.sleep(delays[attempt])

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

    async def _forward_larkx_parts(self, message: dict, routes: list, event_key: str, parse, label: str) -> None:
        # POST and MERGE_FORWARD are envelopes. Materialize the selected leaves
        # for both destinations so a video arrives as a native player message.
        try:
            parts = parse(message)
        except Exception as exc:
            for route in routes:
                platform = "feishu" if route.target_umo.startswith("larkx:") else "qq"
                self.runtime_status.record_delivery(platform, False)
            self.log_larkx_error(f"解析飞书{label}失败", exc)
            return
        chat_id = str(message.get("chat_id") or "")
        active = [route for route in routes if not (
            route.target_umo.startswith("larkx:") and route.target_umo.split(":", 2)[-1] == chat_id
        )]
        for index, part in enumerate(parts):
            if not active:
                break
            kind = self._larkx_content_type(part)
            selected = [route for route in active if kind in route.content_types]
            if not selected:
                continue
            part_key = f"{event_key}:envelope:{index}:{part.get('_rich_part_id', index)}"
            with TemporaryDirectory(prefix="mover-lark-part-") as directory:
                chain = None
                for route in selected:
                    key = route_event_key(part_key, route)
                    if not self.seen.accept(key):
                        continue
                    try:
                        if chain is None:
                            chain = await self._larkx_part_chain(part, directory, index)
                        sent = await self._deliver_chain(route, chain) if chain.chain else True
                    except Exception as exc:
                        platform = "feishu" if route.target_umo.startswith("larkx:") else "qq"
                        self.runtime_status.record_delivery(platform, False)
                        self.log_larkx_error(f"飞书{label}内容转发失败", exc)
                        sent = False
                    if not sent:
                        # Continue other targets, but don't send later parts ahead
                        # of one that failed for this target.
                        active.remove(route)
                        if sent is False:
                            self.seen.discard(key)

    async def on_larkx_message(
        self, message: dict, account_id: str, user_id: str
    ) -> None:
        try:
            self._reload_router_if_changed()
            chat_id = str(message.get("chat_id") or "")
            if chat_id:
                self._record_larkx_receive(chat_id, message)
            if not self.router.enabled:
                return
            message_id = str(message.get("msg_id") or message.get("message_id") or "")
            if await asyncio.to_thread(self.larkx_bridge.is_outgoing, chat_id, message_id):
                return
            content = str(message.get("content") or "")
            media_kind = attachment_kind(message)
            image_message = is_image_message(message)
            sticker_message = media_kind == "sticker"
            rich_post = is_rich_post(message)
            merged = is_merged_forward(message)
            if (not content.strip() and not image_message and not media_kind and not rich_post and not merged):
                return
            if not chat_id:
                return
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
                    return
                message_type = configured.pop()
            routes = self.router.find_routes(
                "larkx", account_id, message_type, chat_id
            )
            own_message = str(message.get("from_id") or "") == str(user_id or "")
            routes = [route for route in routes if not own_message or route.include_self]
            if merged or rich_post:
                if routes:
                    dedup_key = make_event_key(
                        f"larkx:{account_id}", chat_id, message_id,
                        str(message.get("from_id") or ""), content,
                        message.get("create_time") or message.get("timestamp") or "",
                    )
                    if dedup_key not in self._larkx_envelopes_in_flight:
                        parse, label = (merged_forward_parts, "合并转发") if merged else (rich_post_parts, "富文本")
                        self._larkx_envelopes_in_flight.add(dedup_key)
                        try:
                            await self._forward_larkx_parts(message, routes, dedup_key, parse, label)
                        finally:
                            self._larkx_envelopes_in_flight.discard(dedup_key)
                return
            source_content_type = (
                "emoji" if sticker_message else
                "image" if image_message else
                media_kind if media_kind in {"audio", "video", "file"} else
                "text"
            )
            routes = [route for route in routes if source_content_type in route.content_types]
            if not routes:
                return
            dedup_key = make_event_key(
                f"larkx:{account_id}", chat_id,
                str(message.get("msg_id") or message.get("message_id") or ""),
                str(message.get("from_id") or ""), content,
                message.get("create_time") or message.get("timestamp") or "",
            )
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
            for route in native_routes:
                route_key = route_event_key(dedup_key, route)
                if not self.seen.accept(route_key):
                    continue
                sent = await self._deliver_chain(route, MessageChain(), native_larkx_message_id=message_id)
                if not sent:
                    self.seen.discard(route_key)
            if not media_routes:
                return
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
                    await self._deliver_route_parts(route, route_chain, dedup_key)
        except Exception as exc:
            self.runtime_status.record_delivery("qq", False)
            self.log_larkx_error("接收消息转发失败", exc)

    def _record_larkx_receive(self, chat_id: str, message: dict) -> None:
        self.runtime_status.record_receive("feishu", chat_id)

    def _remember_larkx_chat(self, message: dict, chat_id: str) -> None:
        path = self.data_dir / "larkx_chats.json"
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
            temp = path.with_suffix(".tmp")
            temp.write_text(json.dumps({"chats": chats[-500:]}, ensure_ascii=False, indent=2), encoding="utf-8")
            temp.replace(path)
        except Exception as exc:
            self.log_larkx_error("保存会话列表失败", exc)

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



