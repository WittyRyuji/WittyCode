"""Wire details that are easy to confuse with ACP or Python SDK field names, without importing either SDK."""

import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from desktop.core.attachments import Attachment
from desktop.core.factory import EXEC_ARGS, FactoryClient, FactoryEngine
from desktop.core.handoff import Handoff
from desktop.core.memory.launch import McpCommand
from desktop.core.session_state import SessionState
from desktop.core.transcript import TurnStatus
from tests.fakes import until
from tests.support import tiny_png
from tests.test_factory import engine_for


@pytest.mark.skipif(os.environ.get("WITTYCODE_TEST_FACTORY_118") != "1", reason="opt-in installed CLI 1.18.0 probe")
def test_installed_cli_restricts_tools_but_starts_foreign_mcp_before_prompt(tmp_path):
    """Same-home negative control: a tool allowlist does not isolate startup. No auth, message or title request."""
    assert sys.platform == "win32" and tmp_path.is_relative_to(Path(os.environ["WITTYCODE_TEST_WORK"]))
    cli = FactoryEngine(SimpleNamespace(factory_path=""), tmp_path / "index").binary()
    assert cli
    home, work = tmp_path / "home", tmp_path / "project"
    work.mkdir()
    (home / ".factory").mkdir(parents=True)
    _require_unmanaged_probe_machine()
    env = {k: os.environ[k] for k in ("SYSTEMROOT", "WINDIR", "COMSPEC", "TEMP", "TMP", "PATHEXT", "PROGRAMFILES",
                                      "PROGRAMDATA", "PROGRAMW6432", "ALLUSERSPROFILE") if k in os.environ}
    env.update(HOME=str(home), USERPROFILE=str(home), FACTORY_HOME_OVERRIDE=str(home), APPDATA=str(home / "roaming"),
               LOCALAPPDATA=str(home / "local"), PATH=str(Path(env["SYSTEMROOT"]) / "System32"))
    version = subprocess.run([cli, "--version"], env=env, cwd=work, capture_output=True, stdin=subprocess.DEVNULL,
                             text=True, check=True, timeout=10).stdout.strip()
    assert version == "1.18.0", "Re-audit the new CLI in isolation before running its wire probe"
    marker = tmp_path / "foreign-mcp-started"
    canary = {"type": "stdio", "command": sys.executable, "args": ["-I", "-c", f"from pathlib import Path; Path({str(marker)!r}).touch()"]}
    (home / ".factory/mcp.json").write_text(json.dumps({"mcpServers": {"canary": canary}}), encoding="utf-8")
    runtime = tmp_path / "runtime.json"
    runtime.write_text(json.dumps({"hooks": {"hooksDisabled": True}, "mcpPolicy": {"enabled": False}}), encoding="utf-8")
    peer = tmp_path / "manifest.py"
    peer.write_text('''import json, sys
for line in sys.stdin:
    m = json.loads(line)
    if "id" not in m: continue
    if m["method"] == "initialize":
        result = {"protocolVersion": m["params"]["protocolVersion"], "capabilities": {"tools": {}}, "serverInfo": {"name": "demo", "version": "1"}}
    elif m["method"] == "tools/list":
        result = {"tools": [{"name": "echo", "description": "Inert tool", "inputSchema": {"type": "object"}}]}
    else: raise SystemExit("unexpected tool operation")
    print(json.dumps({"jsonrpc": "2.0", "id": m["id"], "result": result}), flush=True)
''', encoding="utf-8")

    async def probe():
        client = FactoryClient([cli, *EXEC_ARGS, "--settings", str(runtime)], cwd=work, env=env)
        try:
            await client.start()
            only = ["MCP:local-bridge/echo"]
            result = await client.request("droid.initialize_session", {"machineId": "test", "cwd": str(work),
                "mcpServers": [{"name": "local-bridge", "command": sys.executable, "args": ["-I", str(peer)], "env": {}}],
                "restrictToolIds": only, "toolExecutionMode": "direct_only", "disableBuiltinSkills": True,
                "interactionMode": "auto", "autonomyLevel": "off"}, timeout=30)
            assert result["settings"]["restrictToolIds"] == only
            await until(marker.exists, timeout=10)
            async with asyncio.timeout(15):
                while True:
                    tools = (await client.request("droid.list_tools", {}))["tools"]
                    allowed = {t["llmId"] for t in tools if t["currentlyAllowed"]}
                    if "local-bridge___echo" in allowed:
                        break
                    await asyncio.sleep(0.1)
            assert "Execute" not in allowed and "Read" not in allowed
            assert "ToolSearch" in allowed  # Not an exclusive tool set, even in direct_only mode.
            servers = (await client.request("droid.list_mcp_servers", {}))["servers"]
            assert any(s["name"] == "canary" and s["source"] == "user" for s in servers)
            await client.request("droid.interrupt_session", {}, timeout=5)  # Idle interrupt only; no real turn.
        finally:
            await client.close()
        assert not client.running

    asyncio.run(probe())


