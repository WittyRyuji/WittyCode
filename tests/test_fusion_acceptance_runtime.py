from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
from dataclasses import replace

import pytest

from desktop.core.fusion.acp import AcpRun
from desktop.core.fusion.drive.retention import Retention
from desktop.core.transcript import NoticeBlock, TextBlockView
from tests.app_support import ready_core
from tests.chatgpt_support import finish_turn
from tests.fake_responses import call, say
from tests.fakes import until
from tests.fusion_acp_support import make_guarded_engine
from tests.test_fusion_identity_runtime import DEVIN, LIGHT, WORKER, _brief, _fusion, _gates, _identity_folder, _output, _turn
from tests.test_fusion_route_runtime import ROUTE_GATES, _edit, _report, _route

GATES = (*ROUTE_GATES, "acceptance")
pytestmark = pytest.mark.fusion_gates("drive", "brief", "report", "route", "acceptance")


def _delegate(identity="Coder", *, call_id="delegate", **kwargs):
    return call("delegate_task", _brief(identity, **kwargs), call_id=call_id)


def _verdict(main):
    drive = main._fusion.drive
    verdict, = [item.record for item in drive.candidates(shared=False, retention=Retention()) if item.record.kind == "verdict"]
    return json.loads("".join(drive.read(pointer) for pointer in verdict.pointers))


def _continuations(main):
    return [block["text"] for item in main.saved.items if item.get("role") == "developer" and isinstance(item.get("content"), list)
            for block in item["content"] if block.get("text", "").startswith("Fusion verification is incomplete.")]


def _notices(main):
    return [block.text for turn in main.transcript.turns for block in turn.blocks
            if isinstance(block, NoticeBlock) and "本轮工作未验证" in block.text]


@pytest.mark.parametrize("verify", [True, False])
def test_missing_runner_continues_once_within_the_same_user_turn(tmp_path, verify):
    async def go():
        async with _fusion(tmp_path, active=GATES) as (main, fake):
            continuation = [_delegate("Runner", call_id="verify"), _passed_runner_report()] if verify else []
            await _turn(main, fake, _route(), _delegate(), _report(), say("First answer."), *continuation, say("Final answer."))
            assert len(main.saved.turns) == 1 and len(fake.requests) == (7 if verify else 5)
            message, = _continuations(main)
            assert "Missing checks: runner" in message and "self_check" in message and "Do not report completion" in message
            assert message in str(fake.requests[4]["input"])
            verdict = _verdict(main)
            assert verdict["status"] == ("verified" if verify else "unverified")
            assert verdict["checks"] == (["runner"] if verify else [])
            assert verdict["missing"] == ([] if verify else ["runner"])
            assert len(_notices(main)) == (0 if verify else 1)
            assert ("本轮工作未验证" in main.saved.turns[-1].note) is (not verify)
            if not verify:
                # The answer is not the program verdict; both are visible and survive reopening.
                assert main.transcript.turns[-1].fusion_outcome == 'unverified'
                assert [b.text for b in main.transcript.turns[-1].blocks if isinstance(b, TextBlockView)] == ['First answer.', 'Final answer.']
                await main.close()
                reopened = await main.manager.open_saved(main.project, main.session_id)
                try:
                    rebuilt = reopened.transcript.turns[-1]
                    unverified = [b for b in rebuilt.blocks if isinstance(b, NoticeBlock) and "本轮工作未验证" in b.text]
                    assert len(unverified) == 1
                    assert [b.text for b in rebuilt.blocks if isinstance(b, TextBlockView)] == ['First answer.', 'Final answer.']
                    assert rebuilt.fusion_outcome == 'unverified'
                finally:
                    await reopened.close()
    asyncio.run(go())


