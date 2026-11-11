"""Notes the application thinks are worth keeping, waiting for the user's word.

After a turn the curator (``core/memory/curator``) asks the engine what in the exchange is worth keeping for later
conversations. What comes back is not written to the memory: it is kept here, in ``proposals.json`` next to the notes,
until the user accepts it (it becomes a note), edits it and accepts, or dismisses it. A dismissed suggestion is
remembered by a fingerprint of its words, so the same thing is not suggested again.

Only the application's own process writes this file (the agents' memory servers do not know it), so a lock inside the
process and an atomic rename are enough.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import threading
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from desktop.core.memory.store import KINDS, MAX_TEXT, MAX_TITLE, SCOPES

FILE = "proposals.json"
MAX_PENDING = 30
"""More waiting suggestions than this and the oldest go: nobody reads a list of a hundred."""
MAX_DISMISSED = 500


def digest_of(title: str, text: str) -> str:
    """A fingerprint of the words of a suggestion (case, spacing and punctuation do not change it)."""
    flat = re.sub(r"[\W_]+", "", f"{title}{text}".casefold())
    return hashlib.sha1(flat.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True, slots=True)
class Proposal:
    id: str
    title: str
    text: str
    scope: str
    project: str
    kind: str
    tags: tuple[str, ...]
    engine: str
    """Whose conversation it came from (``droid`` | ``codex`` | ``devin`` | ``chatgpt``)."""
    conversation: str
    """The title of that conversation."""
    row: str
    """The sidebar row it can be opened from ("" when not known)."""
    why: str
    """One short phrase from the model: what makes it worth keeping."""
    at: float
    digest: str

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id, "title": self.title, "text": self.text, "scope": self.scope, "project": self.project, "kind": self.kind,
            "tags": list(self.tags), "engine": self.engine, "conversation": self.conversation, "row": self.row, "why": self.why,
            "at": self.at, "digest": self.digest,
        }

    @classmethod
    def from_json(cls, raw: Mapping[str, Any]) -> Proposal | None:
        try:
            title, text = str(raw["title"]), str(raw["text"])
            scope, kind = str(raw.get("scope", "project")), str(raw.get("kind", "fact"))
            if not title.strip() or not text.strip() or scope not in SCOPES or kind not in KINDS:
                return None
            return cls(
                id=str(raw["id"]), title=title, text=text, scope=scope, project=str(raw.get("project", "")), kind=kind,
                tags=tuple(str(t) for t in raw.get("tags") or ()), engine=str(raw.get("engine", "")), conversation=str(raw.get("conversation", "")),
                row=str(raw.get("row", "")), why=str(raw.get("why", "")), at=float(raw.get("at", 0)),
                digest=str(raw.get("digest") or digest_of(title, text)),
            )
        except (KeyError, TypeError, ValueError):
            return None


def make(
    *, title: str, text: str, scope: str, project: str, kind: str, tags: Iterable[str], engine: str, conversation: str, row: str = "",
    why: str = "", now: float | None = None,
) -> Proposal:
    stamp = time.time() if now is None else now
    title, text = " ".join(title.split())[:MAX_TITLE], text.strip()[:MAX_TEXT]
    return Proposal(
        f"p-{int(stamp)}-{secrets.token_hex(3)}", title, text, scope, project if scope == "project" else "", kind, tuple(tags), engine,
        conversation, row, why.strip()[:120], stamp, digest_of(title, text),
    )


class ProposalStore:
    def __init__(self, root: str | os.PathLike[str]) -> None:
        self.path = Path(root) / FILE
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ the file

    def _read(self) -> tuple[list[Proposal], list[str]]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return [], []
        if not isinstance(raw, Mapping):
            return [], []
        pending = [p for p in (Proposal.from_json(item) for item in raw.get("pending") or () if isinstance(item, Mapping)) if p is not None]
        dismissed = [str(d) for d in raw.get("dismissed") or () if isinstance(d, str)]
        return pending, dismissed

    def _write(self, pending: list[Proposal], dismissed: list[str]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {"pending": [p.to_json() for p in pending], "dismissed": dismissed[-MAX_DISMISSED:]}
        temporary = self.path.with_name(self.path.name + ".tmp")
        temporary.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        os.replace(temporary, self.path)

    # ------------------------------------------------------------------ what the application asks

    def pending(self) -> list[Proposal]:
        """Waiting suggestions, newest first."""
        with self._lock:
            return sorted(self._read()[0], key=lambda p: p.at, reverse=True)

    def count(self) -> int:
        with self._lock:
            return len(self._read()[0])

    def get(self, proposal_id: str) -> Proposal | None:
        with self._lock:
            return next((p for p in self._read()[0] if p.id == proposal_id), None)

    def add(self, items: Iterable[Proposal]) -> list[Proposal]:
        """Keep these suggestions. Left out: one the user dismissed before, and one already waiting (same words). Returns
        the ones that were kept."""
        with self._lock:
            pending, dismissed = self._read()
            known = {p.digest for p in pending} | set(dismissed)
            kept: list[Proposal] = []
            for item in items:
                if item.digest in known:
                    continue
                known.add(item.digest)
                kept.append(item)
            if not kept:
                return []
            pending = (pending + kept)[-MAX_PENDING:] if len(pending) + len(kept) > MAX_PENDING else pending + kept
            self._write(pending, dismissed)
            return kept

    def edited(self, proposal_id: str, **changes: Any) -> Proposal | None:
        """The suggestion with the user's edits applied (not saved: it is about to become a note)."""
        found = self.get(proposal_id)
        if found is None:
            return None
        allowed = {key: value for key, value in changes.items() if key in ("title", "text", "scope", "kind", "project", "tags") and value is not None}
        return replace(found, **allowed) if allowed else found

    def dismiss(self, proposal_id: str) -> bool:
        """Drop a suggestion and remember its words so that it does not come again."""
        with self._lock:
            pending, dismissed = self._read()
            found = next((p for p in pending if p.id == proposal_id), None)
            if found is None:
                return False
            self._write([p for p in pending if p.id != proposal_id], [*[d for d in dismissed if d != found.digest], found.digest])
            return True

    def dismiss_all(self) -> int:
        with self._lock:
            pending, dismissed = self._read()
            if not pending:
                return 0
            self._write([], [*dismissed, *(p.digest for p in pending)])
            return len(pending)

    def settle(self, proposal_id: str) -> None:
        """A suggestion became a note: it is not waiting any more (and is not suggested again, the note is there)."""
        self.dismiss(proposal_id)

    def discard(self, proposal_id: str) -> bool:
        """Take a suggestion off the list without remembering its words: nobody said no to it (``review.migrate_proposals``
        moves the waiting ones into the memory)."""
        with self._lock:
            pending, dismissed = self._read()
            if not any(p.id == proposal_id for p in pending):
                return False
            self._write([p for p in pending if p.id != proposal_id], dismissed)
            return True

    def dismissed(self) -> list[str]:
        """The fingerprints of what the user said no to (``digest_of``), oldest first."""
        with self._lock:
            return self._read()[1]
