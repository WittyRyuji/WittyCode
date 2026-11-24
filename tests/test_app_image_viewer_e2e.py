"""Drive the real Qt WebEngine window and clipboard, with generated PNGs and a loopback fake model."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest
from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
from PySide6.QtGui import QColor, QImage, QMouseEvent, QPainter, QWheelEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from tests.e2e_support import open_rig
from tests.fake_responses import say
from tests.test_app_e2e import new_conversation
from tests.ui_support import outside_task, run, wait_for

pytestmark = pytest.mark.ui

ATTACHMENT = ".block.user .att-img img"
INLINE = ".inline-picture img"
VIEWER = ".lightbox img"
MENU = ".image-menu-layer .menu"


def picture(path, width, height, color):
    image = QImage(width, height, QImage.Format.Format_RGB32)
    image.fill(QColor(color))
    with QPainter(image) as painter:
        for row in range(6):
            for col in range(9):
                color = QColor("#f8cf6b" if (row + col) % 2 else "#3b6593")
                painter.fillRect(24 + col * 100, 24 + row * 90, 92, 82, color)

    assert image.save(str(path))
    return image


async def natural_width(rig, selector, width=None):
    check = f"=== {width}" if width is not None else "> 0"
    await wait_for(lambda: rig.js(f"document.querySelector({json.dumps(selector)})?.naturalWidth {check}"),
                   10, f"{selector} decoded")


async def copy_picture(rig, clipboard, selector, expected, *, evidence=None):
    await rig.click(selector, right=True)
    await rig.wait_for(f"{MENU} [role=menuitem]", 1)
    assert await rig.texts(".menu-label") == ["复制图片"]
    if evidence:
        await rig.shot(".winframe", evidence)
    # A previous image in the clipboard must not make this check pass.
    clipboard.setText("before image copy")
    await rig.click_text(f"{MENU} [role=menuitem]", "复制图片")
    await wait_for(lambda: not clipboard.image().isNull(), 5, f"{selector} copies an image to QClipboard")
    actual = clipboard.image()
    assert actual.size() == expected.size()
    for x, y in [(0, 0), (45, 45), (145, 45)]:
        assert actual.pixelColor(x, y) == expected.pixelColor(x, y)
    await rig.wait_for(MENU, 0)


async def bounds(rig):
    return json.loads(await rig.js(f"JSON.stringify(document.querySelector('{VIEWER}').getBoundingClientRect())"))


def point(rig, x, y):
    factor = rig.window.view.zoomFactor()
    return QPoint(round(x * factor), round(y * factor))


async def wheel(rig, at, ticks):
    target = rig._target()
    event = QWheelEvent(QPointF(at), QPointF(target.mapToGlobal(at)), QPoint(), QPoint(0, ticks * 120),
                        Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier, Qt.ScrollPhase.NoScrollPhase, False)
    await outside_task(lambda: QApplication.sendEvent(target, event))


def test_chat_pictures_copy_and_the_viewer_zooms_pans_and_resets(qapp, loop, tmp_path):
    if qapp.platformName() != "offscreen":
        pytest.skip("use the offscreen clipboard to avoid overwriting the user's clipboard")
    clipboard = qapp.clipboard()
    evidence = Path(folder) if (folder := os.environ.get("WITTYCODE_TEST_EVIDENCE")) else None

    async def scenario():
        rig = await open_rig(tmp_path, size=(1280, 840))
        try:
            attachment = tmp_path / "attachment.png"
            attached = picture(attachment, 960, 600, "#315f54")
            inline = picture(rig.project / "result.png", 1040, 620, "#4b3875")
            model = await new_conversation(rig)
            model.next(say("Here is the local image:\n\n![Result](result.png)"), hold=False)
            assert await rig.drop_files([attachment]) == Qt.DropAction.CopyAction
            await natural_width(rig, ".att-row .att-thumb")
            await copy_picture(rig, clipboard, ".att-row .att-thumb", attached)
            await rig.send("Compare these pictures")
            await natural_width(rig, ATTACHMENT)
            await copy_picture(rig, clipboard, ATTACHMENT, attached)
            # Leave the sidebar's temporary project path out of both screenshots.
            await rig.press("b", Qt.KeyboardModifier.ControlModifier)
            await rig.wait_for(".sidebar", 0)
            await natural_width(rig, INLINE, 1040)
            await copy_picture(rig, clipboard, INLINE, inline,
                               evidence=evidence / "image-copy-menu.png" if evidence else None)

            await rig.click(ATTACHMENT)
            await natural_width(rig, VIEWER, 960)
            await copy_picture(rig, clipboard, VIEWER, attached)
            initial = await bounds(rig)
            x = initial["left"] + initial["width"] * 0.35
            y = initial["top"] + initial["height"] * 0.35
            at = point(rig, x, y)

            await wheel(rig, at, 1)
            await wait_for(lambda: rig.js(f"document.querySelector('{VIEWER}').getBoundingClientRect().width > {initial['width'] * 1.1}"),
                           5, "wheel up enlarges the image")
            zoomed = await bounds(rig)
            assert (x - zoomed["left"]) / zoomed["width"] == pytest.approx(0.35, abs=0.005)
            assert (y - zoomed["top"]) / zoomed["height"] == pytest.approx(0.35, abs=0.005)
            if evidence:
                await rig.shot(".lightbox", evidence / "image-viewer-zoom.png")

            await wheel(rig, at, -1)
            await wait_for(lambda: rig.js(f"document.querySelector('{VIEWER}').getBoundingClientRect().width < {zoomed['width'] * 0.95}"),
                           5, "wheel down makes it smaller")
            await wheel(rig, at, 1)
            await rig.wait_for(f"{VIEWER}[data-pannable]")
            target = rig._target()
            start = point(rig, x, y)
            end = point(rig, x + 65, y + 40)
            before_drag = await bounds(rig)
            await outside_task(lambda: QTest.mouseMove(target, start))
            await outside_task(lambda: QTest.mousePress(target, Qt.MouseButton.LeftButton, pos=start))
            await outside_task(lambda: QTest.mouseMove(target, end))
            await outside_task(lambda: QTest.mouseRelease(target, Qt.MouseButton.LeftButton, pos=end))
            await wait_for(lambda: rig.js(f"document.querySelector('{VIEWER}').getBoundingClientRect().left > {before_drag['left'] + 50}"),
                           5, "drag moves the zoomed image")
            moved = await bounds(rig)
            assert moved["top"] - before_drag["top"] == pytest.approx(40, abs=2)
            assert moved["width"] == pytest.approx(before_drag["width"], abs=1)

            await rig.press("0")
            assert await bounds(rig) == pytest.approx(initial, abs=1)
            await wheel(rig, at, 1)
            await rig.wait_for(f"{VIEWER}[data-pannable]")
            await outside_task(lambda: QTest.mouseMove(target, at))

            def double_click():
                # QTest spaces simulated clicks apart; timestamp the sequence as one double click.
                button = Qt.MouseButton.LeftButton
                now = time.monotonic_ns() // 1_000_000
                down = QEvent.Type.MouseButtonPress
                up = QEvent.Type.MouseButtonRelease
                for i, event_type in enumerate((down, up, QEvent.Type.MouseButtonDblClick, up)):
                    event = QMouseEvent(event_type, QPointF(at), QPointF(target.mapToGlobal(at)), button,
                                        Qt.MouseButton.NoButton if event_type == up else button,
                                        Qt.KeyboardModifier.NoModifier)
                    event.setTimestamp(now + i * 30)
                    QApplication.sendEvent(target, event)

            await outside_task(double_click)
            await rig.wait_for(f"{VIEWER}[data-pannable]", 0, timeout=5)
            assert await bounds(rig) == pytest.approx(initial, abs=1)
            await rig.press("Escape")
            await rig.wait_for(".lightbox", 0)

            await rig.click(INLINE)
            await natural_width(rig, VIEWER, 1040)
            blank = point(rig, 20, 20)
            await outside_task(lambda: QTest.mouseClick(target, Qt.MouseButton.LeftButton, pos=blank))
            await rig.wait_for(".lightbox", 0)
        finally:
            clipboard.clear()
            await rig.finish()

    run(loop, scenario())
