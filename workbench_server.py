from __future__ import annotations

import json
import ipaddress
import hashlib
import importlib.util
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time
import zipfile
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, parse_qs

from log_store import append_log, query_logs, redact, export_logs
import background_runtime

from astrbot_plugin_message_bridge.bridge_core import (
    BridgeRouter,
    CONTENT_TYPE_ORDER,
    DEFAULT_CONTENT_TYPES,
    read_delivery_stats,
    valid_umo,
)
from astrbot_plugin_message_bridge.larkx_inbox import read_inbox_stats, retry_failed_inbox
from astrbot_plugin_message_bridge.event_log import safe_larkx_recovery_status
from astrbot_plugin_message_bridge import build_identity


WORKBENCH_VERSION = "2.0.16"
_WORKBENCH_FILES = ("workbench_server.py", "background_runtime.py", "log_store.py")
_WORKBENCH_LOADED_MODULES = {name: str(getattr(sys.modules.get(name[:-3]), "MODULE_BUILD", ""))
                            for name in _WORKBENCH_FILES}
_WORKBENCH_LOADED_MODULES["workbench_server.py"] = build_identity.capture_module_build(__file__)
_WORKBENCH_PLUGIN_MODULES = ("build_identity.py", "bridge_core.py", "larkx_inbox.py")
for _filename in _WORKBENCH_PLUGIN_MODULES:
    _module = sys.modules.get(f"astrbot_plugin_message_bridge.{_filename[:-3]}")
    _WORKBENCH_LOADED_MODULES[f"plugin/{_filename}"] = str(getattr(_module, "MODULE_BUILD", ""))


ROOT = Path(__file__).resolve().parent
WEB_ROOT = ROOT / "workbench"
SETTINGS_FILE = ROOT / "workbench.settings.json"
EXAMPLE_SETTINGS_FILE = ROOT / "workbench.settings.example.json"
PLUGIN_SOURCE = ROOT / "astrbot_plugin_message_bridge"
RUNTIME_DIR = ROOT / "runtime"
ASTRBOT_EXE = RUNTIME_DIR / ".venv" / "Scripts" / "astrbot.exe"
WORKBENCH_LOG = RUNTIME_DIR / "workbench.log"
SNOWLUMA_ROOT = RUNTIME_DIR / "SnowLuma"
SNOWLUMA_APP = SNOWLUMA_ROOT / "app"
SNOWLUMA_CONFIG = SNOWLUMA_APP / "config" / "onebot.json"
LARKX_SOURCE = RUNTIME_DIR / "LarkAgentX-runtime" / "LarkAgentX-d288ef4df8a42f6286c5506b7185f64430071490"
LARKX_HOME = RUNTIME_DIR / "LarkAgentX-data"
BOOTSTRAP_SECRET_FILE = RUNTIME_DIR / "AstrBot" / ".workbench-bootstrap.dpapi"
ASTRBOT_PID_FILE = RUNTIME_DIR / "AstrBot" / ".mover-v2-astrbot.pid"
ASTRBOT_RUNNER = ROOT / "astrbot_runner.py"
PLUGIN_NAME = "astrbot_plugin_message_bridge"
PLUGIN_FILES = (
    "__init__.py",
    "build_identity.py",
    "bridge_core.py",
    "bridge_rules.example.json",
    "larkx_bridge.py",
    "larkx_inbox.py",
    "larkx_history.py",
    "larkx_receive.py",
    "larkx_media.py",
    "larkx_merge.py",
    "larkx_richtext.py",
    "qq_emojis.py",
    "qq_transport.py",
    "runtime_status.py",
    "snowluma_watchdog.py",
    "event_log.py",
    "main.py",
    "metadata.yaml",
)
DEFAULT_SETTINGS = {
    "astrbot_root": str(RUNTIME_DIR / "AstrBot"),
    "astrbot_port": 6185,
}
PLATFORM_NAMES = {"aiocqhttp": "QQ / OneBot", "larkx": "飞书个人号"}
MESSAGE_TYPES = {"GroupMessage", "FriendMessage"}
INITIAL_PASSWORD_RE = re.compile(r"Initial password:\s*(\S+)", re.IGNORECASE)

_runtime_lock = threading.Lock()
_astrbot_start_lock = threading.RLock()
_settings_lock = threading.RLock()
_rules_lock = threading.Lock()
_astrbot_process: subprocess.Popen[str] | None = None
_snowluma_process: subprocess.Popen[str] | None = None
_larkx_qr_lock = threading.Lock()
_larkx_qr_state: dict[str, Any] = {
    "status": "not_started",
    "message": "尚未开始扫码",
    "image_data": "",
    "account_ready": False,
    "agent_backend": "none",
}
_larkx_qr_thread: threading.Thread | None = None
_initial_username = "astrbot"

os.environ["LARKX_SOURCE"] = str(LARKX_SOURCE)
os.environ["LARKX_HOME"] = str(LARKX_HOME)
os.environ["LARKX_AGENT_BACKEND"] = "none"
if LARKX_SOURCE.is_dir() and str(LARKX_SOURCE) not in sys.path:
    sys.path.insert(0, str(LARKX_SOURCE))


def _load_bootstrap_password() -> str | None:
    if not BOOTSTRAP_SECRET_FILE.is_file():
        return None
    try:
        import win32crypt

        encrypted = BOOTSTRAP_SECRET_FILE.read_bytes()
        return win32crypt.CryptUnprotectData(encrypted, None, None, None, 0)[1].decode("utf-8")
    except Exception:
        return None


def _store_bootstrap_password(password: str) -> None:
    import win32crypt

    encrypted = win32crypt.CryptProtectData(
        password.encode("utf-8"), "MoverV2 AstrBot bootstrap", None, None, None, 0
    )
    BOOTSTRAP_SECRET_FILE.parent.mkdir(parents=True, exist_ok=True)
    BOOTSTRAP_SECRET_FILE.write_bytes(encrypted)


def save_admin_password(password: str) -> None:
    global _initial_password
    value = password.strip()
    if len(value) < 8 or not re.search(r"[A-Z]", value) or not re.search(r"[a-z]", value) or not re.search(r"\d", value):
        raise ValueError("AstrBot 密码需至少 8 位，并包含大小写字母和数字")
    _store_bootstrap_password(value)
    with _runtime_lock:
        _initial_password = value


def _set_larkx_qr_state(**values: Any) -> dict[str, Any]:
    with _larkx_qr_lock:
        _larkx_qr_state.update(values)
        return dict(_larkx_qr_state)


def local_feishu_desktop_status() -> dict[str, Any]:
    try:
        import psutil

        count = 0
        for process in psutil.process_iter(attrs=["name", "exe"]):
            try:
                name = str(process.info.get("name") or "").lower()
                exe = Path(str(process.info.get("exe") or "")).name.lower()
                if name in {"feishu.exe", "lark.exe"} or exe in {"feishu.exe", "lark.exe"}:
                    count += 1
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        return {"running": count > 0, "process_count": count}
    except Exception:
        return {"running": False, "process_count": 0}


def larkx_monitor_status() -> dict[str, Any]:
    runtime = read_runtime_status()
    if runtime.get("available"):
        activity = runtime.get("feishu", {})
        return {key: activity.get(key, 0 if key.endswith("_count") else "") for key in ("state", "received_count", "last_received_at", "last_chat_id", "sent_count", "failed_count", "last_sent_at", "last_failed_at", "last_send_status")}
    path = rules_path(astrbot_root()).parent / "larkx_monitor_status.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return {
            "state": str(data.get("state", "waiting_login")),
            "received_count": max(0, int(data.get("received_count", 0))),
            "last_received_at": str(data.get("last_received_at", "")),
            "last_chat_id": str(data.get("last_chat_id", "")),
            "sent_count": max(0, int(data.get("sent_count", 0))),
            "failed_count": max(0, int(data.get("failed_count", 0))),
            "last_sent_at": str(data.get("last_sent_at", "")),
            "last_failed_at": str(data.get("last_failed_at", "")),
            "last_send_status": str(data.get("last_send_status", "")),
        }
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return {"state": "waiting_login", "received_count": 0, "last_received_at": "", "last_chat_id": ""}


def larkx_status() -> dict[str, Any]:
    status: dict[str, Any] = {
        "agent_backend": "none",
        "has_credentials": False,
        "region": "feishu",
        "user_id": "",
        "qr_status": _larkx_qr_state.get("status", "not_started"),
    }
    try:
        from larkx.auth import LarkAuth

        auth = LarkAuth("default")
        status.update({
            "has_credentials": bool(auth.cookies),
            "region": auth.region.name,
            "user_id": str(auth.user_id or ""),
        })
    except Exception:
        pass
    try:
        bridge_status_path = LARKX_HOME / "bridge-status.json"
        bridge_status = json.loads(bridge_status_path.read_text(encoding="utf-8"))
        status["bridge_state"] = str(bridge_status.get("state", "waiting_login"))
        status["bridge_message"] = str(bridge_status.get("message", ""))[:180]
    except (OSError, json.JSONDecodeError):
        status["bridge_state"] = "waiting_login" if not status["has_credentials"] else "starting"
        status["bridge_message"] = ""
    runtime = read_runtime_status()
    if runtime.get("available") and not runtime.get("stale"):
        feishu = runtime.get("feishu", {})
        runtime_state = str(feishu.get("state") or "unknown")
        if feishu.get("connected") is True:
            status["bridge_state"] = "running"
            status["bridge_message"] = "飞书个人账号已验证，消息监听已启动"
        elif runtime_state in {"stopped", "waiting_login", "login_required", "reconnecting", "unavailable"}:
            status["bridge_state"] = runtime_state
            status["bridge_message"] = {
                "stopped": "飞书监听已停止",
                "waiting_login": "等待飞书账号扫码登录",
                "login_required": "飞书登录已失效，请在工作台重新扫码",
                "reconnecting": "飞书消息监听中断，正在自动重连",
                "unavailable": "飞书运行状态暂不可用",
            }[runtime_state]
    chats_path = rules_path(astrbot_root()).parent / "larkx_chats.json"
    try:
        data = json.loads(chats_path.read_text(encoding="utf-8"))
        chats = data.get("chats", []) if isinstance(data, dict) else []
        status["chats"] = [item for item in chats if isinstance(item, dict)][-200:] if isinstance(chats, list) else []
    except (OSError, json.JSONDecodeError):
        status["chats"] = []
    status["desktop"] = local_feishu_desktop_status()
    status["monitor"] = larkx_monitor_status()
    status["agent_backend"] = "none"
    return status


