"""``core.harness.store``: saved conversations as files, with an index that is only a cache of them."""

from __future__ import annotations

import copy
import json
import os
import time

import pytest

from desktop.core.diffs import diff_from_contents, diff_for_new_file
from desktop.core.harness.store import (
    CUT_OFF,
    CUT_OFF_UNKNOWN,
    LOST,
    SavedSession,
    SavedTurn,
    SessionStore,
    diff_from_json,
    diff_to_json,
    pair_calls,
    pair_in_place,
    paired,
    recover,
)


def session(session_id: str = "s1", project: str = "D:\\work\\app", title: str = "第一个会话", modified: float = 100.0) -> SavedSession:
    return SavedSession(
        id=session_id,
        project=project,
        title=title,
        model="gpt-6.1-sol",
        reasoning="medium",
        autonomy="code",
        created_at=50.0,
        modified_at=modified,
        items=[{"type": "message", "role": "user", "content": [{"type": "input_text", "text": "你好"}]}],
        turns=[SavedTurn(0, 50.0, 60.0, "done", "", "gpt-6.1-sol")],
        tools={"c1": {"status": "done", "diffs": []}},
        usage={"input": 10, "output": 5, "cacheRead": 3},
        context_used=42,
        recalled=["m-1"],
    )


def test_a_session_comes_back_exactly_as_saved(tmp_path):
    store = SessionStore(tmp_path)
    store.save(session())
    loaded = store.load("s1")
    assert loaded == session()
    assert loaded.messages == 1


def test_an_unknown_session_is_none(tmp_path):
    assert SessionStore(tmp_path).load("nope") is None


def test_hostile_ids_are_refused_so_a_file_cannot_land_outside_the_folder(tmp_path):
    store = SessionStore(tmp_path)
    for bad in ("..\\evil", "../evil", "a/b", "", "x" * 100, "a.b"):
        with pytest.raises(ValueError):
            store.save(session(bad))
    assert list(tmp_path.rglob("evil*")) == []


def test_rows_are_newest_first_and_filtered_by_project(tmp_path):
    store = SessionStore(tmp_path)
    store.save(session("a", "D:\\work\\app", "old", 100.0))
    store.save(session("b", "D:\\work\\app", "new", 200.0))
    store.save(session("c", "D:\\other", "elsewhere", 300.0))
    assert [row.id for row in store.rows("D:\\work\\app")] == ["b", "a"]
    assert [row.id for row in store.rows("d:\\WORK\\app\\")] == ["b", "a"]  # (Windows paths: case and trailing slash do not matter)
    assert [row.id for row in store.rows()] == ["c", "b", "a"]
    assert [row.id for row in store.rows(limit=1)] == ["c"]
    row = store.rows("D:\\work\\app")[0]
    assert (row.title, row.messages, row.modified_at) == ("new", 1, 200.0)


def test_saving_again_replaces_the_row_and_the_file(tmp_path):
    store = SessionStore(tmp_path)
    store.save(session("a", title="one"))
    store.save(session("a", title="two", modified=500.0))
    assert [(r.title, r.modified_at) for r in store.rows()] == [("two", 500.0)]
    assert store.load("a").title == "two"


def test_delete_removes_the_file_and_the_row(tmp_path):
    store = SessionStore(tmp_path)
    store.save(session("a"))
    store.save(session("b"))
    store.delete("a")
    assert list(json.loads((tmp_path / "index.json").read_text(encoding="utf-8"))["sessions"]) == ["b"], "the index itself, not a rebuild"
    assert store.load("a") is None and [row.id for row in store.rows()] == ["b"]
    store.delete("a")  # (again: nothing to do, no error)


def test_a_missing_or_stale_index_is_rebuilt_from_the_files(tmp_path):
    store = SessionStore(tmp_path)
    store.save(session("a", title="alpha"))
    store.save(session("b", title="beta"))
    (tmp_path / "index.json").unlink()
    assert sorted(r.title for r in store.rows()) == ["alpha", "beta"]
    (tmp_path / "sessions" / "b.json").unlink()  # (a file removed behind the index's back)
    assert [r.title for r in store.rows()] == ["alpha"]


def test_a_corrupt_file_is_skipped_not_fatal(tmp_path):
    store = SessionStore(tmp_path)
    store.save(session("good"))
    (tmp_path / "sessions" / "bad.json").write_text("{ not json", encoding="utf-8")
    (tmp_path / "index.json").unlink()
    assert [r.id for r in store.rows()] == ["good"]
    assert store.load("bad") is None


