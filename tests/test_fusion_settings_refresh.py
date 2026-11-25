"""A changed Fusion preset is applied at the next send, with local fake models only."""
from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from dataclasses import replace

import pytest

from desktop.core.app_core import AppError
from desktop.core.fusion import tools
from desktop.core.settings import default_settings_path, load_settings, save_settings, settings_to_dict
from tests.app_support import ready_core
from tests.chatgpt_support import finish_turn
from tests.fake_responses import call, say
from tests.fakes import until

SOL, LUNA = "chatgpt:gpt-6.1-sol", "chatgpt:gpt-6-luna"


async def configure(core, *, main=SOL, worker=LUNA, light=LUNA, reasoning=None):
    for key, value in {"fusionMain": main, "fusionWorkers": [worker], "fusionLight": light,
                       "fusionReasoning": reasoning or {}}.items():
        await core.call("settings.set", {"key": key, "value": value})


def assignment(model=LUNA, **extra):
    return {"workstream": "Login flow", "paths": ["src/auth", "tests/auth"], "model": model,
            "role": "worker", "task": "Inspect current evidence and implement the next step.", **extra}


async def open_fusion(core, tmp_path):
    await configure(core)
    cid = (await core.call("session.new", {"projectId": str(tmp_path / "proj"), "fusion": True}))["id"]
    return cid, core._by_cid[cid].controller


def test_changed_roles_apply_on_send_and_preserve_expert_handoff_and_history(tmp_path):
    async def go():
        core, fake, _, _, _ = await ready_core(tmp_path)
        try:
            cid, main = await open_fusion(core, tmp_path)
            fake.script(call("delegate_task", assignment()), say("Original expert evidence."), say("Reviewed."))
            await core.call("session.send", {"id": cid, "text": "Implement login"})
            await finish_turn(main)
            original = next(iter(main.agents))
            count, history = len(fake.requests), deepcopy(main.saved.items)
            await configure(core, main=LUNA, worker=SOL, light=SOL,
                            reasoning={"main:" + LUNA: "low", "worker:" + SOL: "high"})
            assert len(fake.requests) == count and main.model == "gpt-6.1-sol"
            fake.script(say("Login implemented; evidence retained."), call("delegate_task", assignment(SOL)),
                        say("New expert verified previous evidence."), say("Verified."))
            await core.call("session.send", {"id": cid, "text": "Continue login"})
            await finish_turn(main)
            assert [request["model"] for request in fake.requests[count:]] == [
                "gpt-6.1-sol", "gpt-6-luna", "gpt-6.1-sol", "gpt-6-luna"]
            assert main.saved.items[:len(history)] == history
            assert main._fusion.registry.policy.workers == (SOL,) and main._fusion.registry.policy.light == SOL
            assert main.agents[original].record.status == "retired"
            replacement = next(view for key, view in main.agents.items() if key != original)
            assert replacement.record.transferred_from == original and replacement.record.handoff_complete
            assert "retained tool evidence" in str(fake.requests[-2]["input"])
            assert fake.requests[-2]["reasoning"]["effort"] == "high"
            assert "Authorised worker models: " + SOL in fake.requests[-1]["instructions"]
            assert main._fusion.registry.calls == 2
            assert main.manager.sessions.load(main.session_id).fusion["policy"]["main"] == LUNA
        finally:
            await core.shutdown()
    asyncio.run(go())


