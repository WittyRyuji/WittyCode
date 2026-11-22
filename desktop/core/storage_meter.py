"""Per-write usage metering for the session archive root (final spec ``outputs/session-context-recall-final.md`` §7).

The archive keeps its own occupancy ledger, ``<root>/usage.json``: every byte the session archive
manages is credited to ``history`` or ``dump`` at write time, and the rebuildable index —
``index.sqlite``, its WAL/SHM sidecars, and the ledger file itself — is measured by an explicit
stat list. A ledger update only happens inside the shared index's serialized writer transaction,
so archives in different processes queue on the same lock and never double-count.

Nothing here scans user history. ``usage()`` reads one ledger file and stats a short, explicit
list of managed paths; the only listing it makes is the archive root's own top level, and any
entry the ledger does not cover is reported as *unmetered* — the boundary — instead of being
measured or treated as zero. Pre-existing session data outside this root is out of scope: it is
neither migrated nor scanned.

Cleanup support scope (``CLEANUP_SCOPE``): by default only ``cache`` and ``index`` are eligible
(``storage_policy.DEFAULT_AUTOCLEAN``). Today only ``dump`` has a real deletion pass
(``desktop.core.storage_reclaim``): committed snapshot *files*, oldest first, and only the ones
the ledger registers and the manifest plus the file's own checksum and header attest. The
manifest's generation record, index positions and the checksum stay behind as an explicit gap —
rewriting the manifest under a live ``SessionArchive``'s cache would just be resurrected on its
next save. ``index.sqlite`` is the in-use database every transaction locks on — never deleted;
``usage.json`` is the meter's ledger — deleting it would blind the accounting, never in scope.
The other categories keep their authorized flag but have nothing deletable: they keep refusing.

Pure: no Qt.
"""

from __future__ import annotations

import json
import stat
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from desktop.core import session_archive_io as io

USAGE_NAME = "usage.json"
USAGE_FORMAT = "wittycode-session-usage/1"
CATEGORIES = ("history", "dump")
"""The categories this ledger accounts for; ``index`` is measured live, ``cache`` is not the archive's."""
INDEX_FILES = (io.INDEX_NAME, io.INDEX_NAME + "-wal", io.INDEX_NAME + "-shm", io.INDEX_NAME + "-journal", USAGE_NAME)
BOOKKEEPING = frozenset(INDEX_FILES)
"""Top-level names that are never unmetered content: the rebuildable index and this ledger."""

CLEANUP_SCOPE = {
    "cache": "未实现：应用缓存目录在归档计量之外，清理未接入——勾选不生效，满额仍拒写",
    "index": "未实现：index.sqlite 是每个写事务都在用的数据库与串行锁点，usage.json 是计量账本——都不删除；勾选不生效，满额仍拒写",
    "history": "未实现：完整历史是原始记录（manifest、turn 账本、events.jsonl、保留输出都在使用或是唯一副本），不删除；勾选不生效，满额仍拒写",
    "dump": "已启用：满额时在同一写事务内删最老的非最新已提交快照文件——仅限账本登记、manifest 证实且 sha 与文件头归属一致的对象；manifest 代次记录与位置保留为明确缺口，最新代与 .staging 暂存不动，原始事件留在 events.jsonl",
}
"""What an autoclean pass may touch today, per category (reported to the Storage page as-is)."""


class QuotaExceeded(RuntimeError):
    """A gated archive write did not fit the storage policy; raised before anything is committed.

    ``verdict`` is the ``storage_policy.check_write`` verdict (``allowed``/``reason``/
    ``overAlert``/``reclaimable``) for the caller to report or clean-and-retry on."""

    def __init__(self, verdict: Mapping[str, Any]):
        self.verdict = dict(verdict)
        super().__init__(f"Archive write refused: {self.verdict.get('reason', 'over_quota')}")


def _file_bytes(path: Path) -> int:
    """Bytes of a regular file; 0 for anything missing or not a plain file."""
    try:
        info = path.lstat()
    except OSError:
        return 0
    return info.st_size if stat.S_ISREG(info.st_mode) else 0


def _totals(ledger: dict | None) -> dict[str, int]:
    totals = {name: 0 for name in CATEGORIES}
    if ledger:
        for entry in ledger["files"].values():
            totals[entry["cat"]] += entry["bytes"]
    return totals


