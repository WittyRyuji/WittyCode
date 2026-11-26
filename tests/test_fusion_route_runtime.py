from __future__ import annotations

import asyncio
import json

import pytest

from desktop.core.fusion.acp import AcpRun
from desktop.core.fusion.drive.retention import Retention
from desktop.core.fusion.protocol import ROUTE_DECLARATION, route_refusal
from desktop.core.viewmodel import turn_json
from tests.app_support import ready_core
from tests.chatgpt_support import finish_turn
from tests.fake_responses import call, say, usage
from tests.fakes import until
from tests.fusion_acp_support import make_guarded_engine
from tests.test_fusion_identity_runtime import DEVIN, LEGACY, LIGHT, WORKER, _brief, _fusion, _gates, _identity_folder, _manual_steps, _output, _turn

ROUTE_GATES = ("drive", "brief", "report", "route")
pytestmark = pytest.mark.fusion_gates("drive", "brief", "report", "route")


def _route(destination="coder", verification="runner", *, reason="Matches the size of the task.", call_id="route"):
    return call("submit_route", {"destination": destination, "verification": verification, "reason": reason}, call_id=call_id)


def _report(status="done", *, call_id="report", evidence=(), used=None):
    return call("submit_report", {"status": status, "summary": "Inventory checked.", "evidence": list(evidence), "notes": [], "ratings": []}, call_id=call_id, used=used)


def _edit(call_id="edit"):
    return call("create_file", {"file_path": "marker.txt", "content": "route test"}, call_id=call_id)


def _delegate(call_id="delegate", **kwargs):
    return call("delegate_task", _brief(**kwargs), call_id=call_id)


@pytest.mark.parametrize("accepts_prompt", [True, False])
def test_dispatch_feedback_requires_a_child_prompt_and_reopens_in_order(tmp_path, monkeypatch, accepts_prompt):
    async def go():
        async with _fusion(tmp_path, active=(*ROUTE_GATES, "v3")) as (main, fake):
            expected_opening = "I’ll check the inventory totals."
            expected_dispatch = "Coder-Inventory"
            if not accepts_prompt:
                send = type(main).send_prompt

                def refuse_child(controller, *args, **kwargs):
                    return False if controller is not main else send(controller, *args, **kwargs)

                monkeypatch.setattr(type(main), "send_prompt", refuse_child)
            declare = call("declare_topic", {"kind": "simple", "intent": "execute", "topic": "Check the stock",
                                            "next_step": expected_opening})
            research = dict(read_only=True, paths=[])
            responses = [declare, _delegate("refused-write"), _delegate("research", **research)]
            if accepts_prompt:
                responses += [[("hold", "worker"), *_report()], _delegate("again", **research), _report()]
            fake.script(*responses, say("Reviewed."))
            assert main.send_prompt("Check the inventory.")
            if accepts_prompt:
                await until(lambda: len(fake.requests) == 4)
                running = turn_json(main.transcript.turns[-1])
                assert running["status"] == "running"
                assert running["fusionFeedback"] == {"opening": expected_opening, "dispatches": [expected_dispatch]}
                assert not any(b["kind"] == "text" for b in running["blocks"])
                fake.release("worker")
            await finish_turn(main)
            assert "route" in _output(main, "refused-write")
            assert ("[done]" if accepts_prompt else "not ready") in _output(main, "research")
            assert len(fake.requests) == (7 if accepts_prompt else 4)
            assert all('已交给' not in json.dumps(request, ensure_ascii=False) for request in fake.requests)
            expected = {"opening": expected_opening, "dispatches": [expected_dispatch] if accepts_prompt else []}

            def shown(controller):
                turn = turn_json(controller.transcript.turns[-1])
                blocks = turn["blocks"]
                assert blocks[0]["kind"] == "user" and blocks[-1]["markdown"] == "Reviewed."
                assert not any(b["kind"] == "notice" for b in blocks)
                return turn["fusionFeedback"]

            assert shown(main) == expected
            await main.close()
            reopened = await main.manager.open_saved(main.project, main.session_id)
            try:
                assert shown(reopened) == expected
            finally:
                await reopened.close()
    asyncio.run(go())


def test_route_blocks_writes_before_routing_but_not_reads_or_research(tmp_path):
    async def go():
        async with _fusion(tmp_path, active=ROUTE_GATES) as (main, fake):
            (main.project / "stock.txt").write_text("4 parts", encoding="utf-8")
            await _turn(main, fake, _edit(), call("shell", {"command": "New-Item shell-marker.txt"}, call_id="shell"),
                        call("shell", {"command": "Get-Location"}, call_id="location"),
                        call("read_file", {"file_path": "stock.txt"}, call_id="read"), _delegate(),
                        _delegate("malformed-budget", budget=None),
                        _delegate("research", read_only=True, paths=[]), _report(), say("Reviewed."))
            for ident in ("edit", "shell", "delegate", "malformed-budget"):
                assert route_refusal(None, "edit") in _output(main, ident)
            assert "Refused" not in _output(main, "location") and "4 parts" in _output(main, "read")
            assert "Coder-Inventory [done]" in _output(main, "research")
            assert not (main.project / "marker.txt").exists() and not (main.project / "shell-marker.txt").exists()
            assert next(item for item in fake.requests[0]["tools"] if item["name"] == "submit_route")["parameters"] == ROUTE_DECLARATION["parameters"]
            assert "submit_route" not in {item["name"] for item in fake.requests[-2]["tools"]}
    asyncio.run(go())