def start_larkx_qr_login() -> dict[str, Any]:
    global _larkx_qr_thread
    with _larkx_qr_lock:
        if _larkx_qr_thread and _larkx_qr_thread.is_alive():
            return dict(_larkx_qr_state)
        _larkx_qr_state.update({
            "status": "starting",
            "message": "正在生成飞书扫码登录",
            "image_data": "",
            "account_ready": False,
            "agent_backend": "none",
        })

    def worker() -> None:
        try:
            from io import BytesIO
            import base64
            import qrcode
            from larkx.auth import LarkAuth, QrLogin, QR_STATUS_NAMES
            from larkx.region import get_region

            LARKX_HOME.mkdir(parents=True, exist_ok=True)
            auth = LarkAuth("default")
            region = get_region(os.environ.get("LARKX_REGION", "feishu"))
            for attempt in range(1, 7):
                qr_login = QrLogin(region)
                image = BytesIO()
                qrcode.make(qr_login.qr_content).save(image, format="PNG")
                _set_larkx_qr_state(
                    status="waiting_scan",
                    message=f"第 {attempt}/6 次二维码，请用飞书 App 扫描并确认；过期会自动刷新",
                    image_data=base64.b64encode(image.getvalue()).decode("ascii"),
                )

                def on_status(status: Any, _step: Any) -> None:
                    label = QR_STATUS_NAMES.get(status, "等待扫码")
                    mapped = {0: "authenticated", 1: "waiting_scan", 2: "awaiting_confirm", 3: "cancelled", 4: "error", 5: "expired"}.get(status, "waiting_scan")
                    _set_larkx_qr_state(status=mapped, message=label)

                accepted, _data = qr_login.wait(timeout=50, interval=2, on_status=on_status)
                if accepted:
                    qr_login.finish(auth)
                    _set_larkx_qr_state(
                        status="authenticated",
                        message="飞书账号已登录；LarkAgentX agent backend=none",
                        account_ready=True,
                        user_id=str(auth.user_id or ""),
                        image_data="",
                    )
                    return
                current = _larkx_qr_state.get("status")
                if current in {"cancelled", "error"}:
                    _set_larkx_qr_state(status=current, message="扫码已结束，请重新生成二维码")
                    return
            _set_larkx_qr_state(
                status="expired",
                message="二维码已多次过期，请点重新生成后及时扫码",
                image_data="",
            )
        except Exception as exc:
            _set_larkx_qr_state(
                status="error",
                message=f"登录初始化失败：{type(exc).__name__}",
                image_data="",
            )

    _larkx_qr_thread = threading.Thread(
        target=worker, name="larkagentx-qr-login", daemon=True
    )
    _larkx_qr_thread.start()
    return dict(_larkx_qr_state)


_initial_password: str | None = _load_bootstrap_password()


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + f".{os.getpid()}.{time.time_ns()}.tmp")
    try:
        temp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def load_settings() -> dict[str, Any]:
    def initialized(value: str | Path, *, require_plugins: bool = False) -> bool:
        if not str(value).strip():
            return False
        try:
            candidate = Path(value).expanduser().resolve()
            return (candidate / ".astrbot").is_file() and (
                not require_plugins or (candidate / "data" / "plugins").is_dir()
            )
        except (OSError, ValueError, RuntimeError):
            return False

    with _settings_lock:
        if not SETTINGS_FILE.exists():
            _atomic_json(SETTINGS_FILE, DEFAULT_SETTINGS)
        try:
            data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            data = {}
        if not isinstance(data, dict):
            data = {}
        root = str(data.get("astrbot_root", DEFAULT_SETTINGS["astrbot_root"])).strip()
        local_root = ROOT / "runtime" / "AstrBot"
        # A relocated source tree can retain a deleted extraction directory.
        # Recover only to this installation's initialized runtime, never by
        # searching the machine or overriding a valid custom runtime.
        if "astrbot_root" in data and not initialized(root) and initialized(local_root, require_plugins=True):
            root = str(local_root.resolve())
            _atomic_json(SETTINGS_FILE, {**data, "astrbot_root": root})
        port = data.get("astrbot_port", DEFAULT_SETTINGS["astrbot_port"])
    try:
        port = int(port)
    except (TypeError, ValueError):
        port = DEFAULT_SETTINGS["astrbot_port"]
    if not 1024 <= port <= 65535:
        port = DEFAULT_SETTINGS["astrbot_port"]
    return {"astrbot_root": root, "astrbot_port": port}


def astrbot_root(settings: dict[str, Any] | None = None) -> Path:
    return Path((settings or load_settings())["astrbot_root"]).expanduser().resolve()


def plugin_destination(root: Path) -> Path:
    return root / "data" / "plugins" / PLUGIN_NAME


def prepare_local_setup() -> dict[str, Any]:
    """Prepare this install without starting processes or changing login state."""
    root = astrbot_root()
    if not (root / ".astrbot").is_file() or not (root / "data" / "plugins").is_dir():
        raise ValueError("AstrBot 目录未初始化，请运行一键启动.cmd 自动修复")
    previous_bridge = plugin_destination(root) / "main.py"
    bridge_changed = not previous_bridge.is_file() or any(
        not (plugin_destination(root) / name).is_file()
        or (plugin_destination(root) / name).read_bytes() != (PLUGIN_SOURCE / name).read_bytes()
        for name in PLUGIN_FILES
    )
    install_plugin(root)
    snow_name = "pulids_snowluma_go_to_astrbot"
    snow_plugin = root / "data" / "plugins" / snow_name
    snow_added = not (snow_plugin / "metadata.yaml").is_file()
    if snow_added:
        archive = ROOT / "vendor" / "snowluma-go-1.0.9.zip"
        expected = "176edc48c44ee736504679f187c74de23219ba769a34dbcbaf996ecc17615fc4"
        if not archive.is_file() or hashlib.sha256(archive.read_bytes()).hexdigest() != expected:
            raise ValueError("SnowLuma Go 插件包缺失或校验失败，请重新解压完整安装包")
        import tempfile

        with tempfile.TemporaryDirectory(prefix=".snow-plugin-", dir=root / "data" / "plugins") as folder:
            stage = Path(folder)
            with zipfile.ZipFile(archive) as bundle:
                for entry in bundle.infolist():
                    target = (stage / entry.filename).resolve()
                    if not target.is_relative_to(stage.resolve()):
                        raise ValueError("SnowLuma Go 插件包路径无效")
                bundle.extractall(stage)
            if not (stage / "main.py").is_file() or not (stage / "LICENSE").is_file():
                raise ValueError("SnowLuma Go 插件包不完整")
            snow_plugin.mkdir(parents=True, exist_ok=True)
            shutil.copytree(stage, snow_plugin, dirs_exist_ok=True)

    config_path = root / "data" / "cmd_config.json"
    config = json.loads(config_path.read_text(encoding="utf-8-sig")) if config_path.exists() else {}
    if not isinstance(config, dict) or not isinstance(config.get("platform", []), list):
        raise ValueError("AstrBot 平台配置格式无效，已保留原文件")
    platforms = config.setdefault("platform", [])
    qq_added = not any(isinstance(item, dict) and item.get("id") == "qq_bridge" for item in platforms)
    if qq_added:
        platforms.append({"id": "qq_bridge", "type": "aiocqhttp", "enable": True,
                          "ws_reverse_host": "127.0.0.1", "ws_reverse_port": 6199, "ws_reverse_token": ""})
        if config_path.exists():
            shutil.copy2(config_path, config_path.with_name(f"cmd_config.pre-bootstrap-{time.time_ns()}.json"))
        _atomic_json(config_path, config)

    snow_config_path = root / "data" / "config" / f"{snow_name}_config.json"
    if not snow_config_path.exists():
        _atomic_json(snow_config_path, {"deploy_mode": "native", "auto_start": True,
                                      "auto_sync": True, "auto_hook": True, "auto_start_qq": True,
                                      "snowluma_port": 5099, "webui_bind_host": "127.0.0.1"})
    # Seed the manager-owned runtime, not a second unmanaged SnowLuma copy.
    snow_root = root / "data" / "plugin_data" / snow_name / "snowluma"
    onebot_path = snow_root / "config" / "onebot.json"
    onebot = json.loads(onebot_path.read_text(encoding="utf-8-sig")) if onebot_path.exists() else {}
    if not isinstance(onebot, dict):
        raise ValueError("SnowLuma 配置格式无效，已保留原文件")
    previous = json.dumps(onebot, sort_keys=True)
    _snowluma_ws_client_config(onebot)
    onebot_changed = json.dumps(onebot, sort_keys=True) != previous
    if onebot_changed:
        if onebot_path.exists():
            shutil.copy2(onebot_path, onebot_path.with_name(f"onebot.pre-bootstrap-{time.time_ns()}.json"))
        _atomic_json(onebot_path, onebot)
    account_onebot_changed = _enable_snowluma_account_self_reports(onebot_path.parent)
    runtime_config = snow_root / "config" / "runtime.json"
    if not runtime_config.exists():
        _atomic_json(runtime_config, {"webuiHost": "127.0.0.1", "webuiPort": 5099})
    return {"astrbot_root": str(root), "snowluma_root": str(snow_root),
            "snowluma_plugin_installed": snow_added, "qq_platform_added": qq_added,
            "requires_restart": bridge_changed or snow_added or qq_added or onebot_changed or account_onebot_changed}


def rules_path(root: Path) -> Path:
    return root / "data" / "plugin_data" / PLUGIN_NAME / "bridge_rules.json"


def port_open(port: int, timeout: float = 0.25) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except OSError:
        return False


def lan_ipv4_address() -> str | None:
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            probe.connect(("192.0.2.1", 9))
            address = probe.getsockname()[0]
            parsed = ipaddress.ip_address(address)
            if parsed.version == 4 and parsed.is_private and not parsed.is_loopback:
                return address
        finally:
            probe.close()
    except OSError:
        pass
    try:
        import psutil

        for interface, addresses in psutil.net_if_addrs().items():
            state = psutil.net_if_stats().get(interface)
            if state is not None and not state.isup:
                continue
            if re.search(r"(?i)(virtual|vmware|vbox|hyper-v|wsl|loopback|tailscale|vpn|wireguard)", interface):
                continue
            for item in addresses:
                if item.family != socket.AF_INET:
                    continue
                try:
                    parsed = ipaddress.ip_address(item.address)
                except ValueError:
                    continue
                if parsed.is_private and not parsed.is_loopback and not item.address.startswith("169.254."):
                    return item.address
    except Exception:
        pass
    return None


def mobile_onebot_config(host_ip: str) -> dict[str, Any]:
    address = str(ipaddress.ip_address(host_ip))
    if not ipaddress.ip_address(address).is_private:
        raise ValueError("手机回连地址必须是电脑的局域网 IPv4 地址")
    return {
        "networks": {
            "wsClients": [
                {
                    "name": "mover-v2-astrbot",
                    "enabled": True,
                    "url": f"ws://{address}:6199/ws",
                    "role": "Universal",
                    "reconnectIntervalMs": 5000,
                }
            ]
        }
    }


