"""Independent boundary validation for manifests, costs, continuation bounds and single-writer sessions."""
from __future__ import annotations

import asyncio
import copy
from types import SimpleNamespace

import pytest

from desktop.core.chatgpt.controller import _Watch
from desktop.core.chatgpt.errors import ChatGPTError
from desktop.core.chatgpt.responses import ItemDone, Started, user_message
from desktop.core.harness.loop import Call
from desktop.core.harness.mcp import McpClient, McpTool
from desktop.core.harness.toolset import mcp_name
from desktop.core.memory.launch import McpCommand
from tests.chatgpt_support import finish_turn, make_engine
from tests.fake_responses import FakeResponses, say


def test_late_mcp_starts_in_parallel_and_contract_is_fixed_until_reopen(tmp_path, monkeypatch):
    async def go():
        gate = asyncio.Event()
        started = []
        async def start(client, *args, **kwargs):
            started.append(client.name)
            await gate.wait()
            client.tools = [McpTool(client.name, "look", "Read demo", {"type": "object"}, {"readOnlyHint": True})]
            client.instructions = "Demo server notes"
        monkeypatch.setattr(McpClient, "start", start)
        monkeypatch.setattr("desktop.core.chatgpt.controller.MCP_WAIT_SECONDS", .03)
        project = tmp_path / "project"; project.mkdir()
        with FakeResponses() as fake:
            fake.script(say("first"), say("second"), say("reopened"))
            engine = make_engine(tmp_path, fake)
            engine.memory_servers = lambda ctl: [McpCommand(n, "demo", (), {}) for n in ("a", "b", "c")]
            ctl = await engine.open_new(project, model="gpt-6.1-sol")
            try:
                ctl.send_prompt("one"); await finish_turn(ctl)
                assert sorted(started) == ["a", "b", "c"], "all three handshakes must start before any finishes"
                gate.set(); await ctl._mcp_task
                ctl.send_prompt("two"); await finish_turn(ctl)
                assert fake.requests[0]["tools"] == fake.requests[1]["tools"]
                identifier = ctl.session_id
                await ctl.close()
                reopened = await engine.open_saved(project, identifier)
                reopened.send_prompt("three"); await finish_turn(reopened)
                assert sum(t["name"].startswith("mcp__") for t in fake.requests[-1]["tools"]) == 3
            finally:
                gate.set(); await engine.close_all()
    asyncio.run(go())


def test_two_engines_cannot_write_the_same_open_conversation(tmp_path):
    async def go():
        project = tmp_path / "project"; project.mkdir()
        with FakeResponses() as fake:
            fake.script(say("one"), say("two"))
            first, second = make_engine(tmp_path, fake), make_engine(tmp_path, fake)
            ctl = await first.open_new(project, model="gpt-6.1-sol")
            try:
                ctl.send_prompt("one"); await finish_turn(ctl)
                original = copy.deepcopy(ctl.saved.items)
                with pytest.raises(ChatGPTError, match="另一个窗口"):
                    await second.open_saved(project, ctl.session_id)
                assert second.sessions.load(ctl.session_id).items == original
                await ctl.close()
                resumed = await second.open_saved(project, ctl.session_id)
                resumed.send_prompt("two"); await finish_turn(resumed)
                assert resumed.saved.items[:len(original)] == original
            finally:
                await first.close_all(); await second.close_all()
    asyncio.run(go())


@pytest.mark.parametrize("host,model,expected", [("api.openai.com", "gpt-6.1-sol", 272000), ("demo.example.com", "gpt-6.1-sol", 1050000), ("api.openai.com.example.com", "gpt-6.1-sol", 1050000), ("api.openai.com", "unknown", 1050000)])
def test_economic_window_only_applies_to_known_direct_api_pricing(tmp_path, host, model, expected):
    engine = make_engine(tmp_path, None)
    from desktop.core.chatgpt.controller import ChatGPTController
    ctl = ChatGPTController(engine, tmp_path, requested_model="api:demo:model")
    engine.api = SimpleNamespace(spec=lambda _: SimpleNamespace(base_url="https://" + host, native=model))
    assert ctl._economic_window(1050000) == expected
    watch = _Watch(ctl, SimpleNamespace(), None)
    assert watch._compact_at(1050000) == int(expected * .85)


