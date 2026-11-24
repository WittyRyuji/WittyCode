"""Guarded Factory seats, no accounts or model inference."""

import asyncio
import json
import sys
from types import SimpleNamespace

import pytest

from desktop.core import factory_guard as guard
from desktop.core.factory import FactoryEngine
from desktop.core.fusion.acp import AcpRun


def site_policy():
    return {"strictEnabledPlugins": True, "strictKnownMarketplaces": [],
            "mcpPolicy": {"enabled": True, "allowlist": ["desktop.core.fusion.acp_server"]}}


def tool_manifest():
    return SimpleNamespace(known=lambda n: n == "echo", declarations=lambda: [
        {"name": "echo", "description": "Test tool", "parameters": {"type": "object", "properties": {}}}])


@pytest.mark.parametrize("changes", [None, {"hooks": {"SessionStart": []}}, {"strictEnabledPlugins": False},
                                      {"mcpPolicy": {"enabled": True, "allowlist": []}}])
def test_fusion_refuses_before_creating_a_client_or_starting_mcp(tmp_path, monkeypatch, changes):
    engine = FactoryEngine(SimpleNamespace(factory_path="", factory_enabled=True), tmp_path / "index")
    project = tmp_path / "project"
    policy = tmp_path / "site.json"
    if changes is not None:
        policy.write_text(json.dumps({**site_policy(), **changes}))
    monkeypatch.setattr(guard, "_SYSTEM_POLICY", policy)
    monkeypatch.setattr(engine, "client", lambda *a, **kw: pytest.fail("standalone transport is not isolated"))
    monkeypatch.setattr(engine, "binary", lambda: pytest.fail("must reject unsafe configuration before discovery"))
    with pytest.raises(guard.FactoryGuardUnavailable, match="机器托管策略"):
        engine.guarded_client(project, model="demo", bridge=AcpRun(None))
    assert not (tmp_path / "peer").exists()


@pytest.mark.parametrize(("case", "error"), [("same", None), ("remote", "组织策略来源"),
    ("different-file", "组织策略来源"), ("missing-root", "SYSTEMROOT"), ("empty-root", "SYSTEMROOT")])
def test_child_environment_must_match_the_verified_site(tmp_path, monkeypatch, case, error):
    policy = tmp_path / "site.json"
    policy.write_text(json.dumps(site_policy()))
    root = tmp_path / "system"
    local = "FACTORY_ORG_MANAGED_SETTINGS_LOCAL_PATH"
    env = {"SYSTEMROOT": str(root), "COMSPEC": str(root / "System32/cmd.exe"), local: str(policy)}
    if case == "remote":  # Even a matching local file does not authorize a second source.
        env["FACTORY_ORG_MANAGED_SETTINGS_URL"] = "https://policy.invalid/settings.json"
    elif case == "different-file":
        other = tmp_path / "other.json"
        other.write_bytes(policy.read_bytes())  # Equal contents are not the same policy source.
        env[local] = str(other)
    elif case == "missing-root":
        del env["SYSTEMROOT"]
    elif case == "empty-root":
        env["SYSTEMROOT"] = ""
    original = env.copy()
    monkeypatch.setattr(guard.os, "environ", env)
    monkeypatch.setattr(guard.sys, "platform", "win32")
    monkeypatch.setattr(guard, "_SYSTEM_POLICY", policy)
    home = tmp_path / "child"
    if error:
        with pytest.raises(guard.FactoryGuardUnavailable, match=error):
            guard.guarded_factory_client(None, None)
        with pytest.raises(guard.FactoryGuardUnavailable, match=error):
            guard._environment(home)
        assert not home.exists()
    else:
        guard._check_site()
        assert guard._environment(home)[local] == str(policy)
    assert env == original


def test_no_credentials_never_falls_back_to_the_desktop_profile(monkeypatch):
    monkeypatch.setattr(guard, "_check_site", lambda: None)
    monkeypatch.delenv("FACTORY_API_KEY", raising=False)
    with pytest.raises(guard.FactoryGuardUnavailable, match="FACTORY_API_KEY"):
        guard.guarded_factory_client(None, None)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows guarded launch")
