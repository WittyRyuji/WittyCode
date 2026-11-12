"""Small helpers shared by the tests."""

from __future__ import annotations

import asyncio
from typing import Any


async def until(predicate: Any, timeout: float = 5.0) -> None:
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while not predicate():
        if loop.time() > end:
            raise AssertionError("condition not reached in time")
        await asyncio.sleep(0.005)
