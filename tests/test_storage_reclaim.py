"""The autoclean reclaim pass on the real write path (``desktop.core.storage_reclaim``).

Synthetic roots only — nothing outside ``tmp_path`` is touched. The pass runs inside the
meter's serialized transaction when the gate refuses and the policy is ``autoclean``: the
oldest ledger-registered, manifest- and checksum-proven, non-latest committed dump file
is deleted first — the manifest record and index positions stay as the explicit gap.

Covered: deletion order and completeness; refusal with nothing deleted for the ``stop``
policy, an unauthorized ``dump``, a write too large for clearing everything to help, and
a ``history`` write that cannot borrow ``dump``'s share; the live ``latest`` generation,
live ``.staging`` files, unattested or checksum-mismatched files and unmetered pre-meter
data are never deleted; and a corrupt usage ledger is never rewritten to zero.

Budgets are set on the exact ledger-byte totals (``dump_budget_policy``): the global cap
stays huge so the dump share alone governs the verdict — the index WAL's drift between
transactions can never accidentally decide a test.
"""

from __future__ import annotations

import json
import secrets
from pathlib import Path

import pytest

from desktop.core import session_archive_io as io
from desktop.core.session_archive import GapError, SessionArchive, read_manifest
from desktop.core.storage_meter import ArchiveMeter, QuotaExceeded
from desktop.core.storage_policy import StoragePolicy, check_write


@pytest.fixture
def root(tmp_path: Path) -> Path:
    return tmp_path / "session"


class Gate:
    """The manager-side injection point: a live policy merged with the archive's own usage."""

    def __init__(self, policy: StoragePolicy | None = None) -> None:
        self.policy = policy or StoragePolicy()

    def __call__(self, category: str, incoming: int, archive_usage: dict) -> dict:
        return check_write(self.policy, {"cache": 0, **archive_usage}, category, incoming)


def meter_for(root: Path, gate: Gate | None = None) -> ArchiveMeter:
    return ArchiveMeter(root, gate=gate if gate is not None else Gate())


def ledger_files(root: Path) -> dict:
    return json.loads((root / "usage.json").read_text(encoding="utf-8"))["files"]


def dump_used(root: Path) -> int:
    return sum(entry["bytes"] for entry in ledger_files(root).values() if entry["cat"] == "dump")


def dump_budget_policy(budget: int, autoclean: tuple[str, ...] = ("dump",), *,
                       full_policy: str = "autoclean") -> StoragePolicy:
    """The dump category's budget is ``budget`` bytes; the global cap is huge, so the dump
    share alone governs — no other category's drift can influence the verdict."""
    max_bytes = 10**9
    return StoragePolicy(max_bytes=max_bytes,
                         shares={"history": 0.0, "dump": budget * 100.0 / max_bytes,
                                 "cache": 0.0, "index": 0.0},
                         full_policy=full_policy, autoclean=autoclean)


def dumps(root: Path, archive: SessionArchive, inst) -> Path:
    return root / archive.sid / inst.backend / inst.iid / "dumps"


def generation_file(root: Path, archive: SessionArchive, inst, gen: int) -> Path:
    return dumps(root, archive, inst) / f"g{gen:06d}.jsonl.gz"


def commit_generation(archive: SessionArchive, inst, text: str, seq: int) -> dict:
    event = archive.append_event(inst, {"type": "user", "text": text})
    prepared = archive.prepare_dump(inst, [{"id": event["id"], "seq": seq}], evicted=[event["id"]])
    archive.publish(prepared, {"summary": f"g{seq}"})
    return event


def stage(archive: SessionArchive, inst):
    """A ~200-byte staging file; the write that runs the reclaim pass in these tests."""
    return archive.prepare_dump(inst, [{"id": "x", "seq": 1}], evicted=["x"])


# ---- the real deletion: oldest eligible first, file only -------------------------------------------


def test_full_write_deletes_only_the_oldest_eligible_generation(root: Path):
    gate = Gate()
    meter = meter_for(root, gate)
    archive = SessionArchive.create(root, meter=meter)
    inst = archive.enter("gpt", "conn-a", "native-1")
    first = commit_generation(archive, inst, secrets.token_hex(8000), 1)
    commit_generation(archive, inst, "two", 2)
    commit_generation(archive, inst, "three", 3)                    # g3 = latest
    used = dump_used(root)
    g1 = generation_file(root, archive, inst, 1)
    gate.policy = dump_budget_policy(used - g1.stat().st_size + 250)  # one deletion must free enough

    prepared = stage(archive, inst)
    archive.abort(prepared)

    g2, g3 = (generation_file(root, archive, inst, n) for n in (2, 3))
    assert not g1.exists() and g2.is_file() and g3.is_file(), \
        "the oldest eligible file is deleted; a newer non-latest and the latest stay"
    manifest = read_manifest(root, archive.sid)
    assert [e["gen"] for e in manifest["generations"]] == [1, 2, 3], \
        "the manifest keeps the generation record — no rewrite under a live archive's cache"
    assert manifest["latest"] == 3 and manifest["generations"][2]["parent"] == 2, "the chain is untouched"
    with pytest.raises(GapError):
        archive.read_generation(1)  # the kept record reports a clean, deliberate gap
    assert archive.read_generation(2)["header"]["generation"] == 2
    location = archive.locate(first["id"])
    assert location["generation"] == 1 and location["file"].endswith("g000001.jsonl.gz"), \
        "index positions stay pointing at the removed snapshot — explicit missing, not hidden"
    assert f"{archive.sid}/gpt/{inst.iid}/dumps/g000001.jsonl.gz" not in ledger_files(root)
    assert meter.usage()["dump"] == g2.stat().st_size + g3.stat().st_size


