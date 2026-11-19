"""When a Fusion Drive record stops being used, and how its quality score moves.

Fixed arithmetic (docs/fusion-route-design-2026-11-18.md, 8.2): nothing here reads a clock or a store, and nothing
adapts while the program runs. A record's age counts from its last update; being read or rated is not an update.
The caller decides which records the rules apply to: they govern one conversation using another's records, not a
conversation's own records, map entries or the long-term memory.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

OLD_FROM = 0.3
"""The project's lifetime is split evenly on a log scale that starts at the keep period; the last 70% is old."""
Q_UP = 0.1
Q_DOWN = 0.3
"""A harmful record costs about three helpful ratings: models rate generously, so praise has to be earned."""
SCORES = (-1, 0, 1)


@dataclass(frozen=True, slots=True)
class Retention:
    warm: float = 0.5
    """0..1. The higher it is, the sooner old records leave use."""
    keep_days: float = 7.0
    """Records updated more recently than this are neither dropped nor demoted."""
    qk: float = 0.6
    """Records whose quality is above this ignore ``warm``."""

    def __post_init__(self) -> None:
        values = (self.warm, self.keep_days, self.qk)
        if any(isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) for value in values):
            raise ValueError("Retention settings must be finite numbers.")
        if not (0 <= self.warm <= 1 and self.keep_days > 0 and -1 <= self.qk <= 1):
            raise ValueError("Retention settings are out of range.")


def position(age_days: float, period_days: float, keep_days: float) -> float:
    """Where a record of this age sits in the project's lifetime: 0 is new, 1 is as old as the project."""
    if age_days <= keep_days or period_days <= keep_days:
        return 0.0
    return min(1.0, math.log(age_days / keep_days) / math.log(period_days / keep_days))


def temperature(age_days: float, period_days: float, q: float, settings: Retention) -> float:
    """1 for a new or protected record, falling to 0 as an old one reaches the age of the project."""
    if age_days < settings.keep_days or q > settings.qk:
        return 1.0
    place = position(age_days, period_days, settings.keep_days)
    return 1.0 if place <= OLD_FROM else (1.0 - place) / (1.0 - OLD_FROM)


def in_use(age_days: float, period_days: float, q: float, settings: Retention) -> bool:
    return temperature(age_days, period_days, q, settings) >= settings.warm


def exit_age_days(period_days: float, settings: Retention) -> float:
    """The age past which an unprotected record leaves use; ``inf`` when it never does."""
    if settings.warm <= 0 or period_days <= settings.keep_days:
        return math.inf
    place = max(OLD_FROM, 1.0 - (1.0 - OLD_FROM) * settings.warm)
    return settings.keep_days * (period_days / settings.keep_days) ** place


def next_q(q: float, score: int) -> float:
    """The quality after one explicit rating. Being injected or read without a rating never calls this."""
    if isinstance(score, bool) or score not in SCORES:
        raise ValueError("A rating is -1, 0 or 1.")
    if score > 0:
        q += Q_UP * (1.0 - q)
    elif score < 0:
        q -= Q_DOWN
    return max(-1.0, min(1.0, q))
