"""What the application does with the shared memory: lists it for the page, edits it for the user, says who wrote what.

The page never touches the files: it asks (``memory.list`` and the rest, see ``AppCore``) and this module answers in the
shapes the page uses (camelCase, times in milliseconds). Everything here is synchronous and quick; the application
calls it from a worker thread.

**Whose note is it.** A note records ``by``: ``{"engine", "run"}`` for an agent, ``"user"`` for the user, ``"app"`` for the
application writing on its own (``core/memory/review``: not news, it waits for the weekly review). The ``run`` is
the token of the conversation that wrote it; the application keeps ``runs.json`` (next to the notes) saying which
conversation each run was (its engine, the native session id, the title, the project), because a conversation is only
known to the engine by its own id, and a new conversation does not know it when it starts its memory server.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

from desktop.core.memory.launch import MemoryLaunch
from desktop.core.memory.learn import Learner
from desktop.core.memory.proposals import Proposal, ProposalStore
from desktop.core.memory.review import REVIEW_SIZE, ReviewItem, ReviewStore
from desktop.core.memory.store import (
    APP, KINDS, MAX_ACTIVE, MAX_TEXT, MAX_TITLE, SCOPES, Change, Entry, MemoryRejected, MemoryStore, agent, project_path,
)

MAX_RUNS = 600
RECENT_CHANGES = 50
LIST_LIMIT = 500
MESSAGES = {
    "empty": "标题和内容都不能为空。",
    "title_long": f"标题太长了，最多 {MAX_TITLE} 个字。",
    "text_long": f"内容太长了，最多 {MAX_TEXT} 个字。",
    "secret": "这看起来像密钥或密码，不能存进记忆。",
    "duplicate": "已经有一条相似的记忆了。",
    "limit": f"这个范围的记忆满了（最多 {MAX_ACTIVE} 条），先忘掉一些再加。",
    "not_found": "找不到这条记忆。",
    "busy": "记忆正被别的程序写入，请稍后再试。",
    "bad_scope": "范围不对：项目记忆需要一个项目。",
    "bad_kind": "种类不对。",
    "bad_request": "这次整理的内容不对，记忆没有改动。",
}


def explain(rejected: MemoryRejected) -> str:
    """A refusal of the store in the user's words."""
    return MESSAGES.get(rejected.code, rejected.message)


def _ms(seconds: float) -> int:
    return int(seconds * 1000)


