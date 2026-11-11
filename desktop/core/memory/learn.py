"""What the user keeps: a small classifier learned from the user's own word on the notes the application wrote.

The application writes notes on its own after a turn (``core/memory/curator``) and about once a week shows the user what
it wrote, to keep or drop (``core/memory/review``). Every verdict is a labelled example, and so is a note the user asked
for in so many words ("记住…", ``core/memory/capture``: kept). This module learns from them, so that a note like the
ones the user dropped scores low the next time and one like those kept scores high. It is meant to be checked by hand:
every number in ``learned.json`` counts notes.

**What a note is made of** (``features``): the Latin words of its title and text (two letters or more) and, in Chinese,
Japanese or Korean text, which has no spaces, its pairs of characters (one character says too little); a few everyday
words (the, is, 的, 了 …) are left out. Its kind, scope, engine and tags are features too (``kind:pitfall``,
``tag:commit``). A feature counts once per note.

**The score** (``score``) is a naive Bayes guess at the chance that the user keeps the note: the odds of the verdicts so
far, times, for each feature of the note seen before, how much more often the kept notes had it than the dropped ones
(every count one higher, so that nothing is impossible). A feature never seen says nothing. Overlapping pairs of
characters are not independent of each other (提交信息 is 提交, 交信 and 信息), so a note with more than ``EVIDENCE``
known features counts as that many, at their average: a long note is not surer than a short one. While there are few
verdicts the guess is pulled towards one half, ``(n * p + K / 2) / (n + K)`` with ``K = PRIOR_WEIGHT``: three verdicts
decide nothing.

Only the application's own process writes the file: a lock inside the process and an atomic rename are enough.
"""

from __future__ import annotations

import json
import math
import os
import re
import threading
import time
import unicodedata
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, NamedTuple

from desktop.core.memory.store import KINDS, MAX_TAG, SCOPES, _normal_title, _same_subject, same_project

FILE = "learned.json"
MIN_LABELS = 20
"""Verdicts it takes before the learner has a say (``ready``): in whether a note is written, and in tidying a review."""
PRIOR_WEIGHT = 10
"""How many verdicts the even guess (one half) weighs against what was learned."""
EVIDENCE = 10
"""The most features' worth of evidence one note gives (see above)."""
MAX_TERMS = 4000
"""Features remembered; past this the rarest go, and of those the ones not seen for longest."""
MAX_EXAMPLES = 100
"""Notes remembered for each verdict (``examples``: shown to the model that picks what to keep)."""
EXAMPLE_CHARS = 200
REASONS = 12
"""Features the reasons of a score are made of (``explain``): the ones that moved it most."""

_WORD = re.compile(r"[a-z0-9]+")
_CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]+")
_BREAK = re.compile(r"[的了吗呢吧啊呀哦嘛么]")
"""Characters that only hold a sentence together: a run of characters is cut at them, and no pair spans one."""
STOP_WORDS = frozenset(
    "an the is am are was were be been being to of in on at by for from with into as and or but nor so if then than "
    "it its this that these those there here do does did done has have had can could will would shall may might "
    "i me my we our us you your he him his she her they them their what which who whom whose how when where why "
    "also just only very too about over out up down off all any some each such same other more most much many few "
    "own get got let yes".split()
)
STOP_PAIRS = frozenset(
    "这个 那个 这些 那些 这样 那样 这里 那里 一个 一些 一下 可以 我们 你们 他们 什么 怎么 如何 已经 然后 因为 所以 "
    "如果 但是 还是 就是 不是 没有 现在 自己 进行 或者 以及 而且 并且".split()
)
_KIND_WORDS = {"preference": "偏好", "convention": "约定", "pitfall": "坑", "fact": "事实"}
_SCOPE_WORDS = {"global": "全局", "project": "项目"}
_ENGINE_NAMES = {"droid": "Droid", "codex": "Codex", "devin": "Devin", "chatgpt": "ChatGPT Plan"}


@dataclass(frozen=True, slots=True)
class Sample:
    """A note as the learner reads it."""

    title: str
    text: str
    tags: tuple[str, ...] = ()
    kind: str = ""
    scope: str = ""
    engine: str = ""
    """The engine of the conversation it came from (for an ``Entry``: its author's)."""
    project: str = ""


class Example(NamedTuple):
    """A note the user kept or dropped, as the model that picks what to keep is shown it."""

    title: str
    text: str
    kind: str
    scope: str
    project: str
    at: float


_LOCKS: dict[str, Any] = {}
_LOCKS_GUARD = threading.Lock()


