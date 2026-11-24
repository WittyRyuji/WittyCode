"""Private stdio MCP bridge and native-tool guard for a Fusion ACP process.

The address and ephemeral credential exist only in the child environment. This server never executes tools itself.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request
import uuid
from typing import Any

ADDRESS = "WITTYCODE_FUSION_ADDRESS"
AUTH = "WITTYCODE_FUSION_AUTH"
SERVER = "wittycode-fusion"
PREFIX = "mcp__" + SERVER + "__"
MCP_LIST = "mcp_list_tools"
MCP_CALL = "mcp_call_tool"
MCP_WRAPPERS = (MCP_LIST, MCP_CALL)
NATIVE_IO = ("read", "write", "edit", "multi_edit", "apply_patch", "notebook_read", "notebook_edit", "grep", "glob",
             "find_file_by_name", "exec", "get_output",
             "write_to_process", "kill_shell", "webfetch", "fetch", "web_search", "ask_user_question", "todo_write", "exit_plan_mode",
             "skill", "run_subagent", "read_subagent", "request_scope", "mcp_list_servers", "mcp_read_resource")


def private_mcp_name(name: Any, arguments: Any) -> str:
    """Resolve only an explicitly targeted private MCP discovery or invocation."""
    if not isinstance(name, str):
        return ""
    if name.startswith(PREFIX):
        return name
    if not isinstance(arguments, dict) or arguments.get("server_name") != SERVER:
        return ""
    if name == MCP_LIST and arguments.keys() == {"server_name"}:
        return MCP_LIST
    if name == MCP_CALL and arguments.keys() == {"server_name", "tool_name", "arguments"}:
        tool = arguments["tool_name"]
        if isinstance(tool, str) and isinstance(arguments["arguments"], dict):
            return PREFIX + tool
    return ""


def tool_label(name: Any) -> str:
    """A refusal-safe label: only known native tools and the bridge wrappers are named; the rest is a fixed class."""
    if isinstance(name, str):
        if name in NATIVE_IO or name in MCP_WRAPPERS:
            return name
        if name.startswith(PREFIX):
            return "a private bridge tool"
    return "an unrecognized tool"


def request(method: str, params: Any = None) -> Any:
    address, credential = os.environ[ADDRESS], os.environ[AUTH]
    if not address.startswith("http://127.0.0.1:"):
        raise ValueError("The Fusion bridge must be local.")
    data = json.dumps({"method": method, "params": params or {}}, ensure_ascii=False).encode("utf-8")
    call = urllib.request.Request(address, data=data, headers={"Authorization": "Bearer " + credential, "Content-Type": "application/json"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(call, timeout=None) as response:
        result = json.load(response)
    if "error" in result:
        raise ValueError(result["error"])
    return result.get("result")


def hook(payload: dict[str, Any]) -> tuple[dict[str, Any], int]:
    event = payload.get("hook_event_name")
    if event == "SessionStart":
        request("guard", {"event": event})
        return {}, 0
    name, arguments = payload.get("tool_name"), payload.get("tool_input")
    if event == "PreToolUse":
        if private_mcp_name(name, arguments) and request("guard", {"event": event, "name": name, "input": arguments}).get("allowed") is True:
            return {}, 0  # Execution and user approval still happen in the host's MCP bridge.
    reason = f"Fusion refused tool {tool_label(name)}; tools must run through WittyCode's permission bridge."
    return {"decision": "block", "reason": reason}, 2


def main() -> int:
    if "--hook" in sys.argv:
        try:
            result, code = hook(json.load(sys.stdin))
        except BaseException:
            result, code = {"decision": "block", "reason": "The Fusion guard is unavailable."}, 2
        print(json.dumps(result))
        return code
    generation = uuid.uuid4().hex
    for line in sys.stdin:
        identifier = None
        try:
            message = json.loads(line)
            identifier = message.get("id")
            if identifier is None:
                continue
            method, params = message.get("method"), message.get("params", {})
            if method == "initialize":
                request("tools/list")  # Verify inherited bridge access before advertising a usable MCP server.
                result = {"protocolVersion": params.get("protocolVersion", "2025-11-25"), "capabilities": {"tools": {}},
                          "serverInfo": {"name": SERVER, "version": "1.0.0"},
                          "instructions": "Use only these WittyCode tools. Native tools are disabled. Follow the current Fusion task and permissions."}
            elif method == "ping":
                result = {}
            elif method in ("tools/list", "tools/call"):
                if method == "tools/call":
                    params = {**params, "bridgeCallId": generation + ":" + str(identifier)}
                result = request(method, params)
            else:
                raise ValueError("Unsupported bridge method.")
            reply = {"jsonrpc": "2.0", "id": identifier, "result": result}
        except BaseException:
            reply = {"jsonrpc": "2.0", "id": identifier, "error": {"code": -32603, "message": "The Fusion bridge request failed."}}
        print(json.dumps(reply, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    # Factory launches the isolated interpreter (-I), which ignores PYTHONIOENCODING.
    # MCP JSONL is UTF-8 even when the Windows pipe code page isn't.
    sys.stdin.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
