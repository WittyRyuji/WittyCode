"""The saved timeout pair, session policy and actual expert deadline agree."""
from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

from desktop.core.app_core import AppError
from desktop.core.fusion.registry import Policy
from desktop.core.fusion.step_budget import MAX_SECONDS
from tests.app_support import ready_core
from tests.fake_responses import call, say
from tests.test_fusion_identity_runtime import LIGHT, MAIN, WORKER, _brief, _fusion, _turn
from tests.test_fusion_planned_budget import _brief as budget_brief, _runtime

pytestmark = pytest.mark.fusion_gates


def test_timeout_setting_validates_atomically_and_new_session_receives_it(tmp_path):
    async def go():
        core, _, events, _, _ = await ready_core(tmp_path)
        try:
            core.settings.fusion_main, core.settings.fusion_workers, core.settings.fusion_light = MAIN, [WORKER], LIGHT
            async def save(value):
                return await core.call("settings.set", {"key": "fusionMaxSeconds", "value": value})
            assert core.settings_view()["fusionMaxSecondsAdaptive"]
            assert not (await save(7))["fusionMaxSecondsAdaptive"], "legacy numeric updates are manual"
            cid = (await core.call("session.new", {"projectId": str(tmp_path / "proj"), "fusion": True}))["id"]
            runtime = core._by_cid[cid].controller._fusion
            policy = runtime.registry.policy
            assert policy.max_seconds == 7 and not policy.max_seconds_adaptive
            assert (await save({"seconds": 7, "adaptive": True}))["fusionMaxSecondsAdaptive"]
            assert events.last("settings")["settings"]["fusionMaxSeconds"] == 7
            assert runtime.pending_settings() == replace(policy, max_seconds_adaptive=True)
            for bad in (True, 86401, {"seconds": 0, "adaptive": False}, {"seconds": True, "adaptive": False},
                        {"seconds": 7, "adaptive": "yes"}, {"adaptive": True}, {"adaptive": True, "seconds": 9, "extra": 0}):
                with pytest.raises(AppError):
                    await save(bad)
            assert core.settings.fusion_max_seconds == 7 and core.settings.fusion_max_seconds_adaptive
            assert Policy.from_json(runtime.pending_settings().to_json()) == runtime.pending_settings()
            old = policy.to_json()
            del old["maxSecondsAdaptive"]
            assert Policy.from_json(old) == policy
        finally:
            await core.shutdown()
    asyncio.run(go())


def test_adaptive_defaults_scale_but_cannot_override_plan_or_safety_cap(tmp_path):
    rt = _runtime(tmp_path, max_seconds=1)
    rt.registry.policy = replace(rt.registry.policy, max_seconds_adaptive=True)
    small = rt._bounded_brief(budget_brief(steps=20)).budget
    larger = rt._bounded_brief(budget_brief(steps=40)).budget
    assert 1 < small["seconds"] < larger["seconds"]
    assert rt._bounded_brief(budget_brief(seconds=10**6)).budget["seconds"] == MAX_SECONDS
    rt = _runtime(tmp_path, max_seconds=1, plan_budget={"seconds": 3})
    rt.registry.policy = replace(rt.registry.policy, max_seconds_adaptive=True)
    assert rt._bounded_brief(budget_brief()).budget["seconds"] == 3


@pytest.mark.parametrize("adaptive", [False, True])
def test_remembered_one_second_is_a_real_deadline_only_in_manual_mode(tmp_path, adaptive):
    async def go():
        async with _fusion(tmp_path, max_seconds=1) as (main, fake):
            rt = main._fusion
            rt.registry.policy = replace(rt.registry.policy, max_seconds_adaptive=adaptive)
            await _turn(main, fake, call("delegate_task", _brief(budget={"seconds": 5})),
                        [("sleep", 1.3), *say("Finished the check.")], say("Reviewed."))
            worker, = main.agents.values()
            assert worker.record.status == ("done" if adaptive else "interrupted")
            assert ("5 seconds" if adaptive else "1 second") in worker.record.task
    asyncio.run(go())
