"""Build transcript turns from a saved session's message history.

``Session.resume()`` does not replay history, so saved sessions are rendered from
the messages returned by the low-level ``DroidClient.load_session()``
(``FactoryDroidMessage`` objects: user / assistant / tool roles, tool results
linked by ``tool_use_id``). Hidden context messages are skipped and never
rendered.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from desktop.core.attachments import attachments_from_content, typed_text
from desktop.core.transcript import TurnStatus, is_hidden_message

if TYPE_CHECKING:
    from desktop.core.transcript import Transcript, Turn


def _value(value: Any) -> Any:
    return getattr(value, "value", value)


def _kind(block: Any) -> str:
    return str(_value(getattr(block, "type", "")))


def _seconds(milliseconds: Any) -> float | None:
    return float(milliseconds) / 1000.0 if isinstance(milliseconds, (int, float)) else None


def _apply_result(transcript: Transcript, turn: Turn, block: Any, at: float | None) -> None:
    tool_use_id = getattr(block, "tool_use_id", None)
    if not tool_use_id:
        return
    item = turn.tools.get(tool_use_id) or transcript.ensure_tool(turn, tool_use_id, "?")
    transcript.record_result(turn, item, getattr(block, "content", None), getattr(block, "is_error", None), at=at)


def _finish(transcript: Transcript, turn: Turn) -> None:
    transcript.finish_turn(turn, TurnStatus.DONE)


def build_history_turns(transcript: Transcript, messages: Sequence[Any]) -> list[Turn]:
    turns: list[Turn] = []
    current: Turn | None = None
    for message in messages:
        if is_hidden_message(getattr(message, "id", None), _value(getattr(message, "visibility", None))):
            continue
        role = _value(getattr(message, "role", None))
        created = _seconds(getattr(message, "created_at", None))
        updated = _seconds(getattr(message, "updated_at", None)) or created
        content = list(getattr(message, "content", None) or ())

        if role == "user":
            carries_results = False
            for block in content:
                if _kind(block) == "tool_result":
                    carries_results = True
                    if current is not None:
                        _apply_result(transcript, current, block, updated)
            text = typed_text(content)
            # A message that only returns tool results is part of the running turn, whatever else it carries.
            attachments = [] if carries_results and not text.strip() else attachments_from_content(content)
            if not text.strip() and not attachments:
                continue
            if current is not None:
                _finish(transcript, current)
            current = transcript.new_history_turn(text, message_id=message.id, started_at=created, attachments=attachments)
            current.finished_at = updated
            turns.append(current)
            continue

        if role == "assistant":
            if current is None:
                current = transcript.new_history_turn(None, started_at=created)
                turns.append(current)
            for index, block in enumerate(content):
                kind = _kind(block)
                if kind == "text":
                    if block.text.strip():
                        view = transcript.text_block(current, message.id, index)
                        view.text = block.text
                elif kind == "thinking":
                    if block.thinking.strip():
                        entry = transcript.thinking_entry(current, message.id, index)
                        entry.text = block.thinking
                        entry.complete = True
                        duration = _seconds(getattr(block, "duration_ms", None))
                        entry.duration = duration if duration else None
                elif kind == "redacted_thinking":
                    entry = transcript.thinking_entry(current, message.id, index)
                    entry.redacted = True
                    entry.complete = True
                elif kind == "tool_use":
                    item = transcript.ensure_tool(current, block.id, block.name, message_id=message.id)
                    transcript.set_tool_input(current, item, block.input or {}, final=True)
            model = getattr(message, "model_id", None)
            if model:
                current.model = model
        elif role == "tool" and current is not None:
            for block in content:
                if _kind(block) == "tool_result":
                    _apply_result(transcript, current, block, updated)
        else:
            continue

        if current is not None and updated is not None:
            current.finished_at = max(current.finished_at or updated, updated)

    if current is not None:
        _finish(transcript, current)
    return turns