@pytest.mark.parametrize("destination", ["self", "coder", "split"])
def test_destinations_enforce_edits_commands_and_distinct_workstreams(tmp_path, destination):
    async def go():
        async with _fusion(tmp_path, active=ROUTE_GATES) as (main, fake):
            replies = [_route(destination), _edit(), call("shell", {"command": "New-Item check.txt"}, call_id="check"), _delegate()]
            if destination != "self":
                replies += [_report(), _delegate("again"), _report(), _delegate("other", workstream="Shipping", paths=["shipping.txt"])]
                if destination == "split":
                    replies.append(_report())
            await _turn(main, fake, *replies, say("Reviewed."))
            assert (main.project / "check.txt").exists()
            if destination == "self":
                assert (main.project / "marker.txt").exists() and "assigns writes to you" in _output(main, "delegate")
                assert not main.agents
            else:
                assert not (main.project / "marker.txt").exists() and route_refusal(main._fusion.route_turn.route, "edit") in _output(main, "edit")
                assert "[done]" in _output(main, "again")
                assert len(main.agents) == (1 if destination == "coder" else 2)
                assert ("one write workstream" if destination == "coder" else "Coder-Shipping [done]") in _output(main, "other")
    asyncio.run(go())


def test_route_resets_next_turn_and_plan_refusal_precedes_it(tmp_path):
    async def go():
        async with _fusion(tmp_path, active=ROUTE_GATES) as (main, fake):
            await _turn(main, fake, _route("self"), say("Routed."))
            await _turn(main, fake, _edit(), say("Needs routing."))
            assert "submit a route first" in _output(main, "edit")
            main.mode = "spec"
            await _turn(main, fake, _edit("plan-edit"), _delegate("plan-delegate"), say("Planning."))
            assert all("Plan mode is on" in _output(main, ident) for ident in ("plan-edit", "plan-delegate"))
            assert not (main.project / "marker.txt").exists()
    asyncio.run(go())


def test_approved_plan_is_a_route_and_is_recorded(tmp_path):
    async def go():
        async with _fusion(tmp_path, active=ROUTE_GATES) as (main, fake):
            main.saved.plan = {"id": "plan", "status": "approved", "text": "Check and repair inventory."}
            await _turn(main, fake, _edit(), _delegate(), _report(), say("Reviewed."))
            assert (main.project / "marker.txt").exists() and "[done]" in _output(main, "delegate")
            turn = main._fusion.route_turn
            assert turn.from_plan and turn.route.verification == "runner"
            drive = main._fusion.drive
            route, = [candidate.record for candidate in drive.candidates(shared=False, retention=Retention()) if candidate.record.kind == "route"]
            assert "approved plan" in route.summary and json.loads(drive.read(route.pointers[0]))["source"] == "plan"
    asyncio.run(go())


@pytest.mark.parametrize("over_budget", [False, True])
def test_next_reroute_counts_once_and_survives_reopening(tmp_path, over_budget):
    async def go():
        async with _fusion(tmp_path, active=ROUTE_GATES) as (main, fake):
            if over_budget:
                _manual_steps(main)
            delegate = _delegate(budget={"steps": 1}) if over_budget else _delegate()
            worker = call("list_dir", {"directory_path": "."}) if over_budget else _report("blocked")
            await _turn(main, fake, _route(), delegate, worker,
                        _route("split", reason="The expert needed a different split.", call_id="reroute"),
                        _route("split", reason="The final route.", call_id="same-turn"), say("Reassessed."))
            assert main._fusion.registry.misroutes == 1
            assert main.manager.sessions.load(main.session_id).fusion["misroutes"] == 1
            drive = main._fusion.drive
            route, = [candidate.record for candidate in drive.candidates(shared=False, retention=Retention()) if candidate.record.kind == "route"]
            assert route.summary == "split [runner]: The final route."
            assert json.loads(drive.read(route.pointers[0]))["source"] == "submitted"
            await main.close()
            reopened = await main.manager.open_saved(main.project, main.session_id)
            assert reopened._fusion.registry.misroutes == 1
    asyncio.run(go())


@pytest.mark.parametrize("enabled", [False, True])
def test_gate_off_and_legacy_writing_delegation(tmp_path, enabled):
    async def go():
        active = ("route",) if enabled else ()
        async with _fusion(tmp_path, active=active) as (main, fake):
            legacy = call("delegate_task", LEGACY, call_id="legacy")
            await _turn(main, fake, _edit(), legacy, *([] if enabled else [say("Legacy report.")]), say("Reviewed."))
            assert ("submit_route" in {item["name"] for item in fake.requests[0]["tools"]}) == enabled
            assert (main.project / "marker.txt").exists() != enabled
            assert ("submit a route first" if enabled else "Legacy report.") in _output(main, "legacy")
    asyncio.run(go())


def test_devin_main_receives_route_schema_and_cannot_edit_before_routing(tmp_path, monkeypatch):
    original = AcpRun.dispatch
    manifests = []

    async def observe(self, method, params):
        result = await original(self, method, params)
        if method == "tools/list":
            manifests.append(result["tools"])
        return result

    monkeypatch.setattr(AcpRun, "dispatch", observe)

    async def go():
        _identity_folder()
        harness = make_guarded_engine(tmp_path)
        core, _, _, _, _ = await ready_core(tmp_path, devin_engine=harness.engine)
        try:
            await until(lambda: any(item["source"] == "devin" for item in core.catalog.models), timeout=20)
            core.settings.fusion_gates = _gates(*ROUTE_GATES)
            core.settings.fusion_main, core.settings.fusion_workers, core.settings.fusion_light = DEVIN, [WORKER], LIGHT
            project = tmp_path / "proj"
            cid = (await core.call("session.new", {"projectId": str(project), "fusion": True, "autonomy": "full"}))["id"]
            main = core._by_cid[cid].controller
            assert main.send_prompt("[fusion-write] Check inventory.")
            await finish_turn(main)
            assert any(item["name"] == "submit_route" and item["inputSchema"] == ROUTE_DECLARATION["parameters"]
                       for manifest in manifests for item in manifest)
            assert not (project / "marker.txt").exists()
            assert any("submit a route first" in item.get("output", "") for item in main.saved.items)
        finally:
            await core.shutdown()
    asyncio.run(go())


