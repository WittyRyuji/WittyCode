"""``core.importers.split``: Markdown memory files as candidate notes for the shared memory."""

from __future__ import annotations

import re

import pytest

from desktop.core.importers.base import Report, text_digest
from desktop.core.importers.split import MERGE_LIMIT, cut, front_matter, guess_kind, split_markdown, tidy
from desktop.core.memory.safety import find_secret
from desktop.core.memory.store import MAX_TAG, MAX_TAGS, MAX_TEXT, MAX_TITLE, MemoryStore, agent
from tests.importer_support import BEARER, FAKE_KEY

FILE = "C:\\Users\\me\\.claude\\CLAUDE.md"


def notes(text: str, **options):
    options.setdefault("app", "claude")
    options.setdefault("origin_file", FILE)
    options.setdefault("scope", "global")
    return split_markdown(text, **options)


def fences_balanced(text: str) -> bool:
    return len(re.findall(r"^\s{0,3}```", text, re.MULTILINE)) % 2 == 0


def test_a_small_file_is_one_note_titled_by_its_only_heading():
    (note,) = notes("# 提交信息\n\n- 用中文写\n- 标题一行\n")
    assert note.title == "提交信息"
    assert note.text == "- 用中文写\n- 标题一行"
    assert note.origin == f"{FILE} › 提交信息"
    assert note.app == "claude" and note.scope == "global" and note.project == ""


def test_a_heading_inside_a_code_block_is_text_not_a_section():
    (note,) = notes("# Real heading\n\n```markdown\n# not a heading\nstill code\n```\n\nthe end\n")
    assert note.title == "Real heading"
    assert note.text == "```markdown\n# not a heading\nstill code\n```\n\nthe end"


def test_small_sections_are_merged_up_to_the_limit_and_keep_their_headings():
    text = "# Rules\n\n" + "".join(f"## Topic {i}\n\nOne line about topic {i}.\n\n" for i in range(40)) + "intro " * 0
    found = notes("# Guide\n\n" + "intro text " * 150 + "\n\n" + text)
    assert len(found) < 15
    merged = [n for n in found if "## Topic 1" in n.text or "### Topic 1" in n.text or n.title.startswith("Topic")]
    assert merged and all(len(n.text) <= MERGE_LIMIT for n in merged)
    assert any(" / " in n.title or " 等 " in n.title for n in found)
    body = "\n".join(n.text for n in found)
    for i in range(40):
        assert f"One line about topic {i}." in body


def test_a_section_with_its_small_subsections_is_one_note():
    (note,) = notes("## 测试\n\n先跑单测。\n\n### 单测\n\n- pytest -q\n\n### e2e\n\n- 整文件连跑 20 次\n")
    assert note.title == "测试"
    assert "### 单测" in note.text and "### e2e" in note.text and "先跑单测。" in note.text


def test_a_big_section_is_cut_between_paragraphs_and_the_parts_are_numbered():
    paragraphs = [f"Paragraph {i}: " + "word " * 60 for i in range(30)]
    found = notes("## Long\n\n" + "\n\n".join(paragraphs))
    assert len(found) >= 3
    assert [n.title for n in found] == ["Long"] + [f"Long ({i})" for i in range(2, len(found) + 1)]
    for note in found:
        assert len(note.text) <= MAX_TEXT
        assert note.text.startswith("Paragraph ") and note.text.rstrip().endswith("word")
    assert "\n\n".join(n.text for n in found).count("Paragraph ") == 30


def test_list_items_stay_whole_and_a_tight_list_stays_tight():
    items = [f"- item {i} " + "x" * 90 for i in range(60)]
    found = notes("## List\n\n" + "\n".join(items))
    assert len(found) >= 3
    for note in found:
        assert all(line.startswith("- item ") for line in note.text.split("\n"))
    assert "\n".join(n.text for n in found).split("\n") == items


def test_a_code_block_is_never_cut_open_and_one_too_long_is_cut_between_its_lines():
    code = "```python\n" + "\n".join(f"print('line {i}')" for i in range(300)) + "\n```"
    found = notes("## Code\n\nBefore.\n\n" + code + "\n\nAfter.")
    assert len(found) >= 3
    for note in found:
        assert len(note.text) <= MAX_TEXT
        assert fences_balanced(note.text)
        for piece in re.findall(r"```python\n(.*?)\n```", note.text, re.DOTALL):
            assert all(line.startswith("print('line ") for line in piece.split("\n"))
    joined = "\n".join(n.text for n in found)
    assert all(f"print('line {i}')" in joined for i in range(300))


def test_a_heading_inside_a_code_block_is_code():
    (note,) = notes("## Shell\n\n```bash\n# not a heading\necho hi\n```\n")
    assert note.title == "Shell" and "# not a heading" in note.text


def test_deeper_headings_are_text():
    (note,) = notes("## A\n\n##### small heading\n\ntext\n")
    assert note.title == "A" and "##### small heading" in note.text


