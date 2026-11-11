"""``todo_write``: the model's checklist for a task of several steps.

The model passes the whole list every time (a step starts, a step ends); the last call is the list. Nothing is stored apart from
the conversation itself, and the tool touches no file and runs nothing, so it is allowed at every permission level. The page
shows it as the TodoWrite row the other engines have (``toolinfo.parse_todos`` reads the same ``content`` / ``status`` items).
The idea is that of DeepSeek Harness's ``todo`` package and of Droid's and Claude Code's TodoWrite.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from desktop.core.harness.tools import ToolContext, ToolRegistry, ToolResult, ToolSpec

NAME = "todo_write"
STATES = ("pending", "in_progress", "completed")
MAX_ITEMS = 30
MAX_TEXT = 300

SPEC = ToolSpec(
    name=NAME,
    description=(
        "Keep a short checklist for a task that takes three or more steps, and keep it current as you work. Pass the whole list "
        "every time (it replaces the last one): call it first with every step as `pending`, then whenever a step starts "
        "(`in_progress`, one at a time) or ends (`completed`). Add steps you find on the way; do not delete the steps you did not "
        "do. Do not use it for a task of one or two steps or for a question. It changes nothing in the project."
    ),
    parameters={
        "type": "object",
        "properties": {
            "todos": {
                "type": "array",
                "minItems": 1,
                "items": {
                    "type": "object",
                    "properties": {
                        "content": {"type": "string", "description": "What the step is, in a few words."},
                        "status": {"type": "string", "enum": list(STATES)},
                    },
                    "required": ["content", "status"],
                },
                "description": "Every step of the task, in order, with its state.",
            }
        },
        "required": ["todos"],
    },
    ui_name="TodoWrite",
    kind="read",
)


def read_todos(value: Any) -> list[dict[str, str]]:
    """The list the model passed, cleaned. Raises ``ValueError`` with every problem, for the model to read."""
    problems: list[str] = []
    todos: list[dict[str, str]] = []
    items = value if isinstance(value, list) else []
    if len(items) > MAX_ITEMS:
        problems.append(f"at most {MAX_ITEMS} steps")
    for index, item in enumerate(items[:MAX_ITEMS], 1):
        if not isinstance(item, Mapping):
            problems.append(f"step {index} is not an object with `content` and `status`")
            continue
        content = item.get("content")
        status = item.get("status")
        if not isinstance(content, str) or not content.strip():
            problems.append(f"step {index} has no `content`")
        elif status not in STATES:
            problems.append(f"step {index} has the status {status!r}: use one of {', '.join(STATES)}")
        else:
            todos.append({"content": content.strip()[:MAX_TEXT], "status": status})
    if sum(1 for todo in todos if todo["status"] == "in_progress") > 1:
        problems.append("only one step may be in_progress at a time")
    if problems:
        raise ValueError("; ".join(problems) + ".")
    return todos


def todo_write(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    try:
        todos = read_todos(args.get("todos"))
    except ValueError as error:
        return ToolResult(f"The checklist was not accepted: {error}", is_error=True)
    done = sum(1 for todo in todos if todo["status"] == "completed")
    active = next((todo["content"] for todo in todos if todo["status"] == "in_progress"), "")
    text = f"Checklist updated: {done} of {len(todos)} done."
    if active:
        text += f" In progress: {active}."
    elif done < len(todos):
        text += " Mark the next step in_progress before you start it."
    return ToolResult(text, data={"todos": todos})


def ui_input(args: Mapping[str, Any]) -> dict[str, Any]:
    todos = args.get("todos") if isinstance(args, Mapping) else None
    return {"todos": [dict(todo) for todo in todos if isinstance(todo, Mapping)]} if isinstance(todos, list) else {}


def register(registry: ToolRegistry) -> None:
    registry.register(SPEC, todo_write, ui_input=ui_input)