@pytest.mark.parametrize("preference", [50, 70, 90])
def test_clean_l3_delivery_finishes_after_runner_without_a_sampler_continuation(tmp_path, preference):
    async def go():
        async with _fusion(tmp_path, active=(*GATES, "v3")) as (main, fake):
            runtime = main._fusion
            runtime.registry.policy = replace(runtime.registry.policy, preference=preference)
            _git(main.project, "init", "-q")
            _git(main.project, "commit", "--allow-empty", "-qm", "Fixture baseline")
            declare = call("declare_topic", {"kind": "simple", "intent": "execute", "topic": "Rename the inventory fields."})
            route = call("submit_route", {"destination": "coder", "reason": "Mechanical rename.", "verification": "runner",
                                          "items": [{"id": "rename", "workstream": "Inventory", "role": "worker",
                                                     "score": 0.15, "reason": "Mechanical.", "paths": ["inventory.txt"]}]})
            evidence = call("submit_report", {"status": "done", "summary": "Inventory checked.",
                                               "evidence": ["All inventory checks passed."], "notes": [], "ratings": []})
            edit = call("create_file", {"file_path": "inventory.txt", "content": "Renamed."})
            await _turn(main, fake, declare, route, _delegate(), edit, _report(), _delegate("Runner", call_id="verify"), evidence,
                        say("Verified."), say("Unexpected continuation."))
            verdict = _verdict(main)
            assert (main.project / "inventory.txt").exists()
            assert runtime.registry.allocation_for("Inventory", "worker")["seat"] == "L3"
            assert verdict["checks"] == ["runner"] and verdict["status"] == "verified" and not verdict["signals"]
            assert not _continuations(main) and not _notices(main)
            assert "V3 exception" in fake.requests[0]["instructions"] and "do not add a QualitySampler" in fake.requests[0]["instructions"]
    asyncio.run(go())


def _l3_rename_route(verification, call_id, *, paths=("inventory.txt",), extra=()):
    return call("submit_route", {"destination": "coder", "reason": "Mechanical rename.", "verification": verification,
                                 "items": [{"id": "rename", "workstream": "Inventory", "role": "worker",
                                            "score": 0.15, "reason": "Mechanical.", "paths": list(paths)}, *extra]},
                call_id=call_id)


def _declare_rename():
    return call("declare_topic", {"kind": "simple", "intent": "execute", "topic": "Rename the inventory fields."})


def _passed_runner_report():
    return call("submit_report", {"status": "done", "summary": "Inventory checked.",
                                  "evidence": ["All inventory checks passed."], "notes": [], "ratings": []})


def test_batch_normalizes_each_brief_without_changing_l3_acceptance(tmp_path):
    async def go():
        async with _fusion(tmp_path, active=(*GATES, "v3"), max_seconds=12) as (main, fake):
            runtime = main._fusion
            runtime.registry.policy = replace(runtime.registry.policy, preference=70)
            main.manager.settings.max_steps = 100
            _git(main.project, "init", "-q")
            _git(main.project, "commit", "--allow-empty", "-qm", "Fixture baseline")
            batch = call("delegate_tasks", {"tasks": [
                _brief(budget={"steps": 0}),
                _brief(workstream="Research", read_only=True, budget=None),
            ]}, call_id="batch")
            await _turn(main, fake, _declare_rename(), _l3_rename_route("runner", "route"), batch,
                        say("Done."), say("Done."), _delegate("Runner", call_id="verify"), _passed_runner_report(),
                        say("Reviewed."), say("No further checks."))
            inventory, research = (_output(main, "batch").split("Workstream Research:\n"))
            agents = {agent.record.workstream: agent for agent in main.agents.values() if agent.record.identity == "Coder"}
            assert set(agents) == {"Inventory", "Research"} and runtime.registry.calls == 3
            assert all(agent.record.status == "done" for agent in agents.values())
            steps = agents["Inventory"].controller.turn_step_limit()
            assert f"Ignored budget fields 'steps'; using {steps} steps, 12 seconds" in inventory
            steps = agents["Research"].controller.turn_step_limit()
            assert f"Ignored budget (expected an object, got null); using {steps} steps, 12 seconds" in research
            assert "Ignored 1 write path for this read-only brief." in research
            writes = [result for result in runtime.route_turn.delegations if result.paths]
            assert len(writes) == 1 and writes[0].seat == "L3" and not runtime.route_turn.unfrozen_write
            assert runtime.registry.write_paths(agents["Research"].record) == ()
            verdict = _verdict(main)
            assert verdict["checks"] == ["runner"] and verdict["status"] == "verified" and not verdict["signals"]
            assert not _continuations(main)
    asyncio.run(go())