def test_front_matter_comments_and_imports_are_taken_out():
    text = "---\npaths:\n  - \"src/**\"\nname: x\n---\n<!-- for maintainers -->\n@AGENTS.md\n- @~/.claude/mine.md\n\n# Rules\n\nSee @README for the overview.\n\n```html\n<!-- kept in code -->\n```\n"
    (note,) = notes(text)
    assert "for maintainers" not in note.text and "paths:" not in note.text and "@AGENTS.md" not in note.text
    assert "mine.md" not in note.text
    assert "See @README for the overview." in note.text and "<!-- kept in code -->" in note.text


def test_a_file_of_only_imports_or_only_a_table_of_contents_gives_nothing():
    assert notes("@AGENTS.md\n@docs/rules.md\n") == []
    assert notes("# Contents\n\n- [Setup](#setup)\n- [Testing](docs/testing.md)\n1. [More](more.md)\n") == []
    assert notes("") == [] and notes("   \n\n") == []
    assert notes("# Empty\n\n## Also empty\n") == []


def test_empty_sections_are_dropped():
    found = notes("# Doc\n\n" + "x " * 700 + "\n\n## Empty\n\n## Full\n\n" + "y " * 700)
    assert "Empty" not in [n.title for n in found]
    assert "Full" in [n.title for n in found]


def test_text_before_the_first_heading_is_titled_by_its_first_line_or_the_given_title():
    (note,) = notes("Use pnpm, not npm.\nAlways run the linter.\n")
    assert note.title == "Use pnpm, not npm."
    (named,) = notes("Use pnpm.\n", title="包管理器")
    assert named.title == "包管理器" and named.text == "Use pnpm."


def test_lines_that_look_like_secrets_are_dropped_and_counted():
    report = Report()
    text = (
        "# Keys\n\nThe key is kept in the vault.\n"
        f"OPENAI_API_KEY={FAKE_KEY}\n"
        f"curl -H \"{BEARER}\" https://example.com\n"
        "password = hunter2hunter2\n"
        "-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA\nabc\n-----END RSA PRIVATE KEY-----\n"
        "Rotate it every month.\n"
    )
    (note,) = notes(text, report=report)
    assert report.secret_lines == 7
    assert find_secret(note.title, note.text) is None
    assert note.text == "The key is kept in the vault.\nRotate it every month."


def test_the_same_text_gives_the_same_keys_and_other_places_other_keys():
    text = "# A\n\n" + "a " * 700 + "\n\n## B\n\n" + "b " * 700 + "\n\n## B\n\n" + "c " * 700
    first, second = notes(text), notes(text)
    assert [n.key for n in first] == [n.key for n in second]
    assert len({n.key for n in first}) == len(first)
    elsewhere = notes(text, origin_file="C:\\Users\\me\\other\\CLAUDE.md")
    assert not {n.key for n in first} & {n.key for n in elsewhere}
    assert {n.key for n in notes(text, origin_file=FILE.upper())} == {n.key for n in first}  # (Windows: case does not matter)
    assert notes(text, app="codex")[0].key != first[0].key


def test_titles_fit_the_memory_even_with_a_part_number():
    long = "很长的标题" * 30
    found = notes(f"## {long}\n\n" + "\n\n".join("段落。" * 200 for _ in range(5)))
    assert len(found) >= 2
    for note in found:
        assert len(note.title) <= MAX_TITLE
    assert found[0].title.endswith("…") and found[1].title.endswith("… (2)")


def test_tags_are_the_app_the_given_ones_and_heading_words():
    (note,) = notes("# 项目约定\n\n## Commit messages and 提交信息\n\n- 中文\n", base_tags=("rules",))
    assert note.tags[:2] == ("claude", "rules")
    assert "Commit" in note.tags and "messages" in note.tags and "提交信息" in note.tags and "项目约定" in note.tags
    assert "and" not in note.tags
    many = notes("# " + " ".join(f"word{i}" for i in range(20)) + "\n\ntext\n")[0]
    assert len(many.tags) <= MAX_TAGS and all(len(tag) <= MAX_TAG for tag in many.tags)
    upper = notes("# Claude CLAUDE claude\n\ntext\n")[0]
    assert [t.lower() for t in upper.tags].count("claude") == 1


@pytest.mark.parametrize(
    ("heading", "kind"),
    [
        ("我的偏好", "preference"),
        ("Coding style", "preference"),
        ("提交约定", "convention"),
        ("Rules", "convention"),
        ("踩过的坑", "pitfall"),
        ("Known issues and gotchas", "pitfall"),
        ("Architecture", "fact"),
    ],
)
def test_the_kind_is_guessed_from_the_heading(heading, kind):
    assert notes(f"# {heading}\n\nSomething about it.\n")[0].kind == kind


