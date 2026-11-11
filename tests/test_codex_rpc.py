"""JsonRpcClient against small scripted servers and the fake app-server: framing, order, requests, death, close."""

from __future__ import annotations

import asyncio
import json
import sys
import textwrap
from pathlib import Path

import pytest

from desktop.core.codex import rpc
from desktop.core.codex.errors import CodexProcessError, CodexRpcError, CodexTimeoutError
from desktop.core.codex.rpc import CREATE_NO_WINDOW, JsonRpcClient
from tests.codex_support import fake_client, run


def script(tmp_path: Path, body: str) -> list[str]:
    path = tmp_path / "server.py"
    path.write_text("import json, sys, time\n" + textwrap.dedent(body), encoding="utf-8")
    return [sys.executable, str(path)]


def test_the_handshake_and_errors_look_like_the_real_server(tmp_path):
    async def scenario():
        client = fake_client(tmp_path / "home")
        await client.start()
        with pytest.raises(CodexRpcError) as early:
            await client.request("account/read", {"refreshToken": False})
        assert (early.value.code, early.value.message) == (-32600, "Not initialized")
        result = await client.request("initialize", {"clientInfo": {"name": "wittycode", "title": "WittyCode", "version": "0"}, "capabilities": None})
        assert set(result) == {"userAgent", "codexHome", "platformFamily", "platformOs"}
        await client.notify("initialized")
        with pytest.raises(CodexRpcError, match="Already initialized"):
            await client.request("initialize", {"clientInfo": {"name": "x", "version": "1"}})
        with pytest.raises(CodexRpcError) as unknown:
            await client.request("no/such/method", {})
        assert unknown.value.code == -32600 and "unknown variant" in unknown.value.message
        assert client.running
        await client.close()
        assert not client.running and client.exit_code == 0
        with pytest.raises(CodexProcessError):
            await client.request("account/read", {})
        sent = [json.loads(line) for line in (tmp_path / "home" / "received.jsonl").read_text(encoding="utf-8").splitlines()]
        assert all("jsonrpc" not in message for message in sent)
        assert {"method": "initialized"} in sent and all("params" in m for m in sent if m.get("method") == "account/read")

    run(scenario())


def test_notifications_keep_their_order_even_with_slow_async_handlers(tmp_path):
    argv = script(
        tmp_path,
        """
        for n in range(200):
            sys.stdout.write(json.dumps({"method": "tick", "params": {"n": n}}) + "\\n")
        sys.stdout.flush()
        sys.stdin.readline()
        """,
    )

    async def scenario():
        seen: list[int] = []

        async def on_notification(method, params):
            if params["n"] % 7 == 0:
                await asyncio.sleep(0.002)
            seen.append(params["n"])

        client = JsonRpcClient(argv, on_notification=on_notification)
        await client.start()
        loop = asyncio.get_running_loop()
        end = loop.time() + 10
        while len(seen) < 200 and loop.time() < end:
            await asyncio.sleep(0.01)
        await client.close()
        assert seen == list(range(200))

    run(scenario())


def test_a_line_of_several_megabytes_arrives_whole(tmp_path):
    argv = script(
        tmp_path,
        """
        text = "x" * (3 * 1024 * 1024) + "é"
        sys.stdout.write(json.dumps({"method": "big", "params": {"text": text}}, ensure_ascii=False) + "\\n")
        sys.stdout.write(json.dumps({"method": "small", "params": {}}) + "\\n")
        sys.stdout.flush()
        sys.stdin.readline()
        """,
    )

    async def scenario():
        seen: list[tuple[str, int]] = []
        client = JsonRpcClient(argv, on_notification=lambda method, params: seen.append((method, len(params.get("text", "")))))
        await client.start()
        loop = asyncio.get_running_loop()
        end = loop.time() + 10
        while len(seen) < 2 and loop.time() < end:
            await asyncio.sleep(0.01)
        await client.close()
        assert seen == [("big", 3 * 1024 * 1024 + 1), ("small", 0)]

    run(scenario())


def test_process_death_fails_pending_requests_and_is_reported_once(tmp_path):
    argv = script(
        tmp_path,
        """
        sys.stdin.readline()
        sys.stderr.write("boom: the reason\\n")
        sys.stderr.flush()
        sys.exit(5)
        """,
    )

    async def scenario():
        exits: list[CodexProcessError] = []
        client = JsonRpcClient(argv, on_exit=exits.append)
        await client.start()
        with pytest.raises(CodexProcessError) as error:
            await client.request("thread/start", {})
        assert error.value.exit_code == 5 and not error.value.closed
        loop = asyncio.get_running_loop()
        end = loop.time() + 5
        while not exits and loop.time() < end:
            await asyncio.sleep(0.01)
        assert len(exits) == 1 and exits[0].exit_code == 5 and "boom: the reason" in exits[0].stderr
        assert not client.running
        with pytest.raises(CodexProcessError):
            await client.request("thread/start", {})
        await client.close()
        assert len(exits) == 1, "closing a dead client is not a second death"

    run(scenario())


