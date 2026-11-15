"""Completion belongs to the entire conversation run, not its tool/plan steps, goal rounds or queued turns."""

import asyncio
from pathlib import Path

import pytest

from tests.app_support import ready_core
from tests.fake_responses import call, fail, say
from tests.fakes import until
from tests.test_app_core import idle, new_session


def played(platform):
    return [Path(path).stem for path in platform.played_sounds]


def test_tools_and_completed_plan_substeps_remain_silent_until_the_final_answer(tmp_path):
    async def scenario():
        core, plan, events, platform, _ = await ready_core(tmp_path)
        try:
            cid = await new_session(core, events, tmp_path / "proj")
            (tmp_path / "proj/demo.txt").write_text("Demo input.", encoding="utf-8")
            plan.script(
                call("todo_write", {"todos": [{"content": "Read the file", "status": "completed"}, {"content": "Summarize", "status": "in_progress"}]}),
                call("read_file", {"file_path": "demo.txt"}),
                [("hold", "final-answer"), *say("The whole request is complete.")],
            )
            await core.call("session.send", {"id": cid, "text": "Read and summarize the file."})
            await until(lambda: len(plan.requests) == 3)
            for _ in range(3):
                core._by_cid[cid].controller._changed()
                await asyncio.sleep(0)
            assert played(platform) == []
            plan.release("final-answer")
            await idle(core, cid)
            await until(lambda: played(platform) == ["complete"])
            core._by_cid[cid].controller._changed()
            await core.call("app.init")
            await asyncio.sleep(0)
            assert played(platform) == ["complete"]
        finally:
            await core.shutdown()
    asyncio.run(scenario())


@pytest.mark.parametrize("ending,expected", [("GOAL: DONE", ["complete"]), ("GOAL: BLOCKED Need a decision.", []), ("Still not finished.", [])])
def test_goal_rounds_do_not_sound_until_the_whole_goal_is_done(tmp_path, ending, expected):
    async def scenario():
        core, plan, events, platform, _ = await ready_core(tmp_path)
        try:
            cid = await new_session(core, events, tmp_path / "proj")
            plan.script(say("The first subtask is complete, but more work remains."), [("hold", "goal-final"), *say(ending)])
            assert (await core.call("session.goal", {"id": cid, "text": "Complete the entire demo task."}))["ok"]
            await until(lambda: len(plan.requests) == 2)
            assert core._by_cid[cid].goal is not None
            assert played(platform) == [], "the first goal round is not the end of the user's run"
            plan.release("goal-final")
            await until(lambda: core._by_cid[cid].goal is None)
            await idle(core, cid)
            await asyncio.sleep(0)
            assert played(platform) == expected
        finally:
            await core.shutdown()
    asyncio.run(scenario())


@pytest.mark.parametrize("ending,expected", [("success", ["complete"]), ("failure", ["error"]), ("stop", [])])
def test_queued_turns_form_one_completion_boundary(tmp_path, ending, expected):
    async def scenario():
        core, plan, events, platform, _ = await ready_core(tmp_path)
        try:
            cid = await new_session(core, events, tmp_path / "proj")
            last = fail("bad_request", "Expected demo failure") if ending == "failure" else say("All queued work is finished.")
            plan.script([("hold", "first"), *say("First part finished.")], [("hold", "last"), *last])
            await core.call("session.send", {"id": cid, "text": "First part"})
            await until(lambda: len(plan.requests) == 1)
            assert (await core.call("session.send", {"id": cid, "text": "Second part"}))["queued"]
            plan.release("first")
            await until(lambda: len(plan.requests) == 2)
            assert played(platform) == [], "draining the next message must precede completion notification"
            if ending == "stop":
                await core.call("session.stop", {"id": cid})
            plan.release("last")
            await idle(core, cid)
            await asyncio.sleep(0)
            assert played(platform) == expected
        finally:
            await core.shutdown()
    asyncio.run(scenario())


@pytest.mark.parametrize("ending", ["incomplete", "step-limit"])
def test_unfinished_turns_are_not_announced_as_completed(tmp_path, ending):
    async def scenario():
        core, plan, events, platform, _ = await ready_core(tmp_path)
        try:
            cid = await new_session(core, events, tmp_path / "proj")
            if ending == "incomplete":
                answer = say("A partial result.")
                answer[-1]["type"] = "response.incomplete"
                answer[-1]["response"].update(status="incomplete", incomplete_details={"reason": "content_filter"})
            else:
                core.settings.max_steps = 1
                (tmp_path / "proj/demo.txt").write_text("Demo input.", encoding="utf-8")
                answer = call("read_file", {"file_path": "demo.txt"})
            plan.script(answer)
            await core.call("session.send", {"id": cid, "text": "Do the whole task."})
            await idle(core, cid)
            await asyncio.sleep(0)
            assert core._by_cid[cid].controller.saved.turns[-1].status in ("incomplete", "limit")
            assert played(platform) == []
        finally:
            await core.shutdown()
    asyncio.run(scenario())
