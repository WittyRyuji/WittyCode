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
    if event == "PreToolUse" and str(payload.get("tool_name", "")).startswith(PREFIX):
        return {}, 0  # The bridge applies the actual, current session policy before every operation.
    return {"decision": "block", "reason": "Fusion tools must run through WittyCode's permission bridge."}, 2


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
    raise SystemExit(main())
