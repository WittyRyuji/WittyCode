"""F1: a wildcard must be judged by what it actually picks, in the last segment and behind another wildcard.

``_fixed_part`` answers for ``linked*`` with the folder in front of it (the current one), which is inside the project,
so a read-only command was allowed and ``Get-ChildItem -Recurse linked* | Get-Content`` read a junction into the
protected folder at the ``code`` level. Only the first wildcard segment was listed, so ``cat */linked*/*`` reached a
link one folder down even at the ``full`` level. Every wildcard segment is expanded now, the last one included.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path

import pytest

from desktop.core.harness import shell
from desktop.core.harness.permissions import LEVELS, decide, is_readonly_command, remember_key
from desktop.core.harness.shell import SHELL, argument_targets
from desktop.core.harness.tools import ToolContext, ToolRegistry, ToolResult

windows_only = pytest.mark.skipif(sys.platform != "win32", reason="runs Windows PowerShell")

SENTINEL = "SENTINEL-NOT-A-REAL-SECRET"
OUTSIDE = "OUTSIDE-NOT-A-REAL-SECRET"


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
    (folder / "src" / "b.py").write_text("import os\n", encoding="utf-8")
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


@pytest.fixture
def portal(project: Path, secret: Path, tmp_path: Path) -> ToolContext:
    """A project with ``portal`` pointing at a folder outside it, which is a question rather than a refusal."""
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "x.txt").write_text(OUTSIDE, encoding="utf-8")
    make_dir_link(project / "portal", elsewhere)
    return ToolContext(project=project, protected=(secret,))


def verdict(level: str, args: dict, ctx: ToolContext, remembered=frozenset()) -> str:
    return decide(level, SHELL, args, ctx, remembered=set(remembered)).verdict


THROUGH_AN_END_WILDCARD = [
    "Get-ChildItem -Recurse linked* | Get-Content",
    "Get-ChildItem linked* -Recurse | Select-String SENTINEL",
    "Get-Content linked*",
    "cat linked*",
    "type linked-*",
    "Get-ChildItem linked*",
    "Select-String SENTINEL linked*",
    "cat LINKED*",
    "cat linked-pri?ate",
    "type l[i]nked-private",
    "cat a.txt,linked*",
    "cat *",
    "gci * | gc",
    'cmd /c "type linked*"',
]


@pytest.mark.parametrize("command", THROUGH_AN_END_WILDCARD)
@pytest.mark.parametrize("level", LEVELS)
def test_a_wildcard_that_picks_the_link_is_denied_at_every_level(linked, level, command):
    args = {"command": command}
    decision = decide(level, SHELL, args, linked, remembered=set())
    assert decision.verdict == "deny", (level, command)
    assert "protected" in decision.reason
    assert not is_readonly_command(command, linked.project, linked.project), command
    remembered = {remember_key(SHELL, args, linked), "exec:get-content", "exec:cat", "exec:type", "exec:gci"}
    assert decide(level, SHELL, args, linked, remembered=remembered).verdict == "deny", command


def test_a_wildcard_is_expanded_against_the_commands_own_cwd(linked):
    for level in LEVELS:
        assert verdict(level, {"command": "cat ../linked*", "cwd": "src"}, linked) == "deny", level
    assert verdict("code", {"command": "cat *", "cwd": "src"}, linked) == "allow", "src holds no link"


def test_a_wildcard_that_picks_a_link_out_of_the_project_asks(portal):
    for command in ("cat portal*", "cat porta?", "Get-ChildItem portal*", "cat *"):
        assert not is_readonly_command(command, portal.project, portal.project), command
        assert verdict("ask", {"command": command}, portal) == "ask", command
        assert verdict("code", {"command": command}, portal) == "ask", command
        assert verdict("full", {"command": command}, portal) == "allow", command


def test_where_one_argument_can_lead_includes_what_its_wildcard_picks(linked, project, secret):
    targets = argument_targets("linked*", project)
    assert project.resolve() in targets, "the folder in front of the wildcard is still an answer"
    assert secret.resolve() in targets, "what the wildcard picks is missing"
    assert argument_targets("a.txt", project) == [(project / "a.txt").resolve()]
    assert argument_targets("nothing-*", project) == [project.resolve()], "a pattern that picks nothing adds nothing"
    picked = argument_targets("*", project)
    assert (project / "a.txt").resolve() in picked and secret.resolve() in picked


ORDINARY = [
    "git status",
    "dir",
    "ls src",
    "Get-ChildItem -Recurse -Filter *.py",
    "cat a.txt",
    "cat *.txt",
    "ls src/*.py",
    "Get-Content a.txt -TotalCount 5",
    "Select-String alpha a.txt",
    "rg -n --glob *.py alpha src",
    "rg -l alpha",
    "rg --files-with-matches alpha",
    "findstr /i alpha *.txt",
    "cat a.txt | Select-String alpha",
    "cat nothing-*",
    "git log --oneline -5",
]


@pytest.mark.parametrize("command", ORDINARY)
def test_ordinary_commands_still_run_without_a_question_although_the_project_holds_a_link(linked, command):
    assert is_readonly_command(command, linked.project, linked.project), command
    assert verdict("code", {"command": command}, linked) == "allow", command


FOLLOWS_LINKS = ["rg -L alpha", "rg --follow alpha", "rg -Ln alpha", "rg alpha -L", "where /r . *.txt", "where /R . *.txt"]


@pytest.mark.parametrize("command", FOLLOWS_LINKS)
def test_a_command_told_to_walk_through_links_is_not_allowed_on_its_own(linked, command):
    assert not is_readonly_command(command, linked.project, linked.project), command
    assert verdict("code", {"command": command}, linked) == "ask", command


@windows_only
def test_the_shell_tool_itself_refuses_a_wildcard_that_picks_the_link(linked):
    result: ToolResult = asyncio.run(ToolRegistry.default().run("shell", {"command": "Get-ChildItem -Recurse linked* | Get-Content"}, linked))
    assert result.is_error and "protected" in result.output
    assert SENTINEL not in result.output
    allowed = asyncio.run(ToolRegistry.default().run("shell", {"command": "Get-Content *.txt"}, linked))
    assert not allowed.is_error and "alpha" in allowed.output


# --------------------------------------------------------------------------- a link behind a later wildcard


@pytest.fixture
def deep(project: Path, secret: Path) -> ToolContext:
    """A project whose link to the protected folder sits one folder down, where only a second wildcard reaches it."""
    make_dir_link(project / "src" / "linked-private", secret)
    return ToolContext(project=project, protected=(secret,))


THROUGH_A_LATER_WILDCARD = [
    "cat */linked*/*",
    "Get-Content */linked*/*",
    "cat s*/l*/m*",
    "Get-ChildItem */linked* | Get-Content",
    "Get-ChildItem -Recurse */linked* | Get-Content",
    "cat src/*/marker.txt",
    "cat */*/marker.txt",
    "cat s*/linked-private/marker.txt",
    "cat */linked*/q*",
]


@pytest.mark.parametrize("command", THROUGH_A_LATER_WILDCARD)
@pytest.mark.parametrize("level", LEVELS)
def test_a_link_behind_a_later_wildcard_is_denied_at_every_level(deep, level, command):
    """Only the first wildcard segment used to be listed, so ``cat */linked*/*`` was allowed at ``full`` and read the
    protected folder. ``*/linked*/q*`` picks nothing inside it, but the folder it passes through is the protected one."""
    remembered = {"exec:cat", "exec:get-content", "exec:get-childitem"}
    decision = decide(level, SHELL, {"command": command}, deep, remembered=remembered)
    assert decision.verdict == "deny", (level, command)
    assert "protected" in decision.reason


def test_a_second_wildcard_folder_is_expanded_through_a_link_at_the_top(linked):
    for level in LEVELS:
        assert verdict(level, {"command": "cat */*/marker.txt"}, linked) == "deny", level


def test_where_a_later_wildcard_leads(deep, project, secret):
    assert secret.resolve() in shell._wildcard_targets("*/linked*", project)
    assert (secret / "marker.txt").resolve() in shell._wildcard_targets("s*/l*/m*", project)
    assert shell._wildcard_targets("*/nothing-*/x", project) == [(project / "a.txt").resolve(), (project / "src").resolve()]


def test_ordinary_commands_are_still_allowed_beside_a_deeper_link(deep):
    for command in ("cat *.txt", "ls src/*.py", "cat a.txt", "dir src"):
        assert verdict("code", {"command": command}, deep) == "allow", command


def test_an_expansion_that_would_list_too_many_folders_counts_as_unknown(project, monkeypatch):
    for name in ("one", "two", "three"):
        (project / name).mkdir()
    assert None not in shell._wildcard_targets("*/*/x.txt", project)
    monkeypatch.setattr(shell, "_EXPANSION_LISTINGS", 2)
    assert shell._wildcard_targets("*/*/x.txt", project)[-1] is None


def test_an_expansion_with_too_many_targets_counts_as_unknown(project, monkeypatch):
    for name in ("x1", "x2", "x3"):
        (project / name).write_text("", encoding="utf-8")
    assert None not in shell._wildcard_targets("x*", project)
    monkeypatch.setattr(shell, "_EXPANSION_TARGETS", 2)
    assert shell._wildcard_targets("x*", project)[-1] is None


def test_a_listing_that_was_cut_off_counts_as_unknown(project, monkeypatch):
    for name in ("x1", "y1", "y2"):
        (project / name).write_text("", encoding="utf-8")
    monkeypatch.setattr(shell, "_LISTING_LIMIT", 2)
    assert None in shell._wildcard_targets("x*", project), "an entry past the cut may be the link"


@windows_only
def test_the_shell_tool_itself_refuses_a_link_behind_a_later_wildcard(deep):
    result: ToolResult = asyncio.run(ToolRegistry.default().run("shell", {"command": "cat */linked*/*"}, deep))
    assert result.is_error and "protected" in result.output
    assert SENTINEL not in result.output
