"""A failed check recovers inside its original tool call, never by asking the main model to replay the batch."""
from __future__ import annotations

import asyncio
import json
import os
from dataclasses import replace
from pathlib import Path

import pytest

from tests.chatgpt_support import finish_turn
from tests.fake_responses import call, fail, say
from tests.fakes import until
from tests.test_fusion_acceptance_runtime import GATES, _continuations, _declare_rename, _delegate, _l3_rename_route, _verdict
from tests.test_fusion_identity_runtime import _fusion, _manual_steps, _output, _turn
from tests.test_fusion_route_runtime import _report, _route

pytestmark = pytest.mark.fusion_gates(*GATES)


def _evidence():
    return call("submit_report", {"status": "done", "summary": "Inventory check complete.",
                                  "evidence": ["Listed the delivery directory: inventory.txt exists."]})


@pytest.mark.parametrize(("identity", "reason"), [("Runner", "timeout"), ("QualitySampler", "step limit")])
def test_only_failed_seat_recovers_in_its_original_delegation(tmp_path, identity, reason):
    async def go():
        async with _fusion(tmp_path, active=(*GATES, "v3"), max_seconds=1) as (main, fake):
            rt = main._fusion
            rt.registry.policy = replace(rt.registry.policy, max_seconds_adaptive=True)
            _manual_steps(main, 1, 5)
            second_seat = identity == "QualitySampler"
            ls = call("list_dir", {"directory_path": "."}, call_id="inspect")
            before = [_delegate("Runner", call_id="runner", budget={"steps": 2}), ls, _evidence()] if second_seat else []
            check = _delegate(identity, call_id="check", budget={"steps": 1 if second_seat else 2, "seconds": 20 if second_seat else 1})
            write = call("create_file", {"file_path": "inventory.txt", "content": "Counted."})
            # The recovered Runner takes longer than its first deadline; the recovered QS has a second step.
            first = [("hold", "lost"), *say("Late.")] if not second_seat else ls
            read = call("read_file", {"file_path": "inventory.txt"}, call_id="inspect-file")
            recovered = [[("sleep", 1.2), *ls], _evidence()] if not second_seat else [read, _evidence()]
            try:
                await _turn(main, fake, _declare_rename(), _l3_rename_route("runner_and_sampler" if second_seat else "runner", "route"),
                            _delegate(), write, _report(), *before, check, first, *recovered, say("Finished."))
            finally:
                fake.release("lost")
            assert len(main.saved.turns) == 1 and not _continuations(main)
            assert (main.project / "inventory.txt").read_text(encoding="utf-8") == "Counted."
            assert rt.registry.calls == 3 + int(second_seat)
            checker, = [a for a in main.agents.values() if a.record.identity == identity]
            assert len(checker.controller.saved.turns) == 2 and not checker.record.paths
            assert checker.record.seat == ("L2" if second_seat else "L3")
            assert ("2 steps, 20 seconds" if second_seat else "2 steps, 2 seconds") in checker.record.task
            assert "inventory.txt" in _output(checker.controller, "inspect")
            if second_seat:
                assert "Counted." in _output(checker.controller, "inspect-file")
            assert f"Check attempt 1: {reason}" in _output(main, "check")
            runs = rt.route_turn.delegations
            assert [(r.identity, r.status) for r in runs] == [("Coder", "done"), *([("Runner", "done")] if second_seat else []),
                                                             (identity, "interrupted"), (identity, "done")]
            assert not runs[-2].has_evidence and runs[-1].has_evidence and runs[-1].started > runs[0].finished
            verdict = _verdict(main)
            assert verdict["status"] == "verified"
            assert rt.registry.task_metrics[-1]["runner_first_pass"] is second_seat
            if (folder := os.environ.get("WITTYCODE_TEST_EVIDENCE")) and os.environ.get("WITTYCODE_FUSION_RETRY_EVIDENCE") == "1":
                proof = {"transport": "local fake Responses", "failure": reason, "user_turns": len(main.saved.turns),
                         "delegations": [(r.identity, r.seat, r.status, r.has_evidence) for r in runs],
                         "budgets": _output(main, "check").splitlines()[:2], "checks": verdict["checks"], "status": verdict["status"]}
                (Path(folder) / f"verification-{identity}.json").write_text(json.dumps(proof, indent=2), encoding="utf-8")
    asyncio.run(go())


@pytest.mark.parametrize("failure", ["no report", "empty evidence", "terminal error", "negative report"])
def test_bounded_attempts_and_negative_or_permanent_results_never_pass(tmp_path, failure):
    async def go():
        async with _fusion(tmp_path, active=GATES) as (main, fake):
            check = _delegate("Runner", call_id="check", budget={"steps": 16, "seconds": 2})
            repeatable = failure in ("no report", "empty evidence")
            bad = {"no report": say("No report."), "empty evidence": _report(),
                   "terminal error": fail("invalid_api_key"), "negative report": _report("concerns")}[failure]
            await _turn(main, fake, _route(), _delegate(), _report(), check, *([bad] * (3 if repeatable else 1)),
                        say("Draft."), _route(verification="self_check", call_id="skip"), say("Stopped."))
            assert main._fusion.registry.calls == (4 if repeatable else 2)
            verdict = _verdict(main)
            assert verdict["status"] == "unverified" and verdict["missing"] == ["runner"]
            if repeatable:
                receipt = _output(main, "check")
                for limits in ("16 steps, 2 seconds", "32 steps, 4 seconds", "64 steps, 8 seconds"):
                    assert limits in receipt
    asyncio.run(go())


