"""JSON-RPC over the stdio of an engine's process (``devin acp``): one JSON message per line, in both directions.

Requests are ``{"id", "method", "params"}``; answers carry ``result`` or ``error`` (``{code, message}``); notifications
have no ``id``; the process also sends requests of its own (approvals, questions), answered with the same ``id``. Lines
can be large (whole command outputs, diffs, history pages), so stdout is read in chunks, not with ``readline`` and its
64 KiB limit.

Shutting down: a well-behaved server exits by itself when its stdin is closed, so ``close`` does that first and only then
terminates, and finally kills, the process.

Windows: the process gets ``CREATE_NO_WINDOW`` so that no console window opens under ``pythonw.exe``.
"""

from __future__ import annotations

import asyncio
import collections
import contextlib
import inspect
import itertools
import json
import logging
import os
import platform
import re
import shutil
import sys
from collections.abc import Awaitable, Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from desktop.core.engine_errors import EngineError, EngineProcessError, EngineRpcError, EngineTimeoutError

__all__ = ["CREATE_NO_WINDOW", "INTERNAL_ERROR", "METHOD_NOT_FOUND", "JsonRpcClient"]

log = logging.getLogger("desktop.jsonrpc")

CREATE_NO_WINDOW = 0x08000000
READ_CHUNK = 1 << 16
STDERR_LINES = 40
CLOSE_GRACE_SECONDS = 5.0
KILL_WAIT_SECONDS = 5.0
INTERNAL_ERROR = -32603
METHOD_NOT_FOUND = -32601

NotificationHandler = Callable[[str, Any], "Awaitable[None] | None"]
RequestHandler = Callable[[str, Any, Any], Awaitable[Any]]
"""``(method, params, request id) -> result``; raising ``EngineRpcError`` answers with that error."""
ExitHandler = Callable[[EngineProcessError], None]


