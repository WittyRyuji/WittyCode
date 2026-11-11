"""Factory (Droid): its personal instruction files as notes, its session files as conversations.

Where things are (``~/.factory``; from docs.factory.ai (AGENTS.md, "Discovery and precedence") and the files of a droid 0.25x
install):

- Personal instructions: ``AGENTS.md`` (or ``agents.md`` / ``Agents.md`` / ``CLAUDE.md`` / ``Claude.md``) and the design
  guidelines ``DESIGN.md`` in ``~/.factory``, and the same names in ``~/.agents`` and ``~/.agent``, which Droid also reads.
  Droid has no memory of its own beyond these. Custom droids, commands and skills are not notes and are left out.
- ``sessions/<encoded folder>/<session id>.jsonl`` (and a ``.settings.json`` beside it): the first line is
  ``{"type": "session_start", "id", "title", "cwd", "callingSessionId"?, …}``, then ``{"type": "message", "id",
  "timestamp", "message": {"role", "content": [blocks]}, "visibility"?}`` lines (blocks ``text``, ``tool_use``,
  ``tool_result``, ``thinking``, ``image``, ``document``) and bookkeeping (``todo_state``, ``compaction_state``,
  ``agent_turn_outcome``). Messages the app does not show are left out the way the app leaves them out
  (``transcript.is_hidden_message``, and droid's ``<system-reminder>`` blocks); a session another one started
  (``callingSessionId``: a sub-agent's) is not listed; ``*.bak`` copies are not sessions.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from desktop.core.attachments import is_system_reminder
from desktop.core.context_envelope import strip as strip_context
from desktop.core.importers.base import (
    LABELS,
    Builder,
    ImportFailed,
    MemoryCandidate,
    Message,
    Report,
    SessionInfo,
    as_text,
    first_words,
    lines,
    mtime,
    norm_key,
    parse_time,
    read_text,
    records,
    session_key,
    size_of,
    subject_of,
    tail_lines,
)
from desktop.core.importers.split import split_markdown, tidy
from desktop.core.transcript import is_hidden_message

FULL_SCAN = 8 * 1024 * 1024
HEAD = 1024 * 1024
TAIL = 1024 * 1024
INSTRUCTION_NAMES = ("AGENTS.md", "agents.md", "Agents.md", "CLAUDE.md", "Claude.md", "DESIGN.md", "Design.md", "design.md")


def _typed(content: Any) -> str:
    if isinstance(content, str):
        return strip_context(content)
    if not isinstance(content, list):
        return ""
    texts = [as_text(block.get("text")) for block in content if isinstance(block, dict) and block.get("type") == "text"]
    return strip_context("".join(text for text in texts if not is_system_reminder(text)))


class _Reading:
    def __init__(self, report: Report) -> None:
        self.builder = Builder(report)
        self.native = ""
        self.title = ""
        self.cwd = ""
        self.child = False
        self.first = 0.0
        self.last = 0.0

    def feed(self, record: dict[str, Any]) -> None:
        kind = record.get("type")
        if kind == "session_start":
            self.native = self.native or as_text(record.get("id"))
            self.title = as_text(record.get("title")).strip() or self.title
            self.cwd = self.cwd or as_text(record.get("cwd")).strip()
            self.child = self.child or bool(record.get("callingSessionId"))
            return
        if kind != "message":
            return
        at = parse_time(record.get("timestamp"))
        if at:
            self.first = min(self.first, at) if self.first else at
            self.last = max(self.last, at)
        message = record.get("message") if isinstance(record.get("message"), dict) else record
        message_id = as_text(record.get("id")) or as_text(message.get("id"))
        if is_hidden_message(message_id or None, as_text(record.get("visibility")) or None):
            return
        role = message.get("role")
        content = message.get("content")
        if role == "user":
            self.builder.user(_typed(content), at)
        elif role == "assistant":
            key = message_id or id(record)
            if isinstance(content, str):
                self.builder.assistant(content, at, key)
                return
            for block in content if isinstance(content, list) else ():
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text":
                    self.builder.assistant(as_text(block.get("text")), at, key)
                elif block.get("type") == "tool_use":
                    self.builder.tool(as_text(block.get("name")), subject_of(block.get("input")), at, key)


class FactorySource:
    app = "factory"
    label = LABELS["factory"]

    def __init__(self, root: str | Path, *, home: str | Path | None = None) -> None:
        self.root = Path(root)
        self.home = Path(home) if home is not None else self.root.parent
        self.report = Report()

    def available(self) -> bool:
        return self.root.is_dir()

    def memories(self) -> list[MemoryCandidate]:
        self.report = Report()
        if not self.available():
            return []
        done: set[str] = set()
        out: list[MemoryCandidate] = []
        for folder in (self.root, self.home / ".agents", self.home / ".agent"):
            for name in INSTRUCTION_NAMES:
                path = folder / name
                try:
                    if not path.is_file():
                        continue
                    # (the names differ only in case: on Windows they are one file)
                    key = norm_key(path.resolve())
                except OSError:
                    continue
                if key in done:
                    continue
                done.add(key)
                text = read_text(path, self.report)
                if text is not None:
                    tags = ("design",) if name.lower() == "design.md" else ("AGENTS.md",)
                    out.extend(split_markdown(text, app=self.app, origin_file=str(path), scope="global", base_tags=tags, report=self.report))
        return tidy(out)

    def _files(self) -> list[Path]:
        folder = self.root / "sessions"
        try:
            found = list(folder.glob("*/*.jsonl")) + list(folder.glob("*.jsonl"))
        except OSError:
            return []
        return [path for path in found if path.is_file()]

    def sessions(self) -> list[SessionInfo]:
        """Every session with something to import (not a sub-agent's), newest first. A file bigger than ``FULL_SCAN`` is
        read at its start and end only (its count is then a lower bound)."""
        self.report = Report()
        out: list[SessionInfo] = []
        for path in self._files():
            info = self._info(path)
            if info is None:
                self.report.skipped += 1
            else:
                out.append(info)
        out.sort(key=lambda info: info.updated, reverse=True)
        return out

    def _info(self, path: Path) -> SessionInfo | None:
        size = size_of(path)
        exact = size <= FULL_SCAN
        reading = _Reading(self.report)
        try:
            for record in records(lines(path, limit=None if exact else HEAD), self.report):
                reading.feed(record)
            if not exact:
                for record in records(tail_lines(path, TAIL), self.report):
                    reading.feed(record)
        except OSError:
            return None
        if reading.child or not reading.builder.messages:
            return None
        native = reading.native or path.stem
        first = next((m for m in reading.builder.messages if m.role == "user"), None)
        return SessionInfo(
            key=session_key(self.app, native),
            app=self.app,
            native_id=native,
            title=first_words(reading.title) or (first_words(first.text) if first is not None else "") or "Droid 会话",
            project=reading.cwd,
            started=reading.first or mtime(path),
            updated=max(reading.last, mtime(path)) if reading.last else mtime(path),
            messages=len(reading.builder.messages),
            size=size,
            path=str(path),
            exact=exact,
        )

    def read(self, info: SessionInfo) -> list[Message]:
        self.report = Report()
        path = Path(info.path)
        if not path.is_file():
            raise ImportFailed("找不到这个 Droid 会话的文件了（可能已被删除）。")
        reading = _Reading(self.report)
        try:
            for record in records(lines(path), self.report):
                reading.feed(record)
        except OSError as error:
            raise ImportFailed(f"读不了这个 Droid 会话的文件：{error.strerror or error}") from None
        return reading.builder.messages
