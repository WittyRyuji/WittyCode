"""Codex thread items in the transcript's terms: tool names and inputs, outcomes, diffs, plans, user input, errors.

Pure (no I/O); shared by the live controller and the history loader. Item shapes are those of the 0.165.0 schema
(``ThreadItem``); only ``userMessage`` items and failed / interrupted turns were seen live (no signed-in account), the
rest follows the generated schema.

========================  ==========================  ==============================================================
item                      transcript                  notes
========================  ==========================  ==============================================================
agentMessage              text block                  ``item/agentMessage/delta`` streams it
reasoning                 thinking entry              summary deltas (``summaryIndex`` starts a new paragraph)
commandExecution          ``Execute`` (command)       output deltas are the progress; result = output +
                                                      ``[Process exited with code N]`` (Droid's marker)
fileChange                ``Edit`` / ``Create`` /     one item per batch of changes; ``diff`` is a unified diff for
                          ``ApplyPatch`` (edit)       updates and the file content for adds and deletes
mcpToolCall               ``<server>/<tool>``         arguments as the input
webSearch                 ``WebSearch`` / ``FetchUrl``
imageView                 ``Read``
collabAgentToolCall       ``Task``
turn/plan/updated         ``TodoWrite`` (plan)        not an item; one step per update, like Droid's TodoWrite calls
========================  ==========================  ==============================================================
"""

from __future__ import annotations

import base64
import binascii
import json
import re
from collections.abc import Iterable, Mapping, Sequence
from pathlib import PureWindowsPath
from typing import Any

from desktop.core.attachments import IMAGE, PDF, TEXT, Attachment, image_attachment, sniff_image
from desktop.core.context_envelope import strip as strip_context
from desktop.core.diffs import MAX_DIFF_LINES, DiffLine, FileDiff, diff_for_new_file
from desktop.core.prompts import ActionView
from desktop.core.toolinfo import first_line
from desktop.core.transcript import ToolItem, ToolStatus, result_text

