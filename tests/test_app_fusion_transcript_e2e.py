"""Fusion main transcript placement in the real Qt page, driven by fake Responses."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from desktop.core.transcript import ToolStatus
from tests import e2e_support
from tests.e2e_support import open_rig
from tests.fake_responses import calls, say
from tests.test_app_fusion_e2e import build_page
from tests.test_fusion_debate_runtime import _declare
from tests.test_fusion_identity_runtime import LIGHT, MAIN, WORKER, _gates
from tests.ui_support import run, wait_for

pytestmark = pytest.mark.ui
TURN = '.transcript > .turn:last-child'
GROUP = TURN + ' > .activity'
OPENING = '我先核对库存表和说明的记录。'
FINAL = '已核对，库存表和说明都记录了四件。'


@pytest.fixture(scope='module')
def page(tmp_path_factory):
    return build_page(tmp_path_factory.mktemp('fusion-page'))


@pytest.mark.parametrize('kind', ['simple', 'complex'])
def test_fusion_activity_stays_above_opening_and_answer(qapp, loop, tmp_path, monkeypatch, page, kind):
    monkeypatch.setattr(e2e_support, 'PAGE_FILE', page)

    async def scenario():
        rig = await open_rig(tmp_path)
        try:
            settings = rig.core.settings
            settings.fusion_gates = _gates('v3')
            settings.fusion_main, settings.fusion_workers, settings.fusion_light = MAIN, [WORKER], LIGHT
            settings.fusion_preference = 25  # No debate calls; both kinds still travel through declare_topic.
            result = await rig.core.call('session.new', {'projectId': str(rig.project), 'fusion': True, 'autonomy': 'full'})
            main = rig.core._by_cid[result['id']].controller
            (rig.project / 'stock.txt').write_text('4', encoding='utf-8')
            batch = calls([('read_file', json.dumps({'file_path': 'stock.txt'}), f'read_{i}', f'item_{i}') for i in range(34)])
            rig.plan.script(_declare(kind, 'discussion', '核对库存', next_step=OPENING),
                            [('hold', 'reads'), *batch], [('hold', 'answer'), *say(FINAL)])
            assert main.send_prompt('请核对库存表和说明。')
            await rig.wait_text(TURN + ' .fusion-opening', OPENING)
            await rig.wait_for(TURN + ' .text-block', 0)
            await rig.wait_for(TURN + ' .fusion-dispatch', 0)
            rig.plan.release('reads')
            await rig.wait_text(GROUP + ' .gtext', '35 步')
            await rig.wait_for(GROUP, 1)
            await rig.wait_for(GROUP + ' .gsum[aria-expanded="false"]', 1)
            await rig.wait_for(GROUP + ' .gbody', 0)
            await rig.wait_for(TURN + ' .text-block', 0)

            async def ordered(*selectors):
                rects = await rig.js('JSON.stringify(' + json.dumps(selectors) +
                                     '.map(s => document.querySelector(s)?.getBoundingClientRect().top))')
                ys = json.loads(rects)
                assert None not in ys and all(a < b for a, b in zip(ys, ys[1:])), ys

            await ordered(TURN + ' > .user', GROUP, TURN + ' > .fusion-feedback')
            if kind == 'simple' and (evidence := os.environ.get('WITTYCODE_TEST_EVIDENCE')):
                await rig.shot(TURN, Path(evidence) / 'fusion-opening-35-steps.png')

            await rig.click(GROUP + ' .gsum')
            await rig.wait_for(GROUP + ' .irow', 35)
            assert await rig.js(f'document.querySelector({json.dumps(GROUP)}).querySelector(".gsum").getAttribute("aria-expanded")') == 'true'
            rig.plan.release('answer')
            await rig.wait_text(TURN + ' .text-block', FINAL)
            await wait_for(lambda: not main.busy)
            # A delayed activity notification must not create a second bar or move the published answer.
            turn = main.transcript.turns[-1]
            item = main.transcript.ensure_tool(turn, 'late', 'Read')
            main.transcript.finish_tool(turn, item, '4', status=ToolStatus.DONE)
            main._changed()
            await rig.wait_text(GROUP + ' .gtext', '36 步')
            await rig.wait_for(GROUP, 1)
            await rig.wait_for(GROUP + ' .gsum[aria-expanded="true"]', 1)
            await ordered(TURN + ' > .user', GROUP, TURN + ' > .fusion-feedback', TURN + ' > .text-block')

            await rig.core.call('session.close', {'id': result['id']})
            saved_row = f'.srow:not(.open)[data-session-id="cg:{main.session_id}"]'
            await rig.wait_for(saved_row, 1)
            await rig.click(saved_row)
            await rig.wait_text(TURN + ' .text-block', FINAL)
            await rig.wait_text(GROUP + ' .gtext', '35 步')
            await ordered(TURN + ' > .user', GROUP, TURN + ' > .fusion-feedback', TURN + ' > .text-block')

            rig.plan.script(say('你好。'))
            await rig.send('你好！')
            await rig.wait_text(TURN + ' .text-block', '你好。')
            await rig.wait_for(TURN + ' .fusion-feedback', 0)
            await rig.wait_for(GROUP, 0)
        finally:
            rig.plan.release('reads')
            rig.plan.release('answer')
            await rig.finish()

    run(loop, scenario())
