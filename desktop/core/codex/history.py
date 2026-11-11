"""A saved Codex thread as transcript turns (``thread/turns/list`` with ``itemsView: "full"``).

Every turn is ``{"id", "items": [ThreadItem...], "status": "completed" | "interrupted" | "failed", "error",
"startedAt", "completedAt" (Unix seconds), "durationMs"}`` (verified for failed and interrupted turns, whose items were
only the ``userMessage``). The first ``userMessage`` is the turn's user block (a later one is something the user said to
the running turn, a steer, and is shown where it was said); the handoff text another engine's
conversation put in front of a first message is cut off again (``items.split_handoff``) and only marked on the block.
Plans (``turn/plan/updated``) are not items, so a reopened thread shows no TodoWrite steps.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from desktop.core.codex import items as codex_items
from desktop.core.transcript import Transcript, Turn, TurnStatus

TURN_STATUS = {"completed": TurnStatus.DONE, "interrupted": TurnStatus.INTERRUPTED, "failed": TurnStatus.FAILED}


def _seconds(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def apply_item(transcript: Transcript, turn: Turn, item: Mapping[str, Any]) -> None:
    """One finished item of a saved turn."""
    kind = item.get("type")
    item_id = str(item.get("id") or "")
    if not item_id or kind == "userMessage":
        return
    if kind == "agentMessage" or kind == "plan":
        transcript.end_text(turn, item_id, 0, str(item.get("text") or ""))
        return
    if kind in ("enteredReviewMode", "exitedReviewMode"):
        transcript.end_text(turn, item_id, 0, str(item.get("review") or ""))
        return
    if kind == "reasoning":
        transcript.end_thinking(turn, item_id, 0, text=codex_items.reasoning_text(item))
        return
    spec = codex_items.tool_spec(item)
    if spec is None:
        return
    name, data = spec
    tool = transcript.ensure_tool(turn, item_id, name)
    transcript.set_tool_input(turn, tool, data, final=True)
    if kind == "fileChange":
        transcript.set_tool_diffs(turn, tool, codex_items.file_diffs(codex_items.file_changes(item)))
    if item.get("status") == "inProgress":
        return  # cut off (the app-server stopped mid-turn): finish_turn marks it cancelled
    text, status = codex_items.tool_outcome(item)
    transcript.finish_tool(turn, tool, text, status=status, at=turn.finished_at)


def build_turns(transcript: Transcript, saved: Sequence[Any]) -> list[Turn]:
    """Turns for ``Transcript.add_history_turns``, oldest first (``saved`` must be oldest first too)."""
    turns: list[Turn] = []
    for raw in saved:
        if not isinstance(raw, Mapping):
            continue
        entries = [item for item in raw.get("items") or () if isinstance(item, Mapping)]
        user = next((item for item in entries if item.get("type") == "userMessage"), None)
        text, attachments, handoff = codex_items.user_message_parts(user)
        turn = transcript.new_history_turn(
            text if user is not None else None,
            message_id=str(user.get("id")) if user is not None and user.get("id") else None,
            started_at=_seconds(raw.get("startedAt")),
            attachments=attachments,
        )
        if handoff is not None and turn.user_block is not None:
            turn.user_block.handoff = handoff
        turn.finished_at = _seconds(raw.get("completedAt")) or turn.started_at
        for item in entries:
            if item.get("type") == "userMessage" and item is not user:
                # said to the turn while it ran (``turn/steer``): shown where it was said
                said, shown, _ = codex_items.user_message_parts(item)
                if said.strip() or shown:
                    transcript.add_steer(turn, said, shown, str(item.get("id") or "") or None, pending=False)
                continue
            apply_item(transcript, turn, item)
        status = TURN_STATUS.get(str(raw.get("status")), TurnStatus.DONE)
        if status is TurnStatus.FAILED:
            transcript.append_error(turn, codex_items.turn_error_text(raw.get("error")))
        duration = _seconds(raw.get("durationMs"))
        transcript.finish_turn(turn, status, duration=duration / 1000.0 if duration is not None else None)
        turns.append(turn)
    return turns

