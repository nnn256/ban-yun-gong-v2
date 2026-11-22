from __future__ import annotations

from .build_identity import capture_module_build

MODULE_BUILD = capture_module_build(__file__)

import asyncio
import json
import os
import sqlite3
import sys
import time
import threading
from collections import deque
from pathlib import Path
from typing import Any

from .bridge_core import SentMessageIds, component_content_type
from . import larkx_media
from .qq_emojis import find_face_image
from .larkx_history import BUSY_SECONDS, POLL_SECONDS, LarkXHistory
from .larkx_inbox import RECOVERY_EPOCH, LarkXInbox
from .larkx_merge import is_system_message
from .larkx_receive import (
    MAX_NESTED_SID_DEPTH, READ_BATCH_SIZE, batch_copy_work, completion_preserves_media,
    decode_push_messages, needs_message_completion, normalized_sids, reliable_client_class,
)


RECEIVE_WORKERS = 3
INGRESS_TRACE_LIMIT = 256


class _ConnectionChanged(RuntimeError):
    pass


class _RecoveryRoutesChanged(RuntimeError):
    pass


class _SendGatePending(RuntimeError):
    def __init__(self, code: str):
        self.code = code


async def _settle_task(task: asyncio.Task) -> Any:
    """Let a started disk operation finish despite repeated cancellation.

    Cancelling a to_thread wrapper cannot stop SQLite work already executing.
    Closing its connection before that work settles races with shutdown.
    """
    while True:
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            if task.done():
                return task.result()


