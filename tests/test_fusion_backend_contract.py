"""Independent one-backend lifecycles: naming, work, reopening and tool-free source handoff."""
import asyncio
from collections import Counter
import json

import pytest

from desktop.core import factory_guard
from desktop.core.app_core import AppError
from tests.chatgpt_support import finish_turn, until
from tests.fake_responses import say
from tests.fusion_backend_support import PROFILES, RUNNERS, configure, factory_turns, peers
from tests.test_app_api import says


@pytest.mark.parametrize("runner", RUNNERS)
def test_single_backend_lifecycle(tmp_path, monkeypatch, runner):
    async def scenario():
        async with peers(tmp_path, monkeypatch) as (core, models, plan, api):
            await configure(core, models, runner, runner, runner)
            project = str(tmp_path / "proj")
            cid = (await core.call("session.new", {"projectId": project, "fusion": True}))["id"]
            if runner == "plan":
                plan.script(say("First local answer."), say("Second local answer."), say("Plan source summary."))
            if runner == "api":
                api.script(says("First local answer."), says("Second local answer."), says("API source summary."))
            main = core._by_cid[cid].controller
            assert (await core.call("session.send", {"id": cid, "text": "Inspect the first sample"}))["ok"]
            await finish_turn(main)
            titles = {"plan": plan.title_answer, "api": "Demo title", "devin": "库存事务与去重", "factory": "线协议验收"}
            assert main.title == titles[runner] and len(main.saved.turns) == 1 and main.saved.turns[0].status == "done"
            row = "cg:" + main.session_id
            await core.call("session.close", {"id": cid})
            await until(lambda: main.state.value == "closed")
            cid = (await core.call("session.openSaved", {"projectId": project, "id": row}))["id"]
            reopened = core._by_cid[cid].controller
            assert reopened is not main and reopened._fusion.registry.policy.main == models[runner]
            assert reopened.title == titles[runner]
            assert (await core.call("session.send", {"id": cid, "text": "Inspect the second sample"}))["ok"]
            await finish_turn(reopened)
            assert [turn.status for turn in reopened.saved.turns] == ["done", "done"]
            summary = await reopened.compact_for_switch(64000)
            assert summary.startswith({"plan": "Plan source summary", "api": "API source summary", "devin": "ACP summary", "factory": "Factory handoff summary"}[runner])
            assert reopened.saved.compaction["summary"] == summary
            native, effort = PROFILES[runner]
            plan_calls = [*plan.title_requests, *plan.requests]
            api_calls = [body for _, _, body in [*api.title_posts, *api.posts]]
            assert [(r["model"], r["reasoning"]["effort"]) for r in plan_calls] == [(native, effort)] * (4 if runner == "plan" else 0)
            assert [(r["model"], r["reasoning_effort"]) for r in api_calls] == [(native, effort)] * (4 if runner == "api" else 0)
            devin_calls = [json.loads(line) for path in (tmp_path / "guarded").rglob("received.jsonl") for line in path.read_text(encoding="utf-8").splitlines()]
            assert sum(m.get("method") == "session/prompt" for m in devin_calls) == (4 if runner == "devin" else 0)
            if runner == "devin":
                assert any(m.get("method") == "session/load" for m in devin_calls)
                options = Counter((m["params"]["configId"], m["params"]["value"]) for m in devin_calls if m.get("method") == "session/set_config_option")
                assert options == {("model", native): 4, ("thought_level", effort): 4}
            turns = factory_turns(tmp_path)
            assert len(turns) == (4 if runner == "factory" else 0)
            if turns:
                assert Counter(t["action"] for t in turns) == {"answer": 2, "summary": 1, "title": 1}
                assert all((t["model"], t["effort"]) == (native, effort) and bool(t["tools"]) == (t["action"] == "answer") for t in turns)
                second, = [t for t in turns if t["action"] == "answer" and "second sample" in t["prompt"]]
                assert "first sample" in second["prompt"] and "Factory local answer." in second["prompt"]
    asyncio.run(scenario())


