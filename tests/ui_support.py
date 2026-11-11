"""Helpers shared by the Qt tests."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from PySide6.QtCore import QTimer


async def wait_for(predicate: Callable[[], Any], timeout: float = 15.0, message: str = "condition") -> None:
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while True:
        value = predicate()
        if asyncio.iscoroutine(value):
            value = await value
        if value:
            return
        if loop.time() > end:
            raise AssertionError(f"timed out waiting for {message}")
        await asyncio.sleep(0.02)


async def outside_task(call: Callable[[], Any]) -> Any:
    """Run ``call`` from the Qt event loop instead of from inside this task's step, and wait for its result.

    Qt's synchronous test input (``QTest.mouseMove`` and friends, ``QApplication.sendEvent`` into a web view) runs
    nested event processing. Inside a task's step that nested loop can run the wake-up of *another* task, which asyncio
    refuses (``Cannot enter into task ... while another task is being executed``): qasync has already taken that
    wake-up off its list, so the other task never runs again and cannot be cancelled. Whether it happens depends on a
    wake-up being due at that very moment, so it showed up as a test that hangs once in a few dozen runs (the loop is
    alive, a task is stuck ``cancelling`` and nothing is armed). Called from a plain Qt timer there is no running task
    to collide with.
    """
    done: asyncio.Future[Any] = asyncio.get_running_loop().create_future()

    def run() -> None:
        try:
            result = call()
        except BaseException as error:
            if not done.cancelled():
                done.set_exception(error)
        else:
            if not done.cancelled():
                done.set_result(result)

    QTimer.singleShot(0, run)
    return await done


async def release(window: Any) -> None:
    """Let Qt delete a window after its shutdown.

    Never call processEvents() for this: nested inside a running task it would run another task's first step
    re-entrantly, which asyncio rejects, and that task would then never run (nor be cancellable).
    """
    window.deleteLater()
    await asyncio.sleep(0.05)


def run(loop: Any, coroutine: Any, timeout: float = 60.0) -> Any:
    return loop.run_until_complete(asyncio.wait_for(coroutine, timeout))