UNIT_GATES = ('unit_plan', 'v3', 'route', 'acceptance', 'brief', 'report', 'inject')


def _unit(name='stock', **overrides):
    return {'id': name, 'task': 'Repair the stock count.', 'basis': 'One calculation affects the exported count.',
            'paths': ['stock.txt'], 'acceptance': 'Count equals four.', 'J': 4, 'I': 4, 'C': 4,
            'review_required': True, **overrides}


def test_fact_plan_approval_restart_and_unit_tool_roundtrip(tmp_path):
    from dataclasses import replace
    import hashlib
    from tests.test_fusion_debate_runtime import _declare

    async def go():
        async with _fusion(tmp_path, active=UNIT_GATES) as (main, fake):
            main.mode = 'spec'
            await _turn(main, fake, _declare('simple', 'plan'),
                        call('submit_work_plan', {'data': {'units': [_unit(model=LIGHT)]}}, call_id='forged'),
                        call('submit_work_plan', {'data': {'units': [_unit()]}}, call_id='facts'),
                        call('delegate_task', {'unit': 'stock'}, call_id='early'),
                        call('submit_plan', {'plan': 'Stock check. ' + 'Approval prose. ' * 200}, call_id='plan'))
            assert 'Work plan refused' in _output(main, 'forged')
            assert 'only gated calls' in _output(main, 'early') and not main.agents
            receipt = _output(main, 'facts')
            assert 'worker=Coder@0/L2' in receipt and 'review=QualitySampler@0/L3' in receipt and len(receipt) < 600
            key, = main._fusion.registry.plans
            assert not main._fusion.registry.plans[key]['activated']
            main._fusion.registry.policy = replace(main._fusion.registry.policy, preference=99)
            main._fusion._checkpoint()
            manager, project, session = main.manager, main.project, main.session_id
            await manager.close(main)
            manager.settings.fusion_gates['unit_plan'] = False  # Admission changes, the already compiled format does not.
            main = await manager.open_saved(project, session)
            assert not main._fusion.unit_enabled
            fake.script(call('submit_route', {'destination': 'self'}, call_id='swap'),
                        call('delegate_task', {'unit': 'stock', 'model': LIGHT}, call_id='wrong'),
                        call('delegate_task', {'unit': 'stock'}, call_id='worker'), _edit('outside'),
                        _report(evidence=['Inspected stock.txt: four.']),
                        call('delegate_task', {'unit': 'stock'}, call_id='runner'),
                        call('read_output', {'reference': 'o-' + hashlib.sha256(b'Count equals four.').hexdigest()}, call_id='expand'),
                        _report(evidence=['Stock fixture returns four.']),
                        call('delegate_task', {'unit': 'stock'}, call_id='qs'), _report(evidence=['Checked the boundary case.']),
                        say('Verified.'))
            assert main.approve_plan(main.saved.plan['id'])
            await finish_turn(main)
            assert 'supplies the whole route' in _output(main, 'swap') and 'only a frozen unit' in _output(main, 'wrong')
            assert 'next=runner' in _output(main, 'worker'), _output(main, 'worker')
            assert 'next=review' in _output(main, 'runner') and 'next=done' in _output(main, 'qs')
            plan = main._fusion.registry.plans[key]
            assert main._fusion.registry.policy.preference == 99 and plan['spec']['units']['stock']['facts']['N'] == 25
            case = plan['cases']['stock']
            assert set(case['receipts']) == {'worker', 'runner', 'review'} and case['revision'] == 1
            for stage, expected in [('worker', WORKER), ('runner', LIGHT), ('review', LIGHT)]:
                view = main.agents[case['receipts'][stage]['expert']]
                assert (view.record.plan, view.record.stage, view.record.model) == (key, stage, expected)
                child = view.controller
                first = next(i for i in child.saved.items if i.get('role') == 'user')['content'][0]['text']
                assert first.startswith('@@FPACK v1') and first.endswith('@@END') and len(first.encode()) <= 8192
                assert 'Approval prose.' not in first
                if stage == 'worker':
                    assert 'outside frozen unit' in _output(child, 'outside') and not (project / 'marker.txt').exists()
                if stage == 'runner':
                    assert '@@P7' in first and 'Count equals four.' in _output(child, 'expand')
            assert main._fusion.acceptance_verdict().missing == ()
            await manager.close(main)
            restored = await manager.open_saved(project, session)
            assert restored._fusion.registry.plans[key]['cases']['stock'] == case
    asyncio.run(go())


def test_unit_contract_uses_a_plan_budget_not_a_per_unit_step_request():
    from desktop.core.fusion.planning import UNIT_CONTRACT, unit_facts
    assert 'data.budget' in UNIT_CONTRACT and 'computes unit steps' in UNIT_CONTRACT
    facts, budget = unit_facts({'units': [_unit()], 'budget': {'turns': 20}})
    assert budget['turns'] == 20 and 'budget' not in facts[0]
    with pytest.raises(ValueError, match='Unknown unit field'):
        unit_facts({'units': [_unit(budget={'turns': 20})]})


