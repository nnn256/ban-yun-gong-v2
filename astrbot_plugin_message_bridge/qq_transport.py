"""Send through a specific OneBot account and retain its server receipt."""
from __future__ import annotations

import inspect
import re
import asyncio
import base64
from collections.abc import Mapping
from typing import Any


class QQRoutingError(RuntimeError):
    """The selected QQ destination cannot be assigned to one live account."""


class QQSendError(RuntimeError):
    """OneBot did not confirm a successful send with a message id."""


def has_forward_segments(segments: Any) -> bool:
    return isinstance(segments, list) and any(isinstance(item, dict) and item.get("type") == "forward" for item in segments)


async def expand_forward_segments(segments: list, bot: Any, account_id: str) -> list[dict]:
    """Fetch OneBot chat records and return their ordered message segments."""
    result: list[dict] = []
    snapshots: dict[str, list] = {}
    active: set[str] = set()
    visited = 0

    async def expand(items: list, depth: int) -> None:
        nonlocal visited
        if depth > 8:
            raise ValueError("QQ 合并转发嵌套过深")
        for segment in items:
            visited += 1
            if visited > 1024 or not isinstance(segment, dict):
                raise ValueError("QQ 合并转发消息数量或格式无效")
            if segment.get("type") != "forward":
                result.append(segment)
                continue
            data = segment.get("data")
            if not isinstance(data, dict):
                raise ValueError("QQ 合并转发缺少资源")
            key = str(data.get("id") or data.get("res_id") or data.get("forward_id") or "")
            if key and key in active:
                raise ValueError("QQ 合并转发包含循环引用")
            nodes = data.get("content") or data.get("messages")
            if not nodes and key:
                if key not in snapshots:
                    response = await bot.call_action("get_forward_msg", self_id=account_id, id=key)
                    if isinstance(response, dict) and isinstance(response.get("data"), dict):
                        response = response["data"]
                    nodes = response.get("messages") if isinstance(response, dict) else None
                    if not isinstance(nodes, list):
                        raise ValueError("QQ 合并转发未返回可读取的聊天记录")
                    snapshots[key] = nodes
                nodes = snapshots[key]
            if not isinstance(nodes, list) or len(nodes) > 1024:
                raise ValueError("QQ 合并转发未包含可读取的消息列表")
            if key:
                active.add(key)
            try:
                for node in nodes:
                    payload = node if isinstance(node, dict) else {}
                    if payload.get("type") == "node" and isinstance(payload.get("data"), dict):
                        payload = payload["data"]
                    content = payload.get("message", payload.get("content"))
                    if not isinstance(content, list):
                        raise ValueError("QQ 合并转发子消息正文格式无效")
                    await expand(content, depth + 1)
            finally:
                if key:
                    active.remove(key)

    await expand(segments, 0)
    return result


async def _runtime_snapshot(source: Any) -> Mapping:
    if isinstance(source, Mapping):
        return source
    getter = source if callable(source) else getattr(source, "refresh", None)
    if not callable(getter):
        raise QQRoutingError("多个 QQ 账号在线，但没有可用于确认群归属的实时状态。")
    result = getter()
    if inspect.isawaitable(result):
        result = await result
    if not isinstance(result, Mapping):
        raise QQRoutingError("QQ 账号状态无效，未发送消息。")
    return result


