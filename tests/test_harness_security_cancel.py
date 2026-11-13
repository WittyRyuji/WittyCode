"""R2: stopping a tool call must stop the work, not only the await.

The file tools are synchronous and run in a worker thread (``asyncio.to_thread``). Cancelling the awaiting task does
not stop a thread, so without a commit barrier a Create or an Edit could still write after the cancellation had
returned and the controller had already started the next turn.
"""

from __future__ import annotations

import asyncio
import os
import stat
import sys
import threading
import time
from pathlib import Path

import pytest

from desktop.core.harness import fs_tools
from desktop.core.harness.tools import ToolContext, ToolRegistry, ToolResult


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


def park_in_plan(monkeypatch, planned: threading.Event, release: threading.Event, only: str | None = None) -> list[str]:
    """Hold the worker thread inside the real ``_plan_edit``, after the plan is made and before anything is written."""
    real = fs_tools._plan_edit
    entered: list[str] = []

    def gated(context: ToolContext, args: dict) -> fs_tools._EditPlan:
        entered.append(args["old_str"])
        plan = real(context, args)
        if only is None or args["old_str"] == only:
            planned.set()
            release.wait(10)
        return plan

    monkeypatch.setattr(fs_tools, "_plan_edit", gated)
    return entered


def leftovers(project: Path) -> list[str]:
    return sorted(item.name for item in project.iterdir() if item.name != "f.txt")


# --------------------------------------------------------------------------- the commit barrier


def test_an_edit_stopped_while_it_is_planned_writes_nothing(project, ctx, registry, monkeypatch):
    target = project / "f.txt"
    target.write_bytes(b"before\n")
    planned, release = threading.Event(), threading.Event()
    park_in_plan(monkeypatch, planned, release)

    async def scenario() -> ToolResult:
        task = asyncio.ensure_future(registry.run("edit_file", {"file_path": "f.txt", "old_str": "before", "new_str": "after"}, ctx))
        await until(planned)
        ctx.abort.set()
        release.set()
        return await task

    result = asyncio.run(scenario())
    assert result.is_error and "Stopped by the user" in result.output
    assert target.read_bytes() == b"before\n"
    assert leftovers(project) == []


def test_a_create_stopped_after_it_read_the_old_content_does_not_overwrite(project, ctx, registry, monkeypatch):
    target = project / "f.txt"
    target.write_bytes(b"before\n")
    fs_tools.read_file({"file_path": "f.txt"}, ctx)
    real = fs_tools._decode

    def gated(data: bytes):
        decoded = real(data)
        ctx.abort.set()
        return decoded

    monkeypatch.setattr(fs_tools, "_decode", gated)
    result = asyncio.run(registry.run("create_file", {"file_path": "f.txt", "content": "after\n"}, ctx))
    assert result.is_error and "Stopped by the user" in result.output
    assert target.read_bytes() == b"before\n"
    assert leftovers(project) == []


def test_a_write_is_refused_when_the_turn_was_already_stopped(project, ctx, registry):
    (project / "f.txt").write_bytes(b"before\n")
    ctx.abort.set()
    created = asyncio.run(registry.run("create_file", {"file_path": "new.txt", "content": "x"}, ctx))
    assert created.is_error and "Stopped by the user" in created.output
    assert not (project / "new.txt").exists()
    edited = asyncio.run(registry.run("edit_file", {"file_path": "f.txt", "old_str": "before", "new_str": "after"}, ctx))
    assert edited.is_error and "Stopped by the user" in edited.output
    assert (project / "f.txt").read_bytes() == b"before\n"
    deeper = asyncio.run(registry.run("create_file", {"file_path": "made/up/new.txt", "content": "x"}, ctx))
    assert deeper.is_error and not (project / "made").exists(), "a stopped create still made its folders"


# --------------------------------------------------------------------------- cancellation waits for the thread


