"""Helpers for the tests of the API adapters (``core/llm``): canned server-sent events and a way to read an event stream whole."""

from __future__ import annotations

import asyncio
import json
import struct
import threading
import zlib
from collections.abc import AsyncIterator, Iterable, Mapping
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from tests.fake_responses import http_error

__all__ = ["FakeApi", "collect", "event_frame", "event_stream", "http_error", "kinds", "lines_of"]


def event_frame(name: str, payload: Mapping[str, Any] | bytes | str = b"", *, kind: str = "event") -> bytes:
    """One frame of an AWS event stream (what Bedrock answers a streaming request with): prelude, headers, payload and the two CRCs.
    ``kind``: ``event`` | ``exception`` | ``error``."""
    body = payload if isinstance(payload, bytes) else (payload.encode() if isinstance(payload, str) else json.dumps(payload).encode())
    headers = {":message-type": kind, (":event-type" if kind == "event" else ":exception-type"): name, ":content-type": "application/json"}
    raw = b"".join(bytes([len(key.encode())]) + key.encode() + b"\x07" + struct.pack(">H", len(value.encode())) + value.encode() for key, value in headers.items())
    prelude = struct.pack(">II", 16 + len(raw) + len(body), len(raw))
    message = prelude + struct.pack(">I", zlib.crc32(prelude)) + raw + body
    return message + struct.pack(">I", zlib.crc32(message))


class event_stream:
    """A scripted turn that answers with an AWS event stream; each piece of bytes is one HTTP chunk (a frame may be cut anywhere)."""

    def __init__(self, *pieces: bytes) -> None:
        self.pieces = pieces


async def lines_of(chunks: Iterable[Any]) -> AsyncIterator[str]:
    """The lines of a server-sent event stream: a dict is one ``data:`` event, a string is the raw text of a ``data:`` event (``[DONE]``),
    a tuple ``(name, dict)`` is a named event (Anthropic)."""
    for chunk in chunks:
        if isinstance(chunk, tuple):
            name, payload = chunk
            yield f"event: {name}"
            yield f"data: {json.dumps(payload)}"
        elif isinstance(chunk, str):
            yield f"data: {chunk}"
        else:
            yield f"data: {json.dumps(chunk)}"
        yield ""


class FakeApi:
    """An API server on this machine: any ``POST`` is answered with the next scripted turn, a list of server-sent events (as for
    ``lines_of``) or an ``http_error``; the requests are kept (``posts``: path, lower-cased headers, JSON body)."""

    def __init__(self) -> None:
        self.posts: list[tuple[str, dict[str, str], dict[str, Any]]] = []
        self.title_posts: list[tuple[str, dict[str, str], dict[str, Any]]] = []
        self.title_turn: list[Any] = []
        self._turns: list[Any] = []
        self._lock = threading.Lock()
        self._server: ThreadingHTTPServer | None = None

    def script(self, *turns: Any) -> None:
        with self._lock:
            self._turns.extend(turns)

    @property
    def base_url(self) -> str:
        assert self._server is not None
        return f"http://127.0.0.1:{self._server.server_address[1]}/v1"

    def __enter__(self) -> FakeApi:
        fake = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args: Any) -> None:
                pass

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                from desktop.core.session_title import SYSTEM as TITLE_SYSTEM

                system = body.get("instructions") or body.get("system") or next((m.get("content") for m in body.get("messages", []) if m.get("role") == "system"), "")
                if isinstance(system, list):
                    system = "\n".join(part.get("text", "") for part in system)
                with fake._lock:
                    post = (self.path, {key.lower(): value for key, value in self.headers.items()}, body)
                    if system == TITLE_SYSTEM:
                        fake.title_posts.append(post)
                        turn = fake.title_turn
                    else:
                        fake.posts.append(post)
                        turn = fake._turns.pop(0) if fake._turns else []
                if isinstance(turn, http_error):
                    payload = json.dumps(turn.body).encode()
                    self.send_response(turn.status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(payload)))
                    for name, value in getattr(turn, "headers", {}).items():
                        self.send_header(name, value)
                    self.end_headers()
                    self.wfile.write(payload)
                    return
                if isinstance(turn, event_stream):
                    self.send_response(200)
                    self.send_header("Content-Type", "application/vnd.amazon.eventstream")
                    self.send_header("Transfer-Encoding", "chunked")
                    self.end_headers()
                    try:
                        for piece in turn.pieces:
                            self.wfile.write(f"{len(piece):x}\r\n".encode() + piece + b"\r\n")
                            self.wfile.flush()
                        self.wfile.write(b"0\r\n\r\n")
                    except OSError:
                        pass
                    return
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                text = ""
                for chunk in turn:
                    if isinstance(chunk, tuple):
                        text += f"event: {chunk[0]}\ndata: {json.dumps(chunk[1])}\n\n"
                    elif isinstance(chunk, str):
                        text += f"data: {chunk}\n\n"
                    else:
                        text += f"data: {json.dumps(chunk)}\n\n"
                data = text.encode()
                try:
                    if data:
                        self.wfile.write(f"{len(data):x}\r\n".encode() + data + b"\r\n")
                    self.wfile.write(b"0\r\n\r\n")
                except OSError:
                    pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *exc: Any) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()


def collect(stream: AsyncIterator[Any]) -> list[Any]:
    async def go() -> list[Any]:
        return [event async for event in stream]

    return asyncio.run(go())


def kinds(events: Iterable[Any]) -> list[str]:
    return [type(event).__name__ for event in events]
