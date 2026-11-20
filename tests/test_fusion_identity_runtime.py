from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager

import pytest

from desktop.core.chatgpt.controller import ChatGPTController
from desktop.core.fusion.acp import AcpRun
from desktop.core.fusion.drive.retention import Retention
from desktop.core.fusion.gates import GATES
from desktop.core.fusion.identity import DECOMPOSITION
from desktop.core.fusion.protocol import BRIEF_PARAMETERS, parse_brief
from desktop.core.fusion.registry import Policy
from desktop.core.fusion.tools import parse
from desktop.core.settings import AppSettings, default_settings_path
from desktop.core.transcript import ErrorBlock, NoticeBlock
from tests.app_support import ready_core
from tests.chatgpt_support import finish_turn, make_engine
from tests.fake_responses import FakeResponses, call, say
from tests.fakes import until
from tests.fusion_acp_support import make_guarded_engine
from tests.test_harness_toolset import StubClient, tool

pytestmark = pytest.mark.fusion_gates

MAIN = "chatgpt:gpt-6.1-sol"
WORKER = "chatgpt:gpt-6.1-sol"
LIGHT = "chatgpt:gpt-6-luna"
DEVIN = "devin:swe-2-high"
LEGACY = {"workstream": "Inventory", "paths": ["inventory.txt"], "model": WORKER, "role": "worker", "task": "Check stock."}


def _brief(identity="Coder", **overrides):
    return {"identity": identity, "workstream": "Inventory", "goal": "Verify stock totals.", "constraints": "Stay in the project.",
            "acceptance": "Counts match the inventory.", "paths": ["inventory.txt"] if identity == "Coder" else [],
            "budget": {"steps": 8}, **overrides}


def _gates(*active):
    return {name: name in active for name in GATES}


def _identity_folder():
    folder = default_settings_path().parent / "configs" / "agents"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


@asynccontextmanager
async def _fusion(tmp_path, *, active=("brief",), max_seconds=30):
    project = tmp_path / "project"
    project.mkdir()
    _identity_folder()
    with FakeResponses() as fake:
        engine = make_engine(tmp_path, fake, settings=AppSettings(fusion_gates=_gates(*active)))
        main = await engine.open_new(project, model="gpt-6.1-sol", autonomy="full")
        main.enable_fusion(Policy(MAIN, (WORKER,), LIGHT, 0, max_seconds=max_seconds, preference=25,
                                  reasoning={f"worker:{WORKER}": "medium"}))
        try:
            yield main, fake
        finally:
            await engine.close_all()


async def _turn(main, fake, *responses):
    fake.script(*responses)
    assert main.send_prompt("Check the inventory.")
    await finish_turn(main)


def _output(controller, call_id):
    return next(item["output"] for item in controller.saved.items
                if item.get("type") == "function_call_output" and item.get("call_id") == call_id)


def _names(tools):
    return {item["name"] for item in tools}


def _manual_steps(main, low=1, high=1):
    """Only a both-manual band can give an expert a tiny real step limit; adaptive sizing floors at 16."""
    settings = main.manager.settings
    settings.fusion_steps_min, settings.fusion_steps_min_adaptive = low, False
    settings.fusion_steps_max, settings.fusion_steps_max_adaptive = high, False


def test_structured_models_reuse_and_batch_ownership(tmp_path):
    async def go():
        async with _fusion(tmp_path) as (main, fake):
            coder, runner = _brief(), _brief("Runner")
            await _turn(main, fake, call("delegate_task", coder, call_id="coder-1"), say("Coder checked stock."), say("Checked."))
            coder_id = next(iter(main.agents))
            await _turn(main, fake, call("delegate_task", coder, call_id="coder-2"), say("Coder checked again."), say("Checked."))
            assert list(main.agents) == [coder_id] and main.agents[coder_id].name == "Coder-Inventory"
            assert main.agents[coder_id].record.model == WORKER and main.agents[coder_id].agent_effort == "medium"
            first_coder_prompt = fake.requests[1]["input"]
            assert "Implement the brief within its declared write paths" in str(first_coder_prompt)
            assert "Verify stock totals." in str(first_coder_prompt)
            await _turn(main, fake, call("delegate_tasks", {"tasks": [runner, runner]}, call_id="runner-1"),
                        say("First run checked."), say("Checked."))
            assert "already running" in _output(main, "runner-1")
            await _turn(main, fake, call("delegate_task", runner, call_id="runner-2"), say("Second run checked."), say("Checked."))
            runners = [agent for agent in main.agents.values() if agent.name == "Runner-Inventory"]
            assert len(runners) == 2 and len({agent.session_id for agent in runners}) == 2
            assert all(agent.record.model == LIGHT and agent.record.status == "done" for agent in runners)
            assert [r["model"] for r in fake.requests].count("gpt-6-luna") == 2
            assert "First run checked." not in str(fake.requests[-2]["input"])
            manifest = {item["name"]: item["parameters"] for item in fake.requests[0]["tools"]}
            assert manifest["delegate_task"] == BRIEF_PARAMETERS
            tasks = manifest["delegate_tasks"]["properties"]["tasks"]
            assert tasks["items"] == BRIEF_PARAMETERS and tasks["maxItems"] == 4
            instructions = main._fusion.instructions()
            assert instructions.startswith(DECOMPOSITION) and "cost x1.25, efficiency x0.75" in instructions
            assert WORKER not in instructions and LIGHT not in instructions
    asyncio.run(go())


