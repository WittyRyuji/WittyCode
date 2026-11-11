"""``core.importers.factory``: Droid's personal instruction files and session files."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from desktop.core.importers import ImportFailed
from desktop.core.importers import factory as factory_module
from desktop.core.importers.factory import FactorySource
from tests.importer_support import FAKE_KEY, DroidSession, text_block, tool_use, write


@pytest.fixture
def home(tmp_path: Path) -> Path:
    folder = tmp_path / "home"
    (folder / ".factory").mkdir(parents=True)
    return folder


def source(home: Path) -> FactorySource:
    return FactorySource(home / ".factory", home=home)


def test_personal_instruction_files_are_notes_and_droids_or_commands_are_not(home):
    factory = home / ".factory"
    write(factory / "AGENTS.md", f"# 个人约定\n\n- 提交前跑测试\n- token = {FAKE_KEY}\n")
    write(factory / "DESIGN.md", "# Design\n\nUse the system font.\n")
    write(home / ".agents" / "AGENTS.md", "# Shared agents file\n\nKeep diffs small.\n")
    write(home / ".agent" / "CLAUDE.md", "# Singular folder\n\nAlso read.\n")
    write(factory / "droids" / "reviewer.md", "---\nname: reviewer\n---\nYou are a reviewer.\n")
    write(factory / "commands" / "ship.md", "# Ship\n\nShip it.\n")
    src = source(home)
    found = {note.title: note for note in src.memories()}
    assert set(found) == {"个人约定", "Design", "Shared agents file", "Singular folder"}
    assert FAKE_KEY not in found["个人约定"].text and src.report.secret_lines == 1
    assert "design" in found["Design"].tags and all(note.scope == "global" for note in found.values())


@pytest.mark.skipif(os.name != "nt", reason="the names differ only in case: one file on Windows")
def test_a_file_reached_under_several_spellings_of_its_name_is_imported_once(home):
    write(home / ".factory" / "AGENTS.md", "# Once\n\ntext\n")
    assert [note.title for note in source(home).memories()] == ["Once"]


def session(cwd: str = "D:\\work\\app") -> DroidSession:
    return (
        DroidSession("aaaaaaaa-1111-2222-3333-444444444444", cwd, title="整理测试")
        .message("user", [text_block("<system-reminder>context only for the model</system-reminder>")], message_id="context-1")
        .message("user", [text_block("请整理测试\n\n<wittycode-context>\nrecalled notes\n</wittycode-context>")])
        .message("user", [text_block("<system-reminder>todo</system-reminder>")], message_id="todo-sug-1")
        .message("user", [text_block("internal")], message_id="__internal__system-reminder__1")
        .message("user", [text_block("only for the model")], visibility="llm_only")
        .message("assistant", [{"type": "thinking", "thinking": "hidden", "signature": "s"}, text_block("先看一下。"), tool_use("Read", "t1", file_path="tests/a.py")], message_id="a1")
        .message("user", [{"type": "tool_result", "tool_use_id": "t1", "content": "file text that must not be imported", "is_error": False}])
        .message("assistant", [tool_use("Execute", "t2", command="pytest -q", riskLevel="low")], message_id="repl-1")
        .message("user", [{"type": "tool_result", "tool_use_id": "t2", "content": "5 passed"}], message_id="repl-2")
        .message("assistant", [text_block("都通过了。")], message_id="a2")
        .other("todo_state", todos={"todos": []}, messageIndex=3)
        .other("agent_turn_outcome", reason="completed", resultKind="text", turnId="t")
        .raw("{broken")
        .message("user", [{"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AAAA"}}, text_block("<system-reminder>Attached image local file paths: x</system-reminder>"), text_block("看这张图")])
        .other("compaction_state", summaryText="a summary that is not a message", summaryKind="llm_summary")
        .message("assistant", [text_block("看到了。")], message_id="a3")
    )


def test_a_session_gives_what_the_page_shows_and_a_line_per_tool_call(home):
    session().write(home / ".factory")
    src = source(home)
    (info,) = src.sessions()
    messages = src.read(info)
    assert [(m.role, m.text) for m in messages] == [
        ("user", "请整理测试"),
        ("assistant", "先看一下。\n[工具] Read tests/a.py\n[工具] Execute pytest -q"),
        ("assistant", "都通过了。"),
        ("user", "看这张图"),
        ("assistant", "看到了。"),
    ]
    assert src.report.bad_records == 1
    assert (info.title, info.project, info.native_id, info.messages, info.exact) == ("整理测试", "D:\\work\\app", "aaaaaaaa-1111-2222-3333-444444444444", 5, True)
    assert info.started < info.updated


def test_sub_agent_sessions_backups_and_empty_sessions_are_not_listed(home):
    factory = home / ".factory"
    session().write(factory)
    DroidSession("bbbbbbbb-1111-2222-3333-444444444444", "D:\\w", calling="aaaaaaaa-1111-2222-3333-444444444444").message("user", [text_block("task for the sub-agent")], message_id="task-1").write(factory)
    DroidSession("cccccccc-1111-2222-3333-444444444444", "D:\\w").write(factory)
    write(factory / "sessions" / "-C-work-app" / "aaaaaaaa-1111-2222-3333-444444444444.jsonl.bak", "{}\n")
    src = source(home)
    assert [info.native_id for info in src.sessions()] == ["aaaaaaaa-1111-2222-3333-444444444444"]
    assert src.report.skipped == 2


def test_without_a_title_the_first_words_are_the_title(home):
    DroidSession("dddddddd-1111-2222-3333-444444444444", "D:\\w", title="").message("user", [text_block("第一句话\n第二行")]).write(home / ".factory")
    (info,) = source(home).sessions()
    assert info.title == "第一句话"


def test_a_big_session_is_listed_from_its_start_and_end(home, monkeypatch):
    monkeypatch.setattr(factory_module, "FULL_SCAN", 2500)
    monkeypatch.setattr(factory_module, "HEAD", 1500)
    monkeypatch.setattr(factory_module, "TAIL", 1500)
    big = DroidSession("eeeeeeee-1111-2222-3333-444444444444", "D:\\w").message("user", [text_block("start")])
    for number in range(30):
        big.message("assistant", [text_block(f"answer {number} " + "z" * 60)], message_id=f"a{number}")
    big.write(home / ".factory")
    src = source(home)
    (info,) = src.sessions()
    assert not info.exact and 0 < info.messages < 31 and info.title == "整理测试"
    assert len(src.read(info)) == 31


def test_reading_a_session_that_is_gone_fails_with_a_sentence(home):
    path = session().write(home / ".factory")
    src = source(home)
    (info,) = src.sessions()
    path.unlink()
    with pytest.raises(ImportFailed) as failure:
        src.read(info)
    assert "找不到" in failure.value.message


def test_no_folder_means_not_available(tmp_path):
    src = FactorySource(tmp_path / "none" / ".factory")
    assert not src.available() and src.memories() == [] and src.sessions() == []
