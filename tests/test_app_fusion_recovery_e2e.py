"""Use the normal desktop bundle and QWebChannel for the durable Fusion question."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from tests.e2e_support import open_rig
from tests.fake_responses import call, say
from tests.test_fusion_adaptive_rounds import _decision, _run
from tests.test_fusion_debate_runtime import _declare
from tests.test_fusion_identity_runtime import LIGHT, MAIN, WORKER, _gates, _manual_steps
from tests.test_fusion_route_runtime import UNIT_GATES, _report, _unit
from tests.ui_support import run, wait_for

pytestmark = pytest.mark.ui
TURN = '.transcript > .turn:last-child'


def test_desktop_recovery_question_keeps_body_and_only_notifies_after_acceptance(qapp, loop, tmp_path, monkeypatch):
    async def scenario():
        rig = await open_rig(tmp_path)
        try:
            sounds = []
            monkeypatch.setattr(rig.core, '_play_notification', sounds.append)
            settings = rig.core.settings
            settings.fusion_gates = _gates(*UNIT_GATES)
            settings.fusion_main, settings.fusion_workers, settings.fusion_light = MAIN, [WORKER], LIGHT
            settings.fusion_preference = 25
            result = await rig.core.call('session.new', {'projectId': str(rig.project), 'fusion': True, 'autonomy': 'full'})
            main = rig.core._by_cid[result['id']].controller
            _manual_steps(main, 1, 1)
            (rig.project / 'stock.txt').write_text('Four', encoding='utf-8')
            main.mode = 'spec'
            rig.plan.script(_declare('simple', 'plan'), call('submit_work_plan', {'data': {'units': [_unit(review_required=False)]}}),
                            call('submit_plan', {'plan': '核查库存。'}))
            assert main.send_prompt('核查库存。')
            await wait_for(lambda: not main.busy)
            plan, = main._fusion.registry.plans.values()
            case = plan['cases']['stock']
            sounds.clear()

            def request():
                yield from _decision('fixed', case['budgetReview'], 'continue', steps=2)

            rig.plan.script(_run('author'), call('read_file', {'file_path': 'stock.txt'}), request(), say('已读到库存值，尚未完成验收。'))
            assert main.approve_plan(main.saved.plan['id'])
            await rig.wait_text(TURN + ' .prompt.ask', '批准此次额度')
            await rig.wait_for(TURN + ' .prompt.ask input[type=text]', 0)
            await rig.wait_text(TURN + ' .prompt.ask', '取消原任务')
            await rig.wait_text(TURN + ' .turnfoot', '等待用户 · 未完成')
            await rig.wait_text(TURN + ' .text-block', '已读到库存值，尚未完成验收。')
            assert 'complete' not in sounds
            if evidence := os.environ.get('WITTYCODE_TEST_EVIDENCE'):
                await rig.shot(TURN, Path(evidence) / 'fusion-fixed-budget-waiting.png')

            rig.plan.script(call('submit_route', {}), _run('author-resume', resume=True), _report(evidence=['Stock value ready.']),
                            _run('runner'), _report(evidence=['Fixture passed.']), say('独立检查通过。'))
            await rig.click_text(TURN + ' .prompt.ask label', '批准此次额度')
            await rig.click_text(TURN + ' .prompt.ask button', '提交回答')
            await rig.wait_text(TURN + ' .text-block', '独立检查通过。')
            await rig.wait_for(TURN + ' > .user', 0)  # An automatic restart is not attributed to the user.
            await wait_for(lambda: sounds.count('complete') == 1)
            assert main.saved.turns[-1].fusion_outcome == 'verified'
            assert not main.transcript.pending_prompts()
            if evidence := os.environ.get('WITTYCODE_TEST_EVIDENCE'):
                await rig.shot(TURN, Path(evidence) / 'fusion-verified-after-approval.png')
        finally:
            await rig.finish()

    run(loop, scenario())