@pytest.mark.parametrize("preference", [50, 70])
def test_reroute_cannot_drop_an_accepted_sampler_requirement(tmp_path, preference):
    async def go():
        async with _fusion(tmp_path, active=(*GATES, "v3")) as (main, fake):
            runtime = main._fusion
            runtime.registry.policy = replace(runtime.registry.policy, preference=preference)
            _git(main.project, "init", "-q")
            _git(main.project, "commit", "--allow-empty", "-qm", "Fixture baseline")
            await _turn(main, fake, _declare_rename(), _l3_rename_route("runner_and_sampler", "first-route"),
                        _delegate(call_id="first-write"), _report(call_id="first-report"), _l3_rename_route("runner", "reroute"),
                        _delegate(call_id="second-write"), _report(call_id="second-report"), _delegate("Runner", call_id="verify"),
                        _passed_runner_report(), say("Reviewed."), say("No further checks."))
            assert "Route accepted: coder (runner_and_sampler)" in _output(main, "first-route")
            assert "Route accepted: coder (runner)" in _output(main, "reroute")
            writes = [work for work in runtime.route_turn.delegations if work.paths]
            assert [work.seat for work in writes] == ["L3", "L3"]
            verdict = _verdict(main)
            assert verdict["checks"] == ["runner"] and not verdict["signals"]
            assert verdict["status"] == "unverified" and verdict["missing"] == ["sampler"]
            assert "Missing checks: sampler." in _continuations(main)[0]
    asyncio.run(go())


def test_main_edit_after_a_recovered_check_invalidates_its_evidence(tmp_path):
    async def go():
        async with _fusion(tmp_path, active=GATES) as (main, fake):
            await _turn(main, fake, _route(), _delegate(), _report(), _delegate("Runner", call_id="check"),
                        say("Incomplete check."), _passed_runner_report(), _route("self", call_id="self"), _edit(),
                        say("Draft."), say("No new evidence."))
            assert "Check attempt 1:" in _output(main, "check")
            assert _verdict(main)["status"] == "unverified" and _verdict(main)["missing"] == ["runner"]
    asyncio.run(go())


def test_main_edit_before_l3_delegation_disables_sampler_exemption(tmp_path):
    async def go():
        async with _fusion(tmp_path, active=(*GATES, "v3")) as (main, fake):
            runtime = main._fusion
            runtime.registry.policy = replace(runtime.registry.policy, preference=70)
            _git(main.project, "init", "-q")
            _git(main.project, "commit", "--allow-empty", "-qm", "Fixture baseline")
            await _turn(main, fake, _declare_rename(), _route("self", "self_check", call_id="self-route"), _edit("own-edit"),
                        _l3_rename_route("runner", "coder-route", paths=("inventory.txt", "marker.txt")),
                        _delegate(), _report(), _delegate("Runner", call_id="verify"),
                        _passed_runner_report(), say("Reviewed."), say("No further checks."))
            assert (main.project / "marker.txt").exists() and runtime.route_turn.edits == 1
            assert runtime.registry.allocation_for("Inventory", "worker")["seat"] == "L3"
            verdict = _verdict(main)
            assert verdict["checks"] == ["runner"] and not verdict["signals"]
            assert verdict["status"] == "unverified" and verdict["missing"] == ["sampler"]
            assert "Missing checks: sampler." in _continuations(main)[0]
    asyncio.run(go())


