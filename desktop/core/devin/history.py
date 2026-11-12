"""A saved Devin session as transcript turns.

``session/load`` has no history call of its own: it *replays* the session as notifications (``user_message_chunk``,
``agent_thought_chunk``, ``tool_call``, ``tool_call_update``, ``agent_message_chunk``, ``session_info_update``,
``usage_update`` and ``_cognition.ai/turn_stats``) and then answers. ``Replay`` takes them in the order they came and builds
the turns the way the live controller does.

Where a turn ends: nowhere that is marked. ``_cognition.ai/turn_stats`` (one per prompt) does not close a turn: **they all
come at the very end of the replay** (verified: three prompts, two of them one merged turn, content first, then three
``turn_stats``), so they are only read for what each prompt cost. Every user message starts a turn, except one that comes
straight after another user message with no answer between: Devin merges a prompt sent to a running turn into that turn
and, when it was sent before the turn's first reply, lists it right after the turn's own prompt (a steer, shown as such).
A prompt sent to a turn that had already answered something is listed after that answer, and comes back as a turn of its
own (the turn it continued is cut at that point). The replay carries no times except the ``cognition.ai/timestamp`` of
each update.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from desktop.core.engine_items import command_result
from desktop.core.devin import items
from desktop.core.toolinfo import plan_steps
from desktop.core.transcript import ToolStatus, Transcript, Turn, TurnStatus
from desktop.core.usage import Tokens


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _content_text(update: Mapping[str, Any]) -> str:
    content = update.get("content")
    return _text(content.get("text")) if isinstance(content, Mapping) else ""


class Replay:
    def __init__(self, transcript: Transcript) -> None:
        self.transcript = transcript
        self.usage: dict[str, Tokens] = {}
        """What each prompt cost (``turn_stats``), by the id of the user message: the same ids the live session reports."""
        self.title = ""
        self.context_used: int | None = None
        self.context_window: int | None = None
        self._turns: list[Turn] = []
        self._turn: Turn | None = None
        self._answered = False
        """The turn has had something from the agent since its user message: a message after this starts a new turn."""
        self._pending: dict[str, Any] | None = None
        self._first: float | None = None
        self._last: float | None = None
        self._block = ""
        self._blocks = 0
        self._outputs: dict[str, str] = {}
        self._exits: dict[str, int] = {}

    # ------------------------------------------------------------------ input

    def feed(self, method: str, params: Any) -> None:
        if not isinstance(params, Mapping):
            return
        if method == "_cognition.ai/turn_stats":
            self._flush_user()
            self._stats(params)
            return
        if method != "session/update" or not isinstance(params.get("update"), Mapping):
            return
        update = params["update"]
        kind = update.get("sessionUpdate")
        if kind == "user_message_chunk":
            self._user_chunk(update)
            return
        self._flush_user()
        stamp = items.parse_time(update.get("_meta", {}).get(items.TIMESTAMP_META) if isinstance(update.get("_meta"), Mapping) else None)
        if stamp is not None and self._turn is not None:
            self._last = stamp
        if kind == "agent_message_chunk" and _content_text(update):
            self._words("text", update)
        elif kind == "agent_thought_chunk" and _content_text(update):
            self._words("thinking", update)
        elif kind == "tool_call":
            self._tool_call(update)
        elif kind == "tool_call_update":
            self._tool_update(update)
        elif kind == "plan" and plan_steps(update.get("entries")) is not None:
            self.transcript.set_plan(self._ensure_turn(), update.get("entries"))
        elif kind == "session_info_update":
            title = _text(update.get("title")).strip()
            if title:
                self.title = title
        elif kind == "usage_update" and not (isinstance(update.get("_meta"), Mapping) and items.SUBAGENT_META in update["_meta"]):
            _, used, size = items.usage_call(update)
            self.context_used = used or self.context_used
            self.context_window = size or self.context_window

    def finish(self) -> list[Turn]:
        """The turns, oldest first (a turn the replay cut off is closed as it is)."""
        self._flush_user()
        self._close()
        turns, self._turns = self._turns, []
        return turns

    # ------------------------------------------------------------------ pieces

    def _user_chunk(self, update: Mapping[str, Any]) -> None:
        meta = update.get("_meta") if isinstance(update.get("_meta"), Mapping) else {}
        message_id = _text(meta.get(items.CLIENT_MESSAGE_META))
        content = update.get("content")
        pending = self._pending
        if pending is not None and pending["id"] == message_id and message_id:
            pending["parts"].append(content)
            return
        self._flush_user()
        self._pending = {"id": message_id, "parts": [content], "time": items.parse_time(meta.get(items.TIMESTAMP_META))}

    def _flush_user(self) -> None:
        pending, self._pending = self._pending, None
        if pending is None:
            return
        text, attachments, handoff = items.user_text_and_attachments(pending["parts"])
        if self._turn is None or self._answered:
            self._close()
            turn = self.transcript.new_history_turn(text, message_id=pending["id"] or None, started_at=pending["time"], attachments=attachments)
            if handoff is not None and turn.user_block is not None:
                turn.user_block.handoff = handoff
            self._turn, self._answered = turn, False
            self._first = self._last = pending["time"]
        elif text.strip() or attachments:
            self.transcript.add_steer(self._turn, text, attachments, pending["id"] or None, pending=False)
        self._block = ""

    def _ensure_turn(self) -> Turn:
        if self._turn is None:
            self._turn, self._answered = self.transcript.new_history_turn(None), False
        self._answered = True
        return self._turn

    def _words(self, kind: str, update: Mapping[str, Any]) -> None:
        turn = self._ensure_turn()
        if self._block != kind:
            self._blocks += 1
            self._block = kind
        key = f"replay-{kind}-{self._blocks}"
        if kind == "text":
            self.transcript.append_text(turn, key, 0, _content_text(update))
        else:
            self.transcript.append_thinking(turn, key, 0, _content_text(update))

    def _tool_call(self, update: Mapping[str, Any]) -> None:
        turn, call_id = self._ensure_turn(), _text(update.get("toolCallId"))
        if not call_id:
            return
        self._block = ""
        name, data = items.tool_spec(update)
        tool = self.transcript.ensure_tool(turn, call_id, name)
        self.transcript.set_tool_input(turn, tool, data, final=True)
        diffs = items.call_diffs(update.get("content"))
        if diffs:
            self.transcript.set_tool_diffs(turn, tool, diffs)
        status = items.call_status(update.get("status"))
        if tool.name == "TodoWrite" and status is not None:
            self.transcript.finish_tool(turn, tool, items.content_text(update.get("content")), status=status, at=self._last)

    def _tool_update(self, update: Mapping[str, Any]) -> None:
        turn, call_id = self._turn, _text(update.get("toolCallId"))
        tool = turn.tools.get(call_id) if turn is not None else None
        if turn is None or tool is None:
            return
        if tool.name == "TodoWrite" and isinstance(update.get("rawInput"), Mapping):
            self.transcript.set_tool_input(
                turn, tool, {**tool.input, **update["rawInput"]}, final=True, accept_plan=items.call_status(update.get("status")) is not ToolStatus.ERROR
            )
        text = items.content_text([c for c in update.get("content") or () if not (isinstance(c, Mapping) and c.get("type") == "diff")])
        if text:
            self._outputs[call_id] = text
        diffs = items.call_diffs(update.get("content"))
        if diffs:
            self.transcript.set_tool_diffs(turn, tool, diffs)
        meta = update.get("_meta") if isinstance(update.get("_meta"), Mapping) else {}
        exit_info = meta.get("terminal_exit")
        code = exit_info.get("exit_code") if isinstance(exit_info, Mapping) else None
        if isinstance(code, int) and not isinstance(code, bool):
            self._exits[call_id] = code
        status = items.call_status(update.get("status"))
        if status is not None:
            result = self._outputs.pop(call_id, "")
            if tool.name == items.EXECUTE and call_id in self._exits:
                result = command_result({"exitCode": self._exits.pop(call_id)}, result)
            self.transcript.finish_tool(turn, tool, result, status=status, at=self._last)

    def _stats(self, params: Mapping[str, Any]) -> None:
        message_id = _text(params.get("turnClientMessageId"))
        tokens = items.stats_tokens(params)
        if message_id and tokens is not None:
            self.usage[message_id] = tokens

    def _close(self) -> None:
        turn, self._turn = self._turn, None
        if turn is None:
            return
        turn.finished_at = self._last or turn.started_at
        duration = self._last - self._first if self._last is not None and self._first is not None and self._last >= self._first else None
        self.transcript.finish_turn(turn, TurnStatus.DONE, duration=duration)
        self._turns.append(turn)
        self._answered = False
        self._block = ""
        self._first = self._last = None


__all__ = ["Replay"]
