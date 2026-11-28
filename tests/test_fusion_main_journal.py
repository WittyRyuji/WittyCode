from __future__ import annotations

import asyncio
import json
import sqlite3
import subprocess
import sys
from types import SimpleNamespace

import pytest

from desktop.core.fusion.main_journal import MainJournal, PAGE_SIZE, READ_JOURNAL
from desktop.core.fusion.registry import FusionError, FusionRegistry, Policy
from desktop.core.fusion.runtime import FusionRuntime
from desktop.core.harness.loop import Call, Outcome
from desktop.core.harness.outputs import OutputStore
from desktop.core.harness.tools import ToolContext, ToolRegistry
from desktop.core.harness.toolset import HarnessTools

PLAN = 'p' * 64


def store(tmp_path, name='outputs'):
    return OutputStore(tmp_path / name)


def filled(journal, count, *, start=1, status='done'):
    for index in range(start, start + count):
        call = f'call-{index}'
        journal.start(call, f'intent-{index}', 'shell')
        if status != 'started':
            journal.finish(call, status, '', False)


def test_pages_are_64_rows_and_each_read_returns_at_most_two(tmp_path):
    journal = MainJournal.create(store(tmp_path), PLAN, {})
    filled(journal, 3 * PAGE_SIZE + 7)

    first = journal.read()
    assert len(first['rows']) == PAGE_SIZE + 7 <= 2 * PAGE_SIZE
    assert [row['seq'] for row in first['rows']] == list(range(2 * PAGE_SIZE + 1, 3 * PAGE_SIZE + 8))
    assert first['next_page'] == 2

    second = journal.read(first['next_page'])
    assert len(second['rows']) == 2 * PAGE_SIZE
    assert [row['seq'] for row in second['rows']] == list(range(1, 2 * PAGE_SIZE + 1))
    assert second['next_page'] is None
    with pytest.raises(FusionError):
        journal.read(5)


def test_call_ids_are_permanent_and_pending_intents_are_unique_across_pages(tmp_path):
    journal = MainJournal.create(store(tmp_path), PLAN, {})
    journal.start('open-call', 'cross-page-intent', 'shell')
    filled(journal, 2 * PAGE_SIZE, start=2)

    with pytest.raises(FusionError):
        journal.start('new-call', 'cross-page-intent', 'shell')

    journal.finish('open-call', 'done', 'o-' + 'a' * 64, False)
    with pytest.raises(FusionError):
        journal.start('open-call', 'different-intent', 'shell')
    journal.start('another-call', 'cross-page-intent', 'shell')
    row = next(row for row in journal.read()['rows'] if row['call'] == 'another-call')
    assert row['status'] == 'started'


def test_late_finish_updates_the_same_row_once_after_rebinding(tmp_path):
    outputs = store(tmp_path)
    journal = MainJournal.create(outputs, PLAN, {})
    journal.start('late-call', 'late-intent', 'shell')
    seq = journal.read()['rows'][0]['seq']

    rebound = MainJournal(outputs, PLAN, journal.token)
    rebound.finish('late-call', 'done', 'o-' + 'b' * 64, False)
    rows = rebound.read()['rows']
    assert len(rows) == 1
    assert rows[0]['seq'] == seq and rows[0]['call'] == 'late-call'
    assert rows[0]['status'] == 'done' and rows[0]['result'] == 'o-' + 'b' * 64
    with pytest.raises(FusionError):
        rebound.finish('late-call', 'error', '', False)


def test_legacy_256_rows_import_and_do_not_cap_the_sqlite_journal(tmp_path):
    legacy = {f'legacy-{index}': {'intent': f'legacy-intent-{index}', 'status': 'done', 'tool': 'shell',
                                  'result': '', 'outputPending': False}
              for index in range(256)}
    journal = MainJournal.create(store(tmp_path), PLAN, legacy)
    journal.start('after-legacy', 'after-legacy-intent', 'shell')

    with sqlite3.connect(journal.path) as db:
        count = db.execute('SELECT count(*) FROM operations').fetchone()[0]
        imported = db.execute('SELECT status, output_pending FROM operations WHERE call=?', ('legacy-0',)).fetchone()
    assert count == 257 and imported == ('done', 0)


