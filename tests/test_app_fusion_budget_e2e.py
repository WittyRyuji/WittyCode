from __future__ import annotations

import os
from pathlib import Path

import pytest

from tests import e2e_support
from tests.e2e_support import open_rig
from tests.fake_responses import call, say
from tests.test_app_fusion_e2e import build_page
from tests.test_fusion_debate_runtime import _declare
from tests.test_fusion_identity_runtime import LIGHT, MAIN, WORKER, _gates
from tests.test_fusion_route_runtime import UNIT_GATES, _report, _unit
from tests.ui_support import run

pytestmark = pytest.mark.ui


def test_budget_pause_and_dependency_render_in_real_qt(qapp, loop, tmp_path, monkeypatch):
    monkeypatch.setattr(e2e_support, 'PAGE_FILE', build_page(tmp_path))

    async def scenario():
        rig = await open_rig(tmp_path)
        try:
            settings = rig.core.settings
            settings.fusion_gates = _gates(*UNIT_GATES)
            settings.fusion_main, settings.fusion_workers, settings.fusion_light = MAIN, [WORKER], LIGHT
            settings.fusion_preference = 25
            settings.fusion_steps_min_adaptive, settings.fusion_steps_min = False, 1
            result = await rig.core.call('session.new', {'projectId': str(rig.project), 'fusion': True, 'autonomy': 'full'})
            main = rig.core._by_cid[result['id']].controller
            assert main is not None

            def pause():
                plan, = main._fusion.registry.plans.values()
                review = plan['cases']['stock']['budgetReview']['id']
                yield from call('delegate_task', {'unit': 'stock', 'budget_decision': {'review': review, 'action': 'pause'}})

            rig.plan.script(_declare('complex', 'execute'), call('submit_work_plan', {'data': {'units': [
                                _unit(review_required=False), _unit('notes', paths=['notes.txt'], deps=['stock'], review_required=False)]}}),
                            call('submit_route', {}), call('delegate_task', {'unit': 'stock'}), _report(evidence=['Count inspected.']),
                            call('delegate_task', {'unit': 'stock'}), _report('concerns', evidence=['Fixture reports three.']),
                            pause(), say('Paused for review.'))
            assert main.send_prompt('核对库存，再更新说明。')
            await rig.wait_text('.block.notice', '本轮工作未验证')
            note = [text for text in await rig.texts('.block.notice') if '本轮工作未验证' in text]
            assert len(note) == 1
            assert 'stock v1：本轮额度用尽（步数），Main 已决定暂停' in note[0]
            assert '已用/上限：作者交付 步数 1/1、Runner 检查 步数 1/1' in note[0]
            assert 'notes v0：依赖阻塞，等待 stock 验证通过。' in note[0]
            if evidence := os.environ.get('WITTYCODE_TEST_EVIDENCE'):
                await rig.js("[...document.querySelectorAll('.block.notice')].find(e => "
                             "e.textContent.includes('本轮工作未验证')).setAttribute('data-budget-verdict', '')")
                await rig.js("document.querySelector('[data-budget-verdict]').scrollIntoView({block:'center'})")
                await rig.shot('[data-budget-verdict]', Path(evidence) / 'fusion-adaptive-budget-qt.png')
        finally:
            await rig.finish()

    run(loop, scenario())
