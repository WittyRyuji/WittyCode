"""The shared memory's files: what is written, found, kept apart between projects, undone, and survives writers at once."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from desktop.core.memory import store as store_module
from desktop.core.memory.launch import PACKAGE_ROOT
from desktop.core.memory.store import Entry, MemoryRejected, MemoryStore, agent, project_key, project_path, same_project

CODEX = agent("codex", "run-a")
DROID = agent("droid", "run-b")


@pytest.fixture
def store(tmp_path):
    return MemoryStore(tmp_path / "memory")


@pytest.fixture
def proj(tmp_path):
    folder = tmp_path / "proj-one"
    folder.mkdir()
    return str(folder)


@pytest.fixture
def other(tmp_path):
    folder = tmp_path / "proj-two"
    folder.mkdir()
    return str(folder)


def note(store, title="Commit messages", text="Write commit messages in Chinese.", **more):
    more.setdefault("by", CODEX)
    return store.create(title=title, text=text, **more)


# ----------------------------------------------------------------------------- the files


def test_a_note_is_a_markdown_file_that_reads_back_the_same(store, proj):
    text = "第一行\n---\n含有分隔线、引号 \"quoted\" 和反斜杠 C:\\x\\y\n\n末尾"
    made = note(store, "标题: 带冒号", text, scope="project", project=proj, kind="pitfall", tags=["e2e", "Windows"])
    path = store.root / "projects" / project_key(proj) / f"{made.id}.md"
    raw = path.read_text(encoding="utf-8")
    assert raw.startswith("---\nid: ") and "\n---\n第一行" in raw, "a header of key: JSON lines, then the text as written"
    again = store.read(made.id)
    assert again == made and again.text == text and again.tags == ("e2e", "Windows")
    assert again.source == CODEX and again.editor == CODEX and again.status == "active" and again.rev == 1


def test_global_and_project_notes_live_in_their_own_places(store, proj):
    g = note(store, "Global one", "Holds everywhere.", scope="global")
    p = note(store, "Project one", "Holds here.", scope="project", project=proj)
    assert (store.root / "global" / f"{g.id}.md").is_file()
    assert (store.root / "projects" / project_key(proj) / f"{p.id}.md").is_file()
    assert g.project == "" and p.project == project_path(proj)


def test_a_project_is_the_same_project_however_its_path_is_written(tmp_path):
    folder = tmp_path / "Mixed Case"
    folder.mkdir()
    assert project_key(folder) == project_key(str(folder).lower()) == project_key(str(folder) + os.sep + ".")
    assert same_project(str(folder), str(folder).upper()) and not same_project(str(folder), str(tmp_path))


# ----------------------------------------------------------------------------- finding notes


def test_search_finds_words_in_the_title_the_text_and_the_tags(store, proj):
    note(store, "Commit messages", "Write commit messages in Chinese.", scope="project", project=proj, tags=["git"])
    note(store, "Test loop", "Run the e2e file twenty times in a row.", scope="project", project=proj)
    assert [h.entry.title for h in store.search("commit", project=proj)] == ["Commit messages"]
    assert [h.entry.title for h in store.search("twenty", project=proj)] == ["Test loop"]
    assert [h.entry.title for h in store.search("git", project=proj)] == ["Commit messages"], "a tag is searched too"
    assert store.search("nothing like this", project=proj) == []


def test_chinese_is_found_by_long_words_by_short_words_and_by_a_sentence_that_shares_parts_of_it(store, proj):
    note(store, "提交信息", "提交信息一律用中文，不要推送到远程。", scope="project", project=proj, kind="preference")
    note(store, "偏好", "回答要简短。", scope="project", project=proj)
    titles = lambda q: [h.entry.title for h in store.search(q, project=proj)]  # noqa: E731
    assert titles("提交信息") == ["提交信息"], "three characters or more: the full-text index"
    assert titles("偏好") == ["偏好"], "two characters: LIKE"
    assert titles("远程") == ["提交信息"]
    assert titles("以后写提交信息要用中文吗") == ["提交信息"], "a whole sentence finds the note that shares parts of it"


def test_the_best_match_comes_first_and_this_projects_notes_before_the_globals(store, proj):
    note(store, "Shell", "Use PowerShell here.", scope="global")
    note(store, "Shell on this project", "Use PowerShell here, not bash.", scope="project", project=proj)
    first, second = store.search("PowerShell", project=proj)
    assert (first.entry.scope, second.entry.scope) == ("project", "global")


def test_an_empty_query_lists_the_newest_notes(store, proj):
    for n in range(4):
        note(store, f"Note number {n}", f"Text {n}", scope="project", project=proj, now=1_700_000_000 + n)
    assert [h.entry.title for h in store.search("", project=proj, limit=3)] == ["Note number 3", "Note number 2", "Note number 1"]


def test_a_conversation_sees_the_global_notes_and_its_projects_and_no_others(store, proj, other):
    g = note(store, "For everyone", "Global text about widgets.", scope="global")
    mine = note(store, "Mine", "Project text about widgets.", scope="project", project=proj)
    theirs = note(store, "Theirs", "Other project text about widgets.", scope="project", project=other)
    assert {h.entry.id for h in store.search("widgets", project=proj)} == {g.id, mine.id}
    assert {h.entry.id for h in store.search("widgets", project=proj, scope="project")} == {mine.id}
    assert {h.entry.id for h in store.search("widgets", project=proj, scope="global")} == {g.id}
    assert {h.entry.id for h in store.search("widgets", project=None)} == {g.id, mine.id, theirs.id}, "the application sees all"
    with pytest.raises(MemoryRejected) as hidden:
        store.read(theirs.id, project=proj)
    assert hidden.value.code == "not_found", "no hint that it exists"
    for change in (lambda: store.update(theirs.id, by=DROID, project=proj, text="x"), lambda: store.forget(theirs.id, by=DROID, project=proj)):
        with pytest.raises(MemoryRejected):
            change()
    assert store.read(theirs.id).text.startswith("Other project"), "and it is untouched"


def test_a_forgotten_note_is_not_found_unless_asked_for(store, proj):
    made = note(store, "Old", "Outdated widget advice.", scope="project", project=proj)
    store.forget(made.id, by=CODEX, project=proj)
    assert store.search("widget", project=proj) == []
    assert [h.entry.id for h in store.search("widget", project=proj, status=None)] == [made.id]


# ----------------------------------------------------------------------------- what is refused


@pytest.mark.parametrize(
    "fields, code",
    [
        ({"title": "", "text": "x"}, "empty"),
        ({"title": "x", "text": "  "}, "empty"),
        ({"title": "t" * 81, "text": "x"}, "title_long"),
        ({"title": "x", "text": "t" * 2001}, "text_long"),
        ({"title": "x", "text": "y", "scope": "elsewhere"}, "bad_scope"),
        ({"title": "x", "text": "y", "kind": "rumour"}, "bad_kind"),
        ({"title": "Key", "text": "My key is sk-" + "abcdefghijklmnopqrstuvwxyz123456"}, "secret"),
        ({"title": "Login", "text": "password: hunter2hunter2"}, "secret"),
        ({"title": "Token", "text": "ghp_" + "a" * 36}, "secret"),
        ({"title": "Pem", "text": "-----BEGIN RSA PRIVATE KEY-----\nabc"}, "secret"),
    ],
)
def test_what_cannot_be_a_note_is_refused_with_a_reason(store, proj, fields, code):
    with pytest.raises(MemoryRejected) as refused:
        store.create(scope=fields.pop("scope", "global"), by=CODEX, **fields)
    assert refused.value.code == code and refused.value.message
    assert store.entries(status=None) == [], "nothing was written"


@pytest.mark.parametrize(
    "text",
    [
        "Reverted in commit 82bf0d62aec88a42e8839148e18567946d664628 because of the cache.",
        "The class is named " + "VeryLong" * 8 + " and that is fine.",
        "Run " + "word" * 20 + " to see it.",
        "See https://example.com/docs/some/long/path/segment/with-dashes_and_underscores/page.html for details.",
        "The api key lives in the environment variable MIMO_API_KEY; password manager entry is called work.",
        "Token usage is the sum of In, Out and the cache columns.",
    ],
)
def test_ordinary_long_things_are_not_taken_for_secrets(store, text):
    assert note(store, "Ordinary", text, scope="global").text == text


@pytest.mark.parametrize(
    "text",
    ["Use Zm9vYmFyMTIzNDU2Nzg5MEFCQ0RFRkdISUpLTE1OT1BRUlNUVVZXWFla now.", "Authorization: Bearer abcdefghijklmnopqrstuvwxyz0123456789", "api_key = 'abcd1234efgh5678'", "AKIAIOSFODNN7EXAMPLE is the id"],
)
def test_things_shaped_like_secrets_are_refused(store, text):
    with pytest.raises(MemoryRejected) as refused:
        note(store, "Looks risky", text, scope="global")
    assert refused.value.code == "secret"


def test_a_project_note_needs_a_project(store):
    with pytest.raises(MemoryRejected) as refused:
        note(store, scope="project", project="")
    assert refused.value.code == "bad_scope"


def test_a_note_that_says_what_one_already_says_is_refused_with_the_id_of_that_one(store, proj):
    first = note(store, "Commit messages in Chinese", "Always.", scope="project", project=proj)
    for title, text in (("commit messages in chinese!", "Different words."), ("Commit message in Chinese", "Other words."), ("Something else", "Always.")):
        with pytest.raises(MemoryRejected) as refused:
            note(store, title, text, scope="project", project=proj)
        assert refused.value.code == "duplicate" and refused.value.entry.id == first.id and first.id in refused.value.message
    note(store, "Commit messages in Chinese", "Always.", scope="global")  # (another place: another note)
    assert len(store.entries()) == 2


def test_a_scope_holds_only_so_many_active_notes(store, proj, monkeypatch):
    monkeypatch.setattr(store_module, "MAX_ACTIVE", 3)
    made = [note(store, f"Distinct subject {n}{'abcdefghij'[n]}", f"Body {n} {'q' * n}", scope="project", project=proj) for n in range(3)]
    with pytest.raises(MemoryRejected) as full:
        note(store, "One more entirely", "Over the limit.", scope="project", project=proj)
    assert full.value.code == "limit"
    note(store, "Global is separate", "Not counted with the project.", scope="global")
    store.forget(made[0].id, by=CODEX)
    note(store, "One more entirely", "Room again.", scope="project", project=proj)
    with pytest.raises(MemoryRejected):
        store.restore(made[0].id, by="user")  # (full again)


# ----------------------------------------------------------------------------- changes and undo


def test_update_keeps_what_is_not_given_and_counts_revisions(store, proj):
    made = note(store, "Title", "Text one.", scope="project", project=proj, kind="fact", tags=["a"])
    changed = store.update(made.id, by=DROID, text="Text two.")
    assert (changed.title, changed.text, changed.kind, changed.tags, changed.rev) == ("Title", "Text two.", "fact", ("a",), 2)
    assert changed.source == CODEX and changed.editor == DROID and changed.updated >= made.updated
    assert store.read(made.id) == changed


def test_every_change_is_in_the_journal_with_the_note_before_and_after(store, proj):
    made = note(store, "Title", "Text one.", scope="project", project=proj)
    store.update(made.id, by=DROID, text="Text two.")
    store.forget(made.id, by="user")
    store.restore(made.id, by="user")
    changes = store.journal()
    assert [c.op for c in changes] == ["create", "update", "forget", "restore"] and [c.seq for c in changes] == [1, 2, 3, 4]
    assert changes[0].before is None and changes[0].after.text == "Text one."
    assert changes[1].before.text == "Text one." and changes[1].after.text == "Text two." and changes[1].by == DROID
    assert changes[2].by_user and changes[2].after.status == "forgotten"
    assert [c.op for c in store.journal(since=2)] == ["forget", "restore"]
    assert [c.op for c in store.journal(limit=1)] == ["restore"]


def test_undoing_a_creation_forgets_the_note_and_undoing_that_brings_it_back(store, proj):
    made = note(store, "Title", "Text.", scope="project", project=proj)
    assert store.undo(1).status == "forgotten"
    assert store.search("Text", project=proj) == []
    assert store.undo(2).status == "active", "the forgetting is a change like any other: it can be taken back"
    assert store.search("Text", project=proj)[0].entry.id == made.id


def test_undoing_an_update_puts_the_old_text_back(store, proj):
    made = note(store, "Title", "Text one.", scope="project", project=proj, tags=["x"])
    store.update(made.id, by=DROID, text="Text two.", title="New title", tags=["y"], kind="pitfall")
    back = store.undo(2)
    assert (back.title, back.text, back.tags, back.kind, back.editor, back.rev) == ("Title", "Text one.", ("x",), "fact", "user", 3)
    assert store.search("one", project=proj)[0].entry.id == made.id and store.search("two", project=proj) == [], "the index follows"


def test_forgetting_a_forgotten_note_or_restoring_an_active_one_changes_nothing(store, proj):
    made = note(store, "Title", "Text.", scope="project", project=proj)
    assert store.restore(made.id, by="user") == made, "it is active already"
    forgotten = store.forget(made.id, by=CODEX)
    assert (forgotten.status, forgotten.rev) == ("forgotten", 2), "forgetting is a revision of the note"
    again = store.forget(made.id, by="user")
    assert again == forgotten and again.editor == CODEX, "the second forget changed nothing"
    store.restore(made.id, by="user")
    assert [c.op for c in store.journal()] == ["create", "forget", "restore"], "and left nothing in the journal"


def test_a_note_keeps_at_most_eight_tags_and_each_is_short(store, proj):
    made = note(store, "Title", "Text.", scope="project", project=proj, tags=[f"tag{n}" for n in range(12)] + ["x" * 60])
    assert made.tags == tuple(f"tag{n}" for n in range(8))
    assert note(store, "Other title", "Other text.", scope="global", tags=["y" * 60]).tags == ("y" * 24,)


def test_undo_of_something_that_is_not_there_says_so(store):
    with pytest.raises(MemoryRejected) as missing:
        store.undo(99)
    assert missing.value.code == "not_found"


def test_the_user_can_make_a_project_note_global_and_back(store, proj, other):
    made = note(store, "Move me", "Text.", scope="project", project=proj)
    up = store.update(made.id, by="user", scope="global")
    assert up.scope == "global" and up.project == "" and not (store.root / "projects" / project_key(proj) / f"{made.id}.md").exists()
    assert (store.root / "global" / f"{made.id}.md").is_file() and store.search("Text", project=other)[0].entry.id == made.id
    down = store.update(made.id, by="user", scope="project", move_to=other)
    assert down.project == project_path(other) and store.search("Text", project=proj) == []
    with pytest.raises(MemoryRejected):
        store.update(made.id, by="user", scope="global")  # (fine) …
        store.update(made.id, by="user", scope="project")  # … but a project note needs a project


# ----------------------------------------------------------------------------- the index


def test_the_index_is_rebuilt_when_it_is_missing_or_out_of_step(tmp_path, proj):
    first = MemoryStore(tmp_path / "memory")
    made = note(first, "Findable", "Rebuildable widget text.", scope="project", project=proj)
    for gone in (first.index_path, Path(str(first.index_path) + "-wal"), Path(str(first.index_path) + "-shm")):
        if gone.exists():
            gone.unlink()
    assert [h.entry.id for h in MemoryStore(tmp_path / "memory").search("widget", project=proj)] == [made.id]
    # a note put in by hand (or by a writer that died before it updated the index) is found after a rebuild
    extra = first._path(made).with_name("m-1-abcdef.md")
    extra.write_text(first._path(made).read_text(encoding="utf-8").replace(made.id, "m-1-abcdef").replace("Findable", "Handmade"), encoding="utf-8")
    assert {h.entry.title for h in MemoryStore(tmp_path / "memory").search("widget", project=proj)} == {"Findable", "Handmade"}
    first.rebuild_index()
    assert first.counts() == {"active": 2}


def test_a_note_file_that_is_damaged_is_skipped_not_fatal(tmp_path, proj):
    first = MemoryStore(tmp_path / "memory")
    note(first, "Good", "Good widget note.", scope="project", project=proj)
    (first.root / "global").mkdir(exist_ok=True)
    (first.root / "global" / "m-0-bad.md").write_text("not a note at all", encoding="utf-8")
    (first.root / "global" / "m-0-bad2.md").write_text("---\nid: {broken\n---\nbody\n", encoding="utf-8")
    assert [h.entry.title for h in MemoryStore(tmp_path / "memory").search("widget", project=proj)] == ["Good"]


def test_the_version_changes_when_something_is_written(store, proj):
    before = store.version()
    note(store, scope="project", project=proj)
    assert store.version() != before


# ----------------------------------------------------------------------------- writers at once


WRITER = """
import sys
from desktop.core.memory.store import MemoryStore, agent
store = MemoryStore(sys.argv[1])
worker = sys.argv[2]
for n in range(int(sys.argv[3])):
    store.create(title=f"Note {worker} {n} " + "xyz"[n % 3] * (n + 1), text=f"Written by {worker}, number {n}, " + "abcdefghijklmnop"[n % 16] * (n + 3),
                 scope="project", project=sys.argv[4], by=agent("codex", worker))
