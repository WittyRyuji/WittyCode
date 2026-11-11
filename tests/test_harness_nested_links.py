"""A link nested deeper in the project, and a recursive command that walks into it without any argument naming it.

``Get-ChildItem -Recurse -Include *.txt | Get-Content`` has no argument that leads out of the project; the walk does. A
repository that carries a symlink to the application's own folder (a clone made with symlinks enabled) is the way such a
link gets there. Reading what a walk finds is therefore asked about at ``code``; only listing names stays free.
"""

from __future__ import annotations

import asyncio
import shutil
import sys
from pathlib import Path

import pytest

from desktop.core.harness.permissions import decide, is_readonly_command
from desktop.core.harness.shell import SHELL
from desktop.core.harness.tools import ToolContext, ToolRegistry
from tests.test_harness_security_links import SENTINEL, make_dir_link, project, secret  # noqa: F401  (fixtures)


@pytest.fixture
def nested(project: Path, secret: Path) -> ToolContext:  # noqa: F811
    """``src/deep/portal`` points at the protected folder; nothing in the project root names it."""
    deep = project / "src" / "deep"
    deep.mkdir(parents=True)
    make_dir_link(deep / "portal", secret)
    return ToolContext(project=project, protected=(secret,))


def verdict(level: str, command: str, ctx: ToolContext) -> str:
    return decide(level, SHELL, {"command": command}, ctx, remembered=set()).verdict


READS_WHAT_IT_WALKS = [
    "Get-ChildItem -Recurse -Include *.txt | Get-Content",
    "gci -Recurse -Include *.txt | Get-Content",
    "ls -r -Filter *.txt | cat",
    "dir -Recurse | type",
    "Get-ChildItem -Rec -Include *.txt | gc",
    "Get-ChildItem -Recurse:true -Include *.txt | Get-Content",
    "Get-ChildItem -Recurse | Select-String SENTINEL",
    "gci -Depth 4 -Include *.txt | sls SENTINEL",
    "dir /s /b *.txt | type",
    "findstr /s SENTINEL *.txt",
    "findstr /S /I SENTINEL *.txt",
    "findstr /spec SENTINEL *.txt",
]
LISTS_ONLY = [
    "Get-ChildItem -Recurse",
    "gci -Recurse | Select-Object Name",
    "dir /s /b",
    "Get-Content a.txt",
    "findstr alpha a.txt",
    "Get-ChildItem | Get-Content",
    "rg SENTINEL",
    "rg -r X alpha a.txt | Select-String X",  # (-r is ripgrep's replace flag, not a walk)
    "Get-Content a.txt -ReadCount 1 | Select-String alpha",
]


@pytest.mark.parametrize("command", READS_WHAT_IT_WALKS)
def test_reading_what_a_recursive_walk_finds_is_not_a_read_only_command(nested, command):
    assert not is_readonly_command(command, nested.project, nested.project), command
    assert verdict("code", command, nested) == "ask", command
    assert verdict("ask", command, nested) == "ask", command
    assert verdict("full", command, nested) == "allow", "full allows everything that is not named as protected"


@pytest.mark.parametrize("command", LISTS_ONLY)
def test_listing_names_and_reading_what_is_named_stay_free(nested, command):
    assert is_readonly_command(command, nested.project, nested.project), command
    assert verdict("code", command, nested) == "allow", command


@pytest.mark.skipif(sys.platform != "win32" or shutil.which("rg") is None, reason="runs Windows PowerShell and ripgrep")
def test_ripgrep_does_not_walk_through_the_link_so_it_can_stay_free(nested):
    """The other recursive reader that is allowed: it does not follow links unless it is told to (``-L``, which asks)."""
    result = asyncio.run(ToolRegistry.default().run("shell", {"command": f"rg {SENTINEL}"}, nested))
    assert SENTINEL not in result.output.replace(f"rg {SENTINEL}", "")
    assert verdict("code", "rg -L SENTINEL", nested) == "ask"


@pytest.mark.skipif(sys.platform != "win32", reason="runs Windows PowerShell")
def test_the_walk_really_does_reach_the_secret_so_asking_is_not_overcaution(nested):
    """Straight through the tool (which does not decide): the command gives the sentinel up."""
    result = asyncio.run(ToolRegistry.default().run("shell", {"command": "Get-ChildItem -Recurse -Include *.txt | Get-Content"}, nested))
    if SENTINEL not in result.output:
        pytest.skip("this PowerShell does not walk through the link")
    assert SENTINEL in result.output
