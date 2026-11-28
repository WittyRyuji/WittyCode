"""On-demand session recall: the two judgements, the hard gate and the shared turn ledger.

Fake archives only — the storage callbacks (``latest``/``generation``) are injected, nothing touches the
user's real session data.
"""

from __future__ import annotations

import threading

from desktop.core.harness import compact
from desktop.core.harness.recall_budget import RecallLedger, utf8_bound
from desktop.core.harness.session_recall import recall_history
from desktop.core.session_archive import SessionArchive


def user_item(text: str) -> dict:
    return {"type": "message", "role": "user", "content": [{"type": "input_text", "text": text}]}


def rec(rid: str, text: str, role: str = "user", **extra) -> dict:
    return {"id": rid, "item": {"type": "message", "role": role,
                                "content": [{"type": "input_text", "text": text}]}, **extra}


def gen(parent, records, evicted=None) -> dict:
    evicted = [r["id"] for r in records] if evicted is None else evicted
    return {"header": {"parent": parent, "covers": {"evicted": evicted}}, "records": records}


class Archive:
    """The injected archive callbacks: ``latest`` + ``generation`` with a call log."""

    def __init__(self, snaps: dict[int, dict]):
        self.snaps, self.reads = snaps, []

    def latest(self) -> int | None:
        return max(self.snaps) if self.snaps else None

    def read(self, gid: int) -> dict | None:
        self.reads.append(gid)
        return self.snaps.get(gid)


# ---------------------------------------------------------------------- the gate


def test_a_normal_turn_never_opens_the_archive():
    arc = Archive({1: gen(None, [rec("e1", "我们把超时上限定为 30 秒")])})
    ledger = RecallLedger()
    out = recall_history(query="多找点背景资料", user_text="继续改代码", current_view=[],
                         latest=arc.latest, generation=arc.read, ledger=ledger)
    assert "denied" in out and arc.reads == []
    out = recall_history(query="看看之前的上下文", user_text="刚才压缩过了，看看之前聊了些什么",
                         current_view=[], latest=arc.latest, generation=arc.read, ledger=ledger)
    assert "denied" in out and arc.reads == []


def test_concrete_query_alone_never_opens_the_archive():
    """A query with real words still opens nothing when the user never asked for this session's
    original text and the stated reason is empty or vague."""
    arc = Archive({1: gen(None, [rec("e1", "我们把超时上限定为 30 秒")])})
    for reason in ("", "背景"):
        arc.reads.clear()
        out = recall_history(query="超时上限定为多少", reason=reason, user_text="接着干活",
                             current_view=[], latest=arc.latest, generation=arc.read,
                             ledger=RecallLedger())
        assert "denied" in out and arc.reads == [], reason


def test_declined_and_quoted_asks_do_not_open_the_archive():
    arc = Archive({1: gen(None, [rec("e1", "我们把超时上限定为 30 秒")])})
    ledger = RecallLedger()
    for text in ("别把原话贴出来，给我结论就行", "不用引用原文了", "do not quote my exact words"):
        out = recall_history(query="超时上限", user_text=text, current_view=[],
                             latest=arc.latest, generation=arc.read, ledger=ledger)
        assert "denied" in out, text
    assert arc.reads == []


def test_verbatim_evidence_in_the_view_reads_nothing():
    arc = Archive({1: gen(None, [rec("e1", "别的记录")])})
    view = [user_item("我们把超时上限定为 30 秒"), user_item("把超时上限的原话贴出来")]
    out = recall_history(query="超时上限", user_text="把超时上限的原话贴出来", current_view=view,
                         latest=arc.latest, generation=arc.read, ledger=RecallLedger())
    assert "already in the active view" in out and arc.reads == []
    summary_view = [compact.summary_item("讨论过超时上限的取值。")]
    out = recall_history(query="超时上限", user_text="把超时上限的原话贴出来", current_view=summary_view,
                         latest=arc.latest, generation=arc.read, ledger=RecallLedger())
    assert "already in the active view" not in out and arc.reads == [1]


