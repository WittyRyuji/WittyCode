"""Direct MCP configuration and a real loopback Streamable HTTP peer, behind the bundled stdio adapter."""

import asyncio
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading

import pytest

from desktop.core.connections import ConnectionError, ConnectionStore
from desktop.core.harness.mcp import McpClient
from desktop.core.http_mcp import HttpMcp, validate_http_config
from desktop.core.plugin_http import PluginError, PluginHttp
from tests.test_connections import mcp_script


def test_direct_codex_toml_and_json_addition_survives_restart_without_external_config(tmp_path):
    project, profile = tmp_path / "project", tmp_path / "profile"
    project.mkdir()
    store = ConnectionStore(profile / "connections.json", home=tmp_path / "home")
    text = '[mcp_servers.local]\ncommand = "python"\nargs = ["-m", "demo_tools"]\nenv_vars = ["DEMO_SERVICE_VALUE"]\n'
    with pytest.raises(ConnectionError, match="确认"):
        store.add_mcp("", text, str(project))
    assert not store.file.exists()
    added = store.add_mcp("", text, str(project), confirmed=True)
    restarted = ConnectionStore(store.file, home=store.home)
    assert restarted.active("mcp", project)[0]["id"] == added["id"]
    assert restarted.active("mcp", "") == []
    source = restarted.active("mcp", project)[0]["source"]
    assert source.startswith(str(profile))
    with pytest.raises(ConnectionError, match="同名"):
        restarted.add_mcp("", text, str(project), confirmed=True)
    restarted.add_mcp("global-tools", json.dumps({"command": "python", "args": ["-m", "demo_global"]}), confirmed=True)
    assert [e["name"] for e in restarted.active("mcp", "")] == ["global-tools"]
    assert not (tmp_path / "home/.codex/config.toml").exists()


@pytest.mark.parametrize("config", [
    {"url": "http://example.com/mcp"}, {"url": "https://user:password@example.com/mcp"},
    {"url": "https://example.com/mcp?access_token=demo"}, {"url": "https://example.com/mcp", "command": "python"},
    {"url": "https://example.com/mcp", "http_headers": {"Authorization": "demo"}},
    {"url": "https://example.com/mcp", "http_headers": {"X-Service-Token": "demo"}},
    {"url": "https://example.com/mcp", "env_http_headers": {"Host": "DEMO_HOST"}},
    {"url": "https://example.com/mcp", "env_http_headers": {"X-Value": "${BAD}"}},
    {"url": "https://example.com/mcp", "tool_timeout_sec": 20}, {"command": "python", "enabled": False},
])
def test_unsupported_or_unsafe_settings_are_not_silently_enabled(tmp_path, config):
    store = ConnectionStore(tmp_path / "connections.json", home=tmp_path / "home")
    with pytest.raises(ConnectionError):
        store.create_mcp("demo", config, confirmed=True)
    assert not store.file.exists()


def test_complete_addition_really_starts_a_local_stdio_peer(tmp_path):
    store = ConnectionStore(tmp_path / "profile/connections.json", home=tmp_path / "home")
    import sys
    peer = mcp_script(tmp_path)
    result = store.create_mcp("local", {"command": sys.executable, "args": [str(peer)]}, confirmed=True)
    assert asyncio.run(store.test(result["id"], ""))["tools"] == ["echo"]


