"""The notes the application wrote on its own, waiting for the user's look: one batch about once a week.

After a turn the curator (``core/memory/curator``) writes what it judges worth keeping straight into the shared memory,
as the application (``store.APP``), so the agents have it at once; each such note also waits here, in ``review.json``
next to the notes. About once a week (``due``: at least ``DUE_MIN`` notes wait and the oldest of them has waited
``REVIEW_EVERY_DAYS`` since the last review, or so many wait that it cannot wait) the page shows one batch (``batch``:
up to ``REVIEW_SIZE``, the ones the application is least sure of first) and the user keeps or drops them in one go
(``apply``). A dropped note is forgotten, so it can be restored, and its words are remembered (``digests``): it is not
written again.

Every verdict is a lesson for the learner (``core/memory/learn``). Right after a review the learner, which knows more
now, looks at the notes still waiting (``tidy``): one it is quite sure the user would drop is forgotten, one it is quite
sure the user would keep stops waiting. Neither is a lesson: the user did not say it. A note the user forgot on the
memory page in the meantime counts as the user's drop; one the user changed there waits on in its new words.

Only the application's own process writes this file: a lock inside the process and an atomic rename are enough.
"""

from __future__ import annotations

import json
import logging
import math
import os
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, NamedTuple

from desktop.core.memory.learn import Learner, Sample, file_lock
from desktop.core.memory.proposals import ProposalStore, digest_of
from desktop.core.memory.store import APP, Entry, MemoryRejected, MemoryStore

log = logging.getLogger(__name__)

FILE = "review.json"
REVIEW_SIZE = 50
"""The most notes one review shows."""
REVIEW_EVERY_DAYS = 7
DUE_MIN = 5
"""Fewer notes than this waiting is no reason to ask the user."""
DROP_BELOW = 0.15
"""After a review, a waiting note the learner scores below this is forgotten without asking (``tidy``)."""
KEEP_ABOVE = 0.9
"""... and one it scores this or more stops waiting."""
MAX_PENDING = 4 * REVIEW_SIZE
"""With this many waiting the application writes no more of its own (``core/memory/curator``) until the user has looked."""
MAX_DIGESTS = 500
MAX_HISTORY = 26
"""Reviews remembered: half a year of weekly ones."""
KEEP_DECIDED_DAYS = 30
"""How long a decided note stays in the file (what the daily allowance and the near-duplicate check look at)."""
DAY = 86400.0
PENDING, KEPT, DROPPED = "pending", "kept", "dropped"
STATES = (PENDING, KEPT, DROPPED)


def combined(llm: float, learned: float, ready: bool) -> float:
    """How sure the application is that the user keeps a note: the model's own word and, once the learner knows enough
    (``Learner.ready``), the learner's as much."""
    return 0.5 * llm + 0.5 * learned if ready else llm


@dataclass(frozen=True, slots=True)
class ReviewItem:
    """A note the application wrote, as the review knows it."""

    id: str
    """The note's id in the memory."""
    title: str
    text: str
    """The words as the application wrote them (``batch`` gives them as they are now)."""
    tags: tuple[str, ...]
    kind: str
    scope: str
    project: str
    """The project of the conversation it came from (where a global note goes when the user makes it a project's)."""
    engine: str
    """Whose conversation it came from."""
    conversation: str
    """The title of that conversation."""
    row: str
    """The sidebar row it can be opened from ("" when not known)."""
    why: str
    score: float
    """How sure the application is that the user keeps it (``combined``)."""
    llm: float
    """How sure the model that wrote it was."""
    learned: float
    """What the learner said (one half while it knew nothing)."""
    at: float
    state: str = PENDING
    by: str = ""
    """Who decided: ``user``, ``auto`` (the review's own tidying), ``agent`` (an agent forgot the note)."""
    decided_at: float = 0.0
    digest: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "title": self.title, "text": self.text, "tags": list(self.tags), "kind": self.kind, "scope": self.scope, "project": self.project,
            "engine": self.engine, "conversation": self.conversation, "row": self.row, "why": self.why, "score": self.score, "llm": self.llm,
            "learned": self.learned, "at": self.at, "state": self.state, "by": self.by, "decided_at": self.decided_at, "digest": self.digest,
        }

    @classmethod
    def from_json(cls, note_id: str, raw: Mapping[str, Any]) -> ReviewItem | None:
        try:
            title, text, state = str(raw["title"]), str(raw.get("text") or ""), str(raw.get("state") or PENDING)
            if not note_id or not title.strip() or state not in STATES:
                return None
            return cls(
                id=note_id, title=title, text=text, tags=tuple(str(t) for t in raw.get("tags") or ()), kind=str(raw.get("kind") or "fact"),
                scope=str(raw.get("scope") or "project"), project=str(raw.get("project") or ""), engine=str(raw.get("engine") or ""),
                conversation=str(raw.get("conversation") or ""), row=str(raw.get("row") or ""), why=str(raw.get("why") or ""),
                score=_share(raw.get("score")), llm=_share(raw.get("llm")), learned=_share(raw.get("learned")), at=_time(raw.get("at")), state=state,
                by=str(raw.get("by") or ""), decided_at=_time(raw.get("decided_at")), digest=str(raw.get("digest") or digest_of(title, text)),
            )
        except (KeyError, TypeError, ValueError):
            return None


