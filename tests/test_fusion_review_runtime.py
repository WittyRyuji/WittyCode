"""The V3 acceptance verdict with a stub runtime: a self_check re-route never erases the checks
a delegated write owes, and only the latest check run that started after the last write,
finished cleanly, reported done and kept evidence counts."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from desktop.core.fusion.protocol import Route
from desktop.core.fusion.registry import FusionRegistry, Policy
from desktop.core.fusion.runtime import DelegationResult, FusionRuntime, RouteTurn

pytestmark = pytest.mark.fusion_gates


def _runtime(tmp_path, preference=50, reasoning=None, **gate_overrides):
    policy = Policy("chatgpt:main", ("chatgpt:worker",), "chatgpt:light", 0, 0, 60, preference, reasoning or {})
    registry = FusionRegistry(tmp_path, policy)
    settings = SimpleNamespace(fusion_steps_min=4, fusion_steps_min_adaptive=True,
                               fusion_steps_max=200, fusion_steps_max_adaptive=True,
                               max_steps=1000, fusion_gates=dict(gate_overrides))
    manager = SimpleNamespace(settings=settings,
                              model_info=lambda native: SimpleNamespace(efforts=("low", "medium", "high", "max")))
    parent = SimpleNamespace(manager=manager, project=tmp_path,
                             session_id="fusion-review", saved=SimpleNamespace(origin={}, plan={}, goal={}))
    return FusionRuntime(parent, registry, owner="fusion-review-test", main_tokens=0)


_SEATS = {"Runner": "L3", "QualitySampler": "L2"}


def _work(identity, *, paths=(), started=0.0, finished=0.0, status="done", report_status="done", has_evidence=True, seat=None):
    return DelegationResult(identity, identity, paths, started, finished, status, report_status, has_evidence,
                            _SEATS.get(identity, "") if seat is None else seat)


def _verdict(rt, delegations, *, route=None, submitted_at=0.0, writing_route=None, workstreams=(), observer=False):
    turn = RouteTurn()
    turn.route = route or Route("coder", "why", "runner")
    turn.submitted_at = submitted_at
    turn.writing_route = writing_route
    turn.delegations.extend(delegations)
    turn.workstreams.update(workstreams)
    turn.observer_reported = observer
    rt.route_turn = turn
    return rt.acceptance_verdict()


def _freeze(rt, score=0.8, workstream="ws"):
    rt.registry.freeze([{"id": "i-" + workstream, "workstream": workstream, "role": "worker",
                         "score": score, "reason": "hard"}], rt.registry.policy.preference)


# --------------------------------------------------------------------------- self_check must not waive


def test_a_self_check_reroute_does_not_erase_the_owed_runner(tmp_path):
    rt = _runtime(tmp_path)
    verdict = _verdict(rt, [_work("Coder", paths=("a.py",), finished=10.0)],
                       route=Route("self", "I checked it myself.", "self_check"), submitted_at=30.0,
                       writing_route=Route("coder", "delegated the change", "runner"))
    assert verdict is not None and verdict.missing == ("runner",), "the re-route explains the gap; it does not waive it"


def test_the_legacy_mode_still_allows_the_self_check_waiver(tmp_path):
    rt = _runtime(tmp_path, v3=False)
    verdict = _verdict(rt, [_work("Coder", paths=("a.py",), finished=10.0)],
                       route=Route("self", "I checked it myself.", "self_check"), submitted_at=30.0,
                       writing_route=Route("coder", "delegated the change", "runner"))
    assert verdict is not None and verdict.missing == () and verdict.skip_reason


# --------------------------------------------------------------------------- what counts as evidence


def test_a_check_run_started_after_the_last_write_counts_once(tmp_path):
    rt = _runtime(tmp_path)
    verdict = _verdict(rt, [_work("Coder", paths=("a.py",), finished=10.0),
                            _work("Runner", started=11.0, finished=12.0),
                            _work("Runner", started=12.5, finished=13.0)])
    assert verdict.missing == () and verdict.checks == ("runner",), "the kind is satisfied once"


@pytest.mark.parametrize("runner", [
    _work("Runner", started=5.0, finished=12.0),                    # started during the writes: stale
    _work("Runner", started=11.0, finished=12.0, has_evidence=False),  # report done but no evidence
    _work("Runner", started=11.0, finished=12.0, report_status="concerns"),
    _work("Runner", started=11.0, finished=12.0, report_status="blocked"),
    _work("Runner", started=11.0, finished=12.0, report_status="needs_context"),
    _work("Runner", started=11.0, finished=12.0, report_status=""),    # no structured report
    _work("Runner", started=11.0, finished=12.0, status="failed"),     # the run itself failed
])
def test_failed_stale_or_thin_runs_are_not_evidence(tmp_path, runner):
    rt = _runtime(tmp_path)
    verdict = _verdict(rt, [_work("Coder", paths=("a.py",), finished=10.0), runner])
    assert verdict.missing == ("runner",), f"{runner.report_status or runner.status} must not pass"


def test_a_later_failed_run_voids_the_earlier_pass(tmp_path):
    rt = _runtime(tmp_path)
    writer = _work("Coder", paths=("a.py",), finished=10.0)
    verdict = _verdict(rt, [writer, _work("Runner", started=11.0, finished=12.0),
                            _work("Runner", started=12.5, finished=13.0, status="failed", report_status="")])
    assert verdict.missing == ("runner",), "the latest run of the kind decides"
    verdict = _verdict(rt, [writer, _work("Runner", started=11.0, finished=12.0, status="failed", report_status=""),
                            _work("Runner", started=12.5, finished=13.0)])
    assert verdict.missing == ()


# --------------------------------------------------------------------------- risk drives the sampler


def test_risk_signals_reach_the_sampler_by_score_and_preference(tmp_path):
    writer = _work("Coder", paths=("a.py",), finished=10.0, report_status="concerns")
    runner = _work("Runner", started=11.0, finished=12.0)
    cheap = _runtime(tmp_path / "cheap", preference=0)
    _freeze(cheap)
    verdict = _verdict(cheap, [writer, runner], workstreams=("ws",))
    assert verdict.missing == (), "one signal at cost-lean preference stays under the sampler bar"
    balanced = _runtime(tmp_path / "balanced", preference=50)
    _freeze(balanced)
    verdict = _verdict(balanced, [writer, runner], workstreams=("ws",))
    assert verdict.missing == ("sampler",), "the same signal on a hard item escalates"


def test_runner_and_sampler_are_counted_separately(tmp_path):
    rt = _runtime(tmp_path)
    route = Route("coder", "why", "runner_and_sampler")
    writer = _work("Coder", paths=("a.py",), finished=10.0)
    runner = _work("Runner", started=11.0, finished=12.0)
    sampler = _work("QualitySampler", started=11.0, finished=12.0)
    assert _verdict(rt, [writer, runner], route=route).missing == ("sampler",)
    assert _verdict(rt, [writer, sampler], route=route).missing == ("runner",)
    assert _verdict(rt, [writer, runner, sampler], route=route).missing == ()


# --------------------------------------------------------------------------- no runnable + no evidence: never verified


def test_a_self_check_claim_without_evidence_is_not_verified(tmp_path):
    rt = _runtime(tmp_path)
    verdict = _verdict(rt, [_work("Coder", paths=("a.py",), finished=10.0)],
                       route=Route("coder", "I checked the change myself.", "self_check"))
    assert verdict.missing == ("runner",) and not verdict.checks, \
        "nothing runnable was declared and nothing is on record, so it cannot be verified"


def test_the_self_check_reason_is_kept_but_cannot_hide_the_owed_check(tmp_path):
    rt = _runtime(tmp_path)
    verdict = _verdict(rt, [_work("Coder", paths=("a.py",), finished=10.0)],
                       route=Route("self", "Prose only; there is nothing to run.", "self_check"), submitted_at=30.0,
                       writing_route=Route("coder", "delegated the change", "runner"))
    assert verdict.missing == ("runner",) and verdict.skip_reason == "Prose only; there is nothing to run."


# --------------------------------------------------------------------------- the check seats


def test_each_check_kind_matches_its_own_seat(tmp_path):
    rt = _runtime(tmp_path)
    route = Route("coder", "why", "runner_and_sampler")
    writer = _work("Coder", paths=("a.py",), finished=10.0)
    sample = _work("QualitySampler", started=11.0, finished=12.0)
    review = _work("QualitySampler", seat="L1", started=12.5, finished=13.0)
    verdict = _verdict(rt, [writer, review], route=route)
    assert verdict.checks == ("review",) and verdict.missing == ("runner", "sampler"), \
        "the L1 review is its own check and never doubles as the L2 sample"
    assert _verdict(rt, [writer, sample], route=route).checks == ("sampler",)
    wrong_seat = _work("Runner", seat="", started=11.0, finished=12.0)
    assert _verdict(rt, [writer, wrong_seat], route=route).missing == ("runner", "sampler"), \
        "a Runner not seated at L3 does not count as the overall check"


def test_the_l1_review_is_required_only_on_high_risk_at_a_very_high_preference(tmp_path):
    writer = _work("Coder", paths=("a.py",), finished=10.0)
    runner = _work("Runner", started=11.0, finished=12.0)
    sampler = _work("QualitySampler", started=12.5, finished=13.0)
    review = _work("QualitySampler", seat="L1", started=13.5, finished=14.0)
    high = _runtime(tmp_path / "high", preference=95)
    _freeze(high)
    verdict = _verdict(high, [writer, runner, sampler], workstreams=("ws",))
    assert verdict.missing == ("review",), "a high-risk delivery on a quality-lean session owes the L1 review"
    assert _verdict(high, [writer, runner, sampler, review], workstreams=("ws",)).missing == ()
    neutral = _runtime(tmp_path / "neutral", preference=50)
    _freeze(neutral)
    assert _verdict(neutral, [writer, runner, sampler], workstreams=("ws",)).missing == (), \
        "the same risk at a neutral preference does not conjure an extra check"


def test_the_harness_seats_each_check_by_kind_and_escalation(tmp_path):
    rt = _runtime(tmp_path, preference=95)
    _freeze(rt)
    assert rt._check_seat("Runner", None) == "L3"
    assert rt._check_seat("QualitySampler", None) == "L2"
    turn = RouteTurn()
    turn.delegations.append(_work("Coder", paths=("a.py",), finished=10.0))
    turn.workstreams.add("ws")
    assert rt._check_seat("QualitySampler", turn) == "L2", "the L2 sample must be on record first"
    turn.delegations.append(_work("QualitySampler", started=11.0, finished=12.0))
    assert rt._check_seat("QualitySampler", turn) == "L1", "L2 evidence plus high risk unlocks the review seat"
    neutral = _runtime(tmp_path / "neutral", preference=50)
    assert neutral._check_seat("QualitySampler", turn) == "L2", "below the review bar the seat stays L2"


def test_a_seated_expert_takes_the_seat_reasoning_before_the_identity_default(tmp_path):
    rt = _runtime(tmp_path, reasoning={"light:chatgpt:light": "low", "main:chatgpt:main": "max"})
    profile = SimpleNamespace(reasoning="low")  # the identity's declared default
    assert rt._expert_effort(SimpleNamespace(model="chatgpt:main", role="worker", seat="L1"), profile) == "max", \
        "the seat wins over the identity's reasoning"
    assert rt._expert_effort(SimpleNamespace(model="chatgpt:light", role="light", seat=""), profile) == "low", \
        "unseated experts keep the identity default"
