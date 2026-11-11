"""File tools: read_file, list_dir, glob, grep, create_file, edit_file.

Paths are absolute or relative to the project folder and are resolved with ``Path.resolve()``, so ``..``, symlinks and
junctions are followed before any check. ``ctx.protected`` locations are refused here, whatever the permission level
(a tool called directly must not leak credentials); searches skip them silently. Walks never descend into symlinks or
Windows junctions (reparse points), so they cannot loop or leave the tree they were pointed at.

Text is UTF-8. A BOM is tolerated, and so is UTF-16 with a BOM, because Windows PowerShell 5.1 writes it for ``>`` and
``Set-Content``. ``edit_file`` keeps the file's BOM, its encoding and its dominant line ending: the model's text is
matched against an LF-normalised copy and converted back on write. A file that is not valid UTF-8 is read lossily (and
the result says so) but never edited, since writing it back would destroy the undecodable bytes.

A write (``create_file``, ``edit_file``) goes to a temporary file beside the target and replaces it with ``os.replace``,
so no reader and no crash sees half a file. One file is written by one call at a time, from reading it to replacing it.
Whether the call was stopped and whether the write happens are one decision, taken under ``ctx.writes`` (a
``CommitGuard``): a stopped call writes nothing even though its worker thread could not be stopped, and a call that is
already inside ``os.replace`` is reported as unknown rather than as stopped. A file that cannot be replaced (another
program holds it open without delete sharing) is written in place, as it always was.

``grep`` matches a pattern without regex meta characters in this process; a real regular expression is matched in a
separate Python process that is killed after ``GREP_TIMEOUT_SECONDS`` or when the turn is stopped. ``re`` can backtrack
for minutes on a pattern like ``(a+)+$`` and holds the GIL meanwhile, which would freeze the window from any thread.
"""

from __future__ import annotations

import codecs
import contextlib
import difflib
import fnmatch
import json
import os
import re
import secrets
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import weakref
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from desktop.core.diffs import FileDiff, diff_for_new_file, diff_from_contents
from desktop.core.harness.tools import (
    STOPPED_NOTHING_WRITTEN,
    ToolContext,
    ToolError,
    ToolRegistry,
    ToolResult,
    ToolSpec,
    passthrough_input,
    validate_arguments,
)
from desktop.core.winproc import CREATE_NO_WINDOW

IS_WINDOWS = sys.platform == "win32"

MAX_READ_BYTES = 5 * 1024 * 1024
MAX_GREP_FILE_BYTES = 2 * 1024 * 1024
DEFAULT_READ_LIMIT = 2000
MAX_LINE_CHARS = 2000
MAX_READ_CHARS = 250_000
"""Guard for a page of very long lines; ordinary files stop at the line limit long before this."""
MAX_LIST_ENTRIES = 500
MAX_GLOB_RESULTS = 200
MAX_GLOB_COLLECT = 20_000
MAX_BRACE_STEPS = 2000
MAX_PATTERN_CHARS = 1000
GREP_TEXT_CHARS = 500
BINARY_PROBE = 8192
FILE_ATTRIBUTE_REPARSE_POINT = 0x400

SKIP_DIRS = frozenset({".git", "node_modules", "__pycache__", ".venv", "venv", ".mypy_cache", ".pytest_cache", "dist", "build"})

_REMOTE_PATH = re.compile(r"[\\/]{2}")
REMOTE_REFUSED = (
    "Access denied: {path} starts with two separators, so it is a network or a device path. The tools only work on "
    "local paths: reading a remote path would make Windows connect to that machine and authenticate as this account. "
    "Use a local path."
)


# --------------------------------------------------------------------------- paths


def _fold(text: str) -> str:
    return os.path.normcase(text)


