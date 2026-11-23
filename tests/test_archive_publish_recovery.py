"""Conditional commit at ``SessionArchive.publish`` (the ``checkpoint`` hook) and its pass-through in
``chatgpt/archive_binding``: a companion save that fails inside the commit window must leave the
previous ``latest``, the live record locations and the older committed generations untouched — the
snapshot file landed mid-fault is removed, never kept as the tip. Synthetic sessions only.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from desktop.core import session_archive as archive_mod
from desktop.core.chatgpt.archive_binding import ArchiveBinding
from desktop.core.session_archive import ArchiveError, SessionArchive
from desktop.core.storage_meter import ArchiveMeter


@pytest.fixture
def root(tmp_path: Path) -> Path:
    return tmp_path / "session"


@pytest.fixture
def archive(root: Path) -> SessionArchive:
    return SessionArchive.create(root)


def bind(archive: SessionArchive, backend="gpt", connection="conn-a", native="native-1"):
    return archive.enter(backend, connection, native)


def _staged(archive: SessionArchive, instance, records, evicted):
    return archive.prepare_dump(instance, records, evicted=evicted)


def _dumps(archive: SessionArchive, instance) -> Path:
    return archive.dir / instance.backend / instance.iid / "dumps"


def fail(_entry) -> None:
    raise OSError("compat save failed")


def test_a_failed_checkpoint_restores_the_old_latest_and_locations(archive: SessionArchive, root: Path):
    instance = bind(archive)
    e1 = archive.append_event(instance, {"type": "user", "text": "x"})
    prepared = _staged(archive, instance, [e1], [e1["id"]])
    with pytest.raises(OSError, match="compat save failed"):
        archive.publish(prepared, {"text": "summary"}, checkpoint=fail)

    assert archive.latest() == (None, None)
    assert archive.manifest["generations"] == [] and archive.manifest["latest"] is None
    live = archive.locate(e1["id"])
    assert live["generation"] is None and live["file"].endswith("events.jsonl")
    # The file landed mid-fault is gone — not kept as the tip, not left blocking a retry.
    assert not list(_dumps(archive, instance).glob("g*.jsonl.gz"))
    reopened = SessionArchive.open(root, archive.sid)
    assert reopened.latest() == (None, None)
    # The same generation number stages and commits cleanly afterwards.
    retry = _staged(archive, instance, [e1], [e1["id"]])
    entry = archive.publish(retry, {"text": "summary"})
    assert entry["gen"] == 1 and archive.latest() == (1, None)


def test_a_successful_checkpoint_commits_normally(archive: SessionArchive):
    instance = bind(archive)
    e1 = archive.append_event(instance, {"type": "user", "text": "x"})
    prepared = _staged(archive, instance, [e1], [e1["id"]])
    seen: list[dict] = []
    entry = archive.publish(prepared, {"text": "summary"}, checkpoint=seen.append)

    assert entry["gen"] == 1
    assert seen and seen[0]["gen"] == 1 and seen[0]["file"].endswith("g000001.jsonl.gz")
    assert archive.latest() == (1, None)
    assert archive.locate(e1["id"])["generation"] == 1


def test_a_failed_checkpoint_keeps_earlier_generations_and_live_rows(archive: SessionArchive):
    instance = bind(archive)
    e1 = archive.append_event(instance, {"type": "user", "text": "committed"})
    archive.publish(_staged(archive, instance, [e1], [e1["id"]]), {"text": "gen-1"})
    e2 = archive.append_event(instance, {"type": "assistant", "text": "newer"})

    with pytest.raises(RuntimeError, match="save blew up"):
        archive.publish(_staged(archive, instance, [e1, e2], [e2["id"]]), {"text": "gen-2"},
                        checkpoint=lambda _entry: (_ for _ in ()).throw(RuntimeError("save blew up")))

    assert archive.latest() == (1, None)
    assert archive.read_generation(1)["header"]["generation"] == 1
    # Generation 1's ownership is untouched; only the failed commit's own writes went back.
    assert archive.locate(e1["id"])["generation"] == 1
    live = archive.locate(e2["id"])
    assert live["generation"] is None and live["file"].endswith("events.jsonl")
    assert not (_dumps(archive, instance) / "g000002.jsonl.gz").exists()


def test_a_failed_checkpoint_with_meter_returns_the_commit_bytes(root: Path):
    meter = ArchiveMeter(root)
    archive = SessionArchive.create(root, meter=meter)
    instance = bind(archive)
    e1 = archive.append_event(instance, {"type": "user", "text": "x"})
    before = meter.usage()

    with pytest.raises(OSError, match="compat save failed"):
        archive.publish(_staged(archive, instance, [e1], [e1["id"]]), {"text": "summary"},
                        checkpoint=fail)

    after = meter.usage()
    assert archive.latest() == (None, None)
    assert after["dump"] == before["dump"] == 0
    assert after["history"] == before["history"]
    assert not list(_dumps(archive, instance).glob("g*.jsonl.gz"))


def test_binding_publish_passes_the_checkpoint_through(archive: SessionArchive, root: Path):
    binding = ArchiveBinding(archive, "native-1")
    binding.ensure_instance("gpt", "native-1", "conn-a")
    items = [{"type": "user", "text": "a"}, {"type": "assistant", "text": "b"},
             {"type": "user", "text": "c"}]
    binding.sync(items, [])
    history = [{"kind": "summary", "text": "digest"}, *items]
    pending = binding.prepare(history, 0, 1)
    entry = {"upto": 2, "summary": "digest", "kept": "", "at": 0.0, "before": 9, "after": 3,
             "model": "synthetic", "mechanical": False}

    with pytest.raises(OSError, match="compat save failed"):
        binding.publish(pending, entry, checkpoint=fail)
    assert archive_mod.latest_generation(root, archive.sid) == (None, None)

    binding.publish(binding.prepare(history, 0, 1), entry)
    assert archive_mod.latest_generation(root, archive.sid) == (1, None)
