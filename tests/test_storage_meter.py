"""Metered session-archive usage and the write gate (``desktop.core.storage_meter`` + archive wiring).

Synthetic roots only — nothing outside ``tmp_path`` is touched. Covered: per-write evidence counters
for history/dump/index (sqlite + WAL + the ledger file), the serialized ledger shared by several
archive objects and across processes, gated writes that refuse over quota and roll back partial work
while the old generation stays valid, adoption of previously unmetered sessions through the explicit
stat list, and the archive root's unmetered-entry boundary. The gate is the same factory the manager
injects: ``ArchiveMeter(root, gate=(category, incoming, archive_usage) -> check_write verdict)``,
evaluated inside the writer transaction so a policy read stays live."""

from __future__ import annotations

import json
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from desktop.core import session_archive_io as io
from desktop.core import storage_meter, storage_policy
from desktop.core.session_archive import SessionArchive
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


def manifest_bytes(root: Path, sid: str) -> int:
    return (root / sid / "manifest.json").stat().st_size


def events_path(root: Path, archive: SessionArchive, instance) -> Path:
    return root / archive.sid / instance.backend / instance.iid / "events.jsonl"


def bind(archive: SessionArchive, backend="gpt", connection="conn-a", native="native-1"):
    return archive.enter(backend, connection, native)


# ---- usage figures: real evidence, not a demo -----------------------------------------------


def test_missing_root_reports_a_known_zero_and_creates_nothing(root: Path):
    meter = meter_for(root)
    assert meter.usage() == {"history": 0, "dump": 0, "index": 0}
    assert not root.exists(), "asking for usage must not create the archive"


def test_every_archive_write_is_credited_to_its_category(root: Path):
    meter = meter_for(root)
    archive = SessionArchive.create(root, meter=meter)
    assert meter.usage()["history"] == manifest_bytes(root, archive.sid)

    inst = bind(archive)
    assert meter.usage()["history"] == manifest_bytes(root, archive.sid)

    archive.append_event(inst, {"type": "user", "text": "synthetic"})
    expected = manifest_bytes(root, archive.sid) + events_path(root, archive, inst).stat().st_size
    assert meter.usage()["history"] == expected

    event = archive.append_event(inst, {"type": "assistant", "text": "more"})
    prepared = archive.prepare_dump(inst, [{"id": event["id"], "seq": 1}], evicted=[event["id"]])
    assert meter.usage()["dump"] == prepared.staging.stat().st_size
    archive.publish(prepared, {"summary": "s"})
    final = root / archive.sid / "gpt" / inst.iid / "dumps" / "g000001.jsonl.gz"
    assert meter.usage()["dump"] == final.stat().st_size
    assert meter.usage()["history"] == manifest_bytes(root, archive.sid) + events_path(root, archive, inst).stat().st_size


def test_index_usage_covers_sqlite_wal_sidecars_and_the_ledger(root: Path):
    meter = meter_for(root)
    archive = SessionArchive.create(root, meter=meter)
    bind(archive)
    expected = sum(
        (root / name).stat().st_size
        for name in ("index.sqlite", "index.sqlite-wal", "index.sqlite-shm", "index.sqlite-journal", "usage.json")
        if (root / name).exists())
    assert expected > 0
    assert meter.usage()["index"] == expected


def test_turn_budget_and_retained_outputs_are_metered(root: Path):
    meter = meter_for(root)
    archive = SessionArchive.create(root, meter=meter)
    bind(archive)
    archive.save_budget({"turn": "t1", "ranges": [], "tokens": 5})
    ledger = root / archive.sid / "turn-budget.json"
    assert ledger.is_file()

    store = archive.outputs
    reference = store.put("synthetic retained output")
    output_file = root / archive.sid / "output" / (reference + ".txt")
    output_bytes = output_file.stat().st_size
    with_outputs = meter.usage()["history"]
    store.delete()
    assert meter.usage()["history"] == with_outputs - output_bytes


# ---- the gate: refusal stops the write and the old state stays --------------------------------


