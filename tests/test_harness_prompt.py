"""``core.harness.prompt``: what the model is told about where it is and what it may do."""

from __future__ import annotations

import datetime

from desktop.core.harness import prompt
from desktop.core.harness.prompt import build_instructions, find_agents_file, read_agents

DAY = datetime.date(2026, 11, 4)


def test_the_prompt_says_where_the_model_is_and_what_runs_without_asking(tmp_path):
    text = build_instructions(tmp_path, model="gpt-6.1-sol", level="code", today=DAY)
    assert str(tmp_path) in text and "gpt-6.1-sol" in text and "2026-11-04" in text
    assert "PowerShell" in text and "inside the project run directly" in text


def test_each_permission_level_is_described_differently():
    texts = {level: build_instructions(__import__("pathlib").Path("."), model="m", level=level, today=DAY) for level in ("ask", "code", "full")}
    assert len(set(texts.values())) == 3
    assert "without asking the user" in texts["full"] and "waits for the user's approval" in texts["ask"]


def test_the_nearest_agents_file_is_included(tmp_path):
    (tmp_path / "AGENTS.md").write_text("# Rules\n用中文写注释。", encoding="utf-8")
    sub = tmp_path / "pkg" / "inner"
    sub.mkdir(parents=True)
    assert find_agents_file(sub) == tmp_path / "AGENTS.md"
    text = build_instructions(sub, model="m", level="ask", today=DAY)
    assert "用中文写注释。" in text and "Follow them" in text


def test_a_nearer_agents_file_wins(tmp_path):
    (tmp_path / "AGENTS.md").write_text("outer", encoding="utf-8")
    inner = tmp_path / "pkg"
    inner.mkdir()
    (inner / "AGENTS.md").write_text("inner", encoding="utf-8")
    assert read_agents(inner)[1] == "inner"


def test_no_agents_file_is_fine(tmp_path, monkeypatch):
    monkeypatch.setattr(prompt.Path, "home", staticmethod(lambda: tmp_path))
    assert read_agents(tmp_path) is None
    assert "AGENTS.md" not in build_instructions(tmp_path, model="m", level="ask", today=DAY).split("Environment:")[1]


def test_an_empty_or_huge_agents_file(tmp_path):
    (tmp_path / "AGENTS.md").write_text("   \n", encoding="utf-8")
    assert read_agents(tmp_path) is None
    (tmp_path / "AGENTS.md").write_text("x" * 100_000, encoding="utf-8")
    path, text = read_agents(tmp_path)
    assert len(text) < 25_000 and text.endswith("left out ...]")


def long_file(*, rules_last: bool = True) -> str:
    """A file the size of this repository's own: a big reference section ahead of the rules it ends with."""
    return (
        "# Project\n\nWhat this is, in two lines.\n\n"
        "## Commands\n\n- run the tests: pytest -q\n\n"
        "## Facts\n\n" + "A long detail about the SDK. " * 2_000 + "\n\n"
        "## More facts\n\n" + "Another long detail. " * 1_500 + "\n\n"
        "## 规则\n\n- 不提交、不推送。\n- 新功能都要有测试。\n"
        + ("" if rules_last else "\n## Tail\n\n" + "tail text " * 300)
    )


def test_a_file_over_the_limit_is_cut_by_section_and_the_rules_it_ends_with_stay(tmp_path):
    text = long_file()
    assert len(text) > 2 * prompt.AGENTS_LIMIT
    (tmp_path / "AGENTS.md").write_text(text, encoding="utf-8")
    path, kept = read_agents(tmp_path)
    assert "不提交、不推送。" in kept and "新功能都要有测试。" in kept, "the rules at the end are in the prompt"
    assert "pytest -q" in kept and "What this is, in two lines." in kept
    for heading in ("# Project", "## Commands", "## Facts", "## More facts", "## 规则"):
        assert heading in kept, f"{heading} stays as an outline"
    assert len(kept) < prompt.AGENTS_LIMIT + 200
    assert kept.count("characters of this section were left out") == 2


