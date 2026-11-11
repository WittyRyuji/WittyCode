"""The shared memory: notes that Droid, Codex and Devin (and the user) read and write, kept as plain files.

Layout under the memory folder (``%APPDATA%\\WittyCode\\memory``)::

    global\\<id>.md                 notes for every project
    projects\\<name>-<hash>\\<id>.md  notes for one project (the folder is the project's path, normalized)
    journal.jsonl                  every change, one JSON line each: what undoing is made of
    index.sqlite                   search index (derived: rebuilt from the notes when it is missing or out of step)
    .lock                          held while writing

A note is a Markdown file: a header of ``key: <JSON value>`` lines between ``---`` lines (JSON is YAML, so any YAML reader
reads it), then the text. Nothing is ever deleted: forgetting marks a note ``forgotten`` (it can be restored), and every
change is in the journal with the note as it was before and after, so any change can be undone.

Several processes write at once (one memory server per conversation of each engine, and the application): a write holds
``.lock`` (a byte-range lock on the file, which the system drops if the holder dies), changes the note by writing a
temporary file and renaming it, and updates the index in the same step. Readers need no lock.

Search is SQLite FTS5 with the ``trigram`` tokenizer, which finds Chinese text but only for three characters or more;
shorter terms fall back to ``LIKE``. A long term is also tried in overlapping pieces, so a sentence finds the notes
that share parts of it.
"""

from __future__ import annotations

import contextlib
import difflib
import hashlib
import json
import os
import re
import secrets
import sqlite3
import time
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, NamedTuple

from desktop.core.memory.safety import find_secret

try:  # Windows; the other branch is for running the tests elsewhere
    import msvcrt

    def _lock(handle: Any) -> None:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)

    def _unlock(handle: Any) -> None:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)

except ImportError:  # pragma: no cover
    import fcntl

    def _lock(handle: Any) -> None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock(handle: Any) -> None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


SCOPES = ("global", "project")
KINDS = ("preference", "convention", "pitfall", "fact")
ACTIVE, FORGOTTEN = "active", "forgotten"
MAX_TITLE = 80
MAX_TEXT = 2000
MAX_TAGS = 8
MAX_TAG = 24
MAX_ACTIVE = 500
"""Active notes per scope (the global ones, and each project's)."""
SIMILAR_TITLE = 0.9
LOCK_SECONDS = 15.0
SNIPPET = 160
INDEX_VERSION = "1"


class MemoryRejected(Exception):
    """A request the memory cannot do. ``code`` is for the application to put in the user's words; ``message`` is for a model."""

    def __init__(self, code: str, message: str, entry: Entry | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.entry = entry


@dataclass(frozen=True, slots=True)
class Entry:
    id: str
    title: str
    text: str
    scope: str
    project: str
    kind: str
    tags: tuple[str, ...]
    created: float
    updated: float
    status: str
    rev: int
    source: Any
    """Who wrote it first: ``{"engine", "run"}`` of an agent, or ``"user"``."""
    editor: Any
    """Who changed it last (same shape)."""

    @property
    def active(self) -> bool:
        return self.status == ACTIVE

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id, "title": self.title, "text": self.text, "scope": self.scope, "project": self.project, "kind": self.kind,
            "tags": list(self.tags), "created": self.created, "updated": self.updated, "status": self.status, "rev": self.rev,
            "source": self.source, "editor": self.editor,
        }

    @classmethod
    def from_json(cls, raw: Mapping[str, Any]) -> Entry:
        return cls(
            id=str(raw["id"]), title=str(raw.get("title", "")), text=str(raw.get("text", "")), scope=str(raw.get("scope", "project")),
            project=str(raw.get("project", "")), kind=str(raw.get("kind", "fact")), tags=tuple(str(t) for t in raw.get("tags") or ()),
            created=float(raw.get("created", 0)), updated=float(raw.get("updated", 0)), status=str(raw.get("status", ACTIVE)),
            rev=int(raw.get("rev", 1)), source=raw.get("source"), editor=raw.get("editor"),
        )


class Candidate(NamedTuple):
    """A note as the application matches a message against it: read from the index, without opening its file."""

    id: str
    title: str
    text: str
    kind: str
    scope: str
    tags: tuple[str, ...]
    updated: float


@dataclass(frozen=True, slots=True)
class Hit:
    entry: Entry
    score: float
    snippet: str


