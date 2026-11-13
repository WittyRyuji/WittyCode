"""Saved conversations of the application's own harness.

No engine keeps these for us (Droid and Codex keep their own sessions), so each conversation is a file,
``<root>\\sessions\\<id>.json``: the Responses items to send with the next request (``items``), which of them begin a turn
(``turns``, for rebuilding what the page shows), what each tool call came to (``tools``: the outcome and the diff, which the
items do not hold), the settings and the token figures. ``index.json`` lists them for the sidebar without reading every file.

Every write is atomic (a temp file in the same folder, then ``os.replace``): a crash leaves the old file or the new one.
A file that cannot be read is skipped, never fatal. The index is a cache of the files: when it is missing or wrong it is
rebuilt from them.

A conversation is also saved while a turn runs, so a file can hold a turn the application did not live to end (it was
killed, crashed, lost power): the turn is still ``running``, and a call in it may have no result yet. What is written
then has ``LOST`` as the output of such a call (files of the earlier version have no output at all, and their running
turn says ``done`` with no end). ``recover`` makes such a file one the conversation can go on from: every call has
exactly one output after it (the rule of the loop's history, ``loop``), and the turn is ``interrupted`` with a note
that says why. Nothing is run again: the model reads ``LOST`` and decides.
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import time
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from desktop.core.diffs import DiffLine, FileDiff
from desktop.core.harness.compact import mend_boundary
from desktop.core.settings import same_folder

log = logging.getLogger("desktop.harness.store")

VERSION = 1
SESSIONS = "sessions"
INDEX = "index.json"
_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

LOST = (
    "Result unknown: WittyCode was closed or crashed while this tool call was waiting or running, so no result was "
    "recorded. It may or may not have run, completely or in part. Do not blindly repeat it or any other action with side "
    "effects: first check the current state (read the files, look at what it would have changed), then decide."
)
"""The output of a call whose result never reached the disk."""
CUT_OFF = "WittyCode 在这一轮进行中被关闭或崩溃了，这一轮没有做完；可以回复「继续」。"
CUT_OFF_UNKNOWN = "WittyCode 在这一轮进行中被关闭或崩溃了，这一轮没有做完：当时没有结果的 {count} 个工具调用可能执行过，也可能没有，不会自动重做；可以回复「继续」。"


@dataclass(slots=True)
class SavedTurn:
    start: int
    """Index in ``items`` of the turn's first item (its user message)."""
    at: float
    finished: float | None = None
    status: str = "done"
    """``running`` (saved before the turn ended: in a file only when the application did not live to end it) | ``done`` |
    ``limit`` (stopped at the loop's step limit) | ``incomplete`` (the answer was cut short) | ``interrupted`` |
    ``failed``. Any other value reads as ``done``."""
    error: str = ""
    model: str = ""
    note: str = ""
    """What the page said about how the turn ended (``limit``, ``incomplete``, a turn ``recover`` found cut off)."""


@dataclass(slots=True)
class SavedSession:
    id: str
    project: str
    title: str = ""
    model: str = ""
    reasoning: str = ""
    autonomy: str = ""
    mode: str = ""
    """``spec`` (Plan) or empty (Auto): the mode the conversation was last in."""
    created_at: float = field(default_factory=time.time)
    modified_at: float = field(default_factory=time.time)
    items: list[dict[str, Any]] = field(default_factory=list)
    turns: list[SavedTurn] = field(default_factory=list)
    tools: dict[str, dict[str, Any]] = field(default_factory=dict)
    """Per ``call_id``: ``{"status": "done" | "error" | "cancelled", "diffs": [...]}``."""
    usage: dict[str, Any] = field(default_factory=dict)
    """Tokens of the whole conversation: ``input``, ``output``, optional ``cacheWrite``, ``cacheRead``."""
    prompt: dict[str, str] = field(default_factory=dict)
    """The original instructions and their latest rendered state; updates are appended to ``items``."""
    requests: list[dict[str, Any]] = field(default_factory=list)
    """The most recent content-free request diagnostics (``harness.diagnostics``)."""
    context_used: int = 0
    recalled: list[str] = field(default_factory=list)
    usage_models: dict[str, dict[str, Any]] = field(default_factory=dict)
    """Known cumulative usage per configured model; absent in older files (do not infer it from the latest model)."""
    origin: dict[str, Any] = field(default_factory=dict)
    """Where a conversation imported from another app came from: ``{"app", "native", "at"}`` (``core/importers``); empty
    for one made here. Written to the file only when set, so files without it read as before."""
    compaction: dict[str, Any] = field(default_factory=dict)
    """The summary the model reads in place of the start of ``items`` (``harness.compact``): ``{"upto", "summary", "at",
    "before", "after", "model", "mechanical"}``, ``upto`` being the index in ``items`` where the unsummarised part begins.
    Empty: the model reads all of ``items``. ``items`` itself is never shortened, the page is rebuilt from all of it."""

    @property
    def messages(self) -> int:
        return len(self.turns)


