"""Send through a specific OneBot account and retain its server receipt."""
from __future__ import annotations

import inspect
import re
from collections.abc import Mapping
from typing import Any


class QQRoutingError(RuntimeError):
    """The selected QQ destination cannot be assigned to one live account."""


class QQSendError(RuntimeError):
    """OneBot did not confirm a successful send with a message id."""


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
    from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent

    return await AiocqhttpMessageEvent._parse_onebot_json(chain)


async def send_qq(context: Any, target_umo: str, chain: Any, runtime_status: Any, account_id: str = "", return_account: bool = False) -> str | tuple[str, str]:
    """Send text, image, audio, and video using the destination account.

    Runtime status may be a snapshot, a callable returning one, or the bridge's
    RuntimeStatus object. The latter is refreshed only when account selection
    requires verified group membership.
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
    result = await bot.call_action(action, self_id=identity, **{destination_key: int(destination), "message": segments})
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
