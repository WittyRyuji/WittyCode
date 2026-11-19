from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess

import pytest

from desktop.core.fusion.acp import AcpRun
from desktop.core.fusion.drive.retention import Retention
from desktop.core.transcript import NoticeBlock
from tests.app_support import ready_core
from tests.chatgpt_support import finish_turn
from tests.fake_responses import call, say
from tests.fakes import until
from tests.fusion_acp_support import make_guarded_engine
from tests.test_fusion_identity_runtime import DEVIN, LIGHT, WORKER, _brief, _fusion, _gates, _identity_folder, _turn
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
            continuation = [_delegate("Runner", call_id="verify"), _report()] if verify else []
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
    asyncio.run(go())


def test_coder_concerns_require_sampling_even_with_a_fresh_runner(tmp_path):
    async def go():
        async with _fusion(tmp_path, active=GATES) as (main, fake):
            await _turn(main, fake, _route(), _delegate(), _report("concerns"), _delegate("Runner", call_id="verify"), _report(),
                        say("First answer."), _delegate("QualitySampler", call_id="sample"), _report(), say("Final answer."))
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
            await _turn(main, fake, _route(), _delegate(), _report(), _delegate("Runner", call_id="verify"), _report(runner_status),
                        _delegate(call_id="fix"), _report(), say("First answer."), say("Final answer."))
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
                        _delegate("Runner", call_id="verify"), _report(), say("Reviewed."), *continuation)
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
        finally:
            await core.shutdown()
    asyncio.run(go())