def test_sections_that_hold_rules_keep_more_than_the_others(tmp_path):
    body = "word " * 4_000
    (tmp_path / "AGENTS.md").write_text(f"# T\n\n## Background\n\n{body}\n\n## Conventions\n\n{body}\n\n## Notes\n\n{body}\n", encoding="utf-8")
    context = prompt.load_agents(tmp_path, limit=9_000)
    (item,) = context.files
    kept = {cut.heading: cut.kept for cut in item.cuts}
    assert set(kept) == {"Background", "Conventions", "Notes"}
    assert kept["Conventions"] > kept["Background"] and kept["Conventions"] > kept["Notes"]
    assert prompt.SECTION_SHARE <= kept["Background"]


def test_what_was_cut_is_told_to_the_model_and_to_the_user(tmp_path):
    (tmp_path / "AGENTS.md").write_text(long_file(), encoding="utf-8")
    notes: list[str] = []
    text = build_instructions(tmp_path, model="m", level="ask", today=DAY, notes=notes)
    assert "sections marked as left out were shortened" in text and "read_file" in text
    (note,) = notes
    assert "AGENTS.md" in note and "「Facts」" in note and "按章节缩减" in note and f"{prompt.AGENTS_LIMIT:,}" in note
    short: list[str] = []
    (tmp_path / "AGENTS.md").write_text("# Small\n\nfits.", encoding="utf-8")
    assert "shortened" not in build_instructions(tmp_path, model="m", level="ask", today=DAY, notes=short)
    assert short == [], "nothing to tell when the file went in whole"


def test_the_files_of_the_parent_folders_up_to_the_repository_root_are_merged_nearest_last(tmp_path, monkeypatch):
    monkeypatch.setattr(prompt.Path, "home", staticmethod(lambda: tmp_path))
    (tmp_path / "AGENTS.md").write_text("OUTSIDE THE REPOSITORY", encoding="utf-8")
    repo = tmp_path / "work" / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / "AGENTS.md").write_text("ROOT RULES", encoding="utf-8")
    package = repo / "pkg"
    package.mkdir()
    (package / "AGENTS.md").write_text("PACKAGE RULES", encoding="utf-8")
    text = build_instructions(package, model="m", level="ask", today=DAY)
    assert "ROOT RULES" in text and "PACKAGE RULES" in text
    assert text.index("ROOT RULES") < text.index("PACKAGE RULES"), "the more specific one comes last"
    assert "OUTSIDE THE REPOSITORY" not in text, "the repository's root is as far as it goes"
    assert "the one nearer the working folder wins" in text
    assert [item.path for item in prompt.load_agents(package).files] == [repo / "AGENTS.md", package / "AGENTS.md"]
    only_root = build_instructions(repo, model="m", level="ask", today=DAY)
    assert "ROOT RULES" in only_root and "PACKAGE RULES" not in only_root


def test_a_parent_file_that_finds_no_room_is_left_out_and_said(tmp_path, monkeypatch):
    monkeypatch.setattr(prompt.Path, "home", staticmethod(lambda: tmp_path))
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / "AGENTS.md").write_text("ROOT RULES", encoding="utf-8")
    package = repo / "pkg"
    package.mkdir()
    (package / "AGENTS.md").write_text("# Own\n\n" + "own detail " * 3_000, encoding="utf-8")
    notes: list[str] = []
    text = build_instructions(package, model="m", level="ask", today=DAY, notes=notes)
    assert "ROOT RULES" not in text and "not loaded for lack of room" in text and str(repo / "AGENTS.md") in text
    assert any("放不下" in note and str(repo / "AGENTS.md") in note for note in notes)


def test_this_repositorys_own_rules_reach_the_prompt():
    from pathlib import Path

    project = Path(__file__).resolve().parent.parent
    if not (project / "AGENTS.md").is_file():
        return
    _, kept = read_agents(project)
    assert "## 规则" in kept and "不推送" in kept, "the rules section is the one the old cut lost"


def test_server_notes_are_included_and_empty_ones_skipped(tmp_path):
    text = build_instructions(tmp_path, model="m", level="ask", servers=[("wittycode-memory", "Use memory_search first."), ("quiet", "  ")], today=DAY)
    assert "wittycode-memory" in text and "Use memory_search first." in text and "quiet" not in text
