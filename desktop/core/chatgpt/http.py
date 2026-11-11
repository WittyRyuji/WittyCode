"""HTTP for the ChatGPT engine, from the standard library only.

The project has no HTTP package, and the system proxy matters: ``urllib`` picks up the proxy settings of the environment
and of the Windows registry (and tunnels HTTPS through a proxy), which a hand-made socket client would not. Blocking calls
run in worker threads so the event loop never waits on the network.

``request`` returns every HTTP status as a ``HttpResponse`` (the caller decides what a 400 means); only a connection that
cannot be made or kept raises. ``stream_lines`` yields the lines of a streamed body (server-sent events) as they arrive and
raises ``ChatGPTError`` for an HTTP error, with the error body read; ``stream_chunks`` does the same for a body that is not text
(AWS's event stream), as the bytes that arrive. Closing the generator drops the connection at once
(the worker is blocked in a read: the socket is shut down from here), whatever phase the request is in; a connection that
is still being made when the generator is closed is closed unread by the worker when it arrives (``_StreamCall``).
"""

from __future__ import annotations

import asyncio
import contextlib
import http.client
import json
import queue
import socket
import threading
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from typing import Any

from desktop.core.chatgpt.errors import ChatGPTError, error_from_body

USER_AGENT = "WittyCode"
DEFAULT_TIMEOUT = 30.0
STREAM_IDLE_SECONDS = 120.0
"""A stream that sends nothing (not even a keep-alive) for this long is dead."""
STREAM_QUEUE_LINES = 256
"""Lines that may wait between the worker thread and the consumer. A consumer that stalls holds the worker back: it stops
reading (and so the server stops being drained) instead of buffering the rest of the answer."""
CANCEL_POLL_SECONDS = 0.05
"""How long a worker that is waiting for room in the queue waits before it looks again at the cancelled flag and the loop."""


@dataclass(slots=True)
class HttpResponse:
    status: int
    headers: dict[str, str]
    body: bytes

    def text(self) -> str:
        return self.body.decode("utf-8", "replace")

    def json(self) -> Any:
        return json.loads(self.body.decode("utf-8")) if self.body.strip() else None


def _headers_of(message: Any) -> dict[str, str]:
    return {key.lower(): value for key, value in message.items()}


