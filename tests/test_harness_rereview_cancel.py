"""F2: whether a call was stopped and whether its write happens must be one decision.

``_write`` checked ``abort``, then wrote the temporary file, then replaced the target with no further check, and the
three attempts of ``_replace`` checked nothing either. A cancellation that had already returned (after the real 5 s
wait) therefore still left the file replaced by the worker thread. Every write now goes through ``ctx.writes``
(``CommitGuard``): a stop before the commit refuses the write, and a stop during the commit is reported as unknown
rather than as "nothing was written".
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from pathlib import Path

import pytest

from desktop.core.harness import fs_tools
from desktop.core.harness.tools import CommitGuard, ToolContext, ToolError, ToolRegistry, ToolResult


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


async def until(event: threading.Event, seconds: float = 10.0) -> None:
    end = time.monotonic() + seconds
    while not event.is_set():
        if time.monotonic() > end:
            raise AssertionError("the worker thread never got there")
        await asyncio.sleep(0.01)


def began(caplog) -> list[logging.LogRecord]:
    return [record for record in caplog.records if "write had begun" in record.getMessage()]


def leftovers(project: Path) -> list[str]:
    return sorted(item.name for item in project.iterdir() if item.name != "f.txt")


def park_before_the_replace(monkeypatch, parked: threading.Event, release: threading.Event) -> None:
    """Hold the worker thread where the audit found the hole: the temporary file is written, the target is not."""
    real = fs_tools._temporary_beside

    def gated(path: Path, data: bytes):
        temporary = real(path, data)
        parked.set()
        release.wait(10)
        return temporary

    monkeypatch.setattr(fs_tools, "_temporary_beside", gated)


def watching(registry: ToolRegistry, outcome: list[object], finished: threading.Event) -> ToolRegistry:
    """A registry whose create_file records what the handler itself saw, cancellation included."""
    real = fs_tools.create_file
    watched = ToolRegistry()

    def handler(args: dict, context: ToolContext) -> ToolResult:
        try:
            result = real(args, context)
        except BaseException as error:
            outcome.append(error)
            raise
        else:
            outcome.append(result)
            return result
        finally:
            outcome.append(context.writes)
            finished.set()

    watched.register(fs_tools.CREATE_FILE, handler, ui_input=lambda args: {})
    return watched


# --------------------------------------------------------------------------- the window the audit found


def test_a_cancel_while_the_temporary_file_waits_leaves_the_target_alone(project, ctx, registry, monkeypatch, caplog):
    monkeypatch.setattr("desktop.core.harness.tools.THREAD_WAIT_SECONDS", 0.3)
    caplog.set_level(logging.WARNING, logger="desktop.harness.tools")
    target = project / "f.txt"
    target.write_bytes(b"before\n")
    fs_tools.read_file({"file_path": "f.txt"}, ctx)  # replacing an existing file requires a current read
    parked, release, finished = threading.Event(), threading.Event(), threading.Event()
    park_before_the_replace(monkeypatch, parked, release)
    outcome: list[object] = []
    watched = watching(registry, outcome, finished)

    async def scenario() -> str:
        task = asyncio.ensure_future(watched.run("create_file", {"file_path": "f.txt", "content": "after\n"}, ctx))
        await until(parked)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        at_cancel = target.read_text(encoding="utf-8")
        release.set()
        await asyncio.to_thread(finished.wait, 10)
        return at_cancel

    at_cancel = asyncio.run(scenario())
    assert at_cancel == "before\n"
    assert target.read_text(encoding="utf-8") == "before\n", "the released thread replaced the file after the cancel"
    assert isinstance(outcome[0], ToolError) and "Stopped by the user" in str(outcome[0])
    assert leftovers(project) == [], "the temporary file was left behind"
    assert not began(caplog), "nothing had begun to be written, so nothing may be logged as under way"


def test_a_stop_between_two_replace_attempts_leaves_the_target_alone(project, ctx, registry, monkeypatch):
    target = project / "f.txt"
    target.write_bytes(b"before\n")
    fs_tools.read_file({"file_path": "f.txt"}, ctx)  # replacing an existing file requires a current read
    attempts: list[int] = []
    real = os.replace

    def locked_once(source, destination):
        attempts.append(1)
        if len(attempts) == 1:
            raise PermissionError(13, "The process cannot access the file because it is being used by another process")
        return real(source, destination)

    monkeypatch.setattr(os, "replace", locked_once)
    monkeypatch.setattr(fs_tools, "REPLACE_RETRY_SECONDS", 0.05)
    threading.Timer(0.01, ctx.abort.set).start()
    result = asyncio.run(registry.run("create_file", {"file_path": "f.txt", "content": "after\n"}, ctx))
    assert result.is_error and "Stopped by the user" in result.output
    assert len(attempts) == 1, "the retry ran although the call had been stopped"
    assert target.read_bytes() == b"before\n"
    assert leftovers(project) == []


def test_a_stop_is_refused_before_the_in_place_fallback(project, ctx, registry, monkeypatch):
    """The fallback for a file that cannot be replaced must be guarded like the replace itself."""
    target = project / "f.txt"
    target.write_bytes(b"before\n")
    fs_tools.read_file({"file_path": "f.txt"}, ctx)  # replacing an existing file requires a current read

    def no_temporary_file_and_stopped(path: Path, data: bytes):
        ctx.abort.set()
        return None

    monkeypatch.setattr(fs_tools, "_temporary_beside", no_temporary_file_and_stopped)
    result = asyncio.run(registry.run("create_file", {"file_path": "f.txt", "content": "after\n"}, ctx))
    assert result.is_error and "Stopped by the user" in result.output
    assert target.read_bytes() == b"before\n", "the in-place write ran although the call had been stopped"


def test_a_stop_during_the_last_replace_attempt_is_refused_before_the_in_place_fallback(project, ctx, registry, monkeypatch):
    target = project / "f.txt"
    target.write_bytes(b"before\n")
    fs_tools.read_file({"file_path": "f.txt"}, ctx)  # replacing an existing file requires a current read
    attempts: list[int] = []

    def locked(source, destination):
        attempts.append(1)
        if len(attempts) == fs_tools.REPLACE_ATTEMPTS:
            ctx.abort.set()
        raise PermissionError(13, "locked")

    monkeypatch.setattr(os, "replace", locked)
    monkeypatch.setattr(fs_tools, "REPLACE_RETRY_SECONDS", 0.01)
    result = asyncio.run(registry.run("create_file", {"file_path": "f.txt", "content": "after\n"}, ctx))
    assert len(attempts) == fs_tools.REPLACE_ATTEMPTS
    assert result.is_error and "Stopped by the user" in result.output
    assert target.read_bytes() == b"before\n"
    assert leftovers(project) == []


def test_a_call_that_was_already_stopped_writes_no_temporary_file(project, ctx, registry, monkeypatch):
    seen: list[str] = []
    real = fs_tools._temporary_beside

    def spy(path: Path, data: bytes):
        seen.append(str(path))
        return real(path, data)

    monkeypatch.setattr(fs_tools, "_temporary_beside", spy)
    ctx.abort.set()
    result = asyncio.run(registry.run("create_file", {"file_path": "new.txt", "content": "x"}, ctx))
    assert result.is_error and "Stopped by the user" in result.output
    assert seen == [], "a stopped call still wrote a temporary file"
    assert not (project / "new.txt").exists()


def test_a_call_stopped_after_planning_writes_no_temporary_file(project, ctx, registry, monkeypatch):
    """``create_file`` checks before it makes folders; an edit reaches the write with only the check at its top."""
    target = project / "f.txt"
    target.write_bytes(b"before\n")
    fs_tools.read_file({"file_path": "f.txt"}, ctx)  # replacing an existing file requires a current read
    seen: list[str] = []
    real_temporary, real_plan = fs_tools._temporary_beside, fs_tools._plan_edit

    def spy(path: Path, data: bytes):
        seen.append(str(path))
        return real_temporary(path, data)

    def planned_then_stopped(context: ToolContext, args: dict):
        plan = real_plan(context, args)
        context.abort.set()
        return plan

    monkeypatch.setattr(fs_tools, "_temporary_beside", spy)
    monkeypatch.setattr(fs_tools, "_plan_edit", planned_then_stopped)
    result = asyncio.run(registry.run("edit_file", {"file_path": "f.txt", "old_str": "before", "new_str": "after"}, ctx))
    assert result.is_error and "Stopped by the user" in result.output
    assert seen == [], "a stopped call still wrote a temporary file"
    assert target.read_bytes() == b"before\n"


# --------------------------------------------------------------------------- an honest answer for a write under way


def test_a_cancel_that_arrives_during_the_commit_does_not_claim_that_nothing_was_written(project, ctx, registry, monkeypatch, caplog):
    monkeypatch.setattr("desktop.core.harness.tools.THREAD_WAIT_SECONDS", 0.3)
    caplog.set_level(logging.WARNING, logger="desktop.harness.tools")
    target = project / "f.txt"
    target.write_bytes(b"before\n")
    fs_tools.read_file({"file_path": "f.txt"}, ctx)  # replacing an existing file requires a current read
    inside, release, finished = threading.Event(), threading.Event(), threading.Event()
    real = os.replace

    def gated(source, destination):
        inside.set()
        release.wait(10)
        return real(source, destination)

    monkeypatch.setattr(os, "replace", gated)
    outcome: list[object] = []
    watched = watching(registry, outcome, finished)

    async def scenario() -> None:
        task = asyncio.ensure_future(watched.run("create_file", {"file_path": "f.txt", "content": "after\n"}, ctx))
        await until(inside)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        release.set()
        await asyncio.to_thread(finished.wait, 10)

    asyncio.run(scenario())
    assert target.read_bytes() == b"after\n", "a commit that had begun was expected to finish"
    assert isinstance(outcome[0], ToolResult) and not outcome[0].is_error
    guard = outcome[1]
    assert isinstance(guard, CommitGuard) and guard.written and guard.stopped
    assert not guard.stop(), "a stop must not report success once the write went through"
    assert began(caplog), "a write that was under way when the call was stopped must be recorded"


def test_the_guard_answers_honestly_about_what_it_did():
    guard, abort = CommitGuard(), threading.Event()
    assert guard.stop() is True, "nothing was written, so stopping is the truth"
    with pytest.raises(ToolError, match="nothing was written"):
        with guard.commit(abort):
            raise AssertionError("the window must not open after a stop")
    fresh = CommitGuard()
    with fresh.commit(abort) as written:
        assert fresh.stop() is False, "a write under way cannot be taken back"
        written()
    assert fresh.written and not fresh.writing
    assert fresh.stop() is False
    stopped_by_abort = CommitGuard()
    abort.set()
    with pytest.raises(ToolError, match="nothing was written"):
        with stopped_by_abort.commit(abort):
            raise AssertionError("the window must not open once abort is set")


def test_a_failed_write_lets_a_later_attempt_commit():
    guard, abort = CommitGuard(), threading.Event()
    with pytest.raises(OSError):
        with guard.commit(abort):
            raise OSError("the disk said no")
    assert not guard.writing and not guard.written
    with guard.commit(abort) as written:
        written()
    assert guard.written


def test_a_stopped_call_does_not_stop_the_writes_of_a_later_turn(project, ctx, registry, monkeypatch):
    """The controller keeps one context for the session and gives each turn a new ``abort``. A cancelled call must not
    leave anything behind on it that refuses the writes of every later call."""
    monkeypatch.setattr("desktop.core.harness.tools.THREAD_WAIT_SECONDS", 0.3)
    target = project / "f.txt"
    target.write_bytes(b"before\n")
    fs_tools.read_file({"file_path": "f.txt"}, ctx)  # replacing an existing file requires a current read
    parked, release, finished = threading.Event(), threading.Event(), threading.Event()
    real = fs_tools._temporary_beside

    def parks_once(path: Path, data: bytes):
        temporary = real(path, data)
        if not parked.is_set():
            parked.set()
            release.wait(10)
        return temporary

    monkeypatch.setattr(fs_tools, "_temporary_beside", parks_once)
    outcome: list[object] = []
    watched = watching(registry, outcome, finished)

    async def stopped_turn() -> None:
        task = asyncio.ensure_future(watched.run("create_file", {"file_path": "f.txt", "content": "after\n"}, ctx))
        await until(parked)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        release.set()
        await asyncio.to_thread(finished.wait, 10)

    asyncio.run(stopped_turn())
    assert target.read_bytes() == b"before\n"
    ctx.abort = threading.Event()  # what the controller does for the next turn
    result = asyncio.run(registry.run("create_file", {"file_path": "g.txt", "content": "next\n"}, ctx))
    assert not result.is_error, result.output
    assert (project / "g.txt").read_text(encoding="utf-8") == "next\n"


def test_every_call_gets_its_own_guard(project, registry, monkeypatch):
    """One call's stop must not refuse another call's write, even on the same context."""
    ctx = ToolContext(project=project)
    guards: list[CommitGuard] = []
    real = fs_tools.create_file

    def handler(args: dict, context: ToolContext) -> ToolResult:
        guards.append(context.writes)
        return real(args, context)

    watched = ToolRegistry()
    watched.register(fs_tools.CREATE_FILE, handler, ui_input=lambda args: {})
    assert not asyncio.run(watched.run("create_file", {"file_path": "one.txt", "content": "1"}, ctx)).is_error
    assert not asyncio.run(watched.run("create_file", {"file_path": "two.txt", "content": "2"}, ctx)).is_error
    assert len(guards) == 2 and guards[0] is not guards[1]
    assert guards[0] is not ctx.writes
    assert (project / "one.txt").read_text(encoding="utf-8") == "1"
    assert (project / "two.txt").read_text(encoding="utf-8") == "2"


def test_an_ordinary_write_still_goes_through_a_temporary_file_and_a_replace(project, ctx, registry, monkeypatch):
    target = project / "f.txt"
    target.write_bytes(b"before\n")
    fs_tools.read_file({"file_path": "f.txt"}, ctx)  # replacing an existing file requires a current read
    seen: list[bytes] = []
    real = os.replace

    def spy(source, destination):
        seen.append(Path(source).read_bytes())
        return real(source, destination)

    monkeypatch.setattr(os, "replace", spy)
    result = asyncio.run(registry.run("create_file", {"file_path": "f.txt", "content": "after\n"}, ctx))
    assert not result.is_error, result.output
    assert seen == [b"after\n"]
    assert target.read_bytes() == b"after\n"
    assert leftovers(project) == []