def is_within(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _resolve_quietly(path: Path) -> Path:
    try:
        return path.resolve()
    except (OSError, ValueError, RuntimeError):
        return Path(os.path.abspath(path))


def _unquoted(raw: str) -> str:
    """The raw argument without the quotes a model sometimes leaves around it."""
    text = raw.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        text = text[1:-1].strip()
    return text


def is_remote_path(raw: str) -> bool:
    """Does ``raw`` name a network share (``\\\\host\\share``, ``\\\\?\\UNC\\...``) or a device (``\\\\.\\PhysicalDrive0``)?

    Purely lexical, and asked before anything resolves, stats or opens the path: Windows connects to the other machine
    (and authenticates as this account) while resolving a UNC path, so it must not happen on the way to asking the
    user whether the tool may run at all.
    """
    return _REMOTE_PATH.match(_unquoted(raw)) is not None


def resolve_path(ctx: ToolContext, raw: str) -> tuple[Path, bool]:
    """The absolute, symlink-free path for ``raw`` and whether it lies inside the project."""
    text = _unquoted(raw)
    if not text:
        raise ToolError("The path is empty.")
    if is_remote_path(text):
        raise ToolError(REMOTE_REFUSED.format(path=text))
    path = Path(text)
    if not path.is_absolute():
        path = ctx.project / path
    try:
        resolved = path.resolve()
    except (OSError, ValueError, RuntimeError) as error:
        raise ToolError(f"Invalid path {raw!r}: {error}") from None
    return resolved, is_within(resolved, _resolve_quietly(ctx.project))


def protected_roots(ctx: ToolContext) -> list[Path]:
    return [_resolve_quietly(Path(item)) for item in ctx.protected]


def is_protected(ctx: ToolContext, resolved: Path) -> bool:
    return any(is_within(resolved, root) for root in protected_roots(ctx))


def _target(ctx: ToolContext, raw: str) -> Path:
    resolved, _ = resolve_path(ctx, raw)
    if ctx.protected and is_protected(ctx, resolved):
        raise ToolError(f"Access denied: {resolved} is a protected location (application settings or credentials). Tools cannot read or change it.")
    return resolved


def _folder(ctx: ToolContext, raw: str | None) -> Path:
    path = ctx.project.resolve() if raw is None else _target(ctx, raw)
    if not path.exists():
        raise ToolError(_not_found("Folder", path))
    if not path.is_dir():
        raise ToolError(f"{path} is a file, not a folder.")
    return path


def _similar(path: Path) -> list[str]:
    try:
        names = os.listdir(path.parent)
    except OSError:
        return []
    by_folded = {name.lower(): name for name in names}
    close = difflib.get_close_matches(path.name.lower(), list(by_folded), n=3, cutoff=0.6)
    return [by_folded[name] for name in close]


def _not_found(what: str, path: Path) -> str:
    if not path.parent.exists():
        return f"{what} not found: {path}. The folder {path.parent} does not exist either."
    similar = _similar(path)
    hint = f" Did you mean: {', '.join(similar)}?" if similar else ""
    return f"{what} not found: {path}.{hint}"


def _io_error(action: str, path: Path, error: OSError) -> ToolError:
    return ToolError(f"Cannot {action} {path}: {error.strerror or error}")


def _human_size(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    value = float(size)
    for unit in ("KB", "MB", "GB"):
        value /= 1024
        if value < 1024 or unit == "GB":
            return f"{value:.1f} {unit}"
    return f"{size} B"


def _is_link_dir(entry: os.DirEntry[str]) -> bool:
    """A symlink or (on Windows) any reparse point such as a junction: a directory a walk must not enter."""
    try:
        if entry.is_symlink():
            return True
        if IS_WINDOWS:
            attributes = getattr(entry.stat(follow_symlinks=False), "st_file_attributes", 0)
            return bool(attributes & FILE_ATTRIBUTE_REPARSE_POINT)
    except OSError:
        return True
    return False


# --------------------------------------------------------------------------- text


@dataclass(frozen=True)
class _Decoded:
    text: str
    codec: str
    bom: bytes
    lossy: bool


def _is_binary(data: bytes) -> bool:
    if data.startswith(codecs.BOM_UTF32_LE):
        return True
    if data.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return False
    return b"\0" in data


def _decode(data: bytes) -> _Decoded:
    bom, codec = b"", "utf-8"
    for candidate, name in ((codecs.BOM_UTF8, "utf-8"), (codecs.BOM_UTF16_LE, "utf-16-le"), (codecs.BOM_UTF16_BE, "utf-16-be")):
        if data.startswith(candidate):
            bom, codec = candidate, name
            data = data[len(candidate) :]
            break
    try:
        return _Decoded(data.decode(codec), codec, bom, False)
    except UnicodeDecodeError:
        return _Decoded(data.decode(codec, "replace"), codec, bom, True)


def _split_lines(text: str) -> list[str]:
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return lines


def _cut(line: str, limit: int) -> str:
    return line if len(line) <= limit else f"{line[:limit]} … [line truncated: {len(line) - limit} more characters]"


# --------------------------------------------------------------------------- read_file


def _format_numbered(lines: list[str], first: int) -> tuple[list[str], int]:
    out: list[str] = []
    used = 0
    for number, line in enumerate(lines, start=first):
        text = f"{number:>6}\t{_cut(line, MAX_LINE_CHARS)}"
        if out and used + len(text) > MAX_READ_CHARS:
            break
        out.append(text)
        used += len(text) + 1
    return out, first + len(out) - 1


def _read_window(path: Path, offset: int, limit: int, ctx: ToolContext) -> tuple[list[str], int | None, bool]:
    """Lines ``offset``..``offset+limit-1`` of a file too big to load; the total is unknown."""
    lines: list[str] = []
    more = False
    with path.open("rb") as handle:
        if b"\0" in handle.read(BINARY_PROBE):
            raise ToolError(f"{path} looks like a binary file (it contains NUL bytes); it cannot be read as text.")
        handle.seek(0)
        for number, raw in enumerate(handle, start=1):
            if number % 50_000 == 0 and ctx.abort.is_set():
                raise ToolError("Stopped by the user.")
            if number < offset:
                continue
            if len(lines) >= limit:
                more = True
                break
            text = raw.decode("utf-8", "replace").rstrip("\r\n")
            lines.append(text.removeprefix("\ufeff") if number == 1 else text)
    return lines, None, more


def read_file(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    path = _target(ctx, args["file_path"])
    narrowed = "offset" in args or "limit" in args
    offset = args.get("offset", 1)
    limit = args.get("limit", DEFAULT_READ_LIMIT)
    try:
        info = path.stat()
    except FileNotFoundError:
        raise ToolError(_not_found("File", path)) from None
    except OSError as error:
        raise _io_error("access", path, error) from None
    if stat.S_ISDIR(info.st_mode):
        raise ToolError(f"{path} is a folder, not a file. Use list_dir to see its contents.")
    if not stat.S_ISREG(info.st_mode):
        raise ToolError(f"{path} is not a regular file.")
    if info.st_size > MAX_READ_BYTES and not narrowed:
        raise ToolError(
            f"{path} is {_human_size(info.st_size)}, over the {_human_size(MAX_READ_BYTES)} limit for reading a whole file. "
            "Pass offset and limit to read a part of it, or use grep to find what you need."
        )
    note = ""
    try:
        if info.st_size > MAX_READ_BYTES:
            lines, total, more = _read_window(path, offset, limit, ctx)
            window = lines
        else:
            data = path.read_bytes()
            if _is_binary(data):
                raise ToolError(f"{path} looks like a binary file (it contains NUL bytes); it cannot be read as text.")
            decoded = _decode(data)
            if decoded.lossy:
                note = "[Note: the file is not valid UTF-8; undecodable bytes are shown as U+FFFD.]\n"
            all_lines = _split_lines(decoded.text)
            total = len(all_lines)
            window = all_lines[offset - 1 : offset - 1 + limit]
            more = offset - 1 + limit < total
    except OSError as error:
        raise _io_error("read", path, error) from None
    if total == 0:
        return ToolResult(f"{note}(The file is empty.)")
    if not window:
        size = f", which has {total} lines" if total is not None else ""
        raise ToolError(f"offset {offset} is past the end of the file{size}.")
    numbered, last = _format_numbered(window, offset)
    cut_by_size = last < offset + len(window) - 1
    body = "\n".join(numbered)
    if more or cut_by_size:
        of = f" of {total}" if total is not None else ""
        extra = "" if total is not None else " The file has more lines."
        body += f"\n[Showing lines {offset}-{last}{of}.{extra} Call read_file again with offset={last + 1} to continue.]"
    return ToolResult(note + body)


# --------------------------------------------------------------------------- list_dir


def list_dir(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    path = _target(ctx, args["directory_path"])
    if not path.exists():
        raise ToolError(_not_found("Folder", path))
    if not path.is_dir():
        raise ToolError(f"{path} is a file, not a folder. Use read_file to read it.")
    entries: list[tuple[bool, str, int | None]] = []
    try:
        with os.scandir(path) as scan:
            for entry in scan:
                if ctx.protected and is_protected(ctx, _resolve_quietly(path / entry.name)):
                    continue
                try:
                    is_dir = entry.is_dir()
                    size = None if is_dir else entry.stat().st_size
                except OSError:
                    is_dir, size = False, None
                entries.append((is_dir, entry.name, size))
    except OSError as error:
        raise _io_error("list", path, error) from None
    entries.sort(key=lambda item: (not item[0], item[1].lower(), item[1]))
    lines = [f"{path} ({len(entries)} {'entry' if len(entries) == 1 else 'entries'})"]
    for is_dir, name, size in entries[:MAX_LIST_ENTRIES]:
        if is_dir:
            lines.append(f"{name}/")
        else:
            lines.append(f"{name} ({_human_size(size)})" if size is not None else name)
    if len(entries) > MAX_LIST_ENTRIES:
        lines.append(f"[Showing the first {MAX_LIST_ENTRIES} of {len(entries)} entries. Use glob to narrow the search.]")
    elif not entries:
        lines.append("(empty folder)")
    return ToolResult("\n".join(lines))


# --------------------------------------------------------------------------- glob


def expand_braces(pattern: str) -> list[str]:
    """``*.{ts,tsx}`` -> ``['*.ts', '*.tsx']``; nested groups work, an unbalanced brace is left alone.

    A pattern that is too long or that would take more than ``MAX_BRACE_STEPS`` expansions comes back unexpanded, and
    its braces are then matched as ordinary characters (so it matches nothing). ``{a,b}`` written 40 times stands for
    2**40 names, which no caller may try to build: this runs inside a permission decision as well.
    """
    if len(pattern) > MAX_PATTERN_CHARS:
        return [pattern]
    budget = [MAX_BRACE_STEPS]
    expanded = _expand_braces(pattern, budget)
    return expanded if expanded is not None else [pattern]


def _expand_braces(pattern: str, budget: list[int]) -> list[str] | None:
    """One brace group expanded, then the rest; None once ``budget`` (shared by the whole expansion) runs out."""
    budget[0] -= 1
    if budget[0] < 0:
        return None
    start = pattern.find("{")
    if start < 0:
        return [pattern]
    depth, parts, cursor = 0, [], start + 1
    for index in range(start, len(pattern)):
        char = pattern[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                parts.append(pattern[cursor:index])
                head, tail = pattern[:start], pattern[index + 1 :]
                out: list[str] = []
                for part in parts:
                    more = _expand_braces(head + part + tail, budget)
                    if more is None:
                        return None
                    out.extend(more)
                return out
        elif char == "," and depth == 1:
            parts.append(pattern[cursor:index])
            cursor = index + 1
    return [pattern]


def has_magic(segment: str) -> bool:
    return any(char in segment for char in "*?[")


def _segment_matcher(segment: str):
    return re.compile(fnmatch.translate(segment), re.IGNORECASE if IS_WINDOWS else 0).match


def _normalise_pattern(pattern: str) -> list[str]:
    text = pattern.strip().replace("\\", "/")
    if not text:
        raise ToolError("A glob pattern is empty.")
    if text.startswith("/") or re.match(r"^[A-Za-z]:", text):
        raise ToolError(f"Glob pattern {pattern!r} is absolute. Patterns are relative to `folder`; pass the folder separately.")
    segments = [part for part in text.split("/") if part and part != "."]
    if not segments:
        raise ToolError(f"Glob pattern {pattern!r} selects nothing; name what to match, e.g. '*' or '**/*.py'.")
    return segments


class _Globber:
    def __init__(self, ctx: ToolContext, allow: frozenset[str]) -> None:
        self.ctx = ctx
        self.allow = allow
        self.found: dict[str, tuple[bool, float]] = {}
        self.full = False
        self._blocked = [_fold(str(root)) for root in protected_roots(ctx)]

    def _skipped(self, name: str) -> bool:
        lowered = name.lower()
        return lowered in SKIP_DIRS and lowered not in self.allow

    def _under_protected(self, path: str) -> bool:
        if not self._blocked:
            return False
        folded = _fold(os.path.abspath(path))
        return any(folded == root or folded.startswith(root + os.sep) for root in self._blocked)

    def _emit(self, path: str, rel: str, is_dir: bool | None = None) -> None:
        if self._under_protected(path):
            return
        try:
            info = os.stat(path)
        except OSError:
            try:
                info = os.lstat(path)
            except OSError:
                return
        if is_dir is None:
            is_dir = stat.S_ISDIR(info.st_mode)
        if len(self.found) >= MAX_GLOB_COLLECT:
            self.full = True
            return
        self.found.setdefault(rel, (is_dir, info.st_mtime))

    def _subdirs(self, directory: str) -> list[tuple[str, str]]:
        found = []
        try:
            with os.scandir(directory) as scan:
                for entry in scan:
                    if entry.is_dir(follow_symlinks=False) and not _is_link_dir(entry) and not self._skipped(entry.name):
                        if not self._under_protected(entry.path):
                            found.append((entry.name, entry.path))
        except OSError:
            return []
        return sorted(found)

    def walk(self, directory: str, rel: str, segments: list[str], index: int) -> None:
        if self.full or self.ctx.abort.is_set():
            return
        segment, last = segments[index], index == len(segments) - 1
        if segment == "**":
            if last:
                if rel:
                    self._emit(directory, rel.rstrip("/"), True)
            else:
                self.walk(directory, rel, segments, index + 1)
            for name, sub in self._subdirs(directory):
                self.walk(sub, f"{rel}{name}/", segments, index)
            return
        if not has_magic(segment):
            target = os.path.join(directory, segment)
            if last:
                if os.path.lexists(target):
                    self._emit(target, rel + segment)
            elif os.path.isdir(target) and not self._under_protected(target):
                self.walk(target, f"{rel}{segment}/", segments, index + 1)
            return
        matches = _segment_matcher(segment)
        try:
            with os.scandir(directory) as scan:
                entries = sorted(scan, key=lambda item: item.name)
        except OSError:
            return
        for entry in entries:
            if not matches(entry.name):
                continue
            try:
                is_dir = entry.is_dir()
            except OSError:
                is_dir = False
            if is_dir and self._skipped(entry.name):
                continue
            if last:
                self._emit(entry.path, rel + entry.name, is_dir)
            elif is_dir and not self._under_protected(entry.path):
                self.walk(entry.path, f"{rel}{entry.name}/", segments, index + 1)


def glob(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    folder = _folder(ctx, args.get("folder"))
    patterns = [item for raw in args["patterns"] for item in expand_braces(raw)]
    globber = _Globber(ctx, _named_dirs(patterns))
    for pattern in patterns:
        segments = _normalise_pattern(pattern)
        globber.walk(str(folder), "", segments, 0)
    found = globber.found
    if ctx.protected:
        found = {rel: value for rel, value in found.items() if not is_protected(ctx, _resolve_quietly(folder / rel))}
    header = [f"Paths are relative to {folder}"] if folder != _resolve_quietly(ctx.project) else []
    if not found:
        return ToolResult("\n".join([*header, "No files matched."]))
    ordered = sorted(found.items(), key=lambda item: (-item[1][1], item[0]))
    lines = [rel + ("/" if is_dir else "") for rel, (is_dir, _) in ordered[:MAX_GLOB_RESULTS]]
    if len(ordered) > MAX_GLOB_RESULTS:
        lines.append(f"[Showing the newest {MAX_GLOB_RESULTS} of {len(ordered)} matches. Narrow the pattern to see the rest.]")
    if globber.full:
        lines.append(f"[The search stopped after {MAX_GLOB_COLLECT} matches; the list may be incomplete. Narrow the pattern.]")
    return ToolResult("\n".join([*header, *lines]))


def _named_dirs(patterns: list[str]) -> frozenset[str]:
    """Skipped folders the patterns spell out (``node_modules/**/*.js`` wants node_modules)."""
    names = set()
    for pattern in patterns:
        for part in pattern.replace("\\", "/").split("/"):
            if part.lower() in SKIP_DIRS:
                names.add(part.lower())
    return frozenset(names)


# --------------------------------------------------------------------------- grep

GREP_TIMEOUT_SECONDS = 10.0
GREP_POLL_SECONDS = 0.02
GREP_KILL_WAIT_SECONDS = 5.0
_REGEX_META = frozenset("\\^$.|?*+()[]{}")
_IMPORT_ROOT = Path(__file__).resolve().parents[3]
"""The folder that holds the ``desktop`` package: where the search process imports it from."""
_SEARCH_PROCESS = "import sys; from desktop.core.harness import fs_tools; fs_tools._search_process(sys.argv[1], sys.argv[2])"


_RUN = "run"
"""A step that stands for any number of items: ``*`` inside a segment, ``**`` across segments."""


def _reachable(steps: list[tuple[str, Any]], indexes: Iterable[int]) -> set[int]:
    """The steps reachable without consuming anything, because a run may stand for nothing."""
    out: set[int] = set()
    stack = list(indexes)
    while stack:
        index = stack.pop()
        if index in out:
            continue
        out.add(index)
        if index < len(steps) and steps[index][0] == _RUN:
            stack.append(index + 1)
    return out


def _walk_steps(steps: list[tuple[str, Any]], items: Sequence[Any], fits: Callable[[tuple[str, Any], Any], bool]) -> bool:
    """Does the state machine ``steps`` match ``items`` from end to end?

    One pass over the items, keeping every step that is still possible. That is what keeps a glob linear: the machine
    never goes back to try another division of the text between two runs.
    """
    live = _reachable(steps, (0,))
    for item in items:
        moved: set[int] = set()
        for index in live:
            if index >= len(steps):
                continue
            step = steps[index]
            if step[0] == _RUN:
                moved.add(index)
            elif fits(step, item):
                moved.add(index + 1)
        if not moved:
            return False
        live = _reachable(steps, moved)
    return len(steps) in live


def _char_ranges(body: str) -> tuple[bool, tuple[tuple[str, str], ...]]:
    """A ``[...]`` class as (negated, ranges); ``a-z`` is a range, anything else stands for itself."""
    negated = body.startswith(("!", "^"))
    if negated:
        body = body[1:]
    ranges: list[tuple[str, str]] = []
    index = 0
    while index < len(body):
        if index + 2 < len(body) and body[index + 1] == "-":
            ranges.append((body[index], body[index + 2]))
            index += 3
        else:
            ranges.append((body[index], body[index]))
            index += 1
    return negated, tuple(ranges)


def _segment_steps(segment: str) -> list[tuple[str, Any]]:
    """One step per ``*`` run, ``?``, ``[...]`` or literal character of one path segment."""
    steps: list[tuple[str, Any]] = []
    index = 0
    while index < len(segment):
        char = segment[index]
        if char == "*":
            while index < len(segment) and segment[index] == "*":
                index += 1  # a run of stars is one step: there is nothing to divide the text between
            steps.append((_RUN, None))
            continue
        if char == "?":
            steps.append(("any", None))
        elif char == "[":
            end = segment.find("]", index + 2)
            if end < 0:
                steps.append(("char", char))
            else:
                steps.append(("class", _char_ranges(segment[index + 1 : end])))
                index = end
        else:
            steps.append(("char", char))
        index += 1
    return steps


def _fits_char(step: tuple[str, Any], char: str) -> bool:
    kind, payload = step
    if kind == "char":
        return payload == char
    if kind == "any":
        return True
    negated, ranges = payload
    return any(low <= char <= high for low, high in ranges) != negated


def _fits_segment(step: tuple[str, Any], part: str) -> bool:
    return _walk_steps(step[1], part, _fits_char)


def _quick_match(pattern: str) -> Callable[[str], bool] | None:
    """A string comparison for the shapes nearly every glob has (``name``, ``*.py``, ``build*``, ``test_*.py``), or None
    when the pattern needs the state machine. A ``*`` never stands for a ``/``, hence the test on the whole text."""
    if "/" in pattern or "[" in pattern or "?" in pattern:
        return None
    head, star, tail = pattern.partition("*")
    if not star:
        return lambda text: text == pattern
    if "*" in tail:
        return None
    least = len(head) + len(tail)
    return lambda text: len(text) >= least and text.startswith(head) and text.endswith(tail) and "/" not in text


def _glob_matcher(pattern: str) -> Callable[[str], bool]:
    """ripgrep-style ``--glob``: ``**`` crosses folders, ``*`` and ``?`` stay inside one path segment, ``[...]`` is a
    character class; case is ignored on Windows.

    Matching cannot backtrack, while the equivalent regular expression could: ``re`` needs 0.7 s for ``("a*" * 12) +
    "b"`` against a 28 character name, and holds the GIL while it tries, which stalls the window from any thread.
    """
    folded = pattern.lower() if IS_WINDOWS else pattern
    last_segment = False
    if folded.startswith("**/") and "/" not in folded[3:] and "**" not in folded[3:]:
        folded, last_segment = folded[3:], True  # ``**/x`` is ``x`` matched against the last segment
    quick = _quick_match(folded)
    parts = folded.split("/")
    steps = [(_RUN, None) if part == "**" else ("segment", _segment_steps(part)) for part in parts]
    if len(parts) > 1 and parts[-1] == "**":
        steps.insert(-1, ("segment", [(_RUN, None)]))  # ``src/**`` is what is inside src, not src itself

    def matches(text: str) -> bool:
        if IS_WINDOWS:
            text = text.lower()
        if last_segment:
            text = text.rsplit("/", 1)[-1]
        if quick is not None:
            return quick(text)
        return _walk_steps(steps, text.split("/"), _fits_segment)

    return matches


def _file_filter(glob_pattern: str | None):
    if not glob_pattern:
        return None
    rules = []
    for item in expand_braces(glob_pattern.strip().replace("\\", "/")):
        item = item.removeprefix("./").removeprefix("/")
        rules.append((_glob_matcher(item), "/" in item))

    def accepts(rel: str) -> bool:
        base = rel.rsplit("/", 1)[-1]
        return any(matches(rel if anchored else base) for matches, anchored in rules)

    return accepts


def _walk_files(root: str, rel: str, allow: frozenset[str], aborted: Callable[[], bool], blocked: list[str]) -> Iterator[tuple[str, str]]:
    if aborted():
        return
    try:
        with os.scandir(root) as scan:
            entries = sorted(scan, key=lambda item: item.name)
    except OSError:
        return
    for entry in entries:
        path = entry.path
        if blocked:
            folded = _fold(os.path.abspath(path))
            if any(folded == item or folded.startswith(item + os.sep) for item in blocked):
                continue
        try:
            if entry.is_dir(follow_symlinks=False):
                lowered = entry.name.lower()
                if _is_link_dir(entry) or (lowered in SKIP_DIRS and lowered not in allow):
                    continue
                yield from _walk_files(path, f"{rel}{entry.name}/", allow, aborted, blocked)
            elif entry.is_file():
                if blocked and entry.is_symlink():
                    real = _fold(os.path.realpath(path))
                    if any(real == item or real.startswith(item + os.sep) for item in blocked):
                        continue
                yield path, rel + entry.name
        except OSError:
            continue


def _read_searchable(path: str) -> list[str] | None:
    try:
        if os.path.getsize(path) > MAX_GREP_FILE_BYTES:
            return None
        with open(path, "rb") as handle:
            data = handle.read()
    except OSError:
        return None
    if _is_binary(data):
        return None
    return _split_lines(_decode(data).text)


def grep(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    pattern: str = args["pattern"]
    try:
        re.compile(pattern, re.IGNORECASE if args.get("case_insensitive") else 0)
    except re.error as error:
        raise ToolError(
            f"Invalid regular expression {pattern!r}: {error}. Python `re` syntax applies; "
            "escape literal ( ) [ ] { } . * + ? | ^ $ with a backslash."
        ) from None
    project = _resolve_quietly(ctx.project)
    target = _target(ctx, args["path"]) if "path" in args else project
    if not target.exists():
        raise ToolError(_not_found("Path", target))
    request = {
        "pattern": pattern,
        "case_insensitive": bool(args.get("case_insensitive")),
        "project": str(project),
        "target": str(target),
        "glob_pattern": args.get("glob_pattern"),
        "output_mode": args.get("output_mode", "file_paths"),
        "context": args.get("context", 0),
        "head_limit": args.get("head_limit", 250),
        "blocked": [_fold(str(root)) for root in protected_roots(ctx)],
    }
    if _plain_text(pattern):
        out, truncated = _search(request, ctx.abort.is_set)
    else:
        out, truncated = _search_apart(request, ctx.abort)
    if not out:
        return ToolResult("No matches found.")
    if truncated:
        unit = "files" if request["output_mode"] == "file_paths" else "lines"
        out.append(f"[Output limited to {request['head_limit']} {unit}. Narrow the pattern, path or glob_pattern, or raise head_limit.]")
    return ToolResult("\n".join(out))


def _plain_text(pattern: str) -> bool:
    """No regex meta characters: the pattern can only match as plain text, in linear time, so it cannot backtrack."""
    return not _REGEX_META.intersection(pattern)


def _search(request: Mapping[str, Any], aborted: Callable[[], bool]) -> tuple[list[str], bool]:
    """The search itself, here or in the search process: the output lines, and whether head_limit cut them."""
    regex = re.compile(request["pattern"], re.IGNORECASE if request["case_insensitive"] else 0)
    project, target = Path(request["project"]), Path(request["target"])
    mode, context, head_limit = request["output_mode"], request["context"], request["head_limit"]
    glob_pattern = request["glob_pattern"]

    def shown(path: str) -> str:
        resolved = Path(path)
        try:
            return resolved.relative_to(project).as_posix()
        except ValueError:
            return str(resolved)

    accepts = _file_filter(glob_pattern)
    allow = _named_dirs(expand_braces(glob_pattern)) if glob_pattern else frozenset()
    if target.is_file():
        files: Iterator[tuple[str, str]] = iter([(str(target), target.name)])
    else:
        files = _walk_files(str(target), "", allow, aborted, list(request["blocked"]))
        if accepts:
            files = ((path, rel) for path, rel in files if accepts(rel))

    out: list[str] = []
    emitted = 0
    truncated = False
    for path, _rel in files:
        if aborted():
            raise ToolError("Stopped by the user.")
        lines = _read_searchable(path)
        if lines is None:
            continue
        hits = [index for index, line in enumerate(lines) if regex.search(line)]
        if not hits:
            continue
        label = shown(path)
        if mode == "file_paths":
            if emitted >= head_limit:
                truncated = True
                break
            out.append(label)
            emitted += 1
            continue
        wanted: list[int] = []
        shown_lines = set()
        for hit in hits:
            for index in range(max(0, hit - context), min(len(lines), hit + context + 1)):
                if index not in shown_lines:
                    shown_lines.add(index)
                    wanted.append(index)
        hit_set = set(hits)
        previous = None
        for index in wanted:
            if emitted >= head_limit:
                truncated = True
                break
            if previous is not None and context and index != previous + 1:
                out.append("--")
            sep = ":" if index in hit_set else "-"
            out.append(f"{label}{sep}{index + 1}{sep}{_cut_text(lines[index])}")
            emitted += 1
            previous = index
        if truncated:
            break
    return out, truncated


def _search_apart(request: Mapping[str, Any], abort: threading.Event) -> tuple[list[str], bool]:
    """Run ``_search`` in a separate Python process, killed at the time limit or when the turn is stopped."""
    if abort.is_set():
        raise ToolError("Stopped by the user.")
    folder = Path(tempfile.mkdtemp(prefix="wittycode-grep-"))
    try:
        asked, answered = folder / "request.json", folder / "answer.json"
        asked.write_text(json.dumps({**request, "timeout": GREP_TIMEOUT_SECONDS}), encoding="utf-8")
        extra: dict[str, Any] = {"creationflags": CREATE_NO_WINDOW} if IS_WINDOWS else {"start_new_session": True}
        try:
            process = subprocess.Popen(
                [_worker_python(), "-c", _SEARCH_PROCESS, str(asked), str(answered)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                cwd=str(_IMPORT_ROOT),
                env={**os.environ, "PYTHONPATH": str(_IMPORT_ROOT)},
                **extra,
            )
        except OSError as error:
            raise ToolError(f"The search could not be started: {error.strerror or error}.") from None
        try:
            _wait_for_search(process, abort, request["pattern"])
        finally:
            if process.poll() is None:
                process.kill()
                with contextlib.suppress(subprocess.TimeoutExpired):
                    process.wait(GREP_KILL_WAIT_SECONDS)
        return _search_answer(answered, process.returncode)
    finally:
        shutil.rmtree(folder, ignore_errors=True)


def _wait_for_search(process: subprocess.Popen[bytes], abort: threading.Event, pattern: str) -> None:
    deadline = time.monotonic() + GREP_TIMEOUT_SECONDS
    while True:
        with contextlib.suppress(subprocess.TimeoutExpired):
            process.wait(GREP_POLL_SECONDS)
            return
        if abort.is_set():
            raise ToolError("Stopped by the user.")
        if time.monotonic() >= deadline:
            raise ToolError(
                f"The search took too long (more than {GREP_TIMEOUT_SECONDS:g} s) and was stopped. The regular expression "
                f"{pattern!r} probably makes Python's regex engine backtrack, as nested or overlapping repetition such "
                "as (a+)+ or (a|a)* does. Use a simpler pattern, or plain text, which is matched directly."
            )


def _search_answer(path: Path, code: int | None) -> tuple[list[str], bool]:
    try:
        answer = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        answer = None
    if isinstance(answer, dict) and isinstance(answer.get("failed"), str):
        raise ToolError(f"grep failed: {answer['failed']}")
    if not isinstance(answer, dict) or not isinstance(answer.get("lines"), list):
        raise ToolError(f"The search process ended without an answer (exit code {code}).")
    return [str(line) for line in answer["lines"]], bool(answer.get("truncated"))


def _worker_python() -> str:
    """This program's Python, with a console where there is one (``python.exe`` beside ``pythonw.exe``)."""
    executable = Path(sys.executable)
    if executable.name.lower() == "pythonw.exe":
        console = executable.with_name("python.exe")
        if console.is_file():
            return str(console)
    return str(executable)


def _search_process(asked: str, answered: str) -> None:
    """The search process (``python -c _SEARCH_PROCESS``): reads the request and writes the answer, both as JSON."""
    import faulthandler

    request = json.loads(Path(asked).read_text(encoding="utf-8"))
    # The parent kills this process at the time limit; this is for a parent that died first. A thread could not do it,
    # since a backtracking match holds the GIL; faulthandler's watchdog does not need the GIL.
    sink = open(os.devnull, "w")  # open for as long as the process lives: the watchdog writes to it when it fires
    faulthandler.dump_traceback_later(float(request["timeout"]) + 30, exit=True, file=sink)
    try:
        lines, truncated = _search(request, lambda: False)
        answer: dict[str, Any] = {"lines": lines, "truncated": truncated}
    except Exception as error:  # the parent must get an answer, not a bare exit code
        answer = {"failed": f"{type(error).__name__}: {error}"}
    Path(answered).write_text(json.dumps(answer), encoding="utf-8")


def _cut_text(line: str) -> str:
    return line if len(line) <= GREP_TEXT_CHARS else f"{line[:GREP_TEXT_CHARS]} … [{len(line) - GREP_TEXT_CHARS} more characters]"


# --------------------------------------------------------------------------- writing

REPLACE_ATTEMPTS = 3
REPLACE_RETRY_SECONDS = 0.05


class _Writer:
    __slots__ = ("lock", "__weakref__")

    def __init__(self) -> None:
        self.lock = threading.Lock()


_writers: weakref.WeakValueDictionary[str, _Writer] = weakref.WeakValueDictionary()
_writers_guard = threading.Lock()


@contextlib.contextmanager
def _one_writer(path: Path) -> Iterator[None]:
    """One create or edit of ``path`` at a time, from reading it to writing it: two calls on one file must not both read
    the old text and then each write their own version of it."""
    key = _fold(str(path))
    with _writers_guard:
        writer = _writers.get(key)
        if writer is None:
            writer = _writers[key] = _Writer()
    with writer.lock:
        yield


def _refuse_if_stopped(ctx: ToolContext) -> None:
    if ctx.abort.is_set():
        raise ToolError(STOPPED_NOTHING_WRITTEN)


def _write(path: Path, data: bytes, ctx: ToolContext) -> None:
    """Replace ``path`` with ``data`` as a whole, or write it in place when it cannot be replaced.

    Every write goes through ``ctx.writes``, which makes "is this call stopped?" and "commit this write" one decision:
    a stopped call leaves the file alone, including between two attempts at replacing it, and a call whose commit has
    begun is reported as unknown instead of stopped. A replaced file keeps its permission bits.
    """
    _refuse_if_stopped(ctx)
    try:
        mode: int | None = stat.S_IMODE(path.stat().st_mode)
    except OSError:
        mode = None
    temporary = _temporary_beside(path, data)
    if temporary is not None:
        replaced = False
        try:
            replaced = _replace(temporary, path, ctx)
        finally:
            if not replaced:
                with contextlib.suppress(OSError):
                    os.unlink(temporary)
        if replaced:
            if mode is not None:
                with contextlib.suppress(OSError):
                    os.chmod(path, mode)
            return
    with ctx.writes.commit(ctx.abort) as written:
        path.write_bytes(data)
        written()


def _temporary_beside(path: Path, data: bytes) -> Path | None:
    """A new file holding ``data`` in the folder of ``path`` (so that replacing is a rename on one volume), or None when
    none can be created there. Opened like an ordinary new file, so a new file gets the usual permissions."""
    temporary = path.with_name(f".{path.name[:100]}.{secrets.token_hex(4)}.tmp")
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o666)
    except OSError:
        return None
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temporary)
        raise
    return temporary


def _replace(temporary: Path, path: Path, ctx: ToolContext) -> bool:
    """``os.replace``, tried a few times: a virus scanner or an indexer may hold the file for a moment. False when the
    file stays locked against it.

    Each attempt is a commit of its own, so a stop that arrives while the file is locked (or during the wait between
    two attempts) still leaves it alone.
    """
    for attempt in range(REPLACE_ATTEMPTS):
        with ctx.writes.commit(ctx.abort) as written:
            try:
                os.replace(temporary, path)
            except PermissionError:
                pass
            else:
                written()
                return True
        if attempt + 1 < REPLACE_ATTEMPTS:
            time.sleep(REPLACE_RETRY_SECONDS)
    return False


# --------------------------------------------------------------------------- create_file / edit_file


def create_file(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    path = _target(ctx, args["file_path"])
    content: str = args["content"]
    if path.is_dir():
        raise ToolError(f"{path} is a folder, not a file.")
    try:
        data = content.encode("utf-8")
    except UnicodeEncodeError:
        raise ToolError("content has characters that cannot be written as UTF-8 (a lone surrogate).") from None
    with _one_writer(path):
        existed = path.exists()
        try:
            before = _existing_text(path) if existed else None
            _refuse_if_stopped(ctx)
            path.parent.mkdir(parents=True, exist_ok=True)
            _write(path, data, ctx)
        except OSError as error:
            raise _io_error("write", path, error) from None
    diff = diff_from_contents(str(path), before, content) if existed else diff_for_new_file(str(path), content)
    verb = "Overwrote" if existed else "Created"
    return ToolResult(f"{verb} {path} ({len(_split_lines(content))} lines, {_human_size(len(data))}).", diffs=(diff,))


def _existing_text(path: Path) -> str:
    if path.stat().st_size > MAX_READ_BYTES:
        return ""
    return _decode(path.read_bytes()).text


@dataclass(frozen=True)
class _EditPlan:
    path: Path
    before: str
    after: str
    count: int
    codec: str
    bom: bytes
    crlf: bool
    mixed: bool


def _line_of(text: str, index: int) -> int:
    return text.count("\n", 0, index) + 1


def _mismatch_hint(text: str, old: str) -> str:
    file_lines = text.split("\n")
    old_lines = old.split("\n")
    stripped = [line.strip() for line in file_lines]
    wanted = [line.strip() for line in old_lines]
    first = next((index for index, line in enumerate(wanted) if line), None)
    if first is None:
        return "old_str contains only whitespace."
    best: tuple[int, int] | None = None
    for position, line in enumerate(stripped):
        if line != wanted[first] or position < first:
            continue
        start = position - first
        run = 0
        while run < len(wanted) and start + run < len(stripped) and stripped[start + run] == wanted[run]:
            run += 1
        if best is None or run > best[0]:
            best = (run, start)
    if best:
        run, start = best
        if run == len(wanted):
            return (
                f"The text matches lines {start + 1}-{start + run} only when leading/trailing whitespace is ignored, "
                "so the indentation or spacing differs. Copy the exact text from read_file."
            )
        at = start + run
        actual = file_lines[at] if at < len(file_lines) else "<end of file>"
        return (
            f"The first lines of old_str match at line {start + 1}, but line {run + 1} of old_str differs from line {at + 1} of the file.\n"
            f"  old_str: {_cut_text(old_lines[run])}\n  file:    {_cut_text(actual)}"
        )
    pool = [line for line in dict.fromkeys(stripped[:20_000]) if line]
    close = difflib.get_close_matches(wanted[first], pool, n=1, cutoff=0.6)
    if close:
        number = stripped.index(close[0]) + 1
        return f"The closest line in the file is line {number}: {_cut_text(file_lines[number - 1])}"
    return "No similar line was found; the file may have changed. Read it again with read_file."


def _plan_edit(ctx: ToolContext, args: dict[str, Any]) -> _EditPlan:
    path = _target(ctx, args["file_path"])
    old_str: str = args["old_str"].replace("\r\n", "\n")
    new_str: str = args["new_str"].replace("\r\n", "\n")
    change_all = bool(args.get("change_all", False))
    if old_str == new_str:
        raise ToolError("old_str and new_str are identical; there is nothing to change.")
    if not old_str:
        raise ToolError("old_str is empty. To add text, include the surrounding lines in old_str; to create a file, use create_file.")
    try:
        info = path.stat()
    except FileNotFoundError:
        raise ToolError(_not_found("File", path) + " To create a new file use create_file.") from None
    except OSError as error:
        raise _io_error("access", path, error) from None
    if stat.S_ISDIR(info.st_mode):
        raise ToolError(f"{path} is a folder, not a file.")
    if info.st_size > MAX_READ_BYTES:
        raise ToolError(f"{path} is {_human_size(info.st_size)}; files over {_human_size(MAX_READ_BYTES)} cannot be edited.")
    try:
        data = path.read_bytes()
    except OSError as error:
        raise _io_error("read", path, error) from None
    if _is_binary(data):
        raise ToolError(f"{path} looks like a binary file (it contains NUL bytes) and cannot be edited as text.")
    decoded = _decode(data)
    if decoded.lossy:
        raise ToolError(f"{path} is not valid UTF-8. Editing it would destroy the bytes that cannot be decoded; edit it with a shell command instead.")
    crlf_count = decoded.text.count("\r\n")
    lf_count = decoded.text.count("\n") - crlf_count
    text = decoded.text.replace("\r\n", "\n")
    count = text.count(old_str)
    if count == 0:
        raise ToolError(f"old_str was not found in {path}. {_mismatch_hint(text, old_str)}")
    if count > 1 and not change_all:
        positions, cursor = [], 0
        for _ in range(min(count, 5)):
            cursor = text.index(old_str, cursor)
            positions.append(str(_line_of(text, cursor)))
            cursor += len(old_str)
        more = ", ..." if count > 5 else ""
        raise ToolError(
            f"old_str appears {count} times in {path} (lines {', '.join(positions)}{more}). "
            "Include more surrounding lines to make it unique, or set change_all to true to replace every occurrence."
        )
    after = text.replace(old_str, new_str) if change_all else text.replace(old_str, new_str, 1)
    return _EditPlan(
        path, text, after, count if change_all else 1, decoded.codec, decoded.bom, crlf_count > lf_count, crlf_count > 0 and lf_count > 0
    )


def edit_file(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    with _one_writer(_target(ctx, args["file_path"])):
        plan = _plan_edit(ctx, args)
        out = plan.after.replace("\n", "\r\n") if plan.crlf else plan.after
        try:
            data = plan.bom + out.encode(plan.codec)
        except UnicodeEncodeError:
            raise ToolError("new_str has characters that cannot be written back in the file's encoding.") from None
        try:
            _write(plan.path, data, ctx)
        except OSError as error:
            raise _io_error("write", plan.path, error) from None
    diff = diff_from_contents(str(plan.path), plan.before, plan.after)
    note = " The file mixed line endings; all lines now use " + ("CRLF." if plan.crlf else "LF.") if plan.mixed else ""
    plural = "s" if plan.count != 1 else ""
    return ToolResult(f"Edited {plan.path}: replaced {plan.count} occurrence{plural} (+{diff.added} -{diff.removed} lines).{note}", diffs=(diff,))


def preview_diff(ctx: ToolContext, spec: ToolSpec, args: Any) -> FileDiff | None:
    """The diff a create_file / edit_file call would make against the disk as it is now; None when it cannot apply."""
    try:
        clean = validate_arguments(spec.parameters, args)
        if spec.name == "edit_file":
            plan = _plan_edit(ctx, clean)
            return diff_from_contents(str(plan.path), plan.before, plan.after)
        if spec.name == "create_file":
            path = _target(ctx, clean["file_path"])
            if path.is_file():
                return diff_from_contents(str(path), _existing_text(path), clean["content"])
            return diff_for_new_file(str(path), clean["content"])
    except (ToolError, OSError):
        return None
    return None


# --------------------------------------------------------------------------- specs

_PATH_NOTE = "Absolute, or relative to the project folder."

READ_FILE = ToolSpec(
    name="read_file",
    description=(
        "Read a text file and return its lines numbered like `{line number}<TAB>{text}`. "
        f"{_PATH_NOTE} Returns up to 2000 lines from the start unless offset/limit are given; use them to page through "
        "a long file (the result says how to continue). Lines longer than 2000 characters are cut. UTF-8 and UTF-16 "
        "(with BOM) are read. Binary files are refused, and so are files over 5 MB unless offset and limit narrow the read. "
        "To look at a folder use list_dir."
    ),
    parameters={
        "type": "object",
        "properties": {
            "file_path": {"type": "string", "description": f"File to read. {_PATH_NOTE}"},
            "offset": {"type": "integer", "minimum": 1, "description": "1-based line number to start at (default 1)."},
            "limit": {"type": "integer", "minimum": 1, "description": "Maximum number of lines to return (default 2000)."},
        },
        "required": ["file_path"],
    },
    ui_name="Read",
    kind="read",
)

LIST_DIR = ToolSpec(
    name="list_dir",
    description=(
        "List the entries of one folder (not recursive): folders first with a trailing `/`, then files with their sizes. "
        f"Shows at most {MAX_LIST_ENTRIES} entries and hides nothing else. {_PATH_NOTE} To search by name use glob."
    ),
    parameters={
        "type": "object",
        "properties": {"directory_path": {"type": "string", "description": f"Folder to list. {_PATH_NOTE}"}},
        "required": ["directory_path"],
    },
    ui_name="LS",
    kind="read",
)

GLOB = ToolSpec(
    name="glob",
    description=(
        "Find files and folders by glob pattern, e.g. `src/**/*.py` or `*.{ts,tsx}`: `*` and `?` match within one path "
        "segment, `**` matches any number of folders, braces list alternatives. Patterns are relative to `folder` "
        f"(default: the project folder). Returns paths relative to that folder, newest first, at most {MAX_GLOB_RESULTS}. "
        "Skips .git, node_modules, __pycache__, .venv, venv, .mypy_cache, .pytest_cache, dist and build unless the "
        "pattern names them."
    ),
    parameters={
        "type": "object",
        "properties": {
            "patterns": {"type": "array", "items": {"type": "string"}, "minItems": 1, "description": "One or more glob patterns."},
            "folder": {"type": "string", "description": f"Folder to search in (default: the project folder). {_PATH_NOTE}"},
        },
        "required": ["patterns"],
    },
    ui_name="Glob",
    kind="read",
)

GREP = ToolSpec(
    name="grep",
    description=(
        "Search file contents line by line with a Python regular expression (`re` syntax). `path` is a file or folder "
        "(default: the project folder); `glob_pattern` limits which files are searched, e.g. `*.py` or `src/**/*.ts`. "
        "output_mode `file_paths` (default) lists the files that match; `content` prints `path:line:text` "
        "(context lines as `path-line-text`, groups separated by `--`). Skips binary files, files over 2 MB and the "
        "folders glob skips. Output is limited to head_limit lines (default 250). Paths are relative to the project folder."
    ),
    parameters={
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "Regular expression to search for."},
            "path": {"type": "string", "description": f"File or folder to search (default: the project folder). {_PATH_NOTE}"},
            "glob_pattern": {"type": "string", "description": "Only search files whose path matches this glob."},
            "case_insensitive": {"type": "boolean", "description": "Ignore case (default false)."},
            "output_mode": {"type": "string", "enum": ["file_paths", "content"], "description": "What to return (default file_paths)."},
            "context": {"type": "integer", "minimum": 0, "maximum": 20, "description": "Lines of context around each match in content mode (default 0)."},
            "head_limit": {"type": "integer", "minimum": 1, "maximum": 5000, "description": "Maximum files (file_paths) or lines (content) to return (default 250)."},
        },
        "required": ["pattern"],
    },
    ui_name="Grep",
    kind="read",
)

CREATE_FILE = ToolSpec(
    name="create_file",
    description=(
        "Create a file with the given content, or replace the whole content if it already exists. Missing parent folders are "
        "created. Written as UTF-8 without BOM; line endings are written exactly as given. To change part of an existing "
        "file use edit_file instead."
    ),
    parameters={
        "type": "object",
        "properties": {
            "file_path": {"type": "string", "description": f"File to write. {_PATH_NOTE}"},
            "content": {"type": "string", "description": "The complete new content of the file."},
        },
        "required": ["file_path", "content"],
    },
    ui_name="Create",
    kind="edit",
)

EDIT_FILE = ToolSpec(
    name="edit_file",
    description=(
        "Replace text in an existing file. old_str must match the file exactly, including indentation, and must be unique "
        "unless change_all is true; include enough surrounding lines to make it unique. The file's line endings (LF or "
        "CRLF), BOM and encoding are preserved, and old_str/new_str may use plain newlines. Read the file first and copy "
        "the text from the result. On a mismatch the error shows the closest line."
    ),
    parameters={
        "type": "object",
        "properties": {
            "file_path": {"type": "string", "description": f"File to edit. {_PATH_NOTE}"},
            "old_str": {"type": "string", "description": "Exact text to replace (not empty)."},
            "new_str": {"type": "string", "description": "Replacement text (may be empty to delete)."},
            "change_all": {"type": "boolean", "description": "Replace every occurrence instead of requiring a unique one (default false)."},
        },
        "required": ["file_path", "old_str", "new_str"],
    },
    ui_name="Edit",
    kind="edit",
)


def register(registry: ToolRegistry) -> None:
    keep = lambda *keys: passthrough_input({key: key for key in keys})  # noqa: E731
    registry.register(READ_FILE, read_file, ui_input=keep("file_path", "offset", "limit"))
    registry.register(LIST_DIR, list_dir, ui_input=keep("directory_path"))
    registry.register(GLOB, glob, ui_input=keep("patterns", "folder"))
    registry.register(
        GREP,
        grep,
        ui_input=keep("pattern", "path", "glob_pattern", "case_insensitive", "output_mode", "context", "head_limit"),
    )
    registry.register(CREATE_FILE, create_file, ui_input=keep("file_path", "content"))
    registry.register(EDIT_FILE, edit_file, ui_input=keep("file_path", "old_str", "new_str", "change_all"))
