"""The application window: one frameless Qt window that shows the React page, and the bridge between page and Python.

The page draws everything (title bar included); Python owns the data and the processes. Messages go through one
QWebChannel object named ``backend``:

- page -> Python: ``backend.call(method, paramsJson, callId)``; the answer comes back as ``backend.reply(callId, ok, json)``;
- Python -> page: ``backend.event(name, json)``.

What only the window system can do (dialogs, moving and resizing a frameless window, thumbnails, the browser) is
``QtPlatform`` below, the ``Platform`` of ``core/app_core.py``.

The page is locked down: an off-the-record profile, no navigation away from the loaded document, no pop-up windows,
links go to the system browser (http / https / mailto only).
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from PySide6.QtCore import QByteArray, QFile, QIODevice, QObject, Qt, QUrl, Signal, Slot
from PySide6.QtGui import QCloseEvent, QColor, QDesktopServices, QGuiApplication, QIcon, QShowEvent
from PySide6.QtWebChannel import QWebChannel
from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile, QWebEngineScript, QWebEngineSettings
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import QApplication, QFileDialog, QMainWindow

from desktop.core.app_core import AppCore, AppError, error_text
from desktop.core.attachments import Attachment, Rejection, Thumbnail
from desktop.core.settings import AppSettings, save_settings
from desktop.core.winproc import CREATE_NO_WINDOW
from desktop.ui import imaging, winframe
from desktop.ui.attachments import load_bytes, load_path
from desktop.ui.theme import is_dark

log = logging.getLogger("desktop.shell")

APP_TITLE = "WittyCode"
MIN_SIZE = (900, 600)
DEFAULT_SIZE = (1320, 860)
BACKGROUNDS = {"light": "#ffffff", "dark": "#0b0b0b"}
"""The page's own background colours (``--bg`` of ``tokens.css``): what shows until the page has painted."""
SAFE_SCHEMES = ("http://", "https://", "mailto:")
PAGE_FILE = Path(__file__).resolve().parent.parent / "ui_dist" / "index.html"
ICON_FILE = Path(__file__).resolve().parent.parent / "assets" / "icon.ico"

_qwebchannel_source: str | None = None
_profile: QWebEngineProfile | None = None


def qwebchannel_source() -> str:
    """Qt ships the client script as a resource; it is injected into the page (no file or network access)."""
    global _qwebchannel_source
    if _qwebchannel_source is None:
        file = QFile(":/qtwebchannel/qwebchannel.js")
        if not file.open(QIODevice.OpenModeFlag.ReadOnly):
            raise RuntimeError("the Qt resource qwebchannel.js is missing")
        try:
            _qwebchannel_source = bytes(file.readAll().data()).decode("utf-8")
        finally:
            file.close()
    return _qwebchannel_source


def shared_profile() -> QWebEngineProfile:
    """One off-the-record profile: nothing is written to disk."""
    global _profile
    if _profile is None:
        _profile = QWebEngineProfile(QApplication.instance())
        _profile.setHttpCacheType(QWebEngineProfile.HttpCacheType.MemoryHttpCache)
        _profile.setSpellCheckEnabled(False)
    return _profile


def release_profile() -> None:
    """Call after the view has been deleted (Qt requires pages to go before their profile)."""
    global _profile
    if _profile is not None:
        _profile.deleteLater()
        _profile = None


def open_external(url: str) -> bool:
    if not url.startswith(SAFE_SCHEMES):
        return False
    return QDesktopServices.openUrl(QUrl(url))


def background_for(theme: str) -> QColor:
    name = theme if theme in BACKGROUNDS else ("dark" if is_dark() else "light")
    return QColor(BACKGROUNDS[name])


