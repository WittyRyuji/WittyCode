"""The ACP client: every message names the protocol, answers arrive in order with the notifications, a process that exits
leaves no answer behind."""

from __future__ import annotations

import asyncio
import sys
import textwrap

from desktop.core.devin.errors import DevinProcessError
from desktop.core.devin.rpc import AcpClient

ECHO = textwrap.dedent(
    """
    import json, sys
    for raw in sys.stdin:
        message = json.loads(raw)
        if message.get("method") == "ask":
            sys.stdout.write(json.dumps({"jsonrpc": "2.0", "method": "note", "params": {"seen": message}}) + "\\n")
            sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": {"seen": message}}) + "\\n")
            sys.stdout.flush()
    """
)
ANSWER_AND_LEAVE = textwrap.dedent(
    """
    import json, sys
    message = json.loads(sys.stdin.readline())
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "method": "note", "params": {}}) + "\\n")
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": {"fine": True}}) + "\\n")
    sys.stdout.flush()
    """
)


def run(coroutine):
    return asyncio.run(coroutine)


def test_every_message_carries_the_protocol_version():
    async def scenario():
        seen: list[dict] = []
        client = AcpClient([sys.executable, "-c", ECHO], on_notification=lambda method, params: seen.append(params))
        await client.start()
        try:
            result = await client.request("ask", {"x": 1}, timeout=10)
            assert result["seen"]["jsonrpc"] == "2.0" and result["seen"]["params"] == {"x": 1}
            await client.notify("ask-not", None)
        finally:
            await client.close()

    run(scenario())


def test_an_answer_is_handled_after_the_notifications_that_came_before_it():
    async def scenario():
        order: list[str] = []

        async def slow(method, params):
            await asyncio.sleep(0.1)
            order.append("note")

        client = AcpClient([sys.executable, "-c", ECHO], on_notification=slow)
        await client.start()
        try:
            await client.request("ask", None, timeout=10)
            order.append("answer")
            assert order == ["note", "answer"], "the turn's last words must be in before the end of the turn is known"
        finally:
            await client.close()

    run(scenario())


def test_what_a_process_said_before_it_left_is_delivered_and_only_then_are_the_waiting_failed():
    async def scenario():
        notes: list[str] = []
        exits: list[object] = []

        async def slow(method, params):
            await asyncio.sleep(0.3)  # (the answer sits behind this one when the stream ends)
            notes.append(method)

        client = AcpClient([sys.executable, "-c", ANSWER_AND_LEAVE], on_notification=slow, on_exit=exits.append)
        await client.start()
        try:
            first = asyncio.ensure_future(client.request("ask", None, timeout=10))
            assert await first == {"fine": True}, "its answer was written before it left: it is the answer"
            assert notes == ["note"]
            try:
                await client.request("ask", None, timeout=10)
            except DevinProcessError:
                pass
            else:
                raise AssertionError("a process that is gone answers nothing")
            deadline = asyncio.get_running_loop().time() + 5
            while not exits and asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(0.01)
            assert len(exits) == 1
        finally:
            await client.close()

    run(scenario())
