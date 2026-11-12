"""JSON-RPC over the stdio of ``devin acp`` (the Agent Client Protocol): one message per line, in both directions.

The framing is the Codex app-server's (newline-delimited JSON, requests in both directions), so the client is the same
one with two differences that matter for ACP:

- Every message carries ``"jsonrpc": "2.0"`` (the Rust ACP library behind ``devin acp`` expects it; Codex's server
  neither sends nor wants it).
- **Answers are delivered in order with the notifications.** A prompt's answer comes straight after the last
  ``session/update`` and ``_cognition.ai/turn_stats`` it follows, and ``session/load`` answers after the whole replay of
  the saved session: the answer must not overtake the updates before it, or the turn would be closed before its last
  figures and words arrived (the Codex client resolves answers at once, which is fine there, where turns end with a
  notification of their own).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from desktop.core.engine_errors import EngineRpcError
from desktop.core.jsonrpc import INTERNAL_ERROR, JsonRpcClient

log = logging.getLogger("desktop.devin.rpc")


class AcpClient(JsonRpcClient):
    """One ``devin acp`` process and the conversation with it."""

    def __init__(
        self,
        argv: Sequence[str],
        *,
        env: Mapping[str, str] | None = None,
        cwd: str | Path | None = None,
        name: str = "devin acp",
        **handlers: Any,
    ) -> None:
        super().__init__(argv, env=env, cwd=cwd, name=name, **handlers)
        self._dispatch_done = False

    async def _write(self, message: Mapping[str, Any]) -> None:
        await super()._write({"jsonrpc": "2.0", **message})

    def _receive(self, line: bytes) -> None:
        text = line.decode("utf-8", "replace").strip()
        if not text:
            return
        try:
            message = json.loads(text)
        except ValueError:
            log.warning("%s sent a line that is not JSON: %.200s", self.name, text)
            return
        if not isinstance(message, dict) or self._inbox is None:
            return
        if "method" in message:
            kind = "request" if message.get("id") is not None else "notification"
        else:
            kind = "response"
        self._inbox.put_nowait((kind, message))

    def _resolve(self, message: Mapping[str, Any]) -> None:
        future = self._pending.pop(message.get("id"), None)
        if future is None or future.done():
            log.debug("%s answered request %r that nobody waits for", self.name, message.get("id"))
            return
        error = message.get("error")
        if error is not None:
            details = error if isinstance(error, Mapping) else {"message": str(error)}
            code = details.get("code")
            future.set_exception(
                EngineRpcError(
                    code if isinstance(code, int) and not isinstance(code, bool) else INTERNAL_ERROR,
                    str(details.get("message") or ""),
                    details.get("data"),
                )
            )
        else:
            future.set_result(message.get("result"))

    def _fail_pending(self, error: Any) -> None:
        # When the stream ends, the answers it still holds are in the inbox: they are delivered first.
        if self._closing or self._dispatch_done or self._inbox is None:
            super()._fail_pending(error)
        else:
            self._inbox.put_nowait(("fail", error))

    async def _dispatch(self) -> None:
        assert self._inbox is not None
        try:
            while True:
                kind, payload = await self._inbox.get()
                if kind == "response":
                    self._resolve(payload)
                elif kind == "notification":
                    await self._deliver(payload)
                elif kind == "request":
                    task = asyncio.ensure_future(self._serve(payload))
                    self._serving.add(task)
                    task.add_done_callback(self._serving.discard)
                elif kind == "fail":
                    JsonRpcClient._fail_pending(self, payload)
                elif kind == "exit":
                    if not self._closing and self.on_exit is not None:
                        try:
                            self.on_exit(payload)
                        except Exception:
                            log.exception("handling the exit of %s failed", self.name)
                    return
        finally:
            self._dispatch_done = True
            with contextlib.suppress(Exception):
                if self._ended is not None or self._closing:
                    JsonRpcClient._fail_pending(self, self._ended or self._gone())


__all__ = ["AcpClient"]
