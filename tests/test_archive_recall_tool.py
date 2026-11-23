"""``recall_history`` as a built-in tool: declared only under a live archive binding, answered through
``HarnessTools`` by the gate/walk/ledger service, budgeted by the shared turn ledger, and never replayable
through ``read_output``.

Fake controllers and archives only — ``_archive_binding`` is a stub, ``saved`` a namespace; nothing touches the
user's real session data.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from desktop.core.harness import outputs, recall_tool
from desktop.core.harness.loop import ALLOW, DENY, Call
from desktop.core.harness.recall_budget import FILENAME, RecallLedger
from desktop.core.harness.tools import ToolContext, ToolRegistry
from desktop.core.harness.toolset import IDENTITY_REFUSAL, HarnessTools

run = asyncio.run


def rec(rid: str, text: str, role: str = "user", **extra) -> dict:
    return {"id": rid, "item": {"type": "message", "role": role,
                                "content": [{"type": "input_text", "text": text}]}, **extra}


def gen(parent, records, evicted=None) -> dict:
    evicted = [r["id"] for r in records] if evicted is None else evicted
    return {"header": {"parent": parent, "covers": {"evicted": evicted}}, "records": records}


def user_item(text: str) -> dict:
    return {"type": "message", "role": "user", "content": [{"type": "input_text", "text": text}]}


class FakeArchive:
    """The piece of ``SessionArchive`` the tool reads: ``latest()[0]``, ``read_generation`` and ``dir``."""

    def __init__(self, snaps: dict[int, dict], root: Path | None = None):
        self.snaps, self.reads, self._dir = snaps, [], root

    @property
    def dir(self) -> Path | None:
        return self._dir

    def latest(self) -> tuple[int | None, int | None]:
        return (max(self.snaps), None) if self.snaps else (None, None)

    def read_generation(self, generation: int) -> dict | None:
        self.reads.append(generation)
        return self.snaps.get(generation)


def controller(tmp_path: Path, snaps: dict[int, dict], *, user_text: str = "继续",
               items: list[dict] | None = None, window: int | None = None,
               archive: bool | None = True, turn: Any = "default") -> Any:
    """A controller stub with the attributes ``HistoryRecall`` reads."""
    if turn == "default":
        turn = SimpleNamespace(id="t-1", user_block=SimpleNamespace(text=user_text))
    ctl = SimpleNamespace(
        _turn=turn,
        saved=SimpleNamespace(items=items or [], compaction=None, prompt={}),
        session_id="s-1",
        running_model="model-x",
        manager=SimpleNamespace(model_window=lambda model: window),
    )
    if archive is not None:
        ctl._archive_binding = SimpleNamespace(archive=FakeArchive(snaps, tmp_path) if archive else None)
    return ctl


def tools(tmp_path: Path, ctl: Any | None = None, **context: Any) -> HarnessTools:
    ctx = ToolContext(project=tmp_path, **context)
    if ctl is not None:
        recall_tool.bind_history(ctx, ctl)
    return HarnessTools(ToolRegistry.default(), ctx, "code")


def call(question: str, reason: str = "") -> Call:
    return Call("i1", "c1", recall_tool.NAME, "", {"question": question, "reason": reason})


# ---------------------------------------------------------------------- declaration gating


def test_no_binding_keeps_everything_as_it_was(tmp_path):
    t = tools(tmp_path)
    assert recall_tool.NAME not in [d["name"] for d in t.declarations()]
    assert not t.known(recall_tool.NAME)
    verdict = t.check(call("超时上限"))
    assert verdict.verdict == DENY and recall_tool.UNAVAILABLE in verdict.reason
    outcome = run(t.run(call("超时上限")))
    assert outcome.is_error and recall_tool.UNAVAILABLE in outcome.output


def test_a_binding_without_an_archive_declares_nothing(tmp_path):
    for ctl in (controller(tmp_path, {}, archive=None),          # binding itself absent
                controller(tmp_path, {}, archive=False)):        # binding present, archive None
        t = tools(tmp_path, ctl)
        assert recall_tool.NAME not in [d["name"] for d in t.declarations()]
        assert not t.known(recall_tool.NAME)
        outcome = run(t.run(call("超时上限")))
        assert outcome.is_error and recall_tool.UNAVAILABLE in outcome.output


def test_a_late_binding_still_enables_the_tool(tmp_path):
    ctl = controller(tmp_path, {}, archive=None)
    t = tools(tmp_path, ctl)
    assert recall_tool.NAME not in [d["name"] for d in t.declarations()]
    ctl._archive_binding = SimpleNamespace(archive=FakeArchive({1: gen(None, [rec("e1", "我们把超时上限定为 30 秒")])}, tmp_path))
    assert recall_tool.NAME in [d["name"] for d in t.declarations()]
    assert t.known(recall_tool.NAME)


# ---------------------------------------------------------------------- the call


def test_a_bound_session_recalls_the_archived_original(tmp_path):
    ctl = controller(tmp_path, {1: gen(None, [rec("e1", "我们把超时上限定为 30 秒")])})
    t = tools(tmp_path, ctl)
    ask = call("超时上限定为多少", "核对当时定的超时上限原值")
    assert t.check(ask).verdict == ALLOW
    outcome = run(t.run(ask))
    assert not outcome.is_error
    assert "[g1 · e1 · user]" in outcome.output and "30 秒" in outcome.output
    assert ctl._archive_binding.archive.reads == [1]


def test_the_answer_in_the_active_view_reads_nothing(tmp_path):
    ctl = controller(tmp_path, {1: gen(None, [rec("e1", "别的记录")])},
                     items=[user_item("我们把超时上限定为 30 秒")])
    t = tools(tmp_path, ctl)
    outcome = run(t.run(call("超时上限", "核对超时上限的原文")))
    assert "already in the active view" in outcome.output
    assert ctl._archive_binding.archive.reads == []


def test_a_vague_ask_never_opens_the_archive(tmp_path):
    ctl = controller(tmp_path, {1: gen(None, [rec("e1", "我们把超时上限定为 30 秒")])},
                     user_text="继续改代码")
    t = tools(tmp_path, ctl)
    for question, reason in (("多找点背景资料", ""), ("多找点背景资料", "想要更多上下文"), ("超时上限", "")):
        outcome = run(t.run(call(question, reason)))
        assert outcome.is_error and "denied" in outcome.output, (question, reason)
    assert ctl._archive_binding.archive.reads == []


def test_the_users_own_verbatim_ask_authorizes_the_reason(tmp_path):
    # The user's explicit ask for this session's original words carries the authority a `reason` would.
    ctl = controller(tmp_path, {1: gen(None, [rec("e1", "我们把超时上限定为 30 秒")])},
                     user_text="把超时上限的原话贴出来")
    t = tools(tmp_path, ctl)
    outcome = run(t.run(call("超时上限")))
    assert "30 秒" in outcome.output and "denied" not in outcome.output
    assert ctl._archive_binding.archive.reads == [1]


def test_the_real_user_message_gates_not_the_injected_or_tool_text(tmp_path):
    # The decline lives only in turn.user_block.text; the view carries an unrelated user message.
    ctl = controller(tmp_path, {1: gen(None, [rec("e1", "我们把超时上限定为 30 秒")])},
                     user_text="不用引用原话了，给我结论就行",
                     items=[user_item("超时上限之前怎么定的？")])
    t = tools(tmp_path, ctl)
    outcome = run(t.run(call("超时上限")))
    assert outcome.is_error and "denied" in outcome.output
    assert ctl._archive_binding.archive.reads == []


def test_the_arguments_cannot_open_the_archive_by_themselves(tmp_path):
    ctl = controller(tmp_path, {1: gen(None, [rec("e1", "我们把超时上限定为 30 秒")])})
    t = tools(tmp_path, ctl)
    for args in ({}, {"question": ""}, {"question": "   "}, {"question": 7}):
        outcome = run(t.run(Call("i1", "c1", recall_tool.NAME, "", args)))
        assert outcome.is_error, args
    outcome = run(t.run(Call("i1", "c1", recall_tool.NAME, "", [1, 2])))  # type: ignore[arg-type]
    assert outcome.is_error
    assert ctl._archive_binding.archive.reads == []


# ---------------------------------------------------------------------- the shared ledger


def test_no_live_turn_refuses_instead_of_opening_a_book(tmp_path):
    for turn in (None, SimpleNamespace(user_block=SimpleNamespace(text="继续"))):  # no turn, or a turn without an id
        ctl = controller(tmp_path, {1: gen(None, [rec("e1", "我们把超时上限定为 30 秒")])}, turn=turn)
        t = tools(tmp_path, ctl)
        outcome = run(t.run(call("超时上限")))
        assert outcome.is_error and "no live user turn" in outcome.output
        assert ctl._archive_binding.archive.reads == []
        assert not (tmp_path / FILENAME).exists()


def test_calls_of_one_turn_share_the_disk_ledger(tmp_path):
    snaps = {1: gen(None, [rec("e1", "我们把超时上限定为 30 秒")])}
    ctl = controller(tmp_path, snaps)
    t = tools(tmp_path, ctl)
    first = run(t.run(call("超时上限", "核对超时上限的原值")))
    assert "30 秒" in first.output
    book = json.loads((tmp_path / FILENAME).read_text("utf-8"))
    assert book["turn"] == "t-1" and book["total"] > 0
    # The status frame that follows the evidence is charged like the evidence, under its own one-off key.
    assert "e1" in book["spent"] and any(key.startswith("#frame:") for key in book["spent"])
    second = run(t.run(call("超时上限", "核对超时上限的原值")))
    assert "already delivered" in second.output and "30 秒" not in second.output
    reopened = RecallLedger(tmp_path / FILENAME, "t-1")
    assert reopened.total == book["total"] and reopened.delivered == frozenset(book["spent"])


def test_the_room_cap_limits_what_a_call_may_deliver(tmp_path):
    ctl = controller(tmp_path, {1: gen(None, [rec("e1", "我们把超时上限定为 30 秒")])}, window=1)
    t = tools(tmp_path, ctl)
    outcome = run(t.run(call("超时上限", "核对超时上限的原值")))
    assert "recall budget is spent" in outcome.output
    assert ctl._archive_binding.archive.reads == []


# ---------------------------------------------------------------------- replay containment


def test_a_big_result_is_never_replayable_through_read_output(tmp_path):
    secret = "机密决定原文 " + "超时上限定为 30 秒，" * 300  # > RETAIN_CHARS, so retention would want it
    ctl = controller(tmp_path, {1: gen(None, [rec("e1", secret)])})
    t = tools(tmp_path, ctl, outputs=outputs.OutputStore(tmp_path / "store"))
    outcome = run(t.run(call("超时上限", "核对超时上限的原始决定")))
    assert "30 秒" in outcome.output and len(outcome.output) > outputs.RETAIN_CHARS
    # What got attached is a real, readable reference — to the non-evidence note, never to the archive text.
    assert outcome.reference
    kept = t.context.outputs.read(outcome.reference)
    assert "30 秒" not in kept and "deliberately not retained" in kept
    # This turn's log proves the placeholder belongs to it — reading it back gives the note, not archive text.
    ctl.saved.items = [{"type": "function_call_output", "wc_output_ref": outcome.reference}]
    ctl.saved.turns = [SimpleNamespace(start=0, finished=None)]
    replayed = outputs.read_output({"reference": outcome.reference}, t.context)
    assert "30 秒" not in replayed.output


# ---------------------------------------------------------------------- Fusion experts


def test_an_expert_never_sees_the_parent_sessions_recall(tmp_path):
    ctl = controller(tmp_path, {1: gen(None, [rec("e1", "我们把超时上限定为 30 秒")])})
    t = tools(tmp_path, ctl)
    t.identity_tools = frozenset({"read", "edit", "exec"})
    assert recall_tool.NAME not in [d["name"] for d in t.declarations()]
    assert t.identity_refusal(recall_tool.NAME) == IDENTITY_REFUSAL
    verdict = t.check(call("超时上限"))
    assert verdict.verdict == DENY and verdict.reason == IDENTITY_REFUSAL
    outcome = run(t.run(call("超时上限")))
    assert outcome.is_error and "Refused" in outcome.output
    assert ctl._archive_binding.archive.reads == []