@pytest.mark.parametrize("when", ["before-delegation", "after-runner", "undispatched-item"])
def test_shell_write_outside_frozen_paths_disables_l3_exemption(tmp_path, when):
    async def go():
        async with _fusion(tmp_path, active=(*GATES, "v3")) as (main, fake):
            runtime = main._fusion
            runtime.registry.policy = replace(runtime.registry.policy, preference=70)
            _git(main.project, "init", "-q")
            _git(main.project, "commit", "--allow-empty", "-qm", "Fixture baseline")
            shell_write = call("shell", {"command": "Set-Content -LiteralPath extra.txt -Value outside"}, call_id="shell-write")
            before = [_route("self", "self_check", call_id="self-route"), shell_write] if when != "after-runner" else []
            after = [shell_write] if when == "after-runner" else []
            extra = ({"id": "extra", "workstream": "Not dispatched", "role": "worker", "score": 0.15,
                      "reason": "Mechanical.", "paths": ["extra.txt"]},) if when == "undispatched-item" else ()
            await _turn(main, fake, _declare_rename(), *before, _l3_rename_route("runner", "coder-route", extra=extra),
                        _delegate(), _report(), _delegate("Runner", call_id="verify"), _passed_runner_report(),
                        *after, say("Reviewed."), say("No further checks."))
            assert (main.project / "extra.txt").read_text().strip() == "outside"
            assert runtime.route_turn.edits == 0
            verdict = _verdict(main)
            assert verdict["checks"] == ["runner"] and not verdict["signals"]
            assert verdict["status"] == "unverified" and verdict["missing"] == ["sampler"]
    asyncio.run(go())


@pytest.mark.parametrize("hidden_by", ["gitignore", "assume-unchanged"])
def test_shell_write_hidden_from_git_status_still_blocks_l3_exemption(tmp_path, hidden_by):
    async def go():
        async with _fusion(tmp_path, active=(*GATES, "v3")) as (main, fake):
            runtime = main._fusion
            runtime.registry.policy = replace(runtime.registry.policy, preference=70)
            _git(main.project, "init", "-q")
            if hidden_by == "gitignore":
                (main.project / ".gitignore").write_text("extra.txt\n", encoding="utf-8")
            else:
                (main.project / "extra.txt").write_text("Original.\n", encoding="utf-8")
            _git(main.project, "add", "-A")
            _git(main.project, "commit", "-qm", "Fixture baseline")
            if hidden_by == "assume-unchanged":
                _git(main.project, "update-index", "--assume-unchanged", "extra.txt")
            shell_write = call("shell", {"command": "Set-Content -LiteralPath extra.txt -Value outside"})
            edit = call("create_file", {"file_path": "inventory.txt", "content": "Renamed."})
            await _turn(main, fake, _declare_rename(), _route("self", "self_check", call_id="self-route"), shell_write,
                        _l3_rename_route("runner", "coder-route"), _delegate(), edit, _report(),
                        _delegate("Runner", call_id="verify"), _passed_runner_report(), say("Reviewed."), say("No further checks."))
            assert (main.project / "extra.txt").read_text(encoding="utf-8").strip() == "outside"
            assert runtime.route_turn.edits == 0
            start = runtime.route_turn.exemption_start
            assert start is not None and start.changed(main.project) == {"inventory.txt"}, "legacy scope tracking is unchanged"
            assert start.changed(main.project, for_exemption=True) == {"inventory.txt", "extra.txt"}
            verdict = _verdict(main)
            assert verdict["checks"] == ["runner"] and not verdict["signals"]
            assert verdict["status"] == "unverified" and verdict["missing"] == ["sampler"]
    asyncio.run(go())


