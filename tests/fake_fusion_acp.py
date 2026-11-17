"""Scripted ACP process that really starts the private MCP child and execution guard, never a model."""
from __future__ import annotations

import asyncio
import json
import os
import re
import shlex
import sys
from pathlib import Path

from tests.fake_devin import HOME, Fake, RpcError


class GuardedFake(Fake):
    def __init__(self, loop):
        super().__init__(loop)
        self.bridges = {}
        self.ids = 0
        self.replayed = {}

    async def bridge(self, sid, params):
        config = json.loads(Path(sys.argv[sys.argv.index("--config") + 1]).read_text(encoding="utf-8"))
        assert {"read", "write", "edit", "exec", "grep", "glob"} <= set(config["permissions"]["deny"])
        if os.environ.get("FAKE_FUSION_NO_GUARD") != "1":
            command = config["hooks"]["SessionStart"][0]["hooks"][0]["command"]
            process = await asyncio.create_subprocess_exec(*shlex.split(command), stdin=asyncio.subprocess.PIPE,
                                                          stdout=asyncio.subprocess.PIPE)
            out, _ = await process.communicate(json.dumps({"hook_event_name": "SessionStart", "session_id": sid}).encode())
            assert process.returncode == 0, out
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
        await self.mcp(sid, "tools/list", {})

    async def mcp(self, sid, method, params, identifier=None):
        self.ids += 1
        child = self.bridges[sid]
        child.stdin.write((json.dumps({"jsonrpc": "2.0", "id": self.ids if identifier is None else identifier, "method": method, "params": params}) + "\n").encode())
        await child.stdin.drain()
        return json.loads(await child.stdout.readline())

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
            action = match[1] if match else "report"
            tool, arguments = "", {}
            if action == "write":
                tool, arguments = "create_file", {"file_path": "marker.txt", "content": "guarded ACP write"}
            elif action == "read":
                tool, arguments = "read_file", {"file_path": "evidence.txt"}
            elif action == "plan":
                tool, arguments = "submit_plan", {"plan": "Inspect the module, implement the change, and run tests."}
            elif action == "delegate":
                tool, arguments = "delegate_task", {"scope": "src", "model": "chatgpt:gpt-6-luna", "role": "worker", "task": "Review the module."}
            elif action == "delegate-acp":
                tool, arguments = "delegate_task", {"scope": "src", "model": "devin:swe-2-high", "role": "worker", "task": "[fusion-write] Create the marker."}
            elif action == "duplicate":
                params = {"name": "read_file", "arguments": {"file_path": "evidence.txt"}}
                if sid not in self.replayed:
                    first = await self.mcp(sid, "tools/call", params)
                    self.replayed[sid] = (self.ids, first["result"])
                identifier, original = self.replayed[sid]
                repeated = await self.mcp(sid, "tools/call", params, identifier)
                assert repeated["result"] == original
                changed = await self.mcp(sid, "tools/call", {"name": "create_file", "arguments": {"file_path": "forbidden.txt", "content": "not run"}}, identifier)
                assert "error" in changed
            elif action == "native":
                self.tool(sid, "native", "Attempted native write", "edit", {"file_path": "forbidden.txt"}, "write")
                await turn["cancel"].wait()
                stop = "cancelled"
            elif action == "slow":
                await turn["cancel"].wait()
                stop = "cancelled"
            if tool:
                result = await self.mcp(sid, "tools/call", {"name": tool, "arguments": arguments})
                answer = json.dumps(result)
            else:
                answer = "ACP report."
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
