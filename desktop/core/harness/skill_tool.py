"""``skill``: the model loads one of the user's skills (``harness.skills``) when a request matches its description.

The instructions of the loop list the skills by name and description; this tool answers with the whole ``SKILL.md`` and the
files beside it. It reads a few files of the skill's own folder and changes nothing, so it is allowed at every permission level
and in Plan mode. The page shows it as the Skill row the other engines have (``toolinfo`` reads ``skill``).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from desktop.core.harness import skills
from desktop.core.harness.tools import ToolContext, ToolRegistry, ToolResult, ToolSpec

NAME = "skill"

SPEC = ToolSpec(
    name=NAME,
    description=(
        "Load one of the user's skills: a reusable workflow kept in a file. Call it when the request matches the description of a "
        "skill listed in your instructions, then follow the instructions it returns. It changes nothing."
    ),
    parameters={
        "type": "object",
        "properties": {"skill": {"type": "string", "description": "The name of the skill, as listed in your instructions."}},
        "required": ["skill"],
    },
    ui_name="Skill",
    kind="read",
)


def run(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    wanted = skills.slug(str(args.get("skill") or ""))
    available = [skill for skill in skills.discover(ctx.project) if skill.model_invocable]
    skill = next((item for item in available if item.name == wanted), None)
    if skill is None:
        names = ", ".join(item.name for item in available) or "none"
        return ToolResult(f"There is no skill named {args.get('skill')!r}. Available skills: {names}.", is_error=True)
    return ToolResult(skills.load_text(skill), data={"skill": skill.name})


def ui_input(args: Mapping[str, Any]) -> dict[str, Any]:
    name = args.get("skill") if isinstance(args, Mapping) else None
    return {"skill": name} if isinstance(name, str) else {}


def register(registry: ToolRegistry) -> None:
    registry.register(SPEC, run, ui_input=ui_input)
