"""The Fusion main turn's last answer stays an internal candidate until the turn settles (design: A held as wait-input,
then one merged answer — A plus the continuation's new information — publishes exactly once).

Live, after publication and after a reopen the draft never renders as the answer; it stays in the log for the model
(the ``wc_candidate`` mark is stripped before anything leaves the app) and the continuation asks for the merge.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from desktop.core.fusion.candidate import CANDIDATE, Staging, mark_candidates, unflag
from desktop.core.harness.history import message_text
from desktop.core.transcript import TextBlockView, TurnStatus
from tests.chatgpt_support import finish_turn
from tests.fake_responses import cut, fail, say
from tests.fakes import until
from tests.test_fusion_identity_runtime import _fusion, _turn
from tests.test_fusion_rating_runtime import _rate, _read, _record

GATES = ("drive", "brief", "report", "inject")
pytestmark = pytest.mark.fusion_gates(*GATES)


def _texts(turn):
    return [block.text for block in turn.blocks if isinstance(block, TextBlockView)]


def test_the_held_draft_never_reaches_the_page_and_one_merged_answer_publishes(tmp_path):
    draft = "Draft: the count was 41."
    merged = "Merged: kept the 41 count, now verified as 42."

    async def go():
        async with _fusion(tmp_path, active=GATES) as (main, fake):
            source = await _record(main)
            # The unread record owes a rating: the continuation request is held at the server so the in-between
            # state is observable while the merged answer is still pending.
            fake.script(_read(source), say(draft, item_id="msg_1"),
                        [("hold", "gap"), *_rate(source, -1, "The count was wrong.")], say(merged, item_id="msg_1"))
            assert main.send_prompt("Check the inventory.")
            await until(lambda: len(fake.requests) >= 3)
            turn = main._turn
            assert turn is not None
            assert not any(draft in text for text in _texts(turn))  # held back, not early-visible
            assert turn.tools  # tool progress stayed visible the whole time
            flagged = [item for item in main.saved.items if item.get(CANDIDATE)]
            assert len(flagged) == 1 and draft in message_text(flagged[0])
            sent = json.dumps(fake.requests[2]["input"])
            assert draft in sent  # A reaches the model as material…
            assert "internal draft" in sent and "Rate the Fusion Drive records" in sent  # …with the merge instruction.
            assert CANDIDATE not in sent  # The private mark never leaves the app.
            fake.release("gap")
            await finish_turn(main)
            assert _texts(turn) == [merged] and "41" in merged and "42" in merged
            assert flagged[0].get(CANDIDATE)  # superseded: the mark stays, the draft never publishes
            final = next(item for item in main.saved.items if item.get("type") == "message"
                         and item.get("role") == "assistant" and merged in message_text(item))
            assert CANDIDATE not in final  # unmarked the moment it published
            # Both answers reused the same item id: the held registry could never unmark the superseded draft.
            assert final.get("id") == flagged[0].get("id") == "msg_1"
            await main.close()
            reopened = await main.manager.open_saved(main.project, main.session_id)
            try:
                assert _texts(reopened.transcript.turns[-1]) == [merged]  # a rebuilt history agrees
            finally:
                await reopened.close()
    asyncio.run(go())


def test_without_a_continuation_the_answer_publishes_as_it_always_did(tmp_path):
    async def go():
        async with _fusion(tmp_path, active=GATES) as (main, fake):
            await _turn(main, fake, say("Checked."))
            assert _texts(main.transcript.turns[-1]) == ["Checked."] and len(fake.requests) == 1
            assert not any(item.get(CANDIDATE) for item in main.saved.items)
    asyncio.run(go())


def test_asks_without_a_continuation_release_the_draft(tmp_path):
    # The turn is owed a rating but ended incomplete: no continuation sends it back, so its last words are
    # the answer after all — published once, unmarked, never replaced.
    draft = "Partial: the answer stopped."

    async def go():
        async with _fusion(tmp_path, active=GATES) as (main, fake):
            source = await _record(main)
            await _turn(main, fake, _read(source), cut(draft))
            assert _texts(main.transcript.turns[-1]) == [draft] and len(fake.requests) == 2
            assert not any(item.get(CANDIDATE) for item in main.saved.items)
    asyncio.run(go())


def test_cancelling_keeps_the_buffered_answer_hidden(tmp_path):
    # Stop mid-stream with a full answer buffered: the turn is interrupted, not settled, so the draft
    # stays internal — nothing publishes, the mark survives, and a reopened history agrees.
    draft = "Draft: interrupted before it finished."

    async def go():
        async with _fusion(tmp_path, active=GATES) as (main, fake):
            fake.script([*say(draft)[:-1], ("hold", "stuck"), say(draft)[-1]])
            assert main.send_prompt("Say it.")
            await until(lambda: len(fake.requests) == 1)
            await asyncio.sleep(0.2)  # the stream is blocked after the last text event: the draft is fully buffered
            await main.interrupt()
            turn = main.transcript.turns[-1]
            assert turn.status is TurnStatus.INTERRUPTED
            assert _texts(turn) == []
            flagged = [item for item in main.saved.items if item.get(CANDIDATE)]
            assert len(flagged) == 1 and draft in message_text(flagged[0])
            fake.release("stuck")
            await main.close()
            reopened = await main.manager.open_saved(main.project, main.session_id)
            try:
                assert _texts(reopened.transcript.turns[-1]) == []
            finally:
                await reopened.close()
    asyncio.run(go())


def test_a_failed_continuation_keeps_the_draft_hidden(tmp_path):
    # The rating continuation errors out instead of answering: the turn failed, so the held draft
    # is buried with it — the page shows the failure, not an answer that was never verified.
    draft = "Draft: still owed a rating."

    async def go():
        async with _fusion(tmp_path, active=GATES) as (main, fake):
            source = await _record(main)
            fake.script(_read(source), say(draft), fail("invalid_request_error", "continuation refused"))
            assert main.send_prompt("Check the inventory.")
            await finish_turn(main)
            turn = main.transcript.turns[-1]
            assert turn.status is TurnStatus.FAILED
            assert _texts(turn) == []
            flagged = [item for item in main.saved.items if item.get(CANDIDATE)]
            assert len(flagged) == 1 and draft in message_text(flagged[0])
            await main.close()
            reopened = await main.manager.open_saved(main.project, main.session_id)
            try:
                assert _texts(reopened.transcript.turns[-1]) == []
            finally:
                await reopened.close()
    asyncio.run(go())


def test_the_mark_covers_only_what_is_still_buffered():
    staging = Staging()
    staging.delta("m1", "first ")
    staging.delta("m1", "draft")
    staging.done("m2", "second draft")
    flushed = staging.flush()  # Published as progress before the batch was saved.
    assert [item_id for item_id, _, _ in flushed] == ["m1", "m2"]
    staging.delta("m3", "held")
    items = [
        {"type": "message", "id": "m1", "role": "assistant", "content": [{"type": "output_text", "text": "first draft"}]},
        {"type": "reasoning"},
        {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "held"}]},  # no id: ACP items match by text
        {"type": "function_call"},
    ]
    flagged = mark_candidates(items, staging)
    assert items[0].get(CANDIDATE) is None and items[2].get(CANDIDATE) is True and flagged == {"m3": items[2]}
    assert unflag(flagged, ["m3"]) is True and CANDIDATE not in items[2] and not flagged
    assert unflag(flagged, ["m3"]) is False
