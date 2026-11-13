"""Connection working directories, local-only discovery and selected-row identity use only temporary test data."""

import asyncio
import json
from pathlib import Path
import sys

import pytest

from desktop.core import connections
from desktop.core.connections import ConnectionError
from tests.test_connections import rig, mcp_script, trust, write_json


def test_a_relative_stdio_command_runs_in_the_selected_project(rig):
    store, _, project = rig
    peer = mcp_script(project)
    file = write_json(project / ".mcp.json", {"mcpServers": {"demo": {"command": sys.executable, "args": [peer.name]}}})
    trust(store, file, project)
    entry = store.active("mcp", project)[0]
    assert asyncio.run(store.test(entry["id"], project))["ok"]


def test_testing_a_global_row_does_not_silently_test_its_project_override(rig):
    store, home, project = rig
    global_peer, project_peer = mcp_script(home), mcp_script(project)
    global_peer.write_text(global_peer.read_text().replace("'name':'echo'", "'name':'global_echo'"))
    project_peer.write_text(project_peer.read_text().replace("'name':'echo'", "'name':'project_echo'"))
    global_file = write_json(home / ".mcp.json", {"mcpServers": {"demo": {"command": sys.executable, "args": [str(global_peer)]}}})
    local_file = write_json(project / ".mcp.json", {"mcpServers": {"demo": {"command": sys.executable, "args": [str(project_peer)]}}})
    trust(store, global_file)
    trust(store, local_file, project)
    global_entry = next(e for e in store.active("mcp", project) if not e["project"])
    assert asyncio.run(store.test(global_entry["id"], project))["tools"] == ["global_echo"]


def test_local_import_checks_each_component_before_resolving_a_link(rig, monkeypatch):
    store, _, project = rig
    link = project / "alias"
    original = connections._linked
    monkeypatch.setattr(connections, "_linked", lambda path: True if Path(path) == link else original(path))
    calls = []
    resolve = Path.resolve
    def record(path, *args, **kwargs):
        calls.append(path)
        return resolve(path, *args, **kwargs)
    monkeypatch.setattr(Path, "resolve", record)
    with pytest.raises(ConnectionError, match="目录链接"):
        store.preview(str(link / "config.json"))
    assert calls == []


def test_discovery_does_not_enter_a_linked_codex_root(rig, monkeypatch):
    store, home, project = rig
    original = connections._linked
    monkeypatch.setattr(connections, "_linked", lambda path: True if Path(path) == home / ".codex" else original(path))
    result = store.scan(str(project))
    assert not result["items"] and any("目录链接" in e["text"] for e in result["errors"])


def test_execution_options_not_implemented_by_the_adapter_are_not_ignored(rig):
    store, _, project = rig
    for key, value in (("cwd", "./other"), ("enabled_tools", ["read"]), ("disabled_tools", ["write"])):
        file = write_json(project / ".mcp.json", {"mcpServers": {"demo": {"command": "python", key: value}}})
        with pytest.raises(ConnectionError, match="尚未支持"):
            store.preview(str(file))