def test_main_only_change_keeps_all_34_experts_and_session_authorisations(tmp_path):
    from desktop.core.fusion.runtime import ExpertView

    async def go():
        core, fake, _, _, _ = await ready_core(tmp_path)
        try:
            cid, main = await open_fusion(core, tmp_path)
            fake.script(say("Before."))
            await core.call("session.send", {"id": cid, "text": "Start"})
            await finish_turn(main)
            assert (await core.call("fusion.authorizeModel", {"id": cid, "model": SOL}))["ok"]
            runtime = main._fusion
            for i in range(34):
                record = runtime.registry.assign_workstream(f"Sample {i}", (), SOL if i < 14 else LUNA,
                                                            "worker", identity="Coder")
                record.status, record.started_at, record.finished_at = "done", 1.0, 2.0
                runtime.agents[record.id] = ExpertView(record)
            runtime._checkpoint()
            before = [record.to_json() for record in runtime.registry.agents.values()]
            count = len(fake.requests)
            await core.call("settings.set", {"key": "fusionMain", "value": LUNA})
            fake.script(say("Handoff."), say("No delegation."))
            await core.call("session.send", {"id": cid, "text": "Continue"})
            await finish_turn(main)
            assert [record.to_json() for record in runtime.registry.agents.values()] == before
            assert runtime.registry.policy.workers == (LUNA, SOL)
            assert len(main.agents) == 34 and runtime.registry.calls == 0 and len(fake.requests) == count + 2
        finally:
            await core.shutdown()

    asyncio.run(go())


def test_running_turn_keeps_policy_and_queued_turn_applies_latest(tmp_path):
    async def go():
        core, fake, _, _, _ = await ready_core(tmp_path)
        try:
            cid, main = await open_fusion(core, tmp_path)
            fake.script([("hold", "first"), *say("First result.")], say("First result summary."), say("Second result."))
            await core.call("session.send", {"id": cid, "text": "First"})
            await until(lambda: len(fake.requests) == 1)
            await configure(core, main=LUNA)
            assert main.model == "gpt-6.1-sol"
            assert (await core.call("session.send", {"id": cid, "text": "Second"}))["queued"]
            assert len(fake.requests) == 1
            fake.release("first")
            await until(lambda: len(fake.requests) == 3)
            await finish_turn(main)
            assert [request["model"] for request in fake.requests] == ["gpt-6.1-sol", "gpt-6.1-sol", "gpt-6-luna"]
            assert not core._by_cid[cid].queue and main.model == "gpt-6-luna"
        finally:
            fake.release("first")
            await core.shutdown()
    asyncio.run(go())


def test_stopping_preset_cleanup_returns_the_prepared_message_and_settles_send(tmp_path, monkeypatch):
    async def go():
        core, fake, events, _, _ = await ready_core(tmp_path)
        task = None
        try:
            cid, main = await open_fusion(core, tmp_path)
            await configure(core, worker=SOL)
            entered = asyncio.Event()

            async def closing_old_experts():
                entered.set()
                await asyncio.Event().wait()

            monkeypatch.setattr(main, "release_inactive_acp", closing_old_experts)
            text = "  Keep the original draft  "
            task = asyncio.create_task(core.call("session.send", {"id": cid, "text": text}))
            await asyncio.wait_for(entered.wait(), 5)
            assert any(event["id"] == cid and (event["patch"].get("preparing") or {}).get("text") == text
                       for event in events.of("session"))
            assert not main.transcript.turns and not fake.requests
            await core.call("session.stop", {"id": cid})
            with pytest.raises(AppError, match="已停止"):
                await task
            conv = core._by_cid[cid]
            assert conv.preparing is None and conv.switch_task is None
            assert events.last("restore")["text"] == text
            assert not main.transcript.turns and not fake.requests
            fake.script(say("A new explicit send succeeded."))
            assert (await core.call("session.send", {"id": cid, "text": text}))["ok"]
            await finish_turn(main)
            assert len(fake.requests) == 1
        finally:
            if task is not None and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            await core.shutdown()
    asyncio.run(go())


def test_expert_effort_only_change_reuses_owner_without_main_compaction(tmp_path):
    async def go():
        core, fake, _, _, _ = await ready_core(tmp_path)
        try:
            cid, main = await open_fusion(core, tmp_path)
            await configure(core, worker=SOL, reasoning={"worker:" + SOL: "low"})
            for effort in ("low", "high"):
                await configure(core, worker=SOL, reasoning={"worker:" + SOL: effort})
                count = len(fake.requests)
                fake.script(call("delegate_task", assignment(SOL)), say("Evidence."), say("Reviewed."))
                await core.call("session.send", {"id": cid, "text": "Continue"})
                await finish_turn(main)
                assert len(fake.requests) == count + 3 and not main.saved.compaction
                assert fake.requests[-2]["reasoning"]["effort"] == effort
                assert len(main.agents) == 1
        finally:
            await core.shutdown()
    asyncio.run(go())