def mobile_config_install_script(host_ip: str) -> bytes:
    address = str(ipaddress.ip_address(host_ip))
    if not ipaddress.ip_address(address).is_private:
        raise ValueError("手机回连地址必须是电脑的局域网 IPv4 地址")
    script = f'''#!/system/bin/sh
set -eu
CONFIG_DIR="${{1:-/persistent/SnowLuma/config}}"
CONFIG_FILE="$CONFIG_DIR/onebot.json"
CONFIG_URL="http://{address}:18767/onebot.json"
mkdir -p "$CONFIG_DIR"
command -v python3 >/dev/null 2>&1 || {{ echo "Python 3 is required. In Termux run: pkg install python" >&2; exit 2; }}
python3 - "$CONFIG_FILE" "$CONFIG_URL" <<'PY'
import json, os, shutil, sys, tempfile, urllib.request
from datetime import datetime
path, url = sys.argv[1], sys.argv[2]
request = urllib.request.Request(url, headers={{"Cache-Control": "no-cache"}})
with urllib.request.urlopen(request, timeout=15) as response:
    bridge = json.load(response)
if os.path.exists(path):
    try:
        with open(path, "r", encoding="utf-8-sig") as stream:
            config = json.load(stream)
    except Exception as exc:
        raise SystemExit(f"Existing config is not valid JSON; left unchanged: {{exc}}")
    if not isinstance(config, dict):
        raise SystemExit("Existing config root is not a JSON object; left unchanged")
    backup = path + ".mover-v2-" + datetime.now().strftime("%Y%m%d-%H%M%S") + ".bak"
    shutil.copy2(path, backup)
else:
    config = {{}}
    backup = ""
networks = config.setdefault("networks", {{}})
if not isinstance(networks, dict):
    raise SystemExit("Existing networks field is not an object; left unchanged")
clients = networks.setdefault("wsClients", [])
if not isinstance(clients, list):
    raise SystemExit("Existing wsClients field is not an array; left unchanged")
incoming = bridge["networks"]["wsClients"][0]
target = next((item for item in clients if isinstance(item, dict) and (item.get("name") == incoming["name"] or item.get("url") == incoming["url"])), None)
entry = dict(target or {{}})
entry.update(incoming)
entry.pop("accessToken", None)
if target is None:
    clients.append(entry)
else:
    target.clear()
    target.update(entry)
directory = os.path.dirname(path) or "."
fd, temporary = tempfile.mkstemp(prefix=".onebot.", suffix=".json", dir=directory)
try:
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump(config, stream, ensure_ascii=False, indent=2)
        stream.write("\\n")
    os.replace(temporary, path)
except Exception:
    try: os.unlink(temporary)
    except OSError: pass
    raise
print("SnowLuma phone config updated; OneBot accessToken omitted.")
if backup: print("Backup:", backup)
print("Restart SnowLuma on the phone to connect to ws://{address}:6199/ws")
PY
'''
    return script.encode("utf-8")


def _set_astrbot_mobile_listener() -> dict[str, Any]:
    address = lan_ipv4_address()
    if not address:
        raise ValueError("未检测到电脑的局域网 IPv4 地址；请先连接与手机相同的 Wi-Fi")
    settings = load_settings()
    root = astrbot_root(settings)
    config_path = root / "data" / "cmd_config.json"
    if not config_path.is_file():
        raise ValueError("AstrBot cmd_config.json 尚未创建")
    try:
        config = json.loads(config_path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise ValueError("AstrBot cmd_config.json 不是有效 JSON") from exc
    if not isinstance(config, dict) or not isinstance(config.get("platform"), list):
        raise ValueError("AstrBot 平台配置结构无效")
    adapters = [
        platform for platform in config["platform"]
        if isinstance(platform, dict)
        and platform.get("type") == "aiocqhttp"
        and platform.get("id") == "qq_bridge"
    ]
    if not adapters:
        raise ValueError("AstrBot 尚未添加 ID 为 qq_bridge 的 OneBot v11 平台")
    timestamp = __import__("datetime").datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = config_path.with_name(f"cmd_config.json.{timestamp}.bak")
    shutil.copy2(config_path, backup)
    for adapter in adapters:
        adapter["ws_reverse_host"] = address
        adapter["ws_reverse_port"] = 6199
        adapter["ws_reverse_token"] = ""
    _atomic_json(config_path, config)
    restarted = False
    if port_open(settings["astrbot_port"]):
        stop_astrbot()
        start_astrbot()
        restarted = True
        for _ in range(50):
            if port_open(settings["astrbot_port"]):
                break
            __import__("time").sleep(0.25)
        if not port_open(settings["astrbot_port"]):
            raise RuntimeError("AstrBot 已更新监听地址，但重启后面板未就绪")
    return {
        "configured": True,
        "host": address,
        "port": 6199,
        "url": f"ws://{address}:6199/ws",
        "backup": str(backup),
        "restarted": restarted,
    }


def _managed_astrbot_pid() -> int | None:
    from astrbot_runner import owned_runner, same_process

    try:
        marker = json.loads(ASTRBOT_PID_FILE.read_text(encoding="utf-8"))
        if not isinstance(marker, dict):
            return None
        root = astrbot_root()
        runner_pid = owned_runner(marker, root, ASTRBOT_RUNNER)
        if runner_pid is not None:
            return runner_pid
        # Recover a child if its collector unexpectedly exited. PID plus creation
        # time and exact command/cwd are all required; never trust a PID alone.
        if not same_process(marker):
            return None
        import psutil

        process = psutil.Process(int(marker["pid"]))
        from astrbot_runner import is_astrbot_command

        if not is_astrbot_command(process.cmdline(), process.exe(), ASTRBOT_EXE):
            return None
        if Path(process.cwd()).resolve() == root.resolve():
            return process.pid
        # A Windows console-script launcher may have a different cwd from
        # the interpreter serving AstrBot. Recover the recorded launcher only
        # when its exact executable and a live AstrBot descendant both match.
        if Path(process.exe()).resolve() == ASTRBOT_EXE.resolve():
            for child in process.children(recursive=True):
                try:
                    if (Path(child.cwd()).resolve() == root.resolve()
                            and is_astrbot_command(child.cmdline(), child.exe(), ASTRBOT_EXE)):
                        return process.pid
                except (psutil.Error, OSError, ValueError):
                    continue
        return None
    except Exception:
        return None


def install_plugin(root: Path) -> Path:
    if not (root / ".astrbot").is_file():
        raise ValueError("所选目录不是已初始化的 AstrBot 根目录")
    if not (root / "data" / "plugins").is_dir():
        raise ValueError("AstrBot 插件目录不存在；先运行 AstrBot init")
    for name in PLUGIN_FILES:
        source = PLUGIN_SOURCE / name
        if not source.is_file():
            raise FileNotFoundError(f"插件文件缺失: {source}")

    target = plugin_destination(root)
    target.mkdir(parents=True, exist_ok=True)
    changed = [name for name in PLUGIN_FILES
               if not (target / name).is_file() or (target / name).read_bytes() != (PLUGIN_SOURCE / name).read_bytes()]
    if changed:
        # The later prepare pass cannot detect a copy that has already
        # happened. Persist the restart obligation before replacing sources.
        _atomic_json(rules_path(root).parent / "source-restart.pending.json",
                     {"schema": 1, "generation": secrets.token_hex(16),
                      "astrbot_root": str(root.resolve()), "changed_files": changed})
    for name in PLUGIN_FILES:
        (target / name).write_bytes((PLUGIN_SOURCE / name).read_bytes())

    config_path = rules_path(root)
    if not config_path.exists():
        _atomic_json(
            config_path,
            {"managed_platforms": ["aiocqhttp", "larkx"], "bridge_enabled": True, "rules": []},
        )
    return target


def read_rules(root: Path) -> dict[str, Any]:
    path = rules_path(root)
    if not path.exists():
        data = {"managed_platforms": ["aiocqhttp", "larkx"], "bridge_enabled": True, "rules": []}
    else:
        data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("映射配置必须是 JSON 对象")
    return {
        "managed_platforms": ["aiocqhttp", "larkx"],
        "rules": data.get("rules", []) if isinstance(data.get("rules", []), list) else [],
        "bridge_enabled": bool(data.get("bridge_enabled", True)),
    }


def validate_rules(data: dict[str, Any]) -> dict[str, Any]:
    raw_rules = data.get("rules", [])
    if not isinstance(raw_rules, list) or len(raw_rules) > 200:
        raise ValueError("映射规则格式无效或数量超过 200 条")

    clean_rules: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_rules, start=1):
        if not isinstance(raw, dict):
            raise ValueError(f"第 {index} 条规则格式无效")
        source_platform = str(raw.get("source_platform", "")).strip()
        source_platform_id = str(raw.get("source_platform_id", "")).strip()
        source_session_id = str(raw.get("source_session_id", "")).strip()
        source_message_type = str(raw.get("source_message_type", "GroupMessage")).strip()
        source_account_id = str(raw.get("source_account_id", "")).strip()
        target_account_id = str(raw.get("target_account_id", "")).strip()
        target_umo = str(raw.get("target_umo", "")).strip()
        name = str(raw.get("name", f"映射 {index}")).strip()[:80]
        raw_content_types = raw.get("content_types")
        if raw_content_types is None:
            content_types = list(DEFAULT_CONTENT_TYPES)
        elif not isinstance(raw_content_types, list):
            raise ValueError(f"第 {index} 条规则的转发内容类型格式无效")
        else:
            unknown_types = [value for value in raw_content_types if value not in CONTENT_TYPE_ORDER]
            content_types = [kind for kind in CONTENT_TYPE_ORDER if kind in raw_content_types]
            if unknown_types:
                raise ValueError(f"第 {index} 条规则包含不支持的转发内容类型")
            if not content_types:
                raise ValueError(f"第 {index} 条规则至少选择一种转发内容类型")
        if source_platform not in {"aiocqhttp", "larkx"}:
            raise ValueError(f"第 {index} 条规则的平台必须是 QQ/OneBot 或飞书个人号")
        if not source_platform_id or not source_session_id:
            raise ValueError(f"第 {index} 条规则缺少来源平台实例 ID 或群 ID")
        if source_message_type not in MESSAGE_TYPES:
            raise ValueError(f"第 {index} 条规则消息类型无效")
        if any(value and (not value.isdigit() or len(value) > 20) for value in (source_account_id, target_account_id)):
            raise ValueError(f"第 {index} 条规则 QQ 账号必须为数字 QQ 号")
        if not valid_umo(target_umo):
            raise ValueError(f"第 {index} 条规则的目标会话 ID 格式无效")
        clean_rules.append(
            {
                "name": name or f"映射 {index}",
                "enabled": bool(raw.get("enabled", True)),
                "source_platform": source_platform,
                "source_platform_id": source_platform_id,
                "source_message_type": source_message_type,
                "source_account_id": source_account_id,
                "target_account_id": target_account_id,
                "include_self": bool(raw.get("include_self", False)),
                "content_types": content_types,
                "source_session_id": source_session_id,
                "target_umo": target_umo,
            }
        )

    # BridgeRouter is the same parser used by the plugin; reject any route it would ignore.
    router = BridgeRouter({"managed_platforms": ["aiocqhttp", "larkx"], "rules": clean_rules})
    if len(router.routes) != sum(1 for rule in clean_rules if rule["enabled"]):
        raise ValueError("有规则无法被插件识别，请检查平台与会话类型")
    return {"managed_platforms": ["aiocqhttp", "larkx"], "bridge_enabled": bool(data.get("bridge_enabled", True)), "rules": clean_rules}


