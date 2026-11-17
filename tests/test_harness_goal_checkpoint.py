"""Goal checkpoints survive an AppCore restart, but never restart unattended work on their own."""
from __future__ import annotations

import asyncio

from desktop.core.harness.goal import Goal
from desktop.core.session_state import SessionState
from tests.app_support import ready_core
from tests.fake_responses import say
from tests.fakes import until
from tests.test_app_usage_models import new


def test_goal_is_saved_paused_and_only_resumes_when_explicitly_requested(tmp_path):
    async def go():
        core, plan, events, _, engine = await ready_core(tmp_path)
        cid = await new(core, events, tmp_path)
        try:
            plan.script(say("Need a decision.\nGOAL: BLOCKED choose a target"))
            assert (await core.call("session.goal", {"id": cid, "text": "finish the demo"}))["ok"]
            conv = core._by_cid[cid]
            await until(lambda: conv.goal is None and bool(conv.controller.saved.goal))
            native = conv.controller.session_id
            assert Goal.from_json(engine.sessions.load(native).goal) == Goal("finish the demo", 1, 0)
        finally:
            await core.shutdown()
        again, next_plan, next_events, _, next_engine = await ready_core(tmp_path)
        try:
            reopened = (await again.call("session.openSaved", {"projectId": str(tmp_path / "proj"), "id": "cg:" + native}))["id"]
            conv = again._by_cid[reopened]
            await until(lambda: conv.controller.state is SessionState.READY)
            await asyncio.sleep(.1)
            assert conv.goal is None and next_plan.requests == [], "opening must not run a saved goal"
            next_plan.script(say("Verified.\nGOAL: DONE"))
            assert (await again.call("session.goal", {"id": reopened, "text": "resume"}))["ok"]
            await until(lambda: conv.goal is None and not conv.controller.saved.goal)
            assert "finish the demo" in str(next_plan.requests[0]["input"])
            assert "第 2/20 轮" in str(next_plan.requests[0]["input"])
            assert not next_engine.sessions.load(native).goal
        finally:
            await again.shutdown()
    asyncio.run(go())


def test_clearing_a_paused_checkpoint_does_not_call_a_model(tmp_path):
    async def go():
        core, plan, events, _, _ = await ready_core(tmp_path)
        cid = await new(core, events, tmp_path)
        try:
            ctl = core._by_cid[cid].controller
            ctl.saved.goal = Goal("demo", 3, 1).to_json()
            assert (await core.call("session.goal", {"id": cid, "text": "off"}))["ok"]
            assert not ctl.saved.goal and not plan.requests
            assert not (await core.call("session.goal", {"id": cid, "text": "resume"}))["ok"]
        finally:
            await core.shutdown()
    asyncio.run(go())
