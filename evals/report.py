"""A run as a table for a person, and two runs side by side (the words are Chinese: the reader is the project's owner)."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from evals import SCHEMA
from evals.metrics import ENDS, KINDS, TaskResult, no_tokens

RESULT_FILE = "result.json"
END_WORDS = {
    "done": "正常结束", "limit": "步数上限", "incomplete": "回答不完整", "failed": "回合失败", "interrupted": "被停止",
    "budget": "Token 用尽", "timeout": "超时", "unmetered": "无用量数据", "skipped": "未运行", "error": "评测出错",
}
assert set(END_WORDS) == set(ENDS)


class ReportError(Exception):
    pass


def load(path: str | Path) -> dict[str, Any]:
    """A run's ``result.json`` (given the file or the run's folder), its tasks as ``TaskResult``."""
    file = Path(path)
    file = file / RESULT_FILE if file.is_dir() else file
    try:
        data = json.loads(file.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ReportError(f"{file}: {error}") from error
    if not isinstance(data, dict) or data.get("schema") != SCHEMA or not isinstance(data.get("tasks"), list):
        raise ReportError(f"{file}: not a result of this version (schema {SCHEMA})")
    data["tasks"] = [TaskResult.from_json(raw) for raw in data["tasks"]]
    return data


def totals(tasks: Sequence[TaskResult]) -> dict[str, Any]:
    """The sums over the tasks that ran."""
    ran = [task for task in tasks if task.ran]
    tokens = no_tokens()
    for task in ran:
        for kind in (*KINDS, "total"):
            tokens[kind] += task.tokens[kind]
    base = tokens["input"] + tokens["cacheWrite"] + tokens["cacheRead"]
    return {
        "tasks": len(tasks),
        "ran": len(ran),
        "passed": sum(1 for task in ran if task.passed),
        "tokens": tokens,
        "hit_rate": tokens["cacheRead"] / base if base else None,
        "cost_units": round(sum(task.cost_units for task in ran), 1),
        "requests": sum(task.requests for task in ran),
        "tool_calls": sum(task.tool_calls for task in ran),
        "approvals": sum(task.approvals for task in ran),
        "refusals": sum(task.refusals for task in ran),
        "prefix_breaks": sum(task.prefix_breaks for task in ran),
        "seconds": {key: round(sum(task.seconds[key] for task in ran), 1) for key in ("turn", "requests", "tools", "other")},
    }


def _k(count: float) -> str:
    return f"{count / 1000:,.1f}K"


def _rate(value: float | None) -> str:
    return "-" if value is None else f"{value:.1%}"


def _row(cells: Sequence[Any]) -> str:
    return "| " + " | ".join(str(cell) for cell in cells) + " |"


def _task_row(task: TaskResult) -> list[Any]:
    if not task.ran:
        return [task.id, "-", END_WORDS.get(task.end, task.end), *["-"] * 9]
    tokens, seconds = task.tokens, task.seconds
    return [
        task.id, "通过" if task.passed else "未过", END_WORDS.get(task.end, task.end), task.requests, task.tool_calls, task.approvals,
        _k(tokens["total"]), f"{_k(tokens['input'])} / {_k(tokens['cacheWrite'])} / {_k(tokens['cacheRead'])} / {_k(tokens['output'])}",
        _rate(task.hit_rate), _k(task.cost_units), f"{seconds['turn']:.0f}", f"{seconds['requests']:.0f} / {seconds['tools']:.0f} / {seconds['other']:.0f}",
    ]


def table(run: Mapping[str, Any]) -> str:
    """The run as Markdown: what was run, one line per task, the sums, and what went wrong where."""
    tasks: Sequence[TaskResult] = run["tasks"]
    options, about, total = run.get("options", {}), run.get("about", {}), totals(tasks)
    harness = about.get("commit", "?") + ("（工作树有未提交改动）" if about.get("dirty") else "")
    lines = [
        f"# 评测结果：{run.get('name', '')}",
        "",
        f"- 模型：`{options.get('model', '?')}`，推理档位：{options.get('reasoning') or '默认'}，权限档：{options.get('autonomy', '?')}，任务集：{about.get('suite', '?')}",
        f"- Harness 版本：{harness}",
        f"- 通过：**{total['passed']} / {total['ran']}**" + (f"（另有 {total['tasks'] - total['ran']} 个未运行）" if total["tasks"] != total["ran"] else ""),
        f"- Token 合计：**{total['tokens']['total']:,}**（预算 {options.get('budget', 0):,}），命中率 {_rate(total['hit_rate'])}，折算成本 {_k(total['cost_units'])}",
        f"- 用时：{total['seconds']['turn']:.0f} 秒（等模型 {total['seconds']['requests']:.0f}，工具 {total['seconds']['tools']:.0f}，其他 {total['seconds']['other']:.0f}）；"
        f"请求 {total['requests']} 次，工具调用 {total['tool_calls']} 次，需要人批准 {total['approvals']} 次"
        + (f"（另拒绝了 {total['refusals']} 次任务文件夹之外的读写）" if total["refusals"] else "")
        + f"，前缀被打断 {total['prefix_breaks']} 次",
        "",
        _row(["任务", "结果", "结束方式", "请求", "工具", "批准", "Token", "输入 / 缓存写 / 缓存读 / 输出", "命中率", "折算", "用时 s", "模型 / 工具 / 其他 s"]),
        _row(["---"] * 12),
        *[_row(_task_row(task)) for task in tasks],
        "",
        "折算成本以「未缓存输入 token」为单位（缓存读、缓存写、输出各乘以价格比例），只用于两次运行之间比较，不是账单。",
    ]
    troubled = [task for task in tasks if not task.passed or task.end != "done"]
    if troubled:
        lines += ["", "## 没通过或没正常结束的任务", ""]
        for task in troubled:
            lines.append(f"### {task.id}（{'通过' if task.passed else '未过'}，{END_WORDS.get(task.end, task.end)}）")
            for label, text in (("Harness 的说明", task.note), ("检查的输出", task.check)):
                if text:
                    lines += ["", f"{label}：", "", "```", text, "```"]
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _change(before: float, after: float) -> str:
    return "-" if not before else f"{(after - before) / before:+.0%}"


def compare(before: Mapping[str, Any], after: Mapping[str, Any]) -> str:
    """Two runs task by task: only the tasks both ran count in the sums."""
    first = {task.id: task for task in before["tasks"] if task.ran}
    second = {task.id: task for task in after["tasks"] if task.ran}
    shared = [task_id for task_id in first if task_id in second]
    lines = [
        f"# 对比：{before.get('name', 'A')} → {after.get('name', 'B')}",
        "",
        f"- A：`{before.get('options', {}).get('model', '?')}`，Harness {before.get('about', {}).get('commit', '?')}",
        f"- B：`{after.get('options', {}).get('model', '?')}`，Harness {after.get('about', {}).get('commit', '?')}",
        f"- 两边都跑了的任务：{len(shared)} 个" + ("" if len(shared) == len(first) == len(second) else f"（A 跑了 {len(first)} 个，B 跑了 {len(second)} 个；只比较共同的）"),
        "",
        _row(["任务", "结果 A → B", "Token A", "Token B", "变化", "折算 A", "折算 B", "变化", "请求 A → B", "用时 A → B (s)"]),
        _row(["---"] * 10),
    ]
    word = lambda task: "通过" if task.passed else "未过"  # noqa: E731
    for task_id in shared:
        a, b = first[task_id], second[task_id]
        lines.append(_row([
            task_id, f"{word(a)} → {word(b)}", _k(a.tokens["total"]), _k(b.tokens["total"]), _change(a.tokens["total"], b.tokens["total"]),
            _k(a.cost_units), _k(b.cost_units), _change(a.cost_units, b.cost_units), f"{a.requests} → {b.requests}", f"{a.seconds['turn']:.0f} → {b.seconds['turn']:.0f}",
        ]))
    a, b = totals([first[i] for i in shared]), totals([second[i] for i in shared])
    lines.append(_row([
        "**合计**", f"{a['passed']} → {b['passed']}", _k(a["tokens"]["total"]), _k(b["tokens"]["total"]), _change(a["tokens"]["total"], b["tokens"]["total"]),
        _k(a["cost_units"]), _k(b["cost_units"]), _change(a["cost_units"], b["cost_units"]), f"{a['requests']} → {b['requests']}",
        f"{a['seconds']['turn']:.0f} → {b['seconds']['turn']:.0f}",
    ]))
    flipped = [f"{task_id}（{word(first[task_id])} → {word(second[task_id])}）" for task_id in shared if first[task_id].passed != second[task_id].passed]
    lines += ["", "结果变了的任务：" + ("、".join(flipped) if flipped else "没有") + "。"]
    lines += ["", "任务数很少，通过数差一两个不说明问题；Token、请求数和用时是逐任务配对比较的，更能看出 Harness 改动的影响。同一个模型、同一档位的两次运行才可比。"]
    return "\n".join(lines) + "\n"