def test_refused_append_leaves_the_log_and_index_untouched(root: Path):
    gate = Gate()
    meter = meter_for(root, gate)
    archive = SessionArchive.create(root, meter=meter)
    inst = bind(archive)
    archive.append_event(inst, {"type": "user", "text": "first"})
    before = meter.usage()

    gate.policy = StoragePolicy(max_bytes=before["history"], shares={"history": 100.0, "dump": 0.0, "cache": 0.0, "index": 0.0})
    path = events_path(root, archive, inst)
    old = path.read_bytes()
    with pytest.raises(QuotaExceeded) as caught:
        archive.append_event(inst, {"type": "user", "text": "denied"})
    assert caught.value.verdict["reason"] == "over_category"
    assert path.read_bytes() == old, "a refused append writes nothing"
    assert meter.usage() == before
    assert archive.locate(f"e-{inst.iid}-000002") is None
    assert archive.events(inst)[-1]["text"] == "first"


def test_refused_dump_leaves_no_staging_file(root: Path):
    gate = Gate(StoragePolicy(max_bytes=10**9,
                              shares={"history": 100.0, "dump": 0.0, "cache": 0.0, "index": 0.0}))
    meter = meter_for(root, gate)
    archive = SessionArchive.create(root, meter=meter)
    inst = bind(archive)
    archive.append_event(inst, {"type": "user", "text": "x"})
    with pytest.raises(QuotaExceeded):
        archive.prepare_dump(inst, [{"id": "e", "seq": 1}])
    dumps = root / archive.sid / "gpt" / inst.iid / "dumps"
    assert not list(dumps.glob("*.staging")), "a refused snapshot cleans its staging file"
    assert meter.usage()["dump"] == 0
    assert archive.latest() == (None, None)


def test_refused_publish_keeps_the_staged_dump_and_the_old_latest(root: Path):
    gate = Gate()
    meter = meter_for(root, gate)
    archive = SessionArchive.create(root, meter=meter)
    inst = bind(archive)
    event = archive.append_event(inst, {"type": "user", "text": "x"})
    prepared = archive.prepare_dump(inst, [{"id": event["id"], "seq": 1}], evicted=[event["id"]])

    gate.policy = StoragePolicy(max_bytes=1, shares={"history": 100.0, "dump": 0.0, "cache": 0.0, "index": 0.0})
    with pytest.raises(QuotaExceeded):
        archive.publish(prepared, {"summary": "s"})
    assert prepared.staging.is_file(), "a refused publish keeps the prepared snapshot for retry or abort"
    assert meter.usage()["dump"] == prepared.staging.stat().st_size
    assert archive.latest() == (None, None)

    archive.abort(prepared)
    assert not prepared.staging.exists()
    assert meter.usage()["dump"] == 0


# ---- failed writes roll back ------------------------------------------------------------------


def test_failed_publish_unlinks_the_final_and_frees_its_bytes(root: Path, monkeypatch):
    meter = meter_for(root)
    archive = SessionArchive.create(root, meter=meter)
    inst = bind(archive)
    event = archive.append_event(inst, {"type": "user", "text": "x"})
    prepared = archive.prepare_dump(inst, [{"id": event["id"], "seq": 1}], evicted=[event["id"]])

    real_write = io.atomic_write_text

    def fail_manifest(path, text):
        if Path(path).name == "manifest.json":
            raise OSError("simulated disk failure")
        return real_write(path, text)

    monkeypatch.setattr(io, "atomic_write_text", fail_manifest)
    with pytest.raises(OSError):
        archive.publish(prepared, {"summary": "s"})
    dumps = root / archive.sid / "gpt" / inst.iid / "dumps"
    assert not (dumps / "g000001.jsonl.gz").exists(), "the half-committed final is removed"
    assert not list(dumps.glob("*.staging"))
    assert meter.usage()["dump"] == 0, "the deleted snapshot's bytes leave the ledger"
    assert archive.latest() == (None, None)


