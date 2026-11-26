from __future__ import annotations

import asyncio
import re
from dataclasses import replace

import pytest

from desktop.core.fusion import verification_case as ledger
from desktop.core.fusion.registry import FusionRegistry
from tests.chatgpt_support import finish_turn
from tests.fake_responses import call, fail, say
from tests.test_fusion_debate_runtime import _declare
from tests.test_fusion_identity_runtime import _fusion, _manual_steps, _output, _turn
from tests.test_fusion_route_runtime import UNIT_GATES, _report, _unit


def _run(ident, unit='stock', **kwargs):
    return call('delegate_task', {'unit': unit, **kwargs}, call_id=ident)


def _decision(ident, review, action, **kwargs):
    return _run(ident, budget_decision={'review': review['id'], 'action': action, **kwargs})


def _one_step_adaptive(main):
    _manual_steps(main, 1, 5)
    main.manager.settings.fusion_steps_max_adaptive = True


async def _plan(main, fake, *units, budget=None):
    main.mode = 'spec'
    data = {'units': list(units)}
    if budget:
        data['budget'] = budget
    await _turn(main, fake, _declare('simple', 'plan'), call('submit_work_plan', {'data': data}, call_id='build'),
                call('submit_plan', {'plan': 'Check and repair stock before updating the count notes.'}))
    assert 'Work plan validated' in _output(main, 'build')
    key, = main._fusion.registry.plans
    return key, main._fusion.registry.plans[key]


async def _continue(main, fake, *responses):
    await _turn(main, fake, _declare('simple', 'execute'), call('submit_route', {}), *responses)


def _approve(main):
    assert main.approve_plan(main.saved.plan['id'])


async def _reopen(main):
    manager, project, session = main.manager, main.project, main.session_id
    await manager.close(main)
    return await manager.open_saved(project, session)


def test_finding_requires_main_budget_then_original_author_and_fresh_checks(tmp_path):
    async def go():
        async with _fusion(tmp_path, active=UNIT_GATES) as (main, fake):
            _one_step_adaptive(main)
            key, plan = await _plan(main, fake, _unit(), _unit('notes', paths=['notes.txt'], deps=['stock'], review_required=False))
            original = ledger.page(plan['spec'])
            case = plan['cases']['stock']
            order = 'Fixture returns three instead of four.'

            def read_handoff():
                ref = re.search(r'Details: read_output\((o-[0-9a-f]{64})\)', _output(main, 'runner-v1'))[1]
                yield from call('read_output', {'reference': ref, 'limit': 8000}, call_id='work-order')

            def pause():
                yield from _decision('pause', case['budgetReview'], 'pause')

            fake.script(_run('blocked', 'notes'), _run('worker-v1'), _report(evidence=['Count inspected.']),
                        _run('runner-v1'), _report('concerns', evidence=[order]), read_handoff(),
                        _run('blind', resume=True), pause(), say('Wait for a budget decision.'))
            _approve(main)
            await finish_turn(main)
            review = case['budgetReview']
            fact, = review['facts']
            h = case['handoff']
            assert (h['diagnostic'], h['confirmation'], h['nextOwner'], h['nextRevision']) == ('U03', 'budget_decision', 'Main', 2)
            assert order in _output(main, 'work-order') and 'roundLimits' in _output(main, 'work-order')
            assert h['usage']['round']['worker']['steps'] == h['usage']['total']['runner']['steps'] == 1
            assert h['usage']['lastAttempt']['applied_budget']['steps'] == 1 and h['usage']['repairs'] == 1
            receipt = _output(main, 'runner-v1')
            assert fact['ref'] in receipt and fact['ref'] in h['evidenceRefs']
            assert 'Used/limit: worker steps 1/1, runner steps 1/1.' in receipt
            assert review['limits'] == h['limits']
            assert 'unverified dependencies: stock' in _output(main, 'blocked')
            assert 'Adaptive budget review' in _output(main, 'blind') and main._fusion.registry.calls == 2
            author_id = case['receipts']['worker']['expert']
            pause_note = main.saved.turns[-1].note
            assert 'stock v1：本轮额度用尽（步数），Main 已决定暂停' in pause_note
            assert 'notes v0：依赖阻塞，等待 stock 验证通过。' in pause_note
            assert not main._fusion.acceptance_verdict().continuation()

            main = await _reopen(main)
            case = main._fusion.registry.plans[key]['cases']['stock']
            assert case['budgetReview'] == review and main.saved.turns[-1].note == pause_note
            bogus = main._outputs.put('Unrelated observation.')
            await _continue(main, fake, _decision('repeat-pause', review, 'pause'),
                            _decision('wrong-fact', review, 'continue', evidence=bogus),
                            _decision('manual-seconds', review, 'continue', evidence=fact['ref'], seconds=10),
                            _decision('too-large', review, 'continue', evidence=fact['ref'], steps=999),
                            _decision('approve-round', review, 'continue', evidence=fact['ref'], steps=4),
                            _decision('repeat-grant', review, 'continue', evidence=fact['ref']), say('Budget approved.'))
            assert all('already recorded' in _output(main, i) for i in ('repeat-pause', 'repeat-grant'))
            assert 'requires a new retained' in _output(main, 'wrong-fact')
            assert 'manual seconds limit' in _output(main, 'manual-seconds') and 'between 1 and 5' in _output(main, 'too-large')
            assert 'No expert dispatched' in _output(main, 'approve-round') and main._fusion.registry.calls == 2
            assert '已续额，等待显式派工' in main.saved.turns[-1].note
            assert [d['action'] for d in case['decisions']] == ['pause', 'continue']

            main = await _reopen(main)
            rt = main._fusion
            plan, case = rt.registry.plans[key], rt.registry.plans[key]['cases']['stock']
            assert rt.registry.calls == 2 and case['revision'] == 1 and case['grant']['steps'] == 4
            await _continue(main, fake, _run('early-notes', 'notes'), _run('no-resume'), _run('repair', resume=True),
                            _report(evidence=['Fixed the count.']), _run('runner-v2'), _report(evidence=['Fixture returns four.']),
                            _run('before-qs', 'notes'), _run('qs'), _report(evidence=['Independent sample agrees.']),
                            _run('notes-work', 'notes'), _report(evidence=['Notes updated.']),
                            _run('notes-run', 'notes'), _report(evidence=['Notes match.']), say('Verified.'))
            assert all('unverified dependencies' in _output(main, i) for i in ('early-notes', 'before-qs'))
            assert 'explicit resume' in _output(main, 'no-resume')
            assert ledger.page(plan['spec']) == original and ledger.digest(plan['spec']) == key
            assert case['revision'] == 2 and case['next'] == plan['cases']['notes']['next'] == 'done'
            assert case['receipts']['worker']['expert'] == author_id
            assert case['history'][2]['applied_budget']['steps'] == 4 and '@@P8' in str(main.agents[author_id].controller.saved.items)
            assert {p['revision'] for p in case['receipts'].values()} == {2}
            assert case['attempts'] == {'worker': 2, 'runner': 2, 'review': 1} and case['spent']['worker'] == 2
            assert '本轮工作已验证' in main.saved.turns[-1].note and 'Main 最近决策：继续' in main.saved.turns[-1].note
    asyncio.run(go())