def test_tool_outputs_and_questions_do_not_cover():
    """A tool result carrying the same words, and the user's own question, are not the user's original
    words — neither closes the archive gate; the single generation is read instead."""
    arc = Archive({1: gen(None, [rec("e1", "我们把超时上限定为 30 秒")])})
    for view in ([{"type": "function_call_output", "output": "我们把超时上限定为 30 秒"}],
                 [user_item("超时上限定为多少？")]):
        arc.reads.clear()
        out = recall_history(query="超时上限", user_text="把超时上限的原话贴出来", current_view=view,
                             latest=arc.latest, generation=arc.read, ledger=RecallLedger())
        assert "already in the active view" not in out and arc.reads == [1], view
        assert "[g1 · e1 · user]" in out and "30 秒" in out


# ---------------------------------------------------------------------- the descent


def test_descends_one_generation_at_a_time_and_stops_when_served():
    arc = Archive({
        3: gen(2, [rec("e3", "端口 8081")]),
        2: gen(1, [rec("e2", "我们把超时上限定为 30 秒")]),
        1: gen(None, [rec("e1", "无关紧要")]),
    })
    out = recall_history(query="超时上限定为多少", reason="需要原话核对", current_view=[],
                         latest=arc.latest, generation=arc.read, ledger=RecallLedger())
    assert arc.reads == [3, 2]
    assert "[g2 · e2 · user]" in out and "30 秒" in out and "delivered" in out

    arc = Archive({3: gen(2, [rec("e3", "端口 8081")]), 2: gen(1, [rec("e2", "我们把超时上限定为 30 秒")])})
    out = recall_history(query="端口", reason="需要原话核对", current_view=[], latest=arc.latest,
                         generation=arc.read, ledger=RecallLedger())
    assert arc.reads == [3] and "[g3 · e3 · user]" in out


def test_evicted_filters_records_already_covered_by_an_older_generation():
    arc = Archive({
        # The same original sits in both dumps but was only newly evicted at generation 1.
        2: gen(1, [rec("e1", "我们把超时上限定为 30 秒"), rec("e2", "端口 8081")], evicted=["e2"]),
        1: gen(None, [rec("e1", "我们把超时上限定为 30 秒")], evicted=["e1"]),
    })
    out = recall_history(query="超时上限", reason="需要原话核对", current_view=[], latest=arc.latest,
                         generation=arc.read, ledger=RecallLedger())
    assert arc.reads == [2, 1] and "[g1 · e1 · user]" in out and "30 秒" in out


def test_provenance_keeps_the_real_author():
    """An assistant record is attributed "assistant", a wrapped tool result "tool" — neither may
    surface under the user's name."""
    arc = Archive({1: gen(None, [
        rec("a1", "建议把超时上限定为 30 秒", role="assistant"),
        {"id": "t1", "item": {"type": "function_call_output", "output": "超时上限定为 30 秒"}},
    ])})
    out = recall_history(query="超时上限", reason="需要原话核对", current_view=[], latest=arc.latest,
                         generation=arc.read, ledger=RecallLedger())
    assert "[g1 · a1 · assistant]" in out and "[g1 · t1 · tool]" in out
    assert " · user]" not in out


def test_weak_association_is_not_evidence():
    arc = Archive({1: gen(None, [rec("e1", "上次复盘的结论", ), rec("e2", "限制说明文档")])})
    out = recall_history(query="超时上限", reason="需要原话核对", current_view=[], latest=arc.latest,
                         generation=arc.read, ledger=RecallLedger())
    assert "insufficient" in out and "no original text found" in out
    assert "[g1 ·" not in out


