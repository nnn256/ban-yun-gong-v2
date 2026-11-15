from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import threading
from pathlib import Path
from typing import Any

from .bridge_core import SentMessageIds
from . import larkx_media
from .qq_emojis import find_face_image


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
        self._sent_ids = SentMessageIds()

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
        self.client = None
        self.user_id = ""
        self._set_state("stopped", "飞书监听已停止")

    def send_text(self, chat_id: str, text: str) -> bool:
        client = self.client
        if client is None or not text.strip():
            return False
        try:
            with self._send_lock:
                message_id = client.send_msg(text, str(chat_id))
                self._sent_ids.add(str(chat_id), str(message_id or ""))
                return bool(message_id)
        except Exception as exc:
            self.plugin.log_larkx_error("发送飞书消息失败", exc)
            return False

    def send_chain(self, chat_id: str, chain: Any) -> bool:
        """Preserve text and native images without displaying bridge metadata."""
        if self.client is None:
            return False
        accepted = False
        text_parts: list[str] = []
        with self._send_lock:
            def flush_text() -> bool:
                nonlocal accepted
                value = "".join(text_parts)
                text_parts.clear()
                if value.strip():
                    if not self.send_text(chat_id, value):
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
                        message_id = larkx_media.send_image(self.client, chat_id, face_path)
                        if not message_id:
                            return False
                        self._sent_ids.add(str(chat_id), str(message_id))
                        accepted = True
                    elif kind in {"image", "record", "video", "file"}:
                        if not flush_text():
                            return False
                        if kind == "image":
                            message_id = larkx_media.send_image(self.client, chat_id, component)
                        else:
                            media_kind = "audio" if kind == "record" else kind
                            message_id = larkx_media.send_attachment(self.client, chat_id, component, kind=media_kind)
                        if not message_id:
                            return False
                        self._sent_ids.add(str(chat_id), str(message_id))
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
            except Exception as exc:
                self.plugin.log_larkx_error("发送飞书图片或文字失败", exc)
                return False

    def is_outgoing(self, chat_id: str, message_id: str) -> bool:
        # The receive path runs this in a worker, so a websocket echo arriving
        # before send() returns waits for its receipt without blocking asyncio.
        with self._send_lock:
            return self._sent_ids.contains(str(chat_id), str(message_id))

    def download_image(self, message: dict) -> bytes:
        return larkx_media.download_image(self.client, message)

    def download_attachment(self, message: dict) -> tuple[bytes, str, str]:
        return larkx_media.download_attachment(self.client, message)

    def forward_message(self, chat_id: str, message_id: str) -> bool:
        if self.client is None:
            return False
        with self._send_lock:
            receipts = self.client.forward([str(message_id)], str(chat_id), merge=False)
            for receipt in receipts:
                self._sent_ids.add(str(chat_id), str(receipt))
            return bool(receipts)

    async def _run(self) -> None:
        while not self._stop.is_set():
            client = None
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
                client = await asyncio.to_thread(LarkClient, auth)
                self.client = client
                self.user_id = str(auth.user_id or "")
                astrbot_loop = asyncio.get_running_loop()

                def on_connected() -> None:
                    self._set_state("running", "飞书个人账号已验证，消息监听已启动")

                async def on_message(message: dict) -> None:
                    future = asyncio.run_coroutine_threadsafe(
                        self.plugin.on_larkx_message(message, self.account_id, self.user_id),
                        astrbot_loop,
                    )
                    await asyncio.wrap_future(future)

                await client.connect_websocket(on_message, on_connected=on_connected)
                if not self._stop.is_set():
                    self._set_state("reconnecting", "飞书连接已关闭，5 秒后自动重连")
                    await asyncio.sleep(5)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if not self._stop.is_set() and self._last_state != "reconnecting":
                    self._set_state("reconnecting", f"飞书消息监听中断，将自动重连（{type(exc).__name__}）")
                if not self._stop.is_set():
                    await asyncio.sleep(5)
            finally:
                self.client = None
                runtime_status = getattr(self.plugin, "runtime_status", None)
                if runtime_status is not None:
                    runtime_status.update_feishu()
                if client is not None:
                    try:
                        await asyncio.to_thread(client.close)
                    except Exception:
                        pass


def larkx_source_path() -> Path:
    configured = os.environ.get("LARKX_SOURCE", "").strip()
    if configured:
        return Path(configured)
    return Path(__file__).resolve().parents[5] / "runtime" / "LarkAgentX-runtime" / "LarkAgentX-d288ef4df8a42f6286c5506b7185f64430071490"
