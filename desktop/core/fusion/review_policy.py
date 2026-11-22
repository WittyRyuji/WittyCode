"""The verification plan for one deliverable: which checks run before it may be reported done.

``plan_review`` is pure; the runtime supplies the session cost–quality
``preference`` and a task ``risk`` index (both 0..100), whether the mandatory
acceptance checks are runnable, whether evidence matching the final state is
already on record, and the optional spot-check candidates. The Runner, the
QualitySampler and the optional checks are each decided on their own merit;
the mandatory acceptance run is never traded away against the preference, and
a deliverable nothing verifies is not marked sufficient.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

COVERAGE_BAR = 100
"""An optional check runs when its own risk plus the stronger of the
cost–quality preference and the task risk reaches this."""
SAMPLER_BAR = 100
"""The QualitySampler is dispatched when the task risk plus the preference reaches this."""
REVIEW_RISK = 70
"""The risk index at which a delivery counts as high-risk for an L1 review."""
REVIEW_PREFERENCE = 90
"""The cost–quality preference at which a high-risk delivery earns an L1 independent review."""


@dataclass(frozen=True, slots=True)
class OptionalCheck:
    """A proposed spot check: a short name and its own 0..100 risk index."""
    name: str
    risk: int = 0


@dataclass(frozen=True, slots=True)
class ReviewPlan:
    """The verification chosen for one deliverable: a Runner for the mandatory
    acceptance checks, a QualitySampler for risk-driven spot review, an L1
    independent review for high-risk deliveries on a quality-lean session, the
    selected optional checks ordered by descending risk, whether all that
    leaves enough verification on record to report success, and one short
    reason per decision."""
    runner: bool
    sampler: bool
    review: bool
    checks: tuple[OptionalCheck, ...]
    sufficient: bool
    reasons: tuple[str, ...]


def _index(name: str, value: object) -> int:
    """A real ``int`` in ``0..100`` — never a bool, float or NaN, like the session indices."""
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 100:
        raise ValueError(f"The {name} must be an integer in 0..100.")
    return value


def _optional(item: object) -> OptionalCheck:
    """Normalize one candidate: an ``OptionalCheck``, a ``(name, risk)`` pair or a ``{"name", "risk"}`` mapping."""
    if isinstance(item, OptionalCheck):
        return item
    if isinstance(item, Mapping):
        name, risk = item.get("name"), item.get("risk", 0)
    elif isinstance(item, (tuple, list)) and len(item) == 2:
        name, risk = item
    else:
        raise ValueError("An optional check is a name with a 0..100 risk index.")
    if not isinstance(name, str) or not name.strip():
        raise ValueError("An optional check needs a non-empty name.")
    return OptionalCheck(name.strip(), _index("check risk", risk))


def plan_review(preference: int, risk: int, runnable: bool, has_current_evidence: bool,
                optional_checks: Iterable[OptionalCheck | tuple[str, int] | Mapping[str, object]] = ()) -> ReviewPlan:
    """Decide the verification for one deliverable.

    The Runner runs the mandatory acceptance checks whenever they are runnable,
    unless evidence matching the final state is already on record — the
    preference never buys that away. An optional check is selected when its own
    risk plus the stronger of the preference and the task risk reaches
    ``COVERAGE_BAR``; selections come back ordered by descending risk and
    deduplicated by name. The QualitySampler is dispatched when the task risk
    plus the preference reaches ``SAMPLER_BAR``, or whenever an optional check
    was selected for it. ``sufficient`` is false only when nothing verifies the
    result — no runnable checks, no current evidence and no sampler — and the
    deliverable must not be reported done.
    """
    preference, risk = _index("preference", preference), _index("risk", risk)
    review = risk >= REVIEW_RISK and preference >= REVIEW_PREFERENCE
    runner = bool(runnable) and not has_current_evidence
    reasons = ["runner: " + ("runs the mandatory acceptance checks" if runner else
                             "skipped, current evidence already covers the final state" if has_current_evidence else
                             "skipped, the acceptance checks are not runnable")]
    candidates: dict[str, int] = {}
    for item in optional_checks:
        check = _optional(item)
        candidates[check.name] = max(check.risk, candidates.get(check.name, 0))
    ordered = sorted(candidates.items(), key=lambda entry: -entry[1])  # stable: ties keep proposal order
    pressure = max(preference, risk)
    checks = tuple(OptionalCheck(name, check_risk) for name, check_risk in ordered
                   if check_risk + pressure >= COVERAGE_BAR)
    by_risk = risk + preference >= SAMPLER_BAR
    sampler = by_risk or bool(checks)
    reasons.append("sampler: " + (f"dispatched, risk {risk} + preference {preference} reaches {SAMPLER_BAR}" if by_risk else
                                  f"dispatched for the {len(checks)} selected optional checks" if checks else
                                  "not dispatched"))
    reasons.append("review: " + (f"L1 review, risk {risk} at preference {preference} is high-risk on a quality-lean session"
                                 if review else "not added"))
    sufficient = bool(has_current_evidence) or runner or sampler or review
    if not sufficient:
        reasons.append("insufficient verification: nothing runs and no evidence is current, do not report success")
    return ReviewPlan(runner, sampler, review, checks, sufficient, tuple(reasons))
