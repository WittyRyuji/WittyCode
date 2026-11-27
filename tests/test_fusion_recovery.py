from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import replace

import pytest

from desktop.core.chatgpt.controller import ChatGPTController
from desktop.core.chatgpt.errors import ChatGPTError
from desktop.core.fusion import recovery, verification_case as ledger
from desktop.core.fusion.registry import FusionRegistry
from desktop.core.harness.toolset import HarnessTools
from desktop.core.viewmodel import turn_json
from tests.chatgpt_support import finish_turn
from tests.fake_responses import call, fail, say
from tests.fakes import until
from tests.test_fusion_adaptive_rounds import _approve, _decision, _plan, _reopen, _run
from tests.test_fusion_debate_runtime import _declare
from tests.test_fusion_identity_runtime import LIGHT, _fusion, _manual_steps, _output, _turn
from tests.test_fusion_route_runtime import UNIT_GATES, _report, _unit


@pytest.mark.parametrize('legacy', [False, True])
def test_manual_resume_keeps_the_complex_task_even_after_a_misrouted_follow_up(tmp_path, legacy):
    async def go():
        async with _fusion(tmp_path, active=UNIT_GATES) as (main, fake):
            units = [_unit(name, paths=[name + '.txt'], review_required=False) for name in ('collect', 'native', 'matrix')]
            wrong = _run('bad-decision', 'collect', budget_decision={'review': 'not-a-review', 'action': 'continue'})
            await _turn(main, fake, _declare('complex', 'execute', call_id='original-declare'),
                        call('submit_work_plan', {'data': {'units': units}}, call_id='original-plan'),
                        call('submit_route', {}, call_id='original-route'), wrong, say('Still pending.'), say('Not finished.'))
            key, plan = next(iter(main._fusion.registry.plans.items()))
            assert plan['activated'] and all(not c['attempts'] for c in plan['cases'].values())
            assert 'pending budget review' in _output(main, 'bad-decision')
            original = main.saved.turns[-1]
            if legacy:
                original.fusion_plan = ''
                await _turn(main, fake, _declare('simple', 'execute', call_id='wrong-declare'), say('Wrong path.'))
                mistaken = main.saved.turns[-1]
                main.saved.items[mistaken.start]['content'][0]['text'] = '继续，上次没返回。'
                for record in (original, mistaken):
                    record.fusion_task = record.fusion_kind = record.fusion_resume_source = ''
                main._save_now(required=True)
            main = await _reopen(main)
            fake.script(_declare('simple', 'execute', call_id='try-reclassify'), _run('author', 'collect'),
                        _report(evidence=['Collection rebuilt.']), _run('runner', 'collect'),
                        _report(evidence=['Collection verified.']), say('Other work pending.'), say('Still pending.'))
            assert main.send_prompt('继续，上次没返回。')
            assert not main.send_prompt('继续'), 'a double send must not launch a second turn'
            await finish_turn(main)
            record = main.saved.turns[-1]
            assert record.fusion_plan == key and record.fusion_kind == 'complex'
            assert main._fusion.registry.plans[key]['cases']['collect']['next'] == 'done'
            assert main._fusion.registry.plans[key]['cases']['native']['attempts'] == {}
            assert 'continue' in _output(main, 'try-reclassify').lower()
            main = await _reopen(main)
            turn = main.transcript.turns[-1]
            assert turn.user_block.text == '继续，上次没返回。'
            assert main.saved.turns[-1].fusion_plan == key
            case = main._fusion.registry.plans[key]['cases']['collect']
            attempts = dict(case['attempts'])
            fake.script(_run('already-verified', 'collect'), say('Still working.'), say('No new evidence.'))
            assert main.send_prompt('继续')
            await finish_turn(main)
            assert case['attempts'] == attempts and 'done' in _output(main, 'already-verified')
    asyncio.run(go())


