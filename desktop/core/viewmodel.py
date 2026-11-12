"""The transcript as the React page wants it: plain JSON-able dicts (camelCase keys of ``frontend/src/types.ts``).

Pure: no Qt. The reducer (``core/transcript.py``) owns the state; this only reads it. Nothing here may contain a secret.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from typing import Any

from desktop.core.attachments import Attachment, Thumbnail, image_size
from desktop.core.diffs import FileDiff
from desktop.core.prompts import ActionView, QuestionView
from desktop.core.toolinfo import Category, clip, normalize_newlines, parse_todos, questionnaire_questions
from desktop.core.transcript import (
    ActivityBlock,
    ErrorBlock,
    NoticeBlock,
    PermissionPrompt,
    Prompt,
    PromptStatus,
    QABlock,
    QuestionPrompt,
    SwitchBlock,
    TextBlockView,
    ThinkingEntry,
    ToolItem,
    Turn,
    TurnStatus,
    UserBlock,
)

Thumbnailer = Callable[[Attachment], Thumbnail | None]

OUTPUT_LIMIT = 60_000
"""Characters of one tool's output sent to the page; the rest is cut (the end for commands, the start for the rest)."""
META_VALUE_LIMIT = 400


def attachment_json(item: Attachment, thumbnail: Thumbnailer | None = None) -> dict[str, Any]:
    data: dict[str, Any] = {"id": item.id, "kind": item.kind, "name": item.name, "size": item.size or 0}
    if item.note:
        data["note"] = item.note
    if item.kind == "image":
        size = image_size(item.data)  # the full picture's pixels; the thumbnail below is much smaller
        if size is not None:
            data["width"], data["height"] = size
        if thumbnail is not None:
            thumb = thumbnail(item)
            if thumb is not None:
                data["url"] = thumb.uri
    return data


def diff_json(diff: FileDiff) -> dict[str, Any]:
    lines = [_line_json(line) for line in diff.lines]
    if diff.truncated:
        # the page renders gap rows but reads no flag: the count of the cut lines goes in a row of its own
        lines.append({"t": "gap", "text": f"（已省略 {diff.truncated:,} 行）"})
    return {
        "path": diff.path,
        "kind": "new" if diff.is_new_file else "edit",
        "added": diff.added,
        "removed": diff.removed,
        "lines": lines,
    }


def _line_json(line: Any) -> dict[str, Any]:
    data: dict[str, Any] = {"t": line.kind, "text": line.text}
    if line.old is not None:
        data["old"] = line.old
    if line.new is not None:
        data["new"] = line.new
    return data


def _clip_output(text: str, *, tail: bool) -> str:
    text = normalize_newlines(text)
    if len(text) <= OUTPUT_LIMIT:
        return text
    cut = len(text) - OUTPUT_LIMIT
    note = f"（已省略 {cut:,} 个字符）"
    return f"{note}\n{text[-OUTPUT_LIMIT:]}" if tail else f"{text[:OUTPUT_LIMIT]}\n{note}"


def _meta(item: ToolItem) -> dict[str, str] | None:
    rows: dict[str, str] = {}
    for key, value in item.input.items():
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        rows[str(key)] = clip(normalize_newlines(text), META_VALUE_LIMIT)
    return rows or None


def tool_json(item: ToolItem) -> dict[str, Any]:
    out: dict[str, Any] = {
        "kind": "tool",
        "id": item.id,
        "name": item.name,
        "category": item.category.value,
        "description": item.description,
        "status": item.status.value,
    }
    command = item.input.get("command")
    if item.name == "TodoWrite":
        todos = parse_todos(item.input.get("todos"))
        if todos or item.input.get("todos") == []:
            out["todos"] = [{"text": todo.text, "state": todo.status} for todo in todos]
        else:
            out["meta"] = _meta(item)
    elif item.category is Category.COMMAND and isinstance(command, str) and command.strip():
        out["command"] = command
    elif item.category is Category.ASK:
        questionnaire = item.input.get("questionnaire")
        questions = questionnaire_questions(questionnaire) if isinstance(questionnaire, str) else []
        out["meta"] = {f"问题 {number}": question for number, question in enumerate(questions, 1)} or _meta(item)
    elif not item.diffs:
        out["meta"] = _meta(item)
    if item.diffs:
        out["diffs"] = [diff_json(diff) for diff in item.diffs]
    text = item.result if item.result is not None else item.progress
    if text and text.strip():
        out["output"] = _clip_output(text, tail=item.category is Category.COMMAND)
    elif item.result is not None and item.category is not Category.EDIT:
        out["output"] = "（没有输出）"
    if item.is_error:
        out["outputError"] = True
    if item.decision:
        out["decision"] = item.decision
    if item.started_at is not None and item.finished_at is not None:
        out["seconds"] = round(max(0.0, item.finished_at - item.started_at), 1)
    return {key: value for key, value in out.items() if value is not None}


def thinking_json(entry: ThinkingEntry) -> dict[str, Any]:
    out: dict[str, Any] = {
        "kind": "thinking",
        "id": entry.id,
        "text": "（内容已隐藏）" if entry.redacted and not entry.text else entry.text,
        "complete": entry.complete,
    }
    if entry.duration is not None:
        out["seconds"] = round(entry.duration, 1)
    return out


