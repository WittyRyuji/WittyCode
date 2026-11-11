"""Line diffs for file changes.

Sources, best first: the Edit tool result (``diffLines`` JSON, what actually
happened), the permission request (whole-file ``old_content``/``new_content``),
and the tool input (``old_str``/``new_str`` fragments, Create content, patch text).
"""

from __future__ import annotations

import difflib
import json
import re
from dataclasses import dataclass
from typing import Any, Literal

DiffKind = Literal["add", "del", "ctx", "gap"]
MAX_DIFF_LINES = 400
# difflib is quadratic in the worst case; huge files are cut before diffing.
_MAX_INPUT_LINES = 6000


@dataclass(frozen=True, slots=True)
class DiffLine:
    kind: DiffKind
    text: str
    old: int | None = None
    new: int | None = None


@dataclass(frozen=True, slots=True)
class FileDiff:
    path: str
    lines: tuple[DiffLine, ...]
    added: int
    removed: int
    truncated: int = 0
    is_new_file: bool = False


def _lines(text: str | None) -> list[str]:
    if not text:
        return []
    return text.replace("\r\n", "\n").replace("\r", "\n").removesuffix("\n").split("\n")


def _finish(path: str, lines: list[DiffLine], added: int, removed: int, limit: int, *, new_file: bool = False) -> FileDiff:
    truncated = max(0, len(lines) - limit)
    return FileDiff(path, tuple(lines[:limit]), added, removed, truncated, new_file)


def diff_from_contents(
    path: str,
    old: str | None,
    new: str | None,
    *,
    context: int = 3,
    limit: int = MAX_DIFF_LINES,
    numbered: bool = True,
) -> FileDiff:
    before = _lines(old)[:_MAX_INPUT_LINES]
    after = _lines(new)[:_MAX_INPUT_LINES]
    matcher = difflib.SequenceMatcher(None, before, after, autojunk=False)
    added = removed = 0
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag in ("replace", "delete"):
            removed += i2 - i1
        if tag in ("replace", "insert"):
            added += j2 - j1
    lines: list[DiffLine] = []
    if added or removed:
        for group in matcher.get_grouped_opcodes(context):
            if lines:
                lines.append(DiffLine("gap", ""))
            for tag, i1, i2, j1, j2 in group:
                if tag == "equal":
                    for offset in range(i2 - i1):
                        lines.append(
                            DiffLine(
                                "ctx",
                                before[i1 + offset],
                                i1 + offset + 1 if numbered else None,
                                j1 + offset + 1 if numbered else None,
                            )
                        )
                    continue
                if tag in ("replace", "delete"):
                    for index in range(i1, i2):
                        lines.append(DiffLine("del", before[index], index + 1 if numbered else None))
                if tag in ("replace", "insert"):
                    for index in range(j1, j2):
                        lines.append(DiffLine("add", after[index], None, index + 1 if numbered else None))
    return _finish(path, lines, added, removed, limit, new_file=old is None)


def diff_for_new_file(path: str, content: str | None, *, limit: int = MAX_DIFF_LINES) -> FileDiff:
    lines = [DiffLine("add", text, None, index + 1) for index, text in enumerate(_lines(content))]
    return _finish(path, lines, len(lines), 0, limit, new_file=True)


def diff_from_fragments(path: str, old_str: str | None, new_str: str | None) -> FileDiff:
    """Fragment-level diff from Edit's ``old_str``/``new_str`` (line numbers are unknown)."""
    return diff_from_contents(path, old_str or "", new_str or "", context=2, numbered=False)


