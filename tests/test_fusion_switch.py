"""Idle conversations switch Fusion mode without replaying work or losing expert ownership."""
from __future__ import annotations

import asyncio
from copy import deepcopy
from types import SimpleNamespace

import pytest

from desktop.core.app_core import AppError
from desktop.core.chatgpt.errors import ChatGPTError
from desktop.core.fusion.registry import Policy
from desktop.core.usage import Tokens
from tests.app_support import ready_core
from tests.chatgpt_support import finish_turn
from tests.fake_responses import call, say
from tests.fakes import until

SOL = "chatgpt:gpt-6.1-sol"
LUNA = "chatgpt:gpt-6-luna"
TASK = {"scope": "src/module", "model": LUNA, "role": "worker", "task": "Inspect the module."}


def configure(core):
    core.settings.fusion_main = SOL
    core.settings.fusion_workers = [LUNA]
    core.settings.fusion_light = LUNA
    core.settings.fusion_reasoning = {SOL: "high", LUNA: "low"}


async def choose(core, cid, model):
    """Inspect the applied handoff before starting another turn, so usage/ownership assertions remain deterministic.

    Selection alone no longer applies a handoff. The real select -> send path, including failed sends and drafts, is
    exercised separately in test_model_switch_pending.py; these lifecycle tests cover the application phase itself.
    """
    result = await core.call("session.choose", {"id": cid, "field": "model", "value": model})
    conv = core._by_cid[cid]
    if result.get("ok") and conv.pending_model is not None:
        await core._apply_pending_model(conv)
        core._set_pending_model(conv, None)
    return result


def test_existing_conversation_switches_in_place_and_restores_paused_experts(tmp_path):
    async def go():
        core, fake, _, _, _ = await ready_core(tmp_path)
        try:
            configure(core)
            defaults = core.defaults()
            cid = (await core.call("session.new", {"projectId": str(tmp_path / "proj"), "modelId": LUNA}))["id"]
            main = core._by_cid[cid].controller
            fake.script(say("Before Fusion."))
            await core.call("session.send", {"id": cid, "text": "Inspect"})
            await finish_turn(main)
            items, native, count = deepcopy(main.saved.items), main.session_id, len(fake.requests)
            fake.script(say("Before Fusion."))
            assert (await choose(core, cid, "fusion"))["ok"]
            assert main.session_id == native and main.saved.items == items and len(fake.requests) == count + 1
            assert fake.requests[-1]["model"] == "gpt-6-luna" and main.saved.compaction["upto"] == len(items)
            assert main.model == "gpt-6.1-sol" and main.reasoning == "high"
            fake.script(call("delegate_task", TASK), say("Expert baseline."), say("Reviewed."))
            await core.call("session.send", {"id": cid, "text": "Delegate"})
            await finish_turn(main)
            expert = next(iter(main.agents))
            count = len(fake.requests)
            fake.script(say("Before Fusion. Reviewed."))
            assert (await choose(core, cid, LUNA))["ok"]
            assert main._fusion is None and main._paused_fusion is not None
            assert main.session_id == native and main.model == "gpt-6-luna" and expert in main.agents
            assert len(fake.requests) == count + 1 and fake.requests[-1]["model"] == "gpt-6.1-sol"
            fake.script(say("Between modes."))
            await core.call("session.send", {"id": cid, "text": "Continue without experts"})
            await finish_turn(main)
            assert "delegate_task" not in {tool.get("name") for tool in fake.requests[-1]["tools"]}
            assert "Fusion mode: you are the fixed main agent" not in fake.requests[-1]["instructions"]
            assert main.manager.sessions.load(native).fusion["enabled"] is False
            await core.call("session.close", {"id": cid})
            await until(lambda: main.state.value == "closed")
            cid = (await core.call("session.openSaved", {"projectId": str(tmp_path / "proj"), "id": "cg:" + native}))["id"]
            main = core._by_cid[cid].controller
            await until(lambda: main.state.value == "ready")
            assert main._fusion is None and main._paused_fusion is not None and expert in main.agents
            assert (await core.call("agents.watch", {"id": cid, "agentId": expert}))["ok"]
            core.settings.fusion_main, core.settings.fusion_workers = LUNA, [SOL]
            count = len(fake.requests)
            assert (await choose(core, cid, "fusion"))["ok"]
            assert main.session_id == native and main._fusion.registry.policy.main == LUNA
            assert main._fusion.registry.policy.workers == (SOL,) and main.reasoning == "low"
            assert main._fusion.registry.calls == 1 and len(fake.requests) == count
            fake.script(call("delegate_task", {**TASK, "model": SOL, "task": "Continue inspecting."}), say("Continued."), say("Finished."))
            await core.call("session.send", {"id": cid, "text": "Resume the original responsibility"})
            await finish_turn(main)
            assert len(main.agents) == 2 and main._fusion.registry.calls == 2
            assert main.agents[expert].record.status == "retired"
            replacement = next(view for key, view in main.agents.items() if key != expert)
            assert replacement.record.transferred_from == expert and replacement.record.handoff_complete
            assert "retained tool evidence" in str(fake.requests[-2]["input"])
            assert "Between modes." in str(fake.requests[-3]["input"])
            assert core.defaults() == defaults and len(core.order) == 1
        finally:
            await core.shutdown()
    asyncio.run(go())


