"""Importing what other coding agents keep: Claude Code, Codex, Devin and Factory (Droid).

``sources()`` gives one ``Source`` per app; each reads that app's own files, read-only, and gives

- ``memories()``: its memory and instruction files split into candidate notes (``split.split_markdown``) for the shared
  memory, which the user picks from;
- ``sessions()``: its conversations, newest first, cheap to list; ``read(info)``: one conversation's messages (the user's,
  the assistant's, and a line per tool call; no tool output, no thinking, no pictures), which ``to_saved_session`` turns
  into a ChatGPT Plan conversation.

Nothing raises on a line or file in a shape it does not know; what was left out is counted in ``source.report`` (reset by
each call: a source is not for two threads at once). ``read`` raises ``ImportFailed`` when a conversation cannot be read at
all. Lines that look like a secret are dropped from notes and messages alike.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

from desktop.core.importers.base import (
    APPS,
    LABELS,
    ImportFailed,
    MemoryCandidate,
    Message,
    Report,
    SessionInfo,
    Source,
    saved_id,
    session_key,
    text_digest,
)
from desktop.core.importers.claude import ClaudeSource
from desktop.core.importers.codex import CodexSource
from desktop.core.importers.convert import to_saved_session
from desktop.core.importers.devin import DevinSource
from desktop.core.importers.factory import FactorySource
from desktop.core.importers.split import split_markdown, tidy

__all__ = [
    "APPS", "LABELS", "ClaudeSource", "CodexSource", "DevinSource", "FactorySource", "ImportFailed", "MemoryCandidate",
    "Message", "Report", "SessionInfo", "Source", "saved_id", "session_key", "sources", "split_markdown", "text_digest",
    "tidy", "to_saved_session",
]


def sources(
    *,
    home: str | os.PathLike[str] | None = None,
    appdata: str | os.PathLike[str] | None = None,
    codex_home: str | os.PathLike[str] | None = None,
    env: Mapping[str, str] | None = None,
) -> list[Source]:
    """Claude Code, Codex, Devin and Factory, in that order. Every place can be given; the rest come from ``env`` (the
    process's environment by default): ``CLAUDE_CONFIG_DIR``, ``CODEX_HOME``, ``APPDATA``, ``USERPROFILE`` / ``HOME``."""
    env = os.environ if env is None else env
    if home is None:
        home = env.get("USERPROFILE") or env.get("HOME") or str(Path.home())
    home_path = Path(home)
    if appdata is None:
        appdata = env.get("APPDATA") or (str(home_path / "AppData" / "Roaming") if os.name == "nt" else str(home_path / ".config"))
    devin = Path(appdata) / "devin"
    codex = Path(codex_home) if codex_home is not None else Path(env.get("CODEX_HOME", "").strip() or home_path / ".codex")
    claude = Path(env.get("CLAUDE_CONFIG_DIR", "").strip() or home_path / ".claude")
    return [
        ClaudeSource(claude, home=home_path),
        CodexSource(codex),
        DevinSource(devin, home=home_path),
        FactorySource(home_path / ".factory", home=home_path),
    ]
