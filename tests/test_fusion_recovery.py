from __future__ import annotations

import asyncio

import pytest

from desktop.core.fusion import verification_case as ledger
from desktop.core.fusion.registry import FusionRegistry
from desktop.core.viewmodel import turn_json
from tests.chatgpt_support import finish_turn
from tests.fake_responses import call, fail, say
from tests.fakes import until
from tests.test_fusion_adaptive_rounds import _approve, _decision, _plan, _reopen, _run
from tests.test_fusion_identity_runtime import _fusion, _manual_steps
from tests.test_fusion_route_runtime import UNIT_GATES, _report, _unit


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
