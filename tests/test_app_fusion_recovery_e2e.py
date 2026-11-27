"""Use the normal desktop bundle and QWebChannel for the durable Fusion question."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from tests.e2e_support import open_rig
from tests.fake_responses import call, fail, say
from tests.test_fusion_adaptive_rounds import _decision, _run
from tests.test_fusion_debate_runtime import _declare
from tests.test_fusion_identity_runtime import LIGHT, MAIN, WORKER, _gates, _manual_steps
from tests.test_fusion_route_runtime import UNIT_GATES, _report, _unit
from tests.ui_support import run, wait_for

pytestmark = pytest.mark.ui
TURN = '.transcript > .turn:last-child'


@pytest.mark.parametrize('resume_all', [False, True])
def test_desktop_resume_selects_the_original_task_before_requesting_a_model(qapp, loop, tmp_path, resume_all):
    async def scenario():
        rig = await open_rig(tmp_path)
        try:
            settings = rig.core.settings
            settings.fusion_gates = _gates(*UNIT_GATES)
            settings.fusion_main, settings.fusion_workers, settings.fusion_light = MAIN, [WORKER], LIGHT
            settings.fusion_preference = 25
            result = await rig.core.call('session.new', {'projectId': str(rig.project), 'fusion': True, 'autonomy': 'full'})
            conv = rig.core._by_cid[result['id']]
            main = conv.controller
            await wait_for(lambda: not main.busy)
            for name, goal in [('stock', '核对库存'), ('orders', '核对订单')]:
                unit = _unit(name, paths=[name + '.txt'], review_required=False)
                rig.plan.script(_declare('complex', 'execute', call_id=f'declare-{name}'),
                                call('submit_work_plan', {'data': {'units': [unit]}}), call('submit_route', {}),
                                say('工作尚未完成。'), say('等待下一步。'))
                assert main.send_prompt(goal)
                await rig.wait_text(TURN + ' .text-block', '等待下一步。')
                await wait_for(lambda: not main.busy)
            key, = [k for k, p in main._fusion.registry.plans.items() if 'stock' in p['cases']]
            before = len(rig.plan.requests)
            await rig.send('继续')
            await rig.wait_text(TURN + ' .prompt.ask', '请选择要继续的原任务')
            await rig.wait_text(TURN + ' .prompt.ask', '取消续作')
            assert '取消原任务' not in await rig.text(TURN + ' .prompt.ask')
            assert len(rig.plan.requests) == before and conv.inflight is None and not main.busy
            await rig.wait_text(TURN + ' .prompt.ask', '全部续作')
            if resume_all and (evidence := os.environ.get('WITTYCODE_TEST_EVIDENCE')):
                await rig.shot(TURN + ' .prompt.ask', Path(evidence) / 'fusion-resume-all.png')
            await rig.click_text(TURN + ' .prompt.ask button', '取消续作')
            await rig.wait_for(TURN + ' .prompt.ask', 0)
            assert len(rig.plan.requests) == before and not any(p.get('cancelled') for p in main._fusion.registry.plans.values())
            await rig.send('继续')
            await rig.wait_text(TURN + ' .prompt.ask', '请选择要继续的原任务')
            if resume_all:
                rig.plan.script(_run('chosen-orders', 'orders'), _report(evidence=['Orders ready.']),
                                _run('checked-orders', 'orders'), _report(evidence=['Orders verified.']), say('订单已通过。'))
            rig.plan.script(_run('chosen'), _report(evidence=['Stock ready.']), _run('checked'),
                            _report(evidence=['Stock verified.']), say('原工单已通过。'))
            await rig.click_text(TURN + ' .prompt.ask label', '全部续作' if resume_all else '核对库存')
            await rig.click_text(TURN + ' .prompt.ask button', '提交回答')
            await rig.wait_text(TURN + ' .text-block', '原工单已通过。')
            assert main.saved.turns[-1].fusion_plan == key and main.saved.turns[-1].fusion_resume_source == 'manual'
            if resume_all:
                orders_key, = [k for k, p in main._fusion.registry.plans.items() if 'orders' in p['cases']]
                assert main.saved.turns[-2].fusion_plan == orders_key
                assert main._fusion.registry.plans[orders_key]['cases']['orders']['next'] == 'done'
            await rig.wait_text(TURN + ' > .user', '继续')
        finally:
            await rig.finish()

    run(loop, scenario())


def test_returning_a_queued_resume_does_not_continue_a_fusion_task(qapp, loop, tmp_path):
    async def scenario():
        rig = await open_rig(tmp_path)
        try:
            settings = rig.core.settings
            settings.fusion_gates = _gates(*UNIT_GATES)
            settings.fusion_main, settings.fusion_workers, settings.fusion_light = MAIN, [WORKER], LIGHT
            settings.fusion_preference = 25
            result = await rig.core.call('session.new', {'projectId': str(rig.project), 'fusion': True})
            main = rig.core._by_cid[result['id']].controller
            await wait_for(lambda: not main.busy)
            rig.plan.script([('hold', 'first-failure'), *fail('invalid_request_error', 'test failure')])
            assert main.send_prompt('Check the inventory.')
            await wait_for(lambda: bool(rig.plan.requests))
            await rig.send('继续')
            await rig.wait_for('.qs-row', 1)
            before = len(rig.plan.requests)
            rig.plan.release('first-failure')
            await rig.wait_for('.qs-row', 0)
            assert await rig.js("document.querySelector('.input').value") == '继续'
            assert not main.fusion_resume_waiting and len(rig.plan.requests) == before
        finally:
            rig.plan.release('first-failure')
            await rig.finish()

    run(loop, scenario())


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


def test_desktop_tool_permission_shows_exact_diff_and_can_resume(qapp, loop, tmp_path):
    async def scenario():
        rig = await open_rig(tmp_path)
        try:
            settings = rig.core.settings
            settings.fusion_gates = _gates(*UNIT_GATES)
            settings.fusion_main, settings.fusion_workers, settings.fusion_light = MAIN, [WORKER], LIGHT
            settings.fusion_preference = 25
            result = await rig.core.call('session.new', {'projectId': str(rig.project), 'fusion': True, 'settings': {'autonomy': 'ask'}})
            main = rig.core._by_cid[result['id']].controller
            _manual_steps(main, 8, 8)
            main.mode = 'spec'
            rig.plan.script(_declare('simple', 'plan'), call('submit_work_plan', {'data': {'units': [_unit(review_required=False)]}}),
                            call('submit_plan', {'plan': '核查库存。'}))
            assert main.send_prompt('核查库存。')
            await wait_for(lambda: not main.busy)
            args = {'file_path': 'stock.txt', 'content': 'Inventory count: four'}
            rig.plan.script(_run('author'), call('create_file', args), say('尚未写入，等待授权。'))
            assert main.approve_plan(main.saved.plan['id'])
            await rig.wait_text(TURN + ' .prompt.perm', 'Fusion 续作需要你授权')
            await rig.wait_text(TURN + ' .prompt.perm', 'Inventory count: four')
            await rig.wait_text(TURN + ' .turnfoot', '等待用户 · 未完成')
            assert not (rig.project / 'stock.txt').exists()
            assert '本会话内' not in await rig.text(TURN + ' .prompt.perm')
            if evidence := os.environ.get('WITTYCODE_TEST_EVIDENCE'):
                await rig.shot(TURN, Path(evidence) / 'fusion-tool-permission.png')

            rig.plan.script(call('submit_route', {}), _run('author-resume', resume=True), call('create_file', args),
                            _report(evidence=['Stock value ready.']), _run('runner'), _report(evidence=['Fixture passed.']),
                            say('检查完成。'))
            await rig.click_text(TURN + ' .prompt.perm button', '允许（仅此一次）')
            await rig.wait_text(TURN + ' .text-block', '检查完成。')
            assert (rig.project / 'stock.txt').read_text(encoding='utf-8') == args['content']
            assert main.saved.turns[-1].fusion_outcome == 'verified'
        finally:
            await rig.finish()

    run(loop, scenario())
