"""R6: a pathological regular expression must not stall the event loop and must not run for ever.

Python's ``re`` can backtrack catastrophically (``^(a+)+$`` on 25 characters took 0.33 s and held the GIL, so the
asyncio loop was late by the same amount even though the search ran in a worker thread). The search therefore needs a
hard, killable time limit; only patterns without regex meta characters, which cannot backtrack, stay in process.
"""

from __future__ import annotations

import asyncio
import contextlib
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from desktop.core.harness import fs_tools
from desktop.core.harness.tools import ToolContext, ToolRegistry, ToolResult

PATHOLOGICAL = "^(a+)+$"
SLOW_LINE = "a" * 24 + "!"
"""24 a's: 0.33 s of backtracking, enough to see a stall, short enough to finish within the time limit."""
ENDLESS_LINE = "a" * 40 + "!"


@pytest.fixture
def project(tmp_path: Path) -> Path:
    folder = tmp_path / "proj"
    (folder / "sub").mkdir(parents=True)
    (folder / "a.py").write_text("import os\nTODO fix\nprint('x')\n", encoding="utf-8")
    (folder / "sub" / "b.py").write_text("todo lower\nnothing\n", encoding="utf-8")
    (folder / "sub" / "c.txt").write_text("TODO in text\n", encoding="utf-8")
    return folder


@pytest.fixture
def ctx(project: Path) -> ToolContext:
    return ToolContext(project=project)


@pytest.fixture
def registry() -> ToolRegistry:
    return ToolRegistry.default()


def search(registry: ToolRegistry, ctx: ToolContext, **args) -> ToolResult:
    return asyncio.run(registry.run("grep", args, ctx))


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


def test_a_pathological_pattern_does_not_stall_the_event_loop(project, ctx, registry):
    (project / "line.txt").write_text(SLOW_LINE + "\n", encoding="utf-8")
    result, elapsed, worst = asyncio.run(with_heartbeat(registry.run("grep", {"pattern": PATHOLOGICAL, "path": "line.txt"}, ctx)))
    assert not result.is_error, result.output
    assert result.output == "No matches found."
    assert elapsed < 5, f"the search took {elapsed:.2f} s"
    assert worst < 0.15, f"the event loop was blocked for {worst:.2f} s"


def test_a_search_that_runs_too_long_is_stopped_with_a_clear_error(project, ctx, registry, monkeypatch):
    (project / "line.txt").write_text(ENDLESS_LINE + "\n", encoding="utf-8")
    monkeypatch.setattr(fs_tools, "GREP_TIMEOUT_SECONDS", 1.5)
    result, elapsed, worst = asyncio.run(with_heartbeat(registry.run("grep", {"pattern": PATHOLOGICAL, "path": "line.txt"}, ctx)))
    assert result.is_error, result.output
    assert "too long" in result.output and "simpler pattern" in result.output
    assert 1.0 < elapsed < 8, f"the limit was not honoured ({elapsed:.2f} s)"
    assert worst < 0.15, f"the event loop was blocked for {worst:.2f} s"


def test_a_long_search_stops_when_the_user_stops_the_turn(project, ctx, registry, monkeypatch):
    (project / "line.txt").write_text(ENDLESS_LINE + "\n", encoding="utf-8")
    monkeypatch.setattr(fs_tools, "GREP_TIMEOUT_SECONDS", 60.0)
    threading.Timer(0.5, ctx.abort.set).start()
    started = time.monotonic()
    result = search(registry, ctx, pattern=PATHOLOGICAL, path="line.txt")
    elapsed = time.monotonic() - started
    assert result.is_error and "Stopped by the user" in result.output
    assert elapsed < 5, f"stopping took {elapsed:.2f} s"


@pytest.mark.skipif(sys.platform != "win32", reason="checks that no runaway process is left behind")
def test_no_search_process_is_left_behind(project, ctx, registry, monkeypatch):
    (project / "line.txt").write_text(ENDLESS_LINE + "\n", encoding="utf-8")
    monkeypatch.setattr(fs_tools, "GREP_TIMEOUT_SECONDS", 1.0)
    started: list[subprocess.Popen] = []
    real = subprocess.Popen

    def spy(*args, **kwargs):
        process = real(*args, **kwargs)
        started.append(process)
        return process

    monkeypatch.setattr(subprocess, "Popen", spy)
    assert search(registry, ctx, pattern=PATHOLOGICAL, path="line.txt").is_error
    assert started, "the search did not run in a separate process"
    for process in started:
        assert process.poll() is not None, "a search process outlived the call"


