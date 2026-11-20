from __future__ import annotations

import asyncio

import pytest

from desktop.core.fusion.acp import AcpRun
from desktop.core.fusion.acp_server import PREFIX
from desktop.core.fusion.drive.retention import Retention
from desktop.core.fusion.protocol import REPORT_DECLARATION, REPORT_TOKENS, parse_report
from tests.app_support import ready_core
from tests.fake_responses import call, fail, say
from tests.fakes import until
from tests.fusion_acp_support import make_guarded_engine
from tests.test_fusion_identity_runtime import DEVIN, LIGHT, MAIN, WORKER, _brief, _fusion, _gates, _identity_folder, _manual_steps, _output, _turn

_ACTIVE = ("drive", "brief", "report")
pytestmark = pytest.mark.fusion_gates(*_ACTIVE)

_REPORT = {"status": "done", "summary": "Verified the stock totals.", "evidence": ["inventory.txt: counts match"],
           "notes": ["Keep counts in one place."], "ratings": []}


def _delegate(**overrides):
    return call("delegate_task", _brief(**overrides), call_id="delegate")


def _submit(data=None, *, call_id="report", text=None):
    return call("submit_report", _REPORT if data is None else data, call_id=call_id, text=text)


def _kept(drive, kind):
    return [item.record for item in drive.candidates(shared=False, retention=Retention()) if item.record.kind == kind]


def _assert_order(text, *parts):
    offsets = [text.index(part) for part in parts]
    assert offsets == sorted(offsets) and len(set(offsets)) == len(parts)


def test_accepted_report_ends_turn_and_retains_the_report_and_final_text(tmp_path):
    async def go():
        async with _fusion(tmp_path, active=_ACTIVE) as (main, fake):
            await _turn(main, fake, _delegate(), _submit(text="Inspection complete."), say("Reviewed."))
            agent, = main.agents.values()
            child = agent.controller
            assert {item["name"] for item in fake.requests[0]["tools"]}.isdisjoint({"submit_report"})
            manifest = next(item for item in fake.requests[1]["tools"] if item["name"] == "submit_report")
            assert all(manifest[key] == value for key, value in REPORT_DECLARATION.items())
            assert len(fake.requests) == 3 and len(child.saved.turns) == 1
            assert child.last_turn_end.reason == "report" and not child.saved.plan
            assert "accepted" in _output(child, "report")
            result = _output(main, "delegate")
            _assert_order(result, "Coder-Inventory [done]", _REPORT["summary"], _REPORT["evidence"][0],
                          "The main agent decides at acceptance", _REPORT["notes"][0], "Tool-result previews", "Fusion Drive report")
            drive = main._fusion.drive
            record, = await asyncio.to_thread(_kept, drive, "report")
            assert record.summary == "Coder-Inventory [done]: " + _REPORT["summary"]
            body = await asyncio.to_thread(drive.read, record.pointers[0])
            _assert_order(body, _REPORT["summary"], _REPORT["evidence"][0], _REPORT["notes"][0], "Ratings:", "Expert final text:\nInspection complete.")
            assert record.id in result and not main._fusion.report_runs
    asyncio.run(go())


@pytest.mark.parametrize("over_limit", [False, True])
def test_invalid_report_returns_parser_problems_then_accepts_a_correction_in_the_same_turn(tmp_path, over_limit):
    async def go():
        async with _fusion(tmp_path, active=_ACTIVE) as (main, fake):
            if over_limit:
                bad = {**_REPORT, "summary": "Stock totals. " * REPORT_TOKENS}
            else:
                bad = {key: value for key, value in _REPORT.items() if key != "notes"}
            await _turn(main, fake, _delegate(), _submit(bad, call_id="bad"), _submit(), say("Reviewed."))
            child = next(iter(main.agents.values())).controller
            problems = parse_report(bad, ())[1]
            assert problems and all(problem in _output(child, "bad") for problem in problems)
            assert child.saved.tools["bad"]["status"] == "error"
            assert len(child.saved.turns) == 1 and "accepted" in _output(child, "report")
    asyncio.run(go())


