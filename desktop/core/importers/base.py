"""What the importers share: the shapes they hand to the application, and the small helpers every source uses.

A source reads another app's own files, read-only; nothing here writes anywhere. Files change format without notice, so a
line, a record or a file that cannot be read is skipped (and counted in ``Report``), never fatal; only ``Source.read``
raises, ``ImportFailed``, when the whole conversation cannot be read (its file is gone, its database is locked).
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import math
import os
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from desktop.core.importers import zstd
from desktop.core.memory.safety import find_secret
from desktop.core.memory.store import MAX_TAG, MAX_TAGS, MAX_TEXT, MAX_TITLE

APPS = ("claude", "codex", "devin", "factory")
LABELS = {"claude": "Claude Code", "codex": "Codex", "devin": "Devin", "factory": "Factory Droid"}
KINDS = ("preference", "convention", "pitfall", "fact")
TOOL_PREFIX = "[工具]"
TOOL_SUBJECT = 120
"""Characters of a tool call's subject (its command, its path) kept on its summary line."""
TEXT_FILE_LIMIT = 1024 * 1024
"""A memory file larger than this is not read (it is not a note file; a CLAUDE.md that size would not even load)."""

__all__ = [
    "APPS", "LABELS", "Builder", "ImportFailed", "MemoryCandidate", "Message", "Report", "SessionInfo", "Source", "session_key",
    "saved_id", "text_digest",
]


@dataclass(frozen=True, slots=True)
class MemoryCandidate:
    """A note that could be imported into the shared memory, as the preview shows it."""

    key: str
    """Stable: the same file, heading and part give the same key."""
    app: str
    title: str
    text: str
    scope: str
    """``global`` | ``project``."""
    project: str
    """The project's folder for scope ``project``; empty otherwise."""
    kind: str
    tags: tuple[str, ...]
    origin: str
    """``<file> › <heading> › …``, for people."""
    digest: str
    """``text_digest(text)``: to tell a note the memory already has."""


@dataclass(frozen=True, slots=True)
class SessionInfo:
    """One conversation another app keeps, as the import list shows it."""

    key: str
    """``session_key(app, native_id)``."""
    app: str
    native_id: str
    title: str
    project: str
    """The folder the conversation ran in (empty: unknown)."""
    started: float
    updated: float
    messages: int
    """How many messages the import gives (``Source.read``), exactly when ``exact``; otherwise how many the part of the
    file that was read holds (a big file is read at its start and end only), so at least that many."""
    size: int
    """Bytes of the file it is read from (0: one database holds every conversation of the app)."""
    path: str
    exact: bool = True


@dataclass(frozen=True, slots=True)
class Message:
    role: str
    """``user`` | ``assistant``."""
    text: str
    """What was said; the assistant's tool calls are lines ``[工具] <name> <subject>`` in it."""
    at: float | None = None


@dataclass(slots=True)
class Report:
    """What the last call of a source left out and why (counts only, never content)."""

    secret_lines: int = 0
    """Lines dropped because they looked like a secret (a key, a token, a password)."""
    bad_records: int = 0
    """Lines or records that could not be read (not JSON, an unknown shape)."""
    skipped: int = 0
    """Files or conversations left out: unreadable, too large, not notes, a sub-agent's, empty."""
    problems: list[str] = field(default_factory=list)
    """Sentences for the user about what could not be read at all (a locked database)."""


class ImportFailed(Exception):
    """A conversation that cannot be read at all. ``message`` is a sentence for the user."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class Source(Protocol):
    app: str
    label: str
    report: Report

    def available(self) -> bool:
        """Whether the app's folder is there."""

    def memories(self) -> list[MemoryCandidate]: ...

    def sessions(self) -> list[SessionInfo]:
        """Newest first."""

    def read(self, info: SessionInfo) -> list[Message]: ...


def session_key(app: str, native_id: str) -> str:
    return f"{app}:{native_id}"


def saved_id(key: str) -> str:
    """The id of the imported conversation in the harness store (``harness.store`` ids are ``[A-Za-z0-9_-]{1,64}``):
    the same key always gives the same id, so a second import of a conversation finds the first."""
    app = re.sub(r"[^a-z]", "", key.partition(":")[0].lower())[:12] or "app"
    return f"im-{app}-{hashlib.sha1(key.encode('utf-8')).hexdigest()[:28]}"


