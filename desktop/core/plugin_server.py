"""Bundled stdio MCP adapter for authorized service accounts and configured HTTP MCP servers."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import sys

from desktop.core.connector_auth import ConnectorStore, PROVIDERS
from desktop.core.connector_tools import ConnectorTools, TOOLS
from desktop.core.plugin_http import PluginError


async def handle(message, tools=None, remote=None):
    ident, method = message.get("id"), message.get("method")
    if method not in ("initialize", "notifications/initialized", "tools/list", "tools/call", "ping", "notifications/cancelled"):
        return {"jsonrpc": "2.0", "id": ident, "error": {"code": -32601, "message": "Method not supported"}}
    try:
        if remote:
            return await remote.forward(message)
        if "id" not in message:
            return None
        if method == "initialize":
            result = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}}, "serverInfo": {"name": "wittycode-connectors", "version": "1"}, "instructions": tools.instructions}
        elif method == "tools/list":
            if not tools.store.config(tools.provider)["enabled"]:
                raise PluginError("连接器已停用。")
            result = {"tools": TOOLS[tools.provider]}
        elif method == "tools/call":
            params = message.get("params", {})
            if not isinstance(params, dict):
                raise PluginError("工具参数必须是对象。")
            text = await tools.call(params.get("name"), params.get("arguments", {}))
            result = {"content": [{"type": "text", "text": text}], "isError": False}
        else:
            result = {}
        return {"jsonrpc": "2.0", "id": ident, "result": result}
    except (PluginError, OSError, ValueError, TypeError, KeyError):
        # Never echo service bodies, credentials or raw networking errors into a transcript.
        if method == "tools/call":
            return {"jsonrpc": "2.0", "id": ident, "result": {"content": [{"type": "text", "text": "连接器调用失败，请检查账号授权、服务权限和工具参数；可在设置 → 插件重新连接。"}], "isError": True}}
        return {"jsonrpc": "2.0", "id": ident, "error": {"code": -32000, "message": "Connector unavailable; check configuration and authorization in WittyCode settings"}}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--provider", choices=list(PROVIDERS))
    parser.add_argument("--settings")
    parser.add_argument("--registry")
    parser.add_argument("--mcp")
    parser.add_argument("--project", default="")
    args = parser.parse_args()
    tools, remote = None, None
    if args.provider and args.settings:
        tools = ConnectorTools(args.provider, ConnectorStore(Path(args.settings).parent))
    elif args.registry and args.mcp:
        from desktop.core.connections import ConnectionStore
        from desktop.core.http_mcp import HttpMcp
        entry = next((e for e in ConnectionStore(Path(args.registry)).active("mcp", args.project) if e["id"] == args.mcp), None)
        if entry is None or not entry["config"].get("url"):
            return 1
        remote = HttpMcp(entry["config"])
    else:
        return 1
    try:
        while True:
            raw = sys.stdin.buffer.readline(2_000_001)
            if not raw:
                break
            if len(raw) > 2_000_000 or not raw.endswith(b"\n"):
                return 1
            try:
                message = json.loads(raw)
                if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
                    raise ValueError()
                response = asyncio.run(handle(message, tools, remote))
            except (ValueError, UnicodeError):
                response = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Invalid JSON-RPC"}}
            if response is not None:
                sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
                sys.stdout.flush()
    finally:
        if remote:
            asyncio.run(remote.close())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