def test_manual_resume_inherits_a_simple_execution(tmp_path):
    async def go():
        async with _fusion(tmp_path, active=UNIT_GATES) as (main, fake):
            await _turn(main, fake, _declare('simple', 'execute'), say('Partial details.'))
            original = main.saved.turns[-1]
            fake.script(_declare('complex', 'execute', call_id='new-declaration'), say('More details.'))
            assert main.send_prompt('继续')
            await finish_turn(main)
            resumed = main.saved.turns[-1]
            assert resumed.fusion_task == original.fusion_task and resumed.fusion_kind == 'simple'
            assert not resumed.fusion_plan and not main._fusion.registry.plans
            assert 'continue' in _output(main, 'new-declaration').lower()
            fake.script(say('More on the original task.'))
            assert main.send_prompt('继续')
            await finish_turn(main)
            assert 'Original task: Check the inventory.' in str(fake.requests[-1]['input'])
    asyncio.run(go())


@pytest.mark.parametrize('state', ['unbound', 'cancelled', 'waiting', 'new_scope'])
def test_manual_resume_does_not_bypass_missing_or_waiting_authority(tmp_path, state):
    async def go():
        async with _fusion(tmp_path, active=UNIT_GATES) as (main, fake):
            await _turn(main, fake, _declare('complex', 'execute'),
                        call('submit_work_plan', {'data': {'units': [_unit(review_required=False)]}}),
                        call('submit_route', {}), say('Still pending.'), say('Not finished.'))
            key, plan = next(iter(main._fusion.registry.plans.items()))
            case = plan['cases']['stock']
            count = len(fake.requests)
            if state == 'new_scope':
                fake.script(_declare('simple', 'execute', call_id='new-scope'), say('New task.'))
                assert main.send_prompt('继续，并新增导出文件。')
                await finish_turn(main)
                assert main.saved.turns[-1].fusion_kind == 'simple' and not main.saved.turns[-1].fusion_plan
                return
            if state == 'unbound':
                record = main.saved.turns[-1]
                record.fusion_plan = record.fusion_task = record.fusion_kind = ''
                for entry in main.saved.items:
                    if entry.get('type') == 'function_call_output' and str(entry.get('output')).startswith('Frozen route active:'):
                        entry['output'] = 'No retained activation receipt.'
            elif state == 'cancelled':
                assert main._fusion.cancel_recovery(key)
            else:
                case['recoveryRequest'] = {'id': 'fusion-prerequisite-test', 'status': 'pending', 'question': 'Check the fixture.',
                                           'revision': 0, 'stage': 'worker'}
                main._fusion._checkpoint()
            assert main.send_prompt('继续')
            assert not main.busy and len(fake.requests) == count and not case['attempts']
            question, = main.transcript.pending_prompts()
            if state == 'waiting':
                assert question.id == 'fusion-prerequisite-test'
                main = await _reopen(main)
                assert main.fusion_resume_waiting and not main.busy and len(fake.requests) == count
                assert main.transcript.pending_prompts()[0].id == question.id
                assert main.cancel_waiting_fusion()
                assert not main.answer_question(question.id, ['已完成必要操作'])
            else:
                assert question.questions[0].options == ()
                assert main.answer_question(question.id, None)
                assert not main.answer_question(question.id, None)
    asyncio.run(go())


def test_adaptive_quote_adds_only_shortfall_without_resetting_accounting():
    unit = {'budget': {'steps': 50, 'high': 50, 'hard_steps': 50, 'seconds': 60, 'hard_seconds': 60,
                       'adaptive_steps': True, 'adaptive_seconds': False}}
    case = ledger.new_case()
    case.update(spent={'worker': 45}, history=[{'stage': 'worker', 'steps': 45, 'seconds': 5}])
    for original, source in [('adaptive', 'adaptive'), (50, 'fixed'), (None, 'unknown')]:
        grant, changes = ledger.reforecast(case, unit, 'worker', {'turns': original}, {'steps': 50},
                                          step_cap=80, seconds_cap=60)
        assert changes == {'steps': {'old': 50, 'new': 95, 'needed': 50, 'left': 5, 'source': source}}
        assert grant['steps'] == 50 and case['spent'] == {'worker': 45}
    assert unit['budget']['hard_steps'] == 50


