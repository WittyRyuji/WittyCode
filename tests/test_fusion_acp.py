"""Guarded ACP uses the same tool policy and durable evidence as other Fusion models; no real model calls."""
from __future__ import annotations

import asyncio
import json

import pytest

from desktop.core.fusion.acp import guard_config
from desktop.core.fusion.acp_server import PREFIX, hook
from desktop.core.fusion.registry import Policy
from tests.app_support import ready_core
from tests.chatgpt_support import finish_turn
from tests.fake_responses import call, say
from tests.fakes import until


def test_guard_denies_native_tools_and_keeps_only_the_private_bridge(tmp_path):
    config = guard_config()
    assert {"read", "write", "edit", "exec", "grep", "glob"} <= set(config["permissions"]["deny"])
    assert config["subagents_enabled"] is False
    assert all(not value for value in config["read_config_from"].values())
    for name in ("exec", "read", "mcp__other__run", "run_subagent", "unknown"):
        result, code = hook({"hook_event_name": "PreToolUse", "tool_name": name})
        assert code == 2 and result["decision"] == "block"
    assert hook({"hook_event_name": "PreToolUse", "tool_name": PREFIX + "read_file"}) == ({}, 0)


def test_own_main_can_delegate_to_acp_and_forward_approval_and_restore_context(tmp_path):
    from tests.fusion_acp_support import make_guarded_engine

    async def go():
        harness = make_guarded_engine(tmp_path)
        core, model, _, _, _ = await ready_core(tmp_path, devin_engine=harness.engine)
        try:
            await until(lambda: any(item["source"] == "devin" for item in core.catalog.models), timeout=20)
            cid = (await core.call("session.new", {"projectId": str(tmp_path / "proj"), "autonomy": "ask"}))["id"]
            main = core._by_cid[cid].controller
            await main.update_settings(autonomy="ask")
            main.enable_fusion(Policy("chatgpt:gpt-6.1-sol", ("devin:swe-2-high",), "devin:swe-2-high", 0))
            task = {"scope": "src", "model": "devin:swe-2-high", "role": "worker", "task": "[fusion-write] Create the marker."}
            model.script(call("delegate_task", task), say("Reviewed."))
            await core.call("session.send", {"id": cid, "text": "Inspect"})
            await until(lambda: bool(main.agent_prompts()) or main.state.value != "running", timeout=30)
            assert main.agent_prompts(), [turn.status for turn in main.transcript.turns]
            expert = next(iter(main.agents.values()))
            assert not (tmp_path / "proj" / "marker.txt").exists()
            prompt = main.agent_prompts()[0]
            assert (await core.call("session.answerPermission", {"id": cid, "agentId": expert.session_id, "promptId": prompt.id, "value": "proceed_once"}))["ok"]
            await finish_turn(main, timeout=30)
            assert (tmp_path / "proj" / "marker.txt").read_text() == "guarded ACP write"
            assert expert.record.status == "done" and expert.controller.saved.origin["fusionAcp"]["session"]
            native = expert.controller.saved.origin["fusionAcp"]["session"]
            saved_id = main.session_id
            await main.close()
            reopened = await main.manager.open_saved(tmp_path / "proj", saved_id)
            model.script(call("delegate_task", {**task, "task": "Continue reviewing."}), say("Checked again."))
            assert reopened.send_prompt("Continue")
            await finish_turn(reopened, timeout=30)
            child = next(iter(reopened.agents.values())).controller
            assert child.saved.origin["fusionAcp"]["session"] == native
            assert any(item.get("type") == "function_call_output" for item in child.saved.items)
        finally:
            await core.shutdown()
    asyncio.run(go())


