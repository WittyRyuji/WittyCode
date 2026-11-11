"""The transcript reducer against the recorded Droid sessions (offline replays)."""

from __future__ import annotations

from desktop.core.toolinfo import Category, steps_text, turn_summary_text
from desktop.core.transcript import (
    ActivityBlock,
    ErrorBlock,
    NoticeBlock,
    PromptStatus,
    QABlock,
    TextBlockView,
    ThinkingEntry,
    ToolStatus,
    TurnStatus,
    UserBlock,
)
from tests.support import groups, last_turn, replay_fixture


def kinds(turn):
    return [block.kind for block in turn.blocks]


def tools(turn):
    return [entry for group in groups(turn) for entry in group.tools]


def test_stream_turn_folds_all_tool_work_into_one_group_between_user_and_answer():
    transcript = replay_fixture("stream_turn.jsonl")
    (turn,) = transcript.turns
    assert kinds(turn) == ["user", "activity", "text"]
    assert turn.status is TurnStatus.DONE
    stats = turn.stats()
    assert stats.steps == 8 and stats.thinking == 2 and stats.errors == 0 and stats.cancelled == 0
    assert stats.counts == {Category.READ: 2, Category.EDIT: 2, Category.PLAN: 4}
    assert steps_text(stats.steps, stats.counts) == "8 步 · 读取 2 · 编辑 2 · 计划 4"
    assert turn_summary_text(turn.duration, stats.steps) == "用时 25 秒 · 8 步"
    assert all(item.status is ToolStatus.DONE for item in tools(turn))
    answer = turn.blocks[-1]
    assert isinstance(answer, TextBlockView) and not answer.streaming and "hello.txt" in answer.text
    assert transcript.title == "Execute file operations sequence"


def test_stream_turn_usage_is_cumulative_for_the_session_and_final_for_the_turn():
    transcript = replay_fixture("stream_turn.jsonl")
    turn = last_turn(transcript)
    assert turn.usage is not None and turn.usage.factory_credits == 832.0
    assert transcript.usage is not None and transcript.usage.factory_credits == 832.0
    assert transcript.usage.cache_read_tokens == 91153


def test_stream_turn_permissions_are_answered_and_carry_diffs():
    turn = last_turn(replay_fixture("stream_turn.jsonl"))
    assert [(p.kind, p.status) for p in turn.prompts] == [("permission", PromptStatus.ANSWERED)] * 2
    assert all(prompt.selected == "proceed_once" for prompt in turn.prompts)
    by_name = {item.name: item for item in tools(turn) if item.name in ("Create", "Edit")}
    create, edit = by_name["Create"], by_name["Edit"]
    assert create.decision == edit.decision == "proceed_once"
    assert create.diff_source == "permission" and create.diffs[0].is_new_file
    assert edit.diff_source == "result"
    assert (edit.diffs[0].added, edit.diffs[0].removed) == (1, 1)


def test_thinking_entries_live_in_the_activity_group_and_keep_their_duration():
    turn = last_turn(replay_fixture("stream_turn.jsonl"))
    thinking = [entry for entry in turn.activity_entries() if isinstance(entry, ThinkingEntry)]
    assert len(thinking) == 2 and all(entry.complete for entry in thinking)
    assert thinking[0].duration == 2.404 and thinking[0].text


def test_reasoning_low_fixture():
    turn = last_turn(replay_fixture("stream_turn_reasoning_low.jsonl"))
    assert kinds(turn) == ["user", "activity", "text"]
    assert turn.stats().steps == 3 and turn.status is TurnStatus.DONE


def test_ask_user_becomes_a_qa_block_between_the_tool_and_the_answer():
    transcript = replay_fixture("ask_user.jsonl")
    turn = last_turn(transcript)
    assert kinds(turn) == ["user", "activity", "qa", "text"]
    qa = next(block for block in turn.blocks if isinstance(block, QABlock))
    assert qa.pairs == [("Which greeting word do you prefer?", "Hello")] and not qa.cancelled
    (prompt,) = turn.prompts
    assert prompt.kind == "question" and prompt.status is PromptStatus.ANSWERED and prompt.answers == ("Hello",)
    (ask,) = tools(turn)
    assert ask.category is Category.ASK and ask.status is ToolStatus.DONE


def test_unanswered_prompts_stay_pending_until_the_turn_ends():
    transcript = replay_fixture("ask_user.jsonl", answer=False)
    turn = last_turn(transcript)
    # The recording continues after the answer, so the turn finished and the prompt expired.
    assert turn.status is TurnStatus.DONE
    assert [prompt.status for prompt in turn.prompts] == [PromptStatus.EXPIRED]
    assert transcript.pending_prompts() == []


