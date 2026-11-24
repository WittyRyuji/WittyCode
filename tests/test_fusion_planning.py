"""The V3 planning facts contract: the JSON structure the planning snapshot advertises, its standalone
validator, and the contract text appended to the team snapshot. Pure local validation — nothing here touches
the network or the disk beyond the tmp_path profile records.
"""
from __future__ import annotations

import json
from fractions import Fraction

import pytest

from desktop.core.fusion import planning, profiles
from tests.app_support import MODEL, OTHER_MODEL


def item(**fields) -> dict:
    base = {"id": "a", "workstream": "The first deliverable.", "paths": ["src/a.py"], "n": 0.3,
            "evidence": "One file, a known pattern."}
    base.update(fields)
    return base


def plan(**fields) -> dict:
    base = {"delivery": "direct", "reason": "Fits the request.", "files": None,
            "facts": ["The module exists."], "uncertainties": [], "decisions": [],
            "acceptance": "The tests pass.", "budget": {"tokens": "adaptive", "turns": "adaptive",
                                                        "seconds": "adaptive"}}
    base.update(fields)
    return base


def split(**fields) -> dict:
    base = plan(delivery="candidates", files=2,
                items=[item(), item(id="b", workstream="The second deliverable.", paths=["src/b.py"], n=0.7,
                                    evidence="New code across the API boundary.")])
    base.update(fields)
    return base


# --------------------------------------------------------------------------- the valid shapes

def test_a_minimal_direct_plan_validates():
    checked = planning.check(plan())
    assert checked["delivery"] == "direct" and checked["files"] is None and checked["items"] == []
    assert checked["facts"] == ("The module exists.",) and checked["uncertainties"] == ()
    assert checked["budget"] == {"tokens": "adaptive", "turns": "adaptive", "seconds": "adaptive"}


def test_a_candidates_plan_normalizes_and_validates():
    checked, refusal = planning.parse(json.dumps(split(items=[
        item(paths=["src/a.py", "src\\a.py", " src/c.py "]),  # Backslashes and padding deduplicate away.
        item(id="b", workstream="Second.", paths=["src/b.py"], n=0.7, evidence="Two modules.", deps=["a"]),
    ], files=None)))
    assert refusal == "" and checked is not None
    assert checked["items"][0]["paths"] == ("src/a.py", "src/c.py")
    assert checked["items"][0]["n"] == "0.3" and checked["items"][1]["n"] == "0.7"
    assert checked["items"][1]["deps"] == ("a",)


def test_parse_decodes_json_and_reports_refusals():
    checked, refusal = planning.parse(json.dumps(split()))
    assert refusal == "" and checked["delivery"] == "candidates" and len(checked["items"]) == 2
    checked, refusal = planning.parse("{not json")
    assert checked is None and refusal
    checked, refusal = planning.parse(b"{}")
    assert checked is None and refusal == "A Fusion plan must be a JSON text."


# --------------------------------------------------------------------------- delivery and items

def test_a_direct_plan_carries_at_most_one_item():
    checked = planning.check(plan(delivery="direct", items=[item(n=0.5, deps=[])]))
    only = checked["items"][0]  # One item freezes the whole deliverable for a single Coder, fully validated.
    assert only["n"] == "0.5" and only["paths"] == ("src/a.py",) and only["deps"] == ()
    with pytest.raises(ValueError, match="at most one"):
        planning.check(plan(delivery="direct", items=[item(), item(id="b", evidence="Two.")]))


@pytest.mark.parametrize("items", [[], [item()]])
def test_a_split_needs_at_least_two_candidates(items):
    with pytest.raises(ValueError, match="at least two"):
        planning.check(plan(delivery="candidates", items=items, files=None))


@pytest.mark.parametrize("delivery", ["self", "split", "DIRECT", 0, None])
def test_the_delivery_is_direct_or_candidates(delivery):
    with pytest.raises(ValueError, match="'direct' or 'candidates'"):
        planning.check(plan(delivery=delivery))


def test_item_ids_are_unique_and_deps_resolve_without_cycles():
    with pytest.raises(ValueError, match="unique"):
        planning.check(split(items=[item(), item(evidence="Other.")]))
    with pytest.raises(ValueError, match="unknown ids"):
        planning.check(split(items=[item(deps=["ghost"]), item(id="b", evidence="Two.")]))
    with pytest.raises(ValueError, match="itself"):
        planning.check(split(items=[item(deps=["a"]), item(id="b", evidence="Two.")]))
    with pytest.raises(ValueError, match="cycle"):
        planning.check(split(items=[item(deps=["b"]), item(id="b", evidence="Two.", deps=["a"])]))
    checked = planning.check(split(items=[item(), item(id="b", evidence="Two.", deps=["a"]),
                                          item(id="c", workstream="Third.", paths=[], n=0.1,
                                               evidence="Trivial docs.", deps=["a", "b"])]))
    assert [entry["id"] for entry in checked["items"]] == ["a", "b", "c"]


