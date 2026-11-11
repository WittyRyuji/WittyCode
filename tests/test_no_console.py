"""Running without a console (pythonw.exe): droid.exe must not open console windows; the log goes to a file."""

from __future__ import annotations

import asyncio
import logging
import subprocess
import sys
from pathlib import Path

import pytest
from droid_sdk import Autonomy, DroidError, InteractionHandlers, Mode, SessionConfig
from droid_sdk import transport as sdk_transport
from droid_sdk.transport import ProcessTransport

from desktop.core import winproc
from desktop.core.session_manager import DroidFactory
from desktop.core.settings import SETTINGS_ENV
from desktop.ui.app import setup_logging

ROOT = Path(__file__).resolve().parent.parent
PYTHONW = Path(sys.executable).with_name("pythonw.exe")
on_windows = pytest.mark.skipif(sys.platform != "win32", reason="Windows only")
with_pythonw = pytest.mark.skipif(sys.platform != "win32" or not PYTHONW.is_file(), reason="needs Windows and pythonw.exe")


@pytest.fixture(autouse=True)
def unpatched_sdk(monkeypatch):
    """Every test starts from the plain SDK, whatever an earlier test (or a DroidFactory) installed."""
    monkeypatch.setattr(sdk_transport, "asyncio", asyncio)


class Spawns:
    """Stands in for asyncio.create_subprocess_exec: records each call, then fails like a missing executable."""

    def __init__(self) -> None:
        self.calls: list[tuple[tuple, dict]] = []

    async def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        raise FileNotFoundError("no droid here")

    @property
    def flags(self) -> list:
        return [kwargs.get("creationflags") for _, kwargs in self.calls]


@pytest.fixture
def spawns(monkeypatch):
    recorder = Spawns()
    monkeypatch.setattr(asyncio, "create_subprocess_exec", recorder)
    return recorder


@pytest.fixture
def root_logger():
    """The root logger as setup_logging finds it, put back afterwards.

    logging.basicConfig does nothing while the root logger has handlers and pytest attaches its own for the
    whole test, so a test that calls setup_logging clears the handlers first.
    """
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    yield root
    for handler in root.handlers:
        if handler not in handlers:
            handler.close()
    root.handlers[:] = handlers
    root.setLevel(level)


# ------------------------------------------------------------------------------ the patch itself


def test_the_sdk_spawn_gets_create_no_window_and_nothing_else_changes(spawns, tmp_path):
    def connect() -> None:
        transport = ProcessTransport(exec_path="droid.exe", exec_args=["exec", "--flag"], cwd=str(tmp_path), env={"X": "1"})
        with pytest.raises(FileNotFoundError):
            asyncio.run(transport.connect())

    connect()
    assert spawns.flags == [None], "the SDK passes no creationflags itself: that is what the patch is for"

    assert winproc.install(platform="win32")
    connect()
    args, kwargs = spawns.calls[-1]
    assert args == ("droid.exe", "exec", "--flag")
    assert kwargs["creationflags"] == winproc.CREATE_NO_WINDOW == 0x08000000
    assert kwargs["cwd"] == str(tmp_path) and kwargs["env"]["X"] == "1"
    assert kwargs["stdin"] == kwargs["stdout"] == kwargs["stderr"] == asyncio.subprocess.PIPE


@pytest.mark.parametrize("flags", [0, 0x00000200, 0x08000200])
def test_creation_flags_given_by_the_caller_are_kept(spawns, flags):
    winproc.install(platform="win32")
    with pytest.raises(FileNotFoundError):
        asyncio.run(sdk_transport.asyncio.create_subprocess_exec("x", creationflags=flags))
    assert spawns.flags == [flags]


def test_other_users_of_asyncio_are_not_touched(spawns):
    winproc.install(platform="win32")
    with pytest.raises(FileNotFoundError):
        asyncio.run(asyncio.create_subprocess_exec("x"))
    assert spawns.flags == [None]
    for name in ("Lock", "wait_for", "create_task", "shield", "TimeoutError", "CancelledError", "StreamReader", "subprocess"):
        assert getattr(sdk_transport.asyncio, name) is getattr(asyncio, name), name


def test_installing_twice_changes_nothing_more():
    assert not winproc.is_installed()
    assert winproc.install(platform="win32") is True
    assert winproc.is_installed()
    first = sdk_transport.asyncio
    assert winproc.install(platform="win32") is True
    assert sdk_transport.asyncio is first


def test_off_windows_nothing_is_installed():
    assert winproc.install(platform="linux") is False
    assert sdk_transport.asyncio is asyncio and not winproc.is_installed()


def test_an_sdk_that_no_longer_matches_is_left_alone_with_a_warning(monkeypatch, caplog):
    other = object()
    monkeypatch.setattr(sdk_transport, "asyncio", other)
    with caplog.at_level(logging.WARNING, logger="desktop.winproc"):
        assert winproc.install(platform="win32") is False
    assert sdk_transport.asyncio is other
    assert "console windows" in caplog.text


