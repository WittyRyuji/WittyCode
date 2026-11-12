"""Tool categories, one-line input descriptions and the Chinese summary formats.

Everything here is pure string formatting so it can be unit-tested and shared
by the live reducer, the history loader and the HTML renderer.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import Enum
from pathlib import PureWindowsPath
from typing import Any


class Category(str, Enum):
    READ = "read"
    EDIT = "edit"
    COMMAND = "command"
    PLAN = "plan"
    ASK = "ask"
    MEMORY = "memory"
    OTHER = "other"


CATEGORY_LABELS: dict[Category, str] = {
    Category.READ: "读取",
    Category.EDIT: "编辑",
    Category.COMMAND: "命令",
    Category.PLAN: "计划",
    Category.ASK: "提问",
    Category.MEMORY: "记忆",
    Category.OTHER: "其他",
}
CATEGORY_ORDER: tuple[Category, ...] = tuple(Category)

_TOOL_CATEGORIES: dict[str, Category] = {
    **dict.fromkeys(
        (
            "Read",
            "LS",
            "Glob",
            "Grep",
            "FetchUrl",
            "WebSearch",
            "ReadAutomation",
            "ListAutomations",
            "ListAutomationTemplates",
            "View",
            "ReadFile",
            "ListDirectory",
        ),
        Category.READ,
    ),
    **dict.fromkeys(
        ("Edit", "Create", "ApplyPatch", "MultiEdit", "Write", "WriteFile", "NotebookEdit"),
        Category.EDIT,
    ),
    **dict.fromkeys(("Execute", "Bash", "Shell", "PowerShell"), Category.COMMAND),
    "TodoWrite": Category.PLAN,
    "AskUser": Category.ASK,
}
_TOOL_CATEGORIES_LOWER = {name.lower(): category for name, category in _TOOL_CATEGORIES.items()}


MEMORY_SERVER = "wittycode-memory"
MEMORY_TOOLS = ("memory_search", "memory_read", "memory_write", "memory_update", "memory_forget")


def memory_tool(name: str) -> str | None:
    """Which tool of the shared memory a call is (``memory_write`` …). The engines name an MCP tool each their own way:
    ``wittycode-memory___memory_write``, ``wittycode-memory/memory_write``, ``mcp__wittycode-memory__memory_write`` (Devin)."""
    if MEMORY_SERVER not in name:
        return None
    return next((tool for tool in MEMORY_TOOLS if name.endswith(tool)), None)


def tool_category(name: str) -> Category:
    if memory_tool(name):
        return Category.MEMORY
    return _TOOL_CATEGORIES.get(name) or _TOOL_CATEGORIES_LOWER.get(name.lower(), Category.OTHER)


def count_categories(categories: Iterable[Category]) -> dict[Category, int]:
    return dict(Counter(categories))


def counts_text(counts: Mapping[Category, int]) -> str:
    """``读取 3 · 编辑 2 · 命令 3`` in fixed category order, zero counts omitted."""
    return " · ".join(
        f"{CATEGORY_LABELS[category]} {counts[category]}"
        for category in CATEGORY_ORDER
        if counts.get(category)
    )


def steps_text(steps: int, counts: Mapping[Category, int]) -> str:
    """``8 步 · 读取 3 · 编辑 2 · 命令 3``."""
    detail = counts_text(counts)
    return f"{steps} 步 · {detail}" if detail else f"{steps} 步"


def format_duration(seconds: float | None) -> str:
    """``5.4 秒`` / ``25 秒`` / ``2 分 05 秒`` / ``1 小时 02 分``."""
    if seconds is None or not math.isfinite(seconds) or seconds < 0:
        return "—"
    if seconds < 9.95:
        return f"{seconds:.1f} 秒"
    total = int(round(seconds))
    if total < 60:
        return f"{total} 秒"
    if total < 3600:
        minutes, secs = divmod(total, 60)
        return f"{minutes} 分 {secs:02d} 秒"
    hours, rest = divmod(total, 3600)
    return f"{hours} 小时 {rest // 60:02d} 分"


def turn_summary_text(seconds: float | None, steps: int) -> str:
    """``用时 2 分 05 秒 · 12 步`` (the step part is left out when there were no tool calls)."""
    text = f"用时 {format_duration(seconds)}"
    return f"{text} · {steps} 步" if steps else text


def _scaled(number: float, limit: float, suffix: str) -> tuple[str, float]:
    scaled = number / limit
    text = f"{scaled:.1f}" if abs(scaled) < 100 else f"{scaled:.0f}"
    return text.removesuffix(".0") + suffix, abs(float(text))


def format_count(value: float | None) -> str:
    """Compact token count: ``932``, ``17.6k``, ``91k``, ``1.2M``."""
    if value is None or not math.isfinite(value):
        return "—"
    number = float(value)
    if abs(number) < 1e3:
        return str(int(round(number)))
    if abs(number) < 1e6:
        text, magnitude = _scaled(number, 1e3, "k")
        if magnitude < 1000:
            return text
    return _scaled(number, 1e6, "M")[0]  # also catches 999.96k, which rounds up to 1000k


def format_credits(value: float | None) -> str:
    """Whole credits, except that small amounts keep one decimal (a cheap turn costs a few credits)."""
    if value is None or not math.isfinite(value):
        return "—"
    if abs(value) < 100 and value != int(value):
        return f"{value:.1f}".rstrip("0").rstrip(".")
    return format_count(value)


WORKING_STATE_LABELS: dict[str, str] = {
    "idle": "空闲",
    "thinking": "正在思考…",
    "streaming_assistant_message": "正在生成回复…",
    "waiting_for_tool_confirmation": "等待你的确认",
    "executing_tool": "正在执行工具…",
    "compacting_conversation": "正在压缩上下文…",
}


def working_label(state: str | None, *, stopping: bool = False) -> str:
    """Chinese label for an engine's working state."""
    if stopping:
        return "正在停止…"
    return WORKING_STATE_LABELS.get(state or "", "处理中…")