def text_digest(text: str) -> str:
    """The same for texts that differ only in spacing and case."""
    return hashlib.sha1(re.sub(r"\s+", " ", text).strip().lower().encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------------------------- text


def clip(text: str, limit: int) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def one_line(text: str, limit: int = TOOL_SUBJECT) -> str:
    return clip(re.sub(r"\s+", " ", text), limit)


_PRIVATE_KEY_END = re.compile(r"-----END [A-Z ]*PRIVATE KEY-----")


def scrub(text: str, report: Report | None) -> str:
    """``text`` without the lines that look like a secret (``memory.safety``); a private key goes from its BEGIN line to
    its END line."""
    if not find_secret(text) and "PRIVATE KEY-----" not in text:
        return text
    kept: list[str] = []
    dropped = 0
    in_key = False
    for line in text.split("\n"):
        if in_key:
            dropped += 1
            if _PRIVATE_KEY_END.search(line):
                in_key = False
            continue
        if find_secret(line):
            dropped += 1
            if "PRIVATE KEY-----" in line and not _PRIVATE_KEY_END.search(line):
                in_key = True
            continue
        kept.append(line)
    if report is not None:
        report.secret_lines += dropped
    return "\n".join(kept)


def tool_line(name: str, subject: str = "") -> str:
    subject = one_line(subject) if subject else ""
    return f"{TOOL_PREFIX} {one_line(name, 60) or '?'}" + (f" {subject}" if subject else "")


_SUBJECT_KEYS = ("file_path", "filePath", "notebook_path", "command", "cmd", "pattern", "query", "url", "path", "description", "prompt", "message", "task_name", "name")


def subject_of(arguments: Any) -> str:
    """The part of a tool call's arguments that says what it was about: a path, a command, a pattern."""
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments) if arguments.strip().startswith("{") else arguments
        except ValueError:
            pass
        if isinstance(arguments, str):
            return first_line(arguments)
    if not isinstance(arguments, dict):
        return ""
    for key in _SUBJECT_KEYS:
        value = arguments.get(key)
        if isinstance(value, str) and value.strip():
            return first_line(value)
        if isinstance(value, list) and value and all(isinstance(v, str) for v in value):
            return " ".join(value)
    for value in arguments.values():
        if isinstance(value, str) and value.strip():
            return first_line(value)
    return ""


def first_line(text: str) -> str:
    """The first line that says something (code comments and blank lines skipped)."""
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith(("//", "#!")):
            return line
    return text.strip()


# ---------------------------------------------------------------------------------------------- a conversation


class Builder:
    """The messages of a conversation as the import gives them: what the user said, what the assistant answered, and
    each tool call as one line (``tool_line``) at the end of the assistant's message that made it (a call that comes
    with no text of its own goes to the assistant's message before it, or starts one). Lines that look like a secret are
    dropped (``scrub``)."""

    def __init__(self, report: Report | None) -> None:
        self.report = report
        self.messages: list[Message] = []
        self._key: object = None

    def user(self, text: str, at: float | None) -> None:
        text = scrub(text, self.report).strip()
        if text:
            self.messages.append(Message("user", text, at))
            self._key = None

    def assistant(self, text: str, at: float | None, key: object = None) -> None:
        """``key``: the source's id of the message; text with the same key as the one before continues that message."""
        text = scrub(text, self.report).strip()
        if not text:
            return
        last = self.messages[-1] if self.messages else None
        if key is not None and key == self._key and last is not None and last.role == "assistant":
            self.messages[-1] = Message("assistant", f"{last.text}\n\n{text}", last.at)
        else:
            self.messages.append(Message("assistant", text, at))
        self._key = key

    def tool(self, name: str, subject: str, at: float | None, key: object = None) -> None:
        line = tool_line(name, subject)
        if find_secret(line):
            # (the call stays, with its name only: what it ran or read looked like it held a secret)
            if self.report is not None:
                self.report.secret_lines += 1
            line = tool_line(name)
            if find_secret(line):
                return
        last = self.messages[-1] if self.messages else None
        if last is not None and last.role == "assistant":
            self.messages[-1] = Message("assistant", f"{last.text}\n{line}", last.at)
        else:
            self.messages.append(Message("assistant", line, at))
        if key is not None:
            self._key = key


# ---------------------------------------------------------------------------------------------- time


_ISO = re.compile(r"^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})(?::(\d{2})(?:[.,](\d+))?)?\s*(Z|[+-]\d{2}:?\d{2})?$", re.IGNORECASE)