class MemoryBridge:
    def __init__(
        self, launch: MemoryLaunch, *, row_id: Callable[[str, str], str], review_mode: bool = False, clock: Callable[[], float] = time.time
    ) -> None:
        self.launch = launch
        self.row_id = row_id
        self.runs: dict[str, dict[str, str]] = {}
        self._runs_dirty = False
        self._loaded = False
        self.proposals = ProposalStore(launch.root)
        """Suggestions waiting for the user's word (``core/memory/curator`` makes them)."""
        self.review_mode = review_mode
        """Whether the application writes notes on its own and asks for one look a week (``core/memory/review``) instead of
        asking about every suggestion: ``state()["pending"]`` is then the size of a review that is due (0 while none is)."""
        self._clock = clock

    @property
    def store(self) -> MemoryStore:
        return self.launch.store

    @property
    def review(self) -> ReviewStore:
        """The notes the application wrote on its own, waiting for the user's look."""
        return self.launch.review

    @property
    def learner(self) -> Learner:
        """What the user's verdicts taught."""
        return self.launch.learner

    @property
    def root(self) -> Path:
        return self.launch.root

    # ------------------------------------------------------------------ which conversation was a run

    def _load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        try:
            raw = json.loads((self.root / "runs.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if isinstance(raw, Mapping):
            for run, entry in raw.items():
                if isinstance(run, str) and isinstance(entry, Mapping):
                    self.runs[run] = {key: str(entry.get(key) or "") for key in ("engine", "native", "title", "project")}

    def note_run(self, run: str, engine: str, native: str, title: str, project: str) -> bool:
        """Remember that ``run`` is this conversation (``True``: it is news, ``flush_runs`` has something to write)."""
        self._load()
        entry = {"engine": engine, "native": native or "", "title": title or "", "project": project}
        if self.runs.get(run) == entry:
            return False
        self.runs.pop(run, None)
        self.runs[run] = entry
        while len(self.runs) > MAX_RUNS:
            del self.runs[next(iter(self.runs))]
        self._runs_dirty = True
        return True

    def flush_runs(self) -> None:
        if not self._runs_dirty:
            return
        self._runs_dirty = False
        path = self.root / "runs.json"
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            temporary = path.with_name(path.name + ".tmp")
            temporary.write_text(json.dumps(self.runs, ensure_ascii=False), encoding="utf-8")
            os.replace(temporary, path)
        except OSError:
            self._runs_dirty = True

    # ------------------------------------------------------------------ the shapes the page reads

    def who(self, by: Any) -> dict[str, Any]:
        if by == "user":
            return {"kind": "user"}
        if by == APP:
            return {"kind": "app"}
        if not isinstance(by, Mapping):
            return {"kind": "unknown"}
        self._load()
        out: dict[str, Any] = {"kind": "agent", "engine": str(by.get("engine") or "")}
        known = self.runs.get(str(by.get("run") or ""))
        if known:
            out["conversation"] = {
                "title": known["title"],
                "rowId": self.row_id(known["engine"], known["native"]) if known["native"] else "",
                "projectId": known["project"],
            }
        return out

    def entry_json(self, entry: Entry) -> dict[str, Any]:
        return {
            "id": entry.id,
            "title": entry.title,
            "text": entry.text,
            "scope": entry.scope,
            "project": entry.project,
            "projectName": Path(entry.project).name if entry.project else "",
            "kind": entry.kind,
            "tags": list(entry.tags),
            "created": _ms(entry.created),
            "updated": _ms(entry.updated),
            "status": entry.status,
            "rev": entry.rev,
            "source": self.who(entry.source),
            "editor": self.who(entry.editor),
        }

    def change_json(self, change: Change) -> dict[str, Any]:
        entry = change.entry
        return {
            "seq": change.seq,
            "op": change.op,
            "id": change.id,
            "title": entry.title if entry else "",
            "scope": entry.scope if entry else "",
            "projectName": Path(entry.project).name if entry and entry.project else "",
            "by": self.who(change.by),
            "at": _ms(change.at),
        }

    # ------------------------------------------------------------------ what the page asks

    def list(self, params: Mapping[str, Any]) -> dict[str, Any]:
        status = str(params.get("status") or "active")
        status = None if status == "all" else status
        scope = str(params.get("scope") or "") or None
        project = str(params.get("project") or "") or None
        kind = str(params.get("kind") or "") or None
        engine = str(params.get("engine") or "") or None
        query = str(params.get("query") or "").strip()
        if query:
            hits = self.store.search(query, project=None, scope="all", limit=LIST_LIMIT, status=status)
            entries = [hit.entry for hit in hits]
            entries = [e for e in entries if (not scope or e.scope == scope) and (not kind or e.kind == kind) and (not project or (e.project and project_path(e.project) == project_path(project)))]
            if engine:
                entries = [e for e in entries if (e.source.get("engine") if isinstance(e.source, Mapping) else APP if e.source == APP else "user") == engine]
        else:
            entries = self.store.entries(scope=scope, project=project, kind=kind, engine=engine, status=status, limit=LIST_LIMIT)
        return {"entries": [self.entry_json(entry) for entry in entries], "counts": self.store.counts(), **self.state()}

    def state(self) -> dict[str, Any]:
        """The newest change's number, how many changes by agents the user has not looked at yet, and how many suggestions
        wait for the user's word (``review_mode``: how many notes a review that is due has for the user). What the
        application wrote on its own is not news: it waits for the review."""
        seen = self.seen()
        changes = self.store.journal()
        news = [c for c in changes if c.by != APP]
        state = {
            "latest": changes[-1].seq if changes else 0,
            "unseen": sum(1 for c in news if c.seq > seen and not c.by_user),
            "seen": seen,
            "pending": self.proposals.count(),
        }
        if self.review_mode:
            status = self.review.status(self._clock())
            state["pending"] = status.pending if status.due else 0
        return state

    def _latest(self) -> int:
        return self.state()["latest"]

    def changes(self, params: Mapping[str, Any]) -> dict[str, Any]:
        """The newest changes made by agents (newest first), each with the number to undo it by."""
        since = params.get("since")
        since = since if isinstance(since, int) and not isinstance(since, bool) else 0
        agent_changes = [c for c in self.store.journal(since=since) if not c.by_user]
        agent_changes = [c for c in agent_changes if c.by != APP]
        return {"changes": [self.change_json(c) for c in reversed(agent_changes[-RECENT_CHANGES:])], **self.state()}

    def seen(self) -> int:
        try:
            value = json.loads((self.root / "state.json").read_text(encoding="utf-8")).get("seen", 0)
        except (OSError, ValueError, AttributeError):
            return 0
        return value if isinstance(value, int) and not isinstance(value, bool) else 0

    def mark_seen(self, seq: int | None = None) -> dict[str, Any]:
        latest = self._latest()
        value = latest if seq is None else max(0, min(seq, latest))
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.root / "state.json"
        temporary = path.with_name(path.name + ".tmp")
        temporary.write_text(json.dumps({"seen": value}), encoding="utf-8")
        os.replace(temporary, path)
        return self.state()

    def create(self, params: Mapping[str, Any]) -> dict[str, Any]:
        scope = str(params.get("scope") or "global")
        entry = self.store.create(
            title=str(params.get("title") or ""),
            text=str(params.get("text") or ""),
            scope=scope,
            project=str(params.get("project") or ""),
            kind=str(params.get("kind") or "fact"),
            tags=_tags(params.get("tags")),
            by="user",
        )
        return {"entry": self.entry_json(entry), **self.state()}

    def update(self, params: Mapping[str, Any]) -> dict[str, Any]:
        move_to = str(params.get("project") or "") or None
        entry = self.store.update(
            str(params.get("id") or ""),
            by="user",
            title=_optional(params, "title"),
            text=_optional(params, "text"),
            kind=_optional(params, "kind"),
            tags=_tags(params["tags"]) if "tags" in params else None,
            scope=_optional(params, "scope"),
            move_to=move_to,
        )
        return {"entry": self.entry_json(entry), **self.state()}

    def forget(self, params: Mapping[str, Any]) -> dict[str, Any]:
        return {"entry": self.entry_json(self.store.forget(str(params.get("id") or ""), by="user")), **self.state()}

    def restore(self, params: Mapping[str, Any]) -> dict[str, Any]:
        return {"entry": self.entry_json(self.store.restore(str(params.get("id") or ""), by="user")), **self.state()}

    def undo(self, params: Mapping[str, Any]) -> dict[str, Any]:
        seq = params.get("seq")
        if not isinstance(seq, int) or isinstance(seq, bool):
            raise MemoryRejected("not_found", "There is no such change.")
        return {"entry": self.entry_json(self.store.undo(seq, by="user")), **self.state()}

    # ------------------------------------------------------------------ suggestions

    def proposal_json(self, proposal: Proposal) -> dict[str, Any]:
        return {
            "id": proposal.id,
            "title": proposal.title,
            "text": proposal.text,
            "scope": proposal.scope,
            "project": proposal.project,
            "projectName": Path(proposal.project).name if proposal.project else "",
            "kind": proposal.kind,
            "tags": list(proposal.tags),
            "engine": proposal.engine,
            "conversation": proposal.conversation,
            "rowId": proposal.row,
            "why": proposal.why,
            "at": _ms(proposal.at),
        }

    def suggestions(self, params: Mapping[str, Any]) -> dict[str, Any]:
        return {"proposals": [self.proposal_json(p) for p in self.proposals.pending()], **self.state()}

    def accept(self, params: Mapping[str, Any]) -> dict[str, Any]:
        """Make a suggestion a note (the user may have changed its words, kind or scope first). It is written as the engine
        it came from, which the user has just agreed to: the page does not count it as news."""
        proposal_id = str(params.get("id") or "")
        news_before = self.state()["unseen"]
        edited = self.proposals.edited(
            proposal_id, title=_optional(params, "title"), text=_optional(params, "text"), kind=_optional(params, "kind"),
            scope=_optional(params, "scope"), tags=tuple(_tags(params["tags"]) or ()) if "tags" in params else None,
            project=_optional(params, "project"),
        )
        if edited is not None and edited.scope == "global":
            edited = replace(edited, project="")
        if edited is None:
            raise MemoryRejected("not_found", "That suggestion is gone.")
        try:
            entry = self.store.create(
                title=edited.title, text=edited.text, scope=edited.scope, project=edited.project, kind=edited.kind, tags=edited.tags,
                by=agent(edited.engine),
            )
        except MemoryRejected as rejected:
            if rejected.code == "duplicate":
                self.proposals.settle(proposal_id)  # (it is there already: the suggestion has nothing left to do)
            raise
        self.proposals.settle(proposal_id)
        if news_before == 0:
            self.mark_seen()  # (nothing else was waiting to be looked at: this one, which the user just agreed to, is not news either)
        return {"entry": self.entry_json(entry), **self.state()}

    def dismiss(self, params: Mapping[str, Any]) -> dict[str, Any]:
        self.proposals.dismiss(str(params.get("id") or ""))
        return self.suggestions({})

    def dismiss_all(self, params: Mapping[str, Any]) -> dict[str, Any]:
        self.proposals.dismiss_all()
        return self.suggestions({})

    # ------------------------------------------------------------------ the weekly review (core/memory/review)

    def review_state(self, params: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """Whether a review is due, how many notes wait for it, when the last one was and the next one is (ms, 0: none),
        how many verdicts the learner has, how big a review is, and how many notes were not written for want of
        certainty since the last review."""
        status = self.review.status(self._clock())
        return {
            "due": status.due,
            "pending": status.pending,
            "last": _ms(status.last) if status.last else 0,
            "next": _ms(status.next) if status.next else 0,
            "labels": self.learner.labels,
            "size": REVIEW_SIZE,
            "skipped": status.skipped,
        }

    def review_item_json(self, item: ReviewItem) -> dict[str, Any]:
        score = round(item.score, 2)
        return {
            "id": item.id,
            "title": item.title,
            "text": item.text,
            "kind": item.kind,
            "scope": item.scope,
            "project": item.project,
            "projectName": Path(item.project).name if item.project else "",
            "tags": list(item.tags),
            "engine": item.engine,
            "conversation": item.conversation,
            "row": item.row,
            "why": item.why,
            "score": score,
            "explain": self.learner.explain(item),
            "at": _ms(item.at),
            "suggestKeep": score >= 0.5,
        }

    def review_batch(self, params: Mapping[str, Any]) -> dict[str, Any]:
        """The notes of a review (``size``: at most that many, up to ``REVIEW_SIZE``), the least sure first, in their words as
        they are now."""
        size = params.get("size", REVIEW_SIZE)
        if isinstance(size, bool) or not isinstance(size, int) or not 1 <= size <= REVIEW_SIZE:
            raise MemoryRejected("bad_request", f"size must be a whole number from 1 to {REVIEW_SIZE}.")
        items = self.review.batch(self._clock(), size, store=self.store, learner=self.learner)
        state = self.review_state()
        return {"items": [self.review_item_json(item) for item in items], "total": state["pending"], **state}

    def review_apply(self, params: Mapping[str, Any]) -> dict[str, Any]:
        """The user's word on a review: ``keep`` and ``drop`` (note ids), ``edits`` (``{id: {title?, text?, kind?, scope?,
        tags?}}``, for kept notes). What it did (``kept``, ``dropped``, ``autoKept``, ``autoDropped``, ``labels``, ``left``: the
        notes still waiting, ``next``: ms) with the memory's state."""
        keep, drop, edits = _ids(params, "keep"), _ids(params, "drop"), _edits(params.get("edits"))
        if set(keep) & set(drop):
            raise MemoryRejected("bad_request", "A note cannot be kept and dropped at once.")
        done = self.review.apply(keep, drop, edits, store=self.store, learner=self.learner, by="user", now=self._clock())
        return {
            "kept": done["kept"],
            "dropped": done["dropped"],
            "autoKept": done["autoKept"],
            "autoDropped": done["autoDropped"],
            "labels": done["labels"],
            "left": done["pending"],
            "next": _ms(done["next"]) if done["next"] else 0,
            **self.state(),
        }


def _optional(params: Mapping[str, Any], key: str) -> str | None:
    value = params.get(key)
    return value if isinstance(value, str) else None


def _tags(value: Any) -> Sequence[str] | None:
    return [str(t) for t in value] if isinstance(value, list) else None


def _ids(params: Mapping[str, Any], key: str) -> list[str]:
    value = params.get(key)
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
        raise MemoryRejected("bad_request", f"{key} must be a list of note ids.")
    return value


_EDITABLE = ("title", "text", "kind", "scope", "tags")


def _edits(value: Any) -> dict[str, dict[str, Any]]:
    """The user's changes to the notes of a review, checked: ``{id: {title?, text?, kind?, scope?, tags?}}``."""
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise MemoryRejected("bad_request", "edits must map note ids to changes.")
    out: dict[str, dict[str, Any]] = {}
    for note_id, change in value.items():
        if not isinstance(note_id, str) or not note_id or not isinstance(change, Mapping):
            raise MemoryRejected("bad_request", "edits must map note ids to changes.")
        for key, field in change.items():
            if key not in _EDITABLE:
                raise MemoryRejected("bad_request", f"{key!r} cannot be changed in a review.")
            if key == "tags" and not (isinstance(field, list) and all(isinstance(tag, str) for tag in field)):
                raise MemoryRejected("bad_request", "tags must be a list of words.")
            if key != "tags" and not isinstance(field, str):
                raise MemoryRejected("bad_request", f"{key} must be text.")
        if "kind" in change and change["kind"] not in KINDS:
            raise MemoryRejected("bad_kind", f"kind must be one of {', '.join(KINDS)}.")
        if "scope" in change and change["scope"] not in SCOPES:
            raise MemoryRejected("bad_scope", f"scope must be one of {', '.join(SCOPES)}.")
        out[note_id] = dict(change)
    return out
