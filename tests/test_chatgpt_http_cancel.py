"""Cancelling a streamed request (``core.chatgpt.http.stream_lines``) in every phase: before the connection is made, while it
is being made, while the headers are coming, in the middle of the body (of an answer and of an error), after the end.

The rule under test: a cancel is a fact that stays true for the life of the request. Whatever the worker thread has in
hand when it learns of it (a connection that arrives late, a line it cannot deliver because the consumer is stalled) it
drops, and it exits. Real sockets come from the loopback server of ``test_chatgpt_http``; the rest is a fake opener, so
nothing here reaches a real network or real credentials."""

from __future__ import annotations

import asyncio
import threading
import time
import urllib.error
from collections.abc import Callable

import pytest

from desktop.core.chatgpt import http as http_module
from desktop.core.chatgpt.errors import ChatGPTError
from desktop.core.chatgpt.http import HttpClient
from tests.test_chatgpt_http import _Handler, client, server  # noqa: F401  (fixtures)

URL = "http://stream.invalid/responses"
LINE_CAP = 5000  # (a fake that is never told to stop must still end)


class FakeResponse:
    """What urllib hands back, without a socket: iterate it for lines; closing it makes the next read fail like a closed file."""

    def __init__(self, lines: list[bytes] | None = None, *, endless: bool = False, hold: threading.Event | None = None) -> None:
        self.lines = lines or []
        self.endless = endless
        self.hold = hold  # (a silent server: after the lines, the read waits for this)
        self.closed = threading.Event()
        self.reader: threading.Thread | None = None
        self.produced = 0  # lines the reader has pulled

    def __iter__(self):
        self.reader = threading.current_thread()
        return self._read()

    def _read(self):
        total = LINE_CAP if self.endless else len(self.lines)
        while self.produced < total:
            if self.closed.is_set():
                raise ValueError("I/O operation on closed file.")
            line = f"data: {self.produced}\n".encode() if self.endless else self.lines[self.produced]
            self.produced += 1
            yield line
        if self.hold is not None:
            self.hold.wait(30)

    def close(self) -> None:
        self.closed.set()


class FakeOpener:
    """An opener that can be held in ``open`` (the connection is being made) and then answers or fails."""

    def __init__(self, response: FakeResponse | None = None, *, gate: threading.Event | None = None, error: BaseException | None = None) -> None:
        self.response = response
        self.gate = gate
        self.error = error
        self.entered = threading.Event()
        self.calls = 0
        self.timeout: float | None = None
        self.thread: threading.Thread | None = None

    def open(self, request, timeout=None):
        self.calls += 1
        self.timeout = timeout
        self.thread = threading.current_thread()
        self.entered.set()
        if self.gate is not None:
            self.gate.wait(30)
        if self.error is not None:
            raise self.error
        return self.response


def run(coroutine, seconds: float = 30.0):
    """Run a test's coroutine with a limit: a reader that is never woken must fail the test, not hang it."""
    return asyncio.run(asyncio.wait_for(coroutine, seconds))


async def until(predicate: Callable[[], bool], seconds: float = 5.0) -> None:
    deadline = time.monotonic() + seconds
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("timed out waiting for a condition")
        await asyncio.sleep(0.01)


def wait_until(predicate: Callable[[], bool], seconds: float = 5.0) -> None:
    deadline = time.monotonic() + seconds
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("timed out waiting for a condition")
        time.sleep(0.01)


def streams_since(baseline: set[threading.Thread]) -> list[threading.Thread]:
    return [t for t in threading.enumerate() if t not in baseline and t.name == "chatgpt-stream" and t.is_alive()]


def others_since(baseline: set[threading.Thread], worker: threading.Thread | None) -> list[threading.Thread]:
    """Threads that started after ``baseline`` besides the worker (a helper that cleans up after a cancel would show here)."""
    return [
        t for t in threading.enumerate()
        if t not in baseline and t is not worker and t.is_alive() and not t.name.startswith(("asyncio_", "ThreadPoolExecutor"))
    ]


async def cancel(task: asyncio.Future) -> None:
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


_spare_loops: list[asyncio.AbstractEventLoop] = []


@pytest.fixture(autouse=True)
def close_spare_loops():
    """The loops handed to calls that tests run by hand (they are never run, only closed)."""
    yield
    while _spare_loops:
        _spare_loops.pop().close()


def new_call(opener: FakeOpener, loop: asyncio.AbstractEventLoop | None = None):
    if loop is None:
        loop = asyncio.new_event_loop()
        _spare_loops.append(loop)
    request = http_module._prepare("POST", URL, None, None, {})
    return http_module._StreamCall(opener, request, 30.0, loop, asyncio.Event())


# ------------------------------------------------------------------------------------------------ before and while connecting


def test_a_cancel_before_the_worker_connects_means_it_never_connects():
    opener = FakeOpener(FakeResponse([b"data: x\n"]))
    call = new_call(opener)
    call.cancel()
    call.run()
    assert opener.calls == 0
    assert call.cancelled.is_set()