def test_frozen_packet_read_output_bridges_only_bound_evidence(tmp_path):
    import re
    from desktop.core.harness.tools import ToolError
    from tests.test_fusion_debate_runtime import _declare

    async def go():
        async with _fusion(tmp_path, active=UNIT_GATES) as (main, fake):
            main.mode = 'spec'
            await _turn(main, fake, _declare('simple', 'plan'), call('submit_work_plan', {'data': {'units': [_unit()]}}),
                        call('submit_plan', {'plan': 'Check the count.'}))
            key, = main._fusion.registry.plans
            case = main._fusion.registry.plans[key]['cases']['stock']
            unrelated = main._outputs.put('Private parent record.')
            def dispatch(call_id='delegate', **args):
                return call('delegate_task', {'unit': 'stock', **args}, call_id=call_id)
            pointers = {}

            def read_index(section):
                # Respond to the actual child request: take the address from its latest packet, not from the parent.
                user = next(i for i in reversed(fake.requests[-1]['input']) if i.get('role') == 'user')
                ref = re.search(r'@@' + section + r'\nout:(o-[0-9a-f]{64})@', user['content'][0]['text'])[1]
                yield from call('read_output', {'reference': ref}, call_id='index')

            def read_detail(kind):
                index = next(i['output'] for i in reversed(fake.requests[-1]['input']) if i.get('call_id') == 'index' and 'output' in i)
                ref = re.search(r'"detail": "(o-[0-9a-f]{64})"', index)[1]
                pointers[kind] = ref
                yield from call('read_output', {'reference': ref}, call_id='detail')

            def read_wrong_author():
                yield from call('read_output', {'reference': case['receipts']['worker']['detail']}, call_id='wrong-stage')

            private_report = call('submit_report', {'status': 'done', 'summary': 'Counted the file.', 'evidence': ['Four rows.'],
                                                    'notes': ['Private author notes.'], 'ratings': []})
            fake.script(dispatch('author-v1'), private_report, dispatch(), read_index('P7'), read_detail('author-v1'),
                        _report('concerns', evidence=['One duplicated row.']), dispatch(resume=True), read_index('P8'),
                        read_detail('finding-v1'), _report(evidence=['Removed duplicate.']), dispatch(), _report(evidence=['Three unique rows.']),
                        dispatch(), read_index('P7'), read_detail('runner-v2'), read_wrong_author(),
                        call('read_output', {'reference': unrelated}, call_id='wrong-parent'), _report(evidence=['Sample passed.']), say('Checked.'))
            assert main.approve_plan(main.saved.plan['id'])
            await finish_turn(main)
            assert (case['next'], case['revision']) == ('done', 2)
            children = {(v.record.stage, v.record.revision): v.controller for v in main.agents.values()}
            runner, author, qs = (children[k] for k in [('runner', 1), ('worker', 2), ('review', 2)])
            assert 'Four rows.' in _output(runner, 'detail') and 'Private author notes.' not in _output(runner, 'detail')
            assert 'One duplicated row.' in _output(author, 'detail') and 'Private author notes.' not in _output(author, 'index')
            assert 'Three unique rows.' in _output(qs, 'detail') and 'Removed duplicate.' not in _output(qs, 'index')
            assert all('unavailable' in _output(qs, ident) for ident in ('wrong-parent', 'wrong-stage'))
            assert all(json.loads(main._outputs.read(ref))['plan'] == key for ref in pointers.values())
            raw = re.search(r'read_output\((o-[0-9a-f]{64})\)', _output(main, 'author-v1'))[1]
            assert 'Private author notes.' in main._outputs.read(raw)
            with pytest.raises(ToolError, match='unavailable'):
                runner._outputs.read(raw)
            for ref in (raw, pointers['author-v1'], pointers['finding-v1']):
                with pytest.raises(ToolError, match='unavailable'):
                    qs._outputs.read(ref)
    asyncio.run(go())


@pytest.mark.parametrize('damage', ['substitution', 'stale revision', 'missing', 'corruption'])
def test_frozen_packet_unavailable_source_does_not_dispatch_checker(tmp_path, damage, monkeypatch):
    async def go():
        async with _fusion(tmp_path, active=UNIT_GATES) as (main, fake):
            case = await _prepare_single(main, fake)
            store = main._outputs
            unrelated = store.put('An unrelated retained output.')
            reads = []
            read = store.read

            def tracked(ref):
                reads.append(ref)
                return read(ref)
            monkeypatch.setattr(store, 'read', tracked)

            def break_and_dispatch():
                receipt = case['receipts']['worker']
                path = store.root / (receipt['detail'] + '.txt')  # Only this test's newly issued credential.
                if damage == 'substitution':
                    receipt['detail'] = unrelated
                elif damage == 'stale revision':
                    from desktop.core.fusion.verification_case import page
                    other = {**json.loads(read(receipt['detail'])), 'revision': receipt['revision'] - 1}
                    receipt['detail'] = store.put(page(other))
                elif damage == 'missing':
                    path.unlink()
                else:
                    path.write_bytes(b'changed')
                yield from call('delegate_task', {'unit': 'stock'}, call_id='check')

            fake.script(call('delegate_task', {'unit': 'stock'}), _report(evidence=['Checked rows.']),
                        break_and_dispatch(), say('Evidence unavailable.'), say('Stopped.'))
            assert main.approve_plan(main.saved.plan['id'])
            await finish_turn(main)
            assert case['next'] == 'runner' and case['paused'] and 'runner' not in case['receipts']
            runner = next(v.controller for v in main.agents.values() if v.record.stage == 'runner')
            assert not any(i.get('role') == 'user' for i in runner.saved.items)
            assert unrelated not in reads  # A swapped ref was not even opened in the parent for inspection.
            if damage == 'stale revision':
                assert case['receipts']['worker']['detail'] not in reads
    asyncio.run(go())