def parse_time(value: Any) -> float | None:
    """Seconds since the epoch from an ISO 8601 string (naive: UTC) or a number of seconds, milliseconds or microseconds."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        if not math.isfinite(number) or number <= 0:
            return None
        while number > 1e11:
            number /= 1000.0
        return number
    if not isinstance(value, str):
        return None
    found = _ISO.match(value.strip())
    if not found:
        return None
    year, month, day, hour, minute, second, fraction, zone = found.groups()
    offset = _dt.timedelta(0)
    if zone and zone.upper() != "Z":
        sign = 1 if zone[0] == "+" else -1
        digits = zone[1:].replace(":", "")
        offset = sign * _dt.timedelta(hours=int(digits[:2]), minutes=int(digits[2:4] or 0))
    try:
        moment = _dt.datetime(int(year), int(month), int(day), int(hour), int(minute), int(second or 0), int((fraction or "0")[:6].ljust(6, "0")))
        return (moment.replace(tzinfo=_dt.timezone.utc) - offset).timestamp()
    except (ValueError, OverflowError):
        return None


# ---------------------------------------------------------------------------------------------- files


def read_text(path: Path, report: Report | None = None, limit: int = TEXT_FILE_LIMIT) -> str | None:
    """A text file's content (``None``: not there, unreadable or too large; counted as skipped unless not there)."""
    try:
        if not path.is_file():
            return None
        if path.stat().st_size > limit:
            if report is not None:
                report.skipped += 1
            return None
        data = path.read_bytes()
    except OSError:
        if report is not None:
            report.skipped += 1
        return None
    if data.startswith(b"\xef\xbb\xbf"):
        data = data[3:]
    return data.decode("utf-8", errors="replace").replace("\r\n", "\n").replace("\r", "\n")


def is_compressed(path: Path) -> bool:
    return path.name.endswith(".zst")


def lines(path: Path, *, limit: int | None = None) -> Iterator[bytes]:
    """The non-empty lines of a JSON-lines file (without their line ends), plain or zstd-compressed (``.zst``), from the
    start; with ``limit``, those that begin in its first ``limit`` bytes (decompressed). Raises ``OSError`` (and
    ``zstd.ZstdError``) for a file that cannot be read at all."""
    if is_compressed(path):
        yield from _compressed_lines(path, limit)
        return
    seen = 0
    with open(path, "rb") as stream:
        for line in stream:
            if limit is not None and seen >= limit:
                return
            seen += len(line)
            line = line.rstrip(b"\r\n")
            if line.strip():
                yield line


def _compressed_lines(path: Path, limit: int | None) -> Iterator[bytes]:
    carry = b""
    seen = 0
    with open(path, "rb") as stream:
        for chunk in zstd.chunks(stream):
            parts = (carry + chunk).split(b"\n")
            carry = parts.pop()
            for line in parts:
                if limit is not None and seen >= limit:
                    return
                seen += len(line) + 1
                line = line.rstrip(b"\r")
                if line.strip():
                    yield line
    if carry.strip() and (limit is None or seen < limit):
        yield carry.rstrip(b"\r")


def tail_lines(path: Path, size: int) -> list[bytes]:
    """The whole lines in the last ``size`` bytes of a plain file (the first, cut, line left out unless the file is that small)."""
    with open(path, "rb") as stream:
        end = stream.seek(0, os.SEEK_END)
        start = max(0, end - size)
        stream.seek(start)
        data = stream.read(end - start)
    parts = data.split(b"\n")
    if start > 0:
        parts = parts[1:]
    return [part.rstrip(b"\r") for part in parts if part.strip()]


def records(raw_lines: Iterator[bytes] | list[bytes], report: Report | None) -> Iterator[dict[str, Any]]:
    """The lines that are JSON objects; the others are counted in ``report.bad_records``."""
    for line in raw_lines:
        try:
            record = json.loads(line)
        except (ValueError, RecursionError):
            if report is not None:
                report.bad_records += 1
            continue
        if isinstance(record, dict):
            yield record
        elif report is not None:
            report.bad_records += 1


def same_path(a: str | os.PathLike[str], b: str | os.PathLike[str]) -> bool:
    return os.path.normcase(os.path.normpath(str(a))) == os.path.normcase(os.path.normpath(str(b)))


def norm_key(path: str | os.PathLike[str]) -> str:
    return os.path.normcase(os.path.normpath(str(path)))


def mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def size_of(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def as_text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def sqlite_uri(path: Path) -> str:
    """A read-only connection's address. With a write-ahead log beside the file (the program that owns the database has
    it open, or did not close it cleanly), ``mode=ro``: read with the usual locks, so a writer at the same moment is no
    harm, and the data is what was last committed. Without one, everything is in the file itself, and ``immutable``
    reads it as it is, because ``mode=ro`` alone would create the empty ``-wal`` and ``-shm`` files of a WAL database
    next to it."""
    uri = path.resolve().as_uri()
    return uri + ("?mode=ro" if Path(f"{path}-wal").exists() else "?mode=ro&immutable=1")


def first_words(text: str, limit: int = MAX_TITLE) -> str:
    """A title from what someone said: its first line that says something."""
    for line in text.split("\n"):
        line = re.sub(r"\s+", " ", line).strip().lstrip("#>").strip()
        if line and not line.startswith(TOOL_PREFIX):
            return clip(line, limit)
    return ""