def test_cancelling_an_edit_waits_for_the_worker_thread_and_leaves_the_file_alone(project, ctx, monkeypatch):
    target = project / "f.txt"
    target.write_bytes(b"before\n")
    planned, release, finished = threading.Event(), threading.Event(), threading.Event()
    park_in_plan(monkeypatch, planned, release)
    real_edit = fs_tools.edit_file

    def watched(args: dict, context: ToolContext) -> ToolResult:
        try:
            return real_edit(args, context)
        finally:
            finished.set()

    registry = ToolRegistry()
    registry.register(fs_tools.EDIT_FILE, watched, ui_input=lambda args: {})

    async def scenario() -> float:
        task = asyncio.ensure_future(registry.run("edit_file", {"file_path": "f.txt", "old_str": "before", "new_str": "after"}, ctx))
        await until(planned)
        ctx.abort.set()
        task.cancel()
        # The thread stays parked past the cancellation: only a wait inside run() can see it finish.
        threading.Timer(0.25, release.set).start()
        started = time.monotonic()
        with pytest.raises(asyncio.CancelledError):
            await task
        return time.monotonic() - started

    waited = asyncio.run(scenario())
    assert finished.is_set(), "the cancellation returned while the worker thread was still running the tool"
    assert waited >= 0.1, f"run() did not wait for the thread ({waited:.3f} s)"
    assert target.read_bytes() == b"before\n"
    assert leftovers(project) == []


def test_a_cancelled_tool_that_never_stops_does_not_hold_the_turn_for_ever(project, ctx, monkeypatch):
    monkeypatch.setattr("desktop.core.harness.tools.THREAD_WAIT_SECONDS", 0.4)
    stuck = threading.Event()
    running = threading.Event()

    def never_stops(args: dict, context: ToolContext) -> ToolResult:
        running.set()
        stuck.wait(20)
        return ToolResult("done")

    registry = ToolRegistry()
    registry.register(fs_tools.EDIT_FILE, never_stops, ui_input=lambda args: {})

    async def scenario() -> float:
        task = asyncio.ensure_future(registry.run("edit_file", {"file_path": "f.txt", "old_str": "a", "new_str": "b"}, ctx))
        await until(running)
        task.cancel()
        started = time.monotonic()
        try:
            with pytest.raises(asyncio.CancelledError):
                await task
            return time.monotonic() - started
        finally:
            stuck.set()  # asyncio.run waits for the executor's threads on the way out

    waited = asyncio.run(scenario())
    assert 0.3 <= waited < 3.0, f"the wait is not bounded ({waited:.3f} s)"
    assert ctx.abort.is_set(), "the cancellation must tell the tool to stop"


def test_a_thread_that_outlives_the_wait_still_writes_nothing_once_the_next_turn_began(project, ctx, monkeypatch):
    monkeypatch.setattr("desktop.core.harness.tools.THREAD_WAIT_SECONDS", 0.2)
    target = project / "f.txt"
    target.write_bytes(b"before\n")
    planned, release, finished = threading.Event(), threading.Event(), threading.Event()
    park_in_plan(monkeypatch, planned, release)
    real_edit = fs_tools.edit_file
    outcome: list[ToolResult | BaseException] = []

    def watched(args: dict, context: ToolContext) -> ToolResult:
        try:
            outcome.append(real_edit(args, context))
        except BaseException as error:
            outcome.append(error)
            raise
        finally:
            finished.set()
        return outcome[-1]

    registry = ToolRegistry()
    registry.register(fs_tools.EDIT_FILE, watched, ui_input=lambda args: {})

    async def scenario() -> None:
        task = asyncio.ensure_future(registry.run("edit_file", {"file_path": "f.txt", "old_str": "before", "new_str": "after"}, ctx))
        await until(planned)
        task.cancel()  # as when the session closes: nobody set abort first
        with pytest.raises(asyncio.CancelledError):
            await task
        ctx.abort = threading.Event()  # what the controller does when the next turn starts
        release.set()
        await asyncio.to_thread(finished.wait, 10)

    asyncio.run(scenario())
    assert finished.is_set()
    assert isinstance(outcome[0], fs_tools.ToolError) and "Stopped by the user" in str(outcome[0])
    assert target.read_bytes() == b"before\n"
    assert leftovers(project) == []


# --------------------------------------------------------------------------- one writer per file


def test_two_edits_of_one_file_do_not_interleave(project, ctx, registry, monkeypatch):
    target = project / "f.txt"
    target.write_bytes(b"a\nb\n")
    planned, release = threading.Event(), threading.Event()
    entered = park_in_plan(monkeypatch, planned, release, only="a")

    async def scenario() -> tuple[list[str], ToolResult, ToolResult]:
        first = asyncio.ensure_future(registry.run("edit_file", {"file_path": "f.txt", "old_str": "a", "new_str": "A"}, ctx))
        await until(planned)
        second = asyncio.ensure_future(registry.run("edit_file", {"file_path": "f.txt", "old_str": "b", "new_str": "B"}, ctx))
        await asyncio.sleep(0.4)
        while_held = list(entered)
        release.set()
        return while_held, await first, await second

    while_held, first, second = asyncio.run(scenario())
    assert while_held == ["a"], "the second edit read the file while the first had not written its change yet"
    assert not first.is_error and not second.is_error, (first.output, second.output)
    assert target.read_bytes() == b"A\nB\n"


