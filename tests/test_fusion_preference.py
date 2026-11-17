"""Fusion's model-selection preference is session-scoped, without new-session budget caps."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from desktop.core.app_core import AppError
from desktop.core.fusion.registry import FusionError, FusionRegistry, Policy
from desktop.core.fusion.runtime import FusionRuntime
from desktop.core.settings import settings_from_dict, settings_to_dict
from tests.app_support import ready_core
from tests.chatgpt_support import finish_turn
from tests.fake_responses import say


def test_unlimited_calls_and_preference_round_trip(tmp_path):
    policy = Policy("chatgpt:main", ("chatgpt:worker",), "chatgpt:small", 0, 0, 60, 75)
    registry = FusionRegistry(tmp_path, policy)
    expert = registry.assign("src", "chatgpt:worker", "worker")
    for _ in range(10):
        registry.begin(expert.id)
        registry.finish(expert.id, status="done", tokens=None)
    restored = FusionRegistry.from_json(tmp_path, json.loads(json.dumps(registry.to_json())))
    assert restored.policy.preference == 75 and restored.calls == 10
    parent = SimpleNamespace(saved=SimpleNamespace(plan={}, goal={}))
    snapshot = json.loads(FusionRuntime(parent, restored).context_snapshot())
    assert snapshot["max_calls"] is None and snapshot["max_tokens"] is None
    restored.begin(expert.id)  # unknown usage does not impose a hidden budget
    legacy = policy.to_json()
    del legacy["preference"]
    assert Policy.from_json(legacy).preference == 50
    for invalid in (-1, 101, True, 1.5, "50"):
        with pytest.raises(FusionError):
            Policy("chatgpt:main", ("chatgpt:worker",), "chatgpt:small", 0, 0, 60, invalid)


@pytest.mark.parametrize(("preference", "cost", "efficiency"), [(0, "1.50", "0.50"), (50, "1.00", "1.00"), (100, "0.50", "1.50")])
def test_preference_weights_are_guidance_to_the_main_agent(tmp_path, preference, cost, efficiency):
    policy = Policy("chatgpt:main", ("chatgpt:worker",), "chatgpt:small", 0, 0, 60, preference)
    runtime = FusionRuntime(None, FusionRegistry(tmp_path, policy))
    instructions = runtime.instructions()
    assert f"cost x{cost}" in instructions and f"efficiency x{efficiency}" in instructions
    assert "not prices" in instructions and "Decide the number of calls" in instructions


def test_invalid_saved_preferences_do_not_become_instructions():
    for value in (-1, 101, True, None, 1.5, "50"):
        assert settings_from_dict({"fusion_preference": value}).fusion_preference == 50


def test_new_sessions_snapshot_preference_and_ignore_legacy_default_budgets(tmp_path):
    async def go():
        core, model, _, _, _ = await ready_core(tmp_path)
        try:
            values = {"fusionMain": "chatgpt:gpt-6.1-sol", "fusionWorkers": ["chatgpt:gpt-6-luna"],
                      "fusionLight": "chatgpt:gpt-6-luna", "fusionPreference": 80}
            # These older settings remain readable, but cannot impose hidden limits on new Fusion sessions.
            core.settings.fusion_max_calls, core.settings.fusion_max_tokens = 1, 100
            for key, value in values.items():
                await core.call("settings.set", {"key": key, "value": value})
            for invalid in (-1, 101, True, 1.5, "50"):
                with pytest.raises(AppError):
                    await core.call("settings.set", {"key": "fusionPreference", "value": invalid})
            restored = settings_from_dict(settings_to_dict(core.settings))
            assert restored.fusion_preference == 80
            cid = (await core.call("session.new", {"projectId": str(tmp_path / "proj"), "fusion": True}))["id"]
            main = core._by_cid[cid].controller
            policy = main._fusion.registry.policy
            assert (policy.max_calls, policy.max_tokens, policy.preference) == (0, 0, 80)
            await core.call("settings.set", {"key": "fusionPreference", "value": 10})
            model.script(say("Done."))
            await core.call("session.send", {"id": cid, "text": "Inspect"})
            await finish_turn(main)
            instructions = model.requests[0]["instructions"]
            assert "0.70" in instructions and "1.30" in instructions
            assert "light" in instructions.lower() and "worker" in instructions.lower()
            assert main._fusion.registry.policy.preference == 80
            native = main.session_id
            await main.close()
            reopened = await main.manager.open_saved(tmp_path / "proj", native)
            assert reopened._fusion.registry.policy.preference == 80
            assert "0.70" in reopened._fusion.instructions()
        finally:
            await core.shutdown()
    asyncio.run(go())
