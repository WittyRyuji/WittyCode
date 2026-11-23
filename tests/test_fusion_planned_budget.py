"""A validated plan's ``budget`` becomes local delegation advice: plan ``turns`` and ``seconds``
cap each delegation inside the configured band and the session limit — never widening either —
and plan ``tokens`` stays an estimate threshold the runtime checks honestly against this turn's
known spend. Adaptive fields leave the configured sides in charge and unknown usage can never
pass for zero: the gate says the budget needs review instead."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from desktop.core.fusion import step_budget
from desktop.core.fusion.protocol import Brief
from desktop.core.fusion.registry import FusionRegistry, Policy
from desktop.core.fusion.runtime import FusionRuntime, RouteTurn

pytestmark = pytest.mark.fusion_gates


def _runtime(tmp_path, plan_budget=None, *, preference=50, max_seconds=60, max_tokens=0, **gate_overrides):
    policy = Policy("chatgpt:main", ("chatgpt:worker",), "chatgpt:light", 0, max_tokens, max_seconds,
                    preference, {})
    registry = FusionRegistry(tmp_path, policy)
    settings = SimpleNamespace(fusion_steps_min=4, fusion_steps_min_adaptive=True,
                               fusion_steps_max=200, fusion_steps_max_adaptive=True,
                               max_steps=1000, fusion_gates={"v3": True, **gate_overrides})
    manager = SimpleNamespace(settings=settings,
                              model_info=lambda native: SimpleNamespace(efforts=("low", "medium", "high", "max")))
    turn = object()
    receipt = {"turn": turn, "status": "open",
               "workplan": {"delivery": "direct", "budget": plan_budget} if plan_budget is not None else None}

    def _declare_state():
        return receipt if receipt.get("turn") is turn else None

    parent = SimpleNamespace(manager=manager, project=tmp_path, _turn=turn, _declare_state=_declare_state,
                             usage=SimpleNamespace(total=0), session_id="fusion-plan-budget",
                             saved=SimpleNamespace(origin={}, plan={}, goal={}))
    rt = FusionRuntime(parent, registry, owner="fusion-plan-budget-test", main_tokens=0)
    rt.__dict__["identities"] = SimpleNamespace(get=lambda name: SimpleNamespace(role="worker"))
    return rt


def _brief(**budget):
    return Brief("ws", "Coder", "fix the thing", "keep it small", "it works", ("a.py",), dict(budget))


# --------------------------------------------------------------------------- resolver


def test_planned_budget_resolves_the_plan_fields():
    band = step_budget.Band(20, 40, "adaptive min 20 + adaptive max 40")
    planned = step_budget.planned_budget({"turns": 30, "seconds": 120, "tokens": 5000}, band, 60)
    assert planned is not None
    assert planned.steps == 30 and planned.seconds == 60 and planned.tokens == 5000
    assert "30" in planned.note and "60" in planned.note and "5000" in planned.note


def test_planned_budget_clamps_into_the_band_and_session_cap():
    band = step_budget.Band(20, 40, "adaptive")
    planned = step_budget.planned_budget({"turns": 500, "seconds": 9999, "tokens": "adaptive"}, band, 60)
    assert planned.steps == 40 and planned.seconds == 60 and planned.tokens is None
    low = step_budget.planned_budget({"turns": 1, "seconds": 0, "tokens": 0}, band, 60)
    # The step floor still wins; a plan can only declare positive numbers, so a stray zero adds no cap.
    assert low.steps == 20 and low.seconds is None and low.tokens == 0


def test_planned_budget_adaptive_adds_no_cap():
    band = step_budget.Band(20, 40, "adaptive")
    planned = step_budget.planned_budget({"turns": "adaptive", "seconds": "adaptive", "tokens": "adaptive"}, band, 60)
    assert planned == step_budget.PlannedBudget(None, None, None, "plan budget adaptive")


def test_planned_budget_ignores_a_missing_plan():
    assert step_budget.planned_budget(None, step_budget.Band(20, 40, ""), 60) is None


# --------------------------------------------------------------------------- into the brief


def test_bounded_brief_applies_smaller_plan_numbers(tmp_path):
    rt = _runtime(tmp_path, plan_budget={"turns": 20, "seconds": 30, "tokens": "adaptive"})
    bounded = rt._bounded_brief(_brief(steps=60, seconds=600))
    assert bounded.budget["steps"] == 20  # band clamps 60 to 40, the plan's 20 tightens further
    assert bounded.budget["seconds"] == 30
    assert "plan turns 20" in bounded.budget_note and "plan seconds 30" in bounded.budget_note


def test_bounded_brief_plan_never_widens_the_band_or_session_cap(tmp_path):
    rt = _runtime(tmp_path, plan_budget={"turns": 500, "seconds": 9999, "tokens": "adaptive"})
    bounded = rt._bounded_brief(_brief(steps=4, seconds=10))
    assert bounded.budget["steps"] == 20  # manual-ish floor of the adaptive band still wins
    assert bounded.budget["seconds"] == 10  # plan's 9999 resolves to the session cap but never raises the brief's 10


def test_bounded_brief_plan_seconds_apply_when_the_brief_asked_for_none(tmp_path):
    rt = _runtime(tmp_path, plan_budget={"turns": "adaptive", "seconds": 25, "tokens": "adaptive"})
    bounded = rt._bounded_brief(_brief(steps=60))
    assert bounded.budget["seconds"] == 25 and bounded.budget["steps"] == 40


def test_plan_budget_can_tighten_the_default_brief_budget(tmp_path):
    rt = _runtime(tmp_path, plan_budget={"turns": 24, "seconds": 25, "tokens": "adaptive"})
    bounded = rt._bounded_brief(_brief())
    assert bounded.budget == {"steps": 24, "seconds": 25}
    assert "plan turns 24" in bounded.budget_note and "plan seconds 25" in bounded.budget_note


@pytest.mark.parametrize(("budget", "steps"), [(None, 1), ({}, 1), ({"steps": 8}, 20)],
                         ids=["omitted", "empty", "explicit-keeps-floor"])
def test_dispatch_default_budget_respects_plan_turns_below_configured_floor(tmp_path, monkeypatch, budget, steps):
    import asyncio

    from tests.fake_responses import call, say
    from tests.test_fusion_identity_runtime import _brief as task_brief, _fusion, _output, _turn

    async def go():
        async with _fusion(tmp_path) as (main, fake):
            monkeypatch.setattr(main._fusion, "_plan_budget", lambda: {"turns": 1, "seconds": 25, "tokens": "adaptive"})
            args = task_brief(budget=budget)
            if budget is None:
                del args["budget"]
            await _turn(main, fake, call("delegate_task", args, call_id="delegate"), say("Done."), say("Reviewed."))
            agent, = main.agents.values()
            assert agent.record.status == "done" and agent.controller.turn_step_limit() == steps
            if not budget:
                assert "Default budget: 1 step, 25 seconds" in _output(main, "delegate")
    asyncio.run(go())


def test_bounded_brief_without_a_plan_keeps_existing_behavior(tmp_path):
    rt = _runtime(tmp_path, plan_budget=None)
    bounded = rt._bounded_brief(_brief(steps=60, seconds=600))
    assert bounded.budget["steps"] == 40 and bounded.budget["seconds"] == 600
    assert "plan" not in bounded.budget_note


def test_plan_budget_needs_v3(tmp_path):
    rt = _runtime(tmp_path, plan_budget={"turns": 20, "seconds": 30, "tokens": "adaptive"}, v3=False)
    bounded = rt._bounded_brief(_brief(steps=60, seconds=600))
    assert bounded.budget["steps"] == 40 and bounded.budget["seconds"] == 600
    assert "plan" not in bounded.budget_note


# --------------------------------------------------------------------------- the token estimate gate


def test_token_gate_passes_while_the_turn_stays_under_the_estimate(tmp_path):
    rt = _runtime(tmp_path, plan_budget={"tokens": 1000, "turns": "adaptive", "seconds": "adaptive"})
    assert rt._plan_budget_gate("small task") == ""


def test_token_gate_counts_spend_within_the_turn(tmp_path):
    rt = _runtime(tmp_path, plan_budget={"tokens": 100, "turns": "adaptive", "seconds": "adaptive"})
    rt.route_turn = RouteTurn()
    rt.registry.tokens = 5000  # earlier turns: the baseline, not this turn's spend
    assert rt._plan_budget_gate("x") == ""  # first check fixes the baseline at 5000
    rt.registry.tokens += 95
    assert rt._plan_budget_gate("x") == ""  # 95 + estimate(1) <= 100
    rt.registry.tokens += 5
    refusal = rt._plan_budget_gate("x")
    assert refusal and "token" in refusal and "review" in refusal


def test_token_gate_refuses_when_the_task_alone_would_exceed_the_estimate(tmp_path):
    rt = _runtime(tmp_path, plan_budget={"tokens": 10, "turns": "adaptive", "seconds": "adaptive"})
    refusal = rt._plan_budget_gate("x" * 350)  # input estimate ~101 tokens
    assert refusal and "estimate" in refusal


def test_token_gate_unknown_usage_needs_review_never_zero(tmp_path):
    rt = _runtime(tmp_path, plan_budget={"tokens": 1000, "turns": "adaptive", "seconds": "adaptive"})
    rt.registry.unmetered = True
    refusal = rt._plan_budget_gate("x")
    assert refusal and "unknown" in refusal and "review" in refusal


def test_token_gate_silent_without_a_numeric_plan_budget(tmp_path):
    for budget in (None, {"tokens": "adaptive", "turns": "adaptive", "seconds": "adaptive"}):
        rt = _runtime(tmp_path, plan_budget=budget)
        assert rt._plan_budget_gate("x" * 100000) == ""