class JsonRpcClient:
    """One ``codex app-server`` process and the JSON-RPC conversation with it.

    Server notifications are handed to ``on_notification`` one at a time, in the order they arrived (an async handler is
    awaited before the next one); server requests are served concurrently by ``on_request`` (an approval can wait for the
    user for minutes). Responses never wait behind handlers, so a handler may itself call ``request``. ``on_exit`` is
    told once when the process ends without ``close`` having been asked for, after the notifications it sent before.
    """

    def __init__(
        self,
        argv: Sequence[str],
        *,
        env: Mapping[str, str] | None = None,
        cwd: str | Path | None = None,
        on_notification: NotificationHandler | None = None,
        on_request: RequestHandler | None = None,
        on_exit: ExitHandler | None = None,
        name: str = "codex app-server",
    ) -> None:
        self.argv = [str(part) for part in argv]
        self.env = dict(env) if env is not None else None
        self.cwd = str(cwd) if cwd is not None else None
        self.on_notification = on_notification
        self.on_request = on_request
        self.on_exit = on_exit
        self.name = name
        self.exit_code: int | None = None
        self._process: asyncio.subprocess.Process | None = None
        self._ids = itertools.count(1)
        self._pending: dict[Any, asyncio.Future[Any]] = {}
        self._inbox: asyncio.Queue[tuple[str, Any]] | None = None
        self._tasks: set[asyncio.Task[Any]] = set()
        self._serving: set[asyncio.Task[Any]] = set()
        self._stderr: collections.deque[str] = collections.deque(maxlen=STDERR_LINES)
        self._stderr_task: asyncio.Task[None] | None = None
        self._write_lock: asyncio.Lock | None = None
        self._ended: EngineProcessError | None = None
        self._closing = False
        self._close_task: asyncio.Task[None] | None = None

    # ------------------------------------------------------------------ state

    @property
    def running(self) -> bool:
        """Started, not exited, not closing: requests can be sent."""
        process = self._process
        return process is not None and process.returncode is None and self._ended is None and not self._closing

    @property
    def pid(self) -> int | None:
        return self._process.pid if self._process is not None else None

    @property
    def stderr_tail(self) -> str:
        return "\n".join(self._stderr)

    def _spawn(self, coroutine: Any) -> asyncio.Task[Any]:
        task = asyncio.ensure_future(coroutine)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    def _gone(self) -> EngineProcessError:
        """A fresh error saying why requests cannot be sent (one instance per raise keeps tracebacks apart)."""
        ended = self._ended
        if ended is None:
            return EngineProcessError(f"{self.name} is not running", closed=self._closing)
        return EngineProcessError(ended.message, exit_code=ended.exit_code, stderr=ended.stderr, closed=ended.closed)

    # ------------------------------------------------------------------ start

    async def start(self) -> None:
        if self._process is not None:
            raise RuntimeError("the client was started already")
        options: dict[str, Any] = {}
        if sys.platform == "win32":
            options["creationflags"] = CREATE_NO_WINDOW
        try:
            self._process = await asyncio.create_subprocess_exec(
                *self.argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=self.env,
                cwd=self.cwd,
                **options,
            )
        except (OSError, ValueError) as error:
            raise EngineProcessError(f"cannot start {self.name} ({self.argv[0]}): {error}") from error
        self._inbox = asyncio.Queue()
        self._write_lock = asyncio.Lock()
        self._spawn(self._read_stdout())
        self._stderr_task = self._spawn(self._read_stderr())
        self._spawn(self._dispatch())

    # ------------------------------------------------------------------ outgoing

    async def request(self, method: str, params: Any = None, *, timeout: float | None = None) -> Any:
        """Send a request and wait for its result. ``params=None`` leaves ``params`` out (methods that take none)."""
        if not self.running:
            raise self._gone()
        request_id = next(self._ids)
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        message: dict[str, Any] = {"id": request_id, "method": method}
        if params is not None:
            message["params"] = params
        try:
            await self._write(message)
            if timeout is None:
                return await future
            try:
                return await asyncio.wait_for(future, timeout)
            except asyncio.TimeoutError:
                raise EngineTimeoutError(f"{method} got no answer within {timeout:g} s") from None
        finally:
            self._pending.pop(request_id, None)

    async def notify(self, method: str, params: Any = None) -> None:
        message: dict[str, Any] = {"method": method}
        if params is not None:
            message["params"] = params
        await self._write(message)

    async def _write(self, message: Mapping[str, Any]) -> None:
        process = self._process
        if process is None or process.stdin is None or self._write_lock is None:
            raise self._gone()
        data = (json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        async with self._write_lock:
            if self._ended is not None:
                raise self._gone()
            if process.stdin.is_closing():
                raise EngineProcessError(f"{self.name} is closing", closed=True)
            try:
                process.stdin.write(data)
                await process.stdin.drain()
            except (ConnectionError, OSError, RuntimeError) as error:
                raise EngineProcessError(f"cannot write to {self.name}: {error}") from error

    # ------------------------------------------------------------------ incoming

    async def _read_stdout(self) -> None:
        assert self._process is not None and self._process.stdout is not None
        stream = self._process.stdout
        buffer = bytearray()
        scanned = 0
        try:
            while True:
                chunk = await stream.read(READ_CHUNK)
                if not chunk:
                    break
                buffer += chunk
                start = 0
                while True:
                    end = buffer.find(b"\n", max(start, scanned))
                    if end < 0:
                        break
                    self._receive(bytes(buffer[start:end]))
                    start = end + 1
                if start:
                    del buffer[:start]
                scanned = len(buffer)
            if buffer.strip():
                self._receive(bytes(buffer))
        except asyncio.CancelledError:
            raise
        except Exception as error:  # a broken pipe is the same as the end of the stream
            log.warning("reading from %s failed: %s: %s", self.name, type(error).__name__, error)
        await self._stream_ended()

    async def _read_stderr(self) -> None:
        assert self._process is not None and self._process.stderr is not None
        stream = self._process.stderr
        pending = b""
        with contextlib.suppress(Exception):
            while True:
                chunk = await stream.read(READ_CHUNK)
                if not chunk:
                    break
                pending += chunk
                *lines, pending = pending.split(b"\n")
                for line in lines:
                    text = line.decode("utf-8", "replace").rstrip()
                    if text:
                        self._stderr.append(text)
                        log.debug("%s: %s", self.name, text)
            if pending.strip():
                self._stderr.append(pending.decode("utf-8", "replace").rstrip())

    def _receive(self, line: bytes) -> None:
        text = line.decode("utf-8", "replace").strip()
        if not text:
            return
        try:
            message = json.loads(text)
        except ValueError:
            log.warning("%s sent a line that is not JSON: %.200s", self.name, text)
            return
        if not isinstance(message, dict):
            return
        assert self._inbox is not None
        if "method" in message:
            kind = "request" if message.get("id") is not None else "notification"
            self._inbox.put_nowait((kind, message))
            return
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

    async def _stream_ended(self) -> None:
        process = self._process
        assert process is not None and self._inbox is not None
        with contextlib.suppress(asyncio.TimeoutError, Exception):
            await asyncio.wait_for(process.wait(), KILL_WAIT_SECONDS)
        # Let the last lines of stderr arrive: they usually say why the process died.
        if self._stderr_task is not None and not self._stderr_task.done():
            await asyncio.wait({self._stderr_task}, timeout=0.5)
        self.exit_code = process.returncode
        if self._closing:
            error = EngineProcessError(f"{self.name} was closed", exit_code=self.exit_code, closed=True)
        else:
            code = f" with code {self.exit_code}" if self.exit_code is not None else ""
            error = EngineProcessError(f"{self.name} exited{code}", exit_code=self.exit_code, stderr=self.stderr_tail)
        self._ended = error
        self._fail_pending(error)
        self._inbox.put_nowait(("exit", error))

    def _fail_pending(self, error: EngineProcessError) -> None:
        pending, self._pending = self._pending, {}
        for future in pending.values():
            if not future.done():
                future.set_exception(
                    EngineProcessError(error.message, exit_code=error.exit_code, stderr=error.stderr, closed=error.closed)
                )

    async def _dispatch(self) -> None:
        assert self._inbox is not None
        while True:
            kind, payload = await self._inbox.get()
            if kind == "notification":
                await self._deliver(payload)
            elif kind == "request":
                task = asyncio.ensure_future(self._serve(payload))
                self._serving.add(task)
                task.add_done_callback(self._serving.discard)
            elif kind == "exit":
                if not self._closing and self.on_exit is not None:
                    try:
                        self.on_exit(payload)
                    except Exception:
                        log.exception("handling the exit of %s failed", self.name)
                return

    async def _deliver(self, message: Mapping[str, Any]) -> None:
        handler = self.on_notification
        if handler is None:
            return
        method = str(message.get("method"))
        try:
            outcome = handler(method, message.get("params"))
            if inspect.isawaitable(outcome):
                await outcome
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("handling %s from %s failed", method, self.name)

    async def _serve(self, message: Mapping[str, Any]) -> None:
        request_id = message.get("id")
        method = str(message.get("method"))
        reply: dict[str, Any]
        try:
            if self.on_request is None:
                raise EngineRpcError(METHOD_NOT_FOUND, f"this client does not handle {method}")
            result = await self.on_request(method, message.get("params"), request_id)
            reply = {"id": request_id, "result": result}
        except EngineRpcError as error:
            reply = {"id": request_id, "error": {"code": error.code, "message": error.message}}
        except asyncio.CancelledError:
            raise  # closing: there is nobody left to answer
        except Exception as error:
            log.exception("serving %s from %s failed", method, self.name)
            reply = {"id": request_id, "error": {"code": INTERNAL_ERROR, "message": f"{type(error).__name__}: {error}"}}
        with contextlib.suppress(EngineError):
            await self._write(reply)

    # ------------------------------------------------------------------ close

    async def close(self, *, grace: float = CLOSE_GRACE_SECONDS) -> None:
        """End the process (stdin EOF, then terminate, then kill) and every task of this client. Idempotent."""
        if self._close_task is None:
            self._close_task = asyncio.ensure_future(self._shut_down(grace))
        await asyncio.shield(self._close_task)

    async def _shut_down(self, grace: float) -> None:
        self._closing = True
        process = self._process
        if process is not None:
            if process.stdin is not None:
                with contextlib.suppress(Exception):
                    process.stdin.close()
            if process.returncode is None:
                try:
                    await asyncio.wait_for(process.wait(), grace)
                except asyncio.TimeoutError:
                    log.warning("%s did not exit %.1f s after its stdin was closed; terminating it", self.name, grace)
                    with contextlib.suppress(ProcessLookupError, OSError):
                        process.terminate()
                    try:
                        await asyncio.wait_for(process.wait(), KILL_WAIT_SECONDS)
                    except asyncio.TimeoutError:
                        with contextlib.suppress(ProcessLookupError, OSError):
                            process.kill()
                        with contextlib.suppress(asyncio.TimeoutError):
                            await asyncio.wait_for(process.wait(), KILL_WAIT_SECONDS)
            self.exit_code = process.returncode
        self._fail_pending(self._ended or EngineProcessError(f"{self.name} was closed", exit_code=self.exit_code, closed=True))
        # The readers end by themselves at EOF; whatever still runs (a request waiting for the user) is cancelled.
        if self._tasks:
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(asyncio.gather(*list(self._tasks), return_exceptions=True), 2.0)
        leftovers = [task for task in (*self._tasks, *self._serving) if not task.done()]
        for task in leftovers:
            task.cancel()
        if leftovers:
            await asyncio.gather(*leftovers, return_exceptions=True)
        if process is not None:
            transport = getattr(process, "_transport", None)
            if transport is not None:
                with contextlib.suppress(Exception):
                    transport.close()


# ---------------------------------------------------------------------------------------------- launching