def test_revision_wins_and_withdrawal_reports_insufficiency():
    arc = Archive({
        3: gen(2, [rec("r8", "硬上限改为 8K", kind="revise", targets=["e-cap"])]),
        2: gen(1, [rec("e-cap", "把硬上限定为 16K")]),
        1: gen(None, [rec("e0", "闲聊")]),
    })
    out = recall_history(query="硬上限", reason="需要原话核对", current_view=[], latest=arc.latest,
                         generation=arc.read, ledger=RecallLedger())
    assert arc.reads == [3] and "8K" in out and "16K" not in out

    arc = Archive({
        3: gen(2, [rec("w1", "不设硬上限了", kind="withdraw", targets=["e-cap"]), rec("e3", "别的")]),
        2: gen(1, [rec("e-cap", "把硬上限定为 16K")]),
        1: gen(None, [rec("e0", "闲聊")]),
    })
    out = recall_history(query="硬上限", reason="需要原话核对", current_view=[], latest=arc.latest,
                         generation=arc.read, ledger=RecallLedger())
    assert arc.reads == [3] and "insufficient" in out and "withdrawn" in out
    assert "16K" not in out and "不设硬上限了" in out


def test_superseded_records_stay_blocked_while_descending():
    arc = Archive({
        2: gen(1, [rec("w1", "不设上限值了", kind="withdraw", targets=["e-cap"])]),
        1: gen(None, [rec("e-cap", "端口上限值定为 16K")]),
    })
    out = recall_history(query="上限值，端口", reason="需要原话核对", current_view=[], latest=arc.latest,
                         generation=arc.read, ledger=RecallLedger())
    # 上限值 is withdrawn at g2; e-cap would answer 端口 at g1 but the withdrawal blocks it forever.
    assert arc.reads == [2, 1] and "16K" not in out
    assert "[g2 · w1" in out and "insufficient" in out


# ---------------------------------------------------------------------- the ledger


def test_the_turn_ledger_caps_delivered_text():
    span = "a" * 2500
    arc = Archive({1: gen(None, [rec("r1", "甲" + span + "乙"), rec("r2", "丙" + span + "丁"),
                                 rec("r3", "戊" + span + "己"), rec("r4", "庚" + span + "辛"),
                                 rec("r5", "壬" + span + "癸")])})
    ledger = RecallLedger()
    out = recall_history(query="甲乙，丙丁，戊己，庚辛，壬癸", reason="需要原话核对", current_view=[],
                         latest=arc.latest, generation=arc.read, ledger=ledger)
    # ~2.4 KB per attributed excerpt under the UTF-8 bound: three whole, one clipped, one refused.
    # The refusal lives in the frame — and the frame itself is only returned while it still fits.
    assert ledger.total <= ledger.limit and out.count("[g1 ·") == 4
    assert "r5" not in ledger.delivered


def test_the_ledger_survives_restart_and_never_redelivers(tmp_path):
    path = tmp_path / "turn-budget.json"
    arc = Archive({1: gen(None, [rec("e1", "我们把超时上限定为 30 秒")])})
    ledger = RecallLedger(path, "t7")
    out = recall_history(query="超时上限", reason="需要原话核对", current_view=[], latest=arc.latest,
                         generation=arc.read, ledger=ledger)
    assert "30 秒" in out and ledger.total > 0
    reopened = RecallLedger(path, "t7")
    assert reopened.total == ledger.total
    arc.reads.clear()
    out = recall_history(query="超时上限", reason="需要原话核对", current_view=[], latest=arc.latest,
                         generation=arc.read, ledger=reopened)
    assert "already delivered" in out and "30 秒" not in out and arc.reads == [1]
    assert RecallLedger(path, "t8").total == 0