# --------------------------------------------------------------------------- text helpers


def normalize_newlines(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: max(0, limit - 1)] + "…"


def first_line(text: str, limit: int = 120) -> str:
    for line in normalize_newlines(text).split("\n"):
        if line.strip():
            return clip(line.strip(), limit)
    return ""


def head_preview(text: str, max_lines: int = 12, max_chars: int = 1500) -> tuple[str, bool]:
    lines = normalize_newlines(text).split("\n")
    while lines and not lines[-1].strip():
        lines.pop()
    clipped = len(lines) > max_lines
    out = "\n".join(lines[:max_lines])
    if len(out) > max_chars:
        out, clipped = out[:max_chars], True
    return out, clipped


def tail_preview(text: str, max_lines: int = 12, max_chars: int = 1500) -> tuple[str, bool]:
    lines = [line.rstrip() for line in normalize_newlines(text).split("\n")]
    while lines and not lines[-1]:
        lines.pop()
    while lines and not lines[0]:
        lines.pop(0)
    clipped = len(lines) > max_lines
    out = "\n".join(lines[-max_lines:])
    if len(out) > max_chars:
        out, clipped = out[-max_chars:], True
    return out, clipped


def display_path(path: str, limit: int = 64) -> str:
    """Show long Windows paths as ``…\\parent\\name``."""
    path = path.strip()
    if len(path) <= limit:
        return path
    parts = PureWindowsPath(path).parts
    tail = "\\".join(parts[-2:]) if len(parts) >= 2 else path
    return clip("…\\" + tail, limit)


_EXIT_CODE = re.compile(r"\[Process exited with code (-?\d+)\]|^Exit code: (-?\d+)[ \t]*$", re.MULTILINE)
"""Some agents' shell results end with the first form; WittyCode's own shell tool (``harness/shell``) with the second."""


def exit_code(output: str | None) -> int | None:
    if not output:
        return None
    matches = _EXIT_CODE.findall(output)
    if not matches:
        return None
    droid, own = matches[-1]
    return int(droid or own)


@dataclass(frozen=True, slots=True)
class TodoEntry:
    status: str
    text: str


_TODO_LINE = re.compile(r"^\s*(?:\d+[.)]\s*)?\[(?P<status>[A-Za-z_ -]+)\]\s*(?P<text>.*)$")


