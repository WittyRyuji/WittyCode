"""Versioned Fusion expert catalog and its safe hand-off to runtime identities.

The catalog is a small backend API for the expert management UI and for delegation setup::

    catalog = ExpertCatalog(controlled_folder)          # the caller chooses the folder; tests use tmp_path
    made = catalog.create(stable_id="login-coder", name="登录实现", work_type="execute",
                          tags="登录 windows 登录", markdown="---\ntools: read, edit, exec\n---\n实现登录。")
    listed = catalog.query(work_type="execute", tags="windows")   # metadata only: no Markdown body is read
    frozen = catalog.resolve(made.stable_id)                     # immutable in-memory hand-off
    same = catalog.resolve_version(made.stable_id, frozen.summary.version)
    runtime_identity = frozen.identity                           # the existing desktop.core.fusion.identity.Identity

Stable machine values for the required work type are ``WORK_TYPES``: execute, manage, review, research, verify
and observe. ``WORK_TYPE_LABELS`` carries their Chinese UI labels; storage and API keys never depend on locale.
Tags are whitespace-split and case-insensitively de-duplicated by ``split_tags`` while preserving the first spelling.

Lists, ``get``, ``query`` and ``snapshot`` read only the SQLite index. ``resolve`` and ``resolve_version`` are the
only operations that read a controlled Markdown body. Bodies live below the catalog folder as immutable
content-addressed files (``bodies/e-<sha256>.md``). Every create, update, enable/disable and delete appends a
version row; deleting is a tombstone, not removal of history. A runtime that freezes a delegation can either keep
the returned ``ResolvedExpert`` object or later call ``resolve_version`` with its stable id and version: editing,
disabling or deleting the current entry cannot rewrite or remove that version. A deletion tombstone itself has no
runnable expert and is not resolved.

The API never accepts a caller-supplied body path. Paths shown in summaries are catalog-relative evidence, not
write locations. The Markdown header may tighten the work type's permission ceiling but cannot raise it; Observer
uses the same read-only ceiling as the built-in identity, and the built-in identities are represented read-only so
old sessions can still resolve names such as ``Coder`` exactly as before. The dispatch layer must use ASCII expert
references of at most 180 bytes (for example ``expert:<stable-id>@v<version>``); the write-time P1 limit reserves
that much reference space plus worst-case first-pack framing.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import stat
import tempfile
import time
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path

from desktop.core.fusion.identity import Identity, builtin_markdown, load, parse_controlled, split_front_matter
from desktop.core.fusion.inject import (
    FIRST_PACK_SAFETY,
    FIRST_PACK_SECTION_TOKENS,
    FIRST_PACK_TOKENS,
    utf8_token_upper_bound,
)
from desktop.core.harness.compact import estimate_text

WORK_TYPES = ("execute", "manage", "review", "research", "verify", "observe")
WORK_TYPE_LABELS = {
    "execute": "执行",
    "manage": "管理",
    "review": "审核",
    "research": "信息查找",
    "verify": "运行验证",
    "observe": "观察",
}
WORK_TYPE_CEILING = {
    "execute": "Coder",
    "manage": "Coder",
    "review": "QualitySampler",
    "research": "Runner",
    "verify": "Runner",
    "observe": "Observer",
}
P1_AND_HEAD_LIMIT = FIRST_PACK_TOKENS - sum(FIRST_PACK_SECTION_TOKENS.values())
"""The approved share of the 8192 first-pack budget available to P1 plus packet head and framing (4352)."""
MAX_EXPERT_REF_BYTES = 180
"""A conservative ASCII expert-reference allowance; catalog-generated references are ASCII and well below this."""
_PACKET_HEAD_BYTES = utf8_token_upper_bound(f"@@FPACK v1\n{FIRST_PACK_SAFETY}")
_PACKET_FRAMING_BYTES = (utf8_token_upper_bound("@@END") + 10
                         + sum(utf8_token_upper_bound(f"@@P{number}\n") for number in range(1, 10)))
_P1_REF_BYTES = utf8_token_upper_bound("ref=\n") + MAX_EXPERT_REF_BYTES
MAX_P1_SERIALIZED_UTF8 = P1_AND_HEAD_LIMIT - (_PACKET_HEAD_BYTES + _PACKET_FRAMING_BYTES + _P1_REF_BYTES)
"""Maximum serialized P1 instructions cost in the fallback UTF-8 meter, with worst-case headroom reserved."""
MAX_MARKDOWN_BYTES = 64 * 1024
MAX_NAME_CHARS = 80
MAX_TAGS = 32
MAX_TAG_CHARS = 64
SCHEMA_VERSION = "1"

_STABLE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
_CONTENT_HASH = re.compile(r"sha256:[0-9a-f]{64}")
_TIMEOUT = 10.0
_BUILTIN_WORK_TYPES = {"Coder": "execute", "Runner": "verify", "QualitySampler": "review", "Observer": "observe"}
_SCHEMA = """
CREATE TABLE IF NOT EXISTS experts (
    id TEXT PRIMARY KEY,
    id_key TEXT NOT NULL,
    name TEXT NOT NULL,
    name_key TEXT NOT NULL,
    work_type TEXT NOT NULL,
    tags TEXT NOT NULL,
    enabled INTEGER NOT NULL,
    version INTEGER NOT NULL,
    content_hash TEXT NOT NULL,
    md_path TEXT NOT NULL,
    body_tokens INTEGER NOT NULL,
    role TEXT NOT NULL,
    created REAL NOT NULL,
    updated REAL NOT NULL,
    deleted INTEGER NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS experts_id_key ON experts(id_key);
CREATE UNIQUE INDEX IF NOT EXISTS experts_active_name ON experts(name_key) WHERE deleted = 0;
CREATE INDEX IF NOT EXISTS experts_active_work_type ON experts(work_type) WHERE deleted = 0;
CREATE TABLE IF NOT EXISTS expert_tags (
    id TEXT NOT NULL,
    tag TEXT NOT NULL,
    tag_key TEXT NOT NULL,
    PRIMARY KEY (id, tag_key)
);
CREATE INDEX IF NOT EXISTS expert_tags_key ON expert_tags(tag_key, id);
CREATE TABLE IF NOT EXISTS versions (
    id TEXT NOT NULL,
    version INTEGER NOT NULL,
    name TEXT NOT NULL,
    work_type TEXT NOT NULL,
    tags TEXT NOT NULL,
    enabled INTEGER NOT NULL,
    content_hash TEXT NOT NULL,
    md_path TEXT NOT NULL,
    body_tokens INTEGER NOT NULL,
    role TEXT NOT NULL,
    created REAL NOT NULL,
    deleted INTEGER NOT NULL,
    PRIMARY KEY (id, version)
);
CREATE INDEX IF NOT EXISTS versions_content_hash ON versions(content_hash);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


class ExpertCatalogError(Exception):
    """The catalog cannot complete the requested operation."""


class UnknownExpertError(ExpertCatalogError):
    """No visible expert matches the stable id or legacy/display name."""


class ProtectedExpertError(ExpertCatalogError):
    """Built-in identities are readable for compatibility but cannot be changed through the catalog."""


class ExpertStorageError(ExpertCatalogError):
    """Controlled storage is corrupt, unsafe or externally tampered with."""


@dataclass(frozen=True, slots=True)
class ExpertSummary:
    """One lightweight indexed expert or immutable built-in; no Markdown body is held here."""

    stable_id: str
    name: str
    work_type: str
    tags: tuple[str, ...]
    enabled: bool
    version: int
    md_path: str
    content_hash: str
    body_tokens: int
    role: str
    created: float
    updated: float
    builtin: bool
    deleted: bool = False


@dataclass(frozen=True, slots=True)
class CatalogSnapshot:
    """A consistent lightweight index snapshot. ``revision`` increases once per successful mutation."""

    revision: int
    entries: tuple[ExpertSummary, ...]

    def get(self, key: str) -> ExpertSummary | None:
        wanted = _lookup_key(key)
        return next((entry for entry in self.entries
                     if entry.stable_id.casefold() == wanted or entry.name.casefold() == wanted), None)


@dataclass(frozen=True, slots=True)
class ResolvedExpert:
    """An immutable expert version, safe to keep while a delegation runs."""

    summary: ExpertSummary
    markdown: str
    identity: Identity

    @property
    def instructions(self) -> str:
        """The body text runtime injects into the initial package; ``markdown`` is kept for editing/audit."""
        return self.identity.instructions


@dataclass(frozen=True, slots=True)
class _Document:
    markdown: str
    data: bytes
    content_hash: str
    md_path: str
    body_tokens: int
    identity: Identity


def split_tags(tags: str | Iterable[str]) -> tuple[str, ...]:
    """Split tags on whitespace and de-duplicate case-insensitively, preserving the first spelling."""
    if isinstance(tags, str):
        pieces = tags.split()
    elif isinstance(tags, (bytes, bytearray)):
        raise ValueError("tags must be text or a sequence of text.")
    else:
        pieces = []
        try:
            items = iter(tags)
        except TypeError as error:
            raise ValueError("tags must be text or a sequence of text.") from error
        for item in items:
            if not isinstance(item, str):
                raise ValueError("tags must be text or a sequence of text.")
            pieces.extend(item.split())
    result: list[str] = []
    seen: set[str] = set()
    for tag in pieces:
        if len(tag) > MAX_TAG_CHARS or "\0" in tag:
            raise ValueError(f"tags must be at most {MAX_TAG_CHARS} characters without NUL.")
        key = tag.casefold()
        if key not in seen:
            seen.add(key)
            result.append(tag)
        if len(result) > MAX_TAGS:
            raise ValueError(f"at most {MAX_TAGS} tags are allowed.")
    return tuple(result)


def _stable_id(value: str) -> str:
    if not isinstance(value, str) or not _STABLE_ID.fullmatch(value):
        raise ValueError("stable_id must be 1-64 ASCII letters, digits, '_' or '-', starting with a letter or digit.")
    return value


def _name(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("name must be text.")
    name = value.strip()
    if not name or len(name) > MAX_NAME_CHARS or "\0" in name or "\r" in name or "\n" in name:
        raise ValueError(f"name must be one line of 1-{MAX_NAME_CHARS} characters.")
    return name


def _work_type(value: str) -> str:
    if value not in WORK_TYPES:
        raise ValueError("work_type must be one of: " + ", ".join(WORK_TYPES) + ".")
    return value


def _enabled(value: bool) -> bool:
    if not isinstance(value, bool):
        raise ValueError("enabled must be a boolean.")
    return value


def _lookup_key(value: str) -> str:
    return value.strip().casefold() if isinstance(value, str) else ""


def _ceiling(work_type: str) -> Identity:
    base = load(None).get(WORK_TYPE_CEILING[work_type])
    if base is None:  # Guarded by WORK_TYPES; a broken built-in is a programming error, not user input.
        raise ExpertStorageError("A built-in Fusion identity is missing.")
    return base


def _content_hash(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _body_file(content_hash: str) -> str:
    return "e-" + content_hash.removeprefix("sha256:") + ".md"


def _body_relpath(content_hash: str) -> str:
    return "bodies/" + _body_file(content_hash)


def _is_link(info: os.stat_result) -> bool:
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT)


def _reject_link(path: Path) -> None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    if _is_link(info):
        raise ExpertStorageError("Expert catalog storage links are not allowed.")


def _tags_json(tags: Sequence[str]) -> str:
    return json.dumps(list(tags), ensure_ascii=False, separators=(",", ":"))


def _row_summary(row: sqlite3.Row) -> ExpertSummary:
    return ExpertSummary(
        row["id"], row["name"], row["work_type"], tuple(json.loads(row["tags"])), bool(row["enabled"]),
        int(row["version"]), row["md_path"], row["content_hash"], int(row["body_tokens"]), row["role"],
        float(row["created"]), float(row["updated"]), False, bool(row["deleted"]),
    )


def _version_summary(row: sqlite3.Row) -> ExpertSummary:
    return ExpertSummary(
        row["id"], row["name"], row["work_type"], tuple(json.loads(row["tags"])), bool(row["enabled"]),
        int(row["version"]), row["md_path"], row["content_hash"], int(row["body_tokens"]), row["role"],
        float(row["created"]), float(row["created"]), False, bool(row["deleted"]),
    )


def _normalize_markdown(markdown: str) -> str:
    if not isinstance(markdown, str):
        raise ValueError("markdown must be text.")
    if markdown.startswith("\ufeff"):
        markdown = markdown[1:]
    return markdown.replace("\r\n", "\n").replace("\r", "\n")


def _p1_serialized_cost(instructions: str) -> int:
    """Fallback first-pack cost of instructions after the packet's one-byte-per-`@@`-line escaping."""
    return utf8_token_upper_bound(instructions) + sum(line.startswith("@@") for line in instructions.split("\n"))


class ExpertCatalog:
    """One controlled catalog folder. The constructor never picks or scans the real user settings folder."""

    def __init__(self, root: Path, *, clock=time.time) -> None:
        raw = Path(root).expanduser()
        try:
            _reject_link(raw)  # lstat also catches a broken symlink; exists() alone would miss it.
            if raw.exists() and not raw.is_dir():
                raise ExpertStorageError("Expert catalog root must be a directory.")
        except OSError as error:
            raise ExpertStorageError("Cannot inspect the expert catalog root.") from error
        self._clock = clock
        self._root = raw.resolve()
        self._database = self._root / "experts.sqlite"
        self._bodies = self._root / "bodies"
        try:
            self._root.mkdir(parents=True, exist_ok=True)
            _reject_link(self._root)
            _reject_link(self._bodies)
            if self._bodies.exists() and not self._bodies.is_dir():
                raise ExpertStorageError("Expert catalog bodies must be a directory.")
            self._bodies.mkdir(exist_ok=True)
        except OSError as error:
            raise ExpertStorageError("Cannot create the expert catalog folder.") from error
        self._initialize()

    def _initialize(self) -> None:
        deadline = time.monotonic() + _TIMEOUT
        last_lock = None
        while (remaining := deadline - time.monotonic()) > 0:
            try:
                with self._connect(timeout=min(0.1, remaining)) as db:
                    if (db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
                            or db.execute("PRAGMA journal_mode = WAL").fetchone()[0] == "wal"):
                        db.executescript("BEGIN IMMEDIATE;\n" + _SCHEMA + f"""
INSERT OR IGNORE INTO meta VALUES ('schema', '{SCHEMA_VERSION}');
INSERT OR IGNORE INTO meta VALUES ('revision', '0');
COMMIT;
""")
                        if db.execute("SELECT value FROM meta WHERE key = 'schema'").fetchone()[0] != SCHEMA_VERSION:
                            raise ExpertStorageError("Unsupported expert catalog schema version.")
                        return
            except sqlite3.OperationalError as error:
                if (error.sqlite_errorcode & 0xff) not in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
                    raise
                last_lock = error
            delay = min(0.02, deadline - time.monotonic())
            if delay > 0:
                time.sleep(delay)
        raise ExpertStorageError("Could not initialize the expert catalog index.") from last_lock

    @contextmanager
    def _connect(self, *, timeout: float = _TIMEOUT) -> Iterator[sqlite3.Connection]:
        _reject_link(self._root)
        for path in (self._database, self._database.with_name(self._database.name + "-wal"),
                     self._database.with_name(self._database.name + "-shm")):
            _reject_link(path)
        db = sqlite3.connect(self._database, timeout=timeout, isolation_level=None)
        try:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys = ON")
            yield db
        finally:
            db.close()

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        with self._connect() as db, db:
            # BEGIN IMMEDIATE is the cross-process writer lock; readers can still use the WAL snapshot.
            db.execute("BEGIN IMMEDIATE")
            yield db

    def _body_path(self, content_hash: str, md_path: str) -> Path:
        if not _CONTENT_HASH.fullmatch(content_hash) or md_path != _body_relpath(content_hash):
            raise ExpertStorageError("The controlled expert body path is invalid.")
        path = self._bodies / _body_file(content_hash)
        for part in (self._root, self._bodies, path):
            _reject_link(part)
        try:
            path.resolve(strict=False).relative_to(self._root)
        except (OSError, ValueError) as error:
            raise ExpertStorageError("The controlled expert body escapes the catalog.") from error
        return path

    def _read_body(self, content_hash: str, md_path: str) -> tuple[str, bytes]:
        path = self._body_path(content_hash, md_path)
        try:
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode):
                raise ExpertStorageError("The controlled expert body is missing.")
            with path.open("rb") as stream:
                data = stream.read(MAX_MARKDOWN_BYTES + 1)
        except FileNotFoundError as error:
            raise ExpertStorageError("The controlled expert body is missing.") from error
        except OSError as error:
            raise ExpertStorageError("Cannot read the controlled expert body.") from error
        if len(data) > MAX_MARKDOWN_BYTES:
            raise ExpertStorageError("The controlled expert body is too large.")
        if _content_hash(data) != content_hash:
            raise ExpertStorageError("The controlled expert body failed its integrity check.")
        try:
            return data.decode("utf-8"), data
        except UnicodeError as error:
            raise ExpertStorageError("The controlled expert body must be UTF-8.") from error

    def _write_body(self, document: _Document) -> None:
        path = self._body_path(document.content_hash, document.md_path)
        if path.exists():
            text, _ = self._read_body(document.content_hash, document.md_path)
            if text == document.markdown:
                return
            raise ExpertStorageError("An immutable expert body already has different content.")
        fd, temporary = tempfile.mkstemp(dir=self._bodies, prefix=".e-", suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(document.data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            Path(temporary).unlink(missing_ok=True)

    def _document(self, name: str, work_type: str, markdown: str) -> _Document:
        markdown = _normalize_markdown(markdown)
        if "\0" in markdown:
            raise ValueError("markdown must not contain NUL.")
        try:
            data = markdown.encode("utf-8")
        except UnicodeError as error:
            raise ValueError("markdown must be valid Unicode encodable as UTF-8.") from error
        if len(data) > MAX_MARKDOWN_BYTES:
            raise ValueError(f"markdown must not exceed {MAX_MARKDOWN_BYTES // 1024} KiB.")
        _, body = split_front_matter(markdown, require_name=False)
        if not body.strip():
            raise ValueError("expert Markdown body must not be empty.")
        body_tokens = estimate_text(body)
        identity = parse_controlled(markdown, name=name, ceiling=_ceiling(work_type))
        serialized_cost = _p1_serialized_cost(identity.instructions)
        if serialized_cost > MAX_P1_SERIALIZED_UTF8:
            excess = serialized_cost - MAX_P1_SERIALIZED_UTF8
            raise ValueError(
                "expert Markdown body cannot be dispatched in the first pack: "
                f"serialized P1 costs {serialized_cost} UTF-8 bytes (body text {utf8_token_upper_bound(body)}), "
                f"but the conservative limit is {MAX_P1_SERIALIZED_UTF8} bytes within the approved "
                f"{P1_AND_HEAD_LIMIT}-byte P1+header budget. Remove at least {excess} UTF-8 bytes "
                "or split the expert."
            )
        digest = _content_hash(data)
        return _Document(markdown, data, digest, _body_relpath(digest), body_tokens, identity)

    @staticmethod
    def _bump(db: sqlite3.Connection) -> None:
        db.execute("UPDATE meta SET value = CAST(value AS INTEGER) + 1 WHERE key = 'revision'")

    @staticmethod
    def _replace_tags(db: sqlite3.Connection, stable_id: str, tags: Sequence[str]) -> None:
        db.execute("DELETE FROM expert_tags WHERE id = ?", (stable_id,))
        db.executemany("INSERT INTO expert_tags VALUES (?, ?, ?)",
                       ((stable_id, tag, tag.casefold()) for tag in tags))

    @staticmethod
    def _insert_version(db: sqlite3.Connection, stable_id: str, version: int, name: str, work_type: str,
                        tags: Sequence[str], enabled: bool, content_hash: str, md_path: str, body_tokens: int,
                        role: str, created: float, deleted: bool) -> None:
        db.execute("INSERT INTO versions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (
            stable_id, version, name, work_type, _tags_json(tags), int(enabled), content_hash, md_path,
            body_tokens, role, created, int(deleted),
        ))

    @staticmethod
    def _builtin_keys() -> frozenset[str]:
        return frozenset(name.casefold() for name in load(None).names)

    def _check_namespace(self, db: sqlite3.Connection, stable_id: str, name: str, *, current: str | None = None) -> None:
        stable_key, name_key = stable_id.casefold(), name.casefold()
        if stable_key in self._builtin_keys() or name_key in self._builtin_keys():
            raise ProtectedExpertError("Built-in Fusion identities are protected.")
        row = db.execute("SELECT id FROM experts WHERE id_key = ?", (stable_key,)).fetchone()
        if row is not None and row["id"] != current:
            raise ValueError("stable_id is already reserved.")
        rows = db.execute("SELECT id, id_key, name_key FROM experts WHERE deleted = 0 AND id <> ?", (current or "",)).fetchall()
        for row in rows:
            keys = {row["id_key"], row["name_key"]}
            if stable_key in keys or name_key in keys:
                raise ValueError("stable_id and name must not collide with another expert's stable id or name.")

    def _find_row(self, db: sqlite3.Connection, key: str, *, include_deleted: bool = False) -> sqlite3.Row | None:
        wanted = _lookup_key(key)
        if not wanted:
            return None
        where = "" if include_deleted else "AND deleted = 0"
        row = db.execute(f"SELECT * FROM experts WHERE id_key = ? {where} ORDER BY deleted LIMIT 1", (wanted,)).fetchone()
        if row is not None:
            return row
        return db.execute(
            f"SELECT * FROM experts WHERE name_key = ? {where} ORDER BY deleted, updated DESC LIMIT 1", (wanted,)
        ).fetchone()

    @staticmethod
    def _builtin_summary(name: str) -> ExpertSummary:
        identity = load(None).get(name)
        markdown = builtin_markdown(name)
        if identity is None or markdown is None:
            raise ExpertStorageError("A built-in Fusion identity is missing.")
        data = markdown.encode("utf-8")
        digest = _content_hash(data)
        return ExpertSummary(identity.name, identity.name, _BUILTIN_WORK_TYPES[identity.name], (), True, 0,
                             f"builtin:{identity.name}", digest, estimate_text(identity.instructions), identity.role,
                             0.0, 0.0, True, False)

    @classmethod
    def _builtins(cls) -> tuple[ExpertSummary, ...]:
        return tuple(cls._builtin_summary(name) for name in load(None).names)

    def _entries(self, db: sqlite3.Connection, *, work_type: str | None = None, tags: Sequence[str] = (),
                 enabled: bool | None = None) -> tuple[ExpertSummary, ...]:
        clauses = ["deleted = 0"]
        params: list[object] = []
        if work_type is not None:
            clauses.append("work_type = ?")
            params.append(work_type)
        if enabled is not None:
            clauses.append("enabled = ?")
            params.append(int(enabled))
        for tag in tags:
            clauses.append("EXISTS (SELECT 1 FROM expert_tags WHERE expert_tags.id = experts.id AND tag_key = ?)")
            params.append(tag.casefold())
        rows = db.execute(
            "SELECT * FROM experts WHERE " + " AND ".join(clauses) + " ORDER BY name_key, id_key", params
        ).fetchall()
        custom = tuple(_row_summary(row) for row in rows)
        wanted_tags = {tag.casefold() for tag in tags}
        builtins = tuple(entry for entry in self._builtins()
                         if (work_type is None or entry.work_type == work_type)
                         and (enabled is None or entry.enabled == enabled)
                         and wanted_tags <= {tag.casefold() for tag in entry.tags})
        return builtins + custom

    def list(self) -> tuple[ExpertSummary, ...]:
        """All non-deleted experts, built-ins first. This never reads a Markdown body."""
        return self.query()

    def query(self, *, work_type: str | None = None, tags: str | Iterable[str] = (),
              enabled: bool | None = None) -> tuple[ExpertSummary, ...]:
        """Filter the lightweight index by one stable work type and all supplied tags."""
        if work_type is not None:
            work_type = _work_type(work_type)
        if enabled is not None:
            enabled = _enabled(enabled)
        wanted_tags = split_tags(tags)
        with self._connect() as db:
            return self._entries(db, work_type=work_type, tags=wanted_tags, enabled=enabled)

    def snapshot(self) -> CatalogSnapshot:
        """A consistent lightweight view plus the mutation revision; bodies are not opened."""
        with self._connect() as db, db:
            db.execute("BEGIN")
            row = db.execute("SELECT value FROM meta WHERE key = 'revision'").fetchone()
            revision = int(row["value"]) if row is not None else 0
            return CatalogSnapshot(revision, self._entries(db))

    def get(self, key: str) -> ExpertSummary | None:
        """Look up a non-deleted expert by stable id, or by name for legacy/runtime compatibility."""
        wanted = _lookup_key(key)
        builtin = next((entry for entry in self._builtins()
                        if entry.stable_id.casefold() == wanted or entry.name.casefold() == wanted), None)
        if builtin is not None:
            return builtin
        with self._connect() as db:
            row = self._find_row(db, key)
            return _row_summary(row) if row is not None else None

    def create(self, *, stable_id: str, name: str, work_type: str, markdown: str,
               tags: str | Iterable[str] = (), enabled: bool = True) -> ExpertSummary:
        stable_id, name = _stable_id(stable_id), _name(name)
        work_type, tags, enabled = _work_type(work_type), split_tags(tags), _enabled(enabled)
        document = self._document(name, work_type, markdown)
        with self._write() as db:
            self._check_namespace(db, stable_id, name)
            now = self._clock()
            self._write_body(document)
            db.execute("INSERT INTO experts VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (
                stable_id, stable_id.casefold(), name, name.casefold(), work_type, _tags_json(tags), int(enabled), 1,
                document.content_hash, document.md_path, document.body_tokens, document.identity.role, now, now, 0,
            ))
            self._replace_tags(db, stable_id, tags)
            self._insert_version(db, stable_id, 1, name, work_type, tags, enabled, document.content_hash,
                                 document.md_path, document.body_tokens, document.identity.role, now, False)
            self._bump(db)
            return _row_summary(db.execute("SELECT * FROM experts WHERE id = ?", (stable_id,)).fetchone())

    def update(self, stable_id: str, *, name: str | None = None, work_type: str | None = None,
               tags: str | Iterable[str] | None = None, enabled: bool | None = None,
               markdown: str | None = None) -> ExpertSummary:
        """Replace any supplied fields and append one immutable version when the expert definition changed."""
        stable_id = _stable_id(stable_id)
        if stable_id.casefold() in self._builtin_keys():
            raise ProtectedExpertError("Built-in Fusion identities are protected.")
        with self._write() as db:
            row = db.execute("SELECT * FROM experts WHERE id_key = ? AND deleted = 0", (stable_id.casefold(),)).fetchone()
            if row is None:
                raise UnknownExpertError("No enabled or disabled expert has this stable id.")
            new_name = _name(name) if name is not None else row["name"]
            new_work_type = _work_type(work_type) if work_type is not None else row["work_type"]
            new_tags = split_tags(tags) if tags is not None else tuple(json.loads(row["tags"]))
            new_enabled = _enabled(enabled) if enabled is not None else bool(row["enabled"])
            if markdown is None:
                markdown, _ = self._read_body(row["content_hash"], row["md_path"])
            document = self._document(new_name, new_work_type, markdown)
            changed = (new_name != row["name"] or new_work_type != row["work_type"] or new_tags != tuple(json.loads(row["tags"]))
                       or new_enabled != bool(row["enabled"]) or document.content_hash != row["content_hash"])
            if not changed:
                return _row_summary(row)
            self._check_namespace(db, stable_id, new_name, current=row["id"])
            now, version = self._clock(), int(row["version"]) + 1
            self._write_body(document)
            db.execute("""UPDATE experts SET name = ?, name_key = ?, work_type = ?, tags = ?, enabled = ?, version = ?,
                          content_hash = ?, md_path = ?, body_tokens = ?, role = ?, updated = ? WHERE id = ?""", (
                new_name, new_name.casefold(), new_work_type, _tags_json(new_tags), int(new_enabled), version,
                document.content_hash, document.md_path, document.body_tokens, document.identity.role, now, row["id"],
            ))
            self._replace_tags(db, row["id"], new_tags)
            self._insert_version(db, row["id"], version, new_name, new_work_type, new_tags, new_enabled,
                                 document.content_hash, document.md_path, document.body_tokens,
                                 document.identity.role, now, False)
            self._bump(db)
            return _row_summary(db.execute("SELECT * FROM experts WHERE id = ?", (row["id"],)).fetchone())

    def set_enabled(self, stable_id: str, enabled: bool) -> ExpertSummary:
        """Enable or disable the current expert. The exact frozen versions remain resolvable."""
        stable_id, enabled = _stable_id(stable_id), _enabled(enabled)
        if stable_id.casefold() in self._builtin_keys():
            raise ProtectedExpertError("Built-in Fusion identities are protected.")
        with self._write() as db:
            row = db.execute("SELECT * FROM experts WHERE id_key = ? AND deleted = 0", (stable_id.casefold(),)).fetchone()
            if row is None:
                raise UnknownExpertError("No expert has this stable id.")
            if bool(row["enabled"]) == enabled:
                return _row_summary(row)
            now, version = self._clock(), int(row["version"]) + 1
            db.execute("UPDATE experts SET enabled = ?, version = ?, updated = ? WHERE id = ?",
                       (int(enabled), version, now, row["id"]))
            self._insert_version(db, row["id"], version, row["name"], row["work_type"], tuple(json.loads(row["tags"])),
                                 enabled, row["content_hash"], row["md_path"], int(row["body_tokens"]), row["role"],
                                 now, False)
            self._bump(db)
            return _row_summary(db.execute("SELECT * FROM experts WHERE id = ?", (row["id"],)).fetchone())

    def delete(self, stable_id: str) -> bool:
        """Hide an expert as a tombstone while retaining every prior immutable version. Returns whether it changed."""
        stable_id = _stable_id(stable_id)
        if stable_id.casefold() in self._builtin_keys():
            raise ProtectedExpertError("Built-in Fusion identities are protected.")
        with self._write() as db:
            row = db.execute("SELECT * FROM experts WHERE id_key = ?", (stable_id.casefold(),)).fetchone()
            if row is None or row["deleted"]:
                return False
            now, version = self._clock(), int(row["version"]) + 1
            db.execute("UPDATE experts SET enabled = 0, deleted = 1, version = ?, updated = ? WHERE id = ?",
                       (version, now, row["id"]))
            db.execute("DELETE FROM expert_tags WHERE id = ?", (row["id"],))
            self._insert_version(db, row["id"], version, row["name"], row["work_type"], tuple(json.loads(row["tags"])),
                                 False, row["content_hash"], row["md_path"], int(row["body_tokens"]), row["role"],
                                 now, True)
            self._bump(db)
            return True

    def _resolve_builtin(self, summary: ExpertSummary) -> ResolvedExpert:
        identity = load(None).get(summary.name)
        markdown = builtin_markdown(summary.name)
        if identity is None or markdown is None:
            raise ExpertStorageError("A built-in Fusion identity is missing.")
        return ResolvedExpert(summary, markdown, identity)

    def _resolve_custom(self, summary: ExpertSummary) -> ResolvedExpert:
        markdown, _ = self._read_body(summary.content_hash, summary.md_path)
        parsed = parse_controlled(markdown, name=summary.name, ceiling=_ceiling(summary.work_type))
        # The role in SQLite is an index cache. The immutable document plus its work-type ceiling is authoritative.
        if parsed.role != summary.role:
            summary = replace(summary, role=parsed.role)
        return ResolvedExpert(summary, markdown, parsed)

    def resolve(self, key: str) -> ResolvedExpert:
        """Resolve the current visible expert. Disabled and deleted experts are not runnable through this API."""
        summary = self.get(key)
        if summary is None:
            raise UnknownExpertError("No visible expert matches this stable id or name.")
        if not summary.enabled:
            raise ExpertCatalogError("This expert is disabled; resolve a previously frozen version explicitly instead.")
        return self._resolve_builtin(summary) if summary.builtin else self._resolve_custom(summary)

    def resolve_version(self, key: str, version: int) -> ResolvedExpert:
        """Resolve one frozen immutable version, even after later edits, disablement or deletion.

        Built-ins have version ``0``. A version row created by delete is only a tombstone and raises
        ``ExpertCatalogError``; all earlier runnable versions remain available.
        """
        if not isinstance(version, int) or version < 0:
            raise ValueError("version must be a non-negative integer.")
        wanted = _lookup_key(key)
        builtin = next((entry for entry in self._builtins()
                        if entry.stable_id.casefold() == wanted or entry.name.casefold() == wanted), None)
        if builtin is not None:
            if version != 0:
                raise UnknownExpertError("A built-in expert only has version 0.")
            return self._resolve_builtin(builtin)
        with self._connect() as db:
            owner = self._find_row(db, key, include_deleted=True)
            if owner is None:
                raise UnknownExpertError("No expert matches this stable id or name.")
            row = db.execute("SELECT * FROM versions WHERE id = ? AND version = ?", (owner["id"], version)).fetchone()
        if row is None:
            raise UnknownExpertError("No such expert version.")
        summary = _version_summary(row)
        if summary.deleted:
            raise ExpertCatalogError("This version is a deletion tombstone, not a runnable expert.")
        return self._resolve_custom(summary)
