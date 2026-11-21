"""The Fusion main turn's last answer as an internal candidate ("A") until the turn settles.

Why: a Fusion main turn can still be sent back after the model's last words — missing acceptance checks, unrated
Drive records, an unwritten map description all become one continuation request. Those last words are then a
draft, not the turn's output, and must never have been visible: the user watches the tools and the thinking, then
gets one merged answer (A + what the continuation added), published exactly once.

Two pieces live here:

- ``Staging`` buffers one model request's answer text until the step's fate is known. A tool call, a new request,
  a steer, a notice or an error flush the buffer into the transcript as ordinary progress; the continuation that
  supersedes the draft drops it; a turn that simply ends releases it as the answer. Items only ever publish —
  nothing is edited or hidden after the fact.

- ``CANDIDATE`` marks the assistant messages that are still buffered when they reach the log (``history_grew``,
  so the mark and the item are saved together). The mark comes off the same moment the text is published —
  ``unflag`` — so live and reopened views agree: ``history.build_turn`` skips whatever is still marked, including
  a draft a crash left unpublished. ``for_responses`` strips ``wc_*`` keys: the mark never leaves the app, and the
  draft in the log is exactly what the model wrote, still there for the continuation to merge (the continuation
  text tells it the draft is material, not instruction).
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

CANDIDATE = "wc_candidate"
"""Private flag on an unpublished candidate message: kept for the model, never shown as the answer."""

MERGE_CLAUSE = (
    " The answer you just gave is held back as an internal draft; it remains in the history. Then end with one "
    "complete, standalone answer to the user request: keep from that draft whatever is still correct, correct "
    "whatever the information above contradicts, and add what it was missing."
)
"""Append when a draft was held: one merged answer — still-true facts of A, corrected by what the run added."""


class Staging:
    """One request's answer text, buffered until the step's fate is known.

    Published items stream straight through afterwards — the same ``(message_id, block_index)`` transcript key
    receives their late text, so ``begin`` resets per request (item ids repeat between responses).
    """

    def __init__(self) -> None:
        self.calls = False
        """The request produced a tool call: its answer is mid-turn progress, never the draft."""
        self._order: list[str] = []
        self._texts: dict[str, str] = {}
        self._done: set[str] = set()
        self._published: set[str] = set()

    def begin(self) -> None:
        """A new model request starts."""
        self.calls = False
        self._published.clear()

    def delta(self, item_id: str, text: str) -> bool:
        """Buffer a chunk; ``False`` once the item is public (late text must append, not re-buffer)."""
        if item_id in self._published:
            return False
        if item_id not in self._texts:
            self._order.append(item_id)
            self._texts[item_id] = ""
        self._texts[item_id] += text
        return True

    def done(self, item_id: str, text: str) -> bool:
        if item_id in self._published:
            return False
        if item_id not in self._texts:
            self._order.append(item_id)
        self._texts[item_id] = text
        self._done.add(item_id)
        return True

    def staged(self) -> bool:
        return bool(self._order)

    def held(self) -> dict[str, str]:
        """The staged texts by item id — what a publication boundary still owes the transcript."""
        return dict(self._texts)

    def flush(self) -> list[tuple[str, str, bool]]:
        """Hand back the buffer in item order — it turned out to be mid-turn progress after all."""
        out = [(item_id, self._texts[item_id], item_id in self._done) for item_id in self._order]
        self._published.update(self._order)
        self._order.clear()
        self._texts.clear()
        self._done.clear()
        return out

    def drop(self) -> bool:
        """Forget the buffer — a continuation supersedes it. ``True`` when a draft was actually held."""
        held = bool(self._order)
        self._order.clear()
        self._texts.clear()
        self._done.clear()
        return held


def mark_candidates(items: Sequence[Mapping[str, Any]], staging: Staging) -> dict[str, dict[str, Any]]:
    """Flag the still-staged assistant messages of a persisted batch; returns staged id -> the flagged item.

    The id match covers Responses items, the text match what an adapter wrote without ids (ACP). A message whose
    text was already published is not staged and keeps no flag, so progress stays visible on a reopen too.
    """
    from desktop.core.harness.history import message_text

    texts = staging.held()
    flagged: dict[str, dict[str, Any]] = {}
    if not texts:
        return flagged
    for item in items:
        if item.get("type") != "message" or item.get("role") != "assistant":
            continue
        item_id = str(item.get("id") or "")
        match = item_id if item_id in texts else next((iid for iid, text in texts.items() if text == message_text(item)), "")
        if match:
            item[CANDIDATE] = True  # type: ignore[index]  (the batch is the log's own dicts)
            flagged[match] = item  # type: ignore[assignment]
    return flagged


def unflag(flagged: dict[str, dict[str, Any]], item_ids: Sequence[str]) -> bool:
    """Take the mark off the items now published; ``True`` when anything was unflagged."""
    cleared = False
    for item_id in item_ids:
        item = flagged.pop(item_id, None)
        if item is not None and item.pop(CANDIDATE, None) is not None:
            cleared = True
    return cleared