def test_units_bind_ranked_pool_and_versioned_author_through_repair(tmp_path):
    from dataclasses import replace
    from desktop.core.fusion import profiles
    from desktop.core.fusion.registry import FusionError, FusionRegistry
    from desktop.core.settings import default_settings_path
    from tests.test_fusion_debate_runtime import _declare

    def dispatch(name, ident, resume=False):
        return call('delegate_task', {'unit': name, 'resume': resume}, call_id=ident)

    async def go():
        async with _fusion(tmp_path, active=UNIT_GATES) as (main, fake):
            rt = main._fusion
            catalog = rt.catalog
            author = catalog.create(stable_id='stock-author', name='Stock Author', work_type='execute', tags=['stock'],
                                    markdown='---\n---\nUse the original stock convention.')
            rt.registry.policy = replace(rt.registry.policy, workers=(WORKER, LIGHT), light=WORKER, lights=(WORKER, LIGHT))
            for m, cost in [(LIGHT, 1), (WORKER, 10)]:
                profiles.ensure(default_settings_path().parent, m, lambda _, c=cost: {'score': 40, 'cost': c})
            main.mode = 'spec'
            await _turn(main, fake, _declare('simple', 'plan'),
                        call('submit_work_plan', {'data': {'units': [_unit(tags=['stock']),
                            _unit('doc', paths=['doc.txt'], deps=['stock'], review_required=False)]}}, call_id='compile', used=usage(54, 11, 4)),
                        call('submit_plan', {'plan': 'Repair and verify the two units.'}))
            key, = rt.registry.plans
            catalog.update(author.stable_id, markdown='---\n---\nNew version should not run.')
            catalog.set_enabled(author.stable_id, False)
            proof = lambda label: _report(evidence=[label])
            fake.script(dispatch('doc', 'early'), dispatch('stock', 'first'), proof('checked file'),
                        dispatch('stock', 'run'), _report(),
                        call('shell', {'command': 'New-Item blocked-check.txt'}, call_id='no-rerun'),
                        _report('concerns', evidence=['count mismatch'], used=usage(97, 13, 7)),
                        dispatch('stock', 'no-resume'), dispatch('stock', 'repair', True), proof('fixed count'),
                        dispatch('stock', 'rerun'), proof('rerun passed'), dispatch('stock', 'review'), proof('sample passed'),
                        dispatch('doc', 'doc'), proof('notes updated'), dispatch('doc', 'doc-run'), proof('notes checked'), say('Verified.'))
            assert main.approve_plan(main.saved.plan['id'])
            await finish_turn(main)
            assert 'unverified dependencies' in _output(main, 'early')
            assert 'repair' in _output(main, 'run') and 'explicit resume' in _output(main, 'no-resume')
            assert 'next=done' in _output(main, 'doc-run'), _output(main, 'doc-run')
            plan = rt.registry.plans[key]
            unit, case = plan['spec']['units']['stock'], plan['cases']['stock']
            assert case['revision'] == 2 and case['attempts'] == {'worker': 2, 'runner': 3, 'review': 1}
            missing, defect = [h['handoff'] for h in case['history'] if 'handoff' in h]
            assert (missing['diagnostic'], missing['stage'], missing['confirmation']) == ('U01', 'runner', 'one_evidence_supplement')
            assert (defect['diagnostic'], defect['category'], defect['nextOwner'], defect['nextRevision']) == (
                'U03', 'artifact_defect', 'stock-author', 2)
            assert defect['unit'] == 'stock' and defect['revision'] == 1 and defect['remaining'] == ['worker', 'runner', 'review']
            assert defect['completed'] == ['worker'] and defect['evidenceRefs'] and not defect['inFlight']
            assert all(receipt['revision'] == 2 for receipt in case['receipts'].values())
            assert unit['roster']['worker']['model'] == unit['roster']['runner']['model'] == LIGHT
            assert unit['roster']['review']['model'] == WORKER
            workers = [v for v in main.agents.values() if v.record.workstream == 'stock' and v.record.stage == 'worker']
            worker, = workers
            assert worker.record.identity == author.name and worker.record.revision == 2
            assert any('Use the original stock convention.' in str(item) and '@@P8' in str(item) for item in worker.controller.saved.items)
            assert not any('New version should not run.' in str(item) for item in worker.controller.saved.items)
            runners = [v for v in main.agents.values() if v.record.workstream == 'stock' and v.record.stage == 'runner']
            assert sorted(v.record.revision for v in runners) == [1, 2]
            first_runner = next(v for v in runners if v.record.revision == 1)
            assert 'read-only' in _output(first_runner.controller, 'no-rerun') and not (main.project / 'blocked-check.txt').exists()
            saved = rt.registry.to_json()
            restored = FusionRegistry.from_json(main.project, saved)
            assert restored.plans == rt.registry.plans and restored.policy.lights == (WORKER, LIGHT)
            from desktop.core.fusion.runtime import FusionRuntime
            summary = rt.metrics_summary()['frozen_units']
            assert summary == rt.metrics_summary()['frozen_units'] == FusionRuntime(main, restored).metrics_summary()['frozen_units']
            assert (summary['plans'], summary['units']) == (1, 2)
            groups = summary['groups']
            assert groups['planning']['kinds'] == {'resolve': 1, 'facts': 1, 'proposal': 1}
            assert groups['planning']['calls'] == 2 and groups['planning']['tokens'] == {'input': 170, 'output': 41, 'cacheRead': 4}
            assert groups['exception']['kinds'] == {'diagnosis': 1, 'supplement': 1}
            assert groups['exception']['calls'] == 2 and groups['exception']['tokens'] == {'input': 210, 'output': 43, 'cacheRead': 7}
            assert groups['alignment']['kinds'] == {'finding': 1, 'repair': 1} and groups['alignment']['calls'] == 1
            assert groups['review']['kinds'] == {'runner': 3, 'review': 1} and groups['review']['calls'] == 4
            assert groups['execution']['calls'] == 2
            # Being the first (authorized) pool member is not a substitute for being the selected member.
            next(a for a in saved['agents'] if a['id'] == worker.record.id)['model'] = WORKER
            with pytest.raises(FusionError):
                FusionRegistry.from_json(main.project, saved)
    asyncio.run(go())


