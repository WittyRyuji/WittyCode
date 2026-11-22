"""Offline month-end resume checks through the current product and its loopback fake transport."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from evals.closing_resume_case import case, live_side, live_tools, script

MODEL = "gpt-6.1-sol"  # The default catalog entry of the loopback fake.


def _ctx(ws: Path):
    from desktop.core.harness.tools import ToolContext

    return ToolContext(project=ws)


def test_literal_tool_allowlist(tmp_path):
    ws = tmp_path / "workspace"
    case.write_inputs(ws)
    command = case.adjust_command(3)
    plan = live_tools.match(command.replace(" ", "  \t"), ws, python="PY", die_command=command)
    assert plan and plan.die and plan.argv == ("PY", str(ws / "tools" / "wms.py"), "--db", "books.db",
                                              "adjust", "--wh", "W1", "--sku", "SKU-C", "--qty", "-4", "--reason", "count-diff")
    for command in [*case.REFERENCE, *case.QUERY_COMMANDS, case.transfer_command(5)]:
        assert live_tools.match(command, ws).kind == "run"
    for cmd in ("type TASK.md", "cat closing.csv", "Get-Content tools/wms.py", "type books.journal", "type adjustments.csv",
                "type transfers.csv", "cat inbox/batch-01.csv"):
        assert live_tools.match(cmd, ws).kind == "read"
    for cmd in ("dir", "ls inbox", "Get-ChildItem inbox"):
        assert live_tools.match(cmd, ws).kind == "list"
    for cmd in ("", "python -c pass", "type books.db", "type ../../state.json", "type inbox/../TASK.md", "dir ..", "dir /b",
                "ls inbox | whoami", "cat 'TASK.md'", case.CLOSE_COMMAND + " && whoami", case.REPORT_COMMAND + " > y.csv",
                case.post_command("batch-1.csv"), case.transfer_command(0), case.adjust_command(3).replace("SKU-C", "SKU-I"),
                case.adjust_command(3).replace("--wh W1", "--wh W3"), case.adjust_command(3).replace("-4", "-4.0"),
                case.adjust_command(3).replace("count-diff", "bad reason")):
        assert live_tools.match(cmd, ws) is None, cmd
    result = asyncio.run(live_tools.registry(ws).run("shell", {"command": "dir .."}, _ctx(ws)))
    assert result.is_error and live_tools.ALLOWED_SYNTAX in result.output


def test_hold_latches_every_later_tool_invocation(tmp_path, monkeypatch):
    async def exercise():
        ws = tmp_path / "workspace"
        case.write_inputs(ws)
        calls = []
        original_run = live_tools.subprocess.run

        def record(argv, **kwargs):
            calls.append((argv, kwargs["env"].get("WMS_DIE_AT")))
            return original_run(argv, **kwargs)

        monkeypatch.setattr(live_tools.subprocess, "run", record)
        monkeypatch.setenv("WMS_DIE_AT", "pre_receipt")  # An inherited hook must never reach a different command.
        hold = live_tools.Hold()
        reg = live_tools.registry(ws, die_at="pre_commit", die_command=script.CRASH_COMMAND, hold=hold)

        async def call(command):
            return await reg.run("shell", {"command": command}, _ctx(ws))

        assert not (await call(case.post_command("batch-01.csv"))).is_error
        task = asyncio.create_task(call(script.CRASH_COMMAND))
        try:
            assert await asyncio.to_thread(hold.effect.wait, 10)
            assert not task.done()
            for command in (case.adjust_command(4), case.QUERY_COMMANDS[0], "type books.journal", "dir", "bad command"):
                assert (await call(command)).output == live_tools.NOT_EXECUTED
        finally:
            hold.release.set()
            killed = await task
        assert killed.is_error and killed.output.endswith("Exit code: 9")
        assert (await call(case.adjust_command(4))).output == live_tools.NOT_EXECUTED
        assert [hook for _, hook in calls] == [None, "pre_commit"]
        assert (ws / "books.journal").read_text(encoding="utf-8").splitlines()[-1] == "adjust W1/SKU-C/-4/count-diff started"

    asyncio.run(exercise())


def _fake_call(command: str, call_id: str) -> dict:
    return {"type": "function_call", "name": "shell", "call_id": call_id, "arguments": json.dumps({"command": command})}


def test_permission_policy_needs_an_actual_post_reopen_read(tmp_path):
    repeat = script.CRASH_COMMAND.replace(" ", "\t  ")
    pending = [script.CRASH_COMMAND, case.adjust_command(4)]
    read = _fake_call(script.ADJUSTMENTS_QUERY, "query")
    output = {"type": "function_call_output", "call_id": "query", "output": "Result unknown"}
    assert not live_side._has_readonly_result([read])
    assert not live_side._has_readonly_result([read, output])
    assert live_side.permission_decision(repeat, pending, checked=False, inside=True) == ("cancel", "blind_repeat_refused")
    output["output"] = '{"qty":-4}\nExit code: 0'
    assert live_side._has_readonly_result([read, output])
    assert live_side.permission_decision(repeat, pending, checked=True, inside=True) == ("proceed_once", "approved")
    assert live_side.permission_decision("dir", pending, checked=False, inside=False) == ("cancel", "outside_workspace")
    prompt = SimpleNamespace(actions=[SimpleNamespace(tool_use_id="repeat", reason="结果未知")])
    items = [_fake_call(repeat, "repeat"), _fake_call("dir", "later")]
    assert live_side._prompt_details(prompt, items, tmp_path) == (repeat, "结果未知", True)


def test_native_checkpoint_finds_old_and_new_unreceipted_calls():
    calls = [_fake_call(case.adjust_command(i), str(i)) for i in range(1, 5)]
    outputs = [{"type": "function_call_output", "call_id": str(i), "output": text}
               for i, text in enumerate(("done", "done", "Result unknown"), 1)]
    want = [{"call_id": str(i), "command": case.adjust_command(i)} for i in (3, 4)]
    assert live_side._file_unreceipted({"items": [*calls, *outputs]}) == want
    outputs[-1]["output"] = "other"
    assert live_side._file_unreceipted({"items": [*calls, *outputs], "tools": {"3": {"status": "dispatched"}}}) == want


def _run(tmp_path: Path, mode: str, smoke: str, **options) -> dict:
    return asyncio.run(live_side.Runner({"work": tmp_path / "side", "model": MODEL, "mode": mode, "reasoning": "high",
                                        "offline_smoke": smoke, "seconds": 30, **options}).run())


def _save_evidence(tag: str, result: dict) -> None:
    if folder := os.environ.get("WITTYCODE_TEST_EVIDENCE"):
        keys = ("status", "mode", "metering", "smoke_branch", "precrash", "books_at_crash", "unreceipted", "autonomy",
                "pending_candidates", "turn_status", "holds", "follow_ups", "post_reopen_calls", "end", "stopped", "passed",
                "oracle_ok", "outcome", "turn_errors", "seconds")
        selected = {**{key: result[key] for key in keys}, "categories": result["oracle"]["categories"]}
        (Path(folder) / f"closing-live-{tag}.json").write_text(json.dumps(selected, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


@pytest.mark.parametrize("mode, adjustments", [("pre_commit", 2), ("pre_receipt", 3)])
def test_reference_smoke_keeps_native_checkpoint_and_exact_books(tmp_path, mode, adjustments):
    result = _run(tmp_path, mode, "reference")
    assert result["status"] == "smoke" and result["metering"] == "fake" and result["passed"]
    assert result["oracle_ok"] is True and result["outcome"] == "passed"
    assert result["turn_errors"] == []
    assert [e["turn_error"] for e in result["trace"] if e["event"] == "turn_end"] == [""]
    crash = result["books_at_crash"]
    assert {key: crash[key] for key in ("batches", "transfers", "adjustments", "closings", "closing_lines")} == {
        "batches": 16, "transfers": 9, "adjustments": adjustments, "closings": 0, "closing_lines": 0,
    }
    assert crash["journal_tail"][-1] == "adjust W1/SKU-C/-4/count-diff started"
    assert result["unreceipted_commands"] == [script.CRASH_COMMAND, case.adjust_command(4)]
    assert result["autonomy"] == "full" and result["pending_candidates"] == 1 and result["turn_status"] == "interrupted"
    assert {k: v for k, v in result["precrash"].items() if k != "items_at_reopen"} == {"user_turns": 2, "steps": 13, "calls": 38}
    assert result["precrash"]["items_at_reopen"] > 76
    assert result["follow_ups"] == 0 and all(result["oracle"]["categories"].values())
    assert [c["command"] for c in result["post_reopen_calls"]] == [script.ADJUSTMENTS_QUERY,
        *(case.adjust_command(i) for i in range(adjustments + 1, 9)), case.CLOSE_COMMAND, case.REPORT_COMMAND]
    _save_evidence(mode, result)


def test_blind_repeat_gate_refuses_then_follow_up_recovers(tmp_path):
    result = _run(tmp_path, "pre_receipt", "blind_repeat")
    assert result["passed"] and result["follow_ups"] == 1
    assert result["smoke_branch"] == "blind_repeat_refused_then_reference"
    refused = [hold for hold in result["holds"] if hold["decision"] == "cancel"]
    assert len(refused) == 1
    assert refused[0]["command"] == script.CRASH_COMMAND and refused[0]["rule"] == "blind_repeat_refused"
    assert refused[0]["gate"] and "结果未知" in refused[0]["reason"]
    assert result["prompts"]["refusals"] == 1
    assert [e["text"] for e in result["trace"] if e["phase"] == "resumed" and e["event"] == "user_message"] == [
        case.RESUME_INPUT, live_side.FOLLOW_UP,
    ]
    from desktop.core.harness.loop import DECLINED

    items = json.loads((tmp_path / "side" / "transcript_items.json").read_text(encoding="utf-8"))
    assert any(i.get("call_id") == "call_smoke_repeat_1" and i.get("output") == DECLINED for i in items)
    _save_evidence("blind-repeat", result)


@pytest.mark.parametrize("has_partial_output", [True, False])
def test_resumed_turn_failure_stops_and_reports_saved_error(tmp_path, monkeypatch, has_partial_output):
    from tests.fake_responses import fail, say

    def fail_first_request(self, fake):
        failure = fail("invalid_request_error", "Fake resume failure: " + "x" * 450)
        # Without any output the product may roll back the turn record rather than persist an error.
        fake.script([*say("Starting recovery.")[:-1], failure[-1]] if has_partial_output else failure)

    monkeypatch.setattr(live_side.Runner, "_prepare_smoke", fail_first_request)
    monkeypatch.setattr(live_side.sys, "path", list(live_side.sys.path))
    work, out = tmp_path / "side", tmp_path / "side.json"
    rc = live_side.main(["--source", str(Path(__file__).resolve().parents[1]), "--work", str(work),
                         "--mode", "pre_receipt", "--model", MODEL, "--out", str(out), "--offline-smoke", "reference"])
    result = json.loads(out.read_text(encoding="utf-8"))
    assert rc != 0 and result["status"] == result["outcome"] == "turn_failed"
    assert result["passed"] is None and result["follow_ups"] == 0 and result["stopped"] == ""
    resumed = [e for e in result["trace"] if e["phase"] == "resumed"]
    assert [e["text"] for e in resumed if e["event"] == "user_message"] == [case.RESUME_INPUT]
    session = work / "engine" / "chatgpt" / "sessions" / result["artifacts"]["session_file"]
    saved_turn = json.loads(session.read_text(encoding="utf-8"))["turns"][-1]
    if has_partial_output:
        assert saved_turn["status"] == "failed" and len(saved_turn["error"]) > 400
        assert result["turn_errors"] == [saved_turn["error"][:400]]
    else:
        assert saved_turn["status"] == "interrupted"
        assert result["turn_errors"] == [("Fake resume failure: " + "x" * 450)[:400]]
    assert [(e["turn_status"], e["turn_error"]) for e in resumed if e["event"] == "turn_end"] == [
        ("failed", next(iter(result["turn_errors"]), "")),
    ]


@pytest.mark.parametrize("limit", ["timeout", "request_cap"])
@pytest.mark.parametrize("running", [False, True])
def test_pump_only_interrupts_running_turns(tmp_path, trial_usage, limit, running):
    from desktop.core.session_state import SessionState
    from desktop.core.transcript import TurnStatus
    from evals.closing_resume_case import live_trial

    runner = live_side.Runner({"work": tmp_path, "mode": "pre_receipt", "request_cap": 0})
    ctl = SimpleNamespace(state=SessionState.RUNNING if running else SessionState.READY,
                          saved=SimpleNamespace(requests=[]),
                          transcript=SimpleNamespace(turns=[SimpleNamespace(status=TurnStatus.FAILED)]))
    interrupts = []

    async def interrupt():
        interrupts.append(True)
        ctl.state = SessionState.READY

    ctl.interrupt = interrupt
    requests = live_side.RequestDelta(ctl)
    if limit == "request_cap":
        ctl.saved.requests.append({"kind": "fake"})
    deadline = 0 if limit == "timeout" else float("inf")
    asyncio.run(runner.pump(ctl, deadline=deadline, requests=requests))
    assert interrupts == ([True] if running else [])
    assert runner.stopped == (limit if running else "")
    outcome = live_side._outcome(True, runner.stopped, live_side._turn_status(ctl))
    assert outcome == ("stopped_books_right" if running else "turn_failed")
    if not running:
        ledger, last_id, _ = trial_usage
        report = _trial_report(status=outcome, exit_code=2)
        live_trial._finalize_trial(report, None, ledger, last_id)
        assert report["status"] == "turn_failed" and report["passed"] is None and report["tokens"] is None


@pytest.mark.parametrize("limit", ["timeout", "request_cap", ""])
def test_resume_checks_shared_limits_before_followup(tmp_path, monkeypatch, limit):
    from desktop.core.session_state import SessionState
    from desktop.core.transcript import TurnStatus

    clock = 0.0
    monkeypatch.setattr(live_side, "time", SimpleNamespace(monotonic=lambda: clock, time=live_side.time.time))
    runner = live_side.Runner({"work": tmp_path, "mode": "pre_receipt", "seconds": 10, "request_cap": 1})
    runner.ws.mkdir(parents=True)
    ctl = SimpleNamespace(state=SessionState.READY, autonomy="full", usage=None,
                          saved=SimpleNamespace(items=[], turns=[], requests=[]),
                          transcript=SimpleNamespace(turns=[SimpleNamespace(status=TurnStatus.INTERRUPTED)]))
    sent = []

    def send_prompt(text):
        nonlocal clock
        sent.append(text)
        # Finish before the first poll; pump must not interrupt a finished turn.
        ctl.transcript.turns.append(SimpleNamespace(status=TurnStatus.DONE))
        ctl.saved.requests.append({})
        if len(sent) == 1:
            clock = 11.0 if limit == "timeout" else 1.0
            if limit == "request_cap":
                ctl.saved.requests.append({})
        else:
            (runner.ws / "closing.csv").touch()
        return True

    async def do_not_interrupt():
        pytest.fail("a finished turn must not be interrupted")

    async def open_saved(workspace, session_id):
        return ctl

    ctl.send_prompt, ctl.interrupt = send_prompt, do_not_interrupt
    result = asyncio.run(runner._resume(SimpleNamespace(open_saved=open_saved), "checkpoint", {}))
    wanted = [case.RESUME_INPUT] if limit else [case.RESUME_INPUT, live_side.FOLLOW_UP]
    assert sent == wanted
    assert [event["text"] for event in runner.trace if event["event"] == "user_message"] == wanted
    assert runner.follow_ups == len(wanted) - 1 and runner.stopped == limit
    assert result["end"] == (limit or "done")
    assert (runner.ws / "closing.csv").exists() == (not limit)


def test_request_cap_stops_without_followup(tmp_path):
    result = _run(tmp_path, "pre_receipt", "reference", request_cap=1)
    assert result["status"] == "smoke" and result["stopped"] == result["end"] == "request_cap"
    assert result["follow_ups"] == 0 and not result["passed"], result["trace"]
    assert result["oracle_ok"] is False and result["outcome"] == "stopped_books_wrong"
    assert len(result["usage"]["requests"]) > 1
    assert not (tmp_path / "side" / "case" / "workspace" / "closing.csv").exists()


def _argv(tmp_path: Path) -> list[str]:
    return ["--source", str(tmp_path / "bad"), "--work", str(tmp_path / "work"), "--mode", "pre_receipt",
            "--model", MODEL, "--out", str(tmp_path / "side.json")]


def test_side_cli_refuses_non_product_tree(tmp_path):
    assert live_side.main([*_argv(tmp_path), "--offline-smoke", "reference"]) == 2
    result = json.loads((tmp_path / "side.json").read_text(encoding="utf-8"))
    assert result["status"] == "cannot_run" and "not a product source tree" in result["note"]


def test_real_configuration_cannot_accept_a_script(tmp_path):
    with pytest.raises(ValueError, match="takes no scripted"):
        live_side.Runner({"mode": "pre_commit", "work": tmp_path, "smoke": lambda: None})
    with pytest.raises(ValueError, match="injected transport"):
        live_side.Runner({"mode": "pre_commit", "work": tmp_path, "phase2_factory": lambda: None})
    with pytest.raises(SystemExit) as error:
        live_side.main([*_argv(tmp_path), "--offline-smoke", "reference", "--account", "unused", "--budget-file", "unused"])
    assert error.value.code == 2
    assert not (tmp_path / "side.json").exists()


@pytest.mark.parametrize("catalog", ["verified", "exception", "fallback"])
def test_real_resume_refreshes_metered_catalog_before_open_saved(tmp_path, monkeypatch, catalog):
    from evals.inventory_resume_case import plan_meter

    calls = []

    class ReachedOpenSaved(Exception):
        pass

    class FakeEngine:
        def __init__(self):
            self.http = SimpleNamespace(_window=None)

        async def list_models(self, *, refresh=False):
            calls.append(("list_models", refresh))
            if catalog == "exception":
                raise RuntimeError("catalog unavailable")
            if catalog == "verified":
                self.http._window = 128_000
            return [object(), object()]

        async def open_saved(self, workspace, session_id):
            calls.append(("open_saved", session_id))
            raise ReachedOpenSaved

        async def close_all(self):
            calls.append(("close_all",))

    async def setup(settings, entry):
        return "checkpoint", tmp_path / "checkpoint.json", {}

    runner = live_side.Runner({"mode": "pre_receipt", "work": tmp_path / "work", "model": MODEL,
                               "account": tmp_path / "unused-account", "budget_file": tmp_path / "unused-budget"})
    monkeypatch.setattr(plan_meter, "make_engine", lambda *args, **kwargs: FakeEngine())
    monkeypatch.setattr(runner, "_setup", setup)
    monkeypatch.setattr(runner, "_books_at_crash", lambda: {})
    if catalog != "verified":
        reason = "model catalog refresh failed" if catalog == "exception" else "no verified GET /models catalog"
        with pytest.raises(live_side.CannotRun, match=reason):
            asyncio.run(runner.run())
        assert calls == [("list_models", True), ("close_all",)]
    else:
        with pytest.raises(ReachedOpenSaved):
            asyncio.run(runner.run())
        assert calls == [("list_models", True), ("open_saved", "checkpoint"), ("close_all",)]
    assert [e["models"] for e in runner.trace if e["event"] == "catalog_refreshed"] == ([2] if catalog == "verified" else [])


def test_trial_rejects_nested_run_before_touching_any_state(monkeypatch, capsys):
    from evals.closing_resume_case import live_trial

    monkeypatch.setenv("WITTYCODE_TEST_WORK", "owned-by-runner")
    with pytest.raises(SystemExit) as error:
        live_trial.main(["--revision", "a" * 40, "--label", "B", "--model", MODEL, "--offline-smoke", "reference"])
    assert error.value.code == 2
    assert "never inside a managed run" in capsys.readouterr().err


def test_trial_redacts_paths_in_nested_report(tmp_path):
    from evals.closing_resume_case import live_trial

    work, account = tmp_path / "work", tmp_path / "account"
    report = {"error": f"{work!s}: missing file", "oracle": {"checks": [{"detail": account.as_posix()}]}}
    got = live_trial._redact(report, [(work, "<scratch>"), (account, "<account>")])
    assert got == {"error": "<scratch>: missing file", "oracle": {"checks": [{"detail": "<account>"}]}}


@pytest.fixture
def trial_usage(tmp_path):
    from evals.inventory_resume_case.plan_meter import BudgetLedger

    ledger = BudgetLedger(tmp_path / "budget.sqlite")

    def settle(used):
        post_id = ledger.begin(MODEL, "high")
        ledger.settle(post_id, used=used, input_tokens=used - 1, output_tokens=1, cached_tokens=0,
                      reasoning_tokens=0, cache_write_tokens=None, seconds=0.1, response_id="fake", outcome="completed")
        return post_id

    return ledger, settle(3), settle(17)


def _trial_report(**updates):
    return {"status": "scored", "exit_code": 0, "turn_status": "interrupted", "autonomy": "full",
            "passed": True, "tokens": None, **updates}


@pytest.mark.parametrize("earlier_failure", [None, "cannot_run"])
def test_trial_cleanup_failure_does_not_score(tmp_path, trial_usage, earlier_failure):
    from evals.closing_resume_case import live_trial

    def blocked():
        raise PermissionError("fake sharing violation")

    ledger, last_id, current_id = trial_usage
    report = _trial_report(**({"status": earlier_failure, "error": "child failed"} if earlier_failure else {}))
    temp = SimpleNamespace(name=tmp_path / "scratch", cleanup=blocked)
    live_trial._finalize_trial(report, temp, ledger, last_id)
    assert report["status"] == (earlier_failure or "cleanup_failed")
    assert report.get("error") == ("child failed" if earlier_failure else None)
    assert report["cleanup"] == "blocked: PermissionError"
    assert report["passed"] is None and report["tokens"] is None and report["tokens_spent"] == 17
    assert [row["id"] for row in report["provider_posts"]] == [current_id]
    assert report["budget_after"] == ledger.totals()


@pytest.mark.parametrize("updates, status", [({"exit_code": 2}, "cannot_run"), ({"autonomy": "code"}, "invalid_checkpoint")])
def test_trial_failed_child_does_not_score_settled_usage(trial_usage, updates, status):
    from evals.closing_resume_case import live_trial

    ledger, last_id, _ = trial_usage
    report = _trial_report(error="original failure", **updates)
    live_trial._finalize_trial(report, None, ledger, last_id)
    assert report["status"] == status and report["error"] == "original failure"
    assert report["passed"] is None and report["tokens"] is None and report["tokens_spent"] == 17


def test_trial_pending_usage_is_unmetered(trial_usage):
    from evals.closing_resume_case import live_trial
    from evals.inventory_resume_case.plan_meter import RESERVE

    ledger, last_id, _ = trial_usage
    ledger.begin(MODEL, "high")
    report = _trial_report()
    live_trial._finalize_trial(report, None, ledger, last_id)
    assert report["status"] == "unmetered" and report["passed"] is None and report["tokens"] is None
    assert report["tokens_spent"] == 17
    assert [row["status"] for row in report["provider_posts"]] == ["settled", "pending"]
    assert report["budget_after"]["spent"] == 3 + 17 + RESERVE


def test_trial_scores_only_with_new_settled_usage(trial_usage):
    from evals.closing_resume_case import live_trial

    ledger, last_id, current_id = trial_usage
    scored = _trial_report(passed=False)
    live_trial._finalize_trial(scored, None, ledger, last_id)
    assert scored["status"] == "scored" and scored["passed"] is False and scored["tokens"] == scored["tokens_spent"] == 17
    empty = _trial_report()
    live_trial._finalize_trial(empty, None, ledger, current_id)
    assert empty["status"] == "not_run" and empty["provider_posts"] == [] and empty["tokens_spent"] == 0
    assert empty["passed"] is None and empty["tokens"] is None


def test_missing_shared_ledger_is_refused_without_creating_one(tmp_path, monkeypatch, capsys):
    from argparse import ArgumentParser

    from evals.closing_resume_case import live_trial

    monkeypatch.setattr(live_trial, "REPO", tmp_path)
    missing = tmp_path / "outputs" / "resume-ab-v1" / "not-created.sqlite"
    missing.parent.mkdir(parents=True)
    with pytest.raises(SystemExit) as error:
        live_trial._existing_budget(missing, ArgumentParser())
    assert error.value.code == 2 and "--ledger must point to an existing file" in capsys.readouterr().err
    assert not missing.exists()


def test_trial_keeps_only_error_kinds_without_scoring(tmp_path, monkeypatch, trial_usage):
    from evals.closing_resume_case import live_trial

    work, evidence = tmp_path / "scratch", tmp_path / "evidence"
    work.mkdir()
    ledger, last_id, _ = trial_usage
    account = tmp_path / "account"
    email = "demo@example.com"
    extra_path = "C:/demo/private/state.txt"
    short_token = "fake-" + "short"
    error = f"refresh_disabled: {work}; {account}; {ledger.path}; {email}; {extra_path}; {short_token}"
    child_result = {"status": "turn_failed", "outcome": "turn_failed", "passed": None, "turn_errors": [error],
                    "note": error, "trace": [{"event": "turn_end", "turn_status": "failed", "turn_error": error}]}

    def run(argv, **kwargs):
        if argv[:2] == ["git", "archive"]:
            with live_trial.zipfile.ZipFile(argv[argv.index("-o") + 1], "w"):
                pass
            return SimpleNamespace(returncode=0)
        assert "--offline-smoke" in argv and "--account" not in argv
        out = Path(argv[argv.index("--out") + 1])
        out.write_text(json.dumps(child_result), encoding="utf-8")
        return SimpleNamespace(returncode=2, stderr=error.encode("utf-8"))

    monkeypatch.setattr(live_trial.subprocess, "run", run)
    args = SimpleNamespace(revision="a" * 40, mode="pre_receipt", model=MODEL, reasoning="high", label="A",
                           request_cap=30, offline_smoke="reference")
    report = {}
    live_trial._run_child(args, work, evidence, None, None, report)
    live_trial._finalize_trial(report, None, ledger, last_id)
    assert report["status"] == report["outcome"] == "turn_failed" and report["exit_code"] == 2
    assert report["passed"] is None and report["tokens"] is None and report["tokens_spent"] == 17
    evidence.mkdir()
    dst = evidence / "trial.json"
    public = live_trial._write_report(dst, report, [(work, "<scratch>"), (account, "<account>"), (ledger.path, "<ledger>")])
    assert public["turn_error_kinds"] == ["auth_needs_refresh"]
    assert public["trace"][0]["turn_error_kind"] == public["error"] == "auth_needs_refresh"
    persisted = dst.read_text(encoding="utf-8")
    for forbidden in (email, extra_path, short_token, '"turn_errors"', '"turn_error"', '"child_stderr_tail"', '"_child_stderr"'):
        assert forbidden not in persisted
    assert not (evidence / "diagnostics.txt").exists()


@pytest.mark.parametrize("message, kind", [
    ("refresh_disabled", "auth_needs_refresh"),
    ("登录已失效", "auth_needs_refresh"),
    ("MeterBlocked: no verified GET /models catalog yet", "meter_no_catalog_proof"),
    ("MeterBlocked: the shared budget is spent", "budget_spent"),
    ("MeterBlocked: an earlier request has no settled usage", "meter_pending_usage"),
    ("MeterBlocked: request refused", "meter_blocked"),
    ("Unexpected failure", "other"),
    ("", ""),
])
def test_trial_error_kind_allowlist(message, kind):
    from evals.closing_resume_case import live_trial

    report = live_trial._public_report({"turn_errors": [message], "trace": [{"turn_error": message}]}, [])
    assert report["turn_error_kinds"] == ([kind] if kind else [])
    assert report["trace"] == ([{"turn_error_kind": kind}] if kind else [{}])


def test_opt_in_diagnostics_redact_and_only_write_for_invalid_runs(tmp_path):
    from evals.closing_resume_case import live_trial

    work, account, ledger = tmp_path / "scratch", tmp_path / "account", tmp_path / "budget.sqlite"
    paths = [(work, "<scratch>"), (account, "<account>"), (ledger, "<ledger>")]
    jwt_like = ".".join(("a" * 14, "b" * 14, "c" * 14))
    base64_like = "a+/=" * 10
    prefix = "尾" * 1600
    short_token = "fake-" + "short"
    error = f"Fake turn error at {work}; Bearer {short_token}"
    trial_error = f"no verified GET /models catalog at {account}; Bearer {short_token}"
    stderr = (f"Discard this earlier line\n{prefix}\n{work}; {account}; {ledger}\n"
              f"bEaReR {short_token}\n{jwt_like}\n{base64_like}\n").encode("utf-8") + b"\xff"
    expected = f"{prefix}\n<scratch>; <account>; <ledger>\nbEaReR <redacted>\n<redacted>\n<redacted>\n\ufffd"
    for status in ("unmetered", "scored", "smoke"):
        evidence = tmp_path / status
        evidence.mkdir()
        result = {"status": status, "cleanup": "done", "error": trial_error,
                  "turn_errors": [error], "_child_stderr": stderr}
        dst = evidence / "trial.json"
        live_trial._write_report(dst, result, paths, keep_diagnostics=True)
        assert json.loads(dst.read_text(encoding="utf-8")) == {
            "status": status, "cleanup": "done", "error": "meter_no_catalog_proof", "turn_error_kinds": ["other"],
        }
        diagnostics = evidence / "diagnostics.txt"
        if status != "unmetered":
            assert not diagnostics.exists()
            continue
        text = diagnostics.read_text(encoding="utf-8")
        assert text.splitlines()[0] == "仅供本机排障，可能含本机信息，不得提交或外传"
        assert "Trial error:\nno verified GET /models catalog at <account>; Bearer <redacted>\n" in text
        assert "Fake turn error at <scratch>; Bearer <redacted>" in text
        assert text.split("\nChild stderr tail:\n")[1] == expected[-1500:] + "\n"
        assert short_token not in text


@pytest.mark.parametrize("oracle_ok, stopped, end, expected", [
    (True, "", "done", "passed"),
    (False, "", "done", "books_wrong"),
    (True, "timeout", "done", "stopped_books_right"),
    (True, "", "failed", "turn_failed"),
    (False, "", "incomplete", "turn_failed"),
    (False, "request_cap", "request_cap", "stopped_books_wrong"),
])
def test_trial_outcome_distinguishes_books_and_turn_completion(oracle_ok, stopped, end, expected):
    from evals.closing_resume_case import live_trial

    assert live_side._outcome(oracle_ok, stopped, end) == expected
    assert {"oracle_ok", "outcome"} <= set(live_trial.SUMMARY_FIELDS)
