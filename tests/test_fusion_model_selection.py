"""Offline tests for deterministic Fusion model selection: no network and no product model calls."""
from __future__ import annotations

import json
from decimal import Decimal

import pytest

from desktop.core.fusion.model_selection import actual_model_id, assess_difficulty, resolve_selection


def offer(model: str, score=None, cost=None, *, actual: str | None = None) -> dict:
    row = {"id": model}
    if actual is not None:
        row["actual_model"] = actual
    if score is not None:
        row["score"] = score
    if cost is not None:
        row["cost"] = cost
    return row


def pools(l1=None, l2=None, l3=None) -> dict:
    return {"L1": l1 or [], "L2": l2 or [], "L3": l3 or []}


def standard_pools() -> dict:
    return pools(
        l1=[offer("chatgpt:lead", 70, 20)],
        l2=[offer("chatgpt:worker-a", 60, 5), offer("chatgpt:worker-b", 50, 2)],
        l3=[offer("chatgpt:light-a", 40, 1), offer("chatgpt:light-b", 30, 0.5)],
    )


def test_difficulty_endpoints_and_all_101_preference_steps_are_exact():
    zero = assess_difficulty(0, 10, 10, 10)
    fifty = assess_difficulty(50, 10, 10, 10)
    hundred = assess_difficulty(100, 10, 10, 10)
    assert (zero["x"], zero["worker_L3_upper"], zero["worker_L1_lower"]) == ("0", "0.35", "1.05")
    assert (fifty["x"], fifty["worker_L3_upper"], fifty["worker_L1_lower"]) == ("0.5", "0.2", "0.85")
    assert (hundred["x"], hundred["worker_L3_upper"], hundred["worker_L1_lower"]) == ("1", "0.05", "0.65")
    assert zero["worker_seat"] == "L2", "D tops out at 1, below the N=0 L1 gate 1.05"
    assert fifty["worker_seat"] == hundred["worker_seat"] == "L1"
    assert zero["D"] == fifty["D"] == hundred["D"] == "1"

    previous_low = previous_high = None
    for n in range(101):
        row = assess_difficulty(n, 0, 0, 0)
        assert row["worker_seat"] == "L3"
        assert row["h"] == "0" and row["x"] == str(n / 100).rstrip("0").rstrip(".") if n not in (0, 100) else True
        low, high = row["worker_L3_upper"], row["worker_L1_lower"]
        if previous_low is not None:
            assert float(low) < previous_low and float(high) < previous_high
        previous_low, previous_high = float(low), float(high)
    json.dumps(assess_difficulty(72, 8, 6, 4))


def test_difficulty_formula_and_boundary_seats():
    assert assess_difficulty(50, 6, 7, 9)["D"] == "0.7"  # (12 + 14 + 9) / 50
    assert assess_difficulty(50, 0, 0, 0)["worker_seat"] == "L3"
    assert assess_difficulty(50, 5, 5, 5)["worker_seat"] == "L2"
    assert assess_difficulty(50, 10, 10, 10)["worker_seat"] == "L1"
    with pytest.raises(ValueError):
        assess_difficulty(50.0, 5, 5, 5)
    with pytest.raises(ValueError):
        assess_difficulty(50, 11, 5, 5)


def test_utility_switches_a60_c5_to_b62_c10_between_n71_and_n72():
    model_a = offer("chatgpt:a", 60, 5)
    model_b = offer("chatgpt:b", 62, 10)
    base = pools(l1=[offer("chatgpt:lead", 70, 20)], l2=[model_b, model_a], l3=[offer("chatgpt:light", 30, 1), offer("chatgpt:checker", 20, 0.5)])
    at71 = resolve_selection(71, 5, 5, 5, base)
    at72 = resolve_selection(72, 5, 5, 5, base)
    assert at71["ok"] and at72["ok"]
    assert at71["worker"]["model"] == "chatgpt:a"
    assert at72["worker"]["model"] == "chatgpt:b"
    assert at71["worker"]["utility"] == "8.23"
    assert at72["worker"]["utility"] == "8.368"


def test_two_l2_models_pair_best_worker_with_independent_l2_reviewer():
    result = resolve_selection(50, 5, 0, 10, standard_pools(), review_required=True)
    assert result["ok"]
    assert result["worker"]["actual_model"] == "worker-a"
    assert result["review"]["seat"] == "L2"
    assert result["review"]["actual_model"] == "worker-b"
    assert result["review"]["actual_model"] != result["worker"]["actual_model"]
    assert result["review"]["identity"] != "Coder"


