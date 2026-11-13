"""Real Qt input: compact stationary markers, per-session reading state, and one row across engine changes."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from PySide6.QtCore import Qt

from tests.devin_support import make_engine
from tests.e2e_support import open_rig
from tests.fake_responses import say
from tests.test_app_e2e import devin_listed, new_conversation
from tests.ui_support import run, wait_for

pytestmark = pytest.mark.ui

AT_END = "(() => { const e = document.querySelector('.scroller'); return e.scrollHeight - e.clientHeight - e.scrollTop < 3; })()"


@pytest.mark.parametrize("theme,width", [("light", 1500), ("dark", 900)])
def test_typing_and_switching_sessions_keep_the_outline_and_each_reading_position(qapp, loop, tmp_path, theme, width):
    async def scenario():
        rig = await open_rig(tmp_path, size=(width, 960))
        try:
            await rig.core.call("settings.set", {"key": "theme", "value": theme})
            model = await new_conversation(rig)
            for i in range(3):
                model.next(say("检查输入校验和事务边界。\n\n" * 35), hold=False)
                await rig.send(f"检查第 {i + 1} 项")
                await rig.wait_for(".send.stop", 0)
                await rig.wait_for(".turn-marker", i + 1)
            await wait_for(lambda: rig.js(AT_END), 5, "latest messages")
            top = await rig.js("document.querySelector('.turn-nav').getBoundingClientRect().top")
            assert await rig.js("[...document.querySelectorAll('.turn-marker')].every(e => e.getBoundingClientRect().height <= 12)")
            await rig.focus_composer()
            for _ in range(7):
                await rig.type("输入多行草稿")
                await rig.press(Qt.Key.Key_Return, Qt.KeyboardModifier.ShiftModifier)
                assert abs(await rig.js("document.querySelector('.turn-nav').getBoundingClientRect().top") - top) < 1
            await wait_for(lambda: rig.js(AT_END), 5, "typing keeps latest-follow mode")
            await new_conversation(rig, tab_count=2)
            await rig.click(".tab", 0)
            await rig.wait_for(".turn-marker", 3)
            assert await rig.js(AT_END)
            await rig.click(".turn-marker", 1)
            await wait_for(lambda: rig.js("document.querySelectorAll('.turn-marker')[1].getAttribute('aria-current') === 'step' && !!document.querySelector('.jump')"), 5, "the historical turn")
            await asyncio.sleep(0.6)
            historical = await rig.js("document.querySelector('.scroller').scrollTop")
            await rig.click(".tab", 1)
            await rig.click(".tab", 0)
            await rig.wait_for(".turn-marker", 3)
            assert abs(await rig.js("document.querySelector('.scroller').scrollTop") - historical) < 2
            assert await rig.js("document.querySelectorAll('.turn-marker')[1].getAttribute('aria-current') === 'step'")
            await rig.click(".jump")
            await rig.click(".tab", 1)
            await rig.click(".tab", 0)
            await wait_for(lambda: rig.js(AT_END), 5, "a switch during smooth scrolling keeps the latest intent")
            screenshot = Path(__file__).resolve().parents[1] / "sandbox" / f"session-navigation-qt-{theme}.png"
            screenshot.parent.mkdir(parents=True, exist_ok=True)
            assert rig.window.grab().save(str(screenshot))
            await rig.press(",", Qt.KeyboardModifier.ControlModifier)
            await rig.wait_for(".settings")
            assert (await rig.texts(".snav"))[-1] == "关于"
        finally:
            await rig.finish()

    run(loop, scenario())


def test_real_model_menu_keeps_one_sidebar_row_after_switch_and_close(qapp, loop, tmp_path):
    async def scenario():
        rig = await open_rig(tmp_path, devin_engine=make_engine(tmp_path).engine)
        try:
            await devin_listed(rig)
            model = await new_conversation(rig)
            model.next(say("先检查输入映射。"), hold=False)
            await rig.send("检查库存导入与重复行")
            await rig.wait_for(".send.stop", 0)
            await rig.wait_text(".tab", "会话主题摘要")
            await rig.click(".controls .chip", 0)
            await rig.wait_for(".menu-item")
            await rig.click_text(".menu-item", "SWE-2")
            await rig.wait_for(".block.engine-switch", 1, timeout=15)
            assert await rig.count(".tab") == 1
            await rig.core.call("session.close", {"id": rig.core.order[0].cid})
            await rig.core.call("project.refresh")
            await rig.wait_for(".tab", 0)
            await rig.wait_for(".srow", 1)
            await rig.click(".srow")
            await rig.wait_for(".tab", 1)
            await rig.wait_text(".user .body", "检查库存导入与重复行")
            assert await rig.count(".srow") == 1
        finally:
            await rig.finish()

    run(loop, scenario())
