"""The model's ``ask_user`` tool in the application's own harness: the questions, the card, the answers that go back."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from desktop.core.harness import asking
from desktop.core.harness.loop import ALLOW, Call
from desktop.core.harness.prompt import BASE
from desktop.core.harness.tools import ToolContext, ToolRegistry
from desktop.core.harness.toolset import HarnessTools, ui_call
from desktop.core.prompts import QuestionView
from desktop.core.transcript import PromptStatus, ToolStatus, TurnStatus
from tests.chatgpt_support import ACCESS_TOKEN, finish_turn, make_engine, until
from tests.fake_responses import FakeResponses, call, say

QUESTIONS = {
    "questions": [
        {"question": "Which database?", "header": "Storage", "options": ["SQLite", "Postgres", {"label": "Files"}]},
        {"question": "Which checks to run?", "options": ["lint", "tests", "lint", "types"], "multi_select": True},
    ]
}


def run(coroutine: Any) -> Any:
    return asyncio.run(coroutine)


def a_call(args: Any, name: str = asking.NAME) -> Call:
    return Call(item_id="i1", call_id="c1", name=name, raw=json.dumps(args), args=args if isinstance(args, dict) else {})


def tools(tmp_path: Path, asker: Any = None) -> HarnessTools:
    return HarnessTools(ToolRegistry.default(), ToolContext(tmp_path, __import__("threading").Event()), "code", asker=asker)


# ----------------------------------------------------------------------------------------------- the questions


def test_the_questions_are_read_with_their_options_and_the_odd_ones_cleaned():
    views, problem = asking.parse(QUESTIONS)
    assert problem == ""
    assert [(v.index, v.topic, v.question, v.options, v.multi_select) for v in views] == [
        (0, "Storage", "Which database?", ("SQLite", "Postgres", "Files"), False),
        (1, "", "Which checks to run?", ("lint", "tests", "types"), True),
    ]
    assert all(isinstance(v, QuestionView) for v in views)


def test_a_single_question_object_is_taken_as_a_list_of_one_and_the_extra_questions_are_cut():
    views, _ = asking.parse({"questions": {"question": "Sure?", "options": ["yes", "no"]}})
    assert [v.question for v in views] == ["Sure?"]
    many = {"questions": [{"question": f"Q{n}?", "options": ["a", "b"]} for n in range(7)]}
    assert len(asking.parse(many)[0]) == asking.MAX_QUESTIONS
    long = {"questions": [{"question": "Pick?", "header": "x" * 80, "options": [str(n) for n in range(20)]}]}
    (view,) = asking.parse(long)[0]
    assert len(view.topic) == asking.HEADER_LIMIT and len(view.options) == asking.MAX_OPTIONS


def test_unusable_arguments_are_a_sentence_for_the_model_not_a_card():
    for bad in ({}, {"questions": []}, {"questions": "Which?"}, {"questions": [7]}, {"questions": [{"options": ["a"]}]}, {"questions": [{"question": "  "}]}):
        views, problem = asking.parse(bad)
        assert views == (), bad
        assert any(part in problem for part in ("ask_user needs", "has no text", "is not an object")), bad


def test_the_page_reads_the_questions_like_a_droid_askuser_row():
    registry = ToolRegistry.default()
    name, data = ui_call(registry, asking.NAME, QUESTIONS)
    assert name == "AskUser"
    assert data["questionnaire"].splitlines()[:5] == ["1. [question] Which database?", "[topic] Storage", "[option] SQLite", "[option] Postgres", "[option] Files"]
    from desktop.core.toolinfo import describe_input

    assert describe_input("AskUser", data) == "Which database?（共 2 题）"


def test_the_answers_go_back_to_the_model_with_their_questions():
    views, _ = asking.parse(QUESTIONS)
    text = asking.result(views, ["Postgres", ["lint", "tests"]])
    assert text == "The user answered:\n1. Which database?\n   Answer: Postgres\n2. Which checks to run?\n   Answer: lint, tests"
    assert "(no answer)" in asking.result(views[:1], ["  "])


def test_the_prompt_tells_the_model_to_use_the_tool_not_to_end_the_turn_with_a_question():
    assert "ask_user" in BASE and "do not end the turn with a question in plain text" in BASE


# ----------------------------------------------------------------------------------------------- the tool set


def test_the_tool_is_offered_only_where_there_is_somebody_to_ask(tmp_path):
    async def asker(call, views):
        return ["x"]

    without, with_ = tools(tmp_path), tools(tmp_path, asker)
    assert asking.NAME not in {d["name"] for d in without.declarations()} and not without.known(asking.NAME)
    declared = {d["name"]: d for d in with_.declarations()}
    assert declared[asking.NAME]["parameters"]["required"] == ["questions"] and with_.known(asking.NAME)
    for level in ("ask", "code", "full"):
        with_.level = level
        assert with_.check(a_call(QUESTIONS)).verdict == ALLOW, "asking is not an action: it needs no approval at any level"


def test_running_the_tool_shows_the_questions_and_returns_what_was_answered(tmp_path):
    async def go() -> None:
        seen: list[Any] = []

        async def asker(call, views):
            seen.append((call.call_id, views))
            return ["SQLite", ["lint"]]

        outcome = await tools(tmp_path, asker).run(a_call(QUESTIONS))
        assert not outcome.is_error and not outcome.cancelled
        assert outcome.output.startswith("The user answered:") and "Answer: SQLite" in outcome.output and "Answer: lint" in outcome.output
        assert seen[0][0] == "c1" and [v.question for v in seen[0][1]] == ["Which database?", "Which checks to run?"]

    run(go())


def test_a_skipped_question_and_a_broken_call_are_results_the_model_can_read(tmp_path):
    async def go() -> None:
        async def skip(call, views):
            return None

        skipped = await tools(tmp_path, skip).run(a_call(QUESTIONS))
        assert skipped.cancelled and skipped.output == asking.SKIPPED and "Do not ask it again" in skipped.output
        asked: list[Any] = []

        async def never(call, views):
            asked.append(call)
            return ["x"]

        broken = await tools(tmp_path, never).run(a_call({"questions": []}))
        assert broken.is_error and "ask_user needs" in broken.output and asked == [], "nothing is shown for arguments that cannot be a question"

    run(go())


# ----------------------------------------------------------------------------------------------- a conversation


def opened(tmp_path: Path, fake: FakeResponses):
    async def go():
        folder = tmp_path / "project"
        folder.mkdir(exist_ok=True)
        engine = make_engine(tmp_path, fake)
        return engine, await engine.open_new(folder, model="gpt-6.1-sol", reasoning="medium", autonomy="ask")

    return go()


def test_the_model_asks_mid_turn_the_user_answers_on_the_card_and_the_turn_goes_on(tmp_path):
    async def go() -> None:
        with FakeResponses(token=ACCESS_TOKEN) as fake:
            fake.script(call(asking.NAME, QUESTIONS, call_id="c1", text="I need two decisions."), say("SQLite it is."))
            engine, controller = await opened(tmp_path, fake)
            assert asking.NAME in {t["name"] for t in (await _first_request_tools(fake, controller))}
            await until(lambda: controller.transcript.pending_prompts(), message="the question card")
            (prompt,) = controller.transcript.pending_prompts()
            assert prompt.kind == "question" and [q.question for q in prompt.questions] == ["Which database?", "Which checks to run?"]
            assert prompt.questions[0].options == ("SQLite", "Postgres", "Files") and prompt.questions[1].multi_select
            assert controller.working_label == "等待你的确认"
            assert controller.answer_question(prompt.id, ["SQLite", ["lint", "tests"]])
            await finish_turn(controller)
            (tool,) = [t for turn in controller.transcript.turns for t in turn.tools.values()]
            assert tool.name == "AskUser" and tool.status is ToolStatus.DONE and "Answer: SQLite" in tool.result
            output = fake.requests[1]["input"][-1]
            assert output["type"] == "function_call_output" and output["call_id"] == "c1"
            assert "Answer: SQLite" in output["output"] and "Answer: lint, tests" in output["output"]
            assert controller.transcript.turns[0].status is TurnStatus.DONE
            qa = [b for b in controller.transcript.turns[0].blocks if b.kind == "qa"]
            assert qa and qa[0].pairs == [("Which database?", "SQLite"), ("Which checks to run?", "lint, tests")]
            await engine.close_all()

    run(go())


def test_skipping_the_card_tells_the_model_and_the_turn_still_ends(tmp_path):
    async def go() -> None:
        with FakeResponses(token=ACCESS_TOKEN) as fake:
            fake.script(call(asking.NAME, QUESTIONS, call_id="c1"), say("then I assume SQLite."))
            engine, controller = await opened(tmp_path, fake)
            controller.send_prompt("set up storage")
            await until(lambda: controller.transcript.pending_prompts(), message="the question card")
            (prompt,) = controller.transcript.pending_prompts()
            assert controller.answer_question(prompt.id, None)
            await finish_turn(controller)
            assert fake.requests[1]["input"][-1]["output"] == asking.SKIPPED
            (tool,) = [t for turn in controller.transcript.turns for t in turn.tools.values()]
            assert tool.status is ToolStatus.CANCELLED
            await engine.close_all()

    run(go())


def test_a_card_nobody_answers_ends_with_the_turn_and_a_late_answer_is_refused(tmp_path):
    async def go() -> None:
        with FakeResponses(token=ACCESS_TOKEN) as fake:
            fake.script(call(asking.NAME, QUESTIONS, call_id="c1"), say("never"))
            engine, controller = await opened(tmp_path, fake)
            controller.send_prompt("set up storage")
            await until(lambda: controller.transcript.pending_prompts(), message="the question card")
            (prompt,) = controller.transcript.pending_prompts()
            await controller.interrupt()
            await until(lambda: controller.state.value == "ready", message="the turn to end")
            assert prompt.status is PromptStatus.EXPIRED
            assert not controller.answer_question(prompt.id, ["SQLite", ["lint"]]), "the card is gone"
            assert len(fake.requests) == 1, "the model was not asked again after the stop"
            await engine.close_all()

    run(go())


def test_a_wrong_number_of_answers_is_refused_and_the_card_stays_open(tmp_path):
    async def go() -> None:
        with FakeResponses(token=ACCESS_TOKEN) as fake:
            fake.script(call(asking.NAME, QUESTIONS, call_id="c1"), say("ok"))
            engine, controller = await opened(tmp_path, fake)
            controller.send_prompt("set up storage")
            await until(lambda: controller.transcript.pending_prompts(), message="the question card")
            (prompt,) = controller.transcript.pending_prompts()
            assert not controller.answer_question(prompt.id, ["SQLite"])
            assert prompt.status is PromptStatus.PENDING and controller.state.value == "running"
            assert controller.answer_question(prompt.id, ["SQLite", ["lint"]])
            await finish_turn(controller)
            await engine.close_all()

    run(go())


async def _first_request_tools(fake: FakeResponses, controller: Any) -> list[dict[str, Any]]:
    controller.send_prompt("set up storage")
    await until(lambda: fake.requests, message="the first request")
    return fake.requests[0]["tools"]
