from __future__ import annotations

import asyncio
import json
from pathlib import Path
from datetime import datetime, timezone
from tempfile import TemporaryDirectory

from astrbot import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star, StarTools, register

from .bridge_core import (
    DEFAULT_MANAGED_PLATFORMS,
    BridgeRouter,
    SeenEvents,
    SentMessageIds,
    make_event_key,
)
from .larkx_bridge import LarkAgentXBridge
from .runtime_status import RuntimeStatus
from .larkx_media import is_image_message, attachment_kind
from .qq_transport import send_qq
from .qq_emojis import find_face_image


@register(
    "astrbot_plugin_message_bridge",
    "搬运工V2",
    "在 AstrBot 的 QQ/OneBot 与飞书会话之间双向转发；不直接调用 AI。",
    "2.0.0",
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

    @filter.on_astrbot_loaded()
    async def start_personal_feishu_listener(self) -> None:
        if self.larkx_bridge:
            self.larkx_bridge.start()
        self.runtime_status.start()

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
        if platform == "aiocqhttp":
            self.runtime_status.record_receive("qq", str(event.get_session_id()))
        if not self.router.enabled:
            return

        sender_id = event.get_sender_id()
        message_obj = getattr(event, "message_obj", None)
        async with self._qq_delivery_lock:
            source_account_id = str(getattr(message_obj, "self_id", ""))
            if self.sent_qq.contains(str(event.get_platform_id()), str(getattr(message_obj, "message_id", "")), source_account_id, str(event.get_session_id())):
                return

        message_text = event.get_message_str() or ""
        source_account_id = str(getattr(message_obj, "self_id", ""))
        own_message = bool(sender_id and str(sender_id) == source_account_id)
        message_type = getattr(event.get_message_type(), "value", "")
        platform_id = str(event.get_platform_id())
        session_id = str(event.get_session_id())
        routes = self.router.find_routes(
            platform, platform_id, message_type, session_id,
            source_account_id,
        )
        routes = [] if own_message else routes
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
        if not self.seen.accept(dedup_key):
            event.stop_event()
            return

        chain = self._build_forward_chain(event, platform)
        for route in routes:
            await self._deliver_chain(route, chain)

    async def _deliver_chain(self, route, chain: MessageChain) -> None:
        delivery_platform = "feishu" if route.target_umo.split(":", 2)[0] == "larkx" else "qq"
        if not chain.chain:
            return
        try:
            target_platform = route.target_umo.split(":", 2)[0]
            if target_platform == "larkx":
                target_chat_id = route.target_umo.split(":", 2)[-1]
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
        except Exception as exc:
            self.runtime_status.record_delivery(delivery_platform, False)
            self.log_larkx_error("消息转发失败", exc)

    async def _send_to_qq(self, route, chain: MessageChain) -> str:
        async with self._qq_delivery_lock:
            account_id, message_id = await send_qq(
                self.context, route.target_umo, chain, self.runtime_status,
                account_id=route.target_account_id, return_account=True,
            )
            session_id = route.target_umo.split(":", 2)[2]
            self.sent_qq.add(route.target_umo.split(":", 1)[0], message_id, account_id, session_id)
            return message_id

    def log_larkx_state(self, message: str) -> None:
        logger.info("搬运工V2 LarkAgentX: %s", message)

    def log_larkx_error(self, message: str, error: Exception) -> None:
        logger.warning("搬运工V2 LarkAgentX: %s (%s)", message, type(error).__name__)

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
            if (not content.strip() and not image_message and not media_kind):
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
            routes = [] if own_message else routes
            if not routes:
                return
            dedup_key = make_event_key(
                f"larkx:{account_id}", chat_id,
                str(message.get("msg_id") or message.get("message_id") or ""),
                str(message.get("from_id") or ""), content,
                message.get("create_time") or message.get("timestamp") or "",
            )
            if not self.seen.accept(dedup_key):
                return
            with TemporaryDirectory(prefix="mover-receive-") as directory:
                chain = MessageChain()
                if image_message or sticker_message:
                    from astrbot.api.message_components import Image
                    data = (await asyncio.to_thread(self.larkx_bridge.download_image, message) if image_message
                            else (await asyncio.to_thread(self.larkx_bridge.download_attachment, message))[0])
                    chain.chain.append(Image.fromBytes(data))
                elif media_kind:
                    from astrbot.api.message_components import Record, Video, File
                    data, filename, kind = await asyncio.to_thread(self.larkx_bridge.download_attachment, message)
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
                for route in routes:
                    target_platform, _, target_chat = route.target_umo.split(":", 2)
                    if target_platform == "larkx" and target_chat == chat_id:
                        continue
                    await self._deliver_chain(route, chain)
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
        if self.larkx_bridge:
            await self.larkx_bridge.stop()
        await self.runtime_status.stop()

    @staticmethod
    def _build_forward_chain(event: AstrMessageEvent, platform: str) -> MessageChain:
        chain = MessageChain()
        components = list(event.get_messages())
        by_type: dict[str, list[Any]] = {}
        for component in components:
            by_type.setdefault(type(component).__name__.lower(), []).append(component)

        def take(kind: str) -> Any | None:
            values = by_type.get(kind, [])
            return values.pop(0) if values else None

        raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
        segments = raw.get("message") if isinstance(raw, dict) else None
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
                        chain.chain.append(Image.fromURL(str(url)))
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
                    if component is not None:
                        chain.chain.append(component)
        else:
            chain.chain.extend(
                component for component in components
                if type(component).__name__.lower() in {"plain", "image", "record", "video", "file", "at", "face"}
            )
        if not chain.chain and event.get_message_str():
            fallback = str(event.get_message_str())
            # AstrBot's string rendering may put a component-class label before
            # reply text when raw OneBot segments are unavailable.
            for label in ("[ComponentType.Reply]", "[ComponentType.At]", "[ComponentType.Face]"):
                fallback = fallback.replace(label, "")
            if fallback.strip():
                chain.message(fallback.strip())
        return chain