@pytest.mark.fusion_gates("drive", "brief", "report", "route", "acceptance")
@pytest.mark.parametrize("runner", RUNNERS)
def test_incomplete_check_retries_on_its_original_backend(tmp_path, monkeypatch, runner):
    from tests.test_fusion_acceptance_runtime import _delegate
    from tests.test_fusion_identity_runtime import _output
    from tests.fusion_backend_support import api_call
    from tests.fake_responses import call

    async def scenario():
        async with peers(tmp_path, monkeypatch) as (core, models, plan, api):
            project = tmp_path / "proj"
            (project / "evidence.txt").write_text("Counted 7 units.", encoding="utf-8")
            await configure(core, models, "plan", runner, runner)
            cid = (await core.call("session.new", {"projectId": str(project), "fusion": True}))["id"]
            main = core._by_cid[cid].controller
            plan.script(_delegate("Runner", call_id="check", goal="[fusion-recheck] Check count.", budget={"steps": 16, "seconds": 20}))
            read = {"file_path": "evidence.txt"}
            report = {"status": "done", "summary": "Count confirmed.", "evidence": ["evidence.txt: Counted 7 units."]}
            if runner == "plan":
                plan.script(call("read_file", read), say("Count inspected; no report."), call("submit_report", report))
            elif runner == "api":
                api.script(api_call(read, "read_file", "inspect"), says("Count inspected; no report."), api_call(report, "submit_report", "checked"))
            plan.script(say("Verified the report."))
            assert (await core.call("session.send", {"id": cid, "text": "Check evidence"}))["ok"]
            await finish_turn(main)
            receipt = _output(main, "check")
            assert "Check attempt 1: missing evidence" in receipt and "16 steps, 20 seconds" in receipt, receipt
            assert "32 steps, 40 seconds" in receipt and "evidence.txt: Counted 7 units." in receipt
            expert, = main.agents.values()
            assert expert.record.status == "done" and main._fusion.registry.calls == 2
            child = expert.controller
            assert len(main.saved.turns) == 1 and len(child.saved.turns) == 2
            assert [t.model for t in child.saved.turns] == [child.model] * 2 and expert.record.model == models[runner]
            assert [i['name'] for i in child.saved.items if i.get('type') == 'function_call'] == ['read_file', 'submit_report']
            assert len(plan.requests) == 2 + 3 * (runner == "plan") and len(api.posts) == 3 * (runner == "api")
            assert Counter(t['action'] for t in factory_turns(tmp_path)) == ({'expert': 1, 'recovered': 1} if runner == 'factory' else {})
            logs = (p.read_text(encoding='utf-8') for p in (tmp_path / 'guarded').rglob('received.jsonl'))
            assert sum(entry.count('"session/prompt"') for entry in logs) == 2 * (runner == 'devin')
    asyncio.run(scenario())


@pytest.mark.fusion_gates("v3")
def test_working_factory_debate_seat_uses_its_own_tool_free_connection(tmp_path, monkeypatch):
    from tests.test_fusion_debate_runtime import _declare, _output

    async def scenario():
        async with peers(tmp_path, monkeypatch) as (core, models, plan, api):
            await configure(core, models, "plan", "factory", "api")
            await core.call("settings.set", {"key": "fusionPreference", "value": 75})
            cid = (await core.call("session.new", {"projectId": str(tmp_path / "proj"), "fusion": True}))["id"]
            main = core._by_cid[cid].controller
            plan.script(_declare("complex", "discussion", "Assess local evidence"),
                        say('{"conclusion":"Inspect the file first.","open_issue":""}'), say("Review complete."))
            assert (await core.call("session.send", {"id": cid, "text": "Discuss the evidence"}))["ok"]
            await finish_turn(main)
            assert "Inspect the file first." in _output(main, "declare")
            debate, = factory_turns(tmp_path)
            assert (debate['action'], debate['tools'], debate['model'], debate['effort']) == ('debate', [], *PROFILES['factory'])
            assert len(plan.requests) == 3 and "The local evidence is incomplete." in str(plan.requests[1]['input'])
            assert not api.posts
    asyncio.run(scenario())