def test_fusion_mode_refreshes_instructions_even_when_the_underlying_model_stays_the_same(tmp_path):
    async def go():
        core, fake, _, _, _ = await ready_core(tmp_path)
        try:
            configure(core)
            cid = (await core.call("session.new", {"projectId": str(tmp_path / "proj")}))["id"]
            main = core._by_cid[cid].controller
            for selected, enabled in (("fusion", True), (SOL, False), ("fusion", True)):
                count = len(fake.requests)
                await choose(core, cid, selected)
                assert len(fake.requests) == count, "a mode-only change does not need a source-model handoff"
                fake.script(say("Mode changed."))
                await core.call("session.send", {"id": cid, "text": "Continue"})
                await finish_turn(main)
                assert ("Fusion mode: you are the fixed main agent" in fake.requests[-1]["instructions"]) is enabled
        finally:
            await core.shutdown()
    asyncio.run(go())


@pytest.mark.parametrize("main_model", [SOL, "devin:swe-2-high"])
def test_cross_engine_pause_restores_expert_owner_budget_and_usage_without_double_counting(tmp_path, main_model):
    from tests.fusion_acp_support import make_guarded_engine

    async def go():
        harness = make_guarded_engine(tmp_path)
        core, fake, _, _, _ = await ready_core(tmp_path, devin_engine=harness.engine)
        try:
            configure(core)
            await until(lambda: any(m["source"] == "devin" for m in core.catalog.models))
            cid = (await core.call("session.new", {"projectId": str(tmp_path / "proj")}))["id"]
            main = core._by_cid[cid].controller
            token_limit = 5_000_000 if main_model == SOL else 0
            main.enable_fusion(Policy(main_model, (LUNA,), LUNA, 3, token_limit))
            if main_model == SOL:
                fake.script(call("delegate_task", TASK), say("Persistent expert context."), say("Reviewed."))
            else:
                fake.script(say("Persistent expert context."))
            await core.call("session.send", {"id": cid, "text": "Inspect with an expert" if main_model == SOL else "[fusion-delegate] Inspect"})
            await finish_turn(main)
            conv = core._by_cid[cid]
            core._feed_usage(conv)
            expert, owner = next(iter(main.agents)), main.session_id
            count = len(fake.requests)
            if main_model == SOL:
                fake.script(say("Persistent expert context and reviewed module."))
            assert (await choose(core, cid, "devin:swe-2-high"))["ok"]
            assert core._by_cid[cid].engine == "devin" and len(core.order) == 1
            assert len(fake.requests) == count + (main_model == SOL)
            count = len(fake.requests)
            total, known = conv.ledger.total, main._fusion.known_tokens()
            by_model = dict(conv.ledger.by_model)
            row = core._row_of(conv)
            await until(lambda: main.state.value == "closed")
            other = conv.controller
            await core.call("session.close", {"id": cid})
            await until(lambda: other.state.value == "closed")
            cid = (await core.call("session.openSaved", {"projectId": str(tmp_path / "proj"), "id": row}))["id"]
            conv = core._by_cid[cid]
            await until(lambda: conv.controller.state.value == "ready")
            native_source = conv.controller
            assert (await choose(core, cid, "fusion"))["ok"]
            main = conv.controller
            assert main.session_id != owner and main._fusion.owner == owner and list(main.agents) == [expert]
            assert main._fusion.registry.calls == 1 and main._fusion.registry.policy.max_calls == 3
            assert main._fusion.registry.policy.main == main_model and main.transcript.model == main.model
            assert main._fusion.registry.policy.max_tokens == token_limit and main._fusion.known_tokens() == known
            assert len(fake.requests) == count and len(core.order) == 1
            assert (await core.call("agents.watch", {"id": cid, "agentId": expert}))["ok"]
            core._feed_usage(conv)
            assert conv.ledger.total == total.plus(native_source.usage or Tokens())
            assert all(conv.ledger.by_model[key] == value for key, value in by_model.items() if not key.startswith("devin:"))
            counted = conv.ledger.total
            core._feed_usage(conv)
            assert conv.ledger.total == counted
            if main_model == SOL:
                fake.script(call("delegate_task", {**TASK, "task": "Continue inspecting."}), say("Continued."), say("Finished."))
            else:
                fake.script(say("Continued."))
            await core.call("session.send", {"id": cid, "text": "Continue with the same expert" if main_model == SOL else "[fusion-delegate] Continue"})
            await finish_turn(main)
            assert list(main.agents) == [expert] and main._fusion.registry.calls == 2
            assert "Persistent expert context." in str(fake.requests[-2 if main_model == SOL else -1]["input"])
            assert main.agents[expert].controller.saved.origin["parent"] == owner
        finally:
            await core.shutdown()
    asyncio.run(go())


