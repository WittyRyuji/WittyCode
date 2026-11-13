"""MCP pagination, cancellation and rich result fallbacks against local stub servers."""
from __future__ import annotations

import asyncio
import json

from tests.test_harness_mcp import PRELUDE, stub


def test_tools_list_follows_pages_and_nontext_results_are_not_silently_empty(tmp_path):
    body = PRELUDE + '''
def handle(m):
    if m["method"] == "initialize": INIT(m)
    elif m["method"] == "tools/list":
        page = m.get("params", {}).get("cursor")
        result = {"tools": [{"name": "second" if page else "first", "inputSchema": {"type": "object"}}]}
        if not page: result["nextCursor"] = "page-two"
        send({"jsonrpc": "2.0", "id": m["id"], "result": result})
    elif m["method"] == "tools/call":
        send({"jsonrpc": "2.0", "id": m["id"], "result": {"content": [{"type": "image", "data": "not-a-real-image"}], "structuredContent": {"count": 2}}})
serve(handle)
'''
    async def go():
        client = stub(tmp_path, body)
        await client.start()
        try:
            assert [tool.name for tool in client.tools] == ["first", "second"]
            result = await client.call("second", {})
            assert "image" in result.text and "count" in result.text and "not-a-real-image" not in result.text
        finally:
            await client.close()
    asyncio.run(go())


def test_timeout_sends_cancel_notification_and_stderr_is_drained_without_logging_it(tmp_path):
    body = PRELUDE + '''
import os
def handle(m):
    if m["method"] == "initialize": INIT(m)
    elif m["method"] == "tools/list": send({"jsonrpc": "2.0", "id": m["id"], "result": {"tools": []}})
    elif m["method"] == "tools/call":
        sys.stderr.write("demo diagnostic\\n" * 10000); sys.stderr.flush()
    elif m["method"] == "notifications/cancelled":
        with open(sys.argv[0] + ".cancel.tmp", "w") as output: json.dump(m["params"], output)
        os.replace(sys.argv[0] + ".cancel.tmp", sys.argv[0] + ".cancel")
for line in sys.stdin:
    handle(json.loads(line))
'''
    async def go():
        client = stub(tmp_path, body)
        await client.start()
        try:
            result = await client.call("slow", {}, timeout=.1)
            assert result.is_error and "did not answer" in result.text
            for _ in range(100):
                if (tmp_path / "stub.py.cancel").exists(): break
                await asyncio.sleep(.01)
            sent = json.loads((tmp_path / "stub.py.cancel").read_text())
            assert isinstance(sent["requestId"], int)
            assert len(client.stderr_tail) <= 4096 and "demo diagnostic" in client.stderr_tail
        finally:
            await client.close()
    asyncio.run(go())
