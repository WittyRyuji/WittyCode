"""Qt fixtures: one offscreen QApplication for the whole run and a qasync loop per test."""

from __future__ import annotations

import asyncio
import faulthandler
import io
import os
import threading

import pytest
from _pytest import faulthandler as faulthandler_plugin

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_LOGGING_RULES", "qt.webenginecontext.debug=false;*.debug=false")
# Software rendering: the offscreen platform has no usable GPU surface and Chromium floods stderr trying.
os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS", "--disable-gpu --disable-logging")


@pytest.fixture(autouse=True)
def _no_skills_of_the_real_user(tmp_path_factory, monkeypatch):
    """The user's own skills and commands (``~/.factory/skills`` …) are not part of any test: what the instructions or a message
    carry must not depend on the machine the tests run on. A test of skills says whose folders it means (``skills.USER_HOME``)."""
    from desktop.core.harness import skills

    monkeypatch.setattr(skills, "USER_HOME", tmp_path_factory.mktemp("no-home"))


@pytest.fixture(autouse=True)
def _no_files_of_the_real_user(tmp_path_factory, monkeypatch):
    """WittyCode's own folder (the settings, the API providers' models file) is the test's, never the user's: whatever a
    test builds with the default locations reads and writes a folder of its own."""
    monkeypatch.setenv("WITTYCODE_SETTINGS", str(tmp_path_factory.mktemp("app") / "settings.json"))


@pytest.fixture(scope="session")
def qapp():
    pytest.importorskip("PySide6.QtWebEngineWidgets")
    from PySide6 import QtWebEngineWidgets  # noqa: F401  (must be imported before the QApplication exists)
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication(["wittycode-tests"])
    # Destroying a test's last window must not end the event loop that is still awaiting the test.
    app.setQuitOnLastWindowClosed(False)
    yield app


HANG_SECONDS = 75

_config: pytest.Config | None = None


def pytest_configure(config: pytest.Config) -> None:
    global _config
    _config = config


def _stderr_fd() -> int:
    """The stderr pytest started with (the one its faulthandler plugin keeps). fd 2 itself is captured during a test, and
    whatever is written there is thrown away with the capture when a hung process has to be killed."""
    key = getattr(faulthandler_plugin, "fault_handler_stderr_fd_key", None)
    fd = _config.stash.get(key, None) if _config is not None and key is not None else None
    return fd if fd is not None else 2


def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    """A failure is printed by pytest only when the whole session ends: say it at once, so that a run that hangs after
    a failing test (and is killed) still leaves the reason behind."""
    if report.failed:
        os.write(_stderr_fd(), f"\n=== FAILED {report.nodeid} ({report.when}) ===\n{report.longreprtext}\n".encode("utf-8", "replace"))


def _report_hang(event_loop, finished: threading.Event, phase: dict[str, str]) -> None:
    """A test whose event loop stops answering is a hang, not a failure: nothing times out, nothing is reported.
    After ``HANG_SECONDS`` (the test and its teardown together) say what every task is awaiting, how the loop's timers
    stand and where every thread is."""
    if finished.wait(HANG_SECONDS):
        return
    fd = _stderr_fd()
    out = io.StringIO()
    timer = getattr(event_loop, "_timer", None)
    callbacks = getattr(timer, "_SimpleTimer__callbacks", None)
    print(f"\n=== HANG: a test has been running for {HANG_SECONDS} s (phase: {phase['name']}) ===", file=out)
    print(f"loop closed={event_loop.is_closed()} timer stopped={getattr(timer, '_stopped', '?')} armed={len(callbacks or ())}", file=out)
    for task in list(asyncio.all_tasks(event_loop)):
        print(f"--- {task!r}", file=out)
        task.print_stack(file=out)
    os.write(fd, out.getvalue().encode("utf-8", "replace"))
    faulthandler.dump_traceback(file=fd, all_threads=True)


@pytest.fixture
def loop(qapp):
    import qasync

    from desktop.ui.tasks import kill_pending_timers

    event_loop = qasync.QEventLoop(qapp)
    asyncio.set_event_loop(event_loop)
    lost_steps: list[str] = []

    def on_loop_error(failing_loop, context):
        text = str(context.get("exception") or context.get("message"))
        if "Cannot enter into task" in text:
            lost_steps.append(text)
        failing_loop.default_exception_handler(context)

    event_loop.set_exception_handler(on_loop_error)
    finished = threading.Event()
    phase = {"name": "test"}
    threading.Thread(target=_report_hang, args=(event_loop, finished, phase), daemon=True).start()
    yield event_loop
    phase["name"] = "teardown"
    pending = [task for task in asyncio.all_tasks(event_loop) if not task.done()]
    for task in pending:
        task.cancel()
    if pending:
        # A task that was lost to nested event processing can never finish. asyncio.wait gives up after the timeout and
        # leaves it; wait_for would cancel the gather and then wait for the lost task for ever (the teardown hung).
        event_loop.run_until_complete(asyncio.wait(pending, timeout=3))
    # One loop per test: a timer left armed fires into the next test and, once this loop is collected, into freed memory.
    kill_pending_timers(event_loop)
    qapp.processEvents()
    event_loop.close()
    finished.set()
    asyncio.set_event_loop(None)
    if lost_steps:
        # Nested event processing (QTest input, sendEvent) ran another task's step inside this one: that task is lost
        # for good. Drive Qt input through tests.ui_support.outside_task.
        pytest.fail("a task step was lost to nested event processing: " + lost_steps[0], pytrace=False)