def test_the_worker_is_a_daemon_so_a_stuck_connect_cannot_keep_the_application_alive():
    gate = threading.Event()
    call = new_call(FakeOpener(FakeResponse(), gate=gate))
    thread = call.start()
    try:
        assert thread.daemon and thread.name == "chatgpt-stream"
    finally:
        call.cancel()
        gate.set()


def test_a_connection_that_arrives_after_the_cancel_is_closed_without_being_read():
    # The connection is held up past whatever patience a cleanup helper could have: the cancel must not depend on one.
    baseline = set(threading.enumerate())
    gate = threading.Event()
    response = FakeResponse([b"data: late\n"])
    opener = FakeOpener(response, gate=gate)

    async def go() -> None:
        stream = HttpClient(opener).stream_lines("POST", URL, json_body={})
        pending = asyncio.ensure_future(stream.__anext__())
        await until(opener.entered.is_set)
        await cancel(pending)
        await until(lambda: not others_since(baseline, opener.thread), seconds=30)
        gate.set()
        await until(response.closed.is_set)

    try:
        run(go(), 90)
    finally:
        gate.set()
    assert response.reader is None  # (not one line was read)
    opener.thread.join(5)
    assert not opener.thread.is_alive()


def test_a_cancel_while_the_headers_are_still_coming_hangs_up_when_they_arrive(server, client):
    async def go() -> None:
        baseline = set(threading.enumerate())
        stream = client.stream_lines("POST", server + "/slow-headers", json_body={})
        pending = asyncio.ensure_future(stream.__anext__())
        await until(_Handler.arrived.is_set)
        await cancel(pending)
        _Handler.release.set()
        await until(_Handler.dropped.is_set)
        await until(lambda: not streams_since(baseline))

    run(go())


# ------------------------------------------------------------------------------------------------ in the body


def test_closing_a_stream_in_the_middle_of_the_body_hangs_up_on_the_server(server, client):
    async def go() -> float:
        baseline = set(threading.enumerate())
        stream = client.stream_lines("POST", server + "/hang-watch", json_body={})
        assert await stream.__anext__() == "data: first"
        started = time.monotonic()
        await stream.aclose()
        await until(_Handler.dropped.is_set)
        await until(lambda: not streams_since(baseline))
        return time.monotonic() - started

    assert run(go()) < 5


def test_a_worker_blocked_in_a_read_returns_even_if_the_server_never_answers_the_hang_up(server, client):
    # ("/hang" holds the connection for 10 s and does not notice the client leaving: only closing the handle here can wake the read)
    async def go() -> None:
        baseline = set(threading.enumerate())
        stream = client.stream_lines("POST", server + "/hang", json_body={})
        assert await stream.__anext__() == "data: first"
        await stream.aclose()
        await until(lambda: not streams_since(baseline), seconds=3)

    run(go())


def test_a_cancel_in_the_middle_of_an_error_body_hangs_up_too(server, client):
    async def go() -> None:
        baseline = set(threading.enumerate())
        stream = client.stream_lines("POST", server + "/error-hang", json_body={})
        pending = asyncio.ensure_future(stream.__anext__())
        await until(_Handler.partial.is_set)
        await asyncio.sleep(0.3)  # (the worker is reading the rest of the body)
        await cancel(pending)
        await until(_Handler.dropped.is_set)
        await until(lambda: not streams_since(baseline))

    run(go())


def test_an_error_body_that_is_cut_short_ends_the_stream_with_an_error(server, client):
    async def read() -> None:
        async for _ in client.stream_lines("POST", server + "/error-cut", json_body={}):
            pass

    with pytest.raises(ChatGPTError) as caught:
        asyncio.run(asyncio.wait_for(read(), 10))
    assert caught.value.code == "network", "a cut body is a network problem for the user, not an internal error"


# ------------------------------------------------------------------------------------------------ after the end


def test_cancelling_after_the_end_is_harmless_and_repeatable():
    response = FakeResponse([b"data: a\n", b"\n"])
    opener = FakeOpener(response)

    async def go() -> None:
        stream = HttpClient(opener).stream_lines("POST", URL, json_body={})
        assert [line async for line in stream] == ["data: a", ""]
        await stream.aclose()
        await stream.aclose()

    run(go())
    assert response.closed.is_set()
    opener.thread.join(5)
    assert not opener.thread.is_alive()


def test_cancel_may_be_called_again_and_again_on_a_finished_call():
    response = FakeResponse([b"data: a\n"])
    call = new_call(FakeOpener(response))
    call.run()
    for _ in range(3):
        call.cancel()
    assert call.cancelled.is_set() and response.closed.is_set()


def test_a_finished_response_is_not_held_open_by_a_consumer_that_has_not_read_it(monkeypatch):
    monkeypatch.setattr(http_module, "STREAM_QUEUE_LINES", 2)
    response = FakeResponse([b"data: a\n", b"data: b\n"])
    call = new_call(FakeOpener(response))
    call.start()
    try:
        wait_until(response.closed.is_set)  # (both lines are waiting; the end is waiting for room)
    finally:
        call.cancel()


# ------------------------------------------------------------------------------------------------ back-pressure


