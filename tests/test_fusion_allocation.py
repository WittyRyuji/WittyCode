"""Frozen work-item allocations: N comes from the session preference, seats from the dispatch curve.

The route freezes items once; write delegations then must run on the frozen seat's model, inside
the frozen write scope, and every entry (structured, legacy, batch) refuses an item it never saw.
"""
from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager

import pytest

from desktop.core.fusion import allocation
from desktop.core.fusion.protocol import parse_route
from desktop.core.fusion.registry import FusionError, FusionRegistry, Policy
from desktop.core.settings import AppSettings
from tests.chatgpt_support import make_engine
from tests.fake_responses import FakeResponses, call, say
from tests.test_fusion_identity_runtime import (
    LEGACY, LIGHT, MAIN, WORKER, _brief, _fusion, _gates, _identity_folder, _output, _turn,
)

pytestmark = pytest.mark.fusion_gates


# --------------------------------------------------------------------------- pure helpers


def _item(**overrides):
    return {"id": "w1", "workstream": "Inventory", "role": "worker", "score": 0.5,
            "reason": "Sits between the gates.", "paths": ["inventory.txt"],
            "deps": [], "acceptance": "Counts match.", **overrides}


def _registry(tmp_path, preference=50):
    project = tmp_path / "project"
    project.mkdir(exist_ok=True)
    return FusionRegistry(project, Policy(MAIN, (WORKER,), LIGHT, 0, preference=preference))


def test_seal_derives_the_seat_from_the_session_preference():
    # N=50 is the declared midpoint: the low gate sits at 0.300, the high gate at 0.850.
    sealed = {score: allocation.seal(_item(score=score), 50)["seat"] for score in (0.29, "0.3", 0.5, 0.84, "0.85")}
    assert sealed[0.29] == "L3" and sealed["0.3"] == "L3", "at or under the low gate goes light"
    assert sealed[0.5] == "L2" and sealed[0.84] == "L2", "between the gates goes worker"
    assert sealed["0.85"] == "L1", "at or over the high gate goes main"
    assert all(allocation.seal(_item(score=score), 50)["N"] == 50 for score in sealed)


def test_seal_refuses_a_model_supplied_index_or_seat():
    with pytest.raises(ValueError, match="preference"):
        allocation.seal(_item(N=51), 50)
    with pytest.raises(ValueError, match="seat"):
        allocation.seal(_item(seat="L1"), 50)
    with pytest.raises(ValueError, match="Unknown work item fields"):
        allocation.seal(_item(model=WORKER), 50)
    with pytest.raises(ValueError, match="score"):
        allocation.seal(_item(score=1.5), 50)
    with pytest.raises(ValueError, match="integer"):
        allocation.seal(_item(), 50.0)
    record = allocation.seal(_item(), 50)
    assert allocation.seal(record, 50) == record, "a stored record re-seals unchanged"


def test_same_compares_supplied_fields_not_derived_ones():
    frozen = allocation.seal(_item(), 50)
    assert allocation.same(frozen, _item()) and allocation.same(frozen, frozen)
    assert not allocation.same(frozen, _item(score=0.9)), "a re-score is a different item"
    assert not allocation.same(frozen, _item(workstream="Renamed")) and not allocation.same(frozen, _item(reason="other"))


def test_deps_must_resolve_known_ids_without_cycles():
    first, second, third = _item(id="a"), _item(id="b", deps=["a"]), _item(id="c", deps=["a", "b"])
    assert allocation.check_deps([first, second, third]) == ""
    assert "unknown ids" in allocation.check_deps([second])
    assert "itself" in allocation.check_deps([_item(id="a", deps=["a"])])
    assert "cycle" in allocation.check_deps([_item(id="a", deps=["b"]), _item(id="b", deps=["a"])])


def test_covers_tightens_the_delegated_write_scope():
    record = _item(paths=["src"])
    assert allocation.covers(record, ("src/a.py", "src/lib/b.py"))
    assert not allocation.covers(record, ("src2/a.py",)) and not allocation.covers(record, ("other.txt",))
    assert not allocation.covers(record, ()), "declaring no paths reserves the project, not the frozen scope"
    empty = _item(paths=[])
    assert not allocation.covers(empty, ("a.txt",)) and not allocation.covers(empty, ()), "an empty frozen scope claims none"


# --------------------------------------------------------------------------- protocol and registry


def _route(**overrides):
    return {"destination": "coder", "reason": "One coherent change.", "verification": "runner", **overrides}


