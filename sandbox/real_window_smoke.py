"""A real (not offscreen) WittyCode window on fake Droid sessions: no droid process, no model call.

    .\\.venv\\Scripts\\python.exe sandbox\\real_window_smoke.py [--keep]

Opens the window with the built page, takes screenshots of the screen area it covers (``sandbox/real_window/*.png``) and
checks what only a real window can show: the frameless window maximizes inside the work area (not over the taskbar), the
page paints, the title bar buttons work. ``--keep`` leaves the window open for looking at it by hand.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from PySide6 import QtWebEngineWidgets  # noqa: E402,F401  (before the QApplication)
from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

import qasync  # noqa: E402

from desktop.ui.shell import PAGE_FILE, MainWindow, release_profile  # noqa: E402
from tests.app_support import make_core  # noqa: E402
from tests.support import saved_session  # noqa: E402

OUT = ROOT / "sandbox" / "real_window"


async def settle(seconds: float = 1.0) -> None:
    await asyncio.sleep(seconds)


def grab(window: MainWindow, name: str) -> Path:
    OUT.mkdir(parents=True, exist_ok=True)
    screen = window.screen()
    shot = screen.grabWindow(0, window.x(), window.y(), window.width(), window.height())
    path = OUT / f"{name}.png"
    shot.save(str(path))
    return path


PAD = 60


def luminance(color) -> int:
    return (color.red() * 30 + color.green() * 59 + color.blue() * 11) // 100


def edge_report(window: MainWindow, name: str) -> dict[str, float]:
    """The screen around the window (with its drop shadow: this is what the compositor shows), as numbers.

    ``shadow``: how much darker the pixels just below / left of the window are than the ones 40 px away (on the white plate
    behind it); ``line``: how much darker the window's own outermost column is than the page just inside it."""
    OUT.mkdir(parents=True, exist_ok=True)
    screen = window.screen()
    x, y, w, h = window.x(), window.y(), window.width(), window.height()
    shot = screen.grabWindow(0, x - PAD, y - PAD, w + 2 * PAD, h + 2 * PAD)
    shot.save(str(OUT / f"{name}.png"))
    image = shot.toImage()
    k = image.width() / (w + 2 * PAD)  # device pixels per logical pixel

    def at(px: float, py: float) -> int:
        return luminance(image.pixelColor(int(px * k), int(py * k)))

    xs = range(PAD + 120, PAD + w - 120, 12)
    ys = range(PAD + 120, PAD + h - 120, 12)
    below_near = sum(at(i, PAD + h + 3) for i in xs) / len(xs)
    below_far = sum(at(i, PAD + h + 40) for i in xs) / len(xs)
    left_near = sum(at(PAD - 3, j) for j in ys) / len(ys)
    left_far = sum(at(PAD - 40, j) for j in ys) / len(ys)
    edge = sum(at(PAD + 0.5, j) for j in ys) / len(ys)
    inside = sum(at(PAD + 6, j) for j in ys) / len(ys)
    return {"shadow_below": below_far - below_near, "shadow_left": left_far - left_near, "line": inside - edge}


async def main(keep: bool) -> int:
    import tempfile

    from PySide6.QtWidgets import QWidget

    tmp = Path(tempfile.mkdtemp(prefix="wc-smoke-"))
    core, factory, events, _platform, _engine = make_core(tmp)
    factory.saved = [saved_session("s-1", "修复点击闪烁", minutes_ago=5), saved_session("s-2", "附件功能设计", minutes_ago=1500)]
    window = MainWindow(core.settings, persist=False)
    core.platform = window.platform
    window.attach(core)
    window.load()
    # a white plate behind the window: the case where the app's edge was impossible to see
    window.resize(1100, 700)
    window.move(220, 160)
    plate = QWidget()
    # both on top of everything else for the measurement (the user's own windows would be in the picture otherwise)
    plate.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.Tool | Qt.WindowType.WindowStaysOnTopHint)
    plate.setStyleSheet("background: #ffffff")
    plate.setGeometry(window.x() - 100, window.y() - 100, window.width() + 200, window.height() + 200)
    window.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
    plate.show()
    window.show()
    failures: list[str] = []

    def check(name: str, ok: bool) -> None:
        print("PASS" if ok else "FAIL", name)
        if not ok:
            failures.append(name)

    for _ in range(100):
        if window.backend.connected:
            break
        await settle(0.1)
    check("the page loaded and asked for its state", window.page_loaded and window.backend.connected)
    await settle(1.5)
    check("the page has the keyboard focus without a click (shortcuts work at once)", window.isActiveWindow() and await window.eval_js("document.hasFocus()") is True)
    print("screenshot:", grab(window, "start"))
    check("the saved sessions are listed", await window.eval_js("document.querySelectorAll('.srow').length") == 2)
    print("window frame kind:", repr(window.frame))
    check("the page draws the window's outline", await window.eval_js("!!document.querySelector('.frame-line')") is True)
    report = edge_report(window, "edge")
    print("edge:", {k: round(v, 1) for k, v in report.items()})
    check("the window has a drop shadow on the white plate behind it (darker below and left of it)", report["shadow_below"] >= 12 and report["shadow_left"] >= 4)
    check("the window's outermost pixels are a line, darker than the page just inside", report["line"] >= 25)

    screen = window.screen()
    work = screen.availableGeometry()
    window.platform.window_toggle_maximize()
    await settle(1.0)
    geometry = window.geometry()
    check("maximized: inside the work area, not over the taskbar", work.contains(geometry) or geometry.height() <= work.height() + 16)
    print("work area", work, "window", geometry, "screen", screen.geometry())
    print("screenshot:", grab(window, "maximized"))
    check("the page knows it is maximized", await window.eval_js("document.querySelector('.titlebar .tb-btn[aria-label=\"还原\"]') !== null"))
    check("no outline while maximized (the window fills the screen)", await window.eval_js("!document.querySelector('.frame-line')") is True)
    window.platform.window_toggle_maximize()
    await settle(0.8)
    check("restored", not window.isMaximized())
    check("the outline is back", await window.eval_js("!!document.querySelector('.frame-line')") is True)
    report = edge_report(window, "edge-restored")
    print("edge after restoring:", {k: round(v, 1) for k, v in report.items()})
    check("and so is the drop shadow", report["shadow_below"] >= 12 and report["shadow_left"] >= 4)

    if keep:
        print("window left open: close it with the × in the title bar")
        await window.closed.wait()
    else:
        window.quit_now()
        await asyncio.wait_for(window.closed.wait(), 20)
    window.deleteLater()
    plate.close()
    print("RESULT:", "FAILED " + ", ".join(failures) if failures else "all passed")
    return 1 if failures else 0


if __name__ == "__main__":
    if not PAGE_FILE.is_file():
        raise SystemExit("the page is not built: run `npm run build:app` in frontend/")
    app = QApplication.instance() or QApplication(["smoke"])
    app.setQuitOnLastWindowClosed(False)
    loop = qasync.QEventLoop(app)
    asyncio.set_event_loop(loop)
    with loop:
        code = loop.run_until_complete(main("--keep" in sys.argv))
    release_profile()
    raise SystemExit(code)
