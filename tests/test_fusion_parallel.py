"""Functional expert affinity and actual overlapping model execution, using only local fakes."""
from __future__ import annotations

import asyncio
import json

import pytest

from desktop.core.fusion.registry import FusionError, FusionRegistry, Policy
from tests.chatgpt_support import finish_turn, make_engine
from tests.fake_responses import FakeResponses, call, calls, say
from tests.fakes import until

MAIN, WORKER = "chatgpt:gpt-6.1-sol", "chatgpt:gpt-6-luna"


def task(name, paths):
    return {"workstream": name, "paths": paths, "model": WORKER, "role": "worker", "task": "Implement this deliverable and report evidence."}


def test_workstream_spans_files_reuses_context_and_round_trips(tmp_path):
    registry = FusionRegistry(tmp_path, Policy(MAIN, (WORKER,), WORKER, 0))
    first = registry.assign_workstream("Login flow", ("frontend/login.ts", "desktop/auth.py"), WORKER, "worker")
    registry.begin(first.id)
    registry.finish(first.id, status="done", tokens=25)
    assert registry.assign_workstream("login FLOW", ("tests/auth.py", "docs/login.md"), WORKER, "worker") is first
    other = registry.assign_workstream("Documentation", ("docs/login.md",), WORKER, "worker")
    assert other.id != first.id, "a file may participate in different responsibilities, not permanently own an expert"
    restored = FusionRegistry.from_json(tmp_path, json.loads(json.dumps(registry.to_json())))
    assert restored.agents[first.id].workstream == "Login flow"
    assert restored.assign_workstream("Login flow", ("frontend/session.ts",), WORKER, "worker").id == first.id
    assert restored.tokens == 25


def test_concurrent_writers_conflicts_and_restored_incomplete_work(tmp_path):
    registry = FusionRegistry(tmp_path, Policy(MAIN, (WORKER,), WORKER, 0))
    a = registry.assign_workstream("Login", ("frontend/auth", "desktop/auth.py"), WORKER, "worker")
    b = registry.assign_workstream("Icons", ("frontend/icons",), WORKER, "worker")
    conflict = registry.assign_workstream("Auth tests", ("frontend/auth/test.ts",), WORKER, "worker")
    unknown = registry.assign_workstream("Unscoped review", (), WORKER, "worker")
    registry.begin(a.id)
    registry.begin(b.id)
    with pytest.raises(FusionError, match="already running"):
        registry.begin(a.id)
    for agent in (conflict, unknown):
        with pytest.raises(FusionError, match="overlapping"):
            registry.begin(agent.id)
    assert registry.calls == 2
    restored = FusionRegistry.from_json(tmp_path, registry.to_json())
    assert restored.agents[a.id].status == restored.agents[b.id].status == "interrupted"
    with pytest.raises(FusionError, match="incomplete"):
        restored.begin(a.id)
    registry.finish(a.id, status="done", tokens=10)
    registry.begin(conflict.id)
    assert b.status == conflict.status == "running"


@pytest.mark.parametrize("batch", [False, True])
@pytest.mark.parametrize("stop", [False, True])
def test_independent_workers_overlap_and_stop_without_replay(tmp_path, batch, stop):
    async def go():
        project = tmp_path / "project"
        project.mkdir()
        with FakeResponses() as fake:
            engine = make_engine(tmp_path, fake)
            main = await engine.open_new(project, model="gpt-6.1-sol")
            main.enable_fusion(Policy(MAIN, (WORKER,), WORKER, 0))
            assignments = [task("Login flow", ["frontend/auth", "desktop/auth.py"]), task("Icon design", ["assets/icons"])]
            dispatch = call("delegate_tasks", {"tasks": assignments}) if batch else calls([
                ("delegate_task", json.dumps(item), f"call-{i}", f"fc-{i}") for i, item in enumerate(assignments)])
            fake.script(dispatch, [("hold", "first"), *say("First worker report.")],
                        [("hold", "second"), *say("Second worker report.")], say("Both reports verified."))
            try:
                assert main.send_prompt("Implement independent login and icon work")
                await until(lambda: len(fake.requests) == 3, timeout=10)
                runtime = main._fusion
                assert len(runtime._active) == 2
                assert all(view.record.status == "running" for view in main.agents.values())
                assert {view.name for view in main.agents.values()} == {"Login flow", "Icon design"}
                assert runtime.registry.calls == 2
                if stop:
                    await main.interrupt()
                    await finish_turn(main)
                    assert not runtime._active and not runtime._delegations
                    assert all(view.record.status == "interrupted" for view in main.agents.values())
                    saved = engine.sessions.load(main.session_id)
                    assert all(record["status"] == "interrupted" for record in saved.fusion["agents"])
                    assert len(fake.requests) == 3
                else:
                    fake.release("first")
                    await until(lambda: len(runtime._active) == 1)
                    assert runtime.registry.tokens == 150
                    fake.release("second")
                    await finish_turn(main)
                    assert runtime.registry.tokens == 300 and not runtime._active
                    assert runtime.known_tokens() == 600, "each worker is counted once, including while its sibling runs"
                    assert len(fake.requests) == 4
                    assert "First worker report." in str(fake.requests[-1]["input"])
                    assert "Second worker report." in str(fake.requests[-1]["input"])
                    ident = main.session_id
                    expert = next(view for view in main.agents.values() if view.name == "Login flow")
                    await main.close()
                    resumed = await engine.open_saved(project, ident)
                    fake.script(call("delegate_task", task("Login flow", ["tests/login.py"])), say("Follow-up report."), say("Verified."))
                    assert resumed.send_prompt("Add the login regression")
                    await finish_turn(resumed)
                    assert expert.session_id in resumed.agents and len(resumed.agents) == 2
                    assert "worker report." in str(fake.requests[-2]["input"])
            finally:
                fake.release("first")
                fake.release("second")
                await engine.close_all()
    asyncio.run(go())


