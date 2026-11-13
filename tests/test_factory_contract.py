"""Wire details that are easy to confuse with ACP or Python SDK field names, without importing either SDK."""

import asyncio
import json
from types import SimpleNamespace

from desktop.core.attachments import Attachment
from desktop.core.handoff import Handoff
from desktop.core.memory.launch import McpCommand
from desktop.core.session_state import SessionState
from desktop.core.transcript import TurnStatus
from tests.fakes import until
from tests.support import tiny_png
from tests.test_factory import engine_for


def test_native_mcp_and_attachments_use_wire_aliases_and_handover_is_once(tmp_path):
    async def scenario():
        engine, _, project = engine_for(tmp_path)
        engine.connections = SimpleNamespace(mcp_commands=lambda _project: [McpCommand("local-tools", "python", ("-m", "demo_mcp"), {})])
        image = Attachment("image", "preview.png", tiny_png(), "image/png")
        pdf = Attachment("pdf", "guide.pdf", b"%PDF-1.7\n", "application/pdf")
        prefix = "carry the previous exchange"
        carried = Handoff(prefix, 2, 2, len(prefix), False, attachments=(image,))
        controller = await engine.open_new(project, model="demo-model", handoff=carried)
        try:
            assert controller.send_prompt("first", (pdf,))
            await until(lambda: controller.state is SessionState.READY)
            assert controller.handoff is None
            assert controller.send_prompt("second")
            await until(lambda: controller.state is SessionState.READY)
            rows = [json.loads(line) for line in (tmp_path / "peer/calls.jsonl").read_text().splitlines()]
            start = next(row["params"] for row in rows if row["method"] == "droid.initialize_session")
            assert start["mcpServers"] == [{"name": "local-tools", "command": "python", "args": ["-m", "demo_mcp"], "env": {}}]
            messages = [row["params"] for row in rows if row["method"] == "droid.add_user_message"]
            assert messages[0]["text"].startswith(prefix) and messages[0]["images"][0]["mediaType"] == "image/png"
            assert messages[0]["files"][0]["mediaType"] == "application/pdf"
            assert prefix not in messages[1]["text"] and "images" not in messages[1] and "files" not in messages[1]
        finally:
            await engine.close_all()
    asyncio.run(scenario())


def test_native_error_completion_is_not_presented_as_a_success(tmp_path):
    async def scenario():
        engine, _, project = engine_for(tmp_path)
        controller = await engine.open_new(project, model="demo-model")
        try:
            controller.send_prompt("[hold] wait")
            await asyncio.sleep(0.05)
            controller._notification("droid.session_notification", {"sessionId": controller.session_id, "notification": {"type": "agent_turn_completed", "reason": "model_authentication_failed", "tokenUsage": {"inputTokens": 0, "outputTokens": 0}}})
            assert controller.state is SessionState.READY
            assert controller.transcript.turns[-1].status is TurnStatus.FAILED
        finally:
            await engine.close_all()
    asyncio.run(scenario())


def test_an_unconfirmed_stop_cannot_make_the_connection_ready_again(tmp_path, monkeypatch):
    async def scenario():
        engine, _, project = engine_for(tmp_path)
        controller = await engine.open_new(project, model="demo-model")
        try:
            assert controller.send_prompt("[hold] wait")
            await until(lambda: not controller._tasks)
            request = controller._client.request
            async def fail_stop(method, params, **kwargs):
                if method == "droid.interrupt_session":
                    raise TimeoutError("local peer did not confirm")
                return await request(method, params, **kwargs)
            monkeypatch.setattr(controller._client, "request", fail_stop)
            await controller.interrupt()
            assert controller.state is SessionState.DEAD
            assert not controller.send_prompt("must wait for reconnect")
            assert any(block.kind == "error" and block.recoverable for block in controller.transcript.turns[-1].blocks)
        finally:
            await engine.close_all()
    asyncio.run(scenario())


def test_an_unknown_send_result_requires_reconnect_instead_of_reusing_the_cli(tmp_path, monkeypatch):
    async def scenario():
        engine, _, project = engine_for(tmp_path)
        controller = await engine.open_new(project, model="demo-model")
        try:
            request = controller._client.request
            async def fail_send(method, params, **kwargs):
                if method == "droid.add_user_message":
                    raise TimeoutError("local peer did not acknowledge")
                return await request(method, params, **kwargs)
            monkeypatch.setattr(controller._client, "request", fail_send)
            assert controller.send_prompt("uncertain")
            await until(lambda: not controller._tasks)
            assert controller.state is SessionState.DEAD
            assert not controller.send_prompt("must wait for reconnect")
        finally:
            await engine.close_all()
    asyncio.run(scenario())
