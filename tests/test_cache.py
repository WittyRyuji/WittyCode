"""The application's own cache: what it keeps, and what it refuses to show."""

from __future__ import annotations

import json
import os

from desktop.core.cache import CACHE_VERSION, MAX_SESSION_BYTES, Cache


def test_a_cache_without_a_folder_keeps_nothing(tmp_path):
    cache = Cache(None)
    assert not cache.enabled
    cache.save_index({"rows": {}})
    assert cache.load_index() == {}
    assert cache.save_session("s-1", {"turns": [1]}) is False
    assert cache.load_session("s-1") is None


def test_the_index_and_a_session_come_back_as_they_were_saved(tmp_path):
    cache = Cache(tmp_path / "cache")
    cache.save_index({"rows": {"D:\\p": [{"id": "s-1"}]}, "models": [{"id": "m"}]})
    index = cache.load_index()
    assert index["rows"] == {"D:\\p": [{"id": "s-1"}]} and index["models"] == [{"id": "m"}] and index["v"] == CACHE_VERSION
    assert cache.save_session("cx:abc-1", {"turns": [{"id": "t1", "text": "你好"}], "activeAt": 5.5})
    loaded = cache.load_session("cx:abc-1")
    assert loaded["turns"] == [{"id": "t1", "text": "你好"}] and loaded["activeAt"] == 5.5 and loaded["key"] == "cx:abc-1"
    assert cache.session_path("cx:abc-1").name == "cx_abc-1.json", "an id with a colon is a safe file name"
    assert not [p for p in (tmp_path / "cache").rglob("*") if p.suffix == ".tmp"], "writing leaves no temporary file behind"


def test_a_missing_damaged_or_foreign_file_is_not_used(tmp_path):
    cache = Cache(tmp_path / "cache")
    assert cache.load_index() == {} and cache.load_session("nope") is None
    folder = tmp_path / "cache"
    (folder / "sessions").mkdir(parents=True)
    (folder / "index.json").write_text("{not json", encoding="utf-8")
    (folder / "sessions" / "old.json").write_text(json.dumps({"v": CACHE_VERSION + 1, "turns": [1]}), encoding="utf-8")
    (folder / "sessions" / "list.json").write_text("[1, 2]", encoding="utf-8")
    assert cache.load_index() == {}
    assert cache.load_session("old") is None, "written by another version"
    assert cache.load_session("list") is None, "not an object"


def test_a_conversation_too_big_to_be_worth_it_is_not_cached_and_does_not_leave_the_old_copy(tmp_path):
    cache = Cache(tmp_path / "cache")
    assert cache.save_session("s-1", {"turns": ["small"]})
    big = {"turns": ["x" * (MAX_SESSION_BYTES + 10)]}
    assert cache.save_session("s-1", big) is False
    assert cache.load_session("s-1") is None, "an older, smaller copy would show old content as if it were current"


def test_a_write_that_fails_is_reported_not_raised(tmp_path, monkeypatch):
    cache = Cache(tmp_path / "cache")

    def broken(*args, **kwargs):
        raise PermissionError("disk says no")

    monkeypatch.setattr(os, "replace", broken)
    assert cache.save_session("s-1", {"turns": [1]}) is False
    assert not [p for p in (tmp_path / "cache").rglob("*.tmp")], "the temporary file is cleaned up too"
