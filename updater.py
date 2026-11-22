"""Local, manifest-verified application updates. User data is never a payload target."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import tempfile
import time
import uuid
import zipfile

APP_ID = "information-mover-v2"
MANIFEST_NAME = "release-manifest.json"
ROOT_FILES = {
    ".gitignore", "README.md", "VERIFICATION.txt", "PROTOCOL_REVIEW.md", "launch-workbench.cmd",
    "一键启动.cmd", "更新并启动.cmd", "build-portable.ps1", "portable_launcher.cs",
    "后台运行.vbs", "打开工作台.vbs",
    "install.ps1", "install_astrbot.ps1", "install_larkagentx.ps1", "install_snowluma.ps1",
    "start_snowluma.ps1", "start-workbench.ps1", "stop-workbench.ps1", "verify.ps1",
    "package.ps1", "workbench_server.py", "workbench.settings.example.json",
    "搬运工V2.exe", "mover-v2-launcher.exe", "updater.py", "update-and-start.ps1",
    "background_runtime.py", "background_supervisor.py", "background-watchdog.ps1", "runtime_logs.py", "workbench_logs.py", "log_store.py", "astrbot_runner.py",
}
DIRECTORIES = {
    "astrbot_plugin_message_bridge": {".py", ".yaml", ".json"},
    "tests": {".py", ".cjs"}, "workbench": {".js", ".css", ".html"},
    "vendor": {".zip", ".json"}, "installer": {".ps1", ".wxs", ".wxl", ".md"},
}
TOOLS = {"tools/uv.exe", "tools/LICENSE-APACHE.txt", "tools/LICENSE-MIT.txt"}
FORBIDDEN_NAMES = {"credentials.json", "bridge_rules.json", "cmd_config.json", "onebot.json",
                   "workbench.settings.json", "token.json", ".env"}
REQUIRED = {"start-workbench.ps1", "workbench_server.py", "astrbot_plugin_message_bridge/metadata.yaml"}
MAX_TOTAL = 512 * 1024 * 1024


class UpdateError(ValueError):
    pass


def safe_payload_name(name: str) -> str:
    if not isinstance(name, str) or not name or "\\" in name or ":" in name:
        raise UpdateError("Invalid package path")
    path = PurePosixPath(name)
    if path.is_absolute() or str(path) != name or any(p in {".", ".."} for p in path.parts):
        raise UpdateError("Package path escapes its application directory")
    for part in path.parts:
        if part.endswith((".", " ")) or re.fullmatch(r"(?i)(con|prn|aux|nul|com[1-9]|lpt[1-9])(\..*)?", part):
            raise UpdateError("Invalid Windows package path")
        if part.casefold() in FORBIDDEN_NAMES or part.casefold() == "__pycache__":
            raise UpdateError("Package attempted to include user data")
    if path.suffix.casefold() in {".db", ".sqlite", ".sqlite3", ".log", ".jsonl", ".session", ".dpapi", ".pyc"}:
        raise UpdateError("Package attempted to include runtime storage")
    if name in ROOT_FILES or name in TOOLS:
        return name
    if name == "tests/fixtures/larkx_receive_contract.json":
        return name
    if len(path.parts) >= 2 and path.parts[0] in DIRECTORIES and path.suffix in DIRECTORIES[path.parts[0]]:
        return name
    raise UpdateError(f"File is not an application update target: {name}")


def _reject_links(path: Path) -> None:
    # Check every ancestor: resolving first would hide junctions and symlinks.
    for item in [path, *path.parents]:
        if item.is_symlink() or (hasattr(item, "is_junction") and item.is_junction()):
            raise UpdateError("Application updates cannot traverse links or junctions")
        try:
            attrs = item.lstat().st_file_attributes if os.name == "nt" else 0
        except FileNotFoundError:
            continue
        if attrs & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 1024):
            raise UpdateError("Application updates cannot traverse reparse points")


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def build_manifest(root: Path, version: str, files: list[str] | None = None) -> dict:
    root = Path(root).absolute()
    if files is None:
        files = []
        for name in ROOT_FILES | TOOLS:
            if (root / name).is_file():
                files.append(name)
        for directory in DIRECTORIES:
            base = root / directory
            if base.is_dir():
                for p in base.rglob("*"):
                    name = p.relative_to(root).as_posix()
                    if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc":
                        files.append(name)
    entries = {}
    for name in sorted(files):
        safe_payload_name(name)
        path = root / name
        _reject_links(path)
        data = path.read_bytes()
        entries[name] = {"size": len(data), "sha256": sha256(data)}
    manifest = {"schema": 1, "app_id": APP_ID, "version": str(version), "files": entries}
    _validate_manifest(manifest)
    return manifest


def seal_zip(package: Path, version: str) -> dict:
    """Add a manifest for the exact compiled binaries and sources in a release ZIP."""
    package = Path(package).absolute()
    _reject_links(package)
    entries = {}
    payload = {}
    with zipfile.ZipFile(package) as archive:
        seen = set()
        total = 0
        for info in archive.infolist():
            name = info.filename
            if name.casefold() in seen or info.is_dir() or stat.S_ISLNK(info.external_attr >> 16):
                raise UpdateError("Invalid or duplicate release ZIP entry")
            seen.add(name.casefold())
            if name == MANIFEST_NAME:
                continue
            safe_payload_name(name)
            total += info.file_size
            if info.file_size > 128 * 1024 * 1024 or total > MAX_TOTAL:
                raise UpdateError("Release ZIP exceeds the update size limit")
            data = archive.read(info)
            entries[name] = {"size": len(data), "sha256": sha256(data)}
            payload[name] = data
    manifest = _validate_manifest({"schema": 1, "app_id": APP_ID, "version": version, "files": entries})
    descriptor, temporary = tempfile.mkstemp(prefix=".mover-seal-", suffix=".zip", dir=package.parent)
    os.close(descriptor)
    try:
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for name, data in payload.items():
                archive.writestr(name, data)
            archive.writestr(MANIFEST_NAME, json.dumps(manifest, ensure_ascii=False, indent=2).encode())
        read_release(Path(temporary))
        os.replace(temporary, package)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return {"sealed": True, "version": version, "files": len(entries)}


def _validate_manifest(manifest: dict) -> dict:
    if not isinstance(manifest, dict) or manifest.get("schema") != 1 or manifest.get("app_id") != APP_ID:
        raise UpdateError("This package is not an Information Mover V2 release")
    if not re.fullmatch(r"\d+\.\d+\.\d+(?:[-+][a-zA-Z0-9.]+)?", str(manifest.get("version", ""))):
        raise UpdateError("Invalid release version")
    files = manifest.get("files")
    if not isinstance(files, dict) or not REQUIRED.issubset(files) or len(files) > 5000:
        raise UpdateError("Release manifest is incomplete")
    seen = set()
    total = 0
    for name, spec in files.items():
        safe_payload_name(name)
        if name.casefold() in seen:
            raise UpdateError("Duplicate package path")
        seen.add(name.casefold())
        if not isinstance(spec, dict) or type(spec.get("size")) is not int or not 0 <= spec["size"] <= 128 * 1024 * 1024:
            raise UpdateError("Invalid package file size")
        if not re.fullmatch(r"[a-fA-F0-9]{64}", str(spec.get("sha256", ""))):
            raise UpdateError("Invalid package checksum")
        total += spec["size"]
    if total > MAX_TOTAL:
        raise UpdateError("Package exceeds the update size limit")
    return manifest


def read_release(source: Path) -> tuple[dict, dict[str, bytes]]:
    """Read and verify the entire package before stopping or changing the installation."""
    source = Path(source).absolute()
    _reject_links(source)
    payload = {}
    if source.is_dir():
        manifest_path = source / MANIFEST_NAME
        _reject_links(manifest_path)
        if manifest_path.stat().st_size > 2 * 1024 * 1024:
            raise UpdateError("Manifest is too large")
        manifest = _validate_manifest(json.loads(manifest_path.read_text(encoding="utf-8-sig")))
        for name in manifest["files"]:
            path = source / name
            _reject_links(path)
            if path.stat().st_size != manifest["files"][name]["size"]:
                raise UpdateError(f"Package size mismatch: {name}")
            payload[name] = path.read_bytes()
    else:
        with zipfile.ZipFile(source) as archive:
            names = set()
            entries = {}
            for info in archive.infolist():
                name = info.filename
                if name.casefold() in names:
                    raise UpdateError("ZIP has duplicate entries")
                names.add(name.casefold())
                unix_mode = info.external_attr >> 16
                if stat.S_ISLNK(unix_mode):
                    raise UpdateError("ZIP contains a symbolic link")
                if info.is_dir():
                    # There is no reason for a package to include empty data directories.
                    safe_payload_name(name.rstrip("/") + "/__invalid__")
                elif name == MANIFEST_NAME:
                    if info.file_size > 2 * 1024 * 1024:
                        raise UpdateError("Manifest is too large")
                    entries[name] = info
                else:
                    safe_payload_name(name)
                    entries[name] = info
            if MANIFEST_NAME not in entries:
                raise UpdateError("ZIP has no release manifest; use the new release package")
            manifest = _validate_manifest(json.loads(archive.read(entries.pop(MANIFEST_NAME))))
            if set(entries) != set(manifest["files"]):
                raise UpdateError("ZIP contents do not match its release manifest")
            for name, info in entries.items():
                if info.file_size != manifest["files"][name]["size"]:
                    raise UpdateError(f"Package size mismatch: {name}")
                payload[name] = archive.read(info)
    for name, data in payload.items():
        if sha256(data) != manifest["files"][name]["sha256"].lower():
            raise UpdateError(f"Package checksum mismatch: {name}")
    return manifest, payload


def validate_target(target: Path) -> Path:
    target = Path(target).absolute()
    _reject_links(target)
    if not target.is_dir() or not all((target / f).is_file() for f in REQUIRED):
        raise UpdateError("Target is not an existing Information Mover V2 installation")
    metadata = (target / "astrbot_plugin_message_bridge/metadata.yaml").read_text(encoding="utf-8-sig")
    if "name: astrbot_plugin_message_bridge" not in metadata:
        raise UpdateError("Target plugin identity does not match")
    return target


def _atomic_write(path: Path, data: bytes) -> None:
    _reject_links(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".mover-update-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def apply_release(source: Path, target: Path, *, _before_replace=None) -> dict:
    """Replace only whitelisted application files; restore them on any failure."""
    manifest, payload = read_release(source)
    target = validate_target(target)
    _reject_links(target / MANIFEST_NAME)
    for name in payload:
        _reject_links(target / name)
    _reject_links(target / "runtime/update-backups")
    backup = target / "runtime/update-backups" / (time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8])
    backup.mkdir(parents=True)
    changed = []
    added = []
    journal = {"version": manifest["version"], "state": "applying", "replaced": [], "added": []}
    try:
        for name, data in payload.items():
            destination = target / name
            if destination.is_file() and destination.read_bytes() == data:
                continue
            if destination.exists():
                if not destination.is_file():
                    raise UpdateError(f"Update target is not a regular file: {name}")
                saved = backup / "files" / name
                saved.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(destination, saved)
                journal["replaced"].append(name)
            else:
                journal["added"].append(name)
            _atomic_write(backup / "journal.json", json.dumps(journal, ensure_ascii=False, indent=2).encode())
            if _before_replace:
                _before_replace(name)
            _atomic_write(destination, data)
            changed.append(name)
            if name in journal["added"]:
                added.append(name)
        # Manifest follows successful payload writes and is included in rollback.
        previous_manifest = (target / MANIFEST_NAME).read_bytes() if (target / MANIFEST_NAME).is_file() else None
        try:
            _atomic_write(target / MANIFEST_NAME, json.dumps(manifest, ensure_ascii=False, indent=2).encode())
            journal["state"] = "complete"
            _atomic_write(backup / "journal.json", json.dumps(journal, ensure_ascii=False, indent=2).encode())
        except BaseException:
            if previous_manifest is None:
                (target / MANIFEST_NAME).unlink(missing_ok=True)
            else:
                _atomic_write(target / MANIFEST_NAME, previous_manifest)
            raise
    except BaseException as failure:
        errors = []
        for name in reversed(changed):
            try:
                if name in added:
                    (target / name).unlink(missing_ok=True)
                else:
                    _atomic_write(target / name, (backup / "files" / name).read_bytes())
            except OSError as exc:
                errors.append(f"{name}: {type(exc).__name__}")
        journal["state"] = "rollback_failed" if errors else "rolled_back"
        journal["rollback_errors"] = errors
        _atomic_write(backup / "journal.json", json.dumps(journal, ensure_ascii=False, indent=2).encode())
        if errors:
            raise UpdateError(f"Update failed; restore from {backup}. Rollback errors: {errors}") from failure
        raise
    return {"updated": True, "version": manifest["version"], "changed_files": len(changed), "backup": str(backup), "target": str(target)}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    build = sub.add_parser("manifest")
    build.add_argument("root", type=Path)
    build.add_argument("--version", required=True)
    verify = sub.add_parser("verify")
    verify.add_argument("source", type=Path)
    seal = sub.add_parser("seal")
    seal.add_argument("package", type=Path)
    seal.add_argument("--version", required=True)
    apply = sub.add_parser("apply")
    apply.add_argument("source", type=Path)
    apply.add_argument("target", type=Path)
    args = parser.parse_args()
    if args.action == "manifest":
        manifest = build_manifest(args.root, args.version)
        _atomic_write(args.root / MANIFEST_NAME, json.dumps(manifest, ensure_ascii=False, indent=2).encode())
        result = {"manifest": str(args.root / MANIFEST_NAME), "files": len(manifest["files"]), "version": manifest["version"]}
    elif args.action == "seal":
        result = seal_zip(args.package, args.version)
    elif args.action == "verify":
        manifest, _ = read_release(args.source)
        result = {"verified": True, "version": manifest["version"], "files": len(manifest["files"])}
    else:
        result = apply_release(args.source, args.target)
    print(json.dumps(result, ensure_ascii=True))


if __name__ == "__main__":
    main()