@on_windows
@pytest.mark.parametrize("how", ["new session", "resumed session", "history", "model list"])
def test_every_way_the_app_starts_droid_is_windowless(spawns, tmp_path, how):
    factory = DroidFactory(lambda: tmp_path / "droid.exe")
    handlers = InteractionHandlers()

    async def start() -> None:
        if how == "new session":
            config = SessionConfig(autonomy=Autonomy("off"), mode=Mode("auto"))
            await factory.create(cwd=tmp_path, model=None, reasoning=None, config=config, interactions=handlers).open()
        elif how == "resumed session":
            await factory.resume("00000000-0000-0000-0000-000000000000", interactions=handlers).open()
        elif how == "history":
            await factory.load_history("00000000-0000-0000-0000-000000000000", tmp_path)
        else:
            await factory.list_models(tmp_path)

    with pytest.raises((OSError, DroidError)):
        asyncio.run(start())
    assert spawns.flags == [winproc.CREATE_NO_WINDOW]


# ------------------------------------------------------------------------------ real processes

# A parent without a console (run by pythonw.exe) starts a console program, which reports whether it has a console window.
PARENT_SCRIPT = r'''
import asyncio, os, subprocess, sys, traceback
sys.path.insert(0, @ROOT@)
mode, out, python = sys.argv[1:4]
CHILD = (
    "import ctypes,sys;k=ctypes.windll.kernel32;k.GetConsoleWindow.restype=ctypes.c_void_p;"
    "open(sys.argv[1],'w').write(str(k.GetConsoleWindow() or 0))"
)


async def main():
    if mode == "unpatched":
        # The same as the SDK's call, only hidden at creation so that this control run flashes no window.
        info = subprocess.STARTUPINFO()
        info.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        info.wShowWindow = subprocess.SW_HIDE
        process = await asyncio.create_subprocess_exec(python, "-c", CHILD, out, startupinfo=info)
        await process.wait()
        return
    from desktop.core import winproc
    from droid_sdk.transport import ProcessTransport

    assert winproc.install()
    transport = ProcessTransport(exec_path=python, exec_args=["-c", CHILD, out])
    await transport.connect()
    try:
        for _ in range(300):
            if os.path.exists(out) and os.path.getsize(out):
                break
            await asyncio.sleep(0.1)
    finally:
        await transport.close()


try:
    asyncio.run(main())
except BaseException:
    open(out + ".err", "w").write(traceback.format_exc())
'''


def console_window_of_child(tmp_path: Path, mode: str) -> int:
    script = tmp_path / f"parent_{mode}.py"
    script.write_text(PARENT_SCRIPT.replace("@ROOT@", repr(str(ROOT))), encoding="utf-8")
    out = tmp_path / f"{mode}.txt"
    subprocess.run([str(PYTHONW), str(script), mode, str(out), sys.executable], timeout=120, check=False)
    error = Path(f"{out}.err")
    assert out.is_file() and out.stat().st_size, error.read_text(encoding="utf-8") if error.is_file() else "the probe wrote nothing"
    return int(out.read_text(encoding="utf-8"))


@with_pythonw
def test_a_console_program_started_by_a_console_less_process_gets_no_window_through_the_patched_sdk(tmp_path):
    assert console_window_of_child(tmp_path, "unpatched") != 0, "control: without the flag Windows gives the child a console window"
    assert console_window_of_child(tmp_path, "patched") == 0


# ------------------------------------------------------------------------------ the log file and the launchers


def test_without_a_console_the_log_goes_to_a_file_next_to_the_settings(tmp_path, monkeypatch, root_logger):
    monkeypatch.setattr(sys, "stderr", None)
    monkeypatch.setenv(SETTINGS_ENV, str(tmp_path / "settings.json"))
    root_logger.handlers.clear()
    setup_logging(False)
    logging.getLogger("desktop.sessions").warning("probe without a console")
    logging.getLogger("desktop.sessions").info("below the log level")
    for handler in root_logger.handlers:
        handler.flush()
    text = (tmp_path / "WittyCode.log").read_text(encoding="utf-8")
    assert "WARNING desktop.sessions: probe without a console" in text
    assert "below the log level" not in text


def test_a_log_file_that_cannot_be_created_does_not_stop_the_start(tmp_path, monkeypatch, root_logger):
    blocker = tmp_path / "not-a-folder"
    blocker.write_text("x", encoding="utf-8")
    monkeypatch.setattr(sys, "stderr", None)
    monkeypatch.setenv(SETTINGS_ENV, str(blocker / "settings.json"))
    root_logger.handlers.clear()
    setup_logging(False)
    logging.getLogger("desktop").warning("nowhere to write this")


@with_pythonw
@pytest.mark.parametrize(
    ("start", "folder"),
    [
        (["-m", "desktop"], ROOT),
        ([str(ROOT / "WittyCode.pyw")], None),
    ],
    ids=["pythonw -m desktop", "WittyCode.pyw"],
)
def test_a_start_without_a_console_runs_main_and_reports_into_the_log_file(tmp_path, start, folder):
    settings = tmp_path / "settings.json"
    missing = tmp_path / "missing.jsonl"
    completed = subprocess.run(
        [str(PYTHONW), *start, "--settings", str(settings), "--replay", str(missing)],
        cwd=folder or tmp_path,
        timeout=120,
        check=False,
    )
    assert completed.returncode == 2
    assert "找不到回放文件" in (tmp_path / "WittyCode.log").read_text(encoding="utf-8")