def test_failed_reservation_checkpoint_does_not_leave_phantom_running_experts(tmp_path, monkeypatch):
    async def go():
        project = tmp_path / "project"
        project.mkdir()
        with FakeResponses() as fake:
            engine = make_engine(tmp_path, fake)
            main = await engine.open_new(project, model="gpt-6.1-sol")
            main.enable_fusion(Policy(MAIN, (WORKER,), WORKER, 0))
            original = main.saved.fusion
            def denied(**kwargs):
                raise OSError("test checkpoint failure")
            try:
                with monkeypatch.context() as patch:
                    patch.setattr(main, "_save_now", denied)
                    with pytest.raises(OSError):
                        await main._fusion.delegate("Login flow", WORKER, "worker", "Implement login", ("src/auth",))
                assert main.saved.fusion == original and not main._fusion.registry.agents
                assert main._fusion.registry.calls == 0 and not main.agents and not fake.requests
            finally:
                await engine.close_all()
    asyncio.run(go())


def test_legacy_module_archive_and_transferred_owner_can_adopt_a_workstream(tmp_path):
    registry = FusionRegistry(tmp_path, Policy(MAIN, (MAIN, WORKER), WORKER, 0))
    old = registry.assign("src/auth", WORKER, "worker")
    archive = registry.to_json()
    archive["version"] = 1
    for entry in archive["agents"]:
        entry.pop("workstream")
        entry.pop("paths")
    restored = FusionRegistry.from_json(tmp_path, archive)
    replacement = restored.transfer(old.id, MAIN, "worker")
    assert restored.assign_workstream("src/auth", ("frontend/auth", "desktop/auth.py"), MAIN, "worker") is replacement
    final = FusionRegistry.from_json(tmp_path, restored.to_json())
    assert final.agents[replacement.id].transferred_from == old.id
    assert final.agents[replacement.id].paths == ("frontend/auth", "desktop/auth.py")


def test_acp_main_uses_the_guarded_batch_bridge_for_parallel_workers(tmp_path):
    from tests.app_support import ready_core
    from tests.fusion_acp_support import make_guarded_engine
    async def go():
        harness = make_guarded_engine(tmp_path)
        core, fake, _, _, _ = await ready_core(tmp_path, devin_engine=harness.engine)
        try:
            await until(lambda: any(item["source"] == "devin" for item in core.catalog.models), timeout=20)
            cid = (await core.call("session.new", {"projectId": str(tmp_path / "proj")}))["id"]
            main = core._by_cid[cid].controller
            main.enable_fusion(Policy("devin:swe-2-high", (WORKER,), WORKER, 0))
            fake.script([("hold", "one"), *say("Login report.")], [("hold", "two"), *say("Icon report.")])
            assert main.send_prompt("[fusion-delegate-parallel] Implement the independent workstreams.")
            await until(lambda: len(fake.requests) == 2, timeout=30)
            assert len(main._fusion._active) == 2
            assert all(view.record.status == "running" for view in main.agents.values())
            fake.release("one")
            fake.release("two")
            await finish_turn(main, timeout=30)
            assert all(view.record.status == "done" for view in main.agents.values())
            assert any(item.get("name") == "delegate_tasks" for item in main.saved.items)
            assert main._fusion.registry.tokens == 300
        finally:
            fake.release("one")
            fake.release("two")
            await core.shutdown()
    asyncio.run(go())
