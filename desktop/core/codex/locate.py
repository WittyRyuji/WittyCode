"""Find the Codex command line: settings, PATH, npm, the VS Code / Cursor extension, the Codex desktop app.

Order (the first one that answers ``--version`` wins): the path in the settings (and nothing else when one is set, so a
wrong path shows up as "missing" instead of silently running another Codex), ``codex`` on PATH, npm's global
``%APPDATA%\\npm\\codex.cmd``, the OpenAI extension of VS Code and its forks (``~/.vscode*/extensions/openai.chatgpt-*``
and the same under ``.cursor`` / ``.windsurf``; newest first), then the Codex desktop app: the copy it unpacks to
``%LOCALAPPDATA%\\OpenAI\\Codex\\bin\\<hash>\\codex.exe`` and the MSIX package's
``%ProgramFiles%\\WindowsApps\\OpenAI.Codex_*\\app\\resources\\codex.exe`` (seen on this machine: desktop app
26.1023.6123.0, ``codex-cli 0.165.0-alpha.1.2``; enumerating ``WindowsApps`` is usually denied, which is fine). The
extension's ``bin`` layout was not seen here (no extension installed): any ``bin/**/codex.exe`` counts.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from desktop.core.codex.rpc import CREATE_NO_WINDOW, launch_command

VERSION_SECONDS = 15.0
_VERSION = re.compile(r"\d+\.\d+\.\d+[0-9A-Za-z.+-]*")
EDITOR_FOLDERS = (".vscode", ".vscode-insiders", ".vscode-oss", ".cursor", ".windsurf")


@dataclass(frozen=True, slots=True)
class CodexBinary:
    path: Path
    """What was found: an executable or an npm shim (``codex.cmd``)."""
    version: str
    """From ``codex --version`` (``codex-cli 0.165.0`` -> ``0.165.0``)."""
    source: str = "path"
    """``settings`` | ``path`` | ``npm`` | ``editor`` | ``desktop``."""
    command: tuple[str, ...] = ()
    """The command line that runs it (``launch_command``: a shim becomes the native executable it would start)."""

    def argv(self, *arguments: str) -> list[str]:
        return [*(self.command or launch_command(self.path)), *arguments]


Runner = Callable[..., Any]


def _newest_first(paths: Iterable[Path]) -> list[Path]:
    def modified(path: Path) -> float:
        try:
            return path.stat().st_mtime
        except OSError:
            return 0.0

    return sorted(paths, key=modified, reverse=True)


def _glob(folder: Path, pattern: str) -> list[Path]:
    try:
        return [path for path in folder.glob(pattern) if path.is_file()]
    except OSError:
        return []


def codex_candidates(
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
    on_path = which("codex")
    if on_path:
        found.append((Path(on_path), "path"))
    roaming = Path(env.get("APPDATA", "").strip() or home / "AppData" / "Roaming")
    found.append((roaming / "npm" / "codex.cmd", "npm"))
    for folder in EDITOR_FOLDERS:
        extensions = home / folder / "extensions"
        try:
            installed = [entry for entry in extensions.iterdir() if entry.is_dir() and entry.name.lower().startswith("openai.chatgpt-")]
        except OSError:
            continue
        for extension in _newest_first(installed):
            executables = _glob(extension, "bin/**/codex.exe") + _glob(extension, "bin/**/codex")
            found.extend((path, "editor") for path in _newest_first(executables))
    local = Path(env.get("LOCALAPPDATA", "").strip() or home / "AppData" / "Local")
    found.extend((path, "desktop") for path in _newest_first(_glob(local / "OpenAI" / "Codex" / "bin", "*/codex.exe")))
    program_files = Path(env.get("ProgramFiles", "").strip() or env.get("PROGRAMFILES", "").strip() or r"C:\Program Files")
    packages = _glob(program_files / "WindowsApps", "OpenAI.Codex_*/app/resources/codex.exe")
    found.extend((path, "desktop") for path in _newest_first(packages))
    unique: list[tuple[Path, str]] = []
    seen: set[str] = set()
    for path, source in found:
        key = os.path.normcase(str(path))
        if key not in seen:
            seen.add(key)
            unique.append((path, source))
    return unique


def probe_version(argv: list[str], *, timeout: float = VERSION_SECONDS, run: Runner = subprocess.run) -> str | None:
    """``codex --version`` -> ``0.165.0``; ``None`` when it does not run or says nothing usable."""
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


def locate_codex(
    explicit: str | None = None,
    *,
    env: Mapping[str, str] | None = None,
    which: Callable[[str], str | None] = shutil.which,
    home: Path | None = None,
    run: Runner = subprocess.run,
    timeout: float = VERSION_SECONDS,
) -> CodexBinary | None:
    """The first Codex that runs (see the module docstring for the order), or ``None``."""
    for path, source in codex_candidates(explicit, env=env, which=which, home=home):
        if not path.is_file():
            continue
        command = launch_command(path)
        version = probe_version(command, timeout=timeout, run=run)
        if version is not None:
            return CodexBinary(path=path, version=version, source=source, command=tuple(command))
    return None