@pytest.mark.parametrize('answer', ['approve', 'decline', 'cancel'])
def test_fixed_request_survives_reopen_and_does_not_block_independent_unit(tmp_path, answer):
    approved = answer == 'approve'
    async def go():
        async with _fusion(tmp_path, active=UNIT_GATES) as (main, fake):
            _manual_steps(main, 1, 1)
            key, plan = await _plan(main, fake, _unit(review_required=False), _unit('notes', paths=['notes.txt'], review_required=False))
            case = plan['cases']['stock']
            (main.project / 'stock.txt').write_text('Four', encoding='utf-8')

            def request():
                yield from _decision('fixed-increase', case['budgetReview'], 'continue', steps=2)

            fake.script(_run('first'), call('read_file', {'file_path': 'stock.txt'}), request(),
                        _run('notes-work', 'notes'), _report(evidence=['Notes updated.']),
                        _run('notes-verify', 'notes'), _report(evidence=['Notes verified.']), say('Notes are ready; stock remains.'))
            _approve(main)
            await finish_turn(main)
            assert plan['cases']['notes']['next'] == 'done'
            view = turn_json(main.transcript.turns[-1])
            assert view['fusionOutcome'] == 'waiting_user'
            prompt, = view['prompts']
            assert '1 → 3' in prompt['questions'][0]['question']
            assert any(b.get('markdown') == 'Notes are ready; stock remains.' for b in view['blocks'])
            main = await _reopen(main)
            rt = main._fusion
            plan = rt.registry.plans[key]
            case = plan['cases']['stock']
            assert main.transcript.pending_prompts()[0].id == prompt['id']
            assert not main.answer_question(prompt['id'], ['anything else'])
            if answer == 'cancel':
                assert main.cancel_waiting_fusion()
                assert not main.answer_question(prompt['id'], ['批准此次额度'])
                assert main.saved.turns[-1].fusion_outcome == 'cancelled'
                main = await _reopen(main)
                assert not main.transcript.pending_prompts() and not main.busy
                assert main.saved.turns[-1].fusion_outcome == 'cancelled'
                return
            if approved:
                fake.script(call('submit_route', {}), _run('author', resume=True), _report(evidence=['Stock ready.']),
                            _run('runner'), _report(evidence=['Stock verified.']), say('Both verified.'))
            else:
                fake.script(call('submit_route', {}), request(), say('No funded substitute.'), say('Stock stays unverified.'))
            assert main.answer_question(prompt['id'], ['批准此次额度' if approved else '不增加，改用其他方案'])
            assert not main.answer_question(prompt['id'], ['批准此次额度'])
            await until(lambda: main.busy)
            await finish_turn(main)
            assert main.saved.turns[-1].fusion_plan == key
            assert plan['cases']['notes']['attempts'] == {'worker': 1, 'runner': 1}
            if approved:
                assert case['next'] == 'done' and case['budgetTotals']['worker']['steps'] == 3
                assert main.saved.turns[-1].fusion_outcome == 'verified'
            else:
                assert case['budgetRequest']['status'] == 'declined' and 'grant' not in case
                assert not main.transcript.pending_prompts() and main.saved.turns[-1].fusion_outcome == 'unverified'
    asyncio.run(go())


def test_evidence_checkpoint_exists_before_the_expert_ends(tmp_path):
    async def go():
        async with _fusion(tmp_path, active=UNIT_GATES) as (main, fake):
            _manual_steps(main, 1, 4)
            key, plan = await _plan(main, fake, _unit(review_required=False))
            case = plan['cases']['stock']
            (main.project / 'stock.txt').write_text('Four', encoding='utf-8')
            fake.script(_run('author'), call('read_file', {'file_path': 'stock.txt'}, call_id='stock-read'),
                        [('hold', 'before-report'), *_report(evidence=['Stock inspected.'])], say('Not verified yet.'),
                        fail('server_error', 'Temporary failure.'))
            _approve(main)
            try:
                await until(lambda: 'stock-read' in case.get('journal', {}).get('calls', {}))
                saved = main.manager.sessions.load(main.session_id)
                restored = FusionRegistry.from_json(main.project, saved.fusion).plans[key]['cases']['stock']
                row, = restored['history']
                assert row['journal']['calls']['stock-read']['status'] == 'done'
                assert 'Four' in main._outputs.read(row['journal']['calls']['stock-read']['result'])
                assert row['progressIds'] and restored['next'] == 'worker' and not restored['receipts']
                assert 'reserved' not in restored
            finally:
                fake.release('before-report')
                await finish_turn(main)
    asyncio.run(go())


