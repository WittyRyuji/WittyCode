"""Regression cases independently rebuilt from the audit, using isolated files and child processes."""
from __future__ import annotations

import asyncio
import codecs
import json
import os
from pathlib import Path

import pytest

from desktop.core.chatgpt.errors import ChatGPTError
from desktop.core.chatgpt.responses import Finished, ItemDone, Started, TextDelta, user_message
from desktop.core.harness import compact
from desktop.core.harness.loop import AgentLoop, Observer, Outcome
from desktop.core.harness.permissions import decide
from desktop.core.harness.tools import ToolContext, ToolRegistry
from desktop.core.harness.toolset import HarnessTools
from tests.test_harness_loop import FakeTools, Recorder, Script, assert_paired
from tests.fake_responses import say


def test_file_pages_do_not_lose_the_middle_in_the_loop(tmp_path):
    text = "".join(f"line {n}: " + "x" * 95 + "\n" for n in range(1, 1401))
    (tmp_path / "source.txt").write_text(text, encoding="utf-8")
    registry = ToolRegistry.default()
    first = asyncio.run(registry.run("read_file", {"file_path": "source.txt"}, ToolContext(tmp_path)))
    assert len(first.output) <= 60000
    assert "offset=" in first.output and "1400" not in first.output.splitlines()[-2]
    # Page until EOF using the tool's own next offset, checking every original numbered line exactly once.
    import re
    numbers = []
    ctx = ToolContext(tmp_path)
    offset = 1
    while True:
        result = asyncio.run(registry.run("read_file", {"file_path": "source.txt", "offset": offset}, ctx))
        numbers.extend(int(m[1]) for m in re.finditer(r"(?m)^\s*(\d+)\t", result.output))
        next_page = re.search(r"offset=(\d+)", result.output)
        if next_page is None:
            break
        offset = int(next_page[1])
    assert numbers == list(range(1, 1401))


def test_loop_acknowledged_silent_stream_is_not_resent():
    requests = []
    async def stream(request):
        requests.append(request)
        yield Started()
        raise ChatGPTError("timed out", code="network", transient=True)
    loop = AgentLoop(stream, FakeTools(), Recorder(), model="m", reasoning=None, instructions="", retry_delays=(0, 0))
    with pytest.raises(ChatGPTError):
        asyncio.run(loop.run([user_message("go")]))
    assert len(requests) == 1


def test_stream_failure_keeps_visible_partial_text_and_paired_completed_calls():
    async def stream(request):
        yield TextDelta("demo", "Already shown.")
        yield ItemDone({"type": "function_call", "call_id": "demo-call", "name": "echo", "arguments": "{}"})
        raise ChatGPTError("reset", code="network")
    history = [user_message("go")]
    with pytest.raises(ChatGPTError):
        asyncio.run(AgentLoop(stream, FakeTools(), Observer(), model="m", reasoning=None, instructions="").run(history))
    assert "Already shown." in str(history)
    assert_paired(history)


def test_empty_reasoning_only_response_gets_one_bounded_recovery():
    script = Script(say("", thinking="thinking"), say("answer"))
    history = [user_message("go")]
    end = asyncio.run(AgentLoop(script, FakeTools(), Recorder(), model="m", reasoning=None, instructions="").run(history))
    assert len(script.requests) == 2 and end.reason == "done" and "answer" in str(history)
    script = Script(say("", thinking="thinking"), say("", thinking="still thinking"))
    end = asyncio.run(AgentLoop(script, FakeTools(), Recorder(), model="m", reasoning=None, instructions="").run([user_message("go")]))
    assert len(script.requests) == 2 and end.reason == "incomplete" and end.detail == "empty_response"


def test_summary_receives_every_plan_step_and_edit_argument():
    steps = [{"content": "step " + str(i) + ": " + "detail " * 30, "status": "pending"} for i in range(15)]
    plan = {"type": "function_call", "name": "todo_write", "arguments": json.dumps({"todos": steps})}
    edit = {"type": "function_call", "name": "edit_file", "arguments": json.dumps({"old_str": "a" * 30000 + "MIDDLE_CONSTRAINT" + "z" * 30000})}
    rendered = "\n".join(compact.summary_parts([plan, edit], 4000))
    assert all(step["content"] in rendered for step in steps)
    assert "MIDDLE_CONSTRAINT" in rendered and "characters left out" not in rendered


