"""Fusion efforts are role/model-scoped, validated, and frozen with each new conversation."""
from __future__ import annotations

import asyncio
import json

import pytest

from desktop.core.app_core import AppError
from desktop.core.fusion.registry import FusionError, Policy
from desktop.core.fusion.reasoning import configured_effort
from desktop.core.llm.backend import ApiBackend
from desktop.core.settings import load_settings, settings_from_dict, settings_to_dict
from tests.app_support import MODEL, OTHER_MODEL, ready_core
from tests.chatgpt_support import finish_turn
from tests.fake_responses import call, say
from tests.llm_support import FakeApi
from tests.test_llm_backend import model_id_of, settings_with, store_for
from tests.test_reasoning_args import answer


INVALID_MAPS = [None, [], "high", True, {MODEL: ""}, {MODEL: False}, {MODEL: {}},
                {"factory:demo": "high"}, {"router:" + MODEL: "high"}, {"worker:factory:demo": "high"},
                {"main:" + MODEL: ""}, {MODEL: "a" * 65}, {"api:" + "a" * 513: "low"},
                {f"chatgpt:demo-{index}": "low" for index in range(257)}]


def test_global_worker_setting_is_single_while_existing_session_pools_remain_readable(tmp_path):
    assert settings_from_dict({"fusion_workers": [MODEL, OTHER_MODEL]}).fusion_workers == [MODEL]
    assert settings_from_dict({"fusion_workers": []}).fusion_workers == []
    policy = Policy(MODEL, (MODEL, OTHER_MODEL), OTHER_MODEL, 0)
    assert Policy.from_json(policy.to_json()).workers == (MODEL, OTHER_MODEL)

    async def go():
        core, _, _, _, _ = await ready_core(tmp_path)
        try:
            for selected in ([MODEL], [MODEL], [OTHER_MODEL]):
                await core.call("settings.set", {"key": "fusionWorkers", "value": selected})
                assert core.settings.fusion_workers == selected
            with pytest.raises(AppError):
                await core.call("settings.set", {"key": "fusionWorkers", "value": [MODEL, OTHER_MODEL]})
            assert core.settings.fusion_workers == [OTHER_MODEL]
        finally:
            await core.shutdown()
    asyncio.run(go())


def test_settings_and_policy_copy_and_round_trip_per_model_efforts():
    levels = {MODEL: "high", OTHER_MODEL: "low", "api:demo:small": "off", "devin:demo": "dynamic"}
    restored = settings_from_dict({"fusion_reasoning": levels})
    policy = Policy(MODEL, (OTHER_MODEL,), OTHER_MODEL, 0, reasoning=levels)
    levels[MODEL] = "low"
    assert restored.fusion_reasoning[MODEL] == policy.reasoning[MODEL] == "high"
    assert settings_from_dict(settings_to_dict(restored)).fusion_reasoning == policy.reasoning
    assert Policy.from_json(json.loads(json.dumps(policy.to_json()))).reasoning == policy.reasoning
    view = policy.to_json()
    view["reasoning"][MODEL] = "low"
    assert policy.reasoning[MODEL] == "high"
    del view["reasoning"]
    assert Policy.from_json(view).reasoning == {}
    assert settings_from_dict({}).fusion_reasoning == {}


@pytest.mark.parametrize("value", INVALID_MAPS)
def test_invalid_saved_efforts_do_not_reach_a_policy(value):
    assert settings_from_dict({"fusion_reasoning": value}).fusion_reasoning == {}
    with pytest.raises(FusionError):
        Policy(MODEL, (OTHER_MODEL,), OTHER_MODEL, 0, reasoning=value)


