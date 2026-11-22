"""Bounded queries over local component logs; never accepts a filesystem path."""
from __future__ import annotations

import json
import hashlib
import io
import logging
import re
import threading
import zipfile
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path

try:
    MODULE_BUILD = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
except OSError:
    MODULE_BUILD = ""

COMPONENTS = ("workbench", "astrbot", "snowluma", "bridge", "feishu", "supervisor", "bootstrap")
_lock = threading.RLock()
_handlers = {}
_ansi = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_secrets = re.compile(r"(?i)((?:initial password|authorization|access[_-]?token|refresh[_-]?token|app[_-]?secret|cookie|password|crypto[_-]?token)\s*[\"']?\s*[:=]\s*[\"']?)([^\s,;\"'}]+)")


def redact(line: str) -> str:
    line = _ansi.sub("", line)
    line = re.sub(r"(?i)authorization:\s*bearer\s+\S+", "Authorization: [已隐藏]", line)
    line = re.sub(r"(?i)(?:https?|wss?)://[^\s\"'<>]+", "[资源链接已隐藏]", line)
    return _secrets.sub(r"\1[已隐藏]", line)


def append_log(path: Path, line: str) -> None:
    """Rotate owned output logs while letting upstream keep its own policy."""
    with _lock:
        key = str(path)
        handler = _handlers.get(key)
        if handler is None:
            path.parent.mkdir(parents=True, exist_ok=True)
            handler = RotatingFileHandler(path, maxBytes=8 * 1024 * 1024, backupCount=5, encoding="utf-8", delay=True)
            _handlers[key] = handler
        record = logging.LogRecord("mover", logging.INFO, "", 0, redact(line), (), None)
        handler.emit(record)
        handler.close()


def _sources(project: Path, astrbot: Path):
    specs = [
        ("workbench", project / "runtime", "workbench.log*"),
        ("workbench", project / "runtime/logs", "workbench-*.log"),
        ("bootstrap", project / "runtime/logs", "bootstrap-*.log"),
        ("supervisor", project / "runtime/logs", "supervisor.log*"),
        ("astrbot", astrbot / "data/logs", "*.log*"),
        ("astrbot", astrbot / "data/logs/astrbot", "*.log*"),
        ("snowluma", astrbot / "data/plugin_data/pulids_snowluma_go_to_astrbot/snowluma/logs", "*.log*"),
        ("snowluma", astrbot / "data/plugin_data/pulids_snowluma_go_to_astrbot/snowluma/app/logs", "*.log*"),
        ("snowluma", project / "runtime/SnowLuma/logs", "*.log*"),
        ("snowluma", project / "runtime/SnowLuma/app/logs", "*.log*"),
        ("bridge", astrbot / "data/plugin_data/astrbot_plugin_message_bridge", "events.jsonl*"),
    ]
    seen = set()
    for component, folder, pattern in specs:
        if folder.is_symlink():
            continue
        for path in folder.glob(pattern):
            if path.is_symlink() or not path.is_file() or path in seen:
                continue
            seen.add(path)
            yield component, path