class LarkAgentXBridge:
    """Personal Feishu receive/send bridge using LarkAgentX with its agent disabled."""

    def __init__(self, plugin: Any) -> None:
        self.plugin = plugin
        self.account_id = os.environ.get("MOVER_LARKX_ACCOUNT", "default")
        self._stop = asyncio.Event()
        self._task: asyncio.Task | None = None
        self.client = None
        self.user_id = ""
        self._last_state = ""
        self._send_lock = threading.RLock()
        self._sent_ids = SentMessageIds(ttl_seconds=7 * 86400, max_entries=100000,
                                        path=Path(plugin.data_dir) / "larkx_receipts.sqlite3")
        self._inbox: LarkXInbox | None = None
        self._inbox_task: asyncio.Task | None = None
        self._inbox_wakeup = asyncio.Event()
        self._ingress_traces: deque[tuple[str, dict, str]] = deque()
        self._ingress_trace_task: asyncio.Task | None = None
        self._ingress_trace_omitted = 0
        self._connection_workers: set[asyncio.Task] = set()
        self._history: LarkXHistory | None = None
        self._history_ready: asyncio.Event | None = None
        self._history_identity: tuple[str, str] | None = None
        self._recovery_status = {"state": "offline", "baselined": 0, "configured": 0,
                                 "missing": 0, "recovered": 0, "errors": 0, "checked_at": 0}

    def _set_state(self, state: str, message: str) -> None:
        self._last_state = state
        status_path = Path(os.environ.get("LARKX_HOME", "")) / "bridge-status.json"
        try:
            status_path.parent.mkdir(parents=True, exist_ok=True)
            temp = status_path.with_suffix(".tmp")
            temp.write_text(
                json.dumps({"state": state, "message": message, "updated_at": int(time.time()), "agent_backend": "none"}, ensure_ascii=False),
                encoding="utf-8",
            )
            temp.replace(status_path)
        except Exception:
            pass
        runtime_status = getattr(self.plugin, "runtime_status", None)
        if runtime_status is not None:
            runtime_status.update_feishu()
        self.plugin.log_larkx_state(message)

    def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._stop.clear()
        if self._inbox is None:
            self._inbox = LarkXInbox(Path(self.plugin.data_dir) / "larkx-inbox.sqlite3")
        self._inbox_task = asyncio.create_task(self._receive_worker(), name="mover-larkagentx-inbox")
        self._task = asyncio.create_task(self._run(), name="mover-larkagentx-listener")

    async def stop(self) -> None:
        self._stop.set()
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        self._task = None
        if self._inbox_task and not self._inbox_task.done():
            self._inbox_task.cancel()
            try:
                await self._inbox_task
            except (asyncio.CancelledError, Exception):
                pass
        self._inbox_task = None
        if self._ingress_trace_task is not None:
            self._ingress_trace_task.cancel()
            await asyncio.gather(self._ingress_trace_task, return_exceptions=True)
            self._ingress_trace_task = None
        self._ingress_traces.clear()
        self._ingress_trace_omitted = 0
        if self._inbox is not None:
            self._inbox.close()
            self._inbox = None
        self.client = None
        self.user_id = ""
        self._set_state("stopped", "飞书监听已停止")

    def inbox_status(self) -> dict[str, int]:
        return self._inbox.stats() if self._inbox is not None else {"pending": 0, "processing": 0, "failed": 0, "done": 0}

    def recovery_status(self) -> dict:
        return dict(self._recovery_status)

    def recovery_message_current(self, payload: dict) -> bool:
        epoch = payload.get(RECOVERY_EPOCH)
        if not epoch:
            return True
        chat = str(payload.get("chat_id") or "")
        scope = self._history.scopes.get(chat) if self._history else None
        return bool(scope and scope.epoch == epoch
                    and self.plugin.larkx_recovery_chats(self.account_id).get(chat) == scope.signature)

    def _set_recovery_status(self, **changes: Any) -> None:
        self._recovery_status = {**self._recovery_status, **changes}

    async def _recover_history(self, client: Any, account: str, user: str, history: LarkXHistory,
                               ready: asyncio.Event) -> None:
        inbox = self._inbox
        if inbox is None or not user:
            ready.set()
            return
        reconnect = True
        while not self._stop.is_set():
            def check_connection() -> None:
                if (self._stop.is_set() or self.client is not client or self._history is not history
                        or (self.account_id, self.user_id) != (account, user)):
                    raise _ConnectionChanged()

            delay = POLL_SECONDS
            try:
                check_connection()
                routes = self.plugin.larkx_recovery_chats(account)
                await history.sync(inbox, self._inbox_call, account, user, routes)
                check_connection()

                def check_scope() -> None:
                    check_connection()
                    if self.plugin.larkx_recovery_chats(account) != routes:
                        raise _RecoveryRoutesChanged()

                check_scope()
                ready.set()
                status, busy = await history.poll(inbox, client, self._inbox_call, check_scope,
                                                  account, user, reconnect=reconnect)
                reconnect = False
                previous = self._recovery_status
                if history.recovered > previous["recovered"]:
                    self._inbox_wakeup.set()
                self._set_recovery_status(**status, configured=len(routes))
                trace = getattr(self.plugin, "trace_larkx", None)
                if trace is not None:
                    for field, state in (("baselined", "baseline"), ("recovered", "recovered"), ("missing", "gap")):
                        if status[field] > previous[field]:
                            await trace("larkx_history", state, count=(status[field] if field == "missing"
                                                                     else status[field] - previous[field]), content_type="")
                if busy:
                    delay = BUSY_SECONDS
            except _ConnectionChanged:
                return
            except _RecoveryRoutesChanged:
                delay = BUSY_SECONDS
            except Exception as exc:
                if self._recovery_status["state"] != "degraded":
                    self.plugin.log_larkx_state(f"飞书会话补收暂不可用，将自动重查（{type(exc).__name__}）")
                self._set_recovery_status(state="degraded", errors=1, checked_at=int(time.time()))
            await asyncio.sleep(delay)

    def retry_failed(self) -> int:
        count = self._inbox.retry_failed() if self._inbox is not None else 0
        self._inbox_wakeup.set()
        return count

    def send_text(self, chat_id: str, text: str) -> bool:
        client = self.client
        if client is None or not text.strip():
            return False
        scope = self._receipt_scope(chat_id)
        with self._send_lock:
            return self._send_text(client, scope, chat_id, text)

    def _receipt_scope(self, chat_id: str) -> str:
        return json.dumps([self.account_id, self.user_id, str(chat_id)], separators=(",", ":"))

    def _remember_receipt(self, scope: str, message_id: str) -> None:
        try:
            self._sent_ids.add(scope, str(message_id))
        except Exception as exc:
            raise larkx_media.NativeMediaDeliveryUncertain("飞书发送已返回回执，但本地回执保存失败，未自动重发") from exc

    def _send_text(self, client: Any, scope: str, chat_id: str, text: str) -> bool:
        try:
            message_id = client.send_msg(text, str(chat_id))
        except Exception as exc:
            raise larkx_media.NativeMediaDeliveryUncertain("飞书文字发送结果不确定，未自动重发") from exc
        if not message_id:
            raise larkx_media.NativeMediaDeliveryUncertain("飞书文字发送未返回回执，未自动重发")
        self._remember_receipt(scope, str(message_id))
        return True

    def send_chain(self, chat_id: str, chain: Any) -> bool:
        """Preserve text and native images without displaying bridge metadata."""
        client = self.client
        if client is None:
            return False
        scope = self._receipt_scope(chat_id)
        accepted = False
        text_parts: list[str] = []
        with self._send_lock:
            def flush_text() -> bool:
                nonlocal accepted
                value = "".join(text_parts)
                text_parts.clear()
                if value.strip():
                    if not self._send_text(client, scope, chat_id, value):
                        return False
                    accepted = True
                return True

            try:
                for component in chain.chain:
                    kind = type(component).__name__.lower()
                    if kind == "face":
                        face_path = find_face_image(getattr(component, "id", ""))
                        if face_path is None:
                            text_parts.append("[QQ表情]")
                            continue
                        if not flush_text():
                            return False
                        message_id = larkx_media.send_image(client, chat_id, face_path)
                        if not message_id:
                            return False
                        self._remember_receipt(scope, str(message_id))
                        accepted = True
                    elif component_content_type(component) in {"image", "emoji", "record", "video", "audio", "file"}:
                        if not flush_text():
                            return False
                        media_kind = component_content_type(component)
                        if media_kind in {"image", "emoji"}:
                            message_id = larkx_media.send_image(client, chat_id, component)
                        elif media_kind in {"audio", "video"}:
                            message_id = larkx_media.send_native_media(client, chat_id, component, kind=media_kind)
                        else:
                            message_id = larkx_media.send_attachment(client, chat_id, component, kind=media_kind)
                        if not message_id:
                            return False
                        self._remember_receipt(scope, str(message_id))
                        accepted = True
                    elif kind == "at":
                        mentioned = (getattr(component, "name", "") or getattr(component, "qq", "")
                                     or getattr(component, "user_id", ""))
                        if mentioned:
                            text_parts.append(f"@{mentioned}")
                    else:
                        value = getattr(component, "text", None)
                        if isinstance(value, str):
                            text_parts.append(value)
                return flush_text() and accepted
            except larkx_media.NativeMediaDeliveryUncertain:
                raise
            except Exception as exc:
                self.plugin.log_larkx_error("发送飞书图片或文字失败", exc)
                return False

    def is_outgoing(self, chat_id: str, message_id: str) -> bool:
        # The receive path runs this in a worker, so a websocket echo arriving
        # before send() returns waits for its receipt without blocking asyncio.
        with self._send_lock:
            return self._sent_ids.contains(self._receipt_scope(chat_id), str(message_id))

    def download_image(self, message: dict) -> bytes:
        return larkx_media.download_image(self.client, message)

    def download_attachment(self, message: dict) -> tuple[bytes, str, str]:
        return larkx_media.download_attachment(self.client, message)

    def forward_message(self, chat_id: str, message_id: str) -> bool:
        client = self.client
        if client is None:
            return False
        scope = self._receipt_scope(chat_id)
        with self._send_lock:
            try:
                receipts = client.forward([str(message_id)], str(chat_id), merge=False)
            except Exception as exc:
                raise larkx_media.NativeMediaDeliveryUncertain("飞书原生转发结果不确定，未自动重发") from exc
            if not receipts:
                raise larkx_media.NativeMediaDeliveryUncertain("飞书原生转发未返回回执，未自动重发")
            for receipt in receipts:
                self._remember_receipt(scope, str(receipt))
            return bool(receipts)

    async def _enqueue(self, kind: str, payload: dict, account_id: str | None = None,
                       user_id: str | None = None) -> None:
        if self._inbox is None:
            raise RuntimeError("飞书本地收件箱尚未启动")
        commit = asyncio.create_task(asyncio.to_thread(
            self._inbox.enqueue, kind, payload,
            self.account_id if account_id is None else account_id,
            self.user_id if user_id is None else user_id))
        try:
            inserted = await asyncio.shield(commit)
        except asyncio.CancelledError:
            await _settle_task(commit)
            raise
        self._inbox_wakeup.set()
        if kind == "message" and larkx_media.is_image_message(payload):
            trace = getattr(self.plugin, "trace_larkx", None)
            if trace is not None and not self._stop.is_set():
                # Disk confirmation gates the ACK; diagnostic writes never do.
                if len(self._ingress_traces) >= INGRESS_TRACE_LIMIT:
                    self._ingress_trace_omitted += 1
                else:
                    if payload.get("msg_id") or payload.get("message_id"):
                        payload = {key: payload[key] for key in ("chat_id", "msg_id", "message_id") if key in payload}
                    self._ingress_traces.append(("persisted" if inserted else "duplicate", payload,
                                                 self.account_id if account_id is None else account_id))
                if self._ingress_trace_task is None or self._ingress_trace_task.done():
                    self._ingress_trace_task = asyncio.create_task(
                        self._write_ingress_traces(trace), name="mover-larkagentx-ingress-trace")

    async def _write_ingress_traces(self, trace: Any) -> None:
        while self._ingress_traces or self._ingress_trace_omitted:
            try:
                if self._ingress_traces:
                    state, payload, account = self._ingress_traces.popleft()
                    await trace("larkx_ingress", state, payload, account)
                else:
                    omitted, self._ingress_trace_omitted = self._ingress_trace_omitted, 0
                    await trace("larkx_ingress", "trace_omitted", count=omitted)
            except Exception:
                # The receipt and the inbox are already durable.
                pass

    async def _inbox_call(self, operation: Any, *args: Any, **kwargs: Any) -> Any:
        """Keep transient database trouble from terminating all consumers."""
        warned = False
        while True:
            write = asyncio.create_task(asyncio.to_thread(operation, *args, **kwargs))
            try:
                return await asyncio.shield(write)
            except asyncio.CancelledError:
                await _settle_task(write)
                raise
            except (sqlite3.Error, OSError) as exc:
                if not warned:
                    self.plugin.log_larkx_state(
                        f"飞书收件箱暂时无法读写，记录保留并等待存储恢复（{type(exc).__name__}）")
                    warned = True
                if self._stop.is_set():
                    raise
                await asyncio.sleep(1)

    async def _receive_worker(self) -> None:
        """Read/download/send away from the websocket and its batch callbacks."""
        workers = [asyncio.create_task(self._receive_one_worker(), name=f"mover-larkagentx-receive-{index}")
                   for index in range(RECEIVE_WORKERS)]
        try:
            await asyncio.gather(*workers)
        finally:
            for worker in workers:
                # gather() already propagates cancellation. A second cancel
                # can interrupt a worker while it releases a claimed row.
                if not worker.done() and not worker.cancelling():
                    worker.cancel()
            await asyncio.gather(*workers, return_exceptions=True)

    async def _receive_one_worker(self) -> None:
        # A temporarily unavailable image cannot occupy the whole receive
        # queue. Downloads are bounded here; QQ's delivery lock still serializes
        # actual sends. A recovered image may arrive after a later ready image.
        while not self._stop.is_set():
            self._inbox_wakeup.clear()
            inbox = self._inbox
            if inbox is None:
                return
            if self.client is None:
                item = None
            else:
                claim = asyncio.create_task(self._inbox_call(inbox.claim, account_id=self.account_id, user_id=self.user_id))
                try:
                    item = await asyncio.shield(claim)
                except asyncio.CancelledError:
                    claimed = await _settle_task(claim)
                    if claimed is not None:
                        await _settle_task(asyncio.create_task(self._inbox_call(
                            inbox.release, claimed["id"], undo_attempt=True)))
                    raise
            if item is None:
                try:
                    await asyncio.wait_for(self._inbox_wakeup.wait(), timeout=1)
                except asyncio.TimeoutError:
                    pass
                continue
            try:
                connection_client = self.client
                # claim() yields to a disk thread. The old connection may
                # have finished cleanup while it was in flight; register no
                # work after that cleanup without a valid matching client.
                if (connection_client is None or item["account_id"] != self.account_id
                        or item["user_id"] != self.user_id):
                    raise _ConnectionChanged()
                processing = asyncio.create_task(self._process_inbox(item))
                self._connection_workers.add(processing)
                try:
                    success = await processing
                except asyncio.CancelledError:
                    if asyncio.current_task().cancelling():
                        raise
                    # _run cancels in-flight tasks before replacing/closing a
                    # client; return them to the account's inbox unchanged.
                    await _settle_task(asyncio.create_task(self._inbox_call(
                        inbox.release, item["id"], undo_attempt=True)))
                    continue
                finally:
                    self._connection_workers.discard(processing)
                if self.client is not connection_client:
                    raise _ConnectionChanged()
                if success:
                    await self._inbox_call(inbox.complete, item["id"])
                else:
                    gate = getattr(self.plugin, "larkx_send_gate", None)
                    blocked = (await gate(item["payload"], item["account_id"], include_mixed=True)) if callable(gate) and item["kind"] == "message" else ""
                    if blocked:
                        await self._inbox_call(inbox.defer, item["id"], blocked)
                    else:
                        await self._retry_inbox(inbox, item, "DeliveryNotConfirmed")
            except asyncio.CancelledError:
                await _settle_task(asyncio.create_task(self._inbox_call(inbox.release, item["id"])))
                raise
            except _ConnectionChanged:
                await self._inbox_call(inbox.release, item["id"], undo_attempt=True)
            except _SendGatePending as exc:
                await self._inbox_call(inbox.defer, item["id"], exc.code)
            except Exception as exc:
                await self._retry_inbox(inbox, item, type(exc).__name__)

    async def _retry_inbox(self, inbox: LarkXInbox, item: dict, error_code: str) -> None:
        pending = await self._inbox_call(inbox.retry, item, error_code)
        if pending:
            self.plugin.log_larkx_state(f"飞书收件待重试：类型 {item['kind']}，第 {item['attempts']} 次处理失败（{error_code}）")
        else:
            self.plugin.log_larkx_state(f"飞书收件重试已用尽：类型 {item['kind']}，已保留本地失败记录，可在工作台重试（{error_code}）")

    async def _process_inbox(self, item: dict) -> bool:
        payload = item["payload"]
        if item["account_id"] != self.account_id or (item["user_id"] and item["user_id"] != self.user_id):
            raise _ConnectionChanged()
        if item["kind"] == "message":
            if is_system_message(payload) or payload.get("is_removed") is True:
                return True
            gate = getattr(self.plugin, "larkx_send_gate", None)
            if callable(gate):
                blocked = await gate(payload, item["account_id"])
                if blocked:
                    raise _SendGatePending(blocked)
            if payload.get(RECOVERY_EPOCH):
                ready = self._history_ready
                if ready is None:
                    raise _ConnectionChanged()
                await ready.wait()
                if ready is not self._history_ready:
                    raise _ConnectionChanged()
                if not self.recovery_message_current(payload):
                    return True
            if needs_message_completion(payload):
                client = self.client
                if client is None:
                    return False
                connection_identity = (self.account_id, self.user_id)
                message_id = str(payload.get("msg_id") or payload.get("message_id") or "")
                chat_id, sender_id = str(payload.get("chat_id") or ""), str(payload.get("from_id") or "")
                if not message_id or not chat_id or not sender_id:
                    return False
                messages = await asyncio.to_thread(client.messages_by_ids, [message_id])
                if self.client is not client or (self.account_id, self.user_id) != connection_identity:
                    raise _ConnectionChanged()
                matches = [message for message in messages if isinstance(message, dict)
                           and str(message.get("msg_id") or message.get("message_id") or "") == message_id]
                if len(matches) != 1:
                    return False
                complete = matches[0]
                if (str(complete.get("chat_id") or "") != chat_id
                        or str(complete.get("from_id") or "") != sender_id):
                    return False
                if is_system_message(complete) or complete.get("is_removed") is True:
                    return True
                if (complete.get("is_truncated") is not False or needs_message_completion(complete)
                        or not completion_preserves_media(payload, complete)):
                    return False
                payload = {**payload, **complete}
                # Save the hydrated body before media/send work. A later retry
                # or process restart uses the complete record, never a preview.
                await self._inbox_call(self._inbox.update_payload, item["id"], payload)
                if self.client is not client or (self.account_id, self.user_id) != connection_identity:
                    raise _ConnectionChanged()
            if not self.recovery_message_current(payload):
                return True
            result = await self.plugin.on_larkx_message(payload, item["account_id"], item["user_id"])
            return result is not False
        client = self.client
        if client is None:
            return False
        connection_identity = (self.account_id, self.user_id)

        def check_connection() -> None:
            if self.client is not client or (self.account_id, self.user_id) != connection_identity:
                raise _ConnectionChanged()

        if item["kind"] == "batch":
            wanted = [str(value) for value in payload["ids"]]
            messages = list(payload.get("embedded") or [])
            have = {str(message.get("msg_id") or message.get("message_id") or "") for message in messages}
            missing = [value for value in wanted if value not in have]
            failure = None
            for offset in range(0, len(missing), READ_BATCH_SIZE):
                try:
                    messages.extend(await asyncio.to_thread(client.messages_by_ids, missing[offset:offset + READ_BATCH_SIZE]))
                except Exception as exc:
                    failure = exc
                check_connection()
            have = set()
            for message in sorted(messages, key=lambda value: int(value.get("position") or 0)):
                message_id = str(message.get("msg_id") or message.get("message_id") or "")
                if message_id not in wanted:
                    continue
                if not is_system_message(message) and message.get("is_removed") is not True:
                    if not message.get("from_id"):
                        continue
                    await self._enqueue("message", message, item["account_id"], item["user_id"])
                    check_connection()
                have.add(message_id)
            missing = [value for value in wanted if value not in have]
            if missing:
                await self._inbox_call(self._inbox.update_payload, item["id"], {"ids": missing})
                if failure is not None:
                    raise failure
                return False
            return True
        if item["kind"] == "sid":
            return await self._process_sid(item, client, check_connection)
        raise ValueError("飞书本地收件类型无效")

    async def _process_sid(self, item: dict, client: Any, check_connection: Any) -> bool:
        from google.protobuf.message import DecodeError
        from larkx.client import CMD_BATCH_COPY, CMD_PUSH_MESSAGES, CMD_PUSH_SID

        payload = item["payload"]
        wanted = normalized_sids(payload["sids"])
        path = normalized_sids(payload.get("sid_path", []))
        if not wanted or len(wanted) > READ_BATCH_SIZE or len(path) > MAX_NESTED_SID_DEPTH:
            return False
        response = await asyncio.to_thread(client.api, "improto.PullPacketsBySidsRequest", {"sids": wanted})
        check_connection()
        packets = response.get("packets") if isinstance(response, dict) else None
        if not isinstance(packets, dict):
            return False

        unresolved = []
        for sid in wanted:
            packet = packets.get(sid)
            # The request SID is the response map key. Older packets may
            # omit their own SID; a conflicting packet must never be used.
            if sid in path or not isinstance(packet, dict) or packet.get("sid", sid) != sid:
                unresolved.append(sid)
                continue
            command = packet.get("cmd")
            body = packet.get("payload")
            work = []
            resolved = False
            try:
                if command == CMD_PUSH_MESSAGES and isinstance(body, bytes) and body:
                    messages = decode_push_messages(body)
                    resolved = bool(messages)
                    for message in messages:
                        if is_system_message(message) or message.get("is_removed") is True:
                            continue
                        if not message.get("from_id") or not (message.get("msg_id") or message.get("message_id")):
                            resolved = False
                            continue
                        work.append(("message", message))
                elif command == CMD_BATCH_COPY and isinstance(body, bytes) and body:
                    batch = batch_copy_work(body)
                    if batch["ids"]:
                        work.append(("batch", batch))
                        resolved = True
                elif command == CMD_PUSH_SID and len(path) < MAX_NESTED_SID_DEPTH:
                    # In a pulled Packet the packet SID identifies this
                    # response. Only compactedSids points to additional work.
                    children = normalized_sids(packet.get("compactedSids", []))
                    child_path = [*path, sid]
                    acyclic = [value for value in children if value not in child_path]
                    resolved = bool(children) and len(acyclic) == len(children)
                    for offset in range(0, len(acyclic), READ_BATCH_SIZE):
                        work.append(("sid", {"sids": acyclic[offset:offset + READ_BATCH_SIZE],
                                             "sid_path": child_path}))
            except (DecodeError, ValueError):
                # A corrupt packet cannot make valid sibling SIDs disappear.
                resolved = False
            for kind, child in work:
                await self._enqueue(kind, child, item["account_id"], item["user_id"])
                check_connection()
            if not resolved:
                unresolved.append(sid)

        if unresolved:
            if unresolved != payload["sids"]:
                await self._inbox_call(self._inbox.update_payload, item["id"], {**payload, "sids": unresolved})
                check_connection()
            return False
        return True

    async def _run(self) -> None:
        while not self._stop.is_set():
            client = None
            history_task = None
            retry = 5
            try:
                source = os.environ.get("LARKX_SOURCE", "").strip()
                if source and source not in sys.path:
                    sys.path.insert(0, source)
                os.environ["LARKX_AGENT_BACKEND"] = "none"
                from larkx.accounts import credentials_path
                from larkx.auth import LarkAuth
                from larkx.client import LarkClient

                if not credentials_path(self.account_id).is_file():
                    self.user_id = ""
                    if self._last_state != "waiting_login":
                        self._set_state("waiting_login", "等待飞书账号扫码登录")
                    await asyncio.sleep(10)
                    continue

                auth = await asyncio.to_thread(LarkAuth, self.account_id)
                valid, message = await asyncio.to_thread(auth.validate, fetch_profile=True)
                if not valid:
                    self.user_id = ""
                    if self._last_state != "login_required":
                        self._set_state("login_required", "飞书登录已失效，请在工作台重新扫码")
                    await asyncio.sleep(10)
                    continue
                auth.save()
                astrbot_loop = asyncio.get_running_loop()
                connection_account = self.account_id
                connection_user = str(auth.user_id or "")
                async def enqueue_read(kind: str, payload: dict, _account: str = connection_account,
                                       _user: str = connection_user, _loop: Any = astrbot_loop) -> None:
                    future = asyncio.run_coroutine_threadsafe(
                        self._enqueue(kind, payload, _account, _user), _loop)
                    await asyncio.wrap_future(future)

                client_type = reliable_client_class(LarkClient, enqueue_read)
                client = await asyncio.to_thread(client_type, auth)

                def on_connected() -> None:
                    nonlocal history_task
                    self.client = client
                    self.user_id = connection_user
                    identity = (connection_account, connection_user)
                    history = LarkXHistory()
                    if self._history_identity == identity:
                        history.recovered = self._recovery_status["recovered"]
                    else:
                        self._set_recovery_status(baselined=0, configured=0, missing=0, recovered=0, errors=0, checked_at=0)
                        self._history_identity = identity
                    ready = asyncio.Event()
                    self._history, self._history_ready = history, ready
                    self._set_recovery_status(state="checking")
                    history_task = asyncio.create_task(
                        self._recover_history(client, *identity, history, ready), name="mover-larkagentx-history")
                    self._inbox_wakeup.set()
                    self._set_state("running", "飞书个人账号已验证，消息监听已启动")

                async def on_message(message: dict, _account: str = connection_account,
                                     _user: str = connection_user, _loop: Any = astrbot_loop) -> None:
                    future = asyncio.run_coroutine_threadsafe(
                        self._enqueue("message", message, _account, _user),
                        _loop,
                    )
                    await asyncio.wrap_future(future)

                await client.connect_websocket(on_message, on_connected=on_connected)
                if not self._stop.is_set():
                    self._set_state("reconnecting", "飞书连接已关闭，5 秒后自动重连")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if not self._stop.is_set() and self._last_state != "reconnecting":
                    self._set_state("reconnecting", f"飞书消息监听中断，将自动重连（{type(exc).__name__}）")
            finally:
                self.client = None
                self._history, self._history_ready = None, None
                self._set_recovery_status(state="offline")
                processing = list(self._connection_workers)
                if history_task is not None:
                    processing.append(history_task)
                for task in processing:
                    task.cancel()
                if processing:
                    await asyncio.gather(*processing, return_exceptions=True)
                runtime_status = getattr(self.plugin, "runtime_status", None)
                if runtime_status is not None:
                    runtime_status.update_feishu()
                if client is not None:
                    try:
                        await asyncio.to_thread(client.close)
                    except Exception:
                        pass
            if not self._stop.is_set():
                await asyncio.sleep(retry)


def larkx_source_path() -> Path:
    configured = os.environ.get("LARKX_SOURCE", "").strip()
    if configured:
        return Path(configured)
    return Path(__file__).resolve().parents[5] / "runtime" / "LarkAgentX-runtime" / "LarkAgentX-d288ef4df8a42f6286c5506b7185f64430071490"