# --------------------------------------------------------------------------- the answers stay the same


def test_a_regular_expression_gives_the_same_answers_as_before(project, ctx, registry):
    assert search(registry, ctx, pattern="TOD[O]").output.splitlines() == ["a.py", "sub/c.txt"]
    assert search(registry, ctx, pattern="to.o", case_insensitive=True).output.splitlines() == ["a.py", "sub/b.py", "sub/c.txt"]
    assert search(registry, ctx, pattern="TOD[O]", glob_pattern="*.py").output == "a.py"
    assert search(registry, ctx, pattern="nothing|print", output_mode="content").output.splitlines() == [
        "a.py:3:print('x')",
        "sub/b.py:2:nothing",
    ]
    near = search(registry, ctx, pattern="TODO.fix", output_mode="content", context=1)
    assert near.output.splitlines() == ["a.py-1-import os", "a.py:2:TODO fix", "a.py-3-print('x')"]
    limited = search(registry, ctx, pattern="TOD[O]", head_limit=1).output.splitlines()
    assert limited[0] == "a.py" and limited[1].startswith("[Output limited to 1 files")
    assert search(registry, ctx, pattern="x|y", path="sub/b.py").output == "No matches found."
    assert search(registry, ctx, pattern="to.o", case_insensitive=True, path="sub/b.py").output == "sub/b.py"


def test_a_literal_pattern_is_searched_in_process_and_a_regular_expression_apart(project, ctx, registry, monkeypatch):
    spawned: list[tuple] = []
    real = subprocess.Popen

    def spy(*args, **kwargs):
        spawned.append(args)
        return real(*args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", spy)
    assert search(registry, ctx, pattern="TODO").output.splitlines() == ["a.py", "sub/c.txt"]
    assert spawned == [], "a literal pattern cannot backtrack and must not pay for a process"
    assert search(registry, ctx, pattern="TOD[O]").output.splitlines() == ["a.py", "sub/c.txt"]
    assert len(spawned) == 1
    assert str(spawned[0][0][0]).lower().endswith(("python.exe", "python", "pythonw.exe"))


@pytest.mark.parametrize("pattern", ["TODO", "foo bar", "关键字", "a-b", "x = 1", "#include", "<div>", "'q' \"q\"", "a,b;c", "C:/x"])
def test_text_without_meta_characters_is_plain(pattern):
    assert fs_tools._plain_text(pattern)


@pytest.mark.parametrize("pattern", ["a+", "a*", "a?", "a|b", "(a)", "[a]", "a{2}", "^a", "a$", "a.b", "\\d", "a}"])
def test_any_meta_character_makes_a_regular_expression(pattern):
    assert not fs_tools._plain_text(pattern)


def test_a_turn_already_stopped_starts_no_search_process(project, ctx, registry, monkeypatch):
    spawned: list[tuple] = []
    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: spawned.append(args))
    ctx.abort.set()
    result = search(registry, ctx, pattern="TOD[O]", path="a.py")
    assert result.is_error and "Stopped by the user" in result.output
    assert spawned == []


def test_an_invalid_regular_expression_is_still_reported_before_anything_runs(project, ctx, registry, monkeypatch):
    spawned: list[tuple] = []
    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: spawned.append(args))
    result = search(registry, ctx, pattern="(unclosed")
    assert result.is_error and "Invalid regular expression" in result.output
    assert spawned == []


def test_a_search_apart_also_skips_protected_folders(project, registry):
    secret = project / "secrets"
    secret.mkdir()
    (secret / "token.json").write_text('{"access_token": "SEEKRIT"}', encoding="utf-8")
    (project / "open.txt").write_text("SEEKRIT open", encoding="utf-8")
    guarded = ToolContext(project=project, protected=(secret,))
    assert search(registry, guarded, pattern="SEEK.IT").output == "open.txt"
    assert search(registry, guarded, pattern="SEEK.IT", output_mode="content").output == "open.txt:1:SEEKRIT open"
    refused = search(registry, guarded, pattern="SEEK.IT", path="secrets")
    assert refused.is_error and "protected" in refused.output


def test_a_search_that_cannot_be_started_is_a_clear_error(project, ctx, registry, monkeypatch):
    monkeypatch.setattr(fs_tools, "_worker_python", lambda: str(project / "no-such-python.exe"))
    result = search(registry, ctx, pattern="TOD[O]")
    assert result.is_error and "search" in result.output.lower()