@pytest.mark.parametrize("failure", ["unavailable", "compaction", "cancel"])
def test_refresh_failure_never_sends_message_or_changes_policy(tmp_path, monkeypatch, failure):
    async def go():
        core, fake, _, _, _ = await ready_core(tmp_path)
        try:
            cid, main = await open_fusion(core, tmp_path)
            fake.script(say("Before."))
            await core.call("session.send", {"id": cid, "text": "First"})
            await finish_turn(main)
            previous, history, count = main._fusion.registry.to_json(), deepcopy(main.saved.items), len(fake.requests)
            await configure(core, main="chatgpt:unavailable" if failure == "unavailable" else LUNA)
            async def fail(*args):
                if failure == "cancel":
                    raise asyncio.CancelledError()
                raise RuntimeError("test summary failure")
            monkeypatch.setattr(main, "compact_for_switch", fail)
            with pytest.raises(AppError):
                await core.call("session.send", {"id": cid, "text": "Must not be sent"})
            assert main._fusion.registry.to_json() == previous and main.saved.items == history
            assert len(fake.requests) == count and main.model == "gpt-6.1-sol"
            assert core._by_cid[cid].switch_task is None and not core._by_cid[cid].switching
        finally:
            await core.shutdown()
    asyncio.run(go())


def test_changed_settings_survive_reopen_without_starting_work(tmp_path):
    async def go():
        core, fake, _, _, _ = await ready_core(tmp_path)
        try:
            cid, main = await open_fusion(core, tmp_path)
            fake.script(say("Before."))
            await core.call("session.send", {"id": cid, "text": "First"})
            await finish_turn(main)
            native = main.session_id
            await configure(core, main=LUNA)
            await core.call("session.close", {"id": cid})
            await until(lambda: main.state.value == "closed")
            cid = (await core.call("session.openSaved", {"projectId": str(tmp_path / "proj"), "id": "cg:" + native}))["id"]
            main = core._by_cid[cid].controller
            await until(lambda: main.state.value == "ready")
            assert len(fake.requests) == 1 and main.model == "gpt-6.1-sol"
            fake.script(say("Before summary."), say("After."))
            await core.call("session.send", {"id": cid, "text": "Next"})
            await finish_turn(main)
            assert main.model == "gpt-6-luna" and len(fake.requests) == 3
        finally:
            await core.shutdown()
    asyncio.run(go())


@pytest.mark.parametrize("batch", [False, True])
def test_interrupted_owner_can_explicitly_resume_through_tool_without_replay(tmp_path, batch):
    async def go():
        core, fake, _, _, _ = await ready_core(tmp_path)
        try:
            cid, main = await open_fusion(core, tmp_path)
            fake.script(call("delegate_task", assignment()), say("Saved evidence."), say("Reviewed."))
            await core.call("session.send", {"id": cid, "text": "Initial"})
            await finish_turn(main)
            expert = next(iter(main.agents.values()))
            expert.record.status = "interrupted"
            main._fusion._checkpoint()
            count = len(fake.requests)
            failed = await main._fusion.delegate("Login flow", LUNA, "worker", "Continue", ("src/auth",))
            assert failed.is_error and len(fake.requests) == count
            task = assignment(resume=True)
            dispatch = call("delegate_tasks", {"tasks": [task]}) if batch else call("delegate_task", task)
            fake.script(dispatch, say("Continued after inspection."), say("Verified."))
            await core.call("session.send", {"id": cid, "text": "Inspect and continue the previous work"})
            await finish_turn(main)
            assert len(main.agents) == 1 and expert.record.status == "done"
            assert "Saved evidence." in str(fake.requests[-2]["input"])
            assert len(fake.requests) == count + 3 and main._fusion.registry.calls == 2
            assert "resume" in next(tool for tool in fake.requests[-3]["tools"] if tool["name"] == "delegate_task")["parameters"]["properties"]
        finally:
            await core.shutdown()
    asyncio.run(go())


