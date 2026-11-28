"""The per-session archive: identity binding, the instance timeline, the append-only event log, committed
snapshot generations and the shared thin index (``outputs/session-context-recall-final.md`` §1–§4).

Layout under an explicitly supplied root (``.../session``)::

    index.sqlite                    rebuildable locator; identities and record positions only, no bodies
    <sid>/manifest.json             bindings, timeline, current instance, committed generations, latest
    <sid>/turn-budget.json          the live user turn's recall ledger (survives switches and restarts)
    <sid>/output/o-<sha256>.txt     the session's single tool-output store (desktop.core.harness.outputs)
    <sid>/<backend>/<iid>/events.jsonl
    <sid>/<backend>/<iid>/dumps/gNNNNNN.jsonl.gz

``sid`` is a logical session (16 ``[A-Za-z0-9]``), ``iid`` a backend instance (8). External native ids are only
stored in the identity mapping, never in paths. The index locates ``(sid, backend, iid)`` from
``(connection, native session id)`` and back, and maps ``(sid, record id)`` to the generation/instance/file/line
holding the original record; it stores no message text and is rebuilt from manifests, event logs and committed
dumps on demand.

A caller binds exactly one runtime-authorized ``sid`` per ``SessionArchive``; nothing here scans other sessions,
legacy session stores, or accepts paths from the caller beyond the root it is opened with.

With an ``ArchiveMeter`` (``desktop.core.storage_meter``) bound to the same root, every write passes a serialized
capacity check and is credited per category; a refused or failed write leaves the previous state — old generation,
old manifest, intact ledger — and rolls back its own partial bytes.

Pure: no Qt.
"""

from __future__ import annotations

import json
import os
import re
import stat
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from desktop.core.harness.outputs import OutputStore
from desktop.core import session_archive_io as io
from desktop.core import storage_meter

SID_LEN = 16
IID_LEN = 8
SID = re.compile(rf"^[A-Za-z0-9]{{{SID_LEN}}}\Z")
IID = re.compile(rf"^[A-Za-z0-9]{{{IID_LEN}}}\Z")
BACKEND = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}\Z")
GENERATION = re.compile(r"^g(\d{6,})\.jsonl\.gz\Z")
MANIFEST_FORMAT = "wittycode-session-manifest/1"
DUMP_FORMAT = "wittycode-session-dump/1"
INDEX_NAME = io.INDEX_NAME


class ArchiveError(RuntimeError):
    """The archive refused an operation, or stored data failed validation."""


class GapError(ArchiveError):
    """A committed generation's file is missing or damaged: report the gap, never fabricate history."""


@dataclass(frozen=True, slots=True)
class Instance:
    """One backend instance of a logical session.``iid`` is the only id that may become a path segment."""

    sid: str
    backend: str
    iid: str
    connection: str
    native_id: str


@dataclass(frozen=True, slots=True)
class PreparedDump:
    """A snapshot that was saved and verified but not yet published; ``publish`` commits it, ``abort`` drops it."""

    generation: int
    parent: int | None
    instance: Instance
    staging: Path
    sha256: str
    evicted: tuple[str, ...]
    records: int


def _file_bytes(path: Path) -> int:
    """Bytes of a regular file; 0 for anything missing or not a plain file."""
    try:
        info = path.lstat()
    except OSError:
        return 0
    return info.st_size if stat.S_ISREG(info.st_mode) else 0


def _rollback_append(path: Path, old_size: int, data: bytes) -> None:
    """Undo our own append after a failed write — only when the file's tail is provably the bytes
    we wrote, in full or in part. Anything else stays: another writer's bytes are not ours to delete."""
    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_size <= old_size:
            return
        with path.open("rb") as stream:
            stream.seek(old_size)
            tail = stream.read()
        if tail == data or data.startswith(tail):
            os.truncate(path, old_size)
    except OSError:
        pass  # left bytes stay on disk; the next reconcile re-syncs the ledger


def _index(root: Path) -> io.Index:
    return io.Index(root / INDEX_NAME)


def _new_sid(root: Path, source: Callable[[], str] | None) -> str:
    return io.reserve_name(root, SID_LEN, source=source)


def _new_iid(instance_parent: Path, source: Callable[[], str] | None) -> str:
    return io.reserve_name(instance_parent, IID_LEN, source=source)


def _dump_name(generation: int) -> str:
    return f"g{generation:06d}.jsonl.gz"


def _validate_manifest(data: Any, sid: str) -> dict:
    if not isinstance(data, dict) or data.get("format") != MANIFEST_FORMAT or data.get("sid") != sid:
        raise ArchiveError(f"Not a session manifest for {sid}.")
    if not isinstance(data.get("identities"), list) or not isinstance(data.get("timeline"), list):
        raise ArchiveError(f"Session manifest {sid} is missing its identity records.")
    for entry in data["identities"]:
        if (not isinstance(entry, dict) or BACKEND.fullmatch(entry.get("backend") or "") is None
                or IID.fullmatch(entry.get("iid") or "") is None
                or not isinstance(entry.get("connection"), str) or not isinstance(entry.get("native"), str)):
            raise ArchiveError(f"Session manifest {sid} has a malformed identity record.")
    pairs = {(item["backend"], item["iid"]) for item in data["identities"]}
    if len(pairs) != len(data["identities"]) or len({item["iid"] for item in data["identities"]}) != len(pairs):
        raise ArchiveError(f"Session manifest {sid} repeats an instance id.")
    generations = data.get("generations")
    latest = data.get("latest")
    if not isinstance(generations, list) or (latest is not None and not isinstance(latest, int)):
        raise ArchiveError(f"Session manifest {sid} is missing its generation records.")
    for entry in generations:
        if (not isinstance(entry, dict) or not isinstance(entry.get("gen"), int)
                or not isinstance(entry.get("sha256"), str) or not isinstance(entry.get("evicted"), list)
                or GENERATION.fullmatch(entry.get("file") or "") is None):
            raise ArchiveError(f"Session manifest {sid} has a malformed generation record.")
    if generations and latest != max(item["gen"] for item in generations):
        raise ArchiveError(f"Session manifest {sid} does not point at its newest committed generation.")
    return data