def test_interrupt_marks_the_running_tool_cancelled_and_counts_the_duplicate_result():
    transcript = replay_fixture("interrupt.jsonl")
    first, second = transcript.turns
    assert first.status is TurnStatus.INTERRUPTED
    (execute,) = tools(first)
    assert execute.name == "Execute" and execute.status is ToolStatus.CANCELLED
    assert execute.duplicate_results == 1
    assert first.stats().cancelled == 1 and first.stats().errors == 0
    assert second.status is TurnStatus.DONE and kinds(second) == ["user", "text"]
    assert transcript.working_state.value == "idle"


def test_interrupt_while_a_tool_is_running():
    turn = last_turn(replay_fixture("interrupt_running_tool.jsonl"))
    assert turn.status is TurnStatus.INTERRUPTED
    (execute,) = tools(turn)
    assert execute.status is ToolStatus.CANCELLED and execute.decision == "proceed_once"
    assert not turn.pending_prompts()


def test_handler_errors_are_translated_and_the_prompt_is_settled_by_droid():
    transcript = replay_fixture("robust/errors_handlers.jsonl")
    first = transcript.turns[0]
    error = next(block for block in first.blocks if isinstance(block, ErrorBlock))
    assert error.message == "审批处理出错，Droid 已按「拒绝」处理这一步。"
    (prompt,) = first.prompts
    assert prompt.status in (PromptStatus.RESOLVED, PromptStatus.ANSWERED)
    (execute,) = tools(first)
    assert execute.status is ToolStatus.CANCELLED


def test_multiedit_failed_read_is_an_error_not_a_cancel():
    turn = last_turn(replay_fixture("robust/multiedit.jsonl"))
    assert kinds(turn) == ["user", "activity", "text", "activity", "text", "activity", "qa", "error"]
    first_group = groups(turn)[0]
    assert first_group.stats().errors == 1 and first_group.stats().steps == 3
    failed = next(item for item in first_group.tools if item.status is ToolStatus.ERROR)
    assert failed.name == "Read" and failed.result.startswith("Error:")
    edits = [item for item in tools(turn) if item.name == "Edit"]
    assert len(edits) == 2
    assert all(item.diff_source == "result" and (item.diffs[0].added, item.diffs[0].removed) == (1, 1) for item in edits)
    qa = next(block for block in turn.blocks if isinstance(block, QABlock))
    assert qa.cancelled and qa.pairs[0][1] == "（已跳过）"
    assert isinstance(turn.blocks[-1], ErrorBlock) and "提问处理出错" in turn.blocks[-1].message


def test_successful_hooks_produce_no_noise():
    turn = last_turn(replay_fixture("robust/hooks.jsonl"))
    assert not [block for block in turn.blocks if isinstance(block, (ErrorBlock, NoticeBlock))]
    (execute,) = tools(turn)
    assert execute.status is ToolStatus.DONE and "hook-test" in execute.result


def test_kill_midturn_leaves_the_turn_running_until_the_controller_fails_it():
    transcript = replay_fixture("robust/errors_kill_midturn.jsonl")
    turn = last_turn(transcript)
    assert turn.status is TurnStatus.RUNNING and transcript.active_turn is turn
    block = transcript.fail_turn("Droid 进程异常退出。", recoverable=True)
    assert isinstance(block, ErrorBlock) and block.recoverable
    assert turn.status is TurnStatus.FAILED and transcript.active_turn is None
    assert all(item.terminal for item in tools(turn))
    assert not transcript.pending_prompts()


def test_long_waits_keep_the_real_elapsed_time():
    turn = last_turn(replay_fixture("robust/long_waits_perm.jsonl"))
    assert turn.status is TurnStatus.DONE and turn.duration > 600
    assert turn_summary_text(turn.duration, turn.stats().steps).startswith("用时 10 分 ")
    assert (turn.prompts[0].kind, turn.prompts[0].status) == ("permission", PromptStatus.RESOLVED)


def test_thinking_only_groups_have_no_steps():
    turn = last_turn(replay_fixture("resume.jsonl"))
    (group,) = groups(turn)
    assert group.stats().steps == 0 and group.stats().thinking == 1
    assert kinds(turn) == ["user", "activity", "text"]


def test_user_block_is_first_and_unique_in_every_replayed_turn():
    for name in ("stream_turn.jsonl", "ask_user.jsonl", "interrupt.jsonl", "robust/multiedit.jsonl", "resume.jsonl"):
        for turn in replay_fixture(name).turns:
            assert isinstance(turn.blocks[0], UserBlock)
            assert sum(isinstance(block, UserBlock) for block in turn.blocks) == 1