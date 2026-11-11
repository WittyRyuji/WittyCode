"""Driving the real page in the real window like a user: Qt mouse and key events, DOM queries (offscreen Qt, fake droid)."""

from __future__ import annotations

import asyncio
import json
import warnings
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
from PySide6.QtCore import QMimeData, QPoint, QPointF, Qt, QUrl
from PySide6.QtGui import QDragEnterEvent, QDragMoveEvent, QDropEvent, QInputMethodEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from desktop.core.app_core import AppCore
from desktop.core.session_manager import SessionManager
from desktop.core.settings import AppSettings
from desktop.ui.shell import PAGE_FILE, MainWindow
from tests.app_support import FakeCodexEngine, make_core
from tests.fakes import FakeFactory
from tests.ui_support import outside_task, release, wait_for

KEYS = {
    "Enter": Qt.Key.Key_Return,
    "Escape": Qt.Key.Key_Escape,
    "Tab": Qt.Key.Key_Tab,
    "Backspace": Qt.Key.Key_Backspace,
    "End": Qt.Key.Key_End,
}


def _runs(text: str) -> list[tuple[bool, str]]:
    """``text`` cut into stretches that are all ASCII or all not: ``[(is_ascii, stretch), ...]``."""
    runs: list[tuple[bool, str]] = []
    for char in text:
        if runs and runs[-1][0] == char.isascii():
            runs[-1] = (runs[-1][0], runs[-1][1] + char)
        else:
            runs.append((char.isascii(), char))
    return runs