def test_gb18030_read_search_edit_and_original_encoding_are_preserved(tmp_path):
    path = tmp_path / "legacy.txt"
    path.write_bytes("这是旧内容\r\n第二行\r\n".encode("gb18030"))
    registry, ctx = ToolRegistry.default(), ToolContext(tmp_path)
    read = asyncio.run(registry.run("read_file", {"file_path": "legacy.txt"}, ctx))
    assert "这是旧内容" in read.output
    found = asyncio.run(registry.run("grep", {"pattern": "旧内容"}, ctx))
    assert "legacy.txt" in found.output
    edited = asyncio.run(registry.run("edit_file", {"file_path": "legacy.txt", "old_str": "旧内容", "new_str": "新内容"}, ctx))
    assert not edited.is_error
    assert path.read_bytes() == "这是新内容\r\n第二行\r\n".encode("gb18030")


def test_whole_file_overwrite_requires_read_and_rejects_concurrent_changes(tmp_path):
    path = tmp_path / "notes.txt"
    original = codecs.BOM_UTF16_LE + "original\r\n".encode("utf-16-le")
    path.write_bytes(original)
    registry, ctx = ToolRegistry.default(), ToolContext(tmp_path)
    args = {"file_path": "notes.txt", "content": "replacement\n"}
    denied = asyncio.run(registry.run("create_file", args, ctx))
    assert denied.is_error and "read_file" in denied.output and path.read_bytes() == original
    asyncio.run(registry.run("read_file", {"file_path": "notes.txt"}, ctx))
    path.write_bytes(codecs.BOM_UTF16_LE + "concurrent\r\n".encode("utf-16-le"))
    denied = asyncio.run(registry.run("create_file", args, ctx))
    assert denied.is_error and "changed" in denied.output
    asyncio.run(registry.run("read_file", {"file_path": "notes.txt"}, ctx))
    assert not asyncio.run(registry.run("create_file", args, ctx)).is_error
    assert path.read_bytes() == codecs.BOM_UTF16_LE + "replacement\r\n".encode("utf-16-le")


def test_wrong_parameter_names_are_not_silently_ignored_and_numeric_strings_work(tmp_path):
    (tmp_path / "a.txt").write_text("one\ntwo\nthree\n", encoding="utf-8")
    registry, ctx = ToolRegistry.default(), ToolContext(tmp_path)
    wrong = asyncio.run(registry.run("read_file", {"file_path": "a.txt", "start_line": 2}, ctx))
    assert wrong.is_error and "start_line" in wrong.output
    correct = asyncio.run(registry.run("read_file", {"file_path": "a.txt", "offset": "2", "limit": "1"}, ctx))
    assert not correct.is_error and "two" in correct.output and "one" not in correct.output


@pytest.mark.parametrize("name", ["env:WITTYCODE_DEMO", "HKCU:\\Software", "cert:\\CurrentUser\\My", "variable:PSVersionTable", "FileSystem::notes.txt", "Microsoft.PowerShell.Core\\FileSystem::notes.txt"])
def test_provider_paths_never_qualify_for_automatic_readonly_shell_permission(tmp_path, name):
    ctx, spec = ToolContext(tmp_path), ToolRegistry.default().get("shell")
    verdict = decide("code", spec, {"command": f"gc '{name}'"}, ctx, remembered=set())
    assert verdict.verdict == "ask"


@pytest.mark.skipif(os.name != "nt", reason="Windows junction and PowerShell providers")
def test_filesystem_provider_cannot_bypass_a_protected_junction(tmp_path):
    import subprocess
    project, protected = tmp_path / "project", tmp_path / "protected"
    project.mkdir(); protected.mkdir()
    junction = project / "linked"
    result = subprocess.run(["cmd", "/c", "mklink", "/J", str(junction), str(protected)], capture_output=True)
    assert result.returncode == 0
    try:
        ctx, spec = ToolContext(project, protected=(protected,)), ToolRegistry.default().get("shell")
        for level in ("ask", "code", "full"):
            for provider in ("FileSystem::", "Microsoft.PowerShell.Core\\FileSystem::"):
                assert decide(level, spec, {"command": f"gc '{provider}linked/notes.txt'"}, ctx, remembered=set()).verdict == "deny"
    finally:
        junction.rmdir()
