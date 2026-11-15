from __future__ import annotations

import json
import ipaddress
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from astrbot_plugin_message_bridge.bridge_core import BridgeRouter, valid_umo


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
PLUGIN_NAME = "astrbot_plugin_message_bridge"
PLUGIN_FILES = (
    "__init__.py",
    "bridge_core.py",
    "bridge_rules.example.json",
    "larkx_bridge.py",
    "larkx_media.py",
    "qq_emojis.py",
    "qq_transport.py",
    "runtime_status.py",
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
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temp, path)


def load_settings() -> dict[str, Any]:
    if not SETTINGS_FILE.exists():
        _atomic_json(SETTINGS_FILE, DEFAULT_SETTINGS)
    try:
        data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        data = {}
    root = str(data.get("astrbot_root", DEFAULT_SETTINGS["astrbot_root"])).strip()
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
    with _runtime_lock:
        process = _astrbot_process
    if process is not None and process.poll() is None:
        return process.pid
    try:
        pid = int(ASTRBOT_PID_FILE.read_text(encoding="ascii").strip())
        import psutil

        candidate = psutil.Process(pid)
        command = [str(part).lower() for part in candidate.cmdline()]
        expected_exe = str(ASTRBOT_EXE).lower()
        executable = str(candidate.exe()).lower()
        is_astrbot = executable == expected_exe and "run" in command
        if is_astrbot:
            return pid
    except Exception:
        pass
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
    with log_path.open("a", encoding="utf-8", errors="replace") as log_file:
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
            log_file.write(line + "\n")
            log_file.flush()


def start_astrbot(reset_password: bool = False) -> dict[str, Any]:
    global _astrbot_process, _initial_password
    settings = load_settings()
    root = astrbot_root(settings)
    if not ASTRBOT_EXE.is_file():
        raise ValueError("AstrBot 运行环境未安装；检查 runtime/.venv/Scripts/astrbot.exe")
    if not (root / ".astrbot").is_file():
        raise ValueError("AstrBot 尚未初始化")
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

    command = [str(ASTRBOT_EXE), "run", "-p", str(settings["astrbot_port"])]
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
    creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    process = subprocess.Popen(
        command,
        cwd=root,
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        creationflags=creation_flags,
    )
    with _runtime_lock:
        _astrbot_process = process
    ASTRBOT_PID_FILE.write_text(str(process.pid), encoding="ascii")
    threading.Thread(
        target=_start_output_capture,
        args=(process, log_path),
        name="astrbot-log-reader",
        daemon=True,
    ).start()
    return {"started": True, "pid": process.pid, "port": settings["astrbot_port"]}


def stop_astrbot() -> dict[str, Any]:
    global _astrbot_process
    with _runtime_lock:
        process = _astrbot_process
    tracked_pid = _managed_astrbot_pid()
    if tracked_pid is not None:
        import psutil

        managed_process = psutil.Process(tracked_pid)
        children = managed_process.children(recursive=True)
        children = [child for child in children if child.pid != os.getpid()]
        for child in reversed(children):
            child.terminate()
        managed_process.terminate()
        _, alive = psutil.wait_procs([*children, managed_process], timeout=10)
        for process in alive:
            process.kill()
        if alive:
            psutil.wait_procs(alive, timeout=5)
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


def _snowluma_ws_client_config(config: dict[str, Any]) -> dict[str, Any]:
    networks = config.get("networks")
    if not isinstance(networks, dict):
        networks = {}
        config["networks"] = networks
    clients = networks.get("wsClients")
    if not isinstance(clients, list):
        clients = []
        networks["wsClients"] = clients

    name = "mover-v2-astrbot"
    target_url = "ws://127.0.0.1:6199/ws"
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
    }
    entry.pop("accessToken", None)
    if target is None:
        clients.append(entry)
    else:
        target.clear()
        target.update(entry)
    return config


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
        lines = latest.read_text(encoding="utf-8", errors="replace").splitlines()[-limit:]
    except OSError:
        return []
    return [INITIAL_PASSWORD_RE.sub("Initial password: [已隐藏]", line) for line in lines]


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


