"""Codex: its memories and global instructions as notes, its rollout files as conversations.

Where things are (``CODEX_HOME``, else ``~/.codex``; from developers.openai.com/codex/memories, the memories README of
openai/codex and the files of a Codex 0.16x install):

- ``AGENTS.md`` (and ``AGENTS.override.md``): the user's global instructions.
- ``memories/`` (and the newer ``memories_v2/``), a git working folder Codex maintains itself: ``MEMORY.md`` (the
  consolidated handbook), ``memory_summary.md`` (the short profile put in every prompt), ``raw_memories.md`` and
  ``rollout_summaries/*.md`` (what the consolidation read), ``extensions/<name>/notes/*.md`` (notes of a memory extension).
  Left out: ``.git``, ``.codex-baseline``, ``skills/`` (procedures, not notes), ``extensions/<name>/instructions.md`` (how
  to write the notes), ``phase2_workspace_diff.md`` (a work file).
- ``sessions/YYYY/MM/DD/rollout-<time>-<thread id>.jsonl`` and ``archived_sessions/``: one conversation each. Codex
  compresses older ones with zstd (``….jsonl.zst``, read here with ``zstd``, which is slow: a few MB of file a second).
- ``state_<n>.sqlite`` (table ``threads``) and ``session_index.jsonl``: the thread list Codex keeps, with titles, folders
  and times. Used when there, read-only, so the list needs no decompression; the rollout files stay the source.

A rollout line is ``{"timestamp", "type": "session_meta" | "response_item" | "event_msg" | "turn_context" | "compacted" |
…, "payload": {...}}`` (the oldest format has the response items themselves as lines). The conversation is in the
response items: ``message`` (role ``user`` / ``assistant`` / ``developer``), ``function_call`` / ``custom_tool_call`` /
``local_shell_call`` / ``web_search_call``, their outputs, ``reasoning``. A user message also carries what Codex added to it
(``# AGENTS.md instructions for …``, ``<environment_context>``, ``<heartbeat>`` …), each in an input part of its own:
those parts are left out. A sub-agent's thread (``session_meta.source.subagent``) is not listed.
"""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from typing import Any

from desktop.core.context_envelope import strip as strip_context
from desktop.core.importers import zstd
from desktop.core.importers.base import (
    LABELS,
    Builder,
    ImportFailed,
    MemoryCandidate,
    Message,
    Report,
    SessionInfo,
    as_text,
    first_line,
    first_words,
    is_compressed,
    lines,
    mtime,
    norm_key,
    parse_time,
    read_text,
    records,
    session_key,
    size_of,
    sqlite_uri,
    subject_of,
    tail_lines,
)
from desktop.core.importers.split import split_markdown, tidy

FULL_SCAN = 8 * 1024 * 1024
HEAD = 1024 * 1024
TAIL = 1024 * 1024
COMPRESSED_HEAD = 256 * 1024
"""Bytes decompressed from the start of a compressed rollout when the thread list does not say its title and folder."""
QUIET_TOOLS = frozenset({"sleep", "wait", "wait_agent"})
"""Calls that only wait: no line for them."""

