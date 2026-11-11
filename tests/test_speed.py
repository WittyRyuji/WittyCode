"""How fast the model wrote: a turn's output tokens over the time the model had (``core/speed.py``, and the timing the
transcript keeps for it)."""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

from desktop.core.prompts import OptionView
from desktop.core.speed import MIN_SECONDS, MIN_TOKENS, speed_of, speed_of_turn
from desktop.core.toolinfo import Category
from desktop.core.transcript import PromptStatus, ToolStatus, Transcript, TurnStatus
from desktop.core.usage import Tokens, own_output
from droid_sdk import RunFailure, RunInterrupted, RunSuccess, Usage

ALLOW = OptionView("proceed_once", "允许", "accept", "primary")


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def make():
    clock = Clock()
    return Transcript(clock=clock), clock


def usage(out: int):
    return SimpleNamespace(output_tokens=out)


# ------------------------------------------------------------------------------------------------------ the timing


def test_a_turn_without_tools_is_all_the_models_time_and_the_first_output_is_noted():
    transcript, clock = make()
    turn = transcript.start_turn("write it")
    clock.advance(2)
    transcript.append_text(turn, "m1", 0, "Here ")
    clock.advance(10)
    transcript.append_text(turn, "m1", 0, "it is.")
    transcript.finish_turn(turn, TurnStatus.DONE, usage=usage(300))
    assert (turn.duration, turn.blocked_seconds, turn.model_seconds, turn.output_tokens) == (12.0, 0.0, 12.0, 300)
    assert turn.first_output_at - turn.started_at == 2.0
    speed = speed_of_turn(turn)
    assert (speed.tps, speed.tokens, speed.seconds, speed.first) == (25.0, 300, 12.0, 2.0)
    assert speed.gen_tps == 30.0, "300 tokens in the 10 s after the first output"


def test_the_first_output_is_whatever_the_model_produces_first_and_only_the_first():
    for first in ("text", "thinking", "tool"):
        transcript, clock = make()
        turn = transcript.start_turn("go")
        clock.advance(1)
        assert turn.first_output_at is None, "the user's own message is not output"
        clock.advance(1)
        if first == "text":
            transcript.append_text(turn, "m", 0, "x")
        elif first == "thinking":
            transcript.append_thinking(turn, "m", 0, "x")
        else:
            transcript.ensure_tool(turn, "u1", "Read")
        clock.advance(5)
        transcript.append_text(turn, "m", 1, "later")
        assert turn.first_output_at - turn.started_at == 2.0, first


def test_the_time_a_tool_is_at_work_is_not_the_models():
    transcript, clock = make()
    turn = transcript.start_turn("run it")
    clock.advance(3)
    item = transcript.ensure_tool(turn, "u1", "Execute")
    transcript.tool_running(turn, item)
    clock.advance(6)
    transcript.finish_tool(turn, item, "done", status=ToolStatus.DONE)
    clock.advance(3)
    transcript.finish_turn(turn, TurnStatus.DONE, usage=usage(120))
    assert (turn.duration, turn.blocked_seconds, turn.model_seconds) == (12.0, 6.0, 6.0)
    assert speed_of_turn(turn).tps == 20.0


def test_waiting_for_the_user_is_not_the_models_time_either():
    transcript, clock = make()
    turn = transcript.start_turn("run it")
    clock.advance(2)
    item = transcript.ensure_tool(turn, "u1", "Execute")
    prompt = transcript.add_permission_prompt(["u1"], [], [ALLOW])
    clock.advance(30)  # (the user is away)
    assert transcript.answer_permission(prompt.id, "proceed_once")
    clock.advance(1)
    transcript.finish_tool(turn, item, "ok", status=ToolStatus.DONE)
    clock.advance(2)
    transcript.finish_turn(turn, TurnStatus.DONE, usage=usage(100))
    assert turn.blocked_seconds == 31.0 and turn.model_seconds == 4.0


def test_a_question_to_the_user_waits_the_same_way_and_so_does_a_turn_that_ends_while_it_is_open():
    transcript, clock = make()
    turn = transcript.start_turn("ask me")
    clock.advance(1)
    transcript.ensure_tool(turn, "q1", "AskUser")
    prompt = transcript.add_question_prompt("q1", [])
    clock.advance(10)
    assert prompt.status is PromptStatus.PENDING
    transcript.finish_turn(turn, TurnStatus.INTERRUPTED)
    assert turn.blocked_seconds == 10.0 and turn.model_seconds == 1.0