def test_real_http_mcp_handshake_session_headers_tool_call_and_close(tmp_path, monkeypatch):
    requests = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_POST(self):
            message = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append((message, dict(self.headers)))
            if "id" not in message:
                self.send_response(202); self.end_headers(); return
            method = message["method"]
            result = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}}, "serverInfo": {"name": "demo-http", "version": "1"}} if method == "initialize" else {"tools": [{"name": "echo", "description": "A local echo", "inputSchema": {"type": "object"}, "annotations": {"readOnlyHint": True}}]} if method == "tools/list" else {"content": [{"type": "text", "text": "local HTTP answer"}]}
            answer = {"jsonrpc": "2.0", "id": message["id"], "result": result}
            body = ("event: message\ndata: " + json.dumps(answer) + "\n\n").encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Mcp-Session-Id", "demo-http-session")
            self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
        def do_DELETE(self):
            requests.append(({"method": "delete"}, dict(self.headers)))
            self.send_response(204); self.end_headers()
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("DEMO_HTTP_AUTH", "demo-" + "bearer")
    store = ConnectionStore(tmp_path / "profile/connections.json", home=tmp_path / "home")
    store.create_mcp("remote", {"url": f"http://127.0.0.1:{server.server_port}/mcp", "bearer_token_env_var": "DEMO_HTTP_AUTH"}, confirmed=True)
    async def scenario():
        command, = store.mcp_commands("")
        assert not command.trusted_annotations
        client = McpClient(command.name, command.command, command.args, command.env, cwd=str(tmp_path))
        try:
            await client.start()
            assert [t.name for t in client.tools] == ["echo"]
            answer = await client.call("echo", {})
            assert "local HTTP answer" in str(answer)
        finally:
            await client.close()
    try:
        asyncio.run(scenario())
        initialized = next(headers for message, headers in requests if message["method"] == "tools/list")
        assert initialized["Mcp-Session-Id"] == "demo-http-session"
        assert initialized["Authorization"] == "Bearer demo-" + "bearer"
    finally:
        server.shutdown(); server.server_close(); thread.join(3)


def test_http_sse_result_finishes_before_the_peer_closes_its_connection():
    release, finished = threading.Event(), threading.Event()
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        def log_message(self, *args):
            pass
        def do_POST(self):
            message = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            progress = {"jsonrpc": "2.0", "method": "notifications/progress", "params": {"progress": 1}}
            answer = {"jsonrpc": "2.0", "id": message["id"], "result": {"tools": []}}
            self.wfile.write(("data: " + json.dumps(progress) + "\r\n\r\ndata: " + json.dumps(answer) + "\r\n\r\n").encode())
            self.wfile.flush()
            release.wait(5)
            finished.set()
        def do_DELETE(self):
            self.send_response(204); self.end_headers()
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    async def scenario():
        remote = HttpMcp({"url": f"http://127.0.0.1:{server.server_port}/mcp"})
        try:
            result = await asyncio.wait_for(remote.forward({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}), 2)
            assert result["result"] == {"tools": []}
            assert not finished.is_set(), "a complete RPC must not wait for the SSE socket's EOF"
        finally:
            release.set()
            await remote.close()
    try:
        asyncio.run(scenario())
    finally:
        release.set(); server.shutdown(); server.server_close(); thread.join(3)


def test_concurrent_custom_mcp_additions_preserve_both_registry_transactions(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    import time
    store = ConnectionStore(tmp_path / "profile/connections.json", home=tmp_path / "home")
    original = store._load
    def slow_load():
        result = original()
        time.sleep(.03)  # amplify the formerly unprotected read/modify/write window
        return result
    monkeypatch.setattr(store, "_load", slow_load)
    with ThreadPoolExecutor(max_workers=4) as pool:
        result = list(pool.map(lambda name: store.create_mcp(name, {"command": "python"}, confirmed=True), ["demo-one", "demo-two", "demo-three", "demo-four"]))
    assert len({item["id"] for item in result}) == 4
    restarted = ConnectionStore(store.file, home=store.home)
    assert {item["name"] for item in restarted.active("mcp", "")} == {"demo-one", "demo-two", "demo-three", "demo-four"}


def test_http_credentials_stay_in_environment_and_cannot_inject_headers(monkeypatch):
    config = {"url": "https://example.com/mcp", "bearer_token_env_var": "DEMO_HTTP_AUTH"}
    validate_http_config(config)
    with pytest.raises(PluginError, match="未设置"):
        HttpMcp(config).headers()
    monkeypatch.setenv("DEMO_HTTP_AUTH", "demo\r\nX-Value: bad")
    with pytest.raises(PluginError, match="不合法"):
        HttpMcp(config).headers()
    with pytest.raises(PluginError):
        asyncio.run(PluginHttp().request("GET", "http://example.com"))
