"""Protocol termination, goal completion and durable execution gates (local fake services only)."""
from __future__ import annotations

import asyncio
import errno
from pathlib import Path

import pytest

from desktop.core.chatgpt import http
from desktop.core.chatgpt.responses import Finished, read_events
from tests.app_support import ready_core
from tests.chatgpt_support import finish_turn, make_engine, until
from tests.fake_responses import FakeResponses, call, cut, say
from tests.test_app_usage_models import new


@pytest.mark.parametrize("event", ["response.completed", "response.incomplete"])
def test_terminal_event_does_not_consume_a_late_transport_error(event):
    async def go():
        async def lines():
            yield 'data: {"type": "' + event + '", "response": {}}'
            yield ""
            raise AssertionError("the terminal response must not wait for EOF")
        events = [item async for item in read_events(lines())]
        assert len(events) == 1 and isinstance(events[0], Finished)
    asyncio.run(go())


@pytest.mark.parametrize("kind", ["text", "tool"])
def test_completed_response_closes_connection_without_recovery(tmp_path, monkeypatch, kind):
    async def go():
        monkeypatch.setattr(http, "STREAM_IDLE_SECONDS", 0.15)
        (tmp_path / "project").mkdir()
        with FakeResponses() as fake:
            complete = say("Finished.") if kind == "text" else call("create_file", {"file_path": "acceptance.txt", "content": "PASS"})
            fake.script([*complete, ("hold", "connection")], say("Verified."))
            engine = make_engine(tmp_path, fake)
            ctl = await engine.open_new(tmp_path / "project", model="gpt-6.1-sol", reasoning="low")
            try:
                ctl.send_prompt("Do the demo task")
                await finish_turn(ctl)
                assert len(fake.requests) == (1 if kind == "text" else 2)
                assert (tmp_path / "project" / "acceptance.txt").exists() == (kind == "tool")
                assert all(not entry.get("failed") for entry in ctl.saved.requests)
                assert not any("transport recovery" in str(r) for r in fake.requests)
            finally:
                fake.release("connection")
                await engine.close_all()
    asyncio.run(go())


@pytest.mark.parametrize("ending", ["content_filter", "max_output_tokens", "step_limit"])
def test_goal_never_accepts_a_marker_from_an_abnormal_turn(tmp_path, ending):
    async def go():
        core, plan, events, _, engine = await ready_core(tmp_path)
        try:
            cid = await new(core, events, tmp_path)
            if ending == "step_limit":
                core.settings.max_steps = 1
                answer = call("read_file", {"file_path": "missing-demo.txt"}, text="GOAL: DONE")
            else:
                answer = cut("GOAL: DONE", reason=ending)
            plan.script(answer)
            assert (await core.call("session.goal", {"id": cid, "text": "Verify demo"}))["ok"]
            conv = core._by_cid[cid]
            await until(lambda: conv.goal is None)
            assert conv.controller.saved.goal["rounds"] == 1
            assert conv.controller.saved.turns[-1].status in ("limit", "incomplete")
            assert len(plan.requests) == 1
            assert not any("目标完成了" in getattr(block, "text", "") for block in conv.controller.transcript.turns[-1].blocks)
        finally:
            await core.shutdown()
    asyncio.run(go())


def test_content_filter_does_not_restart_the_goal(tmp_path):
    async def go():
        core, plan, events, _, _ = await ready_core(tmp_path)
        try:
            cid = await new(core, events, tmp_path)
            plan.script(cut("Filtered.", reason="content_filter"))
            await core.call("session.goal", {"id": cid, "text": "Verify demo"})
            conv = core._by_cid[cid]
            await until(lambda: conv.goal is None)
            assert len(plan.requests) == 1 and conv.controller.saved.goal
        finally:
            await core.shutdown()
    asyncio.run(go())


def test_goal_and_running_input_are_saved_before_the_first_answer(tmp_path):
    async def go():
        core, plan, events, _, engine = await ready_core(tmp_path)
        try:
            cid = await new(core, events, tmp_path)
            plan.script([{"type": "response.created", "response": {}}, ("hold", "answer"), *say("GOAL: DONE")])
            await core.call("session.goal", {"id": cid, "text": "Verify demo"})
            ctl = core._by_cid[cid].controller
            await until(lambda: len(plan.requests) == 1)
            saved = engine.sessions.load(ctl.session_id)
            assert saved.goal["text"] == "Verify demo"
            assert saved.turns[-1].status == "running" and saved.items
            plan.release("answer")
            await finish_turn(ctl)
        finally:
            await core.shutdown()
    asyncio.run(go())


@pytest.mark.parametrize("failure", [errno.ENOSPC, errno.EACCES])
def test_failed_tool_checkpoint_prevents_the_write_and_can_recover(tmp_path, monkeypatch, failure):
    async def go():
        (tmp_path / "project").mkdir()
        with FakeResponses() as fake:
            fake.script(call("create_file", {"file_path": "acceptance.txt", "content": "PASS"}))
            engine = make_engine(tmp_path, fake)
            ctl = await engine.open_new(tmp_path / "project", model="gpt-6.1-sol")
            save = engine.sessions.save
            def broken(saved):
                if any(item.get("type") == "function_call" for item in saved.items):
                    raise OSError(failure, "demo storage failure")
                save(saved)
            monkeypatch.setattr(engine.sessions, "save", broken)
            try:
                ctl.send_prompt("Create acceptance.txt")
                await finish_turn(ctl)
                assert not (tmp_path / "project" / "acceptance.txt").exists()
                assert len(fake.requests) == 1
                assert ctl.saved.turns[-1].status == "failed"
                assert any("保存失败" in getattr(block, "text", "") for block in ctl.transcript.turns[-1].blocks)
                monkeypatch.setattr(engine.sessions, "save", save)
                fake.script(call("create_file", {"file_path": "acceptance.txt", "content": "PASS"}), say("Verified."))
                ctl.send_prompt("Storage is available; create acceptance.txt")
                await finish_turn(ctl)
                assert (tmp_path / "project" / "acceptance.txt").read_text() == "PASS"
            finally:
                await engine.close_all()
    asyncio.run(go())


def test_a_reopened_goal_only_checkpoint_can_be_cleared(tmp_path):
    async def go():
        with FakeResponses() as fake:
            engine = make_engine(tmp_path, fake)
            ctl = await engine.open_new(tmp_path / "project", model="gpt-6.1-sol")
            ctl.saved.goal = {"text": "Verify demo", "rounds": 0}
            assert ctl._save_now()
            sid = ctl.session_id
            await ctl.close()
            reopened = await engine.open_saved(tmp_path / "project", sid)
            assert reopened.saved.goal and not reopened.saved.turns
            reopened.saved.goal = {}
            assert reopened._save_now()
            assert engine.sessions.load(sid).goal == {} and not fake.requests
            await engine.close_all()
    asyncio.run(go())


def test_failed_first_checkpoint_never_sends_a_model_request(tmp_path, monkeypatch):
    async def go():
        with FakeResponses() as fake:
            engine = make_engine(tmp_path, fake)
            ctl = await engine.open_new(tmp_path / "project", model="gpt-6.1-sol")
            def broken(saved):
                raise OSError(errno.ENOSPC, "demo storage failure")
            monkeypatch.setattr(engine.sessions, "save", broken)
            try:
                ctl.send_prompt("Do not start without a checkpoint")
                await finish_turn(ctl)
                assert not fake.requests
                assert any("保存失败" in getattr(block, "text", "") for block in ctl.transcript.turns[-1].blocks)
            finally:
                await engine.close_all()
    asyncio.run(go())
