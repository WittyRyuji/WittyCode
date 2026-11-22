"""The new session archive (``desktop.core.session_archive``, final spec §1–§4): synthetic sessions only.

Covered: identity bind/recover and the instance timeline, the case-insensitive atomic claim of ids, per-session
event ownership, committed generation chains with verified snapshots, publish rollback on failure, the thin
rebuildable index that stores no bodies, and the session's single tool-output directory.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from desktop.core import session_archive as archive_mod
from desktop.core.session_archive import ArchiveError, GapError, SessionArchive
from desktop.core import session_archive_io as io


@pytest.fixture
def root(tmp_path: Path) -> Path:
    return tmp_path / "session"


@pytest.fixture
def archive(root: Path) -> SessionArchive:
    return SessionArchive.create(root)


def make_link(link: Path, target: Path) -> None:
    """A symlink if the account may create one, else a junction (no privilege needed); skip when neither works."""
    try:
        os.symlink(target, link, target_is_directory=True)
        return
    except OSError:
        pass
    if sys.platform == "win32":
        done = subprocess.run(["cmd", "/d", "/c", "mklink", "/J", str(link), str(target)], capture_output=True)
        if done.returncode == 0:
            return
    pytest.skip("cannot create a symlink or junction here")


def bind(archive: SessionArchive, backend="gpt", connection="acp-main", native="native-1"):
    return archive.enter(backend, connection, native)


def test_identity_recovery_round_trip(archive: SessionArchive, root: Path):
    instance = bind(archive, backend="gpt", connection="conn-a", native="native-abc")
    assert archive_mod.resolve_local(root, "conn-a", "native-abc") == {
        "sid": archive.sid, "backend": "gpt", "iid": instance.iid}
    assert archive_mod.resolve_native(root, archive.sid, "gpt", instance.iid) == {
        "connection": "conn-a", "native_id": "native-abc"}

    # A temporary disconnect re-enters with the same native id: it is a resume, not a new instance.
    again = bind(archive, backend="gpt", connection="conn-a", native="native-abc")
    assert again.iid == instance.iid
    reopened = SessionArchive.open(root, archive.sid)
    assert reopened.current().iid == instance.iid


def test_three_instance_switches_keep_order_and_reuse_resumable_iid(archive: SessionArchive):
    gpt = bind(archive, backend="gpt", connection="conn-a", native="gpt-1")
    devin = bind(archive, backend="devin", connection="conn-b", native="devin-1")
    back = bind(archive, backend="gpt", connection="conn-a", native="gpt-1")

    assert back.iid == gpt.iid, "resuming the original native session reuses its iid"
    assert devin.iid != gpt.iid
    timeline = archive.timeline()
    assert [item["backend"] for item in timeline] == ["gpt", "devin", "gpt"]
    assert [item["n"] for item in timeline] == [1, 2, 3]
    assert [item["result"] for item in timeline] == ["new", "new", "resumed"]
    assert archive.current().iid == gpt.iid


def test_a_confirmed_new_native_session_claims_a_new_iid(archive: SessionArchive):
    first = bind(archive, backend="gpt", connection="conn-a", native="gpt-1")
    second = bind(archive, backend="gpt", connection="conn-a", native="gpt-2")
    assert second.iid != first.iid
    assert archive.timeline()[-1]["result"] == "new"
    entry = archive_mod.resolve_local(archive.root, "conn-a", "gpt-2")
    assert entry["iid"] == second.iid


def test_a_native_id_bound_elsewhere_is_refused(archive: SessionArchive, root: Path):
    bind(archive, backend="gpt", connection="conn-a", native="shared-1")
    other = SessionArchive.create(root)
    with pytest.raises(ArchiveError):
        other.enter("gpt", "conn-a", "shared-1")


def test_id_claims_are_case_insensitive_unique(root: Path):
    archive = SessionArchive.create(root)
    # Force the generator to offer a case-variant of the existing sid before falling back to a fresh name.
    candidates = iter([archive.sid.swapcase(), "Zz9Xx8Ww7Vv6Uu5T"])
    second = SessionArchive.create(root, name_source=lambda: next(candidates))
    assert second.sid == "Zz9Xx8Ww7Vv6Uu5T"
    assert second.sid.casefold() != archive.sid.casefold()
    assert len({p.name.casefold() for p in root.iterdir()}) == len(list(root.iterdir()))


def test_events_belong_to_their_own_session_and_instance(archive: SessionArchive, root: Path):
    instance = bind(archive, backend="gpt", connection="conn-a", native="n-1")
    event = archive.append_event(instance, {"type": "user", "text": "synthetic-question"})
    other = SessionArchive.create(root)
    peer = other.enter("gpt", "conn-b", "n-2")
    twin = other.append_event(peer, {"type": "user", "text": "other-session"})

    path = archive.dir / "gpt" / instance.iid / "events.jsonl"
    assert json.loads(path.read_text(encoding="utf-8").strip())["text"] == "synthetic-question"
    assert archive.locate(event["id"])["file"].endswith("events.jsonl")
    assert other.locate(twin["id"])["file"].endswith("events.jsonl")
    # The other session's record is never locatable under this sid, even if ids looked alike.
    assert archive.locate(twin["id"]) is None
    # Resuming the instance appends to the same file with continuing sequence numbers.
    resumed = bind(archive, backend="gpt", connection="conn-a", native="n-1")
    second = archive.append_event(resumed, {"type": "assistant", "text": "answer"})
    assert second["seq"] == event["seq"] + 1
    assert [item["id"] for item in archive.events(instance)] == [event["id"], second["id"]]


def test_the_session_has_one_output_directory(archive: SessionArchive):
    bind(archive, backend="gpt", connection="conn-a", native="n-1")
    bind(archive, backend="devin", connection="conn-b", native="n-2")
    reference = archive.outputs.put("a large synthetic tool result")
    assert reference.startswith("o-")
    files = list((archive.dir / "output").glob("o-*.txt"))
    assert len(files) == 1
    assert archive.outputs.read(reference) == "a large synthetic tool result"
    # Instances and dumps reference the same store; no per-backend output copies exist.
    assert not list(archive.dir.glob("*/**/output"))


def _committed(archive: SessionArchive, instance, records, evicted, summary=None):
    prepared = archive.prepare_dump(instance, records, evicted=evicted)
    return archive.publish(prepared, summary or {"text": "synthetic summary"}, evicted=evicted)


def test_generation_chain_and_local_generation_reads(archive: SessionArchive, root: Path):
    gpt = bind(archive, backend="gpt", connection="conn-a", native="n-1")
    e1 = archive.append_event(gpt, {"type": "user", "text": "first"})
    e2 = archive.append_event(gpt, {"type": "assistant", "text": "second"})
    view1 = [{"kind": "summary", "text": "v1"}, e1, e2]
    entry1 = _committed(archive, gpt, view1, [e1["id"]])
    assert entry1["gen"] == 1 and entry1["parent"] is None

    devin = bind(archive, backend="devin", connection="conn-b", native="n-2")
    e3 = archive.append_event(devin, {"type": "user", "text": "third"})
    entry2 = _committed(archive, devin, [e1, e2, e3], [e2["id"], e3["id"]])

    assert entry2["gen"] == 2 and entry2["parent"] == 1
    assert archive_mod.latest_generation(root, archive.sid) == (2, 1)
    assert [item["gen"] for item in archive_mod.generation_chain(root, archive.sid)] == [1, 2]

    dump = archive_mod.read_generation(root, archive.sid, 1)
    assert dump["header"]["generation"] == 1 and dump["header"]["sid"] == archive.sid
    assert dump["evicted"] == [e1["id"]]
    assert [r["text"] for r in dump["records"] if r.get("type") == "user"] == ["first"]

    # The evicted record now locates inside the committed dump; the still-live one stays in the event log.
    located = archive.locate(e1["id"])
    assert located["generation"] == 1 and located["file"].endswith("g000001.jsonl.gz")
    still_live = archive.locate(e2["id"])
    # e2 was evicted into generation 2 by the second commit even though it also appeared in dump 1.
    assert still_live["generation"] == 2 and still_live["file"].endswith("g000002.jsonl.gz")


def test_publish_evicted_narrows_the_prepared_boundary(archive: SessionArchive):
    instance = bind(archive)
    e1 = archive.append_event(instance, {"type": "user", "text": "keep-out"})
    e2 = archive.append_event(instance, {"type": "user", "text": "evicted"})
    prepared = archive.prepare_dump(instance, [e1, e2], evicted=[e1["id"], e2["id"]])
    # Compaction finished and only actually removed e2: the manifest reports the real range.
    entry = archive.publish(prepared, {"text": "partial"}, evicted=[e2["id"]])
    assert entry["evicted"] == [e2["id"]]
    assert archive.locate(e1["id"])["generation"] is None
    assert archive.locate(e2["id"])["generation"] == 1
    assert archive_mod.read_generation(archive.root, archive.sid, 1)["evicted"] == [e2["id"]]
    with pytest.raises(ArchiveError):
        wider = archive.prepare_dump(instance, [e1], evicted=[e1["id"]])
        archive.publish(wider, {"text": "widen"}, evicted=[e1["id"], e2["id"]])


def test_a_record_evicted_once_is_not_reassigned_to_a_later_dump(archive: SessionArchive):
    instance = bind(archive)
    e1 = archive.append_event(instance, {"type": "user", "text": "fixed-decision"})
    _committed(archive, instance, [e1], [e1["id"]])
    e2 = archive.append_event(instance, {"type": "user", "text": "newer"})
    # Generation 2's full view still contains e1, but e1 stays owned by generation 1.
    _committed(archive, instance, [e1, e2], [e2["id"]])
    assert archive.locate(e1["id"])["generation"] == 1
    assert archive.locate(e2["id"])["generation"] == 2


def test_abort_leaves_the_old_view_and_publish_can_continue(archive: SessionArchive):
    instance = bind(archive)
    e1 = archive.append_event(instance, {"type": "user", "text": "x"})
    prepared = archive.prepare_dump(instance, [e1], evicted=[e1["id"]])
    staging = prepared.staging
    archive.abort(prepared)
    assert not staging.exists()
    assert archive.latest() == (None, None)
    # A later attempt at the same generation number works normally.
    e2 = archive.append_event(instance, {"type": "assistant", "text": "y"})
    entry = _committed(archive, instance, [e1, e2], [e1["id"]])
    assert entry["gen"] == 1


def test_a_failed_publish_rolls_back_file_manifest_and_index(archive: SessionArchive, monkeypatch):
    instance = bind(archive)
    e1 = archive.append_event(instance, {"type": "user", "text": "x"})
    prepared = archive.prepare_dump(instance, [e1], evicted=[e1["id"]])
    monkeypatch.setattr(io, "atomic_write_text", lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(OSError):
        archive.publish(prepared, {"text": "summary"})
    assert archive.latest() == (None, None)
    assert not list((archive.dir / "gpt" / instance.iid / "dumps").glob("g*.jsonl.gz"))
    assert archive.locate(e1["id"])["generation"] is None
    reopened = SessionArchive.open(archive.root, archive.sid)
    assert reopened.latest() == (None, None)


def test_a_damaged_staging_file_is_refused_at_publish(archive: SessionArchive):
    instance = bind(archive)
    e1 = archive.append_event(instance, {"type": "user", "text": "x"})
    prepared = archive.prepare_dump(instance, [e1], evicted=[e1["id"]])
    prepared.staging.write_bytes(b"corrupt")
    with pytest.raises(ArchiveError):
        archive.publish(prepared, {"text": "summary"})
    assert archive.latest() == (None, None)


def test_validation_rejects_a_corrupt_manifest_and_dump(archive: SessionArchive, root: Path):
    manifest = archive.dir / "manifest.json"
    good = manifest.read_bytes()
    manifest.write_text("{}", encoding="utf-8")
    with pytest.raises(ArchiveError):
        SessionArchive.open(root, archive.sid)
    manifest.write_bytes(good)

    instance = bind(archive)
    e1 = archive.append_event(instance, {"type": "user", "text": "x"})
    _committed(archive, instance, [e1], [e1["id"]])
    dump_path = archive.dir / "gpt" / instance.iid / "dumps" / "g000001.jsonl.gz"
    dump_path.write_bytes(b"tampered")
    with pytest.raises(GapError):
        archive_mod.read_generation(root, archive.sid, 1)


def test_the_index_is_rebuildable_and_holds_no_bodies(archive: SessionArchive, root: Path):
    instance = bind(archive, backend="gpt", connection="conn-a", native="n-1")
    marker = "unique-synthetic-evidence-string"
    e1 = archive.append_event(instance, {"type": "user", "text": marker})
    _committed(archive, instance, [e1], [e1["id"]])
    db = root / "index.sqlite"
    db.unlink()
    assert archive_mod.rebuild_index(root) == 1
    assert archive.locate(e1["id"])["generation"] == 1
    assert archive_mod.resolve_local(root, "conn-a", "n-1")["sid"] == archive.sid
    # The index answers "where", never "what": no message text is stored in it.
    with sqlite3.connect(db) as conn:
        whole = "\n".join(conn.iterdump())
    assert marker not in whole


def test_links_inside_the_archive_are_refused(archive: SessionArchive, tmp_path: Path, root: Path):
    outside = tmp_path / "outside"
    outside.mkdir()
    instance = bind(archive)
    link = archive.dir / "gpt" / instance.iid / "events-link.jsonl"
    target = outside / "elsewhere.jsonl"
    target.write_text("{}", encoding="utf-8")
    make_link(link, target)
    with pytest.raises(OSError):
        io.append_line(link, "{}")
    # A session directory reached through a link is not an archive session.
    fake = root / "AaBbCcDdEeFfGgHh"
    make_link(fake, archive.dir)
    with pytest.raises(ArchiveError):
        SessionArchive.open(root, "AaBbCcDdEeFfGgHh")


def test_illegal_sids_and_segments_are_rejected(root: Path):
    with pytest.raises(ArchiveError):
        SessionArchive.open(root, "../escape")
    with pytest.raises(ArchiveError):
        SessionArchive.open(root, "short")
    with pytest.raises(io.PathRejected):
        io.check_segment("aux")
    with pytest.raises(io.PathRejected):
        io.check_segment("name ")


def test_concurrent_claims_and_appends_stay_unique(root: Path):
    archives = [SessionArchive.create(root) for _ in range(4)]
    assert len({a.sid.casefold() for a in archives}) == 4
    archive = archives[0]
    instance = bind(archive)
    events, errors = [], []

    def append(n):
        try:
            for _ in range(5):
                events.append(archive.append_event(instance, {"type": "user", "n": n}))
        except Exception as error:  # noqa: BLE001 - surfaced by assertion below
            errors.append(error)

    threads = [threading.Thread(target=append, args=(i,)) for i in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not errors
    seqs = [event["seq"] for event in events]
    assert len(set(seqs)) == len(seqs) == 20


def test_turn_budget_survives_switches_and_reopen(archive: SessionArchive, root: Path):
    bind(archive, backend="gpt", connection="conn-a", native="n-1")
    archive.save_budget({"turn": "turn-1", "ranges": [{"record": "e-x", "generation": 1}], "tokens": 120})
    bind(archive, backend="devin", connection="conn-b", native="n-2")
    reopened = SessionArchive.open(root, archive.sid)
    assert reopened.budget() == {"turn": "turn-1", "ranges": [{"record": "e-x", "generation": 1}], "tokens": 120}
    with pytest.raises(ArchiveError):
        archive.save_budget({"turn": "", "ranges": [], "tokens": 0})


# ---- staged-handle ownership: only the issuing archive may publish or abort ------


def test_another_sessions_handle_cannot_publish_or_abort_here(archive: SessionArchive, root: Path):
    instance = bind(archive)
    e1 = archive.append_event(instance, {"type": "user", "text": "mine"})
    handle = archive.prepare_dump(instance, [e1], evicted=[e1["id"]])
    other = SessionArchive.create(root)
    other.enter("gpt", "conn-b", "n-9")
    # A handle issued for this session is refused by another session's archive — before any file op.
    with pytest.raises(ArchiveError):
        other.publish(handle, {"text": "stolen"})
    assert handle.staging.is_file()
    with pytest.raises(ArchiveError):
        other.abort(handle)
    assert handle.staging.is_file()
    archive.abort(handle)
    assert not handle.staging.exists()


def test_a_handle_issued_by_another_archive_object_of_the_same_sid_is_not_ours(archive: SessionArchive, root: Path):
    instance = bind(archive)
    e1 = archive.append_event(instance, {"type": "user", "text": "x"})
    handle = archive.prepare_dump(instance, [e1], evicted=[e1["id"]])
    twin = SessionArchive.open(root, archive.sid)  # same sid, different archive object
    with pytest.raises(ArchiveError):
        twin.publish(handle, {"text": "via twin"})
    with pytest.raises(ArchiveError):
        twin.abort(handle)
    assert handle.staging.is_file()
    archive.publish(handle, {"text": "owner"})
    assert archive.latest() == (1, None)


def test_only_one_of_two_parallel_stages_publishes_and_the_loser_harms_nothing(archive: SessionArchive):
    instance = bind(archive)
    e1 = archive.append_event(instance, {"type": "user", "text": "x"})
    first = archive.prepare_dump(instance, [e1], evicted=[e1["id"]])
    second = archive.prepare_dump(instance, [e1], evicted=[e1["id"]])
    archive.publish(first, {"text": "winner"})
    assert archive.latest() == (1, None)
    # The loser's generation is now stale: it cannot replace the committed latest.
    with pytest.raises(ArchiveError):
        archive.publish(second, {"text": "loser"})
    assert second.staging.is_file()
    archive.abort(second)
    assert not second.staging.exists()
    committed = archive.dir / "gpt" / instance.iid / "dumps" / "g000001.jsonl.gz"
    assert committed.is_file()
    assert archive_mod.read_generation(archive.root, archive.sid, 1)["summary"] == {"text": "winner"}


def test_an_ended_handle_cannot_be_used_again(archive: SessionArchive):
    instance = bind(archive)
    e1 = archive.append_event(instance, {"type": "user", "text": "x"})
    handle = archive.prepare_dump(instance, [e1], evicted=[e1["id"]])
    archive.publish(handle, {"text": "done"})
    committed = archive.dir / "gpt" / instance.iid / "dumps" / "g000001.jsonl.gz"
    with pytest.raises(ArchiveError):
        archive.publish(handle, {"text": "replay"})
    with pytest.raises(ArchiveError):
        archive.abort(handle)  # refusing must not delete the committed generation
    assert committed.is_file()
    handle2 = archive.prepare_dump(instance, [e1], evicted=[])
    archive.abort(handle2)
    with pytest.raises(ArchiveError):
        archive.abort(handle2)


def test_a_forged_handle_never_reaches_any_path(archive: SessionArchive, tmp_path: Path):
    instance = bind(archive)
    victim = tmp_path / "victim.txt"
    victim.write_text("precious", encoding="utf-8")
    forged = archive_mod.PreparedDump(1, None, instance, victim, "0" * 64, (), 0)
    with pytest.raises(ArchiveError):
        archive.abort(forged)
    assert victim.is_file()
    with pytest.raises(ArchiveError):
        archive.publish(forged, {"text": "x"})
    assert victim.is_file()


def test_handle_ownership_holds_under_the_meter(root: Path):
    from desktop.core.storage_meter import ArchiveMeter

    metered = SessionArchive.create(root, meter=ArchiveMeter(root))
    instance = metered.enter("gpt", "conn-m", "n-m")
    e1 = metered.append_event(instance, {"type": "user", "text": "metered"})
    handle = metered.prepare_dump(instance, [e1], evicted=[e1["id"]])
    plain = SessionArchive.create(root)
    # A metered archive's handle is foreign to a plain one and vice versa.
    with pytest.raises(ArchiveError):
        plain.abort(handle)
    with pytest.raises(ArchiveError):
        plain.publish(handle, {"text": "cross"})
    assert handle.staging.is_file()
    metered.publish(handle, {"text": "metered"})
    assert metered.latest() == (1, None)
    # The same ownership gate sits inside the metered commit transaction.
    first = metered.prepare_dump(instance, [e1], evicted=[])
    second = metered.prepare_dump(instance, [e1], evicted=[])
    metered.publish(first, {"text": "winner"})
    assert metered.latest() == (2, 1)
    with pytest.raises(ArchiveError):
        metered.publish(second, {"text": "stale"})
    assert second.staging.is_file()
    metered.abort(second)
    assert not second.staging.exists()
