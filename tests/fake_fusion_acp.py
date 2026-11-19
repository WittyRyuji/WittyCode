"""Scripted ACP process that really starts the private MCP child and execution guard, never a model."""
from __future__ import annotations

import asyncio
import json
import os
import re
import shlex
import sys
from pathlib import Path

from desktop.core.fusion.acp_server import MCP_CALL, MCP_LIST, MCP_WRAPPERS, PREFIX, SERVER
from desktop.core.session_title import SYSTEM as TITLE_SYSTEM
from tests.fake_devin import HOME, Fake, RpcError


class GuardedFake(Fake):
    def __init__(self, loop):
        super().__init__(loop)
        self.bridges = {}
        self.ids = 0
        self.replayed = {}

    def config(self):
        return json.loads(Path(sys.argv[sys.argv.index("--config") + 1]).read_text(encoding="utf-8"))

    async def run_hook(self, sid, event, **fields):
        command = self.config()["hooks"][event][0]["hooks"][0]["command"]
        process = await asyncio.create_subprocess_exec(*shlex.split(command), stdin=asyncio.subprocess.PIPE,
                                                      stdout=asyncio.subprocess.PIPE)
        out, _ = await process.communicate(json.dumps({"hook_event_name": event, "session_id": sid, **fields}).encode())
        return process.returncode, json.loads(out)

    async def bridge(self, sid, params):
        assert {"read", "write", "edit", "exec", "grep", "glob"} <= set(self.config()["permissions"]["deny"])
        if os.environ.get("FAKE_FUSION_NO_GUARD") != "1":
            assert await self.run_hook(sid, "SessionStart") == (0, {})
        if os.environ.get("FAKE_FUSION_NO_BRIDGE") == "1":
            return
        command = params["mcpServers"][0]
        if os.environ.get("FAKE_FUSION_CACHE_MCP") == "1":
            cache = HOME / "mcp-description-cache.json"
            seen = json.loads(cache.read_text(encoding="utf-8")) if cache.exists() else []
            if command in seen:
                return  # The real CLI can reuse a cached description without starting the MCP child.
            cache.write_text(json.dumps([*seen, command]), encoding="utf-8")
        env = {**os.environ, **{item["name"]: item["value"] for item in command["env"]}}
        child = await asyncio.create_subprocess_exec(command["command"], *command["args"], env=env,
                                                     stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE)
        self.bridges[sid] = child
        await self.mcp(sid, "initialize", {"protocolVersion": "2025-11-25"})
        manifest = (await self.mcp(sid, "tools/list", {}))["result"]["tools"]
        if "--agent-type" in sys.argv:
            assert not manifest and set(MCP_WRAPPERS) <= set(self.config()["permissions"]["deny"])

    async def mcp(self, sid, method, params, identifier=None):
        self.ids += 1
        child = self.bridges[sid]
        child.stdin.write((json.dumps({"jsonrpc": "2.0", "id": self.ids if identifier is None else identifier, "method": method, "params": params}) + "\n").encode())
        await child.stdin.drain()
        return json.loads(await child.stdout.readline())

    async def approve_tool(self, sid, name, arguments):
        call = {"toolCallId": name + ":0#1", "title": "Private MCP call", "kind": "other", "rawInput": arguments}
        if os.environ.get("FAKE_FUSION_BRIDGE_NOTICE") != "id-only":
            call["_meta"] = {"cognition.ai/inferenceToolName": name}
        self.update(sid, {"sessionUpdate": "tool_call", **call})
        answer = await self.ask("session/request_permission", {"sessionId": sid, "toolCall": call, "options": [
            {"optionId": "always", "kind": "allow_always"}, {"optionId": "once", "kind": "allow_once"},
        ]})
        assert answer == {"outcome": {"outcome": "selected", "optionId": "once"}}, answer
        assert await self.run_hook(sid, "PreToolUse", tool_name=name, tool_input=arguments) == (0, {})

    async def m_session_new(self, params):
        result = super().m_session_new(params)
        await self.bridge(result["sessionId"], params)
        return result

    async def m_session_load(self, params):
        if os.environ.get("FAKE_FUSION_MISSING_SESSION") == "1":
            raise RpcError(-32016, "Session not found")
        result = super().m_session_load(params)
        await self.bridge(params["sessionId"], params)
        return result

    async def run_turn(self, sid, session, turn, text):
        stop = "end_turn"
        try:
            current = text.rsplit("Current request:\n", 1)[-1]
            match = re.search(r"\[fusion-([a-z-]+)\]", current)
            summary = "--agent-type" in sys.argv and sys.argv[sys.argv.index("--agent-type") + 1] == "summarizer"
            assert summary == ("Do not call any tools. Return only the summary." in text)
            title = summary and text.startswith(TITLE_SYSTEM)
            if title:
                code, result = await self.run_hook(sid, "PreToolUse", tool_name=PREFIX + "read_file", tool_input={})
                assert code == 2 and result["decision"] == "block"
            action = "summary" if summary else match[1] if match else "report"
            tool, arguments = "", {}
            if action == "write":
                tool, arguments = "create_file", {"file_path": "marker.txt", "content": "guarded ACP write"}
            elif action == "shell":
                tool, arguments = "shell", {"command": "New-Item marker.txt"}
            elif action == "matrix":
                tool, arguments = "delegate_tasks", json.loads(current.splitlines()[0].removeprefix("[fusion-matrix] "))
            elif action == "delegate-parallel":
                tool, arguments = "delegate_tasks", {"tasks": [
                    {"workstream": name, "paths": paths, "model": "chatgpt:gpt-6-luna", "role": "worker", "task": "Report on the independent workstream."}
                    for name, paths in (("Login flow", ["frontend/auth", "desktop/auth.py"]), ("Icon design", ["assets/icons"]))
                ]}
            elif action == "delegate-shell":
                tool, arguments = "delegate_task", {"scope": "src", "model": "chatgpt:gpt-6-luna", "role": "worker", "task": "Create the marker with a command."}
            elif action == "delegate-acp-shell":
                tool, arguments = "delegate_task", {"scope": "src", "model": "devin:swe-2-high", "role": "worker", "task": "[fusion-shell] Create the marker with a command."}
            elif action == "delegate-acp-slow":
                tool, arguments = "delegate_task", {"scope": "src", "model": "devin:swe-2-high", "role": "worker", "task": "[fusion-slow] Wait for cancellation."}
            elif action in ("read", "truncated-tools"):
                tool, arguments = "read_file", {"file_path": "evidence.txt"}
            elif action == "plan":
                tool, arguments = "submit_plan", {"plan": "Inspect the module, implement the change, and run tests."}
            elif action == "delegate":
                tool, arguments = "delegate_task", {"scope": "src", "model": "chatgpt:gpt-6-luna", "role": "worker", "task": "Review the module."}
            elif action == "delegate-acp":
                tool, arguments = "delegate_task", {"scope": "src", "model": "devin:swe-2-high", "role": "worker", "task": "[fusion-write] Create the marker."}
            elif action in ("duplicate", "picture"):
                params = {"name": "read_file", "arguments": {"file_path": "picture.png" if action == "picture" else "evidence.txt"}}
                key = sid, action
                if key not in self.replayed:
                    first = await self.mcp(sid, "tools/call", params)
                    self.replayed[key] = (self.ids, first["result"])
                identifier, original = self.replayed[key]
                repeated = await self.mcp(sid, "tools/call", params, identifier)
                assert repeated["result"] == original
                if action == "picture":
                    (HOME / "picture-replies.json").write_text(json.dumps([original, repeated["result"]]), encoding="utf-8")
                changed = await self.mcp(sid, "tools/call", {"name": "create_file", "arguments": {"file_path": "forbidden.txt", "content": "not run"}}, identifier)
                assert "error" in changed
            elif action in ("native", "unknown-bridge"):
                self.update(sid, {"sessionUpdate": "agent_thought_chunk", "content": {"type": "text", "text": "Private reasoning"}})
                name = "write" if action == "native" else PREFIX + "unregistered"
                self.tool(sid, name + ":0#1", "Attempted unguarded write", "edit", {"file_path": "forbidden.txt"}, name)
                await turn["cancel"].wait()
                stop = "cancelled"
                return
            elif action == "slow":
                await turn["cancel"].wait()
                stop = "cancelled"
            if tool:
                if os.environ.get("FAKE_FUSION_MCP_WRAPPERS") == "1":
                    await self.approve_tool(sid, MCP_LIST, {"server_name": SERVER})
                    manifest = (await self.mcp(sid, "tools/list", {}))["result"]["tools"]
                    assert any(item["name"] == tool for item in manifest)
                    if action == "truncated-tools":
                        # Simulate a CLI that replaces the schemas with a preview and a native output reference.
                        assert len(json.dumps(manifest)) > 256
                        header = "Private tool index (tool name, required arguments, optional arguments):\n"
                        if header not in text:
                            self.update(sid, {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "The tool list was truncated."}})
                            self.tool(sid, "get_output:0#1", "Retrieve truncated tool list", "read", {"id": "preview"}, "get_output")
                            await turn["cancel"].wait()
                            stop = "cancelled"
                            return
                        index = {name: (required, optional) for name, required, optional in json.loads(text.split(header, 1)[1].splitlines()[0])}
                        assert index[tool][0] == ["file_path"]
                        assert "get_output" not in index and set(index) == {item["name"] for item in manifest}
                        code, result = await self.run_hook(sid, "PreToolUse", tool_name="get_output", tool_input={"id": "preview"})
                        assert code == 2 and result["decision"] == "block"
                    await self.approve_tool(sid, MCP_CALL, {"server_name": SERVER, "tool_name": tool, "arguments": arguments})
                else:
                    await self.approve_tool(sid, PREFIX + tool, arguments)
                result = await self.mcp(sid, "tools/call", {"name": tool, "arguments": arguments})
                answer = json.dumps(result)
            elif title:
                answer = "库存事务与去重"
            else:
                answer = "ACP summary: earlier work and evidence are retained; continue from the checkpoint." if summary else "ACP report."
            self.update(sid, {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": answer}})
            self.call(sid, session, turn, 1200, 20, 400)
        finally:
            await self.finish(sid, session, turn, stop)

    async def run(self):
        try:
            await super().run()
        finally:
            for child in self.bridges.values():
                if child.returncode is None:
                    child.terminate()
                await child.wait()


if __name__ == "__main__":
    loop = asyncio.new_event_loop()
    loop.run_until_complete(GuardedFake(loop).run())
