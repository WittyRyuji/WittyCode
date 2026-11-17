"""Model changes summarise with the source model, then re-inject scoped context; no real services."""
from __future__ import annotations

import asyncio
from copy import deepcopy
import json
import re

import pytest

from desktop.core.app_core import AppError
from desktop.core.fusion.registry import Policy
from desktop.core.memory.launch import MemoryLaunch
from tests.app_support import ready_core
from tests.chatgpt_support import finish_turn
from tests.fake_responses import fail, say
from tests.fakes import until

SOL, LUNA = "chatgpt:gpt-6.1-sol", "chatgpt:gpt-6-luna"


async def choose(core, cid, model):
    return await core.call("session.choose", {"id": cid, "field": "model", "value": model})


def test_source_summary_precedes_model_commit_and_target_gets_fresh_rules_and_relevant_memory(tmp_path):
    async def go():
        core, fake, _, _, engine = await ready_core(tmp_path)
        try:
            project = tmp_path / "proj"
            (project / "AGENTS.md").write_text("Use source conventions.", encoding="utf-8")
            cid = (await core.call("session.new", {"projectId": str(project)}))["id"]
            main = core._by_cid[cid].controller
            fake.script(say("Original evidence: inspected the inventory migration."))
            await core.call("session.send", {"id": cid, "text": "Check inventory migration rollback safety"})
            await finish_turn(main)
            original = deepcopy(main.saved.items)
            memory = MemoryLaunch(tmp_path / "memory")
            memory.store.create(title="Inventory migration rollback", text="Keep the migration rollback reversible.",
                                scope="project", project=str(project), kind="convention", tags=["inventory", "migration"], by="user")
            memory.store.create(title="Unrelated topic", text="Prefer oatmeal breakfast.", scope="global", kind="preference", tags=["breakfast"], by="user")
            engine.memory = memory
            (project / "AGENTS.md").write_text("CURRENT_RULE: inspect constraints before schema edits.", encoding="utf-8")
            fake.script([("hold", "summary"), *say("SUMMARY: inventory migration reviewed; rollback check remains.")])
            task = asyncio.create_task(choose(core, cid, LUNA))
            await until(lambda: len(fake.requests) == 2)
            assert main.model == "gpt-6.1-sol" and core._by_cid[cid].switching
            assert fake.requests[-1]["model"] == "gpt-6.1-sol"
            assert "Original evidence" in str(fake.requests[-1]["input"])
            with pytest.raises(AppError):
                await core.call("session.send", {"id": cid, "text": "Do not race the switch"})
            fake.release("summary")
            assert (await task)["ok"]
            assert main.model == "gpt-6-luna" and main.saved.items == original
            assert main.saved.compaction["upto"] == len(original)
            fake.script(say("Continued from summary."))
            await core.call("session.send", {"id": cid, "text": "Continue"})
            await finish_turn(main)
            request = fake.requests[-1]
            assert request["model"] == "gpt-6-luna"
            context = str(request)
            assert "SUMMARY: inventory" in context and "CURRENT_RULE" in context
            assert "Keep the migration rollback reversible" in context
            assert "oatmeal breakfast" not in context
            assert "Original evidence:" not in str(request["input"])
            assert core._by_cid[cid].ledger.by_model == {SOL: 300, LUNA: 150}, "the source model owns the summary's usage"
        finally:
            fake.release("summary")
            await core.shutdown()
    asyncio.run(go())


