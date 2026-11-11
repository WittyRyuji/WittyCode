"""``core.importers.devin``: Devin's rules, session summaries and its SQLite session database (read-only)."""

from __future__ import annotations

import datetime as dt
import sqlite3
from pathlib import Path

import pytest

from desktop.core.importers import ImportFailed, sources
from desktop.core.importers.devin import LOCKED, DevinSource
from tests.importer_support import DevinDb, chain, devin_call, devin_chat, write


@pytest.fixture
def places(tmp_path: Path) -> tuple[Path, Path]:
    devin, home = tmp_path / "Roaming" / "devin", tmp_path / "home"
    devin.mkdir(parents=True)
    home.mkdir()
    return devin, home


def build_db(devin: Path, *, close: bool = True) -> DevinDb:
    old = chain(1, [
        devin_chat("system", "system prompt"),
        devin_chat("user", "first question", user_input=True),
        devin_chat("assistant", "first answer", tool_calls=[devin_call("exec", command="npm test"), devin_call("read", file_path="src/a.py", limit=20)]),
        devin_chat("tool", "test output that must not be imported"),
    ])
    new = chain(10, [
        devin_chat("system", "system prompt again"),
        devin_chat("system", "<summary>what happened before</summary>"),
        devin_chat("user", "second question", user_input=True),
        devin_chat("assistant", "second answer", tool_calls=[devin_call("get_output", shell_id="1"), devin_call("edit", file_path="src/b.py", old_string="a", new_string="b")]),
        devin_chat("tool", "edited"),
    ], metas={10: {"is_system_prefix": True}, 11: {"summarized_from": 4}})
    branch = chain(30, [devin_chat("user", "<conversation_history>summarize this</conversation_history>")])
    db = DevinDb(devin / "cli" / "sessions.db")
    db.session("s-main", "D:\\work\\app", old + new + branch, head=14, title="修复构建", created=1_790_000_000, active=1_790_000_900)
    db.session("s-hidden", "D:\\work\\app", chain(1, [devin_chat("user", "hidden one", user_input=True)]), head=1, hidden=1)
    db.session("s-empty", "D:\\work\\app", chain(1, [devin_chat("system", "only the prompt")]), head=1)
    db.session("s-old", "D:\\work\\old", chain(1, [devin_chat("user", "old style question"), devin_chat("assistant", "old style answer")]), head=2, title=None, active=1_790_000_100)
    db.connection.execute("insert into message_nodes (session_id, node_id, parent_node_id, chat_message, created_at) values ('s-old', 3, 2, '{broken', 0)")
    db.connection.execute("update sessions set main_chain_id = 3 where id = 's-old'")
    db.connection.commit()
    if close:
        db.close()
    return db


def test_rules_and_session_summaries_are_notes_and_conversation_dumps_are_not(places):
    devin, home = places
    write(devin / "AGENTS.md", "# Global rules\n\n- conventional commits\n")
    write(devin / "AGENT.md", "# Singular\n\n- also read\n")
    write(home / ".devin" / "global_rules.md", "# Home rules\n\n- prefer functions\n")
    write(home / ".devin" / "rules" / "testing.md", "# Testing\n\n- run the tests\n")
    write(devin / "summaries" / "witty-abcde.md", "Session summary\n\n## Overview\n\nWe fixed the build.\n\n## Key Details & Breadcrumbs\n\n- lock file\n\n## Current State\n\n- done\n")
    write(devin / "cli" / "summaries" / "history_0123456789abcdef.md", "=== MESSAGE 0 - System ===\nYou are Devin.\n## Not a note\n\ntext\n")
    src = DevinSource(devin, home=home)
    found = src.memories()
    titles = [note.title for note in found]
    assert {"Global rules", "Singular", "Home rules", "Testing"} <= set(titles)
    assert any("We fixed the build." in note.text for note in found)
    assert not any("Not a note" in note.text or "You are Devin." in note.text for note in found)
    assert src.report.skipped == 1
    assert all(note.scope == "global" for note in found)


def test_sessions_are_listed_from_the_database_with_exact_counts(places):
    devin, _ = places
    build_db(devin)
    src = DevinSource(devin)
    listed = src.sessions()
    assert src.report.skipped == 2
    assert [info.native_id for info in listed] == ["s-main", "s-old"]
    main, old = listed
    assert (main.title, main.project, main.started, main.updated, main.exact) == ("修复构建", "D:\\work\\app", 1_790_000_000, 1_790_000_900, True)
    assert old.title == "old style question"
    assert main.messages == len(src.read(main)) == 4
    assert main.path == str(devin / "cli" / "sessions.db") and main.size == 0


def test_a_conversation_follows_its_chain_through_a_compaction(places):
    devin, _ = places
    build_db(devin)
    src = DevinSource(devin)
    main = next(info for info in src.sessions() if info.native_id == "s-main")
    messages = src.read(main)
    assert [(m.role, m.text) for m in messages] == [
        ("user", "first question"),
        ("assistant", "first answer\n[工具] exec npm test\n[工具] read src/a.py"),
        ("user", "second question"),
        ("assistant", "second answer\n[工具] edit src/b.py"),
    ]
    moment = dt.datetime(2026, 11, 7, 10, 0, 0, 123456, tzinfo=dt.timezone.utc).timestamp()
    assert all(m.at == pytest.approx(moment) for m in messages)


