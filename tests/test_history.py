"""Saved sessions (``fixtures/history.json``) turned into the same view model as live turns."""

from __future__ import annotations

from desktop.core.toolinfo import steps_text, turn_summary_text
from desktop.core.transcript import ActivityBlock, TextBlockView, ToolStatus, Transcript, TurnStatus
from tests.support import delta, groups, history_messages, load_json, success, tool_call


def test_history_becomes_two_finished_turns_and_hides_context_messages():
    transcript = Transcript()
    assert transcript.load_history(history_messages()) == 2
    first, second = transcript.turns
    assert all(turn.from_history and turn.status is TurnStatus.DONE for turn in transcript.turns)
    assert first.user_block.text.startswith("List the files here")
    assert not any("context" in (turn.user_block.message_id or "") for turn in transcript.turns)
    assert transcript.active_turn is None


def test_history_turn_matches_the_live_layout():
    transcript = Transcript()
    transcript.load_history(history_messages())
    first, second = transcript.turns
    assert [block.kind for block in first.blocks] == ["user", "activity", "text"]
    stats = first.stats()
    assert steps_text(stats.steps, stats.counts) == "8 步 · 读取 2 · 编辑 2 · 计划 4"
    assert turn_summary_text(first.duration, stats.steps) == "用时 25 秒 · 8 步"
    assert first.model == "claude-haiku-5-5"
    assert all(item.status is ToolStatus.DONE for group in groups(first) for item in group.tools)
    assert isinstance(first.blocks[-1], TextBlockView) and "hello.txt" in first.blocks[-1].text
    assert [block.kind for block in second.blocks] == ["user", "activity", "text"]
    assert second.stats().steps == 0


def test_history_edits_keep_their_diffs():
    transcript = Transcript()
    transcript.load_history(history_messages())
    edits = [item for group in groups(transcript.turns[0]) for item in group.tools if item.name == "Edit"]
    assert edits and edits[0].diff_source in ("result", "input") and edits[0].diffs[0].added >= 1


def test_history_is_inserted_before_turns_that_already_exist():
    transcript = Transcript()
    live = transcript.start_turn("a new question")
    transcript.load_history(history_messages())
    assert transcript.turns[-1] is live and len(transcript.turns) == 3
    assert transcript.active_turn is live
    transcript.apply(delta("answer"))
    transcript.apply(success("answer"))
    assert live.status is TurnStatus.DONE


def test_live_turns_continue_after_loaded_history():
    transcript = Transcript()
    transcript.load_history(history_messages())
    transcript.start_turn("follow up")
    transcript.apply(tool_call("fresh", "Read", file_path="x"))
    assert transcript.active_turn is transcript.turns[-1] and not transcript.turns[-1].from_history
    assert [block.kind for block in transcript.turns[-1].blocks] == ["user", "activity"]
    assert isinstance(transcript.turns[-1].blocks[1], ActivityBlock)


def test_empty_history_loads_nothing():
    transcript = Transcript()
    assert transcript.load_history([]) == 0
    assert transcript.turns == []