def export_logs(project: Path, astrbot: Path, *, max_file_bytes: int = 16 * 1024 * 1024,
                max_total_bytes: int = 128 * 1024 * 1024, max_files: int = 240) -> bytes:
    """Export retained component logs, independent of the screen's 1000 rows.

    Bridge evidence is allocated first, so QQ health polling cannot consume its
    budget. Limits, rotated files, missing files and truncation are all reported
    in the manifest. Only owned, allowlisted log locations are inspected; no
    runtime configuration, account sessions or attachments are included.
    """
    if min(max_file_bytes, max_total_bytes, max_files) <= 0:
        raise ValueError("日志导出限制必须大于零")
    priority = {name: index for index, name in enumerate(("bridge", "feishu", "astrbot", "snowluma", "supervisor", "workbench", "bootstrap"))}
    sources = []
    for component, path in _sources(project, astrbot):
        try:
            stat = path.stat()
        except OSError:
            continue
        sources.append((component, path, stat))
    sources.sort(key=lambda source: (priority.get(source[0], 99), -source[2].st_mtime, source[1].name))
    manifest = {
        "format_version": 1,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "scope": "retained_component_logs",
        "screen_filters_applied": False,
        "limits": {"max_file_bytes": max_file_bytes, "max_total_bytes": max_total_bytes, "max_files": max_files},
        "files": [],
        "complete": True,
    }
    output = io.BytesIO()
    total, count = 0, 0
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for component, path, stat in sources:
            entry = {"component": component, "name": path.name, "source_bytes": stat.st_size}
            manifest["files"].append(entry)
            allowance = min(max_file_bytes, max_total_bytes - total)
            if count >= max_files or allowance <= 0:
                entry["status"] = "skipped_limit"
                manifest["complete"] = False
                continue
            try:
                with path.open("rb") as stream:
                    # Bound the exported snapshot even if the source is growing.
                    end = min(stat.st_size, stream.seek(0, 2))
                    start = max(0, end - allowance)
                    stream.seek(start)
                    if start:
                        stream.readline(min(64 * 1024, max(0, end - start)))
                    start = stream.tell()
                    data = stream.read(max(0, end - start))
            except OSError:
                entry["status"] = "unavailable"
                manifest["complete"] = False
                continue
            text = data.decode("utf-8-sig" if start == 0 else "utf-8", errors="replace")
            # Per-line redaction also prevents URL values from spanning records.
            sanitized = "\n".join(redact(line) for line in text.splitlines())
            filename = f"logs/{component}/{count + 1:03d}-{path.name}"
            archive.writestr(filename, sanitized.encode("utf-8"))
            entry.update(status="truncated" if start else "included", archive_path=filename,
                         start_byte=start, exported_source_bytes=len(data))
            if start:
                manifest["complete"] = False
            total += len(data)
            count += 1
        manifest["exported_files"] = count
        manifest["exported_source_bytes"] = total
        archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8"))
        archive.writestr("使用说明.txt", (
            "此诊断包包含各组件保留的日志和轮转日志，不受工作台当前页1000条或筛选条件限制。\n"
            "没有打包登录数据、规则文件、配置文件或聊天附件。日志中的资源链接与常见凭据字段已隐藏。\n"
            "manifest.json列出所有发现的日志；complete=false表示受大小限制或文件轮转影响，"
            "部分日志被截断或未能读取，不能把此包当作完整历史。已被上游清理的旧日志无法恢复。\n"
        ).encode("utf-8"))
    return output.getvalue()


def _timestamp(line, fallback):
    match = re.search(r"\d{4}-\d\d-\d\d[T ]\d\d:\d\d:\d\d(?:\.\d+)?(?:Z|[+-]\d\d:\d\d)?", line)
    if match:
        try:
            value = datetime.fromisoformat(match.group().replace("Z", "+00:00"))
            return (value if value.tzinfo else value.astimezone()).timestamp()
        except ValueError:
            pass
    return fallback