@pytest.mark.parametrize('mode', ['changed', 'unchanged', 'missing', 'environment'])
def test_threshold_accepts_only_new_program_progress(tmp_path, mode):
    async def go():
        async with _fusion(tmp_path, active=UNIT_GATES) as (main, fake):
            _one_step_adaptive(main)
            _, plan = await _plan(main, fake, _unit(review_required=False))
            case = plan['cases']['stock']
            target = main.project / 'stock.txt'
            target.write_text('Four', encoding='utf-8')
            read = call('read_file', {'file_path': 'stock.txt'})
            disconnected = [*say('Partial output')[:-1], fail('server_error')[-1]]
            first = {'missing': _report('concerns', evidence=[]), 'environment': disconnected}.get(mode, read)
            author = [_run('initial-author'), _report(evidence=['Count delivered.'])] if mode in ('missing', 'environment') else []
            fake.script(*author, _run('first'), first, say('Paused.'),
                        *([say('Still paused.')] if mode in ('changed', 'unchanged') else []))
            _approve(main)
            await finish_turn(main)
            review = case['budgetReview']
            if mode in ('missing', 'environment'):
                assert not review['facts'] and 'finding' not in case
                assert case['handoff']['diagnostic'] == ('U01' if mode == 'missing' else 'U02')
                assert '无新的可核查证据，不能续额' in main.saved.turns[-1].note
                await _continue(main, fake, _decision('deny', review, 'continue', evidence='none'),
                                _decision('stop', review, 'stop'), _run('stopped', resume=True), say('Stopped.'))
                assert 'requires a new retained' in _output(main, 'deny') and main._fusion.registry.calls == 2
                assert 'stock v1：已停止；Main 最近决策：停止；已用/上限：Runner 检查 步数 1/1。' in main.saved.turns[-1].note
                return
            fact, = review['facts']
            assert fact['kind'] == 'progress' and 'Four' in main._outputs.read(fact['ref'])
            if mode == 'changed':
                target.write_text('Five', encoding='utf-8')
            await _continue(main, fake, _decision('grant', review, 'continue', evidence=fact['ref'], steps=1),
                            _run('resumed', resume=True), read, say('Paused again.'),
                            *([say('End.')] if mode == 'changed' else []))
            repeat = case['budgetReview']
            assert repeat['id'] != review['id'] and case['attempts']['worker'] == 2
            assert bool(repeat['facts']) == (mode == 'changed')
            await _continue(main, fake, _decision('recycle', repeat, 'continue', evidence=fact['ref']),
                            _decision('end', repeat, 'stop'), say('Stopped.'))
            assert 'requires a new retained' in _output(main, 'recycle')
    asyncio.run(go())