def test_transport_continuation_is_bounded_and_user_stop_is_not_recovered(tmp_path):
    async def go():
        requests = []
        async def stream(request):
            requests.append(request)
            yield Started()
            raise ChatGPTError("reset", code="stream")
        engine = make_engine(tmp_path, None); engine.stream = stream
        ctl = await engine.open_new(tmp_path / "project", model="gpt-6.1-sol")
        try:
            ctl.send_prompt("go"); await finish_turn(ctl)
            assert len(requests) == 3 and ctl.saved.turns[-1].status == "failed"
            assert len({str(r["input"]) for r in requests}) == 3
        finally:
            await engine.close_all()
    asyncio.run(go())


def test_diagnostic_hashes_reuse_unchanged_items_but_detect_nested_edits(monkeypatch):
    from desktop.core.harness import diagnostics
    trace = diagnostics.RequestTrace()
    original = diagnostics.fingerprint
    hashed = []
    def counting(value):
        hashed.append(value)
        return original(value)
    monkeypatch.setattr(diagnostics, "fingerprint", counting)
    first = user_message("old " * 100000)
    request = {"model": "demo", "instructions": "stable", "input": [first]}
    trace.record(request, None, 0)
    hashed.clear()
    request["input"].append(user_message("new"))
    second = trace.record(request, None, 0)
    assert second["shared_prefix_items"] == 1 and not any(v is first for v in hashed)
    request["input"][0]["content"][0]["text"] = "changed"
    assert trace.record(request, None, 0)["shared_prefix_items"] == 0


@pytest.mark.parametrize("encoding,text,new", [("shift_jis", "日本語テスト", "日本語変更"), ("big5", "繁體中文測試", "繁體中文修改"), ("gb18030", "这是旧内容", "这是新内容")])
def test_legacy_encoding_guess_cannot_corrupt_edits(tmp_path, encoding, text, new):
    from desktop.core.harness.tools import ToolContext, ToolRegistry
    path = tmp_path / "legacy.txt"
    original = (text + "\r\n").encode(encoding); path.write_bytes(original)
    registry, ctx = ToolRegistry.default(), ToolContext(tmp_path)
    async def go():
        guessed = await registry.run("read_file", {"file_path": path.name}, ctx)
        assert "guess" in guessed.output or "U+FFFD" in guessed.output
        denied = await registry.run("edit_file", {"file_path": path.name, "old_str": text, "new_str": new}, ctx)
        assert denied.is_error and path.read_bytes() == original
        read = await registry.run("read_file", {"file_path": path.name, "encoding": encoding}, ctx)
        assert not read.is_error and text in read.output
        edited = await registry.run("edit_file", {"file_path": path.name, "old_str": text, "new_str": new}, ctx)
        assert not edited.is_error and path.read_bytes() == (new + "\r\n").encode(encoding)
    asyncio.run(go())


def test_models_connections_and_profile_are_protected_even_at_full_access(tmp_path):
    async def go():
        engine = make_engine(tmp_path, None)
        ctl = await engine.open_new(tmp_path / "project", model="gpt-6.1-sol", autonomy="full")
        try:
            tools = await ctl._ensure_tools()
            for name in ("models.json", "connections.json", "settings.json", "providers.json"):
                target = engine.root.parent / name
                assert tools.check(Call("i", "c", "read_file", "{}", {"file_path": str(target)})).verdict == "deny"
                assert tools.check(Call("i", "c", "create_file", "{}", {"file_path": str(target), "content": "{}"})).verdict == "deny"
        finally:
            await engine.close_all()
    asyncio.run(go())
