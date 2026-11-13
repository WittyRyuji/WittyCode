"""Connection probes must not forward credentials to a redirect destination or claim inference works there."""
from __future__ import annotations

import asyncio
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from tests.test_providers import draft, make_store


@contextmanager
def server(destination=None):
    received = []
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            received.append(dict(self.headers))
            if destination:
                self.send_response(302)
                self.send_header("Location", destination)
                body = b""
            else:
                self.send_response(200)
                body = b'{"data": [{"id": "demo-model"}]}'
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        def log_message(self, *args):
            pass
    http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=http.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{http.server_port}", received
    finally:
        http.shutdown()
        http.server_close()
        thread.join(2)


@pytest.mark.parametrize("kind", ["openai", "anthropic"])
def test_connection_test_refuses_redirects_without_sending_credentials_to_the_destination(tmp_path, monkeypatch, kind):
    monkeypatch.setenv("NO_PROXY", "*")
    with server() as (destination, received), server(destination + "/models") as (origin, _):
        store = make_store(tmp_path, environ={"DEMO_KEY": "demo-key-value"})
        result = asyncio.run(store.test(draft(name="Demo", kind=kind, baseUrl=origin, keyHint="${DEMO_KEY}")))
        assert not result["ok"], "the inference transport refuses redirects, so its connection probe must too"
        assert received == [], "neither Authorization nor x-api-key may be forwarded to another origin"