async def _select_account(bot: Any, platform_id: str, kind: str, destination: str, runtime_status: Any, account_id: str = "") -> str:
    registry = getattr(bot, "_wsr_api_clients", None)
    if not isinstance(registry, dict) or not registry:
        raise QQRoutingError("目标 QQ 实例没有在线的 OneBot API 连接。")
    online = {str(identity) for identity in registry if str(identity).isdigit()}
    if account_id:
        identity = str(account_id)
        if identity not in online:
            raise QQRoutingError("映射指定的 QQ 账号当前没有在线的 API 连接。")
        if kind == "GroupMessage" and runtime_status is not None:
            snapshot = await _runtime_snapshot(runtime_status)
            if not snapshot.get("stale"):
                for account in snapshot.get("qq", {}).get("accounts", []):
                    if (isinstance(account, Mapping) and account.get("platform_id") == platform_id
                            and str(account.get("user_id") or "") == identity
                            and account.get("identity_verified") and account.get("groups_status") == "verified"):
                        if not any(isinstance(group, Mapping) and str(group.get("group_id") or "") == destination
                                   for group in account.get("groups", [])):
                            raise QQRoutingError("映射指定的 QQ 账号不在目标群的已验证成员列表中。")
        return identity
    if len(online) == 1:
        return next(iter(online))
    if not online:
        raise QQRoutingError("QQ API 连接没有明确的登录账号。")
    if kind != "GroupMessage":
        raise QQRoutingError("多个 QQ 账号在线，私聊目标需要明确选择发送账号。")

    snapshot = await _runtime_snapshot(runtime_status)
    if snapshot.get("stale"):
        raise QQRoutingError("QQ 群列表状态已过期，未发送消息。")
    candidates = set()
    for account in snapshot.get("qq", {}).get("accounts", []):
        if not isinstance(account, Mapping):
            continue
        identity = str(account.get("user_id") or "")
        if (account.get("platform_id") != platform_id or identity not in online
                or not account.get("connected") or not account.get("identity_verified")
                or account.get("groups_status") != "verified"):
            continue
        if any(isinstance(group, Mapping) and str(group.get("group_id") or "") == destination
               for group in account.get("groups", [])):
            candidates.add(identity)
    if len(candidates) != 1:
        if candidates:
            raise QQRoutingError("目标群同时属于多个在线 QQ 账号，请明确选择发送账号。")
        raise QQRoutingError("尚未从在线 QQ 账号的已验证群列表中确认目标群。")
    identity = next(iter(candidates))
    # A refresh can yield to connection cleanup; never use a disconnected id.
    if identity not in {str(key) for key in getattr(bot, "_wsr_api_clients", {})}:
        raise QQRoutingError("选中的 QQ 账号已断开，未发送消息。")
    return identity


async def _onebot_segments(chain: Any) -> list[dict]:
    from astrbot.api.event import MessageChain
    from astrbot.api.message_components import Record
    from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent
    from .larkx_media import attachment_bytes

    segments = []
    for component in chain.chain:
        if isinstance(component, Record):
            # SnowLuma owns its native SILK conversion. AstrBot's generic audio
            # resolver requires a separate ffmpeg.exe and can reject valid Opus.
            data = await asyncio.to_thread(attachment_bytes, component)
            segments.append({"type": "record", "data": {"file": "base64://" + base64.b64encode(data).decode("ascii")}})
        else:
            part = MessageChain()
            part.chain.append(component)
            segments.extend(await AiocqhttpMessageEvent._parse_onebot_json(part))
    return segments


async def _snowluma_api(context: Any) -> Any | None:
    getter = getattr(context, "get_registered_star", None)
    if not callable(getter):
        return None
    try:
        metadata = getter("pulids_snowluma_go_to_astrbot")
        if inspect.isawaitable(metadata):
            metadata = await metadata
    except Exception as exc:
        raise QQSendError("无法确认 SnowLuma 管理器状态，未切换其他发送通道。") from exc
    plugin = getattr(metadata, "star_cls", None)
    manager = getattr(plugin, "manager", None)
    if manager is None:
        return None
    if getattr(metadata, "activated", True) is False:
        raise QQSendError("SnowLuma 管理器所属插件未启用，未切换其他发送通道。")
    api = getattr(manager, "api", None)
    if not callable(getattr(api, "raw_request", None)):
        raise QQSendError("SnowLuma 管理器的已认证会话尚未就绪，未切换其他发送通道。")
    return api


