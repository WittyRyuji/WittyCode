"""``core.importers.convert``: an imported conversation as a ChatGPT Plan conversation the harness can save, show and go
on with."""

from __future__ import annotations

import json
import re

from desktop.core.chatgpt.history import build_turns
from desktop.core.chatgpt.responses import build_request, user_message
from desktop.core.harness.store import SavedSession, SessionStore, _pair, pair_calls, recover
from desktop.core.harness.tools import ToolRegistry
from desktop.core.importers import Message, SessionInfo, saved_id, session_key, to_saved_session
from desktop.core.transcript import Transcript, TurnStatus

INFO = SessionInfo(
    key=session_key("claude", "11111111-2222"), app="claude", native_id="11111111-2222", title="修一下构建", project="D:\\work\\app",
    started=1000.0, updated=1100.0, messages=5, size=1234, path="C:\\x.jsonl",
)
MESSAGES = [
    Message("user", "修一下构建", 1000.0),
    Message("assistant", "先看看。\n[工具] Bash npm run build", 1010.0),
    Message("assistant", "修好了。", 1020.0),
    Message("user", "再跑一次测试", 1050.0),
    Message("assistant", "[工具] Bash npm test\n都过了。", 1090.0),
]


def saved(messages=MESSAGES, info=INFO) -> SavedSession:
    return to_saved_session(info, messages, model="gpt-6.1-sol", reasoning="medium", autonomy="code", now=5000.0)


def test_each_user_message_starts_a_turn_and_the_items_are_responses_messages():
    session = saved()
    assert session.items == [
        user_message("修一下构建"),
        {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "先看看。\n[工具] Bash npm run build", "annotations": []}]},
        {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "修好了。", "annotations": []}]},
        user_message("再跑一次测试"),
        {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "[工具] Bash npm test\n都过了。", "annotations": []}]},
    ]
    assert [(t.start, t.at, t.finished, t.status, t.model) for t in session.turns] == [(0, 1000.0, 1020.0, "done", "Claude Code"), (3, 1050.0, 1090.0, "done", "Claude Code")]
    assert (session.title, session.project, session.model, session.reasoning, session.autonomy) == ("修一下构建", "D:\\work\\app", "gpt-6.1-sol", "medium", "code")
    assert (session.created_at, session.modified_at) == (1000.0, 1100.0)
    assert session.origin == {"app": "claude", "native": "11111111-2222", "at": 5000.0}
    assert session.messages == 2


def test_the_id_is_the_same_for_the_same_conversation_and_valid_for_the_store():
    first, second = saved(), saved()
    assert first.id == second.id == saved_id(INFO.key)
    assert re.fullmatch(r"[A-Za-z0-9_-]{1,64}", first.id) and first.id.startswith("im-claude-")
    other = SessionInfo(**{**{f: getattr(INFO, f) for f in INFO.__slots__}, "key": session_key("claude", "other"), "native_id": "other"})
    assert saved(info=other).id != first.id
    assert saved_id(session_key("codex", "11111111-2222")) != first.id


def test_importing_twice_replaces_the_conversation_in_the_store(tmp_path):
    store = SessionStore(tmp_path)
    store.save(saved())
    store.save(saved(MESSAGES[:2]))
    rows = store.rows()
    assert len(rows) == 1 and rows[0].id == saved().id and rows[0].messages == 1


def test_it_saves_reloads_and_shows_like_a_conversation_of_the_harness(tmp_path):
    store = SessionStore(tmp_path)
    original = saved()
    store.save(original)
    loaded = store.load(original.id)
    assert loaded == original
    assert not recover(loaded)
    turns = build_turns(Transcript(), ToolRegistry.default(), loaded)
    assert [turn.status for turn in turns] == [TurnStatus.DONE, TurnStatus.DONE]
    assert [turn.user_block.text for turn in turns] == ["修一下构建", "再跑一次测试"]
    texts = [[block.text for block in turn.blocks if block.kind == "text"] for turn in turns]
    assert texts == [["先看看。\n[工具] Bash npm run build", "修好了。"], ["[工具] Bash npm test\n都过了。"]]
    assert [turn.model for turn in turns] == ["Claude Code", "Claude Code"]
    row = store.rows("D:\\work\\app")[0]
    assert (row.title, row.messages, row.modified_at) == ("修一下构建", 2, 1100.0)


def test_the_history_is_one_the_next_request_can_send():
    session = saved()
    assert _pair(session.items) is None and pair_calls(session.items) is session.items
    assert not any(item["type"] != "message" for item in session.items)
    request = build_request(model="gpt-6.1-sol", instructions="x", items=session.items, tools=[], reasoning="medium")
    assert request["input"] == session.items
    json.dumps(request)


def test_answers_before_any_question_still_form_a_turn():
    session = saved([Message("assistant", "hello from before", 900.0), Message("user", "hi", 1000.0)])
    assert [(t.start, t.at) for t in session.turns] == [(0, 900.0), (1, 1000.0)]
    turns = build_turns(Transcript(), ToolRegistry.default(), session)
    assert turns[0].user_block is None and [b.text for b in turns[0].blocks if b.kind == "text"] == ["hello from before"]


def test_messages_without_times_take_the_time_before_them_and_empty_ones_are_left_out():
    session = saved([Message("user", "a", None), Message("assistant", "   ", 2000.0), Message("system", "x", 1.0), Message("assistant", "b", None), Message("user", "c", 1500.0), Message("assistant", "d", 1400.0)])
    assert [item["content"][0]["text"] for item in session.items] == ["a", "b", "c", "d"]
    assert [(t.at, t.finished) for t in session.turns] == [(1000.0, 1000.0), (1500.0, 1500.0)]
    assert session.modified_at == 1500.0


def test_without_a_start_time_the_first_message_time_or_now_is_used():
    no_time = SessionInfo(**{**{f: getattr(INFO, f) for f in INFO.__slots__}, "started": 0.0, "updated": 0.0})
    assert saved([Message("user", "a", 700.0)], no_time).created_at == 700.0
    assert saved([Message("user", "a", None)], no_time).created_at == 5000.0
