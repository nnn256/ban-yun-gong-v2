"""Send through a specific OneBot account and retain its server receipt."""
from __future__ import annotations

from .build_identity import capture_module_build

MODULE_BUILD = capture_module_build(__file__)

import inspect
import re
import asyncio
import base64
import time
from collections.abc import Mapping
from typing import Any


class QQRoutingError(RuntimeError):
    """The selected QQ destination cannot be assigned to one live account."""


class QQSendError(RuntimeError):
    """The request was not sent or the server explicitly rejected it."""

    def __init__(self, message: str, *, code: str = "qq_rejected", http_status: int = 0):
        super().__init__(message)
        self.code = code
        self.http_status = http_status


class QQSendBlocked(QQSendError):
    """A known pre-send gate; keep queued work without spending its attempts."""


def _gate(state: str, *, http_status: int = 0) -> dict:
    if state not in {"ready", "consent_required", "password_change_required", "auth_required", "unavailable"}:
        state = "unavailable"
    return {"send_ready": state == "ready", "send_state": state,
            "send_checked_at": int(time.time()), "http_status": http_status}


def rejection_code(status: int, payload: Any) -> str:
    """Only export fixed codes, never an untrusted server message."""
    if status == 403 and isinstance(payload, Mapping):
        if payload.get("consentRequired") is True:
            return "consent_required"
        if payload.get("mustChangePassword") is True:
            return "password_change_required"
    return {401: "auth_required", 404: "account_offline", 429: "rate_limited"}.get(status, "qq_rejected")


def install_onebot_result_guard() -> bool:
    """Ignore duplicate/late replies without tearing down reverse WebSockets."""
    try:
        from aiocqhttp.api_impl import ResultStore
    except ImportError:
        return False
    original = ResultStore.add.__func__
    if getattr(original, "_mover_result_guard", False):
        return True

    def add(cls, result):
        if not isinstance(result, dict):
            return
        echo = result.get("echo")
        if not isinstance(echo, dict) or type(echo.get("seq")) is not int:
            return
        future = cls._futures.get(echo["seq"])
        if future is None or future.done():
            return
        try:
            return original(cls, result)
        except asyncio.InvalidStateError:
            # A timeout/cancel may have settled this request already. It must
            # not abort the socket's read loop or replace the first response.
            return

    add._mover_result_guard = True
    ResultStore.add = classmethod(add)
    return True


async def snowluma_send_gate(manager: Any, *, force: bool = False) -> dict:
    """Probe permission to send using read-only APIs in the owner's session.

    /api/status remains available behind both agreement and password gates.
    It therefore proves liveness, not permission to invoke a OneBot action.
    Coalesce status polling and send checks; never record consent here.
    """
    cached = getattr(manager, "_mover_send_gate", None)
    api = getattr(manager, "api", None)
    if (not force and cached and cached[0] is api
            and time.monotonic() - cached[1] < (5 if cached[2]["send_ready"] else 2)):
        return dict(cached[2])
    task = getattr(manager, "_mover_send_gate_task", None)
    if task is None or task.done():
        async def check() -> dict:
            api = getattr(manager, "api", None)
            try:
                if not callable(getattr(api, "raw_request", None)):
                    return _gate("auth_required")
                for attempt in range(2):
                    if not getattr(api, "token", "") or attempt:
                        ensure = getattr(manager, "ensure_api", None)
                        if not callable(ensure):
                            return _gate("auth_required")
                        api = await ensure(quiet=True)
                        if not callable(getattr(api, "raw_request", None)):
                            return _gate("auth_required")
                    status, agreements = await api.raw_request("GET", "/api/agreements", timeout=5)
                    if status == 401 and not attempt:
                        continue
                    if status != 200:
                        return _gate(rejection_code(status, agreements), http_status=status)
                    if not isinstance(agreements, Mapping) or type(agreements.get("consentRequired")) is not bool:
                        return _gate("unavailable")
                    if agreements["consentRequired"]:
                        # Use the existing, explicit local preference. New
                        # installations keep the vendor's opt-out default.
                        config = getattr(getattr(manager, "plugin", None), "config", {})
                        accept = getattr(manager, "accept_agreements", None)
                        if isinstance(config, Mapping) and config.get("auto_accept_agreements") is True and callable(accept):
                            accepted = await accept()
                            if not isinstance(accepted, Mapping) or accepted.get("ok") is not True:
                                return _gate("consent_required", http_status=403)
                            status, agreements = await api.raw_request("GET", "/api/agreements", timeout=5)
                            if (status != 200 or not isinstance(agreements, Mapping)
                                    or agreements.get("consentRequired") is not False):
                                return _gate("consent_required", http_status=403)
                        else:
                            return _gate("consent_required", http_status=403)
                    status, auth_state = await api.raw_request("GET", "/api/auth/state", timeout=5)
                    if status == 401 and not attempt:
                        continue
                    if status != 200:
                        return _gate(rejection_code(status, auth_state), http_status=status)
                    if not isinstance(auth_state, Mapping) or type(auth_state.get("mustChangePassword")) is not bool:
                        return _gate("unavailable")
                    return _gate("password_change_required" if auth_state["mustChangePassword"] else "ready",
                                 http_status=403 if auth_state["mustChangePassword"] else 200)
                return _gate("auth_required", http_status=401)
            except asyncio.CancelledError:
                raise
            except Exception:
                return _gate("unavailable")

        task = asyncio.create_task(check())
        manager._mover_send_gate_task = task
    result = await asyncio.shield(task)
    manager._mover_send_gate = (getattr(manager, "api", None), time.monotonic(), result)
    return dict(result)