# --------------------------------------------------------------------------- the estimates

def test_each_item_names_its_n_and_the_evidence_for_it():
    # n is the item's score in 0..1, exact like a frozen work item's — 30 is not a score and is refused.
    for broken in (item(n=-1), item(n=2), item(n=30), item(n=1.5), item(n="30"), item(n="abc"), item(n=True),
                   item() | {"n": None}, item(n=Fraction(1, 3)), item(evidence=""), item(evidence="   ")):
        with pytest.raises(ValueError):
            planning.check(split(items=[broken, item(id="b", evidence="Two.")]))
    checked = planning.check(split(items=[item(n=0), item(id="b", evidence="Two.", n=1),
                                          item(id="c", workstream="Third.", paths=[], n="0.5",
                                               evidence="Half.")]))
    assert [entry["n"] for entry in checked["items"]] == ["0", "1", "0.5"]  # Exact decimals, JSON data.
    assert planning.check(split(items=[item(n=0.3), item(id="b", evidence="Two.")]))["items"][0]["n"] == "0.3"


def test_files_is_an_estimate_floored_at_the_declared_paths():
    assert planning.check(split())["files"] == 2
    assert planning.check(split(files=5))["files"] == 5    # Directories cover more files; future ones are unknown.
    assert planning.check(split(files=None))["files"] is None
    for impossible in (0, 1):  # Below the two declared concrete paths: the one clearly impossible answer.
        with pytest.raises(ValueError, match="below the 2 declared"):
            planning.check(split(files=impossible))
    for invalid in (-1, "two", True):
        with pytest.raises(ValueError, match="deduplicated file count or null"):
            planning.check(split(files=invalid))


@pytest.mark.parametrize("side", [0, -5, True, 2.5, "many"])
def test_budget_dimensions_are_adaptive_or_positive_integers(side):
    with pytest.raises(ValueError, match="'adaptive' or a positive integer"):
        planning.check(plan(budget={"tokens": side}))
    checked = planning.check(plan(budget={"tokens": 50_000, "turns": 12}))  # A number is a request, still clamped.
    assert checked["budget"] == {"tokens": 50_000, "turns": 12, "seconds": "adaptive"}


def test_the_budget_is_required_and_keeps_only_the_three_fields():
    with pytest.raises(ValueError, match="tokens, turns and seconds"):
        planning.check(plan(budget=None))
    with pytest.raises(ValueError, match="Unknown plan budget fields"):
        planning.check(plan(budget={"tokens": "adaptive", "dollars": 3}))


# --------------------------------------------------------------------------- the rules the model cannot fill

@pytest.mark.parametrize("field", ["N", "preference"])
def test_the_session_preference_is_never_a_plan_field(field):
    checked, refusal = planning.parse(json.dumps(plan(**{field: 50})))
    assert checked is None and "user's own slider" in refusal


@pytest.mark.parametrize("fields", [{"goals": []}, {"budget": "adaptive"}, {"score": 0.5}])
def test_unknown_fields_and_malformed_values_are_refused(fields):
    with pytest.raises(ValueError):
        planning.check(plan(**fields))


def test_required_facts_uncertainties_decisions_and_acceptance():
    for field in ("facts", "uncertainties", "decisions"):
        with pytest.raises(ValueError, match=field):
            planning.check(plan(**{field: "none"}))
        missing = plan()
        del missing[field]
        with pytest.raises(ValueError, match=field):
            planning.check(missing)
    for field in ("reason", "acceptance"):
        with pytest.raises(ValueError, match=field):
            planning.check(plan(**{field: ""}))


# --------------------------------------------------------------------------- the snapshot itself

def test_the_snapshot_keeps_the_profile_facts_and_appends_the_contract(tmp_path):
    profiles.ensure(tmp_path, MODEL, "high", lambda *_: {"score": 51.5, "taskCostUSD": 1.75, "timeMinutes": 12.4},
                    clock=lambda: 1_760_000_000.0)
    snapshot = profiles.team_snapshot(tmp_path, {"main": (MODEL, "high"), "worker": (OTHER_MODEL, "low")})
    # The hit keeps the matched (model, effort) identifier, capability, task cost, speed and the fetch date.
    assert f"main: {MODEL} @ high — capability 51.5, task cost $1.75 per task, ~12.4 min per task" in snapshot
    assert "fetched 2025-10-" in snapshot
    assert f"worker: {OTHER_MODEL} @ low — no local profile" in snapshot  # A miss stays a miss, not a guess.
    # The planning contract rides along: structure, estimates and limits in the same read-only view.
    assert planning.CONTRACT in snapshot
    assert "'direct' or 'candidates'" not in snapshot  # The rules are prose: the enum shows in the JSON line.
    for marker in ('"direct"|"candidates"', "deduplicated", "n_i", "0..1", "adaptive", "preference N",
                   "tokens", "turns", "seconds", "stays prose"):
        assert marker in snapshot