def test_bad_and_legacy_calls_return_parser_refusals(tmp_path):
    async def go():
        async with _fusion(tmp_path) as (main, fake):
            bad = _brief(budget={"steps": 0})
            await _turn(main, fake, call("delegate_task", bad, call_id="bad"),
                        call("delegate_task", LEGACY, call_id="old"), say("Need a brief."))
            assert _output(main, "bad") == parse_brief(bad)[1]
            refusal = _output(main, "old")
            assert refusal.startswith(parse_brief(LEGACY)[1])
            assert all(name in refusal for name in BRIEF_PARAMETERS["required"])
            assert not main.agents
            assert parse(LEGACY)[0] == ("Inventory", WORKER, "worker", "Check stock.", ("inventory.txt",))
    asyncio.run(go())


@pytest.mark.parametrize("identity", ["Runner", "Coder"])
def test_expert_tool_filter_and_read_only_commands(tmp_path, identity):
    async def go():
        async with _fusion(tmp_path) as (main, fake):
            read_only = identity == "Coder"
            responses = [call("delegate_task", _brief(identity, paths=[], read_only=read_only), call_id="delegate"),
                         call("create_file", {"file_path": "inventory.txt", "content": "not written"}, call_id="edit")]
            if read_only:
                responses.append(call("shell", {"command": "New-Item marker.txt"}, call_id="mutate"))
                responses.append(call("shell", {"command": "Get-Location"}, call_id="inspect"))
            await _turn(main, fake, *responses, say("Checked."), say("Reviewed."))
            child = next(iter(main.agents.values())).controller
            assert "create_file" not in _names(fake.requests[1]["tools"])
            assert "shell" in _names(fake.requests[1]["tools"])
            assert "Refused" in _output(child, "edit")
            assert not list(main.project.iterdir())
            if read_only:
                assert "Do not change anything." in str(fake.requests[1]["input"])
                assert "read-only" in _output(child, "edit") and "read-only" in _output(child, "mutate")
                assert "Refused" not in _output(child, "inspect")
    asyncio.run(go())


def test_read_only_flag_does_not_stop_a_runner_from_running_checks(tmp_path):
    # Found with a real model: the main agent marked Runners read-only (they have no write paths) and they came back blocked.
    async def go():
        async with _fusion(tmp_path) as (main, fake):
            await _turn(main, fake, call("delegate_task", _brief("Runner", paths=[], read_only=True), call_id="delegate"),
                        call("shell", {"command": "New-Item marker.txt"}, call_id="check"), say("Checked."), say("Reviewed."))
            child = next(iter(main.agents.values())).controller
            assert "Refused" not in _output(child, "check") and (main.project / "marker.txt").is_file()
            assert "create_file" not in _names(fake.requests[1]["tools"])
            assert "Do not change anything." not in str(fake.requests[1]["input"])
    asyncio.run(go())