def test_runner_may_share_the_worker_model_when_no_qs_is_required():
    same = offer("chatgpt:same", 60, 5)
    result = resolve_selection(0, 0, 0, 0, pools(l3=[same]))
    assert result["ok"]
    assert result["worker"]["actual_model"] == result["runner"]["actual_model"] == "same"
    assert not result["review"]["required"]


def test_no_self_review_when_qs_is_required_and_no_independent_candidate_exists():
    same = offer("chatgpt:same", 60, 5)
    result = resolve_selection(0, 0, 0, 0, pools(l3=[same]), review_required=True)
    assert not result["ok"]
    assert result["worker"] is None and result["runner"] is None
    assert any("no independent L1, L2 or L3 model is available for QualitySampler review" in reason for reason in result["reasons"])


def test_cross_level_review_at_n50_prefers_l3_for_low_importance_and_l1_for_high_importance():
    both = pools(
        l1=[offer("chatgpt:lead", 70, 20)],
        l2=[offer("chatgpt:writer", 60, 5)],
        l3=[offer("chatgpt:light", 30, 1), offer("chatgpt:runner", 20, 0.5)],
    )
    low_importance = resolve_selection(50, 5, 0, 10, both, review_required=True)
    high_importance = resolve_selection(50, 0, 10, 10, both, review_required=True)
    assert low_importance["ok"] and high_importance["ok"]
    assert low_importance["review"]["seat"] == "L3"
    assert high_importance["review"]["seat"] == "L1"
    assert low_importance["derived"]["review_crossover_N"] == 75
    assert high_importance["derived"]["review_crossover_N"] == 25


def test_cross_level_review_uses_the_only_available_side_regardless_of_crossover():
    only_l1 = pools(l1=[offer("chatgpt:lead", 70, 20)], l2=[offer("chatgpt:writer", 60, 5)], l3=[offer("chatgpt:writer-light", 30, 1), offer("chatgpt:runner", 20, 0.5)])
    result = resolve_selection(50, 5, 0, 10, only_l1, review_required=True)
    # L3 is available here, so this is the low-importance crossover choice. Remove independent L3 below.
    assert result["review"]["seat"] == "L3"
    only_l1["L3"] = [offer("chatgpt:writer", 30, 1)]
    result = resolve_selection(50, 5, 0, 10, only_l1, review_required=True)
    assert result["ok"] and result["review"]["seat"] == "L1"


def test_lead_review_is_unique_and_l1_write_conflict_moves_the_worker_to_l2():
    result = resolve_selection(50, 10, 10, 10, standard_pools())
    assert result["ok"]
    assert result["derived"]["worker_seat"] == "L1"
    assert result["worker"]["seat"] == "L2"
    assert result["review"]["kind"] == "lead"
    assert result["review"]["seat"] == "L1" and result["review"]["actual_model"] == "lead"
    assert "conflict moves the worker to L2" in " ".join(result["reasons"])


def test_single_lead_cannot_review_its_own_actual_model():
    conflicted = pools(
        l1=[offer("chatgpt:same-lead", 70, 20)],
        l2=[offer("chatgpt:same-lead", 60, 5)],
        l3=[offer("chatgpt:light", 30, 1), offer("chatgpt:runner", 20, 0.5)],
    )
    result = resolve_selection(50, 10, 10, 10, conflicted)
    assert not result["ok"]
    assert any("no independent L1 model" in reason for reason in result["reasons"])


def test_author_actual_models_are_protected_from_review_pairing():
    result = resolve_selection(
        50, 5, 0, 10, standard_pools(),
        authors=[{"identity": "Coder", "id": "chatgpt:worker-b"}],
        review_required=True,
    )
    assert result["ok"]
    assert result["worker"]["actual_model"] == "worker-a"
    assert result["review"]["seat"] == "L3", "worker-b is protected as an author, so no independent L2 remains"
    assert result["review"]["actual_model"] not in {"worker-a", "worker-b"}