class ShellPage(QWebEnginePage):
    externalLink = Signal(str)

    def __init__(self, profile: QWebEngineProfile, parent: QObject) -> None:
        super().__init__(profile, parent)
        self.loading_allowed = True
        self.console: list[str] = []
        settings = self.settings()
        for name, value in (
            ("JavascriptCanOpenWindows", False),
            # The copy buttons of the page need it: with False, navigator.clipboard.writeText never finishes, even
            # for a click. Harmless here: the page is our own, has no network (CSP) and cannot navigate away.
            ("JavascriptCanAccessClipboard", True),
            ("LocalContentCanAccessRemoteUrls", False),
            ("LocalContentCanAccessFileUrls", False),
            ("LocalStorageEnabled", False),
            ("PluginsEnabled", False),
            ("ErrorPageEnabled", False),
            ("NavigateOnDropEnabled", False),
            ("ScrollAnimatorEnabled", False),
            ("ShowScrollBars", True),
        ):
            attribute = getattr(QWebEngineSettings.WebAttribute, name, None)
            if attribute is not None:
                settings.setAttribute(attribute, value)

    def acceptNavigationRequest(self, url: QUrl, nav_type: QWebEnginePage.NavigationType, is_main_frame: bool) -> bool:
        if self.loading_allowed:
            return True
        if nav_type == QWebEnginePage.NavigationType.NavigationTypeLinkClicked:
            self.externalLink.emit(url.toString())
        return False

    def createWindow(self, window_type: QWebEnginePage.WebWindowType) -> QWebEnginePage | None:
        return None

    def javaScriptConsoleMessage(self, level: Any, message: str, line: int, source: str) -> None:
        if level == QWebEnginePage.JavaScriptConsoleMessageLevel.ErrorMessageLevel:
            log.error("page error: %s (%s:%s)", message, Path(source).name, line)
            self.console.append(f"error: {message}")
        else:
            log.debug("page console: %s", message)
            self.console.append(message)


class Backend(QObject):
    """The one object the page can talk to (see the module docstring)."""

    reply = Signal(str, bool, str)
    event = Signal(str, str)

    def __init__(self, core: AppCore | None, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.core = core
        self.connected = False
        self._pending: set[asyncio.Future[Any]] = set()
        self.on_event: Callable[[str, dict[str, Any]], None] | None = None

    def emit_event(self, name: str, payload: dict[str, Any]) -> None:
        if self.on_event is not None:
            self.on_event(name, payload)
        self.event.emit(name, json.dumps(payload, ensure_ascii=False, default=str))

    @Slot(str, str, str)
    def call(self, method: str, params_json: str, call_id: str) -> None:
        if method == "app.init":
            self.connected = True
        task = asyncio.ensure_future(self._dispatch(method, params_json, call_id))
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)

    async def _dispatch(self, method: str, params_json: str, call_id: str) -> None:
        try:
            params = json.loads(params_json) if params_json else {}
            if self.core is None:
                raise RuntimeError("the application is not ready")
            result = await self.core.call(method, params)
            payload = json.dumps(result, ensure_ascii=False, default=str)
            self.reply.emit(call_id, True, payload)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            if not isinstance(error, AppError):
                log.exception("call %s failed", method)
            self.reply.emit(call_id, False, json.dumps(error_text(error), ensure_ascii=False))

    async def cancel_pending(self) -> None:
        for task in list(self._pending):
            task.cancel()
        if self._pending:
            await asyncio.gather(*list(self._pending), return_exceptions=True)


class _DialogResult(QObject):
    """Turns a non-blocking dialog into a future (no nested event loop inside an asyncio callback)."""

    def __init__(self, dialog: QFileDialog, future: asyncio.Future[list[str]]) -> None:
        super().__init__(dialog)
        self.dialog = dialog
        self.future = future
        dialog.finished.connect(self.done)

    @Slot(int)
    def done(self, result: int) -> None:
        paths = [str(p) for p in self.dialog.selectedFiles()] if result else []
        if not self.future.done():
            self.future.set_result(paths)
        self.dialog.deleteLater()