@pytest.mark.parametrize("worker", ["chatgpt:gpt-6-luna", "devin:swe-2-high"])
def test_acp_main_delegates_to_either_runner_and_keeps_one_conversation(tmp_path, worker):
    from tests.fusion_acp_support import make_guarded_engine

    async def go():
        harness = make_guarded_engine(tmp_path)
        core, model, _, _, _ = await ready_core(tmp_path, devin_engine=harness.engine)
        try:
            await until(lambda: any(item["source"] == "devin" for item in core.catalog.models), timeout=20)
            core.settings.fusion_main = "devin:swe-2-high"
            core.settings.fusion_workers = [worker]
            core.settings.fusion_light = worker
            cid = (await core.call("session.new", {"projectId": str(tmp_path / "proj"), "fusion": True}))["id"]
            main = core._by_cid[cid].controller
            model.script(say("Worker evidence."))
            assert main.send_prompt("[fusion-delegate-acp] Delegate" if worker.startswith("devin:") else "[fusion-delegate] Delegate")
            await finish_turn(main, timeout=30)
            assert len(core.order) == 1 and len(main.agents) == 1
            expert = next(iter(main.agents.values()))
            assert expert.record.status == "done", expert.controller.last_error
            assert expert.record.model == worker and main.model == "devin:swe-2-high"
            assert main.saved.turns[-1].status == "done"
        finally:
            await core.shutdown()
    asyncio.run(go())


@pytest.mark.parametrize("action", ["native", "read"])
def test_acp_cannot_bypass_protected_paths_or_use_native_tools(tmp_path, action):
    from tests.fusion_acp_support import make_guarded_engine

    async def go():
        harness = make_guarded_engine(tmp_path)
        core, _, _, _, _ = await ready_core(tmp_path, devin_engine=harness.engine)
        try:
            await until(lambda: any(item["source"] == "devin" for item in core.catalog.models), timeout=20)
            core.settings.fusion_main = "devin:swe-2-high"
            core.settings.fusion_workers = ["devin:swe-2-high"]
            core.settings.fusion_light = "devin:swe-2-high"
            cid = (await core.call("session.new", {"projectId": str(tmp_path / "proj"), "fusion": True}))["id"]
            main = core._by_cid[cid].controller
            if action == "read":
                # Even full access must refuse an app-owned protected location through the bridge.
                await main.update_settings(autonomy="full")
                await main._ensure_tools()
                main._context.protected = (*main._context.protected, tmp_path / "proj" / "evidence.txt")
                (tmp_path / "proj" / "evidence.txt").write_text("PRIVATE_TEST_EVIDENCE", encoding="utf-8")
            assert main.send_prompt(f"[fusion-{action}] Inspect")
            await finish_turn(main, timeout=30)
            if action == "native":
                assert main.saved.turns[-1].status == "failed" and "native tool" in main.saved.turns[-1].error
            else:
                outputs = [item["output"] for item in main.saved.items if item.get("type") == "function_call_output"]
                assert outputs and all("PRIVATE_TEST_EVIDENCE" not in item for item in outputs)
                assert "protected" in outputs[0]
        finally:
            await core.shutdown()
    asyncio.run(go())


def test_missing_guard_stops_before_model_request_and_does_not_persist_bridge_secret(tmp_path, monkeypatch):
    from tests.fusion_acp_support import make_guarded_engine
    monkeypatch.setattr("desktop.core.fusion.acp.GUARD_SECONDS", 1.0)

    async def go():
        harness = make_guarded_engine(tmp_path, FAKE_FUSION_NO_GUARD="1")
        core, _, _, _, _ = await ready_core(tmp_path, devin_engine=harness.engine)
        try:
            await until(lambda: any(item["source"] == "devin" for item in core.catalog.models), timeout=20)
            core.settings.fusion_main = core.settings.fusion_light = "devin:swe-2-high"
            core.settings.fusion_workers = ["devin:swe-2-high"]
            cid = (await core.call("session.new", {"projectId": str(tmp_path / "proj"), "fusion": True}))["id"]
            main = core._by_cid[cid].controller
            assert main.send_prompt("Do not run without protection")
            await finish_turn(main)
            lines = [line for path in (tmp_path / "guarded").rglob("received.jsonl") for line in path.read_text(encoding="utf-8").splitlines()]
            assert not any(json.loads(line).get("method") == "session/prompt" for line in lines)
            credential = main._acp.credential
            assert all(credential not in path.read_text(encoding="utf-8") for path in tmp_path.rglob("*.json"))
            assert all(credential not in line for line in lines)
            assert main._acp.client is None and main._acp.server is None
        finally:
            await core.shutdown()
    asyncio.run(go())