def test_abnormal_main_exit_has_body_and_local_receipt_after_reopen(tmp_path):
    async def go():
        async with _fusion(tmp_path, active=UNIT_GATES) as (main, fake):
            _, plan = await _plan(main, fake, _unit(review_required=False))
            case = plan['cases']['stock']
            body_then_disconnect = [*say('Author delivered; independent check still pending.')[:-1], fail('invalid_request_error')[-1]]
            fake.script(_run('author'), _report(evidence=['Stock value ready.']), body_then_disconnect)
            _approve(main)
            await finish_turn(main)
            assert case['next'] == 'runner' and 'worker' in case['receipts']
            assert main.saved.turns[-1].fusion_outcome == 'unverified'
            assert '运行异常' in main.saved.turns[-1].note
            view = turn_json(main.transcript.turns[-1])
            assert any('independent check still pending' in b.get('markdown', '') for b in view['blocks'])
            main = await _reopen(main)
            assert main.saved.turns[-1].fusion_outcome == 'unverified'
            assert any('independent check still pending' in b.get('markdown', '')
                       for b in turn_json(main.transcript.turns[-1])['blocks'])
    asyncio.run(go())


@pytest.mark.parametrize('value', ['proceed_once', 'cancel'])
def test_exact_tool_permission_is_durable_and_one_shot(tmp_path, value):
    async def go():
        async with _fusion(tmp_path, active=UNIT_GATES) as (main, fake):
            main.autonomy = 'ask'
            _manual_steps(main, 8, 8)
            key, plan = await _plan(main, fake, _unit(review_required=False))
            path = main.project / 'stock.txt'
            args = {'file_path': 'stock.txt', 'content': 'Four'}
            fake.script(_run('author'), call('create_file', args, call_id='blocked-write'), say('Waiting for one permission.'))
            _approve(main)
            await finish_turn(main)
            assert not path.exists() and not main._pending
            prompt, = main.transcript.pending_prompts()
            assert prompt.kind == 'permission' and prompt.actions[0].diffs
            main = await _reopen(main)
            case = main._fusion.registry.plans[key]['cases']['stock']
            stored, = main.transcript.pending_prompts()
            assert stored.id == prompt.id and stored.actions == prompt.actions
            assert not main.answer_permission(prompt.id, 'proceed_always')
            if value == 'proceed_once':
                fake.script(call('submit_route', {}), _run('resume', resume=True), call('create_file', args, call_id='allowed-write'),
                            _report(evidence=['File created.']), _run('runner'), _report(evidence=['Inventory verified.']), say('Done.'))
            else:
                fake.script(call('submit_route', {}), _run('resume', resume=True), call('create_file', args, call_id='denied-write'),
                            _report('blocked', evidence=[]), say('No alternative.'), say('Unverified.'))
            assert main.answer_permission(prompt.id, value)
            assert not main.answer_permission(prompt.id, 'proceed_once')
            await until(lambda: main.busy)
            await finish_turn(main)
            assert not main.transcript.pending_prompts()
            if value == 'proceed_once':
                assert path.read_text(encoding='utf-8') == 'Four'
                assert case['permissions'][prompt.id]['status'] == 'consumed'
                assert main.saved.turns[-1].fusion_outcome == 'verified'
            else:
                assert not path.exists() and case['permissions'][prompt.id]['status'] == 'declined'
                assert main.saved.turns[-1].fusion_outcome == 'unverified'
    asyncio.run(go())