def read_manifest(root: Path, sid: str) -> dict:
    """The manifest of a known, runtime-authorized sid. For integration callers; never scans other sessions."""
    root = Path(root)
    if SID.fullmatch(sid or "") is None:
        raise ArchiveError(f"Illegal session id: {sid!r}")
    try:
        directory = io.descend(root, sid, must_exist=True)
        return _validate_manifest(io.read_json(directory / "manifest.json"), sid)
    except ArchiveError:
        raise
    except (OSError, ValueError) as error:
        raise ArchiveError(f"Session manifest {sid} cannot be read.") from error


def generation_chain(root: Path, sid: str) -> list[dict]:
    """Committed generations oldest to newest, each with its ``parent``: the recall walk order."""
    return sorted(read_manifest(root, sid)["generations"], key=lambda item: item["gen"])


def latest_generation(root: Path, sid: str) -> tuple[int | None, int | None]:
    """``(latest, parent)`` — where a recall starts and where it goes next; ``(None, None)`` before any dump."""
    manifest = read_manifest(root, sid)
    latest = manifest["latest"]
    if latest is None:
        return None, None
    entry = next(item for item in manifest["generations"] if item["gen"] == latest)
    return latest, entry["parent"]


def _generation_path(root: Path, sid: str, entry: Mapping) -> Path:
    return io.descend(root, sid, entry["backend"], entry["iid"], "dumps", entry["file"])


def read_generation(root: Path, sid: str, generation: int) -> dict:
    """The committed snapshot of one generation: ``{"header": …, "records": […]}`` read locally.

    The file's sha256 is verified against the manifest before any record is returned; a missing or damaged
    generation raises ``GapError`` so the caller can report the gap instead of inventing evidence."""
    root = Path(root)
    manifest = read_manifest(root, sid)
    entry = next((item for item in manifest["generations"] if item["gen"] == generation), None)
    if entry is None:
        raise GapError(f"Session {sid} has no committed generation {generation}.")
    path = _generation_path(root, sid, entry)
    if not path.is_file() or io.is_link(path):
        raise GapError(f"Generation {generation} of session {sid} is missing from the archive.")
    if io.sha256_file(path) != entry["sha256"]:
        raise GapError(f"Generation {generation} of session {sid} failed its integrity check.")
    try:
        lines = io.gzip_read_lines(path)
    except (OSError, EOFError) as error:
        raise GapError(f"Generation {generation} of session {sid} is unreadable.") from error
    if not lines:
        raise GapError(f"Generation {generation} of session {sid} is empty.")
    header = json.loads(lines[0])
    if (header.get("format") != DUMP_FORMAT or header.get("sid") != sid
            or header.get("generation") != generation):
        raise GapError(f"Generation {generation} of session {sid} is not the manifest's dump.")
    return {"header": header, "records": [json.loads(line) for line in lines[1:]],
            "evicted": list(entry["evicted"]), "summary": dict(entry["summary"]), "parent": entry["parent"]}


def locate_record(root: Path, sid: str, record_id: str) -> dict | None:
    """Where one record's original lives: ``{generation, backend, iid, file, pos}`` or None."""
    rows = _index(Path(root)).query(
        "SELECT generation, backend, iid, file, pos FROM locations WHERE sid = ? AND record_id = ?",
        (sid, record_id))
    return rows[0] if rows else None


def resolve_local(root: Path, connection: str, native_id: str) -> dict | None:
    """``(connection, native session id)`` → ``{sid, backend, iid}``: which local instance an ACP session is."""
    rows = _index(Path(root)).query(
        "SELECT sid, backend, iid FROM identities WHERE connection = ? AND native_id = ?",
        (connection, native_id))
    return rows[0] if rows else None


def resolve_native(root: Path, sid: str, backend: str, iid: str) -> dict | None:
    """``(sid, backend, iid)`` → ``{connection, native_id}``: the reverse direction of the same mapping."""
    rows = _index(Path(root)).query(
        "SELECT connection, native_id FROM identities WHERE sid = ? AND backend = ? AND iid = ?",
        (sid, backend, iid))
    return rows[0] if rows else None


def _index_events(db, root: Path, sid: str, manifest: Mapping) -> None:
    """Every event of every bound instance is locatable by ``(sid, event id)`` at its live position."""
    for identity in manifest["identities"]:
        events = root / sid / identity["backend"] / identity["iid"] / "events.jsonl"
        if not events.is_file() or io.is_link(events):
            continue
        relative = f"{identity['backend']}/{identity['iid']}/events.jsonl"
        for line in io.read_lines(events):
            if not line.strip():
                continue
            record = json.loads(line)
            if not isinstance(record.get("id"), str) or not isinstance(record.get("seq"), int):
                raise ArchiveError(f"Session {sid} has a malformed event; the index cannot be rebuilt over it.")
            db.execute(
                "INSERT OR REPLACE INTO locations (sid, record_id, generation, backend, iid, file, pos)"
                " VALUES (?, ?, NULL, ?, ?, ?, ?)",
                (sid, record["id"], identity["backend"], identity["iid"], relative, record["seq"]))


