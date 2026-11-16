"""The model's explicit, complete plan, submitted for the user's decision."""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

NAME = "submit_plan"
MAX_PLAN_CHARS = 100_000
DECLARATION: dict[str, Any] = {
    "name": NAME,
    "description": (
        "In Plan mode only, present the complete plan to the user for approval. Call this only after research and clarification are "
        "complete. Include the outcome, concrete changes, order, checks and unresolved matters. The turn stops and "
        "the user chooses whether to execute or request revisions. Do not call it to ask a question."
    ),
    "parameters": {
        "type": "object",
        "properties": {"plan": {"type": "string", "description": "The complete plan in Markdown."}},
        "required": ["plan"],
    },
}


def parse(args: Mapping[str, Any]) -> tuple[str, str]:
    text = args.get("plan")
    if not isinstance(text, str) or not text.strip():
        return "", "Provide the complete plan as a non-empty `plan` string."
    text = text.strip()
    if len(text) > MAX_PLAN_CHARS:
        return "", "The plan is too long to present; reduce it without omitting necessary decisions."
    return text, ""