"""


def test_writers_in_several_processes_at_once_lose_nothing_and_corrupt_nothing(tmp_path, proj):
    root = tmp_path / "memory"
    MemoryStore(root)
    env = {**os.environ, "PYTHONPATH": str(PACKAGE_ROOT)}
    workers = [subprocess.Popen([sys.executable, "-c", WRITER, str(root), f"w{i}", "8", proj], env=env, stderr=subprocess.PIPE) for i in range(4)]
    for process in workers:
        _, err = process.communicate(timeout=120)
        assert process.returncode == 0, err.decode("utf-8", "replace")
    fresh = MemoryStore(root)
    entries = fresh.entries()
    assert len(entries) == 32 and len({e.id for e in entries}) == 32
    seqs = [c.seq for c in fresh.journal()]
    assert seqs == list(range(1, 33)), "one journal, every number once, in order"
    assert fresh.counts() == {"active": 32} and len(list((root / "projects" / project_key(proj)).glob("*.md"))) == 32
    assert not list(root.rglob("*.tmp")), "no half-written file is left"
    for entry in entries:
        assert fresh.read(entry.id) == entry


def test_a_lock_held_too_long_is_a_clear_refusal_not_a_hang(store, monkeypatch, proj):
    monkeypatch.setattr(store_module, "LOCK_SECONDS", 0.3)
    with store._locked():
        other_view = MemoryStore(store.root)
        with pytest.raises(MemoryRejected) as busy:
            note(other_view, scope="project", project=proj)
    assert busy.value.code == "busy"
    note(other_view, scope="project", project=proj)  # the lock is released: it works


def test_a_journal_line_is_valid_json_with_the_fields_the_application_reads(store, proj):
    made = note(store, scope="project", project=proj)
    (line,) = [json.loads(l) for l in store.journal_path.read_text(encoding="utf-8").splitlines()]
    assert set(line) == {"seq", "op", "id", "before", "after", "by", "at"} and line["id"] == made.id and line["by"] == CODEX
    assert Entry.from_json(line["after"]) == made