def test_a_database_without_the_typed_mark_keeps_its_user_messages_and_skips_damaged_rows(places):
    devin, _ = places
    build_db(devin)
    src = DevinSource(devin)
    old = next(info for info in src.sessions() if info.native_id == "s-old")
    assert [(m.role, m.text) for m in src.read(old)] == [("user", "old style question"), ("assistant", "old style answer")]
    assert src.report.bad_records == 1


def test_a_summarizers_request_among_marked_messages_is_not_a_user_message(places):
    devin, _ = places
    nodes = chain(1, [
        devin_chat("user", "typed question", user_input=True),
        devin_chat("assistant", "an answer"),
        devin_chat("user", "Summarize the conversation so far"),
    ])
    db = DevinDb(devin / "cli" / "sessions.db")
    db.session("s-sum", "D:\\w", nodes, head=3)
    db.close()
    src = DevinSource(devin)
    (info,) = src.sessions()
    assert info.messages == 2
    assert [(m.role, m.text) for m in src.read(info)] == [("user", "typed question"), ("assistant", "an answer")]


def test_the_database_is_only_read_and_no_files_appear_next_to_it(places):
    devin, _ = places
    build_db(devin)
    folder = devin / "cli"
    before = sorted(p.name for p in folder.iterdir())
    data = (folder / "sessions.db").read_bytes()
    src = DevinSource(devin)
    for info in src.sessions():
        src.read(info)
    assert sorted(p.name for p in folder.iterdir()) == before
    assert (folder / "sessions.db").read_bytes() == data


def test_it_reads_while_devin_has_the_database_open(places):
    devin, _ = places
    db = build_db(devin, close=False)
    try:
        db.connection.execute("update sessions set title = 'renamed while open' where id = 's-main'")
        db.connection.commit()
        assert (devin / "cli" / "sessions.db-wal").exists()
        titles = {info.native_id: info.title for info in DevinSource(devin).sessions()}
        assert titles["s-main"] == "renamed while open"
    finally:
        db.close()


def test_a_locked_database_gives_no_sessions_and_says_why(places):
    devin, _ = places
    build_db(devin)
    writer = sqlite3.connect(devin / "cli" / "sessions.db", isolation_level=None)
    writer.execute("pragma locking_mode=exclusive")
    writer.execute("begin exclusive")
    writer.execute("update sessions set title = 'x'")
    try:
        src = DevinSource(devin)
        src.wait = 0.05
        assert src.sessions() == []
        assert src.report.problems == [LOCKED]
        with pytest.raises(ImportFailed) as failure:
            src.read(_info("s-main", devin))
        assert failure.value.message == LOCKED
    finally:
        writer.execute("rollback")
        writer.close()


def _info(native: str, devin: Path):
    from desktop.core.importers.base import SessionInfo

    return SessionInfo(f"devin:{native}", "devin", native, "t", "", 0, 0, 0, 0, str(devin / "cli" / "sessions.db"))


def test_a_database_of_another_shape_is_reported_not_raised(places):
    devin, _ = places
    path = devin / "cli" / "sessions.db"
    path.parent.mkdir(parents=True)
    connection = sqlite3.connect(path)
    connection.execute("create table sessions (id text, something text)")
    connection.commit()
    connection.close()
    src = DevinSource(devin)
    assert src.sessions() == [] and len(src.report.problems) == 1
    write(path, "not a database at all")
    assert src.sessions() == [] and len(src.report.problems) == 1


def test_a_missing_database_is_no_sessions_and_reading_one_fails(places):
    devin, _ = places
    src = DevinSource(devin)
    assert src.available() and src.sessions() == [] and src.report.problems == []
    with pytest.raises(ImportFailed):
        src.read(_info("s-main", devin))


def test_a_session_deleted_since_the_list_cannot_be_read(places):
    devin, _ = places
    build_db(devin)
    with pytest.raises(ImportFailed) as failure:
        DevinSource(devin).read(_info("s-gone", devin))
    assert "没有这个会话" in failure.value.message


def test_no_folder_means_not_available(tmp_path):
    src = DevinSource(tmp_path / "none" / "devin")
    assert not src.available() and src.memories() == [] and src.sessions() == []


def test_the_folder_comes_from_appdata(tmp_path):
    devin = next(s for s in sources(home=tmp_path / "home", env={"APPDATA": str(tmp_path / "Roaming")}) if s.app == "devin")
    assert devin.root == tmp_path / "Roaming" / "devin"
    given = next(s for s in sources(home=tmp_path / "home", appdata=tmp_path / "given", env={"APPDATA": "x"}) if s.app == "devin")
    assert given.root == tmp_path / "given" / "devin"