def test_missing_database_is_not_recreated_and_bindings_are_isolated(tmp_path):
    outputs = store(tmp_path)
    first = MainJournal.create(outputs, PLAN, {})
    first.start('only-in-first', 'first-intent', 'shell')
    other_plan = 'q' * 64
    second = MainJournal.create(outputs, other_plan, {})
    second.start('only-in-second', 'second-intent', 'shell')

    assert first.path != second.path
    assert [row['call'] for row in first.read()['rows']] == ['only-in-first']
    assert [row['call'] for row in second.read()['rows']] == ['only-in-second']
    with pytest.raises(FusionError):
        MainJournal(outputs, other_plan, first.token).start('intruder', 'intruder-intent', 'shell')

    first.path.unlink()
    with pytest.raises(FusionError):
        MainJournal(outputs, PLAN, first.token).start('after-loss', 'after-loss-intent', 'shell')
    assert not first.path.exists()


class Parent:
    def __init__(self, outputs):
        self.session_id = 'main-session'
        self._outputs = outputs
        self.saved = SimpleNamespace(origin={}, fusion=None)
        self._fusion = None
        self._tools = None
        self.manager = SimpleNamespace(settings=None)
        self.checkpoints = 0

    def _save_now(self, required=False):
        self.checkpoints += 1
        return True

    def _declare_state(self):
        return None


def runtime(tmp_path, outputs):
    parent = Parent(outputs)
    registry = FusionRegistry(tmp_path, Policy('chatgpt:gpt-6.1-sol', ('chatgpt:gpt-6-luna',), 'chatgpt:gpt-6-luna', 0))
    return FusionRuntime(parent, registry), parent


def test_runtime_migrates_inline_rows_binds_reader_and_clears_it_next_turn(tmp_path):
    outputs = store(tmp_path)
    rt, parent = runtime(tmp_path, outputs)
    legacy = {f'legacy-{index}': {'intent': f'intent-{index}', 'status': 'done', 'tool': 'shell',
                                  'result': '', 'outputPending': False}
              for index in range(256)}
    owner = {'journal': legacy}
    tools = HarnessTools(ToolRegistry(), ToolContext(tmp_path, outputs=outputs), 'full')
    parent._tools = tools

    rt.bind_main_journal(tools, PLAN, owner)
    assert owner['journal'] == {} and owner['journalSession'] == parent.session_id
    token = owner['journalToken']
    assert isinstance(token, str) and len(token) == 32 and parent.checkpoints == 1
    assert READ_JOURNAL['name'] in {item['name'] for item in tools.declarations()}

    call = Call('item-1', 'main-call-257', 'shell', '{}', {'command': 'Write-Output x'})
    tools.tool_started(call, 'exec')
    asyncio.run(tools.tool_recorded(call, Outcome('finished'), 'exec'))
    read_call = Call('item-2', 'read-journal', READ_JOURNAL['name'], '{}', {})
    outcome = asyncio.run(tools.run(read_call))
    assert not outcome.is_error
    data = json.loads(outcome.output)
    row = next(row for row in data['rows'] if row['call'] == 'main-call-257')
    assert row['status'] == 'done' and row['result'].startswith('o-') and not row['output_pending']
    with sqlite3.connect((outputs.root / f'fusion-journal-{token}.sqlite3')) as db:
        assert db.execute('SELECT count(*) FROM operations').fetchone()[0] == 257

    rt.start_route_turn()
    assert tools.journal_reader is None
    assert READ_JOURNAL['name'] not in {item['name'] for item in tools.declarations()}


