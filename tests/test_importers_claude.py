"""``core.importers.claude``: Claude Code's instruction files, auto memory and transcripts."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from desktop.core.importers import ImportFailed, sources
from desktop.core.importers import claude as claude_module
from desktop.core.importers.claude import ClaudeSource, encode
from tests.importer_support import FAKE_KEY, ClaudeTranscript, make_repo, text_block, tool_use, write


@pytest.fixture
def home(tmp_path: Path) -> Path:
    folder = tmp_path / "home"
    folder.mkdir()
    return folder


def source(home: Path) -> ClaudeSource:
    return ClaudeSource(home / ".claude", home=home)


def by_title(found):
    return {note.title: note for note in found}


def build_memory(home: Path) -> Path:
    claude = home / ".claude"
    app = make_repo(home / "work" / "app")
    (app / "sub").mkdir()
    write(claude / "CLAUDE.md", "# 我的偏好\n\n- 回答用中文\n")
    write(claude / "rules" / "style.md", "# Style\n\nPrefer small functions.\n")
    write(claude / "rules" / "nested" / "testing.md", "---\npaths:\n  - \"tests/**\"\n---\n# Testing rules\n\nAlways run pytest.\n")
    write(app / "CLAUDE.md", "# App commands\n\n- npm test\n")
    write(app / ".claude" / "CLAUDE.md", "# App layout\n\n- src/ holds the code\n")
    write(app / "CLAUDE.local.md", "# Local\n\n- my sandbox is on port 3001\n")
    write(app / ".claude" / "rules" / "api.md", "# API rules\n\n- validate every input\n")
    # (the transcripts of a session started in a subfolder go under the subfolder's name; auto memory under the repository's)
    ClaudeTranscript(str(app / "sub")).user("hello").write(claude, "lossy-name-that-says-nothing")
    memory = claude / "projects" / encode(str(app)) / "memory"
    write(memory / "MEMORY.md", "# Memory index\n\n- [User role](user_role.md) — who the user is\n- [Testing](feedback_testing.md) — how to test\n\nThe staging server is staging.example.com.\n")
    write(memory / "user_role.md", "---\nname: User role\ndescription: Senior engineer, likes short answers\ntype: user\n---\n\nKeeps answers short.\n")
    write(memory / "feedback_testing.md", "---\nname: Testing\ndescription: Integration tests hit a real database\ntype: feedback\n---\n\nDo not mock the database.\n")
    write(memory / "project_release.md", "---\nname: Release freeze\ntype: project\n---\n\nNo merges on Fridays.\n")
    write(claude / "projects" / "C--nowhere-known" / "memory" / "MEMORY.md", "# Lost\n\nA note of a project no transcript names.\n")
    return app


def test_global_project_and_auto_memory_notes_with_their_scopes(home):
    app = build_memory(home)
    src = source(home)
    found = by_title(src.memories())
    assert found["我的偏好"].scope == "global" and found["我的偏好"].project == ""
    assert found["Style"].scope == "global" and "rules" in found["Style"].tags
    assert found["Testing rules"].scope == "global" and "paths:" not in found["Testing rules"].text
    for title in ("App commands", "App layout", "Local", "API rules"):
        assert found[title].scope == "project" and found[title].project == str(app), title
    assert "lossy" not in " ".join(note.project for note in found.values())


def test_auto_memory_topic_files_keep_their_name_description_and_type(home):
    app = build_memory(home)
    found = by_title(source(home).memories())
    role, testing, release = found["User role"], found["Testing"], found["Release freeze"]
    assert (role.kind, testing.kind, release.kind) == ("preference", "convention", "fact")
    assert role.text.startswith("Senior engineer, likes short answers") and "Keeps answers short." in role.text
    assert role.project == str(app) and role.scope == "project"
    assert "auto-memory" in role.tags and "user" in role.tags and "feedback" in testing.tags


def test_the_memory_index_gives_only_what_is_not_a_link_to_a_topic_file(home):
    build_memory(home)
    found = source(home).memories()
    index = [note for note in found if note.origin.endswith("MEMORY.md") or "MEMORY.md ›" in note.origin]
    texts = " ".join(note.text for note in index)
    assert "staging.example.com" in texts
    assert "user_role.md" not in texts and "feedback_testing.md" not in texts


def test_a_memory_folder_no_transcript_names_is_left_out_and_counted(home):
    build_memory(home)
    src = source(home)
    found = src.memories()
    assert not any("A note of a project no transcript names." in note.text for note in found)
    assert src.report.skipped == 1


def test_a_session_in_the_home_folder_does_not_import_the_global_file_twice(home):
    claude = home / ".claude"
    write(claude / "CLAUDE.md", "# Global\n\nonce\n")
    ClaudeTranscript(str(home)).user("hi").write(claude)
    found = source(home).memories()
    assert [note.title for note in found] == ["Global"] and found[0].scope == "global"


def test_projects_claude_code_lists_in_its_state_file_are_found_without_transcripts(home):
    app = make_repo(home / "work" / "listed")
    write(app / "CLAUDE.md", "# Listed project\n\n- from the state file\n")
    write(home / ".claude" / "projects" / encode(str(app)) / "memory" / "MEMORY.md", "# Kept\n\nAuto memory of a project whose transcripts were cleaned up.\n")
    write(home / ".claude.json", json.dumps({"projects": {str(app).replace("\\", "/"): {"allowedTools": []}, "": {}}, "userID": "x"}))
    found = {note.title: note for note in source(home).memories()}
    assert found["Listed project"].project == str(app)
    assert found["Kept"].project == str(app) and found["Kept"].scope == "project"


def test_a_moved_config_folder_has_its_state_file_inside(tmp_path):
    moved = tmp_path / "config"
    app = make_repo(tmp_path / "work")
    write(app / "CLAUDE.md", "# Inside\n\ntext\n")
    write(moved / ".claude.json", json.dumps({"projects": {str(app): {}}}))
    write(tmp_path / "home" / ".claude.json", json.dumps({"projects": {"C:/elsewhere": {}}}))
    found = ClaudeSource(moved, home=tmp_path / "home").memories()
    assert [note.title for note in found] == ["Inside"]
    write(moved / ".claude.json", "{not json")
    assert ClaudeSource(moved, home=tmp_path / "home").memories() == []


def test_secrets_in_instruction_files_are_dropped(home):
    write(home / ".claude" / "CLAUDE.md", f"# Keys\n\nUse the vault.\nkey: {FAKE_KEY}\n")
    src = source(home)
    (note,) = src.memories()
    assert FAKE_KEY not in note.text and src.report.secret_lines == 1


def conversation(cwd: str) -> ClaudeTranscript:
    return (
        ClaudeTranscript(cwd)
        .bookkeeping()
        .user("Caveat: The messages below were generated by the user while running local commands.", isMeta=True)
        .user("<command-name>/review</command-name>\n<command-message>review</command-message>\n<command-args>src/a.py</command-args>")
        .user("<local-command-stdout>Done</local-command-stdout>")
        .user("Please look at src/a.py")
        .assistant([{"type": "thinking", "thinking": "hidden", "signature": "x"}], "msg_1")
        .assistant([text_block("Reading it.")], "msg_1")
        .assistant([tool_use("Read", "toolu_1", file_path="src/a.py")], "msg_1")
        .tool_result("toolu_1", "file content that must not be imported")
        .assistant([text_block("It has a bug."), tool_use("Bash", "toolu_2", command="npm test\n--watch")], "msg_2")
        .tool_result("toolu_2", "test output that must not be imported")
        .user([text_block("<system-reminder>reminder</system-reminder>"), text_block("[Request interrupted by user]")])
        .user([text_block("Fix it"), {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AAAA"}}])
        .assistant([text_block("sidechain talk")], "msg_side", isSidechain=True)
        .assistant([text_block("API Error: 401")], "msg_err", model="<synthetic>", isApiErrorMessage=True)
        .assistant([text_block("Fixed.")], "msg_3")
        .user("This session is being continued from a previous conversation.", isCompactSummary=True)
        .raw("{not json")
        .raw("[1, 2]")
    )


def test_a_transcript_gives_the_conversation_and_one_line_per_tool_call(home):
    app = make_repo(home / "work" / "app")
    conversation(str(app)).write(home / ".claude")
    src = source(home)
    (info,) = src.sessions()
    messages = src.read(info)
    assert [(m.role, m.text) for m in messages] == [
        ("user", "/review src/a.py"),
        ("user", "Please look at src/a.py"),
        ("assistant", "Reading it.\n[工具] Read src/a.py"),
        ("assistant", "It has a bug.\n[工具] Bash npm test"),
        ("user", "Fix it"),
        ("assistant", "Fixed."),
    ]
    assert src.report.bad_records == 2
    assert info.messages == len(messages) and info.exact
    assert info.project == str(app)
    assert all(m.at for m in messages) and messages[0].at <= messages[-1].at
    assert info.started <= info.updated


def test_the_title_is_the_rename_then_the_generated_title_then_a_summary_then_the_first_words(home):
    claude = home / ".claude"
    base = lambda session: ClaudeTranscript("D:\\w", session=session).user("First question here\nsecond line")  # noqa: E731
    base("a1").meta("summary", summary="A summary", leafUuid="u").meta("ai-title", aiTitle="Generated").meta("custom-title", customTitle="My name").meta("ai-title", aiTitle="Later generated").write(claude)
    base("a2").meta("summary", summary="A summary", leafUuid="u").meta("ai-title", aiTitle="Old").meta("ai-title", aiTitle="Newest").write(claude)
    base("a3").meta("summary", summary="Only a summary", leafUuid="u").write(claude)
    base("a4").write(claude)
    titles = {info.native_id: info.title for info in source(home).sessions()}
    assert titles == {"a1": "My name", "a2": "Newest", "a3": "Only a summary", "a4": "First question here"}


def test_set_aside_copies_sub_agents_and_empty_transcripts_are_not_listed(home):
    claude = home / ".claude"
    folder = claude / "projects" / "D--w"
    ClaudeTranscript("D:\\w", session="main").user("real").write(claude, "D--w")
    ClaudeTranscript("D:\\w", session="main.orphaned-1790000000-abc").user("copy").write(claude, "D--w")
    ClaudeTranscript("D:\\w", session="agent-123").user("old sidechain file").write(claude, "D--w")
    write(folder / "main" / "subagents" / "agent-x.jsonl", ClaudeTranscript("D:\\w").user("sub").lines[0].__repr__())
    ClaudeTranscript("D:\\w", session="empty").bookkeeping().write(claude, "D--w")
    write(folder / "broken.jsonl", "{{{{\n")
    src = source(home)
    assert [info.native_id for info in src.sessions()] == ["main"]
    assert src.report.skipped == 2


def test_sessions_are_newest_first(home):
    claude = home / ".claude"
    ClaudeTranscript("D:\\w", session="old", start=1_700_000_000.0).user("old").write(claude)
    ClaudeTranscript("D:\\w", session="new", start=1_800_000_000.0).user("new").write(claude)
    ClaudeTranscript("D:\\w", session="mid", start=1_750_000_000.0).user("mid").write(claude)
    assert [info.native_id for info in source(home).sessions()] == ["new", "mid", "old"]


def test_a_big_transcript_is_listed_from_its_start_and_end(home, monkeypatch):
    monkeypatch.setattr(claude_module, "FULL_SCAN", 2000)
    monkeypatch.setattr(claude_module, "HEAD", 1500)
    monkeypatch.setattr(claude_module, "TAIL", 1500)
    transcript = ClaudeTranscript("D:\\w", session="big").user("start of it")
    for number in range(40):
        transcript.assistant([text_block(f"answer {number} " + "x" * 50)], f"msg_{number}")
    transcript.meta("custom-title", customTitle="Renamed at the end")
    transcript.write(home / ".claude")
    src = source(home)
    (info,) = src.sessions()
    assert not info.exact and 0 < info.messages < 41
    assert info.title == "Renamed at the end"
    assert len(src.read(info)) == 41


def test_reading_a_transcript_that_is_gone_is_an_import_failure(home):
    claude = home / ".claude"
    path = ClaudeTranscript("D:\\w", session="gone").user("x").write(claude)
    src = source(home)
    (info,) = src.sessions()
    path.unlink()
    with pytest.raises(ImportFailed) as failure:
        src.read(info)
    assert "找不到" in failure.value.message


def test_no_folder_means_not_available_and_nothing_found(tmp_path):
    src = ClaudeSource(tmp_path / "missing" / ".claude")
    assert not src.available()
    assert src.memories() == [] and src.sessions() == []


def test_the_config_folder_can_be_moved_with_its_environment_variable(tmp_path):
    moved = tmp_path / "elsewhere"
    write(moved / "CLAUDE.md", "# Moved\n\ntext\n")
    claude = next(s for s in sources(home=tmp_path / "home", env={"CLAUDE_CONFIG_DIR": str(moved)}) if s.app == "claude")
    assert claude.root == moved and [n.title for n in claude.memories()] == ["Moved"]


def test_git_root_stops_at_the_home_folder(home):
    inner = home / "a" / "b"
    inner.mkdir(parents=True)
    assert claude_module.git_root(str(inner), home) == ""
    make_repo(home / "a")
    assert claude_module.git_root(str(inner), home) == str(home / "a")
