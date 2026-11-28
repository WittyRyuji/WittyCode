"""The step budget band applied to every expert delegation, shared by all identities.

Each side of the configured band is either a manual number or *adaptive*:
Fusion sizes it from the task at delegation time — never from the main agent's
own estimate, which is only clamped into the resolved band. The global
per-turn cap is applied last and wins even under the floor. The functions
here are pure; ``desktop.core.fusion.runtime`` reads the settings.
"""
from __future__ import annotations

import math
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


MAX_SECONDS = 86_400  # Per-attempt safety limit, also the largest legal manual ceiling.


def adaptive_seconds(steps: int) -> int:
    """Estimate wall time from the resolved work allowance when the brief has no time estimate."""
    return min(MAX_SECONDS, max(60, 30 * steps))


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


# --------------------------------------------------------------------------- curve04 (new dispatch, beside legacy V3)

CURVE04_SIZE_MAX = 19
"""The size score uses the existing bounded 0..19 scale."""


@dataclass(frozen=True, slots=True)
class Curve04Budget:
    """One curve04 delegation decision.

    ``m`` is the actual minimum before upper limits: the adaptive value, or the manual
    minimum when that side is manual. ``low`` is that minimum after hard upper limits
    (they win even when they fall below the floor), ``high`` the final ceiling, and
    ``steps`` the value to use for the delegation. ``curve_ceiling`` is the unclamped
    curve result for ``m``; ``caps`` names the upper limits that tightened it.
    """
    m: int
    low: int
    high: int
    steps: int
    curve_ceiling: int
    source: str
    caps: tuple[str, ...] = ()


def curve04_value(m: float) -> float:
    """The curve04 difficulty response ``f(m)`` used only to shape the ceiling ratio."""
    return 10 - m / 5 if m <= 20 else 2 + 4 * math.exp(-(m - 20) / 20)


def curve04_ratio(m: float) -> float:
    """The clipped ratio ``R`` of curve04: 5 at m=10 or below, 1.5 at m=100 or above."""
    low, high = curve04_value(10), curve04_value(100)
    position = (curve04_value(m) - high) / (low - high)
    return 1.5 + 3.5 * min(max(position, 0.0), 1.0)


def curve04_ceiling(m: int | float) -> int:
    """Round half up the curve04 ceiling for a positive minimum ``m``."""
    try:
        value = float(m)
    except (TypeError, ValueError):
        raise ValueError("curve04 m must be a positive finite number.") from None
    if not math.isfinite(value) or value < 1:
        raise ValueError("curve04 m must be a positive finite number.")
    return math.floor(value * curve04_ratio(value) + 0.5)


def _curve04_difficulty(difficulty: int | float) -> float:
    try:
        value = float(difficulty)
    except (TypeError, ValueError):
        raise ValueError("curve04 difficulty must be a number in 0..1.") from None
    if not math.isfinite(value) or not 0.0 <= value <= 1.0:
        raise ValueError("curve04 difficulty must be a number in 0..1.")
    return value


def curve04_minimum(size: int, difficulty: int | float = 0.0) -> int:
    """The adaptive minimum ``m = 16 + 4S + floor((16 + 4S)D/2)``.

    ``S`` is clamped to the existing 0..19 task-size scale. ``D`` is the normalized
    difficulty/rework factor in 0..1; callers using a 0..100 risk index must divide it
    first rather than letting a percent silently become a fiftyfold allowance.
    """
    size = min(max(int(size), 0), CURVE04_SIZE_MAX)
    base = 16 + 4 * size
    return base + math.floor(base * _curve04_difficulty(difficulty) / 2)


def effective_curve04_steps(requested: int | None, budget: Curve04Budget) -> int:
    """Clamp an explicit request into the curve04 band; omitting it starts at ``m`` after caps."""
    return budget.low if requested is None else min(max(requested, budget.low), budget.high)


def resolve_curve04(low: Side, high: Side, size: int, cap: int, *, difficulty: int | float = 0.0,
                    plan: int | None = None, requested: int | None = None) -> Curve04Budget:
    """Resolve the new dispatch step budget without any model estimate in the calculation.

    The adaptive minimum comes from ``curve04_minimum``. When the minimum is manual and
    the maximum adaptive, the curve is applied to that actual manual minimum. A manual
    maximum replaces the adaptive ceiling; the global and approved-plan upper limits then
    tighten whichever ceiling was selected. Upper limits win even below the minimum, so
    the returned decision never exceeds them. With no ``requested`` count the delegation
    starts at the minimum rather than spending the whole ceiling.
    """
    adaptive_m = curve04_minimum(size, difficulty)
    m = adaptive_m if low.adaptive else max(1, int(low.value))
    curve_ceiling = curve04_ceiling(m)
    selected_high = curve_ceiling if high.adaptive else max(1, int(high.value))
    upper = [max(1, int(cap))]
    cap_names = []
    if upper[0] < selected_high:
        cap_names.append(f"global cap {upper[0]}")
    if type(plan) is int and plan > 0:
        upper.append(plan)
        if plan < min(selected_high, upper[0]):
            cap_names.append(f"plan cap {plan}")
    final_high = min(selected_high, *upper)
    final_low = min(m, final_high)
    kinds = ("adaptive" if low.adaptive else "manual", "adaptive" if high.adaptive else "manual")
    source = f"curve04 {kinds[0]} min {m} + {kinds[1]} max {selected_high}"
    if high.adaptive:
        source += f" (curve ceiling {curve_ceiling})"
    if cap_names:
        source += "; " + "; ".join(cap_names)
    steps = effective_curve04_steps(requested, Curve04Budget(m, final_low, final_high, final_low,
                                                             curve_ceiling, source, tuple(cap_names)))
    return Curve04Budget(m, final_low, final_high, steps, curve_ceiling, source, tuple(cap_names))