def test_unverified_version_stops_before_mcp_or_initialize(tmp_path, monkeypatch):
    monkeypatch.setattr(guard, "_check_site", lambda: None)
    monkeypatch.setattr(guard, "_check_scratch", lambda *args: None)
    monkeypatch.setattr(guard, "_environment", lambda home: {})
    bridge = AcpRun(None)
    bridge.tools = tool_manifest()
    # A real local --version subprocess but not a Factory executable. No policy or account is touched.
    seat = guard._Seat(SimpleNamespace(binary=lambda: sys.executable), tmp_path / "project", "demo", "", bridge, None, tmp_path)
    async def go():
        with pytest.raises(guard.FactoryGuardUnavailable, match="版本"):
            async with seat:
                pytest.fail("unverified CLI version")
    asyncio.run(go())
    assert seat._client is None and seat.server is None and not any(tmp_path.iterdir())


@pytest.mark.parametrize(("name", "allowed"), [("wittycode-fusion___echo", True), ("mcp__wittycode-fusion__echo", True),
    ("ToolSearch", False), ("Read", False), ("Execute", False), ("unrelated___echo", False), ("wittycode-fusion___other", False)])
def test_pretooluse_uses_exact_live_bridge_manifest(monkeypatch, name, allowed):
    bridge = AcpRun(None)
    bridge.tools, bridge.active = tool_manifest(), True
    monkeypatch.setattr(guard, "_check_site", lambda: None)
    monkeypatch.setattr(guard.acp_server, "request", lambda m, p: asyncio.run(bridge.dispatch(m, p)))
    payload = {"hook_event_name": "PreToolUse", "tool_name": name, "tool_input": {}}
    assert (guard._hook(payload) == 0) is allowed
    bridge.active = False
    assert guard._hook(payload) == 2


class Wire:
    """Only model-free request/notification ordering; the installed-CLI test covers the actual handshake."""
    running = True

    def __init__(self):
        self.called = []
        self.sent = asyncio.Event()
        self.allowed = [("mcp_wittycode-fusion_echo", "wittycode-fusion___echo"), ("tool-search-cli", "ToolSearch")]

    async def request(self, method, params, *, timeout):
        self.called.append((method, params))
        if method == "droid.list_mcp_servers":
            return {"servers": [{"source": "user", "name": "wittycode-fusion", "status": "connected"}]}
        if method == "droid.list_tools":
            return {"tools": [{"id": ident, "llmId": name, "currentlyAllowed": True} for ident, name in self.allowed]}
        if method == "droid.add_user_message":
            self.sent.set()
        return {}

    async def close(self):
        self.running = False


def opened(tmp_path, monkeypatch):
    monkeypatch.setattr(guard, "_check_site", lambda: None)  # Only in the wire ordering tests below.
    bridge = AcpRun(None)
    bridge.watcher, bridge.loop = SimpleNamespace(current=True, begin_step=lambda: None), object()
    bridge.tools = tool_manifest()
    seat = guard._Seat(None, tmp_path, "demo", "", bridge, None, tmp_path)
    seat._client, seat.names, seat.session, seat.owns_bridge, seat.ready = Wire(), ("echo",), "test-session", True, True
    bridge.client, bridge.session = seat, seat.session
    return seat


@pytest.mark.parametrize("failure", ["no-hook", "extra-native", "lookalike-id", "lost-private-tool"])
def test_no_prompt_until_an_exclusive_manifest_and_hook_are_confirmed(tmp_path, monkeypatch, failure):
    async def go():
        seat = opened(tmp_path, monkeypatch)
        if failure == "no-hook":
            seat.ready = False  # Session ID already received but guard handshake has not finished.
        elif failure == "extra-native":
            seat._client.allowed.append(("execute-cli", "Execute"))
        elif failure == "lookalike-id":
            seat._client.allowed[0] = ("external", "wittycode-fusion___echo")
        else:
            seat._client.allowed.pop(0)
        with pytest.raises(RuntimeError):
            await seat.prompt("inert")
        assert not seat._client.sent.is_set() and not seat.bridge.active
        await seat.close()
    asyncio.run(go())


