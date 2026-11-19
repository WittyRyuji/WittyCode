from __future__ import annotations

import asyncio
import json

import pytest

from desktop.core.fusion import observer
from tests.fake_responses import call, calls, say
from tests.test_fusion_acceptance_runtime import _verdict
from tests.test_fusion_identity_runtime import _brief, _fusion, _output, _turn
from tests.test_fusion_inject_runtime import _prompt, _rated_report
from tests.test_fusion_route_runtime import _report, _route

_ACTIVE = ("drive", "brief", "report", "observer")
pytestmark = pytest.mark.fusion_gates(*_ACTIVE)


def _delegate(call_id="delegate", **kwargs):
    return call("delegate_task", _brief(**kwargs), call_id=call_id)


def _edits(*paths):
    return calls([("create_file", json.dumps({"file_path": path, "content": "Stock counted."}), f"write-{i}", f"edit-{i}")
                  for i, path in enumerate(paths)])


def _records(main, kind):
    fusion = main._fusion
    return [item.record for item in fusion.drive.candidates(shared=False, retention=fusion.retention()) if item.record.kind == kind]


def test_out_of_scope_pauses_after_write_and_before_next_tool_then_can_resume(tmp_path):
    async def go():
        async with _fusion(tmp_path, active=_ACTIVE) as (main, fake):
            await _turn(main, fake, _delegate(), _edits("spill.txt", "inventory.txt"), say("Paused."))
            assert _output(main, "delegate").startswith("Observer report (out_of_scope):")
            assert (main.project / "spill.txt").is_file() and not (main.project / "inventory.txt").exists()
            agent, = main.agents.values()
            assert agent.record.status == "interrupted" and agent.controller.saved.turns[-1].status == "interrupted"
            assert "Observer paused" in _output(agent.controller, "write-1")
            finding, = _records(main, "finding")
            assert finding.key == f"observer:{agent.session_id}" and finding.paths == ("inventory.txt",)
            assert finding.summary.startswith("Observer [out_of_scope] Coder-Inventory:")
            assert main._fusion.registry.task_metrics[-1]["observer_reports"] == 1
            await _turn(main, fake, _delegate("resume", resume=True), _edits("inventory.txt"), _report(), say("Reviewed."))
            assert len(main.agents) == 1 and agent.record.status == "done" and (main.project / "inventory.txt").is_file()
            assert main._fusion.registry.task_metrics[-1]["observer_reports"] == 0
    asyncio.run(go())


@pytest.mark.parametrize("repeats", [2, 3])
def test_repeat_pauses_on_three_identical_calls_and_results(tmp_path, repeats):
    async def go():
        async with _fusion(tmp_path, active=_ACTIVE) as (main, fake):
            (main.project / "inventory.txt").write_text("Stock counted.", encoding="utf-8")
            reads = [call("read_file", {"file_path": "inventory.txt"}, call_id=f"read-{i}") for i in range(repeats)]
            report = [_report()] if repeats == 2 else []
            await _turn(main, fake, _delegate(), *reads, *report, say("Reviewed."))
            assert _output(main, "delegate").startswith("Observer report (repeat):") == (repeats == 3)
            assert len(_records(main, "finding")) == int(repeats == 3)
    asyncio.run(go())


def _events(main, trace):
    return [observer.Event(**json.loads(line)) for line in
            "".join(main._fusion.drive.read(pointer) for pointer in trace.pointers).splitlines()]


def test_in_scope_trace_keeps_completed_events_and_is_not_injected(tmp_path):
    async def go():
        async with _fusion(tmp_path, active=(*_ACTIVE, "inject")) as (main, fake):
            await _turn(main, fake, _delegate(), _edits("inventory.txt"), _report(), say("Reviewed."))
            drive = main._fusion.drive
            trace, = drive.traces()
            edit, report = _events(main, trace)
            assert trace.summary == "Coder-Inventory: 2 tool calls" and trace.paths == ("inventory.txt",)
            assert edit.name == "create_file" and edit.kind == "edit" and edit.changed and edit.written == ["inventory.txt"]
            assert report.name == "submit_report" and report.kind == "read" and not report.changed and edit.at <= report.at
            assert all(len(value) == 64 for value in (edit.call, edit.result, report.call, report.result))
            prior, = _records(main, "report")
            first = len(fake.requests)
            await _turn(main, fake, _delegate("again"), _rated_report(prior), say("Reviewed again."))
            prompt = _prompt(fake.requests[first + 1])
            assert prior.id in prompt and trace.id not in prompt and trace.pointers[0] not in prompt
            first_trace, second_trace = drive.traces()
            assert first_trace == trace and first_trace.key != second_trace.key and not _records(main, "finding")
    asyncio.run(go())


def test_read_only_edit_is_refused_before_observer_runs(tmp_path):
    async def go():
        async with _fusion(tmp_path, active=_ACTIVE) as (main, fake):
            await _turn(main, fake, _delegate(paths=[], read_only=True), _edits("inventory.txt"), _report(), say("Reviewed."))
            agent, = main.agents.values()
            assert "read-only" in _output(agent.controller, "write-0").lower()
            assert agent.record.status == "done" and not (main.project / "inventory.txt").exists()
            assert not _records(main, "finding")
    asyncio.run(go())