def test_parse_route_seals_items_under_the_runtime_N():
    route, problem = parse_route(_route(items=[_item()]), 50)
    assert problem == "" and route.items[0]["N"] == 50 and route.items[0]["seat"] == "L2"
    route, problem = parse_route(_route(items=[_item(N=51)]), 50)
    assert route is None and "preference" in problem, "the model cannot move the difficulty slider"
    route, problem = parse_route(_route(items=[_item()]))
    assert route is None and "N" in problem, "items need the runtime N even when none was passed"
    route, problem = parse_route(_route(items=[_item(new_facts="Evidence from the audit.")]), 50)
    assert problem == "" and route.items[0]["new_facts"] == "Evidence from the audit.", "declared facts ride along"
    route, problem = parse_route(_route(items=[_item(new_facts="")]), 50)
    assert route is None and "new_facts" in problem
    route, problem = parse_route(_route())
    assert route is not None and route.items == (), "item-less routes keep the old signature"


def test_freeze_keeps_a_workstream_binding_stable(tmp_path):
    registry = _registry(tmp_path)
    assert registry.freeze([_item()], 50) == 1
    record = registry.allocation_for("Inventory", "worker")
    assert record["seat"] == "L2" and record["paths"] == ("inventory.txt",)
    assert registry.freeze([_item()], 50) == 1, "an identical re-freeze is a no-op"
    with pytest.raises(FusionError, match="already frozen"):
        registry.freeze([_item(score=0.9)], 50)
    with pytest.raises(FusionError, match="already frozen"):
        registry.freeze([_item(id="w2")], 50)
    with pytest.raises(FusionError, match="another workstream"):
        registry.freeze([_item(workstream="Second")], 50)


def test_freeze_keeps_the_record_when_the_preference_moves(tmp_path):
    registry = _registry(tmp_path)
    registry.freeze([_item()], 50)
    original = dict(registry.allocation_for("Inventory", "worker"))
    assert registry.freeze([_item()], 80) == 1, "the identical item is a no-op under the new preference"
    assert registry.allocation_for("Inventory", "worker") == original, "the frozen record keeps its N and seat"
    assert allocation.same(original, allocation.seal(_item(), 80)), "N and seat never join the comparison"
    with pytest.raises(FusionError, match="already frozen"):
        registry.freeze([_item(score=0.9)], 80)
    with pytest.raises(FusionError, match="new plan"):
        registry.freeze([_item(id="w2", new_facts="The audit renamed the ledger.")], 80)


def test_seal_keeps_declared_new_facts():
    record = allocation.seal(_item(new_facts=" The audit found PII. "), 50)
    assert record["new_facts"] == "The audit found PII."
    assert "new_facts" not in allocation.seal(_item(), 50)
    with pytest.raises(ValueError, match="new_facts"):
        allocation.seal(_item(new_facts="  "), 50)


def test_new_facts_reestimate_the_score_before_dispatch(tmp_path):
    registry = _registry(tmp_path)
    registry.freeze([_item()], 50)
    assert registry.freeze([_item(score=0.9, reason="Raised by the audit.",
                                  new_facts="The audit found encrypted PII in the ledger.")], 50) == 1
    record = registry.allocation_for("Inventory", "worker")
    assert record["score"] == "0.9" and record["seat"] == "L1" and record["new_facts"] == "The audit found encrypted PII in the ledger."
    assert registry.freeze([_item(score=0.9, reason="Raised by the audit.")], 50) == 1, "the re-estimated item re-freezes identically"


def test_new_facts_cannot_rewrite_identity_scope_or_dependencies(tmp_path):
    registry = _registry(tmp_path)
    registry.freeze([_item()], 50)
    for change in ({"id": "w2"}, {"paths": ["other.txt"]}, {"deps": ["x"]}, {"acceptance": "Anything goes."}):
        with pytest.raises(FusionError, match="new plan"):
            registry.freeze([_item(score=0.9, new_facts="Evidence.", **change)], 50)
    assert registry.allocation_for("Inventory", "worker")["score"] == "0.5", "a refused re-estimate leaves the record alone"


def test_new_facts_cannot_reestimate_an_owned_binding(tmp_path):
    registry = _registry(tmp_path)
    registry.freeze([_item()], 50)
    registry.assign_workstream("Inventory", ("inventory.txt",), WORKER, "worker", seat="L2")
    with pytest.raises(FusionError, match="new plan"):
        registry.freeze([_item(score=0.9, new_facts="Evidence.")], 50)


