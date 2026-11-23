from __future__ import annotations

from decimal import Decimal
from fractions import Fraction

import pytest

from desktop.core.fusion.dispatch import tier, thresholds


def test_threshold_endpoints_are_the_anchors():
    # u = 0 at N = 0 pins T to H; u = 1 at N = 100 pins T to L.
    assert thresholds(0) == (Fraction(910, 1000), Fraction(950, 1000))
    assert thresholds(100) == (Fraction(190, 1000), Fraction(275, 1000))


def test_threshold_midpoint_hits_m_exactly():
    # u = 1/2 at N = 50: the curve passes through M with no float drift.
    assert thresholds(50) == (Fraction(300, 1000), Fraction(850, 1000))


def test_thresholds_exact_fractions_all_101():
    for n in range(101):
        low, high = thresholds(n)
        assert type(low) is Fraction and type(high) is Fraction
        assert Fraction(190, 1000) <= low <= Fraction(910, 1000)
        assert Fraction(275, 1000) <= high <= Fraction(950, 1000)
        assert low < high


def test_thresholds_strictly_decrease_all_101():
    gates = [thresholds(n) for n in range(101)]
    for n in range(100):
        assert gates[n][0] > gates[n + 1][0]
        assert gates[n][1] > gates[n + 1][1]


def test_tier_keeps_boundary_scores_on_the_declared_seat():
    # Equal to the low gate is L3, equal to the high gate is L1; strictly between is L2.
    assert tier(50, Fraction(300, 1000)) == "L3"
    assert tier(50, Fraction(300, 1000) + Fraction(1, 10**12)) == "L2"
    assert tier(50, Fraction(850, 1000)) == "L1"
    assert tier(50, Fraction(850, 1000) - Fraction(1, 10**12)) == "L2"


def test_tier_score_and_index_edges():
    assert tier(50, 0) == "L3"
    assert tier(50, 1) == "L1"
    assert tier(0, Fraction(910, 1000)) == "L3"
    assert tier(0, Fraction(19, 20)) == "L1"
    assert tier(100, Fraction(19, 100)) == "L3"
    assert tier(100, Fraction(11, 40)) == "L1"


def test_tier_accepts_decimal_score_forms():
    for score in (Fraction(1, 2), Decimal("0.5"), "0.5", 0.5, "5e-1"):
        assert tier(50, score) == "L2"


def test_tier_sweeps_l3_to_l1_across_all_101():
    seats = [tier(n, Fraction(1, 2)) for n in range(101)]
    order = {"L3": 0, "L2": 1, "L1": 2}
    assert [order[s] for s in seats] == sorted(order[s] for s in seats)
    assert seats[0] == "L3" and seats[-1] == "L1" and "L2" in seats


def test_index_rejects_non_integers_and_out_of_range():
    for bad in (-1, 101, 50.0, "50", True, False, None, Decimal("50"), Fraction(50), float("nan")):
        with pytest.raises(ValueError):
            thresholds(bad)
        with pytest.raises(ValueError):
            tier(bad, Fraction(1, 2))


def test_score_rejects_non_finite_bool_and_out_of_range():
    bad_scores = (float("nan"), float("inf"), -float("inf"), Decimal("NaN"), Decimal("Infinity"),
                  True, False, None, "abc", "", "nan", b"0.5",
                  -1, 2, Fraction(3, 2), Fraction(-1, 10**9), -0.5, 1.5, "-0.1", "1.1")
    for bad in bad_scores:
        with pytest.raises(ValueError):
            tier(50, bad)


def test_score_rejects_resource_exhausting_extremes():
    # Wild exponents or mantissas must not build giant Fractions: refused before any big-int work.
    for bad in ("1e-1000000", "1e1000000", "0." + "0" * 60 + "1", "0." + "1" * 40,
                Decimal("1e-5000"), Decimal("1e5000"), Fraction(1, 10**200), Fraction(10**200, 1) - 1, 5e-324):
        with pytest.raises(ValueError):
            tier(50, bad)
    # Thirty significant digits and a thirty-shift exponent remain exact decimal scores.
    assert tier(50, "0." + "0" * 29 + "5") == "L3"
    assert tier(50, "0.123456789012345678901234567890") == "L3"
