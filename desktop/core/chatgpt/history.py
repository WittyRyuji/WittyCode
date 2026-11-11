"""A saved ChatGPT Plan conversation as the turns the page shows.

The saved file holds the Responses items the next request will send (``harness.store``) and, per turn, where it starts and
how it ended. A turn is rebuilt from the items between its start and the next one: the user message (its text, pictures and
attached files in the form the other engines' histories are read in, so a carried-over conversation and the context WittyCode
added are taken out the same way), the model's messages and reasoning summaries, and each tool call with its output. What the
items do not hold (a tool's outcome and the diff of an edit) is in ``SavedSession.tools``; without it the outcome is read
from the output text (a result that never reached the disk, ``store.LOST``, shows as cancelled, never as done). A turn
that stopped at the step limit or with its answer cut short is a finished turn; the notice it showed (``SavedTurn.note``)
is shown at its end again, and so is the one ``store.recover`` gives a turn that a crash cut off.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

from desktop.core.codex.items import user_message_parts
from desktop.core.harness.loop import DECLINED, INTERRUPTED
from desktop.core.harness.store import LOST, SavedSession, SavedTurn, diff_from_json
from desktop.core.harness.tools import ToolRegistry
from desktop.core.harness.toolset import ui_call
from desktop.core.transcript import NoticeBlock, ToolStatus, Transcript, Turn, TurnStatus

TURN_STATUS = {
    "done": TurnStatus.DONE,
    "limit": TurnStatus.DONE,
    "incomplete": TurnStatus.DONE,
    "interrupted": TurnStatus.INTERRUPTED,
    "running": TurnStatus.INTERRUPTED,
    "failed": TurnStatus.FAILED,
}
TOOL_STATUS = {"done": ToolStatus.DONE, "error": ToolStatus.ERROR, "cancelled": ToolStatus.CANCELLED}


def user_parts(item: Mapping[str, Any]) -> tuple[str, list[Any], tuple[int, bool] | None]:
    """(what the user typed, attachments as chips, handoff mark) of a saved user message."""
    content: list[dict[str, Any]] = []
    for part in item.get("content") or ():
        if not isinstance(part, Mapping):
            continue
        if part.get("type") == "input_text":
            content.append({"type": "text", "text": str(part.get("text") or "")})
        elif part.get("type") == "input_image":
            content.append({"type": "image", "url": str(part.get("image_url") or "")})
    return user_message_parts({"content": content})


def _assistant_text(item: Mapping[str, Any]) -> str:
    return "".join(str(part.get("text") or "") for part in item.get("content") or () if isinstance(part, Mapping) and part.get("type") == "output_text")


def _summary(item: Mapping[str, Any]) -> str:
    return "\n\n".join(str(part.get("text") or "") for part in item.get("summary") or () if isinstance(part, Mapping) and part.get("text"))


def _arguments(item: Mapping[str, Any]) -> dict[str, Any]:
    try:
        parsed = json.loads(str(item.get("arguments") or "") or "{}")
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _status_of(output: str, meta: Mapping[str, Any] | None) -> ToolStatus:
    if output == LOST:
        return ToolStatus.CANCELLED
    if isinstance(meta, Mapping) and meta.get("status") in TOOL_STATUS:
        return TOOL_STATUS[meta["status"]]
    if output in (DECLINED, INTERRUPTED) or output.startswith("Refused"):
        return ToolStatus.CANCELLED
    return ToolStatus.DONE


def _is_user(item: Mapping[str, Any]) -> bool:
    return item.get("type") == "message" and item.get("role") == "user"


def build_turn(transcript: Transcript, registry: ToolRegistry, saved: SavedSession, number: int, meta: SavedTurn, items: Sequence[Mapping[str, Any]]) -> Turn:
    first = items[0] if items and _is_user(items[0]) else None
    text, attachments, handoff = user_parts(first) if first is not None else ("", [], None)
    turn = transcript.new_history_turn(text if first is not None else None, started_at=meta.at, attachments=attachments)
    if handoff is not None and turn.user_block is not None:
        turn.user_block.handoff = handoff
    turn.finished_at = meta.finished or meta.at
    turn.model = meta.model or saved.model or None
    prefix = f"h{number}"
    for index, item in enumerate(items[1 if first is not None else 0 :]):
        kind = item.get("type")
        key = f"{prefix}-{index}"
        if kind == "message" and item.get("role") == "assistant":
            transcript.end_text(turn, key, 0, _assistant_text(item))
        elif kind == "message" and item.get("role") == "user":
            said, shown, _ = user_parts(item)
            if said.strip() or shown:
                transcript.add_steer(turn, said, shown, None, pending=False)
        elif kind == "reasoning":
            transcript.end_thinking(turn, key, 0, text=_summary(item))
        elif kind == "function_call":
            name, data = ui_call(registry, str(item.get("name") or ""), _arguments(item))
            tool = transcript.ensure_tool(turn, str(item.get("call_id") or key), name)
            transcript.set_tool_input(turn, tool, data, final=True)
        elif kind == "function_call_output":
            call_id = str(item.get("call_id") or "")
            tool = turn.tools.get(call_id)
            if tool is None:
                continue
            output = str(item.get("output") or "")
            meta_of = saved.tools.get(call_id)
            diffs = [d for d in (diff_from_json(raw) for raw in (meta_of or {}).get("diffs") or ()) if d is not None]
            if diffs:
                transcript.set_tool_diffs(turn, tool, diffs, source="result")
            transcript.finish_tool(turn, tool, output, status=_status_of(output, meta_of), at=turn.finished_at)
    status = TURN_STATUS.get(meta.status, TurnStatus.DONE)
    if status is TurnStatus.FAILED and meta.error:
        transcript.append_error(turn, meta.error)
    if meta.note:
        turn.blocks.append(NoticeBlock(id=transcript.new_id("b"), text=meta.note))
    duration = (meta.finished - meta.at) if meta.finished is not None else None
    transcript.finish_turn(turn, status, duration=duration)
    return turn


def build_turns(transcript: Transcript, registry: ToolRegistry, saved: SavedSession) -> list[Turn]:
    """Turns for ``Transcript.add_history_turns``, oldest first."""
    starts = [meta for meta in saved.turns if 0 <= meta.start < len(saved.items)]
    if not starts and saved.items:
        starts = [SavedTurn(0, saved.created_at, saved.modified_at)]
    turns: list[Turn] = []
    for number, meta in enumerate(starts):
        end = starts[number + 1].start if number + 1 < len(starts) else len(saved.items)
        turns.append(build_turn(transcript, registry, saved, number, meta, saved.items[meta.start : end]))
    return turns
