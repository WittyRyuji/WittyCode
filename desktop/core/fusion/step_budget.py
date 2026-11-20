"""The step budget band applied to every expert delegation, shared by all identities.

Each side of the configured band is either a manual number or *adaptive*:
Fusion sizes it from the task at delegation time — never from the main agent's
own estimate, which is only clamped into the resolved band. The global
per-turn cap is applied last and wins even under the floor. The functions
here are pure; ``desktop.core.fusion.runtime`` reads the settings.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

ADAPTIVE_BASE = (16, 32)
"""The suggestion for a minimal task: no write paths, one acceptance line, short text."""
ADAPTIVE_STEP = 4
"""Steps added to the floor per size point; the ceiling is always twice the floor."""
ADAPTIVE_LOW_CAP = 96
"""The adaptive floor never exceeds this; the adaptive ceiling therefore stays under 192."""
_SIZE_CAPS = (8, 7, 4)
"""Bounds of the size signals: distinct write paths, acceptance items beyond the first,
and full 1000-character bands of goal/acceptance/task text."""
_ITEM = re.compile(r"(?m)^\s*(?:[-*•–]|\d+[.)、:])\s*\S")


@dataclass(frozen=True, slots=True)
class Side:
    """One configured bound: adaptive (Fusion sizes it) or a remembered manual value."""
    adaptive: bool
    value: int


@dataclass(frozen=True, slots=True)
class Band:
    """The resolved step floor and ceiling for one delegation, with its origin for diagnosis."""
    low: int
    high: int
    source: str


def acceptance_items(text: str) -> int:
    """Count bullet/numbered lines as discernible acceptance items; a non-empty text
    without markers counts as one item."""
    if not text.strip():
        return 0
    return max(1, len(_ITEM.findall(text)))


def task_size(*, paths: int, acceptance: str, text: str) -> int:
    """A bounded, monotonic size signal in ``0..19``; the requested step count is not an input."""
    return (min(max(paths, 0), _SIZE_CAPS[0])
            + min(max(acceptance_items(acceptance) - 1, 0), _SIZE_CAPS[1])
            + min(max(len(text), 0) // 1000, _SIZE_CAPS[2]))


def adaptive_band(size: int) -> tuple[int, int]:
    """The suggestion for one size score: 16–32 for a minimal task, growing to at most 96–192."""
    low = min(ADAPTIVE_BASE[0] + ADAPTIVE_STEP * max(size, 0), ADAPTIVE_LOW_CAP)
    return low, 2 * low


def resolve_band(low: Side, high: Side, size: int, cap: int) -> Band:
    """Mix the two configured sides with the adaptive suggestion, order the pair and cap it.

    A mixed pair can resolve inverted; the manual side wins and the adaptive side
    moves to it. A both-manual inversion is refused before it is ever stored.
    """
    adaptive = adaptive_band(size)
    floor = adaptive[0] if low.adaptive else low.value
    ceiling = adaptive[1] if high.adaptive else high.value
    if floor > ceiling:
        if low.adaptive:
            floor = ceiling
        else:
            ceiling = floor
    kinds = ("adaptive" if low.adaptive else "manual", "adaptive" if high.adaptive else "manual")
    source = (f"{kinds[0]} {floor}-{ceiling}" if kinds[0] == kinds[1]
              else f"{kinds[0]} min {floor} + {kinds[1]} max {ceiling}")
    cap = max(cap, 1)
    if ceiling > cap:
        floor, ceiling = min(floor, cap), cap
        source += f"; global cap {cap}"
    return Band(floor, ceiling, source)


def effective_steps(requested: int | None, band: Band) -> int:
    """Clamp the main agent's estimate into the band; a missing estimate takes the ceiling."""
    return min(max(requested, band.low), band.high) if requested is not None else band.high


def note(band: Band, requested: int | None) -> str:
    """How the effective budget was reached, kept next to it in the brief and the record."""
    return f"{band.source}; requested {requested}" if requested is not None else band.source
