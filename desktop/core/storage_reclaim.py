"""Autoclean inside a refused write's serialized window (``session-context-recall-final.md`` §7).

When the capacity gate refuses a write and the user's policy is ``autoclean``, the oldest
*eligible* managed object is deleted inside the same transaction that will record the new
bytes, then the gate is re-run; nothing deletable means the write stays refused — the
``stop`` policy never reaches here (an empty ``reclaimable`` list keeps the caller out).

Eligible means all of, proven against disk inside the transaction:

* registered — a usage-ledger entry of this meter, so it was written through the managed
  path after metering took effect; pre-meter and foreign files are never in the ledger;
* attributable — the path ``<sid>/<backend>/<iid>/dumps/gNNNNNN.jsonl.gz``, a manifest
  generation record attesting the same backend, instance and file, and the file's own
  dump header agreeing; the file's sha256 must equal the manifest's recorded checksum;
* unused — not the session's ``latest`` generation (the live recall head).

Only the ``dump`` category has a real deletion pass today: committed snapshots, oldest
first. The pass deletes the snapshot *file* and drops its ledger entry — nothing else.
The manifest's generation record, the index locations and the checksum are deliberately
kept: an open ``SessionArchive`` caches its manifest, so rewriting it here would be
resurrected on the archive's next save, and keeping the record is what lets recall report
a clear, deliberate gap (``已清理或缺失的档案标明缺口``) instead of fabricating history —
the ``parent`` chain is never touched. The original events stay in ``events.jsonl``.

``.staging`` files (a live ``prepare_dump`` may still publish them; their owner is
unknowable across processes), event logs, manifests, turn ledgers, retained outputs and
the index database itself are never candidates. ``history``, ``cache`` and ``index`` may
stay authorized in settings but have no deletable objects — their writes keep refusing.

A candidate that fails any check is skipped, not forced; a file that cannot be unlinked
is left alone. No bytes are freed for a ``usage_unknown`` verdict or a refusal the
category cannot fix, and nothing is deleted when clearing every eligible object still
could not fit the write. When nothing qualifies the caller's original verdict stands.
``storage_meter.MeterTxn.check`` drives this; nothing here ever scans user history.
"""

from __future__ import annotations

import json
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from desktop.core import session_archive
from desktop.core import session_archive_io as io

if TYPE_CHECKING:
    from desktop.core import storage_meter

DELETABLE = ("dump",)
"""Categories with a real deletion pass; every other authorized category keeps refusing writes."""

_DUMP_PATH = re.compile(
    rf"\A(?P<sid>[A-Za-z0-9]{{{session_archive.SID_LEN}}})/"
    rf"(?P<backend>[a-z0-9][a-z0-9_-]{{0,31}})/"
    rf"(?P<iid>[A-Za-z0-9]{{{session_archive.IID_LEN}}})/"
    rf"dumps/(?P<file>g(?P<gen>\d{{6,}})\.jsonl\.gz)\Z")
"""A ledger path that can only be a committed snapshot; staging names never match."""


def _file_bytes(path: Path) -> int:
    try:
        info = path.lstat()
    except OSError:
        return 0
    return info.st_size if stat.S_ISREG(info.st_mode) else 0


@dataclass(frozen=True)
class _Candidate:
    """A committed generation proven deletable inside the current transaction."""

    rel: str
    sid: str
    gen: int
    path: Path
    sha256: str
    bytes: int
    order: float


def _manifest(meter_root: Path, sid: str, cache: dict[str, dict | None]) -> dict | None:
    """The session's validated manifest, read once per reclaim pass; None when it cannot vouch."""
    if sid not in cache:
        try:
            cache[sid] = session_archive.read_manifest(meter_root, sid)
        except Exception:
            cache[sid] = None
    return cache[sid]


def _attested(path: Path, sid: str, backend: str, iid: str, gen: int) -> bool:
    """The file's own dump header names this session and generation — attribution the
    file itself confirms, on top of the manifest record and the ledger path."""
    try:
        lines = io.gzip_read_lines(path)
    except (OSError, EOFError):
        return False
    if not lines:
        return False
    try:
        header = json.loads(lines[0])
    except ValueError:
        return False
    return (header.get("format") == session_archive.DUMP_FORMAT
            and header.get("sid") == sid and header.get("backend") == backend
            and header.get("iid") == iid and header.get("generation") == gen)