EXECUTE = "Execute"
EDIT = "Edit"
CREATE = "Create"
APPLY_PATCH = "ApplyPatch"
TODO = "TodoWrite"
ASK = "AskUser"
TEXT_ITEMS = frozenset({"agentMessage", "plan", "enteredReviewMode", "exitedReviewMode"})
QUIET_ITEMS = frozenset({"userMessage", "hookPrompt", "functionCallOutput", "subAgentActivity", "contextCompaction", "reasoning"})
ATTACHMENT_TAG = "attachment"
_ATTACHMENT_HEAD = re.compile(r'^<attachment name="([^"\n]*)" type="([^"\n]*)">\n')
_HUNK = re.compile(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")
_RETRY = re.compile(r"(\d+)\s*/\s*(\d+)")
HANDOFF_START = "WittyCode restored the earlier messages of this same conversation"
HANDOFF_JSON = "Earlier conversation (JSON):\n"
HANDOFF_END = "\n\nCurrent request:\n"
PLAN_STATUS = {"pending": "pending", "inProgress": "in_progress", "in_progress": "in_progress", "completed": "completed"}
_FILE_MARKS = {"add": "A", "delete": "D", "update": "M"}


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _name(path: str) -> str:
    return PureWindowsPath(path).name or path


# ---------------------------------------------------------------------------------------------- tools


def change_kind(change: Mapping[str, Any]) -> str:
    kind = change.get("kind")
    if isinstance(kind, Mapping):
        return _text(kind.get("type")) or "update"
    return _text(kind) or "update"


def change_target(change: Mapping[str, Any]) -> str:
    """Where the change ends up: the new name of a moved file, the path otherwise."""
    kind = change.get("kind")
    moved = kind.get("move_path") if isinstance(kind, Mapping) else None
    return moved if isinstance(moved, str) and moved else _text(change.get("path"))


def file_changes(item: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    return [change for change in item.get("changes") or () if isinstance(change, Mapping) and isinstance(change.get("path"), str)]


def tool_spec(item: Mapping[str, Any]) -> tuple[str, dict[str, Any]] | None:
    """``(tool name, input)`` for an item that is a step of the agent's work; ``None`` for messages and the like."""
    kind = item.get("type")
    if kind == "commandExecution":
        data: dict[str, Any] = {"command": _text(item.get("command"))}
        if item.get("cwd"):
            data["cwd"] = _text(item.get("cwd"))
        return EXECUTE, data
    if kind == "fileChange":
        changes = file_changes(item)
        paths = [change_target(change) for change in changes]
        if len(changes) == 1:
            return (CREATE if change_kind(changes[0]) == "add" else EDIT), {"file_path": paths[0]}
        return APPLY_PATCH, {"file_path": paths[0] if paths else "", "files": paths}
    if kind == "mcpToolCall":
        arguments = item.get("arguments")
        data = dict(arguments) if isinstance(arguments, Mapping) else ({} if arguments is None else {"arguments": arguments})
        return f"{_text(item.get('server')) or 'mcp'}/{_text(item.get('tool')) or '?'}", data
    if kind == "dynamicToolCall":
        arguments = item.get("arguments")
        data = dict(arguments) if isinstance(arguments, Mapping) else ({} if arguments is None else {"arguments": arguments})
        namespace = _text(item.get("namespace"))
        tool = _text(item.get("tool")) or "?"
        return (f"{namespace}/{tool}" if namespace else tool), data
    if kind == "webSearch":
        action = item.get("action") if isinstance(item.get("action"), Mapping) else {}
        if action.get("type") == "openPage":
            return "FetchUrl", {"url": _text(action.get("url"))}
        if action.get("type") == "findInPage":
            return "FetchUrl", {"url": _text(action.get("url")), "pattern": _text(action.get("pattern"))}
        queries = action.get("queries") if isinstance(action.get("queries"), list) else []
        query = _text(item.get("query")) or _text(action.get("query")) or ", ".join(str(q) for q in queries)
        return "WebSearch", {"query": query}
    if kind == "imageView":
        return "Read", {"file_path": _text(item.get("path"))}
    if kind == "collabAgentToolCall":
        prompt = _text(item.get("prompt"))
        return "Task", {"description": first_line(prompt, 100) or _text(item.get("tool")), "prompt": prompt}
    if kind == "imageGeneration":
        return "ImageGeneration", {key: value for key, value in item.items() if key not in ("type", "id") and isinstance(value, str)}
    if kind == "sleep":
        return "Sleep", {key: value for key, value in item.items() if key not in ("type", "id") and isinstance(value, (str, int, float))}
    return None


def command_result(item: Mapping[str, Any], output: str = "") -> str:
    """The output, followed by Droid's exit-code marker so the page shows a non-zero code the same way."""
    aggregated = item.get("aggregatedOutput")
    text = aggregated if isinstance(aggregated, str) else output
    code = item.get("exitCode")
    if isinstance(code, int) and not isinstance(code, bool):
        marker = f"[Process exited with code {code}]"
        return f"{text.rstrip()}\n\n{marker}" if text.strip() else marker
    return text


def tool_outcome(item: Mapping[str, Any], output: str = "") -> tuple[str, ToolStatus]:
    """``(result text, status)`` of a completed item (``item/completed`` or saved history)."""
    kind = item.get("type")
    status = item.get("status")
    if kind == "commandExecution":
        text = command_result(item, output)
        if status == "declined":
            return text or "已拒绝运行这条命令。", ToolStatus.CANCELLED
        code = item.get("exitCode")
        if status == "failed" and not isinstance(code, int):
            return text or "命令没有运行成功。", ToolStatus.ERROR
        return text, ToolStatus.DONE  # a non-zero exit code shows as such, like Droid's commands
    if kind == "fileChange":
        if status == "declined":
            return "已拒绝这次修改。", ToolStatus.CANCELLED
        if status == "failed":
            return "修改没有应用成功。", ToolStatus.ERROR
        lines = [f"{_FILE_MARKS.get(change_kind(change), 'M')} {change_target(change)}" for change in file_changes(item)]
        return "\n".join(lines) or "已修改。", ToolStatus.DONE
    if kind == "mcpToolCall":
        error = item.get("error")
        if status == "failed" or isinstance(error, Mapping):
            message = _text(error.get("message")) if isinstance(error, Mapping) else ""
            return message or "工具调用失败。", ToolStatus.ERROR
        result = item.get("result") if isinstance(item.get("result"), Mapping) else {}
        text = result_text(result.get("content")) if result.get("content") else ""
        if not text and result.get("structuredContent") is not None:
            text = json.dumps(result.get("structuredContent"), ensure_ascii=False)
        return text, ToolStatus.DONE
    if kind == "dynamicToolCall":
        texts = [_text(part.get("text")) for part in item.get("contentItems") or () if isinstance(part, Mapping)]
        failed = status == "failed" or item.get("success") is False
        return "\n".join(text for text in texts if text) or ("工具调用失败。" if failed else ""), (
            ToolStatus.ERROR if failed else ToolStatus.DONE
        )
    if kind == "collabAgentToolCall":
        if status == "failed":
            return "子任务失败。", ToolStatus.ERROR
        if status == "interrupted":
            return "子任务已中断。", ToolStatus.CANCELLED
        return "子任务完成。", ToolStatus.DONE
    if kind == "webSearch":
        spec = tool_spec(item)
        data = spec[1] if spec else {}
        return _text(data.get("query")) or _text(data.get("url")), ToolStatus.DONE
    if kind == "imageView":
        return "[图片]", ToolStatus.DONE
    if status == "failed":
        return "失败。", ToolStatus.ERROR
    return "", ToolStatus.DONE


# ---------------------------------------------------------------------------------------------- diffs


def looks_unified(text: str) -> bool:
    return bool(re.search(r"^@@ -\d", text, re.MULTILINE)) or text.startswith(("--- ", "diff --git"))


def parse_unified_diff(path: str, text: str, *, new_file: bool = False, limit: int = MAX_DIFF_LINES) -> FileDiff:
    """One file's unified diff (hunks with ``@@ -a,b +c,d @@`` headers) with line numbers."""
    lines: list[DiffLine] = []
    added = removed = 0
    old = new = 0
    in_hunk = False
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if raw.startswith("diff --git"):
            in_hunk = False
            continue
        hunk = _HUNK.match(raw)
        if hunk:
            if lines:
                lines.append(DiffLine("gap", ""))
            old, new = int(hunk.group(1)), int(hunk.group(2))
            in_hunk = True
            continue
        if not in_hunk:
            continue  # file headers (---, +++, index, mode lines) before the first hunk
        if raw.startswith("\\"):
            continue  # "\ No newline at end of file"
        if raw.startswith("+"):
            lines.append(DiffLine("add", raw[1:], None, new))
            new += 1
            added += 1
        elif raw.startswith("-"):
            lines.append(DiffLine("del", raw[1:], old, None))
            old += 1
            removed += 1
        elif raw.startswith(" "):
            lines.append(DiffLine("ctx", raw[1:], old, new))
            old += 1
            new += 1
    while lines and lines[-1].kind == "gap":
        lines.pop()
    truncated = max(0, len(lines) - limit)
    return FileDiff(path, tuple(lines[:limit]), added, removed, truncated, new_file)


def deleted_file(path: str, content: str, *, limit: int = MAX_DIFF_LINES) -> FileDiff:
    body = content.replace("\r\n", "\n").removesuffix("\n")
    rows = body.split("\n") if body else []
    lines = [DiffLine("del", text, number, None) for number, text in enumerate(rows, 1)]
    return FileDiff(path, tuple(lines[:limit]), 0, len(lines), max(0, len(lines) - limit))


def file_diffs(changes: Iterable[Mapping[str, Any]]) -> tuple[FileDiff, ...]:
    diffs: list[FileDiff] = []
    for change in changes:
        kind = change_kind(change)
        target = change_target(change)
        body = _text(change.get("diff"))
        if looks_unified(body):
            diffs.append(parse_unified_diff(target, body, new_file=kind == "add"))
        elif kind == "add":
            diffs.append(diff_for_new_file(target, body))
        elif kind == "delete":
            diffs.append(deleted_file(target, body))
        else:
            diffs.append(parse_unified_diff(target, body))
    return tuple(diffs)


# ---------------------------------------------------------------------------------------------- approvals


def command_action(item_id: str, params: Mapping[str, Any], tool: ToolItem | None) -> ActionView:
    reason = _text(params.get("reason")) or None
    network = params.get("networkApprovalContext")
    if isinstance(network, Mapping):
        host = _text(network.get("host")) or "?"
        return ActionView(
            kind="sandbox",
            tool_use_id=item_id,
            tool_name=EXECUTE,
            title=f"需要访问网络：{host}",
            path=host,
            command=_text(params.get("command")) or None,
            reason=reason,
        )
    command = _text(params.get("command")) or (_text(tool.input.get("command")) if tool is not None else "")
    title = "向正在运行的命令输入" if params.get("kind") == "writeStdin" else "运行命令"
    return ActionView(kind="execute", tool_use_id=item_id, tool_name=EXECUTE, title=title, command=command, reason=reason)


def file_action(item_id: str, params: Mapping[str, Any], tool: ToolItem | None) -> ActionView:
    diffs = tool.diffs if tool is not None else ()
    name = tool.name if tool is not None else EDIT
    path = diffs[0].path if diffs else (_text(tool.input.get("file_path")) if tool is not None else "")
    reason = _text(params.get("reason"))
    grant = _text(params.get("grantRoot"))
    if grant:
        reason = f"{reason}（并允许本会话写入 {grant}）" if reason else f"并允许本会话写入 {grant}"
    if name == CREATE:
        kind, title = "create", f"创建文件 {_name(path)}"
    elif len(diffs) > 1:
        kind, title = "apply_patch", f"应用补丁（{len(diffs)} 个文件）"
    else:
        kind, title = "edit", f"编辑文件 {_name(path)}"
    return ActionView(kind=kind, tool_use_id=item_id, tool_name=name, title=title, path=path or None, reason=reason or None, diffs=diffs)


def permissions_action(item_id: str, params: Mapping[str, Any]) -> ActionView:
    wanted = params.get("permissions") if isinstance(params.get("permissions"), Mapping) else {}
    parts = [label for key, label in (("network", "访问网络"), ("fileSystem", "读写更多文件")) if wanted.get(key)]
    return ActionView(
        kind="sandbox",
        tool_use_id=item_id,
        tool_name="RequestPermissions",
        title="请求更多权限：" + ("、".join(parts) if parts else "沙箱之外"),
        reason=_text(params.get("reason")) or None,
        path=_text(params.get("cwd")) or None,
    )


# ---------------------------------------------------------------------------------------------- plans


def plan_todos(plan: Any) -> list[dict[str, str]]:
    """``turn/plan/updated`` steps as TodoWrite's ``todos`` (``content`` / ``status``)."""
    todos: list[dict[str, str]] = []
    for step in plan or ():
        if isinstance(step, Mapping) and _text(step.get("step")).strip():
            todos.append({"content": _text(step.get("step")).strip(), "status": PLAN_STATUS.get(_text(step.get("status")), "pending")})
    return todos


def reasoning_text(item: Mapping[str, Any]) -> str:
    """The readable part of a reasoning item: its summaries, else its raw content."""
    for key in ("summary", "content"):
        parts = [part.strip() for part in item.get(key) or () if isinstance(part, str) and part.strip()]
        if parts:
            return "\n\n".join(parts)
    return ""


# ---------------------------------------------------------------------------------------------- user input


def attachment_block(name: str, media_type: str, body: str) -> str:
    """A text file (or a PDF's text) as one input item of its own, after the typed text."""
    safe = name.replace('"', "'").replace("\n", " ")
    return f'<{ATTACHMENT_TAG} name="{safe}" type="{media_type}">\n{body}\n</{ATTACHMENT_TAG}>'


def turn_input(text: str, attachments: Sequence[Attachment] = ()) -> list[dict[str, Any]]:
    """``turn/start.input``: the typed text first, then pictures (data URLs) and files (their text)."""
    items: list[dict[str, Any]] = [{"type": "text", "text": text, "text_elements": []}]
    for attachment in attachments:
        if attachment.kind == IMAGE and attachment.data:
            media_type = attachment.media_type or sniff_image(attachment.data) or "image/png"
            encoded = base64.b64encode(attachment.data).decode("ascii")
            items.append({"type": "image", "url": f"data:{media_type};base64,{encoded}"})
        elif attachment.kind == TEXT:
            items.append({"type": "text", "text": attachment_block(attachment.name, "text/plain", attachment.text), "text_elements": []})
        elif attachment.kind == PDF:
            if not (attachment.parsed_text or "").strip():
                raise ValueError(f"PDF「{attachment.name}」里没有可读的文字")
            items.append(
                {"type": "text", "text": attachment_block(attachment.name, "application/pdf", attachment.parsed_text or ""), "text_elements": []}
            )
    return items


def _picture(url: str, number: int) -> Attachment:
    name = f"图片 {number}"
    if url.startswith("data:") and ";base64," in url:
        head, _, payload = url.partition(";base64,")
        try:
            data = base64.b64decode(payload, validate=False)
        except (binascii.Error, ValueError):
            data = b""
        if data:
            return image_attachment(name, data, sniff_image(data) or head.removeprefix("data:"))
    return Attachment(kind=IMAGE, name=name)


def user_message_parts(item: Mapping[str, Any] | None) -> tuple[str, list[Attachment], tuple[int, bool] | None]:
    """A saved ``userMessage``: (what the user typed, attachments as chips/pictures, handoff mark)."""
    if not isinstance(item, Mapping):
        return "", [], None
    texts: list[str] = []
    attachments: list[Attachment] = []
    pictures = 0
    for part in item.get("content") or ():
        if not isinstance(part, Mapping):
            continue
        kind = part.get("type")
        if kind == "text":
            body = _text(part.get("text"))
            head = _ATTACHMENT_HEAD.match(body)
            if head and texts:
                media_type = head.group(2)
                attachments.append(Attachment(kind=PDF if media_type == "application/pdf" else TEXT, name=head.group(1) or "文档", media_type=media_type))
                continue
            texts.append(body)
        elif kind == "image":
            pictures += 1
            attachments.append(_picture(_text(part.get("url")), pictures))
        elif kind == "localImage":
            pictures += 1
            path = _text(part.get("path"))
            attachments.append(Attachment(kind=IMAGE, name=_name(path) or f"图片 {pictures}", path=path))
    typed, handoff = split_handoff("\n".join(texts))
    return typed, attachments, handoff


def split_handoff(text: str) -> tuple[str, tuple[int, bool] | None]:
    """A first message that carried another engine's conversation (``core/handoff``): the typed part and
    ``(carried, truncated)``; other text comes back as it is, except that the context WittyCode added after the typed text
    (``core/context_envelope``) is always taken out."""
    text = strip_context(text)
    if not text.startswith(HANDOFF_START):
        return text, None
    end = text.find(HANDOFF_END)
    if end < 0:
        return text, None
    head = text[:end]
    start = head.find(HANDOFF_JSON)
    carried = 0
    if start >= 0:
        try:
            entries = json.loads(head[start + len(HANDOFF_JSON) :])
        except ValueError:
            entries = None
        carried = len(entries) if isinstance(entries, list) else 0
    truncated = "bounded excerpt" in (head[:start] if start >= 0 else head)
    return text[end + len(HANDOFF_END) :], (carried, truncated)


# ---------------------------------------------------------------------------------------------- errors

_ERROR_TEXTS = {
    "usageLimitExceeded": "Codex 订阅的用量已到上限，请等额度重置后再试。",
    "rateLimitExceeded": "Codex 的请求太频繁，被限流了，请稍后再试。",
    "contextWindowExceeded": "对话超出了模型的上下文窗口：请开一个新会话再继续。",
    "sessionBudgetExceeded": "这个会话的用量预算已用完。",
    "unauthorized": "Codex 的登录已失效：请在「设置 → 账号与模型」里重新登录。",
    "modelNotFound": "Codex 找不到这个模型，或你的账号不能用它。",
    "serverOverloaded": "OpenAI 的服务正忙，请稍后再试。",
    "internalServerError": "OpenAI 的服务出错了，请稍后再试。",
    "sandboxError": "沙箱出错，命令没有运行。",
    "badRequest": "Codex 拒绝了这个请求。",
}


def error_kind(info: Any) -> tuple[str, int | None]:
    """``codexErrorInfo`` -> (kind, HTTP status): ``"usageLimitExceeded"`` or ``{"httpConnectionFailed": {...}}``."""
    if isinstance(info, str):
        return info, None
    if isinstance(info, Mapping) and info:
        kind, details = next(iter(info.items()))
        status = details.get("httpStatusCode") if isinstance(details, Mapping) else None
        return str(kind), status if isinstance(status, int) and not isinstance(status, bool) else None
    return "", None


def turn_error_text(error: Any) -> str:
    """A ``TurnError`` (``{message, codexErrorInfo, additionalDetails}``) for the user."""
    if not isinstance(error, Mapping):
        return "本轮失败。"
    message = _text(error.get("message")).strip()
    kind, status = error_kind(error.get("codexErrorInfo"))
    known = _ERROR_TEXTS.get(kind) or (_ERROR_TEXTS["unauthorized"] if status == 401 else None)
    if known and message:
        return f"{known}（{message}）"
    return known or message or "本轮失败。"


def retry_text(message: Any) -> str:
    """``Reconnecting... 2/5`` -> the working label while Codex retries by itself."""
    match = _RETRY.search(_text(message))
    return f"连接中断，正在重试（{match.group(1)}/{match.group(2)}）…" if match else "连接中断，正在重试…"
