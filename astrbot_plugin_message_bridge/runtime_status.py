"""Current, allowlisted bridge evidence without credentials or message bodies."""
from __future__ import annotations

import asyncio
import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .event_log import EventLog
from .snowluma_watchdog import SnowLumaRecovery


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def text(value: Any, limit: int = 200) -> str:
    return str(value or "")[:limit]


class RuntimeStatus:
    def __init__(self, context: Any, directory: Path, larkx: Any, *, interval: float = 7, timeout: float = 3) -> None:
        self.context = context
        self.path = Path(directory) / "current_runtime_status.json"
        self.events = EventLog(Path(directory))
        self.larkx = larkx
        self.interval = interval
        self.timeout = timeout
        self._task: asyncio.Task | None = None
        self._snowluma_recovery: SnowLumaRecovery | None = None
        self._lock = threading.RLock()
        self._activity = {
            platform: {"received_count": 0, "last_received_at": "", "last_chat_id": "",
                       "sent_count": 0, "failed_count": 0, "last_sent_at": "", "last_failed_at": "",
                       "last_send_status": "", "last_message_id": ""}
            for platform in ("qq", "feishu")
        }
        # Activity is scoped to this plugin run; a previous process is never
        # evidence that the current connection has received a message.
        self._state = {
            "schema_version": 1, "updated_at": utc_now(),
            "qq": {"state": "starting", "connected": False, "identity_verified": False, "accounts": []},
            "feishu": self._feishu(),
            "snowluma": self._unknown_snowluma(),
            "routing": {"enabled": True, "total": 0, "active": 0},
        }
        self._write()

    @staticmethod
    def _unknown_snowluma() -> dict:
        return {"state": "unknown", "installed": None, "running": None, "auth_ok": None, "consent_required": None,
                "accounts": [], "webui_port": None, "webui_url": ""}

    def _feishu(self) -> dict:
        state = text(getattr(self.larkx, "_last_state", "") or "starting", 40)
        connected = state == "running" and getattr(self.larkx, "client", None) is not None
        user_id = text(getattr(self.larkx, "user_id", ""))
        return {"state": state, "connected": connected, "identity_verified": connected and bool(user_id),
                "user_id": user_id, "account_id": text(getattr(self.larkx, "account_id", "default"))}

    def _write(self) -> None:
        with self._lock:
            self._state["updated_at"] = utc_now()
            for platform, activity in self._activity.items():
                self._state[platform].update(activity)
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                temporary = self.path.with_suffix(".json.tmp")
                temporary.write_text(json.dumps(self._state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                temporary.replace(self.path)
            except OSError:
                # Status export must not prevent an already received message
                # from being handled when the data disk becomes unavailable.
                pass

    def record_receive(self, platform: str, chat_id: str) -> None:
        if platform not in self._activity:
            return
        with self._lock:
            activity = self._activity[platform]
            activity.update(received_count=activity["received_count"] + 1,
                            last_received_at=utc_now(), last_chat_id=text(chat_id))
            self.events.write("receive", platform, chat_id=chat_id)
            self._write()

    def update_feishu(self) -> None:
        with self._lock:
            current = self._feishu()
            if current.get("state") != self._state.get("feishu", {}).get("state"):
                self.events.write("connection", "feishu", state=current.get("state"))
            self._state["feishu"] = current
            self._write()

    def update_routing(self, enabled: bool, total: int, active: int) -> None:
        with self._lock:
            routing = {"enabled": bool(enabled), "total": max(0, int(total)), "active": max(0, int(active))}
            if routing != self._state.get("routing"):
                self.events.write("rules", **routing)
            self._state["routing"] = routing
            self._write()

    def record_delivery(self, platform: str, success: bool, message_id: str = "") -> None:
        if platform not in self._activity:
            return
        with self._lock:
            self.events.write("send_accepted" if success else "send_failed", platform, success, message_id=message_id)
            activity = self._activity[platform]
            if success:
                # Both transport adapters currently return an acceptance bool.
                # It is not a recipient-side delivery or read receipt.
                activity.update(sent_count=activity["sent_count"] + 1,
                                last_sent_at=utc_now(), last_send_status="accepted",
                                last_message_id=text(message_id))
            else:
                activity.update(failed_count=activity["failed_count"] + 1,
                                last_failed_at=utc_now(), last_send_status="failed",
                                last_message_id=text(message_id))
            self._write()

    def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._task = asyncio.create_task(self._run(), name="mover-current-runtime-status")

    async def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None
        await self._close_snowluma_recovery()
        with self._lock:
            self._state["qq"].update(state="stopped", connected=False, identity_verified=False, accounts=[])
            self._state["feishu"].update(state="stopped", connected=False, identity_verified=False)
            self._write()

    async def _run(self) -> None:
        while True:
            try:
                await self.refresh()
            except asyncio.CancelledError:
                raise
            except Exception:
                # No exception strings are exported: platform errors may embed
                # credentials, request headers, or a remote response body.
                with self._lock:
                    self._state["qq"] = {"state": "unavailable", "connected": False, "identity_verified": False, "accounts": []}
                    self._state["snowluma"] = self._unknown_snowluma()
                    self._state["feishu"] = self._feishu()
                    self._write()
            await asyncio.sleep(self.interval)

    async def refresh(self) -> dict:
        qq, snow = await asyncio.gather(self._qq(), self._snowluma())
        with self._lock:
            current = {"qq": qq, "feishu": self._feishu(), "snowluma": snow}
            for name, value in current.items():
                if value.get("state") != self._state.get(name, {}).get("state") or value.get("running") != self._state.get(name, {}).get("running"):
                    self.events.write("connection", name, state=value.get("state"))
            self._state.update(current)
            self._write()
            return json.loads(json.dumps(self._state))

    async def _qq(self) -> dict:
        manager = getattr(self.context, "platform_manager", None)
        getter = getattr(manager, "get_insts", None)
        if not callable(getter):
            return {"state": "unknown", "connected": False, "identity_verified": False, "accounts": []}
        accounts = []
        for adapter in getter():
            try:
                metadata = adapter.meta()
                if metadata.name != "aiocqhttp":
                    continue
                platform_id = text(metadata.id)
                bot = adapter.get_client()
                # This is the installed CQHttp reverse-WS API client registry,
                # not a listening port or the adapter's generic RUNNING flag.
                clients = getattr(bot, "_wsr_api_clients", None)
                if not isinstance(clients, dict):
                    accounts.append(self._qq_account(platform_id, state="unknown"))
                    continue
                if not clients:
                    accounts.append(self._qq_account(platform_id, state="disconnected"))
                    continue
                checked = await asyncio.gather(*(self._qq_client(bot, platform_id, text(self_id)) for self_id in list(clients)))
                accounts.extend(checked)
            except Exception:
                accounts.append(self._qq_account("", state="unavailable"))
        connected = any(account["connected"] for account in accounts)
        verified = any(account["identity_verified"] for account in accounts)
        state = "connected" if verified else "verifying" if connected else "disconnected" if accounts else "not_configured"
        return {"state": state, "connected": connected, "identity_verified": verified, "accounts": accounts}

    @staticmethod
    def _qq_account(platform_id: str, *, state: str) -> dict:
        return {"platform_id": platform_id, "state": state, "connected": False, "identity_verified": False,
                "user_id": "", "nickname": "", "groups_status": "unavailable", "groups": [], "checked_at": utc_now()}

    async def _qq_client(self, bot: Any, platform_id: str, self_id: str) -> dict:
        account = self._qq_account(platform_id, state="connected")
        account["connected"] = True
        try:
            identity = await asyncio.wait_for(bot.call_action("get_login_info", self_id=self_id), self.timeout)
            user_id = text(identity.get("user_id")) if isinstance(identity, dict) else ""
            if not user_id or user_id != self_id:
                account["state"] = "identity_unverified"
                return account
            account.update(identity_verified=True, user_id=user_id, nickname=text(identity.get("nickname"), 120))
            try:
                groups = await asyncio.wait_for(bot.call_action("get_group_list", self_id=self_id), self.timeout)
                if not isinstance(groups, list):
                    raise ValueError("invalid group response")
                safe_groups = []
                for group in groups:
                    if not isinstance(group, dict) or not group.get("group_id"):
                        continue
                    safe_groups.append({"group_id": text(group["group_id"]), "group_name": text(group.get("group_name"), 120)})
                account.update(groups_status="verified", groups=safe_groups)
            except Exception:
                account["groups_status"] = "unavailable"
        except Exception:
            account["state"] = "identity_unverified"
        finally:
            if self_id not in bot._wsr_api_clients:
                account.update(state="disconnected", connected=False, identity_verified=False, user_id="", nickname="", groups_status="unavailable", groups=[])
        return account

    async def _close_snowluma_recovery(self) -> None:
        if self._snowluma_recovery is not None:
            await self._snowluma_recovery.close()
            self._snowluma_recovery = None

    async def _snowluma(self) -> dict:
        getter = getattr(self.context, "get_registered_star", None)
        if not callable(getter):
            await self._close_snowluma_recovery()
            return self._unknown_snowluma()
        try:
            metadata = getter("pulids_snowluma_go_to_astrbot")
            if metadata is None or getattr(metadata, "activated", True) is False:
                await self._close_snowluma_recovery()
                return self._unknown_snowluma()
            plugin = getattr(metadata, "star_cls", None)
            manager = getattr(plugin, "manager", None)
            if manager is None:
                await self._close_snowluma_recovery()
                return self._unknown_snowluma()
            if self._snowluma_recovery is not None and self._snowluma_recovery.manager is not manager:
                await self._close_snowluma_recovery()
            if self._snowluma_recovery is None and SnowLumaRecovery.can_attach(manager):
                self._snowluma_recovery = SnowLumaRecovery(manager, self.events)
                self._snowluma_recovery.start()
            # Use the manager already owned by AstrBot. Do not request a WebUI
            # token, read configuration, or proxy its unrestricted status JSON.
            raw = await asyncio.wait_for(manager.status(), self.timeout)
            if not isinstance(raw, dict):
                return self._unknown_snowluma()
            result = {"state": "known"}
            for key in ("installed", "running", "auth_ok", "consent_required"):
                result[key] = raw.get(key) if isinstance(raw.get(key), bool) else None
            port = raw.get("webui_port")
            port = port if isinstance(port, int) and not isinstance(port, bool) and 1 <= port <= 65535 else None
            result.update(webui_port=port, webui_url=f"http://127.0.0.1:{port}/" if port else "")
            result["accounts"] = [{"uin": text(item.get("uin")), "nickname": text(item.get("nickname"), 120)}
                                  for item in raw.get("accounts", []) if isinstance(item, dict) and item.get("uin")]
            return result
        except Exception:
            return self._unknown_snowluma()