class Rig:
    """A window with the real page, an application core on fake Droid (and fake Codex) sessions."""

    def __init__(self, window: MainWindow, core: AppCore, factory: FakeFactory, project: Path, codex: Any) -> None:
        self.window = window
        self.core = core
        self.factory = factory
        self.project = project
        self.codex = codex

    # ---- DOM
    async def js(self, source: str) -> Any:
        return await self.window.eval_js(source)

    async def count(self, selector: str) -> int:
        return await self.js(f"document.querySelectorAll({json.dumps(selector)}).length")

    async def text(self, selector: str, index: int = 0) -> str:
        return await self.js(f"(document.querySelectorAll({json.dumps(selector)})[{index}]?.textContent ?? null)")

    async def texts(self, selector: str) -> list[str]:
        raw = await self.js(f"JSON.stringify([...document.querySelectorAll({json.dumps(selector)})].map(e => e.textContent.trim()))")
        return json.loads(raw)

    async def exists(self, selector: str) -> bool:
        return await self.count(selector) > 0

    async def wait_for(self, selector: str, count: int | None = None, timeout: float = 10.0) -> None:
        async def check() -> bool:
            n = await self.count(selector)
            return n == count if count is not None else n > 0

        await wait_for(check, timeout, f"{selector}" + (f" x {count}" if count is not None else ""))

    async def wait_text(self, selector: str, needle: str, timeout: float = 10.0) -> None:
        async def check() -> bool:
            return any(needle in item for item in await self.texts(selector))

        await wait_for(check, timeout, f"{needle!r} in {selector}")

    # ---- the user's hands (real Qt events to the widget Chromium listens on)
    def _target(self):
        return self.window.view.focusProxy() or self.window.view

    async def _center(self, selector: str, index: int) -> QPoint | None:
        raw = await self.js(
            "JSON.stringify((function(){"
            f"var e = document.querySelectorAll({json.dumps(selector)})[{index}]; if (!e) return null;"
            " e.scrollIntoView({block: 'center'}); var r = e.getBoundingClientRect();"
            " return [r.left + r.width / 2, r.top + r.height / 2];})())"
        )
        point = json.loads(raw)
        if point is None:
            return None
        zoom = self.window.view.zoomFactor()
        return QPoint(round(point[0] * zoom), round(point[1] * zoom))

    async def click(self, selector: str, index: int = 0, *, right: bool = False, ctrl: bool = False) -> None:
        at = await self._center(selector, index)
        assert at is not None, f"nothing to click: {selector}[{index}]"
        button = Qt.MouseButton.RightButton if right else Qt.MouseButton.LeftButton
        modifier = Qt.KeyboardModifier.ControlModifier if ctrl else Qt.KeyboardModifier.NoModifier
        target = self._target()

        def mouse() -> None:
            QTest.mouseMove(target, at)
            QTest.mouseClick(target, button, modifier, at)

        await outside_task(mouse)
        await asyncio.sleep(0.05)

    async def click_text(self, selector: str, needle: str) -> None:
        raw = await self.js(f"[...document.querySelectorAll({json.dumps(selector)})].findIndex(e => e.textContent.includes({json.dumps(needle)}))")
        assert raw >= 0, f"no {selector} containing {needle!r}"
        await self.click(selector, raw)

    async def type(self, text: str) -> None:
        """Type like a user: letters as key presses, anything else (Chinese ...) as an input method commits it.

        ``QTest.keyClicks`` cannot be given a character outside ASCII: it kills the whole process (exit 0xC0000409, no
        output, not even from pytest). A real Chinese keyboard goes through an input method anyway, which delivers
        the finished text in one ``QInputMethodEvent``.
        """
        target = self._target()
        for ascii_run, run in _runs(text):
            if ascii_run:
                await outside_task(lambda run=run: QTest.keyClicks(target, run))
            else:
                commit = QInputMethodEvent()
                commit.setCommitString(run)
                await outside_task(lambda commit=commit: QApplication.sendEvent(target, commit))
            await asyncio.sleep(0.05)

    async def press(self, key: str, modifiers: Qt.KeyboardModifier = Qt.KeyboardModifier.NoModifier) -> None:
        target = self._target()
        await outside_task(lambda: QTest.keyClick(target, KEYS.get(key, key) if key in KEYS else key, modifiers))
        await asyncio.sleep(0.05)

    async def drop_files(self, paths: Sequence[str | Path]) -> Qt.DropAction:
        """Drag files from Explorer onto the middle of the page and let go (what the window system does)."""
        mime = QMimeData()
        mime.setUrls([QUrl.fromLocalFile(str(path)) for path in paths])
        actions = Qt.DropAction.CopyAction | Qt.DropAction.MoveAction  # Explorer offers both
        target = self._target()
        centre = QPoint(target.width() // 2, target.height() // 2)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)  # the only constructor that does not crash the process
            enter = QDragEnterEvent(centre, actions, mime, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
        await outside_task(lambda: QApplication.sendEvent(target, enter))
        if not enter.isAccepted():
            return Qt.DropAction.IgnoreAction
        for _ in range(3):  # a real drag keeps moving over the page: Chromium only offers a drop after a dragover
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", DeprecationWarning)
                move = QDragMoveEvent(centre, actions, mime, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
            await outside_task(lambda: QApplication.sendEvent(target, move))
            await asyncio.sleep(0.05)
        drop = QDropEvent(QPointF(centre), actions, mime, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
        await outside_task(lambda: QApplication.sendEvent(target, drop))
        await asyncio.sleep(0.05)
        return drop.dropAction() if drop.isAccepted() else Qt.DropAction.IgnoreAction

    async def focus_composer(self) -> None:
        await self.click(".input")

    async def send(self, text: str) -> None:
        await self.focus_composer()
        await self.type(text)
        await self.press("Enter")

    async def finish(self) -> None:
        await self.window.shutdown()
        await release(self.window)


def require_page() -> Path:
    if not PAGE_FILE.is_file():
        pytest.skip("the page is not built: run `npm run build:app` in frontend/")
    return PAGE_FILE


async def open_rig(
    tmp_path: Path,
    *,
    codex: bool = False,
    codex_signed_in: bool = True,
    providers: Any = None,
    projects: bool = True,
    max_open: int = 4,
    saved: list[Any] | None = None,
    size: tuple[int, int] = (1320, 860),
    frame: str = "",
    cache: Any = None,
    open_delay: float = 0.0,
    list_delay: float = 0.0,
    codex_engine: Any = None,
    devin_engine: Any = None,
    chatgpt_engine: Any = None,
    chatgpt_welcomed: bool = True,
    read_ahead: bool = False,
    history: dict[str, Any] | None = None,
    memory: Any = None,
) -> Rig:
    """``frame``: the window outline Python asks the page for (``winframe.frame_kind``); none under the offscreen platform.
    ``open_delay`` / ``list_delay``: how slow the fake droid.exe is to start a session / to list models and sessions.
    ``read_ahead``: the cache is filled with the saved sessions nobody has opened (off: it would race with the test).
    ``history``: session id -> what droid answers for its saved history, there before the application starts."""
    page = require_page()
    core, factory, _events, _platform, engine = make_core(
        tmp_path, codex=codex, codex_signed_in=codex_signed_in, providers=providers, max_open=max_open, projects=projects, cache=cache,
        codex_engine=codex_engine, devin_engine=devin_engine, chatgpt_engine=chatgpt_engine, read_ahead=read_ahead, memory=memory,
    )
    if devin_engine is not None:
        devin_engine.settings = core.settings  # (one settings object in the application)
    if chatgpt_engine is not None:
        chatgpt_engine.settings = core.settings
        core.settings.chatgpt_welcomed = chatgpt_welcomed  # (the one-time note would otherwise cover the page of every test)
    factory.saved = list(saved or [])
    factory.history.update(history or {})
    factory.list_delay = list_delay
    if open_delay:
        factory.default_options["open_delay"] = open_delay
    settings = core.settings
    window = MainWindow(settings, persist=False, page_file=page)
    # the core made by make_core talks to a fake platform: give it the window's own
    core.platform = window.platform
    window.attach(core)
    window.frame = frame
    window.resize(*size)
    window.load()
    window.show()
    # Without activation the offscreen window never becomes the focus window, and the page drops the first key
    # events (the DOM only sees keys when focused) — Ctrl+, then randomly never opens the settings.
    window.activateWindow()
    window.view.setFocus(Qt.FocusReason.ActiveWindowFocusReason)
    await wait_for(lambda: window.page_loaded, 15, "the page to load")
    await wait_for(lambda: window.backend.connected, 15, "the page to ask for its state")
    rig = Rig(window, core, factory, (tmp_path / "proj").resolve(), engine)
    await rig.wait_for(".titlebar")
    # The title bar is drawn at once, but the page ignores every shortcut (Ctrl+, ...) until app.init has answered: the
    # boot placeholder goes away then. Without this wait a key pressed at once was lost, in about one run of 600.
    await rig.wait_for(".boot", 0)
    await rig.js("document.hasFocus() || (window.focus(), document.hasFocus())")
    return rig
