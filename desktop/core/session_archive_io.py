"""Verified filesystem primitives for the session archive (``desktop.core.session_archive``).

No archive concepts live here: this module only answers "is this path legal and really mine" and "did this write
become durable as one piece". Every name is checked segment by segment (Windows reserved names and illegal
characters, no trailing dots or spaces), every existing component of a path must be a real file or directory
(links are refused through both ``lstat`` and the Windows reparse-point attribute), and the final absolute path
is length-checked with the temporary name included, since a short id alone does not guarantee the limit.

Writes are atomic: a temporary file in the same directory, ``fsync``, then ``os.replace``; a crash leaves the
old file or the new one. Appends to event logs are followed by ``fsync``. New ids are claimed with ``os.mkdir``,
which occupies the name atomically; a name that differs only in case from an existing sibling counts as a
collision (Windows would refuse it) and the generator retries instead of overwriting.

Pure: no Qt.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
import secrets
import sqlite3
import stat
import tempfile
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path, PureWindowsPath
from typing import Any


class ArchiveIOError(OSError):
    """An archive path or write failed its checks."""


class PathRejected(ArchiveIOError, ValueError):
    """The path is not legal archive storage (link, reserved name, illegal segment, overlong)."""


ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*\Z")
RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
MAX_PATH = 260
"""The Windows path limit the archive enforces on the final absolute path (temp name included)."""
SQLITE_TIMEOUT = 10.0


def is_link(path: Path) -> bool:
    """True for symlinks and Windows reparse points (junctions); False for missing or ordinary entries."""
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT)


def reject_link(path: Path) -> None:
    if is_link(path):
        raise PathRejected(f"Archive links are not allowed: {path}")


def check_segment(name: str) -> str:
    """One path segment of an archive name. Only our own ids and fixed names pass; caller text never becomes a segment."""
    if (not isinstance(name, str) or SEGMENT.fullmatch(name) is None or name.endswith((".", " "))
            or name.split(".")[0].upper() in RESERVED or len(name.encode("utf-8")) > 120):
        raise PathRejected(f"Illegal archive path segment: {name!r}")
    return name


def check_final_path(path: Path, *, extra: int = 0) -> None:
    """The final absolute path including the longest temporary name must stay writable. ``extra`` covers a suffix
    longer than the final name (e.g. a ``.tmp`` staging file in the same directory)."""
    resolved = os.path.normcase(str(Path(os.path.abspath(path))))
    if len(resolved) + extra >= MAX_PATH:
        raise PathRejected(f"Archive path exceeds the {MAX_PATH} character limit: {resolved}")


def descend(root: Path, *names: str, must_exist: bool = False) -> Path:
    """Join checked segments and refuse a link at every existing component, root included."""
    reject_link(root)
    path = root
    for name in names:
        path = path / check_segment(name)
        reject_link(path)
    if must_exist and not path.is_dir():
        raise ArchiveIOError(f"Not an archive directory: {path}")
    return path


def reserve_name(parent: Path, length: int, *, source: Callable[[], str] | None = None, attempts: int = 64) -> str:
    """Claim a new random child directory of ``parent`` and return its name.

    ``os.mkdir`` is the atomic occupation: two concurrent reservers never split a name. A candidate that differs
    only in case from an existing entry counts as occupied (Windows would conflate them); the next candidate is
    tried instead of overwriting. ``source`` is injectable for tests; the default uses ``secrets``."""
    existing = {entry.name.casefold() for entry in parent.iterdir()} if parent.is_dir() else set()
    generate = source or (lambda: "".join(secrets.choice(ALPHABET) for _ in range(length)))
    for _ in range(attempts):
        name = generate()
        if not isinstance(name, str) or len(name) != length or any(char not in ALPHABET for char in name):
            raise PathRejected(f"Illegal generated archive name: {name!r}")
        if name.casefold() in existing:
            continue
        try:
            (parent / name).mkdir()
        except FileExistsError:
            existing.add(name.casefold())
            continue
        existing.add(name.casefold())
        check_final_path(parent / name)
        return name
    raise ArchiveIOError(f"Could not claim a unique name under {parent} after {attempts} attempts.")


def _rejected_existing(path: Path) -> None:
    if is_link(path):
        raise PathRejected(f"Refusing to write through a link: {path}")


def atomic_write(path: Path, data: bytes, *, temp_suffix: str = ".tmp") -> Path:
    """Replace ``path`` with ``data`` as one durable step; returns the temp path used."""
    reject_link(path.parent)
    _rejected_existing(path)
    fd, name = tempfile.mkstemp(dir=path.parent, suffix=temp_suffix)
    temp = Path(name)
    check_final_path(temp)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        reject_link(path)
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)
    return temp


def atomic_write_text(path: Path, text: str) -> None:
    atomic_write(path, text.encode("utf-8"))


def append_bytes(path: Path, data: bytes) -> None:
    """Append to a log file and flush to disk. The file must be a regular file, never a link."""
    reject_link(path.parent)
    if path.exists() and (is_link(path) or not path.is_file()):
        raise PathRejected(f"Refusing to append to a non-file: {path}")
    with path.open("ab") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def append_line(path: Path, line: str) -> None:
    if "\n" in line or "\r" in line:
        raise ValueError("An archive line must be a single line.")
    append_bytes(path, line.encode("utf-8") + b"\n")


def read_json(path: Path) -> Any:
    reject_link(path)
    with path.open("rb") as stream:
        data = stream.read(64 * 1024 * 1024 + 1)
    if len(data) > 64 * 1024 * 1024:
        raise ArchiveIOError(f"Archive file is too large to load: {path}")
    return json.loads(data.decode("utf-8"))


def read_lines(path: Path, *, limit: int = 64 * 1024 * 1024) -> list[bytes]:
    reject_link(path)
    lines: list[bytes] = []
    with path.open("rb") as stream:
        for raw in stream:
            lines.append(raw.rstrip(b"\r\n"))
            if sum(map(len, lines)) > limit:
                raise ArchiveIOError(f"Archive file is too large to load: {path}")
    return lines


def last_line(path: Path, *, window: int = 1 << 20) -> bytes:
    """The last non-empty line of a log file, reading only the tail."""
    reject_link(path)
    with path.open("rb") as stream:
        stream.seek(0, os.SEEK_END)
        size = stream.tell()
        stream.seek(max(0, size - window))
        tail = stream.read()
    lines = [line for line in tail.split(b"\n") if line.strip()]
    return lines[-1].rstrip(b"\r") if lines else b""


def sha256_file(path: Path, *, limit: int = 256 * 1024 * 1024) -> str:
    reject_link(path)
    digest = hashlib.sha256()
    total = 0
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            total += len(block)
            if total > limit:
                raise ArchiveIOError(f"Archive file is too large to verify: {path}")
            digest.update(block)
    return digest.hexdigest()


def gzip_write(path: Path, lines: list[bytes]) -> None:
    """Write one gzip file containing one value per line. Used for a dump's staging file, which the caller
    verifies before publishing; not atomic on its own."""
    reject_link(path.parent)
    with path.open("wb") as raw:
        with gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as stream:
            for line in lines:
                stream.write(line.rstrip(b"\r\n") + b"\n")
        raw.flush()
        os.fsync(raw.fileno())


def gzip_read_lines(path: Path, *, limit: int = 256 * 1024 * 1024) -> list[bytes]:
    reject_link(path)
    lines: list[bytes] = []
    with path.open("rb") as raw:
        with gzip.GzipFile(fileobj=raw, mode="rb") as stream:
            for raw_line in stream:
                lines.append(raw_line.rstrip(b"\r\n"))
                if sum(map(len, lines)) > limit:
                    raise ArchiveIOError(f"Archive dump is too large to read: {path}")
    return lines


INDEX_NAME = "index.sqlite"
"""The shared locator at the archive root; its WAL/SHM sidecars share the name's prefix."""