def read_runtime_status() -> dict[str, Any]:
    path = rules_path(astrbot_root()).parent / "current_runtime_status.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        updated = datetime.fromisoformat(str(raw.get("updated_at", "")))
        age = (datetime.now(timezone.utc) - updated).total_seconds()
        fresh = 0 <= age <= 40
    except (OSError, ValueError, TypeError, AttributeError):
        return {"available": False, "stale": True, "qq": {}, "feishu": {}, "snowluma": {}}
    result = {"available": True, "stale": not fresh, "updated_at": raw.get("updated_at")}
    for platform in ("qq", "feishu"):
        source = raw.get(platform, {})
        result[platform] = {
            "state": str(source.get("state") or "unknown") if fresh else "stale",
            "connected": bool(source.get("connected")) and fresh,
            "identity_verified": bool(source.get("identity_verified")) and fresh,
            "received_count": max(0, int(source.get("received_count") or 0)),
            "last_received_at": str(source.get("last_received_at") or ""),
            "last_chat_id": str(source.get("last_chat_id") or ""),
            "sent_count": max(0, int(source.get("sent_count") or 0)),
            "failed_count": max(0, int(source.get("failed_count") or 0)),
            "last_sent_at": str(source.get("last_sent_at") or ""),
            "last_failed_at": str(source.get("last_failed_at") or ""),
            "last_send_status": str(source.get("last_send_status") or ""),
        }
    result["qq"]["accounts"] = []
    for account in raw.get("qq", {}).get("accounts", [])[:20]:
        if not isinstance(account, dict):
            continue
        safe = {key: str(account.get(key) or "")[:160] for key in ("platform_id", "state", "user_id", "nickname", "groups_status", "checked_at")}
        safe.update(connected=bool(account.get("connected")) and fresh, identity_verified=bool(account.get("identity_verified")) and fresh)
        safe["groups"] = [{"group_id": str(group.get("group_id") or ""), "group_name": str(group.get("group_name") or "")[:120]} for group in account.get("groups", []) if isinstance(group, dict) and group.get("group_id")][:3000]
        result["qq"]["accounts"].append(safe)
    result["feishu"].update({key: str(raw.get("feishu", {}).get(key) or "")[:160] for key in ("user_id", "account_id")})
    snow = raw.get("snowluma", {})
    result["snowluma"] = {key: snow.get(key) if fresh and isinstance(snow.get(key), bool) else None for key in ("installed", "running", "auth_ok", "consent_required")}
    port = snow.get("webui_port")
    result["snowluma"].update(state=str(snow.get("state") or "unknown") if fresh else "stale", webui_port=port if type(port) is int and 1024 <= port <= 65535 else None)
    result["snowluma"]["accounts"] = [{"uin": str(account.get("uin") or ""), "nickname": str(account.get("nickname") or "")[:120]} for account in snow.get("accounts", []) if isinstance(account, dict) and account.get("uin")][:20]
    return result


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


def verify_rule_targets(data: dict[str, Any]) -> dict[str, Any]:
    normalized = validate_rules(data)
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

    for rule in normalized["rules"]:
        if not rule["enabled"]:
            continue
        target_platform, _, target_session = rule["target_umo"].split(":", 2)
        _, target_type, _ = rule["target_umo"].split(":", 2)
        qq_instance_ids = {str(item["id"]) for item in discovery["qq_instances"]}
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


def set_bridge_enabled(enabled: bool) -> dict[str, Any]:
    root = astrbot_root()
    current = read_rules(root)
    current["bridge_enabled"] = bool(enabled)
    saved = save_rules(root, current)
    return {"enabled": saved["bridge_enabled"], "total": len(saved["rules"]), "active": sum(bool(rule.get("enabled", True)) for rule in saved["rules"])}


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
        "astrbot": {
            "installed": ASTRBOT_EXE.is_file(),
            "root_ready": root_ready,
            "running": running,
            "managed_here": managed_pid is not None,
            "pid": managed_pid,
            "url": f"http://127.0.0.1:{settings['astrbot_port']}/",
        },
        "plugin": {
            "installed": plugin_installed,
            "config_path": str(rules_path(root)),
        },
        "snowluma_plugin": {
            "installed": (
                root / "data" / "plugins" / "pulids_snowluma_go_to_astrbot" / "metadata.yaml"
            ).is_file(),
            "astrbot_url": "http://127.0.0.1:6185/#/extension/plugins",
        },
        "larkagentx": larkx,
        "runtime": runtime,
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
        WORKBENCH_LOG.parent.mkdir(parents=True, exist_ok=True)
        with WORKBENCH_LOG.open("a", encoding="utf-8") as log_file:
            log_file.write((format % args) + "\n")

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
            data = self._read_json() if path != "/api/astrbot/stop" and path != "/api/bootstrap/clear" else {}
            if path == "/api/settings":
                result = self._save_settings(data)
            elif path == "/api/plugin/install":
                result = {"installed": str(install_plugin(astrbot_root()))}
            elif path == "/api/rules":
                result = save_rules(astrbot_root(), verify_rule_targets(data))
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
                result = start_astrbot(bool(data.get("reset_password", False)))
            elif path == "/api/astrbot/stop":
                result = stop_astrbot()
            elif path == "/api/snowluma/start":
                result = start_snowluma()
            elif path == "/api/snowluma/configure-onebot":
                result = configure_snowluma_onebot()
            elif path == "/api/larkagentx/qr/start":
                result = start_larkx_qr_login()
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


def main() -> None:
    settings = load_settings()
    port = int(os.environ.get("MOVER_WORKBENCH_PORT", "18766"))
    server = WorkbenchServer(("127.0.0.1", port), Handler)
    print(f"搬运工V2 工作台: http://127.0.0.1:{port}/", flush=True)
    print(f"AstrBot 目录: {settings['astrbot_root']}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()

