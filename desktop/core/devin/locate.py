"""Find the Devin command line: settings, PATH, the Devin desktop app's own copy.

Order (the first one that answers ``--version`` wins): the path in the settings (and nothing else when one is set, so a
wrong path shows up as "missing" instead of silently running another Devin), ``devin`` on PATH, then the desktop app's
copy: ``%LOCALAPPDATA%\\Programs\\devin\\resources\\app\\extensions\\windsurf\\devin\\bin\\devin.exe`` (seen on this machine,
3000.11.238; ``devin acp`` is its Agent Client Protocol server, ``devin auth status`` its sign-in check).
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
from typing import Any

from desktop.core.jsonrpc import CREATE_NO_WINDOW

VERSION_SECONDS = 15.0
_VERSION = re.compile(r"\d+(?:\.\d+)+[0-9A-Za-z.+-]*")
DESKTOP_BIN = Path("Programs") / "devin" / "resources" / "app" / "extensions" / "windsurf" / "devin" / "bin"


@dataclass(frozen=True, slots=True)
class DevinBinary:
    path: Path
    version: str
    """From ``devin --version`` (``devin 3000.11.238 (9e593c73d4ca)`` -> ``3000.11.238``)."""
    source: str = "path"
    """``settings`` | ``path`` | ``desktop``."""
    command: tuple[str, ...] = ()
    """The command line that runs it (the executable itself, unless a test points at a script)."""

    def argv(self, *arguments: str) -> list[str]:
        return [*(self.command or (str(self.path),)), *arguments]


Runner = Callable[..., Any]


def devin_candidates(
    explicit: str | None = None,
    *,
    env: Mapping[str, str] | None = None,
    which: Callable[[str], str | None] = shutil.which,
    home: Path | None = None,
) -> list[tuple[Path, str]]:
    """``(path, source)`` in the order they are tried; only the explicit one when it is set."""
    env = os.environ if env is None else env
    home = Path.home() if home is None else home
    if explicit and explicit.strip():
        return [(Path(explicit.strip().strip('"')).expanduser(), "settings")]
    found: list[tuple[Path, str]] = []
    on_path = which("devin")
    if on_path:
        found.append((Path(on_path), "path"))
    local = Path(env.get("LOCALAPPDATA", "").strip() or home / "AppData" / "Local")
    found.append((local / DESKTOP_BIN / "devin.exe", "desktop"))
    unique: list[tuple[Path, str]] = []
    seen: set[str] = set()
    for path, source in found:
        key = os.path.normcase(str(path))
        if key not in seen:
            seen.add(key)
            unique.append((path, source))
    return unique


def probe_version(argv: list[str], *, timeout: float = VERSION_SECONDS, run: Runner = subprocess.run) -> str | None:
    """``devin --version`` -> ``3000.11.238``; ``None`` when it does not run or says nothing usable."""
    options: dict[str, Any] = {}
    if sys.platform == "win32":
        options["creationflags"] = CREATE_NO_WINDOW
    try:
        completed = run(
            [*argv, "--version"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
            stdin=subprocess.DEVNULL,
            **options,
        )
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    if completed.returncode != 0:
        return None
    output = (completed.stdout or "").strip()
    match = _VERSION.search(output)
    if match:
        return match.group(0)
    return output.splitlines()[0] if output else None


def locate_devin(
    explicit: str | None = None,
    *,
    env: Mapping[str, str] | None = None,
    which: Callable[[str], str | None] = shutil.which,
    home: Path | None = None,
    run: Runner = subprocess.run,
    timeout: float = VERSION_SECONDS,
) -> DevinBinary | None:
    """The first Devin that runs (see the module docstring for the order), or ``None``."""
    for path, source in devin_candidates(explicit, env=env, which=which, home=home):
        if not path.is_file():
            continue
        version = probe_version([str(path)], timeout=timeout, run=run)
        if version is not None:
            return DevinBinary(path=path, version=version, source=source, command=(str(path),))
    return None