def test_tools_at_work_together_count_once():
    transcript, clock = make()
    turn = transcript.start_turn("two at once")
    clock.advance(2)
    a, b = transcript.ensure_tool(turn, "a", "Execute"), transcript.ensure_tool(turn, "b", "Execute")
    transcript.tool_running(turn, a)
    clock.advance(2)
    transcript.tool_running(turn, b)
    clock.advance(4)
    transcript.finish_tool(turn, a, "", status=ToolStatus.DONE)
    clock.advance(2)
    transcript.finish_tool(turn, b, "", status=ToolStatus.DONE)
    transcript.finish_turn(turn, TurnStatus.DONE, usage=usage(100))
    assert turn.blocked_seconds == 8.0, "from the first start to the last end, not 4 + 6"


def test_a_tool_that_never_reports_that_it_runs_is_the_models_time():
    transcript, clock = make()
    turn = transcript.start_turn("read it")
    item = transcript.ensure_tool(turn, "u1", "Read")
    clock.advance(5)
    transcript.finish_tool(turn, item, "text", status=ToolStatus.DONE)
    transcript.finish_turn(turn, TurnStatus.DONE, usage=usage(100))
    assert turn.blocked_seconds == 0.0 and turn.model_seconds == 5.0


# ------------------------------------------------------------------------------------------------------ the output


def test_where_the_engine_reports_totals_the_turns_output_is_what_was_counted_since_it_began():
    transcript, clock = make()
    counted = [1000]
    transcript.output_probe = lambda: counted[0]
    turn = transcript.start_turn("write it")
    clock.advance(10)
    counted[0] = 1300
    transcript.finish_turn(turn, TurnStatus.DONE)
    assert turn.output_tokens == 300 and speed_of_turn(turn).tps == 30.0


def test_a_turns_own_figure_is_preferred_to_the_difference_of_totals():
    transcript, clock = make()
    transcript.output_probe = lambda: 5000
    turn = transcript.start_turn("write it")
    clock.advance(10)
    transcript.finish_turn(turn, TurnStatus.DONE, usage=usage(250))
    assert turn.output_tokens == 250


def test_a_session_that_had_counted_nothing_but_had_turns_does_not_put_its_past_into_this_one():
    transcript, clock = make()
    counted: list[int | None] = [None]
    transcript.output_probe = lambda: counted[0]
    transcript.finish_turn(transcript.start_turn("earlier"), TurnStatus.DONE)
    turn = transcript.start_turn("now")  # (nothing counted yet, and not the first turn)
    clock.advance(10)
    counted[0] = 90_000  # (the engine reports the whole conversation's total at last)
    transcript.finish_turn(turn, TurnStatus.DONE)
    assert turn.output_tokens is None


def test_the_first_turn_of_a_session_starts_from_zero_when_nothing_was_counted_yet():
    transcript, clock = make()
    counted: list[int | None] = [None]
    transcript.output_probe = lambda: counted[0]
    turn = transcript.start_turn("first")
    clock.advance(10)
    counted[0] = 300
    transcript.finish_turn(turn, TurnStatus.DONE)
    assert turn.output_tokens == 300


def test_a_total_that_did_not_grow_or_a_probe_that_fails_is_no_figure():
    transcript, clock = make()
    transcript.output_probe = lambda: 100
    turn = transcript.start_turn("write")
    clock.advance(10)
    transcript.finish_turn(turn, TurnStatus.DONE)
    assert turn.output_tokens is None

    transcript, clock = make()

    def broken() -> int:
        raise RuntimeError("no")

    transcript.output_probe = broken
    turn = transcript.start_turn("write")
    transcript.finish_turn(turn, TurnStatus.DONE)
    assert turn.output_tokens is None