def _candidate(tx: storage_meter.MeterTxn, rel: str, entry: dict, match: re.Match,
               manifests: dict[str, dict | None]) -> _Candidate | None:
    """All eligibility proofs for one ledger path; any doubt returns None — the file stays."""
    root = tx._meter.root
    sid, backend, iid = match["sid"], match["backend"], match["iid"]
    manifest = _manifest(root, sid, manifests)
    if manifest is None:
        return None
    gen = int(match["gen"])
    if manifest["latest"] == gen:
        return None  # the live recall head is in use by definition
    record = next((item for item in manifest["generations"] if item["gen"] == gen), None)
    if record is None or (record["backend"], record["iid"], record["file"]) != (backend, iid, match["file"]):
        return None  # the manifest does not attest this file at this path — cannot attribute
    try:
        path = io.descend(root, sid, backend, iid, "dumps", match["file"])
    except io.ArchiveIOError:
        return None
    if not path.is_file():
        return None  # already gone — a stale ledger entry is reconcile's business, frees nothing
    if io.sha256_file(path) != record["sha256"] or not _attested(path, sid, backend, iid, gen):
        return None  # not provably the attested snapshot — never delete the uncertain
    at = record.get("at")
    return _Candidate(rel, sid, gen, path, record["sha256"], entry["bytes"],
                      float(at) if isinstance(at, (int, float)) else float(gen))


def _eligible(tx: storage_meter.MeterTxn) -> list[_Candidate]:
    """Every ledger-registered committed snapshot that passes all proofs, oldest first."""
    manifests: dict[str, dict | None] = {}
    found = []
    for rel, entry in tx._files.items():
        if entry["cat"] != "dump":
            continue
        match = _DUMP_PATH.fullmatch(rel)
        if match is None:
            continue
        candidate = _candidate(tx, rel, entry, match, manifests)
        if candidate is not None:
            found.append(candidate)
    return sorted(found, key=lambda item: (item.order, item.rel))


def _drop(tx: storage_meter.MeterTxn, candidate: _Candidate) -> bool:
    """Delete one proven candidate: re-verify the checksum at the point of deletion, unlink
    the file, drop its ledger entry. The manifest record and index positions stay behind
    as the explicit gap — a later manifest save resurrects nothing because nothing changed."""
    if io.sha256_file(candidate.path) != candidate.sha256:
        return False  # changed since the scan — no longer the attested file
    try:
        candidate.path.unlink()
    except OSError:
        return False  # still held open or refused — left untouched, the caller tries the next one
    tx.drop(candidate.rel)
    return True


def reclaim(tx: storage_meter.MeterTxn, category: str, incoming: int, verdict: dict) -> dict:
    """Delete eligible objects oldest-first until ``incoming`` fits or nothing qualifies.

    Returns the verdict the write should act on: the original one when no deletion could
    help — a refusal the category cannot fix, or a write so large that clearing every
    eligible object still would not fit (the gate is asked with the fully-cleared usage
    first, so nothing is deleted for a lost cause) — or the fresh verdict after the last
    deletion. ``reclaimed`` lists the ledger paths actually dropped."""
    verdict = dict(verdict)
    reason = verdict.get("reason")
    authorized = [name for name in verdict.get("reclaimable", []) if name in DELETABLE]
    if reason not in ("over_category", "over_global") or not authorized:
        return verdict  # usage_unknown and unrelated refusals never free bytes
    if reason == "over_category" and category not in authorized:
        return verdict  # another category's share cannot be borrowed
    candidates = _eligible(tx)
    if not candidates:
        return verdict
    gate = tx._meter._gate
    if gate is None:
        return verdict
    cleared = dict(tx.usage())
    cleared["dump"] = (cleared.get("dump") or 0) - sum(item.bytes for item in candidates)
    if not gate(category, incoming, cleared).get("allowed"):
        return verdict  # clearing everything eligible still cannot fit — delete nothing
    dropped = []
    for candidate in candidates:
        if _drop(tx, candidate):
            dropped.append(candidate.rel)
            verdict = dict(tx._verdict(category, incoming))
            if verdict["allowed"]:
                break
    if dropped:
        verdict["reclaimed"] = dropped
    return verdict
