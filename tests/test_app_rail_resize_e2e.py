"""Real Qt pointer/keyboard resizing and saved width for the plan/Agents Team rail."""
from __future__ import annotations

import asyncio

import pytest
from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QTest

from tests.e2e_support import open_rig
from tests.fake_responses import call, say
from tests.fakes import until
from tests.ui_support import outside_task, run

pytestmark = pytest.mark.ui


@pytest.mark.parametrize("size", [(1500, 960), (900, 800)])
def test_right_rail_drag_keyboard_cancel_and_reload(qapp, loop, tmp_path, size):
    async def scenario():
        rig = await open_rig(tmp_path, size=size)
        try:
            rig.plan.script(call("todo_write", {"todos": [{"content": "Implement login flow", "status": "in_progress"},
                                                         {"content": "Verify output", "status": "pending"}]}), say("Work plan ready."))
            await rig.send("Prepare the work")
            await rig.wait_for(".conversation-splitter")
            await rig.wait_text(".transcript", "Work plan ready.")
            target = rig._target()
            at = await rig._center(".conversation-splitter", 0)
            moved = at - QPoint(round(100 * rig.window.view.zoomFactor()), 0)
            await outside_task(lambda: QTest.mousePress(target, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, at))
            await asyncio.sleep(0.05)
            await outside_task(lambda: QTest.mouseMove(target, moved))
            await asyncio.sleep(0.1)
            assert rig.core.settings.conversation_width == 280, "do not write settings on each pointer move"
            assert await rig.js("Number(document.querySelector('.conversation-splitter').getAttribute('aria-valuenow'))") == 380
            await outside_task(lambda: QTest.mouseRelease(target, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, moved))
            await until(lambda: rig.core.settings.conversation_width == 380)
            assert not await rig.js("document.body.classList.contains('resizing')")
            assert await rig.js("document.documentElement.scrollWidth <= innerWidth")
            assert await rig.js("document.querySelector('.conversation-column').getBoundingClientRect().width >= 400")
            rig.window.view.reload()
            await rig.wait_for(".boot", 0)
            await rig.wait_for(".conversation-splitter")
            assert await rig.js("Number(document.querySelector('.conversation-splitter').getAttribute('aria-valuenow'))") == 380
            await rig.click(".conversation-splitter")
            target = rig._target()
            await outside_task(lambda: QTest.keyClick(target, Qt.Key.Key_Home))
            await until(lambda: rig.core.settings.conversation_width == 240)
            await outside_task(lambda: QTest.keyClick(target, Qt.Key.Key_Left))
            await until(lambda: rig.core.settings.conversation_width == 256)
            await outside_task(lambda: QTest.keyClick(target, Qt.Key.Key_End))
            limit = await rig.js("Number(document.querySelector('.conversation-splitter').getAttribute('aria-valuemax'))")
            await until(lambda: rig.core.settings.conversation_width == limit)
            at = await rig._center(".conversation-splitter", 0)
            moved = at + QPoint(80, 0)
            await outside_task(lambda: QTest.mousePress(target, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, at))
            await outside_task(lambda: QTest.mouseMove(target, moved))
            await asyncio.sleep(0.1)
            await rig.press("Escape")
            await outside_task(lambda: QTest.mouseRelease(target, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, moved))
            assert rig.core.settings.conversation_width == limit
            assert not await rig.js("document.body.classList.contains('resizing')")
            assert rig.window.grab().save(str(tmp_path / f"resizable-rail-{size[0]}.png"))
        finally:
            await rig.finish()
    run(loop, scenario())
