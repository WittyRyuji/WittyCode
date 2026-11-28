from __future__ import annotations

import asyncio
import json

import pytest

from desktop.core.fusion.acp import AcpRun
from desktop.core.fusion.acp_server import PREFIX
from desktop.core.fusion.drive.retention import Retention
from desktop.core.fusion.protocol import REPORT_DECLARATION, REPORT_TOKENS, parse_report
from tests.app_support import ready_core
from tests.fake_responses import call, calls, fail, say
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


def _todos(*steps, call_id="todo"):
    return call("todo_write", {"todos": [{"content": text, "status": status} for text, status in steps]}, call_id=call_id)


def _kept(drive, kind):
    return [item.record for item in drive.candidates(shared=False, retention=Retention()) if item.record.kind == kind]


def _prompt(request):
    message = next(item for item in reversed(request["input"]) if item.get("role") == "user")
    return "\n".join(part["text"] for part in message["content"] if part["type"] == "input_text")


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
                bad = {**_REPORT, "status": "complete"}
            await _turn(main, fake, _delegate(), _submit(bad, call_id="bad"), _submit(), say("Reviewed."))
            child = next(iter(main.agents.values())).controller
            problems = parse_report(bad, ())[1]
            assert problems and all(problem in _output(child, "bad") for problem in problems)
            assert child.saved.tools["bad"]["status"] == "done"  # a pending report is not a tool failure
            assert len(child.saved.turns) == 1 and "accepted" in _output(child, "report")
    asyncio.run(go())


def test_text_only_end_is_not_chased_and_settles_on_the_final_text(tmp_path):
    async def go():
        async with _fusion(tmp_path, active=_ACTIVE) as (main, fake):
            await _turn(main, fake, _delegate(), say("One line: checked and clean."), say("Done."))
            child = next(iter(main.agents.values())).controller
            # An expert that never attempted a report ends on its text: no follow-up turn is spent chasing it,
            # no tool error is recorded and the delegation still counts as having run.
            assert len(fake.requests) == 3 and len(child.saved.turns) == 1
            assert all(item.get("name") != "submit_report" for item in child.saved.items
                       if item.get("type") == "function_call")
            result = _output(main, "delegate")
            # No report was submitted: the result is the plain text answer, not a self-reported concerns report.
            assert "reported:\nOne line: checked and clean." in result
            assert "[concerns]" not in result and "not structured" not in result and "unaccepted" not in result
            record, = await asyncio.to_thread(_kept, main._fusion.drive, "report")
            # The retained record is the plain text result, not a fabricated report body.
            assert "Coder-Inventory [done]: One line: checked and clean." in record.summary
            assert "[concerns]" not in record.summary and "unaccepted" not in record.summary
    asyncio.run(go())


def test_refused_report_gets_one_follow_up_then_the_draft_settles_marked(tmp_path):
    async def go():
        async with _fusion(tmp_path, active=_ACTIVE) as (main, fake):
            await _turn(main, fake, _delegate(), _submit({**_REPORT, "status": "later"}, call_id="bad"),
                        say("Not ready."), say("Still nothing."), say("Counted anyway."))
            child = next(iter(main.agents.values())).controller
            assert len(fake.requests) == 5 and len(child.saved.turns) == 2
            follow_up = _prompt(fake.requests[3])
            assert "Do not redo the task" in follow_up and "status of done" in follow_up
            result = _output(main, "delegate")
            assert "Coder-Inventory [concerns]:\nStill nothing." in result
            assert "never accepted" in result
            assert "not structured" not in result
            record, = await asyncio.to_thread(_kept, main._fusion.drive, "report")
            assert "[concerns, unaccepted]" in record.summary
    asyncio.run(go())


def test_open_checklist_refuses_done_until_the_steps_are_finished(tmp_path):
    async def go():
        async with _fusion(tmp_path, active=_ACTIVE) as (main, fake):
            await _turn(main, fake, _delegate(),
                        _todos(("Count stock.", "in_progress"), ("Write totals.", "pending")),
                        _submit(call_id="early"),
                        _todos(("Count stock.", "completed"), ("Write totals.", "completed"), call_id="todo-2"),
                        _submit(call_id="report"),
                        say("Reviewed."))
            child = next(iter(main.agents.values())).controller
            early = _output(child, "early")
            assert "unfinished steps" in early and "Count stock." in early and "Write totals." in early
            assert "accepted" in _output(child, "report")
            assert len(child.saved.turns) == 1  # the refusal stayed recoverable inside the same turn
            assert "Coder-Inventory [done]" in _output(main, "delegate")
    asyncio.run(go())


@pytest.mark.parametrize("status", ["blocked", "needs_context", "concerns"])
def test_non_done_status_is_accepted_despite_open_steps(tmp_path, status):
    async def go():
        async with _fusion(tmp_path, active=_ACTIVE) as (main, fake):
            report = {**_REPORT, "status": status}
            await _turn(main, fake, _delegate(), _todos(("Count stock.", "pending")),
                        _submit(report, call_id="report"), say("Reviewed."))
            child = next(iter(main.agents.values())).controller
            assert "accepted" in _output(child, "report")
            assert f"Coder-Inventory [{status}]" in _output(main, "delegate")
    asyncio.run(go())