def _index_dumps(db, root: Path, sid: str, manifest: Mapping) -> None:
    """An evicted record's location is the first committed dump that holds it; later copies do not move it."""
    for entry in manifest["generations"]:
        path = _generation_path(root, sid, entry)
        if not path.is_file() or io.is_link(path) or io.sha256_file(path) != entry["sha256"]:
            continue
        positions: dict[str, int] = {}
        for number, line in enumerate(io.gzip_read_lines(path)[1:], start=2):
            record = json.loads(line)
            if isinstance(record.get("id"), str):
                positions.setdefault(record["id"], number)
        relative = f"{entry['backend']}/{entry['iid']}/dumps/{entry['file']}"
        for record_id in entry["evicted"]:
            if record_id in positions:
                db.execute(
                    "INSERT INTO locations (sid, record_id, generation, backend, iid, file, pos)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?)"
                    " ON CONFLICT (sid, record_id) DO UPDATE SET"
                    " generation = excluded.generation, file = excluded.file, pos = excluded.pos"
                    " WHERE locations.generation IS NULL",
                    (sid, record_id, entry["gen"], entry["backend"], entry["iid"], relative, positions[record_id]))


def rebuild_index(root: Path, sids: Iterable[str] | None = None) -> int:
    """Rebuild ``index.sqlite`` from manifests, event logs and committed dumps; returns sessions indexed.

    With ``sids`` given, only those sessions' rows are replaced. Without it, every session directory under the
    archive root that has a manifest is indexed — the archive's own root only, never other stores."""
    root = Path(root)
    io.reject_link(root)
    names = (
        [sid for sid in sids if SID.fullmatch(sid or "") and (root / sid / "manifest.json").is_file()]
        if sids is not None
        else [entry.name for entry in root.iterdir()
              if SID.fullmatch(entry.name) and not io.is_link(entry) and (entry / "manifest.json").is_file()]
        if root.is_dir() else [])
    index = _index(root)
    with index.write() as db:
        if sids is None:
            db.execute("DELETE FROM identities")
            db.execute("DELETE FROM locations")
        for sid in names:
            db.execute("DELETE FROM identities WHERE sid = ?", (sid,))
            db.execute("DELETE FROM locations WHERE sid = ?", (sid,))
            manifest = read_manifest(root, sid)
            for identity in manifest["identities"]:
                db.execute(
                    "INSERT INTO identities (sid, backend, iid, connection, native_id) VALUES (?, ?, ?, ?, ?)",
                    (sid, identity["backend"], identity["iid"], identity["connection"], identity["native"]))
            _index_events(db, root, sid, manifest)
            _index_dumps(db, root, sid, manifest)
    return len(names)


