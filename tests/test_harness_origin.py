"""``harness.store.SavedSession.origin``: where an imported conversation came from, kept in its file only when set."""

from __future__ import annotations

import json
from dataclasses import replace

from desktop.core.harness.store import SavedSession, SavedTurn, SessionStore


def conversation(**fields) -> SavedSession:
    return SavedSession(
        id="s1", project="D:\\work\\app", title="t", created_at=1.0, modified_at=2.0,
        items=[{"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hi"}]}], turns=[SavedTurn(0, 1.0, 2.0)], **fields,
    )


def test_an_origin_survives_saving_and_loading(tmp_path):
    store = SessionStore(tmp_path)
    store.save(conversation(origin={"app": "codex", "native": "019a", "at": 3.5}))
    loaded = store.load("s1")
    assert loaded.origin == {"app": "codex", "native": "019a", "at": 3.5}


def test_a_conversation_made_here_writes_no_origin(tmp_path):
    store = SessionStore(tmp_path)
    store.save(conversation())
    raw = json.loads((tmp_path / "sessions" / "s1.json").read_text(encoding="utf-8"))
    assert "origin" not in raw
    assert store.load("s1").origin == {}


def test_a_file_from_before_origin_existed_loads_as_before(tmp_path):
    store = SessionStore(tmp_path)
    store.save(conversation())
    path = tmp_path / "sessions" / "s1.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw.pop("origin", None)
    path.write_text(json.dumps(raw), encoding="utf-8")
    loaded = store.load("s1")
    assert loaded is not None and loaded.origin == {} and loaded.title == "t"


def test_an_origin_that_is_not_an_object_is_ignored(tmp_path):
    store = SessionStore(tmp_path)
    store.save(conversation())
    path = tmp_path / "sessions" / "s1.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["origin"] = "claude"
    path.write_text(json.dumps(raw), encoding="utf-8")
    assert store.load("s1").origin == {}


def test_the_copy_the_controller_saves_keeps_the_origin():
    session = conversation(origin={"app": "devin", "native": "x", "at": 1.0})
    assert replace(session, items=[]).origin == session.origin
