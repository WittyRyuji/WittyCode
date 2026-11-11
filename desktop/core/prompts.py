"""Display models for permission and AskUser requests: Chinese labels, commands, diffs."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import PureWindowsPath

from droid_sdk import (
    ApplyPatchAction,
    AskUserAction,
    CreateFile,
    DroidShieldViolationAction,
    EditAction,
    ExecuteAction,
    ExitSpecModeAction,
    McpToolAction,
    PermissionOption,
    Question,
    SandboxViolationAction,
)

from desktop.core.diffs import (
    FileDiff,
    diff_for_new_file,
    diff_from_contents,
    diff_from_fragments,
    diffs_from_patch,
)

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


def _value(option: PermissionOption) -> str:
    return getattr(option.value, "value", str(option.value))


def option_views(options: Sequence[PermissionOption]) -> tuple[OptionView, ...]:
    views = []
    for option in options:
        value = _value(option)
        if value in UNSUPPORTED_OPTIONS:
            continue
        tone = "danger" if value == "cancel" else "primary" if value == "proceed_once" else "normal"
        views.append(OptionView(value, OPTION_LABELS.get(value, option.label), option.label, tone))
    return tuple(views)


def _name(path: str) -> str:
    return PureWindowsPath(path).name or path


def action_view(action: object) -> ActionView:
    tool_use = getattr(action, "tool_use", None)
    base = {
        "tool_use_id": getattr(tool_use, "id", ""),
        "tool_name": getattr(tool_use, "name", ""),
    }
    if isinstance(action, ExecuteAction):
        return ActionView(
            kind="execute",
            title="运行命令",
            command=action.full_command or action.command,
            impact=action.impact_level,
            reason=action.risk_level_reason,
            **base,
        )
    if isinstance(action, EditAction):
        if action.old_content is not None or action.new_content is not None:
            diff = diff_from_contents(action.file_path, action.old_content or "", action.new_content or "")
        else:
            data = tool_use.input if tool_use is not None else {}
            diff = diff_from_fragments(action.file_path, data.get("old_str"), data.get("new_str"))
        return ActionView(kind="edit", title=f"编辑文件 {_name(action.file_path)}", path=action.file_path, diffs=(diff,), **base)
    if isinstance(action, CreateFile):
        return ActionView(
            kind="create",
            title=f"创建文件 {_name(action.file_path)}",
            path=action.file_path,
            diffs=(diff_for_new_file(action.file_path, action.content),),
            **base,
        )
    if isinstance(action, ApplyPatchAction):
        diffs: list[FileDiff] = []
        for item in action.files or ():
            if item.operation == "create":
                diffs.append(diff_for_new_file(item.file_path, item.new_content))
            else:
                diffs.append(diff_from_contents(item.move_to or item.file_path, item.old_content or "", item.new_content or ""))
        if not diffs and action.patch_content:
            diffs = diffs_from_patch(action.patch_content)
        count = len(diffs) or 1
        return ActionView(
            kind="apply_patch",
            title=f"应用补丁（{count} 个文件）",
            path=action.file_path,
            diffs=tuple(diffs),
            **base,
        )
    if isinstance(action, ExitSpecModeAction):
        return ActionView(kind="spec", title=action.title or "方案已就绪，是否开始执行？", markdown=action.plan, **base)
    if isinstance(action, AskUserAction):
        return ActionView(kind="ask_user", title="回答问题", markdown=action.questionnaire, **base)
    if isinstance(action, McpToolAction):
        server = f"（{action.server_name}）" if action.server_name else ""
        return ActionView(
            kind="mcp",
            title=f"调用 MCP 工具 {action.actual_tool_name or action.tool_name}{server}",
            impact=action.impact_level,
            **base,
        )
    if isinstance(action, SandboxViolationAction):
        return ActionView(
            kind="sandbox",
            title=f"沙箱拦截：{action.violating_tool_name}",
            path=action.target,
            reason=action.reason,
            **base,
        )
    if isinstance(action, DroidShieldViolationAction):
        return ActionView(kind="shield", title="安全检查拦截了一条命令", command=action.command, reason=action.reason, **base)
    return ActionView(kind="other", title=f"使用工具 {base['tool_name'] or '?'}", **base)


def question_views(questions: Sequence[Question]) -> tuple[QuestionView, ...]:
    return tuple(
        QuestionView(
            index=question.index,
            topic=question.topic,
            question=question.question,
            options=tuple(question.options),
            multi_select=bool(question.multi_select),
        )
        for question in questions
    )