@pytest.mark.fusion_gates
def test_default_gates_keep_simple_execute_and_legacy_approved_tools(tmp_path):
    from tests.test_fusion_debate_runtime import _declare, _plan

    async def go():
        async with _fusion(tmp_path) as (main, fake):
            settings = main.manager.settings
            settings.fusion_gates = {}  # Production defaults, including the new format.
            assert main._fusion.unit_enabled
            await _turn(main, fake, _declare(), _route('self', 'self_check'), _edit('simple'), say('Done.'))
            assert (main.project / 'marker.txt').exists() and not main._fusion.registry.plans
            settings.fusion_gates = {'unit_plan': False}
            main.mode = 'spec'
            await _turn(main, fake, _declare('simple', 'plan'),
                        call('submit_work_plan', {'data': _plan('direct')}, call_id='old'),
                        call('submit_plan', {'plan': 'Inspect inventory.'}))
            assert 'validated' in _output(main, 'old') and not main._fusion.registry.plans
            settings.fusion_gates.clear()
            fake.script(_route('self', 'self_check'), _write('approved.txt', 'approved'), say('Done.'))
            assert main.approve_plan(main.saved.plan['id'])
            await finish_turn(main)
            assert (main.project / 'approved.txt').exists()
            manifest = {t['name']: t['parameters'] for t in fake.requests[-1]['tools']}
            assert 'workstream' in manifest['delegate_task']['properties'] and 'items' in manifest['submit_route']['properties']
    asyncio.run(go())


@pytest.mark.fusion_gates
def test_new_plan_with_production_gate_defaults_uses_frozen_delegations(tmp_path):
    from tests.test_fusion_debate_runtime import _declare

    async def go():
        async with _fusion(tmp_path) as (main, fake):
            main.manager.settings.fusion_gates = {}
            main.mode = 'spec'
            await _turn(main, fake, _declare('simple', 'plan'),
                        call('submit_work_plan', {'data': {'units': [_unit(paths=[])]}}, call_id='new'),
                        call('submit_plan', {'plan': 'Audit the inventory without changing it.'}))
            assert 'worker=Coder@' in _output(main, 'new')
            run = lambda i: call('delegate_task', {'unit': 'stock'}, call_id=i)
            fake.script(run('author'), _report(evidence=['Inspected count.']), run('runner'), _report(evidence=['Checked arithmetic.']),
                        run('review'), _report(evidence=['Sampled zero count.']), say('Verified.'), say('Verified and reported.'))
            assert main.approve_plan(main.saved.plan['id'])
            await finish_turn(main)
            key, = main._fusion.registry.plans
            assert main._fusion.registry.plans[key]['cases']['stock']['next'] == 'done', _output(main, 'author')
            assert all(v.record.plan == key for v in main.agents.values()) and main._fusion.registry.calls == 3
    asyncio.run(go())


def test_real_reviewer_cannot_review_itself_even_with_a_different_model(tmp_path):
    from tests.test_fusion_debate_runtime import _declare

    async def go():
        async with _fusion(tmp_path, active=UNIT_GATES) as (main, fake):
            main.mode = 'spec'
            facts = {'units': [_unit(paths=[], work_type='review', expert='QualitySampler')]}
            await _turn(main, fake, _declare('simple', 'plan'), call('submit_work_plan', {'data': facts}, call_id='self'), say('Blocked.'))
            assert 'independent review expert' in _output(main, 'self') and not main._fusion.registry.plans
            main._fusion.catalog.create(stable_id='other-reviewer', name='Second Sampler', work_type='review', markdown='---\n---\nCheck boundaries.')
            await _turn(main, fake, _declare('simple', 'plan'), call('submit_work_plan', {'data': facts}, call_id='independent'),
                        call('submit_plan', {'plan': 'Check boundaries.'}))
            assert 'validated' in _output(main, 'independent')
            plan, = main._fusion.registry.plans.values()
            roster = plan['spec']['units']['stock']['roster']
            assert roster['worker']['expert'] == 'QualitySampler' and roster['review']['expert'] == 'other-reviewer'
            assert roster['worker']['actual_model'] != roster['review']['actual_model']
    asyncio.run(go())


def test_unit_candidates_use_local_provider_capabilities_not_page_labels(tmp_path, monkeypatch):
    from dataclasses import replace
    from types import SimpleNamespace
    from desktop.core.fusion.registry import FusionError
    from tests.test_fusion_debate_runtime import _declare

    async def go():
        async with _fusion(tmp_path, active=UNIT_GATES) as (main, fake):
            rt, manager = main._fusion, main.manager
            info = manager.model_info
            bad, good, alias = 'api:local:missing', 'api:local:enabled', 'api:local:alias'
            spec = lambda page: (SimpleNamespace(id=page, native=WORKER.split(':')[1], window=0 if page == bad else 32000,
                                                bedrock=None, kind='openai', base_url='http://localhost', extra_args={}, reasoning_args={})
                                 if page in (bad, good, alias) else None)
            monkeypatch.setattr(manager, 'api', SimpleNamespace(spec=spec, providers=None))
            monkeypatch.setattr(manager, 'model_info', lambda m: info(WORKER.split(':')[1]) if m.startswith(('factory:', 'api:', 'devin:')) else info(m))
            rt.registry.policy = replace(rt.registry.policy, workers=(bad, good), light=alias, lights=(alias, 'factory:missing', 'devin:missing', LIGHT))
            main.mode = 'spec'
            await _turn(main, fake, _declare('simple', 'plan'), call('submit_work_plan', {'data': {'units': [_unit()]}}, call_id='build'),
                        call('submit_plan', {'plan': 'Review independent native models.'}))
            assert 'validated' in _output(main, 'build')
            plan, = rt.registry.plans.values()
            roster = plan['spec']['units']['stock']['roster']
            assert roster['worker']['model'] == good and roster['review']['model'] == LIGHT
            assert roster['worker']['actual_model'] == roster['runner']['actual_model']  # Runner is not QS.
            assert {x['model'] for x in plan['spec']['excluded']} == {bad, 'factory:missing', 'devin:missing'}
            manager.settings.chatgpt_enabled = False
            with pytest.raises(FusionError, match='unavailable'):
                rt._actual_model(WORKER)
    asyncio.run(go())


