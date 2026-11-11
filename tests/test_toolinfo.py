from __future__ import annotations

import math

import pytest

from desktop.core.toolinfo import (
    Category,
    count_categories,
    counts_text,
    describe_input,
    display_path,
    exit_code,
    first_line,
    format_count,
    format_credits,
    format_duration,
    head_preview,
    parse_todos,
    steps_text,
    tail_preview,
    tool_category,
    turn_summary_text,
    working_label,
)


@pytest.mark.parametrize(
    ("name", "category"),
    [
        ("Read", Category.READ),
        ("LS", Category.READ),
        ("Glob", Category.READ),
        ("Grep", Category.READ),
        ("FetchUrl", Category.READ),
        ("Edit", Category.EDIT),
        ("Create", Category.EDIT),
        ("ApplyPatch", Category.EDIT),
        ("MultiEdit", Category.EDIT),
        ("Execute", Category.COMMAND),
        ("TodoWrite", Category.PLAN),
        ("AskUser", Category.ASK),
        ("execute", Category.COMMAND),
        ("Task", Category.OTHER),
        ("SomethingNew", Category.OTHER),
        ("", Category.OTHER),
    ],
)
def test_tool_category(name, category):
    assert tool_category(name) is category


def test_counts_text_uses_fixed_category_order_and_skips_zero():
    counts = count_categories(
        [Category.COMMAND, Category.READ, Category.EDIT, Category.READ, Category.PLAN, Category.PLAN, Category.PLAN, Category.PLAN]
    )
    assert counts_text(counts) == "读取 2 · 编辑 1 · 命令 1 · 计划 4"
    assert steps_text(8, counts) == "8 步 · 读取 2 · 编辑 1 · 命令 1 · 计划 4"
    assert steps_text(0, {}) == "0 步"
    assert counts_text({Category.ASK: 1, Category.OTHER: 2}) == "提问 1 · 其他 2"


@pytest.mark.parametrize(
    ("seconds", "text"),
    [
        (0.04, "0.0 秒"),
        (5.43, "5.4 秒"),
        (9.94, "9.9 秒"),
        (10.2, "10 秒"),
        (25.29, "25 秒"),
        (59.4, "59 秒"),
        (59.6, "1 分 00 秒"),
        (125, "2 分 05 秒"),
        (618.2, "10 分 18 秒"),
        (3720, "1 小时 02 分"),
        (None, "—"),
        (-1, "—"),
        (math.nan, "—"),
        (math.inf, "—"),
    ],
)
def test_format_duration(seconds, text):
    assert format_duration(seconds) == text


def test_turn_summary_text():
    assert turn_summary_text(125, 12) == "用时 2 分 05 秒 · 12 步"
    assert turn_summary_text(5.0, 0) == "用时 5.0 秒"
    assert turn_summary_text(None, 3) == "用时 — · 3 步"


@pytest.mark.parametrize(
    ("value", "text"),
    [
        (0, "0"),
        (932, "932"),
        (999, "999"),
        (1000, "1k"),
        (17573, "17.6k"),
        (91153, "91.2k"),
        (123456, "123k"),
        (999960, "1M"),
        (1_234_567, "1.2M"),
        (123_456_789, "123M"),
        (-5, "-5"),
        (None, "—"),
        (math.nan, "—"),
    ],
)
def test_format_count(value, text):
    assert format_count(value) == text


def test_format_credits():
    assert format_credits(None) == "—"
    assert format_credits(832.0) == "832"
    assert format_credits(22370.0) == "22.4k"
    assert format_credits(4.54) == "4.5"
    assert format_credits(12.0) == "12"
    assert format_credits(0.04) == "0"
    assert format_credits(432.7) == "433"


def test_working_label():
    assert working_label("thinking") == "正在思考…"
    assert working_label("waiting_for_tool_confirmation") == "等待你的确认"
    assert working_label("something_new") == "处理中…"
    assert working_label(None) == "处理中…"
    assert working_label("thinking", stopping=True) == "正在停止…"


def test_previews_normalize_newlines_and_report_clipping():
    text = "\r\n".join(f"line {i}" for i in range(30)) + "\r\n\r\n"
    head, clipped = head_preview(text, max_lines=3)
    assert head == "line 0\nline 1\nline 2" and clipped
    tail, clipped = tail_preview(text, max_lines=2)
    assert tail == "line 28\nline 29" and clipped
    short, clipped = head_preview("a\r\nb")
    assert short == "a\nb" and not clipped
    long_head, clipped = head_preview("x" * 3000, max_chars=100)
    assert len(long_head) == 100 and clipped
    assert tail_preview("")[0] == ""