@dataclass(frozen=True, slots=True)
class Change:
    """One line of the journal."""

    seq: int
    op: str
    id: str
    before: Entry | None
    after: Entry | None
    by: Any
    at: float

    @property
    def by_user(self) -> bool:
        return self.by == "user"

    @property
    def entry(self) -> Entry | None:
        return self.after or self.before


def agent(engine: str, run: str = "") -> dict[str, str]:
    """The ``by`` / ``source`` of a change made by an agent."""
    return {"engine": engine, "run": run}


APP = "app"
"""The ``by`` / ``source`` of what the application writes on its own (``core/memory/review``): neither the user nor an agent."""


def project_path(folder: str | os.PathLike[str]) -> str:
    return str(Path(folder).expanduser().resolve(strict=False))


def project_key(folder: str | os.PathLike[str]) -> str:
    """A folder name for a project: its own name for people, a hash of the whole path for uniqueness."""
    path = project_path(folder)
    name = re.sub(r"[^\w.\-]+", "-", Path(path).name).strip("-.")[:32] or "project"
    return f"{name}-{hashlib.sha1(os.path.normcase(path).encode('utf-8')).hexdigest()[:8]}"


def same_project(a: str, b: str) -> bool:
    return bool(a) and bool(b) and os.path.normcase(project_path(a)) == os.path.normcase(project_path(b))


# ----------------------------------------------------------------------------- the file format


def _dump(entry: Entry) -> str:
    head = {k: v for k, v in entry.to_json().items() if k != "text"}
    lines = ["---"] + [f"{key}: {json.dumps(value, ensure_ascii=False)}" for key, value in head.items()] + ["---", entry.text]
    return "\n".join(lines) + "\n"


def _parse(text: str) -> Entry:
    if not text.startswith("---\n"):
        raise ValueError("no header")
    head, separator, body = text[4:].partition("\n---\n")
    if not separator:
        raise ValueError("header not closed")
    raw: dict[str, Any] = {}
    for line in head.splitlines():
        key, colon, value = line.partition(": ")
        if colon:
            raw[key] = json.loads(value)
    raw["text"] = body[:-1] if body.endswith("\n") else body
    return Entry.from_json(raw)


