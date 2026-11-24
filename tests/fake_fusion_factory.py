"""Model-free Factory 1.18.0 JSONL peer, packaged as a temporary Windows Python console launcher."""
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid


def serve():
    if sys.argv[1:] == ["--version"]:
        print("1.18.0")
        return
    from desktop.core import factory_guard
    from desktop.core.fusion.acp_server import SERVER
    from desktop.core.session_title import FIRST_SYSTEM, SYSTEM

    root = Path(sys.argv[0]).parent
    factory_guard._SYSTEM_POLICY = root / "site.json"  # Fake IT deployment, never write the machine policy.
    sid = uuid.uuid4().hex
    names = []
    model = effort = ""
    peer = None

    def mcp(method, params):
        message = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        result = subprocess.run([peer["command"], *peer["args"]], input=json.dumps(message) + "\n", encoding="utf-8",
                                capture_output=True, check=True, timeout=60)
        return json.loads(result.stdout)["result"]

    def write(**parts):
        print(json.dumps({"jsonrpc": "2.0", "factoryApiVersion": "1.0.0", "factoryProtocolVersion": "1.221.0",
                          "type": "notification" if "method" in parts else "response", **parts}), flush=True)

    def notify(kind, **data):
        write(method="droid.session_notification", params={"sessionId": sid, "notification": {"type": kind, **data}})

    for line in sys.stdin:
        msg = json.loads(line)
        assert msg["type"] == "request" and msg["factoryProtocolVersion"] == "1.221.0"
        method, params, result = msg["method"], msg["params"], {}
        if method == "droid.list_models":
            result = {"models": [{"id": "demo-wire", "displayName": "Factory Wire", "contextWindow": 64000,
                                  "supportedReasoningEfforts": ["low", "medium"], "defaultReasoningEffort": "low"}]}
        elif method == "droid.initialize_session":
            assert "--settings" in sys.argv and params["machineId"] == "fusion" and params["cwd"] == os.getcwd()
            assert params["autonomyLevel"] == "off" and params["toolExecutionMode"] == "direct_only"
            assert factory_guard._hook({"hook_event_name": "SessionStart"}) == 0
            config = Path.home() / ".factory/mcp.json"
            if config.exists():
                peer = json.loads(config.read_text())["mcpServers"][SERVER]
                names = [t["name"] for t in mcp("tools/list", {})["tools"]]
                assert params["restrictToolIds"] == [f"MCP:{SERVER}/{name}" for name in names]
            model, effort = params["modelId"], params["reasoningEffort"]
            result = {"sessionId": sid, "settings": params}
        elif method == "droid.list_tools":
            result = {"tools": [{"id": "read", "llmId": "Read", "currentlyAllowed": False},
                                *[{"id": f"mcp_{SERVER}_{n}", "llmId": f"{SERVER}___{n}", "currentlyAllowed": True} for n in names]]}
        elif method == "droid.list_mcp_servers":
            result = {"servers": [{"name": SERVER, "source": "user", "status": "connected"}] if names else []}
        elif method == "droid.interrupt_session":
            write(id=msg["id"], result={})
            notify("agent_turn_completed", reason="interrupted")
            continue
        elif method != "droid.add_user_message":
            raise AssertionError(method)
        write(id=msg["id"], result=result)
        if method != "droid.add_user_message":
            continue
        prompt = params["text"]
        current = prompt.rsplit("\n\nCurrent request:\n", 1)[-1]
        action = "answer"
        if names and "[drop-wire]" in current:
            (root / (sid + ".turn")).write_text(json.dumps({"action": "disconnect", "prompt": prompt, "model": model,
                                                            "effort": effort, "tools": names}), encoding="utf-8")
            return  # Message acknowledged, no terminal: the host cannot roll back a possibly delivered prompt.
        if not names and "Assess whether the expert is stuck" in prompt:
            action, answer = "observer", '{"stuck":true,"confidence":0.97}'
        elif not names and "You are the L2 adversarial seat" in prompt:
            action, answer = "debate", "The local evidence is incomplete."
        elif not names:
            action = "title" if prompt.removeprefix("Fusion request:\n").startswith((FIRST_SYSTEM, SYSTEM)) else "summary"
            answer = "线协议验收" if action == "title" else "Factory handoff summary."
        else:
            tool = ""
            if "delegate_tasks" in names and "[fusion-matrix] " in current:
                action = "delegate"
                tool, args = "delegate_tasks", json.loads(current.split("[fusion-matrix] ", 1)[1].splitlines()[0])
            elif "[fusion-recheck]" in current and "Continue only this incomplete check." in current:
                action, tool = "recovered", "submit_report"
                assert "Counted 7 units." in prompt
                args = {"status": "done", "summary": "Count confirmed.", "evidence": ["evidence.txt: Counted 7 units."]}
            elif "[fusion-recheck]" in current or "[fusion-read]" in current:
                action = "expert"
                tool, args = "read_file", {"file_path": "evidence.txt"}
            if tool:
                assert factory_guard._hook({"hook_event_name": "PreToolUse", "tool_name": f"{SERVER}___{tool}", "tool_input": args}) == 0
                answer = "Factory report: " + mcp("tools/call", {"name": tool, "arguments": args})["content"][0]["text"]
            else:
                answer = "Factory local answer."
        (root / (sid + ".turn")).write_text(json.dumps({"action": action, "prompt": prompt, "model": model, "effort": effort,
                                                        "tools": names}), encoding="utf-8")
        notify("assistant_text_delta", textDelta=answer, messageId="reply", blockIndex=0)
        notify("create_message", message={"id": "reply", "message": {"role": "assistant", "content": [{"type": "text", "text": answer}]}})
        notify("agent_turn_completed", reason="completed", tokenUsage={"inputTokens": 100, "outputTokens": 10})