def test_a_file_of_another_version_is_not_read(tmp_path):
    store = SessionStore(tmp_path)
    store.save(session("a"))
    path = tmp_path / "sessions" / "a.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["version"] = 99
    path.write_text(json.dumps(data), encoding="utf-8")
    assert store.load("a") is None


def test_writes_leave_no_temp_files_behind(tmp_path):
    store = SessionStore(tmp_path)
    for index in range(5):
        store.save(session("a", modified=float(index)))
    assert sorted(p.name for p in (tmp_path / "sessions").iterdir()) == ["a.json"]
    assert sorted(p.name for p in tmp_path.iterdir()) == ["index.json", "sessions"]


def test_chinese_text_is_stored_readably_in_utf8(tmp_path):
    SessionStore(tmp_path).save(session())
    text = (tmp_path / "sessions" / "s1.json").read_bytes().decode("utf-8")
    assert "你好" in text and "第一个会话" in text


def test_diffs_survive_the_round_trip():
    changed = diff_from_contents("a.py", "x = 1\ny = 2\n", "x = 1\ny = 3\n")
    created = diff_for_new_file("b.txt", "新文件\n第二行\n")
    for diff in (changed, created):
        assert diff_from_json(json.loads(json.dumps(diff_to_json(diff)))) == diff
    assert diff_from_json("nonsense") is None and diff_from_json({"lines": [[1]]}) is None


# ----------------------------------------------------------------------------------------------- a turn the application did not live to end


def user(text: str = "go") -> dict:
    return {"type": "message", "role": "user", "content": [{"type": "input_text", "text": text}]}