async def _prepare_single(main, fake):
    from tests.test_fusion_debate_runtime import _declare
    main.mode = 'spec'
    await _turn(main, fake, _declare('simple', 'plan'),
                call('submit_work_plan', {'data': {'units': [_unit(review_required=False)]}}, call_id='unit-plan'),
                call('submit_plan', {'plan': 'Check the stock file.'}))
    assert 'validated' in _output(main, 'unit-plan')
    return next(iter(main._fusion.registry.plans.values()))['cases']['stock']


@pytest.mark.parametrize('mode', ['progress', 'no progress', 'lost proof', 'hard cap', 'disconnect'])
def test_frozen_limits_and_interruption_never_blindly_replay(tmp_path, mode, monkeypatch):
    from tests.fake_responses import fail

    async def go():
        async with _fusion(tmp_path, active=UNIT_GATES) as (main, fake):
            _manual_steps(main, 1, 1 if mode == 'hard cap' else 4)
            (main.project / 'stock.txt').write_text('Four', encoding='utf-8')
            case = await _prepare_single(main, fake)
            read = call('read_file', {'file_path': 'stock.txt'})
            child = (call('list_dir', {'directory_path': '.'}) if mode == 'no progress' else
                     [*say('Partial output')[:-1], fail('server_error')[-1]] if mode == 'disconnect' else read)
            dispatch = lambda i, **args: call('delegate_task', {'unit': 'stock', **args}, call_id=i)
            actions = [dispatch('premature', resume=True), dispatch('first'), child, dispatch('blind')]
            if mode == 'lost proof':
                read_output = main._outputs.read
                def inaccessible(ref):
                    if any(p['result'] == ref for p in case.get('handoff', {}).get('progress', [])):
                        raise OSError('Progress receipt unavailable')
                    return read_output(ref)
                monkeypatch.setattr(main._outputs, 'read', inaccessible)
                actions.append(dispatch('approved', resume=True))
            if mode == 'progress':
                actions.extend([dispatch('approved', resume=True), _report(evidence=['Stock file contains Four.']),
                                dispatch('check'), _report(evidence=['Count checked.'])])
            fake.script(*actions, say('Reviewed.'), say('Pending work acknowledged.'))
            assert main.approve_plan(main.saved.plan['id'])
            await finish_turn(main)
            assert 'normal frozen budget' in _output(main, 'premature')
            handoff = case['history'][0]['handoff']
            from desktop.core.fusion.registry import FusionRegistry
            saved = FusionRegistry.from_json(main.project, main.saved.fusion)
            assert next(iter(saved.plans.values()))['cases']['stock']['history'][0]['handoff'] == handoff
            if mode == 'progress':
                assert handoff['diagnostic'] == 'U04' and handoff['confirmation'] == 'explicit_resume'
                ref = handoff['progress'][0]['result']
                assert ref in handoff['evidenceRefs']
                proof = json.loads(main._outputs.read(ref))
                assert proof['revision'] == 1 and proof['stage'] == 'worker' and 'Four' in proof['observed']
                assert 'soft limit' in _output(main, 'first') and 'explicit resume' in _output(main, 'blind')
                assert case['next'] == 'done' and case['attempts'] == {'worker': 2, 'runner': 1}
                assert case['history'][0]['steps'] == 1 and case['handoff']['progress'][0]['path'] == 'stock.txt'
            else:
                assert main._fusion.registry.calls == 1 and case['attempts'] == {'worker': 1}
                if mode == 'disconnect':
                    assert handoff['diagnostic'] == 'U02' and handoff['inFlight'] == 'worker' and handoff['nextOwner'] == 'Coder'
                    assert case['next'] == 'worker' and 'explicit resume' in _output(main, 'blind')
                else:
                    assert case['handoff']['confirmation'] == 'new_plan_required' and case['handoff']['nextOwner'] == 'Main'
                    assert case['next'] == 'stopped'
                    assert ('hard limit' if mode == 'hard cap' else 'verified progress') in case['paused']
    asyncio.run(go())


def test_frozen_metrics_distinguish_missing_usage_mixed_planning_and_local_zero(tmp_path):
    from copy import deepcopy
    from desktop.core.fusion.metrics import frozen_summary
    from tests.test_fusion_debate_runtime import _declare

    async def go():
        async with _fusion(tmp_path, active=UNIT_GATES) as (main, fake):
            rt = main._fusion
            empty = rt.metrics_summary()['frozen_units']
            assert empty['plans'] == 0 and all(g['events'] == g['calls'] == 0 for g in empty['groups'].values())
            main.mode = 'spec'
            await _turn(main, fake, _declare('simple', 'plan'),
                        call('submit_work_plan', {'data': {'units': [_unit(review_required=False)]}},
                             text='Also answering a separate question.', used=usage(3000, 888, 50)),
                        call('submit_plan', {'plan': 'Verify the count.'}, used=usage(80, 16, 20)))
            planning = rt.metrics_summary()['frozen_units']['groups']['planning']
            assert planning['calls'] == 2 and planning['unknownUsageEvents'] == 1
            assert planning['tokens'] == {'input': None, 'output': None, 'cacheRead': None}
            assert planning['knownTokens'] == {'input': 60, 'output': 16, 'cacheRead': 20}
            no_bill = _report(evidence=['Count confirmed.'])
            del no_bill[-1]['response']['usage']
            fake.script(call('delegate_task', {'unit': 'stock'}), call('list_dir', {'directory_path': '.'}, used=usage(95, 6, 5)),
                        no_bill, call('delegate_task', {'unit': 'stock'}), _report(evidence=['Count tested.']), say('Checked.'))
            assert main.approve_plan(main.saved.plan['id'])
            await finish_turn(main)
            groups = rt.metrics_summary()['frozen_units']['groups']
            assert groups['execution']['events'] == 1 and groups['execution']['calls'] == 2
            assert groups['execution']['tokens'] == planning['tokens'] and groups['execution']['knownTokens'] == {'input': 90, 'output': 6, 'cacheRead': 5}
            assert groups['review']['tokens']['input'] == 120 and groups['exception']['calls'] == 0
            old = deepcopy(rt.registry.plans)
            plan, = old.values()
            del plan['planningUsage']
            for row in plan['cases']['stock']['history']:
                row.pop('meter')
                row.pop('meterKind')
            legacy = frozen_summary(old)['groups']
            assert legacy['planning']['tokens'] == legacy['review']['tokens'] == planning['tokens']
            assert legacy['review']['calls'] is None  # Old total/step count is not a provider bill.
    asyncio.run(go())