def test_server_requests_are_answered_with_their_id(tmp_path):
    argv = script(
        tmp_path,
        """
        def ask(rid, method):
            sys.stdout.write(json.dumps({"id": rid, "method": method, "params": {"threadId": "t"}}) + "\\n")
            sys.stdout.flush()
            reply = json.loads(sys.stdin.readline())
            sys.stdout.write(json.dumps({"method": "echo", "params": reply}) + "\\n")
            sys.stdout.flush()
        ask("abc", "item/fileChange/requestApproval")
        ask(7, "refuse")
        ask(8, "crash")
        ask(9, "unknown")
        sys.stdin.readline()
        """,
    )

    async def scenario():
        echoes: list[dict] = []

        async def on_request(method, params, request_id):
            if method == "refuse":
                raise CodexRpcError(-32000, "nope")
            if method == "crash":
                raise ValueError("bad")
            if method == "unknown":
                raise CodexRpcError(-32601, "not handled")
            return {"decision": "accept", "id": request_id}

        client = JsonRpcClient(argv, on_notification=lambda method, params: echoes.append(params), on_request=on_request)
        await client.start()
        loop = asyncio.get_running_loop()
        end = loop.time() + 10
        while len(echoes) < 4 and loop.time() < end:
            await asyncio.sleep(0.01)
        await client.close()
        assert echoes[0] == {"id": "abc", "result": {"decision": "accept", "id": "abc"}}
        assert echoes[1] == {"id": 7, "error": {"code": -32000, "message": "nope"}}
        assert echoes[2]["id"] == 8 and echoes[2]["error"]["code"] == -32603 and "ValueError" in echoes[2]["error"]["message"]
        assert echoes[3] == {"id": 9, "error": {"code": -32601, "message": "not handled"}}

    run(scenario())


def test_a_notification_handler_can_make_requests_itself(tmp_path):
    argv = script(
        tmp_path,
        """
        sys.stdout.write(json.dumps({"method": "hello", "params": {}}) + "\\n")
        sys.stdout.flush()
        for line in sys.stdin:
            message = json.loads(line)
            sys.stdout.write(json.dumps({"id": message["id"], "result": {"pong": message["method"]}}) + "\\n")
            sys.stdout.flush()
        """,
    )

    async def scenario():
        answers: list[dict] = []
        client: JsonRpcClient

        async def on_notification(method, params):
            answers.append(await client.request("ping", {}, timeout=5))

        client = JsonRpcClient(argv, on_notification=on_notification)
        await client.start()
        loop = asyncio.get_running_loop()
        end = loop.time() + 10
        while not answers and loop.time() < end:
            await asyncio.sleep(0.01)
        await client.close()
        assert answers == [{"pong": "ping"}]

    run(scenario())


def test_requests_time_out_and_late_answers_are_dropped(tmp_path):
    argv = script(
        tmp_path,
        """
        message = json.loads(sys.stdin.readline())
        time.sleep(0.5)
        sys.stdout.write(json.dumps({"id": message["id"], "result": "late"}) + "\\n")
        sys.stdout.flush()
        message = json.loads(sys.stdin.readline())
        sys.stdout.write(json.dumps({"id": message["id"], "result": "second"}) + "\\n")
        sys.stdout.flush()
        sys.stdin.readline()
        """,
    )

    async def scenario():
        client = JsonRpcClient(argv)
        await client.start()
        with pytest.raises(CodexTimeoutError):
            await client.request("slow", {}, timeout=0.1)
        await asyncio.sleep(0.6)
        assert await client.request("next", {}, timeout=5) == "second"
        await client.close()

    run(scenario())


def test_close_ends_a_server_that_ignores_its_stdin(tmp_path):
    argv = script(
        tmp_path,
        """
        sys.stdout.write(json.dumps({"method": "ready", "params": {}}) + "\\n")
        sys.stdout.flush()
        time.sleep(60)
        """,
    )

    async def scenario():
        ready = asyncio.Event()
        client = JsonRpcClient(argv, on_notification=lambda method, params: ready.set())
        await client.start()
        await asyncio.wait_for(ready.wait(), 10)
        started = asyncio.get_running_loop().time()
        await client.close(grace=0.3)
        assert asyncio.get_running_loop().time() - started < 8
        assert client.exit_code is not None
        assert not client._tasks and not client._serving
        assert asyncio.all_tasks() == {asyncio.current_task()}

    run(scenario())


def test_close_leaves_no_task_behind_and_is_idempotent(tmp_path):
    async def scenario():
        client = fake_client(tmp_path / "home")
        await client.start()
        await client.request("initialize", {"clientInfo": {"name": "t", "version": "0"}, "capabilities": None})
        await asyncio.gather(client.close(), client.close())
        await client.close()
        assert client.exit_code == 0
        assert asyncio.all_tasks() == {asyncio.current_task()}

    run(scenario())


def test_a_missing_executable_is_a_process_error(tmp_path):
    async def scenario():
        client = JsonRpcClient([str(tmp_path / "nope.exe"), "app-server"])
        with pytest.raises(CodexProcessError):
            await client.start()

    run(scenario())


@pytest.mark.skipif(sys.platform != "win32", reason="CREATE_NO_WINDOW is a Windows flag")
def test_the_process_gets_no_console_window(tmp_path, monkeypatch):
    seen: dict = {}
    real = asyncio.create_subprocess_exec

    async def spy(*args, **kwargs):
        seen.update(kwargs)
        return await real(*args, **kwargs)

    monkeypatch.setattr(rpc.asyncio, "create_subprocess_exec", spy)

    async def scenario():
        client = fake_client(tmp_path / "home")
        await client.start()
        await client.close()

    run(scenario())
    assert seen["creationflags"] == CREATE_NO_WINDOW
