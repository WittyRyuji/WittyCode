"""Design-review regressions: real loops/controllers and wire contracts, with no remote requests."""
from __future__ import annotations

import asyncio
import copy
import json
from types import SimpleNamespace

import pytest

from desktop.core.chatgpt.errors import ChatGPTError
from desktop.core.chatgpt.responses import Finished, ItemDone, Started, user_message, for_responses
from desktop.core.harness import compact
from desktop.core.harness.loop import AgentLoop, Call, Observer
from desktop.core.harness.mcp import McpTool
from desktop.core.harness.permissions import decide, remember_key
from desktop.core.harness.store import SavedSession, SavedTurn, recover
from desktop.core.harness.tools import ToolContext, ToolRegistry
from desktop.core.harness.toolset import HarnessTools, mcp_name
from desktop.core.llm.anthropic import anthropic_messages
from desktop.core.llm.bedrock import converse_messages
from desktop.core.llm.chat import chat_messages
from tests.chatgpt_support import finish_turn, make_engine
from tests.fake_responses import FakeResponses, say
from tests.test_harness_loop import FakeTools


def thought():
    return {"type": "reasoning", "encrypted_content": "opaque", "wc_origin": "demo", "wc_signature": "signature", "wc_reasoning": "thought"}


def answer():
    return {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "answer"}]}


@pytest.mark.parametrize("ending", ["error", "cancel", "empty"])
def test_loop_does_not_keep_orphan_reasoning(ending):
    requests = []
    async def stream(request):
        requests.append(copy.deepcopy(request))
        if len(requests) == 1:
            yield ItemDone(thought())
            if ending == "error":
                raise ChatGPTError("reset", code="stream")
            if ending == "cancel":
                raise asyncio.CancelledError
            yield Finished(None)
        else:
            from desktop.core.chatgpt.responses import TextDone
            yield TextDone("a", "answer")
            yield ItemDone(answer())
            yield Finished(None)
    history = [user_message("go")]
    loop = AgentLoop(stream, FakeTools(), Observer(), model="m", reasoning=None, instructions="")
    if ending == "empty":
        assert asyncio.run(loop.run(history)).reason == "done"
        assert not any(i.get("type") == "reasoning" for i in requests[1]["input"])
    else:
        with pytest.raises(asyncio.CancelledError if ending == "cancel" else ChatGPTError):
            asyncio.run(loop.run(history))
        assert history == [user_message("go")]


@pytest.mark.parametrize("adapter", [anthropic_messages, converse_messages, for_responses])
def test_wire_contract_removes_orphan_but_keeps_linked_thinking(adapter):
    before = [user_message("one"), thought(), user_message("two"), thought(), answer()]
    original = copy.deepcopy(before)
    sent = adapter(before) if adapter is for_responses else adapter(before, "demo")
    serial = json.dumps(sent)
    assert serial.count("signature" if adapter is not for_responses else "opaque") == (2 if adapter is not for_responses else 1)
    assert before == original
    if adapter is anthropic_messages:
        assert all(m["content"][-1]["type"] not in ("thinking", "redacted_thinking") for m in sent if m["role"] == "assistant")
    elif adapter is converse_messages:
        assert all("reasoningContent" not in m["content"][-1] for m in sent if m["role"] == "assistant")


def test_recover_old_archive_remaps_turns_after_orphan_removal():
    saved = SavedSession("demo", "project", items=[user_message("one"), thought(), user_message("two"), answer()], turns=[SavedTurn(0, 1, 2), SavedTurn(2, 3, 4)])
    assert recover(saved)
    assert len(saved.items) == 3 and saved.turns[1].start == 1
    assert not recover(saved)
    assert compact.ends_whole(saved.items)


@pytest.mark.parametrize("origin", ["demo", "api.openai.com", "api.openai.com.example.com"])
def test_chat_maps_developer_role_for_compatible_services(origin):
    update = {"type": "message", "role": "developer", "content": [{"type": "input_text", "text": "Auto is on"}]}
    sent = chat_messages("rules", [user_message("one"), answer(), update, user_message("two")], origin)
    assert sent[-2]["role"] == ("developer" if origin == "api.openai.com" else "system")


def test_small_context_changes_do_not_copy_project_rules(tmp_path):
    async def go():
        project = tmp_path / "project"; project.mkdir()
        (project / "AGENTS.md").write_text("PROJECT_RULE " * 1700, encoding="utf-8")
        with FakeResponses() as fake:
            fake.script(*[say("ok") for _ in range(7)])
            engine = make_engine(tmp_path, fake)
            ctl = await engine.open_new(project, model="gpt-6.1-sol")
            try:
                for n in range(7):
                    await ctl.update_settings(mode="spec" if n % 2 else "auto")
                    ctl.send_prompt(str(n)); await finish_turn(ctl)
                updates = [i for i in fake.requests[-1]["input"] if i.get("role") == "developer"]
                assert len(updates) == 6
                assert all("PROJECT_RULE" not in str(i) for i in updates)
                assert compact.estimate_items(updates) < 2500
                identifier = ctl.session_id
                await ctl.close()
                ctl = await engine.open_saved(project, identifier)
                fake.script(say("reopened"))
                ctl.send_prompt("unchanged"); await finish_turn(ctl)
                assert sum(i.get("role") == "developer" for i in fake.requests[-1]["input"]) == 6
            finally:
                await engine.close_all()
    asyncio.run(go())


