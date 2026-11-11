"""Claude Code: its instruction files and auto memory as notes, its transcripts as conversations.

Where things are (``CLAUDE_CONFIG_DIR``, else ``~/.claude``; from code.claude.com/docs/en/memory, /sessions and
/claude-directory, and the files of a Claude Code 2.1 install):

- ``CLAUDE.md`` and ``rules/**/*.md``: the user's own instructions for every project (global notes).
- ``projects/<dir>/<session id>.jsonl``: a transcript. ``<dir>`` is the working folder with every character that is not a
  letter or a digit made ``-``, which cannot be turned back into the folder (``C--Users-me`` could be ``C:\\Users\\me`` or
  ``C:\\Users-me``), so the projects are the ``cwd`` the transcripts record. ``<session>.orphaned-….jsonl`` and
  ``.jsonl.superseded-…`` are copies set aside, ``<session>/subagents/`` the sub-agents' transcripts: left out.
- ``projects/<dir>/memory/``: auto memory, ``MEMORY.md`` (an index, a line per note) and one topic file per note with
  front matter ``name`` / ``description`` / ``type`` (``user`` | ``feedback`` | ``project`` | ``reference``). Its ``<dir>`` is
  the git repository's root folder, encoded the same way.
- In each project: ``CLAUDE.md``, ``.claude/CLAUDE.md``, ``CLAUDE.local.md``, ``.claude/rules/**/*.md``.

A transcript line is ``{"type": "user" | "assistant" | "summary" | "custom-title" | "ai-title" | "system" | "attachment" |
…, "message": {"role", "content": str | [blocks]}, "cwd", "timestamp", "isMeta", "isSidechain", …}`` with blocks ``text``,
``tool_use {name, input}``, ``tool_result``, ``thinking``, ``image``; a streamed assistant message is several lines with the
same ``message.id``. The title is the user's ``custom-title`` (``/rename``), else the newest ``ai-title``, else a ``summary``
line, else the first thing the user said. The format is internal to Claude Code and changes between versions.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Iterable
from pathlib import Path
from typing import Any

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
from desktop.core.importers.split import front_matter, split_markdown, tidy

FULL_SCAN = 8 * 1024 * 1024
"""A transcript up to this size is read whole for the list (its count is exact); a bigger one at its start and end."""
HEAD = 1024 * 1024
TAIL = 1024 * 1024
CWD_LINES = 200
"""Lines read from the start of a transcript to find the folder it ran in."""
GIT_LEVELS = 8

MEMORY_TYPES = {"user": "preference", "feedback": "convention", "project": "fact", "reference": "fact"}
"""Auto memory's ``type`` as the shared memory's kind."""
CONFIG_LIMIT = 32 * 1024 * 1024

_WRAPPER = re.compile(r"^\s*<(local-command-[a-z]+|bash-(?:input|stdout|stderr)|system-reminder|user-prompt-submit-hook|task-notification)\b")
_REMINDER = re.compile(r"<system-reminder>.*?</system-reminder>", re.DOTALL)
_COMMAND = re.compile(r"<command-name>(.*?)</command-name>", re.DOTALL)
_ARGS = re.compile(r"<command-args>(.*?)</command-args>", re.DOTALL)
_INTERRUPTED = re.compile(r"^\[Request interrupted by user[^\]]*\]$")
_LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")


def encode(folder: str) -> str:
    """The name Claude Code gives a folder under ``projects``."""
    return re.sub(r"[^A-Za-z0-9]", "-", folder)


def _same_name(name: str, folder: str) -> bool:
    encoded = encode(folder)
    if len(encoded) > 200:
        # (a long name is cut to 200 characters and a hash of the path added; the hash is not reproducible here)
        return name[:200].lower() == encoded[:200].lower()
    return name.lower() == encoded.lower()


def git_root(folder: str, stop: Path | None = None) -> str:
    """The folder of the git repository ``folder`` is in (``""``: none found within ``GIT_LEVELS`` levels up, or above
    ``stop``)."""
    path = Path(folder)
    for _ in range(GIT_LEVELS):
        try:
            if (path / ".git").exists():
                return str(path)
        except OSError:
            return ""
        if stop is not None and norm_key(path) == norm_key(stop):
            return ""
        if path.parent == path:
            return ""
        path = path.parent
    return ""


def _user_text(content: Any) -> str:
    """What the user typed: text blocks without the harness's own wrappers; a slash command as ``/name args``."""
    if isinstance(content, str):
        texts = [content]
    elif isinstance(content, list):
        texts = [as_text(block.get("text")) for block in content if isinstance(block, dict) and block.get("type") == "text"]
    else:
        return ""
    out = []
    for text in texts:
        command = _COMMAND.search(text)
        if command:
            args = _ARGS.search(text)
            out.append(f"{command.group(1).strip()} {args.group(1).strip() if args else ''}".strip())
            continue
        if _WRAPPER.match(text) or _INTERRUPTED.match(text.strip()):
            continue
        text = _REMINDER.sub("", text).strip()
        if text:
            out.append(text)
    return strip_context("\n\n".join(out)).strip()