def test_the_kind_falls_back_to_the_text_and_a_given_kind_wins():
    assert guess_kind(["Notes"], "You must run the tests first.") == "convention"
    assert guess_kind(["Notes"], "I prefer tabs.") == "preference"
    assert guess_kind(["Notes"], "这个坑踩过两次。") == "pitfall"
    assert guess_kind(["Notes"], "The server listens on 8080.") == "fact"
    assert notes("# Rules\n\ntext\n", kind="preference")[0].kind == "preference"
    assert notes("# Rules\n\ntext\n", kind="nonsense")[0].kind == "convention"


def test_a_project_note_carries_its_project_and_a_global_one_none():
    (note,) = notes("# A\n\ntext\n", scope="project", project="D:\\work\\app")
    assert note.scope == "project" and note.project == "D:\\work\\app"
    (other,) = notes("# A\n\ntext\n", scope="global", project="D:\\work\\app")
    assert other.project == ""


def test_the_digest_ignores_spacing_and_case():
    (one,) = notes("# A\n\nUse   pnpm.\n")
    (two,) = notes("# B\n\nuse pnpm.\n")
    assert one.digest == two.digest == text_digest("Use pnpm.")
    assert notes("# A\n\nUse npm.\n")[0].digest != one.digest


def test_windows_line_ends_and_a_byte_order_mark_are_read():
    (note,) = notes("\ufeff# 标题\r\n\r\n- 一\r\n- 二\r\n")
    assert note.title == "标题" and note.text == "- 一\n- 二"


def test_front_matter_reads_the_simple_keys():
    meta, body = front_matter("---\nname: \"用户的角色\"\ndescription: 'short'\ntype: user\nlist:\n  - a\n---\n\nbody\n")
    assert meta == {"name": "用户的角色", "description": "short", "type": "user", "list": ""}
    assert body == "body\n"
    assert front_matter("---\nunclosed: x\nbody") == ({}, "---\nunclosed: x\nbody")


def test_cut_never_gives_a_part_over_the_limit_for_any_text():
    sample = ("A sentence that ends here. " * 40 + "\n\n" + "一句话结束了。" * 200 + "\n" + "x" * 5000 + "\n\n- item\n" * 30)
    parts = cut(sample, 500)
    assert parts and all(len(part) <= 500 for part in parts)
    assert "".join(parts).replace("\n", "").replace(" ", "") == sample.strip().replace("\n", "").replace(" ", "")


def test_every_candidate_is_accepted_by_the_shared_memory(tmp_path):
    store = MemoryStore(tmp_path / "memory")
    text = "# 偏好\n\n" + "\n\n".join(f"## 第 {i} 条\n\n" + f"内容 {i}：" + "说明。" * (i * 40) for i in range(1, 9))
    found = notes(text, scope="project", project=str(tmp_path / "work"))
    assert len(found) > 3
    for note in found:
        store.create(title=note.title, text=note.text, scope=note.scope, project=note.project, kind=note.kind, tags=note.tags, by=agent("claude"))
    assert len(store.entries()) == len(found)


def test_titles_the_memory_would_call_the_same_get_the_heading_above_them(tmp_path):
    text = "".join(f"# Task group {name}\n\n" + "intro " * 220 + f"\n\n## Pitfalls\n\n- {name} pitfall\n\n" for name in ("builds", "tests"))
    found = tidy(notes(text))
    titles = [n.title for n in found]
    assert "Pitfalls" in titles and "Task group tests › Pitfalls" in titles
    store = MemoryStore(tmp_path / "memory")
    for note in found:
        store.create(title=note.title, text=note.text, scope=note.scope, project=note.project, kind=note.kind, tags=note.tags, by=agent("codex"))
    assert len(store.entries()) == len(found)


def test_tidy_leaves_out_repeated_texts_and_numbers_what_still_clashes():
    first = notes("# Same\n\nUse pnpm.\n", origin_file="C:\\a\\MEMORY.md")
    again = notes("# Other title\n\nuse   PNPM.\n", origin_file="C:\\b\\notes.md")
    clash = notes("# Same\n\nSomething else.\n", origin_file="C:\\b\\MEMORY.md")
    clash2 = notes("# Same\n\nA third thing.\n", origin_file="C:\\c\\MEMORY.md")
    found = tidy(first + again + clash + clash2)
    assert [n.title for n in found] == ["Same", "MEMORY › Same", "MEMORY › Same (2)"]
    assert [n.key for n in found] == [first[0].key, clash[0].key, clash2[0].key]


def test_tidy_compares_within_a_scope_and_project_and_with_the_notes_already_there():
    global_note = notes("# Testing\n\nglobal\n")
    project_note = notes("# Testing\n\nproject\n", scope="project", project="D:\\a")
    other_project = notes("# Testing\n\nother\n", scope="project", project="D:\\b")
    assert [n.title for n in tidy(global_note + project_note + other_project)] == ["Testing"] * 3
    assert tidy(global_note, existing=[("global", "", "testing")])[0].title == "CLAUDE › Testing"
    assert tidy(project_note, existing=[("project", "d:\\A", "Testing")])[0].title != "Testing"
    assert tidy(project_note, existing=[("project", "D:\\b", "Testing")])[0].title == "Testing"
