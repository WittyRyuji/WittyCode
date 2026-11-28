"""Frozen work-item allocations: N comes from the session preference, seats from the dispatch curve.

The route freezes items once; write delegations then must run on the frozen seat's model, inside
the frozen write scope, and every entry (structured, legacy, batch) refuses an item it never saw.
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
from contextlib import asynccontextmanager
from dataclasses import replace

import pytest

from desktop.core.fusion import allocation, identity
from desktop.core.fusion.protocol import ROUTE_DECLARATION, parse_route
from desktop.core.fusion.registry import FusionError, FusionRegistry, Policy
from desktop.core.harness.tools import validate_arguments
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


def test_route_guidance_uses_tool_fields_and_a_valid_item_example():
    parameters = ROUTE_DECLARATION["parameters"]
    items = parameters["properties"]["items"]
    description, example = items["description"].split("Example: ")
    identities = identity.load(None)
    roles = items["items"]["properties"]["role"]["enum"]
    assert roles == ["worker", "light"]
    for guidance in (identity.ALLOCATION, description):
        assert "score" in guidance and "n_i" not in guidance
        for role, names in ((roles[0], ("Coder", "QualitySampler")), (roles[1], ("Runner",))):
            assert all(identities.get(name).role == role for name in names)
            assert f"{role} ({', '.join(names)})" in guidance
    args = _route(items=[json.loads(example)])
    assert validate_arguments(parameters, args) == args
    route, problem = parse_route(args, 50)
    assert not problem and route.items[0]["score"] == "0.5"


@pytest.mark.parametrize("alias", ["n", "n_i", "difficulty"])
def test_route_rejects_score_alias_with_actionable_hint(alias):
    item = _item()
    item[alias] = item.pop("score")
    route, problem = parse_route(_route(items=[item]), 50)
    assert route is None and f"Unknown work item fields: {alias}." in problem
    assert "Use score" in problem


def test_route_reports_invalid_fields_and_items_together():
    first = _item(n_i=0.5, role="Coder")
    second = _item(id="second", workstream="Shipping", paths="src", reason="")
    route, problem = parse_route(_route(verification="full", items=[first, second]), 50)
    assert route is None
    route_problem, first_problem, second_problem = problem.splitlines()
    assert "verification depth" in route_problem
    assert first_problem.startswith("Work item 1:") and "Unknown work item fields: n_i." in first_problem
    assert "Use score" in first_problem and "worker (Coder, QualitySampler) or light (Runner)" in first_problem
    assert second_problem.startswith("Work item 2:") and "reason" in second_problem and "paths" in second_problem


def test_route_refuses_explicit_preference_even_when_it_matches():
    route, problem = parse_route(_route(items=[_item(N=50)]), 50)
    assert route is None and "Omit N" in problem and "session preference" in problem


@pytest.mark.parametrize("verification", ["self-check", "runner-and-sampler"])
def test_route_accepts_hyphenated_verification(verification):
    route, problem = parse_route(_route(verification=verification), 50)
    assert not problem and route.verification == verification.replace("-", "_")


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
    with pytest.raises(FusionError, match="another workstream") as refusal:
        registry.freeze([_item(workstream="Second")], 50)
    assert "id 'w1'" in str(refusal.value)
    assert "workstream 'Inventory', role 'worker'" in str(refusal.value)
    assert "Reuse that frozen item unchanged" in str(refusal.value)
    assert list(registry.allocations.values()) == [record]


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


@pytest.fixture
def linked_module_registry(tmp_path):
    registry = _registry(tmp_path)
    actual, entry = registry.project / "actual", registry.project / "entry"
    actual.mkdir()
    try:
        if os.name == "nt":
            junction = subprocess.run(["cmd", "/d", "/c", "mklink", "/J", "entry", "actual"],
                                      cwd=registry.project, capture_output=True)
            if junction.returncode:
                pytest.skip(f"Directory junction creation failed (exit {junction.returncode}).")
        else:
            entry.symlink_to(actual, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"Directory link creation is unavailable (errno {error.errno}).")
    try:
        assert entry.resolve() == actual.resolve()
        yield registry
    finally:
        entry.rmdir() if os.name == "nt" else entry.unlink()


@pytest.mark.parametrize("saved_binding", [True, False], ids=["exact-binding", "older-alias"])
def test_linked_module_owner_blocks_reestimate_and_checkpoint_reopens(linked_module_registry, saved_binding):
    registry = linked_module_registry
    item = _item(workstream="entry", paths=["entry"])
    registry.freeze([item], 50)
    registry.freeze([{**item, "score": 0.6, "new_facts": "Found one more check."}], 50)
    expert = registry.assign("entry", WORKER, "worker", seat="L2")
    registry.begin(expert.id)
    registry.finish(expert.id, status="done", tokens=0)
    assert (expert.scope, expert.aliases, expert.allocation_workstream) == ("actual", ("entry",), "entry")
    if not saved_binding:
        raw = registry.to_json()
        del raw["agents"][0]["allocationWorkstream"]
        registry = FusionRegistry.from_json(registry.project, raw)
    checkpoint = registry.to_json()

    with pytest.raises(FusionError, match="already has an owner"):
        registry.freeze([{**item, "score": 0.9, "new_facts": "Found a harder check."}], 50)
    assert registry.to_json() == checkpoint
    reopened = FusionRegistry.from_json(registry.project, json.loads(json.dumps(checkpoint)))
    assert reopened.to_json() == checkpoint
    assert reopened.allocations[("entry", "worker")]["seat"] == reopened.agents[expert.id].seat == "L2"


def test_restore_accepts_linked_binding_already_reestimated_by_an_older_version(linked_module_registry):
    registry = linked_module_registry
    item = _item(workstream="entry", paths=["entry"])
    registry.freeze([item], 50)
    expert = registry.assign("entry", WORKER, "worker", seat="L2")
    registry.begin(expert.id)
    registry.finish(expert.id, status="done", tokens=0)
    checkpoint = registry.to_json()
    # Emulate a checkpoint written before the re-estimate guard recognized the junction's owner.
    checkpoint["allocations"][0] = allocation.seal(
        {**item, "paths": ["actual"], "score": 0.9, "new_facts": "Found a harder check."}, 50)

    reopened = FusionRegistry.from_json(registry.project, json.loads(json.dumps(checkpoint)))
    assert reopened.to_json() == checkpoint
    assert reopened.agents[expert.id].seat == "L2" and reopened.allocation_for("entry", "worker")["seat"] == "L1"
    with pytest.raises(FusionError, match="seat is frozen"):
        reopened.assign("entry", MAIN, "worker", seat="L1")


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


def test_submit_route_repairs_both_fields_from_one_refusal(tmp_path):
    async def go():
        async with _seats(tmp_path) as (main, fake):
            bad = _item(role="Coder")
            bad["n"] = bad.pop("score")
            await _declared_turn(main, fake,
                                 call("submit_route", _route(items=[bad]), call_id="bad"),
                                 call("submit_route", _route(verification="runner-and-sampler", items=[_item()]), call_id="fixed"),
                                 call("submit_route", _route(items=[_item(workstream="Second")]), call_id="duplicate"),
                                 say("Routed."))
            refusal = _output(main, "bad")
            assert "Use score" in refusal and "worker (Coder, QualitySampler) or light (Runner)" in refusal
            assert "Route accepted: coder (runner_and_sampler)" in _output(main, "fixed")
            assert "workstream 'Inventory', role 'worker'" in _output(main, "duplicate")
            assert list(main._fusion.registry.allocations) == [("inventory", "worker")]
    asyncio.run(go())


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


def test_preset_change_resumes_a_frozen_seat_without_preallocating_an_expert(tmp_path):
    route = _route(items=[_item(score=0.9)])  # L1 uses the main model, even for a worker-role brief.

    async def go():
        async with _seats(tmp_path) as (main, fake):
            await _declared_turn(main, fake, call("submit_route", route, call_id="route"),
                                 call("delegate_task", _brief(), call_id="before"), say("Original evidence."), say("Done."))
            runtime = main._fusion
            registry = runtime.registry
            original, = registry.agents.values()
            original.status = "interrupted"
            runtime._checkpoint()
            assert (original.model, original.seat) == (MAIN, "L1")
            frozen = dict(registry.allocation_for("Inventory", "worker"))

            settings = main.manager.settings
            settings.fusion_main, settings.fusion_workers, settings.fusion_light = LIGHT, [WORKER], LIGHT
            settings.fusion_preference = 70
            prepared = runtime.prepare_settings(runtime.pending_settings())
            settings.fusion_preference = 80  # A later change must remain pending after applying this preparation.
            retired, = prepared.agents.values()
            assert (retired.id, retired.status, retired.replaced_status, retired.seat) == (
                original.id, "retired", "interrupted", "L1")
            assert original.status == "interrupted" and registry.policy.main == MAIN
            assert prepared.allocations == registry.allocations and prepared.allocations is not registry.allocations

            runtime.apply_settings(prepared, "low")
            assert main.saved.origin["fusionSettings"]["preference"] == 70
            assert runtime.pending_settings().preference == 80
            restored = FusionRegistry.from_json(registry.project, main.saved.fusion)
            assert restored.preset_snapshot is None
            checkpoint = restored.agents[retired.id]
            assert (checkpoint.status, checkpoint.replaced_status, checkpoint.seat) == ("retired", "interrupted", "L1")
            assert restored.allocation_for("Inventory", "worker") == frozen
            with pytest.raises(FusionError, match="new plan"):
                restored.freeze([_item(score=0.2, new_facts="A later audit.")], 70)

            await _declared_turn(main, fake, call("submit_route", route, call_id="same-route"),
                                 call("delegate_task", _brief(), call_id="without-resume"), say("Held."))
            assert "explicitly resume" in _output(main, "without-resume")
            assert list(main.agents) == [original.id] and runtime.registry.calls == 1
            await _declared_turn(main, fake, call("submit_route", route, call_id="resume-route"),
                                 call("delegate_task", _brief(resume=True), call_id="resume"),
                                 say("Continued."), say("Verified."))
            successor, = (record for record in runtime.registry.agents.values() if record.id != original.id)
            assert (successor.model, successor.seat, successor.transferred_from) == (LIGHT, "L1", original.id)
            assert successor.status == "done" and successor.handoff_complete and runtime.registry.calls == 2
            assert runtime.registry.allocation_for("Inventory", "worker") == frozen

    asyncio.run(go())


@pytest.mark.parametrize("retire", [False, True], ids=["live-owner", "retired-owner"])
def test_read_only_l1_resume_keeps_its_seat_across_turns(tmp_path, monkeypatch, retire):
    async def go():
        async with _seats(tmp_path) as (main, fake):
            runtime = main._fusion
            sampler = _brief("QualitySampler", workstream="Review")
            with monkeypatch.context() as patch:
                # First turn already earned the L1 review seat; later turns must not have to earn it again.
                patch.setattr(runtime, "_check_seat", lambda *_: "L1")
                await _declared_turn(main, fake, call("delegate_task", sampler, call_id="review"),
                                     say("Original review evidence."), say("Reviewed."))
            old, = runtime.registry.agents.values()
            assert (old.seat, old.model, old.status) == ("L1", MAIN, "done")
            old.status = "interrupted"
            runtime._checkpoint()
            target_model = MAIN
            if retire:
                target_model = LIGHT
                runtime.apply_settings(runtime.prepare_settings(replace(runtime.registry.policy, main=LIGHT)), "low")
                archived = runtime.registry.agents[old.id]
                assert (archived.status, archived.replaced_status) == ("retired", "interrupted")
            assert runtime._check_seat("QualitySampler", None) == "L2"
            before = len(fake.requests)
            await _declared_turn(main, fake, call("delegate_task", {**sampler, "resume": True}, call_id="review-next"),
                                 say("Continued reviewing."), say("Done."))
            active, = (expert for expert in runtime.registry.agents.values() if expert.status != "retired")
            assert (active.seat, active.model, active.status) == ("L1", target_model, "done")
            if retire:
                assert active.id != old.id and active.transferred_from == old.id and active.handoff_complete
            else:
                assert active.id == old.id and "Original review evidence." in str(fake.requests[-2]["input"])
            assert len(fake.requests) == before + 4 and runtime.registry.calls == 2

    asyncio.run(go())


@pytest.mark.parametrize("subscore", [0.5, 0.9], ids=["different-seat", "same-seat"])
def test_retired_legacy_scope_cannot_handoff_to_an_overlapping_frozen_item(tmp_path, subscore):
    async def go():
        async with _seats(tmp_path, active=("route", "v3")) as (main, fake):
            runtime = main._fusion
            route = _route(items=[_item(id="module", workstream="src", score=0.9, paths=["src"])])
            task = {"scope": "src", "model": WORKER, "role": "worker", "task": "Check the module."}
            await _declared_turn(main, fake, call("submit_route", route, call_id="main-route"),
                                 call("delegate_task", task, call_id="module-first"), say("Module evidence."), say("Done."))
            original, = runtime.registry.agents.values()
            assert (original.scope, original.seat) == ("src", "L1") and runtime.registry.calls == 1
            original.status = "interrupted"
            runtime._checkpoint()
            runtime.apply_settings(runtime.prepare_settings(replace(runtime.registry.policy, main=LIGHT)), "low")
            subroute = _route(items=[_item(id="submodule", workstream="src/sub", score=subscore, paths=["src/sub"])])
            subtask = {**task, "scope": "src/sub", "resume": True}
            await _declared_turn(main, fake, call("submit_route", subroute, call_id="sub-route"),
                                 call("delegate_task", subtask, call_id="cross-item"), say("Held."))
            assert runtime.registry.allocation_for("src/sub", "worker")["seat"] == ("L2" if subscore == 0.5 else "L1")
            assert "frozen" in _output(main, "cross-item")
            assert list(main.agents) == [original.id] and runtime.registry.calls == 1

            await _declared_turn(main, fake, call("submit_route", route, call_id="resume-route"),
                                 call("delegate_task", {**task, "resume": True}, call_id="module-resume"),
                                 say("Continued original module."), say("Verified."))
            successor, = (expert for expert in runtime.registry.agents.values() if expert.id != original.id)
            assert (successor.scope, successor.seat, successor.model) == ("src", "L1", LIGHT)
            assert successor.transferred_from == original.id and successor.handoff_complete
            assert successor.status == "done" and runtime.registry.calls == 2

    asyncio.run(go())


def test_retired_unseated_legacy_scope_can_resume_on_its_first_frozen_seat(tmp_path):
    async def go():
        async with _seats(tmp_path, active=()) as (main, fake):
            runtime = main._fusion
            task = {"scope": "src", "model": WORKER, "role": "worker", "task": "Check the module."}
            await _turn(main, fake, call("delegate_task", task, call_id="before-v3"),
                        say("Original evidence."), say("Done."))
            original, = runtime.registry.agents.values()
            assert not original.seat and not runtime.registry.allocations
            original.status = "interrupted"
            runtime._checkpoint()
            runtime.apply_settings(runtime.prepare_settings(replace(runtime.registry.policy, workers=(LIGHT,))), "low")
            assert list(main.agents) == [original.id]
            retired = runtime.registry.agents[original.id]
            assert (retired.status, retired.replaced_status, retired.seat) == ("retired", "interrupted", "")

            main.manager.settings.fusion_gates = _gates("route", "v3")
            route = _route(items=[_item(workstream="src", paths=["src"])])
            await _declared_turn(main, fake, call("submit_route", route, call_id="first-freeze"),
                                 call("delegate_task", {**task, "task": "Continue after inspection."}, call_id="implicit"),
                                 say("Held."))
            assert "explicitly resume" in _output(main, "implicit")
            assert list(main.agents) == [original.id] and runtime.registry.calls == 1
            await _declared_turn(main, fake, call("submit_route", route, call_id="resume-route"),
                                 call("delegate_task", {**task, "task": "Continue after inspection.", "resume": True},
                                      call_id="first-seat"), say("Continued."), say("Verified."))
            successor, = (record for record in runtime.registry.agents.values() if record.id != original.id)
            assert (successor.scope, successor.seat, successor.model) == ("src", "L2", LIGHT)
            assert successor.transferred_from == original.id and successor.handoff_complete
            assert successor.status == "done" and runtime.registry.calls == 2

    asyncio.run(go())


@pytest.mark.parametrize("stored_alias, original_scope, alternate_scope", [
    (False, "src/sub", r"src\sub"),
    (True, "src/sub", r"src\sub"),
    (True, r"src\sub", "src/sub"),
], ids=["new-freeze", "older-checkpoint", "older-checkpoint-backslash-first"])
def test_slash_alias_does_not_block_resuming_the_original_frozen_scope(tmp_path, stored_alias, original_scope, alternate_scope):
    registry = _registry(tmp_path)
    item = _item(id="original", workstream=original_scope, score=0.85, paths=[original_scope])
    alias = {**item, "workstream": alternate_scope, "paths": [alternate_scope]}
    competing = {**alias, "id": "competing", "score": 0.5}
    registry.freeze([item], 50)
    original = registry.assign(original_scope, MAIN, "worker", seat="L1")
    registry.begin(original.id)
    registry.finish(original.id, status="interrupted", tokens=0)
    original.status, original.replaced_status = "retired", "interrupted"
    registry.policy = replace(registry.policy, main=LIGHT, preference=100)
    frozen = dict(registry.allocation_for(original_scope, "worker"))

    if stored_alias:
        # Old checkpoints could contain both raw keys; the competing item's dependency must not block the original.
        raw = registry.to_json()
        raw["allocations"].extend([allocation.seal(_item(id="waiting", workstream="Unfinished"), 50),
                                   allocation.seal({**competing, "deps": ["waiting"]}, 50)])
        registry = FusionRegistry.from_json(registry.project, raw)
        assert registry.allocation_for(alternate_scope, "worker")["id"] == "competing"
        with pytest.raises(FusionError, match="frozen"):
            registry.assign(alternate_scope, WORKER, "worker", seat="L2", resume=True)
    else:
        with pytest.raises(FusionError, match="already frozen"):
            registry.freeze([competing], 50)
        # An otherwise identical spelling is still a no-op even when the current N would pick a different seat.
        assert registry.freeze([alias], 0) == 1
        assert list(registry.allocations.values()) == [frozen]
        assert registry.allocation_for(alias["workstream"], "worker") == frozen

    scope = original_scope if stored_alias else alternate_scope
    successor = registry.assign(scope, LIGHT, "worker", seat="L1", resume=True)
    assert (successor.scope, successor.seat, successor.transferred_from) == ("src/sub", "L1", original.id)
    registry.begin(successor.id, resume=True)
    assert registry.calls == 2 and registry.allocation_for(original_scope, "worker") == frozen


def _legacy_same_seat_checkpoint(registry, *, deps=(), second_scope="src/sub"):
    raw = registry.to_json()
    first_scope = r"src\sub" if second_scope == "src/sub" else "src/sub"
    raw["allocations"] = [
        allocation.seal(_item(id="first", workstream=first_scope, score=0.85, paths=["src/sub"]), 50),
        allocation.seal(_item(id="second", workstream=second_scope, score=0.9, paths=["src/sub"], deps=deps), 50),
        allocation.seal(_item(id="before", workstream="before", paths=["before"]), 50),
    ]
    return FusionRegistry.from_json(registry.project, raw)


def test_retired_legacy_scope_resumes_the_exact_same_seat_key(tmp_path):
    registry = _legacy_same_seat_checkpoint(_registry(tmp_path))
    original = registry.assign("src/sub", MAIN, "worker", seat="L1")
    registry.begin(original.id)
    registry.finish(original.id, status="interrupted", tokens=0)
    original.status, original.replaced_status = "retired", "interrupted"
    registry.policy = replace(registry.policy, main=LIGHT)
    raw = registry.to_json()
    raw["agents"][0].pop("allocationWorkstream", None)  # The old checkpoint did not retain the raw binding.
    registry = FusionRegistry.from_json(registry.project, raw)

    assert registry.allocation_for("src/sub", "worker")["id"] == "second"
    successor = registry.assign("src/sub", LIGHT, "worker", resume=True, seat="L1")
    assert successor.transferred_from == original.id and successor.status == "interrupted"
    registry.begin(successor.id, resume=True)
    registry.finish(successor.id, status="done", tokens=0)
    registry = FusionRegistry.from_json(registry.project, registry.to_json())
    with pytest.raises(FusionError, match="frozen"):
        registry.assign(r"src\sub", LIGHT, "worker", resume=True, seat="L1")
    assert registry.assign("src/sub", LIGHT, "worker", resume=True, seat="L1").id == successor.id


@pytest.mark.parametrize("scope", ["src/sub", r"src\sub"], ids=["slash", "backslash"])
def test_legacy_scope_checks_dependencies_of_the_exact_same_seat_key(tmp_path, scope):
    registry = _legacy_same_seat_checkpoint(_registry(tmp_path), deps=["before"], second_scope=scope)
    record = registry.allocation_for(scope, "worker")
    assert record["id"] == "second" and record["seat"] == "L1"
    expert = registry.assign(scope, registry.policy.seat_model(record["seat"]), "worker", seat=record["seat"])
    registry = FusionRegistry.from_json(registry.project, registry.to_json())
    with pytest.raises(FusionError, match="unfinished dependencies: before"):
        registry.begin(expert.id)
    assert registry.calls == 0

    before = registry.assign("before", WORKER, "worker", seat="L2")
    registry.begin(before.id)
    registry.finish(before.id, status="done", tokens=0)
    registry.begin(expert.id)
    assert registry.calls == 2


def test_frozen_slash_alias_keeps_the_same_structured_owner(tmp_path):
    async def go():
        async with _seats(tmp_path) as (main, fake):
            item = _item(workstream="src/sub", paths=["src/sub"])
            await _declared_turn(main, fake, call("submit_route", _route(items=[item]), call_id="route"),
                                 call("delegate_task", _brief(workstream="src/sub", paths=["src/sub"]), call_id="first"),
                                 say("Original evidence."), say("Done."))
            original, = main.agents
            alias = {**item, "workstream": r"src\sub", "paths": [r"src\sub"]}
            await _declared_turn(main, fake, call("submit_route", _route(items=[alias]), call_id="alias-route"),
                                 call("delegate_task", _brief(workstream=r"src\sub", paths=[r"src\sub"]), call_id="alias"),
                                 say("Continued."), say("Verified."))
            assert "src/sub [w1]: score 0.5 at N=50 seats L2" in _output(main, "alias-route")
            assert list(main.agents) == [original] and main._fusion.registry.calls == 2

    asyncio.run(go())


def test_change_main_rpc_reopens_and_delegates_again_on_frozen_l1(tmp_path):
    from tests.app_support import ready_core
    from tests.fakes import until
    from tests.test_fusion_settings_refresh import open_fusion

    async def go():
        core, fake, _, _, _ = await ready_core(tmp_path)
        try:
            cid, main = await open_fusion(core, tmp_path)
            main.manager.settings.fusion_gates = _gates("route", "brief", "v3")
            route = _route(items=[_item(score=0.9)])
            await _declared_turn(main, fake, call("submit_route", route, call_id="route"),
                                 call("delegate_task", _brief(), call_id="first"), say("Original evidence."), say("Done."))
            runtime = main._fusion
            original, = runtime.registry.agents.values()
            assert (original.model, original.seat, original.status) == (MAIN, "L1", "done")
            frozen = dict(runtime.registry.allocation_for("Inventory", "worker"))
            baseline = dict(main.saved.origin["fusionSettings"])
            main.manager.settings.fusion_preference = 75  # A session-only main switch must not consume this pending change.
            native = main.session_id
            fake.script(say("The original main's handoff."))
            assert (await core.call("fusion.changeMain", {"id": cid, "model": LIGHT}))["ok"]
            await core.call("session.close", {"id": cid})
            await until(lambda: main.state.value == "closed")
            reopened = await core.call("session.openSaved", {"projectId": str(tmp_path / "proj"), "id": "cg:" + native})
            main = core._by_cid[reopened["id"]].controller
            await until(lambda: main.state.value == "ready")
            runtime = main._fusion
            assert runtime.registry.policy.main == LIGHT and list(main.agents) == [original.id]
            archived = runtime.registry.agents[original.id]
            assert (archived.status, archived.replaced_status, archived.seat) == ("retired", "done", "L1")
            assert main.saved.origin["fusionSettings"] == baseline
            assert (runtime.pending_settings().main, runtime.pending_settings().preference) == (LIGHT, 75)

            await _declared_turn(main, fake, call("submit_route", route, call_id="route-next"),
                                 call("delegate_task", _brief(), call_id="next"), say("Continued."), say("Done."))
            successor, = (expert for expert in runtime.registry.agents.values() if expert.id != original.id)
            assert (successor.model, successor.seat, successor.transferred_from) == (LIGHT, "L1", original.id)
            assert successor.status == "done" and successor.handoff_complete and runtime.registry.calls == 2
            assert runtime.registry.allocation_for("Inventory", "worker") == frozen
        finally:
            await core.shutdown()

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
