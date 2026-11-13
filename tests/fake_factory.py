"""A local Factory stream-jsonrpc peer. Validates the native wire contract, never imports the Factory SDK."""

import json
import os
from pathlib import Path
import sys
import uuid

ROOT = Path(os.environ["FAKE_FACTORY_HOME"])
ROOT.mkdir(parents=True, exist_ok=True)
settings = {"modelId": "demo-model", "reasoningEffort": "medium", "autonomyLevel": "off", "interactionMode": "auto"}
session = {"messages": [], "tokenUsage": {"inputTokens": 0, "outputTokens": 0, "cacheReadTokens": 0}}
native = ""
pending = ""


def write(message):
    kind = "request" if "method" in message and "id" in message else "notification" if "method" in message else "response"
    print(json.dumps({"jsonrpc": "2.0", "factoryApiVersion": "1.0.0", "factoryProtocolVersion": "1.221.0", "type": kind, **message}), flush=True)


def notify(kind, **data):
    write({"method": "droid.session_notification", "params": {"sessionId": native, "notification": {"type": kind, **data}}})


def finish():
    text = "Factory local answer."
    session["title"] = "Factory demo task"
    notify("session_title_updated", title=session["title"])
    session["messages"].append({"id": "assistant-demo", "role": "assistant", "content": [{"type": "text", "text": text}]})
    notify("assistant_text_delta", messageId="assistant-demo", blockIndex=0, textDelta=text)
    notify("assistant_text_complete", messageId="assistant-demo", blockIndex=0)
    use = {"inputTokens": 100, "outputTokens": 10, "cacheReadTokens": 20}
    for key, value in use.items():
        session["tokenUsage"][key] += value
    notify("session_token_usage_changed", tokenUsage=session["tokenUsage"], lastCallTokenUsage=use)
    notify("agent_turn_completed", reason="completed", tokenUsage=use, cumulativeTokenUsage=session["tokenUsage"])
    (ROOT / (native + ".json")).write_text(json.dumps({"session": session, "settings": settings}), encoding="utf-8")


for line in sys.stdin:
    message = json.loads(line)
    assert message.get("jsonrpc") == "2.0"
    assert message.get("factoryApiVersion") == "1.0.0"
    assert message.get("factoryProtocolVersion") == "1.221.0"
    method = message.get("method")
    params = message.get("params", {})
    allowed = {
        "droid.list_models": {"includeDisabled"},
        "droid.initialize_session": {"machineId", "cwd", "mcpServers", "autonomyLevel", "interactionMode", "modelId", "reasoningEffort"},
        "droid.load_session": {"sessionId", "mcpServers"},
        "droid.add_user_message": {"messageId", "text", "images", "files"},
        "droid.update_session_settings": {"modelId", "reasoningEffort", "autonomyLevel", "interactionMode"},
        "droid.interrupt_session": set(),
    }
    if method:
        assert method in allowed and set(params) <= allowed[method]
        for server in params.get("mcpServers", []):
            assert set(server) <= {"name", "command", "args", "env"}, "stdio MCP has no type discriminator"
            assert server.get("name") and server.get("command")
        for image in params.get("images", []):
            assert set(image) == {"type", "mediaType", "data"} and image["type"] == "base64"
        for document in params.get("files", []):
            assert document.get("mediaType") == "application/pdf" and document["type"] == "base64"
    if not method:
        assert message.get("type") == "response"
        if pending == "permission":
            if message.get("result", {}).get("selectedOption") == "cancel":
                notify("agent_turn_completed", reason="cancelled", tokenUsage={"inputTokens": 0, "outputTokens": 0, "cacheReadTokens": 0})
            else:
                notify("tool_result", toolUseId="tool-demo", content="done", isError=False, messageId="tool-result")
                finish()
        elif pending == "question":
            assert "answers" in message.get("result", {})
            finish()
        pending = ""
        continue
    assert message.get("type") == "request"
    assert isinstance(message["id"], str)
    uuid.UUID(message["id"])
    with (ROOT / "calls.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"method": method, "params": params}) + "\n")
    result = {}
    if method == "droid.list_models":
        result = {"models": [{"id": "demo-model", "displayName": "Factory Demo", "supportedReasoningEfforts": ["low", "medium", "high"], "defaultReasoningEffort": "medium"}]}
    elif method == "droid.initialize_session":
        assert params["cwd"] and params["machineId"]
        assert params["autonomyLevel"] in ("off", "low", "medium", "high")
        native = "demo-" + uuid.uuid4().hex
        settings.update({key: value for key, value in params.items() if key in settings})
        result = {"sessionId": native, "session": session, "settings": settings}
    elif method == "droid.load_session":
        native = params["sessionId"]
        loaded = json.loads((ROOT / (native + ".json")).read_text(encoding="utf-8"))
        session, settings = loaded["session"], loaded["settings"]
        result = {"session": session, "settings": settings}
    elif method == "droid.update_session_settings":
        settings.update(params)
    elif method == "droid.add_user_message":
        assert params["text"]
        session["messages"].append({"id": params["messageId"], "role": "user", "content": [{"type": "text", "text": params["text"]}]})
    elif method == "droid.interrupt_session":
        notify("agent_turn_completed", reason="cancelled", tokenUsage={"inputTokens": 0, "outputTokens": 0, "cacheReadTokens": 0})
    write({"id": message["id"], "result": result})
    if method == "droid.add_user_message":
        if "[hold]" in params["text"]:
            continue
        if "[permission]" in params["text"]:
            pending = "permission"
            write({"id": "permission-demo", "method": "droid.request_permission", "params": {"toolUses": [{"toolUse": {"type": "tool_use", "id": "tool-demo", "name": "Execute", "input": {"command": "python --version"}}}], "options": [{"value": "proceed_once", "label": "Allow once"}, {"value": "cancel", "label": "Reject"}]}})
        elif "[question]" in params["text"]:
            pending = "question"
            write({"id": "question-demo", "method": "droid.ask_user", "params": {"toolCallId": "question-tool", "questions": [{"index": 1, "topic": "Layout", "question": "Which layout?", "options": ["Compact", "Wide"]}]}})
        else:
            finish()
