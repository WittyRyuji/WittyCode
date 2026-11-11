"""How fast the model wrote: tokens per second, per turn, for the status bar (what matters when a relay API is in between).

A turn's speed is what the model wrote (``Turn.output_tokens``, thinking included, as the engine counts it) over the time
the model had (``Turn.model_seconds``: the turn's time without the tools at work and the questions the user had to
answer). That time still holds the wait for the first output, and every later request's wait too, so it is the speed one
actually gets, not the peak of the stream. The wait for the first output is kept apart (``first``), and the speed without
it (``gen_tps``) is given as well: a slow relay shows in one or the other.

A turn that wrote almost nothing, or took almost no time, says nothing about speed; the latest turn that does is used.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from desktop.core.transcript import Turn, TurnStatus

MIN_TOKENS = 10
"""Fewer tokens than this and the time is mostly the request itself."""
MIN_SECONDS = 0.5
MIN_GENERATING_SECONDS = 0.5
"""The speed without the first wait needs at least this much time left to divide by."""


@dataclass(frozen=True, slots=True)
class Speed:
    tps: float
    tokens: int
    seconds: float
    first: float | None
    """Seconds from the message to the first output."""
    gen_tps: float | None
    """Tokens per second without that wait (``None`` when it is not known or leaves too little time)."""

    def to_json(self) -> dict[str, Any]:
        return {
            "tps": round(self.tps, 1),
            "tokens": self.tokens,
            "seconds": round(self.seconds, 1),
            "first": None if self.first is None else round(self.first, 2),
            "genTps": None if self.gen_tps is None else round(self.gen_tps, 1),
        }


def speed_of_turn(turn: Turn) -> Speed | None:
    if turn.status is not TurnStatus.DONE or turn.from_history:
        return None
    tokens, seconds = turn.output_tokens, turn.model_seconds
    if tokens is None or seconds is None or tokens < MIN_TOKENS or seconds < MIN_SECONDS:
        return None
    first = None
    if turn.first_output_at is not None and turn.started_at is not None:
        first = max(0.0, turn.first_output_at - turn.started_at)
    generating = None if first is None else seconds - first
    return Speed(
        tps=tokens / seconds,
        tokens=tokens,
        seconds=seconds,
        first=first,
        gen_tps=tokens / generating if generating is not None and generating >= MIN_GENERATING_SECONDS else None,
    )


def speed_of(turns: Sequence[Turn]) -> Speed | None:
    """The speed of the latest turn that can tell."""
    for turn in reversed(turns):
        speed = speed_of_turn(turn)
        if speed is not None:
            return speed
    return None