def test_setting_validates_supported_levels_before_saving_and_can_clear(tmp_path, monkeypatch):
    path = tmp_path / "settings.json"
    monkeypatch.setenv("WITTYCODE_SETTINGS", str(path))

    async def go():
        core, service, _, _, _ = await ready_core(tmp_path)
        try:
            core.persist = True
            levels = {MODEL: "high", OTHER_MODEL: "low"}
            view = await core.call("settings.set", {"key": "fusionReasoning", "value": levels})
            assert view["fusionReasoning"] == load_settings(path).fusion_reasoning == levels
            before = path.read_bytes()
            for invalid in [*INVALID_MAPS, {MODEL: "ultra"}, {OTHER_MODEL: "high"}, {"chatgpt:missing": "low"}]:
                with pytest.raises(AppError):
                    await core.call("settings.set", {"key": "fusionReasoning", "value": invalid})
                assert path.read_bytes() == before and core.settings.fusion_reasoning == levels
            # An unrelated edit can retain an older model that is temporarily missing from the catalogue.
            core.settings.fusion_reasoning["api:missing:old"] = "max"
            await core.call("settings.set", {"key": "fusionReasoning", "value": {**core.settings.fusion_reasoning, MODEL: "low"}})
            assert load_settings(path).fusion_reasoning["api:missing:old"] == "max"
            await core.call("settings.set", {"key": "fusionReasoning", "value": {}})
            assert load_settings(path).fusion_reasoning == {} and not service.requests
        finally:
            await core.shutdown()
    asyncio.run(go())


def test_main_workers_and_light_use_their_snapshot_on_requests_and_after_reopening(tmp_path):
    async def go():
        with FakeApi() as api:
            store = store_for(settings_with(tmp_path, api.base_url, apiKey=None, reasoningEffort="high",
                                           reasoningArgs={"off": {"thinking": {"type": "disabled"}}}))
            api_id = model_id_of(store)
            core, service, _, _, engine = await ready_core(tmp_path, providers=store)
            engine.api = ApiBackend(store)
            try:
                levels = {MODEL: "high", OTHER_MODEL: "low", api_id: "off"}
                for key, value in {"fusionMain": MODEL, "fusionWorkers": [OTHER_MODEL], "fusionLight": api_id,
                                   "fusionReasoning": levels}.items():
                    await core.call("settings.set", {"key": key, "value": value})
                cid = (await core.call("session.new", {"projectId": str(tmp_path / "proj"), "fusion": True}))["id"]
                main = core._by_cid[cid].controller
                assert main.reasoning == "high"  # Not the regular new-session default (medium).
                await core.call("settings.set", {"key": "fusionReasoning", "value": {MODEL: "low", api_id: "max"}})
                assert main._fusion.registry.policy.reasoning == levels
                worker = {"scope": "src", "model": OTHER_MODEL, "role": "worker", "task": "Inspect."}
                light = {"scope": "docs", "model": api_id, "role": "light", "task": "Inspect."}
                service.script(call("delegate_task", worker, call_id="worker"), say("Worker done."),
                               call("delegate_task", light, call_id="light"), say("Reviewed."))
                api.script(answer("generic-chat-completion-api"))
                assert main.send_prompt("Inspect both modules")
                await finish_turn(main)
                assert [request["reasoning"]["effort"] for request in service.requests] == ["high", "low", "high", "high"]
                assert api.posts[0][2]["thinking"] == {"type": "disabled"}
                assert "reasoning_effort" not in api.posts[0][2]
                assert {view.record.model: view.agent_effort for view in main.agents.values()} == {OTHER_MODEL: "low", api_id: "off"}
                native = main.session_id
                await main.close()
                reopened = await engine.open_saved(tmp_path / "proj", native)
                assert reopened.reasoning == "high" and reopened._fusion.registry.policy.reasoning == levels
                service.script(call("delegate_task", worker, call_id="worker-again"), say("Worker continued."),
                               call("delegate_task", light, call_id="light-again"), say("Reviewed again."))
                api.script(answer("generic-chat-completion-api"))
                assert reopened.send_prompt("Continue")
                await finish_turn(reopened)
                assert [request["reasoning"]["effort"] for request in service.requests[4:]] == ["high", "low", "high", "high"]
                assert api.posts[-1][2]["thinking"] == {"type": "disabled"} and len(reopened.agents) == 2
                assert reopened._fusion.change_main(OTHER_MODEL, "high") and reopened.reasoning == "low"
                assert reopened._fusion.change_main(MODEL, "low") and reopened.reasoning == "high"
                normal_id = (await core.call("session.new", {"projectId": str(tmp_path / "proj")}))["id"]
                assert core._by_cid[normal_id].controller.reasoning == "medium"
            finally:
                await core.shutdown()
    asyncio.run(go())


