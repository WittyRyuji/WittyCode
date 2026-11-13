"""Only temporary homes/configs and local peers: discovering a connection never executes it or changes its source."""

import asyncio
import json
from pathlib import Path
import sys

import pytest

from desktop.core.connections import ConnectionError, ConnectionStore
from desktop.core.harness import skills
from desktop.core.session_state import SessionState
from tests.app_support import ready_core
from tests.fake_responses import say, call, usage
from tests.fakes import until


@pytest.fixture
def rig(tmp_path, monkeypatch):
    home, project, profile = (tmp_path / name for name in ("home", "project", "profile"))
    for folder in (home, project, profile):
        folder.mkdir()
    monkeypatch.setattr(skills, "USER_HOME", home)
    monkeypatch.setenv("WITTYCODE_SETTINGS", str(profile / "settings.json"))
    return ConnectionStore(profile / "connections.json", home=home), home, project


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def mcp_script(folder):
    path = folder / "mcp_peer.py"
    path.write_text('''import json, sys
for line in sys.stdin:
 m=json.loads(line)
 if 'id' not in m: continue
 method=m.get('method')
 result={'protocolVersion':'2025-06-18','capabilities':{'tools':{}},'serverInfo':{'name':'demo','version':'1'}} if method=='initialize' else {'tools':[{'name':'echo','description':'demo tool','inputSchema':{'type':'object'},'annotations':{'readOnlyHint':True}}]} if method=='tools/list' else {'content':[{'type':'text','text':'local MCP answer'}]}
 print(json.dumps({'jsonrpc':'2.0','id':m['id'],'result':result}),flush=True)
''', encoding="utf-8")
    return path


def hook_script(folder):
    script = folder / "hook.py"
    script.write_text('''import json, pathlib, sys
payload=json.load(sys.stdin)
with (pathlib.Path(payload['cwd'])/'hook-events.jsonl').open('a',encoding='utf-8') as f: f.write(json.dumps(payload)+'\\n')
if len(sys.argv)>1 and sys.argv[1]=='deny': print(json.dumps({'hookSpecificOutput':{'permissionDecision':'deny'}}))
else: print(json.dumps({'hookSpecificOutput':{'additionalContext':'local hook context'}}))
''', encoding="utf-8")
    return script


def quote(path):
    return "'" + str(path).replace("'", "''") + "'"


def trust(store, file, project=""):
    preview = store.preview(str(file), str(project))
    store.commit(preview["preview"], True)
    return preview


def test_codex_toml_is_discovered_without_executing_or_copying_secrets(rig):
    store, home, project = rig
    file = home / ".codex" / "config.toml"
    file.parent.mkdir()
    file.write_text('[mcp_servers.demo]\ncommand = "python"\nargs = ["-m", "demo_mcp"]\nenv = { DEMO_VALUE = "${DEMO_VALUE}" }\n', encoding="utf-8")
    before = file.read_bytes()
    scanned = store.scan(str(project))
    assert len(scanned["items"]) == 1
    entry = scanned["items"][0]
    assert entry["kind"] == "mcp" and not entry["enabled"] and not entry["imported"]
    assert store.mcp_commands(project) == []
    assert file.read_bytes() == before and not store.file.exists()


def test_one_confirmation_imports_and_enables_the_whole_local_config(rig):
    store, _, project = rig
    peer = mcp_script(project)
    file = write_json(project / ".mcp.json", {"mcpServers": {"demo": {"command": sys.executable, "args": [str(peer)]}}})
    preview = store.preview(str(file), str(project))
    assert len(preview["commands"]) == 1
    with pytest.raises(ConnectionError, match="确认"):
        store.commit(preview["preview"], False)
    assert not store.file.exists()
    store.commit(preview["preview"], True)
    assert len(store.mcp_commands(project)) == 1
    restarted = ConnectionStore(store.file, home=store.home)
    assert len(restarted.mcp_commands(project)) == 1
    entry = restarted.active("mcp", project)[0]
    result = asyncio.run(restarted.test(entry["id"], project))
    assert result == {"ok": True, "text": "连接成功 · 1 个工具", "tools": ["echo"]}
    restarted.update(entry["id"], enabled=False, project=str(project))
    assert restarted.mcp_commands(project) == []


