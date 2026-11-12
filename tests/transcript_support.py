"""Building transcripts in tests with the builder API every engine's controller writes them with."""

from __future__ import annotations

from typing import Any

from desktop.core.prompts import APPROVAL_OPTIONS, ActionView, QuestionView
from desktop.core.transcript import PermissionPrompt, QuestionPrompt, ToolItem, Transcript, Turn, TurnStatus


def _turn(transcript: Transcript) -> Turn:
    turn = transcript.active_turn
    assert turn is not None, "no turn is running"
    return turn


def say(transcript: Transcript, text: str, message_id: str = "m1", *, done: bool = True) -> None:
    """The assistant writes ``text`` (streamed, then complete unless ``done`` is false)."""
    turn = _turn(transcript)
    transcript.append_text(turn, message_id, 0, text)
    if done:
        transcript.end_text(turn, message_id, 0)


def tool(transcript: Transcript, tool_id: str, name: str, **data: Any) -> ToolItem:
    """The model calls a tool with these arguments."""
    turn = _turn(transcript)
    item = transcript.ensure_tool(turn, tool_id, name)
    transcript.set_tool_input(turn, item, data, final=True)
    return item


def result(transcript: Transcript, tool_id: str, text: str, *, error: bool = False) -> None:
    """The tool's output (its outcome read from the text, the way a result without a status is)."""
    turn = _turn(transcript)
    transcript.record_result(turn, turn.tools[tool_id], text, error)


def finish(transcript: Transcript, *, seconds: float = 1.0, status: TurnStatus = TurnStatus.DONE, usage: Any = None) -> None:
    transcript.finish_turn(_turn(transcript), status, duration=seconds, usage=usage)


def ask_permission(transcript: Transcript, tool_id: str, command: str = "del x") -> PermissionPrompt:
    """An approval for a command the active turn has called (``tool`` it first)."""
    action = ActionView(kind="execute", tool_use_id=tool_id, tool_name="Execute", title="执行命令", command=command)
    prompt = transcript.add_permission_prompt([tool_id], [action], APPROVAL_OPTIONS)
    assert prompt is not None
    return prompt


def ask_question(transcript: Transcript, tool_id: str = "q1") -> QuestionPrompt:
    questions = (QuestionView(index=0, topic="Colour", question="Which colour?", options=("red", "blue"), multi_select=False),)
    prompt = transcript.add_question_prompt(tool_id, questions)
    assert prompt is not None
    return prompt
