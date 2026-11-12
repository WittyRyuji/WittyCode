"""The real Qt page aligns its reading grid and navigates actual turns without following new output away from history."""

from __future__ import annotations

import asyncio

import pytest
from PySide6.QtCore import Qt

from tests.e2e_support import open_rig
from tests.fake_responses import say
from tests.test_app_e2e import new_conversation
from tests.ui_support import run, wait_for

pytestmark = pytest.mark.ui


def test_turn_markers_jump_with_mouse_and_keyboard_and_do_not_follow_output_in_history(qapp, loop, tmp_path):
    async def scenario():
        rig = await open_rig(tmp_path, size=(1500, 960))
        try:
            model = await new_conversation(rig)
            assert not await rig.exists(".turn-nav"), "an empty session has no fabricated turn markers"
            for i in range(3):
                model.next(say((f"第 {i + 1} 轮：检查正文与输入框的对齐。\n\n" * 35)), hold=False)
                await rig.send(f"检查第 {i + 1} 项")
                await rig.wait_for(".main .transcript > .turn", i + 1)
                await rig.wait_for(".send.stop", 0)
            await rig.wait_for(".turn-marker", 3)
            aligned = "(() => { const t = document.querySelector('.transcript').getBoundingClientRect(), c = document.querySelector('.composer').getBoundingClientRect(); return Math.abs(t.left - c.left) < 1 && Math.abs(t.right - c.right) < 1; })()"
            assert await rig.js(aligned)
            await rig.click(".turn-marker", 0)

            async def at_first():
                return await rig.js("document.querySelector('.scroller').scrollTop < 2 && document.querySelector('.turn-marker').getAttribute('aria-current') === 'step'")

            await wait_for(at_first, 5, "the first turn reached by its marker")
            assert "检查第 1 项" in await rig.text(".turn-preview")
            await rig.press(Qt.Key.Key_Down)

            async def at_second():
                return await rig.js("(() => { const s = document.querySelector('.scroller').getBoundingClientRect(), t = document.querySelectorAll('.transcript > .turn')[1].getBoundingClientRect(); return Math.abs(t.top - s.top - 28) < 2 && document.querySelectorAll('.turn-marker')[1].getAttribute('aria-current') === 'step'; })()")

            await wait_for(at_second, 5, "the next turn reached by the arrow key")
            assert await rig.js("document.activeElement.classList.contains('turn-marker')"), "navigation keeps keyboard focus"
            await rig.press(Qt.Key.Key_Home)
            await wait_for(at_first, 5, "Home returns to the first turn")
            model.next(say("最后一轮的新增输出。\n\n" * 40))
            await rig.core.call("session.send", {"id": rig.core.order[0].cid, "text": "继续检查"})
            await rig.wait_for(".turn-marker", 4)
            await model.asked(4)
            model.go()
            await rig.wait_for(".send.stop", 0)
            await asyncio.sleep(0.2)
            assert await rig.js("document.querySelector('.scroller').scrollTop < 2"), "new output must not steal a historical reading position"
            assert await rig.exists(".jump")
            await rig.click(".jump")

            async def at_end():
                return await rig.js("(() => { const s = document.querySelector('.scroller'); return s.scrollHeight - s.scrollTop - s.clientHeight < 4 && document.querySelectorAll('.turn-marker')[3].getAttribute('aria-current') === 'step'; })()")

            await wait_for(at_end, 5, "jump to latest restores following and the latest marker")
            assert await rig.js("(() => { const s = document.querySelector('.scroller'), r = s.getBoundingClientRect(); return getComputedStyle(s).scrollbarWidth === 'auto' && getComputedStyle(s, '::-webkit-scrollbar').width === '8px' && getComputedStyle(s, '::-webkit-scrollbar-button').display === 'none' && document.elementFromPoint(r.right - 4, r.bottom - 20).classList.contains('scroller'); })()"), "the styled scrollbar is reachable, outside the window resize handles"
            rig.window.resize(900, 960)
            await asyncio.sleep(0.2)
            assert await rig.js(aligned), "the same grid survives a narrow real window"
            await new_conversation(rig, tab_count=2)
            assert not await rig.exists(".turn-nav")
            assert await rig.count(".main .transcript") == 1
            await rig.click(".tab", 0)
            await rig.wait_for(".turn-marker", 4)
            assert await rig.count(".main .transcript") == 1
        finally:
            await rig.finish()

    run(loop, scenario())
