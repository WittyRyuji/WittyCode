"""Guarded ACP uses the same tool policy and durable evidence as other Fusion models; no real model calls."""
from __future__ import annotations

import asyncio
import json
import shlex
from types import SimpleNamespace

import pytest

from desktop.core.devin.policy import CANCELLED
from desktop.core.fusion.acp import AcpRun, guard_config
from desktop.core.fusion.acp_server import MCP_CALL, MCP_LIST, MCP_WRAPPERS, PREFIX, SERVER, hook
from desktop.core.fusion.registry import Policy
from tests.app_support import ready_core
from tests.chatgpt_support import finish_turn
from tests.fake_responses import call, say
from tests.fakes import until


def test_guard_denies_native_tools_and_keeps_only_the_private_bridge():
    config = guard_config()
    assert {"read", "write", "edit", "exec", "get_output", "grep", "glob", "mcp_list_servers", "mcp_read_resource"} <= set(config["permissions"]["deny"])
    assert config["permissions"]["allow"] == [PREFIX + "*"] and {MCP_LIST, MCP_CALL} <= set(config["permissions"]["ask"])
    assert {MCP_LIST, MCP_CALL} <= set(guard_config(tool_free=True)["permissions"]["deny"])
    assert config["subagents_enabled"] is False
    assert all(not value for value in config["read_config_from"].values())
    for name in ("exec", "read", "mcp__other__run", "run_subagent", "unknown"):
        result, code = hook({"hook_event_name": "PreToolUse", "tool_name": name})
        assert code == 2 and result["decision"] == "block"


@pytest.mark.parametrize(("name", "arguments", "allowed"), [
    (MCP_LIST, {"server_name": SERVER}, True),
    (MCP_LIST, None, False),
    (MCP_LIST, {}, False),
    (MCP_LIST, {"server_name": "other"}, False),
    (MCP_CALL, {"server_name": SERVER, "tool_name": "read_file", "arguments": {}}, True),
    (MCP_CALL, {"server_name": SERVER, "tool_name": "read_file", "arguments": []}, False),
    (MCP_CALL, {"server_name": SERVER, "tool_name": "exec", "arguments": {}}, False),
    (MCP_CALL, {"server_name": "other", "tool_name": "read_file", "arguments": {}}, False),
    (PREFIX + "read_file", None, True),
    (PREFIX + "unregistered", None, False),
    ("exec", {"server_name": SERVER, "tool_name": "read_file", "arguments": {}}, False),
])
def test_acp_wrappers_require_an_exact_private_target_at_every_check(monkeypatch, name, arguments, allowed):
    run = AcpRun(SimpleNamespace())
    run.session, run.active = "private", True
    run.tools = SimpleNamespace(known=lambda tool: tool == "read_file")
    call = {"toolCallId": name + ":0#1", "rawInput": arguments}
    params = {"sessionId": run.session, "toolCall": call, "options": [{"kind": "allow_always", "optionId": "always"}]}
    assert asyncio.run(run._request("session/request_permission", params, 0)) == CANCELLED
    params["options"].append({"kind": "allow_once", "optionId": "once"})
    expected = {"outcome": {"outcome": "selected", "optionId": "once"}} if allowed else CANCELLED
    assert asyncio.run(run._request("session/request_permission", params, 1)) == expected
    monkeypatch.setattr("desktop.core.fusion.acp_server.request", lambda method, payload: asyncio.run(run.dispatch(method, payload)))
    payload = {"hook_event_name": "PreToolUse", "tool_name": name, "tool_input": arguments}
    assert hook(payload)[1] == (0 if allowed else 2)
    run._notify("session/update", {"sessionId": run.session, "update": {"sessionUpdate": "tool_call", **call}})
    deferred = not allowed and name in MCP_WRAPPERS and not isinstance(arguments, dict)
    assert bool(run.violation) is (not allowed and not deferred)
    if deferred:  # A recognized wrapper may still complete its input on a later update.
        assert run._pending[call["toolCallId"]] == name
        run._notify("session/update", {"sessionId": run.session, "update": {
            "sessionUpdate": "tool_call_update", "toolCallId": call["toolCallId"], "rawInput": {"server_name": "other"}}})
        assert "mcp_list_tools" in run.violation and "other" not in run.violation
    run.active = False
    assert hook(payload)[1] == 2
    run.active, run.tool_free = True, True
    assert hook(payload)[1] == 2


