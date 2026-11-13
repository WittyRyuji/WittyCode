"""Fresh review findings: old history, walk boundaries and expired summary ownership."""
from __future__ import annotations

import asyncio
import copy
import os

import pytest

from desktop.core.chatgpt import controller as controller_module
from desktop.core.chatgpt.responses import Usage, user_message
from desktop.core.harness import compact
from desktop.core.harness.permissions import decide
from desktop.core.harness.store import SavedTurn
from desktop.core.harness.tools import ToolContext, ToolRegistry
from tests.chatgpt_support import finish_turn
from tests.fake_responses import FakeResponses
from tests.test_chatgpt_controller import opened


@pytest.mark.parametrize("finished", [None, 2.0])
def test_failure_before_the_new_user_item_does_not_delete_or_relabel_the_previous_turn(tmp_path, monkeypatch, finished):
    async def scenario():
        with FakeResponses() as fake:
            engine, ctl = await opened(tmp_path, fake)
            try:
                ctl.saved.items = [user_message("previous interrupted request")]
                ctl.saved.turns = [SavedTurn(0, 1.0, finished, "interrupted")]
                before = copy.deepcopy((ctl.saved.items, ctl.saved.turns))
                def unreadable(*args, **kwargs):
                    raise OSError("cannot discover skills")
                monkeypatch.setattr(controller_module.skills, "discover", unreadable)
                ctl.send_prompt("new request")
                await finish_turn(ctl)
                assert (ctl.saved.items, ctl.saved.turns) == before
            finally:
                await engine.close_all()
    asyncio.run(scenario())


@pytest.mark.parametrize("pattern", ["OUTSIDE_SENTINEL", "OUTSIDE_SENTINE[L]"])
def test_implicit_grep_does_not_follow_a_file_link_out_of_the_approved_root(tmp_path, pattern):
    project = tmp_path / "project"
    project.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("OUTSIDE_SENTINEL", encoding="utf-8")
    link = project / "linked.txt"
    try:
        os.symlink(outside, link)
    except (OSError, NotImplementedError):
        pytest.skip("file symlinks are unavailable on this platform")
    ctx, registry = ToolContext(project), ToolRegistry.default()
    args = {"pattern": pattern, "output_mode": "content"}
    assert decide("code", registry.get("grep"), args, ctx, remembered=set()).verdict == "allow"
    result = asyncio.run(registry.run("grep", args, ctx))
    assert not result.is_error and result.output == "No matches found."
    direct = {**args, "path": str(link)}
    assert decide("code", registry.get("grep"), direct, ctx, remembered=set()).verdict == "ask"
    assert "OUTSIDE_SENTINEL" in asyncio.run(registry.run("grep", direct, ctx)).output, "an explicitly approved path still works"


def test_a_walk_skips_file_link_metadata_even_without_symlink_creation_privileges(tmp_path, monkeypatch):
    from contextlib import nullcontext
    from desktop.core.harness import fs_tools
    (tmp_path / "linked.txt").write_text("example", encoding="utf-8")
    with os.scandir(tmp_path) as entries:
        entry = next(entries)
    class FileLink:
        def is_symlink(self):
            return True
        def is_file(self, **kwargs):
            raise AssertionError("a discovered link's target must not be inspected")
        def __getattr__(self, name):
            return getattr(entry, name)
    monkeypatch.setattr(fs_tools.os, "scandir", lambda path: nullcontext([FileLink()]))
    assert list(fs_tools._walk_files(str(tmp_path), "", frozenset(), lambda: False, [])) == []


def test_an_implicit_glob_does_not_enter_a_directory_link_but_an_explicit_approved_path_can(tmp_path):
    from tests.test_harness_security_links import make_dir_link
    project, outside = tmp_path / "project", tmp_path / "outside"
    project.mkdir()
    outside.mkdir()
    (outside / "outside.txt").write_text("example", encoding="utf-8")
    make_dir_link(project / "linked", outside)
    (project / "src").mkdir()
    make_dir_link(project / "src" / "portal", outside)
    ctx, registry = ToolContext(project), ToolRegistry.default()
    args = {"patterns": ["*/**/*.txt", "src*/portal/**/*.txt"]}
    assert decide("code", registry.get("glob"), args, ctx, remembered=set()).verdict == "allow"
    assert asyncio.run(registry.run("glob", args, ctx)).output == "No files matched."
    explicit = {"patterns": ["linked/**/*.txt"]}
    assert decide("code", registry.get("glob"), explicit, ctx, remembered=set()).verdict == "ask"
    assert "outside.txt" in asyncio.run(registry.run("glob", explicit, ctx)).output


def test_a_prefix_summary_keeps_the_running_turns_empty_reasoning_choice(tmp_path, monkeypatch):
    async def scenario():
        with FakeResponses() as fake:
            engine, ctl = await opened(tmp_path, fake)
            try:
                await ctl._ensure_tools()
                ctl.saved.prompt = {"instructions": "stable", "current": "stable"}
                owner = ctl.transcript.start_turn("summary")
                ctl._turn, ctl._turn_model, ctl._turn_reasoning = owner, ctl.model, ""
                ctl.reasoning = "high"  # selected for the next turn, not this one
                received = []
                async def fake_compact(*args, **kwargs):
                    received.append(kwargs["prefix_request"])
                    return None
                monkeypatch.setattr(compact, "compact", fake_compact)
                await ctl._compact(owner, [user_message("previous")], 100, False)
                assert not received[0].get("reasoning", {}).get("effort")
            finally:
                ctl._turn = None
                await engine.close_all()
    asyncio.run(scenario())


def test_an_expired_summary_does_not_charge_or_change_the_next_turn(tmp_path, monkeypatch):
    async def scenario():
        with FakeResponses() as fake:
            engine, ctl = await opened(tmp_path, fake)
            try:
                owner = ctl.transcript.start_turn("summary")
                ctl._turn, ctl._turn_model = owner, ctl.model
                before = ctl.usage
                async def fake_compact(*args, **kwargs):
                    ctl._turn = ctl.transcript.start_turn("next turn")
                    ctl._turn_model = "gpt-6-luna"
                    ctl.transcript.set_working_state("executing_tool")
                    kwargs["on_usage"](Usage(input_tokens=100, output_tokens=10))
                    return None
                monkeypatch.setattr(compact, "compact", fake_compact)
                await ctl._compact(owner, [user_message("previous")], 100, False)
                assert ctl.usage == before
                assert ctl.transcript.working_state == "executing_tool"
            finally:
                ctl._turn = None
                await engine.close_all()
    asyncio.run(scenario())
