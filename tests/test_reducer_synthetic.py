"""Edge cases of the reducer that the recordings do not exercise."""

from __future__ import annotations

import json

from droid_sdk import (
    PermissionResolved,
    TextBlock,
    ToolConfirmationOutcome,
    WorkingState,
    WorkingStateChanged,
)

from desktop.core.transcript import (
    ActivityBlock,
    ErrorBlock,
    NoticeBlock,
    PromptStatus,
    TextBlockView,
    ToolStatus,
    Transcript,
    TurnStatus,
)
from tests.support import (
    assistant_message,
    complete,
    delta,
    failure,
    groups,
    interrupted,
    permission_request,
    success,
    tool_call,
    tool_result,
    tool_use,
    user_message,
)



def kinds(turn):
    return [block.kind for block in turn.blocks]


def test_text_streams_into_one_block_and_bumps_versions():
    transcript = Transcript()
    turn = transcript.start_turn("hi")
    transcript.apply(delta("Hel"))
    block = turn.blocks[-1]
    assert isinstance(block, TextBlockView) and block.streaming
    before, turn_before, transcript_before = block.version, turn.version, transcript.version
    transcript.apply(delta("lo"))
    assert block.text == "Hello" and block.version > before and turn.version > turn_before and transcript.version > transcript_before
    transcript.apply(complete())
    assert not block.streaming
    transcript.apply(success("Hello"))
    assert turn.status is TurnStatus.DONE and len(turn.blocks) == 2


def test_hidden_context_messages_are_not_shown():
    transcript = Transcript()
    turn = transcript.start_turn("real prompt")
    transcript.apply(user_message("context-abc", "system reminder text"))
    transcript.apply(user_message("todo-suggest-1", "todo nudge"))
    transcript.apply(user_message("msg-1", "real prompt"))
    assert [block.kind for block in turn.blocks] == ["user"]
    assert turn.user_block.text == "real prompt" and turn.user_block.message_id == "msg-1"


def test_tool_work_between_two_texts_forms_two_groups():
    transcript = Transcript()
    turn = transcript.start_turn("go")
    transcript.apply(delta("First, look around.", "m1"))
    transcript.apply(complete("m1"))
    transcript.apply(tool_call("t1", "Read", file_path="a.py"))
    transcript.apply(tool_result("t1", "Read", "content"))
    transcript.apply(tool_call("t2", "LS", directory_path="."))
    transcript.apply(delta("Now the edit.", "m2"))
    transcript.apply(tool_call("t3", "Edit", file_path="a.py", old_str="x", new_str="y"))
    assert kinds(turn) == ["user", "text", "activity", "text", "activity"]
    assert [len(group.tools) for group in groups(turn)] == [2, 1]
    assert [item.status for item in groups(turn)[0].tools] == [ToolStatus.DONE, ToolStatus.PENDING]


def test_unstreamed_assistant_text_is_placed_before_the_tools_of_the_same_message():
    transcript = Transcript()
    turn = transcript.start_turn("go")
    transcript.apply(tool_call("t1", "Read", file_path="a.py"))
    transcript.apply(assistant_message("m1", TextBlock(text="I will read it."), tool_use("t1", "Read", file_path="a.py")))
    assert kinds(turn) == ["user", "text", "activity"]
    assert turn.blocks[1].text == "I will read it."


def test_unstreamed_text_in_the_middle_splits_the_group():
    transcript = Transcript()
    turn = transcript.start_turn("go")
    transcript.apply(tool_call("a", "Read", file_path="a.py"))
    transcript.apply(tool_call("b", "Read", file_path="b.py"))
    transcript.apply(assistant_message("m2", TextBlock(text="midway"), tool_use("b", "Read", file_path="b.py")))
    assert kinds(turn) == ["user", "activity", "text", "activity"]
    assert [len(group.tools) for group in groups(turn)] == [1, 1]
    assert turn.group_of[turn.tools["b"].id] is groups(turn)[1]