def save_rules(root: Path, data: dict[str, Any]) -> dict[str, Any]:
    if "bridge_enabled" not in data:
        data = {**data, "bridge_enabled": read_rules(root)["bridge_enabled"]}
    normalized = validate_rules(data)
    if not (root / ".astrbot").is_file():
        raise ValueError("请先选择已初始化的 AstrBot 目录")
    if not plugin_destination(root).is_dir():
        install_plugin(root)
    _atomic_json(rules_path(root), normalized)
    return normalized


def _write_local_dashboard_config(root: Path, port: int) -> None:
    path = root / "data" / "cmd_config.json"
    if path.exists():
        try:
            config = json.loads(path.read_text(encoding="utf-8-sig"))
        except json.JSONDecodeError as exc:
            raise ValueError("AstrBot data/cmd_config.json 格式错误，请先修复") from exc
    else:
        config = {}
    if not isinstance(config, dict):
        raise ValueError("AstrBot data/cmd_config.json 必须是 JSON 对象")
    dashboard = config.get("dashboard")
    if not isinstance(dashboard, dict):
        dashboard = {}
    dashboard.update({"host": "127.0.0.1", "port": port})
    config["dashboard"] = dashboard
    _atomic_json(path, config)


def _start_output_capture(process: subprocess.Popen[str], log_path: Path) -> None:
    global _initial_password
    log_path.parent.mkdir(parents=True, exist_ok=True)
    stream = process.stdout
    if stream is None:
        return
    for raw_line in stream:
        line = raw_line.rstrip("\r\n")
        match = INITIAL_PASSWORD_RE.search(line)
        if match:
            with _runtime_lock:
                _initial_password = match.group(1)
            _store_bootstrap_password(match.group(1))
            line = INITIAL_PASSWORD_RE.sub("Initial password: [在工作台本地显示]", line)
        append_log(log_path, line)


def start_astrbot(reset_password: bool = False, *, automatic_recovery: bool = False,
                  respect_saved_desired: bool = False) -> dict[str, Any]:
    with _astrbot_start_lock:
        if respect_saved_desired and not background_runtime.settings(ROOT)["astrbot_desired"]:
            return {"started": False, "skipped": True, "reason": "manually_stopped"}
        if automatic_recovery:
            # An automatic request can arrive after the user pressed Stop. Read
            # the desired state under the same lock used by manual start/stop.
            desired = background_runtime.settings(ROOT)
            if not desired["enabled"]:
                return {"started": False, "skipped": True, "reason": "disabled"}
            if not desired["astrbot_desired"]:
                return {"started": False, "skipped": True, "reason": "manually_stopped"}
            pause_reason = background_runtime.paused(ROOT)
            if pause_reason:
                return {"started": False, "skipped": True, "reason": "paused", "pause_reason": pause_reason}
        return _start_astrbot(reset_password)


def _start_astrbot(reset_password: bool) -> dict[str, Any]:
    global _astrbot_process, _initial_password
    settings = load_settings()
    root = astrbot_root(settings)
    astrbot_python = ASTRBOT_EXE.with_name("python.exe")
    if not astrbot_python.is_file():
        raise ValueError("AstrBot 运行环境未安装；请从桌面快捷方式重新启动以修复 Python 环境")
    if not (root / ".astrbot").is_file():
        raise ValueError("AstrBot 尚未初始化")
    pending = {"started": True, "already_starting": True, "port": settings["astrbot_port"]}
    managed_pid = _managed_astrbot_pid()
    if managed_pid is not None:
        return {**pending, "pid": managed_pid}
    with _runtime_lock:
        if _astrbot_process is not None and _astrbot_process.poll() is None:
            return pending
    if port_open(settings["astrbot_port"]):
        raise ValueError(f"端口 {settings['astrbot_port']} 已被占用；未启动第二个 AstrBot")

    install_plugin(root)
    _write_local_dashboard_config(root, settings["astrbot_port"])
    config_path = root / "data" / "cmd_config.json"
    try:
        config_data = json.loads(config_path.read_text(encoding="utf-8-sig"))
        dashboard = config_data.get("dashboard", {})
    except (OSError, json.JSONDecodeError):
        dashboard = {}
    with _runtime_lock:
        if _initial_password is None:
            _initial_password = _load_bootstrap_password()
    needs_initial_password = reset_password or not (
        dashboard.get("pbkdf2_password") or dashboard.get("password")
    )

    # Console-script EXEs embed absolute interpreter paths and can survive a
    # moved/repaired venv with the old path. Use the verified sibling directly.
    command = [str(astrbot_python), "-B", "-m", "astrbot.cli", "run", "-p", str(settings["astrbot_port"])]
    if reset_password:
        command.append("--reset-password")
    environment = os.environ.copy()
    environment["ASTRBOT_DASHBOARD_HOST"] = "127.0.0.1"
    environment["ASTRBOT_DASHBOARD_PORT"] = str(settings["astrbot_port"])
    environment["LARKX_SOURCE"] = str(LARKX_SOURCE)
    environment["LARKX_HOME"] = str(LARKX_HOME)
    environment["LARKX_AGENT_BACKEND"] = "none"
    with _runtime_lock:
        if reset_password or needs_initial_password:
            if reset_password or not _initial_password:
                _initial_password = secrets.token_urlsafe(24)
                _store_bootstrap_password(_initial_password)
            environment["ASTRBOT_DASHBOARD_INITIAL_PASSWORD"] = _initial_password
        else:
            environment.pop("ASTRBOT_DASHBOARD_INITIAL_PASSWORD", None)

    log_path = root / "data" / "logs" / "workbench-astrbot.log"
    if not ASTRBOT_RUNNER.is_file():
        raise ValueError("独立后台运行模块缺失；请完整解压新版 ZIP")
    runner_command = [sys.executable, str(ASTRBOT_RUNNER), "--root", str(root),
                      "--pid-file", str(ASTRBOT_PID_FILE), "--log", str(log_path),
                      "--secret", str(BOOTSTRAP_SECRET_FILE), "--", *command]
    creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "DETACHED_PROCESS", 0)
    process = subprocess.Popen(
        runner_command, cwd=root, env=environment, stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=creation_flags,
    )
    with _runtime_lock:
        _astrbot_process = process
    # Give the runner a brief chance to record ownership. On slow Windows
    # launches the caller can keep polling without sending another start request.
    from astrbot_runner import launched_by

    for _ in range(80):
        if process.poll() is not None:
            raise ValueError("AstrBot 后台启动器退出；请查询 AstrBot 日志")
        runner_pid = _managed_astrbot_pid()
        if runner_pid is not None and launched_by(runner_pid, process.pid):
            return {"started": True, "pid": runner_pid, "port": settings["astrbot_port"]}
        time.sleep(0.05)
    return pending


def stop_astrbot() -> dict[str, Any]:
    with _astrbot_start_lock:
        return _stop_astrbot()