@pytest.mark.fusion_gates("drive", "brief", "report", "observer")
def test_light_factory_observer_has_no_tools_or_plan_fallback(tmp_path, monkeypatch):
    from tests.test_fusion_observer_runtime import _delegate, _failed_reads, _output

    async def scenario():
        async with peers(tmp_path, monkeypatch) as (core, models, plan, api):
            await configure(core, models, "plan", "plan", "factory")
            core.settings.fusion_observer_judgement = True
            cid = (await core.call("session.new", {"projectId": str(tmp_path / "proj"), "fusion": True}))["id"]
            main = core._by_cid[cid].controller
            reads = _failed_reads()
            plan.script(_delegate(), *reads, say("Paused."))
            assert (await core.call("session.send", {"id": cid, "text": "Review progress"}))["ok"]
            await finish_turn(main)
            assert _output(main, "delegate").startswith("Observer report (stuck)")
            assert [(t['action'], t['tools'], t['effort']) for t in factory_turns(tmp_path)] == [('observer', [], 'low')] * 2
            assert len(plan.requests) == len(reads) + 2 and not api.posts
    asyncio.run(scenario())


def test_factory_disconnect_is_not_a_local_refusal_or_replay(tmp_path, monkeypatch):
    from desktop.core.catalog import Catalog
    from desktop.core.settings import normalize_folder

    async def scenario():
        async with peers(tmp_path, monkeypatch) as (core, models, plan, api):
            folder = str(tmp_path / "proj")
            await configure(core, models, "factory", "factory", "factory")
            cid = (await core.call("session.new", {"projectId": folder, "fusion": True}))["id"]
            main = core._by_cid[cid].controller
            assert (await core.call("session.send", {"id": cid, "text": "First delivery"}))["ok"]
            await finish_turn(main)
            with monkeypatch.context() as denied:
                denied.delenv("FACTORY_API_KEY")
                assert (await core.call("session.send", {"id": cid, "text": "Unsent question"}))["ok"]
                await finish_turn(main)
            assert [t.status for t in main.saved.turns] == ['done'] and 'Unsent question' not in str(main.saved.items)
            assert not main._archive_broken and len(factory_turns(tmp_path)) == 2
            assert (await core.call("session.send", {"id": cid, "text": "[drop-wire]"}))["ok"]
            await finish_turn(main)
            assert [t.status for t in main.saved.turns] == ['done', 'failed'] and '[drop-wire]' in str(main.saved.items)
            assert not main._archive_broken and main._fusion.registry.had_unknown
            row = 'cg:' + main.session_id
            await core.call('session.close', {'id': cid})
            await until(lambda: main.state.value == 'closed')
            # A native-only installation still lists the host-owned Fusion record, without a Plan login or API models.
            core.settings.chatgpt_enabled = core.settings.devin_enabled = False
            core.catalog = Catalog.from_items([r for r in core.catalog.models if r['source'] == 'factory'])
            await core._reload_project(folder)
            assert any(r.id == row and r.fusion for r in core._saved[normalize_folder(folder)])
            cid = (await core.call('session.openSaved', {'projectId': folder, 'id': row}))['id']
            reopened = core._by_cid[cid].controller
            assert reopened.model == models['factory'] and [t.status for t in reopened.saved.turns] == ['done', 'failed']
            assert not reopened._archive_broken and not (api.posts or plan.requests or core.factory.controllers)
            assert Counter(t['action'] for t in factory_turns(tmp_path)) == {'answer': 1, 'disconnect': 1, 'title': 1}
    asyncio.run(scenario())


@pytest.mark.parametrize("missing", ["policy", "auth"])
def test_factory_guard_refuses_title_without_dispatch_or_fallback(tmp_path, monkeypatch, missing):
    async def scenario():
        async with peers(tmp_path, monkeypatch) as (core, models, plan, api):
            await configure(core, models, "factory", "factory", "factory")
            if missing == "policy":
                monkeypatch.setattr(factory_guard, "_SYSTEM_POLICY", tmp_path / "absent.json")
            else:
                monkeypatch.delenv("FACTORY_API_KEY")
            cid = (await core.call("session.new", {"projectId": str(tmp_path / "proj"), "fusion": True}))["id"]
            with pytest.raises(AppError, match="机器托管策略" if missing == "policy" else "FACTORY_API_KEY"):
                await core.call("session.send", {"id": cid, "text": "Never dispatch"})
            main = core._by_cid[cid].controller
            assert main._fusion.registry.policy.main == models["factory"] and not main.saved.turns
            assert not (factory_turns(tmp_path) or api.posts or api.title_posts or plan.requests or plan.title_requests or core.factory.controllers)
            assert not list((tmp_path / "guarded").rglob("received.jsonl"))
    asyncio.run(scenario())