@dataclass(frozen=True, slots=True)
class SavedRow:
    id: str
    project: str
    title: str
    modified_at: float
    messages: int
    model: str = ""
    """The model the conversation was last on (empty in an index written before it was kept)."""
    created_at: float = 0.0


def diff_to_json(diff: FileDiff) -> dict[str, Any]:
    return {
        "path": diff.path,
        "lines": [[line.kind, line.text, line.old, line.new] for line in diff.lines],
        "added": diff.added,
        "removed": diff.removed,
        "truncated": diff.truncated,
        "new": diff.is_new_file,
    }


def diff_from_json(raw: Any) -> FileDiff | None:
    if not isinstance(raw, Mapping):
        return None
    try:
        lines = tuple(DiffLine(kind, text, old, new) for kind, text, old, new in raw.get("lines") or ())
        return FileDiff(str(raw.get("path") or ""), lines, int(raw.get("added") or 0), int(raw.get("removed") or 0), int(raw.get("truncated") or 0), bool(raw.get("new")))
    except (TypeError, ValueError):
        return None


def _session_json(session: SavedSession) -> dict[str, Any]:
    data: dict[str, Any] = {
        "version": VERSION,
        "id": session.id,
        "project": session.project,
        "title": session.title,
        "model": session.model,
        "reasoning": session.reasoning,
        "autonomy": session.autonomy,
        "mode": session.mode,
        "created_at": session.created_at,
        "modified_at": session.modified_at,
        "turns": [{"start": t.start, "at": t.at, "finished": t.finished, "status": t.status, "error": t.error, "model": t.model, "note": t.note} for t in session.turns],
        "tools": session.tools,
        "usage": session.usage,
        "context_used": session.context_used,
        "recalled": session.recalled,
        "items": session.items,
    }
    if session.prompt:
        data["prompt"] = session.prompt
    if session.requests:
        data["requests"] = session.requests[-128:]
    if session.usage_models:
        data["usage_models"] = session.usage_models
    if session.origin:
        data["origin"] = session.origin
    if session.compaction:
        data["compaction"] = session.compaction
    return data


def _compaction_from(raw: Any, count: int) -> dict[str, Any]:
    """The compaction of a file, or nothing when it is not one the model could be given (the whole log is read then)."""
    if not isinstance(raw, Mapping) or not isinstance(raw.get("summary"), str) or not raw["summary"].strip():
        return {}
    upto = raw.get("upto")
    if isinstance(upto, bool) or not isinstance(upto, int) or not 0 <= upto <= count:
        return {}
    return dict(raw)


def _session_from(raw: Any) -> SavedSession | None:
    if not isinstance(raw, Mapping) or raw.get("version") != VERSION or not _ID.match(str(raw.get("id") or "")):
        return None
    try:
        turns = [
            SavedTurn(int(t["start"]), float(t["at"]), t.get("finished"), str(t.get("status") or "done"), str(t.get("error") or ""), str(t.get("model") or ""), str(t.get("note") or ""))
            for t in raw.get("turns") or ()
            if isinstance(t, Mapping)
        ]
        items = [item for item in raw.get("items") or () if isinstance(item, dict)]
        return SavedSession(
            id=str(raw["id"]),
            project=str(raw.get("project") or ""),
            title=str(raw.get("title") or ""),
            model=str(raw.get("model") or ""),
            reasoning=str(raw.get("reasoning") or ""),
            autonomy=str(raw.get("autonomy") or ""),
            mode=str(raw.get("mode") or ""),
            created_at=float(raw.get("created_at") or 0.0),
            modified_at=float(raw.get("modified_at") or 0.0),
            items=items,
            turns=turns,
            tools={str(k): dict(v) for k, v in (raw.get("tools") or {}).items() if isinstance(v, Mapping)},
            usage={str(k): int(v) for k, v in (raw.get("usage") or {}).items() if isinstance(v, (int, float))},
            prompt={key: value for key, value in raw.get("prompt", {}).items() if key in ("instructions", "current") and isinstance(value, str)} if isinstance(raw.get("prompt"), Mapping) else {},
            requests=[dict(entry) for entry in raw.get("requests", [])[-128:] if isinstance(entry, Mapping)] if isinstance(raw.get("requests"), list) else [],
            usage_models={str(k): dict(v) for k, v in raw.get("usage_models", {}).items() if isinstance(v, Mapping)} if isinstance(raw.get("usage_models"), Mapping) else {},
            context_used=int(raw.get("context_used") or 0),
            recalled=[str(x) for x in raw.get("recalled") or ()],
            origin=dict(raw["origin"]) if isinstance(raw.get("origin"), Mapping) else {},
            compaction=_compaction_from(raw.get("compaction"), len(items)),
        )
    except (KeyError, TypeError, ValueError):
        return None