def test_runtime_reopening_uses_the_same_token_and_never_recreates_a_missing_ledger(tmp_path):
    outputs = store(tmp_path)
    rt, parent = runtime(tmp_path, outputs)
    owner = {}
    tools = HarnessTools(ToolRegistry(), ToolContext(tmp_path, outputs=outputs), 'full')
    rt.bind_main_journal(tools, PLAN, owner)
    token = owner['journalToken']

    rt.bind_main_journal(tools, PLAN, owner)
    assert owner['journalToken'] == token and parent.checkpoints == 1
    path = outputs.root / f'fusion-journal-{token}.sqlite3'
    path.unlink()
    call = Call('item-1', 'lost-ledger-call', 'shell', '{}', {'command': 'Write-Output x'})
    with pytest.raises(FusionError):
        tools.tool_started(call, 'exec')
    assert not path.exists()


def _external_tool(tmp_path, handler):
    outputs = store(tmp_path)
    rt, parent = runtime(tmp_path, outputs)
    tools = HarnessTools(ToolRegistry(), ToolContext(tmp_path, outputs=outputs), 'full')
    name = 'record_sample'
    tools._mcp[name] = (SimpleNamespace(call=handler), SimpleNamespace(name=name))
    parent._tools = tools
    return rt, tools


def _external_call(call_id, value='first'):
    args = {'value': value}
    return Call('item-' + call_id, call_id, 'record_sample', json.dumps(args), args)


@pytest.mark.parametrize('stop', ['cancel', 'process-exit'])
def test_unknown_external_effect_is_not_replayed_after_rebind(tmp_path, stop):
    effects = tmp_path / 'effects.txt'

    async def unexpected(name, args):
        pytest.fail(f'Unconfirmed action was replayed: {name}, {args}')

    rt, tools = _external_tool(tmp_path, unexpected)
    owner = {}
    rt.bind_main_journal(tools, PLAN, owner)
    snapshot = json.dumps(owner)

    if stop == 'cancel':
        async def cancel_call():
            applied = asyncio.Event()

            async def in_flight(name, args):
                effects.write_text(args['value'], encoding='utf-8')
                applied.set()
                await asyncio.Future()

            active, live_tools = _external_tool(tmp_path, in_flight)
            active.bind_main_journal(live_tools, PLAN, json.loads(snapshot))
            task = asyncio.create_task(live_tools.run(_external_call('original')))
            try:
                await asyncio.wait_for(applied.wait(), 5)
            finally:
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task

        asyncio.run(cancel_call())
    else:
        source = """
import asyncio, json, os, sys
from pathlib import Path
from tests.test_fusion_main_journal import PLAN, _external_call, _external_tool

root = Path(sys.argv[1])
async def lost_reply(name, args):
    (root / 'effects.txt').write_text(args['value'], encoding='utf-8')
    os._exit(42)

rt, tools = _external_tool(root, lost_reply)
rt.bind_main_journal(tools, PLAN, json.loads(sys.argv[2]))
asyncio.run(tools.run(_external_call('original')))
"""
        exited = subprocess.run([sys.executable, '-B', '-c', source, str(tmp_path), snapshot], timeout=20)
        assert exited.returncode == 42

    async def distinct(name, args):
        assert args == {'value': 'different'}
        return SimpleNamespace(text='confirmed', is_error=False)

    reopened, rebound = _external_tool(tmp_path, distinct)
    reopened.bind_main_journal(rebound, PLAN, json.loads(snapshot))
    for call_id in ('original', 'new-id'):
        with pytest.raises(FusionError, match='Inspect rather than replay'):
            asyncio.run(rebound.run(_external_call(call_id)))

    outcome = asyncio.run(rebound.run(_external_call('other-action', 'different')))
    assert not outcome.is_error and outcome.output == 'confirmed'
    rows = MainJournal(store(tmp_path), PLAN, owner['journalToken']).read()['rows']
    assert [(row['call'], row['status']) for row in rows] == [('original', 'started'), ('other-action', 'done')]
    assert effects.read_text(encoding='utf-8') == 'first'
