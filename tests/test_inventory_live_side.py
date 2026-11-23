"""``evals.inventory_resume_case.live_side`` / ``live_tools``: offline interface tests only.

Nothing here touches a real account or the network: the loopback ``FakeResponses`` stands in for the model in both
phases — phase 1's single deterministic proposal (the scene build, never a model score) and the smoke's scripted
post-reopen turns that exercise the runner's mechanics. A real run's recovery is never scripted anywhere.

Covered: the allowlist accepts only the fixed business forms and refuses everything else (no shell, no path
assembly); the restricted registry really posts through the ledger argv; the end-to-end smoke drives the scene
build, the product's own crash checkpoint, ``engine.open_saved`` recovery with the resume gate armed, and the
external oracle — in both commit modes.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from evals.inventory_resume_case import case, live_side, live_tools

REPO = Path(__file__).resolve().parents[1]
MODEL = "gpt-6.1-sol"
LEDGER = "python tools/ledger.py --db books.db"


def _ctx(ws: Path) -> Any:
    from desktop.core.harness.tools import ToolContext

    return ToolContext(project=ws)


# ------------------------------------------------------------------ the allowlist


def test_match_accepts_only_the_fixed_business_forms(tmp_path):
    ws = tmp_path / "ws"
    (ws / "inbox").mkdir(parents=True)
    (ws / "tools").mkdir()

    post = live_tools.match(f"{LEDGER} post inbox/batch-4.csv", ws, python="PY")
    assert post and post.kind == "run" and post.die, "the interrupted batch's post is the die-hooked one"
    assert list(post.argv[3:]) == ["books.db", "post", "inbox/batch-4.csv"]
    early = live_tools.match(f"{LEDGER} post inbox/batch-1.csv", ws)
    assert early and not early.die, "an early batch post carries no die hook"
    for verb in ("batches", "balances"):
        assert live_tools.match(f"{LEDGER} {verb}", ws)
    assert live_tools.match(f"{LEDGER} reconcile --out reconcile.csv", ws)
    read = live_tools.match("type TASK.md", ws)
    assert read and read.kind == "read" and read.path.name == "TASK.md"
    assert live_tools.match("cat inbox/batch-2.receipt", ws).kind == "read"
    assert live_tools.match("dir", ws).kind == "list"
    assert live_tools.match("dir inbox", ws).kind == "list"

    refused = [
        "", "python -c print(1)", f"{LEDGER} post ../meta.json", f"{LEDGER} post inbox/../../x.csv",
        f"{LEDGER} post inbox/batch-1.csv extra", f"{LEDGER} drop", f"{LEDGER} batches; del /f x",
        f"{LEDGER} batches && whoami", "type ../meta.json", "type books.db", "type inbox/../books.journal",
        "Get-Content C:\\Windows\\win.ini", "dir ..", "dir /s", "powershell -c id", f"{LEDGER} POST inbox/batch-4.csv",
        f"{LEDGER} post 'inbox/batch-4.csv'", f"{LEDGER} --db ..\\books.db batches",
    ]
    for text in refused:
        assert live_tools.match(text, ws) is None, text


def test_registry_runs_the_real_ledger_and_refuses_the_rest(tmp_path):
    ws = tmp_path / "ws"
    (ws / "inbox").mkdir(parents=True)
    (ws / "tools").mkdir()
    shutil.copyfile(case.TOOL, ws / "tools" / "ledger.py")
    case._write_csv(ws / "inbox" / "batch-1.csv", case.BATCHES["batch-1.csv"])
    reg = live_tools.registry(ws)

    out = asyncio.run(reg.run("shell", {"command": "del /f books.db"}, _ctx(ws)))
    assert out.is_error and "Refused" in out.output
    out = asyncio.run(reg.run("shell", {"command": f"{LEDGER} post inbox/batch-1.csv"}, _ctx(ws)))
    assert not out.is_error, out.output
    with sqlite3.connect(ws / "books.db") as con:
        names = [n for (n,) in con.execute("SELECT name FROM batches")]
    assert names == ["batch-1.csv"], "the allowed command really posted through subprocess argv"
    out = asyncio.run(reg.run("shell", {"command": f"{LEDGER} balances"}, _ctx(ws)))
    assert not out.is_error and "SKU-101" in out.output


# ------------------------------------------------------------------ the end-to-end offline smoke


def _recovery_turns(mode: str) -> list[Any]:
    from tests.fake_responses import call, say

    if mode == "pre_commit":
        return [
            call("shell", {"command": f"{LEDGER} batches"}, call_id="s1", item_id="fs1"),
            call("shell", {"command": f"{LEDGER} post inbox/batch-4.csv"}, call_id="s2", item_id="fs2"),
            call("shell", {"command": f"{LEDGER} reconcile --out reconcile.csv"}, call_id="s3", item_id="fs3"),
            say("done"),
        ]
    return [
        call("shell", {"command": f"{LEDGER} batches"}, call_id="s1", item_id="fs1"),
        call("shell", {"command": f"{LEDGER} reconcile --out reconcile.csv"}, call_id="s2", item_id="fs2"),
        say("done"),
    ]


@pytest.mark.parametrize("mode", ("pre_commit", "pre_receipt"))
def test_live_side_smoke_both_modes(tmp_path, monkeypatch, mode):
    """The full mechanism offline: scene build → product crash checkpoint → open_saved → scripted smoke turns →
    the real external oracle. ``phase2_factory`` replaces ``plan_meter``; every recovery action is scripted here —
    which is exactly what a real run never does."""
    from tests.chatgpt_support import ACCESS_TOKEN, make_engine
    from tests.fake_responses import DEFAULT_MODELS, FakeResponses

    from desktop.core.chatgpt import controller as controller_module

    monkeypatch.setattr(controller_module, "SAVE_DELAY_SECONDS", live_side.SAVE_DELAY)
    work = tmp_path / mode
    account = tmp_path / "account"
    account.mkdir()
    (account / "state.json").write_text("{}", encoding="utf-8")  # a placeholder: the fake engine never reads it
    budget = tmp_path / "budget.json"
    budget.write_text("{}", encoding="utf-8")

    fake2 = FakeResponses(models=[{**DEFAULT_MODELS[0], "slug": MODEL, "display_name": MODEL}], token=ACCESS_TOKEN)

    def factory(_account, root, _budget, *, model, reasoning):
        return make_engine(root, fake2)  # same engine layout as the runner expects: root/chatgpt/sessions

    cfg = {"work": work, "mode": mode, "model": MODEL, "reasoning": "high", "account": account,
           "budget_file": budget, "seconds": 60.0, "side": "smoke", "phase2_factory": factory,
           "smoke": lambda: fake2.script(*_recovery_turns(mode))}
    with fake2:
        result = asyncio.run(live_side.Runner(cfg).run())

    assert result["status"] == "scored", json.dumps(result, ensure_ascii=False)[:1500]
    events = {e["event"]: e for e in result["trace"]}
    kept = events["evidence_kept"]
    assert kept["unreceipted"] == ["call_crash_post"], "the kept checkpoint must leave the post unreceipted"
    assert events["crash_checkpoint"]["killed_desktop_process"] is False
    from importlib.util import find_spec

    gated = find_spec("desktop.core.harness.resume") is not None
    assert events["reopen"]["pending_candidates"] == int(gated), "only a product with the resume gate tracks the lost call"
    assert result["oracle"]["ok"], json.dumps(result["oracle"], ensure_ascii=False)[:2000]
    assert result["passed"]
    names = [row["name"] for row in result["posts"]]
    assert sorted(names) == sorted(case.BATCHES) and len(names) == len(case.BATCHES)
    assert result["prompts"]["approvals"] >= 2, "the crash post plus at least one resumed call were approved"
    sessions = list((work / "engine" / "chatgpt" / "sessions").glob("*.json"))
    assert sessions, "the product's own session file is the evidence"
    data = json.loads(sessions[0].read_text(encoding="utf-8"))
    outs = {i.get("call_id"): i.get("output") for i in data["items"] if i.get("type") == "function_call_output"}
    assert "Result unknown" in str(outs.get("call_crash_post")), "recover pairs the lost call with the LOST output"


def test_cli_refuses_a_non_product_source(tmp_path):
    bad = tmp_path / "not-a-product"
    bad.mkdir()
    rc = live_side.main(["--source", str(bad), "--work", str(tmp_path / "w"), "--account", str(tmp_path),
                         "--budget-file", str(tmp_path / "b"), "--mode", "pre_commit", "--model", "x",
                         "--out", str(tmp_path / "out.json")])
    assert rc == 2
    out = json.loads((tmp_path / "out.json").read_text(encoding="utf-8"))
    assert out["status"] == "cannot_run"


def test_cli_refuses_a_foreign_loaded_product(tmp_path, monkeypatch):
    """A tree that holds a desktop/ marker but whose imports resolve elsewhere is refused — the active product
    must never be loaded in place of the frozen one."""
    fake_src = tmp_path / "fake-src"
    (fake_src / "desktop" / "core").mkdir(parents=True)
    account = tmp_path / "acct"
    account.mkdir()
    (account / "state.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(live_side, "load_product", lambda source: (_ for _ in ()).throw(
        live_side.CannotRun("module resolves outside the frozen tree")))
    rc = live_side.main(["--source", str(fake_src), "--work", str(tmp_path / "w"), "--account", str(account),
                         "--budget-file", str(tmp_path / "b"), "--mode", "pre_receipt", "--model", "x",
                         "--out", str(tmp_path / "out.json")])
    assert rc == 2
    assert "frozen tree" in json.loads((tmp_path / "out.json").read_text(encoding="utf-8"))["note"]
