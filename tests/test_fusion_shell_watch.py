"""Runner heartbeats stay local; only a final result or the enclosing deadline goes to the model."""

from __future__ import annotations

import asyncio
import os
import re
import sys

import pytest

from desktop.core.harness import shell
from desktop.core.harness.loop import Call
from desktop.core.harness.tools import ToolContext, ToolRegistry
from desktop.core.harness.toolset import HarnessTools
from desktop.core.viewmodel import tool_json
from tests.chatgpt_support import finish_turn, until
from tests.fake_responses import call, say
from tests.test_fusion_identity_runtime import _brief, _fusion, _output

pytestmark = pytest.mark.fusion_gates


def python_command(monkeypatch, source):
    monkeypatch.setattr(shell, "HEARTBEAT_SECONDS", 0.03)
    monkeypatch.setattr(shell, "_launch", lambda *_: ([sys.executable, "-u", "-c", source], dict(os.environ)))


def test_runner_waits_through_heartbeats_without_model_polls_or_an_inner_timeout(tmp_path, monkeypatch):
    python_command(monkeypatch, """
from pathlib import Path
import time
print('test started')
for _ in range(500):
    if Path('release').exists():
        break
    time.sleep(.02)
else:
    raise SystemExit(1)
print('test passed')
""")
    monkeypatch.setattr(shell, "DEFAULT_TIMEOUT", 0.01)

    async def go():
        async with _fusion(tmp_path) as (main, fake):
            fake.script(call("delegate_task", _brief("Runner")),
                        call("shell", {"command": "run tests", "timeout": 601}, call_id="test"),
                        say("Checks passed."), say("Reviewed."))
            assert main.send_prompt("Check the inventory.")

            def live_item():
                for view in main.agents.values():
                    if view.controller is not None and view.controller._turn is not None:
                        return view.controller._turn.tools.get("test")

            try:
                await until(lambda: (item := live_item()) is not None and "距最近输出" in item.progress)
                item = live_item()
                assert "命令仍在运行" in tool_json(item)["output"]
                earlier = item.version
                await until(lambda: item.version > earlier)  # Another heartbeat, not another turn or tool call.
                assert len(fake.requests) == 2
                declaration = next(tool for tool in fake.requests[1]["tools"] if tool["name"] == "shell")
                assert "timeout" not in declaration["parameters"]["properties"]
            finally:
                (main.project / "release").touch()
            await finish_turn(main)
            runner, = main.agents.values()
            output = _output(runner.controller, "test").splitlines()
            assert [line for line in output if line] == ["test started", "test passed", "Exit code: 0"]
            assert len(fake.requests) == 4
            assert "命令仍在运行" not in str(fake.requests)

    asyncio.run(go())


def test_explicit_local_health_failure_returns_one_result_and_cleans_up(tmp_path, monkeypatch):
    python_command(monkeypatch, "import time; print('last checkpoint'); time.sleep(30)")

    async def go():
        tools = HarnessTools(ToolRegistry.default(), ToolContext(tmp_path), "full")
        tools.runner_shell = True
        snapshots = []
        watch = None

        def check(_call, status):
            nonlocal watch
            snapshots.append(status)
            watch = tools.active_shell
            return "test worker missed its required acknowledgement" if status.output_idle is not None else None

        tools.shell_health_check = check
        check_call = Call("item", "check", "shell", '{"command":"run tests"}', {"command": "run tests"})
        result = await asyncio.wait_for(tools.run(check_call), 8)
        assert result.is_error and "last checkpoint" in result.output
        assert result.output.count("Stopped after a local health check failed") == 1
        assert "missed its required acknowledgement" in result.output
        assert "Process-tree termination confirmed." in result.output
        assert snapshots[-1].output_idle is not None
        assert watch is not None and watch.tree_stopped and tools.active_shell is None

    asyncio.run(go())


def test_runner_deadline_interrupts_shell_and_reports_partial_output_to_main(tmp_path, monkeypatch):
    python_command(monkeypatch, "import time; print('unfinished check'); time.sleep(30)")

    async def go():
        async with _fusion(tmp_path, max_seconds=2) as (main, fake):
            fake.script(call("delegate_task", _brief("Runner"), call_id="runner"),
                        call("shell", {"command": "run tests"}, call_id="test"),
                        say("The Runner reached its deadline."))
            assert main.send_prompt("Check the inventory.")
            await finish_turn(main)
            runner, = main.agents.values()
            assert runner.record.status == "interrupted"
            assert not runner.controller._tools.runner_shell
            report = _output(main, "runner")
            assert "Runner reached its 2-second round limit" in report
            assert "Process-tree termination confirmed." in report
            assert "Partial command output (not a completed check)" in report
            ref = re.search(r"o-[0-9a-f]+", report).group()
            assert main._outputs.read(ref).strip() == "unfinished check"
            assert len(fake.requests) == 3  # The timed-out Runner was not asked to report again.

    asyncio.run(go())
