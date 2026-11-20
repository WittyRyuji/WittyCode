"""Real fake subprocesses, including a fresh Windows qasync loop that must close within a deadline.

Each probe is isolated so an IOCP close regression fails with a timeout instead of hanging the pytest process.
No engine binary, user profile or model service is involved.
"""

from __future__ import annotations

import asyncio
import contextlib
import gc
import os
import subprocess
import sys
import tempfile
import textwrap
import warnings
from pathlib import Path

import pytest

from desktop.core.engine_errors import EngineProcessError
from desktop.core.jsonrpc import JsonRpcClient

EOF_SERVER = "import sys, time; sys.stdin.read(); time.sleep(0.2)"


@pytest.mark.parametrize("loop_kind", ["asyncio", "qasync"])
@pytest.mark.parametrize(
    "case",
    [
        "close_during_start", "cancelled_start", "duplicate_start", "cancelled_close", "close_before_start",
        "drain_pipes", "inherited_pipes", "blocked_stdin", "handler_close", "failed_start",
    ],
)
def test_subprocess_shutdown_is_owned_and_bounded(loop_kind, case):
    run_isolated_probe(loop_kind, case)


def test_application_run_cleans_background_processes_and_timers_before_loop_close():
    run_isolated_probe("qasync", "app_run")


