"""How full a session's context is, and how that is put into words.

Three numbers, all in tokens: ``used`` (what the last model call of the conversation carried; ``None`` until one was
measured and right after a compaction), ``window`` (the model's context window; it follows model changes) and ``budget``
(where the conversation is compacted, when the engine says).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ContextState:
    used: int | None = None
    window: int | None = None
    budget: int | None = None
    model: str = ""
    compacted: bool = False
    """The conversation was compacted and no model call has measured the result yet (the reason ``used`` is unknown)."""


def format_tokens(count: int) -> str:
    """``850``, ``2.5K``, ``25K``, ``872K``, ``1M`` (one decimal below 10K and in the millions, none in between)."""
    if count < 1000:
        return str(count)
    if count < 10_000:
        return f"{count / 1000:.1f}".removesuffix(".0") + "K"
    if count < 999_500:
        return f"{round(count / 1000)}K"
    return f"{count / 1_000_000:.1f}".removesuffix(".0") + "M"
