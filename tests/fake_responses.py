"""A loopback stand-in for ``api.openai.com/v1`` as the ChatGPT plan route offers it: ``GET /models`` and a scripted,
streamed ``POST /responses`` (server-sent events in the shape the real service uses, per OpenAI's Responses reference).

Usage::

    with FakeResponses() as fake:
        fake.script(say("Hello"))                       # the next request is answered with one text message
        fake.script(call("shell", {"command": "dir"}))  # ... the one after with a tool call
        url = fake.base_url                             # http://127.0.0.1:<port>/v1

``fake.requests`` keeps every request body (and ``fake.headers`` its headers) so a test can say what was sent. A turn is a
list of event dicts (``say`` / ``call`` / ``fail`` build them); ``("sleep", seconds)`` and ``("hold", name)`` entries pause
the stream (``fake.release(name)`` continues it) so a test can interrupt a turn that is in progress.
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

DEFAULT_MODELS = [
    {"slug": "gpt-6.1-sol", "display_name": "GPT-6.1-Sol", "visibility": "list", "supported_reasoning_levels": [{"effort": "low"}, {"effort": "medium"}, {"effort": "high"}], "default_reasoning_level": "medium", "context_window": 400000},
    {"slug": "gpt-6-luna", "display_name": "GPT-6-Luna", "visibility": "list", "supported_reasoning_levels": [{"effort": "low"}], "default_reasoning_level": "low"},
    {"slug": "internal-model", "display_name": "Internal", "visibility": "hide"},
]


def usage(input_tokens: int = 120, output_tokens: int = 30, cached: int = 0, reasoning: int = 0) -> dict[str, Any]:
    return {
        "input_tokens": input_tokens,
        "input_tokens_details": {"cached_tokens": cached},
        "output_tokens": output_tokens,
        "output_tokens_details": {"reasoning_tokens": reasoning},
        "total_tokens": input_tokens + output_tokens,
    }


def _completed(response_id: str, used: dict[str, Any] | None, status: str = "completed", reason: str | None = None) -> dict[str, Any]:
    response: dict[str, Any] = {"id": response_id, "status": status, "usage": used if used is not None else usage()}
    if reason:
        response["incomplete_details"] = {"reason": reason}
    return {"type": "response.incomplete" if reason else "response.completed", "response": response}


def say(text: str, *, item_id: str = "msg_1", used: dict[str, Any] | None = None, chunks: int = 2, thinking: str | None = None) -> list[Any]:
    events: list[Any] = [{"type": "response.created", "response": {"id": "resp_1", "status": "in_progress"}}]
    if thinking:
        events += [
            {"type": "response.output_item.added", "item": {"type": "reasoning", "id": "rs_1", "summary": []}},
            {"type": "response.reasoning_summary_text.delta", "item_id": "rs_1", "summary_index": 0, "delta": thinking},
            {"type": "response.output_item.done", "item": {"type": "reasoning", "id": "rs_1", "summary": [{"type": "summary_text", "text": thinking}], "encrypted_content": "enc-1"}},
        ]
    events.append({"type": "response.output_item.added", "item": {"type": "message", "id": item_id, "role": "assistant", "content": []}})
    size = max(1, -(-len(text) // chunks))
    for start in range(0, len(text), size):
        events.append({"type": "response.output_text.delta", "item_id": item_id, "content_index": 0, "delta": text[start : start + size]})
    events.append({"type": "response.output_item.done", "item": {"type": "message", "id": item_id, "role": "assistant", "status": "completed", "content": [{"type": "output_text", "text": text}]}})
    events.append(_completed("resp_1", used))
    return events


def call(name: str, arguments: dict[str, Any] | str, *, call_id: str = "call_1", item_id: str = "fc_1", text: str | None = None, used: dict[str, Any] | None = None) -> list[Any]:
    """One tool call (``arguments`` may be a raw string to send something that is not valid JSON)."""
    raw = arguments if isinstance(arguments, str) else json.dumps(arguments)
    return calls([(name, raw, call_id, item_id)], text=text, used=used)


def calls(items: list[tuple[str, str, str, str]], *, text: str | None = None, used: dict[str, Any] | None = None) -> list[Any]:
    events: list[Any] = [{"type": "response.created", "response": {"id": "resp_2", "status": "in_progress"}}]
    if text:
        events += [
            {"type": "response.output_item.added", "item": {"type": "message", "id": "msg_c", "role": "assistant", "content": []}},
            {"type": "response.output_text.delta", "item_id": "msg_c", "delta": text},
            {"type": "response.output_item.done", "item": {"type": "message", "id": "msg_c", "role": "assistant", "content": [{"type": "output_text", "text": text}]}},
        ]
    for name, raw, call_id, item_id in items:
        events += [
            {"type": "response.output_item.added", "item": {"type": "function_call", "id": item_id, "call_id": call_id, "name": name, "arguments": ""}},
            {"type": "response.function_call_arguments.delta", "item_id": item_id, "delta": raw},
            {"type": "response.function_call_arguments.done", "item_id": item_id, "arguments": raw},
            {"type": "response.output_item.done", "item": {"type": "function_call", "id": item_id, "call_id": call_id, "name": name, "arguments": raw, "status": "completed"}},
        ]
    events.append(_completed("resp_2", used))
    return events


def fail(code: str, message: str = "failed", *, param: str | None = None) -> list[Any]:
    error: dict[str, Any] = {"code": code, "message": message}
    if param:
        error["param"] = param
    return [{"type": "response.created", "response": {"id": "resp_3", "status": "in_progress"}}, {"type": "response.failed", "response": {"id": "resp_3", "status": "failed", "error": error}}]


def cut(text: str, reason: str = "max_output_tokens") -> list[Any]:
    events = say(text)[:-1]
    events.append(_completed("resp_4", None, status="incomplete", reason=reason))
    return events


class http_error:
    """A turn that is refused before any stream: an HTTP status and a JSON body."""

    def __init__(self, status: int, body: dict[str, Any], headers: dict[str, str] | None = None) -> None:
        self.status = status
        self.body = body
        self.headers = headers or {}


class FakeResponses:
    def __init__(self, models: list[dict[str, Any]] | None = None, token: str | None = None) -> None:
        self.models = models if models is not None else DEFAULT_MODELS
        self.token = token
        """When set, a request without ``Bearer <token>`` is answered 401."""
        self.requests: list[dict[str, Any]] = []
        self.headers: list[dict[str, str]] = []
        self.model_requests = 0
        self.models_status = 200
        """The HTTP status of the model list (its body is the list whatever the status: a client must not trust it)."""
        self._turns: list[Any] = []
        self._lock = threading.Lock()
        self._holds: dict[str, threading.Event] = {}
        self._server: ThreadingHTTPServer | None = None

    def script(self, *turns: Any) -> None:
        with self._lock:
            self._turns.extend(turns)

    def hold(self, name: str) -> threading.Event:
        return self._holds.setdefault(name, threading.Event())

    def release(self, name: str) -> None:
        self.hold(name).set()

    @property
    def base_url(self) -> str:
        assert self._server is not None
        return f"http://127.0.0.1:{self._server.server_address[1]}/v1"

    def __enter__(self) -> FakeResponses:
        fake = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args: Any) -> None:
                pass

            def _reply(self, status: int, body: bytes, kind: str = "application/json", extra: dict[str, str] | None = None) -> None:
                self.send_response(status)
                self.send_header("Content-Type", kind)
                self.send_header("Content-Length", str(len(body)))
                for key, value in (extra or {}).items():
                    self.send_header(key, value)
                self.end_headers()
                self.wfile.write(body)

            def _authorized(self) -> bool:
                if fake.token is None or self.headers.get("Authorization") == f"Bearer {fake.token}":
                    return True
                self._reply(401, b'{"detail": "bad token"}')
                return False

            def do_GET(self) -> None:
                if self.path.rstrip("/") == "/v1/models":
                    if not self._authorized():
                        return
                    fake.model_requests += 1
                    self._reply(fake.models_status, json.dumps({"models": fake.models}).encode())
                else:
                    self._reply(404, b'{"detail": "no such route"}')

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                if self.path.rstrip("/") != "/v1/responses":
                    self._reply(404, b'{"detail": "no such route"}')
                    return
                if not self._authorized():
                    return
                with fake._lock:
                    fake.requests.append(body)
                    fake.headers.append({key.lower(): value for key, value in self.headers.items()})
                    turn = fake._turns.pop(0) if fake._turns else say("(nothing was scripted)")
                if isinstance(turn, http_error):
                    self._reply(turn.status, json.dumps(turn.body).encode(), extra=turn.headers)
                    return
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                try:
                    for event in turn:
                        if isinstance(event, tuple):
                            if event[0] == "sleep":
                                time.sleep(event[1])
                            elif event[0] == "hold":
                                fake.hold(event[1]).wait(30)
                            continue
                        data = f"event: {event['type']}\ndata: {json.dumps(event)}\n\n".encode()
                        self.wfile.write(f"{len(data):x}\r\n".encode() + data + b"\r\n")
                        self.wfile.flush()
                    self.wfile.write(b"0\r\n\r\n")
                except OSError:
                    pass  # (the client dropped the connection: that is what an interrupt does)

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *exc: Any) -> None:
        for event in self._holds.values():
            event.set()
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
