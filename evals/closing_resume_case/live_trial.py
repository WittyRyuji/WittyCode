"""Run one foreground trial against frozen product source; keep only selected evidence."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path
from typing import Any

from .live_side import MODES, REQUEST_CAP, RESUME_SECONDS
from .script import SMOKES

REPO = Path(__file__).resolve().parents[2]
CHILD_TIMEOUT = 540
SUMMARY_FIELDS = (
    "status", "metering", "smoke_branch", "end", "passed", "stopped", "seconds", "post_reopen_calls", "prompts",
    "journal", "oracle", "oracle_ok", "outcome", "autonomy", "pending_candidates", "turn_status", "precrash", "books_at_crash",
    "unreceipted", "unreceipted_commands", "holds", "follow_ups", "turn_errors",
)
TRACE_FIELDS = frozenset({
    "t", "event", "phase", "decision", "turn_status", "turn_error", "pending_candidates", "stopped", "seconds",
    "killed_desktop_process", "clean_close_differed", "command", "rule", "gate", "kind", "autonomy", "stable_reads",
    "follow_ups", "native", "wms_child_exit",
})
POST_FIELDS = frozenset({
    "id", "status", "used", "input_tokens", "output_tokens", "cached_tokens", "reasoning_tokens", "cache_write_tokens",
    "seconds", "outcome", "error",
})


def _select(record: dict, fields: Any) -> dict:
    return {key: value for key, value in record.items() if key in fields}


def _redact(value: Any, paths: list[tuple[Path, str]]) -> Any:
    if isinstance(value, dict):
        return {key: _redact(child, paths) for key, child in value.items()}
    if isinstance(value, list):
        return [_redact(child, paths) for child in value]
    if isinstance(value, str):
        for path, label in paths:
            for spelling in (str(path).replace("\\", "\\\\"), str(path), path.as_posix()):
                value = re.sub(re.escape(spelling), label, value, flags=re.IGNORECASE if os.name == "nt" else 0)
    return value


def _error_kind(message: str | None) -> str:
    if not message:
        return ""
    message = message.casefold()
    if "refresh_disabled" in message or "登录已失效" in message:
        return "auth_needs_refresh"
    for needle, kind in (("no verified get /models catalog", "meter_no_catalog_proof"),
                         ("shared budget is spent", "budget_spent"),
                         ("no settled usage", "meter_pending_usage"), ("meterblocked", "meter_blocked")):
        if needle in message:
            return kind
    return "other"


def _public_report(report: dict, paths: list[tuple[Path, str]]) -> dict:
    private = {"turn_errors", "trace", "_child_stderr", "child_stderr_tail", "error"}
    result = _redact({key: value for key, value in report.items() if key not in private}, paths)
    result["turn_error_kinds"] = [kind for message in report.get("turn_errors", ()) if (kind := _error_kind(message))]
    if "trace" in report:
        result["trace"] = []
        for event in report["trace"]:
            public_event = _redact({key: value for key, value in event.items() if key != "turn_error"}, paths)
            if kind := _error_kind(event.get("turn_error")):
                public_event["turn_error_kind"] = kind
            result["trace"].append(public_event)
    if kind := _error_kind(report.get("error")):
        result["error"] = kind
    for post in result.get("provider_posts", ()):
        if post.get("error"):
            post["error"] = _error_kind(post["error"])
    return result


def _diagnostic_text(text: str, paths: list[tuple[Path, str]]) -> str:
    text = _redact(text, paths)
    text = re.sub(r"(\bBearer[ \t]+)[A-Za-z0-9._~+/-]+=*", r"\1<redacted>", text, flags=re.IGNORECASE)
    return re.sub(r"[A-Za-z0-9._+/=\-]{40,}", "<redacted>", text)


def _write_report(dst: Path, report: dict, paths: list[tuple[Path, str]], *, keep_diagnostics: bool = False) -> dict:
    public = _public_report(report, paths)
    dst.write_text(json.dumps(public, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if keep_diagnostics and (report["status"] not in {"scored", "smoke"} or report.get("cleanup") != "done"):
        trial_error = _diagnostic_text(report.get("error") or "", paths)
        errors = "\n".join(_diagnostic_text(message, paths) for message in report.get("turn_errors", ()) if message)
        # Scrub before slicing so the boundary cannot expose part of a credential.
        stderr = (report.get("_child_stderr") or b"").decode("utf-8", errors="replace")
        tail = _diagnostic_text(stderr, paths)[-1500:]
        dst.with_name("diagnostics.txt").write_text(
            f"仅供本机排障，可能含本机信息，不得提交或外传\n\nTrial error:\n{trial_error}\n\n"
            f"Turn errors:\n{errors}\n\nChild stderr tail:\n{tail}\n",
            encoding="utf-8",
        )
    return public


def _isolated_env(work: Path, evidence: Path) -> dict[str, str]:
    home, scratch = work / "home", work / "tmp"
    home.mkdir()
    scratch.mkdir()
    env = {key: value for key, value in os.environ.items()
           if not key.upper().startswith(("PYTHON", "WITTYCODE_", "XDG_"))
           and "API_KEY" not in key.upper() and key.upper() not in {"WMS_DIE_AT", "HOMEDRIVE", "HOMEPATH"}}
    env.update(dict.fromkeys(("HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME"), str(home)))
    env.update(dict.fromkeys(("TEMP", "TMP", "TMPDIR", "PYTEST_DEBUG_TEMPROOT"), str(scratch)))
    env.update(WITTYCODE_SETTINGS=str(home / "settings.json"), CODEX_HOME=str(home / ".codex"),
               WITTYCODE_TEST_WORK=str(work), WITTYCODE_TEST_EVIDENCE=str(evidence),
               PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="utf-8")
    return env


def _run_child(args: argparse.Namespace, work: Path, evidence: Path, account: Path | None,
               budget: Path | None, report: dict) -> None:
    source = work / "source"
    archive = work / "frozen.zip"
    side = work / "side.json"
    subprocess.run(["git", "archive", "--format=zip", "-o", str(archive), args.revision, "desktop", "tests"],
                   cwd=REPO, stdin=subprocess.DEVNULL, capture_output=True, check=True, timeout=60)
    with zipfile.ZipFile(archive) as frozen:
        frozen.extractall(source)
    cmd = [sys.executable, "-B", "-m", "evals.closing_resume_case.live_side", "--source", str(source),
           "--work", str(work / "run"), "--mode", args.mode, "--model", args.model, "--reasoning", args.reasoning,
           "--out", str(side), "--side", args.label, "--seconds", str(int(RESUME_SECONDS)),
           "--request-cap", str(args.request_cap)]
    if args.offline_smoke:
        cmd.extend(("--offline-smoke", args.offline_smoke))
    else:
        cmd.extend(("--account", str(account), "--budget-file", str(budget)))
    child = subprocess.run(cmd, cwd=REPO, env=_isolated_env(work, evidence), stdin=subprocess.DEVNULL,
                           capture_output=True, timeout=CHILD_TIMEOUT)
    report["exit_code"] = child.returncode
    report["_child_stderr"] = child.stderr
    if not side.is_file():
        report.update(status="cannot_run", error="product child exited without a side result")
        return
    result = json.loads(side.read_text(encoding="utf-8"))
    report.update(_select(result, SUMMARY_FIELDS))
    report["trace"] = [_select(event, TRACE_FIELDS) for event in result.get("trace", ())]
    report["resume_to_oracle_s"] = result.get("seconds", {}).get("resume_to_oracle")
    if result.get("note"):
        report["error"] = str(result["note"])
    expected = "smoke" if args.offline_smoke else "scored"
    if failure := _child_failure(report, expected):
        report.update(status=failure, passed=None)


def _child_failure(report: dict, expected: str) -> str:
    if report.get("status") == "turn_failed":
        return "turn_failed"
    if report.get("exit_code") != 0 or report.get("status") != expected:
        return "cannot_run"
    if report.get("turn_status") != "interrupted" or report.get("autonomy") != "full":
        return "invalid_checkpoint"
    return ""


def _existing_budget(path: Path, parser: argparse.ArgumentParser) -> Path:
    budget = path.resolve()
    if budget.parent != (REPO / "outputs" / "resume-ab-v1").resolve() or not budget.is_file():
        parser.error("--ledger must point to an existing file under outputs/resume-ab-v1/")
    return budget


def _finalize_trial(report: dict, temp: Any, ledger: Any, last_id: int) -> None:
    try:
        if temp is not None:
            temp.cleanup()
        report["cleanup"] = "done"
    except OSError as error:
        report["cleanup"] = f"blocked: {type(error).__name__}"
        print("Cleanup blocked:", temp.name, file=sys.stderr)
    if ledger is None:
        return

    report.update(provider_posts=[], tokens_spent=0, budget_after=None)
    meter_failure = ""
    try:
        rows = [row for row in ledger.rows() if row["id"] > last_id]
        report["provider_posts"] = [_select(row, POST_FIELDS) for row in rows]
        report["tokens_spent"] = sum(row["used"] for row in rows if row["status"] == "settled")
        if not rows:
            meter_failure = "not_run"
        elif any(row["status"] != "settled" for row in rows):
            meter_failure = "unmetered"
    except Exception as error:
        meter_failure = "unmetered"
        report.setdefault("error", f"ledger finalization: {type(error).__name__}")
    try:
        report["budget_after"] = ledger.totals()
    except Exception as error:
        meter_failure = meter_failure or "unmetered"
        report.setdefault("error", f"ledger finalization: {type(error).__name__}")

    # Child/checkpoint and request failures precede scratch cleanup; neither can become a score.
    if report["status"] == "scored":
        failure = _child_failure(report, "scored") or meter_failure
        if failure or report["cleanup"] != "done":
            report["status"] = failure or "cleanup_failed"
    if report["status"] == "scored":
        report["tokens"] = report["tokens_spent"]
    else:
        report.update(passed=None, tokens=None)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--label", required=True, choices=("A", "B"))
    parser.add_argument("--model", required=True)
    parser.add_argument("--reasoning", default="high")
    parser.add_argument("--mode", choices=MODES, default="pre_receipt")
    parser.add_argument("--account", type=Path)
    parser.add_argument("--ledger", type=Path)
    parser.add_argument("--request-cap", type=int, default=REQUEST_CAP)
    parser.add_argument("--offline-smoke", choices=SMOKES)
    parser.add_argument("--keep-diagnostics", action="store_true", help="keep local-only error text and stderr for invalid runs")
    args = parser.parse_args(argv)
    if "WITTYCODE_TEST_WORK" in os.environ:
        parser.error("start this self-managed trial directly, never inside a managed run")
    if not re.fullmatch(r"[0-9a-f]{40}", args.revision) or args.request_cap < 0:
        parser.error("need a full 40-character commit and a nonnegative request-cap")
    ledger = account = budget = None
    if args.offline_smoke:
        if args.account is not None or args.ledger is not None:
            parser.error("--offline-smoke needs neither --account nor --ledger; do not pass them")
    else:
        if args.account is None or args.ledger is None:
            parser.error("a real trial requires --account and --ledger")
        from evals.inventory_resume_case.plan_meter import LIMIT, RESERVE, BudgetLedger

        budget = _existing_budget(args.ledger, parser)
        account = args.account.resolve()
        if not (account / "state.json").is_file():
            parser.error("need an explicit signed-in account pointer")
        ledger = BudgetLedger(budget)
        totals = ledger.totals()
        if totals["pending"] or totals["spent"] + RESERVE > LIMIT:
            parser.error("unsettled usage or not enough remaining shared allowance; no trial started")
        last_id = max((row["id"] for row in ledger.rows()), default=0)

    output = REPO / "outputs" / "test-runs"
    output.mkdir(parents=True, exist_ok=True)
    evidence = Path(tempfile.mkdtemp(prefix="closing-smoke-" if args.offline_smoke else "closing-plan-", dir=output))
    dst = evidence / "trial.json"
    report = {"version": args.label, "commit": args.revision, "mode": args.mode, "model": args.model,
              "reasoning": args.reasoning, "status": "not_started", "input": "native reopen then continue",
              "precrash_proposal": "fixed, two user turns", "resume_transport": "fake" if args.offline_smoke else "Plan",
              "approval_policy": "same simulated user, decline an unchecked repeat of an unreceipted command",
              "tokens": None, "tokens_spent": 0, "provider_posts": [], "budget_after": None, "cleanup": "pending"}
    temp = None
    paths = [(account, "<account>")] if account is not None else []
    cancelled = False
    try:
        scratch = REPO / "sandbox"
        scratch.mkdir(exist_ok=True)
        temp = tempfile.TemporaryDirectory(prefix="closing-trial-", dir=scratch)
        work = Path(temp.name)
        paths.append((work, "<scratch>"))
        if budget is not None:
            paths.append((budget, "<ledger>"))
        _run_child(args, work, evidence, account, budget, report)
    except KeyboardInterrupt:
        report.update(status="cancelled", passed=None)
        cancelled = True
    except subprocess.TimeoutExpired as error:
        report.update(status="cannot_run", passed=None, error="trial subprocess timed out", _child_stderr=error.stderr)
    except Exception as error:
        report.update(status="cannot_run", passed=None, error=type(error).__name__)
    finally:
        _finalize_trial(report, temp, ledger, last_id if ledger is not None else 0)
        report = _write_report(dst, report, [*paths, (REPO, "<repo>")], keep_diagnostics=args.keep_diagnostics)
    print(dst.relative_to(REPO))
    print(json.dumps(_select(report, ("status", "passed", "tokens", "resume_to_oracle_s", "end", "error", "cleanup", "budget_after"))))
    if cancelled:
        return 130
    return 0 if report["status"] == ("smoke" if args.offline_smoke else "scored") and report["cleanup"] == "done" else 2


if __name__ == "__main__":
    raise SystemExit(main())