@pytest.mark.parametrize("outcome", ["failure", "empty", "cancel"])
def test_summary_failure_or_stop_keeps_original_model_history_and_mode(tmp_path, outcome):
    async def go():
        core, fake, _, _, _ = await ready_core(tmp_path)
        try:
            core.settings.fusion_main, core.settings.fusion_workers, core.settings.fusion_light = LUNA, [SOL], SOL
            cid = (await core.call("session.new", {"projectId": str(tmp_path / "proj")}))["id"]
            main = core._by_cid[cid].controller
            fake.script(say("Original finished work."))
            await core.call("session.send", {"id": cid, "text": "Inspect the module"})
            await finish_turn(main)
            before = deepcopy(main.saved.items)
            fake.script(fail("invalid_request", "summary unavailable") if outcome == "failure" else say("") if outcome == "empty" else [("hold", "summary"), *say("Summary")])
            task = asyncio.create_task(choose(core, cid, "fusion"))
            if outcome == "cancel":
                await until(lambda: len(fake.requests) == 2)
                await core.call("session.stop", {"id": cid})
            with pytest.raises(AppError):
                await task
            assert main.model == "gpt-6.1-sol" and main._fusion is None
            assert main.saved.items == before and not main.saved.compaction
            assert main.state.value == "ready" and not core._by_cid[cid].switching
            assert all(request["model"] == "gpt-6.1-sol" for request in fake.requests)
        finally:
            fake.release("summary")
            await core.shutdown()
    asyncio.run(go())


@pytest.mark.parametrize("engine_name", ["devin", "factory"])
@pytest.mark.parametrize("outcome", ["success", "permission", "cancel"])
def test_native_source_summary_restores_permissions_and_does_not_run_prompt_hooks(tmp_path, monkeypatch, engine_name, outcome):
    from desktop.core import model_switch
    from desktop.core.session_state import SessionState
    from tests.devin_support import make_engine
    from tests.test_factory import engine_for

    async def go():
        native_engine = make_engine(tmp_path).engine if engine_name == "devin" else engine_for(tmp_path)[0]
        core, fake, _, _, _ = await ready_core(tmp_path, **{engine_name + "_engine": native_engine})
        native_engine.settings = core.settings
        try:
            await until(lambda: any(m["source"] == engine_name for m in core.catalog.models))
            model = "devin:swe-2-high" if engine_name == "devin" else "factory:demo-model"
            project = tmp_path / "proj"
            cid = (await core.call("session.new", {"projectId": str(project)}))["id"]
            await choose(core, cid, model)
            source = core._by_cid[cid].controller
            seen = []
            native_engine.hooks.on_prompt(lambda event: seen.append(event.text) if event.engine == engine_name else None)
            await core.call("session.send", {"id": cid, "text": "Inspect inventory constraints"})
            await until(lambda: source.state is SessionState.READY)
            before = list(source.transcript.turns)
            autonomy = "full" if engine_name == "devin" else "high"
            assert await source.update_settings(autonomy=autonomy)
            (project / "AGENTS.md").write_text("FRESH_NATIVE_RULE: keep schema changes reversible.", encoding="utf-8")
            marker = {"devin": {"permission": "[approve]", "cancel": "[slow]"},
                      "factory": {"permission": "[permission]", "cancel": "[hold]"}}
            if outcome != "success":
                monkeypatch.setattr(model_switch, "NATIVE_SUMMARY", model_switch.NATIVE_SUMMARY + "\n" + marker[engine_name][outcome])
            switching = asyncio.create_task(choose(core, cid, SOL))
            if outcome == "cancel":
                await until(lambda: source.state is SessionState.RUNNING and getattr(source, "_switch_summary", False))
                await asyncio.sleep(0.1)
                await core.call("session.stop", {"id": cid})
            if outcome == "success":
                assert (await switching)["switched"]
                target = core._by_cid[cid].controller
                assert target is not source and target.handoff.summary
                assert "FRESH_NATIVE_RULE" in target.handoff.prefix
                assert not fake.requests, "no destination request before the next user message"
                fake.script(say("Continued from the native summary."))
                await core.call("session.send", {"id": cid, "text": "Continue"})
                await finish_turn(target)
                assert target.handoff is None and "FRESH_NATIVE_RULE" in str(fake.requests[-1])
            else:
                with pytest.raises(AppError):
                    await switching
                assert core._by_cid[cid].controller is source
                assert source.state is SessionState.READY and not fake.requests
            assert seen == ["Inspect inventory constraints"], "compaction is not a new user-intent or memory-capture hook"
            assert source.autonomy == autonomy and not getattr(source, "_switch_summary", False)
            assert source.transcript.turns[:len(before)] == before
            assert not source.transcript.pending_prompts()
            assert not core._by_cid[cid].switching
        finally:
            await core.shutdown()
    asyncio.run(go())


