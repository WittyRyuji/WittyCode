"""The Transcript builder methods engines without SDK events use (Codex)."""

from __future__ import annotations

from desktop.core.diffs import diff_for_new_file
from desktop.core.prompts import ActionView, OptionView, QuestionView
from desktop.core.transcript import ActivityBlock, PromptStatus, ToolStatus, Transcript, TurnStatus

OPTIONS = (OptionView("proceed_once", "允许", "accept", "primary"), OptionView("cancel", "拒绝", "decline", "danger"))


def test_text_and_thinking_stream_then_end():
    transcript = Transcript(clock=lambda: 10.0)
    turn = transcript.start_turn("hi")
    before = transcript.version
    entry = transcript.append_thinking(turn, "r", 0, "think")
    block = transcript.append_text(turn, "m", 0, "Hel")
    transcript.append_text(turn, "m", 0, "lo")
    assert block.streaming and block.text == "Hello" and transcript.version > before
    assert transcript.end_text(turn, "m", 0, "Hello!") is block and block.text == "Hello!" and not block.streaming
    assert transcript.end_text(turn, "m2", 0, "  ") is None, "an empty final text makes no block"
    assert transcript.end_thinking(turn, "r", 0, duration=1.5) is entry and entry.complete and entry.text == "think"
    assert transcript.end_thinking(turn, "r2", 0, text="") is None
    assert [type(b) for b in turn.blocks[1:]] == [ActivityBlock, type(block)]


def test_tools_run_get_diffs_and_finish_with_the_status_given():
    transcript = Transcript(clock=lambda: 5.0)
    turn = transcript.start_turn("go")
    tool = transcript.ensure_tool(turn, "c1", "Execute")
    transcript.tool_running(turn, tool, "x" * 5000)
    assert tool.status is ToolStatus.RUNNING and tool.started_at == 5.0 and len(tool.progress) == 4000
    transcript.finish_tool(turn, tool, "declined", status=ToolStatus.CANCELLED)
    assert tool.status is ToolStatus.CANCELLED and tool.result == "declined" and not tool.is_error
    transcript.finish_tool(turn, tool, "again", status=ToolStatus.DONE)
    assert tool.result == "declined" and tool.duplicate_results == 1
    edit = transcript.ensure_tool(turn, "e1", "Create")
    transcript.set_tool_diffs(turn, edit, [diff_for_new_file("n.txt", "a\n")])
    transcript.finish_tool(turn, edit, "failed", status=ToolStatus.ERROR)
    assert edit.diffs[0].added == 1 and edit.diff_source == "input" and edit.is_error


def test_permission_prompts_from_display_data():
    transcript = Transcript()
    assert transcript.add_permission_prompt(["x"], [], OPTIONS) is None, "no active turn"
    turn = transcript.start_turn("go")
    tool = transcript.ensure_tool(turn, "c1", "Execute")
    diff = diff_for_new_file("n.txt", "a\n")
    action = ActionView(kind="execute", tool_use_id="c1", tool_name="Execute", title="运行命令", command="rm x", diffs=(diff,))
    prompt = transcript.add_permission_prompt(["c1", "unknown"], [action], OPTIONS)
    assert prompt.tool_use_ids == ("c1",) and prompt.status is PromptStatus.PENDING
    assert tool.status is ToolStatus.AWAITING_PERMISSION and tool.diff_source == "permission"
    assert transcript.pending_prompts() == [prompt]
    assert transcript.answer_permission(prompt.id, "cancel") and tool.status is ToolStatus.CANCELLED
    done = transcript.ensure_tool(turn, "c2", "Execute")
    transcript.finish_tool(turn, done, "", status=ToolStatus.DONE)
    late = transcript.add_permission_prompt(["c2"], [], OPTIONS)
    assert late.status is PromptStatus.EXPIRED, "a prompt for a finished tool is stale"


def test_question_prompts_working_state_errors_and_history_turns():
    transcript = Transcript()
    turn = transcript.start_turn("go")
    prompt = transcript.add_question_prompt("q1", (QuestionView(0, "Color", "Which?", ("red",), False),))
    assert turn.tools["q1"].name == "AskUser" and turn.tools["q1"].status is ToolStatus.AWAITING_PERMISSION
    assert transcript.answer_question(prompt.id, ["red"])
    transcript.set_working_state("executing_tool")
    assert turn.working_state == "executing_tool" and transcript.working_state.value == "executing_tool"
    transcript.set_working_state("no-such-state")
    assert transcript.working_state is None and turn.working_state == "no-such-state"
    transcript.finish_turn(turn, TurnStatus.DONE)
    old = transcript.new_history_turn("earlier")
    block = transcript.append_error(old, "it failed")
    transcript.finish_turn(old, TurnStatus.FAILED)
    assert transcript.add_history_turns([old]) == 1
    assert transcript.turns[0] is old and old.blocks[-1] is block and transcript.turns[1] is turn
