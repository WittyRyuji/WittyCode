"""Real local stdio startup, including the Windows npx shim; no downloads, accounts or model calls."""

import asyncio
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from desktop.core.harness.mcp import _windows_command
from tests.test_connections import rig, mcp_script, trust, write_json


def test_missing_program_reports_an_actionable_failure_without_raw_process_output(rig):
    store, _, project = rig
    source = write_json(project / ".mcp.json", {"mcpServers": {"missing": {"command": "wittycode-demo-missing-stdio"}}})
    trust(store, source, project)
    entry = store.active("mcp", project)[0]
    result = asyncio.run(store.test(entry["id"], project))
    assert not result["ok"] and result["tools"] == []
    assert "找不到启动程序" in result["text"] and "完整路径" in result["text"]


def test_a_server_exiting_during_handshake_is_not_reported_as_success(rig):
    store, _, project = rig
    peer = project / "exiting_peer.py"
    peer.write_text("import sys\nsys.stderr.write('private server diagnostic')\nraise SystemExit(2)\n", encoding="utf-8")
    source = write_json(project / ".mcp.json", {"mcpServers": {"exiting": {"command": sys.executable, "args": [str(peer)]}}})
    trust(store, source, project)
    result = asyncio.run(store.test(store.active("mcp", project)[0]["id"], project))
    assert not result["ok"] and "提前退出" in result["text"]
    assert "private server diagnostic" not in result["text"]


@pytest.mark.skipif(sys.platform != "win32", reason="Windows executable lookup")
def test_windows_lookup_uses_the_servers_path_and_never_the_implicit_working_folder(tmp_path, monkeypatch):
    work, tools = tmp_path / "work", tmp_path / "tools"
    work.mkdir(); tools.mkdir()
    (work / "demo.exe").touch()
    (tools / "demo.exe").touch()
    monkeypatch.chdir(work)
    assert _windows_command("demo", {"PATH": str(tools), "PATHEXT": ".EXE;.CMD"}, str(work)) == str(tools / "demo.EXE")
    with pytest.raises(FileNotFoundError):
        _windows_command("demo", {"PATH": ".", "PATHEXT": ".EXE;.CMD"}, str(work))


@pytest.mark.skipif(sys.platform != "win32", reason="Windows explicit shim path")
def test_an_explicit_relative_mcp_program_uses_the_project_and_pathext(tmp_path):
    folder = tmp_path / "project/bin"
    folder.mkdir(parents=True)
    (folder / "demo.cmd").touch()
    assert _windows_command(r".\bin\demo", {"PATHEXT": ".EXE;.CMD"}, str(folder.parent)) == str(folder / "demo.CMD")


@pytest.mark.skipif(sys.platform != "win32", reason="Windows drive-relative path")
def test_drive_relative_programs_do_not_use_an_ambient_drive_directory(tmp_path):
    drive = tmp_path.drive
    other = r"Z:\demo-project" if drive.upper() != "Z:" else r"C:\demo-project"
    for command in (drive + "demo.exe", r"\demo.exe"):
        with pytest.raises(OSError, match="drive-relative"):
            _windows_command(command, {}, other)


def test_the_servers_path_override_is_case_insensitive_on_windows(rig):
    if sys.platform != "win32":
        pytest.skip("Windows environment names")
    from desktop.core.harness.mcp import McpClient

    _, _, project = rig
    peer, record = mcp_script(project), project / "reported-path.txt"
    peer.write_text("import os, pathlib\npathlib.Path(" + repr(str(record)) + ").write_text(os.environ.get('PATH', ''), encoding='utf-8')\n" + peer.read_text(), encoding="utf-8")

    async def scenario():
        client = McpClient("server-path", sys.executable, [str(peer)], {"Path": str(project / "server-tools")})
        try:
            await client.start()
        finally:
            await client.close()

    asyncio.run(scenario())
    assert record.read_text(encoding="utf-8") == str(project / "server-tools")


def offline_npx(project, monkeypatch):
    if sys.platform != "win32" or not shutil.which("npx"):
        pytest.skip("Windows Node tooling is not installed")
    # npm is isolated from the user's configuration, cache, registry and network.
    for key, name in (("npm_config_userconfig", "user.npmrc"), ("npm_config_globalconfig", "global.npmrc")):
        file = project / name
        file.write_text("", encoding="utf-8")
        monkeypatch.setenv(key, str(file))
    monkeypatch.setenv("npm_config_cache", str(project / "npm-cache"))
    monkeypatch.setenv("npm_config_update_notifier", "false")
    monkeypatch.setenv("npm_config_offline", "true")
    write_json(project / "package.json", {"name": "wittycode-local-mcp-test", "version": "1.0.0", "private": True})
    peer = mcp_script(project)
    return {"command": "npx", "args": ["--offline", "--call", subprocess.list2cmdline([sys.executable, str(peer)])]}


def test_a_real_offline_npx_stdio_server_can_be_imported_and_tested(rig, monkeypatch):
    store, _, project = rig
    source = write_json(project / ".mcp.json", {"mcpServers": {"offline-node-shim": offline_npx(project, monkeypatch)}})
    trust(store, source, project)
    result = asyncio.run(store.test(store.active("mcp", project)[0]["id"], project))
    assert result == {"ok": True, "text": "连接成功 · 1 个工具", "tools": ["echo"]}


@pytest.mark.skipif(sys.platform != "win32", reason="Windows MCP process-tree cleanup")
def test_a_timed_out_launcher_does_not_leave_its_own_server_child_running(tmp_path):
    from desktop.core.harness.mcp import McpClient, McpError
    from tests.test_harness_shell import pid_alive, wait_gone

    peer, record = tmp_path / "launcher.py", tmp_path / "child.pid"
    peer.write_text("import pathlib, subprocess, sys, time\nchild = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(20)'], creationflags=0x08000000)\npathlib.Path(sys.argv[1]).write_text(str(child.pid))\ntime.sleep(20)\n", encoding="utf-8")

    async def scenario():
        client = McpClient("timed-launcher", sys.executable, [str(peer), str(record)])
        try:
            with pytest.raises(McpError):
                await client.start(timeout=0.5)
        finally:
            await client.close()

    try:
        asyncio.run(scenario())
        assert record.exists()
        assert wait_gone(int(record.read_text()), 1), "closing a test must stop only its own launcher and descendants"
    finally:
        if record.exists() and pid_alive(int(record.read_text())):
            subprocess.run([str(Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/taskkill.exe"), "/F", "/PID", record.read_text()], capture_output=True, timeout=5)