def test_changed_binding_hands_main_a_replan_not_a_replacement(tmp_path, monkeypatch):
    async def go():
        async with _fusion(tmp_path, active=UNIT_GATES) as (main, fake):
            rt = main._fusion
            case = await _prepare_single(main, fake)
            key, = rt.registry.plans
            original = json.dumps(rt.registry.plans[key]['spec'], sort_keys=True)
            monkeypatch.setattr(rt, '_actual_model', lambda _: 'different-native')
            fake.script(call('delegate_task', {'unit': 'stock'}, call_id='changed'),
                        call('delegate_task', {'unit': 'stock', 'resume': True}, call_id='retry'), say('Needs a new plan.'), say('Stopped.'))
            assert main.approve_plan(main.saved.plan['id'])
            await finish_turn(main)
            assert rt.registry.calls == 0 and not main.agents and case['next'] == 'stopped'
            local = rt.metrics_summary()['frozen_units']['groups']['exception']
            assert local['events'] == 1 and local['calls'] == local['unknownUsageEvents'] == 0
            assert local['tokens'] == {'input': 0, 'output': 0, 'cacheRead': 0}
            assert (case['handoff']['diagnostic'], case['handoff']['nextOwner'], case['handoff']['confirmation']) == (
                'U07', 'Main', 'new_plan_required')
            assert '[U07]' in _output(main, 'changed') and 'read_output' in _output(main, 'retry')
            assert json.dumps(rt.registry.plans[key]['spec'], sort_keys=True) == original
    asyncio.run(go())


def test_frozen_cancel_records_one_interruption_and_no_evidence(tmp_path):
    async def go():
        async with _fusion(tmp_path, active=UNIT_GATES) as (main, fake):
            case = await _prepare_single(main, fake)
            before = len(fake.requests)
            fake.script(call('delegate_task', {'unit': 'stock'}), [('hold', 'active'), *_report(evidence=['Not delivered'])])
            assert main.approve_plan(main.saved.plan['id'])
            try:
                await until(lambda: len(fake.requests) >= before + 2)
                from desktop.core.fusion.registry import FusionRegistry
                recovered = FusionRegistry.from_json(main.project, main._fusion.registry.to_json())
                from desktop.core.fusion.metrics import frozen_summary
                groups = frozen_summary(recovered.plans)['groups']
                assert groups['execution']['events'] == groups['execution']['unknownUsageEvents'] == 1
                assert groups['execution']['calls'] is None and groups['exception']['calls'] == 0
                assert frozen_summary(FusionRegistry.from_json(main.project, recovered.to_json()).plans)['groups'] == groups
                lost = next(iter(recovered.plans.values()))['cases']['stock']['handoff']
                assert (lost['cause'], lost['stage'], lost['expert']) == ('process interrupted', 'worker', next(iter(main.agents)))
                assert not lost['completed'] and not lost['progress']
                await main.interrupt()
                await finish_turn(main)
            finally:
                fake.release('active')
            assert main._fusion.registry.calls == 1 and case['attempts'] == {'worker': 1}
            assert 'reserved' not in case and not case['receipts']
            assert case['history'][0]['stop_reason'] == 'cancelled'
            h = case['handoff']
            assert (h['diagnostic'], h['unit'], h['completed'], h['inFlight']) == ('U06', 'stock', [], 'worker')
            assert h['remaining'] == ['worker', 'runner'] and not h['evidenceRefs']
    asyncio.run(go())


def _write(name, call_id):
    return call("create_file", {"file_path": name, "content": "route test"}, call_id=call_id)


def test_more_than_two_edits_of_its_own_must_go_to_a_coder(tmp_path):
    # A hard rule from the user after a real model kept a multi-step change for itself by calling it small.
    async def go():
        async with _fusion(tmp_path, active=ROUTE_GATES) as (main, fake):
            missed = call("edit_file", {"file_path": "a.txt", "old_str": "absent text", "new_str": "never written"}, call_id="failed")
            await _turn(main, fake, _route("self", "self_check"), _write("a.txt", "one"), missed, _write("b.txt", "two"),
                        _write("c.txt", "three"), _route("self", "self_check", call_id="again"), _write("c.txt", "still"),
                        _route(call_id="coder"), _delegate(), _report(), say("Handed over."))
            assert "Refused" not in _output(main, "one") and "Refused" not in _output(main, "two")
            assert "must go to a Coder" not in _output(main, "failed")  # A failed edit is not one of the two.
            assert "must go to a Coder" in _output(main, "three") and "must go to a Coder" in _output(main, "still")
            assert sorted(path.name for path in main.project.iterdir()) == ["a.txt", "b.txt"]
            assert "Coder-Inventory [done]" in _output(main, "delegate")
            # The count belongs to one user turn.
            await _turn(main, fake, _route("self", "self_check"), _write("d.txt", "next"), say("Done."))
            assert "Refused" not in _output(main, "next") and (main.project / "d.txt").is_file()
    asyncio.run(go())


def test_an_approved_plan_is_not_held_to_the_two_edit_limit(tmp_path):
    async def go():
        async with _fusion(tmp_path, active=ROUTE_GATES) as (main, fake):
            main.saved.plan = {"id": "plan", "status": "approved", "text": "Write three notes."}
            await _turn(main, fake, _write("a.txt", "one"), _write("b.txt", "two"), _write("c.txt", "three"), say("Done."))
            assert len(list(main.project.iterdir())) == 3
    asyncio.run(go())