class QQDeliveryUncertain(QQSendError):
    """A send was attempted but its outcome cannot be safely retried.

    A lost response does not mean QQ did not receive the message. The caller
    must retain its de-duplication key instead of automatically sending again.
    """


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
                    # OneBot 11 specifies singular `message`; SnowLuma and
                    # other adapters also return the established plural form.
                    nodes = response.get("messages", response.get("message")) if isinstance(response, dict) else None
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
    gate = await snowluma_send_gate(manager)
    if not gate["send_ready"]:
        raise QQSendBlocked("QQ 发送条件未就绪，消息保留在本地等待处理。",
                            code=gate["send_state"], http_status=gate["http_status"])
    return manager.api


async def qq_send_gate(context: Any) -> dict:
    """Account-independent gate; account/membership checks still precede sends."""
    try:
        await _snowluma_api(context)
    except QQSendBlocked as exc:
        return _gate(exc.code, http_status=exc.http_status)
    except QQSendError:
        return _gate("unavailable")
    return _gate("ready")


def _explicit_onebot_rejection(payload: Any) -> bool:
    if not isinstance(payload, Mapping) or payload.get("status") != "failed":
        return False
    code = payload.get("retcode")
    data = payload.get("data")
    if isinstance(data, Mapping) and data.get("message_id") not in (None, "", 0, "0"):
        # A failure body carrying a receipt contradicts itself.
        return False
    return isinstance(code, int) and not isinstance(code, bool) and code != 0


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
        raise QQDeliveryUncertain("SnowLuma 发送结果未知，已停止自动重发以避免重复消息。") from exc
    if not isinstance(response, tuple) or len(response) != 2:
        raise QQDeliveryUncertain("SnowLuma 发送响应格式无效，无法确认发送结果。")
    status, payload = response
    if not isinstance(status, int) or isinstance(status, bool):
        raise QQDeliveryUncertain("SnowLuma 发送接口未返回有效状态，无法确认发送结果。")
    # These client errors reject the request before invoking QQ. A timeout or
    # gateway/server error can happen after execution, so it remains uncertain.
    if status in {400, 401, 403, 404, 405, 413, 415, 422, 429}:
        code = rejection_code(status, payload)
        error = QQSendBlocked if code in {"consent_required", "password_change_required", "auth_required"} else QQSendError
        raise error("SnowLuma 发送接口已拒绝本次请求。", code=code, http_status=status)
    if status != 200 or not isinstance(payload, Mapping):
        raise QQDeliveryUncertain("SnowLuma 发送接口未返回可确认的结果。")
    retcode = payload.get("retcode")
    if _explicit_onebot_rejection(payload):
        raise QQSendError("SnowLuma OneBot 已明确拒绝本次发送。", code="onebot_rejected", http_status=200)
    if payload.get("status") != "ok" or not isinstance(retcode, int) or isinstance(retcode, bool) or retcode != 0:
        raise QQDeliveryUncertain("SnowLuma OneBot 未返回可确认的发送结果。")
    result = payload.get("data")
    if not isinstance(result, Mapping):
        raise QQDeliveryUncertain("SnowLuma OneBot 未返回消息回执数据，无法确认发送结果。")
    return result


def _onebot_send_error(exc: Exception) -> QQSendError:
    try:
        from aiocqhttp.exceptions import ActionFailed, ApiNotAvailable
    except ImportError:  # The standalone workbench does not import AstrBot.
        pass
    else:
        # aiocqhttp 1.4.4 raises this before a transport emits the action.
        if isinstance(exc, ApiNotAvailable):
            return QQSendError("OneBot 发送连接已经断开，未提交本次消息。")
        if isinstance(exc, ActionFailed) and _explicit_onebot_rejection(exc.result):
            return QQSendError("OneBot 已明确拒绝本次发送。")
    return QQDeliveryUncertain("OneBot 发送结果未知，已停止自动重发以避免重复消息。")


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
        try:
            result = await bot.call_action(action, self_id=identity, **params)
        except Exception as exc:
            raise _onebot_send_error(exc) from None
    raw_message_id = result.get("message_id") if isinstance(result, Mapping) else None
    # OneBot v11 uses a signed 32-bit message_id; negative IDs are valid.
    # Zero, missing IDs, and non-integer values do not prove delivery.
    message_id_int = 0
    if isinstance(raw_message_id, int) and not isinstance(raw_message_id, bool):
        message_id_int = raw_message_id
    elif isinstance(raw_message_id, str) and re.fullmatch(r"[+-]?[0-9]{1,10}", raw_message_id):
        message_id_int = int(raw_message_id)
    if message_id_int == 0 or not -(2 ** 31) <= message_id_int < 2 ** 31:
        raise QQDeliveryUncertain("OneBot 没有返回有效的消息发送回执，无法确认发送结果。")
    receipt = str(message_id_int)
    return (identity, receipt) if return_account else receipt