@pytest.mark.parametrize("identity", ["Coder", "Runner"])
def test_expert_memory_is_read_only_and_external_mcp_follows_identity(tmp_path, monkeypatch, identity):
    server = "wittycode-memory"
    memory = StubClient(server, [tool(server, f"memory_{name}", readOnlyHint=name in ("search", "read"))
                                 for name in ("read", "search", "write", "update", "forget")])
    other = StubClient("inventory", [tool("inventory", "get", readOnlyHint=True), tool("inventory", "set")])
    original = ChatGPTController._ensure_tools

    async def fake_mcp(self):
        tools = await original(self)
        if self._fusion_identity is not None:
            tools.clients = [memory, other]
            tools.refresh()
        return tools

    monkeypatch.setattr(ChatGPTController, "_ensure_tools", fake_mcp)

    async def go():
        async with _fusion(tmp_path) as (main, fake):
            await _turn(main, fake, call("delegate_task", _brief(identity), call_id="delegate"),
                        call("mcp__wittycode-memory__memory_write", {}, call_id="memory"), say("Checked."), say("Reviewed."))
            child = next(iter(main.agents.values())).controller
            names = _names(fake.requests[1]["tools"])
            assert {name for name in names if name.startswith("mcp__wittycode-memory__")} == {
                "mcp__wittycode-memory__memory_search", "mcp__wittycode-memory__memory_read"}
            assert "mcp__inventory__get" in names and ("mcp__inventory__set" in names) == (identity == "Coder")
            assert "Refused" in _output(child, "memory") and not memory.calls
    asyncio.run(go())


def test_step_budget_interrupts_the_expert(tmp_path):
    async def go():
        async with _fusion(tmp_path) as (main, fake):
            _manual_steps(main)
            await _turn(main, fake, call("delegate_task", _brief(budget={"steps": 1}), call_id="delegate"),
                        call("list_dir", {"directory_path": "."}, call_id="read"), say("Reviewed the interruption."))
            expert = next(iter(main.agents.values()))
            assert expert.record.status == "interrupted"
            assert expert.controller.last_turn_end.steps == 1 and len(fake.requests) == 3
            assert "exceeded its step budget" in _output(main, "delegate")
            assert "estimate or the split was wrong" in _output(main, "delegate")
    asyncio.run(go())


@pytest.mark.parametrize(("policy_limit", "brief_limit"), [(1, 15), (15, 1)])
def test_smaller_time_budget_interrupts_the_expert(tmp_path, policy_limit, brief_limit):
    async def go():
        async with _fusion(tmp_path, max_seconds=policy_limit) as (main, fake):
            try:
                fake.script(call("delegate_task", _brief(budget={"seconds": brief_limit}), call_id="delegate"),
                            [("hold", "expert"), *say("Late report.")], say("Reviewed the interruption."))
                assert main.send_prompt("Check the inventory.")
                await finish_turn(main, timeout=10)
                assert next(iter(main.agents.values())).record.status == "interrupted"
                assert "exceeded its time budget" in _output(main, "delegate")
                assert "estimate or the split was wrong" in _output(main, "delegate")
            finally:
                fake.release("expert")
    asyncio.run(go())


def test_old_saved_expert_reopens_and_reuses_when_gate_off(tmp_path):
    async def go():
        async with _fusion(tmp_path, active=()) as (main, fake):
            await _turn(main, fake, call("delegate_task", LEGACY, call_id="old"), say("First report."), say("Reviewed."))
            key = next(iter(main.agents))
            assert "Authorised worker models:" in main._fusion.instructions() and WORKER in main._fusion.instructions()
            await main.close()
            engine = main.manager
            saved = engine.sessions.load(main.session_id)
            saved.fusion["agents"][0].pop("identity")
            engine.sessions.save(saved)
            reopened = await engine.open_saved(main.project, main.session_id)
            try:
                assert not reopened.agents[key].record.identity
                await _turn(reopened, fake, call("delegate_task", LEGACY, call_id="reused"), say("Reused context."), say("Reviewed."))
                assert list(reopened.agents) == [key] and reopened.agents[key].record.status == "done"
                assert "First report." in str(fake.requests[-2]["input"])
            finally:
                await reopened.close()
    asyncio.run(go())


def test_user_identity_override_is_loaded_once_and_bad_file_noticed(tmp_path):
    folder = _identity_folder()
    (folder / "Runner.md").write_text("---\nname: Runner\ntools: read\nreasoning: low\n---\nInspect without running commands.", encoding="utf-8")
    (folder / "Coder.md").write_text("---\nname: Coder\ntools: invalid\n---\nInvalid customization.", encoding="utf-8")

    async def go():
        async with _fusion(tmp_path) as (main, fake):
            runtime = main._fusion
            assert runtime.identities.get("Coder").tools == {"read", "edit", "exec"}
            (folder / "Runner.md").unlink()
            await _turn(main, fake, call("delegate_task", _brief("Runner"), call_id="delegate"), say("Inspected."), say("Reviewed."))
            assert runtime.identities.get("Runner").tools == {"read"}
            assert "shell" not in _names(fake.requests[1]["tools"])
            assert "Inspect without running commands." in str(fake.requests[1]["input"])
            assert next(iter(main.agents.values())).agent_effort == "low"
            notices = [block.text for turn in main.transcript.turns for block in turn.blocks if isinstance(block, NoticeBlock)]
            assert sum("Coder.md" in text for text in notices) == 1
    asyncio.run(go())


