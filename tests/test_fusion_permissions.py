"""Exercise Fusion permissions through real tool execution with local scripted model runners only."""
from __future__ import annotations

import asyncio

import pytest

from desktop.core.harness.loop import AgentLoop, Call, Observer
from desktop.core.harness.tools import ToolContext, ToolRegistry
from desktop.core.harness.toolset import HarnessTools
from tests.app_support import ready_core
from tests.chatgpt_support import finish_turn
from tests.fake_responses import call, say
from tests.fakes import until
from tests.fusion_acp_support import make_guarded_engine
from tests.test_harness_toolset import StubClient, tool

MAINS = ("chatgpt:gpt-6.1-sol", "devin:swe-2-high")
WORKERS = ("chatgpt:gpt-6-luna", "devin:swe-2-high")
COMMAND = {"command": "New-Item marker.txt"}


async def new_main(core, tmp_path, main_model, worker, autonomy):
    await until(lambda: any(item["source"] == "devin" for item in core.catalog.models), timeout=20)
    core.settings.fusion_main = main_model
    core.settings.fusion_workers = [worker]
    core.settings.fusion_light = worker
    cid = (await core.call("session.new", {"projectId": str(tmp_path / "proj"), "fusion": True}))["id"]
    main = core._by_cid[cid].controller
    assert await main.update_settings(autonomy=autonomy)
    assert main.model == main_model.removeprefix("chatgpt:")
    return main


def start_shell(model, main):
    if not main.model.startswith("devin:"):
        model.script(call("shell", COMMAND), say("Main command checked."))
    assert main.send_prompt("[fusion-shell] Create the marker with a command.")


def start_expert_shell(model, main, worker):
    task = {"scope": "src", "model": worker, "role": "worker", "task": "[fusion-shell] Create the marker with a command."}
    script = []
    if not main.model.startswith("devin:"):
        script.append(call("delegate_task", task))
    if not worker.startswith("devin:"):
        script.extend([call("shell", COMMAND), say("Expert command checked.")])
    if not main.model.startswith("devin:"):
        script.append(say("Expert result reviewed."))
    if script:
        model.script(*script)
    action = "delegate-acp-shell" if worker.startswith("devin:") else "delegate-shell"
    assert main.send_prompt(f"[fusion-{action}] Delegate the command check.")


async def settle_command(main, *, expert=False, approval=None):
    prompts = main.agent_prompts if expert else main.transcript.pending_prompts
    await until(lambda: bool(prompts()) or main._turn is None, timeout=30)
    pending = prompts()
    if approval is None:
        assert not pending, "The inherited permission should not require another approval."
    else:
        assert len(pending) == 1, "The command should require explicit approval."
        controller = next(iter(main.agents.values())).controller if expert else main
        assert controller.answer_permission(pending[0].id, approval)
    await finish_turn(main, timeout=30)
    assert main.saved.turns[-1].status == "done", main.last_error
    if expert:
        assert next(iter(main.agents.values())).record.status == "done"


@pytest.mark.parametrize("main_model", MAINS)
@pytest.mark.parametrize("worker", WORKERS)
@pytest.mark.parametrize("autonomy", ("ask", "code", "full"))
def test_main_and_expert_execute_the_same_command_with_the_same_permission(tmp_path, main_model, worker, autonomy):
    async def go():
        harness = make_guarded_engine(tmp_path)
        core, model, _, _, _ = await ready_core(tmp_path, devin_engine=harness.engine)
        try:
            main = await new_main(core, tmp_path, main_model, worker, autonomy)
            marker = tmp_path / "proj" / "marker.txt"
            approval = None if autonomy == "full" else "cancel"
            start_shell(model, main)
            await settle_command(main, approval=approval)
            assert marker.is_file() == (autonomy == "full")
            if marker.exists():
                marker.unlink()
            start_expert_shell(model, main, worker)
            await settle_command(main, expert=True, approval=approval)
            assert marker.is_file() == (autonomy == "full")
        finally:
            await core.shutdown()
    asyncio.run(go())


