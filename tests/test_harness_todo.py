"""``todo_write`` (``core/harness/todo``): the model's checklist, how it is allowed and shown, and the loop's reminder when the model
makes the same call again and again. Both ideas come from DeepSeek Harness (its ``todo`` and ``guard`` packages)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from desktop.core.harness.loop import ASK, REPEAT_AFTER, Verdict
from desktop.core.harness.permissions import LEVELS, decide
from desktop.core.harness.todo import MAX_ITEMS, NAME, read_todos, todo_write
from desktop.core.harness.toolset import HarnessTools, ui_call
from desktop.core.harness.tools import ToolContext, ToolRegistry
from desktop.core.toolinfo import parse_todos
from tests.fake_responses import call, say
from tests.test_harness_loop import FakeTools, Recorder, Script, run


def todos(*pairs: tuple[str, str]) -> list[dict]:
    return [{"content": content, "status": status} for content, status in pairs]


# ------------------------------------------------------------------------------------------------ the tool


def test_the_tool_is_in_the_default_registry_and_shown_as_the_pages_todowrite_row():
    registry = ToolRegistry.default()
    spec = registry.get(NAME)
    assert spec is not None and spec.ui_name == "TodoWrite" and spec.kind == "read"
    assert NAME in [d["name"] for d in HarnessTools(registry, ToolContext(Path(".")), "ask").declarations()]
    args = {"todos": todos(("Read the code", "completed"), ("Fix it", "in_progress"), ("Run the tests", "pending"))}
    name, shown = ui_call(registry, NAME, args)
    assert name == "TodoWrite" and shown == args
    assert [(t.status, t.text) for t in parse_todos(shown["todos"])] == [("completed", "Read the code"), ("in_progress", "Fix it"), ("pending", "Run the tests")]


def test_a_list_is_accepted_and_the_answer_says_how_far_it_is_and_what_is_next():
    result = todo_write({"todos": todos(("a", "completed"), ("b", "in_progress"), ("c", "pending"))}, ToolContext(Path(".")))
    assert not result.is_error and result.output == "Checklist updated: 1 of 3 done. In progress: b."
    assert result.data["todos"] == todos(("a", "completed"), ("b", "in_progress"), ("c", "pending"))
    idle = todo_write({"todos": todos(("a", "completed"), ("b", "pending"))}, ToolContext(Path(".")))
    assert "Mark the next step in_progress" in idle.output
    finished = todo_write({"todos": todos(("a", "completed"))}, ToolContext(Path(".")))
    assert finished.output == "Checklist updated: 1 of 1 done."


@pytest.mark.parametrize(
    ("bad", "fragment"),
    [
        ([{"content": "x", "status": "doing"}], "step 1 has the status 'doing'"),
        ([{"content": "  ", "status": "pending"}], "step 1 has no `content`"),
        ([{"status": "pending"}], "step 1 has no `content`"),
        (["just text"], "step 1 is not an object"),
        (todos(("a", "in_progress"), ("b", "in_progress")), "only one step may be in_progress"),
        (todos(*[(f"s{i}", "pending") for i in range(MAX_ITEMS + 1)]), f"at most {MAX_ITEMS} steps"),
    ],
)
def test_a_list_that_is_not_right_is_refused_with_every_problem_for_the_model_to_read(bad, fragment):
    with pytest.raises(ValueError) as raised:
        read_todos(bad)
    assert fragment in str(raised.value)
    result = todo_write({"todos": bad}, ToolContext(Path(".")))
    assert result.is_error and result.output.startswith("The checklist was not accepted:") and fragment in result.output


def test_a_step_is_cut_to_a_sensible_length():
    (only,) = read_todos([{"content": "x" * 1000, "status": "pending"}])
    assert len(only["content"]) == 300


@pytest.mark.parametrize("level", LEVELS)
def test_it_is_allowed_at_every_level_without_asking(level, tmp_path):
    registry = ToolRegistry.default()
    context = ToolContext(tmp_path)
    decision = decide(level, registry.get(NAME), {"todos": todos(("a", "pending"))}, context, remembered=set())
    assert decision.verdict == "allow", decision


def test_the_whole_run_through_the_registry_checks_the_arguments_like_any_tool(tmp_path):
    async def go() -> None:
        registry = ToolRegistry.default()
        context = ToolContext(tmp_path)
        missing = await registry.run(NAME, {}, context)
        assert missing.is_error and "'todos' is required" in missing.output
        ok = await registry.run(NAME, {"todos": todos(("a", "in_progress"))}, context)
        assert not ok.is_error and ok.data["todos"][0]["status"] == "in_progress"

    asyncio.run(go())


# ------------------------------------------------------------------------------------------------ the reminder


def outputs_of(history: list[dict]) -> list[str]:
    return [item["output"] for item in history if item["type"] == "function_call_output"]


def test_the_same_call_again_and_again_gets_a_reminder_from_the_third_time_and_still_runs():
    turns = [call("echo", {"text": "a"}, call_id=f"c{i}") for i in range(1, 5)] + [say("giving up")]
    end, history, tools, _ = run(Script(*turns))
    results = outputs_of(history)
    assert len(tools.ran) == 4, "advisory: every call is made"
    assert results[0] == "echo:a" and results[1] == "echo:a"
    assert REPEAT_AFTER == 3
    assert results[2].startswith("echo:a\n\n[Reminder: you have now called echo with exactly these arguments 3 times in a row.")
    assert "4 times in a row" in results[3] and "change your approach" in results[3].lower()
    assert end.reason == "done"


def test_another_call_in_between_starts_the_count_again_and_other_arguments_are_another_call():
    turns = [
        call("echo", {"text": "a"}, call_id="c1"), call("echo", {"text": "a"}, call_id="c2"), call("echo", {"text": "b"}, call_id="c3"),
        call("echo", {"text": "a"}, call_id="c4"), call("echo", {"text": "a"}, call_id="c5"), say("ok"),
    ]
    _, history, _, _ = run(Script(*turns))
    assert all("Reminder" not in output for output in outputs_of(history))


def test_arguments_in_another_order_are_still_the_same_call():
    turns = [call("echo", {"text": "a", "x": 1}, call_id="c1"), call("echo", {"x": 1, "text": "a"}, call_id="c2"), call("echo", {"text": "a", "x": 1}, call_id="c3"), say("ok")]
    _, history, _, _ = run(Script(*turns))
    assert "Reminder" in outputs_of(history)[2]


def test_a_call_the_user_declined_gets_no_reminder_it_has_its_own_words():
    tools = FakeTools({"echo": Verdict(ASK, "writes")})
    observer = Recorder(answer="cancel")
    turns = [call("echo", {"text": "a"}, call_id=f"c{i}") for i in range(1, 5)] + [say("ok")]
    _, history, _, _ = run(Script(*turns), tools, observer)
    assert all("Reminder" not in output for output in outputs_of(history))


def test_two_equal_calls_in_one_answer_count_too():
    from tests.fake_responses import calls

    script = Script(calls([("echo", '{"text": "a"}', "c1", "f1"), ("echo", '{"text": "a"}', "c2", "f2"), ("echo", '{"text": "a"}', "c3", "f3")]), say("ok"))
    _, history, _, _ = run(script)
    results = outputs_of(history)
    assert "Reminder" not in results[0] and "Reminder" not in results[1] and "Reminder" in results[2]