def test_identity_file_sets_its_own_compaction_point(tmp_path):
    from types import SimpleNamespace

    from desktop.core.chatgpt.controller import _Watch

    (_identity_folder() / "Coder.md").write_text("---\nname: Coder\ncompact_at: 40\n---\nImplement the brief.", encoding="utf-8")

    async def go():
        async with _fusion(tmp_path) as (main, fake):
            await _turn(main, fake, call("delegate_task", _brief(), call_id="delegate"), say("Done."), say("Reviewed."))
            child = next(iter(main.agents.values())).controller
            point = lambda controller: _Watch._compact_at(SimpleNamespace(ctl=controller), 100_000)  # noqa: E731
            share = main.manager.settings.chatgpt_compact_at
            assert share != 40 and point(main) == 100_000 * share // 100
            assert point(child) == 40_000
    asyncio.run(go())


def test_drive_retains_rendered_brief_with_the_expert_label(tmp_path):
    async def go():
        async with _fusion(tmp_path, active=("brief", "drive")) as (main, fake):
            args = _brief()
            await _turn(main, fake, call("delegate_task", args, call_id="delegate"), say("Inspected."), say("Reviewed."))
            drive = main._fusion.drive
            records = await asyncio.to_thread(drive.candidates, shared=False, retention=Retention())
            brief = next(item.record for item in records if item.record.kind == "brief")
            assert brief.summary.startswith("Coder-Inventory:") and not brief.open
            # The record keeps the rendered brief with the resolved budget and its origin, not the raw request.
            text = await asyncio.to_thread(drive.read, brief.pointers[0])
            assert text.startswith("Goal:\nVerify stock totals.") and text.endswith("Budget: 20 steps (adaptive 20-40; requested 8)")
    asyncio.run(go())


@pytest.mark.parametrize("devin_main", [True, False])
def test_private_acp_uses_structured_schema_and_runner_permissions(tmp_path, monkeypatch, devin_main):
    manifests = {}
    original = AcpRun.dispatch

    async def observe(self, method, params):
        result = await original(self, method, params)
        if method == "tools/list":
            manifests[self.host.session_id] = {item["name"]: item["inputSchema"] for item in result["tools"]}
        return result

    monkeypatch.setattr(AcpRun, "dispatch", observe)

    async def go():
        _identity_folder()
        harness = make_guarded_engine(tmp_path)
        core, model, _, _, _ = await ready_core(tmp_path, devin_engine=harness.engine)
        try:
            await until(lambda: any(item["source"] == "devin" for item in core.catalog.models), timeout=20)
            core.settings.fusion_gates = _gates("brief")
            core.settings.fusion_main = DEVIN if devin_main else MAIN
            core.settings.fusion_workers = [WORKER]
            core.settings.fusion_light = LIGHT if devin_main else DEVIN
            project = tmp_path / "proj"
            cid = (await core.call("session.new", {"projectId": str(project), "fusion": True, "autonomy": "full"}))["id"]
            main = core._by_cid[cid].controller
            if devin_main:
                model.script(say("Checked inventory."))
                assert main.send_prompt("[fusion-matrix] " + json.dumps({"tasks": [_brief()]}))
                await finish_turn(main)
                manifest = manifests[main.session_id]
                assert manifest["delegate_task"] == BRIEF_PARAMETERS
                assert manifest["delegate_tasks"]["properties"]["tasks"]["items"] == BRIEF_PARAMETERS
                assert next(iter(main.agents.values())).record.status == "done"
            else:
                await _turn(main, model, call("delegate_task", _brief("Runner", goal="[fusion-write] Try editing."), call_id="delegate"),
                            say("The edit was not allowed."))
                runner = next(iter(main.agents.values()))
                assert runner.name == "Runner-Inventory" and runner.record.model == DEVIN
                assert "read_file" in manifests[runner.session_id] and "create_file" not in manifests[runner.session_id]
                assert not (project / "marker.txt").exists()
                assert runner.record.status == "failed"
                assert any(isinstance(block, ErrorBlock) and "guarded bridge" in block.message
                           for block in runner.transcript.turns[-1].blocks)
        finally:
            await core.shutdown()
    asyncio.run(go())