EDGES = {
    "n": Qt.Edge.TopEdge,
    "s": Qt.Edge.BottomEdge,
    "e": Qt.Edge.RightEdge,
    "w": Qt.Edge.LeftEdge,
    "ne": Qt.Edge.TopEdge | Qt.Edge.RightEdge,
    "nw": Qt.Edge.TopEdge | Qt.Edge.LeftEdge,
    "se": Qt.Edge.BottomEdge | Qt.Edge.RightEdge,
    "sw": Qt.Edge.BottomEdge | Qt.Edge.LeftEdge,
}


class QtPlatform:
    """What only the window system can do, for ``AppCore``."""

    def __init__(self, window: QMainWindow) -> None:
        self.window = window
        self.close_requested: Callable[[], None] = lambda: None
        self._pdf: Any = None

    # ---- dialogs
    def _ask(self, dialog: QFileDialog) -> asyncio.Future[list[str]]:
        future: asyncio.Future[list[str]] = asyncio.get_running_loop().create_future()
        _DialogResult(dialog, future)
        dialog.setModal(True)
        dialog.open()
        return future

    async def pick_folder(self, start: str) -> str | None:
        dialog = QFileDialog(self.window, "选择项目文件夹", start)
        dialog.setFileMode(QFileDialog.FileMode.Directory)
        dialog.setOption(QFileDialog.Option.ShowDirsOnly, True)
        paths = await self._ask(dialog)
        return paths[0] if paths else None

    async def pick_files(self, start: str) -> list[str]:
        dialog = QFileDialog(self.window, "选择要附加的文件", start)
        dialog.setFileMode(QFileDialog.FileMode.ExistingFiles)
        return await self._ask(dialog)

    # ---- the system
    def open_url(self, url: str) -> None:
        open_external(url)

    def reveal(self, path: str) -> None:
        QDesktopServices.openUrl(QUrl.fromLocalFile(path))

    def open_path(self, path: str) -> None:
        QDesktopServices.openUrl(QUrl.fromLocalFile(path))

    def open_with(self, path: str) -> None:
        if sys.platform == "win32":
            subprocess.Popen(["rundll32.exe", "shell32.dll,OpenAs_RunDLL", path], creationflags=CREATE_NO_WINDOW)
        else:
            self.open_path(path)

    def show_in_folder(self, path: str) -> None:
        if sys.platform == "win32":
            subprocess.Popen(f'explorer.exe /select,"{path}"', creationflags=CREATE_NO_WINDOW)
        else:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(Path(path).parent)))

    # ---- PDFs in the side panel
    def _pdf_reader(self) -> Any:
        if self._pdf is None:
            from desktop.ui.pdfview import PdfReader  # (the whole PDF engine comes with it: only when a PDF is opened)

            self._pdf = PdfReader()
        return self._pdf

    def pdf_pages(self, path: str) -> list[tuple[float, float]]:
        return self._pdf_reader().pages(path)

    def pdf_page(self, path: str, page: int, width: int) -> str:
        return self._pdf_reader().page_image(path, page, width)

    def release_documents(self) -> None:
        if self._pdf is not None:
            self._pdf.close()
            self._pdf = None

    # ---- attachments
    def thumbnail(self, attachment: Attachment) -> Thumbnail | None:
        """A small picture for the page (the cards read the original's dimensions from the attachment itself)."""
        if attachment.kind != "image" or not attachment.data:
            return None
        return imaging.thumbnail(attachment.data)

    def load_bytes(self, name: str, data: bytes) -> Attachment | Rejection:
        return load_bytes(name, data)

    def load_path(self, path: str) -> Attachment | Rejection:
        return load_path(path)

    def full_picture(self, attachment: Attachment) -> str | None:
        if attachment.kind != "image" or not attachment.data:
            return None
        return f"data:{attachment.media_type or 'image/png'};base64,{base64.b64encode(attachment.data).decode('ascii')}"

    # ---- the window
    def window_minimize(self) -> None:
        self.window.showMinimized()

    def window_toggle_maximize(self) -> bool:
        if self.window.isMaximized():
            self.window.showNormal()
        else:
            self.window.showMaximized()
        return self.window.isMaximized()

    def window_close(self) -> None:
        self.close_requested()

    def window_start_move(self) -> None:
        handle = self.window.windowHandle()
        if handle is not None:
            handle.startSystemMove()

    def window_start_resize(self, edge: str) -> None:
        handle = self.window.windowHandle()
        edges = EDGES.get(edge)
        if handle is not None and edges is not None and not self.window.isMaximized():
            handle.startSystemResize(edges)

    def window_is_maximized(self) -> bool:
        return self.window.isMaximized()


