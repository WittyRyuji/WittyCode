"""Locate the droid executable.

Resolution order: explicit override from the settings file, then the
``WITTYCODE_DROID_EXECUTABLE`` environment variable, then the newest
``%LOCALAPPDATA%\\Factory\\app-<version>\\resources\\bin\\droid.exe`` bundled with the
Factory desktop app, then ``droid`` on PATH. The app folder changes on every
desktop update, so callers should resolve again before each process spawn.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

ENV_OVERRIDE = "WITTYCODE_DROID_EXECUTABLE"

_APP_DIR = re.compile(r"^app-(\d+(?:\.\d+)*)(?:-([0-9A-Za-z.-]+))?$")

PrereleaseKey = tuple[tuple[int, int, str], ...]
VersionKey = tuple[tuple[int, ...], int, PrereleaseKey]
Source = Literal["settings", "env", "bundled", "path"]


class DroidNotFoundError(FileNotFoundError):
    """No usable droid executable could be found."""


@dataclass(frozen=True, slots=True)
class DroidLocation:
    path: Path
    source: Source
    app_version: str | None = None


def _prerelease_key(text: str) -> PrereleaseKey:
    parts: list[tuple[int, int, str]] = []
    for part in text.split("."):
        parts.append((0, int(part), "") if part.isdigit() else (1, 0, part))
    return tuple(parts)


def version_key(folder_name: str) -> VersionKey | None:
    """Sort key for an ``app-<version>`` folder name, or None if the name does not match."""
    match = _APP_DIR.match(folder_name)
    if match is None:
        return None
    numbers = tuple(int(part) for part in match.group(1).split("."))
    numbers = numbers + (0,) * (3 - len(numbers))
    prerelease = match.group(2)
    if prerelease:
        return numbers, 0, _prerelease_key(prerelease)
    # A release sorts after any pre-release of the same version (semver rule).
    return numbers, 1, ()


def factory_app_root(env: Mapping[str, str] | None = None) -> Path:
    env = os.environ if env is None else env
    local = env.get("LOCALAPPDATA", "").strip()
    base = Path(local) if local else Path.home() / "AppData" / "Local"
    return base / "Factory"


def bundled_candidates(root: Path | None = None) -> list[tuple[VersionKey, Path, str]]:
    """``(version key, droid.exe, folder version)`` for every installed app folder, newest first."""
    root = factory_app_root() if root is None else root
    found: list[tuple[VersionKey, Path, str]] = []
    try:
        entries = list(root.iterdir())
    except OSError:
        return []
    for entry in entries:
        key = version_key(entry.name)
        if key is None or not entry.is_dir():
            continue
        exe = entry / "resources" / "bin" / "droid.exe"
        if exe.is_file():
            found.append((key, exe, entry.name.removeprefix("app-")))
    found.sort(key=lambda item: item[0], reverse=True)
    return found


def locate_droid(
    override: str | None = None,
    *,
    env: Mapping[str, str] | None = None,
    app_root: Path | None = None,
    which: Callable[[str], str | None] = shutil.which,
) -> DroidLocation:
    env = os.environ if env is None else env
    if override and override.strip():
        path = Path(override.strip()).expanduser()
        if not path.is_file():
            raise DroidNotFoundError(f"设置中指定的 droid 路径不存在：{path}")
        return DroidLocation(path.resolve(), "settings")
    from_env = env.get(ENV_OVERRIDE, "").strip()
    if from_env:
        path = Path(from_env).expanduser()
        if not path.is_file():
            raise DroidNotFoundError(f"环境变量 {ENV_OVERRIDE} 指向的文件不存在：{path}")
        return DroidLocation(path.resolve(), "env")
    candidates = bundled_candidates(factory_app_root(env) if app_root is None else app_root)
    if candidates:
        _, exe, version = candidates[0]
        return DroidLocation(exe, "bundled", version)
    on_path = which("droid")
    if on_path:
        return DroidLocation(Path(on_path).resolve(), "path")
    raise DroidNotFoundError(
        "找不到 droid：请安装 Factory 桌面版，或在设置中指定 droid.exe 的路径，"
        f"或设置环境变量 {ENV_OVERRIDE}。"
    )


def droid_version(executable: Path, timeout: float = 30.0) -> str:
    """Run ``droid --version`` (spawns a short-lived process)."""
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
    completed = subprocess.run(
        [str(executable), "--version"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
        creationflags=flags,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"{executable} --version exited with {completed.returncode}")
    return completed.stdout.strip()