@pytest.mark.parametrize("terminal", ["completed", "cancel", "lost", "error", "boundary"])
def test_ack_is_not_completion_and_only_private_tool_can_be_approved(tmp_path, monkeypatch, terminal):
    async def go():
        seat = opened(tmp_path, monkeypatch)
        turn = asyncio.create_task(seat.prompt("inert"))
        await asyncio.wait_for(seat._client.sent.wait(), 1)
        assert not turn.done() and seat.bridge.active
        for name in ("wittycode-fusion___echo", "ToolSearch", "Execute", "other___echo"):
            reply = await seat._request("droid.request_permission", {"toolUses": [{"toolUse": {"name": name}}],
                                      "options": [{"value": "proceed_once"}, {"value": "proceed_always"}]}, "test")
            assert reply["selectedOption"] == ("proceed_once" if name == "wittycode-fusion___echo" else "cancel")
        if terminal == "cancel":
            await seat.cancel()
            with pytest.raises(asyncio.CancelledError):
                await turn
        elif terminal == "lost":
            seat._lost(None)
            with pytest.raises(RuntimeError):
                await turn
        else:
            if terminal == "boundary":
                seat.bridge.boundary = "step_limit"
                seat.bridge._stop()
                await asyncio.sleep(0)
            event = {"type": "error"} if terminal == "error" else {"type": "agent_turn_completed",
                        "reason": "interrupted" if terminal == "boundary" else "completed"}
            seat._notify("droid.session_notification", {"sessionId": "other", "notification": event})
            assert not turn.done()
            seat._notify("droid.session_notification", {"sessionId": seat.session, "notification": event})
            if terminal == "error":
                with pytest.raises(RuntimeError):
                    await turn
            else:
                assert await turn == event
        assert not seat._client.running and not seat.bridge.active
        interrupts = [p for m, p in seat._client.called if m == "droid.interrupt_session"]
        assert interrupts == ([{}] if terminal in ("boundary", "cancel") else [])
        with pytest.raises(RuntimeError):
            await seat.prompt("must not replay")
    asyncio.run(go())


def test_factory_bridge_checks_permission_and_checkpoints_before_returning_output():
    from desktop.core.harness.loop import ASK, Observer, Outcome, Verdict

    events = []
    class Watcher(Observer):
        current = True
        answer = "cancel"

        async def before_tools(self):
            return True

        async def ask(self, call, verdict):
            events.append("asked")
            return self.answer

        async def retain_output(self, call, outcome):
            events.append("retained")

    async def execute(call):
        events.append("executed")
        return Outcome("inert result")

    async def go():
        tools, watcher = tool_manifest(), Watcher()
        tools.run, tools.check = execute, lambda call: Verdict(ASK)
        host = SimpleNamespace(model="demo", turn_step_limit=lambda: 4, _save_now=lambda *, required: events.append("saved"))
        bridge = guard.factory_bridge(host, tools, watcher, [], "test")
        bridge.active = True
        params = {"name": "echo", "arguments": {}, "bridgeCallId": "first"}
        await bridge.dispatch("tools/call", params)
        assert events == ["asked", "retained", "saved"]
        watcher.answer = "proceed_once"
        params["bridgeCallId"] = "second"
        result = await bridge.dispatch("tools/call", params)
        assert result["content"] == [{"type": "text", "text": "inert result"}]
        assert events[3:] == ["asked", "executed", "retained", "saved"]
        assert await bridge.dispatch("tools/call", params) == result and events.count("executed") == 1
        assert bridge.steps == 2
    asyncio.run(go())


def test_scratch_inside_project_or_with_inherited_config_is_not_started(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    with pytest.raises(guard.FactoryGuardUnavailable):
        guard._check_scratch(project, project)
    base = tmp_path / "other" / "scratch"
    base.mkdir(parents=True)
    (base.parent / ".factory").mkdir()
    assert guard._scratch_ancestors(base) == (base, *base.parents)  # Production does not stop at the runner's root.
    with pytest.raises(guard.FactoryGuardUnavailable):
        guard._check_scratch(base, project)


@pytest.mark.parametrize("marker", [".git", ".factory", ".agents", ".claude"])
def test_offline_peer_still_rejects_real_config_inside_its_test_machine(tmp_path, monkeypatch, marker):
    from tests.fusion_backend_support import factory_peer

    factory_peer(tmp_path, monkeypatch)
    base, project = tmp_path / "parent" / "scratch", tmp_path / "project"
    base.mkdir(parents=True)
    guard._check_scratch(base, project)
    (base.parent / marker).mkdir()
    with pytest.raises(guard.FactoryGuardUnavailable, match="代理配置"):
        guard._check_scratch(base, project)
