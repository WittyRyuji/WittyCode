"""Spike (f): PySide6 + Qt WebEngine + qasync event loop running one streaming Droid turn.

TextDelta chunks are appended to a QWebEngineView with page().runJavaScript();
the script exits 0 when the turn succeeds and the page text contains "pong".

  --no-turn   only check that WebEngine loads and runs JavaScript under qasync (no credits)
  QT_QPA_PLATFORM=offscreen   run without a visible window

Writes fixtures/qasync_smoke.jsonl for the turn.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time

# QtWebEngineWidgets must be imported before the QApplication is created.
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import QApplication

import qasync
from droid_sdk import Autonomy, InteractionHandlers, Session, SessionConfig, TextDelta

from _common import (
    DEFAULT_MODEL,
    DEFAULT_REASONING,
    FIXTURES,
    SANDBOX,
    Recorder,
    close_recorded,
    environment_info,
    make_permission_handler,
    make_runtime,
    open_recorded,
    prepare_sandbox,
    reasoning_effort,
    run_turn,
    setup_console,
)

PROMPT = "Reply with the word pong"
HTML = """<!doctype html>
<html><head><meta charset="utf-8"><title>WittyCode qasync smoke</title>
<style>body{font-family:'Segoe UI',sans-serif;margin:16px}#out{white-space:pre-wrap;border:1px solid #bbb;padding:8px;min-height:2em}</style>
</head><body>
<h3>WittyCode: qasync + Qt WebEngine smoke test</h3>
<div id="status">loaded</div><div id="out"></div>
<script>
function appendDelta(t){var o=document.getElementById('out');o.textContent+=t;return o.textContent.length;}
function setStatus(s){document.getElementById('status').textContent=s;}
function readOut(){return document.getElementById('out').textContent;}
</script>
</body></html>
"""


def run_js(view: QWebEngineView, script: str) -> asyncio.Future:
    future = asyncio.get_running_loop().create_future()

    def done(value: object) -> None:
        if not future.done():
            future.set_result(value)

    view.page().runJavaScript(script, 0, done)
    return future


async def amain(view: QWebEngineView, loaded: asyncio.Future, args: argparse.Namespace) -> int:
    started = time.perf_counter()
    ok_load = await asyncio.wait_for(loaded, timeout=30)
    two = await asyncio.wait_for(run_js(view, "1+1"), timeout=10)
    print(f"WebEngine loadFinished={ok_load} in {time.perf_counter() - started:.2f}s; runJavaScript('1+1') -> {two!r}; "
          f"platform={QApplication.platformName()}", flush=True)
    if not ok_load or two != 2:
        return 1
    if args.no_turn:
        return 0

    prepare_sandbox()
    runtime, droid = make_runtime()
    recorder = Recorder(FIXTURES / "qasync_smoke.jsonl")
    recorder.meta("Environment", **environment_info(droid, model=args.model, cwd=str(SANDBOX),
                                                     qt_platform=QApplication.platformName()))
    session = Session(
        cwd=SANDBOX,
        model=args.model,
        reasoning_effort=reasoning_effort(DEFAULT_REASONING),
        config=SessionConfig(autonomy=Autonomy.OFF),
        interactions=InteractionHandlers(on_permission=make_permission_handler(recorder)),
        runtime=runtime,
    )
    deltas = 0

    def on_event(event: object) -> None:
        nonlocal deltas
        if isinstance(event, TextDelta):
            deltas += 1
            view.page().runJavaScript(f"appendDelta({json.dumps(event.text)})")

    result = None
    try:
        await open_recorded(session, recorder, raw=False)
        view.page().runJavaScript("setStatus('streaming...')")
        result = await run_turn(session, recorder, PROMPT, timeout=args.timeout, on_event=on_event)
    finally:
        await close_recorded(session, recorder)
        recorder.close()
    await run_js(view, f"setStatus({json.dumps('done: ' + (result.subtype if result else 'error'))})")
    page_text = await asyncio.wait_for(run_js(view, "readOut()"), timeout=10)
    print(f"result: {result.subtype if result else None}; TextDelta count={deltas}; page text={page_text!r}")
    return 0 if result is not None and result.success and "pong" in str(page_text).lower() else 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--no-turn", action="store_true")
    parser.add_argument("--hold", type=float, default=1.0, help="seconds to keep the window open at the end")
    args = parser.parse_args()

    app = QApplication(sys.argv)
    view = QWebEngineView()
    view.setWindowTitle("WittyCode qasync smoke")
    view.resize(640, 320)
    loop = qasync.QEventLoop(app)
    asyncio.set_event_loop(loop)
    loaded = loop.create_future()
    view.loadFinished.connect(lambda ok: loaded.done() or loaded.set_result(bool(ok)))
    view.setHtml(HTML)
    view.show()

    async def guarded() -> int:
        try:
            code = await asyncio.wait_for(amain(view, loaded, args), timeout=args.timeout + 60)
        except Exception as exc:
            print(f"ERROR: {type(exc).__name__}: {exc}", flush=True)
            code = 1
        await asyncio.sleep(args.hold)
        return code

    with loop:
        code = loop.run_until_complete(guarded())
    view.close()
    view.deleteLater()
    app.processEvents()
    print(f"qasync loop={type(loop).__name__}; QT_QPA_PLATFORM={os.environ.get('QT_QPA_PLATFORM', '(unset)')}; exit={code}")
    return code


if __name__ == "__main__":
    setup_console()
    sys.exit(main())