@pytest.mark.parametrize("submit_on_retry", [True, False])
def test_text_only_end_gets_one_follow_up_then_uses_report_or_fallback(tmp_path, submit_on_retry):
    async def go():
        async with _fusion(tmp_path, active=_ACTIVE) as (main, fake):
            await _turn(main, fake, _delegate(), say("Initial expert answer."),
                        _submit() if submit_on_retry else say("Fallback expert answer."), say("Reviewed."))
            child = next(iter(main.agents.values())).controller
            assert len(fake.requests) == 4 and len(child.saved.turns) == 2
            assert "No structured report was submitted" in str(fake.requests[2]["input"])
            result = _output(main, "delegate")
            if submit_on_retry:
                assert "Coder-Inventory [done]" in result and _REPORT["summary"] in result
                assert "not structured" not in result
            else:
                assert "Coder-Inventory [concerns]" in result and "Fallback expert answer." in result
                assert "The report was not structured" in result
    asyncio.run(go())


def test_reused_coder_does_not_inherit_report_or_follow_up_count(tmp_path):
    async def go():
        async with _fusion(tmp_path, active=_ACTIVE) as (main, fake):
            for number in (1, 2):
                report = {**_REPORT, "summary": f"Verified stock on attempt {number}."}
                ident = f"delegate-{number}"
                await _turn(main, fake, call("delegate_task", _brief(), call_id=ident), say("Unstructured first answer."),
                            _submit(report, call_id=f"report-{number}"), say("Reviewed."))
                assert report["summary"] in _output(main, ident)
            agent, = main.agents.values()
            assert len(agent.controller.saved.turns) == 4 and len(fake.requests) == 8
            assert "Verified stock on attempt 1." not in _output(main, "delegate-2")
    asyncio.run(go())


@pytest.mark.parametrize("neutral", [False, True])
def test_read_record_requires_a_rating_and_settles_each_record_once(tmp_path, neutral):
    async def go():
        async with _fusion(tmp_path, active=_ACTIVE) as (main, fake):
            drive = main._fusion.drive
            source = await asyncio.to_thread(drive.put, "artifact", "inventory", "Inventory sample", body="There are 4 parts.")
            valid = {"record": source.id, "score": 1, "reason": "It supported the inventory check."}
            stages = []
            if neutral:
                stages.extend([say("Partial report."), _submit(call_id="still-missing"), say("No extra rating.")])
            else:
                for score in (-1, 1):
                    stages.append(_submit({**_REPORT, "ratings": [{**valid, "score": score, "reason": ""}]}, call_id=f"no-reason-{score}"))
                stages.append(_submit({**_REPORT, "ratings": [valid]}))
            await _turn(main, fake, _delegate(), call("read_record", {"pointer": source.pointers[0]}, call_id="read"),
                        _submit(call_id="missing"), *stages, say("Reviewed."))
            child = next(iter(main.agents.values())).controller
            assert f"Fusion Drive record ID (for report ratings): {source.id}" in _output(child, "read")
            assert f"Missing rating for record '{source.id}'" in _output(child, "missing")
            result = _output(main, "delegate")
            assert "Coder-Inventory [done]" in result and "The report was not structured" not in result
            stats = await asyncio.to_thread(drive.stats, Retention())
            stored = await asyncio.to_thread(drive.get, source.id)
            if neutral:
                assert len(child.saved.turns) == 2 and "Missing rating for record" in str(fake.requests[4]["input"])
                assert stats["unrated"] == 1 and stats["ratings"] == {-1: 0, 0: 1, 1: 0}
                assert stored.q == 0
            else:
                for score in (-1, 1):
                    assert "needs a reason" in _output(child, f"no-reason-{score}")
                assert stats["unrated"] == 0 and stats["ratings"] == {-1: 0, 0: 0, 1: 1}
                assert stored.q == pytest.approx(0.1)
    asyncio.run(go())


