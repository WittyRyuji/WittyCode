"""Per-task metrics are counted for every Fusion conversation, with the gates on or off (design section 10)."""
from __future__ import annotations

import asyncio

from desktop.core.fusion.metrics import RETAINED_TASKS, Recorder
from desktop.core.fusion.registry import FusionRegistry, Policy
from tests.chatgpt_support import finish_turn, make_engine, until
from tests.fake_responses import FakeResponses, call, say
from tests.test_fusion_identity_runtime import _brief, _fusion, _turn
from tests.test_fusion_route_runtime import ROUTE_GATES, _report, _route

POLICY = Policy("chatgpt:gpt-6.1-sol", ("chatgpt:gpt-6-luna",), "chatgpt:gpt-6-luna", 5)
LEGACY = {"scope": "src", "model": "chatgpt:gpt-6-luna", "role": "worker", "task": "Inspect."}
DRIVE_COUNTS = {"shared_hits", "unrated", "out_of_use", "above_qk", "rating_-1", "rating_0", "rating_1"}


def _tasks(main):
    return main._fusion.registry.task_metrics


def _delegate(identity="Coder", *, call_id="delegate"):
    return call("delegate_task", _brief(identity), call_id=call_id)


def test_todays_flow_records_the_expert_and_the_main_agent(tmp_path):
    async def go():
        with FakeResponses() as fake:
            engine = make_engine(tmp_path, fake)
            main = await engine.open_new(tmp_path / "project", model="gpt-6.1-sol")
            main.enable_fusion(POLICY)
            fake.script(call("delegate_task", LEGACY, call_id="d1"), say("Inspected."), say("Reviewed."))
            try:
                assert main.send_prompt("Inspect")
                await finish_turn(main)
                task, = _tasks(main)
                assert task["destination"] == "" and task["accepted"] is None and task["runner_first_pass"] is None
                assert not task["drive"] and list(task["experts"]) == ["worker"]
                expert = next(iter(main.agents.values())).controller
                worker = task["experts"]["worker"]
                assert worker["runs"] == 1 and worker["unknown"] == 0 and worker["tokens"] == expert.usage.total > 0
                assert worker["first_request_tokens"] == 120
                assert task["main"]["runs"] == 1 and task["main"]["tokens"] == main.usage.total > 0
                assert engine.sessions.load(main.session_id).fusion["metrics"] == _tasks(main)
                assert main._fusion.measurement is None
            finally:
                await engine.close_all()
    asyncio.run(go())


def test_a_routed_and_verified_task_records_route_identities_and_acceptance(tmp_path):
    async def go():
        async with _fusion(tmp_path, active=(*ROUTE_GATES, "acceptance")) as (main, fake):
            (main.project / "inventory.txt").write_text("Two boxes.", encoding="utf-8")
            await _turn(main, fake, _route(), _delegate(), _report(), _delegate("Runner", call_id="verify"),
                        call("read_file", {"file_path": "inventory.txt"}), _report(evidence=["inventory.txt: Two boxes."]),
                        say("Verified."))
            task, = _tasks(main)
            assert task["destination"] == "coder" and task["accepted"] is True and task["runner_first_pass"] is True
            assert task["sampler_blocking"] is None and task["misroutes"] == 0
            assert {name: usage["runs"] for name, usage in task["experts"].items()} == {"Coder": 1, "Runner": 1}
            assert all(usage["tokens"] and not usage["unknown"] for usage in task["experts"].values())
            assert set(task["drive"]) == DRIVE_COUNTS and task["drive"]["unrated"] == 0
            summary = main._fusion.metrics_summary()
            assert summary["tasks"] == summary["accepted"] == 1 and summary["destinations"]["coder"] == 1
            assert summary["runner_first_pass_rate"] == 1.0 and summary["tokens_per_accepted"] > 0
    asyncio.run(go())