def test_source_change_after_preview_and_after_import_requires_review(rig):
    store, _, project = rig
    file = write_json(project / ".mcp.json", {"mcpServers": {"demo": {"command": "python", "args": ["one.py"]}}})
    preview = store.preview(str(file), str(project))
    write_json(file, {"mcpServers": {"demo": {"command": "python", "args": ["two.py"]}}})
    with pytest.raises(ConnectionError, match="发生变化"):
        store.commit(preview["preview"], True)
    trust(store, file, project)
    file.write_text(file.read_text() + "\n", encoding="utf-8")
    assert store.active("mcp", project) == []
    assert store.scan(str(project))["items"][0]["changed"]


def test_codex_plugin_imports_skills_hooks_and_mcp_with_parent_switch(rig):
    store, _, project = rig
    plugin = project / "demo-plugin"
    write_json(plugin / ".codex-plugin/plugin.json", {"name": "demo", "skills": "./skills"})
    skill = plugin / "skills/summarize-diff/SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("---\nname: summarize-diff\ndescription: Summarize a diff\n---\nUse the local diff.", encoding="utf-8")
    write_json(plugin / "hooks/hooks.json", {"hooks": {"PreToolUse": [{"matcher": "shell", "hooks": [{"type": "command", "command": "Write-Output ok"}]}]}})
    write_json(plugin / ".mcp.json", {"mcpServers": {"demo": {"command": "python", "args": ["${PLUGIN_ROOT}/server.py"]}}})
    trust(store, plugin, project)
    assert {e["kind"] for e in store.scan(str(project))["items"] if e["imported"]} == {"plugin", "skill", "hook", "mcp"}
    assert any(s.name == "summarize-diff" for s in skills.discover(project))
    assert store.mcp_commands(project)[0].args == (str(plugin) + "/server.py",)
    parent = store.active("plugin", project)[0]
    store.update(parent["id"], enabled=False, project=str(project))
    assert store.active("hook", project) == [] and store.mcp_commands(project) == []
    assert not any(s.name == "summarize-diff" for s in skills.discover(project))


def test_plugin_paths_cannot_escape_the_plugin_root(rig):
    store, _, project = rig
    root = project / "bad-plugin"
    write_json(root / ".codex-plugin/plugin.json", {"name": "bad", "skills": "../outside"})
    with pytest.raises(ConnectionError, match="越出"):
        store.preview(str(root))


def test_codex_skill_can_be_disabled_without_editing_it(rig):
    store, home, project = rig
    file = home / ".agents/skills/demo-skill/SKILL.md"
    file.parent.mkdir(parents=True)
    file.write_text("---\nname: demo-skill\ndescription: Local demo\n---\nDo the local task.", encoding="utf-8")
    before = file.read_bytes()
    entry = next(e for e in store.scan(str(project))["items"] if e["kind"] == "skill")
    assert entry["enabled"]
    store.update(entry["id"], enabled=False, project=str(project))
    assert skills.discover(project) == []
    assert file.read_bytes() == before


def test_invalid_or_remote_configs_are_explicitly_rejected(rig):
    store, _, project = rig
    for config in ({"url": "https://example.com/mcp"}, {"command": "python", "args": "bad"}, {"command": "python", "env": {"VALUE": "plaintext"}}):
        file = write_json(project / ".mcp.json", {"mcpServers": {"demo": config}})
        with pytest.raises(ConnectionError):
            store.preview(str(file))
    with pytest.raises(ConnectionError, match="本地"):
        store.preview(r"\\example\share\config.json")


def test_plaintext_secrets_never_reach_the_page_during_discovery(rig):
    store, home, project = rig
    file = write_json(home / ".codex/.mcp.json", {"mcpServers": {"demo": {"command": "python", "env": {"API_KEY": "sk-" + "demo123456789012345678901234567890"}}}})
    result = store.scan(str(project))
    assert result["items"] == [] and result["errors"]
    assert "demo123456" not in json.dumps(result)
    with pytest.raises(ConnectionError):
        store.preview(str(file))