async def _send_through_snowluma(api: Any, identity: str, action: str, params: dict) -> Mapping:
    try:
        response = await api.raw_request(
            "POST", "/api/debug/invoke",
            json_body={"uin": identity, "action": action, "params": params},
            timeout=120,
        )
    except Exception as exc:
        # Manager errors may contain session credentials or raw submitted media.
        # A failed request must not become a second send through another transport.
        raise QQSendError("SnowLuma 发送请求未确认成功，未切换其他通道重发。") from exc
    if not isinstance(response, tuple) or len(response) != 2:
        raise QQSendError("SnowLuma 发送响应格式无效，未切换其他通道重发。")
    status, payload = response
    if not isinstance(status, int) or isinstance(status, bool) or status != 200 or not isinstance(payload, Mapping):
        raise QQSendError("SnowLuma 发送接口未返回成功响应，未切换其他通道重发。")
    retcode = payload.get("retcode")
    if payload.get("status") != "ok" or not isinstance(retcode, int) or isinstance(retcode, bool) or retcode != 0:
        raise QQSendError("SnowLuma OneBot 未确认发送成功，未切换其他通道重发。")
    result = payload.get("data")
    if not isinstance(result, Mapping):
        raise QQSendError("SnowLuma OneBot 未返回消息回执数据，未切换其他通道重发。")
    return result


async def send_qq(context: Any, target_umo: str, chain: Any, runtime_status: Any, account_id: str = "", return_account: bool = False) -> str | tuple[str, str]:
    """Send text, image, audio, and video using the destination account.

    Runtime status may be a snapshot, a callable returning one, or the bridge's
    RuntimeStatus object. The latter is refreshed only when account selection
    requires verified group membership. The registered SnowLuma manager owns
    the primary media-capable send session; an existing manager failure must
    never cause a second send through the generic event adapter.
    """
    try:
        platform_id, kind, destination = target_umo.split(":", 2)
    except (ValueError, AttributeError):
        raise QQRoutingError("QQ 目标会话格式无效。") from None
    if not platform_id or kind not in {"GroupMessage", "FriendMessage"} or not destination.isdigit():
        raise QQRoutingError("QQ 目标必须是平台实例与数字群号或好友账号。")
    getter = getattr(getattr(context, "platform_manager", None), "get_insts", None)
    if not callable(getter):
        raise QQRoutingError("AstrBot 平台管理器不可用。")
    matches = []
    for adapter in getter():
        metadata = adapter.meta()
        if metadata.id == platform_id and metadata.name == "aiocqhttp":
            matches.append(adapter)
    if len(matches) != 1:
        raise QQRoutingError("未找到唯一的目标 OneBot 平台实例。")
    bot = matches[0].get_client()
    identity = await _select_account(bot, platform_id, kind, destination, runtime_status, account_id)
    segments = await _onebot_segments(chain)
    if not segments:
        raise QQSendError("消息中没有可发送的内容。")
    action = "send_group_msg" if kind == "GroupMessage" else "send_private_msg"
    destination_key = "group_id" if kind == "GroupMessage" else "user_id"
    params = {destination_key: int(destination), "message": segments}
    api = await _snowluma_api(context)
    if api is not None:
        result = await _send_through_snowluma(api, identity, action, params)
    else:
        result = await bot.call_action(action, self_id=identity, **params)
    raw_message_id = result.get("message_id") if isinstance(result, Mapping) else None
    # OneBot v11 uses a signed 32-bit message_id; negative IDs are valid.
    # Zero, missing IDs, and non-integer values do not prove delivery.
    message_id_int = 0
    if isinstance(raw_message_id, int) and not isinstance(raw_message_id, bool):
        message_id_int = raw_message_id
    elif isinstance(raw_message_id, str) and re.fullmatch(r"[+-]?[0-9]+", raw_message_id):
        message_id_int = int(raw_message_id)
    if message_id_int == 0:
        raise QQSendError("OneBot 没有返回有效的消息发送回执。")
    receipt = str(message_id_int)
    return (identity, receipt) if return_account else receipt
