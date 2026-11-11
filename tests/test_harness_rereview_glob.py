"""F6: a glob must not backtrack, and brace expansion must not explode.

Whether a search goes to the killable subprocess depends on ``pattern`` alone. ``glob_pattern`` was turned into a
regular expression and matched in whatever process the search ran in, so ``("a*" * 12) + "b"`` took 0.73 s of
backtracking in the worker thread and the event loop was late by as much. ``{a,b}`` written 40 times stood for 2**40
names, which ``expand_braces`` tried to build (reached from ``glob`` and from a permission decision alike).
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from pathlib import Path

import pytest

from desktop.core.harness import fs_tools
from desktop.core.harness.permissions import decide
from desktop.core.harness.tools import ToolContext, ToolRegistry, ToolResult

BOMB = "a*" * 12 + "b"
"""The audit's pattern: twelve stars, each of which the regular expression could divide the name at."""
BRACE_BOMB = "{a,b}" * 40


@pytest.fixture
def project(tmp_path: Path) -> Path:
    folder = tmp_path / "proj"
    (folder / "src").mkdir(parents=True)
    (folder / ("a" * 28 + ".txt")).write_text("needle\n", encoding="utf-8")
    (folder / "src" / "b.py").write_text("needle here\n", encoding="utf-8")
    (folder / "src" / "c.ts").write_text("needle there\n", encoding="utf-8")
    return folder


@pytest.fixture
def ctx(project: Path) -> ToolContext:
    return ToolContext(project=project)


@pytest.fixture
def registry() -> ToolRegistry:
    return ToolRegistry.default()


async def with_heartbeat(coro) -> tuple[ToolResult, float, float]:
    """Run ``coro`` while a 50 ms heartbeat measures how late the loop gets; returns (result, seconds, worst delay)."""
    loop = asyncio.get_running_loop()
    late: list[float] = []

    async def heartbeat() -> None:
        while True:
            mark = loop.time()
            await asyncio.sleep(0.05)
            late.append(loop.time() - mark - 0.05)

    beat = asyncio.ensure_future(heartbeat())
    await asyncio.sleep(0.15)
    started = loop.time()
    result = await coro
    elapsed = loop.time() - started
    beat.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await beat
    return result, elapsed, max(late)


# --------------------------------------------------------------------------- the glob itself


def test_a_glob_that_would_backtrack_is_matched_at_once():
    matches = fs_tools._glob_matcher(BOMB)
    started = time.perf_counter()
    assert not matches("a" * 28)
    assert not matches("a" * 400)
    assert matches("a" * 20 + "b")
    assert time.perf_counter() - started < 0.5, "the matcher is still dividing the name between its stars"
    deep = fs_tools._glob_matcher("**/" + "a*" * 30 + "b")
    started = time.perf_counter()
    assert not deep("x/y/" + "a" * 300)
    assert time.perf_counter() - started < 0.5


@pytest.mark.parametrize(
    "pattern,text,wanted",
    [
        ("*.py", "a.py", True),
        ("*.py", "a.pyc", False),
        ("*.py", "sub/a.py", False),
        ("*.PY", "a.py", fs_tools.IS_WINDOWS),
        ("*.py", "A.PY", fs_tools.IS_WINDOWS),
        ("a.py", "a.py", True),
        ("a.py", "b.py", False),
        ("*", "anything", True),
        ("*", "sub/a", False),
        ("**", "sub/a", True),
        ("**/*.py", "a/b/c.py", True),
        ("**/*.py", "c.py", True),
        ("**/x", "x", True),
        ("**/x", "a/x", True),
        ("**/x", "ax", False),
        ("src/*.py", "src/a.py", True),
        ("src/*.py", "src/sub/a.py", False),
        ("src/**", "src/a/b.c", True),
        ("src/**", "src/a", True),
        ("src/**", "src", False),
        ("**", "a", True),
        ("src/**/*.ts", "src/a/b.ts", True),
        ("src/**/*.ts", "src/b.ts", True),
        ("test_*.py", "test_x.py", True),
        ("test_*.py", "x_test.py", False),
        ("?.py", "a.py", True),
        ("?.py", "ab.py", False),
        ("[ab].py", "b.py", True),
        ("[ab].py", "c.py", False),
        ("[!ab].py", "c.py", True),
        ("[!ab].py", "a.py", False),
        ("[a-c]x", "bx", True),
        ("[a-c]x", "dx", False),
        ("a?c", "abc", True),
        ("a*b*c", "axxbyyc", True),
        ("a*b*c", "axxc", False),
        ("ab*ba", "aba", False),
        ("ab*ba", "abba", True),
        ("[ab]*", "a", True),
        ("[ab]*", "c", False),
        ("[ab]*x", "bzzx", True),
        ("a[", "a[", True),
        ("x.{a}", "x.{a}", True),
    ],
)
def test_the_matcher_reads_a_glob_the_way_ripgrep_does(pattern, text, wanted):
    assert fs_tools._glob_matcher(pattern)(text) is wanted


def test_a_run_of_stars_inside_a_segment_stays_inside_it():
    """``**`` only crosses folders as a whole segment, which is what ripgrep does with it."""
    assert fs_tools._glob_matcher("x**")("x")
    assert not fs_tools._glob_matcher("x**")("x/y")
    assert fs_tools._glob_matcher("a**b")("axb")
    assert not fs_tools._glob_matcher("a**b")("a/b")


