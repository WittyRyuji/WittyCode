"""The fixed retention and quality rules of the Fusion Drive (design 8.2)."""
import math

import pytest

from desktop.core.fusion.drive.retention import Retention, exit_age_days, in_use, next_q, position, temperature

# Project period in days -> the day a record becomes old, then the day it leaves use for Warm 0.3, 0.5, 0.7 and 1.0
# (the table in the design, keep period 7 days).
TABLE = {
    30: (11, 22, 18, 15, 11),
    90: (15, 53, 37, 26, 15),
    365: (23, 159, 91, 53, 23),
    1000: (31, 353, 176, 88, 31),
}


@pytest.mark.parametrize("period", sorted(TABLE))
def test_exit_days_match_the_design_table(period):
    old, *exits = TABLE[period]
    assert round(exit_age_days(period, Retention(warm=1.0))) == old
    for warm, days in zip((0.3, 0.5, 0.7, 1.0), exits):
        settings = Retention(warm=warm)
        limit = exit_age_days(period, settings)
        assert round(limit) == days
        assert in_use(limit * 0.999, period, 0.0, settings)
        assert not in_use(limit * 1.001, period, 0.0, settings)


def test_position_is_logarithmic_from_the_keep_period():
    assert position(7, 90, 7) == 0.0
    assert position(3, 90, 7) == 0.0
    assert position(90, 90, 7) == pytest.approx(1.0)
    assert position(500, 90, 7) == 1.0
    assert position(math.sqrt(7 * 90), 90, 7) == pytest.approx(0.5)
    assert position(50, 5, 7) == 0.0  # A project younger than the keep period has no old records.


def test_recent_and_high_quality_records_are_protected():
    settings = Retention(warm=1.0, keep_days=7, qk=0.6)
    assert temperature(6.9, 1000, -0.5, settings) == 1.0
    assert temperature(900, 1000, 0.61, settings) == 1.0
    assert temperature(900, 1000, 0.6, settings) < 1.0  # Only a quality above Qk is exempt.
    assert not in_use(900, 1000, 0.6, settings)


def test_a_better_rated_record_never_leaves_sooner():
    settings = Retention()
    for age in (1, 8, 20, 40, 80, 200):
        heat = [temperature(age, 365, q / 10, settings) for q in range(-10, 11)]
        assert heat == sorted(heat)
    for q in (-1.0, -0.2, 0.0, 0.4, 0.9, 1.0):
        assert next_q(q, 1) >= next_q(q, 0) >= next_q(q, -1)


def test_older_records_are_never_warmer():
    settings = Retention()
    heat = [temperature(age, 365, 0.0, settings) for age in range(1, 400)]
    assert heat == sorted(heat, reverse=True)
    assert heat[0] == 1.0 and heat[-1] == 0.0


def test_quality_moves_only_by_explicit_ratings():
    q = 0.0
    reached = {}
    for count in range(1, 17):
        q = next_q(q, 1)
        reached[count] = q
    assert reached[8] < 0.6 <= reached[9]
    assert reached[15] < 0.8 <= reached[16]
    assert next_q(0.5, 0) == 0.5
    assert next_q(0.5, -1) == pytest.approx(0.2)
    assert next_q(-0.9, -1) == -1.0
    assert next_q(1.0, 1) == 1.0
    for bad in (2, -2, True, 0.5, "1"):
        with pytest.raises(ValueError):
            next_q(0.0, bad)


def test_warm_zero_keeps_everything_and_bad_settings_are_refused():
    assert exit_age_days(365, Retention(warm=0.0)) == math.inf
    assert in_use(364, 365, -1.0, Retention(warm=0.0))
    for kwargs in ({"warm": 1.5}, {"warm": -0.1}, {"keep_days": 0}, {"qk": 2}, {"warm": math.nan}, {"keep_days": True}):
        with pytest.raises(ValueError):
            Retention(**kwargs)
