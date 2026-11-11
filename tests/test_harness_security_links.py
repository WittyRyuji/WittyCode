"""R1: a directory junction inside the project must not become a way into the protected folder.

The file tools resolve real paths, so they already refuse a link into ``ToolContext.protected``. These tests cover the
shell tool, whose check used to look at the command text only: a plain relative name such as ``linked-private/x.txt``
named no protected path and the command was treated as an allowed read-only one.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path

import pytest

from desktop.core.harness.permissions import LEVELS, decide, is_readonly_command, remember_key
from desktop.core.harness.shell import SHELL, resolved_argument
from desktop.core.harness.tools import ToolContext, ToolRegistry, ToolResult

windows_only = pytest.mark.skipif(sys.platform != "win32", reason="runs Windows PowerShell")

SENTINEL = "SENTINEL-NOT-A-REAL-SECRET"


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


@pytest.fixture
def project(tmp_path: Path) -> Path:
    folder = tmp_path / "proj"
    (folder / "src").mkdir(parents=True)
    (folder / "a.txt").write_text("alpha\nbeta\n", encoding="utf-8")
    return folder


@pytest.fixture
def secret(tmp_path: Path) -> Path:
    folder = tmp_path / "WittyCode" / "chatgpt"
    folder.mkdir(parents=True)
    (folder / "marker.txt").write_text(SENTINEL, encoding="utf-8")
    return folder


@pytest.fixture
def linked(project: Path, secret: Path) -> ToolContext:
    """A project with ``linked-private`` pointing at the protected folder."""
    make_dir_link(project / "linked-private", secret)
    return ToolContext(project=project, protected=(secret,))


def verdict(level: str, args: dict, ctx: ToolContext, remembered=frozenset()) -> str:
    return decide(level, SHELL, args, ctx, remembered=set(remembered)).verdict


def shell(ctx: ToolContext, command: str, **extra) -> ToolResult:
    return asyncio.run(ToolRegistry.default().run("shell", {"command": command, **extra}, ctx))


THROUGH_THE_LINK = [
    "Get-Content linked-private/marker.txt",
    "Get-Content linked-private\\marker.txt",
    "Get-Content ./linked-private/marker.txt",
    "type linked-private/marker.txt",
    "cat linked-private/marker.txt",
    "Get-Content -LiteralPath linked-private/marker.txt",
    "Get-Content -Path:linked-private/marker.txt",
    "Select-String SENTINEL linked-private/marker.txt",
    "Get-Content linked-private/*.txt",
    "Get-ChildItem linked-private",
    "cat src/../linked-private/marker.txt",
    "cat a.txt | Select-String linked-private/marker.txt",
    "Get-Content a.txt,linked-private/marker.txt",
    'cmd /c "type linked-private\\marker.txt"',
    'powershell -NoProfile -Command "Get-Content \'linked-private/marker.txt\'"',
]


@pytest.mark.parametrize("command", THROUGH_THE_LINK)
@pytest.mark.parametrize("level", LEVELS)
def test_a_command_reaching_the_protected_folder_through_a_link_is_denied(linked, level, command):
    args = {"command": command}
    decision = decide(level, SHELL, args, linked, remembered=set())
    assert decision.verdict == "deny", (level, command)
    assert "protected" in decision.reason and "not" in decision.reason
    remembered = {remember_key(SHELL, args, linked), "exec:get-content", "exec:cat", "exec:type", "exec:select-string"}
    assert decide(level, SHELL, args, linked, remembered=remembered).verdict == "deny", command


def test_a_link_reached_from_a_cwd_deeper_in_the_project_is_denied(linked):
    for level in LEVELS:
        assert verdict(level, {"command": "cat ../linked-private/marker.txt", "cwd": "src"}, linked) == "deny"


def test_an_extended_length_path_through_the_link_is_denied(linked, project):
    command = f"Get-Content \\\\?\\{project}\\linked-private\\marker.txt"
    assert verdict("full", {"command": command}, linked) == "deny"


def test_a_network_path_is_not_resolved_while_deciding(linked, monkeypatch):
    real = Path.resolve
    seen: list[str] = []

    def spy(self, *args, **kwargs):
        seen.append(str(self))
        return real(self, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", spy)
    assert resolved_argument("\\\\server\\share\\x.txt", linked.project) is None
    assert verdict("code", {"command": "cat \\\\server\\share\\x.txt"}, linked) == "ask"
    assert verdict("full", {"command": "cat //server/share/x.txt"}, linked) == "allow"
    assert not [item for item in seen if "server" in item], seen


def test_each_item_of_a_list_argument_is_checked(linked):
    assert is_readonly_command("cat a.txt,src", linked.project, linked.project)
    assert not is_readonly_command("cat a.txt,C:\\Windows\\win.ini", linked.project, linked.project)
    assert not is_readonly_command("cat a.txt,..\\outside.txt", linked.project, linked.project)


def test_a_link_named_through_an_environment_variable_is_denied(linked, project, monkeypatch):
    monkeypatch.setenv("WC_TEST_LINK", str(project / "linked-private"))
    for command in ("type %WC_TEST_LINK%\\marker.txt", "type $env:WC_TEST_LINK\\marker.txt"):
        assert verdict("full", {"command": command}, linked) == "deny", command


@windows_only
def test_the_shell_tool_itself_refuses_a_command_that_leads_into_the_protected_folder(linked, project):
    result = shell(linked, "Get-Content linked-private/marker.txt")
    assert result.is_error and "protected" in result.output
    assert SENTINEL not in result.output
    deeper = shell(linked, "cat ../linked-private/marker.txt", cwd="src")
    assert deeper.is_error and "protected" in deeper.output and SENTINEL not in deeper.output


ORDINARY = [
    "git status",
    "dir",
    "ls src",
    "Get-ChildItem -Recurse -Filter *.py",
    "cat a.txt",
    "Get-Content a.txt -TotalCount 5",
    "type a.txt",
    "Select-String alpha a.txt",
    "rg -n --glob *.py alpha src",
    "findstr /i alpha *.txt",
    "cat a.txt | Select-String alpha",
    "echo hi",
    "pwd",
    "git log --oneline -5",
    "python --version",
]


@pytest.mark.parametrize("command", ORDINARY)
def test_ordinary_read_only_commands_inside_the_project_still_run_without_a_question(linked, command):
    assert is_readonly_command(command, linked.project, linked.project), command
    assert verdict("code", {"command": command}, linked) == "allow", command
    assert verdict("code", {"command": command, "cwd": "src"}, linked) == "allow", command


@windows_only
def test_an_ordinary_command_still_runs_with_a_link_in_the_project(linked, project):
    result = shell(linked, "Get-Content a.txt")
    assert not result.is_error and "alpha" in result.output


def test_a_link_out_of_the_project_asks_but_is_not_denied(project, tmp_path, secret):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "x.txt").write_text("outside", encoding="utf-8")
    make_dir_link(project / "portal", elsewhere)
    ctx = ToolContext(project=project, protected=(secret,))
    assert not is_readonly_command("cat portal/x.txt", project, project)
    assert verdict("ask", {"command": "cat portal/x.txt"}, ctx) == "ask"
    assert verdict("code", {"command": "cat portal/x.txt"}, ctx) == "ask"
    assert verdict("full", {"command": "cat portal/x.txt"}, ctx) == "allow"


def test_an_argument_that_cannot_be_resolved_asks(linked):
    for command in (
        "cat $target",
        "cat $env:APPDATA\\x.txt",
        "cat ${env:APPDATA}\\x.txt",
        "cat (Get-Item x).FullName",
        "cat `whoami`",
        "cat src/*/marker.txt",
        "cat nothing-*/marker.txt",
        "cat C:x.txt",
    ):
        assert verdict("code", {"command": command}, linked) == "ask", command
        assert not is_readonly_command(command, linked.project, linked.project), command
    assert verdict("full", {"command": "cat src/*/marker.txt"}, linked) == "allow"


@pytest.mark.parametrize(
    "command",
    ["cat linked-*/marker.txt", "cat */marker.txt", "Get-Content linked-pri?ate/marker.txt", "type l[i]nked-private\\marker.txt", "cat LINKED-*/marker.txt"],
)
@pytest.mark.parametrize("level", LEVELS)
def test_a_wildcard_that_expands_through_the_link_is_denied(linked, level, command):
    decision = decide(level, SHELL, {"command": command}, linked, remembered=set())
    assert decision.verdict == "deny", (level, command)
    assert "marker.txt" in decision.reason


def test_a_wildcard_is_expanded_in_the_folder_it_names(linked):
    for level in LEVELS:
        assert verdict(level, {"command": "cat ../linked-*/marker.txt", "cwd": "src"}, linked) == "deny", level