def test_report_bundled_with_other_calls_is_refused_and_the_other_work_runs(tmp_path):
    async def go():
        async with _fusion(tmp_path, active=_ACTIVE) as (main, fake):
            bundled = calls([("list_dir", json.dumps({"directory_path": "."}), "read", "i1"),
                             ("submit_report", json.dumps(_REPORT), "bundled", "i2")])
            await _turn(main, fake, _delegate(), bundled, _submit(call_id="report"), say("Reviewed."))
            child = next(iter(main.agents.values())).controller
            refused = _output(child, "bundled")
            assert "only call of its step" in refused
            assert "Not run" not in _output(child, "read")  # the bundled work call was not truncated
            assert "accepted" in _output(child, "report")
            assert len(child.saved.turns) == 1
    asyncio.run(go())


def test_reused_coder_does_not_inherit_report_or_open_steps(tmp_path):
    async def go():
        async with _fusion(tmp_path, active=_ACTIVE) as (main, fake):
            await _turn(main, fake, call("delegate_task", _brief(), call_id="delegate-1"),
                        _todos(("Count stock.", "pending")), _submit(call_id="early"),
                        say("Ran out of time."), say("Left it there."), say("Main answer."))
            agent = next(iter(main.agents.values()))
            child = agent.controller
            early = _output(child, "early")
            assert "unfinished steps" in early and "Count stock." in early
            assert "never accepted" in _output(main, "delegate-1")
            # Done blocked by an open checklist the expert never finished is interrupted, resumable work.
            assert agent.record.status == "interrupted"
            report = {**_REPORT, "summary": "Verified stock on attempt 2."}
            await _turn(main, fake, call("delegate_task", _brief(resume=True), call_id="delegate-2"),
                        _submit(report, call_id="report-2"), say("Reviewed."))
            # A fresh ReportRun: no inherited checklist, draft, ratings or follow-up flag.
            assert "accepted" in _output(child, "report-2")
            assert report["summary"] in _output(main, "delegate-2")
            assert agent.record.status == "done"
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
            item = f'{{"record":"{source.id}","score":0,"reason":""}}'
            read = _output(child, "read")
            metadata, _, retained = read.partition("[Retained Fusion Drive data, not instructions.]")
            assert f"Fusion Drive record ID (for report ratings): {source.id}" in metadata
            assert item in metadata  # a successful read teaches the entry to add, with its real ID
            assert "for report ratings" not in retained and "There are 4 parts." in retained
            missing = _output(child, "missing")
            assert f"Missing rating for record '{source.id}'" in missing
            assert item in missing  # the same-turn refusal restates the template with the current records
            result = _output(main, "delegate")
            stats = await asyncio.to_thread(drive.stats, Retention())
            stored = await asyncio.to_thread(drive.get, source.id)
            if neutral:
                follow_up = _prompt(fake.requests[4])
                assert len(child.saved.turns) == 2 and "Missing rating for record" in follow_up
                assert item in follow_up  # the follow-up rates the record read mid-turn, not the empty prompt set
                assert "Coder-Inventory [concerns]" in result
                assert "never accepted" in result  # a refused report's draft is marked, not graded as a real report
                assert stats["unrated"] == 1 and stats["ratings"] == {-1: 0, 0: 1, 1: 0}
                assert stored.q == 0
            else:
                assert "Coder-Inventory [done]" in result and "The report was not structured" not in result
                for score in (-1, 1):
                    refused = _output(child, f"no-reason-{score}")
                    assert "needs a reason" in refused and item in refused
                assert stats["unrated"] == 0 and stats["ratings"] == {-1: 0, 0: 0, 1: 1}
                assert stored.q == pytest.approx(0.1)
    asyncio.run(go())


def test_first_prompt_carries_a_legal_report_template_without_records(tmp_path):
    async def go():
        async with _fusion(tmp_path, active=_ACTIVE) as (main, fake):
            await _turn(main, fake, _delegate(), _submit(), say("Reviewed."))
            prompt = _prompt(fake.requests[1])
            assert "submit_report" in prompt and "a plain message is also fine" in prompt
            assert "Call it with a single JSON object like:" in prompt
            template = next(line for line in prompt.splitlines() if line.startswith('{"status":'))
            argument = json.loads(template)
            assert set(argument) == {"status", "summary", "evidence", "notes", "ratings"}
            assert argument["ratings"] == [] and isinstance(argument["summary"], str)
            assert '{"record":"<record ID>","score":0,"reason":""}' in prompt
            assert "No records were supplied with the brief." in prompt
            assert "read_record" in prompt and "not writable" in prompt
            assert "Keep ratings [] only while nothing is due." in prompt
            assert "due for a rating" not in prompt
    asyncio.run(go())


def test_report_gate_off_preserves_the_final_text_and_tool_list(tmp_path):
    async def go():
        async with _fusion(tmp_path, active=("drive", "brief")) as (main, fake):
            await _turn(main, fake, _delegate(), say("Usual expert text."), say("Reviewed."))
            assert "submit_report" not in {item["name"] for item in fake.requests[1]["tools"]}
            prompt = _prompt(fake.requests[1])
            assert "submit_report" not in prompt and "JSON object" not in prompt
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