def test_first_line_skips_blank_lines_and_clips():
    assert first_line("\n\n  hello world \nsecond") == "hello world"
    assert first_line("x" * 500, 20) == "x" * 19 + "…"
    assert first_line("") == ""


def test_display_path_shortens_long_windows_paths():
    assert display_path("D:\\a\\b.txt") == "D:\\a\\b.txt"
    shortened = display_path("C:\\very\\long\\" + "x" * 80 + "\\file.txt", 40)
    assert len(shortened) == 40 and shortened.startswith("…\\")


def test_exit_code_takes_the_last_marker():
    assert exit_code("out\n[Process exited with code 0]") == 0
    assert exit_code("[Process exited with code 1]\nmore\n[Process exited with code -1]") == -1
    assert exit_code("no marker") is None
    assert exit_code(None) is None


def test_exit_code_reads_the_line_wittycodes_own_shell_tool_ends_with():
    assert exit_code("hello\nExit code: 0") == 0
    assert exit_code("boom\nExit code: 3\n") == 3
    assert exit_code("Exit code: 2\nstill printing\nExit code: -1") == -1
    assert exit_code("the word Exit code: 5 in the middle of a line") is None
    assert exit_code("Timed out after 5 s\nExit code: 1") == 1


def test_parse_todos_from_text_and_lists():
    todos = parse_todos("1. [completed] Read files\n2. [in_progress] Edit the\n   continuation line\n3) [pending] Run tests")
    assert [(entry.status, entry.text) for entry in todos] == [
        ("completed", "Read files"),
        ("in_progress", "Edit the continuation line"),
        ("pending", "Run tests"),
    ]
    listed = parse_todos([{"content": "A", "status": "Completed"}, {"title": "B"}, "junk"])
    assert [(entry.status, entry.text) for entry in listed] == [("completed", "A"), ("pending", "B")]
    assert parse_todos(None) == []


@pytest.mark.parametrize(
    ("name", "data", "expected"),
    [
        ("Read", {"file_path": "D:\\p\\a.py", "offset": 10, "limit": 20}, "D:\\p\\a.py（第 10–30 行）"),
        ("Read", {"file_path": "a.py", "offset": 5}, "a.py（从第 5 行）"),
        ("LS", {"directory_path": "D:\\p"}, "D:\\p"),
        ("Glob", {"patterns": ["*.py", "*.md"], "folder": "D:\\p"}, "*.py, *.md · D:\\p"),
        ("Grep", {"pattern": "foo", "path": "src"}, "“foo” · src"),
        ("Execute", {"command": "ls -la\necho hi"}, "ls -la"),
        ("Execute", {"command": "", "summary": "list files"}, "list files"),
        ("TodoWrite", {"todos": "1. [completed] a\n2. [in_progress] b\n3. [pending] c"}, "1/3 完成 · b"),
        ("AskUser", {"questionnaire": "1. [question] Which?\n2. [question] And?"}, "Which?（共 2 题）"),
        ("AskUser", {"questionnaire": "1. [question] Which?"}, "Which?"),
        ("Edit", {"file_path": "D:\\a\\b.txt", "old_str": "x"}, "D:\\a\\b.txt"),
        ("Create", {"file_path": "D:\\a\\new.txt", "content": "x"}, "D:\\a\\new.txt"),
        (
            "ApplyPatch",
            {"input": "*** Begin Patch\n*** Update File: a.py\n@@\n-x\n+y\n*** Add File: b.py\n+z\n*** End Patch"},
            "a.py 等 2 个文件",
        ),
        ("Task", {"description": "investigate"}, "investigate"),
        ("FetchUrl", {"url": "https://example.com/x"}, "https://example.com/x"),
        ("WebSearch", {"query": "qt webchannel"}, "qt webchannel"),
        ("Skill", {"skill": "review"}, "review"),
        ("Unknown", {"count": 3, "text": "hello\nworld"}, "hello"),
        ("Read", {}, ""),
    ],
)
def test_describe_input(name, data, expected):
    assert describe_input(name, data) == expected