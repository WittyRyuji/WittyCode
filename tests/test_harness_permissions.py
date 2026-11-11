"""Permission levels, the read-only shell allowlist, protected paths, remember keys and the approval card."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from desktop.core.harness import fs_tools
from desktop.core.harness.permissions import (
    LEVELS,
    Decision,
    action_view,
    decide,
    is_readonly_command,
    permission_options,
    remember_key,
)
from desktop.core.harness.shell import SHELL
from desktop.core.harness.tools import ToolContext, ToolRegistry, ToolSpec
from desktop.core.prompts import ActionView

READ_FILE, LIST_DIR, GLOB, GREP = fs_tools.READ_FILE, fs_tools.LIST_DIR, fs_tools.GLOB, fs_tools.GREP
CREATE_FILE, EDIT_FILE = fs_tools.CREATE_FILE, fs_tools.EDIT_FILE


@pytest.fixture
def project(tmp_path: Path) -> Path:
    folder = tmp_path / "proj"
    (folder / "src").mkdir(parents=True)
    (folder / "a.txt").write_text("alpha\nbeta\n", encoding="utf-8")
    return folder


@pytest.fixture
def outside(tmp_path: Path) -> Path:
    folder = tmp_path / "other"
    folder.mkdir()
    (folder / "x.txt").write_text("outside", encoding="utf-8")
    return folder


@pytest.fixture
def ctx(project: Path) -> ToolContext:
    return ToolContext(project=project)


def verdict(level, spec, args, ctx, remembered=frozenset()) -> str:
    return decide(level, spec, args, ctx, remembered=set(remembered)).verdict


def make_dir_link(link: Path, target: Path) -> None:
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


# --------------------------------------------------------------------------- the matrix


def test_levels():
    assert LEVELS == ("ask", "code", "full")
    assert Decision("allow", "r") == Decision("allow", "r")


def read_cases(outside: Path):
    return [
        (READ_FILE, {"file_path": "a.txt"}, {"file_path": str(outside / "x.txt")}),
        (LIST_DIR, {"directory_path": "src"}, {"directory_path": str(outside)}),
        (GLOB, {"patterns": ["**/*.py"]}, {"patterns": ["*.txt"], "folder": str(outside)}),
        (GLOB, {"patterns": ["src/*.py"]}, {"patterns": ["../other/*.txt"]}),
        (GLOB, {"patterns": ["*.py"]}, {"patterns": [str(outside / "*.txt")]}),
        (GREP, {"pattern": "alpha"}, {"pattern": "outside", "path": str(outside)}),
        (GREP, {"pattern": "alpha", "path": "a.txt"}, {"pattern": "outside", "path": "../other/x.txt"}),
    ]


@pytest.mark.parametrize("index", range(7))
def test_reads_inside_are_allowed_everywhere_and_outside_ask_until_full(ctx, outside, index):
    spec, inside_args, outside_args = read_cases(outside)[index]
    for level in LEVELS:
        assert verdict(level, spec, inside_args, ctx) == "allow", (spec.name, level)
    assert verdict("ask", spec, outside_args, ctx) == "ask"
    assert verdict("code", spec, outside_args, ctx) == "ask"
    assert verdict("full", spec, outside_args, ctx) == "allow"


@pytest.mark.parametrize(
    "spec, inside_args, outside_args",
    [
        (CREATE_FILE, {"file_path": "new.txt", "content": "x"}, "file_path"),
        (CREATE_FILE, {"file_path": "src/deep/new.txt", "content": "x"}, "file_path"),
        (EDIT_FILE, {"file_path": "a.txt", "old_str": "alpha", "new_str": "A"}, "file_path"),
    ],
)
def test_writes_inside_ask_at_ask_allow_at_code_and_full(ctx, outside, spec, inside_args, outside_args):
    assert verdict("ask", spec, inside_args, ctx) == "ask"
    assert verdict("code", spec, inside_args, ctx) == "allow"
    assert verdict("full", spec, inside_args, ctx) == "allow"
    elsewhere = {**inside_args, outside_args: str(outside / "x.txt")}
    assert verdict("ask", spec, elsewhere, ctx) == "ask"
    assert verdict("code", spec, elsewhere, ctx) == "ask"
    assert verdict("full", spec, elsewhere, ctx) == "allow"
    escaping = {**inside_args, outside_args: "../other/x.txt"}
    assert verdict("code", spec, escaping, ctx) == "ask"


def test_writes_through_dotdot_that_stay_inside_are_inside(ctx):
    args = {"file_path": "src/../new.txt", "content": "x"}
    assert verdict("code", CREATE_FILE, args, ctx) == "allow"


def test_shell_asks_at_ask_and_full_allows(ctx):
    for command in ("Remove-Item -Recurse .", "git status", "dotnet build"):
        assert verdict("ask", SHELL, {"command": command}, ctx) == "ask"
        assert verdict("full", SHELL, {"command": command}, ctx) == "allow"
    assert verdict("code", SHELL, {"command": "dotnet build"}, ctx) == "ask"
    assert verdict("code", SHELL, {"command": "git status"}, ctx) == "allow"


def test_shell_cwd_outside_the_project_asks_even_for_a_read_only_command(ctx, outside, project):
    assert verdict("code", SHELL, {"command": "git status", "cwd": "src"}, ctx) == "allow"
    assert verdict("code", SHELL, {"command": "git status", "cwd": str(outside)}, ctx) == "ask"
    assert verdict("code", SHELL, {"command": "git status", "cwd": ".."}, ctx) == "ask"
    assert verdict("full", SHELL, {"command": "git status", "cwd": str(outside)}, ctx) == "allow"


def test_unknown_level_is_treated_as_ask(ctx):
    assert verdict("bogus", CREATE_FILE, {"file_path": "n.txt", "content": "x"}, ctx) == "ask"
    assert verdict("bogus", READ_FILE, {"file_path": "a.txt"}, ctx) == "allow"
    assert verdict("bogus", SHELL, {"command": "git status"}, ctx) == "ask"


def test_malformed_arguments_do_not_raise_and_never_allow_a_write(ctx):
    for args in (None, "x", [], {}, {"file_path": 5}, {"file_path": ""}, {"file_path": "a\0b", "content": "x"}):
        assert verdict("code", CREATE_FILE, args, ctx) == "ask"
        assert verdict("ask", READ_FILE, args, ctx) in ("allow", "ask")
        assert verdict("code", SHELL, args, ctx) == "ask"
        action_view("id", CREATE_FILE, args, ctx)
        action_view("id", EDIT_FILE, args, ctx)
        action_view("id", SHELL, args, ctx)
        remember_key(CREATE_FILE, args, ctx)
    assert verdict("full", CREATE_FILE, {}, ctx) == "allow"


def test_a_tool_the_module_does_not_know(ctx, outside):
    mcp_read = ToolSpec("memory_search", "d", {"type": "object", "properties": {}}, "memory_search", "read")
    mcp_edit = ToolSpec("memory_write", "d", {"type": "object", "properties": {}}, "memory_write", "edit")
    assert verdict("code", mcp_edit, {"title": "x"}, ctx) == "ask"
    assert verdict("code", mcp_read, {"query": "x"}, ctx) == "ask"
    with_path = ToolSpec("custom_read", "d", {"type": "object", "properties": {}}, "custom", "read")
    assert verdict("code", with_path, {"path": "a.txt"}, ctx) == "allow"
    assert verdict("code", with_path, {"path": str(outside)}, ctx) == "ask"
    assert verdict("full", mcp_edit, {"title": "x"}, ctx) == "allow"


@pytest.mark.skipif(sys.platform != "win32", reason="Windows path semantics")
def test_case_insensitive_paths_are_inside_on_windows(ctx, project):
    shouted = {"file_path": str(project).swapcase() + "\\SRC\\New.TXT", "content": "x"}
    assert verdict("code", CREATE_FILE, shouted, ctx) == "allow"


def test_a_link_out_of_the_project_is_outside(ctx, project, outside):
    make_dir_link(project / "portal", outside)
    args = {"file_path": "portal/x.txt", "content": "x"}
    assert verdict("code", CREATE_FILE, args, ctx) == "ask"
    assert verdict("code", READ_FILE, {"file_path": "portal/x.txt"}, ctx) == "ask"
    assert verdict("code", LIST_DIR, {"directory_path": "portal"}, ctx) == "ask"


# --------------------------------------------------------------------------- protected


@pytest.fixture
def guarded(project: Path, tmp_path: Path):
    secret = tmp_path / "WittyCode" / "chatgpt"
    secret.mkdir(parents=True)
    (secret / "acct.json").write_text("{}", encoding="utf-8")
    return ToolContext(project=project, protected=(secret,)), secret


def protected_calls(secret: Path):
    return [
        (READ_FILE, {"file_path": str(secret / "acct.json")}),
        (READ_FILE, {"file_path": str(secret / ".." / "chatgpt" / "acct.json")}),
        (LIST_DIR, {"directory_path": str(secret)}),
        (GLOB, {"patterns": ["*"], "folder": str(secret)}),
        (GLOB, {"patterns": [str(secret / "*.json")]}),
        (GREP, {"pattern": "x", "path": str(secret)}),
        (CREATE_FILE, {"file_path": str(secret / "new.json"), "content": "x"}),
        (EDIT_FILE, {"file_path": str(secret / "acct.json"), "old_str": "{", "new_str": "["}),
        (SHELL, {"command": f"Get-Content '{secret}\\acct.json'"}),
        (SHELL, {"command": f"Get-Content {str(secret).replace(chr(92), '/')}/acct.json"}),
        (SHELL, {"command": f"type {str(secret).upper()}"}),
        (SHELL, {"command": "git status", "cwd": str(secret)}),
    ]


@pytest.mark.parametrize("index", range(12))
@pytest.mark.parametrize("level", LEVELS)
def test_protected_paths_are_denied_at_every_level(guarded, level, index):
    context, secret = guarded
    spec, args = protected_calls(secret)[index]
    decision = decide(level, spec, args, context, remembered=set())
    assert decision.verdict == "deny", (spec.name, level, args)
    assert "protected" in decision.reason and "not" in decision.reason
    # Not even a remembered approval of the very same kind of call changes that.
    remembered = {remember_key(spec, args, context), "edit", "read", "exec:get-content", "exec:git", "exec:type"}
    assert decide(level, spec, args, context, remembered=remembered).verdict == "deny"


def test_protected_environment_variable_forms_are_denied(guarded, monkeypatch):
    context, secret = guarded
    monkeypatch.setenv("WC_TEST_APP", str(secret.parent))
    for command in ("type $env:WC_TEST_APP\\chatgpt\\acct.json", "type %WC_TEST_APP%\\chatgpt\\acct.json"):
        assert verdict("full", SHELL, {"command": command}, context) == "deny"
    assert verdict("full", SHELL, {"command": "type C:\\somewhere\\else.txt"}, context) == "allow"


def test_a_link_into_a_protected_folder_is_denied(guarded, project):
    context, secret = guarded
    make_dir_link(project / "sneaky", secret)
    for level in LEVELS:
        assert verdict(level, READ_FILE, {"file_path": "sneaky/acct.json"}, context) == "deny"
        assert verdict(level, CREATE_FILE, {"file_path": "sneaky/n.json", "content": "x"}, context) == "deny"


def test_nearby_paths_are_not_protected(guarded, tmp_path):
    context, secret = guarded
    sibling = secret.parent / "chatgpt-notes"
    sibling.mkdir()
    assert verdict("full", READ_FILE, {"file_path": str(sibling / "n.txt")}, context) == "allow"
    assert verdict("full", SHELL, {"command": f"type {sibling}\\n.txt"}, context) == "allow"
    assert verdict("code", READ_FILE, {"file_path": str(secret.parent)}, context) == "ask"


@pytest.mark.skipif(sys.platform != "win32", reason="Windows path semantics")
def test_protected_check_ignores_case_on_windows(guarded):
    context, secret = guarded
    args = {"file_path": str(secret).swapcase() + "\\ACCT.json"}
    assert verdict("full", READ_FILE, args, context) == "deny"


# --------------------------------------------------------------------------- remember keys


def test_remember_key_for_shell(ctx):
    key = lambda command: remember_key(SHELL, {"command": command}, ctx)  # noqa: E731
    assert key("git status") == "exec:git"
    assert key("GIT.EXE push origin") == "exec:git"
    assert key("  Remove-Item -Recurse x") == "exec:remove-item"
    assert key("C:\\Program Files\\Git\\bin\\git.exe status".replace("C:\\Program Files\\Git\\bin\\", "")) == "exec:git"
    assert key("& 'C:\\Program Files\\Git\\bin\\git.exe' status") == "exec:git"
    assert key('"C:\\tools\\Build.CMD" all') == "exec:build"
    assert key("./scripts/run.ps1 -x") == "exec:run"
    assert key("python -m pytest") == "exec:python"
    assert key("") == "exec:"


def test_remember_key_for_writes(ctx, project, outside):
    assert remember_key(CREATE_FILE, {"file_path": "new.txt", "content": "x"}, ctx) == "edit"
    assert remember_key(EDIT_FILE, {"file_path": "src/a.py", "old_str": "a", "new_str": "b"}, ctx) == "edit"
    key = remember_key(EDIT_FILE, {"file_path": str(outside / "x.txt"), "old_str": "a", "new_str": "b"}, ctx)
    assert key == "edit:" + str(outside).lower()
    again = remember_key(CREATE_FILE, {"file_path": str(outside / "other-name.txt"), "content": "x"}, ctx)
    assert again == key
    deeper = remember_key(CREATE_FILE, {"file_path": str(outside / "sub" / "n.txt"), "content": "x"}, ctx)
    assert deeper == "edit:" + str(outside / "sub").lower()


def test_remember_key_for_reads(ctx, project, outside):
    assert remember_key(READ_FILE, {"file_path": "a.txt"}, ctx) == "read"
    assert remember_key(READ_FILE, {"file_path": str(outside / "x.txt")}, ctx) == "read:" + str(outside).lower()
    assert remember_key(LIST_DIR, {"directory_path": str(outside)}, ctx) == "read:" + str(outside).lower()
    assert remember_key(GREP, {"pattern": "x", "path": str(outside)}, ctx) == "read:" + str(outside).lower()
    assert remember_key(GLOB, {"patterns": ["*"], "folder": str(outside)}, ctx) == "read:" + str(outside).lower()


def test_remembered_keys_turn_an_ask_into_an_allow(ctx, outside):
    command = {"command": "dotnet build"}
    assert verdict("code", SHELL, command, ctx) == "ask"
    assert verdict("code", SHELL, command, ctx, {"exec:dotnet"}) == "allow"
    assert verdict("code", SHELL, {"command": "dotnet test"}, ctx, {"exec:dotnet"}) == "allow"
    assert verdict("code", SHELL, {"command": "npm test"}, ctx, {"exec:dotnet"}) == "ask"
    create = {"file_path": "n.txt", "content": "x"}
    assert verdict("ask", CREATE_FILE, create, ctx) == "ask"
    assert verdict("ask", CREATE_FILE, create, ctx, {"edit"}) == "allow"
    assert verdict("ask", EDIT_FILE, {"file_path": "a.txt", "old_str": "alpha", "new_str": "b"}, ctx, {"edit"}) == "allow"
    away = {"file_path": str(outside / "n.txt"), "content": "x"}
    assert verdict("code", CREATE_FILE, away, ctx, {"edit"}) == "ask"
    assert verdict("code", CREATE_FILE, away, ctx, {"edit:" + str(outside).lower()}) == "allow"
    read = {"file_path": str(outside / "x.txt")}
    assert verdict("code", READ_FILE, read, ctx, {"read:" + str(outside).lower()}) == "allow"
    assert verdict("code", READ_FILE, read, ctx, {"read"}) == "ask"


def test_a_remembered_program_does_not_cover_chained_commands(ctx):
    remembered = {"exec:dotnet"}
    for command in (
        "dotnet build; Remove-Item -Recurse .",
        "dotnet build && del x",
        "dotnet build | Out-File x.txt",
        "dotnet build > out.txt",
        "dotnet build\nRemove-Item x",
        "dotnet $(Remove-Item x)",
        "dotnet `whoami`",
        'dotnet "$(Remove-Item x)"',
        "dotnet 'unterminated",
    ):
        assert verdict("code", SHELL, {"command": command}, ctx, remembered) == "ask", command
    assert verdict("code", SHELL, {"command": 'dotnet build -p:Note="a;b"'}, ctx, remembered) == "allow"


def test_a_remembered_key_does_not_widen_full_or_read_only_levels(ctx):
    assert verdict("full", SHELL, {"command": "x"}, ctx, {"exec:other"}) == "allow"
    assert decide("code", SHELL, {"command": "git status"}, ctx, remembered={"exec:git"}).reason != ""


# --------------------------------------------------------------------------- the read-only allowlist

READ_ONLY = [
    "git status",
    "GIT STATUS",
    "git.exe status",
    "git diff",
    "git diff --stat HEAD~1",
    "git diff HEAD^ -- src/a.py",
    "git log --oneline -5",
    "git log origin/main..HEAD",
    "git log --format=%h",
    "git show HEAD",
    "git show HEAD:src/a.py",
    "git branch",
    "git branch -a",
    "git branch --show-current",
    "git rev-parse HEAD",
    "git ls-files",
    "git blame a.txt",
    "git remote -v",
    "dir",
    "ls",
    "ls src",
    "ls ./src",
    "Get-ChildItem -Recurse -Filter *.py",
    "gci",
    "cat a.txt",
    "cat 'a b.txt'",
    "type a.txt",
    "Get-Content a.txt -TotalCount 5",
    "gc a.txt",
    "pwd",
    "Get-Location",
    "echo hi",
    "Write-Output hi",
    "where git",
    "Get-Command git",
    "rg foo",
    'rg "a|b" src',
    'rg "a;b" src',
    "rg -n --glob *.py foo src",
    "findstr /i foo *.py",
    'findstr /c:"foo bar" a.txt',
    "Select-String foo a.txt",
    "sls foo a.txt",
    "head -n 5 a.txt",
    "tail a.txt",
    "wc -l a.txt",
    "python --version",
    "node --version",
    "echo '$notexpanded'",
    "git log | Select-String fix",
    "git status | Select-Object -First 5",
    "Get-ChildItem | Sort-Object Length",
    "Get-ChildItem | Measure-Object",
    "cat a.txt | Select-String alpha",
]

NEEDS_ASKING = [
    "git push",
    "git push --force",
    "git commit -m x",
    "git add .",
    "git reset --hard",
    "git checkout .",
    "git stash",
    "git fetch",
    "git branch -D old",
    "git branch newbranch",
    "git branch -m a b",
    "git remote",
    "git remote add origin x",
    "git remote -v add",
    "git -c core.pager=evil status",
    "git diff --output=out.txt",
    "git log --output=out.txt",
    "git diff --ext-diff",
    "git",
    "git status; git push",
    "git status && git push",
    "git status || git push",
    "git status & whoami",
    "echo hi; whoami",
    "pwd;whoami",
    "ls ; whoami",
    "echo hi & whoami",
    "echo hi && whoami",
    "echo hi 2>&1",
    "echo {x}",
    "echo {x",
    "echo hi }",
    "echo (x)",
    "echo (x",
    "echo hi)",
    "echo hi\rwhoami",
    "dir > out.txt",
    "echo hi >> a.txt",
    "cat < a.txt",
    "echo `whoami`",
    "echo $(whoami)",
    "echo ${env:USERNAME}",
    "echo $env:USERNAME",
    'echo "$home"',
    'echo "a`nb"',
    "echo hi\nwhoami",
    "echo hi\r\nwhoami",
    "Get-ChildItem | Remove-Item",
    "dir | iex",
    "iex 'dir'",
    "Invoke-Expression dir",
    "echo Remove-Item",
    "Remove-Item a.txt",
    "Set-Content a.txt x",
    "Add-Content a.txt x",
    "Out-File x.txt",
    "New-Item x",
    "Start-Process notepad",
    "python script.py",
    'python -c "print(1)"',
    "python --version --help",
    "python --version; whoami",
    "node -e 1",
    "npm test",
    "dotnet build",
    "whoami",
    "git log | whoami",
    "whoami | git log",
    "git log | Select-String x | Select-Object -First 1",
    "dir || dir",
    "Get-ChildItem | ForEach-Object { $_ }",
    "Get-ChildItem | Sort-Object { $_.Length }",
    "echo (Get-Date)",
    "rg --pre cmd foo",
    "rg --pre=cmd foo",
    "rg -z foo",
    "rg --search-zip foo",
    ".\\git status",
    "./git status",
    "C:\\Windows\\System32\\findstr.exe foo a.txt",
    "& git status",
    "echo 'unterminated",
    'echo "unterminated',
    "",
    "   ",
    "Select-String foo a.txt | Out-File x.txt",
    "x" * 2001,
]


@pytest.mark.parametrize("command", READ_ONLY)
def test_read_only_commands_run_at_code_without_asking(ctx, command):
    assert is_readonly_command(command, ctx.project, ctx.project), command
    assert verdict("code", SHELL, {"command": command}, ctx) == "allow"
    assert verdict("ask", SHELL, {"command": command}, ctx) == "ask"


@pytest.mark.parametrize("command", NEEDS_ASKING)
def test_everything_else_asks_at_code(ctx, command):
    assert not is_readonly_command(command, ctx.project, ctx.project), repr(command)
    assert verdict("code", SHELL, {"command": command}, ctx) == "ask"
    assert verdict("full", SHELL, {"command": command}, ctx) == "allow"


def test_arguments_pointing_outside_the_project_ask(ctx, project, outside):
    inside = project / "a.txt"
    assert is_readonly_command(f"cat {inside}", project, project)
    assert is_readonly_command(f"cat '{inside}'", project, project)
    for command in (
        f"cat {outside / 'x.txt'}",
        f'cat "{outside / "x.txt"}"',
        "cat ../other/x.txt",
        "cat ..\\other\\x.txt",
        "cat src/../../other/x.txt",
        "cat C:\\Windows\\win.ini",
        "cat C:win.ini",
        "cat ~/.ssh/id_rsa",
        "cat ~",
        "ls /",
        "ls \\",
        "ls /Windows",
        "Get-Content \\\\server\\share\\f.txt",
        "dir C:\\",
        "Get-ChildItem -Path:C:\\Windows",
        "rg foo --ignore-file=C:\\x",
        "git diff --no-index C:\\a C:\\b",
        "git log -- ../x",
        f"findstr /f:{outside / 'x.txt'} foo",
    ):
        assert not is_readonly_command(command, project, project), command
    assert is_readonly_command("cat src/../a.txt", project, project)
    assert is_readonly_command("git log HEAD..origin/main", project, project)
    assert is_readonly_command("ls -Recurse", project, project)
    assert is_readonly_command("findstr /i foo a.txt", project, project)


def test_a_read_only_command_in_a_folder_outside_asks(project, outside):
    assert not is_readonly_command("git status", outside, project)
    assert not is_readonly_command("dir", project.parent, project)
    assert is_readonly_command("dir", project / "src", project)


def test_pipe_is_allowed_only_once_and_only_between_allowlisted_commands(ctx):
    assert is_readonly_command("git log | Select-String x", ctx.project, ctx.project)
    assert not is_readonly_command("git log |", ctx.project, ctx.project)
    assert not is_readonly_command("| git log", ctx.project, ctx.project)
    assert not is_readonly_command("git log | | Select-String x", ctx.project, ctx.project)
    assert is_readonly_command('rg "a|b|c" src', ctx.project, ctx.project)


# --------------------------------------------------------------------------- the approval card


def test_action_view_for_shell(ctx):
    view = action_view("tool-1", SHELL, {"command": "dotnet build 项目"}, ctx)
    assert isinstance(view, ActionView)
    assert (view.kind, view.title, view.command) == ("execute", "运行命令", "dotnet build 项目")
    assert (view.tool_use_id, view.tool_name) == ("tool-1", "Execute")
    assert view.diffs == () and view.path is None


def test_action_view_for_edit_shows_the_real_diff(ctx, project):
    (project / "src" / "说明.txt").write_text("one\ntwo\nthree\nfour\n", encoding="utf-8")
    args = {"file_path": "src/说明.txt", "old_str": "two\nthree", "new_str": "2\n3\n3.5"}
    view = action_view("t2", EDIT_FILE, args, ctx)
    assert (view.kind, view.title, view.tool_name, view.tool_use_id) == ("edit", "编辑文件 说明.txt", "Edit", "t2")
    assert view.path == str((project / "src" / "说明.txt").resolve())
    (diff,) = view.diffs
    assert (diff.added, diff.removed) == (3, 2)
    assert any(line.kind == "ctx" and line.old == 1 and line.text == "one" for line in diff.lines)
    assert (project / "src" / "说明.txt").read_text(encoding="utf-8") == "one\ntwo\nthree\nfour\n"


def test_action_view_for_edit_falls_back_to_the_fragments(ctx, project):
    broken = action_view("t3", EDIT_FILE, {"file_path": "a.txt", "old_str": "not in the file", "new_str": "x"}, ctx)
    (diff,) = broken.diffs
    assert (diff.added, diff.removed) == (1, 1) and diff.path.endswith("a.txt")
    assert all(line.old is None and line.new is None for line in diff.lines), "fragments have no line numbers"
    missing = action_view("t4", EDIT_FILE, {"file_path": "gone.txt", "old_str": "a", "new_str": "b"}, ctx)
    assert missing.kind == "edit" and missing.title == "编辑文件 gone.txt" and len(missing.diffs) == 1


def test_action_view_for_create(ctx, project):
    new = action_view("t5", CREATE_FILE, {"file_path": "docs/新文件.md", "content": "# 标题\n正文\n"}, ctx)
    assert (new.kind, new.title, new.tool_name) == ("create", "创建文件 新文件.md", "Create")
    assert new.diffs[0].is_new_file and new.diffs[0].added == 2
    over = action_view("t6", CREATE_FILE, {"file_path": "a.txt", "content": "alpha\nGAMMA\n"}, ctx)
    assert over.kind == "create" and not over.diffs[0].is_new_file
    assert (over.diffs[0].added, over.diffs[0].removed) == (1, 1)
    assert not (project / "docs").exists()


def test_action_view_for_reads_is_other(ctx, outside):
    read = action_view("t7", READ_FILE, {"file_path": str(outside / "x.txt")}, ctx)
    assert (read.kind, read.title, read.tool_name) == ("other", "读取文件 x.txt", "Read")
    assert read.path == str((outside / "x.txt").resolve())
    assert action_view("t8", LIST_DIR, {"directory_path": str(outside)}, ctx).title == "列出文件夹 other"
    glob = action_view("t9", GLOB, {"patterns": ["*.py", "*.md"], "folder": str(outside)}, ctx)
    assert glob.kind == "other" and "*.py, *.md" in glob.title and glob.path == str(outside.resolve())
    grep = action_view("t10", GREP, {"pattern": "TODO", "path": str(outside)}, ctx)
    assert grep.title == "搜索 “TODO”" and grep.tool_name == "Grep"
    unknown = ToolSpec("mystery", "d", {"type": "object", "properties": {}}, "Mystery", "read")
    assert action_view("t11", unknown, {}, ctx).title == "使用工具 Mystery"


def test_permission_options():
    options = permission_options(SHELL, {"command": "x"}, ToolContext(Path(".")))
    assert [(option.value, option.tone) for option in options] == [
        ("proceed_once", "primary"),
        ("proceed_always", "normal"),
        ("cancel", "danger"),
    ]
    assert all(option.label for option in options)
    assert "不再询问" in options[1].label


def test_registry_specs_work_with_the_permission_functions(ctx):
    for spec in ToolRegistry.default().specs():
        args = {"file_path": "a.txt", "content": "x", "old_str": "alpha", "new_str": "b", "command": "git status", "pattern": "x",
                "patterns": ["*"], "directory_path": "."}
        assert decide("code", spec, args, ctx, remembered=set()).verdict in ("allow", "ask")
        assert isinstance(remember_key(spec, args, ctx), str)
        assert action_view("id", spec, args, ctx).tool_use_id == "id"