# --------------------------------------------------------------------------- the write itself


def test_a_file_that_cannot_be_replaced_is_written_in_place(project, ctx, registry, monkeypatch):
    """As when another program holds the file open without delete sharing: renaming over it fails, writing works."""
    target = project / "f.txt"
    target.write_bytes(b"before\n")
    tried: list[str] = []

    def refuse(source, destination):
        tried.append(str(destination))
        raise PermissionError(13, "The process cannot access the file because it is being used by another process")

    monkeypatch.setattr(os, "replace", refuse)
    result = asyncio.run(registry.run("edit_file", {"file_path": "f.txt", "old_str": "before", "new_str": "after"}, ctx))
    assert not result.is_error, result.output
    assert tried, "the edit did not try to replace the file"
    assert target.read_bytes() == b"after\n"
    assert leftovers(project) == []


def test_a_stop_while_the_file_is_locked_writes_nothing(project, ctx, registry, monkeypatch):
    target = project / "f.txt"
    target.write_bytes(b"before\n")

    def locked_then_stopped(source, destination):
        ctx.abort.set()
        raise PermissionError(13, "The process cannot access the file because it is being used by another process")

    monkeypatch.setattr(os, "replace", locked_then_stopped)
    result = asyncio.run(registry.run("edit_file", {"file_path": "f.txt", "old_str": "before", "new_str": "after"}, ctx))
    assert result.is_error and "Stopped by the user" in result.output
    assert target.read_bytes() == b"before\n"
    assert leftovers(project) == []


@pytest.mark.skipif(sys.platform == "win32", reason="Windows keeps only a read-only flag, and a read-only file is never replaced")
def test_a_replaced_file_keeps_its_permission_bits(project, ctx, registry):
    target = project / "f.txt"
    target.write_bytes(b"before\n")
    os.chmod(target, 0o751)
    result = asyncio.run(registry.run("edit_file", {"file_path": "f.txt", "old_str": "before", "new_str": "after"}, ctx))
    assert not result.is_error, result.output
    assert stat.S_IMODE(target.stat().st_mode) == 0o751


def test_an_edit_goes_through_a_temporary_file_and_a_replace(project, ctx, registry, monkeypatch):
    target = project / "f.txt"
    target.write_bytes(b"before\n")
    seen: list[tuple[str, bytes]] = []
    real = os.replace

    def spy(source, destination):
        seen.append((Path(source).parent.name, Path(source).read_bytes()))
        return real(source, destination)

    monkeypatch.setattr(os, "replace", spy)
    result = asyncio.run(registry.run("edit_file", {"file_path": "f.txt", "old_str": "before", "new_str": "after"}, ctx))
    assert not result.is_error, result.output
    assert seen == [(project.name, b"after\n")]
    assert target.read_bytes() == b"after\n"
    assert leftovers(project) == []


@pytest.mark.skipif(sys.platform != "win32", reason="a read-only file can still be renamed over elsewhere")
def test_a_write_that_fails_leaves_the_file_as_it_was_and_no_leftovers(project, ctx, registry):
    target = project / "f.txt"
    target.write_bytes(b"before\n")
    os.chmod(target, stat.S_IREAD)
    try:
        result = asyncio.run(registry.run("edit_file", {"file_path": "f.txt", "old_str": "before", "new_str": "after"}, ctx))
        assert result.is_error and "Cannot write" in result.output
        assert target.read_bytes() == b"before\n"
        assert leftovers(project) == []
    finally:
        os.chmod(target, stat.S_IREAD | stat.S_IWRITE)


def test_a_normal_edit_still_replaces_the_text_and_keeps_the_file_readable(project, ctx, registry):
    target = project / "f.txt"
    target.write_bytes(b"one\r\ntwo\r\n")
    mode = target.stat().st_mode
    result = asyncio.run(registry.run("edit_file", {"file_path": "f.txt", "old_str": "two", "new_str": "2"}, ctx))
    assert not result.is_error and "replaced 1 occurrence" in result.output
    assert target.read_bytes() == b"one\r\n2\r\n"
    assert target.stat().st_mode == mode
    assert leftovers(project) == []
    created = asyncio.run(registry.run("create_file", {"file_path": "sub/new.txt", "content": "x\n"}, ctx))
    assert not created.is_error and (project / "sub" / "new.txt").read_bytes() == b"x\n"
