r"""Starts the application the way the release does, offscreen, and checks that it comes up.

    <python> scripts\smoke_release.py [--timeout 60]

Run it with the interpreter that is being checked (the release's own ``python\python.exe``, or the development one). It builds
the real application with settings in a temporary folder (no model is called, nothing is written to %APPDATA%), loads the page
into Qt WebEngine, waits for the channel to the page to connect and for the message box to be drawn, then shuts the window
down. Exit code 0: it came up; anything else: what is missing is printed.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import tempfile
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
# Software rendering: the offscreen platform has no usable GPU surface and Chromium floods stderr trying.
os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS", "--disable-gpu --disable-logging")

ROOT = Path(__file__).resolve().parents[1]


async def wait_until(check, what: str, timeout: float) -> None:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        value = check()
        if asyncio.iscoroutine(value):
            value = await value
        if value:
            return
        await asyncio.sleep(0.1)
    raise TimeoutError(f"timed out waiting for {what}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--timeout", type=float, default=60.0)
    given = parser.parse_args()

    from PySide6 import QtWebEngineWidgets  # noqa: F401  (must be imported before the QApplication)

    from desktop.ui import app as wittycode

    print(f"python {sys.version.split()[0]} at {sys.executable}")
    folder = Path(tempfile.mkdtemp(prefix="wc-smoke-"))
    project = folder / "project"
    project.mkdir()
    os.environ["WITTYCODE_SETTINGS"] = str(folder / "settings.json")  # (the API providers' files go next to it too)
    options, qt_args = wittycode.parse_args(["--settings", str(folder / "settings.json"), "--project", str(project)])
    context = wittycode.build_application(options, qt_args)
    window = context.window
    failure: list[str] = []

    async def serve() -> None:
        window.load()
        window.show()
        try:
            await wait_until(lambda: window.page_loaded, "the page to load", given.timeout)
            print("page loaded")
            await wait_until(lambda: window.backend.connected, "the page to ask for its state", given.timeout)
            print("channel connected")
            await wait_until(lambda: window.eval_js("document.querySelectorAll('.titlebar').length > 0"), "the page to draw its title bar", given.timeout)
            await wait_until(lambda: window.eval_js("!document.querySelector('.boot') && !!document.querySelector('.input')"), "the message box", given.timeout)
            print("message box drawn")
        except Exception as error:
            failure.append(f"{type(error).__name__}: {error}")
            try:
                look = "JSON.stringify({text: document.body ? document.body.innerText.slice(0, 200) : null})"
                print("page state:", await window.eval_js(look, 5.0))
            except Exception as inner:
                print(f"page state unreadable: {inner}")
        finally:
            await window.shutdown()

    try:
        with context.loop:
            context.loop.run_until_complete(serve())
    finally:
        wittycode.kill_pending_timers(context.loop)
        window.deleteLater()
        wittycode.cleanup(context.app)
    if failure:
        print("FAILED: " + failure[0])
        return 1
    print("OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
