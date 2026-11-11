"""``core.harness.mcp``: the MCP client against the real shared-memory server (a subprocess) and against small stubs
that misbehave."""

from __future__ import annotations

import asyncio
import sys
import textwrap

import pytest

from desktop.core.harness.mcp import McpClient, McpError
from desktop.core.memory.launch import MemoryLaunch


def memory_client(tmp_path, run: str = "run1") -> McpClient:
    command = MemoryLaunch(tmp_path / "memory").server_for("chatgpt", str(tmp_path / "project"), run)
    return McpClient(command.name, command.command, command.args, command.env)


def stub(tmp_path, body: str) -> McpClient:
    script = tmp_path / "stub.py"
    script.write_text(textwrap.dedent(body), encoding="utf-8")
    return McpClient("stub", sys.executable, [str(script)])


PRELUDE = """
import json, sys, time
def send(message):
    sys.stdout.write(json.dumps(message) + "\\n"); sys.stdout.flush()
def serve(handler):
    for line in sys.stdin:
        message = json.loads(line)
        if "id" in message:
            handler(message)
INIT = lambda m: send({"jsonrpc": "2.0", "id": m["id"], "result": {"protocolVersion": "2025-06-18", "capabilities": {}, "serverInfo": {"name": "stub", "version": "1"}, "instructions": "Stub says hi."}})
"""


def test_the_memory_server_is_started_listed_called_and_closed(tmp_path):
    async def go() -> None:
        client = memory_client(tmp_path)
        await client.start()
        try:
            assert client.running
            names = {tool.name for tool in client.tools}
            assert names == {"memory_search", "memory_read", "memory_write", "memory_update", "memory_forget"}
            assert "memory_search" in client.instructions
            write = next(t for t in client.tools if t.name == "memory_write")
            assert write.harmless and not write.read_only
            assert next(t for t in client.tools if t.name == "memory_search").read_only
            saved = await client.call("memory_write", {"title": "Deploy command", "text": "Deploy with make ship-it.", "tags": ["deploy", "部署"]})
            assert not saved.is_error and "Saved as" in saved.text
            found = await client.call("memory_search", {"query": "deploy"})
            assert "Deploy command" in found.text
            rejected = await client.call("memory_read", {"id": "m-nope"})
            assert rejected.is_error
        finally:
            await client.close()
        assert not client.running

    asyncio.run(go())


def test_a_note_written_through_the_client_says_who_wrote_it(tmp_path):
    async def go() -> None:
        client = memory_client(tmp_path, run="abc123")
        await client.start()
        try:
            await client.call("memory_write", {"title": "Chinese note", "text": "用中文回答。", "scope": "global"})
        finally:
            await client.close()

    asyncio.run(go())
    from desktop.core.memory.store import MemoryStore

    entries = MemoryStore(tmp_path / "memory").entries(project=None)
    assert [e.title for e in entries] == ["Chinese note"]
    assert entries[0].source == {"engine": "chatgpt", "run": "abc123"}


def test_a_command_that_does_not_exist_fails_to_start(tmp_path):
    async def go() -> None:
        client = McpClient("ghost", str(tmp_path / "no-such-program.exe"))
        with pytest.raises(McpError):
            await client.start(timeout=5)
        assert not client.running

    asyncio.run(go())


def test_a_server_that_exits_at_once_fails_to_start_and_is_cleaned_up(tmp_path):
    async def go() -> None:
        client = stub(tmp_path, "import sys\nsys.exit(3)\n")
        with pytest.raises(McpError):
            await client.start(timeout=10)
        assert not client.running

    asyncio.run(go())


def test_a_server_that_never_answers_times_out(tmp_path):
    async def go() -> None:
        client = stub(tmp_path, "import time\ntime.sleep(60)\n")
        with pytest.raises(McpError):
            await client.start(timeout=1)

    asyncio.run(go())


