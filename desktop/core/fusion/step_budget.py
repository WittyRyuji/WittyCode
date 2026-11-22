"""The step budget band applied to every expert delegation, shared by all identities.

Each side of the configured band is either a manual number or *adaptive*:
Fusion sizes it from the task at delegation time — never from the main agent's
own estimate, which is only clamped into the resolved band. The global
per-turn cap is applied last and wins even under the floor. The functions
here are pure; ``desktop.core.fusion.runtime`` reads the settings.
"""
from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

ADAPTIVE_BASE = (16, 32)
"""The suggestion for a minimal task: no write paths, one acceptance line, short text."""
ADAPTIVE_STEP = 4
"""Steps added to the floor per size point; the ceiling is always twice the floor."""
ADAPTIVE_LOW_CAP = 96
"""The first-pass floor never exceeds this; a risk rework allowance may add up to half again."""
ADAPTIVE_REWORK = 200
"""Denominator of the rework allowance: the floor gains ``floor * risk / 200``, up to +50% at risk 100."""
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


def adaptive_band(size: int, risk: int = 0) -> tuple[int, int]:
    """The suggestion for one size score: 16–32 for a minimal task, growing to at most 96–192.

    ``risk`` is a 0..100 index adding a rework allowance of up to half the
    first-pass floor, so a few high-risk tasks are not pinned to the minimal
    band; with risk 0 the band is exactly the first-pass suggestion.
    """
    first = min(ADAPTIVE_BASE[0] + ADAPTIVE_STEP * max(size, 0), ADAPTIVE_LOW_CAP)
    low = first + first * min(max(int(risk), 0), 100) // ADAPTIVE_REWORK
    return low, 2 * low


def default_steps(size: int, risk: int = 0) -> int:
    """The default count for one delegation: the first-pass floor plus the rework allowance for ``risk``.

    A starting point for the brief; the resolved band may still raise or cap it.
    """
    return adaptive_band(size, risk)[0]


def resolve_band(low: Side, high: Side, size: int, cap: int, *, risk: int = 0) -> Band:
    """Mix the two configured sides with the adaptive suggestion, order the pair and cap it.

    A mixed pair can resolve inverted; the manual side wins and the adaptive side
    moves to it. A both-manual inversion is refused before it is ever stored.
    ``risk`` scales only the adaptive suggestion — manual sides and the global
    cap are untouched.
    """
    risk = min(max(int(risk), 0), 100)
    adaptive = adaptive_band(size, risk)
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
    if risk:
        source += f"; risk {risk}"
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


@dataclass(frozen=True, slots=True)
class PlannedBudget:
    """A validated plan's ``budget`` resolved into delegation advice for the current turn.

    ``steps`` caps each delegation's step count and ``seconds`` each expert's run time —
    both only ever tighten the configured band and the session limit, never widen them.
    ``tokens`` is the plan-level estimate threshold the runtime checks honestly against
    known spend; it is review data here, never a hard backend limit. ``note`` records
    where each figure came from for the brief.
    """
    steps: int | None
    seconds: int | None
    tokens: int | None
    note: str


def planned_budget(raw: Mapping[str, Any] | None, band: Band, max_seconds: int) -> PlannedBudget | None:
    """Resolve a plan's ``budget`` object into per-delegation caps; ``None`` when there is none.

    ``turns`` bounds each delegation's steps: a number is clamped into the resolved
    ``band`` like any request — manual sides and the global cap still win, and it can
    never go under the floor. ``seconds`` bounds each expert's run time, clamped to
    ``1..max_seconds``; ``"adaptive"`` on either field leaves the configured side in
    charge, so it adds no cap. ``tokens`` passes through untouched: the plan cannot
    know actual prices or consumption, so it stays an estimate the runtime must not
    turn into a silent zero or a promised hard limit.
    """
    if not isinstance(raw, Mapping):
        return None
    turns, seconds, tokens = raw.get("turns"), raw.get("seconds"), raw.get("tokens")
    steps = effective_steps(turns, band) if type(turns) is int else None
    seconds_cap = min(seconds, max(1, max_seconds)) if type(seconds) is int and seconds > 0 else None
    tokens_est = tokens if type(tokens) is int else None
    parts = []
    if steps is not None:
        parts.append(f"plan turns {turns}" + (f"→{steps} steps" if steps != turns else " steps"))
    if seconds_cap is not None:
        parts.append(f"plan seconds {seconds_cap}" + (f" (of {seconds})" if seconds_cap != seconds else ""))
    if tokens_est is not None:
        parts.append(f"plan tokens ~{tokens_est} estimate")
    return PlannedBudget(steps, seconds_cap, tokens_est, "; ".join(parts) if parts else "plan budget adaptive")