def test_acp_main_preserves_full_evidence_and_context_across_runner_changes(tmp_path):
    from tests.fusion_acp_support import make_guarded_engine

    async def go():
        harness = make_guarded_engine(tmp_path)
        core, model, _, _, _ = await ready_core(tmp_path, devin_engine=harness.engine)
        try:
            await until(lambda: any(item["source"] == "devin" for item in core.catalog.models), timeout=20)
            core.settings.fusion_main = core.settings.fusion_light = "devin:swe-2-high"
            core.settings.fusion_workers = ["chatgpt:gpt-6-luna"]
            cid = (await core.call("session.new", {"projectId": str(tmp_path / "proj"), "fusion": True}))["id"]
            main = core._by_cid[cid].controller
            evidence = "".join(f"row {i}: " + "evidence " * 12 + "\n" for i in range(1800))
            (tmp_path / "proj" / "evidence.txt").write_text(evidence, encoding="utf-8")
            assert main.send_prompt("[fusion-read] Inspect the full evidence")
            await finish_turn(main)
            reference = next(item["wc_output_ref"] for item in main.saved.items if item.get("wc_output_ref"))
            retained = main._outputs.read(reference)
            assert len(retained) > 40_000 and "row 0: " in retained and "row 200: " in retained
            assert "Call read_file again with offset=" in retained  # Explicit file pagination is retained too.
            native = main.saved.origin["fusionAcp"]["session"]
            assert (await core.call("fusion.changeMain", {"id": cid, "model": "chatgpt:gpt-6-luna"}))["ok"]
            model.script(say("The own runner reviewed the evidence."))
            assert main.send_prompt("Review the saved evidence")
            await finish_turn(main)
            assert main.saved.items and model.requests[-1]["model"] == "gpt-6-luna"
            assert (await core.call("fusion.changeMain", {"id": cid, "model": "devin:swe-2-high"}))["ok"]
            assert main.send_prompt("Continue from the review")
            await finish_turn(main)
            assert main.saved.origin["fusionAcp"]["session"] == native
            assert main._outputs.read(reference) == retained and main.saved.turns[-1].status == "done"
            saved = main.session_id
            await main.close()
            reopened = await main.manager.open_saved(tmp_path / "proj", saved)
            assert reopened._outputs.read(reference) == retained
            assert reopened.saved.origin["fusionAcp"]["session"] == native
        finally:
            await core.shutdown()
    asyncio.run(go())


def test_duplicate_mcp_delivery_does_not_reexecute_or_reuse_an_id_for_other_arguments(tmp_path):
    from tests.fusion_acp_support import make_guarded_engine

    async def go():
        harness = make_guarded_engine(tmp_path)
        core, _, _, _, _ = await ready_core(tmp_path, devin_engine=harness.engine)
        try:
            await until(lambda: any(item["source"] == "devin" for item in core.catalog.models), timeout=20)
            core.settings.fusion_main = core.settings.fusion_light = "devin:swe-2-high"
            core.settings.fusion_workers = ["devin:swe-2-high"]
            cid = (await core.call("session.new", {"projectId": str(tmp_path / "proj"), "fusion": True}))["id"]
            main = core._by_cid[cid].controller
            (tmp_path / "proj" / "evidence.txt").write_text("One execution", encoding="utf-8")
            for _ in range(2):
                assert main.send_prompt("[fusion-duplicate] Check delivery")
                await finish_turn(main)
                assert main.saved.turns[-1].status == "done"
            assert sum(item.get("type") == "function_call" for item in main.saved.items) == 1
            assert not (tmp_path / "proj" / "forbidden.txt").exists()
        finally:
            await core.shutdown()
    asyncio.run(go())