def test_first_tool_result_wins_and_later_ones_are_counted():
    transcript = Transcript()
    turn = transcript.start_turn("go")
    transcript.apply(tool_call("t1", "Execute", command="sleep 9"))
    transcript.apply(tool_result("t1", "Execute", "Error: Tool execution cancelled by user", is_error=True))
    transcript.apply(tool_result("t1", "Execute", "late output", is_error=False))
    item = turn.tools["t1"]
    assert item.status is ToolStatus.CANCELLED and item.result.startswith("Error:") and item.duplicate_results == 1


def test_plain_error_results_are_errors_not_cancellations():
    transcript = Transcript()
    turn = transcript.start_turn("go")
    transcript.apply(tool_call("t1", "Read", file_path="nope"))
    transcript.apply(tool_result("t1", "Read", "Error: Failed to read file", is_error=True))
    assert turn.tools["t1"].status is ToolStatus.ERROR


def test_edit_result_json_replaces_the_input_diff():
    transcript = Transcript()
    turn = transcript.start_turn("go")
    transcript.apply(tool_call("t1", "Edit", file_path="a.py", old_str="a", new_str="b"))
    item = turn.tools["t1"]
    assert item.diff_source == "input" and item.diffs
    payload = {"success": True, "file_path": "a.py", "linesAdded": 3, "linesRemoved": 2, "diffLines": [
        {"type": "added", "content": "x", "lineNumber": {"new": 1}}]}
    transcript.apply(tool_result("t1", "Edit", json.dumps(payload)))
    assert item.diff_source == "result" and (item.diffs[0].added, item.diffs[0].removed) == (3, 2)


def test_permission_prompt_marks_the_tool_and_stays_pending():
    transcript = Transcript()
    turn = transcript.start_turn("go")
    request = permission_request()
    prompt = transcript.add_permission(request)
    assert prompt is not None and prompt.status is PromptStatus.PENDING
    item = turn.tools[request.actions[0].tool_use.id]
    assert item.status is ToolStatus.AWAITING_PERMISSION
    assert transcript.pending_prompts() == [prompt]
    assert transcript.answer_permission(prompt.id, "proceed_once") is True
    assert (prompt.status, item.status, item.decision) == (PromptStatus.ANSWERED, ToolStatus.RUNNING, "proceed_once")
    assert transcript.answer_permission(prompt.id, "proceed_once") is False


def test_unknown_option_is_rejected_and_prompt_stays_pending():
    transcript = Transcript()
    transcript.start_turn("go")
    prompt = transcript.add_permission(permission_request())
    assert transcript.answer_permission(prompt.id, "proceed_edit") is False
    assert transcript.answer_permission(prompt.id, "made_up") is False
    assert prompt.status is PromptStatus.PENDING


def test_declining_marks_the_tool_cancelled():
    transcript = Transcript()
    turn = transcript.start_turn("go")
    request = permission_request()
    prompt = transcript.add_permission(request)
    transcript.answer_permission(prompt.id, "cancel")
    assert turn.tools[request.actions[0].tool_use.id].status is ToolStatus.CANCELLED


def test_answering_after_an_interrupt_is_refused_without_error():
    transcript = Transcript()
    turn = transcript.start_turn("go")
    prompt = transcript.add_permission(permission_request())
    transcript.apply(interrupted(2.0))
    assert turn.status is TurnStatus.INTERRUPTED and prompt.status is PromptStatus.EXPIRED
    assert transcript.answer_permission(prompt.id, "proceed_once") is False
    assert transcript.answer_question("missing-id", ["x"]) is False
    assert transcript.answer_permission("missing-id", "proceed_once") is False