def test_deletion_continues_oldest_first_until_the_write_fits(root: Path):
    gate = Gate()
    meter = meter_for(root, gate)
    archive = SessionArchive.create(root, meter=meter)
    inst = archive.enter("gpt", "conn-a", "native-1")
    commit_generation(archive, inst, secrets.token_hex(8000), 1)
    commit_generation(archive, inst, secrets.token_hex(4000), 2)
    commit_generation(archive, inst, "three", 3)                    # g3 = latest
    used = dump_used(root)
    g1, g2 = (generation_file(root, archive, inst, n) for n in (1, 2))
    # freeing g1 alone is not enough; freeing g1+g2 is — the latest must still remain
    gate.policy = dump_budget_policy(used - g1.stat().st_size - g2.stat().st_size + 250)

    prepared = stage(archive, inst)
    archive.abort(prepared)

    g3 = generation_file(root, archive, inst, 3)
    assert not g1.exists() and not g2.exists() and g3.is_file()
    manifest = read_manifest(root, archive.sid)
    assert manifest["latest"] == 3 and len(manifest["generations"]) == 3


# ---- refusal without any deletion ------------------------------------------------------------------


def test_stop_policy_and_unauthorized_dump_never_delete(root: Path):
    gate = Gate()
    meter = meter_for(root, gate)
    archive = SessionArchive.create(root, meter=meter)
    inst = archive.enter("gpt", "conn-a", "native-1")
    commit_generation(archive, inst, secrets.token_hex(8000), 1)
    commit_generation(archive, inst, "two", 2)
    used = dump_used(root)
    g1 = generation_file(root, archive, inst, 1)

    gate.policy = dump_budget_policy(used - g1.stat().st_size + 250, full_policy="stop")
    with pytest.raises(QuotaExceeded) as caught:
        stage(archive, inst)
    assert caught.value.verdict["reclaimable"] == [], "stop names nothing to clean"
    assert g1.is_file()

    gate.policy = dump_budget_policy(used - g1.stat().st_size + 250, autoclean=("cache", "index"))
    with pytest.raises(QuotaExceeded):
        stage(archive, inst)
    assert g1.is_file() and generation_file(root, archive, inst, 2).is_file(), \
        "an unauthorized dump is refused, not deleted"


def test_latest_and_staging_are_never_deleted(root: Path):
    gate = Gate()
    meter = meter_for(root, gate)
    archive = SessionArchive.create(root, meter=meter)
    inst = archive.enter("gpt", "conn-a", "native-1")
    commit_generation(archive, inst, secrets.token_hex(8000), 1)    # g1 is the latest: in use
    staged = archive.prepare_dump(inst, [{"id": "y", "seq": 1}], evicted=["y"])
    gate.policy = dump_budget_policy(dump_used(root) + 100)

    with pytest.raises(QuotaExceeded):
        stage(archive, inst)
    assert generation_file(root, archive, inst, 1).is_file(), "the live recall head is never deleted"
    # A live staged snapshot is registered in the ledger yet is not an eligible committed object.
    assert staged.staging.is_file(), "a live .staging the ledger registers is still never deleted"
    archive.abort(staged)


def test_clearing_everything_that_still_cannot_fit_deletes_nothing(root: Path):
    gate = Gate()
    meter = meter_for(root, gate)
    archive = SessionArchive.create(root, meter=meter)
    inst = archive.enter("gpt", "conn-a", "native-1")
    commit_generation(archive, inst, secrets.token_hex(8000), 1)    # g1
    commit_generation(archive, inst, "two", 2)                      # g2 = latest
    used = dump_used(root)
    g1 = generation_file(root, archive, inst, 1)
    # even dropping g1 leaves less room than the ~200-byte write needs
    gate.policy = dump_budget_policy(used - g1.stat().st_size + 100)

    with pytest.raises(QuotaExceeded):
        stage(archive, inst)
    assert g1.is_file(), "a write too large for a full clear keeps every candidate"
    assert not list(dumps(root, archive, inst).glob("*.staging")), "the refused staging is cleaned"


