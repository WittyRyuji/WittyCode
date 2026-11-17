"""Tool contract for the main agent; workers never receive this tool."""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

NAME = "delegate_task"
DECLARATION: dict[str, Any] = {
    "name": NAME,
    "description": (
        "In Auto mode only, delegate one scoped coding task to a Fusion expert from the authorised model pool. You decide whether and "
        "how to delegate, the task's difficulty, model, scope and acceptance checks. Reuse the existing owner of a "
        "module. Include the relevant user goal, constraints and evidence; the expert cannot see your full history. "
        "The expert may edit files under the current permissions. Review its result and verify the workspace yourself. "
        "If the needed model is outside the pool or unavailable, ask the user to authorise a model in this session; "
        "do not silently fall back or transfer ownership."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "scope": {"type": "string", "description": "Concrete project-relative module directory or file."},
            "model": {"type": "string", "description": "Exact authorised model id."},
            "role": {"type": "string", "enum": ["worker", "light"]},
            "task": {"type": "string", "description": "Self-contained task, requirements and checks."},
        },
        "required": ["scope", "model", "role", "task"],
    },
}


def parse(args: Mapping[str, Any]) -> tuple[tuple[str, str, str, str] | None, str]:
    values = [args.get(key) for key in ("scope", "model", "role", "task")]
    if any(not isinstance(value, str) or not value.strip() for value in values):
        return None, "Provide non-empty scope, model, role and task strings."
    scope, model, role, task = (value.strip() for value in values)
    if role not in ("worker", "light") or len(scope) > 1024 or len(model) > 512 or len(task) > 50_000:
        return None, "The role or task fields are invalid or too long."
    return (scope, model, role, task), ""