@pytest.mark.parametrize('cap', ['repeat finding', 'global calls', 'global steps', 'plan steps', 'manual steps'])
def test_repeated_work_order_or_hard_cap_cannot_open_another_round(tmp_path, cap, monkeypatch):
    async def go():
        async with _fusion(tmp_path, active=UNIT_GATES) as (main, fake):
            _one_step_adaptive(main)
            if cap == 'global steps':
                resolve = ledger.resolve
                def freeze_at_one(*args, settings, **kwargs):
                    return resolve(*args, settings=replace(settings, max_steps=1), **kwargs)
                monkeypatch.setattr(ledger, 'resolve', freeze_at_one)
            if cap == 'manual steps':
                _manual_steps(main, 1, 1)
                main._fusion.registry.policy = replace(main._fusion.registry.policy, max_seconds_adaptive=True)
            if cap == 'global calls':
                main._fusion.registry.policy = replace(main._fusion.registry.policy, max_calls=2)
            _, plan = await _plan(main, fake, _unit(review_required=False), budget={'turns': 1} if cap == 'plan steps' else None)
            case = plan['cases']['stock']
            fake.script(_run('author'), _report(evidence=['Count inspected.']), _run('check'),
                        _report('concerns', evidence=['Count is three.']), say('Paused.'),
                        *([say('Pending.')] if cap in ('global calls', 'repeat finding') else []))
            _approve(main)
            await finish_turn(main)
            if cap in ('global steps', 'plan steps', 'manual steps'):
                assert case['next'] == 'stopped' and case['paused'] == 'hard limit' and not case.get('budgetReview')
                h = case['handoff']
                assert h['diagnostic'] == 'U05' and h['finding']['evidence'] == ['Count is three.']
                assert {'dimension': 'steps', 'used': 1, 'limit': 1, 'stage': 'worker'} in h['limits']
                assert '[U05]' in _output(main, 'check') and 'worker steps 1/1' in _output(main, 'check')
                assert '已用/上限：作者交付 步数 1/1' in main.saved.turns[-1].note
                assert main._fusion.registry.calls == 2
                return
            review = case['budgetReview']
            fact, = review['facts']
            if cap == 'global calls':
                await _continue(main, fake, _decision('grant', review, 'continue', evidence=fact['ref']),
                                _decision('stop', review, 'stop'), say('End.'))
                assert 'global Fusion call limit' in _output(main, 'grant') and main._fusion.registry.calls == 2
                return
            await _continue(main, fake, _decision('grant', review, 'continue', evidence=fact['ref'], steps=1),
                            _run('repair', resume=True), _report(evidence=['Checked the change.']), _run('recheck'),
                            _report('concerns', evidence=['COUNT   is three.']), say('Blocked.'))
            assert case['revision'] == 2 and case['finding']['revision'] == 2 and not case['budgetReview']['facts']
            assert '已有检查工单；无新的可核查证据，不能续额' in main.saved.turns[-1].note
    asyncio.run(go())


@pytest.mark.parametrize('adaptive', [True, False])
def test_attempt_and_repair_counters_are_round_thresholds_only_in_adaptive(adaptive):
    unit = {'id': 'stock', 'roster': {'worker': {}}, 'budget': {'steps': 10, 'high': 20, 'hard_steps': 100,
            'seconds': 60, 'adaptive_steps': adaptive}}
    case = ledger.new_case()
    case.update(paused='repair', repairs=3, spent={'worker': 3}, attempts={'worker': 3},
                history=[{'stage': 'worker', 'steps': 1, 'seconds': 1, 'facts': []}] * 3)
    limits = ledger.settle_budget('plan', case, unit, 'worker', 'repair')
    assert limits == [{'dimension': 'attempts', 'used': 3, 'limit': 3, 'stage': 'worker'},
                      {'dimension': 'repairs', 'used': 3, 'limit': 2, 'stage': None}]
    if adaptive:
        assert case['next'] == 'worker' and case['budgetReview']['thresholds'] == ['attempts', 'repairs']
    else:
        assert case['next'] == 'stopped' and 'budgetReview' not in case


