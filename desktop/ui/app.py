"""Application entry: command line, Qt application, qasync event loop, the application core and the window."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# QtWebEngine has to be imported before the QApplication exists.
from PySide6 import QtWebEngineWidgets  # noqa: F401  (import for its side effect)
from PySide6.QtCore import QCoreApplication, QEvent, Qt
from PySide6.QtWidgets import QApplication

import qasync

from desktop.core.app_core import AppCore
from desktop.core.cache import Cache
from desktop.core.memory.launch import MemoryLaunch
from desktop.core.session_manager import SessionManager
from desktop.core.settings import SETTINGS_ENV, AppSettings, default_settings_path, load_settings, migrate_legacy_settings
from desktop.ui.shell import APP_TITLE, MainWindow, release_profile
from desktop.ui.tasks import cancel_all, kill_pending_timers

log = logging.getLogger("desktop")
APP_USER_MODEL_ID = "WittyCode.app"
LOG_FILE_NAME = "WittyCode.log"


@dataclass
class AppContext:
    app: QApplication
    loop: qasync.QEventLoop
    manager: SessionManager
    core: AppCore
    window: MainWindow
    options: argparse.Namespace


def parse_args(argv: list[str]) -> tuple[argparse.Namespace, list[str]]:
    parser = argparse.ArgumentParser(
        prog=APP_TITLE,
        description="WittyCode：编程代理的桌面客户端，把工具调用折叠起来，优先展示助手的回答和阶段结果。",
    )
    parser.add_argument("--replay", metavar="FIXTURE", help="离线演示：回放一份录制的会话（fixtures/*.jsonl），不启动 droid，也不调用模型")
    parser.add_argument("--speed", type=float, default=1.0, help="回放速度倍数（默认 1）")
    parser.add_argument("--settings", metavar="FILE", help="使用指定的设置文件，而不是 %%APPDATA%%\\WittyCode\\settings.json")
    parser.add_argument("--project", action="append", default=[], metavar="DIR", help="启动时加入的项目文件夹（可重复）")
    parser.add_argument("--page", metavar="FILE", help="使用指定的界面文件（开发用），而不是 desktop/ui_dist/index.html")
    parser.add_argument("--debug", action="store_true", help="输出调试日志")
    return parser.parse_known_args(argv)


def apply_settings_option(options: argparse.Namespace) -> None:
    """``--settings FILE`` is passed on through the environment so that every reader of the settings path agrees."""
    if options.settings:
        os.environ[SETTINGS_ENV] = options.settings


def log_file_path() -> Path:
    return default_settings_path().with_name(LOG_FILE_NAME)


def setup_logging(debug: bool) -> None:
    level = logging.DEBUG if debug else logging.WARNING
    fmt = "%(asctime)s %(levelname)s %(name)s: %(message)s"
    if sys.stderr is not None:
        logging.basicConfig(level=level, format=fmt)
        return
    # pythonw.exe has no console: keep the log next to the settings instead.
    path = log_file_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        logging.basicConfig(level=level, format=fmt, filename=path, encoding="utf-8")
    except OSError:
        logging.basicConfig(level=level, format=fmt, handlers=[logging.NullHandler()])


def _log_loop_exception(loop: asyncio.AbstractEventLoop, context: dict[str, Any]) -> None:
    log.error("unhandled asyncio error: %s", context.get("message"), exc_info=context.get("exception"))


def _set_app_user_model_id() -> None:
    """A stable id lets Windows group the taskbar button and use this app's icon instead of python.exe's."""
    if sys.platform != "win32":
        return
    try:
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_USER_MODEL_ID)
    except Exception:  # purely cosmetic
        pass


def make_codex(settings: AppSettings) -> Any:
    """The Codex engine, or ``None`` when it is not part of this build."""
    try:
        from desktop.core.codex import CodexEngine
    except ImportError:
        return None
    return CodexEngine(settings)


def make_devin(settings: AppSettings) -> Any:
    """The Devin engine, or ``None`` when it is not part of this build."""
    try:
        from desktop.core.devin import DevinEngine
    except ImportError:
        return None
    return DevinEngine(settings)


def make_chatgpt(settings: AppSettings, open_url: Any, providers: Any = None) -> Any:
    """The engine of the application's own harness, or ``None``: the user's ChatGPT plan, and with ``providers`` the models of the
    API providers, which it asks itself (no Droid in between)."""
    try:
        from desktop.core.chatgpt.engine import ChatGPTEngine
        from desktop.core.llm.backend import ApiBackend
    except ImportError:
        return None
    api = ApiBackend(providers) if providers is not None else None
    return ChatGPTEngine(settings, default_settings_path().parent / "chatgpt", open_browser=open_url, api=api)


def make_providers() -> Any:
    from desktop.core.providers import ProviderStore

    return ProviderStore()


def build_application(options: argparse.Namespace, qt_args: list[str] | None = None) -> AppContext:
    QCoreApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)
    app = QApplication.instance() or QApplication([APP_TITLE, *(qt_args or [])])
    app.setApplicationName(APP_TITLE)
    # The window closing must not end the loop: sessions are closed asynchronously first (MainWindow.shutdown).
    app.setQuitOnLastWindowClosed(False)
    _set_app_user_model_id()
    loop = qasync.QEventLoop(app)
    asyncio.set_event_loop(loop)
    loop.set_exception_handler(_log_loop_exception)

    apply_settings_option(options)
    replay = bool(options.replay)
    if not replay:
        migrate_legacy_settings()
    settings = AppSettings() if replay else load_settings()
    for folder in options.project:
        settings.add_project(folder)
    factory = None
    if replay:
        from desktop.devtools.replay import ReplayFactory

        factory = ReplayFactory(options.replay, speed=options.speed)
    manager = SessionManager(settings, factory)
    memory = None
    if not replay:
        memory = MemoryLaunch(
            default_settings_path().parent / "memory",
            enabled=lambda engine: settings.memory_enabled and engine in settings.memory_engines,
            recall=lambda: settings.memory_recall,
            capture=lambda: settings.memory_capture,
            learn=lambda: settings.memory_curate,
        )
    window = MainWindow(
        settings,
        persist=not replay,
        page_file=Path(options.page) if options.page else None,
        subtitle="离线回放" if replay else "",
    )
    providers = None if replay else make_providers()
    core = AppCore(
        manager,
        window.platform,
        codex=None if replay else make_codex(settings),
        devin=None if replay else make_devin(settings),
        chatgpt=None if replay else make_chatgpt(settings, window.platform.open_url, providers),
        providers=providers,
        persist=not replay,
        offline=replay,
        cache=Cache(None if replay else default_settings_path().parent / "cache"),
        memory=memory,
    )
    window.attach(core)
    return AppContext(app, loop, manager, core, window, options)


def cleanup(app: QApplication) -> None:
    """Delete the web view, then the shared web profile (Qt wants the pages gone first)."""
    app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    app.processEvents()
    release_profile()
    app.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def run(context: AppContext) -> int:
    window = context.window

    async def serve() -> None:
        window.load()
        window.show()
        await window.closed.wait()
        await cancel_all()

    try:
        with context.loop:
            context.loop.run_until_complete(serve())
    finally:
        kill_pending_timers(context.loop)
        window.deleteLater()
        cleanup(context.app)
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv if argv is None else argv)
    options, qt_args = parse_args(argv[1:])
    apply_settings_option(options)
    setup_logging(options.debug)
    if options.replay and not Path(options.replay).is_file():
        message = f"找不到回放文件：{options.replay}"
        if sys.stderr is None:  # started without a console: the log file is the only place to say it
            log.error(message)
        else:
            print(message, file=sys.stderr)
        return 2
    return run(build_application(options, qt_args))