def test_editing_configs_requires_confirmation_and_leaves_source_unchanged(rig):
    store, _, project = rig
    file = write_json(project / ".mcp.json", {"mcpServers": {"demo": {"command": "python", "args": []}}})
    trust(store, file, project)
    before = file.read_bytes()
    entry = store.active("mcp", project)[0]
    with pytest.raises(ConnectionError, match="确认"):
        store.update(entry["id"], config={"command": "python", "args": ["other.py"]}, project=str(project))
    store.update(entry["id"], config={"command": "python", "args": ["other.py"]}, confirmed=True, project=str(project))
    assert store.mcp_commands(project)[0].args == ("other.py",)
    assert file.read_bytes() == before


def test_command_hooks_run_only_after_import_and_can_deny(rig):
    store, _, project = rig
    script = hook_script(project)
    command = f"& {quote(sys.executable)} {quote(script)}"
    file = write_json(project / "hooks.json", {"hooks": {"UserPromptSubmit": [{"hooks": [{"type": "command", "command": command}]}]}})
    assert asyncio.run(store.hooks("UserPromptSubmit", project, {"prompt": "demo"})) == ""
    assert not (project / "hook-events.jsonl").exists()
    trust(store, file, project)
    assert asyncio.run(store.hooks("UserPromptSubmit", project, {"prompt": "demo"})) == "local hook context"
    write_json(file, {"hooks": {"PreToolUse": [{"matcher": "shell", "hooks": [{"type": "command", "command": command + " deny"}]}]}})
    trust(store, file, project)
    with pytest.raises(ConnectionError, match="拒绝"):
        asyncio.run(store.hooks("PreToolUse", project, {"tool_name": "shell"}))
    assert asyncio.run(store.hooks("PreToolUse", project, {"tool_name": "read_file"})) == ""


def test_real_own_loop_uses_imported_mcp_and_prompt_lifecycle_hooks(tmp_path, rig):
    store, _, project = rig
    peer, hook = mcp_script(project), hook_script(project)
    command = f"& {quote(sys.executable)} {quote(hook)}"
    config = {"mcpServers": {"demo": {"command": sys.executable, "args": [peer.name]}}, "hooks": {event: [{"hooks": [{"type": "command", "command": command}]}] for event in ("SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse", "Stop", "SessionEnd")}}
    file = write_json(project / "connections.json", config)
    trust(store, file)
    async def scenario():
        core, plan, _, _, _ = await ready_core(tmp_path)
        core.settings.add_project(project)
        try:
            cid = (await core.call("session.new", {"projectId": str(project)}))["id"]
            controller = core._by_cid[cid].controller
            plan.script(call("mcp__demo__echo", {}, call_id="local-call"), say("done", used=usage(100, 10)))
            await core.call("session.send", {"id": cid, "text": "use the local tool"})
            await until(lambda: controller.state is SessionState.READY, timeout=15)
            assert len(plan.requests) == 2
            assert "local hook context" in json.dumps(plan.requests[0]["input"])
            assert "local MCP answer" in json.dumps(plan.requests[1]["input"])
        finally:
            await core.shutdown()
        events = [json.loads(line)["hook_event_name"] for line in (project / "hook-events.jsonl").read_text().splitlines()]
        assert events == ["SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse", "Stop", "SessionEnd"]
    asyncio.run(scenario())


def test_trusted_switches_do_not_reprompt_until_the_source_or_config_changes(rig):
    store, _, project = rig
    file = write_json(project / ".mcp.json", {"mcpServers": {"demo": {"command": "python"}}})
    trust(store, file, project)
    entry = store.active("mcp", project)[0]
    store.update(entry["id"], enabled=False, project=str(project))
    store.update(entry["id"], enabled=True, project=str(project))
    assert len(store.active("mcp", project)) == 1
    file.write_text(file.read_text() + "\n", encoding="utf-8")
    with pytest.raises(ConnectionError, match="来源已经变化"):
        store.update(entry["id"], enabled=True, confirmed=True, project=str(project))


def test_project_mcp_overrides_same_name_global_server(rig):
    store, home, project = rig
    local = write_json(project / ".mcp.json", {"mcpServers": {"demo": {"command": "python", "args": ["project.py"]}}})
    global_ = write_json(home / ".mcp.json", {"mcpServers": {"demo": {"command": "python", "args": ["global.py"]}}})
    trust(store, local, project)
    trust(store, global_)
    assert [c.args for c in store.mcp_commands(project)] == [("project.py",)]
    assert [c.args for c in store.mcp_commands(home)] == [("global.py",)]


