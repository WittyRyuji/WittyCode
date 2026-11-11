"""Fire-and-forget asyncio tasks that never fail silently."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Coroutine
from typing import Any

log = logging.getLogger("desktop")

_running: set[asyncio.Future[Any]] = set()


def spawn(
    coroutine: Coroutine[Any, Any, Any],
    *,
    on_error: Callable[[BaseException], None] | None = None,
) -> asyncio.Future[Any]:
    """Run a coroutine on the event loop, keeping a reference and reporting any exception."""
    task = asyncio.ensure_future(coroutine)
    _running.add(task)

    def done(finished: asyncio.Future[Any]) -> None:
        _running.discard(finished)
        if finished.cancelled():
            return
        error = finished.exception()
        if error is None:
            return
        log.error("background task failed", exc_info=error)
        if on_error is not None:
            try:
                on_error(error)
            except Exception:  # reporting must never raise into the event loop
                log.exception("error handler failed")

    task.add_done_callback(done)
    return task


def kill_pending_timers(loop: Any) -> None:
    """Cancel the native Qt timers that ``call_later`` / ``asyncio.sleep`` left armed on a qasync loop.

    qasync's ``close()`` only flags its timer object stopped; the timers behind pending handles stay armed, and the
    Win32 event dispatcher keeps delivering them. Once the loop (and its timer QObject) has been collected, a delivery
    lands on freed memory: an access violation inside Qt6Core (``QCoreApplication::notifyInternal2``), intermittent
    and far from its cause. Call this before closing a loop whose process goes on running or is about to tear Qt down.
    A qasync that renames its internals leaves the timers alone; ``tests/test_tasks.py`` notices.
    """
    timer = getattr(loop, "_timer", None)
    callbacks = getattr(timer, "_SimpleTimer__callbacks", None)
    if not callbacks:
        return
    for timer_id in list(callbacks):
        timer.killTimer(timer_id)
    callbacks.clear()


async def cancel_all() -> None:
    pending = [task for task in _running if not task.done()]
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)