@pytest.mark.parametrize("python", [r"C:\Tools\Python\python.exe", r"C:\Tools With Spaces\python.exe", "C:/Tools' Profile/python.exe"])
def test_guard_command_uses_posix_quoting_even_for_windows_paths(monkeypatch, python):
    monkeypatch.setattr("desktop.core.fusion.acp.console_python", lambda: python)
    config = guard_config()
    for hooks in config["hooks"].values():
        assert shlex.split(hooks[0]["hooks"][0]["command"]) == [python.replace("\\", "/"), "-m", "desktop.core.fusion.acp_server", "--hook"]
    assert config["read_config_from"]["cloud"] is False


def test_own_main_can_delegate_to_acp_and_forward_approval_and_restore_context(tmp_path):
    from tests.fusion_acp_support import make_guarded_engine

    async def go():
        harness = make_guarded_engine(tmp_path, FAKE_FUSION_BRIDGE_NOTICE="id-only", FAKE_FUSION_MCP_WRAPPERS="1")
        core, model, _, _, _ = await ready_core(tmp_path, devin_engine=harness.engine)
        try:
            await until(lambda: any(item["source"] == "devin" for item in core.catalog.models), timeout=20)
            cid = (await core.call("session.new", {"projectId": str(tmp_path / "proj"), "autonomy": "ask"}))["id"]
            main = core._by_cid[cid].controller
            await main.update_settings(autonomy="ask")
            main.enable_fusion(Policy("chatgpt:gpt-6.1-sol", ("devin:swe-2-high",), "devin:swe-2-high", 0,
                                      reasoning={"devin:swe-2-high": "medium"}))
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
            assert expert.agent_effort == "medium"
            requests = [json.loads(line) for path in (tmp_path / "guarded").rglob("received.jsonl") for line in path.read_text(encoding="utf-8").splitlines()]
            assert any(item.get("method") == "session/set_config_option" and item["params"].get("configId") == "thought_level"
                       and item["params"].get("value") == "medium" for item in requests)
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
            levels = {"main:devin:swe-2-high": "medium", f"worker:{worker}": "low" if worker.startswith("chatgpt:") else "max"}
            await core.call("settings.set", {"key": "fusionReasoning", "value": levels})
            cid = (await core.call("session.new", {"projectId": str(tmp_path / "proj"), "fusion": True}))["id"]
            main = core._by_cid[cid].controller
            model.script(say("Worker evidence."))
            assert main.send_prompt("[fusion-delegate-acp] Delegate" if worker.startswith("devin:") else "[fusion-delegate] Delegate")
            await finish_turn(main, timeout=30)
            assert len(core.order) == 1 and len(main.agents) == 1
            expert = next(iter(main.agents.values()))
            assert expert.record.status == "done", expert.controller.last_error
            assert expert.record.model == worker and main.model == "devin:swe-2-high"
            assert main.reasoning == "medium" and expert.agent_effort == levels[f"worker:{worker}"]
            assert main.saved.turns[-1].status == "done"
        finally:
            await core.shutdown()
    asyncio.run(go())