class MeterTxn:
    """The in-transaction view of the ledger; the archive calls check → write → record → index → commit.

    Obtained from :meth:`ArchiveMeter.txn`; ``db`` is the shared index's writer connection, so a
    caller's index rows commit in the same transaction that flushes the ledger."""

    def __init__(self, meter: ArchiveMeter, db) -> None:
        self.db = db
        self._meter = meter
        raw = meter._read_ledger()
        self._broken = raw is None and meter.ledger_path.exists()
        """A ledger file that failed validation. It is never overwritten with a fresh one —
        that would report the corrupt state as a clean zero — and every check fails closed."""
        self._ledger = raw if raw is not None else meter._new_ledger()
        self._files: dict[str, dict[str, Any]] = self._ledger["files"]
        self._dirty = False

    def usage(self) -> dict[str, int]:
        """Archive-side usage inside this transaction: ``history``/``dump`` from the ledger,
        ``index`` from live stat. The gate merges app-side categories like ``cache``.

        This is the registered-byte view: a top-level entry the ledger does not cover stays
        outside the accounting here too (the public :meth:`ArchiveMeter.usage` reports it as
        the unmetered boundary, and adoption happens through ``open``'s reconcile — never by
        scanning). A broken ledger stays unusable: :meth:`_verdict` short-circuits on
        ``_broken`` before this view is even consulted."""
        totals = _totals(self._ledger)
        totals["index"] = self._meter._index_bytes()
        return totals

    def _verdict(self, category: str, incoming: int) -> dict:
        """The gate's verdict for ``incoming`` against this transaction's usage view.

        A corrupt ledger is unusable evidence: the check refuses ``usage_unknown`` even with
        no gate, so no write proceeds unaccounted and the broken file is left for the user."""
        if self._broken:
            return {"allowed": False, "reason": "usage_unknown", "overAlert": False, "reclaimable": []}
        gate = self._meter._gate
        if gate is None:
            return {"allowed": True, "reason": "ungated", "overAlert": False, "reclaimable": []}
        return gate(category, incoming, self.usage())

    def check(self, category: str, incoming: int) -> dict:
        """The authoritative gate verdict, or ``QuotaExceeded``.

        Called inside the transaction so the decision and the accounting it leads to cannot race
        another process. Without a gate the write is metered but not limited. A refused verdict
        carrying ``reclaimable`` first runs one autoclean pass in this same serialized window
        (``desktop.core.storage_reclaim``): the oldest eligible managed objects go, the gate is
        re-run, and only what survives that is refused or allowed."""
        verdict = self._verdict(category, incoming)
        if not verdict.get("allowed") and verdict.get("reclaimable"):
            from desktop.core import storage_reclaim
            verdict = storage_reclaim.reclaim(self, category, incoming, verdict)
        if not verdict.get("allowed"):
            raise QuotaExceeded(verdict)
        return verdict

    def record(self, relpath: str, category: str, nbytes: int | None) -> None:
        """Set the ledger's byte count for one managed file (a root-relative ``/`` path); ``None`` drops it."""
        if nbytes is None:
            self.drop(relpath)
            return
        if (category not in CATEGORIES or not isinstance(nbytes, int)
                or isinstance(nbytes, bool) or nbytes < 0):
            raise ValueError(f"Illegal meter record: {relpath!r} {category!r} {nbytes!r}")
        self._files[relpath] = {"cat": category, "bytes": nbytes}
        self._dirty = True

    def move(self, old_rel: str, new_rel: str) -> None:
        """Rename a ledger entry — a staged snapshot becoming its committed name keeps its bytes."""
        entry = self._files.pop(old_rel, None)
        if entry is not None:
            self._files[new_rel] = entry
            self._dirty = True

    def drop(self, relpath: str) -> int:
        """Remove a ledger entry; returns the bytes it freed."""
        entry = self._files.pop(relpath, None)
        if entry is None:
            return 0
        self._dirty = True
        return entry["bytes"]

    def files_under(self, prefix: str) -> list[str]:
        return sorted(rel for rel in self._files if rel.startswith(prefix))

    def flush(self) -> None:
        """Persist the ledger now — also used on rollback paths, before the transaction unwinds.
        A corrupt ledger is never rewritten: the broken file stays as evidence, not a clean zero."""
        if self._dirty and not self._broken:
            self._meter._write_ledger(self._ledger)
            self._dirty = False


