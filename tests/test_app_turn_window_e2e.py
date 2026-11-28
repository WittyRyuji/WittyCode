"""Actual Qt/WebChannel regression for local history pages and live code nodes."""
import os
from pathlib import Path

import pytest

from desktop.core.chatgpt.controller import user_item
from desktop.core.harness.store import SavedTurn
from desktop.core.session_state import SessionState
from tests.e2e_support import open_rig
from tests.ui_support import run, wait_for

pytestmark = pytest.mark.ui


def test_history_first_page_prepend_anchor_and_streaming_code_identity(qapp, loop, tmp_path):
    async def scenario():
        rig = await open_rig(tmp_path, saved=[('demo', '分页示例', 5)])
        try:
            stored = rig.engine.sessions.load('demo')
            stored.items.clear()
            stored.turns.clear()
            for i in range(1, 13):
                stored.turns.append(SavedTurn(start=len(stored.items), at=1, finished=2))
                stored.items.append(user_item(f'历史第 {i:02} 轮', ()))
                stored.items.append({'type': 'message', 'role': 'assistant', 'content': [
                    {'type': 'output_text', 'text': f'### 已记录第 {i:02} 轮\n\n' + '这段合成记录用于验证本地历史向上翻页和阅读位置。\n\n' * 5},
                ]})
            rig.engine.sessions.save(stored)
            opened = await rig.core.call('session.openSaved', {'id': 'cg:demo', 'projectId': str(rig.project)})
            conv = rig.core._by_cid[opened['id']]
            assert conv.controller.state is SessionState.READY
            await rig.wait_for('.transcript > .turn', 5)
            assert await rig.texts('.transcript > .turn .body') == [f'历史第 {i:02} 轮' for i in range(8, 13)]
            assert len(conv.controller.transcript.turns) == 12, 'visual paging does not truncate the engine history'
            await rig.wait_for('.history-more button', 1)
            # Let a scroller-specific up gesture load exactly one page, preserving the old first turn as the anchor.
            await rig.js("""(() => {
                const el = document.querySelector('.scroller');
                el.scrollTop = 0;
                window.readingTurn = document.querySelector('.transcript > .turn');
                window.readingTop = window.readingTurn.getBoundingClientRect().top;
                el.dispatchEvent(new WheelEvent('wheel', { bubbles: true, deltaY: -40 }));
            })()""")
            await rig.wait_for('.transcript > .turn', 10)
            assert await rig.texts('.transcript > .turn .body') == [f'历史第 {i:02} 轮' for i in range(3, 13)]
            assert await rig.js('Math.abs(window.readingTurn.getBoundingClientRect().top - window.readingTop) < 3')
            await rig.click('.history-more button')
            await rig.wait_for('.transcript > .turn', 12)
            await rig.wait_for('.history-more button', 0)
            assert await rig.text('.transcript > .turn .body') == '历史第 01 轮'

            transcript = conv.controller.transcript
            turn = transcript.start_turn('代码增量示例')
            markdown = '```python\ncount = 1\n```\n\n'
            transcript.append_text(turn, 'demo', 0, markdown)
            rig.core._flush(conv)
            await rig.wait_for('.codeblock code', 1)
            await rig.js("window.codeNode = document.querySelector('.codeblock code')")
            transcript.append_text(turn, 'demo', 0, '追加新字🙂')
            rig.core._flush(conv)
            await rig.wait_text('.text-block .text', '追加新字🙂')
            assert await rig.js("window.codeNode === document.querySelector('.codeblock code')")
            transcript.end_text(turn, 'demo', 0)
            rig.core._flush(conv)
            await wait_for(lambda: rig.exists('.text-block .text:has(.codeblock):not(.streaming)'))
            assert await rig.js("window.codeNode === document.querySelector('.codeblock code')")

            if os.environ.get('WITTYCODE_HISTORY_EVIDENCE') and (evidence := os.environ.get('WITTYCODE_TEST_EVIDENCE')):
                await rig.js("document.querySelector('.scroller').scrollTop = 0")
                await rig.shot('.stage', Path(evidence) / 'history-window.png')
        finally:
            await rig.finish()

    run(loop, scenario())
