"""A small MCP client over stdio, enough to give the agent loop the shared-memory server (and any server like it).

MCP over stdio is newline-delimited JSON-RPC 2.0, UTF-8, one message per line (the server's logs go to stderr). Only the
client half the loop needs exists: ``initialize`` / ``initialized``, ``tools/list`` and ``tools/call``; a ``ping`` from the
server is answered, any other request from it is refused with "method not found" (this client offers no sampling, roots or
elicitation).

One client is one server process, started with the conversation and closed with it. A server that dies fails every call
waiting for it and every later one; the loop tells the model the tool failed, and the conversation goes on without it.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from desktop.core.winproc import CREATE_NO_WINDOW

log = logging.getLogger("desktop.harness.mcp")

PROTOCOL_VERSION = "2025-06-18"
START_SECONDS = 30.0
CALL_SECONDS = 120.0
CLOSE_SECONDS = 3.0
LINE_LIMIT = 1 << 24


class McpError(Exception):
    """The server could not be started, died, or answered a request with an error."""


@dataclass(frozen=True, slots=True)
class McpTool:
    server: str
    name: str
    description: str
    input_schema: dict[str, Any]
    annotations: dict[str, Any] = field(default_factory=dict)

    @property
    def read_only(self) -> bool:
        return self.annotations.get("readOnlyHint") is True

    @property
    def harmless(self) -> bool:
        """Declared neither destructive nor open-world: such a call needs no approval (the memory server's tools)."""
        return self.read_only or (self.annotations.get("destructiveHint") is False and self.annotations.get("openWorldHint") is False)


@dataclass(frozen=True, slots=True)
class McpResult:
    text: str
    is_error: bool = False


class McpClient:
    def __init__(self, name: str, command: str, args: Sequence[str] = (), env: Mapping[str, str] | None = None) -> None:
        self.name = name
        self.command = command
        self.args = tuple(args)
        self.env = dict(env or {})
        self.tools: list[McpTool] = []
        self.instructions = ""
        self._process: asyncio.subprocess.Process | None = None
        self._reader: asyncio.Task[None] | None = None
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._next = 0
        self._dead = ""

    @property
    def running(self) -> bool:
        return self._process is not None and self._process.returncode is None and not self._dead

    async def start(self, timeout: float = START_SECONDS) -> None:
        """Start the server, shake hands and read its tools. Raises ``McpError`` (the process is stopped again)."""
        kwargs: dict[str, Any] = {}
        if sys.platform == "win32":
            kwargs["creationflags"] = CREATE_NO_WINDOW
        try:
            self._process = await asyncio.create_subprocess_exec(
                self.command,
                *self.args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                env={**os.environ, **self.env},
                limit=LINE_LIMIT,
                **kwargs,
            )
        except OSError as error:
            raise McpError(f"{self.name}: cannot start {self.command}: {error}") from error
        self._reader = asyncio.ensure_future(self._read())
        try:
            async with asyncio.timeout(timeout):
                answer = await self._request(
                    "initialize",
                    {"protocolVersion": PROTOCOL_VERSION, "capabilities": {}, "clientInfo": {"name": "WittyCode", "version": "1"}},
                )
                self.instructions = str(answer.get("instructions") or "") if isinstance(answer, Mapping) else ""
                await self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})
                listed = await self._request("tools/list", {})
        except (TimeoutError, McpError) as error:
            await self.close()
            raise McpError(f"{self.name}: {error or 'timed out starting'}") from error
        self.tools = [
            McpTool(self.name, str(tool["name"]), str(tool.get("description") or ""), dict(tool.get("inputSchema") or {"type": "object", "properties": {}}), dict(tool.get("annotations") or {}))
            for tool in (listed.get("tools") if isinstance(listed, Mapping) else None) or ()
            if isinstance(tool, Mapping) and tool.get("name")
        ]

    async def call(self, tool: str, arguments: Mapping[str, Any], timeout: float = CALL_SECONDS) -> McpResult:
        try:
            async with asyncio.timeout(timeout):
                answer = await self._request("tools/call", {"name": tool, "arguments": dict(arguments)})
        except TimeoutError:
            return McpResult(f"The tool {tool} did not answer within {timeout:.0f} seconds.", True)
        except McpError as error:
            return McpResult(f"The tool {tool} failed: {error}", True)
        content = answer.get("content") if isinstance(answer, Mapping) else None
        parts = [str(part.get("text") or "") for part in content or () if isinstance(part, Mapping) and part.get("type") == "text"]
        return McpResult("\n".join(parts), bool(isinstance(answer, Mapping) and answer.get("isError")))

    async def close(self) -> None:
        process, self._process = self._process, None
        if process is not None:
            with contextlib.suppress(Exception):
                if process.stdin is not None:
                    process.stdin.close()
            try:
                await asyncio.wait_for(process.wait(), CLOSE_SECONDS)
            except (TimeoutError, asyncio.TimeoutError):
                with contextlib.suppress(ProcessLookupError):
                    process.kill()
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(process.wait(), CLOSE_SECONDS)
        reader, self._reader = self._reader, None
        if reader is not None:
            reader.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await reader
        self._fail_pending("closed")

    # ------------------------------------------------------------------ wire

    async def _send(self, message: Mapping[str, Any]) -> None:
        process = self._process
        if process is None or process.stdin is None or self._dead:
            raise McpError(self._dead or "not running")
        try:
            process.stdin.write((json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8"))
            await process.stdin.drain()
        except (OSError, ConnectionError) as error:
            raise McpError(f"the server is gone: {error}") from error

    async def _request(self, method: str, params: Mapping[str, Any]) -> Any:
        self._next += 1
        identifier = self._next
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[identifier] = future
        try:
            await self._send({"jsonrpc": "2.0", "id": identifier, "method": method, "params": dict(params)})
            return await future
        finally:
            self._pending.pop(identifier, None)

    def _fail_pending(self, reason: str) -> None:
        self._dead = self._dead or reason
        for future in list(self._pending.values()):
            if not future.done():
                future.set_exception(McpError(self._dead))

    async def _read(self) -> None:
        process = self._process
        assert process is not None and process.stdout is not None
        try:
            while True:
                line = await process.stdout.readline()
                if not line:
                    break
                try:
                    message = json.loads(line.decode("utf-8", "replace"))
                except ValueError:
                    log.warning("%s sent something that is not JSON: %.200r", self.name, line)
                    continue
                if isinstance(message, Mapping):
                    await self._dispatch(message)
        except (asyncio.CancelledError, GeneratorExit):
            raise
        except Exception:
            log.exception("reading from %s failed", self.name)
        self._fail_pending("the server exited")

    async def _dispatch(self, message: Mapping[str, Any]) -> None:
        method, identifier = message.get("method"), message.get("id")
        if isinstance(method, str):
            if identifier is None:
                return  # a notification (logging, progress, list changed): nothing here uses it
            if method == "ping":
                await self._reply(identifier, {"jsonrpc": "2.0", "id": identifier, "result": {}})
            else:
                await self._reply(identifier, {"jsonrpc": "2.0", "id": identifier, "error": {"code": -32601, "message": f"Method not found: {method}"}})
            return
        future = self._pending.get(identifier) if isinstance(identifier, int) else None
        if future is None or future.done():
            return
        error = message.get("error")
        if isinstance(error, Mapping):
            future.set_exception(McpError(str(error.get("message") or error)))
        else:
            future.set_result(message.get("result"))

    async def _reply(self, identifier: Any, message: Mapping[str, Any]) -> None:
        with contextlib.suppress(McpError):
            await self._send(message)