def test_freeze_checks_dependencies_across_route_submissions(tmp_path):
    registry = _registry(tmp_path)
    registry.freeze([_item(id="a")], 50)
    assert registry.freeze([_item(id="b", workstream="Second", deps=["a"])], 50) == 1
    with pytest.raises(FusionError, match="unknown ids"):
        registry.freeze([_item(id="c", workstream="Third", deps=["missing"])], 50)
    with pytest.raises(FusionError, match="cycle"):
        registry.freeze([_item(id="d", workstream="Fourth", deps=["e"]), _item(id="e", workstream="Fifth", deps=["d"])], 50)


def test_the_registry_roundtrip_restores_and_rejects_tampering(tmp_path):
    registry = _registry(tmp_path)
    registry.freeze([_item(), _item(id="w2", workstream="Second", score=0.9, deps=["w1"])], 50)
    registry.assign_workstream("Inventory", ("inventory.txt",), WORKER, "worker", seat="L2")
    restored = FusionRegistry.from_json(registry.project, registry.to_json())
    assert restored.allocations == registry.allocations
    expert, = restored.agents.values()
    assert expert.seat == "L2" and expert.model == WORKER
    for field, value in (("seat", "L1"), ("score", 0.9), ("N", "50.0")):
        raw = registry.to_json()
        raw["allocations"][0][field] = value
        with pytest.raises(FusionError, match="invalid"):
            FusionRegistry.from_json(registry.project, raw)
    raw = registry.to_json()
    raw["agents"][0]["seat"] = "L3"
    with pytest.raises(FusionError, match="invalid"):
        FusionRegistry.from_json(registry.project, raw)


def test_check_deps_walks_deep_chains_without_recursing():
    depth = 3000
    records = [allocation.seal(_item(id=f"w{i}", workstream=f"ws{i}", deps=[f"w{i - 1}"] if i else []), 50)
               for i in range(depth)]
    assert allocation.check_deps(records) == ""
    records[0]["deps"] = (f"w{depth - 1}",)
    assert "cycle" in allocation.check_deps(records)


def test_freeze_refuses_to_grow_past_the_allocation_cap(tmp_path):
    registry = _registry(tmp_path)
    with pytest.raises(FusionError, match="1024"):
        registry.freeze([_item(id=f"w{i}", workstream=f"ws{i}") for i in range(1025)], 50)


def test_begin_waits_for_the_item_dependencies(tmp_path):
    registry = _registry(tmp_path)
    registry.freeze([_item(id="a"), _item(id="b", workstream="Second", deps=["a"])], 50)
    first = registry.assign_workstream("Inventory", ("inventory.txt",), WORKER, "worker", seat="L2")
    second = registry.assign_workstream("Second", ("second.txt",), WORKER, "worker", seat="L2")
    with pytest.raises(FusionError, match="unfinished dependencies: a"):
        registry.begin(second.id)
    registry.begin(first.id)
    with pytest.raises(FusionError, match="unfinished dependencies"):  # a running dependency is not done
        registry.begin(second.id)
    registry.finish(first.id, status="done", tokens=0)
    registry.begin(second.id)
    assert second.status == "running"


# --------------------------------------------------------------------------- runtime


async def _declared_turn(main, fake, *responses):
    """V3's intent gate refuses undeclared routes and delegations, so the fake script declares a simple execution first."""
    return await _turn(main, fake, call("declare_topic", {"kind": "simple", "intent": "execute",
                                                          "topic": "Run the scripted allocation."}, call_id="declare"),
                       *responses)


@asynccontextmanager
async def _seats(tmp_path, *, active=("route", "brief", "v3"), preference=50, reasoning=None):
    """The identity fixture with a controllable preference and per-seat reasoning."""
    project = tmp_path / "project"
    project.mkdir()
    _identity_folder()
    with FakeResponses() as fake:
        engine = make_engine(tmp_path, fake, settings=AppSettings(fusion_gates=_gates(*active)))
        main = await engine.open_new(project, model="gpt-6.1-sol", autonomy="full")
        main.enable_fusion(Policy(MAIN, (WORKER,), LIGHT, 0, max_seconds=30, preference=preference,
                                  reasoning=reasoning or {}))
        try:
            yield main, fake
        finally:
            await engine.close_all()


def test_writes_without_a_frozen_item_are_refused_before_any_side_effect(tmp_path):
    async def go():
        async with _fusion(tmp_path, active=("brief", "v3")) as (main, fake):
            await _declared_turn(main, fake, call("delegate_task", _brief(), call_id="coder"), say("Acknowledged."))
            assert "frozen work item" in _output(main, "coder") and not main.agents
    asyncio.run(go())