_INDEX_SCHEMA = """
CREATE TABLE IF NOT EXISTS identities (
    sid TEXT NOT NULL,
    backend TEXT NOT NULL,
    iid TEXT NOT NULL,
    connection TEXT NOT NULL,
    native_id TEXT NOT NULL,
    PRIMARY KEY (sid, backend, iid)
);
CREATE UNIQUE INDEX IF NOT EXISTS identities_native ON identities(connection, native_id);
CREATE TABLE IF NOT EXISTS locations (
    sid TEXT NOT NULL,
    record_id TEXT NOT NULL,
    generation INTEGER,
    backend TEXT NOT NULL,
    iid TEXT NOT NULL,
    file TEXT NOT NULL,
    pos INTEGER NOT NULL,
    PRIMARY KEY (sid, record_id)
);
"""


class Index:
    """The shared ``index.sqlite`` at the archive root: a rebuildable cache, never the only copy of anything."""

    def __init__(self, path: Path):
        self.path = path
        self._initialize()

    def _initialize(self) -> None:
        deadline = time.monotonic() + SQLITE_TIMEOUT
        last_lock: sqlite3.Error | None = None
        while (remaining := deadline - time.monotonic()) > 0:
            try:
                with self._connect(timeout=min(0.1, remaining)) as db:
                    if (db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
                            or db.execute("PRAGMA journal_mode = WAL").fetchone()[0] == "wal"):
                        db.executescript("BEGIN IMMEDIATE;\n" + _INDEX_SCHEMA + "\nCOMMIT;")
                        return
            except sqlite3.OperationalError as error:
                if (error.sqlite_errorcode & 0xFF) not in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
                    raise
                last_lock = error
            delay = min(0.02, deadline - time.monotonic())
            if delay > 0:
                time.sleep(delay)
        raise ArchiveIOError(f"Could not initialize the archive index: {self.path}") from last_lock

    @contextmanager
    def _connect(self, *, timeout: float = SQLITE_TIMEOUT) -> Iterator[sqlite3.Connection]:
        reject_link(self.path.parent)
        reject_link(self.path)
        db = sqlite3.connect(self.path, timeout=timeout, isolation_level=None)
        try:
            db.row_factory = sqlite3.Row
            yield db
        finally:
            db.close()

    @contextmanager
    def write(self) -> Iterator[sqlite3.Connection]:
        """One serialized writer transaction; concurrent writers queue on the SQLite lock."""
        with self._connect() as db, db:
            db.execute("BEGIN IMMEDIATE")
            yield db

    def query(self, sql: str, args: tuple = ()) -> list[dict]:
        with self._connect() as db:
            return [dict(row) for row in db.execute(sql, args)]