def test_a_droid_run_result_brings_its_usage_and_its_duration():
    transcript, clock = make()
    turn = transcript.start_turn("write")
    clock.advance(1)
    transcript.append_text(turn, "m", 0, "answer")
    clock.advance(4)
    used = Usage(input_tokens=4, output_tokens=380, cache_creation_tokens=0, cache_read_tokens=0, thinking_tokens=244)
    transcript.apply(RunSuccess(text="answer", messages=(), usage=used, duration=timedelta(seconds=8), turn_count=1, session_id="s"))
    assert (turn.output_tokens, turn.duration, turn.model_seconds) == (380, 8.0, 8.0)
    assert speed_of_turn(turn).tps == 47.5


# ------------------------------------------------------------------------------------------------------ which turns tell


def finished(transcript, clock, seconds, tokens, status=TurnStatus.DONE):
    turn = transcript.start_turn("m")
    clock.advance(seconds)
    transcript.finish_turn(turn, status, usage=usage(tokens))
    return turn


def test_only_a_finished_turn_that_wrote_enough_for_long_enough_tells_the_speed():
    transcript, clock = make()
    assert speed_of_turn(finished(transcript, clock, 10, MIN_TOKENS - 1)) is None, "too little written"
    assert speed_of_turn(finished(transcript, clock, MIN_SECONDS - 0.1, 500)) is None, "too quick"
    assert speed_of_turn(finished(transcript, clock, 10, 200, TurnStatus.INTERRUPTED)) is None
    assert speed_of_turn(finished(transcript, clock, 10, 200, TurnStatus.FAILED)) is None
    assert speed_of_turn(finished(transcript, clock, 10, 200)).tps == 20.0
    running = transcript.start_turn("still going")
    assert speed_of_turn(running) is None


def test_the_latest_turn_that_can_tell_is_the_one_shown():
    transcript, clock = make()
    finished(transcript, clock, 10, 200)
    finished(transcript, clock, 10, 400)
    finished(transcript, clock, 1, 3)  # ("ok")
    finished(transcript, clock, 10, 1000, TurnStatus.INTERRUPTED)
    assert speed_of(transcript.turns).tps == 40.0
    assert speed_of([]) is None


def test_history_has_no_speed():
    transcript, clock = make()
    turn = transcript.new_history_turn("an old question")
    transcript.finish_turn(turn, TurnStatus.DONE, duration=20.0, usage=usage(500))
    assert turn.model_seconds is None and turn.output_tokens is None and speed_of(transcript.turns) is None


def test_without_a_first_output_there_is_no_speed_without_the_wait():
    transcript, clock = make()
    turn = transcript.start_turn("m")
    clock.advance(10)
    transcript.finish_turn(turn, TurnStatus.DONE, usage=usage(200))
    speed = speed_of_turn(turn)
    assert (speed.tps, speed.first, speed.gen_tps) == (20.0, None, None)


def test_a_first_wait_that_is_almost_all_of_the_time_leaves_nothing_to_divide_by():
    transcript, clock = make()
    turn = transcript.start_turn("m")
    clock.advance(9.8)
    transcript.append_text(turn, "m", 0, "x")
    clock.advance(0.4)
    transcript.finish_turn(turn, TurnStatus.DONE, usage=usage(200))
    speed = speed_of_turn(turn)
    assert round(speed.first, 1) == 9.8 and speed.gen_tps is None


def test_the_json_for_the_page_is_rounded():
    transcript, clock = make()
    turn = transcript.start_turn("m")
    clock.advance(1.234)
    transcript.append_text(turn, "m", 0, "x")
    clock.advance(7.777)
    transcript.finish_turn(turn, TurnStatus.DONE, usage=usage(333))
    assert speed_of_turn(turn).to_json() == {"tps": 37.0, "tokens": 333, "seconds": 9.0, "first": 1.23, "genTps": 42.8}


# ------------------------------------------------------------------------------------------------------ the session's own count


def test_a_sessions_own_output_leaves_out_its_sub_agents():
    assert own_output({}) is None
    assert own_output({"self": Tokens(1, 300, None, 0), "agent:t1": Tokens(1, 5000, None, 0)}) == 300
    assert own_output({"turn:a": Tokens(0, 100, None, 0), "turn:b": Tokens(0, 40, None, 0)}) == 140
    assert own_output({"agent:t1": Tokens(0, 9, None, 0)}) is None
