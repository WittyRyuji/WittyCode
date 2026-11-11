"""The window around the page, with the real Qt WebEngine (offscreen) and a stand-in page that only speaks the transport.

What is checked here is the mechanics: the channel between page and Python, the lock-down of the page, the frameless
window and its shutdown. The real page is checked in ``test_app_e2e.py``.
"""

from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path

import pytest
from PySide6.QtCore import Qt

from desktop.core.app_core import AppCore
from desktop.core.session_manager import SessionManager
from desktop.core.settings import AppSettings
from desktop.ui import shell
from desktop.ui.shell import MainWindow
from tests.app_support import FakeCodexEngine
from tests.fakes import FakeFactory, until
from tests.support import saved_session
from tests.ui_support import release, run, wait_for

pytestmark = pytest.mark.ui

PAGE = Path(__file__).parent / "pages" / "channel.html"


class Rig:
    def __init__(self, window: MainWindow, core: AppCore, factory: FakeFactory, project: Path) -> None:
        self.window = window
        self.core = core
        self.factory = factory
        self.project = project

    async def js(self, source: str):
        return await self.window.eval_js(source)

    _keys = 0

    async def rpc(self, method: str, params: dict | None = None):
        """Call the backend from the page and wait for its answer there: (ok, value or the error message)."""
        Rig._keys += 1
        key = f"k{Rig._keys}"
        await self.js(f"window.rpcStore({json.dumps(key)}, {json.dumps(method)}, {json.dumps(params or {})})")

        async def arrived() -> bool:
            return await self.js(f"{json.dumps(key)} in window.results")

        await wait_for(arrived, message=f"the answer to {method}")
        answer = json.loads(await self.js(f"window.results[{json.dumps(key)}]"))
        return answer["ok"], answer["v"]

    async def events(self, name: str | None = None) -> list:
        raw = await self.js("JSON.stringify(window.log)")
        items = json.loads(raw)
        return [payload for event, payload in items if name is None or event == name]

    async def finish(self) -> None:
        await self.window.shutdown()
        await release(self.window)


async def open_rig(tmp_path, *, codex: bool = False) -> Rig:
    project = tmp_path / "proj"
    project.mkdir()
    settings = AppSettings(default_model="claude-haiku-5-5", reasoning_effort="low")
    settings.add_project(project)
    factory = FakeFactory()
    manager = SessionManager(settings, factory)
    window = MainWindow(settings, persist=False, page_file=PAGE)
    core = AppCore(manager, window.platform, codex=FakeCodexEngine(settings) if codex else None, persist=False)
    window.attach(core)
    window.load()
    window.show()
    await wait_for(lambda: window.page_loaded, message="the page to load")
    await wait_for(lambda: window.eval_js("window.ready === true"), message="the channel to connect")
    return Rig(window, core, factory, project)


def test_the_client_script_is_there_before_the_page_runs_and_the_channel_connects(qapp, loop, tmp_path):
    async def scenario():
        rig = await open_rig(tmp_path)
        assert await rig.js("window.sawQWebChannel") is True
        assert await rig.js("typeof window.backend.call") == "function"
        await rig.finish()

    run(loop, scenario())


def test_a_call_from_the_page_reaches_the_core_and_its_events_come_back(qapp, loop, tmp_path):
    async def scenario():
        rig = await open_rig(tmp_path)
        ok, init = await rig.rpc("app.init")
        assert ok and init["version"] == 1 and init["projects"][0]["name"] == "proj"
        assert init["defaults"]["reasoning"] == "low" and init["maximized"] is False
        await wait_for(lambda: rig.events("models"), message="the model list event")
        (models,) = (await rig.events("models"))[:1]
        assert "claude-haiku-5-5" in [m["id"] for m in models["models"]]
        ok, created = await rig.rpc("session.new", {"projectId": str(rig.project.resolve())})
        assert ok and created["id"] == "c1"
        await wait_for(lambda: rig.events("tabs"), message="the tabs event")
        assert (await rig.events("tabs"))[0] == {"tabs": ["c1"], "select": "c1"}

        async def ready() -> bool:
            return any(p["patch"].get("mark") == "ready" for p in await rig.events("session"))

        await wait_for(ready, message="the session to become ready")
        await rig.finish()

    run(loop, scenario())


def test_a_failing_call_is_an_error_message_in_the_page_not_an_exception_in_python(qapp, loop, tmp_path):
    async def scenario():
        rig = await open_rig(tmp_path)
        ok, message = await rig.rpc("no.such.call")
        assert ok is False and "没有这个操作" in message
        ok, message = await rig.rpc("session.send", {"id": "c9", "text": "x"})
        assert ok is False and "已经关闭" in message
        ok, message = await rig.rpc("project.add", {"path": str(rig.project.parent / "missing")})
        assert ok is False and "不是有效的文件夹" in message
        await rig.finish()

    run(loop, scenario())


def test_large_and_non_ascii_payloads_survive_the_channel(qapp, loop, tmp_path):
    async def scenario():
        rig = await open_rig(tmp_path)
        title = "重构：读取上下文 🐉 " * 3000
        rig.factory.saved = [saved_session("s-1", title, minutes_ago=3)]
        await rig.rpc("app.init")
        await wait_for(lambda: rig.events("sidebar"), message="the sidebar event")
        sidebar = (await rig.events("sidebar"))[-1]
        assert sidebar["sessions"]["s-1"]["title"] == title
        await rig.finish()

    run(loop, scenario())


