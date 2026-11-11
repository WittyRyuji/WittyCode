"""Spike (item 9): QWebChannel two-way bridge while a live Droid turn streams.

Flow (qasync event loop owns the Qt loop, same pattern as qasync_smoke.py):
  1. Load an HTML page with qwebchannel.js inlined from the Qt resource
     :/qtwebchannel/qwebchannel.js (a qrc:// <script src> is blocked when the
     page base URL is a remote scheme); the page grabs the exposed ``bridge``
     QObject and calls bridge.echo() (JS -> Python).
  2. Run one live turn; every TextDelta is pushed into the page via
     runJavaScript (Python -> JS) and the page accumulates them.
  3. Read back window state via JavaScript callbacks and verify the echo
     round-trip and that page-side accumulated text matches the streamed deltas.

Run twice: windowed (default) and headless with QT_QPA_PLATFORM=offscreen set
in the environment. Writes fixtures/robust/qwebchannel_summary_{windowed,offscreen}.json
and the turn recording fixtures/robust/qwebchannel.jsonl.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time

from PySide6.QtCore import QFile, QIODevice, QObject, Slot, QUrl
from PySide6.QtWebChannel import QWebChannel
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import QApplication

import qasync
from droid_sdk import Autonomy, InteractionHandlers, Session, SessionConfig, TextDelta

from _common import (
    DEFAULT_MODEL,
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
from _robust_common import ROBUST_FIXTURES, ROBUST_SANDBOX


def qwebchannel_js() -> str:
    resource = QFile(":/qtwebchannel/qwebchannel.js")
    resource.open(QIODevice.OpenModeFlag.ReadOnly)
    try:
        return bytes(resource.readAll()).decode("utf-8")
    finally:
        resource.close()


HTML = """<!DOCTYPE html>
<html><head><meta charset="utf-8">
<script>
__QWEBCHANNEL_JS__
</script>
</head><body>
<div id="log"></div>
<script>
window.chunks = [];
window.echoResult = null;
window.lastError = null;
window.onerror = function (m) { window.lastError = String(m); };
window.push = function (t) { window.chunks.push(t); };
new QWebChannel(qt.webChannelTransport, function (channel) {
    window.bridge = channel.objects.bridge;
    window.bridge.echo("hello-from-js", function (res) { window.echoResult = res; });
});
</script>
</body></html>
""".replace("__QWEBCHANNEL_JS__", qwebchannel_js())

PROMPT = "Reply with the word webchannel five times separated by spaces and nothing else."


class Bridge(QObject):
    def __init__(self) -> None:
        super().__init__()
        self.echo_calls: list[str] = []

    @Slot(str, result=str)
    def echo(self, text: str) -> str:
        self.echo_calls.append(text)
        return f"pong:{text}"


def run_js(view: QWebEngineView, script: str) -> asyncio.Future:
    future = asyncio.get_running_loop().create_future()

    def done(value: object) -> None:
        if not future.done():
            future.set_result(value)

    view.page().runJavaScript(script, 0, done)
    return future


async def amain(view: QWebEngineView, bridge: Bridge, loaded: asyncio.Future, args: argparse.Namespace) -> dict:
    started = time.perf_counter()
    ok_load = await asyncio.wait_for(loaded, timeout=30)
    summary: dict = {
        "platform": QApplication.platformName(),
        "load_ok": bool(ok_load),
        "load_s": round(time.perf_counter() - started, 3),
    }
    ready = None
    for _ in range(100):
        await asyncio.sleep(0.1)
        ready = await run_js(view, "window.echoResult")
        if ready is not None:
            break
    summary["echo_result"] = ready
    summary["bridge_calls"] = list(bridge.echo_calls)

    cwd = ROBUST_SANDBOX / "web"
    cwd.mkdir(parents=True, exist_ok=True)
    runtime, droid = make_runtime()
    recorder = Recorder(
        ROBUST_FIXTURES / "qwebchannel.jsonl",
        raw_path=ROBUST_FIXTURES / "qwebchannel.raw.jsonl",
    )
    recorder.meta("Environment", **environment_info(droid, model=DEFAULT_MODEL, autonomy="off", cwd=str(cwd),
                                                     qt_platform=QApplication.platformName()))
    session = Session(
        cwd=cwd,
        model=DEFAULT_MODEL,
        reasoning_effort=reasoning_effort(),
        config=SessionConfig(autonomy=Autonomy.OFF),
        interactions=InteractionHandlers(on_permission=make_permission_handler(recorder)),
        runtime=runtime,
    )
    pushed = 0

    def on_event(event: object) -> None:
        nonlocal pushed
        if isinstance(event, TextDelta):
            pushed += 1
            view.page().runJavaScript(f"window.push({json.dumps(event.text)})")

    result = None
    try:
        await open_recorded(session, recorder)
        result = await run_turn(session, recorder, PROMPT, timeout=args.timeout, on_event=on_event)
    finally:
        await close_recorded(session, recorder)
        recorder.close()
    await asyncio.sleep(0.5)  # let queued runJavaScript calls land
    summary["turn_subtype"] = getattr(result, "subtype", None)
    summary["turn_text"] = getattr(result, "text", None)
    summary["deltas_pushed"] = pushed
    summary["page_chunks"] = await run_js(view, "window.chunks.length")
    summary["page_joined"] = await run_js(view, "window.chunks.join('')")
    summary["js_error"] = await run_js(view, "window.lastError")
    summary["js_matches_turn"] = (summary["page_joined"] or "").strip() == (summary["turn_text"] or "").strip()
    return summary


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--timeout", type=float, default=180.0)
    parser.add_argument("--hold", type=float, default=0.5)
    args = parser.parse_args()
    offscreen = os.environ.get("QT_QPA_PLATFORM") == "offscreen"
    prepare_sandbox()
    ROBUST_FIXTURES.mkdir(parents=True, exist_ok=True)

    app = QApplication(sys.argv)
    view = QWebEngineView()
    bridge = Bridge()
    channel = QWebChannel(view.page())
    channel.registerObject("bridge", bridge)
    view.page().setWebChannel(channel)
    loop = qasync.QEventLoop(app)
    asyncio.set_event_loop(loop)
    loaded = loop.create_future()
    view.loadFinished.connect(lambda ok: loaded.done() or loaded.set_result(bool(ok)))
    view.setHtml(HTML, QUrl("https://local.spike/index.html"))
    if not offscreen:
        view.resize(640, 480)
        view.show()

    summary: dict = {}
    async def guarded() -> int:
        nonlocal summary
        try:
            summary = await asyncio.wait_for(amain(view, bridge, loaded, args), timeout=args.timeout + 90)
        except Exception as exc:
            print(f"ERROR: {type(exc).__name__}: {exc}", flush=True)
            return 1
        await asyncio.sleep(args.hold)
        if not offscreen:
            view.grab().save(str(ROBUST_FIXTURES / "qwebchannel.png"))
        ok = (
            summary.get("echo_result") == "pong:hello-from-js"
            and summary.get("deltas_pushed", 0) >= 1
            and summary.get("js_matches_turn")
        )
        return 0 if ok else 1

    with loop:
        code = loop.run_until_complete(guarded())
    view.close()
    view.deleteLater()
    app.processEvents()
    path = ROBUST_FIXTURES / f"qwebchannel_summary_{'offscreen' if offscreen else 'windowed'}.json"
    path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(json.dumps(summary, ensure_ascii=False, indent=2, default=str), flush=True)
    print(f"platform={QApplication.platformName()} offscreen={offscreen} exit={code}; wrote {path}", flush=True)
    return code


if __name__ == "__main__":
    setup_console()
    sys.exit(main())