def _prepare(
    method: str,
    url: str,
    headers: Mapping[str, str] | None,
    form: Mapping[str, str] | None,
    json_body: Any,
    body: bytes | None = None,
) -> urllib.request.Request:
    merged = {"User-Agent": USER_AGENT, "Accept": "*/*", **(headers or {})}
    data: bytes | None = None
    if body is not None:  # (bytes already made, e.g. the ones a signature was computed over)
        data = body
    elif form is not None:
        data = urllib.parse.urlencode(form).encode("ascii")
        merged.setdefault("Content-Type", "application/x-www-form-urlencoded")
    elif json_body is not None:
        data = json.dumps(json_body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        merged.setdefault("Content-Type", "application/json")
    return urllib.request.Request(url, data=data, headers=merged, method=method.upper())


def _network_error(error: BaseException) -> ChatGPTError:
    reason = getattr(error, "reason", None) or error
    return ChatGPTError(str(reason) or type(reason).__name__, code="network", transient=True)


def _socket_of(response: Any) -> Any:
    """The socket under an open response (an ``HTTPError`` wraps the response one level deeper)."""
    try:
        return response.fp.raw._sock
    except AttributeError:
        return response.fp.fp.raw._sock


def _drop_connection(response: Any) -> None:
    """Make a thread that is blocked reading ``response`` return, and drop the connection (the server stops generating).

    Closing the response would wait for the blocked read (it holds the reader's lock), and on Windows ``shutdown`` does not
    wake that read either. urllib has also already "closed" the socket object (only the response's reader keeps the handle
    alive), so ``close()`` on it would do nothing: ``_real_close`` closes the handle itself, which fails the pending read.
    Nothing here waits, so it is safe on the event loop; the closing of the response itself is left to its reader."""
    if response is None:
        return
    with contextlib.suppress(Exception):
        sock = _socket_of(response)
        with contextlib.suppress(OSError):
            sock.shutdown(socket.SHUT_RDWR)
        socket.socket._real_close(sock)


def _abort(response: Any) -> None:
    """Drop the connection and close the response. Only for a thread that is not the one blocked in a read of it."""
    if response is None:
        return
    _drop_connection(response)
    with contextlib.suppress(Exception):
        response.close()


class _StreamCall:
    """One streamed request: a worker thread reads the response, the async generator that owns the call consumes the lines.

    Cancelling is a state of the call, not an event somebody has to catch: ``cancelled`` stays set for the life of the call
    and the worker looks at it before it connects, the moment it has a response, between lines and while it waits for room
    in the queue. So a response that arrives long after the cancel (a connect can take as long as ``timeout``) is closed
    unread by the worker itself, and there is no helper whose patience could run out first. A cancel also drops a connection
    that is open, which is what wakes a worker that is blocked in a read.

    The worker is a daemon thread and holds nothing the event loop needs: when the loop is gone it cancels itself."""

    def __init__(
        self,
        opener: Any,
        request: urllib.request.Request,
        timeout: float,
        loop: asyncio.AbstractEventLoop,
        wake: asyncio.Event,
        chunk: int = 0,
    ) -> None:
        self._chunk = chunk
        """0: the body is read line by line; otherwise it is read in pieces of at most this many bytes, as they arrive."""
        self._opener = opener
        self._request = request
        self._timeout = timeout
        self._loop = loop
        self._wake = wake
        self._lines: queue.Queue[tuple[str, Any]] = queue.Queue(STREAM_QUEUE_LINES)
        self._lock = threading.Lock()
        self._response: Any = None
        self.cancelled = threading.Event()

    def start(self) -> threading.Thread:
        thread = threading.Thread(target=self.run, name="chatgpt-stream", daemon=True)
        thread.start()
        return thread

    # ---------------------------------------------------------------- the consumer's side

    def take(self) -> tuple[str, Any]:
        """The next item, or ``queue.Empty``."""
        return self._lines.get_nowait()

    def cancel(self) -> None:
        """Safe to repeat and to call from any thread, at any phase, including after the end."""
        with self._lock:
            self.cancelled.set()
            response = self._response
        _drop_connection(response)
        # (a worker waiting for room gets it at once, sees the flag and goes)
        with contextlib.suppress(queue.Empty):
            while True:
                self._lines.get_nowait()

    # ---------------------------------------------------------------- the worker's side

    def run(self) -> None:
        held: Any = None
        try:
            try:
                if self.cancelled.is_set():
                    return
                held = self._opener.open(self._request, timeout=self._timeout)
                if not self._adopt(held):
                    return
                if self._chunk:
                    while True:
                        data = held.read1(self._chunk)
                        if not data or not self._put("line", data):
                            break
                    if self.cancelled.is_set():
                        return
                else:
                    for raw in held:
                        if not self._put("line", raw.decode("utf-8", "replace").rstrip("\r\n")):
                            return
                _abort(held)  # (a consumer that is slow to take the end does not keep the connection)
                self._put("end", None)
            except urllib.error.HTTPError as error:
                held = error
                if self._adopt(error):
                    self._put("error", error_from_body(error.code, error.read(), _headers_of(error.headers)))
        except (urllib.error.URLError, OSError, TimeoutError, ValueError, http.client.HTTPException) as error:
            self._put("error", _network_error(error))
        except BaseException as error:  # the consumer must not hang on anything
            self._put("error", error)
        finally:
            _abort(held)

    def _adopt(self, response: Any) -> bool:
        """Let a cancel find ``response``. False, with the response dropped, when the call was cancelled before it arrived."""
        with self._lock:
            if not self.cancelled.is_set():
                self._response = response
                return True
        _abort(response)
        return False

    def _put(self, kind: str, value: Any) -> bool:
        """Hand an item to the consumer, waiting for room. False once the call is cancelled (nothing more is wanted)."""
        item = (kind, value)
        while not self.cancelled.is_set():
            try:
                self._lines.put(item, timeout=CANCEL_POLL_SECONDS)
            except queue.Full:
                if self._loop.is_closed():  # (nobody will ever make room)
                    self.cancel()
                continue
            try:
                self._loop.call_soon_threadsafe(self._wake.set)
            except RuntimeError:  # the loop is gone
                self.cancel()
                return False
            return True
        return False


class HttpClient:
    """Requests with a fixed opener (the default one honours the proxy settings)."""

    def __init__(self, opener: urllib.request.OpenerDirector | None = None) -> None:
        self._opener = opener or urllib.request.build_opener()

    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        form: Mapping[str, str] | None = None,
        json_body: Any = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> HttpResponse:
        request = _prepare(method, url, headers, form, json_body)
        return await asyncio.to_thread(self._send, request, timeout)

    def _send(self, request: urllib.request.Request, timeout: float) -> HttpResponse:
        try:
            with self._opener.open(request, timeout=timeout) as response:
                return HttpResponse(response.status, _headers_of(response.headers), response.read())
        except urllib.error.HTTPError as error:
            with error:
                return HttpResponse(error.code, _headers_of(error.headers), error.read())
        except (urllib.error.URLError, OSError, TimeoutError, ValueError, http.client.HTTPException) as error:
            raise _network_error(error) from error

    async def stream_lines(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        json_body: Any = None,
        body: bytes | None = None,
        timeout: float = STREAM_IDLE_SECONDS,
    ) -> AsyncIterator[str]:
        """The lines of the response body, without their line ends. ``timeout`` is how long a read may wait."""
        async for line in self._stream(method, url, headers, json_body, body, timeout, 0):
            yield line

    async def stream_chunks(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        body: bytes | None = None,
        timeout: float = STREAM_IDLE_SECONDS,
    ) -> AsyncIterator[bytes]:
        """The response body as the pieces of bytes that arrive (at most 64 KiB each)."""
        async for piece in self._stream(method, url, headers, None, body, timeout, 65536):
            yield piece

    async def _stream(self, method: str, url: str, headers: Mapping[str, str] | None, json_body: Any, body: bytes | None, timeout: float, chunk: int) -> AsyncIterator[Any]:
        request = _prepare(method, url, headers, None, json_body, body)
        wake = asyncio.Event()
        call = _StreamCall(self._opener, request, timeout, asyncio.get_running_loop(), wake, chunk)
        call.start()
        try:
            while True:
                try:
                    kind, value = call.take()
                except queue.Empty:
                    # (an item put after the take above has its wake-up queued behind this step, so clearing cannot lose it)
                    wake.clear()
                    await wake.wait()
                    continue
                if kind == "line":
                    yield value
                elif kind == "end":
                    return
                else:
                    raise value
        finally:
            call.cancel()