@pytest.mark.parametrize("scenario", ["native-main", "native-expert", "change-main"])
def test_returning_to_own_loop_releases_inactive_native_processes_but_retains_context(tmp_path, scenario):
    from tests.fusion_acp_support import make_guarded_engine

    async def go():
        harness = make_guarded_engine(tmp_path)
        core, fake, _, _, _ = await ready_core(tmp_path, devin_engine=harness.engine)
        try:
            await until(lambda: any(model["source"] == "devin" for model in core.catalog.models))
            configure(core)
            native_main = scenario != "native-expert"
            core.settings.fusion_main = "devin:swe-2-high" if native_main else SOL
            core.settings.fusion_workers = ["devin:swe-2-high"]
            core.settings.fusion_light = "devin:swe-2-high"
            cid = (await core.call("session.new", {"projectId": str(tmp_path / "proj"), "fusion": True}))["id"]
            main = core._by_cid[cid].controller
            task = {"scope": "src", "model": "devin:swe-2-high", "role": "worker", "task": "Inspect the module."}
            if not native_main:
                fake.script(call("delegate_task", task), say("Reviewed."))
            assert main.send_prompt("Inspect with the native runner")
            await finish_turn(main, timeout=30)
            runner = main if native_main else next(iter(main.agents.values())).controller
            acp, native = runner._acp, runner.saved.origin["fusionAcp"]["session"]
            assert acp.client.running
            if scenario == "change-main":
                assert (await core.call("fusion.changeMain", {"id": cid, "model": SOL}))["ok"]
            else:
                assert (await choose(core, cid, SOL))["ok"]
            assert acp.client is None
            await until(lambda: all(not client.running for client in harness.clients))  # An overlapping metadata query may still be closing.
            assert not harness.engine.running
            fake.script(say("Own-loop work."))
            assert main.send_prompt("Continue with ChatGPT")
            await finish_turn(main)
            assert main.saved.turns[-1].status == "done"
            await until(lambda: all(not client.running for client in harness.clients), timeout=15)
            if scenario == "change-main":
                assert (await core.call("fusion.changeMain", {"id": cid, "model": "devin:swe-2-high"}))["ok"]
            else:
                assert (await choose(core, cid, "fusion"))["ok"]
            assert acp.client is None  # Mode changes never open a guarded model runner; metadata probes may be brief.
            if not native_main:
                fake.script(call("delegate_task", {**task, "task": "Continue inspecting."}), say("Reviewed again."))
            assert main.send_prompt("Continue in the same native context")
            await finish_turn(main, timeout=30)
            runner = main if native_main else next(iter(main.agents.values())).controller
            if native_main:
                assert runner.saved.origin["fusionAcp"]["session"] != native
                assert any(item["session"] == native for item in runner.saved.origin["fusionAcpHistory"])
            else:
                assert runner.saved.origin["fusionAcp"]["session"] == native  # Experts were not reassigned or compacted.
            assert main.saved.turns[-1].status == "done"
        finally:
            await core.shutdown()
    asyncio.run(go())