def test_l3_exemption_requires_head_at_verdict_not_just_at_baseline(tmp_path):
    from desktop.core.fusion.drive.fingerprint import _head

    async def go():
        async with _fusion(tmp_path, active=(*GATES, "v3")) as (main, fake):
            runtime = main._fusion
            runtime.registry.policy = replace(runtime.registry.policy, preference=70)
            _git(main.project, "init", "-q")
            _git(main.project, "commit", "--allow-empty", "-qm", "Fixture baseline")
            edit = call("create_file", {"file_path": "inventory.txt", "content": "Renamed."})
            fake.script(_declare_rename(), _l3_rename_route("runner", "route"), _delegate(), edit, _report(),
                        _delegate("Runner", call_id="verify"), _passed_runner_report(),
                        [("hold", "before-verdict"), *say("Reviewed.")], say("No further checks."))
            assert main.send_prompt("Check the inventory.")
            try:
                await until(lambda: any(work.identity == "Runner" for work in runtime.route_turn.delegations))
                start = runtime.route_turn.exemption_start
                assert start is not None and start.commit == _head(main.project) and start.commit
                _git(main.project, "symbolic-ref", "HEAD", "refs/heads/fixture-unborn")
                assert not _head(main.project) and start.changed(main.project) == {"inventory.txt"}
                assert start.changed(main.project, for_exemption=True) is None
            finally:
                fake.release("before-verdict")
            await finish_turn(main)
            verdict = _verdict(main)
            assert verdict["checks"] == ["runner"] and not verdict["signals"]
            assert verdict["status"] == "unverified" and verdict["missing"] == ["sampler"]
    asyncio.run(go())


@pytest.mark.parametrize("failure", ["plain", "missing-head", "final-diff"])
def test_l3_exemption_requires_reliable_workspace_changes(tmp_path, monkeypatch, failure):
    from desktop.core.fusion import runtime as fusion_runtime

    async def go():
        async with _fusion(tmp_path, active=(*GATES, "v3")) as (main, fake):
            runtime = main._fusion
            runtime.registry.policy = replace(runtime.registry.policy, preference=70)
            if failure != "plain":
                _git(main.project, "init", "-q")
            if failure == "final-diff":
                _git(main.project, "commit", "--allow-empty", "-qm", "Fixture baseline")
                real_changed_since = fusion_runtime.changed_since
                monkeypatch.setattr(fusion_runtime, "changed_since", lambda project, commit:
                                    None if (project / "inventory.txt").exists() else real_changed_since(project, commit))
            edit = call("create_file", {"file_path": "inventory.txt", "content": "Renamed."})
            await _turn(main, fake, _declare_rename(), _l3_rename_route("runner", "route"), _delegate(), edit, _report(),
                        _delegate("Runner", call_id="verify"), _passed_runner_report(), say("Reviewed."), say("No further checks."))
            verdict = _verdict(main)
            assert (main.project / "inventory.txt").exists()
            assert verdict["checks"] == ["runner"] and not verdict["signals"]
            assert verdict["status"] == "unverified" and verdict["missing"] == ["sampler"]
    asyncio.run(go())


def test_legacy_delegation_records_an_unfrozen_write(tmp_path):
    async def go():
        async with _fusion(tmp_path, active=GATES) as (main, fake):
            await _turn(main, fake, _route(), _delegate(), _report(), _delegate("Runner", call_id="verify"),
                        _passed_runner_report(), say("Verified."))
            runtime = main._fusion
            writer, = (work for work in runtime.route_turn.delegations if work.paths)
            assert not runtime.gate("v3") and not runtime.registry.allocations and writer.seat == ""
            assert runtime.route_turn.edits == 0 and runtime.route_turn.unfrozen_write
            assert _verdict(main)["status"] == "verified", "the legacy acceptance decision is unchanged"
    asyncio.run(go())


def test_coder_concerns_require_sampling_even_with_a_fresh_runner(tmp_path):
    async def go():
        async with _fusion(tmp_path, active=GATES) as (main, fake):
            await _turn(main, fake, _route(), _delegate(), _report("concerns"), _delegate("Runner", call_id="verify"), _passed_runner_report(),
                        say("First answer."), _delegate("QualitySampler", call_id="sample"), _passed_runner_report(), say("Final answer."))
            assert "Missing checks: sampler." in _continuations(main)[0]
            verdict = _verdict(main)
            assert verdict["signals"] == ["concerns"] and verdict["checks"] == ["runner", "sampler"] and verdict["status"] == "verified"
    asyncio.run(go())