class ReviewStatus(NamedTuple):
    due: bool
    pending: int
    last: float
    """When the user last reviewed (0: never)."""
    next: float
    """When the oldest waiting note will have waited ``REVIEW_EVERY_DAYS`` since the last review (0: nothing waits)."""
    skipped: int
    """Notes the application did not write since the last review: it was not sure enough of them."""


def _time(value: Any) -> float:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value > 0 else 0.0


def _share(value: Any) -> float:
    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
        return min(1.0, max(0.0, float(value)))
    return 0.5


@dataclass
class _State:
    items: dict[str, ReviewItem] = field(default_factory=dict)
    last_review: float = 0.0
    history: list[dict[str, Any]] = field(default_factory=list)
    skipped: int = 0
    digests: list[str] = field(default_factory=list)

    def waiting(self) -> list[ReviewItem]:
        return [item for item in self.items.values() if item.state == PENDING]

    def start(self) -> float | None:
        waiting = self.waiting()
        return max(self.last_review, min(item.at for item in waiting)) if waiting else None

    def due(self, now: float) -> bool:
        count = len(self.waiting())
        if count < DUE_MIN:
            return False
        if count >= 2 * REVIEW_SIZE:
            return True
        start = self.start()
        return start is not None and now - start >= REVIEW_EVERY_DAYS * DAY

    def next(self) -> float:
        start = self.start()
        return 0.0 if start is None else start + REVIEW_EVERY_DAYS * DAY

    def remember(self, *digests: str) -> None:
        for digest in digests:
            if digest and digest not in self.digests:
                self.digests.append(digest)
        del self.digests[:-MAX_DIGESTS]


def _sample(item: ReviewItem, entry: Entry | None = None) -> Sample:
    """What the learner learns from: the note as it is now (``entry``), from the conversation the item came from."""
    words = entry if entry is not None else item
    return Sample(words.title, words.text, tuple(words.tags), words.kind, words.scope, item.engine, words.project)


def _in_memory(store: MemoryStore, note_id: str) -> Entry | None:
    try:
        return store.read(note_id)
    except MemoryRejected:
        return None


def _edit(store: MemoryStore, entry: Entry, change: Mapping[str, Any], item: ReviewItem, by: Any) -> Entry:
    """A kept note with the user's changes (``title``, ``text``, ``kind``, ``scope``, ``tags``); unchanged, no new revision."""
    wanted: dict[str, Any] = {}
    for key in ("title", "text", "kind", "scope"):
        value = change.get(key)
        if isinstance(value, str) and value.strip() != getattr(entry, key):
            wanted[key] = value
    tags = change.get("tags")
    if isinstance(tags, (list, tuple)) and tuple(str(tag) for tag in tags) != tuple(entry.tags):
        wanted["tags"] = [str(tag) for tag in tags]
    if not wanted:
        return entry
    move_to = (item.project or None) if wanted.get("scope") == "project" else None
    return store.update(entry.id, by=by, move_to=move_to, **wanted)


