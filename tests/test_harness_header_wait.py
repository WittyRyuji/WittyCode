"""Header wait and cancellation use real HTTP connections with shortened deadlines."""
from __future__ import annotations

import asyncio
import threading

import pytest

from desktop.core.chatgpt import http
from desktop.core.chatgpt.errors import ChatGPTError
from tests.test_chatgpt_http import server, client, _Handler


def test_slow_headers_use_read_deadline_not_connect_deadline(server, client, monkeypatch):
    monkeypatch.setattr(http, "STREAM_CONNECT_SECONDS", .04)
    timer = threading.Timer(.18, _Handler.release.set); timer.start()
    async def go():
        stream = client.stream_lines("POST", server + "/slow-headers", json_body={}, timeout=1)
        try:
            assert await stream.__anext__() == "data: late"
        finally:
            await stream.aclose()
    try:
        asyncio.run(go())
        assert len(_Handler.seen) == 1
    finally:
        timer.join()


def test_header_timeout_after_sending_is_not_retryable_network_error(server, client, monkeypatch):
    monkeypatch.setattr(http, "STREAM_CONNECT_SECONDS", .03)
    async def go():
        return [line async for line in client.stream_lines("POST", server + "/slow-headers", json_body={}, timeout=.1)]
    with pytest.raises(ChatGPTError) as caught:
        asyncio.run(go())
    assert caught.value.code == "stream_idle" and not caught.value.transient
    assert len(_Handler.seen) == 1


@pytest.mark.parametrize("effort,expected", [(None, 120), ("low", 120), ("medium", 300), ("high", 600), ("xhigh", 600), ("max", 600)])
def test_reasoning_patience_is_bounded_by_effort(effort, expected):
    assert http.stream_idle_timeout({"reasoning": {"effort": effort}}) == expected


def test_cancel_before_headers_closes_socket_without_waiting_for_server(server, client):
    async def go():
        stream = client.stream_lines("POST", server + "/slow-headers", json_body={}, timeout=3)
        task = asyncio.create_task(stream.__anext__())
        assert await asyncio.to_thread(_Handler.arrived.wait, 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError): await task
        await stream.aclose()
        # The original handler waits on its gate; releasing it makes its recv see the closed client immediately.
        _Handler.release.set()
        assert await asyncio.to_thread(_Handler.dropped.wait, 2)
    asyncio.run(go())