_ID = re.compile(r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$", re.IGNORECASE)
_WRAPPED = re.compile(r"^\s*<([A-Za-z][\w-]*)(?:\s[^>]*)?>(.*)</\1>\s*$", re.DOTALL)
_ADDED_TAGS = frozenset({"heartbeat", "instructions", "user_instructions", "environment_context", "permissions"})
_UNWRAP = frozenset({"send_user_message_question_reply"})
"""Wrappers around what the user did type (an answer to the model's question)."""
_PATCH_FILE = re.compile(r"^\*\*\* (?:Add|Update|Delete) File: (.+)$", re.MULTILINE)
_RESPONSE_TYPES = frozenset({"message", "function_call", "custom_tool_call", "local_shell_call", "web_search_call", "image_generation_call"})


def _added_part(text: str) -> str | None:
    """``None`` for an input part Codex (or WittyCode) added to the user's message; else what the user typed in it."""
    stripped = text.strip()
    if stripped.startswith("# AGENTS.md instructions for "):
        return None
    found = _WRAPPED.match(stripped)
    if found:
        tag = found.group(1)
        if tag.lower() in _UNWRAP:
            return found.group(2).strip()
        if tag.lower() in _ADDED_TAGS or "_" in tag or "-" in tag:
            return None
    return text


def _user_text(content: Any) -> str:
    if isinstance(content, str):
        parts = [content]
    elif isinstance(content, list):
        parts = [as_text(part.get("text")) for part in content if isinstance(part, dict) and part.get("type") in ("input_text", "text")]
    else:
        return ""
    kept = [kept for kept in (_added_part(part) for part in parts) if kept and kept.strip()]
    return strip_context("\n\n".join(kept)).strip()


def _assistant_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "".join(as_text(part.get("text")) for part in content if isinstance(part, dict) and part.get("type") in ("output_text", "text"))


def _subject(name: str, arguments: Any) -> str:
    if isinstance(arguments, str) and "*** Begin Patch" in arguments:
        return ", ".join(_PATCH_FILE.findall(arguments))
    if isinstance(arguments, str) and not arguments.lstrip().startswith("{"):
        return first_line(arguments)
    try:
        parsed = json.loads(arguments) if isinstance(arguments, str) else arguments
    except ValueError:
        return first_line(arguments)
    if isinstance(parsed, dict):
        command = parsed.get("command")
        if isinstance(command, list) and all(isinstance(part, str) for part in command):
            return " ".join(command[2:] if command[:2] in (["bash", "-lc"], ["powershell", "-Command"], ["pwsh", "-Command"]) else command)
        patch = parsed.get("input") if isinstance(parsed.get("input"), str) else ""
        if "*** Begin Patch" in patch:
            return ", ".join(_PATCH_FILE.findall(patch))
    return subject_of(parsed)


class _Reading:
    def __init__(self, report: Report) -> None:
        self.builder = Builder(report)
        self.cwd = ""
        self.started = 0.0
        self.last = 0.0
        self.subagent = False

    def feed(self, record: dict[str, Any]) -> None:
        kind = record.get("type")
        payload = record.get("payload")
        at = parse_time(record.get("timestamp"))
        if at:
            self.last = max(self.last, at)
        if kind == "session_meta" and isinstance(payload, dict):
            self.cwd = self.cwd or as_text(payload.get("cwd")).strip()
            self.started = self.started or parse_time(payload.get("timestamp")) or at or 0.0
            source = payload.get("source")
            if (isinstance(source, dict) and "subagent" in source) or payload.get("thread_source") == "subagent":
                self.subagent = True
            return
        if kind in _RESPONSE_TYPES and payload is None:
            payload, kind = record, "response_item"
        elif kind is None and "instructions" in record and "id" in record and not self.started:
            # (the oldest rollouts begin with the session itself, untyped)
            self.started = parse_time(record.get("timestamp")) or 0.0
            return
        if kind != "response_item" or not isinstance(payload, dict):
            return
        item = payload.get("type")
        if item == "message":
            role = payload.get("role")
            if role == "user":
                self.builder.user(_user_text(payload.get("content")), at)
            elif role == "assistant":
                self.builder.assistant(_assistant_text(payload.get("content")), at, payload.get("id") or id(payload))
        elif item in ("function_call", "custom_tool_call"):
            name = as_text(payload.get("name")) or "?"
            if name not in QUIET_TOOLS:
                self.builder.tool(name, _subject(name, payload.get("arguments") if item == "function_call" else payload.get("input")), at)
        elif item == "local_shell_call":
            action = payload.get("action") if isinstance(payload.get("action"), dict) else {}
            self.builder.tool("shell", _subject("shell", {"command": action.get("command")}), at)
        elif item == "web_search_call":
            action = payload.get("action") if isinstance(payload.get("action"), dict) else {}
            self.builder.tool("web_search", as_text(action.get("query")) or as_text(action.get("url")), at)
        elif item == "image_generation_call":
            self.builder.tool("image_generation", "", at)


class CodexSource:
    app = "codex"
    label = LABELS["codex"]

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.report = Report()

    def available(self) -> bool:
        return self.root.is_dir()

    # ------------------------------------------------------------------ notes

    def memories(self) -> list[MemoryCandidate]:
        self.report = Report()
        if not self.available():
            return []
        files: list[tuple[Path, tuple[str, ...]]] = [(self.root / "AGENTS.md", ("AGENTS.md",)), (self.root / "AGENTS.override.md", ("AGENTS.md",))]
        for folder in ("memories", "memories_v2"):
            base = self.root / folder
            files += [(base / "memory_summary.md", ("memory",)), (base / "MEMORY.md", ("memory",)), (base / "raw_memories.md", ("raw",))]
            files += [(path, ("rollout",)) for path in _sorted(base / "rollout_summaries", "*.md")]
            files += [(path, ("note",)) for path in _sorted(base / "extensions", "*/notes/**/*.md")]
        out: list[MemoryCandidate] = []
        for path, tags in files:
            text = read_text(path, self.report)
            if text is not None:
                out.extend(split_markdown(text, app=self.app, origin_file=str(path), scope="global", base_tags=tags, report=self.report))
        return tidy(out)

    # ------------------------------------------------------------------ conversations

    def _rollouts(self) -> dict[str, Path]:
        """Thread id -> its rollout file (a plain one before a compressed one, the live folder before the archive)."""
        found: dict[str, tuple[int, Path]] = {}
        for rank_folder, folder in enumerate(("sessions", "archived_sessions")):
            try:
                paths = [p for p in (self.root / folder).rglob("rollout-*.jsonl*") if p.name.endswith((".jsonl", ".jsonl.zst"))]
            except OSError:
                continue
            for path in paths:
                stem = path.name[: -len(".jsonl.zst")] if path.name.endswith(".zst") else path.name[: -len(".jsonl")]
                match = _ID.search(stem)
                if not match:
                    continue
                rank = rank_folder * 2 + (1 if is_compressed(path) else 0)
                native = match.group(1).lower()
                if native not in found or rank < found[native][0]:
                    found[native] = (rank, path)
        return {native: path for native, (_, path) in found.items()}

    def _threads(self) -> dict[str, dict[str, Any]]:
        """What Codex's own thread list says (``{}`` when it cannot be read: it is a cache of the rollouts)."""
        candidates = sorted(self.root.glob("state_*.sqlite"), key=lambda p: int(re.sub(r"\D", "", p.stem) or 0), reverse=True)
        for path in candidates:
            try:
                connection = sqlite3.connect(sqlite_uri(path), uri=True, timeout=1)
            except sqlite3.Error:
                continue
            try:
                connection.row_factory = sqlite3.Row
                columns = {row[1] for row in connection.execute("pragma table_info('threads')")}
                if "id" not in columns:
                    continue
                wanted = [c for c in ("id", "title", "name", "cwd", "created_at", "updated_at", "created_at_ms", "updated_at_ms", "source", "thread_source") if c in columns]
                return {str(row["id"]).lower(): dict(row) for row in connection.execute(f"select {', '.join(wanted)} from threads")}
            except sqlite3.Error:
                continue
            finally:
                connection.close()
        return {}

    def _names(self) -> dict[str, str]:
        names: dict[str, str] = {}
        try:
            for record in records(lines(self.root / "session_index.jsonl"), None):
                native, name = as_text(record.get("id")).lower(), as_text(record.get("thread_name")).strip()
                if native and name:
                    names[native] = name
        except OSError:
            pass
        return names

    def sessions(self) -> list[SessionInfo]:
        """Every main thread with a rollout file, newest first. Titles, folders and times come from Codex's thread list
        when it has them. Counts: exact for a plain file up to ``FULL_SCAN``; a bigger one is read at its start and end
        (a lower bound); a compressed one is not counted (0, ``exact`` false) unless it had to be opened for its title,
        and then only its first ``COMPRESSED_HEAD`` bytes are."""
        self.report = Report()
        threads = self._threads()
        names = self._names()
        out: list[SessionInfo] = []
        for native, path in self._rollouts().items():
            row = threads.get(native, {})
            if row.get("thread_source") == "subagent" or "subagent" in as_text(row.get("source")):
                self.report.skipped += 1
                continue
            info = self._info(native, path, row, names.get(native, ""))
            if info is None:
                self.report.skipped += 1
            else:
                out.append(info)
        out.sort(key=lambda info: info.updated, reverse=True)
        return out

    def _info(self, native: str, path: Path, row: dict[str, Any], name: str) -> SessionInfo | None:
        size = size_of(path)
        reading = _Reading(self.report)
        title = name or as_text(row.get("name")).strip() or as_text(row.get("title")).strip()
        cwd = as_text(row.get("cwd")).strip()
        exact = False
        try:
            if not is_compressed(path):
                exact = size <= FULL_SCAN
                for record in records(lines(path, limit=None if exact else HEAD), self.report):
                    reading.feed(record)
                if not exact:
                    for record in records(tail_lines(path, TAIL), self.report):
                        reading.feed(record)
            elif not (title and cwd and row):
                for record in records(lines(path, limit=COMPRESSED_HEAD), self.report):
                    reading.feed(record)
        except (OSError, zstd.ZstdError):
            return None
        if reading.subagent or (exact and not reading.builder.messages):
            return None
        first = next((m for m in reading.builder.messages if m.role == "user"), None)
        updated = parse_time(row.get("updated_at_ms")) or parse_time(row.get("updated_at")) or (reading.last if not is_compressed(path) else 0.0) or mtime(path)
        return SessionInfo(
            key=session_key(self.app, native),
            app=self.app,
            native_id=native,
            title=first_words(title) if title else (first_words(first.text) if first is not None else "") or "Codex 会话",
            project=cwd or reading.cwd,
            started=parse_time(row.get("created_at_ms")) or parse_time(row.get("created_at")) or reading.started or updated,
            updated=updated,
            messages=len(reading.builder.messages),
            size=size,
            path=str(path),
            exact=exact,
        )

    def read(self, info: SessionInfo) -> list[Message]:
        """Every message of the thread (a compressed file is decompressed as it is read; a big one takes a while)."""
        self.report = Report()
        path = Path(info.path)
        if not path.is_file():
            raise ImportFailed("找不到这个 Codex 会话的文件了（可能已被归档、压缩或删除，请刷新列表）。")
        reading = _Reading(self.report)
        try:
            for record in records(lines(path), self.report):
                reading.feed(record)
        except zstd.ZstdError:
            raise ImportFailed("这个 Codex 会话的文件是压缩过的，但解压失败了（文件不完整，或用了不支持的压缩方式）。") from None
        except OSError as error:
            raise ImportFailed(f"读不了这个 Codex 会话的文件：{error.strerror or error}") from None
        return reading.builder.messages


def _sorted(folder: Path, pattern: str) -> list[Path]:
    try:
        return sorted((path for path in folder.glob(pattern) if path.is_file()), key=lambda p: norm_key(p))
    except OSError:
        return []