class _Reading:
    """What a transcript says, read line by line."""

    def __init__(self, report: Report) -> None:
        self.builder = Builder(report)
        self.cwd = ""
        self.custom = ""
        self.ai = ""
        self.summary = ""
        self.first = 0.0
        self.last = 0.0

    def feed(self, record: dict[str, Any]) -> None:
        kind = record.get("type")
        if kind == "custom-title" and as_text(record.get("customTitle")).strip():
            self.custom = as_text(record.get("customTitle")).strip()
        elif kind == "ai-title" and as_text(record.get("aiTitle")).strip():
            self.ai = as_text(record.get("aiTitle")).strip()
        elif kind == "summary" and as_text(record.get("summary")).strip():
            self.summary = as_text(record.get("summary")).strip()
        if kind != "ai-title" and as_text(record.get("aiTitle")).strip():
            self.ai = as_text(record.get("aiTitle")).strip()
        at = parse_time(record.get("timestamp"))
        if at:
            self.first = min(self.first, at) if self.first else at
            self.last = max(self.last, at)
        if not self.cwd and as_text(record.get("cwd")).strip():
            self.cwd = as_text(record.get("cwd")).strip()
        if record.get("isSidechain") or record.get("isMeta") or record.get("isCompactSummary") or record.get("isVisibleInTranscriptOnly"):
            return
        message = record.get("message")
        if not isinstance(message, dict):
            return
        if kind == "user":
            self.builder.user(_user_text(message.get("content")), at)
        elif kind == "assistant":
            if record.get("isApiErrorMessage") or message.get("model") == "<synthetic>":
                return
            key = message.get("id") or record.get("uuid") or id(record)
            content = message.get("content")
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

    @property
    def title(self) -> str:
        if self.custom or self.ai or self.summary:
            return first_words(self.custom or self.ai or self.summary)
        first = next((m for m in self.builder.messages if m.role == "user"), None)
        return first_words(first.text) if first is not None else ""