def test_parallel_recalls_share_the_one_ledger():
    """Every byte returned to any caller — excerpt, provenance, gap and status line — is charged once to
    the one shared ledger, so eight parallel recalls deliver eight distinct records and the encoded
    return values sum exactly to the ledger total."""
    subjects = ["甲乙", "丙丁", "戊己", "庚辛", "壬癸", "子丑", "寅卯", "辰巳"]
    arc = Archive({1: gen(None, [rec(f"e{i}", f"{subject} " + "b" * 900)
                                for i, subject in enumerate(subjects)])})
    ledger = RecallLedger()
    results = [None] * len(subjects)

    def ask(i):
        results[i] = recall_history(query=subjects[i], reason="需要原话核对", current_view=[],
                                    latest=arc.latest, generation=arc.read, ledger=ledger)

    threads = [threading.Thread(target=ask, args=(i,)) for i in range(len(subjects))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert all(r and "[g1 · e" in r for r in results)
    assert utf8_bound("".join(results)) == ledger.total
    for i in range(len(subjects)):
        assert sum(f"[g1 · e{i} ·" in out for out in results) == 1, f"e{i} delivered twice"


def test_a_spend_that_returns_zero_never_redelivers():
    """The race window: the record passes the delivered check, a parallel call then spends the same key
    first — ``spend`` answering 0 must close the race, not send the excerpt a second time."""

    class RacyLedger(RecallLedger):
        def spend(self, key, text, *, room=None):
            return 0

    arc = Archive({1: gen(None, [rec("e1", "我们把超时上限定为 30 秒")])})
    out = recall_history(query="超时上限", reason="需要原话核对", current_view=[], latest=arc.latest,
                         generation=arc.read, ledger=RacyLedger())
    assert arc.reads == [1] and "[g1 ·" not in out and "30 秒" not in out
    assert "already delivered" in out


def test_a_corrupt_ledger_fails_closed(tmp_path):
    path = tmp_path / "turn-budget.json"
    path.write_text("{not json", encoding="utf-8")
    ledger = RecallLedger(path, "t7")
    assert ledger.corrupt and ledger.remaining() == 0 and ledger.spend("e1", "x") is None
    arc = Archive({1: gen(None, [rec("e1", "我们把超时上限定为 30 秒")])})
    out = recall_history(query="超时上限", reason="需要原话核对", current_view=[], latest=arc.latest,
                         generation=arc.read, ledger=ledger)
    assert "budget is spent" in out and arc.reads == []


# ---------------------------------------------------------------------- the real store


def test_real_archive_uses_the_published_evicted_range(tmp_path):
    """prepare_dump stages the wider pre-compaction candidate in ``header.covers.evicted``;
    ``publish(evicted=subset)`` narrows it to what actually left the window — the top-level range
    ``read_generation`` returns. A staged record that stayed live is not recallable from that
    generation; the next generation that really evicts it owns it."""
    archive = SessionArchive.create(tmp_path)
    instance = archive.enter("devin", "conn-1", "native-1")
    e1, e2 = rec("e1", "我们把超时上限定为 30 秒"), rec("e2", "端口定为 8081")
    prepared = archive.prepare_dump(instance, [e1, e2], evicted=["e1", "e2"])
    archive.publish(prepared, {"text": "前半段摘要"}, evicted=["e1"])

    def latest() -> int | None:
        return archive.latest()[0]

    out = recall_history(query="端口", reason="需要原话核对", current_view=[],
                         latest=latest, generation=archive.read_generation, ledger=RecallLedger())
    assert "[g1 · e2" not in out and "8081" not in out and "insufficient" in out

    out = recall_history(query="超时上限", reason="需要原话核对", current_view=[],
                         latest=latest, generation=archive.read_generation, ledger=RecallLedger())
    assert "[g1 · e1 · user]" in out and "30 秒" in out

    prepared = archive.prepare_dump(instance, [e1, e2], evicted=["e2"])
    archive.publish(prepared, {"text": "第二段摘要"}, evicted=["e2"])
    out = recall_history(query="端口", reason="需要原话核对", current_view=[],
                         latest=latest, generation=archive.read_generation, ledger=RecallLedger())
    assert "[g2 · e2 · user]" in out and "8081" in out