def test_a_call_that_hangs_comes_back_as_an_error_result_not_a_hang(tmp_path):
    body = PRELUDE + """
def handle(m):
    if m["method"] == "initialize": INIT(m)
    elif m["method"] == "tools/list": send({"jsonrpc": "2.0", "id": m["id"], "result": {"tools": [{"name": "slow", "inputSchema": {"type": "object"}}]}})
    else: time.sleep(30)
serve(handle)
"""

    async def go() -> None:
        client = stub(tmp_path, body)
        await client.start()
        try:
            result = await client.call("slow", {}, timeout=0.5)
            assert result.is_error and "did not answer" in result.text
        finally:
            await client.close()

    asyncio.run(go())


def test_a_server_that_dies_mid_call_fails_that_call_and_later_ones(tmp_path):
    body = PRELUDE + """
def handle(m):
    if m["method"] == "initialize": INIT(m)
    elif m["method"] == "tools/list": send({"jsonrpc": "2.0", "id": m["id"], "result": {"tools": [{"name": "die", "inputSchema": {"type": "object"}}]}})
    else: sys.exit(1)
serve(handle)
"""

    async def go() -> None:
        client = stub(tmp_path, body)
        await client.start()
        try:
            first = await client.call("die", {}, timeout=10)
            assert first.is_error
            second = await client.call("die", {}, timeout=10)
            assert second.is_error and not client.running
        finally:
            await client.close()

    asyncio.run(go())


def test_pings_are_answered_and_other_requests_from_the_server_are_refused(tmp_path):
    body = PRELUDE + """
def handle(m):
    if m["method"] == "initialize": INIT(m)
    elif m["method"] == "tools/list":
        send({"jsonrpc": "2.0", "id": 900, "method": "ping"})
        send({"jsonrpc": "2.0", "id": 901, "method": "sampling/createMessage", "params": {}})
        send({"jsonrpc": "2.0", "id": m["id"], "result": {"tools": [{"name": "t", "inputSchema": {"type": "object"}}]}})
    elif m["method"] == "tools/call":
        send({"jsonrpc": "2.0", "id": m["id"], "result": {"content": [{"type": "text", "text": "ok"}, {"type": "image", "data": "x"}, {"type": "text", "text": "!"}], "isError": False}})
def serve2():
    for line in sys.stdin:
        message = json.loads(line)
        if message.get("id") in (900, 901):
            with open(sys.argv[0] + ".log", "a") as log: log.write(json.dumps(message) + "\\n")
        elif "id" in message: handle(message)
serve2()
"""

    async def go() -> None:
        client = stub(tmp_path, body)
        await client.start()
        try:
            assert (await client.call("t", {})).text == "ok\n!"
        finally:
            await client.close()

    asyncio.run(go())
    answers = (tmp_path / "stub.py.log").read_text().splitlines()
    assert '"result": {}' in answers[0] and '"error"' in answers[1] and "-32601" in answers[1]


def test_the_tool_annotations_decide_what_needs_no_approval(tmp_path):
    body = PRELUDE + """
TOOLS = [
  {"name": "a", "inputSchema": {"type": "object"}, "annotations": {"readOnlyHint": True}},
  {"name": "b", "inputSchema": {"type": "object"}, "annotations": {"destructiveHint": False, "openWorldHint": False}},
  {"name": "c", "inputSchema": {"type": "object"}},
  {"name": "d", "inputSchema": {"type": "object"}, "annotations": {"destructiveHint": True, "openWorldHint": False}},
]
def handle(m):
    if m["method"] == "initialize": INIT(m)
    elif m["method"] == "tools/list": send({"jsonrpc": "2.0", "id": m["id"], "result": {"tools": TOOLS}})
serve(handle)
"""

    async def go() -> None:
        client = stub(tmp_path, body)
        await client.start()
        try:
            assert [(t.name, t.read_only, t.harmless) for t in client.tools] == [("a", True, True), ("b", False, True), ("c", False, False), ("d", False, False)]
        finally:
            await client.close()

    asyncio.run(go())