def test_truncated_acp_tool_list_can_use_the_private_index(tmp_path):
    from tests.fusion_acp_support import make_guarded_engine

    async def go():
        harness = make_guarded_engine(tmp_path, FAKE_FUSION_MCP_WRAPPERS="1")
        core, _, _, _, _ = await ready_core(tmp_path, devin_engine=harness.engine)
        try:
            await until(lambda: any(item["source"] == "devin" for item in core.catalog.models), timeout=20)
            core.settings.fusion_main = core.settings.fusion_light = "devin:swe-2-high"
            core.settings.fusion_workers = ["devin:swe-2-high"]
            project = tmp_path / "proj"
            (project / "evidence.txt").write_text("INDEX_RECOVERY_EVIDENCE", encoding="utf-8")
            cid = (await core.call("session.new", {"projectId": str(project), "fusion": True}))["id"]
            main = core._by_cid[cid].controller
            assert main.send_prompt("[fusion-truncated-tools] Read the project note.")
            await finish_turn(main, timeout=30)
            turn = main.saved.turns[-1]
            assert turn.status == "done", turn.error
            outputs = [item["output"] for item in main.saved.items if item.get("type") == "function_call_output"]
            assert any("INDEX_RECOVERY_EVIDENCE" in result for result in outputs)
        finally:
            await core.shutdown()
    asyncio.run(go())


def test_pending_acp_wrapper_call_is_approved_once_its_input_arrives(tmp_path):
    from tests.fusion_acp_support import make_guarded_engine

    async def go():
        harness = make_guarded_engine(tmp_path, FAKE_FUSION_MCP_WRAPPERS="1", FAKE_FUSION_STAGED="1")
        core, _, _, _, _ = await ready_core(tmp_path, devin_engine=harness.engine)
        try:
            await until(lambda: any(item["source"] == "devin" for item in core.catalog.models), timeout=20)
            core.settings.fusion_main = core.settings.fusion_light = "devin:swe-2-high"
            core.settings.fusion_workers = ["devin:swe-2-high"]
            project = tmp_path / "proj"
            (project / "evidence.txt").write_text("STAGED_EVIDENCE", encoding="utf-8")
            cid = (await core.call("session.new", {"projectId": str(project), "fusion": True}))["id"]
            main = core._by_cid[cid].controller
            assert main.send_prompt("[fusion-read] Read the project note.")
            await finish_turn(main, timeout=30)
            turn = main.saved.turns[-1]
            assert turn.status == "done", turn.error
            outputs = [item["output"] for item in main.saved.items if item.get("type") == "function_call_output"]
            assert any("STAGED_EVIDENCE" in result for result in outputs)
        finally:
            await core.shutdown()
    asyncio.run(go())


def test_acp_turn_fails_when_a_wrapper_call_is_never_completed(tmp_path):
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
            assert main.send_prompt("[fusion-unresolved] Stall")
            await finish_turn(main, timeout=30)
            turn = main.saved.turns[-1]
            assert turn.status == "failed" and "unverified tool calls" in turn.error
        finally:
            await core.shutdown()
    asyncio.run(go())


@pytest.mark.parametrize("action", ["native", "unknown-bridge", "read"])
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
            if action in ("native", "unknown-bridge"):
                assert main.saved.turns[-1].status == "failed" and "guarded bridge" in main.saved.turns[-1].error
                assert not (tmp_path / "proj" / "forbidden.txt").exists()
            else:
                outputs = [item["output"] for item in main.saved.items if item.get("type") == "function_call_output"]
                assert outputs and all("PRIVATE_TEST_EVIDENCE" not in item for item in outputs)
                assert "protected" in outputs[0]
        finally:
            await core.shutdown()
    asyncio.run(go())


@pytest.mark.parametrize("missing", ["GUARD", "BRIDGE"])
def test_missing_guard_or_bridge_stops_before_model_request_and_does_not_persist_bridge_secret(tmp_path, monkeypatch, missing):
    from tests.fusion_acp_support import make_guarded_engine
    monkeypatch.setattr("desktop.core.fusion.acp.GUARD_SECONDS", 1.0)

    async def go():
        harness = make_guarded_engine(tmp_path, **{"FAKE_FUSION_NO_" + missing: "1"})
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
            assert main.saved.origin["fusionAcp"]["session"] != native
            assert any(item["session"] == native for item in main.saved.origin["fusionAcpHistory"])
            refreshed_native = main.saved.origin["fusionAcp"]["session"]
            assert main._outputs.read(reference) == retained and main.saved.turns[-1].status == "done"
            saved = main.session_id
            await main.close()
            reopened = await main.manager.open_saved(tmp_path / "proj", saved)
            assert reopened._outputs.read(reference) == retained
            assert reopened.saved.origin["fusionAcp"]["session"] == refreshed_native
            assert any(item["session"] == native for item in reopened.saved.origin["fusionAcpHistory"])
        finally:
            await core.shutdown()
    asyncio.run(go())