def file_lock(path: Path) -> Any:
    """The lock of one file for every object in this process that reads and writes it: two learners (or reviews) of one
    memory folder must not each write what the other has not read."""
    key = os.path.normcase(os.path.abspath(path))
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(key, threading.RLock())


def _field(candidate: Any, key: str) -> Any:
    return candidate.get(key) if isinstance(candidate, Mapping) else getattr(candidate, key, None)


def sample_of(candidate: Any) -> Sample:
    """Anything with ``title``, ``text``, ``tags``, ``kind``, ``scope`` and ``engine`` (a review item, a ``Proposal``, an
    ``Entry``, whose engine is its author's), or a mapping with those keys; ``project`` when there is one."""
    if isinstance(candidate, Sample):
        return candidate
    engine = _field(candidate, "engine")
    if not engine:
        source = _field(candidate, "source")
        engine = source.get("engine") if isinstance(source, Mapping) else ""
    tags = _field(candidate, "tags")
    return Sample(
        title=str(_field(candidate, "title") or ""),
        text=str(_field(candidate, "text") or ""),
        tags=tuple(str(tag) for tag in tags) if isinstance(tags, (list, tuple)) else (),
        kind=str(_field(candidate, "kind") or ""),
        scope=str(_field(candidate, "scope") or ""),
        engine=str(engine or ""),
        project=str(_field(candidate, "project") or ""),
    )


def terms(text: str) -> set[str]:
    """The words of a text as the learner counts them (``features`` adds kind, scope, engine and tags)."""
    flat = unicodedata.normalize("NFKC", text).casefold()
    found = {word for word in _WORD.findall(flat) if len(word) >= 2 and not word.isdigit() and word not in STOP_WORDS}
    for run in _CJK.findall(flat):
        for part in _BREAK.split(run):
            found.update(pair for pair in (part[i : i + 2] for i in range(len(part) - 1)) if pair not in STOP_PAIRS)
    return found


def features(candidate: Any) -> set[str]:
    """What the learner counts of a note: the words of its title and text, its kind, scope, engine and tags."""
    sample = sample_of(candidate)
    found = terms(f"{sample.title}\n{sample.text}")
    if sample.kind in KINDS:
        found.add(f"kind:{sample.kind}")
    if sample.scope in SCOPES:
        found.add(f"scope:{sample.scope}")
    if sample.engine.strip():
        found.add(f"engine:{sample.engine.strip().casefold()}")
    for tag in sample.tags:
        flat = " ".join(unicodedata.normalize("NFKC", tag).casefold().split())[:MAX_TAG]
        if flat:
            found.add(f"tag:{flat}")
    return found


def _label(term: str) -> str:
    prefix, colon, rest = term.partition(":")
    if not colon:
        return term
    if prefix == "kind":
        return _KIND_WORDS.get(rest, rest)
    if prefix == "scope":
        return _SCOPE_WORDS.get(rest, rest)
    if prefix == "engine":
        return _ENGINE_NAMES.get(rest, rest)
    return rest


def _sigmoid(x: float) -> float:
    if x >= 0:
        return 1.0 / (1.0 + math.exp(-x))
    z = math.exp(x)
    return z / (1.0 + z)


@dataclass
class _Model:
    kept: int = 0
    dropped: int = 0
    seq: int = 0
    """Verdicts learned so far, ever: how long ago a feature was last seen is counted in these."""
    terms: dict[str, list[int]] = field(default_factory=dict)
    """feature -> [kept notes that had it, dropped notes that had it, ``seq`` of the last verdict that had it]"""
    examples: dict[str, list[dict[str, Any]]] = field(default_factory=lambda: {"kept": [], "dropped": []})
    """``kept`` / ``dropped`` -> the newest notes of that verdict, oldest first."""