def test_failed_policy_checkpoint_rolls_back_owners_models_and_settings(tmp_path, monkeypatch):
    async def go():
        core, fake, _, _, _ = await ready_core(tmp_path)
        try:
            cid, main = await open_fusion(core, tmp_path)
            fake.script(call("delegate_task", assignment()), say("Evidence."), say("Reviewed."))
            await core.call("session.send", {"id": cid, "text": "Initial"})
            await finish_turn(main)
            before = deepcopy(main.saved.fusion), deepcopy(main.saved.origin), main._fusion.registry, main.agents
            await configure(core, worker=SOL)
            def fail(**kwargs):
                raise OSError("test save denied")
            with monkeypatch.context() as patch:
                patch.setattr(main, "_save_now", fail)
                with pytest.raises(OSError):
                    await core.call("session.send", {"id": cid, "text": "Must not start"})
            assert (main.saved.fusion, main.saved.origin, main._fusion.registry, main.agents) == before
            assert len(fake.requests) == 3 and main.model == "gpt-6.1-sol"
        finally:
            await core.shutdown()
    asyncio.run(go())


def test_explicit_single_model_selection_takes_priority_over_changed_fusion_preset(tmp_path):
    async def go():
        core, fake, _, _, _ = await ready_core(tmp_path)
        try:
            cid, main = await open_fusion(core, tmp_path)
            await core.call("session.choose", {"id": cid, "field": "model", "value": SOL})
            await configure(core, main=LUNA)
            fake.script(say("Single-agent reply."))
            await core.call("session.send", {"id": cid, "text": "Continue alone"})
            await finish_turn(main)
            assert main._fusion is None and len(fake.requests) == 1
            assert fake.requests[0]["model"] == "gpt-6.1-sol"
            assert "delegate_task" not in {tool["name"] for tool in fake.requests[0]["tools"]}
        finally:
            await core.shutdown()
    asyncio.run(go())


def test_summary_unknown_usage_survives_policy_replacement_and_blocks_legacy_budget(tmp_path):
    async def go():
        core, fake, _, _, _ = await ready_core(tmp_path)
        try:
            cid, main = await open_fusion(core, tmp_path)
            main._fusion.registry.policy = replace(main._fusion.registry.policy, max_tokens=5_000_000)
            main._fusion._checkpoint()
            fake.script(say("Initial result."))
            await core.call("session.send", {"id": cid, "text": "Initial"})
            await finish_turn(main)
            await configure(core, main=LUNA)
            summary = say("Initial result summary.")
            summary[-1]["response"]["usage"] = None
            fake.script(summary, say("Must not request the target model."))
            with pytest.raises(AppError, match="Fusion"):
                await core.call("session.send", {"id": cid, "text": "Next"})
            assert main._fusion.registry.unmetered and main._fusion.registry.had_unknown
            assert main.saved.fusion["unmetered"] and len(fake.requests) == 2
            assert main.model == "gpt-6.1-sol" and len(main.saved.turns) == 1
        finally:
            await core.shutdown()
    asyncio.run(go())


def test_role_change_leaves_interrupted_owner_in_history_until_explicit_resume(tmp_path):
    async def go():
        core, fake, _, _, _ = await ready_core(tmp_path)
        try:
            cid, main = await open_fusion(core, tmp_path)
            fake.script(call("delegate_task", assignment()), say("Original evidence."), say("Reviewed."))
            await core.call("session.send", {"id": cid, "text": "Initial"})
            await finish_turn(main)
            old = next(iter(main.agents.values()))
            old.record.status = "interrupted"
            main._fusion._checkpoint()
            await configure(core, worker=SOL)
            fake.script(say("Assess the changed team first."))
            await core.call("session.send", {"id": cid, "text": "Assess"})
            await finish_turn(main)
            assert list(main.agents) == [old.record.id]
            assert main.agents[old.record.id].record.replaced_status == "interrupted"
            assert (await core.call("agents.watch", {"id": cid, "agentId": old.record.id}))["ok"]
            rejected = await main._fusion.delegate("Login flow", SOL, "worker", "Check first", ("src/auth",))
            assert rejected.is_error and len(main.agents) == 1 and len(fake.requests) == 4
            fake.script(call("delegate_task", assignment(SOL, resume=True)), say("Continued safely."), say("Verified."))
            await core.call("session.send", {"id": cid, "text": "Continue after checking"})
            await finish_turn(main)
            new = next(view for view in main.agents.values() if view.record.transferred_from == old.record.id)
            assert new.record.status == "done" and new.record.handoff_complete
            assert len(fake.requests) == 7
        finally:
            await core.shutdown()
    asyncio.run(go())