class ReviewStore:
    def __init__(self, root: str | os.PathLike[str]) -> None:
        self.path = Path(root) / FILE
        self._lock = file_lock(self.path)

    # ------------------------------------------------------------------ the file

    def _read(self) -> _State:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError, RecursionError):
            return _State()
        state = _State()
        if not isinstance(raw, Mapping):
            return state
        items = raw.get("items")
        if isinstance(items, Mapping):
            for note_id, value in items.items():
                item = ReviewItem.from_json(note_id, value) if isinstance(note_id, str) and isinstance(value, Mapping) else None
                if item is not None:
                    state.items[note_id] = item
        state.last_review = _time(raw.get("last_review"))
        history = raw.get("history")
        state.history = [dict(entry) for entry in history if isinstance(entry, Mapping)][-MAX_HISTORY:] if isinstance(history, list) else []
        skipped = raw.get("skipped")
        state.skipped = skipped if isinstance(skipped, int) and not isinstance(skipped, bool) and skipped > 0 else 0
        digests = raw.get("digests")
        state.digests = [d for d in digests if isinstance(d, str) and d][-MAX_DIGESTS:] if isinstance(digests, list) else []
        return state

    def _write(self, state: _State, now: float | None = None) -> None:
        if now is not None:
            horizon = now - KEEP_DECIDED_DAYS * DAY
            state.items = {note_id: item for note_id, item in state.items.items() if item.state == PENDING or item.decided_at >= horizon}
        data = {
            "version": 1,
            "items": {note_id: item.to_json() for note_id, item in state.items.items()},
            "last_review": state.last_review,
            "history": state.history[-MAX_HISTORY:],
            "skipped": state.skipped,
            "digests": state.digests[-MAX_DIGESTS:],
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(self.path.name + ".tmp")
        temporary.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        os.replace(temporary, self.path)

    # ------------------------------------------------------------------ what the curator asks

    def add(
        self, entry: Entry, *, engine: str = "", conversation: str = "", row: str = "", why: str = "", project: str | None = None, score: float,
        llm: float, learned: float, now: float | None = None,
    ) -> ReviewItem:
        """Put a note the application has just written up for review (a note that is there already stays as it was).
        ``project``: the conversation's (a global note has none of its own)."""
        stamp = time.time() if now is None else now
        item = ReviewItem(
            entry.id, entry.title, entry.text, tuple(entry.tags), entry.kind, entry.scope, entry.project or (project or ""), engine, conversation, row,
            " ".join(why.split())[:120], _share(score), _share(llm), _share(learned), stamp, digest=digest_of(entry.title, entry.text),
        )
        with self._lock:
            state = self._read()
            if entry.id in state.items:
                return state.items[entry.id]
            state.items[entry.id] = item
            self._write(state, stamp)
            return item

    def skip(self, count: int = 1) -> None:
        """Notes the application was not sure enough of to write (shown with the next review)."""
        if count <= 0:
            return
        with self._lock:
            state = self._read()
            state.skipped += count
            self._write(state)

    def remember(self, digests: Iterable[str]) -> None:
        """Words that are not to be written again (``proposals.digest_of``)."""
        with self._lock:
            state = self._read()
            before = list(state.digests)
            state.remember(*digests)
            if state.digests != before:
                self._write(state)

    def digests(self) -> set[str]:
        with self._lock:
            return set(self._read().digests)

    def counts(self, since: float) -> tuple[int, int]:
        """How many notes were written from ``since`` on, and how many wait for the user."""
        with self._lock:
            state = self._read()
            return sum(1 for item in state.items.values() if item.at >= since), len(state.waiting())

    def recent(self, since: float) -> list[ReviewItem]:
        """Notes written from ``since`` on that the memory does not keep as they are (waiting, or dropped)."""
        with self._lock:
            return [item for item in self._read().items.values() if item.at >= since and item.state != KEPT]

    # ------------------------------------------------------------------ what the page asks

    def get(self, note_id: str) -> ReviewItem | None:
        with self._lock:
            return self._read().items.get(note_id)

    def pending(self) -> list[ReviewItem]:
        """The notes waiting, newest first."""
        with self._lock:
            return sorted(self._read().waiting(), key=lambda item: (item.at, item.id), reverse=True)

    def count_pending(self) -> int:
        with self._lock:
            return len(self._read().waiting())

    def history(self) -> list[dict[str, Any]]:
        """The reviews so far, oldest first: ``{at, kept, dropped, auto_kept, auto_dropped, labels}``."""
        with self._lock:
            return self._read().history

    def due(self, now: float) -> bool:
        with self._lock:
            return self._read().due(now)

    def status(self, now: float) -> ReviewStatus:
        with self._lock:
            state = self._read()
            return ReviewStatus(state.due(now), len(state.waiting()), state.last_review, state.next(), state.skipped)

    def _settle_gone(self, state: _State, item: ReviewItem, entry: Entry | None, now: float) -> tuple[Sample, bool] | None:
        """A waiting note that is not in the memory any more: forgotten by the user (their drop, and a lesson: returned), by
        an agent, or not there at all."""
        if entry is None:
            state.items[item.id] = replace(item, state=DROPPED, by="auto", decided_at=now)
            return None
        by = "user" if entry.editor == "user" else "auto" if entry.editor == APP else "agent"
        state.items[item.id] = replace(item, state=DROPPED, by=by, decided_at=now)
        state.remember(item.digest, digest_of(entry.title, entry.text))
        return (_sample(item, entry), False) if by == "user" else None

    def batch(self, now: float, size: int = REVIEW_SIZE, *, store: MemoryStore | None = None, learner: Learner | None = None) -> list[ReviewItem]:
        """The notes for a review: up to ``size`` of the waiting ones, the least sure first (the oldest first among equals).
        With ``store`` each comes in its words as they are now, and one that was forgotten meanwhile is not shown; with
        ``learner`` too that one is settled (forgotten by the user: a drop, and a lesson) and the scores are made again
        with what the learner knows now."""
        with self._lock:
            state = self._read()
            ready = learner.ready if learner is not None else False
            if learner is not None:
                for item in state.waiting():
                    learned = learner.score(item)
                    state.items[item.id] = replace(item, learned=learned, score=combined(item.llm, learned, ready))
            shown: list[ReviewItem] = []
            lessons: list[tuple[Sample, bool]] = []
            for item in sorted(state.waiting(), key=lambda item: (item.score, item.at, item.id)):
                if len(shown) >= size:
                    break
                if store is None:
                    shown.append(item)
                    continue
                entry = _in_memory(store, item.id)
                if entry is None or not entry.active:
                    if learner is not None:
                        lesson = self._settle_gone(state, item, entry, now)
                        if lesson is not None:
                            lessons.append(lesson)
                    continue
                view = replace(item, title=entry.title, text=entry.text, tags=tuple(entry.tags), kind=entry.kind, scope=entry.scope, project=entry.project)
                if learner is not None:
                    learned = learner.score(_sample(item, entry))
                    view = replace(view, learned=learned, score=combined(item.llm, learned, ready))
                    state.items[item.id] = replace(item, learned=view.learned, score=view.score)
                shown.append(view)
            if learner is not None:
                self._write(state, now)
                if lessons:
                    learner.learn_all(lessons, now=now)
        return sorted(shown, key=lambda item: (item.score, item.at, item.id))

    def apply(
        self, keep: Iterable[str], drop: Iterable[str], edits: Mapping[str, Mapping[str, Any]] | None = None, *, store: MemoryStore,
        learner: Learner, by: Any = "user", now: float | None = None,
    ) -> dict[str, int]:
        """The user's word on a review. ``keep`` and ``drop`` are note ids, ``edits`` the changes to make to a kept note
        first (``title``, ``text``, ``kind``, ``scope``, ``tags``): what the user kept in the end is what is learned. A
        note named in neither, or in both, waits on; an id that is not waiting is passed over. A dropped note is
        forgotten (``by``), and its words are not written again. Then the learner, which has just learned from all of
        it, tidies the notes still waiting (``tidy``).

        A change the memory refuses (an edit that would make a duplicate, say) raises ``MemoryRejected`` before anything
        is marked or learned: doing it all again is harmless.

        Returns ``{kept, dropped, autoKept, autoDropped, labels, pending, next}``: what the user kept and dropped (a note
        the user forgot on the memory page meanwhile counts as dropped), what the tidying did, the lessons learned in all,
        the notes still waiting and when the next review is due (``ReviewStatus.next``, in seconds)."""
        stamp = time.time() if now is None else now
        keep_ids, drop_ids = list(dict.fromkeys(keep)), list(dict.fromkeys(drop))
        torn = set(keep_ids) & set(drop_ids)
        changes = edits or {}
        with self._lock:
            state = self._read()
            lessons: list[tuple[Sample, bool]] = []
            kept = dropped = 0
            alive: dict[str, Entry] = {}
            for note_id in [*keep_ids, *drop_ids]:
                item = state.items.get(note_id)
                if note_id in torn or item is None or item.state != PENDING:
                    continue
                entry = _in_memory(store, note_id)
                if entry is None or not entry.active:
                    lesson = self._settle_gone(state, item, entry, stamp)
                    if lesson is not None:
                        lessons.append(lesson)
                        dropped += 1
                    continue
                alive[note_id] = entry
            for note_id in keep_ids:
                if note_id in alive and changes.get(note_id):
                    alive[note_id] = _edit(store, alive[note_id], changes[note_id], state.items[note_id], by)
            for note_id in drop_ids:
                if note_id in alive:
                    store.forget(note_id, by=by)
            for note_id in keep_ids:
                if note_id in alive:
                    item = state.items[note_id]
                    state.items[note_id] = replace(item, state=KEPT, by="user", decided_at=stamp)
                    lessons.append((_sample(item, alive[note_id]), True))
                    kept += 1
            for note_id in drop_ids:
                if note_id in alive:
                    item, entry = state.items[note_id], alive[note_id]
                    state.items[note_id] = replace(item, state=DROPPED, by="user", decided_at=stamp)
                    state.remember(item.digest, digest_of(entry.title, entry.text))
                    lessons.append((_sample(item, entry), False))
                    dropped += 1
            state.last_review, state.skipped = stamp, 0
            self._write(state, stamp)
            try:
                learner.learn_all(lessons, now=stamp)
            except OSError:
                log.warning("the memory review could not save what it learned", exc_info=True)
            tidied = self.tidy(store, learner, stamp)
            state = self._read()
            labels = learner.labels
            state.history.append(
                {"at": stamp, "kept": kept, "dropped": dropped, "auto_kept": tidied["autoKept"], "auto_dropped": tidied["autoDropped"], "labels": labels}
            )
            self._write(state, stamp)
            return {
                "kept": kept, "dropped": dropped, "autoKept": tidied["autoKept"], "autoDropped": tidied["autoDropped"], "labels": labels,
                "pending": len(state.waiting()), "next": int(state.next()),
            }

    def tidy(self, store: MemoryStore, learner: Learner, now: float) -> dict[str, int]:
        """What the learner, once it knows enough (``Learner.ready``), is quite sure of among the waiting notes: a note it
        scores below ``DROP_BELOW`` is forgotten (by the application; it can be restored) and its words are remembered,
        one from ``KEEP_ABOVE`` stops waiting. Neither is a lesson. The others get their scores again. Returns
        ``{autoKept, autoDropped}``."""
        done = {"autoKept": 0, "autoDropped": 0}
        if not learner.ready:
            return done
        with self._lock:
            state = self._read()
            for item in state.waiting():
                learned = learner.score(item)
                if DROP_BELOW <= learned < KEEP_ABOVE:
                    state.items[item.id] = replace(item, learned=learned, score=combined(item.llm, learned, True))
                    continue
                entry = _in_memory(store, item.id)
                if entry is None or not entry.active:
                    continue  # (the next batch settles it: a note the user forgot is the user's drop, not the learner's)
                learned = learner.score(_sample(item, entry))
                score = combined(item.llm, learned, True)
                if learned < DROP_BELOW:
                    try:
                        store.forget(item.id, by=APP)
                    except MemoryRejected:
                        log.warning("the memory review could not forget a note", exc_info=True)
                        state.items[item.id] = replace(item, learned=learned, score=score)
                        continue
                    state.items[item.id] = replace(item, learned=learned, score=score, state=DROPPED, by="auto", decided_at=now)
                    state.remember(item.digest, digest_of(entry.title, entry.text))
                    done["autoDropped"] += 1
                elif learned >= KEEP_ABOVE:
                    state.items[item.id] = replace(item, learned=learned, score=score, state=KEPT, by="auto", decided_at=now)
                    done["autoKept"] += 1
                else:
                    state.items[item.id] = replace(item, learned=learned, score=score)
            self._write(state, now)
        return done


def remembered(learner: Learner, entry: Entry, engine: str = "", *, now: float | None = None) -> None:
    """A note the user asked for in so many words (``core/memory/capture``): the user keeps notes like it. A lesson, and
    nothing to review: the user has said it."""
    learner.learn(Sample(entry.title, entry.text, tuple(entry.tags), entry.kind, entry.scope, engine, entry.project), True, now=now)


def migrate_proposals(proposals: ProposalStore, store: MemoryStore, review: ReviewStore, learner: Learner, now: float | None = None) -> int:
    """From asking about every suggestion to writing on its own: each suggestion still waiting becomes a note (by the
    application) waiting for the review, and leaves the list without its words being remembered as refused (nobody
    refused it). The words the user did refuse are carried over. Doing it again does nothing. Returns how many moved."""
    stamp = time.time() if now is None else now
    review.remember(proposals.dismissed())
    ready = learner.ready
    moved = 0
    for proposal in proposals.pending():
        try:
            entry = store.create(
                title=proposal.title, text=proposal.text, scope=proposal.scope, project=proposal.project, kind=proposal.kind, tags=proposal.tags,
                by=APP,
            )
        except MemoryRejected as rejected:
            if rejected.code in ("limit", "busy"):
                continue  # (it can move another time)
            found = rejected.entry
            if rejected.code != "duplicate" or found is None or found.source != APP or review.get(found.id) is not None:
                proposals.discard(proposal.id)
                continue
            entry = found  # (written by a move that stopped half way)
        learned = learner.score(proposal)
        review.add(
            entry, engine=proposal.engine, conversation=proposal.conversation, row=proposal.row, why=proposal.why,
            score=combined(0.5, learned, ready), llm=0.5, learned=learned, now=proposal.at or stamp,
        )
        proposals.discard(proposal.id)
        moved += 1
    return moved
