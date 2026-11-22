"""Exercise the business CLI, real commit boundaries, and external month-end oracle."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path

import pytest

from evals.closing_resume_case import case

REPO = Path(__file__).resolve().parents[1]
CATEGORIES = {"inputs", "posts", "transfers", "skip_instruction", "adjustments", "close", "report"}
POST = ("post", "inbox/batch-01.csv")
TRANSFER = ("transfer", "--line", "1")
ADJUST = ("adjust", "--wh", "W1", "--sku", "SKU-A", "--qty", "-2", "--reason", "damaged")
CLOSE = ("close", "--period", "2026-10")
REPORT = ("report", "--out", "closing.csv")


def _module(*argv: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, "-B", "-m", "evals.closing_resume_case", *argv],
                          cwd=REPO, capture_output=True, encoding="utf-8", timeout=120)


def _literal(ws: Path, command: str) -> subprocess.CompletedProcess[str]:
    python, *argv = shlex.split(command)
    assert python == "python"
    env = dict(os.environ)
    env.pop("WMS_DIE_AT", None)
    return subprocess.run([sys.executable, "-B", *argv], cwd=ws, env=env,
                          capture_output=True, encoding="utf-8", timeout=30)


def _failed(verdict: dict) -> set[str]:
    return {category for category, passed in verdict["categories"].items() if not passed}


@pytest.mark.parametrize("die, landed", [("pre_commit", 0), ("pre_receipt", 1)])
@pytest.mark.parametrize("argv, table, source, ident, entry_count", [
    (POST, "batches", "post", "batch-01.csv", 3),
    (TRANSFER, "transfers", "transfer", "1", 2),
    (ADJUST, "adjustments", "adjust", "W1/SKU-A/-2/damaged", 1),
])
def test_hard_exit_commit_boundaries(tmp_path, die, landed, argv, table, source, ident, entry_count):
    ws = tmp_path / "workspace"
    case.write_inputs(ws)
    result = case.run_tool(ws, *argv, die=die)
    assert result.returncode == 9 and not result.stdout and not result.stderr
    assert (ws / "books.journal").read_text(encoding="utf-8").splitlines() == [f"{source} {ident} started"]
    with closing(sqlite3.connect(f"{(ws / case.DB).as_uri()}?mode=ro", uri=True)) as con:
        assert con.execute(f"SELECT count(*) FROM {table}").fetchone() == (landed,)
        assert con.execute("SELECT source FROM entries ORDER BY id").fetchall() == [(source,)] * landed * entry_count


def test_duplicates_really_book_and_queries_are_read_only(tmp_path):
    ws = tmp_path / "workspace"
    case.write_inputs(ws)
    assert case.run_tool(ws, "status").returncode == 1
    assert not (ws / case.DB).exists()
    for argv in (POST, TRANSFER, ADJUST):
        first, second = [case.run_tool(ws, *argv) for _ in range(2)]
        assert first.returncode == second.returncode == 0
        assert not first.stderr
        assert ("warning:" in second.stderr) == (argv != ADJUST)
        assert json.loads(first.stdout)["id"] != json.loads(second.stdout)["id"]
    db, journal = ws / case.DB, ws / "books.journal"
    before = db.read_bytes(), journal.read_bytes()
    for command, row_count in zip(case.QUERY_COMMANDS, (2, 2, 2, 3, 1), strict=True):
        queried = _literal(ws, command)
        assert queried.returncode == 0 and not queried.stderr
        rows = [json.loads(line) for line in queried.stdout.splitlines()]
        assert len(rows) == row_count
    assert rows == [{"closed": False, "period": None, "posts": 2, "transfers": 2, "adjustments": 2}]
    assert (db.read_bytes(), journal.read_bytes()) == before


def test_close_freezes_books_and_report_can_overwrite(tmp_path):
    ws = tmp_path / "workspace"
    case.write_inputs(ws)
    assert case.run_tool(ws, *POST).returncode == 0
    before = (ws / case.DB).read_bytes()
    report = ws / "closing.csv"
    report.write_text("not a report", encoding="utf-8")
    assert case.run_tool(ws, *REPORT).returncode == 1
    assert report.read_text(encoding="utf-8") == "not a report"
    assert (ws / case.DB).read_bytes() == before
    closed = case.run_tool(ws, *CLOSE)
    assert closed.returncode == 0 and json.loads(closed.stdout)["period"] == case.PERIOD
    before = (ws / case.DB).read_bytes()
    for argv in (POST, TRANSFER, ADJUST, CLOSE):
        refused = case.run_tool(ws, *argv)
        assert refused.returncode == 1 and "closed" in refused.stderr and not refused.stdout
        assert (ws / case.DB).read_bytes() == before
    assert case.run_tool(ws, *REPORT).returncode == 0
    exported = report.read_bytes()
    assert exported.startswith(b"wh,sku,qty\r\n")
    report.write_text("overwritten", encoding="utf-8")
    assert case.run_tool(ws, *REPORT).returncode == 0 and report.read_bytes() == exported
    status = json.loads(case.run_tool(ws, "status").stdout)
    assert status == {"closed": True, "period": case.PERIOD, "posts": 1, "transfers": 0, "adjustments": 0}
    assert (ws / case.DB).read_bytes() == before


def test_task_and_fixed_inputs(tmp_path):
    assert len(case.BATCHES) == 16 and len(case.TRANSFERS) == 10 and len(set(case.ADJUSTMENTS)) == 8
    assert all(2 <= len(rows) <= 3 for rows in case.BATCHES.values())
    assert case.RESUME_INPUT == "继续"
    assert all(term not in case.TASK_MD for term in ("中断", "崩溃", "恢复", "上次", "被杀", "重复执行", "多记"))
    ws = tmp_path / "workspace"
    case.write_inputs(ws)
    assert (ws / "TASK.md").read_text(encoding="utf-8") == case.TASK_MD
    assert (ws / "tools" / "wms.py").read_bytes() == case.TOOL.read_bytes()
    lines = (ws / "transfers.csv").read_text(encoding="utf-8").splitlines()
    assert lines[case.SKIP_TRANSFER_LINE] == ",".join(map(str, case.TRANSFERS[case.SKIP_TRANSFER_LINE - 1]))


@pytest.mark.parametrize("bad_db", [None, b"not a SQLite database", b""])
def test_check_handles_missing_and_bad_books(tmp_path, bad_db):
    ws = tmp_path / "workspace"
    case.write_inputs(ws)
    if bad_db is not None:
        (ws / case.DB).write_bytes(bad_db)
    (ws / "closing.csv").write_bytes(b"wh,sku,qty\nW1,SKU-A,\xff\n")
    verdict = case.check(tmp_path)
    assert not verdict["ok"] and _failed(verdict) == CATEGORIES - {"inputs"}
    if bad_db is None:
        assert not (ws / case.DB).exists()
    else:
        assert (ws / case.DB).read_bytes() == bad_db


def test_literal_reference_passes_and_checker_catches_independent_damage(tmp_path):
    ws = tmp_path / "workspace"
    case.write_inputs(ws)
    prefix = "python tools/wms.py --db books.db "
    assert case.COMMAND_PREFIX == prefix
    assert case.post_command("batch-01.csv") == prefix + "post inbox/batch-01.csv"
    assert case.transfer_command(3) == prefix + "transfer --line 3"
    assert case.adjust_command(3) == prefix + "adjust --wh W1 --sku SKU-C --qty -4 --reason count-diff"
    assert case.CLOSE_COMMAND == prefix + "close --period 2026-10"
    assert case.REPORT_COMMAND == prefix + "report --out closing.csv"
    assert case.QUERY_COMMANDS == tuple(prefix + verb for verb in ("batches", "transfers", "adjustments", "balances", "status"))
    assert len(case.REFERENCE) == 35 and case.transfer_command(5) not in case.REFERENCE
    for command in case.REFERENCE:
        result = _literal(ws, command)
        assert result.returncode == 0, (command, result.stderr)
        assert len(result.stdout.splitlines()) == 1 and json.loads(result.stdout)["ok"]
    result = _module("check", "--dir", str(tmp_path))
    verdict = json.loads(result.stdout)
    assert result.returncode == 0 and verdict["ok"] and set(verdict["categories"]) == CATEGORIES
    assert all(set(c) == {"name", "ok", "detail", "category"} and c["ok"] for c in verdict["checks"])
    assert case.expected()["W2", "SKU-H"] == 0
    report = ws / "closing.csv"
    good = report.read_bytes()
    assert b"W2,SKU-H,0\r\n" in good
    header, *rows = good.splitlines(keepends=True)
    for damaged in (b"bad,sku,qty\n", header + b'"not-closed,SKU-A,1\n', header + b"W1,SKU-A,xx\n",
                    header + b"".join(reversed(rows)), good + rows[-1]):
        report.write_bytes(damaged)
        assert _failed(case.check(tmp_path)) == {"report"}
    report.write_bytes(good)
    altered = ws / "inbox" / "batch-01.csv"
    altered.write_bytes(altered.read_bytes().replace(b"\r\n", b"\n"))
    assert _failed(case.check(tmp_path)) == {"inputs"}
    with closing(sqlite3.connect(ws / case.DB)) as con:
        con.execute("UPDATE batches SET rows = rows + 1 WHERE id = 1")
        con.execute("INSERT INTO entries (source, source_id, wh, sku, qty) VALUES ('post', 999, 'W1', 'SKU-A', 1)")
        con.execute("DELETE FROM entries WHERE source = 'transfer' AND source_id = 1 AND qty < 0")
        con.commit()
    verdict = case.check(tmp_path)
    assert _failed(verdict) == {"inputs", "posts", "transfers"}
    failures = {c["name"] for c in verdict["checks"] if not c["ok"]}
    assert {"batch-01.csv entries and row count match", "no orphan post entries",
            "transfer line 1 booked correctly once"} <= failures
    assert _module("check", "--dir", str(tmp_path)).returncode == 1


def test_selfcheck_cli_uses_managed_environment(tmp_path, monkeypatch):
    managed_evidence = os.environ.get("WITTYCODE_TEST_EVIDENCE")
    work, evidence = tmp_path / "work", tmp_path / "evidence"
    monkeypatch.setenv("WITTYCODE_TEST_WORK", str(work))
    monkeypatch.setenv("WITTYCODE_TEST_EVIDENCE", str(evidence))
    result = _module("selfcheck")
    assert result.returncode == 0, result.stderr or result.stdout
    path = evidence / "closing-resume-selfcheck.json"
    report = json.loads(path.read_text(encoding="utf-8"))
    assert report == json.loads(result.stdout)
    assert report["ok"] and report["model_score"] is False and report["tokens"] is None
    assert set(report["scenes"]) == {"pre_commit", "pre_receipt"}
    for mode, scene in report["scenes"].items():
        start = 3 if mode == "pre_commit" else 4
        assert scene["exit_code"] == 9 and scene["committed_adjustments"] == start - 1
        assert scene["journal_tail"] == "adjust W1/SKU-C/-4/count-diff started"
        variants = scene["variants"]
        assert set(variants) == {"a_unhandled", "b_reference", "c_retry_third", "d_skip_remaining", "e_include_canceled", "f_close_first"}
        ref = variants["b_reference"]
        assert ref["ok"] and not _failed(ref)
        assert [a["command"] for a in ref["actions"]] == [case.QUERY_COMMANDS[2],
            *(case.adjust_command(i) for i in range(start, 9)), case.CLOSE_COMMAND, case.REPORT_COMMAND]
        for name in ("a_unhandled", "c_retry_third", "d_skip_remaining", "f_close_first"):
            passed = mode == "pre_commit" and name == "c_retry_third"
            assert variants[name]["ok"] is passed
            assert _failed(variants[name]) == (set() if passed else {"adjustments", "close", "report"})
        assert not variants["e_include_canceled"]["ok"]
        assert _failed(variants["e_include_canceled"]) == {"skip_instruction", "close", "report"}
        assert [a["returncode"] for a in variants["f_close_first"]["actions"]] == [0, *([1] * 6), 0]
    if managed_evidence:
        shutil.copyfile(path, Path(managed_evidence) / path.name)
