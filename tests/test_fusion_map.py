"""Project maps use portable paths and stay independent of Fusion Drive state."""
import os
import stat
import subprocess

import pytest

from desktop.core.fusion import map as project_map
from desktop.core.fusion.inject import ENTRY_TOKENS
from desktop.core.fusion.map import (
    STATUSES, MapEntry, ProjectMap, digest, entries_for, entry_text, initial, needs_review, parse, read, render, touch,
    traits_text, write,
)
from desktop.core.harness.compact import estimate_text


def _file(project, path, text=""):
    target = project / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    return target


@pytest.mark.parametrize("location", ["CODEOWNERS", ".github/CODEOWNERS", "docs/CODEOWNERS"])
def test_initial_prefers_codeowners(location, tmp_path):
    _file(tmp_path, location, "# Comment\n* @team\n/zeta/** @team\n/alpha/core/ @team\n/zeta/ @team\n"
          "/../escape/** @team\n/node_modules/ @team\n")
    _file(tmp_path, "some_package/__init__.py")
    draft = initial(tmp_path)
    assert draft == ProjectMap((MapEntry("alpha/core", ("alpha/core",)), MapEntry("zeta", ("zeta",))))
    assert "alpha/core" in digest(draft) and "zeta" in digest(draft) and "@team" not in digest(draft)


def test_initial_reads_workspace_and_python_package_definitions(tmp_path):
    _file(tmp_path, "package.json", '{"workspaces": {"packages": ["web/*", "!web/sample", "../outside"]}}')
    _file(tmp_path, "web/tool/main.ts")
    _file(tmp_path, "web/sample/main.ts")
    _file(tmp_path, "web/.cache/main.ts")
    _file(tmp_path, "engine/__init__.py")
    _file(tmp_path, "src/declared/mod.py")
    _file(tmp_path, "lib/extra/mod.py")
    _file(tmp_path, "misc/script.py")
    _file(tmp_path, "pyproject.toml", '[tool.setuptools]\npackages = ["declared"]\npackage-dir = {"" = "src"}\n'
          '[tool.poetry]\npackages = [{include = "extra", from = "lib"}]\n')
    assert [entry.paths for entry in initial(tmp_path).entries] == [
        ("engine",), ("lib/extra",), ("src/declared",), ("web/tool",),
    ]


def test_initial_falls_back_to_sorted_source_folders(tmp_path):
    _file(tmp_path, "package.json", "{invalid")
    for path in ("zeta/nested/runner.go", "alpha/client.ts", "misc/readme.txt", "ignored/build/nested.py",
                 "node_modules/repo/example.js", ".git/fake.py", ".hidden/example.py", "__pycache__/file.py",
                 ".venv/mod.py", "venv/mod.py", "dist/mod.ts", "build/mod.py"):
        _file(tmp_path, path)
    assert initial(tmp_path) == ProjectMap((MapEntry("alpha", ("alpha",)), MapEntry("zeta", ("zeta",))))


def test_empty_map_and_scan_budget(tmp_path, monkeypatch):
    assert initial(tmp_path) == ProjectMap(())
    assert "no modules" in digest(initial(tmp_path))
    _file(tmp_path, "src/main.py")
    monkeypatch.setattr(project_map, "_SCAN_LIMIT", 1)
    assert initial(tmp_path) == ProjectMap(())


@pytest.mark.parametrize("link_name", ["linked", ".github"])
def test_initial_does_not_follow_directory_links(tmp_path, link_name):
    root = tmp_path / "project"
    external = tmp_path / "other"
    _file(external, "CODEOWNERS", "/external/ @team\n")
    _file(external, "examples/demo.py")
    _file(root, "actual/main.py")
    link = root / link_name
    if os.name == "nt":
        # A junction exercises Windows reparse points without symlink privileges.
        result = subprocess.run(["cmd", "/d", "/c", "mklink", "/J", str(link), str(external)], capture_output=True)
        if result.returncode:
            pytest.skip("Directory junctions are unavailable")
    else:
        link.symlink_to(external, target_is_directory=True)
    try:
        assert initial(root) == ProjectMap((MapEntry("actual", ("actual",)),))
    finally:
        link.rmdir() if os.name == "nt" else link.unlink()


