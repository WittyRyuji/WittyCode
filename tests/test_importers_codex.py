"""``core.importers.codex``: Codex's memories, global instructions and rollout files (plain and zstd-compressed)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from desktop.core.importers import ImportFailed, sources
from desktop.core.importers import codex as codex_module
from desktop.core.importers.codex import CodexSource
from tests.importer_support import BEARER, Rollout, codex_state, compressed_rollout, write, write_jsonl, zstd_frame

THREAD = "019a1111-2222-7333-8444-955556666777"


@pytest.fixture
def home(tmp_path: Path) -> Path:
    folder = tmp_path / "codex"
    folder.mkdir()
    return folder


def build_memories(home: Path) -> None:
    write(home / "AGENTS.md", "# Global rules\n\nAnswer in Chinese.\n")
    memories = home / "memories"
    write(memories / "MEMORY.md", "# Task group: builds\n\n## Pitfalls\n\n- the build needs Node 22\n")
    write(memories / "memory_summary.md", "# User profile\n\nPrefers short answers.\n")
    write(memories / "raw_memories.md", "# Raw memories\n\n- raw note one\n")
    write(memories / "rollout_summaries" / "2026-11-07T10-00-00-abcd-fix_build.md", "# Fixing the build\n\nIt was the lock file.\n")
    write(memories / "extensions" / "ad_hoc" / "instructions.md", "# How to write ad hoc notes\n\nWrite one file per note.\n")
    write(memories / "extensions" / "ad_hoc" / "notes" / "2026-11-07-port.md", "# Port\n\nThe dev server uses port 5173.\n")
    write(memories / "skills" / "deploy" / "SKILL.md", "# Deploy\n\nRun the script.\n")
    write(memories / "phase2_workspace_diff.md", "# Diff\n\n+ added\n")
    write(memories / ".git" / "description", "# git\n\nnot a note\n")
    write(memories / ".codex-baseline" / "lock" / "x.md", "# baseline\n\nnot a note\n")
    write(home / "memories_v2" / "memory_summary.md", "# User Profile\n\n# Tips\n\n## More\n\n# End\n")


def test_memories_are_the_global_instructions_and_the_memory_files_only(home):
    build_memories(home)
    found = {note.title: note for note in CodexSource(home).memories()}
    assert set(found) == {"Global rules", "Pitfalls", "User profile", "Raw memories", "Fixing the build", "Port"}
    assert all(note.scope == "global" and note.app == "codex" for note in found.values())
    assert found["Pitfalls"].kind == "pitfall"
    assert "rollout" in found["Fixing the build"].tags and "raw" in found["Raw memories"].tags and "note" in found["Port"].tags


def main_thread(cwd: str = "D:\\work\\app") -> Rollout:
    return (
        Rollout(THREAD, cwd, start=1_790_000_000.0)
        .developer("<permissions instructions>\nsandboxed\n</permissions instructions>")
        .context(cwd)
        .user("修一下构建\n\n<wittycode-context>\nnotes the app added\n</wittycode-context>")
        .event("task_started", turn_id="t1")
        .reasoning()
        .assistant("先看看。", "msg_1", phase="commentary")
        .call("shell", {"command": ["powershell", "-Command", "npm run build"], "workdir": cwd}, "call_1", "big output that must not be imported")
        .custom("apply_patch", "*** Begin Patch\n*** Update File: src/a.ts\n@@\n-x\n+y\n*** Add File: src/b.ts\n+z\n*** End Patch", "call_2")
        .custom("exec", "// find the config\nconst found = await tools.exec_command({cmd: 'git status'});\n", "call_3")
        .call("sleep", {"duration_ms": 1000}, "call_4")
        .call("exec_command", {"cmd": f"curl -H '{BEARER}' https://example.com"}, "call_5")
        .raw("{broken json")
        .assistant("修好了。", "msg_2")
        .event("task_complete", turn_id="t1")
        .user("<heartbeat>\n<automation_id>a1</automation_id>\n</heartbeat>")
        .user("<send_user_message_question_reply>用方案二</send_user_message_question_reply>")
        .assistant("好的，用方案二。", "msg_3")
        .event("token_count", info={"total_token_usage": {"input_tokens": 3}})
    )


def test_a_rollout_gives_the_users_words_the_answers_and_one_line_per_call(home):
    main_thread().write(home)
    src = CodexSource(home)
    (info,) = src.sessions()
    messages = src.read(info)
    assert [(m.role, m.text) for m in messages] == [
        ("user", "修一下构建"),
        (
            "assistant",
            "先看看。\n[工具] shell npm run build\n[工具] apply_patch src/a.ts, src/b.ts\n[工具] exec const found = await tools.exec_command({cmd: 'git status'});\n[工具] exec_command",
        ),
        ("assistant", "修好了。"),
        ("user", "用方案二"),
        ("assistant", "好的，用方案二。"),
    ]
    assert src.report.bad_records == 1 and src.report.secret_lines == 1
    assert info.exact and info.messages == 5 and info.project == "D:\\work\\app" and info.title == "修一下构建"
    assert info.native_id == THREAD and info.key == f"codex:{THREAD}"


def test_threads_of_sub_agents_are_not_listed(home):
    Rollout("019a2222-0000-7000-8000-900000000001", "D:\\w", source={"subagent": {"thread_spawn": {"parent_thread_id": THREAD}}}, thread_source="subagent").user("sub").write(home)
    Rollout("019a2222-0000-7000-8000-900000000002", "D:\\w", thread_source=None).user("main").write(home)
    src = CodexSource(home)
    assert [info.native_id for info in src.sessions()] == ["019a2222-0000-7000-8000-900000000002"]
    assert src.report.skipped == 1


def test_a_compressed_rollout_is_read_through_the_zstd_decoder(home):
    rollout = compressed_rollout()
    path = home / "sessions" / "2026" / "11" / "01" / (rollout.name() + ".zst")
    path.parent.mkdir(parents=True)
    path.write_bytes(zstd_frame("rollout"))
    src = CodexSource(home)
    (info,) = src.sessions()
    assert info.path == str(path) and info.native_id == rollout.thread
    assert info.title == "压缩过的会话里的问题" and info.project == "D:\\work\\zipped"
    assert not info.exact
    messages = src.read(info)
    assert [(m.role, m.text) for m in messages] == [
        ("user", "压缩过的会话里的问题"),
        ("assistant", "压缩过的会话里的回答\n[工具] shell Get-ChildItem"),
        ("assistant", "列好了。"),
    ]


def test_the_thread_list_gives_titles_folders_and_times_without_opening_compressed_files(home, monkeypatch):
    rollout = compressed_rollout()
    path = home / "archived_sessions" / (rollout.name() + ".zst")
    path.parent.mkdir(parents=True)
    path.write_bytes(zstd_frame("rollout"))
    codex_state(home, [{"id": rollout.thread, "title": "first words", "name": "", "cwd": "D:\\from-db", "created_at": 1_790_000_000, "updated_at": 1_790_000_900, "updated_at_ms": 1_790_000_900_500, "source": "vscode", "thread_source": "user"}])
    write_jsonl(home / "session_index.jsonl", [{"id": rollout.thread, "thread_name": "Old name", "updated_at": "x"}, "{bad", {"id": rollout.thread, "thread_name": "Named thread", "updated_at": "y"}])
    opened = []
    real = codex_module.lines
    monkeypatch.setattr(codex_module, "lines", lambda path, **k: (opened.append(path) if path.name.endswith(".zst") else None) or real(path, **k))
    (info,) = CodexSource(home).sessions()
    assert opened == []
    assert info.title == "Named thread" and info.project == "D:\\from-db"
    assert info.updated == pytest.approx(1_790_000_900.5) and info.started == 1_790_000_000
    assert info.messages == 0 and not info.exact


def test_a_title_from_the_thread_list_and_sub_agents_it_marks(home):
    Rollout(THREAD, "D:\\a").user("typed first").write(home)
    other = "019a3333-0000-7000-8000-900000000003"
    Rollout(other, "D:\\b").user("also main in its file").write(home)
    codex_state(home, [{"id": THREAD, "name": "From the list", "title": "t"}, {"id": other, "thread_source": "subagent", "source": "{\"subagent\": {}}"}])
    (info,) = CodexSource(home).sessions()
    assert info.native_id == THREAD and info.title == "From the list"


def test_a_plain_file_is_preferred_to_a_compressed_or_archived_copy(home):
    rollout = compressed_rollout()
    plain = write_jsonl(home / "sessions" / "2026" / "11" / "02" / rollout.name(), rollout.lines)
    archived = home / "archived_sessions" / (rollout.name() + ".zst")
    archived.parent.mkdir(parents=True)
    archived.write_bytes(zstd_frame("rollout"))
    (info,) = CodexSource(home).sessions()
    assert info.path == str(plain) and info.exact


def test_a_plain_copy_is_preferred_to_a_compressed_one_in_the_same_folder(home):
    rollout = compressed_rollout()
    plain = write_jsonl(home / "sessions" / "2026" / "11" / "02" / rollout.name(), rollout.lines)
    plain.with_name(plain.name + ".zst").write_bytes(zstd_frame("rollout"))
    (info,) = CodexSource(home).sessions()
    assert info.path == str(plain) and info.exact


def test_a_wrapped_part_codex_added_is_not_the_users_words_even_when_the_tag_is_new(home):
    Rollout(THREAD, "D:\\a").user("<session_context>\nleft over state\n</session_context>", "the real question").write(home)
    src = CodexSource(home)
    (info,) = src.sessions()
    assert [(m.role, m.text) for m in src.read(info)] == [("user", "the real question")]
    assert info.title == "the real question"


def test_the_oldest_rollout_format_without_wrappers_is_read(home):
    lines = [
        {"id": THREAD, "timestamp": "2025-06-01T10:00:00.000Z", "instructions": None},
        {"record_type": "state"},
        {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "<user_instructions>\nrules\n</user_instructions>"}]},
        {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "old question"}]},
        {"type": "function_call", "name": "shell", "arguments": json.dumps({"command": ["bash", "-lc", "ls -la"]}), "call_id": "c1"},
        {"type": "function_call_output", "call_id": "c1", "output": "{\"output\": \"files\"}"},
        {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "old answer"}]},
    ]
    write_jsonl(home / "sessions" / "2025" / "06" / "01" / f"rollout-2025-06-01T10-00-00-{THREAD}.jsonl", lines)
    src = CodexSource(home)
    (info,) = src.sessions()
    assert [(m.role, m.text) for m in src.read(info)] == [("user", "old question"), ("assistant", "[工具] shell ls -la"), ("assistant", "old answer")]
    assert info.started == pytest.approx(1_748_772_000.0)


def test_a_big_plain_rollout_is_listed_from_its_start_and_end(home, monkeypatch):
    monkeypatch.setattr(codex_module, "FULL_SCAN", 3000)
    monkeypatch.setattr(codex_module, "HEAD", 2000)
    monkeypatch.setattr(codex_module, "TAIL", 2000)
    rollout = Rollout(THREAD, "D:\\big").user("start")
    for number in range(60):
        rollout.assistant(f"answer {number} " + "y" * 40, f"m{number}")
    rollout.write(home)
    src = CodexSource(home)
    (info,) = src.sessions()
    assert not info.exact and 0 < info.messages < 61
    assert len(src.read(info)) == 61


def test_a_damaged_compressed_rollout_is_skipped_and_cannot_be_read(home):
    rollout = compressed_rollout()
    path = home / "sessions" / "2026" / "11" / "01" / (rollout.name() + ".zst")
    path.parent.mkdir(parents=True)
    path.write_bytes(zstd_frame("rollout")[:200])
    src = CodexSource(home)
    assert src.sessions() == [] and src.report.skipped == 1
    info = codex_module.SessionInfo("codex:x", "codex", rollout.thread, "t", "", 0, 0, 0, 0, str(path), False)
    with pytest.raises(ImportFailed) as failure:
        src.read(info)
    assert "解压" in failure.value.message


def test_a_thread_list_that_is_not_a_database_is_ignored(home):
    write(home / "state_5.sqlite", "not a database")
    Rollout(THREAD, "D:\\a").user("still listed").write(home)
    (info,) = CodexSource(home).sessions()
    assert info.title == "still listed"


def test_the_thread_list_is_opened_read_only_and_left_as_it_was(home):
    Rollout(THREAD, "D:\\a").user("x").write(home)
    db = codex_state(home, [{"id": THREAD, "name": "n"}])
    before = (db.read_bytes(), db.stat().st_mtime_ns)
    CodexSource(home).sessions()
    assert (db.read_bytes(), db.stat().st_mtime_ns) == before
    assert sorted(p.name for p in home.iterdir() if p.name.startswith("state_")) == ["state_5.sqlite"]


def test_codex_home_comes_from_the_environment_unless_given(tmp_path):
    env = {"CODEX_HOME": str(tmp_path / "codex-home")}
    codex = next(s for s in sources(home=tmp_path / "home", env=env) if s.app == "codex")
    assert codex.root == tmp_path / "codex-home"
    default = next(s for s in sources(home=tmp_path / "home", env={}) if s.app == "codex")
    assert default.root == tmp_path / "home" / ".codex"
    given = next(s for s in sources(home=tmp_path / "home", env=env, codex_home=tmp_path / "x") if s.app == "codex")
    assert given.root == tmp_path / "x"


def test_no_folder_means_not_available(tmp_path):
    src = CodexSource(tmp_path / "none")
    assert not src.available() and src.memories() == [] and src.sessions() == []