class ArchiveMeter:
    """The shared occupancy meter for one archive root; pass it to ``SessionArchive`` as ``meter``.

    ``gate`` is ``(category, incoming, archive_usage) -> verdict`` — normally a closure over
    ``storage_policy.check_write`` that merges the archive-side figures this object supplies with
    the application's other categories. It is consulted inside the writer transaction, so it should
    read live configuration rather than a snapshot. ``None`` meters without limiting."""

    def __init__(self, root: Path | str, gate: Callable[[str, int, Mapping[str, int]], dict] | None = None,
                 *, clock: Callable[[], float] = time.time) -> None:
        self._root = Path(root)
        self._gate = gate
        self._clock = clock

    @property
    def root(self) -> Path:
        return self._root

    @property
    def ledger_path(self) -> Path:
        return self._root / USAGE_NAME

    # ---- ledger ------------------------------------------------------------

    def _new_ledger(self) -> dict:
        return {
            "format": USAGE_FORMAT,
            "boundary": {
                "created": self._clock(),
                "note": "usage covers bytes written through this meter from this point on; "
                        "anything older or foreign stays unmetered and is never scanned",
            },
            "files": {},
        }

    def _read_ledger(self) -> dict | None:
        path = self.ledger_path
        if not path.is_file() or io.is_link(path):
            return None
        try:
            data = io.read_json(path)
        except (OSError, ValueError):
            return None
        files = data.get("files") if isinstance(data, dict) else None
        if (not isinstance(data, dict) or data.get("format") != USAGE_FORMAT
                or not isinstance(data.get("boundary"), dict) or not isinstance(files, dict)):
            return None
        for rel, entry in files.items():
            if (not isinstance(rel, str) or not isinstance(entry, dict)
                    or entry.get("cat") not in CATEGORIES
                    or not isinstance(entry.get("bytes"), int) or isinstance(entry.get("bytes"), bool)
                    or entry["bytes"] < 0):
                return None
        return data

    def _write_ledger(self, ledger: dict) -> None:
        io.atomic_write_text(self.ledger_path, json.dumps(ledger, ensure_ascii=False, indent=2) + "\n")

    # ---- figures -----------------------------------------------------------

    def _index_bytes(self) -> int:
        return sum(_file_bytes(self._root / name) for name in INDEX_FILES)

    def _unmetered(self, ledger: dict | None) -> list[str]:
        """Top-level entries the ledger does not cover — the boundary, reported, never opened."""
        try:
            names = [entry.name for entry in self._root.iterdir() if not io.is_link(entry)]
        except OSError:
            return []
        covered = {rel.split("/", 1)[0] for rel in ledger["files"]} if ledger else set()
        return sorted(name for name in names if name not in BOOKKEEPING and name not in covered)

    def usage(self) -> dict[str, int | None]:
        """Archive-side occupancy: ``history``/``dump`` from the ledger, ``index`` from live stat.

        ``None`` for a category the meter cannot vouch for: a corrupt ledger or unmetered entries
        under the root. A missing or bookkeeping-only root is a known empty archive — 0, not
        unknown — so the first metered write can pass its own gate."""
        ledger = self._read_ledger()
        unmetered = self._unmetered(ledger)
        broken = ledger is None and self.ledger_path.exists()
        known = not broken and not unmetered
        totals = _totals(ledger)
        return {
            "history": totals["history"] if known else None,
            "dump": totals["dump"] if known else None,
            "index": self._index_bytes(),
        }

    def describe(self) -> dict:
        """What the Storage page may show about the metered scope: root, boundary, coverage, cleanup."""
        ledger = self._read_ledger()
        return {
            "root": str(self._root),
            "present": self._root.is_dir(),
            "ledger": ledger is not None,
            "ledger_broken": ledger is None and self.ledger_path.exists(),
            "files": len(ledger["files"]) if ledger else 0,
            "boundary": dict(ledger["boundary"]) if ledger else None,
            "unmetered": self._unmetered(ledger),
            "cleanup": dict(CLEANUP_SCOPE),
        }

    def check_now(self, category: str, incoming: int) -> dict:
        """Advisory gate check outside the transaction — fail fast before expensive IO.

        ``MeterTxn.check`` inside the transaction stays authoritative; this one can only race.
        A corrupt ledger refuses here too: it is unusable evidence, never silently zero."""
        if self._read_ledger() is None and self.ledger_path.exists():
            raise QuotaExceeded({"allowed": False, "reason": "usage_unknown",
                                 "overAlert": False, "reclaimable": []})
        if self._gate is None:
            return {"allowed": True, "reason": "ungated", "overAlert": False, "reclaimable": []}
        verdict = self._gate(category, incoming, self.usage())
        if not verdict.get("allowed"):
            raise QuotaExceeded(verdict)
        return verdict

    # ---- the serialized write window ---------------------------------------

    @contextmanager
    def txn(self) -> Iterator[MeterTxn]:
        """One serialized metering window: the shared index's writer transaction serializes every
        process metering this root. The ledger is flushed inside it, before the index rows commit,
        so a crash can only overcount (fail closed, repaired by the next reconcile) — never lose
        accounted bytes."""
        io.reject_link(self._root)
        self._root.mkdir(parents=True, exist_ok=True)
        index = io.Index(self._root / io.INDEX_NAME)
        with index.write() as db:
            tx = MeterTxn(self, db)
            yield tx
            tx.flush()
