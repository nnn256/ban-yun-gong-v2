"""Resolve installed QQ system-face IDs to local image assets."""
from __future__ import annotations

import os
import re
from functools import cmp_to_key
from pathlib import Path
from typing import Iterable


_FACE_ID = re.compile(r"[0-9]+\Z")
_FACE_CACHE: dict[str, Path] = {}


def _natural_version_key(value: str) -> tuple:
    # Split digit runs so 9.9.10 sorts after 9.9.9.
    return tuple((1, int(part)) if part.isdigit() else (0, part.casefold())
                 for part in re.split(r"(\d+)", value) if part)


def _registry_install_roots() -> list[Path]:
    if os.name != "nt":
        return []
    try:
        import winreg
    except ImportError:
        return []

    roots = []
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        for key_name in (
            r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\QQ.exe",
            r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\App Paths\QQ.exe",
        ):
            try:
                with winreg.OpenKey(hive, key_name) as key:
                    executable, _ = winreg.QueryValueEx(key, None)
                path = Path(os.path.expandvars(str(executable)))
                if path.name.casefold() == "qq.exe":
                    roots.append(path.parent)
            except OSError:
                continue
    return roots


def _default_install_roots() -> list[Path]:
    roots = _registry_install_roots()
    roots.extend((
        Path("D:/Program Files/Tencent/QQNT"),
        Path("C:/Program Files/Tencent/QQNT"),
        Path("C:/Program Files (x86)/Tencent/QQNT"),
    ))
    seen = set()
    unique = []
    for root in roots:
        key = os.path.normcase(str(root))
        if key not in seen:
            seen.add(key)
            unique.append(root)
    return unique


def _version_directories(install_root: Path) -> list[Path]:
    versions = install_root / "versions"
    if versions.is_dir():
        return sorted(
            (entry for entry in versions.iterdir() if entry.is_dir()),
            key=lambda entry: _natural_version_key(entry.name),
            reverse=True,
        )
    return [install_root]


def _asset_path(install_root: Path, face_id: str) -> Path | None:
    for version in _version_directories(install_root):
        path = version / "resources" / "app" / "resource" / "default-emojis" / f"{face_id}.png"
        if path.is_file():
            return path
    return None


def find_face_image(face_id: int | str, *, install_roots: Iterable[str | Path] | None = None) -> Path | None:
    """Return the local QQ PNG for an exact numeric OneBot Face id.

    Missing IDs, non-Windows hosts without QQ assets, and unknown install
    layouts return ``None``; no URL or filename is fabricated.
    ``install_roots`` is an optional discovery override for tests/portable QQ.
    Cached paths are checked on every call so removed assets are never returned.
    """
    if isinstance(face_id, bool):
        return None
    value = str(face_id)
    if not _FACE_ID.fullmatch(value):
        return None
    cached = _FACE_CACHE.get(value)
    if cached is not None:
        if cached.is_file():
            return cached
        _FACE_CACHE.pop(value, None)

    roots = list(install_roots) if install_roots is not None else _default_install_roots()
    for root in roots:
        try:
            resolved = Path(root)
        except (TypeError, ValueError):
            continue
        found = _asset_path(resolved, value)
        if found is not None:
            _FACE_CACHE[value] = found
            return found
    return None