def test_same_model_uses_independent_role_efforts_on_requests_and_after_reopening(tmp_path):
    async def go():
        core, service, _, _, engine = await ready_core(tmp_path)
        try:
            levels = {f"{role}:{MODEL}": level for role, level in [("main", "high"), ("worker", "medium"), ("light", "low")]}
            for key, value in {"fusionMain": MODEL, "fusionWorkers": [MODEL], "fusionLight": MODEL, "fusionReasoning": levels}.items():
                await core.call("settings.set", {"key": key, "value": value})
            assert settings_from_dict(settings_to_dict(core.settings)).fusion_reasoning == levels
            cid = (await core.call("session.new", {"projectId": str(tmp_path / "proj"), "fusion": True}))["id"]
            main = core._by_cid[cid].controller
            native = main.session_id
            for reopen in (False, True):
                if reopen:
                    await main.close()
                    main = await engine.open_saved(tmp_path / "proj", native)
                before = len(service.requests)
                service.script(call("delegate_task", {"scope": "src", "model": MODEL, "role": "worker", "task": "Inspect."}),
                               say("Worker done."),
                               call("delegate_task", {"scope": "docs", "model": MODEL, "role": "light", "task": "Summarise."}),
                               say("Light done."), say("Reviewed."))
                assert main.send_prompt("Inspect the modules")
                await finish_turn(main)
                assert [item["reasoning"]["effort"] for item in service.requests[before:]] == ["high", "medium", "high", "low", "high"]
                assert {view.record.role: view.agent_effort for view in main.agents.values()} == {"worker": "medium", "light": "low"}
                assert main._fusion.registry.policy.reasoning == levels
            await core.call("settings.set", {"key": "fusionReasoning", "value": {f"main:{MODEL}": "low"}})
            assert main._fusion.registry.policy.reasoning == levels  # Existing sessions retain their snapshot.
            with pytest.raises(AppError):
                await core.call("settings.set", {"key": "fusionReasoning", "value": {f"light:{OTHER_MODEL}": "high"}})
            assert configured_effort({MODEL: "high", f"worker:{MODEL}": "low"}, MODEL, ("low", "high"), role="worker") == "low"
            assert configured_effort({MODEL: "high"}, MODEL, ("low", "high"), role="light") == "high"
        finally:
            await core.shutdown()
    asyncio.run(go())


def test_stale_main_or_expert_effort_is_not_silently_replaced_or_sent(tmp_path):
    async def go():
        core, service, _, _, _ = await ready_core(tmp_path)
        try:
            core.settings.fusion_main = OTHER_MODEL
            core.settings.fusion_workers = [OTHER_MODEL]
            core.settings.fusion_light = OTHER_MODEL
            core.settings.fusion_reasoning = {OTHER_MODEL: "high"}  # Simulate an outdated saved setting.
            with pytest.raises(AppError, match="思考等级"):
                await core.call("session.new", {"projectId": str(tmp_path / "proj"), "fusion": True})
            assert not core.order and not service.requests
            core.settings.fusion_main = MODEL
            cid = (await core.call("session.new", {"projectId": str(tmp_path / "proj"), "fusion": True}))["id"]
            main = core._by_cid[cid].controller
            outcome = await main._fusion.delegate("src", OTHER_MODEL, "worker", "Inspect.")
            assert outcome.is_error and "reasoning level" in outcome.output and not service.requests
            assert all(view.controller is None for view in main.agents.values())
            with pytest.raises(FusionError, match="reasoning level"):
                main._fusion.change_main(OTHER_MODEL)
            assert main.model == "gpt-6.1-sol"
        finally:
            await core.shutdown()
    asyncio.run(go())