@pytest.mark.parametrize('adaptive_time', [True, False])
def test_actual_timeout_respects_the_other_fixed_dimension(tmp_path, monkeypatch, adaptive_time):
    async def go():
        async with _fusion(tmp_path, active=UNIT_GATES, max_seconds=1) as (main, fake):
            _manual_steps(main, 4, 10)
            rt = main._fusion
            rt.registry.policy = replace(rt.registry.policy, max_seconds_adaptive=adaptive_time)
            main.manager.settings.fusion_steps_max_adaptive = not adaptive_time
            monkeypatch.setattr(ledger.step_budget, 'adaptive_seconds', lambda steps: 1)
            _, plan = await _plan(main, fake, _unit(review_required=False))
            case = plan['cases']['stock']
            (main.project / 'stock.txt').write_text('Four', encoding='utf-8')
            fake.script(_run('timed'), call('read_file', {'file_path': 'stock.txt'}),
                        [('hold', 'timeout'), *say('Too late.')], say('Time used.'),
                        *([say('Waiting for a decision.')] if adaptive_time else []))
            _approve(main)
            try:
                await finish_turn(main)
            finally:
                fake.release('timeout')
            assert case['history'][0]['stop_reason'] == 'time limit'
            assert case['handoff']['usage']['lastAttempt']['applied_budget']['seconds'] == 1
            if not adaptive_time:
                assert case['paused'] == 'hard time limit' and not case.get('budgetReview')
                assert case['handoff']['diagnostic'] == 'U05' and rt.registry.calls == 1
                return
            review = case['budgetReview']
            assert review['thresholds'] == ['seconds'] and case['handoff']['diagnostic'] == 'U04'
            ref = review['facts'][0]['ref']
            spent = case['spent']['worker']
            await _continue(main, fake, _decision('manual-steps', review, 'continue', evidence=ref, steps=4),
                            _decision('grant-time', review, 'continue', evidence=ref, seconds=1),
                            _run('same-author', resume=True), _report(evidence=['Count checked.']),
                            _run('verified'), _report(evidence=['Fixture passed.']), say('Verified.'))
            assert 'manual steps limit' in _output(main, 'manual-steps')
            assert case['next'] == 'done' and case['spent']['worker'] > spent
            assert case['history'][1]['applied_budget']['steps'] <= 10 - spent
            assert case['history'][0]['expert'] == case['receipts']['worker']['expert']
    asyncio.run(go())


def test_crashed_reservation_charged_once_without_replaying_decision(tmp_path):
    async def go():
        async with _fusion(tmp_path, active=UNIT_GATES) as (main, fake):
            _one_step_adaptive(main)
            key, plan = await _plan(main, fake, _unit(review_required=False))
            case = plan['cases']['stock']
            (main.project / 'stock.txt').write_text('Four', encoding='utf-8')
            fake.script(_run('first'), call('read_file', {'file_path': 'stock.txt'}), say('Paused.'), say('Paused.'))
            _approve(main)
            await finish_turn(main)
            review = case['budgetReview']
            ref = review['facts'][0]['ref']
            await _continue(main, fake, _decision('grant', review, 'continue', evidence=ref, steps=1), say('Resume later.'))
            fake.script(_declare('simple', 'execute'), call('submit_route', {}), _run('in-flight', resume=True),
                        [('hold', 'crash'), *_report(evidence=['Undelivered.'])])
            assert main.send_prompt('Resume.')
            try:
                from tests.fakes import until
                await until(lambda: case.get('reserved') == 1)
                raw = main._fusion.registry.to_json()
                restored = FusionRegistry.from_json(main.project, raw)
                recovered = restored.plans[key]['cases']['stock']
                assert recovered['spent']['worker'] == 2 and recovered['attempts']['worker'] == 2
                assert recovered['decisions'] == case['decisions'] and len(recovered['history']) == 2
                assert recovered['budgetReview']['id'] != review['id'] and not recovered['budgetReview']['facts']
                assert recovered['handoff']['cause'] == 'process interrupted' and not recovered['receipts']
                assert FusionRegistry.from_json(main.project, restored.to_json()).plans == restored.plans
                await main.interrupt()
                await finish_turn(main)
            finally:
                fake.release('crash')
            assert case['paused'] == 'cancelled' and case['next'] == 'stopped' and not case.get('budgetReview')
    asyncio.run(go())
