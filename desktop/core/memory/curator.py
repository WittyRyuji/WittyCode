"""After a turn: what in it is worth keeping?

The application asks an engine (``Extractor``: one small request that leaves nothing behind in the user's conversation
lists) to read the exchange that just ended and say what the user would want remembered in later conversations: a stated
preference, a rule of the project, a pitfall and its way out.

What comes back is kept one of two ways. Without a ``ReviewStore`` it goes to ``ProposalStore`` as suggestions the user
confirms one by one on the memory page; nothing is written to the memory without that word. With one
(``core/memory/review``) the application writes what is worth it itself, as the application (``store.APP``), and the
note waits for the weekly review: the model says how sure it is (``confidence``), the learner (``core/memory/learn``)
has as much say once it has enough of the user's verdicts, and below ``WRITE_AT`` nothing is written. The prompt then
also shows the model a few notes the user kept and dropped before.

Cost control, because this is a request per turn: only turns that said something (``MIN_USER`` / ``MIN_ASSISTANT``), at
most one request in flight, one per conversation every ``GAP_SECONDS``, ``PER_HOUR`` in all. When the application
writes itself, at most ``AUTO_PER_DAY`` notes in any 24 hours, and none (and no request) while ``review.MAX_PENDING``
notes wait for the user.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import re
import time
from collections import deque
from collections.abc import Awaitable, Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from desktop.core.memory.learn import Learner, Sample, terms
from desktop.core.memory.proposals import Proposal, ProposalStore, digest_of, make
from desktop.core.memory.review import DAY, MAX_PENDING, ReviewStore, combined
from desktop.core.memory.safety import find_secret
from desktop.core.memory.store import (
    APP, KINDS, MAX_TAG, MAX_TAGS, MAX_TEXT, MAX_TITLE, SCOPES, Entry, MemoryRejected, MemoryStore, _normal_title, _same_subject, project_path,
)

log = logging.getLogger(__name__)

MIN_USER = 8
MIN_ASSISTANT = 40
GAP_SECONDS = 90.0
PER_HOUR = 20
MAX_NOTES = 3
EXCHANGE_CHARS = 6000
"""How much of the exchange the model reads (the start of the user's words, the end of the answer)."""
KNOWN_TITLES = 40
WRITE_AT = 0.55
"""How sure the application must be that the user keeps a note (``review.combined``) to write it."""
AUTO_PER_DAY = 20
"""Notes the application writes on its own in any 24 hours: a burst of turns cannot flood the memory."""
NEAR_DUPLICATE = 0.7
"""Words in common (Jaccard, title and text) from which a note is the same as one the memory has, or as one the
application wrote lately (``RECENT_DAYS``) that the user dropped or has not looked at yet."""
RECENT_DAYS = 14
EXAMPLES = 5
"""Notes the user kept, and as many dropped, shown to the model."""
EXAMPLE_TEXT = 120

Extractor = Callable[[str, str, str], Awaitable["str | None"]]
"""``(engine, project, prompt)`` -> the model's reply; ``None``: this engine cannot be asked right now."""

INSTRUCTIONS = (
    "You keep the long-term memory of a developer's coding agents. You are shown the latest exchange of one conversation. "
    "Decide whether it holds something worth remembering in FUTURE conversations: a preference or working rule the user "
    "stated, a convention of this project, a pitfall that cost time together with its way out, or a fact that is not "
    "obvious from the code. Do NOT note: anything that only matters for this task, what the code or the git history "
    "already says, secrets (keys, tokens, passwords), guesses, or what the existing notes below already cover. Be strict: "
    "most exchanges hold nothing worth keeping, and then the answer is an empty list. Each note is short and "
    "self-contained (it will be read without the conversation)."
)

FORMAT = (
    'Reply with ONLY a JSON object, no other text: {"notes": [{"title": "a few words", "text": "the point in one to three '
    'sentences, with the reason when it is not obvious", "kind": "preference|convention|pitfall|fact", '
    '"scope": "project|global", "tags": ["words someone would use when asking about it"], "why": "a short phrase: what makes '
    'it worth keeping"}]} with at most 3 notes. Use scope "global" only for what holds for the user in every project. '
    "Write in the language the user wrote in."
)

FORMAT_SURE = (
    'Reply with ONLY a JSON object, no other text: {"notes": [{"title": "a few words", "text": "the point in one to three '
    'sentences, with the reason when it is not obvious", "kind": "preference|convention|pitfall|fact", '
    '"scope": "project|global", "tags": ["words someone would use when asking about it"], "why": "a short phrase: what makes '
    'it worth keeping", "confidence": 0.8}]} with at most 3 notes. "confidence" is a number from 0 to 1: how sure you are '
    "that the user would want the note kept (notes are saved at once; the user looks over them later). Use scope "
    '"global" only for what holds for the user in every project. Write in the language the user wrote in.'
)


@dataclass(frozen=True, slots=True)
class Exchange:
    """The turn that just ended, as far as the curator needs it."""

    engine: str
    project: str
    conversation: str
    """The conversation's title."""
    row: str
    """The sidebar row id ("" when not known)."""
    user: str
    assistant: str
    key: str = ""
    """What tells conversations apart for the one-per-``GAP_SECONDS`` rule (the row id, else the project)."""


@dataclass(frozen=True, slots=True)
class _Note:
    """A note of the model's reply that is fit to keep."""

    title: str
    text: str
    kind: str
    scope: str
    tags: list[str]
    why: str
    raw: dict[str, Any]


def _example_line(example: Any) -> str:
    title = " ".join(str(example.title).split())
    text = " ".join(str(example.text).split())
    if len(text) > EXAMPLE_TEXT:
        text = text[: EXAMPLE_TEXT - 1] + "…"
    return f"- {title}: {text}" if text else f"- {title}"


def _taste(kept: Sequence[Any], dropped: Sequence[Any]) -> str:
    """What the user decided about earlier notes (``Learner.examples``), for the model to follow."""
    if not kept and not dropped:
        return ""
    lines = ["What this user decided about earlier notes (follow the same taste):"]
    if kept:
        lines += ["The user kept notes like:", *map(_example_line, kept)]
    if dropped:
        lines += ["The user dropped notes like:", *map(_example_line, dropped)]
    return "\n".join(lines) + "\n\n"


def build_prompt(
    exchange: Exchange, known: Iterable[str], *, kept: Sequence[Any] = (), dropped: Sequence[Any] = (), confidence: bool = False
) -> str:
    """The prompt: the instructions, the titles of the notes there already are, what the user kept and dropped before
    (``kept`` / ``dropped``: examples with a title and a text), the format (``confidence``: each note says how sure the
    model is), the exchange."""
    user, answer = exchange.user.strip(), exchange.assistant.strip()
    room = EXCHANGE_CHARS
    if len(user) + len(answer) > room:
        share = room // 3
        user = user if len(user) <= share else user[:share] + " …"
        answer = answer if len(answer) <= room - len(user) else "… " + answer[-(room - len(user)) :]
    titles = [t for t in known if t.strip()][:KNOWN_TITLES]
    notes = "\n".join(f"- {t}" for t in titles) if titles else "(none yet)"
    name = Path(exchange.project).name if exchange.project else "(no project)"
    return (
        f"{INSTRUCTIONS}\n\nProject: {name}\n\nExisting notes (titles):\n{notes}\n\n{_taste(kept, dropped)}{FORMAT_SURE if confidence else FORMAT}\n\n"
        f"<exchange>\nUSER:\n{user}\n\nASSISTANT:\n{answer}\n</exchange>"
    )


def parse_reply(reply: str) -> list[dict[str, Any]]:
    """The notes in the model's reply (``[]``: none, or it could not be read)."""
    text = reply.strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fenced:
        text = fenced.group(1).strip()
    for opening, closing in (("{", "}"), ("[", "]")):
        start, end = text.find(opening), text.rfind(closing)
        if start < 0 or end <= start:
            continue
        try:
            data = json.loads(text[start : end + 1])
        except ValueError:
            continue
        items = data.get("notes") if isinstance(data, dict) else data
        if isinstance(items, list):
            return [item for item in items if isinstance(item, dict)]
    return []


def confidence_of(value: Any) -> float:
    """How sure the model said it is, between 0 and 1 (a number, ``"0.8"``, ``"80%"``); one half when it did not say."""
    if isinstance(value, bool):
        return 0.5
    if isinstance(value, str):
        text = value.strip()
        try:
            number = float(text[:-1]) / 100 if text.endswith("%") else float(text)
        except ValueError:
            return 0.5
    elif isinstance(value, (int, float)):
        number = float(value)
    else:
        return 0.5
    return min(1.0, max(0.0, number)) if math.isfinite(number) else 0.5


def _tags(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for tag in value:
        text = " ".join(str(tag).split())[:MAX_TAG].strip()
        if text and text not in out:
            out.append(text)
    return out[:MAX_TAGS]


def _jaccard(a: set[str], b: set[str]) -> float:
    union = a | b
    return len(a & b) / len(union) if union else 0.0


def _gist(title: str, text: str) -> tuple[set[str], list[str]]:
    """What a near-duplicate is told by: the words, and the numbers in the title."""
    return terms(f"{title}\n{text}"), re.findall(r"\d+", title)


def _same(a: tuple[set[str], list[str]], b: tuple[set[str], list[str]]) -> bool:
    """Whether two notes say the same thing: most of their words in common, and (as in the memory) not two titles with
    other numbers, ``Step 1`` and ``Step 2``; a title without numbers is told by its words alone."""
    if a[1] and b[1] and a[1] != b[1]:
        return False
    return _jaccard(a[0], b[0]) >= NEAR_DUPLICATE


class Curator:
    def __init__(
        self,
        store: MemoryStore,
        proposals: ProposalStore,
        extract: Extractor,
        *,
        enabled: Callable[[str], bool] = lambda engine: True,
        changed: Callable[[], None] = lambda: None,
        clock: Callable[[], float] = time.time,
        review: ReviewStore | None = None,
        learner: Learner | None = None,
    ) -> None:
        self.store = store
        self.proposals = proposals
        self.extract = extract
        self.enabled = enabled
        self.changed = changed
        self._clock = clock
        self.review = review
        """Where the notes the application writes on its own wait for the user's look; ``None``: it only suggests."""
        self.learner = learner if learner is not None or review is None else Learner(review.path.parent)
        """What the user's verdicts taught (only with ``review``)."""
        self._busy = False
        self._last: dict[str, float] = {}
        self._asked: deque[float] = deque()

    def worth_asking(self, exchange: Exchange) -> bool:
        """Whether this turn is worth a request (the cheap rules; ``consider`` also applies the rate limits)."""
        return (
            self.enabled(exchange.engine)
            and len(exchange.user.strip()) >= MIN_USER
            and len(exchange.assistant.strip()) >= MIN_ASSISTANT
        )

    def _allowed(self, exchange: Exchange) -> bool:
        now = self._clock()
        while self._asked and now - self._asked[0] > 3600:
            self._asked.popleft()
        if self._busy or len(self._asked) >= PER_HOUR:
            return False
        last = self._last.get(exchange.key or exchange.project)
        return last is None or now - last >= GAP_SECONDS

    def _room(self) -> int:
        """How many notes the application may still write on its own now (the day's allowance, the notes waiting for the
        user); without ``review`` there is no such limit."""
        if self.review is None:
            return MAX_NOTES
        written, waiting = self.review.counts(self._clock() - DAY)
        return max(0, min(MAX_NOTES, AUTO_PER_DAY - written, MAX_PENDING - waiting))

    async def consider(self, exchange: Exchange) -> list[Proposal] | list[Entry]:
        """Ask about the exchange and keep what is worth it: the suggestions made, or (with ``review``) the notes written.
        Never raises: a failed request is only logged."""
        if not self.worth_asking(exchange) or not self._allowed(exchange):
            return []
        try:
            if not self._room():
                return []  # (nothing could be written: no request)
        except Exception:
            log.warning("the memory curator could not read the review", exc_info=True)
            return []
        self._busy = True
        now = self._clock()
        self._last[exchange.key or exchange.project] = now
        self._asked.append(now)
        try:
            known = [c.title for c in self.store.candidates(exchange.project or None)]
            reply = await self.extract(exchange.engine, exchange.project, self._prompt(exchange, known))
            if not reply:
                return []
            kept = self.proposals.add(self._suggestions(reply, exchange)) if self.review is None else self._record(reply, exchange)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.warning("the memory curator could not ask about a turn", exc_info=True)
            return []
        finally:
            self._busy = False
        if kept:
            self.changed()
        return kept

    def _prompt(self, exchange: Exchange, known: list[str]) -> str:
        if self.review is None or self.learner is None:
            return build_prompt(exchange, known)
        return build_prompt(
            exchange, known, kept=self.learner.examples(True, EXAMPLES, project=exchange.project),
            dropped=self.learner.examples(False, EXAMPLES, project=exchange.project), confidence=True,
        )

    def _notes(self, reply: str, exchange: Exchange, out: list[Any]) -> Iterator[_Note]:
        """The notes of the reply fit to keep, until ``out`` (what the caller made of them) holds ``MAX_NOTES``: not a
        secret, not too long, not what the memory has already."""
        existing = self.store.candidates(exchange.project or None)
        for item in parse_reply(reply)[: MAX_NOTES * 3]:
            if len(out) >= MAX_NOTES:
                break
            title = " ".join(str(item.get("title") or "").split())
            text = str(item.get("text") or "").strip()
            if not title or not text or len(title) > MAX_TITLE or len(text) > MAX_TEXT or find_secret(title, text):
                continue
            kind = str(item.get("kind") or "fact")
            scope = str(item.get("scope") or "project")
            kind = kind if kind in KINDS else "fact"
            scope = scope if scope in SCOPES else "project"
            if scope == "project" and not exchange.project:
                scope = "global"
            wanted = _normal_title(title)
            if any(c.text.strip() == text or _same_subject(wanted, _normal_title(c.title)) for c in existing):
                continue
            yield _Note(title, text, kind, scope, _tags(item.get("tags")), str(item.get("why") or ""), item)

    def _suggestions(self, reply: str, exchange: Exchange) -> list[Proposal]:
        out: list[Proposal] = []
        for note in self._notes(reply, exchange, out):
            out.append(
                make(
                    title=note.title, text=note.text, scope=note.scope, project=exchange.project, kind=note.kind, tags=note.tags,
                    engine=exchange.engine, conversation=exchange.conversation, row=exchange.row, why=note.why, now=self._clock(),
                )
            )
        return out

    def _record(self, reply: str, exchange: Exchange) -> list[Entry]:
        """Write what the application is sure enough the user keeps, and put it up for review."""
        review, learner = self.review, self.learner
        assert review is not None and learner is not None
        now = self._clock()
        room = self._room()
        refused = review.digests() | set(self.proposals.dismissed())
        known = [_gist(c.title, c.text) for c in self.store.candidates(exchange.project or None)]
        known += [_gist(item.title, item.text) for item in review.recent(now - RECENT_DAYS * DAY)]
        ready = learner.ready
        written: list[Entry] = []
        unsure = 0
        for note in self._notes(reply, exchange, written):
            if len(written) >= room:
                break
            if digest_of(note.title, note.text) in refused:
                continue
            gist = _gist(note.title, note.text)
            if any(_same(gist, other) for other in known):
                continue
            llm = confidence_of(note.raw.get("confidence"))
            project = exchange.project if note.scope == "project" else ""
            learned = learner.score(Sample(note.title, note.text, tuple(note.tags), note.kind, note.scope, exchange.engine, project))
            score = combined(llm, learned, ready)
            if score < WRITE_AT:
                unsure += 1
                continue
            try:
                entry = self.store.create(title=note.title, text=note.text, scope=note.scope, project=project, kind=note.kind, tags=note.tags, by=APP)
            except MemoryRejected as rejected:
                log.info("the memory curator did not write a note: %s", rejected.code)
                continue
            review.add(
                entry, engine=exchange.engine, conversation=exchange.conversation, row=exchange.row, why=note.why,
                project=project_path(exchange.project) if exchange.project else "", score=score, llm=llm, learned=learned, now=now,
            )
            written.append(entry)
            known.append(gist)
        if unsure:
            review.skip(unsure)
        return written