def test_failed_append_truncates_back_to_the_old_log(root: Path, monkeypatch):
    meter = meter_for(root)
    archive = SessionArchive.create(root, meter=meter)
    inst = bind(archive)
    archive.append_event(inst, {"type": "user", "text": "ok"})
    path = events_path(root, archive, inst)
    old = path.read_bytes()

    real_append = io.append_bytes

    def partial(target, data):
        real_append(target, data[: len(data) // 2])
        raise OSError("simulated partial write")

    monkeypatch.setattr(io, "append_bytes", partial)
    with pytest.raises(OSError):
        archive.append_event(inst, {"type": "user", "text": "denied"})
    assert path.read_bytes() == old, "a partial append is truncated back to the log's old size"


def test_refused_output_put_writes_nothing(root: Path):
    gate = Gate()
    meter = meter_for(root, gate)
    archive = SessionArchive.create(root, meter=meter)
    bind(archive)
    store = archive.outputs
    store.put("kept")
    gate.policy = StoragePolicy(max_bytes=1, shares={"history": 100.0, "dump": 0.0, "cache": 0.0, "index": 0.0})
    with pytest.raises(QuotaExceeded):
        store.put("denied")
    output = root / archive.sid / "output"
    assert len(list(output.glob("*.txt"))) == 1


# ---- the boundary and adoption -----------------------------------------------------------------


def test_unmetered_sessions_are_a_boundary_until_reopened(root: Path):
    plain = SessionArchive.create(root)  # written before metering existed
    inst = bind(plain)
    plain.append_event(inst, {"type": "user", "text": "old"})

    meter = meter_for(root)
    usage = meter.usage()
    assert usage["history"] is None and usage["dump"] is None, "unmetered content is a boundary, not zero"
    assert meter.describe()["unmetered"] == [plain.sid]

    SessionArchive.open(root, plain.sid, meter=meter)
    usage = meter.usage()
    expected = manifest_bytes(root, plain.sid) + events_path(root, plain, inst).stat().st_size
    assert usage["history"] == expected
    assert meter.describe()["unmetered"] == []


def test_reconcile_resyncs_the_ledger_to_real_bytes(root: Path):
    meter = meter_for(root)
    archive = SessionArchive.create(root, meter=meter)
    inst = bind(archive)
    archive.append_event(inst, {"type": "user", "text": "x"})
    path = events_path(root, archive, inst)
    with path.open("ab") as stream:  # bytes that arrived outside the meter — a crash window's orphan
        stream.write(b'{"type": "note"}\n')
    assert meter.usage()["history"] < path.stat().st_size + manifest_bytes(root, archive.sid)

    SessionArchive.open(root, archive.sid, meter=meter)
    assert meter.usage()["history"] == manifest_bytes(root, archive.sid) + path.stat().st_size


# ---- one ledger, several writers ---------------------------------------------------------------


def test_two_archives_share_one_ledger(root: Path):
    meter = meter_for(root)
    first = SessionArchive.create(root, meter=meter)
    second = SessionArchive.create(root, meter=meter)
    ia, ib = bind(first, native="n-1"), bind(second, native="n-2")
    first.append_event(ia, {"type": "user", "text": "a"})
    second.append_event(ib, {"type": "user", "text": "b"})
    usage = meter.usage()
    expected = (manifest_bytes(root, first.sid) + manifest_bytes(root, second.sid)
                + events_path(root, first, ia).stat().st_size
                + events_path(root, second, ib).stat().st_size)
    assert usage["history"] == expected


def test_threads_serialize_on_the_shared_index_lock(root: Path):
    meter = meter_for(root)
    archive = SessionArchive.create(root, meter=meter)
    inst = bind(archive)
    errors: list[BaseException] = []

    def write_many():
        try:
            for n in range(5):
                archive.append_event(inst, {"type": "user", "text": f"t-{n}"})
        except BaseException as error:  # noqa: BLE001 — collected and re-raised in the main thread
            errors.append(error)

    threads = [threading.Thread(target=write_many) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(60)
    assert not errors
    assert all(not thread.is_alive() for thread in threads), "no writer may wedge on the shared lock"
    assert len(archive.events(inst)) == 20
    assert meter.usage()["history"] == (manifest_bytes(root, archive.sid)
                                        + events_path(root, archive, inst).stat().st_size)


def test_the_ledger_is_consistent_across_processes(root: Path):
    """A second process writing through the same metered archive lands in the same ledger — the
    shared index's writer transaction is the serialization point, not in-process state."""
    script = (
        "import sys; sys.path.insert(0, sys.argv[1]);"
        "from desktop.core.storage_meter import ArchiveMeter;"
        "from desktop.core.session_archive import SessionArchive;"
        "a = SessionArchive.create(sys.argv[2], meter=ArchiveMeter(sys.argv[2]));"
        "i = a.enter('gpt', 'conn-sub', 'native-sub');"
        "a.append_event(i, {'type': 'user', 'text': 'from-subprocess'});"
        "print(a.sid)")
    out = subprocess.run([sys.executable, "-c", script, str(Path(__file__).resolve().parents[1]), str(root)],
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    sid = out.stdout.strip()
    events = list((root / sid).glob("gpt/*/events.jsonl"))
    assert len(events) == 1
    meter = meter_for(root)
    assert meter.usage()["history"] == manifest_bytes(root, sid) + events[0].stat().st_size
    assert meter.describe()["unmetered"] == []
