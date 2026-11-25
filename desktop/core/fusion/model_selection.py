"""Deterministic Fusion seat and model selection.

Pure Python: no I/O, no network and no model request. The harness supplies raw facts and trusted
provider-native model identities; the returned receipt is JSON-serializable and compact enough to
freeze or show in the UI.

Supported API::

    receipt = resolve_selection(
        72, 8, 6, 4,
        {
            "L1": [{"id": "chatgpt:gpt-lead", "score": 62, "cost": 10}],
            "L2": [
                {"id": "api:main:gpt-a", "actual_model": "gpt-a", "score": 60, "cost": 5},
                {"id": "api:main:gpt-b", "actual_model": "gpt-b", "score": 62, "cost": 10},
            ],
            "L3": [{"id": "chatgpt:gpt-light", "score": 40, "cost": 1}],
        },
        authors=[{"identity": "Coder", "id": "chatgpt:gpt-a"}],
        review_required=True,
    )

``chatgpt:``, ``devin:`` and ``factory:`` page ids carry their native model id. An ``api:`` page id
is only a page alias: pass ``actual_model`` (or ``native``) from the provider record. Unknown profile
fields stay unknown; they are never filled with zero.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from functools import cmp_to_key
import math
from typing import Any

from desktop.core.fusion.backends import backend_for

SEATS = ("L1", "L2", "L3")
_VERSION = 1
_MAX_TEXT = 512
_MAX_ITEMS = 128
_METRIC_DIGITS = 30


@dataclass(frozen=True, slots=True)
class _Assessment:
    N: int
    J: int
    I: int
    C: int
    x: Fraction
    h: Fraction
    D: Fraction
    low: Fraction
    high: Fraction
    qs: Fraction
    lead: Fraction
    worker_seat: str
    crossover_N: int


@dataclass(frozen=True, slots=True)
class _Offer:
    model: str
    actual: str
    score: Decimal | None
    cost: Decimal | None
    position: int

    def utility(self, x: Fraction) -> Fraction | None:
        score = Fraction(self.score) if self.score is not None else None
        cost = Fraction(self.cost) if self.cost is not None else None
        if x == 0:
            return None if cost is None else -cost / 5
        if x == 1:
            return None if score is None else score / 5
        if score is None or cost is None:
            return None
        return x * score / 5 - (1 - x) * cost / 5


@dataclass(frozen=True, slots=True)
class _Author:
    identity: str
    actual: str

    @property
    def identity_key(self) -> str:
        return self.identity.casefold()

    @property
    def actual_key(self) -> str:
        return self.actual.casefold()


def _index(name: str, value: object, upper: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= upper:
        raise ValueError(f"{name} must be an integer in 0..{upper}.")
    return value


def _flag(name: str, value: object) -> bool:
    if type(value) is not bool:
        raise ValueError(f"{name} must be true or false.")
    return value


def _text(value: object, name: str, limit: int = _MAX_TEXT) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit or any(ord(c) < 32 for c in value):
        raise ValueError(f"{name} must be a short non-empty string.")
    return value.strip()


def actual_model_id(page_id: object, native: object = None) -> str:
    """The trusted actual model id for a configured page model id.

    Non-API Fusion page ids encode their native model id. API page ids are aliases and require the
    provider-native ``native`` value; using the alias itself is refused because it cannot prove identity.
    """
    model = _text(page_id, "A model id")
    if native is not None:
        return _text(native, "The provider-native model id")
    backend = backend_for(model)
    if backend is None:
        raise ValueError("A model id must use a configured Fusion backend prefix.")
    if backend.name == "api":
        raise ValueError("An API page model id is an alias; pass the provider-native model id.")
    return model[len(backend.prefix):].strip()


def _explicit_actual(item: Mapping[str, object]) -> object:
    values = [item[key] for key in ("actual_model", "actual", "native") if item.get(key) is not None]
    if len(values) > 1 and len({_text(value, "The provider-native model id") for value in values}) != 1:
        raise ValueError("Conflicting provider-native model ids.")
    return values[0] if values else None


def _model_and_actual(item: Mapping[str, object], owner: str) -> tuple[str, str]:
    raw_model = item.get("id", item.get("model"))
    model = _text(raw_model, f"A {owner} model id")
    backend = backend_for(model)
    if backend is None:
        raise ValueError(f"A {owner} model id must use a configured Fusion backend prefix.")
    actual = _text(_explicit_actual(item), "The provider-native model id") if _explicit_actual(item) is not None else None
    if backend.name == "api":
        if actual is None:
            raise ValueError(f"A {owner} API page model id is an alias; pass the provider-native model id.")
        return model, actual
    encoded = model[len(backend.prefix):].strip()
    if actual is not None and actual != encoded:
        raise ValueError(f"A {owner} native model id does not match its non-API page model id.")
    return model, encoded


def _metric(value: object, name: str) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a finite non-negative decimal or null.")
    try:
        if isinstance(value, int):
            number = Decimal(value)
        elif isinstance(value, float):
            if not math.isfinite(value):
                raise ValueError
            number = Decimal(str(value))
        elif isinstance(value, Decimal):
            number = value
        elif isinstance(value, str):
            if not value.strip():
                raise ValueError
            number = Decimal(value.strip())
        else:
            raise ValueError
    except (InvalidOperation, ValueError, OverflowError):
        raise ValueError(f"{name} must be a finite non-negative decimal or null.") from None
    if (not number.is_finite() or number < 0 or len(number.as_tuple().digits) > _METRIC_DIGITS
            or abs(number.adjusted()) > _METRIC_DIGITS):
        raise ValueError(f"{name} must be a finite non-negative decimal or null.")
    return number


def _offer(item: object, seat: str, position: int) -> _Offer:
    if not isinstance(item, Mapping):
        raise ValueError(f"A {seat} model offer must be an object.")
    model, actual = _model_and_actual(item, f"{seat} model")
    return _Offer(model, actual, _metric(item.get("score"), "A model score"), _metric(item.get("cost"), "A model cost"), position)


def _compare_metric(left: Decimal | None, right: Decimal | None, *, high_first: bool) -> int:
    """A known tie-break value outranks an unknown one; two unknowns stay tied for configured order."""
    if left is None or right is None:
        return 0 if left is right else (-1 if left is not None else 1)
    if left == right:
        return 0
    better = left > right if high_first else left < right
    return -1 if better else 1


def _compare_position(left: _Offer, right: _Offer) -> int:
    return (left.position > right.position) - (left.position < right.position)


def _stable_model_key(offer: _Offer) -> tuple[str, str, str, str]:
    """A case-normalized, input-order-independent model key; exact text only breaks casefold collisions."""
    return offer.model.casefold(), offer.model, offer.actual.casefold(), offer.actual


def _compare_model_id(left: _Offer, right: _Offer) -> int:
    left_key, right_key = _stable_model_key(left), _stable_model_key(right)
    return (left_key > right_key) - (left_key < right_key)


def _complete_metrics(offer: _Offer) -> bool:
    return offer.score is not None and offer.cost is not None


def _compare_offers(left: _Offer, right: _Offer, x: Fraction) -> int:
    left_utility, right_utility = left.utility(x), right.utility(x)
    if left_utility is None or right_utility is None:
        if left_utility is not None:
            return -1
        if right_utility is not None:
            return 1
        return _compare_position(left, right)
    if left_utility != right_utility:
        return -1 if left_utility > right_utility else 1
    cost_order = _compare_metric(left.cost, right.cost, high_first=False)
    if cost_order:
        return cost_order
    score_order = _compare_metric(left.score, right.score, high_first=True)
    if score_order:
        return score_order
    if _complete_metrics(left) and _complete_metrics(right):
        model_order = _compare_model_id(left, right)
        if model_order:
            return model_order
    return _compare_position(left, right)


def _ranked_pool(items: object, seat: str, x: Fraction) -> list[_Offer]:
    if items is None:
        return []
    if isinstance(items, (str, bytes)) or not isinstance(items, Iterable):
        raise ValueError(f"The {seat} model pool must be a list of model offers.")
    values = list(items)
    if len(values) > _MAX_ITEMS:
        raise ValueError(f"The {seat} model pool is too large.")
    ranked = sorted((_offer(item, seat, position) for position, item in enumerate(values)),
                    key=cmp_to_key(lambda left, right: _compare_offers(left, right, x)))
    unique: dict[str, _Offer] = {}
    for offer in ranked:
        unique.setdefault(offer.actual.casefold(), offer)
    return list(unique.values())


def _pools(raw: object, x: Fraction) -> dict[str, list[_Offer]]:
    if not isinstance(raw, Mapping):
        raise ValueError("Model pools must be an object with L1, L2 and L3 lists.")
    unknown = set(raw) - set(SEATS)
    if unknown:
        raise ValueError(f"Unknown model pool seats: {', '.join(sorted(map(str, unknown)))}.")
    return {seat: _ranked_pool(raw.get(seat), seat, x) for seat in SEATS}


def _author(item: object) -> _Author:
    if not isinstance(item, Mapping):
        raise ValueError("An author must be an object with identity and model facts.")
    identity = _text(item.get("identity"), "An author identity", 128)
    _, actual = _model_and_actual(item, "author")
    return _Author(identity, actual)


def _authors(items: object) -> tuple[_Author, ...]:
    if items is None:
        return ()
    if isinstance(items, (str, bytes)) or not isinstance(items, Iterable):
        raise ValueError("Authors must be a list of objects.")
    values = list(items)
    if len(values) > _MAX_ITEMS:
        raise ValueError("The protected author set is too large.")
    unique: dict[tuple[str, str], _Author] = {}
    for item in values:
        author = _author(item)
        unique.setdefault((author.identity_key, author.actual_key), author)
    return tuple(unique.values())


def _assess(N: object, J: object, I: object, C: object) -> _Assessment:
    n = _index("N", N, 100)
    j, i, c = (_index(name, value, 10) for name, value in (("J", J), ("I", I), ("C", C)))
    x = Fraction(n, 100)
    h = Fraction(i, 10)
    d = Fraction(2 * j + 2 * i + c, 50)
    low = Fraction(35, 100) - Fraction(30, 100) * x
    high = Fraction(105, 100) - Fraction(40, 100) * x
    qs = Fraction(90, 100) - Fraction(40, 100) * x
    lead = Fraction(95, 100) - Fraction(10, 100) * x
    seat = "L3" if d <= low else "L1" if d >= high else "L2"
    return _Assessment(n, j, i, c, x, h, d, low, high, qs, lead, seat, 75 - 5 * i)


def _decimal_text(value: Fraction | Decimal) -> str:
    fraction = Fraction(value)
    numerator, denominator = fraction.numerator, fraction.denominator
    sign = "-" if numerator < 0 else ""
    numerator = abs(numerator)
    twos = fives = 0
    rest = denominator
    while rest % 2 == 0:
        twos += 1
        rest //= 2
    while rest % 5 == 0:
        fives += 1
        rest //= 5
    if rest != 1:
        return f"{sign}{numerator}/{denominator}"
    scale = max(twos, fives)
    numerator *= 2 ** (scale - twos) * 5 ** (scale - fives)
    digits = str(numerator).zfill(scale + 1)
    whole, fraction_part = digits[:-scale or None], digits[-scale:] if scale else ""
    text = whole + (("." + fraction_part.rstrip("0")) if fraction_part.rstrip("0") else "")
    return sign + (text if text else "0")


def _assessment_json(assessment: _Assessment) -> dict[str, object]:
    return {
        "D": _decimal_text(assessment.D), "h": _decimal_text(assessment.h), "x": _decimal_text(assessment.x),
        "worker_L3_upper": _decimal_text(assessment.low), "worker_L1_lower": _decimal_text(assessment.high),
        "QS_threshold": _decimal_text(assessment.qs), "Lead_review_threshold": _decimal_text(assessment.lead),
        "review_crossover_N": assessment.crossover_N, "worker_seat": assessment.worker_seat,
    }


def assess_difficulty(N: object, J: object, I: object, C: object) -> dict[str, object]:
    """The serializable difficulty assessment and formula-derived worker seat, before model availability."""
    assessment = _assess(N, J, I, C)
    return {"N": assessment.N, "J": assessment.J, "I": assessment.I, "C": assessment.C, **_assessment_json(assessment)}


def _rank_basis(offer: _Offer, x: Fraction) -> str:
    if offer.utility(x) is not None:
        if x == 0 and offer.score is None:
            return "cost-only endpoint utility; missing score cannot break ties, so configured order decides"
        if x == 1 and offer.cost is None:
            return "score-only endpoint utility; missing cost cannot break ties, so configured order decides"
        return "utility; ties use cost, score, then the casefolded stable model id"
    if x == 0:
        return "configured-order fallback: utility is unknown because cost is missing at the cost-only endpoint"
    if x == 1:
        return "configured-order fallback: utility is unknown because score is missing at the score-only endpoint"
    missing = ", ".join(name for name, value in (("score", offer.score), ("cost", offer.cost)) if value is None)
    return f"configured-order fallback: utility is unknown at 0<x<1; missing {missing}"


def _offer_json(offer: _Offer, seat: str, identity: str, x: Fraction) -> dict[str, object]:
    utility = offer.utility(x)
    return {
        "seat": seat, "identity": identity, "model": offer.model, "actual_model": offer.actual,
        "score": _decimal_text(offer.score) if offer.score is not None else None,
        "cost": _decimal_text(offer.cost) if offer.cost is not None else None,
        "utility": _decimal_text(utility) if utility is not None else None,
        "rank_basis": _rank_basis(offer, x),
    }


def _independent(offer: _Offer, identity: str, authors: tuple[_Author, ...]) -> bool:
    author_models = {author.actual_key for author in authors}
    author_identities = {author.identity_key for author in authors}
    return offer.actual.casefold() not in author_models and identity.casefold() not in author_identities


def _first_independent(pool: list[_Offer], identity: str, authors: tuple[_Author, ...]) -> _Offer | None:
    return next((offer for offer in pool if _independent(offer, identity, authors)), None)


def _review_choice(seat: str, offer: _Offer, x: Fraction, kind: str, reason: str) -> dict[str, object]:
    return {"required": True, "kind": kind, **_offer_json(offer, seat, "QualitySampler", x), "reason": reason}


def _no_review() -> dict[str, object]:
    return {"required": False, "kind": "none", "seat": None, "identity": None, "model": None, "actual_model": None,
            "score": None, "cost": None, "utility": None, "rank_basis": None, "reason": "not required"}


def _choose_review(assessment: _Assessment, pools: dict[str, list[_Offer]], authors: tuple[_Author, ...], *,
                   needed: bool, lead: bool) -> tuple[dict[str, object] | None, str]:
    if not needed:
        return _no_review(), ""
    identity = "QualitySampler"
    if lead:
        offer = _first_independent(pools["L1"], identity, authors)
        if offer is not None:
            return _review_choice("L1", offer, assessment.x, "lead", "Lead review reserved"), ""
        return None, "no independent L1 model is available for the reserved Lead review"
    offer = _first_independent(pools["L2"], identity, authors)
    if offer is not None:
        return _review_choice("L2", offer, assessment.x, "independent", "independent L2 preferred"), ""
    l1 = _first_independent(pools["L1"], identity, authors)
    l3 = _first_independent(pools["L3"], identity, authors)
    if l1 is not None and l3 is not None:
        use_l1 = assessment.N > assessment.crossover_N
        seat, offer = ("L1", l1) if use_l1 else ("L3", l3)
        return _review_choice(seat, offer, assessment.x, "cross-level",
                              f"both L1 and L3 are independent; N {assessment.N} {'>' if use_l1 else '<='} {assessment.crossover_N}"), ""
    if l1 is not None:
        return _review_choice("L1", l1, assessment.x, "cross-level", "no independent L2; only L1 remains"), ""
    if l3 is not None:
        return _review_choice("L3", l3, assessment.x, "cross-level", "no independent L2; only L3 remains"), ""
    return None, "no independent L1, L2 or L3 model is available for QualitySampler review"


def resolve_selection(N: object, J: object, I: object, C: object, pools: object, *,
                      authors: object = (), review_required: object = False, risk: object = False,
                      force_lead: object = False) -> dict[str, object]:
    """Resolve worker, Runner and QualitySampler seats deterministically.

    Malformed facts raise ``ValueError``. A well-formed but impossible combination returns ``ok: false``
    with explicit reasons and no assignment; feasibility of the whole worker/Runner/review combination is
    established before the worker is fixed. The receipt contains only JSON-compatible values.
    """
    assessment = _assess(N, J, I, C)
    review_required = _flag("review_required", review_required)
    risk = _flag("risk", risk)
    force_lead = _flag("force_lead", force_lead)
    protected = _authors(authors)
    ranked = _pools(pools, assessment.x)

    lead_review = force_lead or assessment.D >= assessment.lead
    review_needed = review_required or risk or lead_review or assessment.D >= assessment.qs
    original_seat = assessment.worker_seat
    worker_seat = "L2" if lead_review and original_seat == "L1" else original_seat

    facts = {"N": assessment.N, "J": assessment.J, "I": assessment.I, "C": assessment.C,
             "review_required": review_required, "risk": risk, "force_lead": force_lead}
    reasons = [f"worker seat {original_seat}: D {_decimal_text(assessment.D)} against "
               f"L3 upper {_decimal_text(assessment.low)} and L1 lower {_decimal_text(assessment.high)}"]
    if worker_seat != original_seat:
        reasons.append("Lead review is reserved, so the L1 write conflict moves the worker to L2")

    failures: list[str] = []
    if not ranked[worker_seat]:
        failures.append(f"no available {worker_seat} worker model")
    assignment: tuple[_Offer, _Offer, dict[str, object]] | None = None
    for worker in ranked[worker_seat]:
        worker_author = _Author("Coder", worker.actual)
        joint_authors = (*protected, worker_author)
        runner = next(iter(ranked["L3"]), None)
        if runner is None:
            failures.append("no available L3 model is available for Runner")
            continue
        review, problem = _choose_review(assessment, ranked, joint_authors, needed=review_needed, lead=lead_review)
        if review is None:
            failures.append(problem)
            continue
        assignment = (worker, runner, review)
        break

    if assignment is None:
        reasons.extend(dict.fromkeys(failures or ["no jointly feasible worker, Runner and review combination"]))
        return {"version": _VERSION, "ok": False, "facts": facts, "derived": _assessment_json(assessment),
                "worker": None, "runner": None, "review": None,
                "authors": [{"identity": author.identity, "actual_model": author.actual} for author in protected],
                "reasons": reasons}

    worker, runner, review = assignment
    reasons.append("joint worker/Runner/review feasibility established before fixing the worker")
    reasons.append("Runner uses the base L3 seat")
    if review["required"]:
        reasons.append(f"review uses {review['seat']}: {review['reason']}")
    return {"version": _VERSION, "ok": True, "facts": facts, "derived": _assessment_json(assessment),
            "worker": _offer_json(worker, worker_seat, "Coder", assessment.x),
            "runner": _offer_json(runner, "L3", "Runner", assessment.x),
            "review": review,
            "authors": [{"identity": author.identity, "actual_model": author.actual} for author in protected],
            "reasons": reasons}