class ClaudeSource:
    app = "claude"
    label = LABELS["claude"]

    def __init__(self, root: str | Path, *, home: str | Path | None = None) -> None:
        self.root = Path(root)
        self.home = Path(home) if home is not None else None
        self.report = Report()

    def available(self) -> bool:
        return self.root.is_dir()

    # ------------------------------------------------------------------ files

    def _transcripts(self) -> list[Path]:
        out: list[Path] = []
        try:
            folders = [entry for entry in (self.root / "projects").iterdir() if entry.is_dir()]
        except OSError:
            return []
        for folder in folders:
            try:
                for entry in folder.iterdir():
                    name = entry.name
                    if name.endswith(".jsonl") and ".orphaned-" not in name and not name.startswith("agent-") and entry.is_file():
                        out.append(entry)
            except OSError:
                continue
        return out

    def _cwd(self, path: Path) -> str:
        try:
            for number, record in enumerate(records(lines(path, limit=HEAD), None)):
                cwd = as_text(record.get("cwd")).strip()
                if cwd:
                    return cwd
                if number >= CWD_LINES:
                    break
        except OSError:
            pass
        return ""

    def _listed_projects(self) -> list[str]:
        """The folders Claude Code's own state file lists as projects (``projects`` in ``.claude.json``, which sits next
        to ``~/.claude``, or in the moved config folder; versions after 2.1 no longer keep the list there)."""
        default = self.home is not None and norm_key(self.root) == norm_key(self.home / ".claude")
        path = self.home / ".claude.json" if default and self.home is not None else self.root / ".claude.json"
        try:
            if path.stat().st_size > CONFIG_LIMIT:
                return []
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        projects = data.get("projects") if isinstance(data, dict) else None
        if not isinstance(projects, dict):
            return []
        return [str(Path(key)) for key in projects if isinstance(key, str) and key.strip()]

    def _projects(self) -> tuple[dict[str, str], list[str]]:
        """(``projects/<dir>`` name -> the folder it is for, the project folders), from the folders the transcripts ran in,
        the projects Claude Code lists, and the git repositories they are in."""
        seen: dict[str, Counter[str]] = {}
        for path in self._transcripts():
            cwd = self._cwd(path)
            if cwd:
                seen.setdefault(path.parent.name, Counter())[cwd] += 1
        names: dict[str, str] = {}
        folders: dict[str, str] = {}
        for name, counts in seen.items():
            cwd = counts.most_common(1)[0][0]
            names[name.lower()] = cwd
            folders.setdefault(norm_key(cwd), cwd)
        for folder in self._listed_projects():
            folders.setdefault(norm_key(folder), folder)
            names.setdefault(encode(folder).lower(), folder)
        for cwd in list(folders.values()):
            root = git_root(cwd, self.home)
            if root:
                folders.setdefault(norm_key(root), root)
                names.setdefault(encode(root).lower(), root)
        return names, [folder for folder in folders.values() if Path(folder).is_dir()]

    def _memory_folder(self, name: str, names: dict[str, str]) -> str:
        if name.lower() in names:
            return names[name.lower()]
        return next((folder for folder in names.values() if _same_name(name, folder)), "")

    # ------------------------------------------------------------------ notes

    def memories(self) -> list[MemoryCandidate]:
        self.report = Report()
        if not self.available():
            return []
        done: set[str] = set()
        out: list[MemoryCandidate] = []

        def add(path: Path, scope: str, project: str, tags: tuple[str, ...] = ()) -> None:
            key = norm_key(path.resolve() if path.exists() else path)
            if key in done:
                return
            done.add(key)
            text = read_text(path, self.report)
            if text is not None:
                out.extend(split_markdown(text, app=self.app, origin_file=str(path), scope=scope, project=project, base_tags=tags, report=self.report))

        add(self.root / "CLAUDE.md", "global", "")
        for path in _markdown_under(self.root / "rules"):
            add(path, "global", "", ("rules",))
        names, projects = self._projects()
        for folder in sorted(projects, key=norm_key):
            base = Path(folder)
            for path in (base / "CLAUDE.md", base / ".claude" / "CLAUDE.md", base / "CLAUDE.local.md"):
                add(path, "project", folder)
            for path in _markdown_under(base / ".claude" / "rules"):
                add(path, "project", folder, ("rules",))
        try:
            memory_folders = sorted(entry for entry in (self.root / "projects").iterdir() if (entry / "memory").is_dir())
        except OSError:
            memory_folders = []
        for entry in memory_folders:
            folder = self._memory_folder(entry.name, names)
            files = list(_markdown_under(entry / "memory"))
            if not folder:
                # (no transcript says which folder this is, and a project's notes must not become everyone's)
                self.report.skipped += len(files)
                continue
            out.extend(self._auto_memory(files, folder, done))
        return tidy(out)

    def _auto_memory(self, files: list[Path], folder: str, done: set[str]) -> list[MemoryCandidate]:
        out: list[MemoryCandidate] = []
        siblings = {path.name.lower() for path in files}
        for path in files:
            key = norm_key(path)
            if key in done:
                continue
            done.add(key)
            text = read_text(path, self.report)
            if text is None:
                continue
            if path.name.lower() == "memory.md":
                text = _without_index_lines(text, siblings)
                out.extend(split_markdown(text, app=self.app, origin_file=str(path), scope="project", project=folder, base_tags=("auto-memory",), report=self.report))
                continue
            meta, body = front_matter(text)
            description = meta.get("description", "").strip()
            if description and description not in body:
                body = f"{description}\n\n{body}"
            kind = MEMORY_TYPES.get(meta.get("type", "").strip().lower())
            tags = ("auto-memory", meta["type"].strip()) if meta.get("type", "").strip() else ("auto-memory",)
            out.extend(
                split_markdown(
                    body, app=self.app, origin_file=str(path), scope="project", project=folder, base_tags=tags,
                    title=meta.get("name", "").strip() or path.stem, kind=kind, report=self.report,
                )
            )
        return out

    # ------------------------------------------------------------------ conversations

    def sessions(self) -> list[SessionInfo]:
        """Every transcript with something to import, newest first. A transcript bigger than ``FULL_SCAN`` is read at its
        start and end only: its title can then miss a rename made in the middle, and its count is a lower bound
        (``exact`` false)."""
        self.report = Report()
        out: list[SessionInfo] = []
        for path in self._transcripts():
            info = self._info(path)
            if info is None:
                self.report.skipped += 1
            else:
                out.append(info)
        out.sort(key=lambda info: info.updated, reverse=True)
        return out

    def _info(self, path: Path) -> SessionInfo | None:
        size = size_of(path)
        reading = _Reading(self.report)
        exact = size <= FULL_SCAN
        try:
            if exact:
                for record in records(lines(path), self.report):
                    reading.feed(record)
            else:
                for record in records(lines(path, limit=HEAD), self.report):
                    reading.feed(record)
                for record in records(tail_lines(path, TAIL), self.report):
                    reading.feed(record)
        except OSError:
            return None
        if not reading.builder.messages:
            return None
        native = path.stem
        return SessionInfo(
            key=session_key(self.app, native),
            app=self.app,
            native_id=native,
            title=reading.title or "Claude Code 会话",
            project=reading.cwd,
            started=reading.first or mtime(path),
            updated=reading.last or mtime(path),
            messages=len(reading.builder.messages),
            size=size,
            path=str(path),
            exact=exact,
        )

    def read(self, info: SessionInfo) -> list[Message]:
        self.report = Report()
        path = Path(info.path)
        if not path.is_file():
            raise ImportFailed("找不到这个 Claude Code 会话的文件了（可能已被 Claude Code 按保留期清理）。")
        reading = _Reading(self.report)
        try:
            for record in records(lines(path), self.report):
                reading.feed(record)
        except OSError as error:
            raise ImportFailed(f"读不了这个 Claude Code 会话的文件：{error.strerror or error}") from None
        return reading.builder.messages


def _markdown_under(folder: Path) -> Iterable[Path]:
    try:
        return sorted((path for path in folder.rglob("*.md") if path.is_file()), key=lambda p: str(p).lower())
    except OSError:
        return []


def _without_index_lines(text: str, siblings: set[str]) -> str:
    """``MEMORY.md`` without its index lines (a link to a topic file next to it, which is imported on its own)."""
    kept = []
    for line in text.split("\n"):
        targets = _LINK.findall(line)
        if targets and re.match(r"^\s*(?:[-*+]|\d+[.)])\s", line) and any(Path(target).name.lower() in siblings for target in targets):
            continue
        kept.append(line)
    return "\n".join(kept)
