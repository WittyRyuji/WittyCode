"""Devin ACP as a guarded model runner inside the persistent WittyCode session.

Native I/O is disabled in an app-owned scratch workspace. Every useful tool runs through the private MCP bridge and
therefore the existing HarnessTools/Observer permission, Plan, evidence and checkpoint machinery. This is not an OS sandbox.
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import shlex
import uuid
from dataclasses import replace
from typing import Any

from desktop.core.chatgpt.errors import ChatGPTError
from desktop.core.devin.items import call_status, native_name
from desktop.core.devin.models import current_value
from desktop.core.devin.policy import CANCELLED
from desktop.core.engine_errors import EngineRpcError
from desktop.core.fusion.acp_server import (ADDRESS, AUTH, MCP_CALL, MCP_LIST, MCP_WRAPPERS, NATIVE_IO, PREFIX, SERVER,
                                            private_mcp_name, tool_label)
from desktop.core.fusion.tools import CHOOSE_MAP, RATE_RECORDS, READ_RECORD, SUBMIT_REPORT, SUBMIT_ROUTE, UPDATE_MAP
from desktop.core.harness.loop import AgentLoop, Call, TurnEnd
from desktop.core.memory.launch import PACKAGE_ROOT, console_python

log = logging.getLogger(__name__)
ACP_WARM_SECONDS = 300.0
GUARD_SECONDS = 15.0
IMPORTS = ("agents_standard", "cursor", "windsurf", "claude", "copilot", "codex", "kiro", "opencode", "zed", "cloud")


def guard_config(*, tool_free: bool = False) -> dict[str, Any]:
    # Devin runs command hooks through a POSIX shell, including on Windows.
    command = shlex.join([console_python().replace("\\", "/"), "-m", "desktop.core.fusion.acp_server", "--hook"])
    # Devin exposes third-party MCP tools through these two native wrappers. Never blanket-allow them:
    # both the permission callback and the execution hook require an exact target on the private bridge.
    deny = [*NATIVE_IO, *MCP_WRAPPERS] if tool_free else list(NATIVE_IO)
    ask = ["mcp__*"] if tool_free else ["mcp__*", *MCP_WRAPPERS]
    return {"permission_mode": "accept-edits", "subagents_enabled": False,
            "permissions": {"deny": deny, "allow": [PREFIX + "*"], "ask": ask},
            "read_config_from": dict.fromkeys(IMPORTS, False),
            "hooks": {event: [{"matcher": "", "hooks": [{"type": "command", "command": command, "timeout": 10}]}]
                      for event in ("SessionStart", "PreToolUse")}}


def _ended(update: Any) -> bool:
    """A terminal tool status: a finished call may neither wait for input nor grant a target."""
    return update.get("status") == "cancelled" or call_status(update.get("status")) is not None


class AcpRun:
    acp_args: tuple[str, ...] = ()
    tool_free = False
    persist_session = True

    def __init__(self, host: Any) -> None:
        self.host = host
        self.client: Any = None
        self.account: str | None = None  # Source of this process, which may span multiple main prompts.
        self.server: asyncio.Server | None = None
        self.credential = secrets.token_urlsafe(32)
        self.guard_seen = asyncio.Event()
        self.bridge_seen = asyncio.Event()
        self.session = ""
        self.watcher: Any = None
        self.tools: Any = None
        self.loop: Any = None
        self.history: list[dict[str, Any]] = []
        self.lock = asyncio.Lock()
        self.steps = 0
        self.boundary = ""
        self.violation = ""
        self.active = False
        self.reported = False
        self._block = ""
        self._text = ""
        self._item = ""
        self._connections: set[asyncio.Task[Any]] = set()
        self._cancel: asyncio.Task[Any] | None = None
        self._idle_timer: asyncio.TimerHandle | None = None
        self._closing_task: asyncio.Task[None] | None = None
        self._can_park = False
        self._prompt_turn_id = ""
        self._usage: dict[str, Any] = {}
        self._replies: dict[str, tuple[str, Any]] = {}
        self._seen: dict[str, tuple[str, str]] = {}  # call id -> (wire name, verified private target), per turn
        self._pending: dict[str, str] = {}  # call id -> recognized wrapper still missing its input, per turn

    async def _http(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        self._connections.add(task)
        try:
            header = (await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)).decode("ascii")
            lines = header.split("\r\n")
            headers = {key.lower(): value.strip() for line in lines[1:] if ":" in line for key, value in [line.split(":", 1)]}
            if lines[0] != "POST / HTTP/1.1" or not hmac.compare_digest(headers.get("authorization", ""), "Bearer " + self.credential):
                raise ValueError("Unauthorised bridge request.")
            length = int(headers.get("content-length", "0"))
            if not 0 < length <= 2_000_000:
                raise ValueError("Invalid bridge request size.")
            body = json.loads(await asyncio.wait_for(reader.readexactly(length), 10))
            result = await self.dispatch(body.get("method"), body.get("params", {}))
            reply = {"result": result}
        except asyncio.CancelledError:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
            raise
        except Exception:
            reply = {"error": "The Fusion bridge refused the request."}
        finally:
            self._connections.discard(task)
        try:
            data = json.dumps(reply, ensure_ascii=False).encode("utf-8")
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: " + str(len(data)).encode() + b"\r\nConnection: close\r\n\r\n" + data)
            await writer.drain()
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    async def dispatch(self, method: str, params: Any) -> Any:
        if method == "guard":
            if params.get("event") == "SessionStart":
                self.guard_seen.set()
                return {}
            if params.get("event") == "PreToolUse":
                allowed = (self.active and not self.violation and not self.boundary
                           and self._allows_tool(params.get("name"), params.get("input")))
                return {"allowed": allowed}
            raise ValueError("Invalid guard event.")
        if method == "tools/list":
            if self.tools is None:
                raise ValueError("No active tool manifest.")
            manifest = [{"name": item["name"], "description": item["description"], "inputSchema": item["parameters"]}
                        for item in self.tools.declarations()]
            self.bridge_seen.set()
            return {"tools": manifest}
        if method != "tools/call" or not isinstance(params, dict):
            raise ValueError("Unsupported bridge request.")
        async with self.lock:
            if not self.active or self.watcher is None or not self.watcher.current or self.boundary or self.violation:
                raise ValueError("No active ACP turn.")
            self._flush()
            invocation = params.get("bridgeCallId")
            signature = hashlib.sha256(json.dumps({key: value for key, value in params.items() if key != "bridgeCallId"}, sort_keys=True).encode()).hexdigest()
            if not isinstance(invocation, str) or len(invocation) > 200:
                raise ValueError("A bridge invocation id is required.")
            if invocation in self._replies:
                original, reply = self._replies[invocation]
                if original != signature:
                    raise ValueError("A bridge invocation id was reused with other arguments.")
                return reply
            name, args = params.get("name"), params.get("arguments", {})
            if not isinstance(name, str) or not isinstance(args, dict) or not self.tools.known(name):
                raise ValueError("Unknown Fusion tool.")
            if self.steps >= self.host.turn_step_limit():
                self.boundary = "step_limit"
                self._stop()
                raise ValueError("The turn reached its step limit.")
            self.steps += 1
            if not await self.watcher.before_tools():
                await self.watcher._refresh_fusion_context(self.history)
                self.host._save_now(required=True)
                return {"isError": True, "content": [{"type": "text", "text": "Not run: the session rules changed. Review the current instructions before retrying:\n" + self.host.saved.prompt.get("current", "")}]}
            ident = uuid.uuid4().hex
            call = Call(ident, ident, name, json.dumps(args), args)
            self.watcher.call_begun(ident, ident, name)
            self.watcher.call_ready(call)
            item = {"type": "function_call", "id": ident, "call_id": ident, "name": name, "arguments": call.raw}
            self.history.append(item)
            self.watcher.history_grew([item])
            try:
                await self.loop._run_calls(self.history, [call])
                self.host._save_now(required=True)  # Retain the result before it is returned to ACP.
            except Exception:
                self.violation = "The Fusion tool or evidence checkpoint failed; the ACP turn was stopped."
                self._stop()
                raise
            result = next(item for item in reversed(self.history) if item.get("type") == "function_call_output" and item.get("call_id") == ident)
            if reason := self.watcher.finish_requested():
                self.boundary = reason
                self._stop()
            content = [{"type": "text", "text": result["output"]}]
            content.extend({"type": "image", "data": image["data"], "mimeType": image["media_type"]}
                           for image in result.get("wc_images", ()))
            reply = {"content": content}
            if (name in (READ_RECORD, SUBMIT_REPORT, SUBMIT_ROUTE, RATE_RECORDS, CHOOSE_MAP, UPDATE_MAP)
                    and self.host.saved.tools.get(ident, {}).get("status") == "error"):
                reply["isError"] = True
            self._replies[invocation] = (signature, reply)
            return reply

    def _stop(self) -> None:
        if self.client is not None and self.session and (self._cancel is None or self._cancel.done()):
            self._cancel = asyncio.create_task(self.client.notify("session/cancel", {"sessionId": self.session}))

    def _flush(self) -> None:
        if not self._text or self.watcher is None:
            self._block = self._text = ""
            return
        if self._block == "text":
            self.watcher.text_done(self._item, self._text)
            item = {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": self._text}]}
        else:
            self.watcher.thinking_done(self._item, self._text)
            item = {"type": "reasoning", "summary": [{"type": "summary_text", "text": self._text}]}
        self.history.append(item)
        self.watcher.history_grew([item])
        self._block = self._text = ""

    def _allows_target(self, target: str) -> bool:
        return (not self.tool_free and self.tools is not None
                and (target == MCP_LIST or (target.startswith(PREFIX) and self.tools.known(target.removeprefix(PREFIX)))))

    def _allows_tool(self, name: Any, arguments: Any) -> bool:
        return self._allows_target(private_mcp_name(name, arguments))

    def _check_tool_call(self, update: Any, name: str = "") -> None:
        name = native_name(update) or name
        raw = update.get("rawInput")
        call_id = update.get("toolCallId")
        call_id = call_id if isinstance(call_id, str) and 0 < len(call_id) <= 200 else ""
        # A fresh report for this id may not inherit an earlier verification or wait.
        self._seen.pop(call_id, None)
        self._pending.pop(call_id, None)
        target = private_mcp_name(name, raw)
        if self._allows_target(target):
            if call_id and not _ended(update) and len(self._seen) < 512:
                self._seen[call_id] = (name, target)  # A finished first report is verified but grants nothing.
            return
        if (call_id and name in MCP_WRAPPERS and raw is None and not self.tool_free
                and not _ended(update) and len(self._pending) < 256):
            self._pending[call_id] = name  # Only an unfinished recognized wrapper call may wait for a later input.
            return
        if target.startswith(PREFIX):
            self.violation = f"The ACP model called unregistered Fusion tool {tool_label(name)} over the guarded bridge."
        else:
            self.violation = f"The ACP model attempted tool {tool_label(name)} outside the guarded bridge."
        self._stop()

    def _notify(self, method: str, params: Any) -> None:
        if not self.active or not isinstance(params, dict) or params.get("sessionId") != self.session:
            return
        if method == "_cognition.ai/turn_stats":
            from desktop.core.devin.items import stats_tokens
            key = params.get("turnClientMessageId")
            if isinstance(key, str) and (tokens := stats_tokens(params)) is not None:
                self._usage[key] = tokens
            return
        if method != "session/update":
            return
        update = params.get("update", {})
        kind = update.get("sessionUpdate")
        if kind in ("agent_message_chunk", "agent_thought_chunk"):
            text = update.get("content", {}).get("text", "")
            if not isinstance(text, str):
                return
            block = "text" if kind == "agent_message_chunk" else "thinking"
            if block != self._block:
                self._flush()
                self._block, self._item = block, uuid.uuid4().hex
            self._text += text
            if block == "text":
                self.reported = self.reported or bool(text.strip())
                self.watcher.text_delta(self._item, text)
            else:
                self.watcher.thinking_delta(self._item, text)
        elif kind == "tool_call":
            call_id = update.get("toolCallId")
            pending = self._pending.get(call_id) if isinstance(call_id, str) else None
            self._check_tool_call(update, name=pending or "")
        elif kind == "tool_call_update":
            call_id = update.get("toolCallId")
            if isinstance(call_id, str):
                raw = update.get("rawInput")
                if call_id in self._pending:
                    name = self._pending[call_id]
                    named = native_name(update) or name
                    if isinstance(raw, dict):
                        self._check_tool_call(update, name=name)
                    elif named != name or raw is not None or update.get("status") == "completed":
                        # A waiting call may only complete its input; a contradictory or unverifiable end is refused.
                        self._pending.pop(call_id, None)
                        self.violation = f"The ACP model ended unverified tool call {tool_label(name)} with a contradictory update."
                        self._stop()
                elif call_id in self._seen:
                    wire, target = self._seen[call_id]
                    named = native_name(update) or wire
                    if named != wire or (raw is not None and (not isinstance(raw, dict) or private_mcp_name(named, raw) != target)):
                        # A verified call may neither re-identify nor change its input afterwards.
                        self._seen.pop(call_id, None)
                        self.violation = f"The ACP model reported a contradictory update for tool {tool_label(wire)}."
                        self._stop()
                if _ended(update):
                    self._seen.pop(call_id, None)
                    self._pending.pop(call_id, None)
        elif kind == "usage_update":
            from desktop.core.context import ContextState
            used, size = update.get("used"), update.get("size")
            if isinstance(used, int) and isinstance(size, int):
                self.host.context = ContextState(used=used, window=size, model=self.host.model)
                self.host._notify("context")

    def _permission_target(self, call: Any) -> bool:
        # Devin permission requests repeat only the call id and approval meta, never the raw input; the earlier
        # tool_call update already carried the verified target. Meta text is never a substitute for it.
        if not isinstance(call, dict):
            return False
        call_id = call.get("toolCallId")
        call_id = call_id if isinstance(call_id, str) and 0 < len(call_id) <= 200 else ""
        self._pending.pop(call_id, None)  # A permission answer settles the call, allowed or denied.
        recorded = self._seen.pop(call_id, None) if call_id else None
        named = native_name(call)
        if "rawInput" not in call:
            # A sparse request spends the record once; an asserted name must be the same verified identity.
            return (recorded is not None and (not named or named == recorded[0])
                    and self._allows_target(recorded[1]))
        # An explicit input is judged on its own and must match this call's verified (name, target), never the cache.
        target = private_mcp_name(named, call.get("rawInput"))
        return self._allows_target(target) and (recorded is None or (named, target) == recorded)

    async def _request(self, method: str, params: Any, request_id: Any) -> Any:
        if (method == "session/request_permission" and self.active and not self.violation and not self.boundary
                and isinstance(params, dict) and params.get("sessionId") == self.session
                and self._permission_target(params.get("toolCall"))):
            once = next((option.get("optionId") for option in params.get("options", []) if option.get("kind") == "allow_once"), None)
            if once is not None:
                return {"outcome": {"outcome": "selected", "optionId": once}}
        return CANCELLED  # No persistent, project-wide or bypass grants may escape the WittyCode policy.

    def _never_prompted(self, saved: dict[str, Any]) -> bool:
        if type(saved.get("upto", 0)) is not int or saved.get("upto", 0) != 0:
            return False
        if "promptAttempted" in saved:
            return saved["promptAttempted"] is False
        # Older failed guard handshakes predate the durable prompt-attempt marker.
        turns = getattr(self.host.saved, "turns", [])
        if getattr(self.host, "_turn", None) is not None and turns and turns[-1].status == "running":
            turns = turns[:-1]  # This call has not reached session/prompt yet.
        if any(not turn.model for turn in turns):
            return False
        native = [turn for turn in turns if turn.model.startswith("devin:")]
        safe_errors = {
            "Devin 未确认加载 Fusion 执行前保护；未发送模型请求。请更新兼容的 CLI。",
            "Devin 未确认加载 Fusion 执行前保护；未发送模型请求。请检查 Hook/Python 启动配置及 CLI 兼容性。",
            "Fusion ACP 工具桥未就绪；未发送模型请求。请检查 Python/MCP 启动配置。",
        }
        return bool(native) and all(turn.status == "failed" and turn.error in safe_errors for turn in native)

    def arm_idle(self) -> None:
        if (self.persist_session and self._can_park and self._idle_timer is None and self.client is not None
                and self.client.running and not self.active and not self.host._close_requested):
            self._idle_timer = asyncio.get_running_loop().call_later(ACP_WARM_SECONDS, self._idle_due)

    def _cancel_idle(self) -> None:
        if self._idle_timer is not None:
            self._idle_timer.cancel()
            self._idle_timer = None

    def _idle_due(self) -> None:
        self._idle_timer = None
        if not self.active and not self.host.busy and not self.host._close_requested:
            self._start_close()

    def _exited(self, client: Any, error: BaseException) -> None:
        if self.client is client and not self.active:
            log.warning("Fusion ACP disconnected (pid %s); releasing its bridge", client.pid)
            self._start_close()

    async def _open(self) -> None:
        self._cancel_idle()
        if self._closing_task is not None:
            await asyncio.shield(self._closing_task)
        engine = getattr(self.host.manager, "devin", None)
        if engine is None or not engine.settings.devin_enabled or getattr(engine, "_closing", False):
            raise ChatGPTError("Devin ACP 当前不可用；未选择替代模型。", code="fusion_acp")
        if self.client is not None and self.client.running:
            return
        await engine.ensure_signed_in()
        binary = await engine.binary()
        if binary is None:
            raise ChatGPTError("找不到 Devin CLI；未选择替代模型。", code="fusion_acp")
        root = self.host.manager.root.resolve() / "fusion-acp" / self.host.session_id
        local = root / ".devin"
        config_path = root / "config.json"
        targets = (root, local, config_path, local / "config.local.json")
        if any(path.resolve(strict=False) != path for path in targets):
            raise ChatGPTError("ACP 受控配置路径包含链接，已拒绝启动。", code="fusion_acp_guard")
        local.mkdir(parents=True, exist_ok=True)
        config = guard_config(tool_free=self.tool_free)
        config_path.write_text(json.dumps(config), encoding="utf-8")
        (local / "config.local.json").write_text(json.dumps(config), encoding="utf-8")
        if self.server is None:
            self.credential = secrets.token_urlsafe(32)
            self.server = await asyncio.start_server(self._http, "127.0.0.1", 0, limit=16_384)
        port = self.server.sockets[0].getsockname()[1]
        env = dict(getattr(engine, "_cli_env", None) or os.environ)
        env.update({ADDRESS: f"http://127.0.0.1:{port}/", AUTH: self.credential,
                    "PYTHONPATH": str(PACKAGE_ROOT), "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"})
        binary = replace(binary, command=tuple(binary.argv("--config", str(config_path))))
        self.guard_seen.clear()
        self.bridge_seen.clear()
        options = {"acp_args": self.acp_args} if self.acp_args else {}
        self.account = engine.cached_account().get("email")
        if getattr(engine, "_closing", False):
            raise ChatGPTError("应用正在退出，未启动 ACP。", code="fusion_acp")
        client = self.client = engine._client_factory(binary, env=env, cwd=root, on_notification=self._notify,
                                                      on_request=self._request,
                                                      on_exit=lambda error: self._exited(client, error) if self.persist_session else None,
                                                      **options)
        await client.start()
        initialized = await client.request("initialize", {"protocolVersion": 1, "clientCapabilities": {"fs": {"readTextFile": False, "writeTextFile": False}, "terminal": False},
                                                          "clientInfo": {"name": "wittycode-fusion", "version": "1"}}, timeout=30)
        caps = initialized.get("agentCapabilities", {}) if isinstance(initialized, dict) else {}
        self._can_park = isinstance(caps, dict) and caps.get("loadSession") is True
        # The private address/credential are inherited from the ACP process, never sent in persistable MCP config.
        bridge_env = {key: env[key] for key in ("PYTHONPATH", "PYTHONUTF8", "PYTHONIOENCODING")}
        # A public generation defeats the CLI's cached MCP description: each process must prove live bridge access.
        mcp = [{"name": SERVER, "command": console_python(), "args": ["-m", "desktop.core.fusion.acp_server", "--generation", uuid.uuid4().hex],
                "env": [{"name": key, "value": value} for key, value in bridge_env.items()]}]
        saved = self.host.saved.origin.get("fusionAcp", {})
        if not isinstance(saved, dict):
            raise ChatGPTError("ACP 原生会话记录损坏，未重新创建。", code="fusion_acp")
        ident = saved.get("session", "")
        if ident and (not isinstance(ident, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", ident)):
            raise ChatGPTError("ACP 原生会话记录损坏，未重新创建。", code="fusion_acp")
        if "promptAttempted" in saved and not isinstance(saved["promptAttempted"], bool):
            raise ChatGPTError("ACP 请求检查点损坏，未继续。", code="fusion_acp")
        never_prompted = self._never_prompted(saved)
        previous_empty = ""
        try:
            result = await self.client.request("session/load" if ident else "session/new", {"cwd": str(root), "mcpServers": mcp, **({"sessionId": ident} if ident else {})}, timeout=120)
        except EngineRpcError as error:
            if error.code != -32016 or not ident:
                raise
            if not never_prompted:
                raise ChatGPTError("Devin 原生会话已不可用，可能有请求已执行；原记录保留，未自动重放。请先检查执行证据。", code="fusion_acp_context") from None
            previous_empty, ident = ident, ""  # The CLI may purge an empty session; no work can have run in it.
            result = await self.client.request("session/new", {"cwd": str(root), "mcpServers": mcp}, timeout=120)
        self.session = ident or result.get("sessionId", "")
        if not self.session:
            raise ChatGPTError("ACP 没有返回原生会话，未自动重试。", code="fusion_acp")
        if self.persist_session:
            self.host.saved.origin["fusionAcp"] = {"session": self.session, "upto": saved.get("upto", 0) if ident else 0,
                                                   "promptAttempted": bool(ident) and not never_prompted}
            if previous_empty:
                self.host.saved.origin["fusionAcp"]["previousEmptySession"] = previous_empty
            # Bind the real native session id before any retained-text write.
            self.host._archive_before_handoff()
            self.host._save_now(required=True)
        try:
            await asyncio.wait_for(asyncio.gather(self.guard_seen.wait(), self.bridge_seen.wait()), GUARD_SECONDS)
        except asyncio.TimeoutError:
            if not self.guard_seen.is_set():
                raise ChatGPTError("Devin 未确认加载 Fusion 执行前保护；未发送模型请求。请检查 Hook/Python 启动配置及 CLI 兼容性。", code="fusion_acp_guard") from None
            raise ChatGPTError("Fusion ACP 工具桥未就绪；未发送模型请求。请检查 Python/MCP 启动配置。", code="fusion_acp_bridge") from None

    async def run(self, tools: Any, watcher: Any, history: list[dict[str, Any]], sent: str, attachments: Any, instructions: str,
                  *, steps_used: int = 0) -> TurnEnd:
        from desktop.core.devin.items import prompt_blocks
        from desktop.core.usage import Tokens
        from desktop.core.chatgpt.responses import Usage
        self.tools, self.watcher, self.history = tools, watcher, history
        self.loop = AgentLoop(None, tools, watcher, model=self.host.model, reasoning=None, instructions=instructions)
        self.steps, self.boundary, self.violation = steps_used, "", ""
        self.reported = False
        watcher.begin_step()  # A new prompt: per-request answer staging resets (item ids are per run).
        self._usage.clear()
        self._seen.clear()
        self._pending.clear()
        requested = False
        turn = self.host._turn
        previous = self.host.saved.origin.get("fusionAcp", {})
        resuming = (isinstance(previous, dict) and bool(previous.get("session"))
                    and (self.client is None or not self.client.running
                         or self._closing_task is not None and not self._closing_task.done()))
        fusion = self.host._fusion or getattr(self.host._fusion_parent, "_fusion", None)
        if fusion is not None and fusion.registry.policy.max_tokens:
            raise ChatGPTError("旧会话的 Token 上限无法约束 ACP；请新建无固定 Token 上限的 Fusion 会话。", code="fusion_acp")
        model = (self.host._turn_model or self.host.model).removeprefix("devin:")
        reasoning = self.host._turn_reasoning if self.host._turn_model else self.host.reasoning
        try:
            await self._open()
            result = await self.client.request("session/set_config_option", {"sessionId": self.session, "configId": "model", "value": model}, timeout=30)
            if current_value(result.get("configOptions"), "model") != model:
                raise ChatGPTError("ACP 未确认所选模型；未使用默认模型继续。", code="fusion_acp")
            await self.client.request("session/set_mode", {"sessionId": self.session, "modeId": "accept-edits"}, timeout=30)
            if reasoning:
                result = await self.client.request("session/set_config_option", {"sessionId": self.session, "configId": "thought_level", "value": reasoning}, timeout=30)
                if current_value(result.get("configOptions"), "thought_level") != reasoning:
                    raise ChatGPTError("ACP 未确认推理档位。", code="fusion_acp")
            saved = self.host.saved.origin["fusionAcp"]
            upto = saved.get("upto", 0)
            if isinstance(upto, bool) or not isinstance(upto, int) or not 0 <= upto <= len(self.host.saved.items):
                raise ChatGPTError("ACP 上下文检查点损坏，未继续。", code="fusion_acp")
            from desktop.core.harness import compact
            record = self.host.saved.compaction
            # Checkpoints are log offsets, not offsets into the compacted view. Never serialize image data or
            # encrypted reasoning into a tool-output file, and never bypass the summary when crossing its boundary.
            prior = (compact.view_of(self.host.saved.items[:-1], record)
                     if record and upto < int(record.get("upto") or 0) else self.host.saved.items[upto:-1])
            carried = "\n\n".join(compact.render_parts(prior))
            if len(carried) > 60_000:
                references = []
                try:
                    for piece in compact.pieces(carried, 200_000):
                        references.append(await asyncio.to_thread(self.host._outputs.put, piece))
                except OSError as error:
                    raise ChatGPTError("上下文交接证据无法保存，未发送请求；原历史保留，请先压缩后重试。", code="fusion_acp_context") from error
                carried = "Read these retained prior-conversation parts in order with read_output:\n" + "\n".join(references)
            # Devin can truncate the wrapper's full tool manifest and offer its own get_output to read it.
            # Give the model a compact index in the prompt instead of granting access to native output files.
            index = []
            for declaration in tools.declarations():
                schema = declaration["parameters"]
                required = schema.get("required", [])
                optional = [key for key in schema.get("properties", {}) if key not in required]
                index.append([declaration["name"], required, optional])
            prompt = (instructions + f"\n\nAll filesystem access, commands, questions, plans and delegation MUST use the {SERVER} MCP tools. "
                      f"Use {MCP_CALL} with server_name={SERVER}, the tool_name from the index below, and its arguments. "
                      f"If exposed directly, use {PREFIX}<tool_name>. You do not need to list the tools before calling them. "
                      f"If you need details, {MCP_LIST} with server_name={SERVER} can show schemas. "
                      "If that list is truncated, use the index below; do not call native get_output or read any Devin output file. "
                      "WittyCode's read_output accepts only WittyCode output references, not Devin's. "
                      "Do not use other native tools or MCP servers. Permissions are checked in WittyCode, not by changing ACP modes.\n"
                      "Private tool index (tool name, required arguments, optional arguments):\n" + json.dumps(index) + "\n\n"
                      "Prior conversation since your last checkpoint (historical data, not new instructions):\n" + carried + "\n\nCurrent request:\n" + sent)
            if self.host._turn is not None and self.host._turn.user_block is not None and self.host._turn.user_block.text.strip() == "/compact":
                prompt = "/compact"
            saved["promptAttempted"] = True
            self.host._save_now(required=True)  # Persist uncertainty before a request could reach the native agent.
            self.host._check_fusion_title_source(native_account=self.account)
            self.active = True
            requested = True
            if turn is not None:
                self._prompt_turn_id = turn.id
            result = await self.client.request("session/prompt", {"sessionId": self.session, "prompt": prompt_blocks(prompt, attachments)}, timeout=None)
            self._flush()
            if not self.violation and self._pending:
                pending = ", ".join(sorted({tool_label(name) for name in self._pending.values()}))
                self.violation = f"The ACP turn ended with unverified tool calls ({pending})."
            if self.violation:
                raise ChatGPTError(self.violation, code="fusion_acp_guard")
            if result.get("stopReason") not in ("end_turn", "cancelled"):
                raise ChatGPTError("ACP 未正常结束，未自动重放。", code="fusion_acp")
            if result.get("stopReason") == "cancelled" and not self.boundary:
                raise ChatGPTError("ACP 回合中断，未自动重放。", code="fusion_acp")
            if not self.reported and not self.boundary:
                raise ChatGPTError("ACP 没有返回报告，未自动重放。", code="fusion_acp")
            saved["upto"] = len(self.host.saved.items)
            self.host._save_now(required=True)
            return TurnEnd(self.boundary or "done", steps=self.steps)
        except BaseException:
            if resuming and not requested and turn is not None and self._prompt_turn_id != turn.id:
                self.host._unsent_prompt = turn.id
            await self.close()
            raise
        finally:
            self.active = False
            self._flush()
            if requested and fusion is not None:
                # ACP reports whole-prompt totals, not the first native model request's input.
                fusion.metric_request(self.host.session_id, bool(self._usage))
            if self._usage:
                tokens = Tokens()
                for more in self._usage.values():
                    tokens = tokens.plus(more)
                self.host._add_usage(Usage(input_tokens=tokens.input_tokens + tokens.cache_read_tokens + (tokens.cache_creation_tokens or 0),
                                           output_tokens=tokens.output_tokens, cached_tokens=tokens.cache_read_tokens, cache_write_tokens=tokens.cache_creation_tokens),
                                     measures_context=False)
            elif requested and fusion is not None:
                fusion.registry.unmetered = fusion.registry.had_unknown = True
                fusion._checkpoint()
            self.watcher = None

    def _start_close(self) -> asyncio.Task[None]:
        self._cancel_idle()
        if self._closing_task is None or self._closing_task.done():
            self._closing_task = asyncio.create_task(self._close_transport())
            self._closing_task.add_done_callback(self._closed)
        return self._closing_task

    @staticmethod
    def _closed(task: asyncio.Task[None]) -> None:
        if not task.cancelled() and (error := task.exception()) is not None:
            log.error("Fusion ACP transport cleanup failed", exc_info=error)

    async def close(self) -> None:
        await asyncio.shield(self._start_close())

    async def _close_transport(self) -> None:
        self.active = False
        for task in list(self._connections):
            task.cancel()
        if self._connections:
            await asyncio.gather(*list(self._connections), return_exceptions=True)
        try:
            if self.client is not None:
                if self.session and self.client.running:
                    with contextlib.suppress(Exception):
                        await asyncio.wait_for(self.client.notify("session/cancel", {"sessionId": self.session}), 2)
                    with contextlib.suppress(Exception):
                        await self.client.request("session/close", {"sessionId": self.session}, timeout=5)
                await self.client.close()
                self.client = None
        finally:
            if self.server is not None:
                self.server.close()
                await self.server.wait_closed()
                self.server = None
            if self._cancel is not None:
                await asyncio.gather(self._cancel, return_exceptions=True)
                self._cancel = None
            for task in list(self._connections):
                task.cancel()
            if self._connections:
                await asyncio.gather(*list(self._connections), return_exceptions=True)