def test_transient_failure_after_output_recovers_within_call(tmp_path):
    async def go():
        async with _fusion(tmp_path, active=GATES) as (main, fake):
            broken = [*say("Partial check")[:-1], fail("server_error")[-1]]
            await _turn(main, fake, _route(), _delegate(), _report(), _delegate("Runner", call_id="check"),
                        broken, _evidence(), say("Finished."))
            assert _verdict(main)["status"] == "verified" and main._fusion.registry.calls == 3
            assert "Check attempt 1: incomplete run" in _output(main, "check")
    asyncio.run(go())


def test_retry_must_reserve_another_call_from_the_same_policy(tmp_path):
    async def go():
        async with _fusion(tmp_path, active=GATES) as (main, fake):
            rt = main._fusion
            rt.registry.policy = replace(rt.registry.policy, max_calls=2)
            await _turn(main, fake, _route(), _delegate(), _report(), _delegate("Runner", call_id="check"),
                        say("Incomplete."), say("Draft."), say("Stopped."))
            assert rt.registry.calls == 2 and _verdict(main)["status"] == "unverified"
            assert "budget is exhausted" in _output(main, "check")
    asyncio.run(go())


@pytest.mark.parametrize("cap", ["manual", "plan"])
def test_timeout_at_time_ceiling_stops_without_spending_on_identical_retry(tmp_path, monkeypatch, cap):
    async def go():
        async with _fusion(tmp_path, active=GATES, max_seconds=1) as (main, fake):
            rt = main._fusion
            if cap == "plan":
                rt.registry.policy = replace(rt.registry.policy, max_seconds_adaptive=True)
                monkeypatch.setattr(rt, "_plan_budget", lambda: {"seconds": 1, "turns": 16})
            try:
                await _turn(main, fake, _route(), _delegate(), _report(), _delegate("Runner", call_id="check", budget={"seconds": 12}),
                            [("hold", "lost"), *say("Late.")], say("Draft."), say("Stopped."))
            finally:
                fake.release("lost")
            assert rt.registry.calls == 2 and _verdict(main)["status"] == "unverified"
            receipt = _output(main, "check")
            assert "Check attempt 1: timeout" in receipt and "no room" in receipt
    asyncio.run(go())


@pytest.mark.parametrize("cap", ["manual", "plan"])
def test_step_retry_cannot_exceed_manual_or_planned_turns(tmp_path, monkeypatch, cap):
    async def go():
        async with _fusion(tmp_path, active=GATES) as (main, fake):
            rt = main._fusion
            _manual_steps(main, 1, 1 if cap == "manual" else 8)
            if cap == "plan":
                monkeypatch.setattr(rt, "_plan_budget", lambda: {"turns": 1})
            await _turn(main, fake, _route(), _delegate(), _report(), _delegate("Runner", call_id="check"),
                        call("list_dir", {"directory_path": "."}), say("Draft."), say("Stopped."))
            assert rt.registry.calls == 2 and _verdict(main)["status"] == "unverified"
            assert "no room" in _output(main, "check")
    asyncio.run(go())


def test_two_turns_with_the_same_check_name_recover_only_the_current_expert(tmp_path):
    async def go():
        async with _fusion(tmp_path, active=GATES) as (main, fake):
            await _turn(main, fake, _delegate("Runner", goal="Old delivery."), _evidence(), say("First."))
            old, = main.agents.values()
            await _turn(main, fake, _route(), _delegate(), _report(), _delegate("Runner", call_id="new", goal="New delivery."),
                        say("No report."), _evidence(), say("Second."))
            new, = [a for a in main.agents.values() if a.record.identity == "Runner" and a is not old]
            assert len(new.controller.saved.turns) == 2 and len(old.controller.saved.turns) == 1
            assert "Old delivery." not in str(fake.requests[-2]["input"])
            assert _verdict(main)["status"] == "verified"
    asyncio.run(go())


@pytest.mark.parametrize("stop", ["first", "retry"])
def test_cancel_does_not_start_another_attempt(tmp_path, stop):
    async def go():
        async with _fusion(tmp_path, active=GATES) as (main, fake):
            sequence = [say("No report.")] if stop == "retry" else []
            fake.script(_route(), _delegate(), _report(), _delegate("QualitySampler"), *sequence,
                        [("hold", "active"), *_evidence()])
            assert main.send_prompt("Check the delivery.")
            try:
                await until(lambda: len(fake.requests) >= 5 + len(sequence))
                await main.interrupt()
                await finish_turn(main)
            finally:
                fake.release("active")
            assert main._fusion.registry.calls == 2 + len(sequence)
            assert main.saved.turns[-1].status == "interrupted"
            assert not any(run.has_evidence for run in main._fusion.route_turn.delegations if run.identity == "QualitySampler")
    asyncio.run(go())