def test_fusion_preset_switch_recovers_after_a_blocked_native_turn(tmp_path):
    from copy import deepcopy

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
            assert main.send_prompt("[fusion-native] Check the workspace")
            await finish_turn(main)
            native = main.saved.origin["fusionAcp"]["session"]
            original = deepcopy(main.saved.items)
            assert main.saved.turns[-1].status == "failed" and original[-1]["type"] == "reasoning"
            await core.call("settings.set", {"key": "fusionMain", "value": "chatgpt:gpt-6-luna"})
            assert main.model == "devin:swe-2-high"
            model.script(say("Continued on the new main model."))
            assert (await core.call("session.send", {"id": cid, "text": "Continue without replaying"}))["ok"]
            await finish_turn(main)
            assert len(main.transcript.turns) == 2 and main.saved.items[:len(original)] == original
            assert main.saved.compaction["upto"] == len(original)
            assert main.saved.origin["fusionAcpHistory"][-1]["session"] == native
            assert len(model.requests) == 1 and model.requests[0]["model"] == "gpt-6-luna"
            assert "ACP summary:" in str(model.requests[0]["input"])
            assert "Private reasoning" not in str(model.requests[0]["input"])
            requests = [[json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
                        for path in (tmp_path / "guarded").glob("summary-*/received.jsonl")]
            summary = [item for group in requests for item in group if item.get("method") == "session/prompt"]
            assert len(summary) == 1 and "Check the workspace" in str(summary[0]["params"]["prompt"])
            assert "Private reasoning" not in str(summary[0]["params"]["prompt"])
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


@pytest.mark.parametrize("model,vision", [("swe-2-high", True), ("glm-5-2", False)])
def test_picture_tool_reply_reaches_the_fake_cli_and_replays_unchanged(tmp_path, model, vision):
    import base64
    from tests.fusion_acp_support import make_guarded_engine
    from tests.tool_image_support import png

    async def go():
        harness = make_guarded_engine(tmp_path)
        core, _, _, _, _ = await ready_core(tmp_path, devin_engine=harness.engine)
        try:
            await until(lambda: any(item["source"] == "devin" for item in core.catalog.models), timeout=20)
            main_model = "devin:" + model
            core.settings.fusion_main = core.settings.fusion_light = main_model
            core.settings.fusion_workers = [main_model]
            cid = (await core.call("session.new", {"projectId": str(tmp_path / "proj"), "fusion": True}))["id"]
            main = core._by_cid[cid].controller
            assert main.model == main_model
            data = png()
            (tmp_path / "proj" / "picture.png").write_bytes(data)
            assert main.send_prompt("[fusion-picture] Inspect the picture")
            await finish_turn(main)
            assert main.saved.turns[-1].status == "done"
            [reply_file] = (tmp_path / "guarded").rglob("picture-replies.json")
            first, replay = json.loads(reply_file.read_text(encoding="utf-8"))
            assert first == replay
            [item] = [i for i in main.saved.items if i.get("type") == "function_call_output"]
            text, *images = first["content"]
            assert text == {"type": "text", "text": item["output"]}
            if vision:
                assert "picture.png (image/png, 2 x 3 px" in text["text"]
                assert images == [{"type": "image", "data": base64.b64encode(data).decode("ascii"), "mimeType": "image/png"}]
                assert item["wc_images"] == [{"data": images[0]["data"], "media_type": "image/png"}]
            else:
                assert "current model does not support images" in text["text"]
                assert images == [] and "wc_images" not in item
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