def test_a_stalled_consumer_holds_the_worker_back_and_loses_nothing(monkeypatch):
    monkeypatch.setattr(http_module, "STREAM_QUEUE_LINES", 4)
    response = FakeResponse(endless=True)
    opener = FakeOpener(response)

    async def go() -> None:
        stream = HttpClient(opener).stream_lines("POST", URL, json_body={})
        assert await stream.__anext__() == "data: 0"
        await until(lambda: response.produced >= 6)
        await asyncio.sleep(0.3)
        assert response.produced == 6  # one read + four waiting + one in the worker's hand
        for number in range(1, 40):
            assert await stream.__anext__() == f"data: {number}"
        await stream.aclose()

    run(go())
    wait_until(response.closed.is_set)


def test_a_cancel_reaches_a_worker_that_is_waiting_for_room_in_the_queue(monkeypatch):
    # (the wait for room is very long here: the cancel itself has to make room)
    monkeypatch.setattr(http_module, "STREAM_QUEUE_LINES", 2)
    monkeypatch.setattr(http_module, "CANCEL_POLL_SECONDS", 60.0)
    response = FakeResponse(endless=True)
    opener = FakeOpener(response)

    async def go() -> None:
        stream = HttpClient(opener).stream_lines("POST", URL, json_body={})
        assert await stream.__anext__() == "data: 0"
        await until(lambda: response.produced >= 4)
        await asyncio.sleep(0.1)
        await stream.aclose()
        await until(response.closed.is_set, seconds=3)

    run(go())
    reads = response.produced
    time.sleep(0.2)
    assert response.produced == reads  # (it did not go on reading)
    opener.thread.join(3)
    assert not opener.thread.is_alive()


def test_the_worker_stops_reading_when_it_is_cancelled_between_lines():
    response = FakeResponse(endless=True)
    call = new_call(FakeOpener(response))
    thread = call.start()
    wait_until(lambda: response.produced > 50)
    call.cancel()
    thread.join(5)
    assert not thread.is_alive() and response.closed.is_set()
    assert response.produced < LINE_CAP


def test_the_worker_stops_when_the_event_loop_it_serves_is_gone(monkeypatch):
    monkeypatch.setattr(http_module, "STREAM_QUEUE_LINES", 2)
    response = FakeResponse(endless=True)
    loop = asyncio.new_event_loop()
    call = new_call(FakeOpener(response), loop)
    thread = call.start()
    wait_until(lambda: response.produced >= 3)  # (the queue is full and nobody reads it)
    loop.close()
    thread.join(5)
    assert not thread.is_alive() and response.closed.is_set()


def test_a_worker_whose_loop_is_gone_does_not_read_on(monkeypatch):
    monkeypatch.setattr(http_module, "STREAM_QUEUE_LINES", 50)
    response = FakeResponse(endless=True)
    loop = asyncio.new_event_loop()
    loop.close()
    call = new_call(FakeOpener(response), loop)
    thread = call.start()
    thread.join(5)
    assert not thread.is_alive() and response.closed.is_set()
    assert response.produced == 1


def test_the_default_bounds_are_finite():
    assert 0 < http_module.STREAM_QUEUE_LINES <= 4096
    assert 0 < http_module.CANCEL_POLL_SECONDS <= 0.5


def test_a_reader_waiting_for_a_slow_server_sleeps_instead_of_spinning():
    hold = threading.Event()
    response = FakeResponse([b"data: only\n"], hold=hold)
    opener = FakeOpener(response)

    async def go() -> float:
        stream = HttpClient(opener).stream_lines("POST", URL, json_body={})
        assert await stream.__anext__() == "data: only"
        waiting = asyncio.ensure_future(stream.__anext__())
        await asyncio.sleep(0.05)
        started = time.process_time()
        await asyncio.sleep(0.6)
        spent = time.process_time() - started
        await cancel(waiting)
        return spent

    try:
        assert run(go()) < 0.3
    finally:
        hold.set()


def test_the_idle_timeout_is_what_the_opener_is_given():
    opener = FakeOpener(FakeResponse([b"data: a\n"]))

    async def read() -> None:
        async for _ in HttpClient(opener).stream_lines("POST", URL, json_body={}, timeout=7.5):
            pass

    run(read())
    assert opener.timeout == 7.5


# ------------------------------------------------------------------------------------------------ failures still arrive


def test_a_connection_that_cannot_be_made_is_delivered_as_a_network_error():
    opener = FakeOpener(error=urllib.error.URLError(ConnectionRefusedError("refused")))

    async def read() -> None:
        async for _ in HttpClient(opener).stream_lines("POST", URL, json_body={}):
            pass

    with pytest.raises(ChatGPTError) as caught:
        asyncio.run(asyncio.wait_for(read(), 10))
    assert caught.value.code == "network"


def test_an_unexpected_failure_in_the_worker_reaches_the_reader_instead_of_hanging_it():
    opener = FakeOpener(error=RuntimeError("surprise"))

    async def read() -> None:
        async for _ in HttpClient(opener).stream_lines("POST", URL, json_body={}):
            pass

    with pytest.raises(RuntimeError, match="surprise"):
        asyncio.run(asyncio.wait_for(read(), 10))
