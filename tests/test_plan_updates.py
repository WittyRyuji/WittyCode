"""Accepted plan snapshots follow update order rather than the position of a tool in the transcript."""
from __future__ import annotations

import pytest

from desktop.core.devin import items
from desktop.core.devin.history import Replay
from desktop.core.transcript import ToolStatus, Transcript
from desktop.core.viewmodel import turn_json


def steps(first="pending", second="pending"):
    return [{"content": "检查", "status": first}, {"content": "验证", "status": second}]


def write(transcript, turn, call_id, todos, status=ToolStatus.DONE):
    tool = transcript.ensure_tool(turn, call_id, "TodoWrite")
    transcript.set_tool_input(turn, tool, {"todos": todos}, final=True)
    transcript.finish_tool(turn, tool, "Checklist updated.", status=status)
    return tool


def test_current_plan_follows_updates_to_existing_rows_and_ignores_duplicate_completions():
    transcript = Transcript()
    turn = transcript.start_turn("检查计划")
    first = write(transcript, turn, "first", steps("in_progress"))
    second = write(transcript, turn, "second", steps("completed", "in_progress"))
    assert turn_json(turn)["plan"]["id"] == second.id
    before = turn.version
    transcript.set_tool_input(turn, first, {"todos": steps("completed", "completed")}, final=True)
    snapshot = turn_json(turn)["plan"]
    assert snapshot == {"id": first.id, "steps": [{"text": "检查", "state": "completed"}, {"text": "验证", "state": "completed"}]}
    assert turn.version > before
    transcript.finish_tool(turn, second, "Repeated completion", status=ToolStatus.DONE)
    transcript.set_tool_input(turn, second, second.input, final=True)
    assert turn_json(turn)["plan"] == snapshot, "duplicate output and identical old input cannot resurrect an older plan"
    assert list(turn.tools) == ["first", "second"], "the transcript's historical row order is unchanged"


@pytest.mark.parametrize("bad", [None, {}, "not a checklist", [{"content": "", "status": "completed"}], [{"content": "partial", "status": "unknown"}], [None]])
def test_partial_or_failed_plan_updates_do_not_erase_the_accepted_snapshot(bad):
    transcript = Transcript()
    turn = transcript.start_turn("检查计划")
    write(transcript, turn, "accepted", steps("completed", "in_progress"))
    snapshot = turn_json(turn)["plan"]
    write(transcript, turn, "invalid", bad)
    write(transcript, turn, "failed", steps("completed", "completed"), ToolStatus.ERROR)
    assert turn_json(turn)["plan"] == snapshot


def test_native_plan_replay_updates_and_clears_without_fabricating_tool_calls():
    transcript = Transcript()
    replay = Replay(transcript)
    replay.feed("session/update", {"update": {"sessionUpdate": "user_message_chunk", "content": {"type": "text", "text": "检查"}}})
    for todos in (steps("in_progress"), steps("completed", "in_progress"), steps("completed", "completed")):
        replay.feed("session/update", {"update": {"sessionUpdate": "plan", "entries": todos}})
    (turn,) = replay.finish()
    assert [step["state"] for step in turn_json(turn)["plan"]["steps"]] == ["completed", "completed"]
    assert not turn.tools and turn.stats().steps == 0

    replay = Replay(Transcript())
    replay.feed("session/update", {"update": {"sessionUpdate": "plan", "entries": steps("in_progress")}})
    replay.feed("session/update", {"update": {"sessionUpdate": "plan", "entries": []}})
    assert turn_json(replay.finish()[0])["plan"]["steps"] == []


def test_devin_todo_alias_and_updated_raw_input_replay_use_the_new_snapshot():
    assert items.tool_spec({"toolCallId": "todo_write:1", "rawInput": {"todos": steps()}}) == ("TodoWrite", {"todos": steps()})
    replay = Replay(Transcript())
    replay.feed("session/update", {"update": {"sessionUpdate": "tool_call", "toolCallId": "todo_write:1", "rawInput": {"todos": steps("in_progress")}}})
    replay.feed("session/update", {"update": {"sessionUpdate": "tool_call_update", "toolCallId": "todo_write:1", "rawInput": {"todos": steps("completed", "completed")}, "status": "completed"}})
    (turn,) = replay.finish()
    assert [step["state"] for step in turn_json(turn)["plan"]["steps"]] == ["completed", "completed"]


def test_completed_plan_calls_need_no_separate_completion_event_and_invalid_native_events_add_no_turn():
    replay = Replay(Transcript())
    replay.feed("session/update", {"update": {"sessionUpdate": "plan", "entries": [{"content": "partial"}]}})
    assert replay.finish() == []
    for status, accepted in (("completed", True), ("failed", False)):
        replay = Replay(Transcript())
        replay.feed("session/update", {"update": {"sessionUpdate": "tool_call", "toolCallId": "todo_write:demo", "rawInput": {"todos": steps("completed", "completed")}, "status": status}})
        (turn,) = replay.finish()
        assert ("plan" in turn_json(turn)) is accepted


def test_an_explicit_empty_native_plan_is_serialized_to_clear_a_previous_turn():
    transcript = Transcript()
    old = transcript.start_turn("旧任务")
    write(transcript, old, "old", steps("in_progress"))
    turn = transcript.start_turn("下一项任务")
    assert transcript.set_plan(turn, [])
    assert turn_json(turn)["plan"]["steps"] == []
    assert not transcript.set_plan(turn, []), "an identical snapshot needs no extra update"
    assert not turn.tools