def test_the_page_cannot_navigate_away_open_windows_or_load_remote_content(qapp, loop, tmp_path, monkeypatch):
    async def scenario():
        rig = await open_rig(tmp_path)
        opened: list[str] = []
        monkeypatch.setattr(shell.QDesktopServices, "openUrl", lambda url: opened.append(url.toString()) or True)
        here = await rig.js("location.href")
        await rig.js("location.href = 'https://example.com/elsewhere'; true")
        await rig.js("location.href = 'file:///C:/Windows/notepad.exe'; true")
        await asyncio.sleep(0.4)
        assert await rig.js("location.href") == here, "navigation by script is refused"
        await rig.js("document.getElementById('ext').click(); true")
        await wait_for(lambda: "https://example.com/docs" in opened, message="the link to be handed to the browser")
        assert await rig.js("location.href") == here, "the page itself never moves"
        assert opened and all(url.startswith("https://") for url in opened), "only web links get to the browser"
        assert await rig.js("window.ready === true") and rig.window.page_loaded, "a refused navigation leaves the page as it is"
        assert await rig.js("window.open('https://example.com') === null") is True, "no pop-up windows"
        await rig.finish()

    run(loop, scenario())


def test_only_web_links_leave_the_application(qapp, monkeypatch):
    sent: list[str] = []
    monkeypatch.setattr(shell.QDesktopServices, "openUrl", lambda url: sent.append(url.toString()) or True)
    assert shell.open_external("https://example.com/a") and shell.open_external("mailto:a@b.c")
    assert not shell.open_external("file:///C:/Windows/notepad.exe") and not shell.open_external("javascript:alert(1)")
    assert not shell.open_external("ms-settings:") and sent == ["https://example.com/a", "mailto:a@b.c"]


def test_the_window_is_frameless_and_follows_the_page_in_what_it_does(qapp, loop, tmp_path):
    async def scenario():
        rig = await open_rig(tmp_path)
        window = rig.window
        assert window.windowFlags() & Qt.WindowType.FramelessWindowHint
        assert (window.minimumWidth(), window.minimumHeight()) == shell.MIN_SIZE
        await rig.rpc("app.init")
        assert (await rig.rpc("window.toggleMaximize"))[1] is True and window.isMaximized()
        await wait_for(lambda: rig.events("window"), message="the window event")
        assert (await rig.events("window"))[-1] == {"maximized": True}
        assert (await rig.rpc("window.isMaximized"))[1] is True
        assert (await rig.rpc("window.toggleMaximize"))[1] is False

        async def restored() -> bool:
            items = await rig.events("window")
            return len(items) >= 2 and items[-1] == {"maximized": False}

        await wait_for(restored, message="the window event for the restore")
        for edge in ("n", "se", "w"):
            assert (await rig.rpc("window.startResize", {"edge": edge}))[0], "resizing from a handle must not fail"
        assert (await rig.rpc("window.startMove"))[0]
        assert (await rig.rpc("window.startResize", {"edge": "bogus"}))[0], "an unknown edge is ignored"
        await rig.finish()

    run(loop, scenario())


def test_closing_the_window_asks_the_page_and_the_page_answers_with_window_close(qapp, loop, tmp_path):
    async def scenario():
        rig = await open_rig(tmp_path)
        await rig.rpc("app.init")
        ok, created = await rig.rpc("session.new", {"projectId": str(rig.project.resolve())})
        await until(lambda: rig.factory.sessions and rig.factory.sessions[0].log.count("open-end") == 1, timeout=5)
        rig.window.close()  # Alt+F4, the taskbar ...
        await wait_for(lambda: rig.events("closeRequested"), message="the page to be asked")
        assert not rig.window.closed.is_set(), "the window stays until the page agrees"
        ok, _ = await rig.rpc("window.close")
        assert ok
        await asyncio.wait_for(rig.window.closed.wait(), 10)
        assert rig.factory.sessions[0].closed == 1, "every session is closed on the way out"
        assert not rig.window.isVisible()
        await release(rig.window)

    run(loop, scenario())


def test_closing_before_the_page_is_connected_just_shuts_down(qapp, loop, tmp_path):
    async def scenario():
        project = tmp_path / "p"
        project.mkdir()
        settings = AppSettings()
        window = MainWindow(settings, persist=False, page_file=PAGE)
        core = AppCore(SessionManager(settings, FakeFactory()), window.platform, persist=False)
        window.attach(core)
        window.show()
        window.close()
        await asyncio.wait_for(window.closed.wait(), 10)
        await release(window)

    run(loop, scenario())


def test_the_geometry_is_saved_with_the_settings_and_handed_back_on_the_next_start(qapp, loop, tmp_path, monkeypatch):
    async def scenario():
        rig = await open_rig(tmp_path)
        await rig.finish()
        saved = rig.window.settings.window_geometry
        assert saved, "the geometry is written when the window shuts down"

        # The offscreen screen is smaller than the window, so Qt would clamp it: check what is handed to Qt instead.
        handed: list[bytes] = []
        monkeypatch.setattr(MainWindow, "restoreGeometry", lambda self, data: handed.append(bytes(data.data())) or True)
        again = MainWindow(AppSettings(window_geometry=saved), persist=False, page_file=PAGE)
        assert [base64.b64encode(item).decode("ascii") for item in handed] == [saved]
        again.deleteLater()
        await asyncio.sleep(0.05)

    run(loop, scenario())


def test_the_page_file_is_required(qapp, tmp_path):
    window = MainWindow(AppSettings(), persist=False, page_file=tmp_path / "missing.html")
    with pytest.raises(FileNotFoundError, match="npm run build:app"):
        window.load()
    window.deleteLater()