def test_render_parse_round_trip():
    draft = ProjectMap((MapEntry("Core", ("desktop/core", "core,extras"), "职责内容\nSecond line", "实现内容"),
                        MapEntry("Tools", ())), "工程说明\n- Trait detail")
    markdown = render(draft)
    assert markdown.startswith("# 项目地图\n由 WittyCode")
    assert '## Core\n- 路径：desktop/core, "core,extras"\n- 职责：职责内容' in markdown
    assert "## 工程特点\n工程说明" in markdown and parse(markdown) == draft
    assert all(status not in markdown for status in STATUSES)
    assert parse(render(ProjectMap(()))) == ProjectMap(())


def test_parse_tolerates_hand_edits():
    text = """Unrelated introduction

## Core
- 实现方法: Fixed order

- Unknown: ignored

- 路径： core, ./tests, ../private, Z:/outside
- 职责： Route work

## 工程特点

Plain files

"""
    assert parse(text) == ProjectMap((MapEntry("Core", ("core", "tests"), "Route work", "Fixed order"),), "Plain files")
    with pytest.raises(ValueError):
        parse("# Title but no section\n- 路径：src\n")


@pytest.mark.parametrize("path", ("/rooted", "Z:/absolute", "Z:drive-relative", r"\rooted", r"\\host\share", "../parent", "core/../parent"))
def test_render_refuses_machine_or_parent_paths(path):
    with pytest.raises(ValueError):
        render(ProjectMap((MapEntry("Unsafe", (path,)),)))


def test_write_overwrite_and_read_hand_edits(tmp_path):
    assert read(tmp_path) is None
    a = ProjectMap((MapEntry("First", ("a",)),))
    b = ProjectMap((MapEntry("Second", ("b",), "路由"),))
    target = write(tmp_path, a)
    assert target == tmp_path / ".witty_map.md" and read(tmp_path) == a
    assert write(tmp_path, b) == target
    assert read(tmp_path) == b and target.read_bytes() == render(b).encode("utf-8")
    assert list(tmp_path.iterdir()) == [target]
    if os.name == "nt":
        assert not target.stat().st_file_attributes & stat.FILE_ATTRIBUTE_HIDDEN
    target.write_text("## Second\n- 路径：c\n", encoding="utf-8")
    assert read(tmp_path) == ProjectMap((MapEntry("Second", ("c",)),))
    target.write_bytes(b"\xff")
    assert read(tmp_path) is None


def test_touch_and_overlapping_paths():
    engine = MapEntry("Engine", ("src/engine",), "Runs jobs")
    runtime = MapEntry("Runtime", ("src/runtime",))
    draft = ProjectMap((engine, runtime), "Plain files")
    assert entries_for(draft, ("src",)) == (engine, runtime)
    assert entries_for(draft, (r"src\engine\api.py", "src/runtime-extra")) == (engine,)
    assert entries_for(draft, ("src/engine-other.py",)) == ()
    assert touch(draft, "Workstream", ("src",)) == (draft, ("Engine", "Runtime"))
    assert touch(draft, "Empty", ()) == (draft, ())
    updated, names = touch(draft, "Docs", ("./docs/guide.md", r"docs\guide.md"))
    assert names == ("Docs",) and updated.entries == (*draft.entries, MapEntry("Docs", ("docs/guide.md",)))
    assert updated.traits == draft.traits
    expanded, names = touch(draft, "Engine", ("extra",))
    assert names == ("Engine",) and expanded.entries == (MapEntry("Engine", ("src/engine", "extra"), "Runs jobs"), runtime)


def test_needs_review_matches_only_changes_under_the_entry():
    entry = MapEntry("Core", ("src/core", "tool.py"))
    assert needs_review(entry, ("src/core/api.py",))
    assert needs_review(entry, ("tool.py",))
    assert not needs_review(entry, ("src", "src/core-old/demo.py", "tool.py.bak", "../src/core/api.py"))


def test_l0_text_limits():
    entry = MapEntry("Core", ("src/core",), "职责" * 900, "实现" * 900)
    text = entry_text(entry)
    assert "Module: Core" in text and "Paths: src/core" in text
    assert estimate_text(text) <= ENTRY_TOKENS
    assert traits_text(ProjectMap(())) == ""
    traits = traits_text(ProjectMap((), "特色" * 1_000))
    assert traits.startswith("Engineering traits:") and estimate_text(traits) <= ENTRY_TOKENS
