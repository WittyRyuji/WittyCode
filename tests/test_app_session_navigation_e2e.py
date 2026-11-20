"""Real Qt input: compact stationary markers, per-session reading state, and one row across engine changes."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest
from PySide6.QtCore import Qt

from tests.devin_support import make_engine
from tests.e2e_support import open_rig
from tests.fake_responses import fail, say
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
            centered = "(() => { const n=document.querySelector('.turn-nav').getBoundingClientRect(), s=document.querySelector('.stage').getBoundingClientRect(); return Math.abs(n.top+n.height/2-s.top-s.height/2)<1; })()"
            assert await rig.js(centered)
            assert await rig.js("[...document.querySelectorAll('.turn-marker')].every(e => e.getBoundingClientRect().height <= 6 && parseFloat(getComputedStyle(e.firstElementChild).height) === (e.getAttribute('aria-current') === 'step' ? 3 : 1.5))")
            await rig.focus_composer()
            for _ in range(7):
                await rig.type("输入多行草稿")
                await rig.press(Qt.Key.Key_Return, Qt.KeyboardModifier.ShiftModifier)
                assert await rig.js(centered), "the outline stays vertically centered as the input grows"
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
            assert rig.window.grab().save(str(tmp_path / f"session-navigation-qt-{theme}.png"))
            await rig.press(",", Qt.KeyboardModifier.ControlModifier)
            await rig.wait_for(".settings")
            assert (await rig.texts(".snav"))[-1] == "关于"
        finally:
            await rig.finish()

    run(loop, scenario())


def test_sidebar_pin_menus_survive_opening_refresh_reload_and_archive(qapp, loop, tmp_path):
    async def scenario():
        rig = await open_rig(tmp_path, saved=[("new", "Newer", 5), ("mid", "Middle", 20), ("old", "Older", 60)])
        try:
            await rig.wait_for(".srow", 3)
            old = '.srow[data-session-id="cg:old"]'
            new = '.srow[data-session-id="cg:new"]'
            await rig.click(old, right=True)
            await rig.wait_for(".menu-item")
            await rig.click_text(".menu-item", "置顶")
            await rig.wait_for(old + " .srow-pin")
            assert await rig.texts(".slist .stitle") == ["Older", "Newer", "Middle"]
            await rig.click(new + " .more")
            await rig.wait_for(".menu-item")
            await rig.click_text(".menu-item", "置顶")
            await rig.wait_for(".srow-pin", 2)
            assert await rig.texts(".slist .stitle") == ["Newer", "Older", "Middle"]

            await rig.click(old)
            await rig.wait_for(".tab.current")
            await rig.wait_for(".srow.open .srow-pin")
            assert await rig.texts(".slist .stitle") == ["Newer", "Older", "Middle"]
            await rig.click('.side-head-actions [aria-label="刷新会话列表"]')
            await rig.js("window.__pinTestOldPage = true")
            # The page rejects its own navigation; load a new document through the host.
            rig.window._load_seen = False
            rig.window.load()
            await wait_for(lambda: rig.js("!window.__pinTestOldPage && !!document.querySelector('.srow.open .srow-pin')"), message="the pin on the reloaded page")
            assert await rig.texts(".slist .stitle") == ["Newer", "Older", "Middle"]
            if evidence := os.environ.get("WITTYCODE_TEST_EVIDENCE"):
                await rig.js("document.querySelector('.ppath').textContent = 'C:\\\\Projects\\\\demo'")
                await asyncio.sleep(0.05)
                assert rig.window.grab().save(str(Path(evidence) / "session-pins-qt.png"))

            await rig.click(".srow.open", right=True)
            await rig.wait_for(".menu-item")
            await rig.click_text(".menu-item", "归档")
            await rig.wait_for(".tab", 0)
            await rig.wait_for(".srow", 2)
            assert await rig.texts(".slist .stitle") == ["Newer", "Middle"]
            await rig.click(".archived .proj-toggle")
            await rig.wait_text(".archived-row", "Older")
            assert not await rig.count(".archived-row .srow-pin")
            await rig.click(".archived-row .more")
            await rig.wait_for(old + " .srow-pin")
            await rig.click(new + " .more")
            await rig.wait_for(".menu-item")
            await rig.click_text(".menu-item", "取消置顶")
            await rig.wait_for(".srow-pin", 1)
            assert await rig.texts(".slist .stitle") == ["Older", "Newer", "Middle"]
        finally:
            await rig.finish()

    run(loop, scenario())


def test_draft_and_rejected_first_send_stay_out_of_the_project_sidebar(qapp, loop, tmp_path):
    async def scenario():
        rig = await open_rig(tmp_path)
        try:
            await rig.click(".newbtn")
            await rig.focus_composer()
            await rig.type("landing draft")
            assert not rig.core.order, "a draft in the landing composer does not open an engine session"
            await rig.wait_for(".srow", 0)
            await rig.press("A", Qt.KeyboardModifier.ControlModifier)
            await rig.press(Qt.Key.Key_Backspace)

            await new_conversation(rig)
            await rig.focus_composer()
            await rig.type("draft only")
            assert await rig.js("document.querySelector('.input').value") == "draft only"
            await rig.wait_for(".srow", 0)
            await rig.core.call("session.close", {"id": rig.core.order[0].cid})
            await rig.wait_for(".tab", 0)
            await rig.wait_for(".srow", 0)

            model = await new_conversation(rig)
            model.next(fail("subscription_sharing_usage_limit_exceeded"), hold=False)
            await rig.send("first attempt")
            await wait_for(lambda: rig.js("document.querySelector('.input').value === 'first attempt'"), 5, "draft restored")
            await rig.wait_for(".srow", 0)
            await rig.focus_composer()
            await rig.press(Qt.Key.Key_End)
            await rig.type(" edited")
            assert await rig.js("document.querySelector('.input').value") == "first attempt edited"
            model.next(say("answer accepted"), hold=False)
            await rig.press(Qt.Key.Key_Return)
            await rig.wait_text(".transcript", "first attempt edited")
            await rig.wait_text(".transcript", "answer accepted")
            await rig.wait_for(".srow", 1)
            await rig.core.call("session.close", {"id": rig.core.order[0].cid})
            await rig.wait_for(".tab", 0)
            await rig.wait_for(".srow", 1)
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
            await rig.wait_text(".chip-model .chip-v", "SWE-2")
            assert await rig.count(".block.engine-switch") == 0
            await rig.send("Continue in the same conversation")
            await rig.wait_for(".block.engine-switch", 1, timeout=15)
            await rig.wait_text(".transcript", "echo:")
            await rig.wait_for(".send.stop", 0)
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