@pytest.mark.parametrize("main_model", MAINS)
@pytest.mark.parametrize("worker", WORKERS)
def test_main_session_command_approval_is_inherited_live_and_can_be_revoked(tmp_path, main_model, worker):
    async def go():
        harness = make_guarded_engine(tmp_path)
        core, model, _, _, _ = await ready_core(tmp_path, devin_engine=harness.engine)
        try:
            main = await new_main(core, tmp_path, main_model, worker, "code")
            marker = tmp_path / "proj" / "marker.txt"
            start_shell(model, main)
            await settle_command(main, approval="proceed_always")
            assert marker.is_file()
            marker.unlink()
            start_expert_shell(model, main, worker)
            await settle_command(main, expert=True)
            assert marker.is_file()
            marker.unlink()
            original = next(iter(main.agents))
            main._tools.remembered.clear()
            start_expert_shell(model, main, worker)
            await settle_command(main, expert=True, approval="cancel")
            assert not marker.exists() and next(iter(main.agents)) == original
        finally:
            await core.shutdown()
    asyncio.run(go())


@pytest.mark.parametrize("worker", WORKERS)
def test_reused_and_reopened_expert_inherits_the_current_main_level(tmp_path, worker):
    async def go():
        harness = make_guarded_engine(tmp_path)
        core, model, _, _, _ = await ready_core(tmp_path, devin_engine=harness.engine)
        try:
            main = await new_main(core, tmp_path, MAINS[0], worker, "full")
            marker = tmp_path / "proj" / "marker.txt"
            start_expert_shell(model, main, worker)
            await settle_command(main, expert=True)
            assert marker.is_file()
            marker.unlink()
            original = next(iter(main.agents))
            assert await main.update_settings(autonomy="ask")
            start_expert_shell(model, main, worker)
            await settle_command(main, expert=True, approval="cancel")
            assert not marker.exists() and next(iter(main.agents)) == original
            saved_id = main.session_id
            assert await main.update_settings(autonomy="full")
            await main.close()
            reopened = await main.manager.open_saved(tmp_path / "proj", saved_id)
            start_expert_shell(model, reopened, worker)
            await settle_command(reopened, expert=True)
            assert marker.is_file() and next(iter(reopened.agents)) == original
        finally:
            await core.shutdown()
    asyncio.run(go())


@pytest.mark.parametrize("main_model", MAINS)
@pytest.mark.parametrize("worker", WORKERS)
def test_plan_rejects_delegation_before_any_expert_tool_can_execute(tmp_path, main_model, worker):
    async def go():
        harness = make_guarded_engine(tmp_path)
        core, model, _, _, _ = await ready_core(tmp_path, devin_engine=harness.engine)
        try:
            main = await new_main(core, tmp_path, main_model, worker, "full")
            assert await main.update_settings(mode="spec")
            task = {"scope": "src", "model": worker, "role": "worker", "task": "[fusion-shell] Create the marker."}
            if not main.model.startswith("devin:"):
                model.script(call("delegate_task", task), say("Wait for plan approval."))
            action = "delegate-acp-shell" if worker.startswith("devin:") else "delegate-shell"
            assert main.send_prompt(f"[fusion-{action}] Try delegation during planning.")
            await finish_turn(main)
            assert not main.agents and not (tmp_path / "proj" / "marker.txt").exists()
            outputs = [item["output"] for item in main.saved.items if item.get("type") == "function_call_output"]
            assert outputs and "Plan mode" in outputs[0]
            # The runtime also refuses a direct call, including before a toolset exists for a child.
            outcome = await main._fusion.delegate("src", worker, "worker", "Create the marker.")
            assert outcome.is_error and "Plan mode" in outcome.output and not main.agents
        finally:
            await core.shutdown()
    asyncio.run(go())


@pytest.mark.parametrize("current_mode", ("auto", "spec"))
def test_delegation_uses_the_active_plan_snapshot_not_the_next_turns_selection(tmp_path, current_mode):
    async def go():
        harness = make_guarded_engine(tmp_path)
        core, model, _, _, _ = await ready_core(tmp_path, devin_engine=harness.engine)
        try:
            worker = WORKERS[1]
            main = await new_main(core, tmp_path, MAINS[0], worker, "full")
            assert await main.update_settings(mode=current_mode)
            task = {"scope": "src", "model": worker, "role": "worker", "task": "[fusion-shell] Create the marker."}
            model.script([("hold", "delegation"), *call("delegate_task", task)], say("Reviewed."))
            assert main.send_prompt("Delegate if the current turn permits it.")
            await until(lambda: bool(model.requests))
            assert await main.update_settings(mode="spec" if current_mode == "auto" else "auto")
            model.release("delegation")
            await finish_turn(main)
            assert (tmp_path / "proj" / "marker.txt").is_file() == (current_mode == "auto")
            assert bool(main.agents) == (current_mode == "auto")
        finally:
            model.release("delegation")
            await core.shutdown()
    asyncio.run(go())


