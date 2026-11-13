"""Cache-prefix boundary cases: delayed/dead memory and compaction, using real local servers."""
from __future__ import annotations

import asyncio
import copy
import json

from desktop.core.chatgpt.responses import build_request, user_message
from desktop.core.harness import compact
from desktop.core.harness.mcp import McpClient
from desktop.core.harness.prompt import context_update
from desktop.core.memory.launch import MemoryLaunch
from tests.chatgpt_support import finish_turn, make_engine
from tests.fake_responses import FakeResponses, say
from tests.test_harness_loop import Script


def test_memory_starting_late_does_not_change_tools_or_instructions(tmp_path, monkeypatch):
    async def go():
        gate = asyncio.Event()
        original = McpClient.start
        async def delayed(client, *args, **kwargs):
            await gate.wait()
            await original(client, *args, **kwargs)
        monkeypatch.setattr(McpClient, "start", delayed)
        monkeypatch.setattr("desktop.core.chatgpt.controller.MCP_WAIT_SECONDS", .01)
        project = tmp_path / "project"; project.mkdir()
        with FakeResponses() as fake:
            fake.script(say("one"), say("two"))
            engine = make_engine(tmp_path, fake)
            engine.memory = MemoryLaunch(tmp_path / "memory")
            ctl = await engine.open_new(project, model="gpt-6.1-sol")
            try:
                ctl.send_prompt("one"); await finish_turn(ctl)
                assert sum(t["name"].startswith("mcp__") for t in fake.requests[0]["tools"]) == 5
                gate.set(); await ctl._mcp_task
                ctl.send_prompt("two"); await finish_turn(ctl)
                assert fake.requests[0]["tools"] == fake.requests[1]["tools"]
                assert fake.requests[0]["instructions"] == fake.requests[1]["instructions"]
                assert not any(item.get("role") == "developer" for item in fake.requests[1]["input"])
            finally:
                gate.set()
                await engine.close_all()
    asyncio.run(go())


def test_dead_memory_restarts_once_without_disappearing_from_the_prompt(tmp_path):
    async def go():
        project = tmp_path / "project"; project.mkdir()
        with FakeResponses() as fake:
            fake.script(say("one"), say("two"), say("three"))
            engine = make_engine(tmp_path, fake)
            engine.memory = MemoryLaunch(tmp_path / "memory")
            ctl = await engine.open_new(project, model="gpt-6.1-sol")
            try:
                ctl.send_prompt("one"); await finish_turn(ctl)
                client = ctl._clients[0]
                client._process.kill(); await client._process.wait()
                ctl.send_prompt("two"); await finish_turn(ctl)
                assert client.running
                assert (await client.call("memory_search", {"query": ""})).is_error is False
                client._process.kill(); await client._process.wait()
                ctl.send_prompt("three"); await finish_turn(ctl)
                assert not client.running
                assert fake.requests[0]["tools"] == fake.requests[1]["tools"] == fake.requests[2]["tools"]
                assert fake.requests[0]["instructions"] == fake.requests[2]["instructions"]
                notices = [b.text for t in ctl.transcript.turns for b in t.blocks if b.kind == "notice"]
                assert sum("重启一次" in n for n in notices) == 1
            finally:
                await engine.close_all()
    asyncio.run(go())


def test_compaction_carries_current_developer_rules_as_instructions_not_a_summary_guess():
    async def go():
        view = [user_message("old " * 10000), {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "done " * 10000}]}]
        script = Script(say("short summary"))
        result = await compact.compact(view, stream=script, model="m", reasoning=None, window=20000, whole=True, rules="Current rules: never remove tests.")
        assert result.fits
        assert result.view[1] == context_update("Current rules: never remove tests.")
        record = {"summary": result.summary, "kept": result.kept, "upto": result.cut, "rules": "Current rules: never remove tests."}
        assert compact.synthetic_count(record) == 2
        assert compact.view_of(view, record) == result.view
        assert compact.log_index(len(result.view), result.cut, 2) == len(view)
    asyncio.run(go())


def test_summary_reuses_the_same_request_prefix_only_when_it_fits():
    view = [user_message("first " * 5000), {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "answer " * 5000}]}]
    base = build_request(model="m", reasoning="high", instructions="stable", tools=[{"name": "read_file", "parameters": {"type": "object"}}], items=[])
    original = copy.deepcopy(base)
    script = Script(say("summary"))
    result = asyncio.run(compact.compact(view, stream=script, model="m", reasoning="low", window=30000, whole=True, prefix_request=base))
    request = script.requests[0]
    assert request["instructions"] == base["instructions"] and request["tools"] == base["tools"]
    assert request["reasoning"] == base["reasoning"] and request["tool_choice"] == "none"
    assert request["input"][:-1] == view and request["input"][-1]["content"][0]["text"].startswith(compact.SUMMARY_TASK)
    assert result.fits and base == original
    assert compact.prefix_summary_request(base, view, 1000) is None
