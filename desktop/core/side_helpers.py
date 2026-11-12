"""Helpers of the side questions every engine can ask (``core/<engine>/side.py``): which model is the cheap one, the
weakest effort, waiting for a request still on its way, cleaning up, and the reason for the log."""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Coroutine, Sequence
from typing import Any

log = logging.getLogger("desktop.side")


CHEAP = ("nano", "mini", "luna", "lite", "flash")
"""Words in the ids of small, fast models, the cheapest kind first (the subscription lists ``gpt-6-luna``, "Fast and
affordable", next to the ``sol`` workhorses). A word counts only as a whole part of the id (``gemini`` is no ``mini``)."""


EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra")
"""Reasoning efforts (and Devin's thought levels), weakest first."""


def words(model_id: str) -> set[str]:
    return set(re.split(r"[^a-z0-9]+", model_id.lower()))


def cheapness(model_id: str) -> int:
    """Where the model's kind stands in ``CHEAP`` (``len(CHEAP)``: none of those words is in its id)."""
    found = words(model_id)
    return next((rank for rank, word in enumerate(CHEAP) if word in found), len(CHEAP))


def lowest_effort(efforts: Sequence[str]) -> str:
    """The weakest of the efforts a model offers ("": it offers none, its own default applies)."""
    known = [effort for effort in efforts if effort in EFFORTS]
    if known:
        return min(known, key=EFFORTS.index)
    return efforts[0] if efforts else ""


async def settle(request: asyncio.Future[Any] | None, seconds: float) -> Any:
    """What a request still on its way answers, if it does within ``seconds`` (``None`` otherwise: then it is dropped)."""
    if request is None:
        return None
    if not request.done():
        await asyncio.wait({request}, timeout=seconds)
    if not request.done():
        request.cancel()
        return None
    if request.cancelled() or request.exception() is not None:
        return None
    return request.result()


async def finish(engine: Any, cleanup: Coroutine[Any, Any, None]) -> None:
    """Run ``cleanup`` to its end, also when the question is cancelled again meanwhile (it then goes on as one of the
    engine's tasks); only the engine closing stops it early, and its process is going then anyway. Never raises but
    the question's own cancellation."""
    task = engine._spawn(cleanup)
    try:
        await asyncio.shield(task)
    except asyncio.CancelledError:
        current = asyncio.current_task()
        if current is not None and current.cancelling():
            raise
    except Exception:
        log.warning("cleaning up after a side question failed", exc_info=True)


def reason(error: BaseException, timeout: float) -> str:
    """For the log: why a side question got no answer."""
    if isinstance(error, TimeoutError) and not str(error):
        return f"no answer within {timeout:g} s"
    return f"{type(error).__name__}: {error}"
