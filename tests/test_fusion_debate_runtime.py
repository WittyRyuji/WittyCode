"""The V3 intent gate: a turn may plan, route, delegate or change only within its declared intent.

A main Fusion session gets ``declare_topic`` plus the ``declare_state`` receipt. Undeclared, failed and
discussion turns may chat and read but may not submit plans, routes, delegations or changes; a ``plan``
declaration allows only ``submit_plan``, and the user's plan approval is the one execute receipt the
model cannot mint itself. Declarations dedup per turn, so a renamed topic cannot buy a second debate.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

import pytest

from desktop.core.fusion.registry import Policy
from desktop.core.settings import AppSettings
from desktop.core.viewmodel import turn_json
from tests.chatgpt_support import finish_turn, make_engine
from tests.fake_responses import FakeResponses, call, calls, say
from tests.fakes import until
from tests.test_fusion_identity_runtime import (
    LEGACY, LIGHT, MAIN, WORKER, _fusion, _gates, _identity_folder, _output, _turn,
)
from tests.test_harness_toolset import StubClient, call as tool_call, make, tool

pytestmark = pytest.mark.fusion_gates


@asynccontextmanager
async def _debating(tmp_path, *, preference=75, active=("v3",)):
    """The identity fixture with a slider that lets a complex declaration run a real debate."""
    project = tmp_path / "project"
    project.mkdir()
    _identity_folder()
    with FakeResponses() as fake:
        engine = make_engine(tmp_path, fake, settings=AppSettings(fusion_gates=_gates(*active)))
        main = await engine.open_new(project, model="gpt-6.1-sol", autonomy="full")
        main.enable_fusion(Policy(MAIN, (WORKER,), LIGHT, 0, max_seconds=30, preference=preference, reasoning={}))
        try:
            yield main, fake
        finally:
            await engine.close_all()


def _declare(kind="simple", intent="execute", topic="Do the chore.", call_id="declare", **extra):
    return call("declare_topic", {"kind": kind, "intent": intent, "topic": topic, **extra}, call_id=call_id)


def _opening(turn):
    return turn_json(turn).get("fusionFeedback", {}).get("opening", "")


def _plan(delivery="direct", items=None):
    plan = {"delivery": delivery, "reason": "The shape fits the ask.", "files": None, "facts": ["Checked locally."],
            "uncertainties": [], "decisions": ["As scoped."], "acceptance": "The checks pass.",
            "budget": {"tokens": "adaptive", "turns": "adaptive", "seconds": "adaptive"}}
    if items is not None:
        plan["items"] = items
    return plan


def _work_item(id_, workstream, n, paths):
    return {"id": id_, "workstream": workstream, "paths": paths, "n": n,
            "evidence": "The score's basis.", "deps": [], "acceptance": "It checks out."}


def test_an_undeclared_turn_cannot_delegate(tmp_path):
    async def go():
        async with _fusion(tmp_path, active=("v3",)) as (main, fake):
            await _turn(main, fake, call("delegate_task", LEGACY, call_id="work"), say("Stopped."))
            assert "declare_topic" in _output(main, "work") and not main.agents
    asyncio.run(go())


def test_a_simple_execute_declaration_unlocks_the_turn(tmp_path):
    async def go():
        async with _fusion(tmp_path, active=("v3",)) as (main, fake):
            await _turn(main, fake, _declare(), call("delegate_task", LEGACY, call_id="work"), say("Done."))
            declared = _output(main, "declare")
            assert "Declared simple" in declared and "team model profiles" not in declared
            assert "frozen work item" in _output(main, "work"), "the intent gate passed it to the next gate"
    asyncio.run(go())


def test_a_discussion_declaration_cannot_execute(tmp_path):
    async def go():
        async with _fusion(tmp_path, active=("v3",)) as (main, fake):  # preference 25: the debate is skipped
            await _turn(main, fake,
                        _declare("complex", "discussion", "Is a cache worth it?"),
                        call("delegate_task", LEGACY, call_id="work"), say("The honest answer."))
            assert "not debated" in _output(main, "declare")
            assert "declared a discussion" in _output(main, "work") and not main.agents
    asyncio.run(go())


def test_one_declaration_per_turn_and_a_rename_cannot_reopen_it(tmp_path):
    async def go():
        async with _fusion(tmp_path, active=("v3",)) as (main, fake):
            both = calls([("declare_topic", '{"kind": "complex", "intent": "discussion", "topic": "Should we shard?", '
                                            '"next_step": "I will compare the trade-offs."}', "first", "i1"),
                          ("declare_topic", '{"kind": "simple", "intent": "execute", "topic": "Should we shard, rephrased", '
                                            '"next_step": "I will ship this now."}', "second", "i2")])
            await _turn(main, fake, both, call("delegate_task", LEGACY, call_id="work"), say("Done."))
            assert _output(main, "second") == _output(main, "first"), "the stored receipt answers the repeat declare"
            assert "declared a discussion" in _output(main, "work")
            assert _opening(main.transcript.turns[-1]) == "I will compare the trade-offs."
            await _turn(main, fake, _declare(next_step="I will check capacity.", call_id="next"), say("Done."))
            assert _opening(main.transcript.turns[-1]) == "I will check capacity."
    asyncio.run(go())


def test_a_plan_declaration_allows_only_submit_plan_and_approval_executes(tmp_path):
    async def go():
        async with _fusion(tmp_path, active=("v3",)) as (main, fake):
            main.mode = "spec"
            await _turn(main, fake,
                        _declare("simple", "plan", "Plan the refactor."),
                        call("delegate_task", LEGACY, call_id="work"),
                        call("submit_plan", {"plan": "Step one: take stock."}, call_id="early"),
                        call("submit_work_plan", {"data": _plan()}, call_id="workplan"),
                        call("submit_plan", {"plan": "Step one: take stock."}, call_id="plan"))
            assert "team model profiles" in _output(main, "declare")
            assert "only gated calls" in _output(main, "work")
            assert "submit_work_plan before submitting" in _output(main, "early")
            assert "validated" in _output(main, "workplan")
            assert "plan has been submitted" in _output(main, "plan")
            fake.script(call("delegate_task", LEGACY, call_id="exec"), say("Executed."))
            assert main.approve_plan(main.saved.plan["id"])
            await finish_turn(main)
            # The approval seeded this turn's execute receipt: the delegation reaches the allocation
            # gate instead of the intent gate, and no debate ran for it.
            assert "frozen work item" in _output(main, "exec")
    asyncio.run(go())


def test_a_complex_execute_declaration_runs_the_debate_once(tmp_path):
    async def go():
        async with _debating(tmp_path) as (main, fake):  # preference 75: default 2 rounds, cap 3
            fake.script(_declare("complex", "execute", "Shard the store?", next_step="  I’ll compare\n the trade-offs.  "),
                        [("hold", "debate"), *say("Sharding is premature.")],
                        say('{"conclusion": "Do not shard now.", "open_issue": ""}'), say("Decided."))
            assert main.send_prompt("Consider sharding the store.")
            await until(lambda: len(fake.requests) == 2)
            turn = main.transcript.turns[-1]
            assert turn.running and _opening(turn) == "I’ll compare the trade-offs."
            assert not any(b["kind"] == "text" for b in turn_json(turn)["blocks"])
            fake.release("debate")
            await finish_turn(main)
            declared = _output(main, "declare")
            assert "concluded" in declared and "Do not shard now." in declared
            assert "team model profiles" in declared
            assert len(fake.requests) == 4, "the declare request, two seats and the final reply only"
    asyncio.run(go())


def test_a_plain_answer_needs_no_declaration(tmp_path):
    async def go():
        async with _fusion(tmp_path, active=("v3",)) as (main, fake):
            await _turn(main, fake, say("Just a chat answer."))
            assert len(fake.requests) == 1, "one model reply and no debate or expert call"
            assert not _opening(main.transcript.turns[-1])
            await _turn(main, fake, _declare("simple", "discussion"), say("Another answer."))
            assert not _opening(main.transcript.turns[-1])
    asyncio.run(go())


@pytest.mark.parametrize("opening", [None, {"text": "Wrong type"}, "x" * 241, " \n "])
def test_unusable_openings_do_not_change_the_declaration(tmp_path, opening):
    async def go():
        async with _fusion(tmp_path, active=("v3",)) as (main, fake):
            await _turn(main, fake, _declare(next_step=opening), say("Done."))
            assert "Declared simple" in _output(main, "declare")
            assert not _opening(main.transcript.turns[-1])
    asyncio.run(go())


def test_read_only_work_can_have_an_opening_without_debate(tmp_path):
    async def go():
        async with _fusion(tmp_path, active=("v3",)) as (main, fake):
            (main.project / "stock.txt").write_text("4 parts", encoding="utf-8")
            await _turn(main, fake, _declare("simple", "discussion", "How many parts?", next_step="I’ll read the stock file."),
                        call("read_file", {"file_path": "stock.txt"}, call_id="read"), say("4 parts."))
            assert "4 parts" in _output(main, "read") and len(fake.requests) == 3
            assert _opening(main.transcript.turns[-1]) == "I’ll read the stock file."
    asyncio.run(go())


def test_gates_off_keeps_the_undeclared_flow(tmp_path):
    async def go():
        async with _fusion(tmp_path, active=()) as (main, fake):
            await _turn(main, fake, call("delegate_task", LEGACY, call_id="work"), say("Done."))
            assert "declare_topic" not in _output(main, "work"), "the intent gate stays inert without v3"
    asyncio.run(go())


def test_complex_execute_needs_the_validated_work_plan(tmp_path):
    async def go():
        async with _fusion(tmp_path, active=("v3",)) as (main, fake):  # preference 25 skips the debate
            await _turn(main, fake,
                        _declare("complex", "execute", "Rebuild the cache."),
                        call("delegate_task", LEGACY, call_id="early"),
                        call("submit_work_plan", {"data": {"delivery": "bogus"}}, call_id="bad"),
                        call("edit_file", {"file_path": "x.txt", "old_str": "a", "new_str": "b"}, call_id="edit"),
                        call("submit_work_plan", {"data": _plan("candidates", [_work_item("a", "Alpha", "0.9", ["a.txt"]),
                                                                              _work_item("b", "Beta", "0.5", ["b.txt"])])},
                             call_id="workplan"),
                        call("delegate_task", LEGACY, call_id="work"), say("Done."))
            assert "submit_work_plan" in _output(main, "early")
            assert "Work plan refused" in _output(main, "bad")
            assert "submit_work_plan" in _output(main, "edit"), "writes stay gated until the plan validates"
            assert "validated" in _output(main, "workplan")
            assert "frozen work item" in _output(main, "work"), "the valid plan unlocks the next gate"
    asyncio.run(go())


def test_a_simple_execute_declaration_skips_work_plans(tmp_path):
    async def go():
        async with _fusion(tmp_path, active=("v3",)) as (main, fake):
            await _turn(main, fake,
                        _declare("simple", "execute", "Fix it."),
                        call("submit_work_plan", {"data": _plan()}, call_id="workplan"), say("Done."))
            assert "plan or complex-execute" in _output(main, "workplan")
    asyncio.run(go())


def test_a_validated_work_plan_supplies_the_route_items(tmp_path):
    async def go():
        # N=50 keeps the 0.300/0.850 seat gates and skips the debate outright.
        async with _debating(tmp_path, preference=50, active=("route", "v3")) as (main, fake):
            await _turn(main, fake,
                        _declare("complex", "execute", "Split the rebuild."),
                        call("submit_work_plan", {"data": _plan("candidates", [_work_item("a", "Alpha", "0.9", ["a.txt"]),
                                                                              _work_item("b", "Beta", "0.5", ["b.txt"])])},
                             call_id="workplan"),
                        call("submit_route", {"destination": "split", "reason": "Two workstreams.", "verification": "runner",
                                              "items": [{"id": "x", "workstream": "Fake", "role": "worker", "score": 0.01,
                                                         "reason": "side channel", "paths": ["z.txt"], "deps": []}]},
                             call_id="route"),
                        say("Routed."))
            frozen = main._fusion.registry.allocations
            assert ("fake", "worker") not in frozen, "the model cannot re-score items past the work plan"
            assert frozen[("alpha", "worker")]["seat"] == "L1" and frozen[("beta", "worker")]["seat"] == "L2"
    asyncio.run(go())


def test_the_gate_covers_map_tools_and_untrusted_mcp_reads(tmp_path):
    rogue = StubClient("rogue", [tool("rogue", "sweep", readOnlyHint=True)])
    trusty = StubClient("trusty", [tool("trusty", "peek", readOnlyHint=True, destructiveHint=False, openWorldHint=False)])
    trusty.trusted_annotations = True
    tools = make(tmp_path, clients=[rogue, trusty])
    tools.declare_state = lambda: None
    denied = tools.check(tool_call("update_map_entry"))
    assert denied.verdict == "deny" and "declare_topic" in denied.reason
    denied = tools.check(tool_call("choose_map_location"))
    assert denied.verdict == "deny" and "declare_topic" in denied.reason
    assert tools.check(tool_call("mcp__rogue__sweep")).verdict == "deny", "an untrusted read_only flag is no bypass"
    assert tools.check(tool_call("mcp__trusty__peek")).verdict == "allow", "a trusted read stays a read"
    tools.declare_state = lambda: {"intent": "discussion"}
    assert tools.check(tool_call("mcp__trusty__peek")).verdict == "allow"
    assert tools.check(tool_call("update_map_entry")).verdict == "deny"


def test_an_approval_turn_routes_the_bound_work_plan(tmp_path):
    async def go():
        async with _debating(tmp_path, preference=50, active=("route", "v3")) as (main, fake):
            main.mode = "spec"
            await _turn(main, fake,
                        _declare("simple", "plan", "Plan the rebuild."),
                        call("submit_work_plan", {"data": _plan("candidates", [_work_item("a", "Alpha", "0.9", ["a.txt"]),
                                                                              _work_item("b", "Beta", "0.5", ["b.txt"])])},
                             call_id="workplan"),
                        call("submit_plan", {"plan": "Ship the split."}, call_id="plan"))
            assert "validated" in _output(main, "workplan")
            bound = main.saved.origin.get("fusionWorkPlan")
            assert isinstance(bound, dict) and bound["plan"] == main.saved.plan["id"], \
                "the work plan binds to the pending version"
            fake.script(call("submit_work_plan", {"data": _plan("direct")}, call_id="swap"),
                        call("submit_route", {"destination": "split", "reason": "Two workstreams.", "verification": "runner",
                                              "items": [{"id": "x", "workstream": "Fake", "role": "worker", "score": 0.01,
                                                         "reason": "side channel", "paths": ["z.txt"], "deps": []}]},
                             call_id="route"),
                        say("Routed."))
            assert main.approve_plan(main.saved.plan["id"])
            await finish_turn(main)
            assert "cannot be replaced" in _output(main, "swap"), "the bound work plan is not the model's to rewrite"
            frozen = main._fusion.registry.allocations
            assert ("fake", "worker") not in frozen, "the approval turn ignores the model's side-channel items"
            assert frozen[("alpha", "worker")]["seat"] == "L1" and frozen[("beta", "worker")]["seat"] == "L2"
    asyncio.run(go())


def test_a_reopened_approval_carries_no_execute_receipt(tmp_path):
    async def go():
        project = tmp_path / "project"
        project.mkdir()
        _identity_folder()
        with FakeResponses() as fake:
            engine = make_engine(tmp_path, fake, settings=AppSettings(fusion_gates=_gates("v3")))
            main = await engine.open_new(project, model="gpt-6.1-sol", autonomy="full")
            main.enable_fusion(Policy(MAIN, (WORKER,), LIGHT, 0, max_seconds=30, preference=25, reasoning={}))
            try:
                main.mode = "spec"
                await _turn(main, fake,
                            _declare("simple", "plan", "Plan the rebuild."),
                            call("submit_work_plan", {"data": _plan("direct")}, call_id="workplan"),
                            call("submit_plan", {"plan": "Ship it."}, call_id="plan"))
                plan_id = main.saved.plan["id"]
                # The process dies between approval and the first dispatch: the archive is what a reopen restores.
                main.saved.plan = {**main.saved.plan, "status": "approved", "execution": "starting"}
                main._save_now(required=True)
                session_id = main.session_id
                await engine.close(main)
                restored = await engine.open_saved(project, session_id)
                bound = restored.saved.origin.get("fusionWorkPlan")
                assert isinstance(bound, dict) and bound["plan"] == plan_id, "the binding survives the file round-trip"
                reopened_id = restored.saved.plan["id"]
                assert reopened_id != plan_id and restored.saved.plan["status"] == "pending"
                assert restored.mode == "spec"
                fake.script(call("delegate_task", LEGACY, call_id="exec"), say("Answered."))
                assert restored.approve_plan(reopened_id)
                await finish_turn(restored)
                receipt = restored._declare_receipt
                assert receipt.get("approved") and receipt.get("intent") == "discussion", \
                    "approving the re-issued version must not mint an execute receipt"
                assert "declared a discussion" in _output(restored, "exec") and not restored.agents
            finally:
                await engine.close_all()
    asyncio.run(go())
