"""``core.chatgpt.http`` against a real loopback server: JSON and form bodies, HTTP errors, streams, dropping a stream."""

from __future__ import annotations

import asyncio
import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from desktop.core.chatgpt.errors import ChatGPTError, error_from_body, friendly_failure
from desktop.core.chatgpt.http import HttpClient


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    seen: list[dict] = []
    release = threading.Event()
    arrived = threading.Event()  # (a request is in, the answer is being held back)
    partial = threading.Event()  # (the first part of an error body is out, the rest is held back)
    dropped = threading.Event()  # (the client went away while the answer was being held back)

    def log_message(self, *args) -> None:  # silence
        pass

    def _watch(self) -> None:
        """Hold the connection until the client goes away (a server would stop generating then) and say so."""
        self.connection.settimeout(10)
        try:
            gone = self.connection.recv(1024) == b""
        except socket.timeout:
            gone = False
        except OSError:  # (reset: the client closed with data unread)
            gone = True
        if gone:
            type(self).dropped.set()

    def _body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b""

    def _send(self, status: int, body: bytes, kind: str = "application/json", extra: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        body = self._body()
        type(self).seen.append({"path": self.path, "type": self.headers.get("Content-Type"), "body": body, "agent": self.headers.get("User-Agent")})
        if self.path == "/echo":
            self._send(200, body)
        elif self.path == "/limit":
            error = {"error": {"code": "subscription_sharing_usage_limit_exceeded", "message": "limit"}}
            self._send(429, json.dumps(error).encode(), extra={"x-request-id": "req_1"})
        elif self.path == "/detail":
            self._send(403, b'{"detail": "region"}')
        elif self.path == "/stream":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            for text in ("data: one\r\n\r\n", "data: two\n\n"):
                chunk = text.encode()
                self.wfile.write(f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n")
                self.wfile.flush()
            self.wfile.write(b"0\r\n\r\n")
        elif self.path == "/hang":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            chunk = b"data: first\n\n"
            self.wfile.write(f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n")
            self.wfile.flush()
            type(self).release.wait(10)
        elif self.path == "/hang-watch":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            chunk = b"data: first\n\n"
            self.wfile.write(f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n")
            self.wfile.flush()
            self._watch()
        elif self.path == "/slow-headers":
            type(self).arrived.set()
            type(self).release.wait(10)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            chunk = b"data: late\n\n"
            self.wfile.write(f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n")
            self.wfile.flush()
            self._watch()
        elif self.path in ("/error-hang", "/error-cut"):
            self.send_response(500)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", "100")
            self.end_headers()
            self.wfile.write(b"oops")
            self.wfile.flush()
            type(self).partial.set()
            if self.path == "/error-hang":
                self._watch()
            self.close_connection = True
        else:
            self._send(404, b"nothing here", "text/plain")


@pytest.fixture()
def server():
    _Handler.seen = []
    _Handler.release = threading.Event()
    _Handler.arrived = threading.Event()
    _Handler.partial = threading.Event()
    _Handler.dropped = threading.Event()
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    _Handler.release.set()
    httpd.shutdown()
    httpd.server_close()


@pytest.fixture()
def client():
    import urllib.request

    # (no proxy: the machine's own settings must not decide a loopback test)
    return HttpClient(urllib.request.build_opener(urllib.request.ProxyHandler({})))


def test_json_body_is_sent_as_utf8_json(server, client):
    response = asyncio.run(client.request("POST", server + "/echo", json_body={"text": "你好"}))
    assert response.status == 200
    assert response.json() == {"text": "你好"}
    assert _Handler.seen[0]["type"] == "application/json"
    assert _Handler.seen[0]["agent"] == "WittyCode"


def test_form_body_is_urlencoded(server, client):
    asyncio.run(client.request("POST", server + "/echo", form={"grant_type": "refresh_token", "resource": "https://x/v1"}))
    assert _Handler.seen[0]["type"] == "application/x-www-form-urlencoded"
    assert _Handler.seen[0]["body"] == b"grant_type=refresh_token&resource=https%3A%2F%2Fx%2Fv1"


def test_an_http_error_is_a_response_not_an_exception(server, client):
    response = asyncio.run(client.request("POST", server + "/limit", json_body={}))
    assert response.status == 429
    assert response.headers["x-request-id"] == "req_1"
    assert response.json()["error"]["code"] == "subscription_sharing_usage_limit_exceeded"


def test_a_connection_that_cannot_be_made_is_a_network_error(client):
    with pytest.raises(ChatGPTError) as caught:
        asyncio.run(client.request("GET", "http://127.0.0.1:9/", timeout=3))
    assert caught.value.code == "network"


def test_stream_yields_lines_without_their_ends(server, client):
    async def read() -> list[str]:
        return [line async for line in client.stream_lines("POST", server + "/stream", json_body={})]

    assert asyncio.run(read()) == ["data: one", "", "data: two", ""]


def test_stream_raises_the_error_of_a_failed_request_with_its_facts(server, client):
    async def read() -> None:
        async for _ in client.stream_lines("POST", server + "/limit", json_body={}):
            pass

    with pytest.raises(ChatGPTError) as caught:
        asyncio.run(read())
    error = caught.value
    assert (error.status, error.code, error.request_id) == (429, "subscription_sharing_usage_limit_exceeded", "req_1")
    assert "用量" in friendly_failure(error)


def test_a_detail_body_is_kept_as_the_message(server, client):
    async def read() -> None:
        async for _ in client.stream_lines("POST", server + "/detail", json_body={}):
            pass

    with pytest.raises(ChatGPTError) as caught:
        asyncio.run(read())
    assert caught.value.message == "region" and caught.value.status == 403 and caught.value.code is None


def test_closing_a_stream_drops_the_connection_while_the_server_is_silent(server, client):
    async def read() -> float:
        started = time.monotonic()
        stream = client.stream_lines("POST", server + "/hang", json_body={})
        assert await stream.__anext__() == "data: first"
        await stream.aclose()
        return time.monotonic() - started

    # (the server holds the connection for 10 s: closing must not wait for it)
    assert asyncio.run(read()) < 5


def test_error_from_body_reads_the_oauth_shape_and_plain_text():
    oauth = error_from_body(400, b'{"error": "invalid_grant", "error_description": "gone"}', {})
    assert (oauth.code, oauth.message) == ("invalid_grant", "gone")
    plain = error_from_body(502, b"bad gateway", {})
    assert (plain.code, plain.message, plain.status) == (None, "bad gateway", 502)
    param = error_from_body(400, b'{"error": {"code": "subscription_sharing_unsupported_capability", "message": "no", "param": "tools[0]"}}', {})
    assert "tools[0]" in param.message
