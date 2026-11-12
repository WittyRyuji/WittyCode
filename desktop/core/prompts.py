"""Display models for permission and AskUser requests: Chinese labels, commands, diffs."""

from __future__ import annotations

from dataclasses import dataclass

from desktop.core.diffs import FileDiff


OPTION_LABELS: dict[str, str] = {
    "proceed_once": "允许（仅此一次）",
    "proceed_always": "允许，并始终允许低风险操作",
    "proceed_always_file": "允许，并始终允许此文件",
    "proceed_always_tools": "允许，并始终允许这些工具",
    "proceed_always_server": "允许，并始终允许此 MCP 服务器",
    "proceed_auto_run": "批准并自动执行",
    "proceed_auto_run_low": "批准，自主度：低",
    "proceed_auto_run_medium": "批准，自主度：中",
    "proceed_auto_run_high": "批准，自主度：高",
    "proceed_new_session": "批准，在新会话中执行",
    "proceed_new_session_low": "新会话执行，自主度：低",
    "proceed_new_session_medium": "新会话执行，自主度：中",
    "proceed_new_session_high": "新会话执行，自主度：高",
    "proceed_edit": "修改后执行",
    "cancel": "拒绝",
}
# PROCEED_EDIT requires edited spec content, which this client cannot provide yet.
UNSUPPORTED_OPTIONS = frozenset({"proceed_edit"})


@dataclass(frozen=True, slots=True)
class OptionView:
    value: str
    label: str
    original: str
    tone: str  # "primary" | "danger" | "normal"


APPROVAL_OPTIONS: tuple[OptionView, ...] = (
    OptionView("proceed_once", "允许（仅此一次）", "accept", "primary"),
    OptionView("proceed_always", "允许，本会话内同类操作不再询问", "acceptForSession", "normal"),
    OptionView("cancel", "拒绝", "decline", "danger"),
)
"""The approval buttons of the engines that answer once / for the session / no (the own loop's)."""


@dataclass(frozen=True, slots=True)
class ActionView:
    kind: str
    tool_use_id: str
    tool_name: str
    title: str
    path: str | None = None
    command: str | None = None
    impact: str | None = None
    reason: str | None = None
    diffs: tuple[FileDiff, ...] = ()
    markdown: str | None = None


@dataclass(frozen=True, slots=True)
class QuestionView:
    index: int
    topic: str
    question: str
    options: tuple[str, ...]
    multi_select: bool