def _int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def diff_from_edit_result(content: str, fallback_path: str = "", *, limit: int = MAX_DIFF_LINES) -> FileDiff | None:
    """Parse the Edit tool result: ``{"success", "file_path", "diffLines": [...], "linesAdded", "linesRemoved"}``."""
    try:
        data = json.loads(content)
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("diffLines"), list):
        return None
    lines: list[DiffLine] = []
    last_old = last_new = None
    counted_add = counted_del = 0
    for entry in data["diffLines"]:
        if not isinstance(entry, dict):
            continue
        raw_kind = entry.get("type")
        text = entry.get("content")
        text = text if isinstance(text, str) else ""
        numbers = entry.get("lineNumber") if isinstance(entry.get("lineNumber"), dict) else {}
        old, new = _int(numbers.get("old")), _int(numbers.get("new"))
        kind: DiffKind = "add" if raw_kind == "added" else "del" if raw_kind == "removed" else "ctx"
        jumped = (old is not None and last_old is not None and old > last_old + 1) or (
            new is not None and last_new is not None and new > last_new + 1
        )
        if jumped and lines and lines[-1].kind != "gap":
            lines.append(DiffLine("gap", ""))
        lines.append(DiffLine(kind, text, old, new))
        last_old = old if old is not None else last_old
        last_new = new if new is not None else last_new
        counted_add += kind == "add"
        counted_del += kind == "del"
    added = _int(data.get("linesAdded"))
    removed = _int(data.get("linesRemoved"))
    path = data.get("file_path") if isinstance(data.get("file_path"), str) else fallback_path
    return _finish(
        path,
        lines,
        counted_add if added is None else added,
        counted_del if removed is None else removed,
        limit,
    )


_PATCH_HEADER = re.compile(r"^\*\*\* (Update|Add|Delete) File: (.+)$")


def diffs_from_patch(patch: str, *, limit: int = MAX_DIFF_LINES) -> list[FileDiff]:
    """Parse ``*** Begin Patch`` (apply_patch) or unified diff text into per-file diffs."""
    files: list[tuple[str, bool, list[DiffLine]]] = []

    def start(path: str, new_file: bool = False) -> list[DiffLine]:
        files.append((path, new_file, []))
        return files[-1][2]

    current: list[DiffLine] | None = None
    for raw in _lines(patch):
        if raw.startswith(("*** Begin Patch", "*** End Patch", "*** Move to:", "diff --git", "index ")):
            continue
        header = _PATCH_HEADER.match(raw)
        if header:
            current = start(header.group(2).strip(), header.group(1) == "Add")
            continue
        if raw.startswith("--- "):
            current = start(raw[4:].strip().removeprefix("a/"))
            continue
        if raw.startswith("+++ "):
            if files:
                path, new_file, lines = files[-1]
                target = raw[4:].strip().removeprefix("b/")
                files[-1] = (target if target != "/dev/null" else path, new_file, lines)
            continue
        if current is None:
            current = start("(patch)")
        if raw.startswith("@@"):
            if current:
                current.append(DiffLine("gap", ""))
            continue
        if raw.startswith("+"):
            current.append(DiffLine("add", raw[1:]))
        elif raw.startswith("-"):
            current.append(DiffLine("del", raw[1:]))
        else:
            current.append(DiffLine("ctx", raw[1:] if raw.startswith(" ") else raw))
    result = []
    for path, new_file, lines in files:
        added = sum(1 for line in lines if line.kind == "add")
        removed = sum(1 for line in lines if line.kind == "del")
        result.append(_finish(path, lines, added, removed, limit, new_file=new_file))
    return result


def input_diffs(tool_name: str, data: dict[str, Any]) -> tuple[FileDiff, ...]:
    """Best-effort diff from a tool call's input alone."""
    path = data.get("file_path") if isinstance(data.get("file_path"), str) else ""
    if tool_name == "Edit" and isinstance(data.get("old_str"), str) and isinstance(data.get("new_str"), str):
        return (diff_from_fragments(path, data["old_str"], data["new_str"]),)
    if tool_name == "Create" and isinstance(data.get("content"), str):
        return (diff_for_new_file(path, data["content"]),)
    if tool_name == "ApplyPatch":
        for value in data.values():
            if isinstance(value, str) and ("*** Begin Patch" in value or "\n@@" in value):
                return tuple(diffs_from_patch(value))
    return ()


def result_diff(tool_name: str, result: str, data: dict[str, Any]) -> FileDiff | None:
    if tool_name in ("Edit", "MultiEdit", "ApplyPatch") and result.lstrip().startswith("{"):
        fallback = data.get("file_path") if isinstance(data.get("file_path"), str) else ""
        return diff_from_edit_result(result, fallback)
    return None