@pytest.mark.parametrize("target_images", [False, True])
def test_image_support_is_checked_against_destination_not_old_fusion_main(tmp_path, monkeypatch, target_images):
    from desktop.core.attachments import Attachment, IMAGE
    async def go():
        core, fake, _, _, _ = await ready_core(tmp_path)
        try:
            cid, main = await open_fusion(core, tmp_path)
            lookup = main.manager.model_info
            def info(model):
                result = lookup(model)
                return replace(result, supports_images=target_images if model == "gpt-6-luna" else not target_images) if result else None
            monkeypatch.setattr(main.manager, "model_info", info)
            fake.script(say("Initial result."))
            await core.call("session.send", {"id": cid, "text": "Initial"})
            await finish_turn(main)
            await configure(core, main=LUNA)
            core._by_cid[cid].drafts = [Attachment(IMAGE, "sample.png", b"test-image", "image/png", id="image")]
            fake.script(say("Initial result summary."), say("Image checked."))
            if target_images:
                assert (await core.call("session.send", {"id": cid, "text": "Inspect image"}))["ok"]
                await finish_turn(main)
                assert len(fake.requests) == 3 and not core._by_cid[cid].drafts
                assert "input_image" in str(fake.requests[-1]["input"])
            else:
                with pytest.raises(AppError, match="不支持图片"):
                    await core.call("session.send", {"id": cid, "text": "Do not send image"})
                assert len(fake.requests) == 2 and len(core._by_cid[cid].drafts) == 1
            assert main.model == "gpt-6-luna"
        finally:
            await core.shutdown()
    asyncio.run(go())


def test_cross_engine_fusion_restore_checks_images_after_latest_preset_is_applied(tmp_path, monkeypatch):
    from desktop.core.attachments import Attachment, IMAGE
    from tests.devin_support import make_engine
    from tests.test_fusion_switch import choose
    async def go():
        harness = make_engine(tmp_path)
        core, fake, _, _, _ = await ready_core(tmp_path, devin_engine=harness.engine)
        try:
            await until(lambda: any(model["source"] == "devin" for model in core.catalog.models))
            cid, main = await open_fusion(core, tmp_path)
            lookup = main.manager.model_info
            monkeypatch.setattr(main.manager, "model_info", lambda model: replace(lookup(model), supports_images=False)
                                if model == "gpt-6.1-sol" else lookup(model))
            fake.script(say("Initial result."), say("Initial result summary."))
            await core.call("session.send", {"id": cid, "text": "Initial"})
            await finish_turn(main)
            assert (await choose(core, cid, "devin:swe-2-high"))["ok"]
            conv = core._by_cid[cid]
            assert conv.engine == "devin"
            await configure(core, main=LUNA)
            assert (await core.call("session.choose", {"id": cid, "field": "model", "value": "fusion"}))["ok"]
            conv.drafts = [Attachment(IMAGE, "sample.png", b"test-image", "image/png", id="image")]
            fake.script(say("Image checked."))
            assert (await core.call("session.send", {"id": cid, "text": "Inspect image"}))["ok"]
            await finish_turn(conv.controller)
            assert conv.controller.model == "gpt-6-luna" and not conv.drafts
            assert "input_image" in str(fake.requests[-1]["input"])
            assert fake.requests[-1]["model"] == "gpt-6-luna" and len(fake.requests) == 3
        finally:
            await core.shutdown()
    asyncio.run(go())


def test_resume_flag_requires_a_boolean():
    assert tools.parse(assignment(resume="true"))[0] is None
    assert tools.parse(assignment(resume=True))[0][-1] is True