class MainWindow(QMainWindow):
    """A frameless window around the page. Closing it never closes it directly: the page decides (it may ask first)."""

    _last_maximized = False
    _load_seen = False
    backend: Backend | None = None  # Qt delivers change events while the constructor is still running

    def __init__(self, settings: AppSettings, *, persist: bool = True, page_file: Path | None = None, subtitle: str = "") -> None:
        super().__init__()
        self.settings = settings
        self.persist = persist
        self.page_file = page_file or PAGE_FILE
        self.subtitle = subtitle
        self.closed = asyncio.Event()
        """Set when the window has finished shutting down (every session closed)."""
        self.core: AppCore | None = None
        self.page_loaded = False
        self._shutdown_task: asyncio.Future[None] | None = None
        self.frame = winframe.frame_kind(QGuiApplication.platformName())
        """The corner shape of the window's outline the page draws (``winframe.frame_kind``); ``""``: none."""
        self._frame_applied = False

        self.setWindowTitle(f"{APP_TITLE}（{subtitle}）" if subtitle else APP_TITLE)
        self.setWindowFlags(
            Qt.WindowType.Window
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowSystemMenuHint
            | Qt.WindowType.WindowMinimizeButtonHint
            | Qt.WindowType.WindowMaximizeButtonHint
            | Qt.WindowType.WindowCloseButtonHint
        )
        if ICON_FILE.is_file():
            self.setWindowIcon(QIcon(str(ICON_FILE)))
        self.setMinimumSize(*MIN_SIZE)
        self.resize(*DEFAULT_SIZE)

        self.platform = QtPlatform(self)
        self.platform.close_requested = self.quit_now

        self.view = QWebEngineView(self)
        self.page = ShellPage(shared_profile(), self.view)
        self.view.setPage(self.page)
        self.page.setBackgroundColor(background_for(settings.theme))
        self.view.setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)
        self.page.externalLink.connect(open_external)
        self.channel = QWebChannel(self.page)
        # Owned by the channel, not by the window: Qt deletes a window's children in creation order, and a channel that
        # is destroyed after the object registered with it crashes the process.
        self.backend = Backend(None, self.channel)
        self.channel.registerObject("backend", self.backend)
        self.page.setWebChannel(self.channel)
        script = QWebEngineScript()
        script.setName("qwebchannel")
        script.setSourceCode(qwebchannel_source())
        script.setInjectionPoint(QWebEngineScript.InjectionPoint.DocumentCreation)
        script.setWorldId(QWebEngineScript.ScriptWorldId.MainWorld)
        script.setRunsOnSubFrames(False)
        self.page.scripts().insert(script)
        self.page.loadFinished.connect(self._on_loaded)
        self.setCentralWidget(self.view)
        self.backend.on_event = self._on_event

        geometry = settings.window_geometry
        if geometry:
            self.restoreGeometry(QByteArray.fromBase64(geometry.encode("ascii", "ignore")))
        # A frameless window starts without the maximize/minimize bookkeeping a framed one has: follow it ourselves.
        self._last_maximized = self.isMaximized()

    # ------------------------------------------------------------------ wiring

    def attach(self, core: AppCore) -> None:
        """Connect the application core: its events go to the page, the page's calls to it."""
        self.core = core
        self.backend.core = core
        core.emit = self.backend.emit_event

    def load(self) -> None:
        if not self.page_file.is_file():
            raise FileNotFoundError(f"找不到界面文件：{self.page_file}（先在 frontend 目录里运行 npm run build:app）")
        self.page.loading_allowed = True
        url = QUrl.fromLocalFile(str(self.page_file))
        query = f"theme={self.settings.theme}&fs={self.settings.font_scale}"  # what the page shows before app.init answers
        url.setQuery(f"{query}&frame={self.frame}" if self.frame else query)
        self.page.load(url)

    def showEvent(self, event: QShowEvent) -> None:  # noqa: N802 (Qt name)
        super().showEvent(event)
        if self.frame and not self._frame_applied:
            # the native window exists now: the system's drop shadow (and rounded corners on Windows 11) for it
            self._frame_applied = True
            winframe.apply(int(self.winId()))

    def _on_loaded(self, ok: bool) -> None:
        if self._load_seen:
            return  # a refused navigation also ends with loadFinished(False); the page itself is still there
        self._load_seen = True
        self.page_loaded = ok
        self.page.loading_allowed = False  # from now on the page may not go anywhere else
        if not ok:
            log.error("the page did not load: %s", self.page_file)

    def _on_event(self, name: str, payload: dict[str, Any]) -> None:
        if name == "settings":
            self.page.setBackgroundColor(background_for(str(payload.get("settings", {}).get("theme", ""))))

    def changeEvent(self, event: Any) -> None:  # noqa: N802 (Qt name)
        super().changeEvent(event)
        maximized = self.isMaximized()
        if maximized != self._last_maximized:
            self._last_maximized = maximized
            if self.backend is not None:
                self.backend.emit_event("window", {"maximized": maximized})

    # ------------------------------------------------------------------ closing

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 (Qt name)
        if self._shutdown_task is not None:
            event.ignore()
            return
        event.ignore()  # the window stays until every droid process is closed; it only looks closed
        if self.page_loaded and self.backend.connected:
            self.backend.emit_event("closeRequested", {})  # the page may ask first, then calls window.close
        else:
            self.quit_now()

    def quit_now(self) -> None:
        self.begin_shutdown()

    def begin_shutdown(self) -> asyncio.Future[None]:
        if self._shutdown_task is None:
            self._shutdown_task = asyncio.ensure_future(self.shutdown())
        return self._shutdown_task

    def save_state(self) -> None:
        self.settings.window_geometry = bytes(self.saveGeometry().toBase64().data()).decode("ascii")
        if not self.persist:
            return
        try:
            save_settings(self.settings)
        except OSError as error:
            log.warning("could not save settings: %s", error)

    async def shutdown(self) -> None:
        """Save state, hide the window, close every session (consumers are cancelled first), then signal ``closed``."""
        try:
            self.save_state()
            self.hide()
            await self.backend.cancel_pending()
            if self.core is not None:
                await self.core.shutdown()
        finally:
            self.platform.release_documents()
            self._detach_channel()
            self.closed.set()

    def _detach_channel(self) -> None:
        """Stop the page and the backend talking to each other, before either is deleted."""
        if self.backend is None:
            return
        self.backend.on_event = None
        try:
            self.page.setWebChannel(None)
            self.channel.deregisterObject(self.backend)
        except RuntimeError:  # already gone
            pass

    # ------------------------------------------------------------------ for tests and tools

    def run_js(self, source: str, callback: Callable[[Any], None] | None = None) -> None:
        if callback is None:
            self.page.runJavaScript(source)
        else:
            self.page.runJavaScript(source, callback)

    async def eval_js(self, source: str, timeout: float = 10.0) -> Any:
        """Evaluate ``source`` in the page and return the (JSON-compatible) result."""
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()

        def done(value: Any) -> None:
            if not future.done():
                future.set_result(value)

        self.page.runJavaScript(source, done)
        return await asyncio.wait_for(future, timeout)

