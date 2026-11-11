"""Timers a qasync loop leaves armed (``desktop/ui/tasks.py``)."""

from __future__ import annotations

import asyncio
import gc
import time

import pytest
from PySide6.QtCore import QPoint
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QWidget

from desktop.ui.tasks import kill_pending_timers
from tests.ui_support import outside_task

pytestmark = pytest.mark.ui


def pump(app, seconds: float) -> None:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        app.processEvents()
        time.sleep(0.005)


def test_a_timer_that_is_still_armed_fires_without_the_helper(qapp, loop):
    """The hazard itself: qasync delivers a pending ``call_later`` whether or not the loop is running."""
    fired: list[int] = []
    loop.call_later(0.02, fired.append, 1)
    pump(qapp, 0.3)
    assert fired == [1], "if this stops being true, qasync changed and kill_pending_timers may be unnecessary"


def test_pending_timers_are_killed_and_never_fire(qapp, loop):
    fired: list[int] = []
    loop.call_later(0.05, fired.append, 1)
    loop.call_later(0.1, fired.append, 2)
    assert loop._timer._SimpleTimer__callbacks, "the handles are armed on qasync's timer"
    kill_pending_timers(loop)
    assert not loop._timer._SimpleTimer__callbacks
    pump(qapp, 0.4)
    assert fired == []


def run_background_while_clicking(loop, qapp, click) -> tuple[str, list[str]]:
    """A task that wakes up all the time, while another task makes Qt's synchronous mouse input (``click(widget)``)."""
    errors: list[str] = []
    loop.set_exception_handler(lambda _loop, context: errors.append(str(context.get("exception") or context.get("message"))))
    widget = QWidget()
    widget.resize(100, 100)
    widget.show()
    steps = [0]

    async def background() -> None:
        for _ in range(300):
            await asyncio.sleep(0)
            steps[0] += 1

    async def scenario() -> str:
        task = asyncio.ensure_future(background())
        for _ in range(100):
            await click(widget)
            await asyncio.sleep(0)
        try:
            await asyncio.wait_for(asyncio.shield(task), 1.5)
        except asyncio.TimeoutError:
            return f"lost after {steps[0]} steps"
        return "finished"

    try:
        return loop.run_until_complete(scenario()), errors
    finally:
        widget.close()
        gc.collect()  # a lost task nobody refers to is dropped here, not waited for in the fixture's teardown


@pytest.mark.filterwarnings("ignore:coroutine .* was never awaited:RuntimeWarning")  # the lost task is the point
def test_qt_input_inside_a_task_step_loses_other_tasks(qapp, loop):
    """The hazard ``outside_task`` exists for: QTest.mouseMove processes events nested inside the calling task's step."""

    async def nested(widget) -> None:
        QTest.mouseMove(widget, QPoint(5, 5))

    result, errors = run_background_while_clicking(loop, qapp, nested)
    assert result.startswith("lost"), "if this stops being true, Qt or qasync changed and outside_task may be unnecessary"
    assert any("Cannot enter into task" in error for error in errors)


def test_qt_input_through_outside_task_loses_nothing(qapp, loop):
    async def outside(widget) -> None:
        await outside_task(lambda: QTest.mouseMove(widget, QPoint(5, 5)))

    result, errors = run_background_while_clicking(loop, qapp, outside)
    assert result == "finished" and not errors, (result, errors)


def test_a_failing_call_is_raised_in_the_awaiting_task(qapp, loop):
    async def scenario() -> None:
        with pytest.raises(ZeroDivisionError):
            await outside_task(lambda: 1 / 0)
        assert await outside_task(lambda: 42) == 42

    loop.run_until_complete(scenario())


def test_a_loop_with_nothing_pending_is_left_alone(qapp, loop):
    kill_pending_timers(loop)
    kill_pending_timers(loop)
    kill_pending_timers(object())  # not a qasync loop: nothing to do, nothing raised