def parse_todos(value: Any) -> list[TodoEntry]:
    """TodoWrite input: ``"1. [in_progress] ...\\n2. [pending] ..."`` (or a list of dicts)."""
    entries: list[TodoEntry] = []
    if isinstance(value, str):
        for line in normalize_newlines(value).split("\n"):
            match = _TODO_LINE.match(line)
            if match:
                entries.append(TodoEntry(match["status"].strip().lower().replace(" ", "_"), match["text"].strip()))
            elif line.strip() and entries:
                entries[-1] = TodoEntry(entries[-1].status, f"{entries[-1].text} {line.strip()}")
    elif isinstance(value, (list, tuple)):
        for item in value:
            if isinstance(item, Mapping):
                text = item.get("content") or item.get("text") or item.get("title") or ""
                entries.append(TodoEntry(str(item.get("status", "pending")).lower(), str(text)))
    return entries


_QUESTION = re.compile(r"\[question\]\s*(.+)")


def questionnaire_questions(text: str) -> list[str]:
    return [match.strip() for match in _QUESTION.findall(text or "")]


_PATCH_FILE = re.compile(r"^\*\*\* (?:Update|Add|Delete) File: (.+)$", re.MULTILINE)


def patch_files(patch: str) -> list[str]:
    return [item.strip() for item in _PATCH_FILE.findall(patch or "")]


def _string(data: Mapping[str, Any], *keys: str) -> str:
    for key in keys:
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _patch_text(data: Mapping[str, Any]) -> str:
    for value in data.values():
        if isinstance(value, str) and ("*** Begin Patch" in value or "\n@@" in value):
            return value
    return ""


def describe_input(name: str, data: Mapping[str, Any]) -> str:
    """One-line, human-oriented description of a tool call's input (empty while it streams)."""
    if not data:
        return ""
    category = tool_category(name)
    memory = memory_tool(name)
    if memory == "memory_write":
        return "记住：" + clip(_string(data, "title"), 80)
    if memory == "memory_search":
        return "回忆：" + (clip(_string(data, "query"), 80) or "最近的记忆")
    if memory:
        return {"memory_read": "读取记忆 ", "memory_update": "修改记忆 ", "memory_forget": "忘记记忆 "}[memory] + _string(data, "id")
    if name == "Read":
        text = display_path(_string(data, "file_path", "path"))
        offset, limit = data.get("offset"), data.get("limit")
        if isinstance(offset, int) and isinstance(limit, int):
            text += f"（第 {offset}–{offset + limit} 行）"
        elif isinstance(offset, int):
            text += f"（从第 {offset} 行）"
        return text
    if name == "LS":
        return display_path(_string(data, "directory_path", "path")) or "."
    if name == "Glob":
        patterns = data.get("patterns")
        if isinstance(patterns, (list, tuple)):
            text = ", ".join(str(item) for item in patterns)
        else:
            text = str(patterns or "")
        folder = _string(data, "folder")
        return clip(f"{text} · {display_path(folder, 40)}" if folder else text, 120)
    if name == "Grep":
        pattern = _string(data, "pattern")
        where = _string(data, "path", "glob_pattern")
        text = f"“{clip(pattern, 60)}”"
        return f"{text} · {display_path(where, 48)}" if where else text
    if name == "Execute" or category is Category.COMMAND:
        return first_line(_string(data, "command"), 140) or _string(data, "summary")
    if name == "TodoWrite":
        todos = parse_todos(data.get("todos"))
        if not todos:
            return ""
        done = sum(1 for entry in todos if entry.status == "completed")
        active = next((entry.text for entry in todos if entry.status == "in_progress"), "")
        text = f"{done}/{len(todos)} 完成"
        return f"{text} · {clip(active, 60)}" if active else text
    if name == "AskUser":
        questions = questionnaire_questions(_string(data, "questionnaire"))
        if not questions:
            return ""
        more = f"（共 {len(questions)} 题）" if len(questions) > 1 else ""
        return clip(questions[0], 100) + more
    if name == "ApplyPatch":
        files = patch_files(_patch_text(data))
        if files:
            return display_path(files[0]) + (f" 等 {len(files)} 个文件" if len(files) > 1 else "")
    if category is Category.EDIT:
        return display_path(_string(data, "file_path", "path"))
    if name == "Task":
        return clip(_string(data, "description", "subagent_type", "prompt"), 120)
    if name == "FetchUrl":
        return clip(_string(data, "url"), 120)
    if name == "WebSearch":
        return clip(_string(data, "query"), 120)
    if name == "Skill":
        return _string(data, "skill")
    if name == "ExitSpecMode":
        return clip(_string(data, "title"), 120)
    for value in data.values():
        if isinstance(value, str) and value.strip():
            return first_line(value, 100)
    return ""