def _stop_astrbot() -> dict[str, Any]:
    global _astrbot_process
    with _runtime_lock:
        process = _astrbot_process
    tracked_pid = _managed_astrbot_pid()
    if tracked_pid is not None:
        from astrbot_runner import same_process, stop_owned_tree

        try:
            marker = json.loads(ASTRBOT_PID_FILE.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError("AstrBot 进程所有权记录不可用；为避免停止其他程序，未执行强制停止") from exc
        records = [marker.get("runner"), marker]
        owner = next((item for item in records if isinstance(item, dict) and item.get("pid") == tracked_pid), None)
        if owner is None or not same_process(owner):
            raise ValueError("AstrBot 进程身份已变化；为避免停止 PID 复用后的程序，请重试")
        stop_owned_tree(int(owner["pid"]), float(owner["process_started_at"]))
        if same_process(owner):
            raise ValueError("AstrBot 后台进程未能安全退出；请查看后台运行日志")
    else:
        if port_open(load_settings()["astrbot_port"]):
            raise ValueError("AstrBot 正在运行，但不是由此工作台启动；请在 AstrBot 原控制台关闭")
        return {"stopped": False, "message": "AstrBot 当前未运行"}
    with _runtime_lock:
        _astrbot_process = None
        _initial_password = None
    ASTRBOT_PID_FILE.unlink(missing_ok=True)
    return {"stopped": True}


def start_snowluma() -> dict[str, Any]:
    global _snowluma_process
    if port_open(5099):
        return {"started": False, "already_running": True, "url": "http://127.0.0.1:5099/"}
    node = SNOWLUMA_APP / "node.exe"
    entry = SNOWLUMA_APP / "index.mjs"
    if not node.is_file() or not entry.is_file():
        raise ValueError("SnowLuma 尚未安装；运行 install_snowluma.ps1 后重试")
    log_dir = SNOWLUMA_ROOT / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    stdout = (log_dir / "runtime.stdout.log").open("a", encoding="utf-8", errors="replace")
    stderr = (log_dir / "runtime.stderr.log").open("a", encoding="utf-8", errors="replace")
    try:
        _snowluma_process = subprocess.Popen(
            [str(node), str(entry)],
            cwd=SNOWLUMA_APP,
            stdin=subprocess.DEVNULL,
            stdout=stdout,
            stderr=stderr,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    finally:
        stdout.close()
        stderr.close()
    return {"started": True, "pid": _snowluma_process.pid, "url": "http://127.0.0.1:5099/"}


_MOVER_ONEBOT_NODE = "mover-v2-astrbot"
_MOVER_ONEBOT_URL = "ws://127.0.0.1:6199/ws"


def _snowluma_ws_client_config(config: dict[str, Any]) -> dict[str, Any]:
    networks = config.get("networks")
    if not isinstance(networks, dict):
        networks = {}
        config["networks"] = networks
    clients = networks.get("wsClients")
    if not isinstance(clients, list):
        clients = []
        networks["wsClients"] = clients

    name = _MOVER_ONEBOT_NODE
    target_url = _MOVER_ONEBOT_URL
    target = next(
        (
            client
            for client in clients
            if isinstance(client, dict)
            and (client.get("name") == name or client.get("url") == target_url)
        ),
        None,
    )
    entry = {
        **(target or {}),
        "name": name,
        "enabled": True,
        "url": target_url,
        "role": "Universal",
        "reconnectIntervalMs": 5000,
        "reportSelfMessage": True,
    }
    entry.pop("accessToken", None)
    if target is None:
        clients.append(entry)
    else:
        target.clear()
        target.update(entry)
    return config


def _enable_snowluma_account_self_reports(directory: Path) -> bool:
    """Account snapshots can override the global SnowLuma connection settings."""
    changed = False
    for path in directory.glob("onebot_*.json"):
        if not re.fullmatch(r"onebot_[0-9]+\.json", path.name) or not path.is_file() or path.is_symlink():
            continue
        try:
            config = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            continue
        networks = config.get("networks") if isinstance(config, dict) else None
        clients = networks.get("wsClients") if isinstance(networks, dict) else None
        if not isinstance(clients, list):
            continue
        owned = [client for client in clients if isinstance(client, dict)
                 and client.get("name") == _MOVER_ONEBOT_NODE
                 and str(client.get("url") or "").rstrip("/") == _MOVER_ONEBOT_URL
                 and client.get("reportSelfMessage") is not True]
        if not owned:
            continue
        for client in owned:
            client["reportSelfMessage"] = True
        shutil.copy2(path, path.with_name(f"{path.stem}.pre-self-report-{time.time_ns()}.json"))
        _atomic_json(path, config)
        changed = True
    return changed


def snowluma_onebot_configured() -> bool:
    try:
        config = json.loads(SNOWLUMA_CONFIG.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return False
    networks = config.get("networks", {}) if isinstance(config, dict) else {}
    clients = networks.get("wsClients", []) if isinstance(networks, dict) else []
    if not isinstance(clients, list):
        return False
    return any(
        isinstance(client, dict)
        and client.get("url") == "ws://127.0.0.1:6199/ws"
        and client.get("enabled", True)
        and client.get("reportSelfMessage") is True
        and not client.get("accessToken")
        for client in clients
    )


def configure_snowluma_onebot() -> dict[str, Any]:
    if not (SNOWLUMA_APP / "node.exe").is_file():
        raise ValueError("SnowLuma 尚未安装")
    SNOWLUMA_CONFIG.parent.mkdir(parents=True, exist_ok=True)
    if SNOWLUMA_CONFIG.exists():
        try:
            config = json.loads(SNOWLUMA_CONFIG.read_text(encoding="utf-8-sig"))
        except json.JSONDecodeError as exc:
            raise ValueError("SnowLuma OneBot 配置 JSON 无效；原文件保持不变") from exc
        if not isinstance(config, dict):
            raise ValueError("SnowLuma OneBot 配置根节点必须为对象；原文件保持不变")
        timestamp = __import__("datetime").datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = SNOWLUMA_CONFIG.with_name(f"onebot.json.{timestamp}.bak")
        if backup.exists():
            backup = SNOWLUMA_CONFIG.with_name(
                f"onebot.json.{timestamp}-{os.getpid()}.bak"
            )
        shutil.copy2(SNOWLUMA_CONFIG, backup)
    else:
        config = {}
        backup = None

    updated = _snowluma_ws_client_config(config)
    temp_path = SNOWLUMA_CONFIG.with_suffix(".json.tmp")
    temp_path.write_text(
        json.dumps(updated, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    os.replace(temp_path, SNOWLUMA_CONFIG)
    if not snowluma_onebot_configured():
        raise RuntimeError("SnowLuma 回连配置写入后未通过校验")

    restarted = False
    if port_open(5099):
        try:
            import psutil

            listener_pid = next(
                (
                    connection.pid
                    for connection in psutil.net_connections(kind="tcp")
                    if connection.status == psutil.CONN_LISTEN
                    and connection.laddr.port == 5099
                    and connection.pid
                ),
                None,
            )
            if listener_pid:
                process = psutil.Process(listener_pid)
                expected_node = str(SNOWLUMA_APP / "node.exe").lower()
                if str(process.exe()).lower() == expected_node:
                    process.terminate()
                    try:
                        process.wait(timeout=8)
                    except psutil.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)
                    restarted = True
        except Exception as exc:
            raise RuntimeError(
                f"回连配置已保存，但 SnowLuma 未能自动重启：{exc}"
            ) from exc

    if restarted:
        start_snowluma()
        for _ in range(40):
            if port_open(5099):
                break
            __import__("time").sleep(0.25)
        if not port_open(5099):
            raise RuntimeError("配置已保存，但 SnowLuma 重启后控制台未就绪")
    return {
        "configured": True,
        "url": "ws://127.0.0.1:6199/ws",
        "token_required": False,
        "backup": str(backup) if backup else None,
        "restarted": restarted,
    }


def _tail_logs(root: Path, limit: int = 80) -> list[str]:
    candidates: list[Path] = []
    for folder in (root / "data" / "logs", root / "data" / "logs" / "astrbot"):
        if folder.exists():
            candidates.extend(path for path in folder.glob("*.log") if path.is_file())
    candidates.append(root / "data" / "logs" / "workbench-astrbot.log")
    existing = [path for path in candidates if path.is_file()]
    if not existing:
        return []
    latest = max(existing, key=lambda path: path.stat().st_mtime)
    try:
        with latest.open("rb") as stream:
            offset = max(0, latest.stat().st_size - 256 * 1024)
            stream.seek(offset)
            if offset:
                stream.readline()
            lines = stream.read(256 * 1024).decode("utf-8", errors="replace").splitlines()[-limit:]
    except OSError:
        return []
    return [redact(INITIAL_PASSWORD_RE.sub("Initial password: [已隐藏]", line)) for line in lines]


def _snowluma_first_run_pending() -> bool:
    for path in (
        SNOWLUMA_ROOT / "app" / "logs" / f"snowluma-{__import__('datetime').datetime.now():%Y-%m-%d}.log",
        SNOWLUMA_ROOT / "logs" / "runtime.stdout.log",
    ):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")[-100_000:]
        except OSError:
            continue
        if "awaiting EULA/PRIVACY consent" in text:
            return True
    return False


def _read_runtime_document() -> tuple[dict, bool]:
    path = rules_path(astrbot_root()).parent / "current_runtime_status.json"
    try:
        if path.stat().st_size > 8 * 1024 * 1024:
            return {}, False
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return {}, False
        updated = datetime.fromisoformat(str(raw.get("updated_at", "")))
        age = (datetime.now(timezone.utc) - updated).total_seconds()
        return raw, 0 <= age <= 40
    except (OSError, ValueError, TypeError, AttributeError):
        return {}, False


def _status_map(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _status_list(value: Any, limit: int) -> list:
    return value[:limit] if isinstance(value, list) else []


def _status_count(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (ValueError, TypeError, OverflowError):
        return 0


def read_runtime_status() -> dict[str, Any]:
    raw, fresh = _read_runtime_document()
    if not raw:
        return {"available": False, "stale": True, "qq": {}, "feishu": {}, "snowluma": {}}
    result = {"available": True, "stale": not fresh, "updated_at": raw.get("updated_at"),
              "bridge_build": build_identity.safe_runtime_identity(raw.get("bridge_build"))}
    for platform in ("qq", "feishu"):
        source = _status_map(raw.get(platform))
        result[platform] = {
            "state": str(source.get("state") or "unknown") if fresh else "stale",
            "connected": bool(source.get("connected")) and fresh,
            "identity_verified": bool(source.get("identity_verified")) and fresh,
            "received_count": _status_count(source.get("received_count")),
            "last_received_at": str(source.get("last_received_at") or ""),
            "last_chat_id": str(source.get("last_chat_id") or ""),
            "sent_count": _status_count(source.get("sent_count")),
            "failed_count": _status_count(source.get("failed_count")),
            "last_sent_at": str(source.get("last_sent_at") or ""),
            "last_failed_at": str(source.get("last_failed_at") or ""),
            "last_send_status": str(source.get("last_send_status") or ""),
        }
    recovery = _status_map(raw.get("feishu")).get("recovery")
    if isinstance(recovery, dict):
        result["feishu"]["recovery"] = safe_larkx_recovery_status(recovery)
        if not fresh:
            result["feishu"]["recovery"]["state"] = "stale"
    result["qq"]["accounts"] = []
    for account in _status_list(_status_map(raw.get("qq")).get("accounts"), 20):
        if not isinstance(account, dict):
            continue
        safe = {key: str(account.get(key) or "")[:160] for key in ("platform_id", "state", "user_id", "nickname", "groups_status", "checked_at")}
        safe.update(connected=bool(account.get("connected")) and fresh, identity_verified=bool(account.get("identity_verified")) and fresh)
        safe["groups"] = [{"group_id": str(group.get("group_id") or ""), "group_name": str(group.get("group_name") or "")[:120]} for group in _status_list(account.get("groups"), 3000) if isinstance(group, dict) and group.get("group_id")]
        result["qq"]["accounts"].append(safe)
    result["feishu"].update({key: str(_status_map(raw.get("feishu")).get(key) or "")[:160] for key in ("user_id", "account_id")})
    snow = _status_map(raw.get("snowluma"))
    result["snowluma"] = {key: snow.get(key) if fresh and isinstance(snow.get(key), bool) else None for key in ("installed", "running", "auth_ok", "consent_required", "must_change_password", "send_ready")}
    send_state = snow.get("send_state")
    result["snowluma"]["send_state"] = send_state if fresh and send_state in {"ready", "consent_required", "password_change_required", "auth_required", "account_offline", "rate_limited", "unavailable"} else "unavailable"
    result["snowluma"]["send_checked_at"] = _status_count(snow.get("send_checked_at")) if fresh else 0
    port = snow.get("webui_port")
    result["snowluma"].update(state=str(snow.get("state") or "unknown") if fresh else "stale", webui_port=port if type(port) is int and 1024 <= port <= 65535 else None)
    result["snowluma"]["accounts"] = [{"uin": str(account.get("uin") or ""), "nickname": str(account.get("nickname") or "")[:120]} for account in _status_list(snow.get("accounts"), 20) if isinstance(account, dict) and account.get("uin")]
    return result


def bridge_build_status(runtime: dict | None = None) -> dict[str, Any]:
    if runtime is None:
        raw, fresh = _read_runtime_document()
        runtime = {"stale": not fresh, "bridge_build": raw.get("bridge_build")}
    root = astrbot_root()
    expected = build_identity.expected_modules(PLUGIN_SOURCE)
    installed = build_identity.expected_modules(plugin_destination(root))
    complete = all(expected.values())
    installed_matches = complete and installed == expected
    record = build_identity.safe_runtime_identity(runtime.get("bridge_build"))
    result = {"state": "unavailable", "matches": False, "runtime": None,
              "expected": {"version": WORKBENCH_VERSION,
                           "modules_fingerprint": build_identity.fingerprint(expected) if complete else ""},
              "installed_matches": installed_matches,
              "restart_pending": (rules_path(root).parent / "source-restart.pending.json").is_file()}
    if record is None:
        return result
    result["runtime"] = {key: record[key] for key in
                         ("version", "process_id", "run_id", "modules_fingerprint", "loaded_at")}
    if runtime.get("stale", True) or not record["running"] or not build_identity.process_matches(record, root):
        result["state"] = "stale"
    elif complete:
        matches = record["modules"] == expected and installed_matches and record["version"] == WORKBENCH_VERSION
        result.update(state="current" if matches else "mismatch", matches=matches,
                      mismatched_files=[name for name in build_identity.CODE_FILES
                                        if record["modules"].get(name) != expected[name] or installed[name] != expected[name]])
    return result


def build_diagnostics() -> dict[str, Any]:
    """A shareable version check, deliberately independent of logs and bootstrap secrets."""
    expected = build_identity.expected_modules(ROOT, _WORKBENCH_FILES)
    expected.update({f"plugin/{name}": value for name, value in
                     build_identity.expected_modules(PLUGIN_SOURCE, _WORKBENCH_PLUGIN_MODULES).items()})
    matches = all(expected.values()) and expected == _WORKBENCH_LOADED_MODULES
    return {"schema_version": 1,
            "workbench": {"version": WORKBENCH_VERSION, "matches": matches,
                          "modules_fingerprint": build_identity.fingerprint(_WORKBENCH_LOADED_MODULES),
                          "expected_fingerprint": build_identity.fingerprint(expected)},
            "plugin": bridge_build_status()}


def _chat_type(value: Any) -> str:
    normalized = str(value or "").upper()
    return "GroupMessage" if normalized in {"2", "3", "GROUP", "TOPIC_GROUP", "GROUPMESSAGE"} else "FriendMessage"


def _chat_display_name(client: Any, info: dict[str, Any], chat_id: str) -> str:
    """Use chat metadata for groups and the other member's profile for DMs."""
    name = str(info.get("name") or "").strip()
    if name:
        return name[:120]
    localized = info.get("i18nNames")
    if isinstance(localized, dict):
        for locale in ("zh_cn", "zh_CN", "zh", "en_us", "en_US"):
            translated = localized.get(locale)
            if isinstance(translated, str) and translated.strip():
                return translated.strip()[:120]
    if _chat_type(info.get("type")) == "GroupMessage":
        getter = getattr(client, "get_group_name", None)
        try:
            result = getter(chat_id) if callable(getter) else None
        except Exception:
            result = None
        return str(result or "").strip()[:120]
    own_id = str(getattr(getattr(client, "auth", None), "user_id", "") or "")
    try:
        members = client.api("chats.PullChatChattersRequest", {"chatId": chat_id, "needChatters": True, "limit": 20}) or {}
        chatters = members.get("chatters") or {}
        if not isinstance(chatters, dict):
            return ""
        peers = [str(user_id) for user_id in chatters if str(user_id) != own_id]
        if not peers and own_id in chatters:
            peers = [own_id]
        for peer_id in peers:
            result = client.get_user_name(peer_id, chat_id)
            if isinstance(result, str) and result.strip():
                return result.strip()[:120]
    except Exception:
        pass
    return ""


def mapping_discovery() -> dict[str, Any]:
    runtime = read_runtime_status()
    try:
        config = json.loads((astrbot_root() / "data" / "cmd_config.json").read_text(encoding="utf-8-sig"))
        instances = [{"id": str(item["id"]), "name": str(item.get("name") or item["id"]), "accounts": [account for account in runtime.get("qq", {}).get("accounts", []) if account.get("platform_id") == item["id"]]} for item in config.get("platform", []) if isinstance(item, dict) and item.get("type") == "aiocqhttp" and item.get("id") and item.get("enable", True)]
    except (OSError, ValueError, TypeError):
        instances = []
    try:
        preferences = json.loads((ROOT / "data" / "mapping-preferences.json").read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        preferences = {}
    larkx = larkx_status()
    chats = []
    for chat in larkx.get("chats", []):
        if not isinstance(chat, dict) or not chat.get("chat_id"):
            continue
        message_type = _chat_type(chat.get("message_type") or "GroupMessage")
        verified = chat.get("source") == "account_metadata" and bool(str(chat.get("name") or "").strip())
        chats.append({
            "chat_id": str(chat["chat_id"]),
            "name": str(chat.get("name") or "")[:120] if verified else ("群聊（名称待刷新）" if message_type == "GroupMessage" else "私聊（名称待刷新）"),
            "name_verified": verified,
            "message_type": message_type,
            "source": str(chat.get("source") or "received_event"),
        })
    warnings = []
    if not runtime.get("qq", {}).get("identity_verified"):
        warnings.append("QQ 账号尚未通过 OneBot 身份验证；群号可预填，需连接后核验。")
    qq_group_ids = {group["group_id"] for instance in instances for account in instance["accounts"] if account.get("identity_verified") for group in account.get("groups", [])}
    if preferences.get("qq_group") and runtime.get("qq", {}).get("identity_verified") and str(preferences["qq_group"]) not in qq_group_ids:
        warnings.append("预设 QQ 群不在当前已登录账号返回的群列表中，请选择当前账号可见的群。")
    selected_chat = str(preferences.get("feishu_chat") or "")
    if selected_chat and selected_chat not in {chat["chat_id"] for chat in chats}:
        warnings.append("所选飞书会话尚未从当前账号列表确认。")
    saved_qq_account = str(preferences.get("qq_account") or "")
    verified_qq_ids = {str(account.get("user_id") or "") for instance in instances for account in instance["accounts"] if account.get("identity_verified")}
    if saved_qq_account not in verified_qq_ids:
        saved_qq_account = ""
    return {
        "qq_instances": instances,
        "feishu_accounts": [{"id": "default", "user_id": str(larkx.get("user_id") or "")}],
        "feishu_chats": chats,
        "suggested": {"qq_instance": instances[0]["id"] if len(instances) == 1 else str(preferences.get("qq_instance") or ""), "qq_account": saved_qq_account, "qq_group": str(preferences.get("qq_group") or ""), "feishu_account": "default", "feishu_chat": selected_chat},
        "coverage": {"qq": "verified_groups" if runtime.get("qq", {}).get("identity_verified") else "waiting_connection", "feishu": "visible_recent_chats" if chats else "waiting_discovery"},
        "warnings": warnings,
    }


def discover_feishu_chats() -> dict[str, Any]:
    from larkx.auth import LarkAuth
    from larkx.client import LarkClient

    client = LarkClient(LarkAuth("default"))
    try:
        chats = []
        recent_ids = client.feed_chats(time.time() - 86400 * 90, max_pages=3)
        path = rules_path(astrbot_root()).parent / "larkx_chats.json"
        try:
            previous = json.loads(path.read_text(encoding="utf-8-sig")).get("chats", [])
        except (OSError, ValueError, AttributeError):
            previous = []
        cached_ids = [str(chat["chat_id"]) for chat in previous if isinstance(chat, dict) and chat.get("chat_id")]
        chat_ids = list(dict.fromkeys([str(chat_id) for chat_id in recent_ids] + cached_ids))[:200]
        for chat_id in chat_ids:
            try:
                info = client.get_chat_info(chat_id) or {}
                if not isinstance(info, dict) or str(info.get("id") or "") != chat_id:
                    continue
                if info.get("isDissolved") or info.get("isInvisible") or str(info.get("status", "")).upper() in {"2", "DELETED"}:
                    continue
                if str(info.get("type") or "").upper() not in {"1", "2", "3", "P2P", "GROUP", "TOPIC_GROUP"}:
                    continue
                chats.append({"chat_id": chat_id, "name": _chat_display_name(client, info, chat_id),
                              "message_type": _chat_type(info.get("type")), "source": "account_metadata",
                              "metadata_checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                              "last_seen": datetime.now(timezone.utc).isoformat(timespec="seconds")})
            except Exception:
                # One inaccessible chat must not leave stale, unverified
                # names in the selectable account metadata cache.
                continue
        _atomic_json(rules_path(astrbot_root()).parent / "larkx_chats.json", {"chats": chats})
    finally:
        client.close()
    return mapping_discovery()


def launch_local_qq() -> dict[str, Any]:
    import psutil

    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\QQ.exe") as key:
            executable = Path(winreg.QueryValueEx(key, None)[0])
    except OSError:
        candidates = [Path("D:/Program Files/Tencent/QQNT/QQ.exe"), Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "Tencent/QQNT/QQ.exe"]
        executable = next((candidate for candidate in candidates if candidate.is_file()), None)
    if executable is None or not executable.is_file():
        raise ValueError("没有找到本机 QQ，请安装 QQ 后重试。")
    already_running = any(str(process.info.get("exe") or "").lower() == str(executable).lower() for process in psutil.process_iter(["exe"]))
    # Use QQ's own launcher. It reuses the current desktop session when one exists.
    os.startfile(str(executable))
    return {"opened": True, "already_running": already_running, "message": "已打开本机 QQ；如未登录，请用手机 QQ 扫描客户端二维码。"}


def _rule_endpoints_key(rule: dict[str, Any]) -> tuple[str, ...]:
    return (
        str(rule.get("source_platform", "")).strip(), str(rule.get("source_platform_id", "")).strip(),
        str(rule.get("source_account_id", "")).strip(), str(rule.get("source_message_type", "GroupMessage")).strip(),
        str(rule.get("source_session_id", "")).strip(), str(rule.get("target_umo", "")).strip(),
        str(rule.get("target_account_id", "")).strip(),
    )


def verify_rule_targets(data: dict[str, Any], existing_rules: list | None = None) -> dict[str, Any]:
    normalized = validate_rules(data)
    # Settings on already active directions can be changed while a platform is offline.
    # New, re-enabled or rerouted directions still require current account verification.
    existing = {_rule_endpoints_key(rule) for rule in (existing_rules or [])
                if isinstance(rule, dict) and rule.get("enabled", True)}
    to_verify = [rule for rule in normalized["rules"] if rule["enabled"] and _rule_endpoints_key(rule) not in existing]
    if not to_verify:
        return normalized
    discovery = mapping_discovery()
    qq_targets: dict[tuple[str, str], set[str]] = {}
    qq_accounts: dict[str, set[str]] = {}
    for instance in discovery["qq_instances"]:
        for account in instance["accounts"]:
            if not account.get("connected") or not account.get("identity_verified"):
                continue
            account_id = str(account.get("user_id") or "")
            qq_accounts.setdefault(instance["id"], set()).add(account_id)
            for group in account.get("groups", []):
                qq_targets.setdefault((instance["id"], str(group["group_id"])), set()).add(account_id)
    feishu_targets = {chat["chat_id"] for chat in discovery["feishu_chats"]}

    def verify_qq(instance: str, group: str, account_id: str, label: str) -> None:
        owners = qq_targets.get((instance, group), set())
        if not owners:
            raise ValueError(f"{label} QQ 群未出现在已验证账号的群列表中。")
        if account_id and account_id not in owners:
            raise ValueError(f"{label} QQ 群不属于所选 QQ 账号。")
        if not account_id and len(owners) != 1:
            raise ValueError(f"{label} QQ 群关联多个账号，请明确选择 QQ 账号。")

    def verify_qq_account(instance: str, account_id: str, label: str) -> None:
        online = qq_accounts.get(instance, set())
        if account_id and account_id not in online:
            raise ValueError(f"{label} QQ 账号当前未通过 OneBot 登录验证。")
        if not account_id and len(online) != 1:
            raise ValueError(f"{label} QQ 私聊关联多个账号，请明确选择 QQ 账号。")

    qq_instance_ids = {str(item["id"]) for item in discovery["qq_instances"]}
    for rule in to_verify:
        target_platform, _, target_session = rule["target_umo"].split(":", 2)
        _, target_type, _ = rule["target_umo"].split(":", 2)
        target_is_qq = target_platform in qq_instance_ids
        if target_platform != "larkx" and not target_is_qq:
            raise ValueError("目标平台必须是 QQ/OneBot 或飞书个人号。")
        source_endpoint = (rule["source_platform"], rule["source_platform_id"],
                           rule["source_account_id"] if rule["source_platform"] == "aiocqhttp" else "",
                           rule["source_message_type"], rule["source_session_id"])
        target_kind = "larkx" if target_platform == "larkx" else "aiocqhttp"
        target_instance = "default" if target_kind == "larkx" else target_platform
        target_endpoint = (target_kind, target_instance,
                           rule["target_account_id"] if target_is_qq else "",
                           target_type, target_session)
        if source_endpoint == target_endpoint:
            raise ValueError("来源和目标不能是同一个账号会话。")
        if rule["source_platform"] == "aiocqhttp":
            if rule["source_message_type"] == "GroupMessage":
                verify_qq(rule["source_platform_id"], rule["source_session_id"], rule["source_account_id"], "来源")
            else:
                verify_qq_account(rule["source_platform_id"], rule["source_account_id"], "来源")
        else:
            if rule["source_session_id"] not in feishu_targets:
                raise ValueError("来源飞书会话未从当前账号确认，未启用映射。")
        if not target_is_qq:
            if target_session not in feishu_targets:
                raise ValueError("目标飞书会话未从当前账号确认，未启用映射。")
        else:
            if target_type == "GroupMessage":
                verify_qq(target_platform, target_session, rule["target_account_id"], "目标")
            else:
                verify_qq_account(target_platform, rule["target_account_id"], "目标")
    return normalized


def save_workbench_rules(data: dict[str, Any]) -> dict[str, Any]:
    root = astrbot_root()
    with _rules_lock:
        current = read_rules(root)
        updated = {"bridge_enabled": current["bridge_enabled"], **data}
        return save_rules(root, verify_rule_targets(updated, current["rules"]))


def set_bridge_enabled(enabled: bool) -> dict[str, Any]:
    root = astrbot_root()
    with _rules_lock:
        current = read_rules(root)
        current["bridge_enabled"] = bool(enabled)
        saved = save_rules(root, current)
    return {"enabled": saved["bridge_enabled"], "total": len(saved["rules"]), "active": sum(bool(rule.get("enabled", True)) for rule in saved["rules"])}


def delivery_queue_status() -> dict[str, Any]:
    directory = rules_path(astrbot_root()).parent
    try:
        return {"available": True,
                **read_inbox_stats(directory / "larkx-inbox.sqlite3"),
                "receipts": read_delivery_stats(directory / "delivery_seen.sqlite3")}
    except Exception:
        # Database details and pending message payloads remain local to the
        # transport; the status endpoint only exposes aggregate counts.
        return {"available": False, "pending": 0, "processing": 0, "failed": 0, "done": 0,
                "receipts": {"sent": 0, "uncertain": 0}}


def retry_delivery_queue() -> dict[str, Any]:
    directory = rules_path(astrbot_root()).parent
    try:
        count = retry_failed_inbox(directory / "larkx-inbox.sqlite3")
    except Exception as exc:
        raise ValueError("无法读取本地失败队列，请导出诊断日志") from exc
    return {"queued": count}


def get_status() -> dict[str, Any]:
    settings = load_settings()
    root = astrbot_root(settings)
    running = port_open(settings["astrbot_port"])
    with _runtime_lock:
        process = _astrbot_process
        bootstrap_password = _initial_password
    managed_pid = _managed_astrbot_pid()
    plugin_dir = plugin_destination(root)
    plugin_installed = all((plugin_dir / name).is_file() for name in PLUGIN_FILES)
    try:
        config = read_rules(root)
    except (OSError, json.JSONDecodeError, ValueError):
        config = {"managed_platforms": ["aiocqhttp", "larkx"], "bridge_enabled": True, "rules": []}
    rules = config.get("rules", [])
    active_rules = [rule for rule in rules if isinstance(rule, dict) and rule.get("enabled", True)]
    try:
        astrbot_config = json.loads((root / "data" / "cmd_config.json").read_text(encoding="utf-8-sig"))
        provider_list = astrbot_config.get("provider", []) if isinstance(astrbot_config, dict) else []
        model_count = len(provider_list) if isinstance(provider_list, list) else 0
    except (OSError, json.JSONDecodeError):
        model_count = 0
    logs = _tail_logs(root)
    log_text = "\n".join(logs)
    runtime = read_runtime_status()
    qq_connected = runtime.get("qq", {}).get("connected") is True
    larkx = larkx_status()
    feishu_connected = runtime.get("feishu", {}).get("connected") is True
    snow = runtime.get("snowluma", {})
    snow_port = snow.get("webui_port")
    snow_url = f"http://127.0.0.1:{snow_port}/" if snow_port else "http://127.0.0.1:5099/"
    root_ready = (root / ".astrbot").is_file() and (root / "data" / "plugins").is_dir()
    return {
        "settings": settings,
        "workbench_root": str(ROOT),
        "workbench_pid": os.getpid(),
        "version": WORKBENCH_VERSION,
        "background": background_runtime.status(ROOT),
        "astrbot": {
            "installed": (ASTRBOT_EXE.with_name("python.exe").is_file()
                          and importlib.util.find_spec("astrbot") is not None),
            "root_ready": root_ready,
            "running": running,
            "starting": not running and process is not None and process.poll() is None,
            "managed_here": managed_pid is not None,
            "pid": managed_pid,
            "url": f"http://127.0.0.1:{settings['astrbot_port']}/",
        },
        "plugin": {
            "installed": plugin_installed,
            "config_path": str(rules_path(root)),
            "build": bridge_build_status(runtime),
        },
        "snowluma_plugin": {
            "installed": (
                root / "data" / "plugins" / "pulids_snowluma_go_to_astrbot" / "metadata.yaml"
            ).is_file(),
            "astrbot_url": "http://127.0.0.1:6185/#/extension/plugins",
        },
        "larkagentx": larkx,
        "runtime": runtime,
        "delivery_queue": delivery_queue_status(),
        "snowluma": {
            "installed": snow.get("installed"),
            "running": snow.get("running"),
            "onebot_configured": snowluma_onebot_configured(),
            "first_run_pending": snow.get("consent_required"),
            "url": snow_url,
        },
        "platforms": {
            "qq": "connected" if qq_connected else ("waiting" if running else "stopped"),
            "feishu": "connected" if feishu_connected else ("waiting" if running else "stopped"),
        },
        "rules": {"total": len(rules), "enabled": len(active_rules)},
        "bridge_enabled": bool(config.get("bridge_enabled", True)),
        "models": {"configured": model_count > 0, "provider_count": model_count},
        "ai": "not_configured",
        "bootstrap": {
            "username": _initial_username,
            "password": bootstrap_password,
            "password_pending": bootstrap_password is not None,
        },
        "logs": logs,
    }


def integration_status() -> dict[str, Any]:
    """Expose only non-secret runtime and routing metadata to the main workbench."""
    settings = load_settings()
    root = astrbot_root(settings)
    running = port_open(settings["astrbot_port"])
    plugin_dir = plugin_destination(root)
    plugin_installed = all((plugin_dir / name).is_file() for name in PLUGIN_FILES)
    runtime = read_runtime_status()
    qq_runtime = runtime.get("qq", {})
    feishu_runtime = runtime.get("feishu", {})
    snow = runtime.get("snowluma", {})
    qq_connected = bool(qq_runtime.get("connected"))
    try:
        rules = read_rules(root).get("rules", [])
    except (OSError, json.JSONDecodeError, ValueError):
        rules = []
    enabled_rules = [rule for rule in rules if isinstance(rule, dict) and rule.get("enabled", True)]
    larkx = larkx_status()
    chats = larkx.get("chats", [])
    monitor = feishu_runtime
    return {
        "app": "information-mover-platform-bridge",
        "workbench_root": str(ROOT),
        "checked_at": time.time(),
        "runtime": runtime,
        "astrbot": {
            "running": running,
            "plugin_installed": plugin_installed,
            "websocket_port_open": port_open(6199),
        },
        "snowluma": {
            "installed": snow.get("installed"),
            "running": snow.get("running"),
            "onebot_configured": snowluma_onebot_configured(),
            "consent_pending": snow.get("consent_required"),
            "url": f"http://127.0.0.1:{snow.get('webui_port') or 5099}/",
        },
        "qq": {
            "status": "connected" if qq_connected else ("waiting" if running else "stopped"),
            "identity_verified": bool(qq_runtime.get("identity_verified")),
            "identity_source": "OneBot get_login_info",
            "account_id": next((account["user_id"] for account in qq_runtime.get("accounts", []) if account.get("identity_verified")), ""),
            "monitor": {key: qq_runtime.get(key, 0 if key.endswith("_count") else "") for key in ("received_count", "sent_count", "failed_count", "last_received_at", "last_sent_at", "last_send_status")},
            "monitor": {key: qq_runtime.get(key, 0 if key.endswith("_count") else "") for key in ("received_count", "sent_count", "failed_count", "last_received_at", "last_sent_at", "last_send_status")},
        },
        "feishu": {
            "has_credentials": bool(larkx.get("has_credentials")),
            "user_id": str(larkx.get("user_id") or ""),
            "listener_state": str(feishu_runtime.get("state") or "unknown"),
            "listener_message": str(larkx.get("bridge_message") or "")[:160],
            "agent_backend": "none",
            "monitor": {
                "received_count": int(monitor.get("received_count") or 0),
                "last_received_at": str(monitor.get("last_received_at") or ""),
                "last_chat_id": str(monitor.get("last_chat_id") or ""),
                "sent_count": int(monitor.get("sent_count") or 0),
                "failed_count": int(monitor.get("failed_count") or 0),
                "last_sent_at": str(monitor.get("last_sent_at") or ""),
                "last_failed_at": str(monitor.get("last_failed_at") or ""),
                "last_send_status": str(monitor.get("last_send_status") or ""),
            },
            "chats": [
                {
                    "chat_id": str(chat.get("chat_id") or ""),
                    "name": str(chat.get("name") or "")[:120],
                    "message_type": str(chat.get("message_type") or ""),
                    "last_seen": str(chat.get("last_seen") or ""),
                }
                for chat in chats[-200:]
                if isinstance(chat, dict) and chat.get("chat_id")
            ] if isinstance(chats, list) else [],
        },
        "routing": {
            "total": len(rules),
            "enabled": len(enabled_rules),
            "bridge_enabled": bool(read_rules(root).get("bridge_enabled", True)),
            "ai_disabled": True,
        },
    }


class WorkbenchServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class Handler(BaseHTTPRequestHandler):
    server_version = "MoverWorkbench/2.0"

    def log_message(self, format: str, *args: Any) -> None:
        if args and len(args) > 1 and str(args[1]) == "200" and str(args[0]).split(" ")[1].split("?")[0] in {"/api/status", "/api/logs", "/api/larkagentx/qr/status"}:
            return
        # Query strings can contain searches or resource identifiers.
        append_log(WORKBENCH_LOG, datetime.now(timezone.utc).isoformat(timespec="seconds") + " " + re.sub(r"\?[^ ]*", "", format % args))

    def _send_bytes(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self'; script-src 'self'; connect-src 'self'; img-src 'self' data:")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send_bytes(status, body, "application/json; charset=utf-8")

    def _local_request(self) -> bool:
        host = self.headers.get("Host", "").split(":", 1)[0].strip("[]").lower()
        origin = self.headers.get("Origin", "")
        if host not in {"127.0.0.1", "localhost"}:
            return False
        if origin:
            return urlparse(origin).hostname in {"127.0.0.1", "localhost"}
        return True

    def _read_json(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ValueError("Content-Length 无效") from exc
        if length <= 0 or length > 1_000_000:
            raise ValueError("请求内容为空或超过 1 MB")
        data = json.loads(self.rfile.read(length).decode("utf-8"))
        if not isinstance(data, dict):
            raise ValueError("请求内容必须是 JSON 对象")
        return data

    def do_GET(self) -> None:
        if not self._local_request():
            self._json(403, {"error": "仅允许本机访问"})
            return
        path = urlparse(self.path).path
        if path == "/api/integration/status":
            self._json(200, integration_status())
        elif path == "/api/status":
            self._json(200, get_status())
        elif path == "/api/diagnostics/build":
            self._json(200, build_diagnostics())
        elif path == "/api/logs":
            try:
                params = {key: values[-1] for key, values in parse_qs(urlparse(self.path).query).items()}
                self._json(200, query_logs(ROOT, astrbot_root(), params))
            except (ValueError, OSError) as exc:
                self._json(400, {"error": str(exc)})
        elif path == "/api/logs/export":
            try:
                self._send_bytes(200, export_logs(ROOT, astrbot_root()), "application/zip")
            except (ValueError, OSError):
                self._json(500, {"error": "诊断日志导出失败，请稍后重试"})
        elif path == "/api/larkagentx/status":
            self._json(200, larkx_status())
        elif path == "/api/larkagentx/qr/status":
            with _larkx_qr_lock:
                self._json(200, dict(_larkx_qr_state))
        elif path == "/api/rules":
            try:
                self._json(200, read_rules(astrbot_root()))
            except Exception as exc:
                self._json(500, {"error": str(exc)})
        elif path == "/api/bridge/status":
            config = read_rules(astrbot_root())
            self._json(200, {"enabled": config["bridge_enabled"], "total": len(config["rules"]), "active": sum(bool(rule.get("enabled", True)) for rule in config["rules"])})
        elif path == "/api/mapping/discovery":
            self._json(200, mapping_discovery())
        elif path == "/":
            self._send_file(WEB_ROOT / "index.html", "text/html; charset=utf-8")
        elif path == "/assets/app.js":
            self._send_file(WEB_ROOT / "app.js", "text/javascript; charset=utf-8")
        elif path == "/assets/style.css":
            self._send_file(WEB_ROOT / "style.css", "text/css; charset=utf-8")
        else:
            self._json(404, {"error": "Not found"})

    def _send_file(self, path: Path, content_type: str) -> None:
        try:
            self._send_bytes(200, path.read_bytes(), content_type)
        except OSError:
            self._json(404, {"error": "WorkBench asset missing"})

    def do_POST(self) -> None:
        if not self._local_request():
            self._json(403, {"error": "仅允许本机访问"})
            return
        path = urlparse(self.path).path
        try:
            data = {} if path in {"/api/astrbot/stop", "/api/bootstrap/clear"} and self.headers.get("Content-Length", "0") == "0" else self._read_json()
            if path == "/api/settings":
                result = self._save_settings(data)
            elif path == "/api/plugin/install":
                result = {"installed": str(install_plugin(astrbot_root()))}
            elif path == "/api/rules":
                result = save_workbench_rules(data)
            elif path == "/api/bridge/enable":
                result = set_bridge_enabled(data.get("enabled") is True)
            elif path == "/api/qq/launch":
                result = launch_local_qq()
            elif path == "/api/mapping/discovery":
                try:
                    result = discover_feishu_chats()
                except Exception as exc:
                    raise ValueError(f"读取飞书会话失败（{type(exc).__name__}）；本机映射与凭证保持不变。") from exc
            elif path == "/api/astrbot/start":
                automatic_recovery = data.get("automatic_recovery") is True
                respect_saved_desired = data.get("respect_saved_desired") is True
                with _astrbot_start_lock:
                    result = start_astrbot(bool(data.get("reset_password", False)), automatic_recovery=automatic_recovery,
                                          respect_saved_desired=respect_saved_desired)
                    if not automatic_recovery and not respect_saved_desired and not data.get("preserve_background"):
                        background_runtime.set_desired(ROOT, astrbot_desired=True)
            elif path == "/api/astrbot/stop":
                with _astrbot_start_lock:
                    if not data.get("preserve_background"):
                        background_runtime.set_desired(ROOT, astrbot_desired=False)
                    result = stop_astrbot()
            elif path == "/api/background/config":
                result = background_runtime.configure(ROOT, data, port=self.server.server_address[1])
            elif path == "/api/snowluma/start":
                result = start_snowluma()
            elif path == "/api/snowluma/configure-onebot":
                result = configure_snowluma_onebot()
            elif path == "/api/larkagentx/qr/start":
                result = start_larkx_qr_login()
            elif path == "/api/bridge/retry-failed":
                result = retry_delivery_queue()
            elif path == "/api/bootstrap/clear":
                global _initial_password
                with _runtime_lock:
                    _initial_password = None
                BOOTSTRAP_SECRET_FILE.unlink(missing_ok=True)
                result = {"cleared": True}
            elif path == "/api/bootstrap/set":
                password = str(data.get("password", ""))
                save_admin_password(password)
                result = {"saved": True}
            elif path == "/api/workbench/stop":
                if not data.get("preserve_background"):
                    background_runtime.set_desired(ROOT, enabled=False)
                self._json(200, {"stopping": True})
                threading.Thread(target=self.server.shutdown, daemon=True).start()
                return
            else:
                self._json(404, {"error": "Not found"})
                return
            self._json(200, result)
        except (ValueError, OSError, json.JSONDecodeError, subprocess.SubprocessError) as exc:
            self._json(400, {"error": str(exc)})

    def _save_settings(self, data: dict[str, Any]) -> dict[str, Any]:
        root_value = str(data.get("astrbot_root", "")).strip()
        if not root_value:
            raise ValueError("AstrBot 目录不能为空")
        root = Path(root_value).expanduser().resolve()
        if not (root / ".astrbot").is_file():
            raise ValueError("目录中没有 .astrbot 标记；请填写已初始化的 AstrBot 根目录")
        try:
            port = int(data.get("astrbot_port", 6185))
        except (TypeError, ValueError) as exc:
            raise ValueError("端口必须是数字") from exc
        if not 1024 <= port <= 65535:
            raise ValueError("端口范围必须在 1024 到 65535 之间")
        if port != load_settings()["astrbot_port"] and port_open(port):
            raise ValueError(f"端口 {port} 已被占用")
        result = {"astrbot_root": str(root), "astrbot_port": port}
        _atomic_json(SETTINGS_FILE, result)
        return result


def _ensure_background_guard(port: int) -> None:
    """Start the guard for direct launches without blocking the local HTTP service."""
    try:
        background_runtime.ensure_settings(ROOT)
        background_runtime.ensure_supervisor(ROOT, port)
    except Exception as exc:
        # Keep settings/status reachable when the guard cannot start. Do not log
        # exception text: platform paths and process output may be private.
        message = f"Background supervisor did not become ready ({type(exc).__name__}); check background status."
        print(message, file=sys.stderr, flush=True)
        try:
            append_log(WORKBENCH_LOG, datetime.now(timezone.utc).isoformat(timespec="seconds") + " ERROR " + message)
        except OSError:
            pass


def main() -> None:
    settings = load_settings()
    port = int(os.environ.get("MOVER_WORKBENCH_PORT", str(background_runtime.settings(ROOT)["port"])))
    server = WorkbenchServer(("127.0.0.1", port), Handler)
    print(f"搬运工V2 工作台: http://127.0.0.1:{port}/", flush=True)
    print(f"AstrBot 目录: {settings['astrbot_root']}", flush=True)
    try:
        # Launch only after this install owns its port. A separate thread lets
        # the supervisor probe /api/status while startup waits for its heartbeat.
        threading.Thread(target=_ensure_background_guard, args=(port,), daemon=True,
                         name="mover-background-start").start()
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()