def query_logs(project: Path, astrbot: Path, params: dict) -> dict:
    component = str(params.get("component", "all"))
    if component not in ("all", *COMPONENTS):
        raise ValueError("未知日志组件")
    level = str(params.get("level", "all")).upper()
    if level not in {"ALL", "DEBUG", "INFO", "WARNING", "ERROR"}:
        raise ValueError("未知日志级别")
    try:
        limit = max(1, min(int(params.get("limit", 300)), 1000))
        since = datetime.fromisoformat(str(params["since"]).replace("Z", "+00:00")).timestamp() if params.get("since") else 0
        until = datetime.fromisoformat(str(params["until"]).replace("Z", "+00:00")).timestamp() if params.get("until") else float("inf")
    except (ValueError, TypeError) as exc:
        raise ValueError("日志数量或时间格式无效") from exc
    keyword = str(params.get("q", ""))[:200].casefold()
    rows, files = [], []
    sources = sorted(_sources(project, astrbot), key=lambda x: x[1].stat().st_mtime, reverse=True)
    relevant = [(c, p) for c, p in sources if component in ("all", c) or (component == "feishu" and c in {"astrbot", "bridge"}) or (component == "bridge" and c == "astrbot")]
    bounded = False
    selected = str(params.get("file", ""))
    try:
        before = max(0, int(params["before"])) if params.get("before") else None
    except (ValueError, TypeError) as exc:
        raise ValueError("日志分页位置无效") from exc
    next_before = None
    # A query scans a bounded tail; date-named files remain selectable after rotation.
    for source_component, path in relevant[:60]:
        try:
            stat = path.stat()
        except OSError:
            continue
        file_id = hashlib.sha256(str(path).encode("utf-8")).hexdigest()[:20]
        files.append({"id": file_id, "component": source_component, "name": path.name, "bytes": stat.st_size})
        if selected and selected != file_id:
            continue
        if since and stat.st_mtime < since:
            continue
        try:
            with path.open("rb") as stream:
                end = min(before, stat.st_size) if selected and before is not None else stat.st_size
                offset = max(0, end - 256 * 1024)
                stream.seek(offset)
                if offset:
                    stream.readline()
                    bounded = True
                start = stream.tell()
                lines = stream.read(max(0, end - start)).splitlines(keepends=True)
                if selected:
                    next_before = start if offset else None
        except OSError:
            continue
        line_offset = start
        for index, data in enumerate(lines):
            raw_offset = line_offset
            line_offset += len(data)
            raw = data.decode("utf-8", errors="replace").rstrip("\r\n")
            label, stamp, detected = source_component, _timestamp(raw, stat.st_mtime), "INFO"
            try:
                event = json.loads(raw)
                if isinstance(event, dict):
                    label = str(event.get("component") or label)
                    detected = str(event.get("level") or "INFO").upper()
            except (ValueError, TypeError):
                pass
            if label == "astrbot" and any(t in raw for t in ("搬运工V2", "MessageBridge", "message_bridge")):
                label = "bridge"
            if label == "astrbot" and any(t in raw for t in ("LarkAgentX", "larkx", "飞书")):
                label = "feishu"
            if re.search(r"\b(ERROR|CRITICAL|FATAL)\b|Traceback|失败|异常", raw, re.I):
                detected = "ERROR"
            elif re.search(r"\b(WARN|WARNING)\b", raw, re.I):
                detected = "WARNING"
            elif re.search(r"\bDEBUG\b", raw):
                detected = "DEBUG"
            if component != "all" and label != component:
                continue
            text = redact(raw)[:8000]
            if stamp < since or stamp > until or (keyword and keyword not in text.casefold()) or (level != "ALL" and detected != level):
                continue
            rows.append({"component": label, "level": detected, "time": datetime.fromtimestamp(stamp, timezone.utc).isoformat(timespec="seconds"), "text": text, "file": path.name, "line": index, "_offset": raw_offset})
    if selected:
        # A file cursor follows physical records, including traceback lines
        # without a timestamp or records appended out of timestamp order.
        rows.sort(key=lambda item: item["_offset"])
    else:
        rows.sort(key=lambda item: (item["time"], item["file"], item["line"]))
    visible = rows[-limit:]
    if selected and len(rows) > limit:
        # The 256 KB read window may contain many more than 1000 short lines.
        # Continue at the first visible record, not at the start of the window,
        # otherwise those unreturned records disappear from pagination.
        next_before = min(row["_offset"] for row in visible)
    for row in visible:
        row.pop("_offset", None)
    return {"entries": visible, "files": files, "components": list(COMPONENTS), "bounded": bounded or len(relevant) > 60 or len(rows) > limit, "limit": limit, "next_before": next_before}