def test_history_write_cannot_borrow_dump_share(root: Path):
    gate = Gate()
    meter = meter_for(root, gate)
    archive = SessionArchive.create(root, meter=meter)
    inst = archive.enter("gpt", "conn-a", "native-1")
    commit_generation(archive, inst, secrets.token_hex(8000), 1)
    commit_generation(archive, inst, "two", 2)
    gate.policy = StoragePolicy(max_bytes=10**9, full_policy="autoclean", autoclean=("dump",),
                              shares={"history": 0.0, "dump": 100.0, "cache": 0.0, "index": 0.0})

    g1 = generation_file(root, archive, inst, 1)
    with pytest.raises(QuotaExceeded) as caught:
        archive.append_event(inst, {"type": "user", "text": "no room"})
    assert caught.value.verdict["reason"] == "over_category"
    assert g1.is_file(), "deleting another category cannot fix the writer's own share"


# ---- candidates that fail a proof are skipped --------------------------------------------------------


def test_unattested_file_is_never_deleted(root: Path):
    gate = Gate()
    meter = meter_for(root, gate)
    archive = SessionArchive.create(root, meter=meter)
    inst = archive.enter("gpt", "conn-a", "native-1")
    commit_generation(archive, inst, secrets.token_hex(8000), 1)
    commit_generation(archive, inst, "two", 2)
    foreign = dumps(root, archive, inst) / "g000099.jsonl.gz"
    io.gzip_write(foreign, [b'{"format": "wittycode-session-dump/1"}', b'{}'])  # unregistered, unattested
    used = dump_used(root)
    g1 = generation_file(root, archive, inst, 1)
    gate.policy = dump_budget_policy(used - g1.stat().st_size + 250)

    prepared = stage(archive, inst)
    archive.abort(prepared)
    assert foreign.is_file(), "a file the ledger never registered and the manifest never attested stays"
    assert not g1.exists(), "the real oldest eligible went instead"


def test_tampered_snapshot_is_never_deleted(root: Path):
    gate = Gate()
    meter = meter_for(root, gate)
    archive = SessionArchive.create(root, meter=meter)
    inst = archive.enter("gpt", "conn-a", "native-1")
    commit_generation(archive, inst, secrets.token_hex(8000), 1)
    commit_generation(archive, inst, "two", 2)
    g1 = generation_file(root, archive, inst, 1)
    tampered = b'tampered, not the attested snapshot'
    io.gzip_write(g1, [tampered])  # same name, wrong bytes: sha256 fails the manifest's record
    used = dump_used(root)
    gate.policy = dump_budget_policy(used - g1.stat().st_size + 250)

    with pytest.raises(QuotaExceeded):
        stage(archive, inst)
    assert io.gzip_read_lines(g1) == [tampered], "a checksum mismatch fails the proof — refused, kept"


# ---- unmetered data and the corrupt ledger -----------------------------------------------------------


def test_unmetered_data_is_never_deleted(root: Path):
    plain = SessionArchive.create(root)            # written before metering: a boundary, unmetered
    pinst = plain.enter("gpt", "conn-b", "native-2")
    plain.append_event(pinst, {"type": "user", "text": "old"})
    gate = Gate()
    meter = meter_for(root, gate)
    archive = SessionArchive.create(root, meter=meter)
    inst = archive.enter("gpt", "conn-a", "native-1")
    commit_generation(archive, inst, secrets.token_hex(8000), 1)
    commit_generation(archive, inst, "two", 2)
    assert meter.usage()["history"] is None, "unmetered content is the boundary, not zero"
    used = dump_used(root)
    g1 = generation_file(root, archive, inst, 1)
    gate.policy = dump_budget_policy(used - g1.stat().st_size + 250)

    prepared = stage(archive, inst)
    archive.abort(prepared)
    assert not g1.exists(), "the registered eligible snapshot freed room for the write"
    assert (root / plain.sid / "gpt" / pinst.iid / "events.jsonl").is_file(), \
        "nothing pre-meter or outside the ledger is ever touched"


def test_corrupt_ledger_is_never_rewritten_or_deleted_from(root: Path):
    gate = Gate()
    meter = meter_for(root, gate)
    archive = SessionArchive.create(root, meter=meter)
    inst = archive.enter("gpt", "conn-a", "native-1")
    commit_generation(archive, inst, secrets.token_hex(8000), 1)
    commit_generation(archive, inst, "two", 2)
    g1 = generation_file(root, archive, inst, 1)
    garbage = b"not a usage ledger {"
    meter.ledger_path.write_bytes(garbage)
    assert meter.usage()["history"] is None and meter.describe()["ledger_broken"]

    gate.policy = dump_budget_policy(10**9)
    with pytest.raises(QuotaExceeded):
        archive.append_event(inst, {"type": "user", "text": "denied"})
    assert meter.ledger_path.read_bytes() == garbage, "the corrupt ledger is evidence, not a clean zero"
    assert g1.is_file(), "nothing is deleted while usage cannot be vouched for"

    SessionArchive.open(root, archive.sid, meter=meter)  # reconcile must not clobber it either
    assert meter.ledger_path.read_bytes() == garbage

    ungated = SessionArchive.open(root, archive.sid, meter=ArchiveMeter(root))
    with pytest.raises(QuotaExceeded):
        ungated.append_event(inst, {"type": "user", "text": "ungated"})
    assert meter.ledger_path.read_bytes() == garbage, "even ungated metering fails closed"