class SessionArchive:
    """One logical session bound at runtime. All reads and writes stay under ``<root>/<sid>``."""

    def __init__(self, root: Path, sid: str, manifest: dict, *, clock: Callable[[], float] = time.time,
                 meter: storage_meter.ArchiveMeter | None = None) -> None:
        self._root = Path(root)
        self.sid = sid
        self._manifest = manifest
        self._clock = clock
        self._meter = meter
        self._lock = threading.Lock()
        self._seqs: dict[str, int] = {}
        self._staged: set[PreparedDump] = set()

    # ---- construction -----------------------------------------------------

    @classmethod
    def create(cls, root: Path | str, *, name_source: Callable[[], str] | None = None,
               clock: Callable[[], float] = time.time,
               meter: storage_meter.ArchiveMeter | None = None) -> SessionArchive:
        """Claim a fresh ``sid`` directory under the explicit archive root and bind it.

        With ``meter`` the first manifest write already passes the capacity gate; a refused create
        removes the freshly claimed — provably empty — directory and leaves nothing behind."""
        root = Path(root)
        io.reject_link(root)
        root.mkdir(parents=True, exist_ok=True)
        sid = _new_sid(root, name_source)
        manifest = {
            "format": MANIFEST_FORMAT,
            "sid": sid,
            "created": clock(),
            "identities": [],
            "timeline": [],
            "current": None,
            "generations": [],
            "latest": None,
        }
        archive = cls(root, sid, manifest, clock=clock, meter=meter)
        if meter is None:
            archive._write_manifest()
        else:
            try:
                with meter.txn() as tx:
                    archive._manifest_write_gated(tx)
            except BaseException:
                try:
                    (root / sid).rmdir()
                except OSError:
                    pass  # a dir that is not provably empty stays; nothing unsafe is deleted
                raise
        return archive

    @classmethod
    def open(cls, root: Path | str, sid: str, *, clock: Callable[[], float] = time.time,
             meter: storage_meter.ArchiveMeter | None = None) -> SessionArchive:
        """Bind a runtime-authorized sid that already exists; the manifest must validate.

        With ``meter`` the session's own files are re-synced into the ledger once — the explicit
        stat list that also adopts sessions written before metering reached them."""
        manifest = read_manifest(Path(root), sid)
        archive = cls(Path(root), sid, manifest, clock=clock, meter=meter)
        if meter is not None:
            with meter.txn() as tx:
                archive._reconcile_meter(tx)
        return archive

    @classmethod
    def bind(cls, root: Path | str, sid: str, *, clock: Callable[[], float] = time.time,
             meter: storage_meter.ArchiveMeter | None = None) -> SessionArchive:
        """Alias of ``open``: the runtime binds the sid it was authorized for; no arbitrary id or path is taken."""
        return cls.open(root, sid, clock=clock, meter=meter)

    # ---- basic properties --------------------------------------------------

    @property
    def root(self) -> Path:
        return self._root

    @property
    def dir(self) -> Path:
        return io.descend(self._root, self.sid, must_exist=True)

    @property
    def manifest(self) -> dict:
        return dict(self._manifest)

    @property
    def outputs(self) -> OutputStore:
        """The session's single tool-output directory; backend instances and dumps reference, never copy.

        With a meter the store is wrapped: ``put`` passes the capacity gate first and credits the
        bytes actually written, ``delete`` frees what the ledger held."""
        store = OutputStore(io.descend(self.dir, "output"))
        return _MeteredOutputs(store, self, self._meter) if self._meter is not None else store

    # ---- identity: bind, recover, timeline --------------------------------

    def _instance_dir(self, backend: str, iid: str) -> Path:
        return io.descend(self.dir, backend, iid)

    def _write_manifest(self) -> None:
        io.atomic_write_text(self.dir / "manifest.json",
                             json.dumps(self._manifest, ensure_ascii=False, indent=2) + "\n")

    def _manifest_write_gated(self, tx: storage_meter.MeterTxn, content: str | None = None) -> None:
        """Inside a meter transaction: gate the manifest's growth, write it, credit the actual bytes.

        Only growth is gated — a rewrite that does not enlarge the file can never push usage up,
        so it is never refused (cleanup paths must not be able to brick themselves)."""
        path = self.dir / "manifest.json"
        content = content if content is not None else json.dumps(self._manifest, ensure_ascii=False, indent=2) + "\n"
        delta = len(content.encode("utf-8")) - _file_bytes(path)
        if delta > 0:
            tx.check("history", delta)
        io.atomic_write_text(path, content)
        tx.record(f"{self.sid}/manifest.json", "history", _file_bytes(path))

    def _reconcile_meter(self, tx: storage_meter.MeterTxn) -> None:
        """Re-sync the ledger to this session's real files: an explicit stat list of the paths the
        archive itself owns — manifest, recall ledger, each identity's event log, committed or
        staged dumps, retained outputs — never a recursive scan. Entries the list misses were
        never ours; stale ledger rows for this sid are dropped."""
        sid = self.sid
        base = self.dir
        expected: dict[str, str] = {}

        def add(path: Path, category: str) -> None:
            if path.is_file() and not io.is_link(path):
                expected[path.relative_to(self._root).as_posix()] = category

        add(base / "manifest.json", "history")
        add(base / "turn-budget.json", "history")
        output = base / "output"
        if output.is_dir():
            for file in output.glob("*.txt"):
                add(file, "history")
        for identity in self._manifest["identities"]:
            instance = self._instance_dir(identity["backend"], identity["iid"])
            add(instance / "events.jsonl", "history")
            dumps = instance / "dumps"
            if dumps.is_dir():
                for file in dumps.iterdir():
                    if GENERATION.fullmatch(file.name) or file.name.endswith(".staging"):
                        add(file, "dump")
        for rel in tx.files_under(f"{sid}/"):
            if rel not in expected:
                tx.drop(rel)
        for rel, category in expected.items():
            tx.record(rel, category, _file_bytes(self._root / rel))

    def _identity_of(self, connection: str, native_id: str) -> dict | None:
        return next((entry for entry in self._manifest["identities"]
                     if entry["connection"] == connection and entry["native"] == native_id), None)

    def enter(self, backend: str, connection: str, native_id: str, *,
              id_source: Callable[[], str] | None = None) -> Instance:
        """Bind a backend instance to this session: re-entering the same ``(connection, native session id)``
        resumes its ``iid``; only a newly established native session claims a new ``iid``.

        Every call is one timeline entry, so ``GPT → Devin → GPT`` keeps the order of its three entries even
        though the third reuses the first ``iid``. A native id already bound to another session is refused."""
        if BACKEND.fullmatch(backend or "") is None:
            raise ArchiveError(f"Illegal backend name: {backend!r}")
        if not isinstance(connection, str) or not connection or not isinstance(native_id, str) or not native_id:
            raise ArchiveError("An instance bind needs a connection identifier and a native session id.")
        with self._lock:
            found = self._identity_of(connection, native_id)
            if found is not None and found["backend"] != backend:
                raise ArchiveError(f"Native session {native_id!r} is already bound to backend {found['backend']!r}.")
            owner = resolve_local(self._root, connection, native_id)
            if owner is not None and owner["sid"] != self.sid:
                raise ArchiveError(f"Native session {native_id!r} is already bound to another session.")
            if found is not None:
                iid, identities, result = found["iid"], self._manifest["identities"], "resumed"
            else:
                parent = self.dir / backend
                io.reject_link(parent)
                parent.mkdir(exist_ok=True)
                iid = _new_iid(parent, id_source)
                found = {"backend": backend, "iid": iid, "connection": connection, "native": native_id}
                identities = self._manifest["identities"] + [found]
                result = "new"
            entry = {"n": len(self._manifest["timeline"]) + 1,
                     "backend": backend, "iid": iid,
                     "connection": connection, "native": native_id,
                     "result": result}
            updated = {**self._manifest, "identities": identities,
                       "timeline": self._manifest["timeline"] + [entry],
                       "current": {"backend": backend, "iid": iid}}
            content = json.dumps(updated, ensure_ascii=False, indent=2) + "\n"
            if self._meter is None:
                io.atomic_write_text(self.dir / "manifest.json", content)
                if result == "new":
                    with _index(self._root).write() as db:
                        db.execute(
                            "INSERT INTO identities (sid, backend, iid, connection, native_id) VALUES (?, ?, ?, ?, ?)",
                            (self.sid, backend, iid, connection, native_id))
            else:
                try:
                    with self._meter.txn() as tx:
                        self._manifest_write_gated(tx, content)
                        if result == "new":
                            tx.db.execute(
                                "INSERT INTO identities (sid, backend, iid, connection, native_id) VALUES (?, ?, ?, ?, ?)",
                                (self.sid, backend, iid, connection, native_id))
                except BaseException:
                    if result == "new":
                        try:
                            (self.dir / backend / iid).rmdir()
                        except OSError:
                            pass  # the claimed instance dir stays if not provably empty
                    raise
            self._manifest = updated
            return Instance(self.sid, backend, iid, connection, native_id)

    def timeline(self) -> list[dict]:
        """Instance entries in entry order; the sequence is explicit, not inferred from names or times."""
        return [dict(entry) for entry in self._manifest["timeline"]]

    def current(self) -> Instance | None:
        current = self._manifest["current"]
        if current is None:
            return None
        identity = next(item for item in self._manifest["identities"] if item["iid"] == current["iid"])
        return Instance(self.sid, identity["backend"], identity["iid"], identity["connection"], identity["native"])

    # ---- events -------------------------------------------------------------

    def _next_seq(self, instance: Instance) -> int:
        cached = self._seqs.get(instance.iid)
        if cached is not None:
            return cached
        events = self._instance_dir(instance.backend, instance.iid) / "events.jsonl"
        seq = 0
        if events.is_file():
            tail = io.last_line(events)
            if tail:
                record = json.loads(tail)
                if not isinstance(record.get("seq"), int):
                    raise ArchiveError(f"Instance {instance.iid} has a malformed event log.")
                seq = record["seq"]
        self._seqs[instance.iid] = seq
        return seq

    def append_event(self, instance: Instance, record: Mapping[str, Any]) -> dict:
        """Append one event to the instance's log with a stable ``e-<iid>-<seq>`` id and its session order;
        resuming an instance continues the same file. A copy with the assigned ids is returned.

        With a meter the capacity check, the append, the ledger credit and the index row share one
        serialized window: a refused write appends nothing, and a failed append is truncated back
        to the size the log had before."""
        self._check_instance(instance)
        event = dict(record)
        with self._lock:
            seq = self._next_seq(instance) + 1
            event["id"] = f"e-{instance.iid}-{seq:06d}"
            event["seq"] = seq
            event.setdefault("turn", 0)
            path = self._instance_dir(instance.backend, instance.iid) / "events.jsonl"
            line = json.dumps(event, ensure_ascii=False)
            if self._meter is None:
                io.append_line(path, line)
                self._seqs[instance.iid] = seq
                with _index(self._root).write() as db:
                    db.execute(
                        "INSERT OR REPLACE INTO locations (sid, record_id, generation, backend, iid, file, pos)"
                        " VALUES (?, ?, NULL, ?, ?, ?, ?)",
                        (self.sid, event["id"], instance.backend, instance.iid,
                         f"{instance.backend}/{instance.iid}/events.jsonl", seq))
            else:
                data = line.encode("utf-8") + b"\n"
                old_size = _file_bytes(path)
                with self._meter.txn() as tx:
                    tx.check("history", len(data))
                    try:
                        io.append_line(path, line)
                    except OSError:
                        _rollback_append(path, old_size, data)
                        raise
                    self._seqs[instance.iid] = seq
                    tx.record(f"{self.sid}/{instance.backend}/{instance.iid}/events.jsonl",
                              "history", _file_bytes(path))
                    tx.db.execute(
                        "INSERT OR REPLACE INTO locations (sid, record_id, generation, backend, iid, file, pos)"
                        " VALUES (?, ?, NULL, ?, ?, ?, ?)",
                        (self.sid, event["id"], instance.backend, instance.iid,
                         f"{instance.backend}/{instance.iid}/events.jsonl", seq))
            return event

    def events(self, instance: Instance) -> list[dict]:
        """This instance's own events in session order; another session's log is never opened."""
        self._check_instance(instance)
        path = self._instance_dir(instance.backend, instance.iid) / "events.jsonl"
        if not path.is_file():
            return []
        return [json.loads(line) for line in io.read_lines(path) if line.strip()]

    def _check_instance(self, instance: Instance) -> None:
        if (not isinstance(instance, Instance) or instance.sid != self.sid
                or self._identity_of(instance.connection, instance.native_id) is None):
            raise ArchiveError("The instance is not bound to this session.")

    # ---- snapshots: prepare, publish, abort ---------------------------------

    def _dumps_dir(self, instance: Instance) -> Path:
        directory = self._instance_dir(instance.backend, instance.iid) / "dumps"
        io.reject_link(directory)
        return directory

    def prepare_dump(self, instance: Instance, records: Iterable[Mapping[str, Any]], *,
                     evicted: Iterable[str] = ()) -> PreparedDump:
        """Stage one snapshot: fix the input boundary (``records`` as given), save it, then verify it by
        reading the staged gzip back. Nothing is published; the manifest still points at the old latest."""
        self._check_instance(instance)
        materialized = [dict(record) for record in records]
        evicted_ids = tuple(evicted)
        if any(not isinstance(item, str) or not item for item in evicted_ids):
            raise ArchiveError("The evicted range must be event ids of this session.")
        generation = (self._manifest["latest"] or 0) + 1
        parent = self._manifest["latest"]
        header = {
            "format": DUMP_FORMAT,
            "sid": self.sid,
            "backend": instance.backend,
            "iid": instance.iid,
            "generation": generation,
            "parent": parent,
            "covers": {"evicted": list(evicted_ids)},
        }
        lines = [json.dumps(header, ensure_ascii=False).encode("utf-8")]
        lines += [json.dumps(record, ensure_ascii=False).encode("utf-8") for record in materialized]
        dumps = self._dumps_dir(instance)
        dumps.mkdir(parents=True, exist_ok=True)
        staging = dumps / f"g{generation:06d}-{os.urandom(4).hex()}.staging"
        io.check_final_path(dumps / _dump_name(generation))
        io.gzip_write(staging, lines)
        # Verify before offering it: a staged dump that cannot be read back in full is never published.
        try:
            read_back = io.gzip_read_lines(staging)
        except (OSError, EOFError) as error:
            staging.unlink(missing_ok=True)
            raise ArchiveError("The staged snapshot failed verification.") from error
        if len(read_back) != len(lines) or json.loads(read_back[0])["generation"] != generation:
            staging.unlink(missing_ok=True)
            raise ArchiveError("The staged snapshot failed verification.")
        if self._meter is not None:
            try:
                with self._meter.txn() as tx:
                    tx.check("dump", _file_bytes(staging))
                    tx.record(
                        f"{self.sid}/{instance.backend}/{instance.iid}/dumps/{staging.name}",
                        "dump", _file_bytes(staging))
            except BaseException:
                staging.unlink(missing_ok=True)
                raise
        prepared = PreparedDump(generation, parent, instance, staging, io.sha256_file(staging),
                                evicted_ids, len(materialized))
        with self._lock:
            self._staged.add(prepared)
        return prepared

    def _claim_staged(self, prepared: object, what: str) -> PreparedDump:
        """Accept only a live handle this archive's own ``prepare_dump`` issued, refused before any
        path or delete operation: a fabricated handle, another archive's handle — even for the same
        sid — or one already published or aborted never reaches the filesystem checks that follow.
        ``publish`` re-verifies staleness inside the commit transaction, where the lock is held."""
        if not isinstance(prepared, PreparedDump):
            raise ArchiveError(f"{what} takes the object prepare_dump() returned.")
        instance = prepared.instance
        if not isinstance(instance, Instance) or instance.sid != self.sid:
            raise ArchiveError("The staged snapshot belongs to another session.")
        self._check_instance(instance)
        if prepared not in self._staged:
            raise ArchiveError("The staged snapshot was not issued by this archive or has already ended.")
        name = prepared.staging.name if isinstance(prepared.staging, Path) else ""
        pattern = rf"g{prepared.generation:06d}-[0-9a-f]{{8}}\.staging"
        if re.fullmatch(pattern, name) is None or prepared.staging.parent != self._dumps_dir(instance):
            raise ArchiveError("The staged snapshot path is not the one this archive issued.")
        return prepared

    def _require_committable(self, prepared: PreparedDump, final: Path) -> None:
        """Inside ``self._lock``: the handle is still live and its generation is still the next one —
        the loser of a parallel stage stops here. A refusal keeps the handle live for retry or abort;
        it is consumed only at the point of no return, right before the staged file is replaced."""
        expected = (self._manifest["latest"] or 0) + 1
        if prepared not in self._staged:
            raise ArchiveError("The staged snapshot was not issued by this archive or has already ended.")
        if (prepared.generation != expected or prepared.parent != self._manifest["latest"]
                or final.exists()):
            raise ArchiveError("The staged snapshot is stale; it cannot replace the current latest.")

    def publish(self, prepared: PreparedDump, summary: Mapping[str, Any],
                evicted: Iterable[str] | None = None,
                *, checkpoint: Callable[[Mapping[str, Any]], None] | None = None) -> dict:
        """Commit a staged snapshot: verify it is unchanged, move it to its final ``gNNNNNN.jsonl.gz`` name,
        then point the manifest's ``latest`` at it — inside one index transaction, so a failed publish leaves
        the previous generation latest, the old view intact and no half-committed snapshot.

        ``evicted`` is the event range actually removed from the active window (chosen once the summary
        succeeded); it defaults to the boundary declared at ``prepare_dump`` and may only narrow it, never
        widen past it or past what the staged dump holds. A record already owned by an earlier generation
        keeps that ownership: appearing again in a full view does not move it.

        ``checkpoint`` — an optional conditional-commit hook, run inside the commit window after the new
        manifest is durable but before the locator rows and the in-memory switch commit. It receives the
        generation entry. When it raises — the caller's own companion save failed — the previous manifest
        is restored, the just-landed snapshot is removed and the index transaction unwinds: a mid-commit
        failure never leaves the new file as ``latest``. The hook must not call back into this archive
        (the commit lock is held). Without it the behaviour is exactly as before."""
        self._claim_staged(prepared, "publish()")
        if not isinstance(summary, Mapping) or not summary:
            raise ArchiveError("A generation may only be published with its verified summary.")
        evicted_ids = list(prepared.evicted if evicted is None else evicted)
        if any(not isinstance(item, str) or not item for item in evicted_ids):
            raise ArchiveError("The evicted range must be event ids of this session.")
        if not set(evicted_ids) <= set(prepared.evicted):
            raise ArchiveError("The evicted range cannot widen past the boundary fixed at prepare_dump.")
        staging = prepared.staging
        dumps = self._dumps_dir(prepared.instance)
        final = dumps / _dump_name(prepared.generation)
        if final.exists() or io.is_link(final):
            raise ArchiveError(f"Generation {prepared.generation} already has a committed snapshot.")
        if not staging.is_file() or io.is_link(staging) or io.sha256_file(staging) != prepared.sha256:
            raise ArchiveError("The staged snapshot changed or is missing; publish refuses it.")
        lines = io.gzip_read_lines(staging)
        header = json.loads(lines[0]) if lines else {}
        expected = {"format": DUMP_FORMAT, "sid": self.sid, "backend": prepared.instance.backend,
                    "iid": prepared.instance.iid, "generation": prepared.generation,
                    "parent": prepared.parent}
        if any(header.get(key) != value for key, value in expected.items()):
            raise ArchiveError("The staged snapshot's header does not match its handle.")
        positions: dict[str, int] = {}
        for number, line in enumerate(lines[1:], start=2):
            record = json.loads(line)
            if isinstance(record.get("id"), str):
                positions.setdefault(record["id"], number)
        missing = [record_id for record_id in evicted_ids if record_id not in positions]
        if missing:
            raise ArchiveError(f"The staged snapshot does not hold evicted record(s) {missing!r}.")
        relative = f"{prepared.instance.backend}/{prepared.instance.iid}/dumps/{final.name}"
        entry = {
            "gen": prepared.generation,
            "parent": prepared.parent,
            "backend": prepared.instance.backend,
            "iid": prepared.instance.iid,
            "file": final.name,
            "sha256": prepared.sha256,
            "evicted": evicted_ids,
            "summary": dict(summary),
            "at": self._clock(),
        }
        if self._meter is None:
            with self._lock, _index(self._root).write() as db:
                self._require_committable(prepared, final)
                self._staged.discard(prepared)
                os.replace(staging, final)
                try:
                    updated = {**self._manifest,
                               "generations": self._manifest["generations"] + [entry],
                               "latest": prepared.generation}
                    io.atomic_write_text(self.dir / "manifest.json",
                                         json.dumps(updated, ensure_ascii=False, indent=2) + "\n")
                except BaseException:
                    final.unlink(missing_ok=True)
                    raise
                self._commit_checkpoint(checkpoint, entry, final)
                self._manifest = updated
                for record_id in evicted_ids:
                    db.execute(
                        "INSERT INTO locations (sid, record_id, generation, backend, iid, file, pos)"
                        " VALUES (?, ?, ?, ?, ?, ?, ?)"
                        " ON CONFLICT (sid, record_id) DO UPDATE SET"
                        " generation = excluded.generation, file = excluded.file, pos = excluded.pos"
                        " WHERE locations.generation IS NULL",
                        (self.sid, record_id, prepared.generation, prepared.instance.backend,
                         prepared.instance.iid, relative, positions[record_id]))
        else:
            inst = prepared.instance
            staging_rel = f"{self.sid}/{inst.backend}/{inst.iid}/dumps/{staging.name}"
            final_rel = f"{self.sid}/{inst.backend}/{inst.iid}/dumps/{final.name}"
            with self._lock, self._meter.txn() as tx:
                self._require_committable(prepared, final)
                updated = {**self._manifest,
                           "generations": self._manifest["generations"] + [entry],
                           "latest": prepared.generation}
                content = json.dumps(updated, ensure_ascii=False, indent=2) + "\n"
                manifest_path = self.dir / "manifest.json"
                delta = len(content.encode("utf-8")) - _file_bytes(manifest_path)
                if delta > 0:
                    tx.check("history", delta)
                self._staged.discard(prepared)
                os.replace(staging, final)
                try:
                    io.atomic_write_text(manifest_path, content)
                except BaseException:
                    final.unlink(missing_ok=True)
                    tx.drop(staging_rel)
                    tx.flush()
                    raise
                tx.move(staging_rel, final_rel)
                tx.record(f"{self.sid}/manifest.json", "history", _file_bytes(manifest_path))
                self._commit_checkpoint(checkpoint, entry, final,
                                        restore=lambda: self._restore_committed(tx, manifest_path, final_rel))
                self._manifest = updated
                for record_id in evicted_ids:
                    tx.db.execute(
                        "INSERT INTO locations (sid, record_id, generation, backend, iid, file, pos)"
                        " VALUES (?, ?, ?, ?, ?, ?, ?)"
                        " ON CONFLICT (sid, record_id) DO UPDATE SET"
                        " generation = excluded.generation, file = excluded.file, pos = excluded.pos"
                        " WHERE locations.generation IS NULL",
                        (self.sid, record_id, prepared.generation, prepared.instance.backend,
                         prepared.instance.iid, relative, positions[record_id]))
        return dict(entry)

    def _commit_checkpoint(self, checkpoint: Callable[[Mapping[str, Any]], None] | None,
                           entry: Mapping[str, Any], final: Path,
                           restore: Callable[[], None] | None = None) -> None:
        """Run the caller's conditional-commit hook inside the commit window. On a hook failure the
        previous manifest is written back and the just-landed snapshot removed, so a file committed
        mid-fault is never left as ``latest``; ``self._manifest`` and the index rows are untouched
        because the caller has not switched yet. ``restore`` carries the metered path's extra
        bookkeeping. If the old manifest itself cannot be restored, that is said plainly."""
        if checkpoint is None:
            return
        try:
            checkpoint(dict(entry))
        except BaseException as error:
            manifest_path = self.dir / "manifest.json"
            old_content = json.dumps(self._manifest, ensure_ascii=False, indent=2) + "\n"
            try:
                io.atomic_write_text(manifest_path, old_content)
            except OSError:
                final.unlink(missing_ok=True)
                if restore is not None:
                    restore()
                raise ArchiveError(
                    "The commit's checkpoint failed and the previous manifest could not be restored."
                ) from error
            final.unlink(missing_ok=True)
            if restore is not None:
                restore()
            raise

    def _restore_committed(self, tx: storage_meter.MeterTxn, manifest_path: Path, final_rel: str) -> None:
        """Meter bookkeeping for a checkpoint rollback, flushed before the transaction unwinds."""
        tx.drop(final_rel)
        tx.record(f"{self.sid}/manifest.json", "history", _file_bytes(manifest_path))
        tx.flush()

    def abort(self, prepared: PreparedDump) -> None:
        """Drop a staged snapshot: the previous latest generation stays the only valid view.
        Only a live handle this archive itself issued may delete its own staging file — an
        already-ended or foreign handle is refused before anything is removed."""
        self._claim_staged(prepared, "abort()")
        with self._lock:
            if prepared not in self._staged:
                raise ArchiveError("The staged snapshot was not issued by this archive or has already ended.")
            self._staged.discard(prepared)
        if self._meter is None:
            prepared.staging.unlink(missing_ok=True)
            return
        inst = prepared.instance
        rel = f"{self.sid}/{inst.backend}/{inst.iid}/dumps/{prepared.staging.name}"
        with self._meter.txn() as tx:
            prepared.staging.unlink(missing_ok=True)
            tx.drop(rel)

    # ---- committed-generation reads ----------------------------------------

    def read_generation(self, generation: int) -> dict:
        """This session's committed snapshot of one generation; see the module-level ``read_generation``."""
        return read_generation(self._root, self.sid, generation)

    def latest(self) -> tuple[int | None, int | None]:
        """``(latest committed generation, its parent)`` for this session."""
        return latest_generation(self._root, self.sid)

    def locate(self, record_id: str) -> dict | None:
        """Where one of this session's records lives, from the index."""
        return locate_record(self._root, self.sid, record_id)

    # ---- the user-turn recall ledger ----------------------------------------

    def budget(self) -> dict | None:
        """The live user turn's recall ledger (``turn-budget.json``), or None before the first delivery.
        It belongs to the logical session: switching backend, retrying and restarting never clears it."""
        path = self.dir / "turn-budget.json"
        if not path.is_file():
            return None
        data = io.read_json(path)
        if (not isinstance(data, dict) or not isinstance(data.get("turn"), str)
                or not isinstance(data.get("ranges"), list) or not isinstance(data.get("tokens"), int)):
            raise ArchiveError(f"Session {self.sid} has a malformed turn-budget ledger.")
        return data

    def save_budget(self, ledger: Mapping[str, Any]) -> None:
        """Persist the recall ledger atomically; ``{"turn": str, "ranges": [...], "tokens": int}``."""
        data = dict(ledger)
        if (not isinstance(data.get("turn"), str) or not data["turn"]
                or not isinstance(data.get("ranges"), list) or not isinstance(data.get("tokens"), int)
                or data["tokens"] < 0):
            raise ArchiveError("The recall ledger needs a turn id, delivered ranges and a token total.")
        content = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
        if self._meter is None:
            io.atomic_write_text(self.dir / "turn-budget.json", content)
            return
        path = self.dir / "turn-budget.json"
        with self._meter.txn() as tx:
            delta = len(content.encode("utf-8")) - _file_bytes(path)
            if delta > 0:
                tx.check("history", delta)
            io.atomic_write_text(path, content)
            tx.record(f"{self.sid}/turn-budget.json", "history", _file_bytes(path))


class _MeteredOutputs:
    """The session's output store behind the same gate: ``put`` passes the capacity check first and
    credits the bytes actually written; ``delete`` frees what the ledger held. Reads, expansion and
    listing delegate unchanged."""

    def __init__(self, store: OutputStore, archive: SessionArchive, meter: storage_meter.ArchiveMeter) -> None:
        self._store, self._archive, self._meter = store, archive, meter

    @property
    def root(self) -> Path:
        return self._store.root

    def put(self, text: str) -> str:
        data = text.encode("utf-8")
        prefix = f"{self._archive.sid}/output"
        with self._meter.txn() as tx:
            tx.check("history", len(data))
            reference = self._store.put(text)
            tx.record(f"{prefix}/{reference}.txt", "history",
                      _file_bytes(self._store.root / (reference + ".txt")))
        return reference

    def delete(self) -> None:
        prefix = f"{self._archive.sid}/output/"
        with self._meter.txn() as tx:
            self._store.delete()
            for rel in tx.files_under(prefix):
                tx.drop(rel)

    def __getattr__(self, name: str):
        return getattr(self._store, name)