def test_entering_fusion_from_native_engine_keeps_attachment_confirmation(tmp_path):
    from desktop.core.attachments import text_attachment
    from desktop.core.transcript import TurnStatus
    from tests.devin_support import make_engine

    async def go():
        harness = make_engine(tmp_path)
        core, fake, _, _, _ = await ready_core(tmp_path, devin_engine=harness.engine)
        try:
            configure(core)
            await until(lambda: any(m["source"] == "devin" for m in core.catalog.models))
            cid = (await core.call("session.new", {"projectId": str(tmp_path / "proj"), "modelId": "devin:swe-2-high"}))["id"]
            original = core._by_cid[cid].controller
            turn = original.transcript.new_history_turn("Read these notes", attachments=(text_attachment("notes.txt", "Carry these reviewed notes."),))
            turn.status = TurnStatus.DONE
            original.transcript.add_history_turns([turn])
            result = await choose(core, cid, "fusion")
            assert not result["ok"] and result["choose"]["attachments"][0]["name"] == "notes.txt"
            assert core._by_cid[cid].controller is original and not fake.requests
            with pytest.raises(AppError):
                await core.call("session.choose", {"id": cid, "field": "model", "value": "fusion", "attachments": ["missing"]})
            assert core._by_cid[cid].controller is original
            picked = [result["choose"]["attachments"][0]["id"]]
            result = await core.call("session.choose", {"id": cid, "field": "model", "value": "fusion", "attachments": picked})
            assert result["ok"] and core._by_cid[cid].controller is original and not fake.requests
            fake.script(say("Continued with the selected notes."))
            await core.call("session.send", {"id": cid, "text": "Continue"})
            main = core._by_cid[cid].controller
            assert main._fusion is not None and main.handoff.files == 1
            await finish_turn(main)
            assert "Carry these reviewed notes." in str(fake.requests[-1]["input"])
        finally:
            await core.shutdown()
    asyncio.run(go())


@pytest.mark.parametrize("active", [False, True], ids=["enter", "leave"])
@pytest.mark.parametrize("blocked", ["running", "queue", "pending-plan", "goal"])
def test_mode_switch_is_blocked_until_idle_with_no_pending_work(tmp_path, active, blocked):
    async def go():
        core, fake, _, _, _ = await ready_core(tmp_path)
        conv = None
        try:
            configure(core)
            cid = (await core.call("session.new", {"projectId": str(tmp_path / "proj")}))["id"]
            conv = core._by_cid[cid]
            main = conv.controller
            if active:
                main.enable_fusion(Policy(SOL, (LUNA,), LUNA, 0))
            if blocked == "running":
                fake.script(("hold", "busy"))
                await core.call("session.send", {"id": cid, "text": "Wait"})
                await until(lambda: bool(fake.requests))
            elif blocked == "queue":
                conv.queue.append(SimpleNamespace(id="queued", text="Queued task", attachments=()))
            elif blocked == "pending-plan":
                main.saved.plan = {"status": "pending", "id": "demo-plan", "text": "Inspect"}
            else:
                conv.goal = SimpleNamespace(text="Demo goal")
            before = (main.session_id, main.model, len(fake.requests))
            with pytest.raises(AppError):
                await choose(core, cid, LUNA if active else "fusion")
            assert before == (main.session_id, main.model, len(fake.requests))
            assert (main._fusion is not None) is active
        finally:
            if conv is not None:
                conv.queue.clear()
                conv.goal = None
                conv.controller.saved.plan = {}
            fake.release("busy")
            await core.shutdown()
    asyncio.run(go())