# ---------------------------------------------------------------------- a turn the application did not live to end


def _call_id(item: Mapping[str, Any]) -> str:
    return str(item.get("call_id") or "")


def _is_user(item: Mapping[str, Any]) -> bool:
    return item.get("type") == "message" and item.get("role") == "user"


def _pair(items: list[dict[str, Any]], missing: str = LOST) -> tuple[list[dict[str, Any]], list[int]] | None:
    """``None`` when every ``function_call`` already has exactly one ``function_call_output`` after it. Else the items with
    that made true, and ``position[i]``: where item ``i`` went (where what followed it went, when it was left out).

    The loop's order is kept: the outputs of a step come after all the items of the model's answer and before the next
    step (the next answer, or a user message). A call without an output there gets the one that comes later in the list
    (a turn stopped late), else ``missing`` (``LOST``); an output with no call before it, or a second one for a call, is left out."""
    later: dict[str, int] = {}
    called: set[str] = set()
    for index, item in enumerate(items):
        if item.get("type") == "function_call":
            called.add(_call_id(item))
        elif item.get("type") == "function_call_output" and _call_id(item) in called:
            later.setdefault(_call_id(item), index)
    out: list[dict[str, Any]] = []
    position = [0] * len(items)
    waiting: dict[str, None] = {}
    used: set[int] = set()
    changed = False
    after_output = False

    def settle() -> None:
        for call_id in waiting:
            index = later.get(call_id)
            if index is not None and index not in used:
                used.add(index)
                position[index] = len(out)
                out.append(items[index])
            else:
                out.append({"type": "function_call_output", "call_id": call_id, "output": missing})
        waiting.clear()

    for index, item in enumerate(items):
        if index in used:
            continue
        kind = item.get("type")
        if kind != "function_call_output" and waiting and (after_output or _is_user(item)):
            settle()
            changed = True
        position[index] = len(out)
        if kind == "function_call_output":
            if _call_id(item) not in waiting:
                changed = True
                continue
            del waiting[_call_id(item)]
            used.add(index)
            after_output = True
        else:
            after_output = False
            if kind == "function_call":
                waiting[_call_id(item)] = None
        out.append(item)
    if waiting:
        settle()
        changed = True
    return (out, position) if changed else None