def test_the_file_filter_takes_a_name_or_a_path_as_before():
    for pattern, rel, wanted in [
        ("*.py", "src/a.py", True),
        ("*.py", "src/a.ts", False),
        ("src/*.py", "src/a.py", True),
        ("src/*.py", "other/a.py", False),
        ("./*.py", "a.py", True),
        ("/*.py", "a.py", True),
        ("*.{ts,tsx}", "x/a.tsx", True),
        ("*.{ts,tsx}", "x/a.js", False),
        ("src\\*.py", "src/a.py", True),
        (BOMB, "a" * 28, False),
    ]:
        assert fs_tools._file_filter(pattern)(rel) is wanted, (pattern, rel)
    assert fs_tools._file_filter(None) is None
    assert fs_tools._file_filter("") is None


# --------------------------------------------------------------------------- brace expansion


def test_brace_expansion_still_works_and_no_longer_explodes():
    assert fs_tools.expand_braces("*.py") == ["*.py"]
    assert fs_tools.expand_braces("*.{ts,tsx}") == ["*.ts", "*.tsx"]
    assert fs_tools.expand_braces("{a,b}/{c,d}") == ["a/c", "a/d", "b/c", "b/d"]
    assert fs_tools.expand_braces("x.{a,{b,c}}") == ["x.a", "x.b", "x.c"]
    assert fs_tools.expand_braces("odd{brace") == ["odd{brace"]
    assert len(fs_tools.expand_braces("*.{js,jsx,ts,tsx,mjs,cjs,json,md}")) == 8
    assert len(fs_tools.expand_braces("{a,b}" * 9)) == 512
    assert fs_tools.expand_braces("{a,b}" * 12) == ["{a,b}" * 12], "4096 names take more expansions than allowed"
    long = "x" * fs_tools.MAX_PATTERN_CHARS + "{a,b}"
    assert fs_tools.expand_braces(long) == [long], "a pattern this long is not expanded at all"
    started = time.perf_counter()
    assert fs_tools.expand_braces(BRACE_BOMB) == [BRACE_BOMB], "an impossible pattern must come back unexpanded"
    assert fs_tools.expand_braces("{a,b}" * 125) == ["{a,b}" * 125]
    assert fs_tools.expand_braces("{a,b," * 300) == ["{a,b," * 300]
    assert time.perf_counter() - started < 1.0, "the expansion is still trying to build every name"


def test_an_impossible_brace_pattern_matches_nothing_instead_of_hanging(project, ctx, registry):
    started = time.perf_counter()
    result = asyncio.run(registry.run("glob", {"patterns": [BRACE_BOMB]}, ctx))
    assert not result.is_error, result.output
    assert "No files matched" in result.output
    assert time.perf_counter() - started < 5, "glob is still expanding the braces"


def test_a_permission_decision_does_not_expand_an_impossible_brace_pattern(project, ctx):
    started = time.perf_counter()
    decision = decide("code", fs_tools.GLOB, {"patterns": [BRACE_BOMB + "/*.py"]}, ctx, remembered=set())
    assert decision.verdict in ("allow", "ask")
    assert time.perf_counter() - started < 5, "the decision is still expanding the braces"


# --------------------------------------------------------------------------- the search that uses it


def test_a_glob_pattern_does_not_stall_the_event_loop(project, ctx, registry):
    result, elapsed, worst = asyncio.run(with_heartbeat(registry.run("grep", {"pattern": "needle", "glob_pattern": BOMB}, ctx)))
    assert not result.is_error, result.output
    assert result.output == "No matches found."
    assert elapsed < 3, f"the search took {elapsed:.2f} s"
    assert worst < 0.2, f"the event loop was blocked for {worst:.2f} s"


def test_a_glob_pattern_with_a_real_regular_expression_does_not_stall_either(project, ctx, registry):
    """The pattern goes to the search process; the glob is matched there, which has the same budget."""
    result, elapsed, worst = asyncio.run(with_heartbeat(registry.run("grep", {"pattern": "need(le)+", "glob_pattern": BOMB}, ctx)))
    assert not result.is_error, result.output
    assert elapsed < 15, f"the search took {elapsed:.2f} s"
    assert worst < 0.2, f"the event loop was blocked for {worst:.2f} s"


def test_an_ordinary_search_with_a_glob_still_finds_what_it_should(project, ctx, registry):
    found = asyncio.run(registry.run("grep", {"pattern": "needle", "glob_pattern": "*.py"}, ctx))
    assert not found.is_error and "src/b.py" in found.output and "c.ts" not in found.output
    both = asyncio.run(registry.run("grep", {"pattern": "needle", "glob_pattern": "*.{py,ts}"}, ctx))
    assert "src/b.py" in both.output and "src/c.ts" in both.output
    anchored = asyncio.run(registry.run("grep", {"pattern": "needle", "glob_pattern": "src/*.py"}, ctx))
    assert "src/b.py" in anchored.output and "c.ts" not in anchored.output
    lines = asyncio.run(registry.run("grep", {"pattern": "needle", "glob_pattern": "**/*.ts", "output_mode": "content"}, ctx))
    assert "needle there" in lines.output


def test_glob_still_finds_files_by_pattern(project, ctx, registry):
    result = asyncio.run(registry.run("glob", {"patterns": ["**/*.py", "**/*.ts"]}, ctx))
    assert not result.is_error and "src/b.py" in result.output and "src/c.ts" in result.output
    braced = asyncio.run(registry.run("glob", {"patterns": ["**/*.{py,ts}"]}, ctx))
    assert "src/b.py" in braced.output and "src/c.ts" in braced.output