def test_report_gate_off_preserves_the_final_text_and_tool_list(tmp_path):
    async def go():
        async with _fusion(tmp_path, active=("drive", "brief")) as (main, fake):
            await _turn(main, fake, _delegate(), say("Usual expert text."), say("Reviewed."))
            assert "submit_report" not in {item["name"] for item in fake.requests[1]["tools"]}
            result = _output(main, "delegate")
            assert "reported:\nUsual expert text." in result and "not structured" not in result
            assert len(fake.requests) == 3
    asyncio.run(go())


def test_expert_without_identity_is_unchanged_even_with_report_gate_on(tmp_path):
    async def go():
        async with _fusion(tmp_path, active=_ACTIVE) as (main, fake):
            fake.script(say("Old expert text."))
            result = await main._fusion.delegate("Inventory", WORKER, "worker", "Read totals.", ("inventory.txt",))
            assert not result.is_error and "reported:\nOld expert text." in result.output
            assert len(fake.requests) == 1 and "submit_report" not in {item["name"] for item in fake.requests[0]["tools"]}
    asyncio.run(go())


@pytest.mark.parametrize("step_limit", [False, True])
def test_failed_or_over_budget_expert_returns_no_report_and_is_not_retried(tmp_path, step_limit):
    async def go():
        async with _fusion(tmp_path, active=_ACTIVE) as (main, fake):
            if step_limit:
                _manual_steps(main)
            delegate = _delegate(budget={"steps": 1}) if step_limit else _delegate()
            worker = call("list_dir", {"directory_path": "."}, call_id="read") if step_limit else fail("bad_request")
            await _turn(main, fake, delegate, worker, say("Reviewed."))
            expert = next(iter(main.agents.values()))
            assert expert.record.status == ("interrupted" if step_limit else "failed") and len(fake.requests) == 3
            assert "Fusion Drive report" not in _output(main, "delegate")
            drive = main._fusion.drive
            assert not await asyncio.to_thread(_kept, drive, "report")
            assert not (await asyncio.to_thread(_kept, drive, "brief"))[0].open
    asyncio.run(go())


def test_devin_report_has_its_own_early_end_reason(tmp_path, monkeypatch):
    original = AcpRun.dispatch
    delivered = []

    async def report_from_fake(self, method, params):
        # The fake process has a scripted read, not a report action. Reuse its real MCP envelope.
        if method == "tools/call" and params.get("name") == "read_file":
            manifest = (await original(self, "tools/list", {}))["tools"]
            schema = next(item["inputSchema"] for item in manifest if item["name"] == "submit_report")
            assert schema == REPORT_DECLARATION["parameters"] and self._allows_tool(PREFIX + "submit_report", _REPORT)
            result = await original(self, method, {**params, "name": "submit_report", "arguments": _REPORT})
            delivered.append(result)
            return result
        return await original(self, method, params)

    monkeypatch.setattr(AcpRun, "dispatch", report_from_fake)

    async def go():
        _identity_folder()
        harness = make_guarded_engine(tmp_path)
        core, model, _, _, _ = await ready_core(tmp_path, devin_engine=harness.engine)
        try:
            await until(lambda: any(item["source"] == "devin" for item in core.catalog.models), timeout=20)
            core.settings.fusion_gates = _gates(*_ACTIVE)
            core.settings.fusion_main = MAIN
            core.settings.fusion_workers = [DEVIN]
            core.settings.fusion_light = LIGHT
            cid = (await core.call("session.new", {"projectId": str(tmp_path / "proj"), "fusion": True}))["id"]
            main = core._by_cid[cid].controller
            await _turn(main, model, _delegate(goal="[fusion-read] Check stock."), say("Reviewed."))
            child = next(iter(main.agents.values())).controller
            assert len(delivered) == 1 and "accepted" in delivered[0]["content"][0]["text"]
            assert child.last_turn_end.reason == "report" and not child.saved.plan and child.mode == "auto"
            assert "Coder-Inventory [done]" in _output(main, "delegate") and len(child.saved.turns) == 1
        finally:
            await core.shutdown()
    asyncio.run(go())