def test_partial_tool_approval_only_resumes_the_authorized_unit(tmp_path):
    async def go():
        async with _fusion(tmp_path, active=UNIT_GATES) as (main, fake):
            main.autonomy = 'ask'
            _manual_steps(main, 8, 8)
            _, plan = await _plan(main, fake, _unit(review_required=False), _unit('notes', paths=['notes.txt'], review_required=False))
            stock = {'file_path': 'stock.txt', 'content': 'Four'}
            notes = {'file_path': 'notes.txt', 'content': 'Fixture notes'}
            fake.script(_run('first-stock'), call('create_file', stock, call_id='stock-write'),
                        _run('first-notes', 'notes'), call('create_file', notes, call_id='notes-write'), say('Two actions need approval.'))
            _approve(main)
            await finish_turn(main)
            first, second = main.transcript.pending_prompts()
            assert first.actions[0].path == 'stock.txt' and second.actions[0].path == 'notes.txt'
            fake.script(call('submit_route', {}), _run('resume-stock', resume=True), call('create_file', stock, call_id='allowed'),
                        _report(evidence=['Stock created.']), _run('stock-check'), _report(evidence=['Stock verified.']),
                        say('Notes still need a decision.'))
            assert main.answer_permission(first.id, 'proceed_once')
            await until(lambda: main.busy)
            await finish_turn(main)
            assert plan['cases']['stock']['next'] == 'done'
            assert plan['cases']['notes']['next'] == 'worker' and not (main.project / 'notes.txt').exists()
            assert [prompt.id for prompt in main.transcript.pending_prompts()] == [second.id]
            assert main.saved.turns[-1].fusion_outcome == 'waiting_user'
    asyncio.run(go())


def test_retry_after_waits_locally_then_resumes_without_user_input(tmp_path, monkeypatch):
    async def go():
        stream = ChatGPTController._stream_turn
        rejected = False
        async def rate_limit_once(ctl, request):
            nonlocal rejected
            if ctl._fusion_parent is not None and not rejected:
                rejected = True
                raise ChatGPTError('retry later', code='rate_limit', status=429, retry_after=1.1)
            async for event in stream(ctl, request):
                yield event
        monkeypatch.setattr(ChatGPTController, '_stream_turn', rate_limit_once)
        async with _fusion(tmp_path, active=UNIT_GATES) as (main, fake):
            _manual_steps(main, 8, 8)
            key, plan = await _plan(main, fake, _unit(review_required=False))
            case = plan['cases']['stock']
            fake.script(_run('first'), _run('early-retry', resume=True), say('The provider asked us to wait.'))
            _approve(main)
            await finish_turn(main)
            assert case['externalWait']['status'] == 'waiting' and case['attempts'] == {'worker': 1}
            assert 'scheduled retry' in _output(main, 'early-retry')
            count = len(fake.requests)
            main = await _reopen(main)
            case = main._fusion.registry.plans[key]['cases']['stock']
            assert case['externalWait']['status'] == 'waiting' and len(fake.requests) == count
            fake.script(call('submit_route', {}), _run('resume', resume=True), _report(evidence=['Delivery ready.']),
                        _run('runner'), _report(evidence=['Independent check passed.']), say('Done.'))
            await until(lambda: main.busy)
            await finish_turn(main)
            assert case['externalWait']['status'] == 'released' and case['next'] == 'done'
            assert len(fake.requests) == count + 6
    asyncio.run(go())


def test_wait_bounds_do_not_create_an_unlimited_model_poll():
    owner = {'revision': 1, 'next': 'runner'}
    for index in range(2):
        owner['externalWait'] = recovery.wait_record(owner, 10, now=100 + index)
        assert owner['externalWait']['until'] == 110 + index
    assert recovery.wait_record(owner, 10, now=102) is None
    assert recovery.wait_record({}, float('nan')) is None
    assert recovery.wait_record({}, 3600) is None


