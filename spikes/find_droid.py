"""Locate the droid.exe bundled with the Factory desktop app.

Resolution order:
1. ``WITTYCODE_DROID_EXECUTABLE`` environment variable (must point to an existing file).
2. Newest ``%LOCALAPPDATA%\\Factory\\app-<version>\\resources\\bin\\droid.exe``.
   The ``app-<version>`` folder changes on every Factory desktop update, so
   versions are compared numerically (``app-0.210.0`` > ``app-0.99.0``).
3. ``droid`` on PATH.

Run standalone to print the resolved path and ``droid --version``.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

ENV_OVERRIDE = "WITTYCODE_DROID_EXECUTABLE"

_APP_DIR = re.compile(r"^app-(\d+(?:\.\d+)*)(?:-([0-9A-Za-z.-]+))?$")


def _version_key(folder_name: str) -> tuple[tuple[int, ...], int, str] | None:
    match = _APP_DIR.match(folder_name)
    if match is None:
        return None
    numbers = tuple(int(part) for part in match.group(1).split("."))
    prerelease = match.group(2)
    # A release sorts after any pre-release of the same version (semver rule).
    return numbers, 0 if prerelease else 1, prerelease or ""


def factory_app_root() -> Path:
    local = os.environ.get("LOCALAPPDATA")
    base = Path(local) if local else Path.home() / "AppData" / "Local"
    return base / "Factory"


def bundled_candidates(root: Path | None = None) -> list[tuple[tuple[tuple[int, ...], int, str], Path]]:
    """Return (version_key, droid.exe) pairs for every installed app folder, newest first."""
    root = factory_app_root() if root is None else root
    found: list[tuple[tuple[tuple[int, ...], int, str], Path]] = []
    try:
        entries = list(root.iterdir())
    except OSError:
        return []
    for entry in entries:
        key = _version_key(entry.name)
        if key is None or not entry.is_dir():
            continue
        exe = entry / "resources" / "bin" / "droid.exe"
        if exe.is_file():
            found.append((key, exe))
    found.sort(key=lambda item: item[0], reverse=True)
    return found


def find_droid() -> Path:
    override = os.environ.get(ENV_OVERRIDE, "").strip()
    if override:
        path = Path(override).expanduser()
        if not path.is_file():
            raise FileNotFoundError(f"{ENV_OVERRIDE} points to a missing file: {path}")
        return path.resolve()
    candidates = bundled_candidates()
    if candidates:
        return candidates[0][1]
    on_path = shutil.which("droid")
    if on_path:
        return Path(on_path).resolve()
    raise FileNotFoundError(
        "droid executable not found: set WITTYCODE_DROID_EXECUTABLE, install the Factory "
        f"desktop app (looked in {factory_app_root()}), or put droid on PATH"
    )


def droid_version(executable: Path, timeout: float = 30.0) -> str:
    completed = subprocess.run(
        [str(executable), "--version"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"{executable} --version exited with {completed.returncode}: {completed.stderr.strip()}"
        )
    return completed.stdout.strip()


def main() -> int:
    candidates = bundled_candidates()
    for key, exe in candidates:
        print(f"candidate: {'.'.join(map(str, key[0]))}{'-' + key[2] if key[2] else ''} -> {exe}")
    try:
        exe = find_droid()
    except FileNotFoundError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    source = "env override" if os.environ.get(ENV_OVERRIDE, "").strip() else (
        "bundled" if candidates and exe == candidates[0][1] else "PATH"
    )
    print(f"selected ({source}): {exe}")
    print(f"droid --version: {droid_version(exe)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