def test_third_party_readonly_annotation_is_not_permission(tmp_path):
    tool = McpTool("third-party", "drop_database", "claimed read", {}, {"readOnlyHint": True})
    client = SimpleNamespace(name=tool.server, tools=[tool], instructions="", running=True)
    tools = HarnessTools(ToolRegistry.default(), ToolContext(tmp_path), "ask", [client])
    call = Call("a", "b", mcp_name(tool.server, tool.name), "{}")
    assert tools.check(call).verdict == "ask"
    tools.plan = True
    assert tools.check(call).verdict == "deny"


@pytest.mark.parametrize("first,other", [("git add .", "git push --force"), ("python -m pytest", 'python -c "print(1)"'), ("npm test", "npm publish"), ("git reset file", "git reset --hard")])
def test_command_approval_does_not_grant_other_operations(tmp_path, first, other):
    ctx, spec = ToolContext(tmp_path), ToolRegistry.default().get("shell")
    key = remember_key(spec, {"command": first}, ctx)
    assert decide("ask", spec, {"command": other}, ctx, remembered={key}).verdict == "ask"


@pytest.mark.parametrize("left,right", [("long-" + "a" * 80, "long-" + "a" * 80 + "b"), ("a.b", "a_b")])
def test_mcp_tool_names_cannot_collide(left, right):
    assert mcp_name("demo", left) != mcp_name("demo", right)
    assert max(len(mcp_name("demo", left)), len(mcp_name("demo", right))) <= 64


def test_stop_hook_failure_does_not_fail_completed_turn(tmp_path):
    async def go():
        from desktop.core.connections import ConnectionError
        class Hooks:
            def active(self, *args): return []
            def mcp_commands(self, project): return []
            async def hooks(self, event, *args):
                if event == "Stop": raise ConnectionError("blocked")
                return ""
        with FakeResponses() as fake:
            fake.script(say("done"))
            engine = make_engine(tmp_path, fake); engine.connections = Hooks()
            ctl = await engine.open_new(tmp_path, model="gpt-6.1-sol")
            try:
                ctl.send_prompt("go"); await finish_turn(ctl)
                assert ctl.saved.turns[-1].status == "done"
                assert any("Hook" in b.text for b in ctl.transcript.turns[-1].blocks if b.kind == "notice")
            finally:
                await engine.close_all()
    asyncio.run(go())


def test_controller_continues_partial_network_answer_with_a_new_request(tmp_path):
    async def go():
        from desktop.core.chatgpt.responses import TextDelta, TextDone
        requests = []
        async def stream(request):
            requests.append(copy.deepcopy(request))
            yield Started()
            if len(requests) == 1:
                yield TextDelta("partial", "Already checked the files.")
                raise ChatGPTError("reset", code="stream")
            yield TextDone("done", "Finished.")
            yield ItemDone(answer())
            yield Finished(None)
        engine = make_engine(tmp_path, None); engine.stream = stream
        ctl = await engine.open_new(tmp_path / "project", model="gpt-6.1-sol")
        try:
            ctl.send_prompt("go"); await finish_turn(ctl)
            assert ctl.saved.turns[-1].status == "done" and len(requests) == 2
            assert requests[0]["input"] != requests[1]["input"]
            assert "Already checked the files." in str(requests[1]["input"])
            assert "Do not repeat" in str(requests[1]["input"])
        finally:
            await engine.close_all()
    asyncio.run(go())


def test_subprocess_environments_strip_secrets_and_honour_mcp_opt_in(monkeypatch):
    from desktop.core.child_env import environment
    monkeypatch.setenv("DEMO_API_KEY", "fake-value")
    monkeypatch.setenv("DEMO_ORDINARY", "configured-value")
    monkeypatch.setenv("BUILD_LABEL", "demo")
    shell = environment(blocked=("DEMO_ORDINARY",))
    assert "DEMO_API_KEY" not in shell and "DEMO_ORDINARY" not in shell and shell["BUILD_LABEL"] == "demo"
    mcp = environment(minimal=True, explicit={"DEMO_API_KEY": "opt-in"})
    assert mcp["DEMO_API_KEY"] == "opt-in" and "DEMO_ORDINARY" not in mcp and "BUILD_LABEL" not in mcp


def test_compaction_rebases_prompt_and_drops_obsolete_updates(tmp_path):
    async def go():
        project = tmp_path / "project"; project.mkdir()
        (project / "AGENTS.md").write_text("Never remove tests.", encoding="utf-8")
        with FakeResponses() as fake:
            fake.script(say("a " * 10000), say("b " * 10000), say("summary"), say("next"))
            engine = make_engine(tmp_path, fake)
            ctl = await engine.open_new(project, model="gpt-6.1-sol")
            try:
                ctl.send_prompt("one"); await finish_turn(ctl)
                await ctl.update_settings(mode="spec")
                ctl.send_prompt("two"); await finish_turn(ctl)
                expected = ctl.saved.prompt["current"]
                assert ctl.start_compaction() is None; await finish_turn(ctl)
                assert ctl.saved.compaction and ctl.saved.prompt["instructions"] == expected
                ctl.send_prompt("three"); await finish_turn(ctl)
                assert fake.requests[-1]["instructions"] == expected
                assert not any(i.get("wc_context_update") for i in ctl.saved.items[ctl.saved.compaction["upto"]:])
                assert not any(i.get("role") == "developer" for i in fake.requests[-1]["input"])
            finally:
                await engine.close_all()
    asyncio.run(go())