def answer(text: str = "ok") -> dict:
    return {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": text}]}


def fc(call_id: str) -> dict:
    return {"type": "function_call", "call_id": call_id, "name": "shell", "arguments": "{}"}


def out(call_id: str, text: str = "done") -> dict:
    return {"type": "function_call_output", "call_id": call_id, "output": text}


def lost(call_id: str) -> dict:
    return out(call_id, LOST)


REASONING = {"type": "reasoning", "summary": [], "encrypted_content": "enc"}


def test_a_history_where_every_call_has_its_output_is_given_back_as_it_is():
    items = [user(), REASONING, fc("c1"), fc("c2"), out("c1"), out("c2"), user("also"), answer()]
    assert pair_calls(items) is items


def test_a_call_left_without_an_output_gets_lost_after_the_outputs_of_its_step():
    items = [user(), answer("on it"), fc("c1"), fc("c2"), out("c1")]
    before = [dict(item) for item in items]
    assert pair_calls(items) == [user(), answer("on it"), fc("c1"), fc("c2"), out("c1"), lost("c2")]
    assert items == before, "the list given is not changed (it may be the loop's own history)"


def test_the_outputs_come_after_the_whole_answer_of_the_model_in_the_order_of_the_calls():
    items = [user(), REASONING, fc("c1"), answer("and then"), fc("c2")]
    assert pair_calls(items) == [user(), REASONING, fc("c1"), answer("and then"), fc("c2"), lost("c1"), lost("c2")]


def test_a_missing_output_goes_before_the_next_step_or_the_next_message_of_the_user():
    assert pair_calls([user(), fc("c1"), fc("c2"), out("c1"), answer("next step")]) == [user(), fc("c1"), fc("c2"), out("c1"), lost("c2"), answer("next step")]
    assert pair_calls([user(), fc("c1"), out("c1"), fc("c2"), out("c2"), fc("c3")]) == [user(), fc("c1"), out("c1"), fc("c2"), out("c2"), fc("c3"), lost("c3")]
    assert pair_calls([user(), fc("c1"), user("next turn"), answer()]) == [user(), fc("c1"), lost("c1"), user("next turn"), answer()]


def test_an_output_without_a_call_before_it_and_a_second_output_for_a_call_are_left_out():
    items = [user(), out("ghost"), fc("c1"), out("c1"), out("c1", "again"), answer()]
    assert pair_calls(items) == [user(), fc("c1"), out("c1"), answer()]


def test_an_output_answers_one_call_only_even_when_the_model_repeats_a_call_id():
    assert pair_calls([user(), fc("c1"), out("c1"), fc("c1"), user("next")]) == [user(), fc("c1"), out("c1"), fc("c1"), lost("c1"), user("next")]


def test_an_output_before_its_call_is_not_taken_for_its_result():
    assert pair_calls([user(), out("c1", "too early"), fc("c1")]) == [user(), fc("c1"), lost("c1")]


def test_an_output_written_after_the_next_message_is_brought_back_to_its_step():
    items = [user(), fc("c1"), user("next turn"), answer(), out("c1", "stopped late")]
    assert pair_calls(items) == [user(), fc("c1"), out("c1", "stopped late"), user("next turn"), answer()]


def test_recover_ends_a_turn_saved_while_it_ran_as_interrupted_at_the_last_save_and_says_why():
    first = SavedTurn(0, 100.0, 130.0, "done", "", "m")
    running = SavedTurn(2, 140.0, None, "running", "", "m")
    items = [user("one"), answer(), user("two"), answer("on it"), fc("c1"), lost("c1")]
    saved = SavedSession(id="s", project="p", modified_at=150.0, items=list(items), turns=[first, running])
    assert recover(saved)
    assert saved.items == items, "already a valid history"
    assert (running.status, running.finished, running.note) == ("interrupted", 150.0, CUT_OFF_UNKNOWN.format(count=1))
    assert (first.status, first.finished, first.note) == ("done", 130.0, "")
    assert not recover(saved), "nothing left to recover the second time"


def test_recover_without_a_lost_result_says_only_that_the_turn_did_not_finish():
    turn = SavedTurn(0, 100.0, None, "running", "", "m")
    saved = SavedSession(id="s", project="p", modified_at=90.0, items=[user(), fc("c1"), out("c1")], turns=[turn])
    assert recover(saved)
    assert (turn.status, turn.finished, turn.note) == ("interrupted", 100.0, CUT_OFF), "never before its own start"


def test_recover_repairs_a_file_of_the_earlier_version_and_moves_the_turns_that_follow():
    cut_off = SavedTurn(0, 100.0, None, "done", "", "m")  # (the earlier version: a running turn said "done", with no end)
    later = SavedTurn(2, 200.0, 210.0, "done", "", "m")
    saved = SavedSession(id="s", project="p", modified_at=300.0, items=[user("one"), fc("c1"), user("two"), answer()], turns=[cut_off, later])
    assert recover(saved)
    assert saved.items == [user("one"), fc("c1"), lost("c1"), user("two"), answer()]
    assert later.start == 3 and saved.items[later.start] == user("two")
    assert (cut_off.status, cut_off.finished, cut_off.note) == ("interrupted", 200.0, CUT_OFF_UNKNOWN.format(count=1)), "it ended when the next one began"
    assert (later.status, later.note) == ("done", "")


def test_recover_drops_an_output_without_its_call_and_moves_the_turns_back():
    one, two = SavedTurn(0, 100.0, 110.0), SavedTurn(3, 120.0, 130.0)
    saved = SavedSession(id="s", project="p", items=[user("one"), out("ghost"), answer(), user("two"), answer()], turns=[one, two])
    assert recover(saved)
    assert saved.items == [user("one"), answer(), user("two"), answer()] and two.start == 2
    assert (one.status, two.status) == ("done", "done")


def test_recover_leaves_a_finished_conversation_alone():
    saved = session()
    saved.turns.append(SavedTurn(0, 70.0, 80.0, "limit", "", "m", "这一轮用了 80 步还没结束"))
    saved.turns.append(SavedTurn(0, 90.0, 95.0, "interrupted", "", "m"))
    before = copy.deepcopy(saved)
    assert not recover(saved)
    assert saved == before


def test_the_note_and_the_statuses_of_this_version_survive_the_round_trip(tmp_path):
    store = SessionStore(tmp_path)
    saved = session()
    saved.turns = [SavedTurn(0, 50.0, 60.0, "limit", "", "m", "这一轮用了 80 步还没结束，先停在这里；可以回复「继续」。"), SavedTurn(0, 61.0, None, "running", "", "m")]
    store.save(saved)
    assert store.load("s1") == saved


def test_a_turn_without_a_note_in_a_file_of_the_earlier_version_loads(tmp_path):
    store = SessionStore(tmp_path)
    store.save(session())
    path = tmp_path / "sessions" / "s1.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    del data["turns"][0]["note"]
    data["turns"][0]["unknown_later_field"] = 1
    path.write_text(json.dumps(data), encoding="utf-8")
    assert store.load("s1") == session()


# ----------------------------------------------------------------------------------------------- the model's view: compaction


def _msg(text: str, role: str = "user") -> dict:
    kind = "input_text" if role == "user" else "output_text"
    return {"type": "message", "role": role, "content": [{"type": kind, "text": text}]}


def _call(call_id: str) -> dict:
    return {"type": "function_call", "call_id": call_id, "name": "echo", "arguments": "{}"}


def _out(call_id: str, output: str = "ok") -> dict:
    return {"type": "function_call_output", "call_id": call_id, "output": output}


COMPACTION = {"upto": 2, "summary": "Earlier: the user wanted a parser.", "at": 10.0, "before": 9000, "after": 1200, "model": "m", "mechanical": False}


def test_a_compaction_is_saved_and_comes_back(tmp_path):
    store = SessionStore(tmp_path)
    saved = session()
    saved.items = [_msg("one"), _msg("two", "assistant"), _msg("three")]
    saved.compaction = dict(COMPACTION)
    store.save(saved)
    assert store.load("s1").compaction == COMPACTION
    assert json.loads((tmp_path / "sessions" / "s1.json").read_text(encoding="utf-8"))["compaction"]["upto"] == 2


def test_a_file_without_a_compaction_has_none_and_does_not_write_one(tmp_path):
    store = SessionStore(tmp_path)
    store.save(session())
    assert "compaction" not in json.loads((tmp_path / "sessions" / "s1.json").read_text(encoding="utf-8"))
    assert store.load("s1").compaction == {}


@pytest.mark.parametrize(
    "broken",
    [
        {"upto": 2},
        {"upto": 2, "summary": "   "},
        {"upto": -1, "summary": "s"},
        {"upto": 99, "summary": "s"},
        {"upto": "2", "summary": "s"},
        {"upto": True, "summary": "s"},
        {"upto": 1, "summary": 7},
        "not a mapping",
    ],
)
def test_a_compaction_the_model_could_not_be_given_is_ignored_and_the_whole_log_is_read(tmp_path, broken):
    store = SessionStore(tmp_path)
    saved = session()
    saved.items = [_msg("one"), _msg("two", "assistant"), _msg("three")]
    store.save(saved)
    path = tmp_path / "sessions" / "s1.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["compaction"] = broken
    path.write_text(json.dumps(data), encoding="utf-8")
    assert store.load("s1").compaction == {}


def test_pairing_moves_the_turns_and_the_place_the_summary_stops_with_their_items():
    saved = SavedSession(
        id="s", project="p", items=[_msg("one"), _call("c1"), _msg("two"), _call("c2"), _out("c2"), _msg("three")],
        turns=[SavedTurn(0, 1.0, 2.0), SavedTurn(2, 3.0, 4.0), SavedTurn(5, 5.0, None, "running")],
        compaction={"upto": 2, "summary": "s"},
    )
    assert pair_in_place(saved, "gone")
    assert saved.items[2] == _out("c1", "gone"), "the call c1 had no output: one is written before the next message"
    assert [turn.start for turn in saved.turns] == [0, 3, 6] and saved.compaction["upto"] == 3
    assert saved.items[saved.turns[1].start] == _msg("two") and saved.items[saved.compaction["upto"]] == _msg("two")


def test_a_snapshot_copy_follows_the_pairing_and_leaves_the_log_alone():
    saved = SavedSession(id="s", project="p", items=[_msg("one"), _call("c1"), _msg("two")], turns=[SavedTurn(0, 1.0), SavedTurn(2, 2.0)], compaction={"upto": 2, "summary": "s"})
    copy_ = paired(saved)
    assert copy_ is not saved and copy_.items[2] == _out("c1", LOST) and [turn.start for turn in copy_.turns] == [0, 3] and copy_.compaction["upto"] == 3
    assert len(saved.items) == 3 and [turn.start for turn in saved.turns] == [0, 2] and saved.compaction["upto"] == 2, "the live log is not touched"
    assert copy_.turns[0] is not saved.turns[0]


def test_a_conversation_whose_calls_are_all_answered_is_its_own_snapshot():
    saved = SavedSession(id="s", project="p", items=[_msg("one"), _call("c1"), _out("c1")], turns=[SavedTurn(0, 1.0)])
    assert paired(saved) is saved and not pair_in_place(saved)


def test_recover_keeps_the_compaction_pointing_at_the_same_item():
    saved = SavedSession(
        id="s", project="p", modified_at=9.0, items=[_msg("one"), _call("c1"), _msg("two"), _msg("three")],
        turns=[SavedTurn(0, 1.0, None, "running"), SavedTurn(2, 3.0, 4.0)], compaction={"upto": 2, "summary": "s"},
    )
    assert recover(saved)
    assert saved.items[saved.compaction["upto"]] == _msg("two")