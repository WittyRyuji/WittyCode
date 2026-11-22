"""The delivery verification plan and the risk-aware step suggestion:
mandatory checks are never budgeted away, optional coverage follows the
preference and the task risk, and unverified work is not marked sufficient."""
from __future__ import annotations

import pytest

from desktop.core.fusion import review_policy, step_budget
from desktop.core.fusion.review_policy import OptionalCheck, plan_review
from desktop.core.fusion.step_budget import Side


# --------------------------------------------------------------------------- plan_review


def test_the_runner_is_mandatory_at_every_preference():
    for preference in (0, 50, 100):
        plan = plan_review(preference, risk=0, runnable=True, has_current_evidence=False)
        assert plan.runner, f"preference {preference} must not buy the mandatory checks away"
        assert plan.sufficient


def test_matching_final_evidence_is_not_rerun():
    plan = plan_review(100, risk=80, runnable=True, has_current_evidence=True)
    assert not plan.runner and plan.sufficient
    assert any("current evidence" in reason for reason in plan.reasons)


def test_optional_checks_follow_the_preference_bar():
    pool = [("low", 40), ("mid", 60), ("high", 90)]
    assert plan_review(0, 0, True, False, pool).checks == ()
    plan50 = plan_review(50, 0, True, False, pool)
    assert [check.name for check in plan50.checks] == ["high", "mid"], "a check's risk plus the preference must reach 100"
    plan100 = plan_review(100, 0, True, False, pool)
    assert [check.name for check in plan100.checks] == ["high", "mid", "low"]


def test_optional_checks_are_sorted_by_risk_and_deduplicated():
    pool = [OptionalCheck("x", 30), ("y", 80), {"name": "x", "risk": 60}, ("z", 80)]
    plan = plan_review(50, 0, True, False, pool)
    # Duplicates keep the higher risk; equal risks keep the proposal order.
    assert [(check.name, check.risk) for check in plan.checks] == [("y", 80), ("z", 80), ("x", 60)]


def test_high_task_risk_selects_coverage_at_zero_preference():
    plan = plan_review(0, risk=100, runnable=True, has_current_evidence=False,
                       optional_checks=[("a", 0), ("b", 30)])
    assert [check.name for check in plan.checks] == ["b", "a"] and plan.sampler


def test_the_sampler_is_decided_by_risk_plus_preference():
    assert plan_review(50, 50, True, False).sampler
    assert not plan_review(50, 49, True, False).sampler
    assert plan_review(100, 0, True, False).sampler, "a fully quality-lean session always spot-checks"
    assert not plan_review(0, 99, True, False).sampler


def test_the_l1_review_needs_high_risk_and_a_very_high_preference():
    assert plan_review(90, 70, True, False).review
    assert not plan_review(89, 100, True, False).review, "the preference bar is not traded away"
    assert not plan_review(100, 69, True, False).review, "the risk bar is not traded away"
    assert plan_review(90, 70, True, False).sufficient


def test_insufficient_verification_is_not_marked_success():
    plan = plan_review(0, 0, runnable=False, has_current_evidence=False)
    assert not plan.runner and not plan.sampler and not plan.checks and not plan.sufficient
    assert any("insufficient" in reason for reason in plan.reasons)
    # A task without runnable checks is still verifiable through the sampler.
    plan = plan_review(100, 0, runnable=False, has_current_evidence=False)
    assert plan.sampler and plan.sufficient


def test_indices_and_candidates_are_validated():
    for bad in (-1, 101, True, 1.5, "50"):
        with pytest.raises(ValueError):
            plan_review(bad, 0, True, False)
        with pytest.raises(ValueError):
            plan_review(0, bad, True, False)
    for bad_item in ({"name": " ", "risk": 10}, {"name": "x", "risk": 101}, ("x",), 7, "x"):
        with pytest.raises(ValueError):
            plan_review(0, 0, True, False, [bad_item])


# --------------------------------------------------------------------------- risk-aware step suggestion


def test_the_rework_allowance_grows_the_adaptive_band_with_risk():
    assert step_budget.adaptive_band(0) == (16, 32), "risk defaults to zero"
    assert step_budget.adaptive_band(0, risk=100) == (24, 48), "up to +50% of the first-pass floor"
    assert step_budget.adaptive_band(4, risk=50) == (40, 80)
    assert step_budget.adaptive_band(20, risk=100) == (144, 288), "the allowance may pass the first-pass cap"
    assert step_budget.default_steps(0) == 16 and step_budget.default_steps(0, risk=100) == 24


def test_high_risk_tasks_are_not_pinned_to_the_band_floor():
    band = step_budget.resolve_band(Side(True, 0), Side(True, 0), size=0, cap=1000, risk=100)
    assert (band.low, band.high) == (24, 48) and "risk 100" in band.source
    assert step_budget.effective_steps(8, band) == 24, "the request is clamped to the risk-aware floor, not the bare minimum"


def test_manual_bounds_and_the_global_cap_still_win_over_risk():
    band = step_budget.resolve_band(Side(False, 50), Side(True, 0), size=0, cap=1000, risk=100)
    assert (band.low, band.high) == (50, 50) and band.source.startswith("manual min 50 + adaptive max 50")
    band = step_budget.resolve_band(Side(True, 0), Side(True, 0), size=0, cap=10, risk=100)
    assert (band.low, band.high) == (10, 10) and "global cap 10" in band.source


def test_omitting_risk_keeps_the_legacy_band():
    for size in range(21):
        assert step_budget.adaptive_band(size, risk=0) == step_budget.adaptive_band(size)
        plain = step_budget.resolve_band(Side(True, 0), Side(True, 0), size, 1000)
        assert step_budget.resolve_band(Side(True, 0), Side(True, 0), size, 1000, risk=0) == plain
