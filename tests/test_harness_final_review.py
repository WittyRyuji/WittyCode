"""Findings from the single final review, reproduced without accounts or remote services."""
from __future__ import annotations

import asyncio
import json
import ssl
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from desktop.core.chatgpt.errors import ChatGPTError
from desktop.core.chatgpt.http import HttpClient, _StreamHTTPSHandler
from desktop.core.chatgpt.responses import user_message
from desktop.core.harness.diagnostics import fingerprint
from desktop.core.harness.mcp import McpClient, McpError
from desktop.core.harness.toolset import mcp_name
from desktop.core.llm.chat import chat_messages
from desktop.core.providers import ProviderError
from tests.chatgpt_support import finish_turn, make_engine
from tests.fake_responses import FakeResponses, say
from tests.test_harness_mcp import PRELUDE, stub


@pytest.mark.parametrize("streamed", [False, True])
def test_authenticated_http_does_not_follow_a_cross_origin_redirect(streamed):
    seen = []
    class Destination(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_GET(self):
            seen.append(self.headers.get("Authorization"))
            self.send_response(200); self.send_header("Content-Length", "0"); self.end_headers()
    destination = ThreadingHTTPServer(("127.0.0.1", 0), Destination)
    class Redirect(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            self.send_response(302)
            self.send_header("Location", f"http://127.0.0.1:{destination.server_address[1]}/other")
            self.send_header("Content-Length", "0"); self.end_headers()
    source = ThreadingHTTPServer(("127.0.0.1", 0), Redirect)
    threads = [threading.Thread(target=s.serve_forever, daemon=True) for s in (source, destination)]
    for t in threads: t.start()
    client = HttpClient(urllib.request.build_opener(urllib.request.ProxyHandler({})))
    async def go():
        args = {"headers": {"Authorization": "Bearer demo-value"}, "json_body": {"text": "demo"}}
        url = f"http://127.0.0.1:{source.server_address[1]}/responses"
        if streamed:
            with pytest.raises(ChatGPTError) as error:
                _ = [line async for line in client.stream_lines("POST", url, **args)]
            assert error.value.status == 302
        else:
            assert (await client.request("POST", url, **args)).status == 302
    try:
        asyncio.run(go())
        assert seen == [], "even an empty redirected response must not receive credentials"
    finally:
        for s in (source, destination): s.shutdown(); s.server_close()
        for t in threads: t.join()


def test_streaming_preserves_an_explicit_tls_context():
    context = ssl.create_default_context()
    client = HttpClient(urllib.request.build_opener(urllib.request.HTTPSHandler(context=context)))
    handler = next(h for h in client._opener.handlers if isinstance(h, _StreamHTTPSHandler))
    assert handler._context is context


def test_broken_provider_metadata_does_not_fail_an_existing_plan_conversation(tmp_path):
    async def go():
        def broken(): raise ProviderError("invalid demo model config")
        with FakeResponses() as fake:
            fake.script(say("still works"))
            engine = make_engine(tmp_path, fake)
            engine.api = SimpleNamespace(spec=lambda _: None, providers=SimpleNamespace(secret_environment_names=broken))
            ctl = await engine.open_new(tmp_path / "project", model="gpt-6.1-sol")
            try:
                ctl.send_prompt("go"); await finish_turn(ctl)
                assert ctl.saved.turns[-1].status == "done"
                assert len(fake.requests) == 1
            finally:
                await engine.close_all()
    asyncio.run(go())


def test_broken_mcp_launch_definition_does_not_block_the_model(tmp_path):
    async def go():
        from desktop.core.connections import ConnectionError
        def broken(ctl): raise ConnectionError("missing demo environment variable")
        with FakeResponses() as fake:
            fake.script(say("ok"))
            engine = make_engine(tmp_path, fake); engine.memory_servers = broken
            ctl = await engine.open_new(tmp_path / "project", model="gpt-6.1-sol")
            try:
                ctl.send_prompt("go"); await finish_turn(ctl)
                assert ctl.saved.turns[-1].status == "done" and len(fake.requests) == 1
            finally:
                await engine.close_all()
    asyncio.run(go())


@pytest.mark.parametrize("cached", ["corrupt", {"tools": [{"name": "look"}]}])
def test_corrupt_cached_mcp_contract_does_not_poison_the_conversation(tmp_path, monkeypatch, cached):
    async def go():
        from desktop.core.memory.launch import McpCommand
        async def start(client, *args, **kwargs): pass
        monkeypatch.setattr(McpClient, "start", start)
        with FakeResponses() as fake:
            fake.script(say("ok"))
            engine = make_engine(tmp_path, fake)
            command = McpCommand("demo", "demo", (), {})
            engine.memory_servers = lambda ctl: [command]
            ctl = await engine.open_new(tmp_path / "project", model="gpt-6.1-sol")
            ctl.saved.prompt["mcp"] = {fingerprint([command.name, command.command, command.args, command.env]): cached}
            try:
                ctl.send_prompt("go"); await finish_turn(ctl)
                assert ctl.saved.turns[-1].status == "done"
            finally:
                await engine.close_all()
    asyncio.run(go())


def test_invalid_mcp_schema_is_a_contained_startup_error(tmp_path):
    body = PRELUDE + '''
def handle(m):
    if m["method"] == "initialize": INIT(m)
    elif m["method"] == "tools/list": send({"jsonrpc":"2.0", "id":m["id"], "result":{"tools":[{"name":"bad", "inputSchema":"invalid"}]}})
serve(handle)
'''
    async def go():
        client = stub(tmp_path, body)
        try:
            with pytest.raises(McpError): await client.start()
            assert not client.running
        finally:
            await client.close()
    asyncio.run(go())


def test_mcp_delimiter_cannot_alias_two_server_tool_pairs():
    assert mcp_name("a__b", "c") != mcp_name("a", "b__c")


def test_remembered_cwd_is_bound_to_resolved_target_not_the_alias(tmp_path, monkeypatch):
    from desktop.core.harness import permissions
    from desktop.core.harness.tools import ToolContext, ToolRegistry
    ctx = ToolContext(tmp_path)
    target = [tmp_path / "first"]
    monkeypatch.setattr(permissions, "_locate", lambda *args: (target[0], True))
    args = {"command": "npm test", "cwd": "work"}
    first = permissions.remember_key(ToolRegistry.default().get("shell"), args, ctx)
    target[0] = tmp_path / "second"
    assert permissions.remember_key(ToolRegistry.default().get("shell"), args, ctx) != first


@pytest.mark.parametrize("arguments", ["not-json", "[]", '"text"'])
def test_chat_replay_does_not_reemit_unparseable_tool_arguments(arguments):
    items = [user_message("go"), {"type": "function_call", "name": "demo", "call_id": "demo", "arguments": arguments}, {"type": "function_call_output", "call_id": "demo", "output": "Arguments rejected; nothing ran."}]
    sent = chat_messages("rules", items, "demo")
    assert json.loads(next(m for m in sent if "tool_calls" in m)["tool_calls"][0]["function"]["arguments"]) == {}


def test_goal_does_not_declare_success_when_stop_hook_rejects_it(tmp_path):
    async def go():
        from desktop.core.connections import ConnectionError
        from tests.app_support import ready_core
        from tests.test_app_usage_models import new
        from tests.fakes import until
        class Hooks:
            def active(self, *args): return []
            def mcp_commands(self, project): return []
            async def hooks(self, event, *args):
                if event == "Stop": raise ConnectionError("blocked")
                return ""
        core, plan, events, _, engine = await ready_core(tmp_path)
        cid = await new(core, events, tmp_path)
        engine.connections = Hooks()
        ctl = core._by_cid[cid].controller
        try:
            plan.script(say("Verified.\nGOAL: DONE"))
            assert (await core.call("session.goal", {"id": cid, "text": "finish demo"}))["ok"]
            await until(lambda: core._by_cid[cid].goal is None)
            assert ctl.saved.turns[-1].status == "done", "do not misclassify completed model output as failed"
            assert ctl.saved.goal, "a rejected Stop Hook must not mark the goal complete"
        finally:
            await core.shutdown()
    asyncio.run(go())


def test_responses_refusal_is_visible_without_empty_response_retries():
    from desktop.core.harness.loop import AgentLoop
    from desktop.core.chatgpt.history import _assistant_text
    from tests.test_harness_loop import FakeTools, Recorder, Script
    refusal = {"type": "message", "id": "demo", "role": "assistant", "content": [{"type": "refusal", "refusal": "I cannot do that."}]}
    script = Script([{"type": "response.created"}, {"type": "response.output_item.done", "item": refusal}, {"type": "response.completed", "response": {}}])
    observer = Recorder()
    end = asyncio.run(AgentLoop(script, FakeTools(), observer, model="m", reasoning=None, instructions="").run([user_message("go")]))
    assert end.reason == "done" and len(script.requests) == 1
    assert ("text", "I cannot do that.") in observer.log
    assert _assistant_text(refusal) == "I cannot do that."