class Decline(Observer):
    def __init__(self):
        self.asked = []

    async def ask(self, tool_call, verdict):
        self.asked.append(tool_call.name)
        return "cancel"


def execution(tools, observer):
    return AgentLoop(None, tools, observer, model="scripted", reasoning=None, instructions="")


def test_inherited_mcp_approval_is_tool_specific_and_does_not_bypass_plan(tmp_path):
    async def go():
        parent_client = StubClient("demo", [tool("demo", "one"), tool("demo", "two")])
        child_client = StubClient("demo", parent_client.tools)
        registry = ToolRegistry.default()
        parent = HarnessTools(registry, ToolContext(tmp_path), "code", [parent_client])
        child = HarnessTools(registry, ToolContext(tmp_path), "full", [child_client])
        child.permission_parent = parent
        approved = Call("one", "one", "mcp__demo__one", "{}", {})
        other = Call("two", "two", "mcp__demo__two", "{}", {})
        parent.remember(approved)
        observer = Decline()
        loop = execution(child, observer)
        assert not (await loop._execute(approved)).is_error
        assert (await loop._execute(other)).cancelled
        assert child_client.calls == [("one", {})] and observer.asked == [other.name]
        parent.plan = True
        assert (await loop._execute(approved)).cancelled
        assert child_client.calls == [("one", {})]  # Neither a remembered grant nor the child's full snapshot overrides Plan.
        assert not child.remembered and parent_client.calls == []
    asyncio.run(go())


def test_actual_file_execution_uses_the_main_live_level_and_plan_over_child_snapshots(tmp_path):
    async def go():
        registry = ToolRegistry.default()
        parent = HarnessTools(registry, ToolContext(tmp_path), "full")
        child = HarnessTools(registry, ToolContext(tmp_path), "full")
        child.permission_parent = parent
        observer = Decline()
        loop = execution(child, observer)
        allowed = Call("allow", "allow", "create_file", "{}", {"file_path": "allowed.txt", "content": "allowed"})
        restricted = Call("ask", "ask", "create_file", "{}", {"file_path": "restricted.txt", "content": "must not be written"})
        assert not (await loop._execute(allowed)).is_error
        assert (tmp_path / "allowed.txt").read_text() == "allowed"
        parent.level = "ask"
        assert (await loop._execute(restricted)).cancelled
        assert not (tmp_path / "restricted.txt").exists() and observer.asked == ["create_file"]
        parent.level = "full"
        parent.plan = True
        parent.remember(restricted)
        assert (await loop._execute(restricted)).cancelled
        assert not (tmp_path / "restricted.txt").exists() and observer.asked == ["create_file"]
    asyncio.run(go())


def test_main_and_child_protected_paths_win_over_full_access_and_grants(tmp_path):
    async def go():
        registry = ToolRegistry.default()
        parent_context = ToolContext(tmp_path, protected=(tmp_path / "main-private.txt",))
        child_context = ToolContext(tmp_path, protected=(tmp_path / "child-private.txt",))
        parent = HarnessTools(registry, parent_context, "full")
        child = HarnessTools(registry, child_context, "full")
        child.permission_parent = parent
        observer = Decline()
        loop = execution(child, observer)
        for name in ("main-private.txt", "child-private.txt"):
            tool_call = Call(name, name, "create_file", "{}", {"file_path": name, "content": "must not be written"})
            parent.remember(tool_call)
            child.remember(tool_call)
            outcome = await loop._execute(tool_call)
            assert outcome.cancelled and "protected" in outcome.output
            assert not (tmp_path / name).exists()
        assert not observer.asked
    asyncio.run(go())