@pytest.mark.parametrize("active", [False, True], ids=["enter", "leave"])
def test_mode_save_failure_restores_model_mode_and_retained_configuration(tmp_path, monkeypatch, active):
    async def go():
        core, fake, _, _, _ = await ready_core(tmp_path)
        store = core.chatgpt.sessions
        try:
            configure(core)
            cid = (await core.call("session.new", {"projectId": str(tmp_path / "proj"), "modelId": SOL, "fusion": active}))["id"]
            conv = core._by_cid[cid]
            main = conv.controller
            fake.script(say("Persist this turn before switching modes."))
            assert (await core.call("session.send", {"id": cid, "text": "Inspect the inventory."}))["ok"]
            await finish_turn(main)
            if conv.controller._fusion_title_task is not None:
                await conv.controller._fusion_title_task
            native = main.session_id
            archived = store.load(native)
            assert archived is not None and [turn.status for turn in archived.turns] == ["done"]
            assert conv.in_history and bool(archived.fusion) is active
            before = (main.model, main.reasoning, deepcopy(main.saved.fusion), deepcopy(main.saved.origin))
            runtimes = (main._fusion, main._paused_fusion)
            requests = (len(fake.requests), len(fake.fake.title_requests))
            writes = []
            disk_error = OSError("Simulated disk failure")

            def fail_save(snapshot):
                writes.append((snapshot.id, snapshot.model, bool(snapshot.fusion), snapshot.fusion.get("enabled", True)))
                raise disk_error

            # Keep the native model fixed so no source summary can fail before the mode checkpoint.
            with monkeypatch.context() as patch:
                patch.setattr(store, "save", fail_save)
                with pytest.raises(ChatGPTError) as error:
                    await choose(core, cid, SOL if active else "fusion")
            assert error.value.code == "checkpoint" and error.value.__cause__ is disk_error
            assert writes == [(native, before[0], True, not active)]
            assert before == (main.model, main.reasoning, main.saved.fusion, main.saved.origin)
            assert (main._fusion, main._paused_fusion) == runtimes and (main._fusion is not None) is active
            assert store.load(native) == archived
            assert (len(fake.requests), len(fake.fake.title_requests)) == requests
        finally:
            await core.shutdown()
    asyncio.run(go())


def test_empty_draft_can_enter_and_leave_fusion_without_saving(tmp_path, monkeypatch):
    async def go():
        core, fake, _, _, _ = await ready_core(tmp_path)
        store = core.chatgpt.sessions
        save = store.save
        writes = []

        def record_save(snapshot):
            writes.append(snapshot.id)
            save(snapshot)

        monkeypatch.setattr(store, "save", record_save)
        try:
            configure(core)
            cid = (await core.call("session.new", {"projectId": str(tmp_path / "proj"), "modelId": LUNA}))["id"]
            main = core._by_cid[cid].controller
            native = main.session_id
            for selected, model, active in (("fusion", SOL, True), (LUNA, LUNA, False)):
                assert (await choose(core, cid, selected))["ok"]
                assert main.model == model.removeprefix("chatgpt:") and (main._fusion is not None) is active
                assert store.load(native) is None and core.project_rows()[0]["sessionIds"] == []
            await core.call("session.close", {"id": cid})
        finally:
            await core.shutdown()
        assert not writes and store.load(native) is None
        assert not fake.requests and not fake.fake.title_requests
    asyncio.run(go())


def test_fusion_is_not_a_default_model_and_invalid_configuration_does_not_change_the_session(tmp_path):
    async def go():
        core, fake, _, _, _ = await ready_core(tmp_path)
        try:
            defaults = core.defaults()
            with pytest.raises(AppError):
                await core.call("session.choose", {"field": "model", "value": "fusion"})
            cid = (await core.call("session.new", {"projectId": str(tmp_path / "proj")}))["id"]
            main = core._by_cid[cid].controller
            with pytest.raises(AppError):
                await choose(core, cid, "fusion")
            configure(core)
            core.settings.fusion_reasoning[SOL] = "max"
            with pytest.raises(AppError):
                await choose(core, cid, "fusion")
            assert main._fusion is None and main.model == "gpt-6.1-sol" and not fake.requests
            assert core.defaults() == defaults
        finally:
            await core.shutdown()
    asyncio.run(go())