def run_isolated_probe(loop_kind, case):
    result = subprocess.run(
        [sys.executable, "-m", "tests.test_jsonrpc_shutdown", loop_kind, case],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=15,
        env={**os.environ, "QT_QPA_PLATFORM": "offscreen", "PYTHONASYNCIODEBUG": "1"},
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "shutdown settled" in result.stdout
    assert "Traceback" not in result.stderr and "Exception ignored" not in result.stderr, result.stderr


def assert_settled(client):
    process = client._process
    assert process is not None and process.returncode is not None, "the owned child is still running"
    transport = process._transport
    assert transport.is_closing(), "the subprocess transport was left open"
    assert transport._loop is None, "connection_lost has not finished before close returned"
    assert process._protocol._transport is None, "the stream protocol still owns the subprocess transport"
    assert process._protocol._stdin_closed.done(), "stdin has not disconnected"
    for fd in (0, 1, 2):
        pipe = transport.get_pipe_transport(fd)
        assert pipe is not None and pipe.is_closing(), f"pipe {fd} was left open"
    assert not [task for task in (*client._tasks, *client._serving) if not task.done()]


async def probe(case):
    loop = asyncio.get_running_loop()
    errors = []
    loop.set_exception_handler(lambda _loop, context: errors.append(context))
    clients = []
    transports = []
    start_tasks = []
    release = asyncio.Event()
    created = asyncio.Event()
    original = loop.subprocess_exec
    inherited_release = None
    inherited_done = None

    async def until(predicate):
        async def wait():
            while not predicate():
                await asyncio.sleep(0.01)
        await asyncio.wait_for(wait(), 30)

    async def delayed_spawn(*args, **kwargs):
        transport, protocol = await original(*args, **kwargs)
        transports.append(transport)
        created.set()
        await release.wait()  # the child exists, but create_subprocess_exec has not handed it to its caller yet
        return transport, protocol

    def client(code=EOF_SERVER):
        rpc = JsonRpcClient([sys.executable, "-u", "-c", code], name="fake rpc")
        clients.append(rpc)
        return rpc

    try:
        if case in {"close_during_start", "cancelled_start", "duplicate_start"}:
            loop.subprocess_exec = delayed_spawn
            rpc = client()
            starting = asyncio.create_task(rpc.start())
            start_tasks.append(starting)
            await asyncio.wait_for(created.wait(), 3)
            if case == "close_during_start":
                closing = asyncio.create_task(rpc.close(grace=0.05))
                await asyncio.sleep(0.03)
                finished_too_soon = closing.done()
                release.set()
                await closing
                outcome = await asyncio.gather(starting, return_exceptions=True)
                assert not finished_too_soon, "close returned while a subprocess was still being acquired"
                assert isinstance(outcome[0], EngineProcessError) and outcome[0].closed
            elif case == "cancelled_start":
                starting.cancel()
                loop.call_later(0.03, release.set)
                with pytest.raises(asyncio.CancelledError):
                    await starting
                assert rpc._close_task is not None and rpc._close_task.done(), "cancelled start abandoned its child"
            else:
                duplicate = asyncio.create_task(rpc.start())
                start_tasks.append(duplicate)
                await asyncio.sleep(0.03)
                release.set()
                outcomes = await asyncio.gather(starting, duplicate, return_exceptions=True)
                assert len(transports) == 1, "concurrent start created an unowned second process"
                assert isinstance(outcomes[1], RuntimeError)
                await rpc.close(grace=0.05)
            assert_settled(rpc)
        elif case == "cancelled_close":
            rpc = client()
            await rpc.start()
            closing = asyncio.create_task(rpc.close())
            other = asyncio.create_task(rpc.close())
            await asyncio.sleep(0.03)
            closing.cancel()
            loop.call_later(0.03, closing.cancel)  # repeated cancellation must not abandon shielded cleanup either
            with pytest.raises(asyncio.CancelledError):
                await closing
            assert rpc._close_task is not None and rpc._close_task.done(), "cancelled close returned before cleanup"
            assert_settled(rpc)
            await other
            await rpc.close()  # another caller still observes the same completed close
        elif case == "close_before_start":
            rpc = client()
            await rpc.close()
            with pytest.raises(EngineProcessError) as ended:
                await rpc.start()
            assert ended.value.closed and rpc.pid is None
        elif case == "drain_pipes":
            code = textwrap.dedent('''
                import json, sys
                sys.stdin.read()
                for _ in range(64):
                    print(json.dumps({"method": "tail", "params": "x" * 8192}), flush=True)
                    print("e" * 8192, file=sys.stderr, flush=True)
            ''')
            notes = []
            rpc = client(code)
            rpc.on_notification = lambda method, params: notes.append(method)
            await rpc.start()
            await asyncio.wait_for(rpc.close(), 3)
            assert rpc.exit_code == 0 and len(notes) == 64, "shutdown failed to drain both output pipes"
            assert rpc.stderr_tail
            assert_settled(rpc)
        elif case == "inherited_pipes":
            with tempfile.TemporaryDirectory(prefix="rpc-pipes-") as folder:
                inherited_release = Path(folder) / "release"
                inherited_done = Path(folder) / "done"
                holder = textwrap.dedent(f'''
                    import pathlib, time
                    release = pathlib.Path({str(inherited_release)!r})
                    end = time.monotonic() + 10
                    while not release.exists() and time.monotonic() < end:
                        time.sleep(0.01)
                    pathlib.Path({str(inherited_done)!r}).touch()
                ''')
                # The direct child exits, but its helper legitimately keeps stdout and stderr open. Closing our
                # endpoints must not wait for, terminate or kill the helper (or any unrelated process).
                code = f"import subprocess, sys; subprocess.Popen([sys.executable, '-c', {holder!r}], stdin=subprocess.DEVNULL)"
                rpc = client(code)
                try:
                    await rpc.start()
                    await until(lambda: rpc._process.returncode is not None)
                    await asyncio.wait_for(rpc.close(grace=0.05), 3)
                    assert_settled(rpc)
                    assert not inherited_done.exists(), "cleanup terminated a helper it did not own"
                finally:
                    inherited_release.touch()
                    await until(inherited_done.exists)
        elif case == "blocked_stdin":
            rpc = client("import time; time.sleep(10)")
            await rpc.start()
            writing = asyncio.create_task(rpc.notify("large", "x" * (1 << 20)))
            await asyncio.sleep(0.03)
            await asyncio.wait_for(rpc.close(grace=0.05), 3)
            assert_settled(rpc)
            result = await asyncio.gather(writing, return_exceptions=True)
            assert isinstance(result[0], EngineProcessError), "the blocked write was not released"
        elif case == "handler_close":
            rpc = client('import sys; print(\'{"method":"stop"}\', flush=True); sys.stdin.read()')
            returned = asyncio.Event()

            async def stop(method, params):
                await rpc.close(grace=0.05)
                returned.set()

            rpc.on_notification = stop
            await rpc.start()
            await asyncio.wait_for(returned.wait(), 3)
            await until(lambda: not rpc._tasks)
            assert_settled(rpc)
        elif case == "failed_start":
            rpc = client()
            rpc.argv[0] = str(Path(tempfile.gettempdir()) / "no-rpc-binary" / "python")
            with pytest.raises(EngineProcessError):
                await rpc.start()
            await rpc.close(grace=0.05)
            assert rpc.pid is None
        else:
            raise AssertionError(case)
        await asyncio.sleep(0)
        assert not errors, errors
        assert not [task for task in asyncio.all_tasks() if task is not asyncio.current_task() and not task.done()]
    finally:
        loop.subprocess_exec = original
        release.set()
        # Even the old, intentionally failing implementation must not leave a fake process behind.
        await asyncio.gather(*start_tasks, return_exceptions=True)
        for rpc in clients:
            with contextlib.suppress(Exception):
                await rpc.close(grace=0.01)
            if rpc._process is not None:
                transports.append(rpc._process._transport)
        for transport in transports:
            transport.close()
        await asyncio.sleep(0.1)


def probe_app_run(app, loop):
    from argparse import Namespace
    from PySide6.QtWidgets import QWidget

    from desktop.ui import app as application
    from desktop.ui.tasks import kill_pending_timers, spawn

    rpc = JsonRpcClient([sys.executable, "-u", "-c", EOF_SERVER], name="fake rpc")
    original_close = loop.close

    class Window(QWidget):
        def __init__(self):
            super().__init__()
            self.closed = asyncio.Event()

        def load(self):
            async def operation():
                try:
                    await rpc.start()
                    self.closed.set()
                    await asyncio.Future()
                finally:
                    await rpc.close(grace=0.05)
            spawn(operation())
            loop.call_later(60, lambda: None)  # must be killed before qasync.close, not after it

    def closing():
        assert not loop._timer._SimpleTimer__callbacks, "Qt timers were still armed when loop.close began"
        assert_settled(rpc)
        original_close()

    loop.close = closing
    window = Window()
    try:
        context = application.AppContext(app, loop, None, window, Namespace())
        assert application.run(context) == 0
        assert loop.is_closed()
    finally:
        loop.close = original_close
        kill_pending_timers(loop)
        original_close()


def run_probe(loop_kind, case):
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", ResourceWarning)
        if loop_kind == "asyncio":
            asyncio.run(probe(case))
        else:
            from PySide6 import QtWebEngineWidgets  # noqa: F401 (must precede QApplication)
            from PySide6.QtWidgets import QApplication
            import qasync

            from desktop.ui.tasks import kill_pending_timers

            app = QApplication(["rpc-shutdown-test"])
            app.setQuitOnLastWindowClosed(False)
            loop = qasync.QEventLoop(app)
            asyncio.set_event_loop(loop)
            try:
                if case == "app_run":
                    probe_app_run(app, loop)
                else:
                    loop.run_until_complete(probe(case))
            finally:
                kill_pending_timers(loop)
                loop.close()  # this is where the reported Windows IOCP hang happened
                asyncio.set_event_loop(None)
        gc.collect()
        assert not [warning for warning in caught if issubclass(warning.category, ResourceWarning)], caught
    print("shutdown settled", flush=True)


if __name__ == "__main__":
    run_probe(*sys.argv[1:])
