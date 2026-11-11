"""Windows: keep droid.exe from opening console windows when WittyCode itself has no console.

droid.exe is a console program. A process without a console (``pythonw.exe``, a shortcut to
``WittyCode.pyw``) that starts one without ``creationflags`` makes Windows open a new console window for it,
and the shells droid starts for its Execute tool share that window. The SDK has no option for this:
``ProcessTransport.connect`` calls ``asyncio.create_subprocess_exec`` and nothing else in the SDK spawns droid.

``install`` swaps the name ``asyncio`` inside ``droid_sdk.transport`` (and only there) for a stand-in that adds
``CREATE_NO_WINDOW`` to that one call when the caller passed no ``creationflags``. Every other use of
``asyncio``, in the SDK and elsewhere, is untouched.

``DETACHED_PROCESS`` would be wrong: the child would have no console at all, and every console program it
starts (its shells) would then open a window of its own. ``CREATE_NO_WINDOW`` gives the child a console
without a window, which its own children share.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from typing import Any

log = logging.getLogger("desktop.winproc")

CREATE_NO_WINDOW = 0x08000000


class _AsyncioWithoutConsole:
    """What ``droid_sdk.transport`` sees as ``asyncio``: the real module, except for one function."""

    def __getattr__(self, name: str) -> Any:
        return getattr(asyncio, name)

    async def create_subprocess_exec(self, program: Any, *args: Any, **kwargs: Any) -> Any:
        kwargs.setdefault("creationflags", CREATE_NO_WINDOW)
        return await asyncio.create_subprocess_exec(program, *args, **kwargs)


def is_installed() -> bool:
    from droid_sdk import transport

    return isinstance(getattr(transport, "asyncio", None), _AsyncioWithoutConsole)


def install(*, platform: str = sys.platform) -> bool:
    """Make the SDK start droid without a console window. True when that is in effect.

    Does nothing off Windows. Safe to call any number of times.
    """
    if platform != "win32":
        return False
    from droid_sdk import transport

    current = getattr(transport, "asyncio", None)
    if isinstance(current, _AsyncioWithoutConsole):
        return True
    if current is not asyncio:
        log.warning("droid_sdk.transport no longer uses asyncio the way this patch expects; droid.exe may open console windows")
        return False
    transport.asyncio = _AsyncioWithoutConsole()
    return True