def test_permission_resolved_without_ids_settles_everything_pending():
    transcript = Transcript()
    turn = transcript.start_turn("go")
    request = permission_request()
    prompt = transcript.add_permission(request)
    transcript.apply(PermissionResolved(request_id="", tool_use_ids=(), selected_option=ToolConfirmationOutcome.CANCEL))
    assert prompt.status is PromptStatus.RESOLVED and prompt.selected == "cancel"
    assert turn.tools[request.actions[0].tool_use.id].status is ToolStatus.CANCELLED


def test_stale_permission_request_for_a_finished_tool_is_expired_immediately():
    transcript = Transcript()
    transcript.start_turn("go")
    request = permission_request()
    tool_use_id = request.actions[0].tool_use.id
    transcript.apply(tool_call(tool_use_id, request.actions[0].tool_use.name))
    transcript.apply(tool_result(tool_use_id, request.actions[0].tool_use.name, "done"))
    prompt = transcript.add_permission(request)
    assert prompt.status is PromptStatus.EXPIRED


def test_permission_requests_outside_a_turn_are_ignored():
    transcript = Transcript()
    assert transcript.add_permission(permission_request()) is None


def test_new_turn_finishes_a_still_running_previous_turn_as_interrupted():
    transcript = Transcript()
    first = transcript.start_turn("one")
    transcript.apply(tool_call("t1", "Execute", command="x"))
    second = transcript.start_turn("two")
    assert first.status is TurnStatus.INTERRUPTED and transcript.active_turn is second
    assert first.tools["t1"].status is ToolStatus.CANCELLED


def test_failure_result_adds_one_error_block():
    transcript = Transcript()
    turn = transcript.start_turn("go")
    transcript.apply(failure("quota exceeded"))
    errors = [block for block in turn.blocks if isinstance(block, ErrorBlock)]
    assert [error.message for error in errors] == ["quota exceeded"]
    assert turn.status is TurnStatus.FAILED


def test_abort_turn_adds_a_notice_and_interrupts():
    transcript = Transcript()
    turn = transcript.start_turn("go")
    transcript.abort_turn("本轮已取消。")
    assert turn.status is TurnStatus.INTERRUPTED
    assert isinstance(turn.blocks[-1], NoticeBlock) and turn.blocks[-1].text == "本轮已取消。"


def test_notices_and_errors_without_an_active_turn_attach_to_the_last_turn():
    transcript = Transcript()
    transcript.add_error("boom")
    assert len(transcript.turns) == 1 and isinstance(transcript.turns[0].blocks[-1], ErrorBlock)
    transcript.start_turn("go")
    transcript.apply(success("ok"))
    transcript.add_notice("later")
    assert isinstance(transcript.turns[-1].blocks[-1], NoticeBlock)


def test_recoverable_errors_can_be_resolved():
    transcript = Transcript()
    transcript.start_turn("go")
    block = transcript.fail_turn("died", recoverable=True)
    assert not block.resolved
    transcript.resolve_recoverable_errors()
    assert block.resolved


def test_mark_stopping_is_cleared_when_the_turn_ends():
    transcript = Transcript()
    turn = transcript.start_turn("go")
    transcript.mark_stopping()
    assert turn.stopping
    transcript.apply(interrupted())
    assert not turn.stopping


def test_working_state_follows_events_and_resets_when_the_turn_ends():
    transcript = Transcript()
    turn = transcript.start_turn("go")
    transcript.apply(WorkingStateChanged(state=WorkingState.EXECUTING_TOOL))
    assert transcript.working_state is WorkingState.EXECUTING_TOOL and turn.working_state == "executing_tool"
    transcript.apply(success())
    assert transcript.working_state is WorkingState.IDLE and turn.working_state is None


def test_unfinished_streaming_text_and_thinking_are_closed_when_the_turn_ends():
    transcript = Transcript()
    turn = transcript.start_turn("go")
    transcript.apply(delta("partial"))
    transcript.apply(interrupted())
    text = turn.blocks[-1]
    assert isinstance(text, TextBlockView) and not text.streaming
    assert all(isinstance(block, (TextBlockView, ActivityBlock)) or block.kind == "user" for block in turn.blocks)