def _whole(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def _example(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, Mapping) or not isinstance(raw.get("title"), str) or not isinstance(raw.get("text"), str):
        return None
    at = raw.get("at")
    return {
        "title": raw["title"], "text": raw["text"][:EXAMPLE_CHARS], "kind": str(raw.get("kind") or ""), "scope": str(raw.get("scope") or ""),
        "project": str(raw.get("project") or ""), "at": float(at) if isinstance(at, (int, float)) and not isinstance(at, bool) else 0.0,
    }


def _read_model(raw: Any) -> _Model:
    """What a file says, as far as it can be read: anything out of shape is left out."""
    model = _Model()
    if not isinstance(raw, Mapping):
        return model
    model.kept, model.dropped, model.seq = _whole(raw.get("kept")), _whole(raw.get("dropped")), _whole(raw.get("seq"))
    counts = raw.get("terms")
    if isinstance(counts, Mapping):
        for term, value in counts.items():
            if not isinstance(term, str) or not term or not isinstance(value, list) or len(value) < 2:
                continue
            kept, dropped = min(_whole(value[0]), model.kept), min(_whole(value[1]), model.dropped)
            if kept or dropped:
                model.terms[term] = [kept, dropped, _whole(value[2]) if len(value) > 2 else 0]
    model.seq = max([model.seq, *(value[2] for value in model.terms.values())])
    examples = raw.get("examples")
    if isinstance(examples, Mapping):
        for verdict in ("kept", "dropped"):
            shelf = examples.get(verdict)
            if isinstance(shelf, list):
                model.examples[verdict] = [e for e in map(_example, shelf) if e is not None][-MAX_EXAMPLES:]
    return model


def _weigh(model: _Model, found: Iterable[str]) -> tuple[float, dict[str, float]]:
    """The log of the odds that a note with these features is kept, and what each feature the learner knows added."""
    kept, dropped = model.kept, model.dropped
    weights: dict[str, float] = {}
    for term in found:
        counts = model.terms.get(term)
        if counts is not None:
            weights[term] = math.log((counts[0] + 1) / (kept + 2)) - math.log((counts[1] + 1) / (dropped + 2))
    evidence = math.fsum(weights.values())
    if len(weights) > EVIDENCE:
        evidence *= EVIDENCE / len(weights)
    return math.log((kept + 1) / (dropped + 1)) + evidence, weights


def _prune(model: _Model) -> None:
    extra = len(model.terms) - MAX_TERMS
    if extra > 0:
        for term, _ in sorted(model.terms.items(), key=lambda item: (item[1][0] + item[1][1], item[1][2], item[0]))[:extra]:
            del model.terms[term]


class Learner:
    def __init__(self, root: str | os.PathLike[str], *, clock: Callable[[], float] = time.time) -> None:
        self.path = Path(root) / FILE
        self._clock = clock
        self._lock = file_lock(self.path)
        self._cache: _Model | None = None
        self._stamp: tuple[int, int, int] | None = None

    # ------------------------------------------------------------------ the file

    def _model(self) -> _Model:
        """The model as the file has it (read again only when the file changed)."""
        try:
            stat = self.path.stat()
            stamp: tuple[int, int, int] | None = (stat.st_ino, stat.st_mtime_ns, stat.st_size)
        except OSError:
            stamp = None
        if self._cache is None or stamp != self._stamp:
            model = _Model()
            if stamp is not None:
                try:
                    model = _read_model(json.loads(self.path.read_text(encoding="utf-8")))
                except (OSError, ValueError, RecursionError):
                    model = _Model()
            self._cache, self._stamp = model, stamp
        return self._cache

    def _save(self, model: _Model) -> None:
        data = {"version": 1, "kept": model.kept, "dropped": model.dropped, "seq": model.seq, "terms": model.terms, "examples": model.examples}
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_name(self.path.name + ".tmp")
            temporary.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
            os.replace(temporary, self.path)
            stat = self.path.stat()
        except BaseException:
            self._cache = self._stamp = None  # (the model in hand was changed and not written: read the file again)
            raise
        self._cache, self._stamp = model, (stat.st_ino, stat.st_mtime_ns, stat.st_size)

    # ------------------------------------------------------------------ what it knows

    def counts(self) -> tuple[int, int]:
        """Verdicts learned: (kept, dropped)."""
        with self._lock:
            model = self._model()
            return model.kept, model.dropped

    @property
    def labels(self) -> int:
        kept, dropped = self.counts()
        return kept + dropped

    @property
    def ready(self) -> bool:
        """Whether it has learned enough to have a say (``MIN_LABELS``)."""
        return self.labels >= MIN_LABELS

    def score(self, candidate: Any) -> float:
        """The chance that the user keeps a note like this one: strictly between 0 and 1, one half while nothing is learned."""
        found = features(candidate)
        with self._lock:
            model = self._model()
            labels = model.kept + model.dropped
            if not labels:
                return 0.5
            odds, _ = _weigh(model, found)
        return (labels * _sigmoid(odds) + PRIOR_WEIGHT * 0.5) / (labels + PRIOR_WEIGHT)

    def explain(self, candidate: Any, top: int = 3) -> list[str]:
        """What in a note moved its score most (towards keeping when the score is one half or more, else towards dropping),
        in words the user reads: a run of Chinese characters rather than its pairs, 约定 rather than ``kind:convention``."""
        sample = sample_of(candidate)
        found = features(sample)
        with self._lock:
            model = self._model()
            if top <= 0 or not model.kept + model.dropped:
                return []
            odds, weights = _weigh(model, found)
        side = 1.0 if odds >= 0 else -1.0
        pushing = sorted(((weight * side, term) for term, weight in weights.items() if weight * side > 0), reverse=True)
        pool = {term: push for push, term in pushing[: max(REASONS, top * 4)]}
        phrases: list[tuple[float, str]] = []
        merged: set[str] = set()
        for run in _CJK.findall(unicodedata.normalize("NFKC", f"{sample.title}\n{sample.text}").casefold()):
            for part in _BREAK.split(run):
                start = 0
                while start < len(part) - 1:
                    end = start
                    while end < len(part) - 1 and part[end : end + 2] in pool:
                        end += 1
                    if end == start:
                        start += 1
                        continue
                    pairs = {part[i : i + 2] for i in range(start, end)}
                    phrases.append((math.fsum(pool[pair] for pair in pairs), part[start : end + 1]))
                    merged |= pairs
                    start = end
        ranked = phrases + [(push, _label(term)) for term, push in pool.items() if term not in merged]
        out: list[str] = []
        for _, label in sorted(ranked, key=lambda item: -item[0]):
            if label in out or (_CJK.fullmatch(label) and any(label in other for other in out)):
                continue
            out.append(label)
            if len(out) >= top:
                break
        return out

    def examples(self, kept: bool, limit: int = 5, *, project: str | None = None) -> list[Example]:
        """The newest notes the user kept (or dropped), newest first, no two with nearly the same title. ``project``: only
        those a conversation of that project may see (the global ones and the project's own; ``""``: the global ones);
        ``None``: all of them."""
        with self._lock:
            shelf = list(self._model().examples["kept" if kept else "dropped"])
        out: list[Example] = []
        titles: list[str] = []
        for raw in reversed(shelf):
            if len(out) >= limit:
                break
            if project is not None and raw["scope"] != "global" and not same_project(raw["project"], project):
                continue
            title = _normal_title(raw["title"])
            if any(_same_subject(title, other) for other in titles):
                continue
            titles.append(title)
            out.append(Example(raw["title"], raw["text"], raw["kind"], raw["scope"], raw["project"], raw["at"]))
        return out

    # ------------------------------------------------------------------ learning

    def learn(self, candidate: Any, kept: bool, *, now: float | None = None) -> None:
        """One verdict: the user kept (or dropped) this note."""
        self.learn_all([(candidate, kept)], now=now)

    def learn_all(self, verdicts: Iterable[tuple[Any, bool]], *, now: float | None = None) -> int:
        """Several verdicts (a note, and whether the user kept it), written once. Returns how many there were."""
        stamp = self._clock() if now is None else now
        lessons = [(sample_of(candidate), bool(kept)) for candidate, kept in verdicts]
        if not lessons:
            return 0
        with self._lock:
            model = self._model()
            try:
                for sample, kept in lessons:
                    model.seq += 1
                    if kept:
                        model.kept += 1
                    else:
                        model.dropped += 1
                    column = 0 if kept else 1
                    for term in features(sample):
                        counts = model.terms.setdefault(term, [0, 0, 0])
                        counts[column] += 1
                        counts[2] = model.seq
                    shelf = model.examples["kept" if kept else "dropped"]
                    shelf.append(
                        {"title": sample.title, "text": sample.text[:EXAMPLE_CHARS], "kind": sample.kind, "scope": sample.scope, "project": sample.project, "at": stamp}
                    )
                    del shelf[:-MAX_EXAMPLES]
                _prune(model)
            except BaseException:
                self._cache = None
                raise
            self._save(model)
        return len(lessons)

    def forget(self, candidate: Any, kept: bool) -> bool:
        """Take back a verdict learned before (the same note, the same verdict). ``False``: the note is not among the last
        ``MAX_EXAMPLES`` of that verdict, which are the only ones it can still tell apart."""
        sample = sample_of(candidate)
        found = features(sample)
        verdict = "kept" if kept else "dropped"
        with self._lock:
            model = self._model()
            shelf = model.examples[verdict]
            text = sample.text[:EXAMPLE_CHARS]
            index = next((i for i in range(len(shelf) - 1, -1, -1) if shelf[i]["title"] == sample.title and shelf[i]["text"] == text), None)
            if index is None or not (model.kept if kept else model.dropped):
                return False
            del shelf[index]
            if kept:
                model.kept -= 1
            else:
                model.dropped -= 1
            column = 0 if kept else 1
            for term in found:
                counts = model.terms.get(term)
                if counts is not None and counts[column]:
                    counts[column] -= 1
            for term, counts in list(model.terms.items()):
                counts[0], counts[1] = min(counts[0], model.kept), min(counts[1], model.dropped)
                if not counts[0] and not counts[1]:
                    del model.terms[term]
            self._save(model)
            return True
