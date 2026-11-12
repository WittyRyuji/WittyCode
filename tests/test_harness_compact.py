"""``core.harness.compact``: sizes, where to cut, what the summary request holds, and the loop's two doors into it (the
estimate before each request, and the server's refusal as too big)."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from desktop.core.chatgpt.errors import ChatGPTError
from desktop.core.chatgpt.responses import call_output, user_message
from desktop.core.harness import compact
from desktop.core.harness.loop import AgentLoop, Observer
from tests.fake_responses import call, fail, say, usage
from tests.test_harness_loop import FakeTools, Recorder, Script


def u(text: str) -> dict[str, Any]:
    return user_message(text)


def a(text: str) -> dict[str, Any]:
    return {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": text, "annotations": []}]}


def fc(call_id: str, name: str = "echo", arguments: str = "{}") -> dict[str, Any]:
    return {"type": "function_call", "call_id": call_id, "name": name, "arguments": arguments}


def fo(call_id: str, output: str = "ok") -> dict[str, Any]:
    return call_output(call_id, output)


def thought() -> dict[str, Any]:
    return {"type": "reasoning", "summary": [], "encrypted_content": "x" * 100}


def words(count: int) -> str:
    return "word " * count


def run(coroutine: Any) -> Any:
    return asyncio.run(coroutine)


# ----------------------------------------------------------------------------------------------- sizes


def test_a_latin_text_is_about_a_token_in_three_and_a_half_characters_and_any_other_character_is_one():
    assert compact.estimate_text("a" * 350) == 101
    assert compact.estimate_text("你" * 100) == 101
    assert compact.estimate_text("") == 1


def test_an_image_has_a_size_of_its_own_whatever_its_data_url_says():
    item = user_message("look", ["data:image/png;base64," + "A" * 500_000])
    assert compact.IMAGE_TOKENS <= compact.estimate_item(item) < compact.IMAGE_TOKENS + 50


def test_every_kind_of_item_has_a_size():
    assert compact.estimate_item(a("x" * 700)) > 200
    assert compact.estimate_item(fc("c1", "read_file", '{"file_path": "' + "p" * 350 + '"}')) > 100
    assert compact.estimate_item(fo("c1", "y" * 700)) > 200
    assert compact.estimate_item(thought()) > 20
    assert compact.estimate_item({"type": "something_new", "value": "z" * 350}) > 100
    assert compact.estimate_items([a("x" * 350), a("x" * 350)]) == 2 * compact.estimate_item(a("x" * 350))


# ----------------------------------------------------------------------------------------------- the view


def test_without_a_compaction_the_view_is_a_copy_of_the_log():
    log = [u("one"), a("two")]
    view = compact.view_of(log, None)
    assert view == log and view is not log
    assert compact.view_of(log, {}) == log


def test_with_a_compaction_the_view_is_the_summary_and_the_log_after_it():
    log = [u("one"), a("two"), u("three"), a("four")]
    view = compact.view_of(log, {"upto": 2, "summary": "They said one and two."})
    assert view[1:] == log[2:] and compact.is_summary(view[0]) and compact.summary_text(view[0]) == "They said one and two."
    assert view[0]["role"] == "user"
    assert not compact.is_summary(u("a message that only mentions a summary"))


def test_a_compaction_that_points_past_the_log_is_held_to_its_end():
    log = [u("one"), a("two"), u("three"), a("four")]
    assert compact.view_of(log, {"upto": 99, "summary": "s"})[1:] == []
    assert compact.view_of(log, {"upto": -1, "summary": "s"})[1:] == log, "a negative place is the start, not 'the last one'"


def test_a_place_in_the_view_is_a_place_in_the_log():
    log = [u("one"), a("two"), u("three"), a("four")]
    record = {"upto": 2, "summary": "s"}
    view = compact.view_of(log, record)
    synthetic = compact.synthetic_count(record)
    assert synthetic == 1 and compact.log_index(2, 2, synthetic) == 3 and log[compact.log_index(2, 2, synthetic)] == view[2] == a("four")
    plain = compact.view_of(log, None)
    assert compact.synthetic_count(None) == compact.synthetic_count({}) == 0 and compact.log_index(3, 0, 0) == 3


# ----------------------------------------------------------------------------------------------- where to cut


def test_a_request_can_begin_between_steps_but_not_between_a_call_and_its_output():
    view = [u("go"), thought(), fc("c1"), fo("c1"), a("done"), u("more")]
    points = compact.cut_points(view)
    assert 3 not in points and 4 in points and 5 in points
    assert 2 not in points, "a reasoning item belongs to the call that follows it"
    assert 0 not in points


def test_two_calls_of_one_answer_are_one_unit():
    view = [u("go"), fc("c1"), fc("c2"), fo("c1"), fo("c2"), a("done")]
    assert compact.cut_points(view) == [1, 5]


def test_the_cut_is_the_earliest_message_of_the_user_whose_rest_fits():
    view = [u(words(300)), a(words(300)), u(words(300)), a(words(300)), u(words(300)), a(words(300))]
    each = compact.estimate_item(view[0])
    assert compact.choose_cut(view, keep_tokens=each * 2 + 10) == 4
    assert compact.choose_cut(view, keep_tokens=each * 4 + 10) == 2
    assert compact.choose_cut(view, keep_tokens=each * 6 + 10) == 2, "the first item is never cut off alone: a place is found even when everything fits, the caller decides"


def test_a_turn_that_alone_is_too_big_is_cut_inside():
    view = [u("go"), fc("c1"), fo("c1", words(2000)), fc("c2"), fo("c2", words(2000)), a("done")]
    big = compact.estimate_item(view[2])
    cut = compact.choose_cut(view, keep_tokens=big + 50)
    assert cut == 3, "the user message is the only boundary of that kind; the step boundary is used"
    assert view[cut]["type"] == "function_call"


def test_when_even_the_smallest_tail_is_too_big_it_is_still_the_smallest_tail():
    view = [u("go"), fc("c1"), fo("c1", words(5000)), a("done")]
    assert compact.cut_points(view) == [1, 3]
    assert compact.choose_cut(view, keep_tokens=1) == 3, "nothing fits: the last place, which leaves the least"


def test_there_is_no_cut_when_nothing_is_before_it():
    assert compact.choose_cut([u("only")], 1000) is None
    assert compact.choose_cut([], 1000) is None
    summary = compact.summary_item("earlier")
    assert compact.choose_cut([summary, u("now")], 1000, synthetic=1) is None, "the summary is not summarised again by itself"
    assert compact.choose_cut([summary, u("now"), a("ok")], 1, synthetic=1) == 2


# ----------------------------------------------------------------------------------------------- what the model reads


def test_the_log_is_rendered_as_plain_text_without_what_carries_nothing():
    # (the summary of an earlier compaction is not in what is rendered: ``compact`` leaves out the view's own items)
    parts = compact.render_parts(
        [u("fix the bug"), thought(), fc("c1", "read_file", '{"file_path": "a.py"}'), fo("c1", "line"), a("fixed")]
    )
    assert parts == ["USER: fix the bug", 'ASSISTANT CALLED TOOL read_file: {"file_path": "a.py"}', "TOOL RESULT: line", "ASSISTANT: fixed"]


def test_long_pieces_are_cut_in_the_middle_and_images_are_counted():
    long_output = "start " + "x" * 5000 + " finish"
    text = compact.render(fo("c1", long_output))
    assert text.startswith("TOOL RESULT: start") and text.endswith("finish") and "characters left out" in text
    assert len(text) < compact.RESULT_LIMIT + 120
    assert "[1 image(s) attached]" in compact.render(user_message("see", ["data:image/png;base64,AAAA"]))


def test_the_parts_are_grouped_to_the_budget_and_a_part_that_is_too_big_is_cut_down():
    parts = ["a" * 3500] * 5
    groups = compact.chunks(parts, budget=2100)
    assert [len(group) for group in groups] == [2, 2, 1]
    huge = compact.chunks(["b" * 100_000], budget=500)
    assert len(huge) == 1 and len(huge[0][0]) < 2000


def test_the_chunk_budget_follows_the_window_between_a_floor_and_a_ceiling():
    assert compact.chunk_budget(10_000) == compact.CHUNK_FLOOR
    assert compact.chunk_budget(100_000) == 40_000
    assert compact.chunk_budget(10_000_000) == compact.CHUNK_CEILING


def test_the_digest_is_what_the_user_asked_oldest_dropped_first_when_it_is_too_long():
    items = [u(f"question {n} " + "x" * 280) for n in range(40)] + [a("an answer is not in it")]
    text = compact.digest(items, previous="Earlier summary.")
    assert text.startswith("Earlier summary.") and "could not be summarised" in text
    assert "question 39" in text and "question 0 " not in text and "an answer" not in text
    assert len(text) < compact.DIGEST_LIMIT + 400


# ----------------------------------------------------------------------------------------------- the whole thing


def long_view(turns: int = 6, size: int = 300) -> list[dict[str, Any]]:
    view: list[dict[str, Any]] = []
    for number in range(turns):
        view += [u(f"question {number} {words(size)}"), a(f"answer {number} {words(size)}")]
    return view


def test_the_front_of_the_view_becomes_a_summary_and_the_rest_stays_word_for_word():
    view = long_view()
    script = Script(say("The summary.", used=usage(900, 40)))
    seen = []
    result = run(compact.compact(view, stream=script, model="m", reasoning="low", window=4000, on_usage=seen.append))
    assert result is not None and not result.mechanical and result.problem == ""
    assert result.view[0] == compact.summary_item("The summary.")
    assert result.view[1:] == view[result.cut :] and 0 < result.cut < len(view)
    assert result.before == compact.estimate_items(view) and result.after == compact.estimate_items(result.view) < result.before
    request = script.requests[0]
    assert request["model"] == "m" and request["reasoning"]["effort"] == "low"
    assert "tools" not in request and request["instructions"] == compact.INSTRUCTIONS
    assert "include" not in request, "no encrypted reasoning is asked for"
    body = request["input"][0]["content"][0]["text"]
    assert "question 0" in body and "(none: this is the beginning)" in body
    assert f"question {len(view) // 2 - 1}" not in body, "what stays word for word is not in the summary request"
    assert [spent.input_tokens for spent in seen] == [900], "the request is counted"


def test_the_previous_summary_goes_into_the_next_one_and_is_not_rendered_as_a_message():
    view = [compact.summary_item("Before: the user wants a parser.")] + long_view(4)
    script = Script(say("New summary."))
    result = run(compact.compact(view, stream=script, model="m", reasoning=None, window=4000, synthetic=1))
    assert result is not None
    body = script.requests[0]["input"][0]["content"][0]["text"]
    assert "<previous_summary>\nBefore: the user wants a parser.\n</previous_summary>" in body
    assert "USER: [Summary" not in body
    assert "reasoning" not in script.requests[0]
    assert result.view[0] == compact.summary_item("New summary.") and len(result.view) < len(view)


def test_what_is_too_big_for_one_request_is_summarised_in_pieces_each_carrying_the_last_summary():
    view = [u(words(1200)) if n % 2 == 0 else a(words(1200)) for n in range(14)] + [u("now"), a("ok")]
    script = Script(say("S1"), say("S2"), say("S3"), say("S4"), say("S5"))
    result = run(compact.compact(view, stream=script, model="m", reasoning=None, window=10_000))
    assert result is not None and not result.mechanical
    assert len(script.requests) >= 2
    first = script.requests[0]["input"][0]["content"][0]["text"]
    second = script.requests[1]["input"][0]["content"][0]["text"]
    assert "(none: this is the beginning)" in first and "<previous_summary>\nS1\n" in second
    assert result.summary == f"S{len(script.requests)}", "the last piece's summary is the summary"


def test_a_summary_that_comes_back_empty_or_cut_short_is_replaced_by_the_digest():
    view = long_view()
    empty = run(compact.compact(view, stream=Script(say("   ")), model="m", reasoning=None, window=4000))
    assert empty is not None and empty.mechanical and "empty" in empty.problem
    assert "could not be summarised" in empty.summary and "question 0" in empty.summary
    short = run(compact.compact(view, stream=Script([*say("part")[:-1], {"type": "response.incomplete", "response": {"id": "r", "incomplete_details": {"reason": "max_output_tokens"}}}]), model="m", reasoning=None, window=4000))
    assert short is not None and short.mechanical and "cut short" in short.problem


def test_a_request_that_fails_does_not_end_the_conversation():
    result = run(compact.compact(long_view(), stream=Script(fail("invalid_request_error", "overloaded")), model="m", reasoning=None, window=4000))
    assert result is not None and result.mechanical and "overloaded" in result.problem
    assert compact.is_summary(result.view[0])


def _pauses(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    seconds: list[float] = []

    async def pause(delay: float) -> None:
        seconds.append(delay)

    monkeypatch.setattr(compact, "_sleep", pause)
    return seconds


def test_a_summary_request_that_failed_in_a_way_that_may_pass_is_asked_again(monkeypatch):
    pauses = _pauses(monkeypatch)
    script = Script(fail("server_error", "overloaded"), fail("rate_limit_exceeded", "slow down"), say("The summary."))
    result = run(compact.compact(long_view(), stream=script, model="m", reasoning=None, window=4000))
    assert result is not None and not result.mechanical and result.summary == "The summary."
    assert len(script.requests) == 3 and script.requests[0] == script.requests[2], "the same request, asked again"
    assert pauses == list(compact.RETRY_DELAYS[:2])


def test_a_summary_request_is_asked_again_a_few_times_at_most_and_never_for_a_failure_that_stays(monkeypatch):
    pauses = _pauses(monkeypatch)
    busy = Script(*[fail("server_error", "overloaded") for _ in range(len(compact.RETRY_DELAYS) + 1)])
    result = run(compact.compact(long_view(), stream=busy, model="m", reasoning=None, window=4000))
    assert result is not None and result.mechanical and "overloaded" in result.problem
    assert len(busy.requests) == len(compact.RETRY_DELAYS) + 1 and pauses == list(compact.RETRY_DELAYS)
    pauses.clear()
    wrong = Script(fail("invalid_request_error", "bad request"), say("never"))
    result = run(compact.compact(long_view(), stream=wrong, model="m", reasoning=None, window=4000))
    assert result is not None and result.mechanical and len(wrong.requests) == 1 and pauses == []


def test_a_summary_request_waits_as_long_as_the_server_asks_within_a_limit(monkeypatch):
    pauses = _pauses(monkeypatch)
    errors = [ChatGPTError("slow", status=429, retry_after=7.0), ChatGPTError("slow", status=429, retry_after=600.0)]
    answers = Script(say("S"))

    def stream(request: dict[str, Any]):
        if not errors:
            return answers(request)
        error = errors.pop(0)

        async def failing():
            raise error
            yield  # pragma: no cover

        return failing()

    result = run(compact.compact(long_view(), stream=stream, model="m", reasoning=None, window=4000))
    assert result is not None and not result.mechanical and result.summary == "S"
    assert pauses == [7.0, compact.RETRY_LONGEST]


def test_a_stop_during_the_summary_is_not_swallowed():
    def stream(request: dict[str, Any]):
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        run(compact.compact(long_view(), stream=stream, model="m", reasoning=None, window=4000))


def test_nothing_is_asked_when_there_is_nothing_to_cut():
    script = Script()
    assert run(compact.compact([u("only")], stream=script, model="m", reasoning=None, window=4000)) is None
    assert script.requests == []


# ----------------------------------------------------------------------------------------------- the server says no


@pytest.mark.parametrize(
    "code, message",
    [
        ("context_length_exceeded", "x"),
        ("input_too_long", "x"),
        (None, "Your input exceeds the context window of this model."),
        (None, "This model's maximum context length is 272000 tokens."),
        (None, "The prompt is too long."),
        ("invalid_request_error", "Input is too large for the model."),
        (None, "too many tokens in the request"),
    ],
)
def test_the_refusals_that_mean_too_big(code, message):
    assert compact.is_overflow(ChatGPTError(message, code=code, status=400))


@pytest.mark.parametrize(
    "code, message",
    [
        ("subscription_sharing_usage_limit_exceeded", "usage limit exceeded, the context window of the plan"),
        ("rate_limit_exceeded", "Too many requests."),
        (None, "The stream ended before the response was complete."),
        ("invalid_api_key", "Incorrect key."),
        (None, "The model gpt-x does not exist."),
    ],
)
def test_the_refusals_that_do_not(code, message):
    assert not compact.is_overflow(ChatGPTError(message, code=code))


# ----------------------------------------------------------------------------------------------- the loop's doors


class Watching(Recorder):
    """A recorder that is asked before each request and answers as told."""

    def __init__(self, replace_with: list[dict[str, Any]] | None = None, replace_on: int = 1) -> None:
        super().__init__()
        self.asked_before: list[tuple[int, bool, int]] = []
        self.replace_with = replace_with
        self.replace_on = replace_on

    async def prepare(self, history, estimate, overflow):
        self.asked_before.append((estimate, overflow, len(history)))
        if self.replace_with is not None and len(self.asked_before) == self.replace_on:
            history[:] = self.replace_with
            return True
        return False


def run_loop(script: Script, observer: Observer, history: list[dict[str, Any]] | None = None):
    history = history if history is not None else [u("go")]
    loop = AgentLoop(script, FakeTools(), observer, model="m", reasoning=None, instructions="be brief " * 50)
    return asyncio.run(loop.run(history)), history, loop


def test_the_observer_is_asked_before_each_request_with_the_measure_and_what_came_after_it():
    script = Script(call("echo", {"text": "x" * 700}, call_id="c1", used=usage(1000, 50)), say("fine", used=usage(1300, 10)))
    observer = Watching()
    _, history, _ = run_loop(script, observer)
    (first, _, count_one), (second, _, count_two) = observer.asked_before
    assert count_one == 1 and count_two == 3
    assert first > compact.estimate_text("be brief " * 50), "before any response the instructions and the tools are in the estimate"
    assert second == 1050 + compact.estimate_item(history[2]), "the last request's input and answer, plus the tool result that came after"
    assert [flag for _, flag, _ in observer.asked_before] == [False, False]


def test_a_history_the_observer_replaced_is_measured_again_from_scratch():
    shorter = [u("summary"), u("go")]
    script = Script(call("echo", {}, call_id="c1", used=usage(5000, 50)), say("fine", used=usage(300, 10)))
    observer = Watching(replace_with=shorter, replace_on=2)
    run_loop(script, observer)
    assert script.requests[1]["input"][0] == u("summary"), "the second request already reads the shorter history"
    assert len(observer.asked_before) == 2


def test_after_a_replacement_the_old_measure_no_longer_counts():
    shorter = [u("tiny")]
    # (the answer after the replacement brings no figures: whatever is measured then can only be left from before it)
    script = Script(call("echo", {}, call_id="c1", used=usage(50_000, 50)), call("echo", {}, call_id="c2", used=usage(0, 0)), say("fine", used=usage(120, 5)))
    observer = Watching(replace_with=shorter, replace_on=2)
    run_loop(script, observer)
    third = observer.asked_before[2][0]
    assert third < 2_000, "the 50 000 belonged to a history that is gone"


def test_a_request_refused_as_too_big_is_asked_again_once_after_the_observer_shortened_the_history():
    shorter = [u("summary"), u("go")]
    script = Script(fail("context_length_exceeded", "too long"), say("fine", used=usage(40, 4)))
    observer = Watching(replace_with=shorter, replace_on=2)
    end, history, _ = run_loop(script, observer)
    assert end.reason == "done" and len(script.requests) == 2
    assert [flag for _, flag, _ in observer.asked_before] == [False, True]
    assert script.requests[1]["input"][0] == u("summary")
    assert history[0] == u("summary") and history[-1]["role"] == "assistant"


def test_a_refusal_in_a_later_step_is_tried_again_whatever_the_earlier_steps_streamed():
    shorter = [u("summary"), u("go")]
    script = Script(call("echo", {}, call_id="c1", used=usage(100, 5)), fail("context_length_exceeded", "too long"), say("fine", used=usage(40, 4)))
    observer = Watching(replace_with=shorter, replace_on=3)
    end, _, _ = run_loop(script, observer)
    assert end.reason == "done" and len(script.requests) == 3
    assert [flag for _, flag, _ in observer.asked_before] == [False, False, True]


def test_a_refusal_the_observer_cannot_help_with_is_the_error():
    script = Script(fail("context_length_exceeded", "too long"))
    with pytest.raises(ChatGPTError, match="too long"):
        run_loop(script, Watching())


def test_a_second_refusal_is_the_error_not_another_attempt():
    script = Script(fail("context_length_exceeded", "too long"), fail("context_length_exceeded", "still too long"), say("never"))
    observer = Watching(replace_with=[u("go")], replace_on=2)
    with pytest.raises(ChatGPTError, match="still too long"):
        run_loop(script, observer)
    assert len(script.requests) == 2


def test_a_refusal_after_the_answer_began_is_not_tried_again():
    broken = [*say("partial text")[:3], fail("context_length_exceeded", "too long")[1]]
    observer = Watching(replace_with=[u("go")], replace_on=2)
    with pytest.raises(ChatGPTError):
        run_loop(Script(broken, say("never")), observer)
    assert len(observer.asked_before) == 1, "the observer was asked once, before the request, and not again for the refusal"


def test_an_error_that_is_not_about_size_is_not_tried_again():
    observer = Watching(replace_with=[u("go")], replace_on=2)
    with pytest.raises(ChatGPTError):
        run_loop(Script(fail("invalid_request_error", "oops"), say("never")), observer)  # (a busy server is asked again, so not that kind of error)
    assert len(observer.asked_before) == 1