def test_replan_failed_runner_binding_retains_author_and_frozen_scope(tmp_path, monkeypatch):
    async def go():
        async with _fusion(tmp_path, active=UNIT_GATES) as (main, fake):
            _manual_steps(main, 8, 8)
            key, plan = await _plan(main, fake, _unit(review_required=False))
            unit = plan['spec']['units']['stock']
            case = plan['cases']['stock']
            original = ledger.digest(plan['spec'])
            rt = main._fusion
            alternative = 'chatgpt:gpt-6.1-nano'
            actual_model = rt._actual_model
            monkeypatch.setattr(rt, '_actual_model', lambda model: ('gpt-6.1-nano' if model == alternative else actual_model(model)))
            rt.registry.policy = replace(rt.registry.policy, light=alternative, lights=(alternative,))
            fake.script(_run('author'), _report(evidence=['Delivery ready.']), _run('unavailable-runner'),
                        _run('replan', replan=True), say('Replanned.'), say('Still waiting for independent check.'))
            _approve(main)
            await finish_turn(main)
            assert 'same work' in _output(main, 'replan')
            assert case['next'] == 'runner' and case['attempts'] == {'worker': 1}
            assert rt.registry.binding(key, 'stock', 'runner')['model'] == alternative
            assert ledger.digest(plan['spec']) == original and unit['roster']['runner']['model'] == LIGHT
            restored = FusionRegistry.from_json(main.project, rt.registry.to_json())
            assert restored.plans[key]['cases']['stock']['receipts'] == case['receipts']
    asyncio.run(go())


@pytest.mark.parametrize('checkpoint_kind', ['unknown-write', 'accepted-report'])
def test_local_crash_reconciliation_preserves_facts_without_replaying(tmp_path, monkeypatch, checkpoint_kind):
    async def go():
        async with _fusion(tmp_path, active=UNIT_GATES) as (main, fake):
            _manual_steps(main, 5, 5)
            key, _ = await _plan(main, fake, _unit(review_required=False))
            rt = main._fusion
            original_checkpoint = rt._checkpoint
            raw = None
            written = {'file_path': 'stock.txt', 'content': 'Four'}
            original_run = HarnessTools._run

            async def crash_after_write(tools, operation):
                nonlocal raw
                result = await original_run(tools, operation)
                if checkpoint_kind == 'unknown-write' and operation.call_id == 'write':
                    raw = deepcopy(main.saved.fusion)  # Intent persisted; the result did not return to Harness yet.
                return result

            def checkpoint():
                nonlocal raw
                original_checkpoint()
                if checkpoint_kind == 'accepted-report':
                    case = rt.registry.plans[key]['cases']['stock']
                    if any(row.get('report') for row in case.get('journal', {}).get('calls', {}).values()):
                        raw = deepcopy(main.saved.fusion)

            monkeypatch.setattr(HarnessTools, '_run', crash_after_write)
            monkeypatch.setattr(rt, '_checkpoint', checkpoint)
            fake.script(_run('author'), call('create_file', written, call_id='write'), _report(evidence=['Ready.']),
                        say('Delivered.'), say('Not yet checked.'))
            _approve(main)
            await finish_turn(main)
            assert raw is not None
            rt.registry = FusionRegistry.from_json(main.project, raw)
            case = rt.registry.plans[key]['cases']['stock']
            count = len(fake.requests)
            await rt.reconcile_checkpoints()
            await rt.reconcile_checkpoints()  # Idempotent, even at zero remaining work budget.
            assert len(fake.requests) == count and case['spent']['worker'] == 5
            if checkpoint_kind == 'unknown-write':
                row = case['history'][-1]['journal']['calls']['write']
                assert row['before']['fingerprint'] == 'absent' and row['status'] == 'started'
                assert row['observation'] and row['observedAfter']['fingerprint'] != 'absent'
                assert case['next'] == 'worker' and not case['receipts']
                assert case['handoff']['evidencePending']
            else:
                assert case['next'] == 'runner' and case['receipts']['worker']['detail']
                assert 'runner' not in case['receipts']
            FusionRegistry.from_json(main.project, rt.registry.to_json())
    asyncio.run(go())