def test_acp_startup_keeps_the_turns_reasoning_snapshot(tmp_path, monkeypatch):
    from desktop.core.fusion.acp import AcpRun
    from tests.fusion_acp_support import make_guarded_engine

    async def go():
        entered, proceed = asyncio.Event(), asyncio.Event()
        original = AcpRun._open

        async def delayed(runner):
            entered.set()
            await proceed.wait()
            await original(runner)

        monkeypatch.setattr(AcpRun, "_open", delayed)
        harness = make_guarded_engine(tmp_path)
        core, _, _, _, _ = await ready_core(tmp_path, devin_engine=harness.engine)
        try:
            await until(lambda: any(item["source"] == "devin" for item in core.catalog.models), timeout=20)
            core.settings.fusion_main = core.settings.fusion_light = "devin:swe-2-high"
            core.settings.fusion_workers = ["devin:swe-2-high"]
            cid = (await core.call("session.new", {"projectId": str(tmp_path / "proj"), "fusion": True}))["id"]
            main = core._by_cid[cid].controller
            await main.update_settings(reasoning="medium")
            assert main.send_prompt("First request")
            await asyncio.wait_for(entered.wait(), 10)
            await main.update_settings(reasoning="max")
            proceed.set()
            await finish_turn(main)
            assert main.send_prompt("Next request")
            await finish_turn(main)
            requests = [json.loads(line) for path in (tmp_path / "guarded").rglob("received.jsonl") for line in path.read_text(encoding="utf-8").splitlines()]
            efforts = [item["params"]["value"] for item in requests if item.get("method") == "session/set_config_option" and item["params"].get("configId") == "thought_level"]
            assert efforts == ["medium", "max"]
        finally:
            proceed.set()
            await core.shutdown()
    asyncio.run(go())


def test_acp_stop_closes_its_bridge_without_automatically_replaying(tmp_path):
    from tests.fusion_acp_support import make_guarded_engine

    async def go():
        harness = make_guarded_engine(tmp_path)
        core, _, _, _, _ = await ready_core(tmp_path, devin_engine=harness.engine)
        try:
            await until(lambda: any(item["source"] == "devin" for item in core.catalog.models), timeout=20)
            core.settings.fusion_main = core.settings.fusion_light = "devin:swe-2-high"
            core.settings.fusion_workers = ["devin:swe-2-high"]
            cid = (await core.call("session.new", {"projectId": str(tmp_path / "proj"), "fusion": True}))["id"]
            main = core._by_cid[cid].controller
            assert main.send_prompt("[fusion-slow] Wait")
            await until(lambda: main._acp is not None and main._acp.active, timeout=30)
            await core.call("session.stop", {"id": cid})
            await finish_turn(main)
            assert main._acp.client is None and main._acp.server is None
            assert main.transcript.turns[-1].status.value == "interrupted"
            requests = [json.loads(line) for path in (tmp_path / "guarded").rglob("received.jsonl") for line in path.read_text(encoding="utf-8").splitlines()]
            assert sum(item.get("method") == "session/prompt" for item in requests) == 1
        finally:
            await core.shutdown()
    asyncio.run(go())


def test_acp_can_be_the_main_and_uses_versioned_plan_approval(tmp_path):
    from tests.fusion_acp_support import make_guarded_engine

    async def go():
        harness = make_guarded_engine(tmp_path)
        core, model, _, _, _ = await ready_core(tmp_path, devin_engine=harness.engine)
        try:
            await until(lambda: any(item["source"] == "devin" for item in core.catalog.models), timeout=20)
            for key, value in {"fusionMain": "devin:swe-2-high", "fusionWorkers": ["chatgpt:gpt-6-luna"], "fusionLight": "devin:swe-2-high"}.items():
                await core.call("settings.set", {"key": key, "value": value})
            cid = (await core.call("session.new", {"projectId": str(tmp_path / "proj"), "fusion": True}))["id"]
            main = core._by_cid[cid].controller
            assert main.model == "devin:swe-2-high"
            assert core._session_meta(core._by_cid[cid])["settings"]["model"] == "devin:swe-2-high"
            await main.update_settings(mode="spec")
            assert main.send_prompt("[fusion-plan] Plan the change")
            await finish_turn(main, timeout=30)
            assert main.saved.plan.get("status") == "pending"
            assert not main.agents and not model.requests
            approved = main.saved.plan["id"]
            await main.update_settings(mode="auto")
            assert not main.send_prompt("Continue")
            assert main.saved.plan["id"] == approved
            await main.update_settings(mode="spec")
            assert (await core.call("session.approvePlan", {"id": cid, "planId": approved}))["ok"]
            await finish_turn(main, timeout=30)
            assert main.saved.plan["status"] == "approved" and main.saved.plan["execution"] == "started"
            assert not (await core.call("session.approvePlan", {"id": cid, "planId": approved}))["ok"]
        finally:
            await core.shutdown()
    asyncio.run(go())
