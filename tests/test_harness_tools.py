"""The harness tool registry and the file tools: contract, arguments, every tool's success and failure paths."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path

import pytest

from desktop.core import toolinfo
from desktop.core.harness import fs_tools
from desktop.core.harness.tools import ToolContext, ToolError, ToolRegistry, ToolResult, ToolSpec, validate_arguments

WINDOWS = pytest.mark.skipif(sys.platform != "win32", reason="Windows path semantics")


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def project(tmp_path: Path) -> Path:
    folder = tmp_path / "proj"
    folder.mkdir()
    return folder


@pytest.fixture
def ctx(project: Path) -> ToolContext:
    return ToolContext(project=project)


@pytest.fixture
def registry() -> ToolRegistry:
    return ToolRegistry.default()


def call(registry: ToolRegistry, ctx: ToolContext, name: str, **args) -> ToolResult:
    return run(registry.run(name, args, ctx))


def make_dir_link(link: Path, target: Path) -> None:
    """A symlink if the account may create one, else a junction (no privilege needed); skip when neither works."""
    try:
        os.symlink(target, link, target_is_directory=True)
        return
    except (OSError, NotImplementedError):
        pass
    if sys.platform == "win32":
        done = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True)
        if done.returncode == 0:
            return
    pytest.skip("cannot create a symlink or junction here")


# --------------------------------------------------------------------------- registry contract


def test_default_registry_has_the_nine_tools(registry: ToolRegistry):
    specs = {spec.name: spec for spec in registry.specs()}
    assert list(specs) == ["read_file", "list_dir", "glob", "grep", "create_file", "edit_file", "shell", "todo_write", "skill"]
    assert {name: (spec.ui_name, spec.kind) for name, spec in specs.items()} == {
        "read_file": ("Read", "read"),
        "list_dir": ("LS", "read"),
        "glob": ("Glob", "read"),
        "grep": ("Grep", "read"),
        "create_file": ("Create", "edit"),
        "edit_file": ("Edit", "edit"),
        "shell": ("Execute", "exec"),
        "todo_write": ("TodoWrite", "read"),
        "skill": ("Skill", "read"),
    }
    assert registry.get("grep") is specs["grep"]
    assert registry.get("nope") is None


def test_schemas_are_plain_json_schema_with_required_lists(registry: ToolRegistry):
    for spec in registry.specs():
        schema = spec.parameters
        assert schema["type"] == "object"
        assert schema["required"] and set(schema["required"]) <= set(schema["properties"])
        assert "strict" not in schema and "additionalProperties" not in schema
        assert spec.description.isascii() and len(spec.description) > 40
        for prop in schema["properties"].values():
            assert prop["type"] in ("string", "integer", "boolean", "array")


def test_ui_names_are_categorised_by_the_page(registry: ToolRegistry):
    expected = {"read": toolinfo.Category.READ, "edit": toolinfo.Category.EDIT, "exec": toolinfo.Category.COMMAND}
    for spec in registry.specs():
        # (the checklist is only "read" to the permissions, it touches nothing; the page files it under the plan)
        # (and the skill, which the page has always filed under the others)
        wanted = {"todo_write": toolinfo.Category.PLAN, "skill": toolinfo.Category.OTHER}.get(spec.name, expected[spec.kind])
        assert toolinfo.tool_category(spec.ui_name) is wanted


def test_ui_input_maps_to_the_keys_the_page_reads(registry: ToolRegistry):
    assert registry.ui_input("read_file", {"file_path": "a.py"}) == {"file_path": "a.py"}
    assert registry.ui_input("read_file", {"file_path": "a.py", "offset": 3, "limit": 4}) == {"file_path": "a.py", "offset": 3, "limit": 4}
    assert registry.ui_input("list_dir", {"directory_path": "src"}) == {"directory_path": "src"}
    assert registry.ui_input("glob", {"patterns": ["*.py"], "folder": "src"}) == {"patterns": ["*.py"], "folder": "src"}
    assert registry.ui_input("glob", {"patterns": ["*.py"]}) == {"patterns": ["*.py"]}
    grep = registry.ui_input("grep", {"pattern": "x", "path": "src", "glob_pattern": "*.py", "context": 2})
    assert grep == {"pattern": "x", "path": "src", "glob_pattern": "*.py", "context": 2}
    assert registry.ui_input("create_file", {"file_path": "a", "content": "c"}) == {"file_path": "a", "content": "c"}
    edit = registry.ui_input("edit_file", {"file_path": "a", "old_str": "o", "new_str": "n", "change_all": True})
    assert edit == {"file_path": "a", "old_str": "o", "new_str": "n", "change_all": True}
    assert registry.ui_input("shell", {"command": "ls", "cwd": "src", "timeout": 5}) == {"command": "ls", "cwd": "src", "timeout": 5}
    assert registry.ui_input("shell", {"command": "ls", "cwd": None}) == {"command": "ls"}
    assert registry.ui_input("read_file", "garbage") == {}
    assert registry.ui_input("unknown", {"a": 1}) == {"a": 1}


def test_page_can_describe_every_ui_input(registry: ToolRegistry):
    samples = {
        "read_file": {"file_path": "src/a.py", "offset": 10},
        "list_dir": {"directory_path": "src"},
        "glob": {"patterns": ["**/*.py", "*.md"], "folder": "src"},
        "grep": {"pattern": "TODO", "path": "src"},
        "create_file": {"file_path": "src/new.py", "content": "x"},
        "edit_file": {"file_path": "src/a.py", "old_str": "a", "new_str": "b"},
        "shell": {"command": "git status"},
    }
    for name, args in samples.items():
        spec = registry.get(name)
        assert spec is not None
        text = toolinfo.describe_input(spec.ui_name, registry.ui_input(name, args))
        assert text, name
    assert toolinfo.describe_input("Read", {"file_path": "a.py", "offset": 10}) == "a.py（从第 10 行）"
    assert "**/*.py, *.md" in toolinfo.describe_input("Glob", {"patterns": ["**/*.py", "*.md"]})


def test_result_data_is_the_ui_input(registry: ToolRegistry, ctx: ToolContext):
    result = call(registry, ctx, "create_file", file_path="a.txt", content="x")
    assert result.data == {"file_path": "a.txt", "content": "x"}
    failed = call(registry, ctx, "read_file", file_path="missing.txt")
    assert failed.is_error and failed.data == {"file_path": "missing.txt"}


def test_unknown_tool_is_an_error_result(registry: ToolRegistry, ctx: ToolContext):
    result = run(registry.run("rm_rf", {}, ctx))
    assert result.is_error and "Unknown tool" in result.output and "read_file" in result.output


@pytest.mark.parametrize(
    "name, args, fragment",
    [
        ("read_file", {}, "'file_path' is required"),
        ("read_file", {"file_path": 5}, "'file_path' must be a string"),
        ("read_file", {"file_path": "a", "offset": "three"}, "'offset' must be an integer"),
        ("read_file", {"file_path": "a", "offset": True}, "'offset' must be an integer"),
        ("read_file", {"file_path": "a", "offset": 0}, "at least 1"),
        ("glob", {"patterns": "*.py"}, "'patterns' must be an array"),
        ("glob", {"patterns": [1]}, "array of strings"),
        ("glob", {"patterns": []}, "at least 1"),
        ("grep", {"pattern": "x", "output_mode": "lines"}, "must be one of"),
        ("grep", {"pattern": "x", "case_insensitive": "yes"}, "true or false"),
        ("edit_file", {"file_path": "a", "old_str": "o"}, "'new_str' is required"),
        ("shell", {"command": "ls", "timeout": 601}, "at most 600"),
        ("shell", {"command": None}, "'command' is required"),
    ],
)
def test_invalid_arguments_come_back_as_errors(registry: ToolRegistry, ctx: ToolContext, name, args, fragment):
    result = run(registry.run(name, args, ctx))
    assert result.is_error
    assert fragment in result.output
    assert result.output.startswith(f"Invalid arguments for {name}:")


def test_arguments_must_be_an_object(registry: ToolRegistry, ctx: ToolContext):
    for bad in (None, "x", ["a"], 3):
        result = run(registry.run("read_file", bad, ctx))
        assert result.is_error and "JSON object" in result.output


def test_null_counts_as_absent_unknown_keys_are_errors_and_integral_values_are_accepted():
    schema = fs_tools.READ_FILE.parameters
    assert validate_arguments(schema, {"file_path": "a", "offset": None}) == {"file_path": "a"}
    with pytest.raises(ToolError, match="unknown parameter 'bogus'"):
        validate_arguments(schema, {"file_path": "a", "offset": None, "bogus": 1})
    assert validate_arguments(schema, {"file_path": "a", "limit": "5"}) == {"file_path": "a", "limit": 5}
    assert validate_arguments(schema, {"file_path": "a", "limit": 5.0}) == {"file_path": "a", "limit": 5}
    with pytest.raises(ToolError):
        validate_arguments(schema, {"file_path": "a", "limit": 5.5})


def test_a_failing_handler_never_raises(ctx: ToolContext):
    registry = ToolRegistry()

    def boom(args, context):
        raise RuntimeError("kaput")

    async def async_boom(args, context):
        raise ToolError("tell the model this")

    schema = {"type": "object", "properties": {}, "required": []}
    registry.register(ToolSpec("boom", "d", schema, "Boom", "read"), boom, ui_input=lambda a: {})
    registry.register(ToolSpec("aboom", "d", schema, "ABoom", "read"), async_boom, ui_input=lambda a: {}, is_async=True)
    first = run(registry.run("boom", {}, ctx))
    assert first.is_error and "RuntimeError" in first.output and "kaput" in first.output
    second = run(registry.run("aboom", {}, ctx))
    assert second.is_error and second.output == "tell the model this"
    with pytest.raises(ValueError):
        registry.register(ToolSpec("boom", "d", schema, "Boom", "read"), boom, ui_input=lambda a: {})


def test_context_defaults(project: Path):
    context = ToolContext(project)
    assert context.protected == () and context.shell == "powershell"
    assert not context.abort.is_set()


# --------------------------------------------------------------------------- paths


def test_resolve_path_inside_and_outside(ctx: ToolContext, project: Path):
    path, inside = fs_tools.resolve_path(ctx, "src/a.py")
    assert path == (project / "src" / "a.py").resolve() and inside
    assert fs_tools.resolve_path(ctx, ".")[1] is True
    assert fs_tools.resolve_path(ctx, str(project / "x.txt"))[1] is True
    path, inside = fs_tools.resolve_path(ctx, "../elsewhere.txt")
    assert path == (project.parent / "elsewhere.txt").resolve() and not inside
    assert fs_tools.resolve_path(ctx, "a/../../up.txt")[1] is False
    assert fs_tools.resolve_path(ctx, "a/../back.txt")[1] is True
    assert fs_tools.resolve_path(ctx, str(project.parent))[1] is False
    assert fs_tools.resolve_path(ctx, f'"{project / "q.txt"}"')[0] == (project / "q.txt").resolve()


def test_resolve_path_rejects_empty_and_nul(ctx: ToolContext):
    for raw in ("", "   ", "a\0b"):
        with pytest.raises(ToolError):
            fs_tools.resolve_path(ctx, raw)


@WINDOWS
def test_paths_are_case_insensitive_on_windows(ctx: ToolContext, project: Path):
    (project / "Src").mkdir()
    (project / "Src" / "File.txt").write_text("x", encoding="utf-8")
    shouted = str(project).swapcase() + "\\SRC\\file.TXT"
    assert fs_tools.resolve_path(ctx, shouted)[1] is True
    assert fs_tools.resolve_path(ctx, str(project.parent).swapcase())[1] is False
    protected = ToolContext(project, protected=(project / "Src",))
    with pytest.raises(ToolError):
        fs_tools.read_file({"file_path": str(project).swapcase() + "\\SRC\\file.TXT"}, protected)


def test_link_out_of_the_project_is_outside(ctx: ToolContext, project: Path, tmp_path: Path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "f.txt").write_text("hidden TREASURE", encoding="utf-8")
    make_dir_link(project / "link", outside)
    path, inside = fs_tools.resolve_path(ctx, "link/f.txt")
    assert path == (outside / "f.txt").resolve() and not inside


def test_search_does_not_walk_through_links(ctx: ToolContext, project: Path, tmp_path: Path, registry: ToolRegistry):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "f.txt").write_text("hidden TREASURE", encoding="utf-8")
    (project / "mine.txt").write_text("my TREASURE", encoding="utf-8")
    make_dir_link(project / "link", outside)
    assert call(registry, ctx, "grep", pattern="TREASURE").output == "mine.txt"
    found = call(registry, ctx, "glob", patterns=["**/*.txt"]).output.splitlines()
    assert "mine.txt" in found and not any(line.startswith("link/") for line in found if "/" in line)


# --------------------------------------------------------------------------- protected paths


@pytest.fixture
def guarded(project: Path):
    secret = project / "secrets"
    secret.mkdir()
    (secret / "token.json").write_text('{"access_token": "SEEKRIT"}', encoding="utf-8")
    (project / "open.txt").write_text("SEEKRIT open", encoding="utf-8")
    return ToolContext(project, protected=(secret,)), secret


@pytest.mark.parametrize(
    "name, args",
    [
        ("read_file", {"file_path": "secrets/token.json"}),
        ("list_dir", {"directory_path": "secrets"}),
        ("grep", {"pattern": "SEEKRIT", "path": "secrets"}),
        ("glob", {"patterns": ["*"], "folder": "secrets"}),
        ("create_file", {"file_path": "secrets/new.txt", "content": "x"}),
        ("create_file", {"file_path": "secrets/token.json", "content": "x"}),
        ("edit_file", {"file_path": "secrets/token.json", "old_str": "SEEKRIT", "new_str": "x"}),
        ("read_file", {"file_path": "secrets/../secrets/token.json"}),
    ],
)
def test_protected_paths_are_refused_by_every_tool(guarded, registry: ToolRegistry, name, args):
    context, secret = guarded
    result = run(registry.run(name, args, context))
    assert result.is_error and "protected" in result.output
    assert "SEEKRIT" not in result.output.replace("SEEKRIT open", "")
    assert (secret / "token.json").read_text(encoding="utf-8") == '{"access_token": "SEEKRIT"}'
    assert not (secret / "new.txt").exists()


def test_protected_handlers_refuse_when_called_directly(guarded):
    context, _ = guarded
    with pytest.raises(ToolError, match="protected"):
        fs_tools.read_file({"file_path": "secrets/token.json"}, context)
    with pytest.raises(ToolError, match="protected"):
        fs_tools.edit_file({"file_path": "secrets/token.json", "old_str": "a", "new_str": "b"}, context)


def test_searches_skip_protected_paths_silently(guarded, registry: ToolRegistry):
    context, _ = guarded
    assert run(registry.run("grep", {"pattern": "SEEKRIT"}, context)).output == "open.txt"
    assert run(registry.run("grep", {"pattern": "SEEKRIT", "output_mode": "content"}, context)).output == "open.txt:1:SEEKRIT open"
    found = run(registry.run("glob", {"patterns": ["**/*"]}, context)).output.splitlines()
    assert "open.txt" in found and not any("token" in line for line in found)
    listing = run(registry.run("list_dir", {"directory_path": "."}, context)).output
    assert "open.txt" in listing and "secrets" not in listing


def test_a_link_into_a_protected_folder_is_refused(guarded, registry: ToolRegistry, project: Path):
    context, secret = guarded
    make_dir_link(project / "sneaky", secret)
    result = run(registry.run("read_file", {"file_path": "sneaky/token.json"}, context))
    assert result.is_error and "protected" in result.output
    assert run(registry.run("grep", {"pattern": "SEEKRIT"}, context)).output == "open.txt"


# --------------------------------------------------------------------------- read_file


def test_read_file_numbers_lines(registry, ctx, project):
    (project / "a.txt").write_text("alpha\nbeta\n", encoding="utf-8")
    result = call(registry, ctx, "read_file", file_path="a.txt")
    assert not result.is_error
    assert result.output == "     1\talpha\n     2\tbeta"


def test_read_file_accepts_absolute_paths(registry, ctx, project):
    (project / "a.txt").write_text("alpha", encoding="utf-8")
    assert call(registry, ctx, "read_file", file_path=str(project / "a.txt")).output == "     1\talpha"


def test_read_file_pages_with_offset_and_limit(registry, ctx, project):
    (project / "a.txt").write_text("".join(f"l{n}\n" for n in range(1, 6)), encoding="utf-8")
    result = call(registry, ctx, "read_file", file_path="a.txt", offset=2, limit=2)
    assert result.output.splitlines() == [
        "     2\tl2",
        "     3\tl3",
        "[Showing lines 2-3 of 5. Call read_file again with offset=4 to continue.]",
    ]
    tail = call(registry, ctx, "read_file", file_path="a.txt", offset=4, limit=10)
    assert tail.output == "     4\tl4\n     5\tl5"


def test_read_file_stops_at_2000_lines_by_default(registry, ctx, project):
    (project / "big.txt").write_text("".join(f"{n}\n" for n in range(1, 2501)), encoding="utf-8")
    lines = call(registry, ctx, "read_file", file_path="big.txt").output.splitlines()
    assert lines[1999] == "  2000\t2000"
    assert lines[2000] == "[Showing lines 1-2000 of 2500. Call read_file again with offset=2001 to continue.]"


def test_read_file_cuts_long_lines(registry, ctx, project):
    (project / "wide.txt").write_text("x" * 2500 + "\nshort\n", encoding="utf-8")
    first, second = call(registry, ctx, "read_file", file_path="wide.txt").output.splitlines()
    assert first.startswith("     1\t" + "x" * 2000 + " … [line truncated: 500 more characters]")
    assert second == "     2\tshort"


def test_read_file_tolerates_bom_and_crlf(registry, ctx, project):
    (project / "bom.txt").write_bytes(b"\xef\xbb\xbfhello\r\nworld\r\n")
    assert call(registry, ctx, "read_file", file_path="bom.txt").output == "     1\thello\n     2\tworld"


def test_read_file_reads_utf16_with_bom(registry, ctx, project):
    (project / "ps.txt").write_bytes("你好\r\nworld\r\n".encode("utf-16"))
    assert call(registry, ctx, "read_file", file_path="ps.txt").output == "     1\t你好\n     2\tworld"


def test_read_file_lossy_decode_says_so(registry, ctx, project):
    (project / "latin.txt").write_bytes(b"caf\xe9\n")
    result = call(registry, ctx, "read_file", file_path="latin.txt")
    assert not result.is_error
    assert "not valid UTF-8" in result.output and "caf\ufffd" in result.output


def test_read_file_chinese_name_and_content(registry, ctx, project):
    (project / "文档").mkdir()
    (project / "文档" / "说明.txt").write_text("你好\n世界\n", encoding="utf-8")
    assert call(registry, ctx, "read_file", file_path="文档/说明.txt").output == "     1\t你好\n     2\t世界"


def test_read_file_refuses_a_folder(registry, ctx, project):
    result = call(registry, ctx, "read_file", file_path=".")
    assert result.is_error and "list_dir" in result.output


def test_read_file_suggests_similar_names(registry, ctx, project):
    (project / "notes.txt").write_text("x", encoding="utf-8")
    result = call(registry, ctx, "read_file", file_path="note.txt")
    assert result.is_error and "not found" in result.output and "notes.txt" in result.output
    gone = call(registry, ctx, "read_file", file_path="nofolder/x.txt")
    assert gone.is_error and "does not exist" in gone.output


def test_read_file_refuses_binary(registry, ctx, project):
    (project / "b.bin").write_bytes(b"abc\0def")
    result = call(registry, ctx, "read_file", file_path="b.bin")
    assert result.is_error and "binary" in result.output


def test_read_file_empty(registry, ctx, project):
    (project / "e.txt").write_bytes(b"")
    result = call(registry, ctx, "read_file", file_path="e.txt")
    assert not result.is_error and "empty" in result.output


def test_read_file_offset_past_the_end(registry, ctx, project):
    (project / "a.txt").write_text("one\ntwo\n", encoding="utf-8")
    result = call(registry, ctx, "read_file", file_path="a.txt", offset=9)
    assert result.is_error and "past the end" in result.output and "2 lines" in result.output


def test_read_file_over_5_mb_needs_a_window(registry, ctx, project):
    rows = "".join(f"row {n:07d} " + "x" * 80 + "\n" for n in range(1, 60_000))
    (project / "huge.txt").write_text(rows, encoding="utf-8")
    assert (project / "huge.txt").stat().st_size > 5 * 1024 * 1024
    refused = call(registry, ctx, "read_file", file_path="huge.txt")
    assert refused.is_error and "offset and limit" in refused.output and "5.0 MB" in refused.output
    window = call(registry, ctx, "read_file", file_path="huge.txt", offset=5, limit=2)
    assert not window.is_error
    lines = window.output.splitlines()
    assert lines[0].startswith("     5\trow 0000005 ") and lines[1].startswith("     6\trow 0000006 ")
    assert "offset=7" in lines[2] and "more lines" in lines[2]
    deep = call(registry, ctx, "read_file", file_path="huge.txt", offset=59_999, limit=50)
    assert deep.output.startswith(" 59999\trow 0059999 ") and "offset=" not in deep.output
    beyond = call(registry, ctx, "read_file", file_path="huge.txt", offset=70_000, limit=5)
    assert beyond.is_error and "past the end" in beyond.output


def test_read_file_big_binary_window_is_refused(registry, ctx, project):
    (project / "big.bin").write_bytes(b"\0" * (5 * 1024 * 1024 + 10))
    result = call(registry, ctx, "read_file", file_path="big.bin", offset=1, limit=2)
    assert result.is_error and "binary" in result.output


# --------------------------------------------------------------------------- list_dir


def test_list_dir_sorts_folders_first_and_marks_sizes(registry, ctx, project):
    (project / "b_dir").mkdir()
    (project / "A_dir").mkdir()
    (project / "z.txt").write_text("12345", encoding="utf-8")
    (project / "a.txt").write_bytes(b"")
    (project / "big.bin").write_bytes(b"x" * 2048)
    result = call(registry, ctx, "list_dir", directory_path=".")
    lines = result.output.splitlines()
    assert lines[0] == f"{project.resolve()} (5 entries)"
    assert lines[1:] == ["A_dir/", "b_dir/", "a.txt (0 B)", "big.bin (2.0 KB)", "z.txt (5 B)"]


def test_list_dir_chinese_names(registry, ctx, project):
    (project / "文件夹").mkdir()
    (project / "说明.md").write_text("你好", encoding="utf-8")
    assert call(registry, ctx, "list_dir", directory_path=".").output.splitlines()[1:] == ["文件夹/", "说明.md (6 B)"]


def test_list_dir_empty_missing_and_file(registry, ctx, project):
    (project / "empty").mkdir()
    (project / "f.txt").write_text("x", encoding="utf-8")
    assert "(empty folder)" in call(registry, ctx, "list_dir", directory_path="empty").output
    missing = call(registry, ctx, "list_dir", directory_path="emtpy")
    assert missing.is_error and "empty" in missing.output
    file = call(registry, ctx, "list_dir", directory_path="f.txt")
    assert file.is_error and "read_file" in file.output


def test_list_dir_caps_at_500_entries(registry, ctx, project):
    for number in range(505):
        (project / f"f{number:03}.txt").write_bytes(b"")
    lines = call(registry, ctx, "list_dir", directory_path=".").output.splitlines()
    assert lines[0].endswith("(505 entries)")
    assert len(lines) == 1 + 500 + 1
    assert lines[-1].startswith("[Showing the first 500 of 505 entries")


# --------------------------------------------------------------------------- glob


def test_glob_finds_files_newest_first(registry, ctx, project):
    (project / "sub" / "deep").mkdir(parents=True)
    for rel, stamp in (("a.py", 1000), ("sub/b.py", 3000), ("sub/deep/c.py", 2000), ("sub/readme.md", 4000)):
        (project / rel).write_text("x", encoding="utf-8")
        os.utime(project / rel, (stamp, stamp))
    result = call(registry, ctx, "glob", patterns=["**/*.py"])
    assert result.output.splitlines() == ["sub/b.py", "sub/deep/c.py", "a.py"]
    assert call(registry, ctx, "glob", patterns=["*.py"]).output == "a.py"
    assert call(registry, ctx, "glob", patterns=["sub/*.py", "*.md", "sub/*.md"]).output.splitlines() == ["sub/readme.md", "sub/b.py"]


def test_glob_braces_and_backslashes(registry, ctx, project):
    (project / "src").mkdir()
    for name in ("a.ts", "b.tsx", "c.js"):
        (project / "src" / name).write_text("x", encoding="utf-8")
    found = set(call(registry, ctx, "glob", patterns=["src/*.{ts,tsx}"]).output.splitlines())
    assert found == {"src/a.ts", "src/b.tsx"}
    assert set(call(registry, ctx, "glob", patterns=["src\\*.ts"]).output.splitlines()) == {"src/a.ts"}


def test_glob_folder_is_given_and_results_are_relative_to_it(registry, ctx, project):
    (project / "src").mkdir()
    (project / "src" / "a.py").write_text("x", encoding="utf-8")
    result = call(registry, ctx, "glob", patterns=["*.py"], folder="src")
    lines = result.output.splitlines()
    assert lines == [f"Paths are relative to {(project / 'src').resolve()}", "a.py"]


def test_glob_skips_noise_folders_unless_named(registry, ctx, project):
    for folder in (".git", "node_modules/pkg", "__pycache__", ".venv", "venv", ".mypy_cache", ".pytest_cache", "dist", "build", "src"):
        (project / folder).mkdir(parents=True)
        (project / folder / "m.py").write_text("x", encoding="utf-8")
    found = call(registry, ctx, "glob", patterns=["**/*.py"]).output.splitlines()
    assert found == ["src/m.py"]
    named = call(registry, ctx, "glob", patterns=["node_modules/**/*.py"]).output.splitlines()
    assert named == ["node_modules/pkg/m.py"]
    assert call(registry, ctx, "glob", patterns=["dist/*.py"]).output == "dist/m.py"
    top = call(registry, ctx, "glob", patterns=["*"]).output.splitlines()
    assert top == ["src/"]


def test_glob_caps_at_200_results(registry, ctx, project):
    for number in range(205):
        path = project / f"f{number:03}.txt"
        path.write_bytes(b"")
        os.utime(path, (number + 1, number + 1))
    lines = call(registry, ctx, "glob", patterns=["*.txt"]).output.splitlines()
    assert lines[0] == "f204.txt" and lines[199] == "f005.txt"
    assert len(lines) == 201 and lines[-1].startswith("[Showing the newest 200 of 205 matches")


def test_glob_no_match_and_bad_patterns(registry, ctx, project):
    assert call(registry, ctx, "glob", patterns=["*.nothing"]).output == "No files matched."
    for bad in ("", "/etc/*", "C:/x/*", "./"):
        result = call(registry, ctx, "glob", patterns=[bad])
        assert result.is_error, bad
    missing = call(registry, ctx, "glob", patterns=["*"], folder="nope")
    assert missing.is_error and "not found" in missing.output


def test_glob_chinese_names_and_folders_in_results(registry, ctx, project):
    (project / "文档").mkdir()
    (project / "文档" / "说明.md").write_text("你好", encoding="utf-8")
    assert call(registry, ctx, "glob", patterns=["**/*.md"]).output == "文档/说明.md"
    assert call(registry, ctx, "glob", patterns=["文*"]).output == "文档/"


# --------------------------------------------------------------------------- grep


@pytest.fixture
def tree(project: Path) -> Path:
    (project / "sub").mkdir()
    (project / "a.py").write_text("import os\nTODO fix\nprint('x')\n", encoding="utf-8")
    (project / "sub" / "b.py").write_text("todo lower\nnothing\n", encoding="utf-8")
    (project / "sub" / "c.txt").write_text("TODO in text\n", encoding="utf-8")
    return project


def test_grep_lists_matching_files(registry, ctx, tree):
    assert call(registry, ctx, "grep", pattern="TODO").output.splitlines() == ["a.py", "sub/c.txt"]
    assert call(registry, ctx, "grep", pattern="nomatch").output == "No matches found."


def test_grep_case_insensitive_and_glob_pattern(registry, ctx, tree):
    assert call(registry, ctx, "grep", pattern="todo", case_insensitive=True).output.splitlines() == ["a.py", "sub/b.py", "sub/c.txt"]
    assert call(registry, ctx, "grep", pattern="TODO", glob_pattern="*.py").output == "a.py"
    assert call(registry, ctx, "grep", pattern="todo", case_insensitive=True, glob_pattern="sub/*.py").output == "sub/b.py"
    assert call(registry, ctx, "grep", pattern="TODO", glob_pattern="*.{py,txt}").output.splitlines() == ["a.py", "sub/c.txt"]
    assert call(registry, ctx, "grep", pattern="TODO", glob_pattern="**/*.txt").output == "sub/c.txt"


def test_grep_content_mode_and_context(registry, ctx, project):
    (project / "f.txt").write_text("".join(f"l{n}\n" for n in range(1, 10)), encoding="utf-8")
    plain = call(registry, ctx, "grep", pattern="l4|l8", output_mode="content")
    assert plain.output.splitlines() == ["f.txt:4:l4", "f.txt:8:l8"]
    near = call(registry, ctx, "grep", pattern="l4", output_mode="content", context=1)
    assert near.output.splitlines() == ["f.txt-3-l3", "f.txt:4:l4", "f.txt-5-l5"]
    apart = call(registry, ctx, "grep", pattern="l2|l8", output_mode="content", context=1)
    assert apart.output.splitlines() == ["f.txt-1-l1", "f.txt:2:l2", "f.txt-3-l3", "--", "f.txt-7-l7", "f.txt:8:l8", "f.txt-9-l9"]


def test_grep_path_can_be_a_file_or_folder(registry, ctx, tree):
    assert call(registry, ctx, "grep", pattern="TODO", path="sub").output == "sub/c.txt"
    assert call(registry, ctx, "grep", pattern="todo", path="sub/b.py", output_mode="content").output == "sub/b.py:1:todo lower"
    assert call(registry, ctx, "grep", pattern="x", path="nope").is_error


def test_grep_invalid_regex_is_an_error_message(registry, ctx, tree):
    result = call(registry, ctx, "grep", pattern="(unclosed")
    assert result.is_error and "Invalid regular expression" in result.output and "re" in result.output


def test_grep_skips_binary_big_and_noise_folders(registry, ctx, project):
    (project / "bin.dat").write_bytes(b"NEEDLE\0\x01\x02")
    (project / "huge.txt").write_text("NEEDLE\n" + "x" * (2 * 1024 * 1024 + 10), encoding="utf-8")
    (project / "node_modules").mkdir()
    (project / "node_modules" / "m.js").write_text("NEEDLE", encoding="utf-8")
    (project / "ok.txt").write_text("NEEDLE", encoding="utf-8")
    assert call(registry, ctx, "grep", pattern="NEEDLE").output == "ok.txt"
    assert call(registry, ctx, "grep", pattern="NEEDLE", path="node_modules").output == "node_modules/m.js"
    assert call(registry, ctx, "grep", pattern="NEEDLE", glob_pattern="node_modules/*.js").output == "node_modules/m.js"


def test_grep_head_limit(registry, ctx, project):
    (project / "f.txt").write_text("hit\n" * 10, encoding="utf-8")
    for name in "abcde":
        (project / f"{name}.txt").write_text("hit", encoding="utf-8")
    content = call(registry, ctx, "grep", pattern="hit", path="f.txt", output_mode="content", head_limit=3).output.splitlines()
    assert content[:3] == ["f.txt:1:hit", "f.txt:2:hit", "f.txt:3:hit"]
    assert content[3].startswith("[Output limited to 3 lines")
    files = call(registry, ctx, "grep", pattern="hit", head_limit=2).output.splitlines()
    assert len(files) == 3 and files[2].startswith("[Output limited to 2 files")
    exact = call(registry, ctx, "grep", pattern="hit", path="f.txt", output_mode="content", head_limit=10).output.splitlines()
    assert len(exact) == 10 and "limited" not in exact[-1]


def test_grep_chinese_content_names_and_utf16(registry, ctx, project):
    (project / "文档").mkdir()
    (project / "文档" / "说明.txt").write_text("第一行\n关键字：你好\n", encoding="utf-8")
    (project / "ps.txt").write_bytes("关键字 in utf16\r\n".encode("utf-16"))
    result = call(registry, ctx, "grep", pattern="关键字", output_mode="content")
    assert result.output.splitlines() == ["ps.txt:1:关键字 in utf16", "文档/说明.txt:2:关键字：你好"]


def test_grep_long_lines_are_cut(registry, ctx, project):
    (project / "min.js").write_text("a" * 3000 + " NEEDLE\n", encoding="utf-8")
    line = call(registry, ctx, "grep", pattern="NEEDLE", output_mode="content").output
    assert len(line) < 700 and "more characters" in line


# --------------------------------------------------------------------------- create_file


def test_create_file_writes_utf8_without_bom_and_makes_parents(registry, ctx, project):
    result = call(registry, ctx, "create_file", file_path="new/dir/文件.txt", content="你好\r\n世界\n")
    assert not result.is_error and "Created" in result.output and "2 lines" in result.output
    data = (project / "new" / "dir" / "文件.txt").read_bytes()
    assert data == "你好\r\n世界\n".encode("utf-8") and not data.startswith(b"\xef\xbb\xbf")
    (diff,) = result.diffs
    assert diff.is_new_file and diff.added == 2 and diff.removed == 0
    assert diff.path == str((project / "new" / "dir" / "文件.txt").resolve())


def test_create_file_overwrites_and_diffs(registry, ctx, project):
    (project / "a.txt").write_text("one\ntwo\nthree\n", encoding="utf-8")
    assert not call(registry, ctx, "read_file", file_path="a.txt").is_error
    result = call(registry, ctx, "create_file", file_path="a.txt", content="one\nTWO\nthree\nfour\n")
    assert "Overwrote" in result.output
    (diff,) = result.diffs
    assert not diff.is_new_file and diff.added == 2 and diff.removed == 1
    assert (project / "a.txt").read_text(encoding="utf-8") == "one\nTWO\nthree\nfour\n"


def test_create_file_can_write_an_empty_file_and_refuses_folders(registry, ctx, project):
    assert not call(registry, ctx, "create_file", file_path="empty.txt", content="").is_error
    assert (project / "empty.txt").read_bytes() == b""
    (project / "dir").mkdir()
    result = call(registry, ctx, "create_file", file_path="dir", content="x")
    assert result.is_error and "folder" in result.output


def test_create_file_blocked_by_a_file_in_the_way(registry, ctx, project):
    (project / "afile").write_text("x", encoding="utf-8")
    result = call(registry, ctx, "create_file", file_path="afile/inner.txt", content="x")
    assert result.is_error and "Cannot write" in result.output


def test_create_file_rejects_unencodable_text(registry, ctx, project):
    result = call(registry, ctx, "create_file", file_path="bad.txt", content="a\ud800b")
    assert result.is_error and "UTF-8" in result.output
    assert not (project / "bad.txt").exists()


# --------------------------------------------------------------------------- edit_file


def edit(registry, ctx, path="f.txt", old="", new="", **extra) -> ToolResult:
    return call(registry, ctx, "edit_file", file_path=path, old_str=old, new_str=new, **extra)


def test_edit_replaces_one_occurrence(registry, ctx, project):
    (project / "f.txt").write_text("alpha\nbeta\ngamma\n", encoding="utf-8")
    result = edit(registry, ctx, old="beta", new="BETA")
    assert not result.is_error and "replaced 1 occurrence " in result.output
    assert (project / "f.txt").read_text(encoding="utf-8") == "alpha\nBETA\ngamma\n"
    (diff,) = result.diffs
    assert (diff.added, diff.removed) == (1, 1)


def test_edit_keeps_crlf(registry, ctx, project):
    (project / "f.txt").write_bytes(b"one\r\ntwo\r\nthree\r\n")
    result = edit(registry, ctx, old="two\nthree", new="2\n3\n4")
    assert not result.is_error
    assert (project / "f.txt").read_bytes() == b"one\r\n2\r\n3\r\n4\r\n"
    crlf_in_args = edit(registry, ctx, old="2\r\n3", new="two")
    assert not crlf_in_args.is_error
    assert (project / "f.txt").read_bytes() == b"one\r\ntwo\r\n4\r\n"


def test_edit_keeps_lf_and_missing_final_newline(registry, ctx, project):
    (project / "f.txt").write_bytes(b"a\nb\nc")
    edit(registry, ctx, old="b", new="B1\nB2")
    assert (project / "f.txt").read_bytes() == b"a\nB1\nB2\nc"


def test_edit_keeps_bom(registry, ctx, project):
    (project / "f.txt").write_bytes(b"\xef\xbb\xbfhello\r\nworld\r\n")
    assert not edit(registry, ctx, old="world", new="世界").is_error
    assert (project / "f.txt").read_bytes() == b"\xef\xbb\xbfhello\r\n" + "世界".encode("utf-8") + b"\r\n"


def test_edit_keeps_utf16(registry, ctx, project):
    (project / "f.txt").write_bytes("你好\r\nworld\r\n".encode("utf-16"))
    assert not edit(registry, ctx, old="world", new="there").is_error
    raw = (project / "f.txt").read_bytes()
    assert raw.startswith(b"\xff\xfe") and raw.decode("utf-16") == "你好\r\nthere\r\n"


def test_edit_notes_mixed_line_endings(registry, ctx, project):
    (project / "f.txt").write_bytes(b"a\r\nb\nc\r\n")
    result = edit(registry, ctx, old="a", new="A")
    assert "mixed line endings" in result.output and "CRLF" in result.output
    assert (project / "f.txt").read_bytes() == b"A\r\nb\r\nc\r\n"


def test_edit_chinese(registry, ctx, project):
    (project / "说明.txt").write_text("第一行\n第二行\n", encoding="utf-8")
    assert not edit(registry, ctx, path="说明.txt", old="第二行", new="第二行（改）").is_error
    assert (project / "说明.txt").read_text(encoding="utf-8") == "第一行\n第二行（改）\n"


def test_edit_deletes_with_empty_new_str(registry, ctx, project):
    (project / "f.txt").write_text("keep\ndrop\nkeep2\n", encoding="utf-8")
    assert not edit(registry, ctx, old="drop\n", new="").is_error
    assert (project / "f.txt").read_text(encoding="utf-8") == "keep\nkeep2\n"


def test_edit_no_match_shows_the_closest_candidate(registry, ctx, project):
    (project / "f.txt").write_text("def foo():\n    return 1\n\nprint('end')\n", encoding="utf-8")
    original = (project / "f.txt").read_bytes()
    second_line = edit(registry, ctx, old="def foo():\n    return 2", new="x")
    assert second_line.is_error and "not found" in second_line.output
    assert "line 2 of old_str differs from line 2 of the file" in second_line.output
    assert "return 2" in second_line.output and "return 1" in second_line.output
    indentation = edit(registry, ctx, old="def foo():\nreturn 1", new="x")
    assert indentation.is_error and "indentation" in indentation.output
    fuzzy = edit(registry, ctx, old="def fooo():", new="x")
    assert fuzzy.is_error and "closest line in the file is line 1" in fuzzy.output
    unrelated = edit(registry, ctx, old="completely different text", new="x")
    assert unrelated.is_error and "read_file" in unrelated.output
    assert (project / "f.txt").read_bytes() == original


def test_edit_many_matches_needs_context_or_change_all(registry, ctx, project):
    (project / "f.txt").write_text("x = 1\ny = 2\nx = 1\nz\nx = 1\n", encoding="utf-8")
    result = edit(registry, ctx, old="x = 1", new="x = 9")
    assert result.is_error and "appears 3 times" in result.output and "lines 1, 3, 5" in result.output
    assert "change_all" in result.output and "surrounding" in result.output
    assert (project / "f.txt").read_text(encoding="utf-8").count("x = 1") == 3
    done = edit(registry, ctx, old="x = 1", new="x = 9", change_all=True)
    assert not done.is_error and "replaced 3 occurrences" in done.output
    assert (project / "f.txt").read_text(encoding="utf-8") == "x = 9\ny = 2\nx = 9\nz\nx = 9\n"


def test_edit_argument_errors(registry, ctx, project):
    (project / "f.txt").write_text("abc\n", encoding="utf-8")
    assert "identical" in edit(registry, ctx, old="abc", new="abc").output
    empty = edit(registry, ctx, old="", new="x")
    assert empty.is_error and "empty" in empty.output and "create_file" in empty.output
    missing = edit(registry, ctx, path="nofile.txt", old="a", new="b")
    assert missing.is_error and "not found" in missing.output and "create_file" in missing.output
    (project / "dir").mkdir()
    assert "folder" in edit(registry, ctx, path="dir", old="a", new="b").output


def test_edit_refuses_binary_and_undecodable_files(registry, ctx, project):
    (project / "b.bin").write_bytes(b"abc\0def")
    assert "binary" in edit(registry, ctx, path="b.bin", old="abc", new="x").output
    (project / "latin.txt").write_bytes(b"caf\xe9 ok\n")
    result = edit(registry, ctx, path="latin.txt", old="ok", new="fine")
    assert result.is_error and "not valid UTF-8" in result.output
    assert (project / "latin.txt").read_bytes() == b"caf\xe9 ok\n"


def test_edit_refuses_files_over_5_mb(registry, ctx, project):
    (project / "huge.txt").write_text("needle\n" + "x" * (5 * 1024 * 1024 + 1), encoding="utf-8")
    result = edit(registry, ctx, path="huge.txt", old="needle", new="y")
    assert result.is_error and "cannot be edited" in result.output


def test_edit_outside_the_project_is_still_a_valid_call(registry, ctx, project, tmp_path):
    other = tmp_path / "other.txt"
    other.write_text("keep", encoding="utf-8")
    assert not edit(registry, ctx, path=str(other), old="keep", new="kept").is_error
    assert other.read_text(encoding="utf-8") == "kept"


def test_preview_diff_matches_what_the_edit_would_do(ctx, project):
    (project / "f.txt").write_text("a\nb\nc\n", encoding="utf-8")
    spec = fs_tools.EDIT_FILE
    diff = fs_tools.preview_diff(ctx, spec, {"file_path": "f.txt", "old_str": "b", "new_str": "B"})
    assert diff is not None and (diff.added, diff.removed) == (1, 1)
    assert fs_tools.preview_diff(ctx, spec, {"file_path": "f.txt", "old_str": "zzz", "new_str": "B"}) is None
    assert fs_tools.preview_diff(ctx, spec, {"file_path": "f.txt"}) is None
    created = fs_tools.preview_diff(ctx, fs_tools.CREATE_FILE, {"file_path": "new.txt", "content": "x\ny\n"})
    assert created is not None and created.is_new_file and created.added == 2
    again = fs_tools.preview_diff(ctx, fs_tools.CREATE_FILE, {"file_path": "f.txt", "content": "a\nc\n"})
    assert again is not None and not again.is_new_file and again.removed == 1
    assert (project / "f.txt").read_text(encoding="utf-8") == "a\nb\nc\n" and not (project / "new.txt").exists()


# --------------------------------------------------------------------------- helpers


def test_expand_braces():
    assert fs_tools.expand_braces("*.py") == ["*.py"]
    assert fs_tools.expand_braces("*.{ts,tsx}") == ["*.ts", "*.tsx"]
    assert fs_tools.expand_braces("{a,b}/{c,d}") == ["a/c", "a/d", "b/c", "b/d"]
    assert fs_tools.expand_braces("x.{a,{b,c}}") == ["x.a", "x.b", "x.c"]
    assert fs_tools.expand_braces("odd{brace") == ["odd{brace"]
