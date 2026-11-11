"""The page of a saved ChatGPT Plan conversation (``chatgpt.history``) for turns that did not end the ordinary way: cut off
by a crash, stopped at the step limit, with an answer cut short."""

from __future__ import annotations

from desktop.core.chatgpt.history import build_turns
from desktop.core.harness.loop import INTERRUPTED
from desktop.core.harness.store import LOST, SavedSession, SavedTurn
from desktop.core.harness.tools import ToolRegistry
from desktop.core.transcript import ToolStatus, Transcript, TurnStatus

USER = {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "go"}]}
ANSWER = {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "Done."}]}


def call(call_id: str) -> dict:
    return {"type": "function_call", "call_id": call_id, "name": "shell", "arguments": '{"command": "dir"}'}


def output(call_id: str, text: str) -> dict:
    return {"type": "function_call_output", "call_id": call_id, "output": text}


def page(items: list[dict], turns: list[SavedTurn], tools: dict | None = None) -> list:
    saved = SavedSession(id="s", project="p", items=items, turns=turns, tools=tools or {})
    return build_turns(Transcript(), ToolRegistry.default(), saved)


def kinds(turn) -> list[str]:
    return [block.kind for block in turn.blocks]


def test_a_result_that_never_reached_the_disk_is_shown_as_cancelled_whatever_else_the_file_says():
    (turn,) = page([USER, call("c1"), output("c1", LOST)], [SavedTurn(0, 10.0, 20.0, "interrupted")], tools={"c1": {"status": "done", "diffs": []}})
    (tool,) = turn.tools.values()
    assert tool.status is ToolStatus.CANCELLED and tool.result == LOST


def test_a_stopped_call_is_still_cancelled_and_a_finished_one_done():
    (turn,) = page([USER, call("c1"), output("c1", "Volume in drive D"), call("c2"), output("c2", INTERRUPTED)], [SavedTurn(0, 10.0, 20.0, "interrupted")])
    assert [tool.status for tool in turn.tools.values()] == [ToolStatus.DONE, ToolStatus.CANCELLED]


def test_a_turn_that_is_still_running_in_the_file_is_shown_as_cut_off_not_as_finished():
    (turn,) = page([USER, call("c1")], [SavedTurn(0, 10.0, None, "running")])
    assert turn.status is TurnStatus.INTERRUPTED
    assert next(iter(turn.tools.values())).status is ToolStatus.CANCELLED


def test_the_note_of_a_turn_is_shown_at_its_end_and_limit_and_incomplete_are_finished_turns():
    turns = page(
        [USER, ANSWER, USER, ANSWER, USER, call("c1"), output("c1", LOST)],
        [
            SavedTurn(0, 10.0, 20.0, "limit", "", "m", "这一轮用了 80 步还没结束，先停在这里；可以回复「继续」。"),
            SavedTurn(2, 30.0, 40.0, "incomplete", "", "m", "回答太长，被截断了；可以回复「继续」。"),
            SavedTurn(4, 50.0, 70.0, "interrupted", "", "m", "WittyCode 在这一轮进行中被关闭或崩溃了"),
        ],
    )
    assert [turn.status for turn in turns] == [TurnStatus.DONE, TurnStatus.DONE, TurnStatus.INTERRUPTED]
    assert [kinds(turn)[-1] for turn in turns] == ["notice", "notice", "notice"]
    assert [turn.blocks[-1].text[:6] for turn in turns] == ["这一轮用了 ", "回答太长，被", "WittyC"]
    assert turns[2].duration == 20.0


def test_a_turn_without_a_note_gets_no_notice():
    (turn,) = page([USER, ANSWER], [SavedTurn(0, 10.0, 20.0, "done")])
    assert "notice" not in kinds(turn) and turn.status is TurnStatus.DONE