def _require_unmanaged_probe_machine():
    from desktop.core import factory_guard as guard
    if guard._SYSTEM_POLICY.exists() or any(os.environ.get(k) for k in (
            "FACTORY_API_KEY", "FACTORY_ORG_MANAGED_SETTINGS_LOCAL_PATH", "FACTORY_ORG_MANAGED_SETTINGS_URL")):
        pytest.skip("The no-auth fixture must not replace existing machine/endpoint policy or inherit credentials")


@pytest.mark.skipif(os.environ.get("WITTYCODE_TEST_FACTORY_118") != "1", reason="opt-in no-prompt installed CLI handshake")
@pytest.mark.parametrize(("tool_free", "missing"), [(False, ""), (True, ""), (False, "guard"), (False, "bridge")])
def test_private_profile_real_wire_handshake_without_user_or_project_mcp(tmp_path, monkeypatch, tool_free, missing):
    from desktop.core import factory_guard as guard
    from desktop.core.fusion.acp import AcpRun
    from tests.test_factory_guard import site_policy, tool_manifest

    _require_unmanaged_probe_machine()
    assert tmp_path.is_relative_to(Path(os.environ["WITTYCODE_TEST_WORK"]))
    engine = FactoryEngine(SimpleNamespace(factory_path="", factory_enabled=True), tmp_path / "index")
    cli = engine.binary()  # Locate before simulating a different desktop user's environment.
    monkeypatch.setattr(engine, "binary", lambda: cli)
    previous_home, project = tmp_path / "parent-home", tmp_path / "parent-project"
    for label, directory in (("user", previous_home), ("project", project)):
        (directory / ".factory").mkdir(parents=True)
        canary = {"type": "stdio", "command": sys.executable, "args": ["-I", "-c", f"from pathlib import Path; Path({str(tmp_path / label)!r}).touch()"]}
        (directory / ".factory/mcp.json").write_text(json.dumps({"mcpServers": {label: canary}}), encoding="utf-8")
    for k in ("HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA"):
        monkeypatch.setenv(k, str(previous_home))
    monkeypatch.chdir(project)
    # No system file was changed. Both the CLI and our preflight consume the same synthetic org fixture.
    # Only the test injects this source. Production requires the actual authoritative machine policy.
    policy = tmp_path / "managed.json"
    policy.write_text(json.dumps(site_policy()), encoding="utf-8")
    monkeypatch.setenv("FACTORY_ORG_MANAGED_SETTINGS_LOCAL_PATH", str(policy))
    monkeypatch.setattr(guard, "_SYSTEM_POLICY", policy)
    base = tmp_path / "scratch"
    base.mkdir()

    async def probe():
        bridge = AcpRun(SimpleNamespace())
        bridge.tools, bridge.tool_free = tool_manifest(), tool_free
        # Handshake only: test the transport below the production credential-presence check, without any key.
        seat = guard._Seat(engine, project, "claude-opus-5-5", "", bridge, None, base)
        if missing:
            dispatch = bridge.dispatch
            async def withhold(method, params):
                if method == {"guard": "guard", "bridge": "tools/list"}[missing]:
                    raise ValueError("receipt withheld by test host")
                return await dispatch(method, params)
            bridge.dispatch = withhold
            monkeypatch.setattr(guard, "_STARTUP_SECONDS", 3)
            with pytest.raises(guard.FactoryGuardUnavailable, match="未发送请求"):
                async with seat:
                    pytest.fail("unverified CLI must not become ready")
            assert not seat.ready and not seat.attempted
        else:
            async with seat:
                assert seat.session and not seat.attempted and bridge.guard_seen.is_set()
                assert bridge.bridge_seen.is_set() is (not tool_free)
                assert await seat._manifest_ready()
        assert not (tmp_path / "user").exists() and not (tmp_path / "project").exists()
        assert not any(base.iterdir()) and not seat._client.running
    asyncio.run(probe())


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