def test_observer_signal_requires_a_sampler_at_acceptance(tmp_path):
    async def go():
        async with _fusion(tmp_path, active=(*_ACTIVE, "route", "acceptance")) as (main, fake):
            await _turn(main, fake, _route(), _delegate(), _edits("spill.txt"), say("Paused."), say("Still needs checks."))
            verdict = _verdict(main)
            assert "observer_reported" in verdict["signals"] and "sampler" in verdict["missing"]
    asyncio.run(go())


def test_observer_gate_off_does_not_pause_or_retain_observations(tmp_path):
    async def go():
        async with _fusion(tmp_path, active=_ACTIVE[:-1]) as (main, fake):
            await _turn(main, fake, _delegate(), _edits("spill.txt", "inventory.txt"), _report(), say("Reviewed."))
            assert (main.project / "inventory.txt").is_file() and "Observer report" not in _output(main, "delegate")
            assert not _records(main, "finding") and not main._fusion.drive.traces()
    asyncio.run(go())


def _failed_reads():
    return [call("read_file", {"file_path": f"missing-{i}.txt"}, call_id=f"miss-{i}") for i in range(observer.STALL_ROUNDS)]


def test_judgement_off_does_not_request_a_model_when_precondition_holds(tmp_path):
    async def go():
        async with _fusion(tmp_path, active=_ACTIVE) as (main, fake):
            main.manager.settings.fusion_observer_judgement = False
            reads = _failed_reads()
            await _turn(main, fake, _delegate(), *reads, _report(), say("Reviewed."))
            trace, = main._fusion.drive.traces()
            errors = _events(main, trace)[:-1]
            assert len(errors) == observer.STALL_ROUNDS and observer.precondition(errors)
            assert len({event.call for event in errors}) == len(reads)
            assert len(fake.requests) == len(reads) + 3 and not _records(main, "finding")
    asyncio.run(go())


@pytest.mark.parametrize("agrees", [False, True])
def test_enabled_judgement_uses_two_lightweight_requests_and_requires_agreement(tmp_path, agrees):
    async def go():
        async with _fusion(tmp_path, active=_ACTIVE) as (main, fake):
            main.manager.settings.fusion_observer_judgement = True
            yes = say('{"stuck":true,"confidence":0.97}')
            second = yes if agrees else say('{"stuck":false,"confidence":0.97}')
            report = [] if agrees else [_report()]
            await _turn(main, fake, _delegate(), *_failed_reads(), yes, second, *report, say("Reviewed."))
            requests = [request for request in fake.requests if request["model"] == "gpt-6-luna"]
            assert len(requests) == 2 and all(not request.get("tools") for request in requests)
            assert _output(main, "delegate").startswith("Observer report (stuck):") == agrees
            assert len(_records(main, "finding")) == int(agrees)
    asyncio.run(go())


@pytest.mark.parametrize(("unit", "kind"), [("step", "over_budget"), ("time", "timeout")])
def test_budget_stops_keep_existing_outcome_and_are_counted_once(tmp_path, unit, kind):
    async def go():
        async with _fusion(tmp_path, active=_ACTIVE) as (main, fake):
            budget = {"steps": 1} if unit == "step" else {"seconds": 1}
            events = _edits("inventory.txt") if unit == "step" else [("hold", "expert"), *say("Late report.")]
            try:
                await _turn(main, fake, _delegate(budget=budget), events, say("Reviewed."))
            finally:
                fake.release("expert")
            assert _output(main, "delegate").startswith(f"The expert exceeded its {unit} budget.")
            assert main._fusion.registry.task_metrics[-1]["observer_reports"] == 1
            finding, = _records(main, "finding")
            assert f"Observer [{kind}]" in finding.summary
    asyncio.run(go())


def test_acp_bridge_tool_uses_the_same_observer_boundary(tmp_path, monkeypatch):
    from desktop.core.fusion.acp import AcpRun
    from tests.app_support import ready_core
    from tests.fakes import until
    from tests.fusion_acp_support import make_guarded_engine
    from tests.test_fusion_identity_runtime import DEVIN, LIGHT, MAIN, _gates, _identity_folder

    original = AcpRun.dispatch

    async def outside_write(self, method, params):
        if method == "tools/call" and params.get("name") == "read_file":
            params = {**params, "name": "create_file", "arguments": {"file_path": "spill.txt", "content": "Stock counted."}}
        return await original(self, method, params)

    monkeypatch.setattr(AcpRun, "dispatch", outside_write)

    async def go():
        _identity_folder()
        core, fake, _, _, _ = await ready_core(tmp_path, devin_engine=make_guarded_engine(tmp_path).engine)
        try:
            await until(lambda: any(item["source"] == "devin" for item in core.catalog.models), timeout=20)
            core.settings.fusion_gates = _gates(*_ACTIVE)
            core.settings.fusion_main, core.settings.fusion_workers, core.settings.fusion_light = MAIN, [DEVIN], LIGHT
            cid = (await core.call("session.new", {"projectId": str(tmp_path / "proj"), "fusion": True}))["id"]
            main = core._by_cid[cid].controller
            assert await main.update_settings(autonomy="full")
            await _turn(main, fake, _delegate(goal="[fusion-read] Check stock."), say("Reviewed."))
            child = next(iter(main.agents.values())).controller
            assert child.last_turn_end.reason == "observer" and child.saved.turns[-1].status == "interrupted"
            trace, = main._fusion.drive.traces()
            event, = _events(main, trace)
            assert event.written == ["spill.txt"] and _output(main, "delegate").startswith("Observer report (out_of_scope):")
        finally:
            await core.shutdown()
    asyncio.run(go())