def _write_file(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    try:
        temporary.write_text(text, encoding="utf-8", newline="\n")
        os.replace(temporary, path)
    except BaseException:
        with contextlib.suppress(OSError):
            temporary.unlink()
        raise


def _clean_tags(tags: Sequence[str] | None) -> tuple[str, ...]:
    out: list[str] = []
    for tag in tags or ():
        tag = re.sub(r"\s+", " ", str(tag)).strip()[:MAX_TAG]
        if tag and tag not in out:
            out.append(tag)
    return tuple(out[:MAX_TAGS])


def _normal_title(title: str) -> str:
    return re.sub(r"[\W_]+", "", title).lower()


def _same_subject(a: str, b: str) -> bool:
    """Whether two normalized titles are the same title, give or take a typo or a plural ("Step 1" and "Step 2" are not)."""
    if re.findall(r"\d+", a) != re.findall(r"\d+", b):
        return False
    return a == b or difflib.SequenceMatcher(None, a, b).ratio() >= SIMILAR_TITLE


def _terms(query: str) -> list[tuple[str, int]]:
    """Search terms with their weights: each word of the query, and for a long word its overlapping 3-character pieces."""
    terms: dict[str, int] = {}
    for word in re.split(r"[\s,，。;；:：!！?？()（）\[\]{}\"'“”‘’]+", query.strip()):
        if not word:
            continue
        terms[word] = max(terms.get(word, 0), 3)
        if len(word) > 4 and not word.isascii():
            for i in range(0, min(len(word) - 2, 20)):
                terms.setdefault(word[i : i + 3], 1)
    return list(terms.items())[:24]


def _like(term: str) -> str:
    return "%" + term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


class MemoryStore:
    def __init__(self, root: str | os.PathLike[str]) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._checked = False

    # ------------------------------------------------------------------ places

    @property
    def journal_path(self) -> Path:
        return self.root / "journal.jsonl"

    @property
    def index_path(self) -> Path:
        return self.root / "index.sqlite"

    def _path(self, entry: Entry) -> Path:
        if entry.scope == "global":
            return self.root / "global" / f"{entry.id}.md"
        return self.root / "projects" / project_key(entry.project) / f"{entry.id}.md"

    def version(self) -> tuple[int, int]:
        """Changes whenever the journal does (what the application polls to know that something was written)."""
        try:
            stat = self.journal_path.stat()
        except OSError:
            return (0, 0)
        return (stat.st_size, stat.st_mtime_ns)

    # ------------------------------------------------------------------ locking, journal, index

    @contextlib.contextmanager
    def _locked(self) -> Iterator[None]:
        with open(self.root / ".lock", "a+b") as handle:
            deadline = time.monotonic() + LOCK_SECONDS
            while True:
                try:
                    _lock(handle)
                    break
                except OSError:
                    if time.monotonic() > deadline:
                        raise MemoryRejected("busy", "The memory is being written by something else; try again in a moment.") from None
                    time.sleep(0.02)
            try:
                yield
            finally:
                with contextlib.suppress(OSError):
                    _unlock(handle)

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.index_path, timeout=10)
        db.execute("pragma journal_mode=wal")
        db.executescript(
            """
            create table if not exists meta(key text primary key, value text);
            create table if not exists entries(
                id text primary key, scope text, project text, pkey text, kind text, title text, text text, tags text,
                status text, created real, updated real, engine text);
            create virtual table if not exists fts using fts5(id unindexed, title, text, tags, tokenize='trigram');
            """
        )
        return db

    def _index(self) -> sqlite3.Connection:
        db = self._connect()
        if not self._checked:
            self._checked = True
            version = db.execute("select value from meta where key='version'").fetchone()
            files = sum(1 for _ in self._files())
            rows = db.execute("select count(*) from entries").fetchone()[0]
            if version is None or version[0] != INDEX_VERSION or files != rows:
                self._rebuild(db)
        return db

    def _files(self) -> Iterator[Path]:
        yield from (self.root / "global").glob("*.md")
        yield from (self.root / "projects").glob("*/*.md")

    def _rebuild(self, db: sqlite3.Connection) -> None:
        db.execute("delete from entries")
        db.execute("delete from fts")
        for path in self._files():
            with contextlib.suppress(OSError, ValueError, KeyError):
                self._upsert(db, _parse(path.read_text(encoding="utf-8")))
        db.execute("insert or replace into meta values('version', ?)", (INDEX_VERSION,))
        db.commit()

    def rebuild_index(self) -> None:
        with self._locked():
            db = self._connect()
            try:
                self._rebuild(db)
            finally:
                db.close()

    @staticmethod
    def _upsert(db: sqlite3.Connection, entry: Entry) -> None:
        engine = entry.source.get("engine", "") if isinstance(entry.source, Mapping) else APP if entry.source == APP else "user"
        tags = " ".join(entry.tags)
        db.execute("delete from fts where id = ?", (entry.id,))
        db.execute(
            "insert or replace into entries values(?,?,?,?,?,?,?,?,?,?,?,?)",
            (entry.id, entry.scope, entry.project, project_key(entry.project) if entry.project else '', entry.kind, entry.title, entry.text, tags, entry.status, entry.created, entry.updated, engine),
        )
        db.execute("insert into fts(id, title, text, tags) values(?,?,?,?)", (entry.id, entry.title, entry.text, tags))

    def _next_seq(self) -> int:
        try:
            with open(self.journal_path, "rb") as handle:
                size = handle.seek(0, os.SEEK_END)
                handle.seek(max(0, size - 65536))
                lines = [line for line in handle.read().splitlines() if line.strip()]
        except FileNotFoundError:
            return 1
        for line in reversed(lines):
            with contextlib.suppress(ValueError, KeyError):
                return int(json.loads(line)["seq"]) + 1
        return 1

    def _record(self, op: str, before: Entry | None, after: Entry | None, by: Any) -> Change:
        change = Change(self._next_seq(), op, (after or before).id, before, after, by, time.time())  # type: ignore[union-attr]
        line = json.dumps(
            {
                "seq": change.seq, "op": op, "id": change.id, "before": before.to_json() if before else None,
                "after": after.to_json() if after else None, "by": by, "at": change.at,
            },
            ensure_ascii=False,
        )
        with open(self.journal_path, "a", encoding="utf-8", newline="\n") as handle:
            handle.write(line + "\n")
        return change

    @staticmethod
    def _change_of(raw: Mapping[str, Any]) -> Change:
        return Change(
            int(raw["seq"]), str(raw["op"]), str(raw["id"]), Entry.from_json(raw["before"]) if raw.get("before") else None,
            Entry.from_json(raw["after"]) if raw.get("after") else None, raw.get("by"), float(raw.get("at", 0)),
        )

    def journal(self, *, since: int = 0, limit: int | None = None) -> list[Change]:
        """The changes after ``since`` (a sequence number), oldest first; with ``limit`` the newest that many."""
        out: list[Change] = []
        try:
            with open(self.journal_path, encoding="utf-8") as handle:
                for line in handle:
                    if not line.strip():
                        continue
                    try:
                        raw = json.loads(line)
                    except ValueError:
                        continue
                    if int(raw.get("seq", 0)) > since:
                        out.append(self._change_of(raw))
        except FileNotFoundError:
            return []
        return out[-limit:] if limit else out

    # ------------------------------------------------------------------ reading

    def _visible(self, entry: Entry, project: str | None) -> bool:
        return project is None or entry.scope == "global" or same_project(entry.project, project)

    def read(self, entry_id: str, *, project: str | None = None) -> Entry:
        """One note. ``project``: the project of the one asking (it sees the global notes and its own, nothing else)."""
        db = self._index()
        try:
            row = db.execute("select scope, project from entries where id = ?", (entry_id,)).fetchone()
        finally:
            db.close()
        if row is None:
            raise MemoryRejected("not_found", f"There is no memory {entry_id!r}.")
        probe = Entry(entry_id, "", "", row[0], row[1], "fact", (), 0, 0, ACTIVE, 1, None, None)
        try:
            entry = _parse(self._path(probe).read_text(encoding="utf-8"))
        except (OSError, ValueError, KeyError):
            raise MemoryRejected("not_found", f"There is no memory {entry_id!r}.") from None
        if not self._visible(entry, project):
            raise MemoryRejected("not_found", f"There is no memory {entry_id!r}.")
        return entry

    def search(
        self, query: str = "", *, project: str | None = None, scope: str = "all", limit: int = 5, status: str | None = ACTIVE
    ) -> list[Hit]:
        """Notes matching ``query`` (best first). ``project``: the asker's project (``None``: every project, for the
        application itself); ``scope``: ``all`` | ``global`` | ``project``. No query: the newest notes."""
        where, args = [], []
        if status:
            where.append("e.status = ?")
            args.append(status)
        if scope == "global":
            where.append("e.scope = 'global'")
        elif scope == "project":
            if project is None:
                where.append("e.scope = 'project'")
            else:
                where.append("e.scope = 'project' and e.pkey = ?")
                args.append(project_key(project))
        elif project is not None:
            where.append("(e.scope = 'global' or e.pkey = ?)")
            args.append(project_key(project))
        base = " and ".join(where) or "1"
        db = self._index()
        try:
            scores: dict[str, float] = {}
            terms = _terms(query)
            if not terms:
                rows = db.execute(f"select e.id from entries e where {base} order by e.updated desc limit ?", (*args, max(1, limit))).fetchall()
                scores = {row[0]: 0.0 for row in rows}
            for term, weight in terms:
                if len(term) >= 3:
                    cursor = db.execute(
                        f"select e.id from fts join entries e on e.id = fts.id where fts match ? and {base}",
                        ('"' + term.replace('"', '""') + '"', *args),
                    )
                else:
                    pattern = _like(term)
                    cursor = db.execute(
                        f"select e.id from entries e where (e.title like ? escape '\\' or e.text like ? escape '\\' or e.tags like ? escape '\\') and {base}",
                        (pattern, pattern, pattern, *args),
                    )
                for (found,) in cursor.fetchall():
                    scores[found] = scores.get(found, 0.0) + weight
            entries = [self._load_row(found) for found in scores]
        finally:
            db.close()
        hits = [Hit(entry, scores[entry.id], _snippet(entry.text)) for entry in entries if entry is not None]
        here = project_path(project) if project else ""
        hits.sort(key=lambda hit: (-hit.score, 0 if hit.entry.project == here and here else 1, -hit.entry.updated))
        return hits[: max(1, limit)]

    def _load_row(self, entry_id: str) -> Entry | None:
        try:
            return self.read(entry_id)
        except MemoryRejected:
            return None

    def entries(
        self, *, scope: str | None = None, project: str | None = None, kind: str | None = None, engine: str | None = None,
        status: str | None = ACTIVE, limit: int = 1000,
    ) -> list[Entry]:
        """Notes for the application's list, newest first (no filter: every note of every project)."""
        where, args = [], []
        for column, value in (("scope", scope), ("kind", kind), ("status", status), ("engine", engine)):
            if value:
                where.append(f"{column} = ?")
                args.append(value)
        if project:
            where.append("pkey = ?")
            args.append(project_key(project))
        db = self._index()
        try:
            ids = [row[0] for row in db.execute(f"select id from entries where {' and '.join(where) or '1'} order by updated desc limit ?", (*args, limit))]
        finally:
            db.close()
        return [entry for entry in map(self._load_row, ids) if entry is not None]

    def candidates(self, project: str | None) -> list[Candidate]:
        """Every active note a conversation of ``project`` may see (the global ones and the project's own; ``None``: the
        global ones only), newest first."""
        where, args = "e.status = 'active' and (e.scope = 'global'", []
        if project:
            where += " or e.pkey = ?"
            args.append(project_key(project))
        db = self._index()
        try:
            rows = db.execute(f"select e.id, e.title, e.text, e.kind, e.scope, e.tags, e.updated from entries e where {where}) order by e.updated desc", args).fetchall()
        finally:
            db.close()
        return [Candidate(i, title, text, kind, scope, tuple((tags or "").split()), updated) for i, title, text, kind, scope, tags, updated in rows]

    def counts(self) -> dict[str, int]:
        db = self._index()
        try:
            return {status: count for status, count in db.execute("select status, count(*) from entries group by status")}
        finally:
            db.close()

    # ------------------------------------------------------------------ writing

    def _check(self, title: str, text: str, scope: str, kind: str, project: str) -> None:
        if not title.strip():
            raise MemoryRejected("empty", "A memory needs a title.")
        if not text.strip():
            raise MemoryRejected("empty", "A memory needs some text.")
        if len(title) > MAX_TITLE:
            raise MemoryRejected("title_long", f"The title is {len(title)} characters; keep it under {MAX_TITLE}.")
        if len(text) > MAX_TEXT:
            raise MemoryRejected("text_long", f"The text is {len(text)} characters; keep it under {MAX_TEXT} (split it, or keep only what matters).")
        if scope not in SCOPES:
            raise MemoryRejected("bad_scope", f"scope must be one of {', '.join(SCOPES)}.")
        if kind not in KINDS:
            raise MemoryRejected("bad_kind", f"kind must be one of {', '.join(KINDS)}.")
        if scope == "project" and not project:
            raise MemoryRejected("bad_scope", "A project memory needs a project.")
        secret = find_secret(title, text)
        if secret:
            raise MemoryRejected("secret", f"This looks like it contains {secret}. Secrets are never stored in the memory; describe where it is kept instead.")

    def _active_count(self, db: sqlite3.Connection, scope: str, project: str) -> int:
        if scope == "global":
            return db.execute("select count(*) from entries where scope = 'global' and status = 'active'").fetchone()[0]
        return db.execute("select count(*) from entries where scope = 'project' and pkey = ? and status = 'active'", (project_key(project),)).fetchone()[0]

    def _find_similar(self, db: sqlite3.Connection, title: str, text: str, scope: str, project: str, skip: str = "") -> Entry | None:
        rows = db.execute(
            "select id, title, text from entries where status = 'active' and scope = ? and (scope = 'global' or pkey = ?)",
            (scope, project_key(project) if project else ""),
        ).fetchall()
        wanted = _normal_title(title)
        for found, other_title, other_text in rows:
            if found == skip:
                continue
            if other_text.strip() == text.strip() or _same_subject(wanted, _normal_title(other_title)):
                return self._load_row(found)
        return None

    def create(
        self, *, title: str, text: str, scope: str = "project", project: str = "", kind: str = "fact", tags: Sequence[str] | None = None, by: Any,
        now: float | None = None,
    ) -> Entry:
        title, text = title.strip(), text.strip()
        project = project_path(project) if scope == "project" and project else ""
        self._check(title, text, scope, kind, project)
        with self._locked():
            db = self._index()
            try:
                similar = self._find_similar(db, title, text, scope, project)
                if similar is not None:
                    raise MemoryRejected(
                        "duplicate", f"There is already a similar memory, {similar.id} ({similar.title!r}). Update that one instead (memory_update).", similar
                    )
                if self._active_count(db, scope, project) >= MAX_ACTIVE:
                    raise MemoryRejected("limit", f"The {scope} memory is full ({MAX_ACTIVE} notes). Forget or merge some before adding more.")
                stamp = now if now is not None else time.time()
                entry = Entry(
                    f"m-{int(stamp)}-{secrets.token_hex(3)}", title, text, scope, project, kind, _clean_tags(tags), stamp, stamp, ACTIVE, 1, by, by
                )
                _write_file(self._path(entry), _dump(entry))
                self._upsert(db, entry)
                self._record("create", None, entry, by)
                db.commit()
                return entry
            finally:
                db.close()

    def update(
        self, entry_id: str, *, by: Any, project: str | None = None, title: str | None = None, text: str | None = None,
        tags: Sequence[str] | None = None, kind: str | None = None, scope: str | None = None, move_to: str | None = None, _op: str = "update",
    ) -> Entry:
        """Change a note. ``project``: the asker's (an agent sees only its own); ``scope`` / ``move_to``: make it global or
        give it to a project (the user only)."""
        with self._locked():
            before = self.read(entry_id, project=project)
            new_scope = scope or before.scope
            new_project = project_path(move_to) if new_scope == "project" and move_to else before.project if new_scope == before.scope else ""
            if new_scope == "project" and not new_project:
                raise MemoryRejected("bad_scope", "A project memory needs a project.")
            after = replace(
                before,
                title=before.title if title is None else title.strip(),
                text=before.text if text is None else text.strip(),
                tags=before.tags if tags is None else _clean_tags(tags),
                kind=before.kind if kind is None else kind,
                scope=new_scope,
                project=new_project,
                updated=time.time(),
                rev=before.rev + 1,
                editor=by,
            )
            if after.active:
                self._check(after.title, after.text, after.scope, after.kind, after.project)
            db = self._index()
            try:
                if after.active and (after.scope, after.project) != (before.scope, before.project) and self._active_count(db, after.scope, after.project) >= MAX_ACTIVE:
                    raise MemoryRejected("limit", f"The {after.scope} memory is full ({MAX_ACTIVE} notes).")
                if after.active and _normal_title(after.title) != _normal_title(before.title):
                    similar = self._find_similar(db, after.title, after.text, after.scope, after.project, skip=after.id)
                    if similar is not None:
                        raise MemoryRejected("duplicate", f"There is already a similar memory, {similar.id} ({similar.title!r}).", similar)
                old_path, new_path = self._path(before), self._path(after)
                _write_file(new_path, _dump(after))
                if old_path != new_path:
                    with contextlib.suppress(OSError):
                        old_path.unlink()
                self._upsert(db, after)
                self._record(_op, before, after, by)
                db.commit()
                return after
            finally:
                db.close()

    def forget(self, entry_id: str, *, by: Any, project: str | None = None) -> Entry:
        with self._locked():
            before = self.read(entry_id, project=project)
            if not before.active:
                return before
            return self._set_status(before, FORGOTTEN, by, "forget")

    def restore(self, entry_id: str, *, by: Any) -> Entry:
        with self._locked():
            before = self.read(entry_id)
            if before.active:
                return before
            db = self._index()
            try:
                if self._active_count(db, before.scope, before.project) >= MAX_ACTIVE:
                    raise MemoryRejected("limit", f"The {before.scope} memory is full ({MAX_ACTIVE} notes).")
            finally:
                db.close()
            return self._set_status(before, ACTIVE, by, "restore")

    def _set_status(self, before: Entry, status: str, by: Any, op: str) -> Entry:
        after = replace(before, status=status, updated=time.time(), rev=before.rev + 1, editor=by)
        db = self._index()
        try:
            _write_file(self._path(after), _dump(after))
            self._upsert(db, after)
            self._record(op, before, after, by)
            db.commit()
        finally:
            db.close()
        return after

    def undo(self, seq: int, *, by: Any = "user") -> Entry:
        """Take back the change with this sequence number: a new change that puts the note as it was."""
        change = next((c for c in self.journal(since=seq - 1, limit=None) if c.seq == seq), None)
        if change is None:
            raise MemoryRejected("not_found", f"There is no change {seq}.")
        if change.op == "create":
            return self.forget(change.id, by=by)
        if change.op == "forget":
            return self.restore(change.id, by=by)
        if change.op == "restore":
            return self.forget(change.id, by=by)
        before = change.before
        assert before is not None
        return self.update(
            change.id, by=by, title=before.title, text=before.text, tags=before.tags, kind=before.kind,
            scope=before.scope, move_to=before.project or None, _op="update",
        )


def _snippet(text: str) -> str:
    flat = re.sub(r"\s+", " ", text).strip()
    return flat if len(flat) <= SNIPPET else flat[: SNIPPET - 1] + "…"
