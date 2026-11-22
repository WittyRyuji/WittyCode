"""``read_output`` inside an archive-bound session: this turn's own tool output reads directly because the
runtime — not the model — proved the reference belongs to the live turn. A reference an earlier turn's log
recorded is session-archive evidence and passes only through the turn's shared recall ledger: need-gated,
charged before delivery, never re-delivered, impossible to page past the 8,192 bound or reset by a restart.
A hash that never appeared in a tool result — listed, pasted or guessed — is refused outright.

Fake controllers and archives only — ``_archive_binding`` is a stub, ``saved`` a namespace; nothing touches the
user's real session data.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from desktop.core.harness import outputs, recall_tool
from desktop.core.harness.loop import Call
from desktop.core.harness.outputs import OutputStore
from desktop.core.harness.recall_budget import FILENAME, LIMIT, RecallLedger
from desktop.core.harness.tools import ToolContext, ToolRegistry
from desktop.core.harness.toolset import HarnessTools

run = asyncio.run


class FakeArchive:
    """The piece of ``SessionArchive`` ``HistoryRecall`` reads: ``dir`` for the shared book."""

    def __init__(self, root: Path):
        self.dir = root

    def latest(self):
        return (None, None)

    def read_generation(self, generation: int):
        return None


def controller(tmp_path: Path, *, user_text: str = "继续", items: list[dict] | None = None,
               start: int | None = 0, window: int | None = None, archive: bool = True,
               turn: Any = "default") -> Any:
    """A controller stub with the attributes ``HistoryRecall`` reads. ``saved.turns[-1].start`` is where the
    live turn's log begins; ``items[:start]`` are earlier turns'."""
    if turn == "default":
        turn = SimpleNamespace(id="t-1", user_block=SimpleNamespace(text=user_text))
    turns = [] if start is None else [SimpleNamespace(start=start, finished=None)]
    return SimpleNamespace(
        _turn=turn,
        saved=SimpleNamespace(items=list(items or []), compaction=None, prompt={}, turns=turns),
        session_id="s-1",
        running_model="model-x",
        manager=SimpleNamespace(model_window=lambda model: window),
        _archive_binding=SimpleNamespace(archive=FakeArchive(tmp_path) if archive else None),
    )


def tools(tmp_path: Path, ctl: Any | None = None, **context: Any) -> HarnessTools:
    context.setdefault("outputs", OutputStore(tmp_path / "out"))
    ctx = ToolContext(project=tmp_path, **context)
    if ctl is not None:
        recall_tool.bind_history(ctx, ctl)
    return HarnessTools(ToolRegistry.default(), ctx, "code")


def read_call(reference: str, **args: Any) -> Call:
    return Call("i1", "c1", "read_output", "", {"reference": reference, **args})


def _this_turn(ctl: Any, *refs: str) -> None:
    """Make ``refs`` provable outputs of the live turn: logged at indices at or after ``turns[-1].start``."""
    start = ctl.saved.turns[-1].start
    ctl.saved.items = ([{}] * start + [{"type": "function_call_output", "wc_output_ref": ref} for ref in refs])


def _older(ctl: Any, ref: str) -> None:
    """Make ``ref`` a provable output of an earlier turn: logged before ``turns[-1].start``."""
    ctl.saved.items = [{"type": "function_call_output", "wc_output_ref": ref},
                       {"type": "message", "role": "user"}]
    ctl.saved.turns[-1].start = 1


# ---------------------------------------------------------------------- provenance tiers


def test_this_turns_output_reads_directly(tmp_path):
    ctl = controller(tmp_path)
    t = tools(tmp_path, ctl)
    ref = t.context.outputs.put("line one\nline two\n" + "x" * 3000)
    _this_turn(ctl, ref)
    outcome = run(t.run(read_call(ref, limit=2000)))
    assert not outcome.is_error
    assert "line one" in outcome.output and "line two" in outcome.output
    assert not (tmp_path / FILENAME).exists()  # the live turn's own output needs no ledger and opens no book


def test_an_older_turns_output_is_ledger_charged(tmp_path):
    ctl = controller(tmp_path)
    t = tools(tmp_path, ctl)
    ref = t.context.outputs.put("OLD-OUTPUT-" + "y" * 5000)
    _older(ctl, ref)
    denied = run(t.run(read_call(ref)))  # no reason, no explicit ask
    assert denied.is_error and "concrete reason" in denied.output
    out = run(t.run(read_call(ref, limit=1500, reason="核对上一回合长输出的原值")))
    assert not out.is_error and "OLD-OUTPUT-" in out.output
    book = RecallLedger(tmp_path / FILENAME, "t-1")
    assert any(key.startswith(f"#out:{ref}:") for key in book.delivered)
    assert 0 < book.total <= LIMIT  # frame included: the whole page was charged before delivery


def test_a_hash_never_in_a_tool_result_is_refused_even_when_asked(tmp_path):
    # An explicit user ask opens proven evidence, never a hash the runtime never recorded: listed, pasted or
    # guessed references are not authorization, whatever the user said.
    ctl = controller(tmp_path, user_text="把上一回合那个输出的原文贴出来")
    t = tools(tmp_path, ctl)
    ref = t.context.outputs.put("orphaned retained output " + "z" * 2000)
    for reason in ("", "核对上一回合长输出的原值"):
        outcome = run(t.run(read_call(ref, reason=reason)))
        assert outcome.is_error and "not a tool result" in outcome.output
    assert not (tmp_path / FILENAME).exists()


def test_no_live_turn_fails_closed(tmp_path):
    ctl = controller(tmp_path, turn=None)
    t = tools(tmp_path, ctl)
    ref = t.context.outputs.put("Q" * 1000)
    _this_turn(ctl, ref)
    outcome = run(t.run(read_call(ref, reason="核对上一回合长输出的原值")))
    assert outcome.is_error and "cannot prove" in outcome.output
    assert not (tmp_path / FILENAME).exists()


def test_an_unbound_context_keeps_the_plain_read(tmp_path):
    ctl = controller(tmp_path)
    t = tools(tmp_path)  # never bound: no archive authorization, the legacy read stays
    ref = t.context.outputs.put("plain " + "q" * 2500)
    assert "plain" in run(t.run(read_call(ref, limit=100))).output
    ctl._archive_binding.archive = None  # a binding without an archive disables the gate, not the read
    t2 = tools(tmp_path, ctl)
    t2.context.outputs = t.context.outputs
    assert "plain" in run(t2.run(read_call(ref, limit=100))).output


def test_the_users_decline_closes_old_reads(tmp_path):
    ctl = controller(tmp_path, user_text="不用贴原话")
    t = tools(tmp_path, ctl)
    ref = t.context.outputs.put("D" * 3000)
    _older(ctl, ref)
    outcome = run(t.run(read_call(ref, reason="核对上一回合长输出的原值")))
    assert outcome.is_error and "denied" in outcome.output


def test_listing_is_free_but_never_authorizes(tmp_path):
    ctl = controller(tmp_path)
    t = tools(tmp_path, ctl)
    ref = t.context.outputs.put("L" * 3000)
    _older(ctl, ref)
    listing = run(t.run(Call("i1", "c1", "read_output", "", {})))
    assert ref in listing.output  # seeing a hash in the list is not authorization to read it
    denied = run(t.run(read_call(ref, limit=100)))
    assert denied.is_error and "concrete reason" in denied.output


# ---------------------------------------------------------------------- the shared ledger


def test_the_same_page_is_not_delivered_twice(tmp_path):
    ctl = controller(tmp_path)
    t = tools(tmp_path, ctl)
    ref = t.context.outputs.put("S" * 6000)
    _older(ctl, ref)
    args = {"limit": 1500, "reason": "核对上一回合长输出的原值"}
    first = run(t.run(read_call(ref, **args)))
    assert "SSS" in first.output
    spent = RecallLedger(tmp_path / FILENAME, "t-1").total
    again = run(t.run(read_call(ref, **args)))
    assert "already delivered" in again.output and "SSS" not in again.output
    assert RecallLedger(tmp_path / FILENAME, "t-1").total == spent  # a retried block is not billed twice


def test_paging_an_old_output_is_bounded_by_the_turn_budget(tmp_path):
    ctl = controller(tmp_path)
    t = tools(tmp_path, ctl)
    ref = t.context.outputs.put("P" * 6000)
    _older(ctl, ref)
    RecallLedger(tmp_path / FILENAME, "t-1").spend("#out:seed:1-10", "s" * 7900)  # earlier evidence already spent
    out = run(t.run(read_call(ref, limit=1500, reason="核对上一回合长输出的原值")))
    assert not out.is_error
    assert "insufficient" in out.output and "PPP" not in out.output  # the page did not fit the turn's budget


def test_restart_keeps_the_book_and_the_dedupe(tmp_path):
    ctl = controller(tmp_path)
    t = tools(tmp_path, ctl)
    ref = t.context.outputs.put("R" * 4000)
    _older(ctl, ref)
    args = {"limit": 1500, "reason": "核对上一回合长输出的原值"}
    run(t.run(read_call(ref, **args)))
    spent = RecallLedger(tmp_path / FILENAME, "t-1").total
    # a later run in the same turn reads the same disk book — the charge was not reset
    again = run(t.run(read_call(ref, **args)))
    assert "already delivered" in again.output
    assert RecallLedger(tmp_path / FILENAME, "t-1").total == spent