def test_light_pool_legacy_migration_and_settings_bridge_contract(tmp_path):
    legacy = tmp_path / "settings.json"
    legacy.write_text(json.dumps({"fusion_light": LUNA, "fusion_lights": [SOL, SOL]}), encoding="utf-8")
    loaded = load_settings(legacy)
    assert loaded.fusion_lights == [LUNA, SOL]
    assert loaded.fusion_light == LUNA
    save_settings(loaded, legacy)
    written = json.loads(legacy.read_text(encoding="utf-8"))
    assert written["fusion_lights"] == [LUNA, SOL] and written["fusion_light"] == LUNA
    assert settings_to_dict(loaded)["fusion_light"] == LUNA

    async def go():
        core, _, _, _, _ = await ready_core(tmp_path)
        try:
            await core.call("settings.set", {"key": "fusionWorkers", "value": [SOL, LUNA, SOL]})
            await core.call("settings.set", {"key": "fusionLights", "value": [LUNA, SOL, LUNA]})
            assert core.settings.fusion_workers == [SOL, LUNA]
            assert core.settings.fusion_lights == [LUNA, SOL]
            assert core.settings.fusion_light == LUNA
            view = core.settings_view()
            assert view["fusionWorkers"] == [SOL, LUNA]
            assert view["fusionLights"] == [LUNA, SOL]
            assert view["fusionLight"] == LUNA
            await core.call("settings.set", {"key": "fusionLight", "value": SOL})
            assert core.settings.fusion_lights == [SOL] and core.settings.fusion_light == SOL
        finally:
            await core.shutdown()
    asyncio.run(go())


def test_expert_catalog_bridge_crud_uses_lazy_controlled_settings_root(tmp_path, monkeypatch):
    monkeypatch.setenv("WITTYCODE_SETTINGS", str(tmp_path / "profile" / "settings.json"))
    markdown = "---\ndescription: Checks security-sensitive changes.\ntools: read\n---\n检查认证、授权和密钥处理，只报告事实。"

    async def go():
        core, _, _, _, _ = await ready_core(tmp_path)
        try:
            root = default_settings_path().parent / "fusion-experts"
            assert not root.exists()
            initial = await core.call("experts.list")
            catalog_root = str(core.expert_catalog()._root).removeprefix("\\\\?\\")
            assert root.is_dir() and catalog_root == str(root.resolve())
            assert initial["revision"] == 0
            assert initial["experts"] and all(entry["builtin"] for entry in initial["experts"])
            assert {item["id"] for item in initial["workTypes"]} == {"execute", "manage", "review", "research", "verify", "observe"}

            created = await core.call("experts.create", {
                "stableId": "review-security", "name": "安全检查", "workType": "review",
                "markdown": markdown, "tags": "security api security", "enabled": True,
            })
            entry = next(item for item in created["experts"] if item["stableId"] == "review-security")
            assert (entry["version"], entry["enabled"], entry["tags"], entry["builtin"]) == (1, True, ["security", "api"], False)
            detail = await core.call("experts.get", {"stableId": "review-security"})
            assert detail["markdown"] == markdown and detail["expert"]["contentHash"] == entry["contentHash"]

            updated = await core.call("experts.update", {
                "stableId": "review-security", "name": "安全复核", "workType": "verify",
                "tags": "security verify", "markdown": markdown + "\n保留证据路径。",
            })
            entry = next(item for item in updated["experts"] if item["stableId"] == "review-security")
            assert (entry["name"], entry["workType"], entry["version"], entry["tags"]) == ("安全复核", "verify", 2, ["security", "verify"])
            disabled = await core.call("experts.setEnabled", {"stableId": "review-security", "enabled": False})
            entry = next(item for item in disabled["experts"] if item["stableId"] == "review-security")
            assert not entry["enabled"] and entry["version"] == 3
            detail = await core.call("experts.get", {"stableId": "review-security"})
            assert detail["expert"]["version"] == 3

            removed = await core.call("experts.delete", {"stableId": "review-security"})
            assert all(item["stableId"] != "review-security" for item in removed["experts"])
            with pytest.raises(AppError, match="没有找到这个专家"):
                await core.call("experts.get", {"stableId": "review-security"})
        finally:
            await core.shutdown()
    asyncio.run(go())