def test_legacy_and_batch_writes_cannot_skip_the_allocation(tmp_path):
    async def go():
        async with _fusion(tmp_path, active=("v3",)) as (main, fake):
            batch = {"tasks": [LEGACY, {**LEGACY, "workstream": "Second"}]}
            await _declared_turn(main, fake, call("delegate_task", LEGACY, call_id="legacy"),
                                 call("delegate_tasks", batch, call_id="batch"), say("Acknowledged."))
            assert "frozen work item" in _output(main, "legacy")
            assert _output(main, "batch").count("frozen work item") == 2, "each batch task is checked on its own"
            assert not main.agents
    asyncio.run(go())


def test_frozen_items_seat_all_three_tiers_with_their_reasoning(tmp_path):
    reasoning = {f"main:{MAIN}": "low", f"worker:{WORKER}": "high", f"light:{LIGHT}": "low"}
    route = _route(destination="split", reason="Three independent workstreams.",
                   items=[_item(id="a", workstream="Alpha", score=0.3, paths=["a.txt"]),
                          _item(id="b", workstream="Beta", score=0.5, paths=["b.txt"]),
                          _item(id="c", workstream="Gamma", score=0.85, paths=["c.txt"])])

    def brief(workstream, path):
        return _brief(workstream=workstream, paths=[path])

    async def go():
        async with _seats(tmp_path, reasoning=reasoning) as (main, fake):
            await _declared_turn(main, fake, call("submit_route", route, call_id="route"),
                                 call("delegate_task", brief("Alpha", "a.txt"), call_id="d-a"), say("Alpha done."),
                                 call("delegate_task", brief("Beta", "b.txt"), call_id="d-b"), say("Beta done."),
                                 call("delegate_task", brief("Gamma", "c.txt"), call_id="d-c"), say("Gamma done."),
                                 say("All seated."))
            agents = {agent.record.workstream: agent for agent in main.agents.values()}
            assert [(agents[name].record.seat, agents[name].record.model, agents[name].agent_effort)
                    for name in ("Alpha", "Beta", "Gamma")] == [
                ("L3", LIGHT, "low"), ("L2", WORKER, "high"), ("L1", MAIN, "low")]
            receipt = _output(main, "route")
            assert receipt.startswith("Route accepted: split (runner).")
            for name, item_id, score, seat, model, effort in (
                    ("Alpha", "a", "0.3", "L3", "gpt-6-luna", "low"),
                    ("Beta", "b", "0.5", "L2", "gpt-6.1-sol", "high"),
                    ("Gamma", "c", "0.85", "L1", "gpt-6.1-sol", "low")):
                assert (f"{name} [{item_id}]: score {score} at N=50 seats {seat} on {model}, {effort} reasoning; "
                        f"scored as \"Sits between the gates.\"") in receipt
            # A second turn re-freezes the identical items and re-dispatches onto the same owners.
            await _declared_turn(main, fake, call("submit_route", route, call_id="route-2"),
                                 call("delegate_task", brief("Alpha", "a.txt"), call_id="d-a2"), say("Alpha again."),
                                 say("Reused."))
            assert {agent.record.workstream: agent for agent in main.agents.values()}["Alpha"] is agents["Alpha"]
            assert "Alpha [a]: score 0.3 at N=50 seats L3" in _output(main, "route-2"), \
                "a re-frozen item echoes its stored record, not a new score"
            await _declared_turn(main, fake, call("submit_route", _route(), call_id="route-3"), say("Plain."))
            assert _output(main, "route-3") == "Route accepted: coder (runner).", "no items means no scoring lines"
    asyncio.run(go())


def test_delegated_writes_outside_the_frozen_scope_are_refused(tmp_path):
    route = _route(items=[_item(paths=["src"])])
    async def go():
        async with _seats(tmp_path) as (main, fake):
            await _declared_turn(main, fake, call("submit_route", route, call_id="route"),
                                 call("delegate_task", _brief(paths=["src/x.txt", "other.txt"]), call_id="wide"),
                                 say("Kept inside."))
            assert "exceed the frozen work item's scope" in _output(main, "wide") and not main.agents
    asyncio.run(go())


def test_a_delegation_declaring_no_paths_never_expands_the_frozen_scope(tmp_path):
    route = _route(items=[_item()])
    async def go():
        async with _fusion(tmp_path, active=("route", "v3")) as (main, fake):
            await _declared_turn(main, fake, call("submit_route", route, call_id="route"),
                                 call("delegate_task", {**LEGACY, "paths": []}, call_id="empty"),
                                 say("Kept inside."))
            assert "exceed the frozen work item's scope" in _output(main, "empty") and not main.agents
    asyncio.run(go())
