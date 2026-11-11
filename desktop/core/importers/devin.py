"""Devin (Cognition's Devin CLI and Devin Desktop): its rules and session summaries as notes, its sessions as conversations.

Where things are (``%APPDATA%\\devin`` on Windows, ``~/.config/devin`` elsewhere; from docs.devin.ai/cli/extensibility/rules
and /configuration, and the files of a Devin CLI install):

- ``AGENTS.md`` / ``AGENT.md`` in that folder, ``~/.devin/global_rules.md`` and ``~/.devin/rules/*.md``: global rules.
- ``summaries/*.md``: what Devin wrote down at the end of a session (overview, details, state). Global notes: nothing says
  which project a summary is about.
- ``cli/summaries/history_*.md`` are not notes: each is the whole conversation as sent to a summarizer (``=== MESSAGE 0 -
  System ===``, the system prompt, every message), so they are left out.
- ``cli/sessions.db``: SQLite (WAL), opened read-only. ``sessions`` (``id``, ``working_directory``, ``title``,
  ``created_at`` / ``last_activity_at`` in seconds, ``main_chain_id``, ``hidden``) and ``message_nodes`` (a forest per
  session: ``node_id``, ``parent_node_id``, ``chat_message`` JSON ``{role, content, tool_calls, metadata: {created_at,
  is_user_input}}``, ``metadata`` JSON ``{summarized_from}``). The conversation is the chain from ``main_chain_id`` up to its
  root; where Devin compacted it, a node's ``summarized_from`` names the end of the chain it replaced, which comes first.
  Devin may be running: the database is only read, and a lock is reported, not waited out.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Sequence
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
    parse_time,
    read_text,
    session_key,
    sqlite_uri,
    subject_of,
)
from desktop.core.importers.split import split_markdown, tidy

LOCKED = "Devin 的会话数据库正被占用（database is locked），请稍后再试，或先关掉 Devin 再导入。"
UNREADABLE = "Devin 的会话数据库读不了：{error}"
QUIET_TOOLS = frozenset({"get_output", "sleep", "wait"})
BATCH = 400
_DUMP = re.compile(r"^=== MESSAGE \d+ - \w+ ===")
_COLUMNS = ("id", "working_directory", "title", "created_at", "last_activity_at", "main_chain_id", "hidden")


def _problem(error: sqlite3.Error) -> str:
    text = str(error)
    return LOCKED if "locked" in text.lower() or "busy" in text.lower() else UNREADABLE.format(error=text)


def _json(text: Any) -> dict[str, Any]:
    if not isinstance(text, str):
        return {}
    try:
        value = json.loads(text)
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def _order(nodes: dict[int, tuple[int | None, Any]], head: Any) -> list[int]:
    """The node ids of the conversation in order: the chain ending at ``head``, with the chain each compaction replaced
    put before the node that summarizes it."""
    seen: set[int] = set()

    def chain(end: Any) -> list[int]:
        ids: list[int] = []
        current = end
        while isinstance(current, int) and current in nodes and current not in seen:
            seen.add(current)
            ids.append(current)
            current = nodes[current][0]
        ids.reverse()
        return ids

    def expand(ids: list[int], depth: int) -> list[int]:
        out: list[int] = []
        for node in ids:
            source = nodes[node][1]
            if depth < 32 and isinstance(source, int) and source in nodes and source not in seen:
                out += expand(chain(source), depth + 1)
            out.append(node)
        return out

    return expand(chain(head), 0)


class DevinSource:
    app = "devin"
    label = LABELS["devin"]
    wait = 1.0
    """Seconds to wait for a lock Devin holds on its database before saying so."""

    def __init__(self, root: str | Path, *, home: str | Path | None = None) -> None:
        self.root = Path(root)
        self.home = Path(home) if home is not None else None
        self.report = Report()

    @property
    def database(self) -> Path:
        return self.root / "cli" / "sessions.db"

    def available(self) -> bool:
        return self.root.is_dir()

    # ------------------------------------------------------------------ notes

    def memories(self) -> list[MemoryCandidate]:
        self.report = Report()
        if not self.available():
            return []
        files: list[tuple[Path, tuple[str, ...]]] = [(self.root / "AGENTS.md", ("rules",)), (self.root / "AGENT.md", ("rules",))]
        if self.home is not None:
            files.append((self.home / ".devin" / "global_rules.md", ("rules",)))
            files += [(path, ("rules",)) for path in _sorted(self.home / ".devin" / "rules", "*.md")]
        files += [(path, ("summary",)) for path in _sorted(self.root / "summaries", "*.md")]
        files += [(path, ("summary",)) for path in _sorted(self.root / "cli" / "summaries", "*.md")]
        out: list[MemoryCandidate] = []
        for path, tags in files:
            text = read_text(path, self.report)
            if text is None:
                continue
            if _DUMP.match(text.lstrip()):
                self.report.skipped += 1
                continue
            out.extend(split_markdown(text, app=self.app, origin_file=str(path), scope="global", base_tags=tags, report=self.report))
        return tidy(out)

    # ------------------------------------------------------------------ the database

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(sqlite_uri(self.database), uri=True, timeout=self.wait)
        connection.execute("pragma query_only = on")
        return connection

    def _nodes(self, connection: sqlite3.Connection, session: str) -> dict[int, tuple[int | None, Any]]:
        nodes: dict[int, tuple[int | None, Any]] = {}
        for node, parent, meta in connection.execute("select node_id, parent_node_id, metadata from message_nodes where session_id = ?", (session,)):
            if isinstance(node, int):
                nodes[node] = (parent if isinstance(parent, int) else None, _json(meta).get("summarized_from"))
        return nodes

    def _messages(self, connection: sqlite3.Connection, session: str, head: Any, report: Report) -> list[Message]:
        order = _order(self._nodes(connection, session), head)
        rows: dict[int, tuple[Any, Any]] = {}
        for start in range(0, len(order), BATCH):
            part = order[start : start + BATCH]
            marks = ",".join("?" * len(part))
            for node, raw, created in connection.execute(
                f"select node_id, chat_message, created_at from message_nodes where session_id = ? and node_id in ({marks})", (session, *part)
            ):
                rows[node] = (raw, created)
        chats = []
        for node in order:
            raw, created = rows.get(node, (None, None))
            chat = _json(raw)
            if not chat:
                report.bad_records += 1
                continue
            chats.append((node, chat, created))
        return _conversation(chats, report)

    def sessions(self) -> list[SessionInfo]:
        """Every session that is not hidden and has something to import, newest first; counts are exact. When the
        database cannot be read (Devin holds a lock on it) the list is empty and ``report.problems`` says why."""
        self.report = Report()
        if not self.database.is_file():
            return []
        out: list[SessionInfo] = []
        try:
            connection = self._connect()
        except sqlite3.Error as error:
            self.report.problems.append(_problem(error))
            return []
        try:
            columns = {row[1] for row in connection.execute("pragma table_info('sessions')")}
            if not {"id", "main_chain_id"} <= columns:
                self.report.problems.append(UNREADABLE.format(error="不认识的数据库结构"))
                return []
            wanted = [column if column in columns else f"null as {column}" for column in _COLUMNS]
            rows = connection.execute(f"select {', '.join(wanted)} from sessions").fetchall()
            for native, folder, title, created, active, head, hidden in rows:
                if hidden:
                    self.report.skipped += 1
                    continue
                messages = self._messages(connection, str(native), head, Report())
                if not messages:
                    self.report.skipped += 1
                    continue
                first = next((m for m in messages if m.role == "user"), None)
                started = parse_time(created) or (messages[0].at or 0.0)
                out.append(
                    SessionInfo(
                        key=session_key(self.app, str(native)),
                        app=self.app,
                        native_id=str(native),
                        title=first_words(as_text(title)) or (first_words(first.text) if first is not None else "") or "Devin 会话",
                        project=as_text(folder),
                        started=started,
                        updated=parse_time(active) or (messages[-1].at or started),
                        messages=len(messages),
                        size=0,
                        path=str(self.database),
                    )
                )
        except sqlite3.Error as error:
            self.report.problems.append(_problem(error))
            return []
        finally:
            connection.close()
        out.sort(key=lambda info: info.updated, reverse=True)
        return out

    def read(self, info: SessionInfo) -> list[Message]:
        self.report = Report()
        if not self.database.is_file():
            raise ImportFailed("找不到 Devin 的会话数据库了。")
        try:
            connection = self._connect()
        except sqlite3.Error as error:
            raise ImportFailed(_problem(error)) from None
        try:
            row = connection.execute("select main_chain_id from sessions where id = ?", (info.native_id,)).fetchone()
            if row is None:
                raise ImportFailed("Devin 里已经没有这个会话了（可能已被删除）。")
            return self._messages(connection, info.native_id, row[0], self.report)
        except sqlite3.Error as error:
            raise ImportFailed(_problem(error)) from None
        finally:
            connection.close()


def _conversation(chats: Sequence[tuple[int, dict[str, Any], Any]], report: Report) -> list[Message]:
    builder = Builder(report)
    # (a summarizer's request is a "user" message too, without the mark a typed one has; the oldest databases mark none)
    marked = any(isinstance(chat.get("metadata"), dict) and "is_user_input" in chat["metadata"] for _, chat, _ in chats if chat.get("role") == "user")
    for node, chat, created in chats:
        meta = chat.get("metadata") if isinstance(chat.get("metadata"), dict) else {}
        at = parse_time(meta.get("created_at")) or parse_time(created)
        role = chat.get("role")
        if role == "user":
            if marked and meta.get("is_user_input") is not True:
                continue
            builder.user(strip_context(as_text(chat.get("content"))), at)
        elif role == "assistant":
            key = chat.get("message_id") or node
            builder.assistant(as_text(chat.get("content")), at, key)
            for call in chat.get("tool_calls") or ():
                if isinstance(call, dict) and as_text(call.get("name")) not in QUIET_TOOLS:
                    builder.tool(as_text(call.get("name")), subject_of(call.get("arguments")), at, key)
    return builder.messages


def _sorted(folder: Path, pattern: str) -> list[Path]:
    try:
        return sorted((path for path in folder.glob(pattern) if path.is_file()), key=lambda p: str(p).lower())
    except OSError:
        return []
