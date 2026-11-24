"""A reused summary prefix must not reactivate rules invalidated in the kept tail."""

from __future__ import annotations

import asyncio
from copy import deepcopy

import pytest

from desktop.core.chatgpt.responses import build_request, user_message
from desktop.core.harness import compact
from desktop.core.harness.prompt import context_refresh, context_update
from tests.fake_responses import say
from tests.test_harness_loop import Script


OLDER = "gpt-6.1-sol"
NEWER = "gpt-6.2-sol"
SNAPSHOT = "Current rules: report the evidence."


def _assistant(text):
    return {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": text}]}


@pytest.fixture
def history():
    # The earlier answer is too large to keep; both recent exchanges fit in the tail.
    covered = [user_message("Start the review."), _assistant("Review started."),
               context_update("Obsolete rule: write a plan only."),
               user_message("Review the initial batch."), _assistant("Earlier evidence. " * 250)]
    recent = [user_message("Review the next batch."), _assistant("The next batch is complete."),
              user_message("Confirm the findings."), _assistant("The findings are confirmed.")]
    return covered, recent


def _base(model=OLDER, instructions=SNAPSHOT):
    return build_request(model=model, instructions=instructions, items=[], reasoning=None,
                         tools=[{"name": "read_file", "parameters": {"type": "object"}}])


def _expected_prefix(base, items):
    return {**base, "tool_choice": "none",
            "input": [*base["input"], *items, user_message(compact.SUMMARY_TASK + compact.INSTRUCTIONS)]}


def _run(view, base, window=4_000):
    stream = Script(say("Summary."))
    result = asyncio.run(compact.compact(view, stream=stream, model=base["model"], reasoning=None,
                                         window=window, prefix_request=base, rebase=True))
    assert result is not None and result.fits and not result.mechanical
    (request,) = stream.requests
    return result, request


@pytest.mark.parametrize("model", [OLDER, NEWER])
def test_prefix_summary_restores_the_refresh_before_the_task(history, model):
    covered, recent = history
    refresh = context_refresh()
    tail = [*recent, refresh, user_message("Continue under the new rules.")]
    view = [*covered, *tail]
    base = _base(model)
    original_view, original_base = deepcopy(view), deepcopy(base)

    result, request = _run(view, base)

    assert request == _expected_prefix(original_base, [*covered, refresh])
    if model == NEWER:
        assert "instructions" not in request
        assert request["input"][0] == {"type": "message", "role": "developer",
                                       "content": [{"type": "input_text", "text": SNAPSHOT}]}
    else:
        assert request["instructions"] == SNAPSHOT
        assert request["input"][0] == covered[0]
    assert result.cut == len(covered)
    assert result.view[1:] == original_view[result.cut:] == tail
    assert view == original_view and base == original_base


@pytest.mark.parametrize("case", ["no-update", "refresh-already-covered", "no-refresh"])
def test_other_prefix_summaries_are_unchanged(history, case):
    covered, recent = history
    if case == "no-update":
        covered = [item for item in covered if not item.get("wc_context_update")]
    elif case == "refresh-already-covered":
        # A refresh already follows the old update, so do not copy another one from the tail.
        covered = [*covered, context_refresh()]
    tail = [*recent, *([] if case == "no-refresh" else [context_refresh()]), user_message("Continue.")]
    base = _base()

    result, request = _run([*covered, *tail], base)

    assert result.cut == len(covered) and result.view[1:] == tail
    assert request == _expected_prefix(base, covered)


@pytest.mark.parametrize("later_update", [False, True], ids=["already-invalidated", "invalidated-in-tail"])
def test_prefix_summary_uses_the_last_refresh_to_check_which_updates_still_apply(history, later_update):
    covered, recent = history
    refresh = context_refresh()
    # The first refresh precedes the initial batch; the second remains in the tail.
    covered = [*covered[:-2], refresh, *covered[-2:]]
    if later_update:
        covered.append(context_update("Obsolete rule: stop after the first batch."))
    tail = [*recent, refresh, user_message("Continue.")]
    base = _base()

    result, request = _run([*covered, *tail], base)

    expected = [*covered, refresh] if later_update else covered
    assert request == _expected_prefix(base, expected)
    assert result.cut == len(covered) and result.view[1:] == tail


def test_refresh_that_exceeds_the_prefix_budget_falls_back_to_standalone_summary(history):
    covered, recent = history
    refresh = context_refresh()
    tail = [*recent, refresh, user_message("Continue.")]
    base = _base(NEWER, instructions=(SNAPSHOT + " ") * 40)
    plain = compact._request_tokens(_expected_prefix(base, covered))
    marked = compact._request_tokens(_expected_prefix(base, [*covered, refresh]))
    window = plain * 100 // 95
    assert plain <= compact.budget(window) < marked

    result, request = _run([*covered, *tail], base, window)

    assert result.cut == len(covered) and result.view[1:] == tail
    assert request["instructions"] == compact.INSTRUCTIONS
    assert "tools" not in request and "tool_choice" not in request
    (message,) = request["input"]
    assert message["role"] == "user"
    body = message["content"][0]["text"]
    assert "<conversation_part>\n" + "\n\n".join(compact.render_parts(covered)) + "\n</conversation_part>" in body
    assert "DEVELOPER: WittyCode context update:" in body
    assert "Obsolete rule: write a plan only." in body
