"""The V3 tiering curve deciding which seat a work item's score dispatches to.

Two rational thresholds bound the score band of the worker seat (L2): at or
below the low threshold the light seat (L3) takes the item, at or above the
high threshold the main seat (L1) does. Both thresholds slide with the
difficulty index ``N`` (0-100) along a smoothstep-shaped rational curve
pinned at its declared high, midpoint and low anchors, so gate positions are
stable, reproducible and comparable exactly. All arithmetic is ``Fraction``;
scores are decimals in ``0..1`` and boundary scores keep their declared seat.
"""
from __future__ import annotations

import math
from decimal import Decimal, InvalidOperation
from fractions import Fraction

_LOW_ANCHORS = (Fraction(910, 1000), Fraction(300, 1000), Fraction(190, 1000))
_HIGH_ANCHORS = (Fraction(950, 1000), Fraction(850, 1000), Fraction(275, 1000))
"""(H, M, L) anchors of the L3->L2 and L2->L1 threshold curves."""


def check_index(N: object) -> int:
    """The difficulty index is a real ``int`` in ``0..100`` — never a bool, float or NaN."""
    if isinstance(N, bool) or not isinstance(N, int):
        raise ValueError("The difficulty index N must be an integer in 0..100.")
    if not 0 <= N <= 100:
        raise ValueError("The difficulty index N must be an integer in 0..100.")
    return N


_SCORE_DIGITS = 30
"""A score may carry at most 30 significant digits and a 30-shift decimal exponent; anything wilder is refused
before it can construct a giant ``Fraction`` or a huge ``Decimal`` mantissa."""
_MAX_SCORE_BITS = 160
"""A supplied ``Fraction``'s numerator/denominator bit bound; the exact decimals above need far less."""


def _bounded_decimal(value: Decimal) -> Decimal:
    if (not value.is_finite() or len(value.as_tuple().digits) > _SCORE_DIGITS
            or abs(value.adjusted()) > _SCORE_DIGITS):
        raise ValueError("The score must be a finite decimal in 0..1.")
    return value


def check_score(value: object) -> Fraction:
    """One decimal score in ``0..1``, converted exactly; bool and non-finite values are refused."""
    score: Fraction
    if isinstance(value, bool):
        raise ValueError("The score must be a decimal in 0..1.")
    if isinstance(value, Fraction):
        score = value
    elif isinstance(value, int):
        score = Fraction(value)
    elif isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("The score must be a finite decimal in 0..1.")
        score = Fraction(str(value))
    elif isinstance(value, Decimal):
        score = Fraction(_bounded_decimal(value))
    elif isinstance(value, str):
        try:
            parsed = Decimal(value)
        except InvalidOperation:
            raise ValueError("The score must be a decimal in 0..1.") from None
        score = Fraction(_bounded_decimal(parsed))
    else:
        raise ValueError("The score must be a decimal in 0..1.")
    if score.denominator.bit_length() > _MAX_SCORE_BITS or score.numerator.bit_length() > _MAX_SCORE_BITS:
        raise ValueError("The score must be a decimal in 0..1.")
    if not 0 <= score <= 1:
        raise ValueError("The score must be a decimal in 0..1.")
    return score


def _threshold(anchors: tuple[Fraction, Fraction, Fraction], N: int) -> Fraction:
    """The exact threshold at index ``N``, decreasing strictly from ``H`` to ``L``.

    ``x = N/100``; ``u = x²(3−2x)``; ``r = (H−M)/(M−L)``;
    ``T = L + (H−L)(1−u)/(1−u+ru)`` passes through ``M`` at ``u = 1/2``.
    """
    high, mid, low = anchors
    x = Fraction(N, 100)
    u = x * x * (3 - 2 * x)
    r = (high - mid) / (mid - low)
    return low + (high - low) * (1 - u) / (1 - u + r * u)


def thresholds(N: int) -> tuple[Fraction, Fraction]:
    """The ``(low, high)`` gates at index ``N``: at or under ``low`` goes L3, at or over ``high`` goes L1."""
    N = check_index(N)
    return _threshold(_LOW_ANCHORS, N), _threshold(_HIGH_ANCHORS, N)


def tier(N: int, score: object) -> str:
    """The seat one scored work item dispatches to: ``"L3"``, ``"L2"`` or ``"L1"``.

    Boundary scores keep the declared seat: equal to the low gate is L3, equal
    to the high gate is L1; anything strictly between them is L2.
    """
    low, high = thresholds(N)
    value = check_score(score)
    if value <= low:
        return "L3"
    if value >= high:
        return "L1"
    return "L2"