def test_failed_checks_and_an_unverified_turn_are_counted_as_such(tmp_path):
    async def go():
        async with _fusion(tmp_path, active=(*ROUTE_GATES, "acceptance")) as (main, fake):
            await _turn(main, fake, _route(verification="runner_and_sampler"), _delegate(), _report(),
                        _delegate("Runner", call_id="verify"), _report("concerns"),
                        _delegate("QualitySampler", call_id="sample"), _report("concerns"), say("Both checks found problems."),
                        say("There is no passing check."))
            task, = _tasks(main)
            assert task["runner_first_pass"] is False and task["sampler_blocking"] is True
            assert task["accepted"] is False  # A completed check reporting concerns is not a pass.
            assert main._fusion.metrics_summary()["runner_first_pass_rate"] == 0.0
            await _turn(main, fake, _route(), _delegate(call_id="again"), _report(), say("First answer."), say("Final answer."))
            assert [item["accepted"] for item in _tasks(main)] == [False, False]
            assert main._fusion.metrics_summary()["accepted"] == 0
    asyncio.run(go())


def test_a_misjudged_route_is_counted_in_the_task(tmp_path):
    async def go():
        async with _fusion(tmp_path, active=ROUTE_GATES) as (main, fake):
            await _turn(main, fake, _route(), _delegate(), _report("blocked"),
                        _route("self", "self_check", reason="Too entangled to hand over.", call_id="again"), say("I will do it myself."))
            task, = _tasks(main)
            assert task["misroutes"] == 1 and task["destination"] == "self" and task["accepted"] is None
            assert main._fusion.metrics_summary()["misroutes"] == 1
    asyncio.run(go())


def test_unknown_expert_usage_stays_unknown(tmp_path):
    async def go():
        with FakeResponses() as fake:
            engine = make_engine(tmp_path, fake)
            main = await engine.open_new(tmp_path / "project", model="gpt-6.1-sol")
            main.enable_fusion(POLICY)
            answer = say("Inspected.")
            answer[-1]["response"]["usage"] = None
            fake.script(call("delegate_task", LEGACY, call_id="d1"), answer, say("Reviewed."))
            try:
                assert main.send_prompt("Inspect")
                await finish_turn(main)
                worker = _tasks(main)[0]["experts"]["worker"]
                assert worker["tokens"] is None and worker["unknown"] == 1 and worker["runs"] == 1
                assert main._fusion.metrics_summary()["unrouted"]["tokens"] is None
            finally:
                await engine.close_all()
    asyncio.run(go())


def test_a_stopped_turn_still_closes_its_record(tmp_path):
    async def go():
        with FakeResponses() as fake:
            engine = make_engine(tmp_path, fake)
            main = await engine.open_new(tmp_path / "project", model="gpt-6.1-sol")
            main.enable_fusion(POLICY)
            fake.script([("hold", "main"), *say("Never shown.")])
            try:
                assert main.send_prompt("Inspect")
                await until(lambda: len(fake.requests) == 1)
                await main.interrupt()
                await until(lambda: main._turn is None)
                task, = _tasks(main)
                assert task["main"]["runs"] == 1 and not task["experts"] and main._fusion.measurement is None
                fake.release("main")
                fake.script(say("Second turn."))
                assert main.send_prompt("Again")
                await finish_turn(main)
                assert len(_tasks(main)) == 2
            finally:
                fake.release("main")
                await engine.close_all()
    asyncio.run(go())


def test_finished_tasks_are_capped_and_survive_a_reload(tmp_path):
    registry = FusionRegistry(tmp_path, POLICY)
    recorder = Recorder()
    for index in range(RETAINED_TASKS + 5):
        recorder.begin(f"task-{index}")
        recorder.main(10, 0.5)
        registry.record_task(recorder.finish())
    assert len(registry.task_metrics) == RETAINED_TASKS and registry.task_metrics[0]["task_id"] == "task-5"
    assert FusionRegistry.from_json(tmp_path, registry.to_json()).task_metrics == registry.task_metrics
    damaged = registry.to_json()
    damaged["metrics"] = [*damaged["metrics"][:2], {"task_id": "no figures"}, "junk"]
    assert [task["task_id"] for task in FusionRegistry.from_json(tmp_path, damaged).task_metrics] == ["task-5", "task-6"]
    older = {key: value for key, value in registry.to_json().items() if key != "metrics"}
    assert FusionRegistry.from_json(tmp_path, older).task_metrics == []