def test_corrupt_registry_is_not_overwritten_by_an_import(rig):
    store, _, project = rig
    store.file.write_text("{damaged", encoding="utf-8")
    file = write_json(project / ".mcp.json", {"mcpServers": {"demo": {"command": "python"}}})
    preview = store.preview(str(file))
    with pytest.raises(ConnectionError):
        store.commit(preview["preview"], True)
    assert store.file.read_text() == "{damaged"


def test_unsupported_hook_types_and_env_var_shapes_are_not_silently_accepted(rig):
    store, _, project = rig
    hook = write_json(project / "hooks.json", {"hooks": {"PreToolUse": [{"hooks": [{"type": "prompt", "prompt": "decide"}]}]}})
    with pytest.raises(ConnectionError, match="暂支持"):
        store.preview(str(hook))
    mcp = write_json(project / ".mcp.json", {"mcpServers": {"demo": {"command": "python", "env_vars": "WRONG"}}})
    with pytest.raises(ConnectionError, match="env_vars"):
        store.preview(str(mcp))
    with pytest.raises(ConnectionError, match="预览标识"):
        store.commit({}, True)


def test_environment_values_are_resolved_only_for_a_trusted_process(rig, monkeypatch):
    store, _, project = rig
    file = write_json(project / ".mcp.json", {"mcpServers": {"demo": {"command": "python", "env": {"DEMO": "${WITTYCODE_CONNECTION_DEMO}"}}}})
    trust(store, file, project)
    monkeypatch.delenv("WITTYCODE_CONNECTION_DEMO", raising=False)
    with pytest.raises(ConnectionError, match="尚未设置"):
        store.mcp_commands(project)
    monkeypatch.setenv("WITTYCODE_CONNECTION_DEMO", "runtime-only-demo")
    assert store.mcp_commands(project)[0].env == {"DEMO": "runtime-only-demo"}
    assert "runtime-only-demo" not in store.file.read_text() and "runtime-only-demo" not in json.dumps(store.scan(str(project)))


def test_prompt_hook_can_stop_a_real_turn_before_any_model_request(tmp_path, rig):
    store, _, project = rig
    hook = hook_script(project)
    command = f"& {quote(sys.executable)} {quote(hook)} deny"
    file = write_json(project / "hooks.json", {"hooks": {"UserPromptSubmit": [{"hooks": [{"type": "command", "command": command}]}]}})
    trust(store, file)
    async def scenario():
        core, plan, _, _, _ = await ready_core(tmp_path)
        core.settings.add_project(project)
        try:
            cid = (await core.call("session.new", {"projectId": str(project)}))["id"]
            controller = core._by_cid[cid].controller
            await core.call("session.send", {"id": cid, "text": "blocked"})
            await until(lambda: controller.state is SessionState.READY)
            assert plan.requests == []
            assert controller.transcript.turns[-1].status.value == "failed"
            assert any(block.kind == "error" and "扩展连接" in block.message for block in controller.transcript.turns[-1].blocks)
        finally:
            await core.shutdown()
    asyncio.run(scenario())


def test_openai_tool_aliases_match_before_a_write_is_executed(tmp_path, rig):
    store, _, project = rig
    hook = hook_script(project)
    command = f"& {quote(sys.executable)} {quote(hook)} deny"
    file = write_json(project / "hooks.json", {"hooks": {"PreToolUse": [{"matcher": "apply_patch", "hooks": [{"type": "command", "command": command}]}]}})
    trust(store, file)
    async def scenario():
        core, plan, _, _, _ = await ready_core(tmp_path)
        core.settings.add_project(project)
        try:
            cid = (await core.call("session.new", {"projectId": str(project)}))["id"]
            controller = core._by_cid[cid].controller
            plan.script(call("create_file", {"file_path": "must-not-exist.txt", "content": "blocked"}), say("stopped"))
            await core.call("session.send", {"id": cid, "text": "write the test file"})
            await until(lambda: controller.state is SessionState.READY)
            assert not (project / "must-not-exist.txt").exists()
            event = json.loads((project / "hook-events.jsonl").read_text().splitlines()[0])
            assert event["tool_name"] == "apply_patch" and event["wittycode_tool_name"] == "create_file"
        finally:
            await core.shutdown()
    asyncio.run(scenario())
