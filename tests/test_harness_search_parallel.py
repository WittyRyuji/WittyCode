"""Git-aware search and bounded read-only concurrency, with no remote repository or model calls."""
from __future__ import annotations

import asyncio
import json
import shutil
import subprocess

import pytest

from desktop.core.harness import fs_tools
from desktop.core.harness.loop import AgentLoop, Observer
from desktop.core.harness.tools import ToolContext, ToolRegistry, ToolResult, ToolSpec
from desktop.core.harness.toolset import HarnessTools
from tests.fake_responses import calls, say
from tests.test_harness_loop import Script, assert_paired


@pytest.mark.skipif(shutil.which("git") is None, reason="Git is not installed")
@pytest.mark.parametrize("pattern", ["NEEDLE", "NEE[D]LE"])
def test_search_uses_git_inventory_and_keeps_untracked_and_tracked_ignored_files(tmp_path, pattern):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / ".gitignore").write_text("ignored/\n*.generated\n", encoding="utf-8")
    (tmp_path / "tracked.generated").write_text("NEEDLE", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", "-f", "tracked.generated"], check=True)
    (tmp_path / "local.txt").write_text("NEEDLE", encoding="utf-8")
    (tmp_path / "ignored").mkdir()
    for number in range(100):
        (tmp_path / "ignored" / f"noise{number}.txt").write_text("NEEDLE", encoding="utf-8")
    registry, ctx = ToolRegistry.default(), ToolContext(tmp_path)
    result = asyncio.run(registry.run("grep", {"pattern": pattern}, ctx))
    assert not result.is_error and set(result.output.splitlines()) == {"local.txt", "tracked.generated"}
    globbed = asyncio.run(registry.run("glob", {"patterns": ["**/*.txt"]}, ctx))
    assert globbed.output == "local.txt"
    rooted = asyncio.run(registry.run("glob", {"patterns": ["**/*.txt"], "folder": "."}, ctx))
    assert rooted.output == "local.txt", "spelling out the default root does not opt out of ignore rules"
    # An explicit folder is still searchable, even if Git normally ignores it.
    explicit = asyncio.run(registry.run("grep", {"pattern": "NEEDLE", "path": "ignored"}, ctx))
    assert "ignored/noise0.txt" in explicit.output


def test_timeout_message_does_not_claim_a_regular_expression_is_known_to_be_at_fault(monkeypatch):
    class Busy:
        def wait(self, timeout):
            raise subprocess.TimeoutExpired("search", timeout)
    import threading
    monkeypatch.setattr(fs_tools, "GREP_TIMEOUT_SECONDS", .001)
    with pytest.raises(Exception, match="Narrow the path") as error:
        fs_tools._wait_for_search(Busy(), threading.Event(), "simple")
    assert "probably makes" not in str(error.value)


@pytest.mark.parametrize("cancel", [False, True])
def test_allowed_file_reads_start_together_but_results_stay_ordered_and_stops_are_paired(tmp_path, cancel):
    async def go():
        entered = []
        gate = asyncio.Event()
        registry = ToolRegistry()
        async def read(args, ctx):
            entered.append(args["file_path"])
            if len(entered) == 3:
                gate.set()
            await gate.wait()
            if cancel:
                await asyncio.sleep(100)
            return ToolResult(args["file_path"])
        spec = ToolSpec("read_file", "Read a demo file.", {"type": "object", "properties": {"file_path": {"type": "string"}}, "required": ["file_path"]}, "Read", "read")
        registry.register(spec, read, ui_input=dict, is_async=True)
        tools = HarnessTools(registry, ToolContext(tmp_path), "code")
        from desktop.core.connections import ConnectionStore
        tools.connections = ConnectionStore(tmp_path / "connections.json", home=tmp_path / "home")
        script = Script(calls([("read_file", json.dumps({"file_path": str(n)}), f"c{n}", f"f{n}") for n in range(3)]), say("done"))
        history = []
        loop = AgentLoop(script, tools, Observer(), model="m", reasoning=None, instructions="")
        task = asyncio.create_task(loop.run(history))
        try:
            await asyncio.wait_for(gate.wait(), 1)
            assert entered == ["0", "1", "2"]
            if cancel:
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            else:
                await task
                assert [i["output"] for i in history if i["type"] == "function_call_output"] == ["0", "1", "2"]
            assert_paired(history)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    asyncio.run(go())