def test_api_alias_is_not_an_actual_identity_and_duplicate_actual_models_are_deduped():
    with pytest.raises(ValueError, match="alias"):
        actual_model_id("api:provider:display-name")
    with pytest.raises(ValueError, match="alias"):
        resolve_selection(50, 5, 5, 5, pools(l2=[offer("api:p:alias", 60, 5)], l3=[offer("chatgpt:light")]))
    with pytest.raises(ValueError, match="does not match"):
        resolve_selection(50, 5, 5, 5, pools(l2=[offer("chatgpt:real", 60, 5, actual="other")], l3=[offer("chatgpt:light")]))

    rows = pools(
        l2=[
            offer("api:p:first-alias", 50, 2, actual="real-a"),
            offer("api:p:second-alias", 60, 5, actual="real-a"),
            offer("api:p:independent", 40, 1, actual="real-b"),
        ],
        l3=[offer("chatgpt:light", 30, 1), offer("chatgpt:runner", 20, 0.5)],
    )
    result = resolve_selection(50, 5, 0, 10, rows, review_required=True)
    assert result["ok"]
    assert result["worker"]["model"] == "api:p:second-alias"
    assert result["worker"]["actual_model"] == "real-a"
    assert result["review"]["actual_model"] == "real-b"


def test_unknown_profile_is_never_zero_but_a_single_candidate_needs_no_ranking():
    single = pools(l2=[offer("chatgpt:mystery")], l3=[offer("chatgpt:light", 30, 1), offer("chatgpt:runner", 20, 0.5)])
    result = resolve_selection(50, 5, 5, 5, single)
    assert result["ok"]
    assert result["worker"]["actual_model"] == "mystery"
    assert result["worker"]["score"] is None and result["worker"]["cost"] is None and result["worker"]["utility"] is None

    mixed = pools(
        l2=[offer("chatgpt:mystery"), offer("chatgpt:known", 1, 100), offer("chatgpt:independent", 40, 1)],
        l3=[offer("chatgpt:light", 30, 1), offer("chatgpt:runner", 20, 0.5)],
    )
    result = resolve_selection(50, 5, 5, 5, mixed)
    assert result["ok"]
    assert result["worker"]["actual_model"] == "independent", "complete profiles outrank unknowns; missing score is not zero"


def test_unknown_profiles_have_a_stable_order_and_output_is_json_serializable():
    rows = pools(
        l2=[offer("chatgpt:z-unknown"), offer("chatgpt:a-unknown"), offer("chatgpt:independent")],
        l3=[offer("chatgpt:light"), offer("chatgpt:runner")],
    )
    first = resolve_selection(50, 5, 5, 5, rows)
    second = resolve_selection(50, 5, 5, 5, rows)
    assert first == second
    assert first["worker"]["actual_model"] == "z-unknown", "unknown utilities keep configured order, not name order"
    assert "configured-order fallback" in first["worker"]["rank_basis"]
    json.dumps(first)


def test_cost_endpoint_compares_cost_even_when_score_is_unknown():
    rows = pools(
        l2=[
            offer("chatgpt:score-only", score=100),
            offer("chatgpt:complete", 80, 4),
            offer("chatgpt:cost-only", cost=1),
        ],
        l3=[offer("chatgpt:light", 30, 1), offer("chatgpt:runner", 20, 0.5)],
    )
    result = resolve_selection(0, 5, 5, 5, rows)
    assert result["ok"]
    assert result["worker"]["actual_model"] == "cost-only"
    assert result["worker"]["utility"] == "-0.2"
    assert result["worker"]["score"] is None
    assert result["worker"]["rank_basis"].startswith("cost-only endpoint utility")


def test_score_endpoint_compares_score_even_when_cost_is_unknown():
    rows = pools(
        l2=[
            offer("chatgpt:cost-only", cost=0),
            offer("chatgpt:complete", 60, 100),
            offer("chatgpt:score-only", score=70),
        ],
        l3=[offer("chatgpt:light", 30, 1), offer("chatgpt:runner", 20, 0.5)],
    )
    result = resolve_selection(100, 5, 5, 5, rows)
    assert result["ok"]
    assert result["worker"]["actual_model"] == "score-only"
    assert result["worker"]["utility"] == "14"
    assert result["worker"]["cost"] is None
    assert result["worker"]["rank_basis"].startswith("score-only endpoint utility")


def test_endpoint_ties_with_missing_secondary_metrics_use_configured_order():
    cost_rows = pools(
        l2=[offer("chatgpt:first-cost", cost=2), offer("chatgpt:second-cost", cost=2)],
        l3=[offer("chatgpt:light", 30, 1), offer("chatgpt:runner", 20, 0.5)],
    )
    result = resolve_selection(0, 5, 5, 5, cost_rows)
    assert result["ok"] and result["worker"]["actual_model"] == "first-cost"
    assert "missing score cannot break ties" in result["worker"]["rank_basis"]

    score_rows = pools(
        l2=[offer("chatgpt:first-score", score=60), offer("chatgpt:second-score", score=60)],
        l3=[offer("chatgpt:light", 30, 1), offer("chatgpt:runner", 20, 0.5)],
    )
    result = resolve_selection(100, 5, 5, 5, score_rows)
    assert result["ok"] and result["worker"]["actual_model"] == "first-score"
    assert "missing cost cannot break ties" in result["worker"]["rank_basis"]


