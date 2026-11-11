"""The page's Devin permission levels, and the approval buttons, in ACP terms.

A Devin session has a *mode* (``session/set_mode``; also the ``mode`` config option) and that is its permission level:

========  ===============  ==========================================================================
level     ACP mode         what Devin says it is
========  ===============  ==========================================================================
ask       ``ask``          Answer questions without code changes
plan      ``plan``         Plan changes before implementing
code      ``accept-edits`` Write and edit code (the CLI's default; it asks before running commands)
smart     ``smart``        Auto-approve actions the model judges safe
full      ``bypass``       Auto-approve all tool calls (unless the organization's policy requires approval)
========  ===============  ==========================================================================

Approvals arrive as ``session/request_permission`` with the options Devin offers for that tool call (``allow_once``,
``allow_session``, ``allow_always``, ``switch_bypass``, ``reject_once``; the names are English and say what the choice
covers, e.g. "Yes, allow edits in D:\\x (this session)"). The page shows every option Devin gave, with Chinese wording for the
ids known, and answers with the id chosen; a prompt that expires unanswered (the turn was stopped, the session closed,
the process gone) is answered ``cancelled``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from desktop.core.prompts import OptionView

MODE_OF_LEVEL: dict[str, str] = {
    "ask": "ask",
    "plan": "plan",
    "code": "accept-edits",
    "smart": "smart",
    "full": "bypass",
}
LEVEL_OF_MODE: dict[str, str] = {mode: level for level, mode in MODE_OF_LEVEL.items()}
DEFAULT_LEVEL = "code"

OPTION_LABELS: dict[str, str] = {
    "allow_once": "允许（仅此一次）",
    "allow_session": "允许，本会话内同类操作不再询问",
    "allow_always": "允许，以后一律允许同类操作",
    "allow_always_global": "允许，所有项目里以后一律允许同类操作",
    "switch_bypass": "允许，并切换到完全访问",
    "reject_once": "拒绝",
    "reject_always": "拒绝，以后一律拒绝",
}
CANCELLED = {"outcome": {"outcome": "cancelled"}}


def normalize_level(value: Any) -> str | None:
    """A permission level from the page's or Devin's spelling; ``None`` for anything else."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if text in MODE_OF_LEVEL:
        return text
    return LEVEL_OF_MODE.get(text)


def mode_of(level: Any) -> str:
    return MODE_OF_LEVEL[normalize_level(level) or DEFAULT_LEVEL]


def level_of(mode: Any) -> str | None:
    return LEVEL_OF_MODE.get(mode) if isinstance(mode, str) else None


def _rejects(option: Mapping[str, Any]) -> bool:
    return str(option.get("kind") or "").startswith("reject") or str(option.get("optionId") or "").startswith("reject")


def option_views(options: Any) -> tuple[OptionView, ...]:
    """The buttons for a permission request's ``options``; every option Devin gave is there. The first one that says no
    is the page's ``cancel`` (the value the page and the transcript treat as a refusal); the others keep their ids."""
    views = []
    refused = False
    for option in options or ():
        if not isinstance(option, Mapping) or not isinstance(option.get("optionId"), str):
            continue
        value = option["optionId"]
        name = str(option.get("name") or value)
        if _rejects(option):
            if not refused:
                refused = True
                views.append(OptionView("cancel", OPTION_LABELS["reject_once"], name, "danger"))
                continue
            views.append(OptionView(value, OPTION_LABELS.get(value, name), name, "danger"))
            continue
        views.append(OptionView(value, _label(value, name), name, "primary" if value == "allow_once" else "normal"))
    return tuple(views)


SCOPED = ("allow_session", "allow_always", "allow_always_global")


def _label(option_id: str, name: str) -> str:
    """Devin's own words say what the choice covers (which commands, which folder): they follow the Chinese ones."""
    label = OPTION_LABELS.get(option_id, name)
    if option_id in SCOPED and name.strip():
        detail = name.strip()
        if detail.lower().startswith("yes, "):
            detail = detail[5:]
        return f"{label}：{detail}"
    return label


def selected(option_id: str) -> dict[str, Any]:
    return {"outcome": {"outcome": "selected", "optionId": option_id}}


def reject_option(options: Sequence[Any]) -> str | None:
    """The id that says no (the first option that is a rejection)."""
    for option in options or ():
        if isinstance(option, Mapping) and _rejects(option) and isinstance(option.get("optionId"), str):
            return option["optionId"]
    return None


def outcome_for(value: str | None, options: Sequence[Any]) -> dict[str, Any]:
    """The answer to a permission request for what the page chose; no choice (``None``: the prompt expired) is a
    cancellation, and so is a refusal when Devin offered no option to refuse with."""
    if value is None:
        return CANCELLED
    if value == "cancel":
        rejected = reject_option(options)
        return selected(rejected) if rejected else CANCELLED
    return selected(value)