def pair_calls(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """``items`` as a history the next request could send: every call with exactly one output after it (``_pair``).
    ``items`` itself when it is one already; else a new list (``items`` is not changed)."""
    paired = _pair(items)
    return items if paired is None else paired[0]


def _remapped(session: SavedSession, items: list[dict[str, Any]], position: list[int], *, copy: bool) -> tuple[list[SavedTurn], dict[str, Any]]:
    """The turns and the compaction of ``session`` for ``items``, the paired version of its items: every index that
    pointed into the old list (where a turn begins, where the summary stops) is moved to where that item went. The turns
    are changed where they are, or copied (``copy``)."""
    count = len(session.items)
    turns = []
    for turn in session.turns:
        start = position[turn.start] if 0 <= turn.start < count else turn.start
        if copy:
            turn = replace(turn)
        turn.start = start
        turns.append(turn)
    compaction = session.compaction
    if compaction:
        upto = int(compaction.get("upto") or 0)
        compaction = {**compaction, "upto": position[upto] if upto < count else len(items)}
    return turns, compaction


def pair_in_place(session: SavedSession, missing: str = LOST) -> bool:
    """Make every call of ``session`` have exactly one output after it, in place (``_pair``; ``missing`` is the output of a
    call that has none), the indices of its turns and of its compaction following their items. Whether anything changed."""
    pairing = _pair(session.items, missing)
    if pairing is None:
        return False
    items, position = pairing
    session.turns, session.compaction = _remapped(session, items, position, copy=False)
    session.items = items
    return True


def paired(session: SavedSession) -> SavedSession:
    """``session`` as a history the next request could send: itself when its calls are paired already, else a copy that is
    (the log of a running turn has calls that have no output yet; what is saved must not)."""
    pairing = _pair(session.items)
    if pairing is None:
        return session
    items, position = pairing
    turns, compaction = _remapped(session, items, position, copy=True)
    return replace(session, items=items, turns=turns, compaction=compaction)


def recover(session: SavedSession) -> bool:
    """Make a conversation the application did not live to finish a turn of one that can go on, in place: the calls are
    paired (``_pair``), and a turn saved before it ended (``running``, or no end in a file of the earlier version) is
    ``interrupted``, ends where the next one began or at the last save, and gets a note for the page. A compaction whose
    summary stops inside a step (an earlier version put it one place too early when the conversation began with a
    message that looked like a summary) is moved to the end of that step, so that no tool result is sent without its
    call. Whether anything changed (the caller saves it then, once: a recovered conversation has nothing left to recover)."""
    changed = pair_in_place(session)
    if session.compaction:
        upto = int(session.compaction.get("upto") or 0)
        mended = mend_boundary(session.items, upto)
        if mended != upto:
            log.warning("the summary of the conversation %s stopped inside a step (at %d); moved to %d", session.id, upto, mended)
            session.compaction = {**session.compaction, "upto": mended}
            changed = True
    for number, turn in enumerate(session.turns):
        if turn.status != "running" and turn.finished is not None:
            continue
        following = session.turns[number + 1] if number + 1 < len(session.turns) else None
        end = following.start if following is not None else len(session.items)
        unknown = sum(1 for item in session.items[turn.start : end] if item.get("type") == "function_call_output" and item.get("output") == LOST)
        turn.status = "interrupted"
        turn.finished = max(turn.at, following.at if following is not None else session.modified_at)
        turn.note = CUT_OFF_UNKNOWN.format(count=unknown) if unknown else CUT_OFF
        changed = True
    return changed


def _write(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, separators=(",", ":"))
        os.replace(temp, path)
    except BaseException:
        try:
            os.unlink(temp)
        except OSError:
            pass
        raise


class SessionStore:
    def __init__(self, root: str | os.PathLike[str]) -> None:
        self.root = Path(root)

    def _path(self, session_id: str) -> Path:
        if not _ID.match(session_id):
            raise ValueError(f"not a session id: {session_id!r}")
        return self.root / SESSIONS / f"{session_id}.json"

    def save(self, session: SavedSession) -> None:
        _write(self._path(session.id), _session_json(session))
        index = self._index()
        index[session.id] = self._row_json(session)
        self._write_index(index)

    def load(self, session_id: str) -> SavedSession | None:
        """The conversation as it was saved, a turn the application did not live to end included (``recover``)."""
        try:
            raw = json.loads(self._path(session_id).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return _session_from(raw)

    def delete(self, session_id: str) -> None:
        try:
            self._path(session_id).unlink()
        except OSError:
            pass
        index = self._index()
        if index.pop(session_id, None) is not None:
            self._write_index(index)

    def rows(self, project: str | None = None, limit: int | None = None) -> list[SavedRow]:
        rows = []
        for session_id, raw in self._index().items():
            if project is not None and not same_folder(raw.get("project", ""), project):
                continue
            rows.append(
                SavedRow(
                    session_id, str(raw.get("project") or ""), str(raw.get("title") or ""), float(raw.get("modified_at") or 0.0),
                    int(raw.get("messages") or 0), str(raw.get("model") or ""), float(raw.get("created_at") or 0.0),
                )
            )
        rows.sort(key=lambda row: row.modified_at, reverse=True)
        return rows[:limit] if limit is not None else rows

    # ------------------------------------------------------------------ index

    @staticmethod
    def _row_json(session: SavedSession) -> dict[str, Any]:
        return {"project": session.project, "title": session.title, "created_at": session.created_at, "modified_at": session.modified_at, "messages": session.messages, "model": session.model}

    def _index(self) -> dict[str, dict[str, Any]]:
        path = self.root / INDEX
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            index = raw.get("sessions") if isinstance(raw, Mapping) and raw.get("version") == VERSION else None
            if isinstance(index, dict) and self._consistent(index):
                return {str(k): dict(v) for k, v in index.items() if isinstance(v, Mapping)}
        except (OSError, ValueError):
            pass
        return self._rebuild()

    def _consistent(self, index: Mapping[str, Any]) -> bool:
        if any(isinstance(row, Mapping) and "created_at" not in row for row in index.values()):
            return False  # rebuild old indices from the archives' real creation times, not their latest activity
        folder = self.root / SESSIONS
        try:
            names = {entry.stem for entry in folder.glob("*.json")}
        except OSError:
            return not index
        return names == set(index)

    def _rebuild(self) -> dict[str, dict[str, Any]]:
        index: dict[str, dict[str, Any]] = {}
        for path in sorted((self.root / SESSIONS).glob("*.json")):
            session = self.load(path.stem)
            if session is not None:
                index[session.id] = self._row_json(session)
        if index or (self.root / INDEX).exists():
            self._write_index(index)
        return index

    def _write_index(self, index: Mapping[str, Any]) -> None:
        try:
            _write(self.root / INDEX, {"version": VERSION, "sessions": index})
        except OSError:
            log.warning("could not write the session index", exc_info=True)