def test_midpoint_partial_unknowns_fall_back_to_configured_order_not_zero_or_names():
    forward = pools(
        l2=[offer("chatgpt:z-cost-only", cost=1), offer("chatgpt:a-score-only", score=100)],
        l3=[offer("chatgpt:light", 30, 1), offer("chatgpt:runner", 20, 0.5)],
    )
    result = resolve_selection(50, 5, 5, 5, forward)
    assert result["ok"] and result["worker"]["actual_model"] == "z-cost-only"
    assert result["worker"]["utility"] is None
    assert "configured-order fallback" in result["worker"]["rank_basis"] and "missing score" in result["worker"]["rank_basis"]

    reversed_rows = pools(
        l2=[offer("chatgpt:a-score-only", score=100), offer("chatgpt:z-cost-only", cost=1)],
        l3=[offer("chatgpt:light", 30, 1), offer("chatgpt:runner", 20, 0.5)],
    )
    result = resolve_selection(50, 5, 5, 5, reversed_rows)
    assert result["ok"] and result["worker"]["actual_model"] == "a-score-only"
    assert "missing cost" in result["worker"]["rank_basis"]


def test_complete_profile_ties_use_casefolded_stable_model_id_not_pool_order():
    tied = [offer("chatgpt:Zulu", 60, 10), offer("chatgpt:alpha", 60, 10)]

    def rows(models):
        return pools(l2=models, l3=[offer("chatgpt:light", 30, 1), offer("chatgpt:runner", 20, 0.5)])

    forward = resolve_selection(50, 5, 5, 5, rows(tied))
    reversed_order = resolve_selection(50, 5, 5, 5, rows(list(reversed(tied))))
    assert forward["ok"] and reversed_order["ok"]
    assert forward["worker"]["actual_model"] == reversed_order["worker"]["actual_model"] == "alpha"
    assert "casefolded stable model id" in forward["worker"]["rank_basis"]


def test_ties_break_by_cost_then_score():
    cost_tie = pools(
        l2=[offer("chatgpt:expensive", 62, 12), offer("chatgpt:cheap", 60, 10), offer("chatgpt:reviewer", 40, 1)],
        l3=[offer("chatgpt:light", 30, 1), offer("chatgpt:runner", 20, 0.5)],
    )
    result = resolve_selection(50, 5, 5, 5, cost_tie)
    assert result["worker"]["actual_model"] == "cheap", "equal utility at N=50, lower cost wins"

    score_tie = pools(
        l2=[offer("chatgpt:low-score", 40, 1), offer("chatgpt:high-score", 60, 1), offer("chatgpt:reviewer", 30, 2)],
        l3=[offer("chatgpt:light", 30, 1), offer("chatgpt:runner", 20, 0.5)],
    )
    result = resolve_selection(0, 5, 5, 5, score_tie)
    assert result["worker"]["actual_model"] == "high-score", "at N=0 utility ties on equal cost, higher score wins"


def test_qs_threshold_and_risk_or_mandatory_review_flags():
    assert assess_difficulty(50, 6, 7, 9)["QS_threshold"] == "0.7"
    result = resolve_selection(50, 6, 7, 9, standard_pools())
    assert result["ok"] and result["review"]["required"], "D exactly at the QS threshold enters review"
    result = resolve_selection(50, 0, 0, 0, standard_pools(), risk=True)
    assert result["review"]["required"]
    result = resolve_selection(50, 0, 0, 0, standard_pools(), review_required=True)
    assert result["review"]["required"]
    result = resolve_selection(50, 0, 0, 0, standard_pools())
    assert not result["review"]["required"]


def test_metrics_and_flags_are_strictly_validated():
    good = standard_pools()
    for bad in (float("nan"), float("inf"), -1, True, Decimal("NaN"), "1e1000000"):
        bad_pool = standard_pools()
        bad_pool["L2"][0]["score"] = bad
        with pytest.raises(ValueError):
            resolve_selection(50, 5, 5, 5, bad_pool)
    for flag in (0, 1, "true", None):
        with pytest.raises(ValueError):
            resolve_selection(50, 5, 5, 5, good, risk=flag)