def block_json(block: Any, thumbnail: Thumbnailer | None = None) -> dict[str, Any]:
    if isinstance(block, UserBlock):
        out: dict[str, Any] = {"kind": "user", "id": block.id, "text": block.text}
        if block.who:
            out["who"] = block.who
        if block.steer:
            out["steer"] = True
            if block.pending:
                out["pending"] = True
        if block.attachments:
            out["attachments"] = [attachment_json(item, thumbnail) for item in block.attachments]
        if block.handoff is not None:
            out["handoff"] = {"carried": block.handoff[0], "truncated": block.handoff[1]}
        return out
    if isinstance(block, TextBlockView):
        return {"kind": "text", "id": block.id, "markdown": block.text, "streaming": block.streaming}
    if isinstance(block, ActivityBlock):
        entries = [thinking_json(e) if isinstance(e, ThinkingEntry) else tool_json(e) for e in block.entries]
        return {"kind": "activity", "id": block.id, "entries": entries}
    if isinstance(block, QABlock):
        return {"kind": "qa", "id": block.id, "pairs": [list(pair) for pair in block.pairs], "cancelled": block.cancelled}
    if isinstance(block, ErrorBlock):
        return {
            "kind": "error",
            "id": block.id,
            "message": block.message,
            "recoverable": block.recoverable,
            "resolved": block.resolved,
        }
    if isinstance(block, NoticeBlock):
        return {"kind": "notice", "id": block.id, "text": block.text}
    if isinstance(block, SwitchBlock):
        return {
            "kind": "switch",
            "id": block.id,
            "from": {"model": block.from_model, "engine": block.from_engine},
            "to": {"model": block.to_model, "engine": block.to_engine},
            "carried": block.carried,
            "total": block.total,
            "bytes": block.size,
            "truncated": block.truncated,
        }
    raise TypeError(f"unknown block {type(block).__name__}")


def _action_json(action: ActionView) -> dict[str, Any]:
    out: dict[str, Any] = {"title": action.title, "toolName": action.tool_name}
    if action.path:
        out["path"] = action.path
    if action.command:
        out["command"] = action.command
    if action.impact:
        out["impact"] = str(action.impact).lower()
    if action.reason:
        out["reason"] = action.reason
    if action.diffs:
        out["diffs"] = [diff_json(diff) for diff in action.diffs]
    if action.markdown:
        out["markdown"] = action.markdown
    return out


def _question_json(view: QuestionView) -> dict[str, Any]:
    return {"topic": view.topic, "question": view.question, "options": list(view.options), "multiSelect": view.multi_select}


def prompt_json(prompt: Prompt) -> dict[str, Any]:
    status = "pending" if prompt.status is PromptStatus.PENDING else "resolved"
    if isinstance(prompt, PermissionPrompt):
        out: dict[str, Any] = {
            "kind": "permission",
            "id": prompt.id,
            "status": status,
            "actions": [_action_json(action) for action in prompt.actions],
            "options": [{"value": o.value, "label": o.label, "tone": o.tone} for o in prompt.options],
        }
        if prompt.selected:
            out["selected"] = prompt.selected
        return out
    assert isinstance(prompt, QuestionPrompt)
    out = {"kind": "question", "id": prompt.id, "status": status, "questions": [_question_json(q) for q in prompt.questions]}
    if prompt.who:
        out["who"] = prompt.who
    return out


_TURN_STATUS = {
    TurnStatus.RUNNING: "running",
    TurnStatus.DONE: "done",
    TurnStatus.INTERRUPTED: "interrupted",
    TurnStatus.FAILED: "failed",
}


def turn_json(turn: Turn, thumbnail: Thumbnailer | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {
        "id": turn.id,
        "status": _TURN_STATUS[turn.status],
        "blocks": [block_json(block, thumbnail) for block in turn.blocks],
        "prompts": [prompt_json(prompt) for prompt in turn.prompts if prompt.status is PromptStatus.PENDING],
    }
    if turn.duration is not None:
        out["seconds"] = round(turn.duration, 1)
    steps = turn.stats().steps
    if steps:
        out["steps"] = steps
    return out


def usage_json(usage: Any) -> dict[str, Any] | None:
    """Cumulative token use. ``cacheWrite`` is ``None`` where the engine has no such thing (Codex)."""
    if usage is None:
        return None

    def count(name: str) -> int | None:
        value = getattr(usage, name, None)
        return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None

    return {
        "input": count("input_tokens") or 0,
        "output": count("output_tokens") or 0,
        "cacheWrite": count("cache_creation_tokens"),
        "cacheRead": count("cache_read_tokens") or 0,
    }


def context_json(context: Any, *, window_hint: int | None = None) -> dict[str, Any]:
    """``window`` 0 means unknown (a session that has not been opened yet)."""
    return {
        "used": getattr(context, "used", None),
        "window": getattr(context, "window", None) or window_hint or 0,
        "model": getattr(context, "model", "") or "",
        "compacted": bool(getattr(context, "compacted", False)),
    }


def turns_json(turns: list[Turn], thumbnail: Thumbnailer | None = None) -> list[dict[str, Any]]:
    return [turn_json(turn, thumbnail) for turn in turns]


def plain_mapping(value: Mapping[str, Any]) -> dict[str, Any]:
    return {str(key): value[key] for key in value}