def test_explanation_after_the_write_satisfies_the_gate_and_is_retained(tmp_path):
    async def go():
        async with _fusion(tmp_path, active=GATES) as (main, fake):
            reason = "Only prose changed; there are no runnable checks for it."
            await _turn(main, fake, _route(), _delegate(), _report("concerns"), say("First answer."),
                        _route(verification="self_check", reason=reason, call_id="explain"), say("Reviewed the diff."))
            verdict = _verdict(main)
            assert verdict["status"] == "verified" and verdict["skip_reason"] == reason and verdict["signals"] == ["concerns"]
            assert len(_continuations(main)) == 1 and not _notices(main)
    asyncio.run(go())


@pytest.mark.parametrize("runner_status", ["done", "concerns"])
def test_runner_before_the_last_coder_is_stale_and_failed_checks_mark_repairs(tmp_path, runner_status):
    async def go():
        async with _fusion(tmp_path, active=GATES) as (main, fake):
            runner = _passed_runner_report() if runner_status == "done" else _report(runner_status)
            await _turn(main, fake, _route(), _delegate(), _report(), _delegate("Runner", call_id="verify"), runner,
                        _delegate(call_id="fix"), _report(), say("First answer."), say("Final answer."))
            assert [work.identity for work in main._fusion.route_turn.delegations] == ["Coder", "Runner", "Coder"]
            verdict = _verdict(main)
            assert verdict["checks"] == [] and verdict["status"] == "unverified"
            assert verdict["missing"] == (["runner"] if runner_status == "done" else ["runner", "sampler"])
            assert ("repaired" in verdict["signals"]) == (runner_status != "done")
    asyncio.run(go())


def _git(project, *arguments):
    executable = shutil.which("git")
    if executable is None:
        pytest.skip("git is unavailable")
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
    subprocess.run([executable, "-c", "core.hooksPath=" + os.devnull, "-c", "user.name=Demo", "-c", "user.email=demo@example.com",
                    *arguments], cwd=project, env=env, check=True, capture_output=True)


@pytest.mark.parametrize("git", [False, True])
@pytest.mark.parametrize("outside", [False, True])
def test_workspace_signal_only_counts_changes_during_delegations(tmp_path, git, outside):
    async def go():
        async with _fusion(tmp_path, active=GATES) as (main, fake):
            stale = main.project / "preexisting.txt"
            stale.write_text("Old text", encoding="utf-8")
            if git:
                _git(main.project, "init", "-q")
                _git(main.project, "add", "preexisting.txt")
                _git(main.project, "commit", "-qm", "Fixture baseline")
            stale.write_text("Changed before the turn", encoding="utf-8")
            written = "spill.txt" if outside else "inventory.txt"
            continuation = [say("No further checks.")] if outside else []
            await _turn(main, fake, _route(), _delegate(), call("create_file", {"file_path": written, "content": "New text"}), _report(),
                        _delegate("Runner", call_id="verify"), _passed_runner_report(), say("Reviewed."), *continuation)
            verdict = _verdict(main)
            assert (main.project / written).is_file()
            assert ("out_of_scope" in verdict["signals"]) == outside
            assert verdict["missing"] == (["sampler"] if outside else [])
            assert "preexisting.txt" not in main._fusion.route_turn.changes
    asyncio.run(go())


@pytest.mark.parametrize("git", [False, True])
def test_caches_left_by_a_test_run_are_not_out_of_scope_changes(tmp_path, git):
    # Found with a real model: in a git project a Coder's test run left __pycache__ behind, and a sampler was demanded for it.
    from desktop.core.fusion.runtime import WorkspaceStart

    (tmp_path / "stats.py").write_text("x = 1\n", encoding="utf-8")
    if git:
        _git(tmp_path, "init", "-q")
        _git(tmp_path, "add", "-A")
        _git(tmp_path, "commit", "-qm", "Fixture baseline")
    start = WorkspaceStart.capture(tmp_path)
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "__pycache__" / "stats.cpython-311.pyc").write_bytes(b"compiled")
    (tmp_path / "stats.py").write_text("x = 2\n", encoding="utf-8")
    assert start is not None and start.changed(tmp_path) == {"stats.py"}
    if git:
        assert start.changed(tmp_path, for_exemption=True) == {"stats.py"}