@pytest.mark.parametrize("engine_name", ["devin", "factory"])
def test_unused_native_segment_forwards_pending_summary_without_another_model_request(tmp_path, engine_name):
    from tests.devin_support import make_engine
    from tests.test_factory import engine_for

    async def go():
        native_engine = make_engine(tmp_path).engine if engine_name == "devin" else engine_for(tmp_path)[0]
        core, fake, _, _, _ = await ready_core(tmp_path, **{engine_name + "_engine": native_engine})
        native_engine.settings = core.settings
        try:
            await until(lambda: any(m["source"] == engine_name for m in core.catalog.models))
            cid = (await core.call("session.new", {"projectId": str(tmp_path / "proj")}))["id"]
            fake.script(say("Initial inspection."), say("SUMMARY: inspection completed; verify constraints next."))
            await core.call("session.send", {"id": cid, "text": "Inspect inventory"})
            await finish_turn(core._by_cid[cid].controller)
            await choose(core, cid, "devin:swe-2-high" if engine_name == "devin" else "factory:demo-model")
            native = core._by_cid[cid].controller
            turns = list(native.transcript.turns)
            summary = native.handoff.summary
            await choose(core, cid, SOL)
            assert core._by_cid[cid].controller.handoff.summary == summary
            assert all(turn in core._by_cid[cid].controller.transcript.turns for turn in turns)
            assert not any(tokens.total for tokens in native.usage_series().values())
            assert len(fake.requests) == 2
        finally:
            await core.shutdown()
    asyncio.run(go())


@pytest.mark.parametrize("payload", ["image", "text"])
@pytest.mark.parametrize("summarised", [False, True])
def test_acp_handoff_never_retains_image_payloads_and_respects_summary_boundary(tmp_path, summarised, payload):
    from tests.fusion_acp_support import make_guarded_engine
    async def go():
        harness = make_guarded_engine(tmp_path)
        core, _, _, _, _ = await ready_core(tmp_path, devin_engine=harness.engine)
        try:
            await until(lambda: any(m["source"] == "devin" for m in core.catalog.models))
            cid = (await core.call("session.new", {"projectId": str(tmp_path / "proj")}))["id"]
            main = core._by_cid[cid].controller
            main.saved.items = [{"type": "message", "role": "user", "content": [
                {"type": "input_text", "text": "ORIGINAL_IMAGE_CONTEXT"},
                ({"type": "input_image", "image_url": "data:image/png;base64," + "A" * (17 * 1024 * 1024)} if payload == "image"
                 else {"type": "input_text", "text": "A" * (17 * 1024 * 1024)}),
            ]}]
            if summarised:
                main.saved.compaction = {"upto": 1, "summary": "COMPACTED_IMAGE_CONTEXT", "at": 1}
            main.enable_fusion(Policy("devin:swe-2-high", (LUNA,), LUNA, 0))
            assert main.send_prompt("Continue from the handoff")
            await finish_turn(main, timeout=30)
            assert main.saved.turns[-1].status == "done", main.saved.turns[-1].error
            requests = [json.loads(line) for path in (tmp_path / "guarded").rglob("received.jsonl") for line in path.read_text(encoding="utf-8").splitlines()]
            prompt = next(r["params"]["prompt"] for r in requests if r.get("method") == "session/prompt")
            text = str(prompt)
            assert "data:image/png;base64" not in text
            assert ("COMPACTED_IMAGE_CONTEXT" in text) is summarised
            if payload == "text" and not summarised:
                references = re.findall(r"o-[a-f0-9]{64}", text)
                assert len(references) > 1 and len(text) < 60_000
                carried = "".join(main._outputs.read(reference) for reference in references)
                assert "ORIGINAL_IMAGE_CONTEXT" in carried and carried.count("A") >= 17 * 1024 * 1024
            else:
                assert ("ORIGINAL_IMAGE_CONTEXT" in text) is not summarised
            assert all(path.stat().st_size < 16 * 1024 * 1024 for path in main._outputs.root.glob("*.txt"))
        finally:
            await core.shutdown()
    asyncio.run(go())