@pytest.mark.parametrize("scenario", ["self", "no writes", "off", "plan"])
def test_acceptance_scope_and_approved_plan_default(tmp_path, scenario):
    async def go():
        async with _fusion(tmp_path, active=ROUTE_GATES if scenario == "off" else GATES) as (main, fake):
            if scenario == "self":
                responses = [_route("self"), _edit(), say("Reviewed.")]
            elif scenario == "no writes":
                responses = [_route(), _delegate(read_only=True, paths=[]), _report(), say("Reviewed.")]
            elif scenario == "off":
                responses = [_route(), _delegate(), _report(), say("Reviewed.")]
            else:
                main.saved.plan = {"id": "plan", "status": "approved", "text": "Check and repair inventory."}
                responses = [_delegate(), _report(), say("First answer."), say("Final answer.")]
            await _turn(main, fake, *responses)
            if scenario == "plan":
                verdict = _verdict(main)
                assert verdict["from_plan"] and verdict["route"]["verification"] == "runner"
                assert verdict["missing"] == ["runner"]
            else:
                assert not _continuations(main) and not _notices(main)
                assert not any(item.record.kind == "verdict" for item in main._fusion.drive.candidates(shared=False, retention=Retention()))
    asyncio.run(go())


def test_devin_main_uses_a_second_acp_prompt_in_the_same_turn(tmp_path, monkeypatch):
    original_dispatch, original_run = AcpRun.dispatch, AcpRun.run
    invocations = []

    async def route_then_delegate(self, method, params):
        # The fake's matrix action sends a real batch call; supply the route it can't script before that call.
        if method == "tools/call" and params.get("name") == "delegate_tasks":
            result = await original_dispatch(self, method, {"name": "submit_route", "bridgeCallId": params["bridgeCallId"] + ":route",
                                                            "arguments": {"destination": "coder", "reason": "One change.", "verification": "runner"}})
            assert "Route accepted" in result["content"][0]["text"]
        return await original_dispatch(self, method, params)

    async def observe(self, tools, watcher, history, sent, attachments, instructions, **kwargs):
        turn = self.host._turn
        result = await original_run(self, tools, watcher, history, sent, attachments, instructions, **kwargs)
        invocations.append((turn, self.session, sent))
        return result

    monkeypatch.setattr(AcpRun, "dispatch", route_then_delegate)
    monkeypatch.setattr(AcpRun, "run", observe)

    async def go():
        _identity_folder()
        harness = make_guarded_engine(tmp_path)
        core, model, _, _, _ = await ready_core(tmp_path, devin_engine=harness.engine)
        try:
            await until(lambda: any(item["source"] == "devin" for item in core.catalog.models), timeout=20)
            core.settings.fusion_gates = _gates(*GATES)
            core.settings.fusion_main, core.settings.fusion_workers, core.settings.fusion_light = DEVIN, [WORKER], LIGHT
            cid = (await core.call("session.new", {"projectId": str(tmp_path / "proj"), "fusion": True}))["id"]
            main = core._by_cid[cid].controller
            model.script(_report())
            assert main.send_prompt("[fusion-matrix] " + json.dumps({"tasks": [_brief()]}))
            await finish_turn(main)
            assert len(main.saved.turns) == 1 and len(invocations) == 2
            assert invocations[0][0] is invocations[1][0] and invocations[0][1] == invocations[1][1]
            assert "Missing checks: runner" in invocations[1][2] and _verdict(main)["status"] == "unverified"
            answers = [item for item in main.saved.items if item.get("type") == "message" and item.get("role") == "assistant"]
            assert len(answers) == 2 and not any(item.get('wc_candidate') for item in answers)
            assert main.transcript.turns[-1].fusion_outcome == 'unverified'
            assert len([b.text for b in main.transcript.turns[-1].blocks if isinstance(b, TextBlockView)]) == 2
            assert "internal draft" not in invocations[1][2]
        finally:
            await core.shutdown()
    asyncio.run(go())
