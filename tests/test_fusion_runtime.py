"""The own harness runs a fixed main agent and reusable experts against a local fake."""
from __future__ import annotations

import asyncio

import pytest

from desktop.core.app_core import AppError
from desktop.core.chatgpt.errors import ChatGPTError
from desktop.core.fusion.registry import Policy
from tests.app_support import ready_core
from tests.chatgpt_support import finish_turn, make_engine
from tests.fake_responses import FakeResponses, call, say
from tests.fakes import until


def test_own_harness_delegates_and_reuses_the_same_expert_context(tmp_path):
    async def go():
        project = tmp_path / "project"
        project.mkdir()
        with FakeResponses() as fake:
            engine = make_engine(tmp_path, fake)
            main = await engine.open_new(project, model="gpt-6.1-sol")
            policy = Policy("chatgpt:gpt-6.1-sol", ("chatgpt:gpt-6-luna",), "chatgpt:gpt-6-luna", 5)
            main.enable_fusion(policy)
            task = {"scope": "src/module", "model": "chatgpt:gpt-6-luna", "role": "worker", "task": "Inspect the module."}
            fake.script(call("delegate_task", task, call_id="delegate-1"), say("Module inspected."), say("I checked the expert's result."),
                        call("delegate_task", {**task, "scope": "src/module/file.py", "task": "Check the test."}, call_id="delegate-2"),
                        say("Test checked."), say("Finished."))
            try:
                assert main.send_prompt("Investigate the module")
                await finish_turn(main)
                assert [r["model"] for r in fake.requests] == ["gpt-6.1-sol", "gpt-6-luna", "gpt-6.1-sol"]
                assert main.model == "gpt-6.1-sol" and len(main.agents) == 1
                expert = next(iter(main.agents.values()))
                native = expert.session_id
                assert main.send_prompt("Continue with the test")
                await finish_turn(main)
                assert len(main.agents) == 1 and next(iter(main.agents.values())).session_id == native
                child_request = fake.requests[4]
                assert "Module inspected." in str(child_request["input"])
                assert "Check the test." in str(child_request["input"])
                assert main.model == "gpt-6.1-sol"
                assert engine.sessions.load(main.session_id).fusion["agents"][0]["id"] == native
                assert all(row.id != native for row in engine.sessions.rows(str(project)))
            finally:
                await engine.close_all()
    asyncio.run(go())


def test_reopening_restores_the_same_expert_and_its_history(tmp_path):
    async def go():
        project = tmp_path / "project"
        project.mkdir()
        with FakeResponses() as fake:
            engine = make_engine(tmp_path, fake)
            main = await engine.open_new(project, model="gpt-6.1-sol")
            main.enable_fusion(Policy("chatgpt:gpt-6.1-sol", ("chatgpt:gpt-6-luna",), "chatgpt:gpt-6-luna", 5))
            task = {"scope": "src/module", "model": "chatgpt:gpt-6-luna", "role": "worker", "task": "Inspect the module."}
            fake.script(call("delegate_task", task, call_id="d-1"), say("Remember this module."), say("Reviewed."))
            try:
                assert main.send_prompt("Inspect")
                await finish_turn(main)
                native, expert = main.session_id, next(iter(main.agents))
                await main.close()
                reopened = await engine.open_saved(project, native)
                assert reopened.model == "gpt-6.1-sol" and expert in reopened.agents
                fake.script(call("delegate_task", {**task, "task": "Continue reviewing."}, call_id="d-2"), say("Continued."), say("Done."))
                assert reopened.send_prompt("Continue")
                await finish_turn(reopened)
                assert next(iter(reopened.agents)) == expert
                assert "Remember this module." in str(fake.requests[4]["input"])
            finally:
                await engine.close_all()
    asyncio.run(go())


def test_expert_permission_is_routed_to_the_main_conversation(tmp_path):
    async def go():
        core, model, events, _, _ = await ready_core(tmp_path)
        try:
            cid = (await core.call("session.new", {"projectId": str(tmp_path / "proj"), "autonomy": "ask"}))["id"]
            await until(lambda: cid in core._by_cid and core._by_cid[cid].controller.state.value == "ready")
            main = core._by_cid[cid].controller
            assert await main.update_settings(autonomy="ask")
            main.enable_fusion(Policy("chatgpt:gpt-6.1-sol", ("chatgpt:gpt-6-luna",), "chatgpt:gpt-6-luna", 3))
            model.script(call("delegate_task", {"scope": "src", "model": "chatgpt:gpt-6-luna", "role": "worker", "task": "Create a file."}),
                         call("create_file", {"file_path": "src/demo.py", "content": "hello"}), say("The write was declined."), say("I will report it."))
            await core.call("session.send", {"id": cid, "text": "Create a file"})
            await until(lambda: bool(main.agent_prompts()))
            expert = next(iter(main.agents.values()))
            prompt = main.agent_prompts()[0]
            assert core._mark(core._by_cid[cid]) == "attention"
            assert (await core.call("session.answerPermission", {"id": cid, "agentId": expert.session_id, "promptId": prompt.id, "value": "cancel"}))["ok"]
            await finish_turn(main)
            assert len(core.order) == 1 and not (tmp_path / "proj" / "src" / "demo.py").exists()
            expert.controller._tools.remembered.add("old-expert-approval")
            assert await main.update_settings(autonomy="code")
            assert not expert.controller._tools.remembered
        finally:
            await core.shutdown()
    asyncio.run(go())


def test_app_settings_start_fusion_explicitly_and_pin_the_main(tmp_path):
    async def go():
        core, _, events, _, _ = await ready_core(tmp_path)
        try:
            values = {"fusionMain": "chatgpt:gpt-6.1-sol", "fusionWorkers": ["chatgpt:gpt-6-luna"],
                      "fusionLight": "chatgpt:gpt-6-luna", "fusionMaxCalls": 3, "fusionMaxTokens": 50_000, "fusionMaxSeconds": 60}
            for key, value in values.items():
                await core.call("settings.set", {"key": key, "value": value})
            cid = (await core.call("session.new", {"projectId": str(tmp_path / "proj"), "fusion": True}))["id"]
            main = core._by_cid[cid].controller
            assert main.model == "gpt-6.1-sol" and core._session_meta(core._by_cid[cid])["fusion"] is True
            assert core.settings.fusion_max_calls == 3 and main._fusion.registry.policy.max_seconds == 60
            from desktop.core.app_core import AppError
            with pytest.raises(AppError, match="主模型"):
                await core.call("session.choose", {"id": cid, "field": "model", "value": "chatgpt:gpt-6-luna"})
            assert main.model == "gpt-6.1-sol"
            from desktop.core.settings import settings_from_dict, settings_to_dict
            restored = settings_from_dict(settings_to_dict(core.settings))
            assert restored.fusion_workers == ["chatgpt:gpt-6-luna"] and restored.fusion_max_tokens == 50_000
        finally:
            await core.shutdown()
    asyncio.run(go())


def test_fusion_injects_changed_project_rules_before_the_next_request(tmp_path, monkeypatch):
    async def go():
        from desktop.core.harness.toolset import HarnessTools
        project = tmp_path / "project"
        project.mkdir()
        (project / "AGENTS.md").write_text("Initial rule.", encoding="utf-8")
        (project / "readme.txt").write_text("hello", encoding="utf-8")
        original = HarnessTools._run
        async def change_rules(tools, tool_call):
            if tool_call.name == "read_file":
                (project / "AGENTS.md").write_text("Urgent updated rule.", encoding="utf-8")
            return await original(tools, tool_call)
        monkeypatch.setattr(HarnessTools, "_run", change_rules)
        with FakeResponses() as fake:
            engine = make_engine(tmp_path, fake)
            main = await engine.open_new(project, model="gpt-6.1-sol")
            main.enable_fusion(Policy("chatgpt:gpt-6.1-sol", ("chatgpt:gpt-6-luna",), "chatgpt:gpt-6-luna", 5))
            fake.script(call("read_file", {"file_path": "readme.txt"}), say("Finished."))
            try:
                assert main.send_prompt("Inspect")
                await finish_turn(main)
                assert "Initial rule." in fake.requests[0]["instructions"]
                assert "Urgent updated rule." in str(fake.requests[1]["input"])
            finally:
                await engine.close_all()
    asyncio.run(go())


def test_rule_change_stops_remaining_tools_before_injecting_the_new_rule(tmp_path, monkeypatch):
    async def go():
        from desktop.core.harness.toolset import HarnessTools
        project = tmp_path / "project"
        project.mkdir()
        (project / "AGENTS.md").write_text("Initial rule.", encoding="utf-8")
        (project / "one.txt").write_text("first", encoding="utf-8")
        (project / "two.txt").write_text("MUST_NOT_READ", encoding="utf-8")
        original = HarnessTools._run
        async def change_rules(tools, tool_call):
            if tool_call.name == "read_file" and tool_call.args.get("file_path") == "one.txt":
                (project / "AGENTS.md").write_text("Stop and reconsider.", encoding="utf-8")
            return await original(tools, tool_call)
        monkeypatch.setattr(HarnessTools, "_run", change_rules)
        with FakeResponses() as fake:
            engine = make_engine(tmp_path, fake)
            main = await engine.open_new(project, model="gpt-6.1-sol")
            main.enable_fusion(Policy("chatgpt:gpt-6.1-sol", ("chatgpt:gpt-6-luna",), "chatgpt:gpt-6-luna", 5))
            batch = [*call("read_file", {"file_path": "one.txt"}, call_id="one", item_id="one-item")[:-1],
                     *call("read_file", {"file_path": "two.txt"}, call_id="two", item_id="two-item")]
            fake.script(batch, say("Reconsidered."))
            try:
                assert main.send_prompt("Inspect")
                await finish_turn(main)
                outputs = {item.get("call_id"): item.get("output", "") for item in main.saved.items if item.get("type") == "function_call_output"}
                assert "MUST_NOT_READ" not in outputs["two"] and "not run" in outputs["two"].lower()
                assert "Stop and reconsider." in str(fake.requests[1]["input"])
            finally:
                await engine.close_all()
    asyncio.run(go())


def test_incomplete_model_response_never_starts_an_expert(tmp_path):
    async def go():
        with FakeResponses() as fake:
            engine = make_engine(tmp_path, fake)
            main = await engine.open_new(tmp_path / "project", model="gpt-6.1-sol")
            main.enable_fusion(Policy("chatgpt:gpt-6.1-sol", ("chatgpt:gpt-6-luna",), "chatgpt:gpt-6-luna", 5))
            events = call("delegate_task", {"scope": "src", "model": "chatgpt:gpt-6-luna", "role": "worker", "task": "Write code."})
            events[-1] = {"type": "response.incomplete", "response": {"id": "demo", "status": "incomplete", "incomplete_details": {"reason": "max_output_tokens"}}}
            fake.script(events, say("must not be requested"))
            try:
                assert main.send_prompt("Inspect")
                await finish_turn(main)
                assert not main.agents and len(fake.requests) == 1
                assert main.saved.turns[-1].status == "incomplete"
                assert any(item.get("type") == "function_call_output" and "not run" in item.get("output", "").lower() for item in main.saved.items)
            finally:
                await engine.close_all()
    asyncio.run(go())


def test_plan_mode_never_delegates_even_if_the_model_tries(tmp_path):
    async def go():
        core, model, events, _, _ = await ready_core(tmp_path)
        try:
            cid = (await core.call("session.new", {"projectId": str(tmp_path / "proj")}))["id"]
            main = core._by_cid[cid].controller
            main.enable_fusion(Policy("chatgpt:gpt-6.1-sol", ("chatgpt:gpt-6-luna",), "chatgpt:gpt-6-luna", 5))
            model.script(say("Ready."))
            await core.call("session.send", {"id": cid, "text": "Start"})
            await finish_turn(main)
            await core.call("session.choose", {"id": cid, "field": "mode", "value": "spec"})
            model.script(call("delegate_task", {"scope": "src", "model": "chatgpt:gpt-6-luna", "role": "worker", "task": "Write code."}),
                         call("submit_plan", {"plan": "Review before changing anything."}, call_id="plan"))
            await core.call("session.send", {"id": cid, "text": "Plan the change"})
            await finish_turn(main)
            assert main.saved.plan["status"] == "pending" and not main.agents
            with pytest.raises(AppError):
                await core.call("fusion.changeMain", {"id": cid, "model": "chatgpt:gpt-6-luna"})
            assert main.saved.plan["status"] == "pending" and main.model == "gpt-6.1-sol"
            assert [request["model"] for request in model.requests] == ["gpt-6.1-sol"] * 3
            assert model.requests[0]["tools"] == model.requests[1]["tools"] == model.requests[2]["tools"]
        finally:
            await core.shutdown()
    asyncio.run(go())


def test_interrupted_expert_needs_explicit_resume_and_is_not_replayed(tmp_path):
    async def go():
        core, model, events, _, _ = await ready_core(tmp_path)
        try:
            cid = (await core.call("session.new", {"projectId": str(tmp_path / "proj")}))["id"]
            main = core._by_cid[cid].controller
            main.enable_fusion(Policy("chatgpt:gpt-6.1-sol", ("chatgpt:gpt-6-luna",), "chatgpt:gpt-6-luna", 5))
            task = {"scope": "src", "model": "chatgpt:gpt-6-luna", "role": "worker", "task": "Inspect."}
            model.script(call("delegate_task", task, call_id="d1"), [("hold", "expert"), *say("Never replay this.")])
            await core.call("session.send", {"id": cid, "text": "Inspect"})
            await until(lambda: len(model.requests) >= 2 and bool(main.agents))
            await core.call("session.stop", {"id": cid})
            await until(lambda: main.state.value == "ready" and next(iter(main.agents.values())).record.status == "interrupted")
            agent = next(iter(main.agents))
            native = main.session_id
            await core.call("session.close", {"id": cid})
            await until(lambda: main.state.value == "closed")
            cid = (await core.call("session.openSaved", {"projectId": str(tmp_path / "proj"), "id": "cg:" + native}))["id"]
            await until(lambda: core._by_cid[cid].controller.state.value == "ready")
            main = core._by_cid[cid].controller
            before = len(model.requests)
            model.script(call("delegate_task", task, call_id="d2"), say("Need explicit permission."))
            await core.call("session.send", {"id": cid, "text": "Continue"})
            await finish_turn(main)
            assert len(model.requests) == before + 2 and main.agents[agent].record.status == "interrupted"
            assert (await core.call("fusion.resumeExpert", {"id": cid, "agentId": agent}))["ok"]
            assert len(model.requests) == before + 2  # Confirmation itself never starts work.
            model.script(call("delegate_task", task, call_id="d3"), say("Checked safely."), say("Reviewed."))
            await core.call("session.send", {"id": cid, "text": "Continue after checking"})
            await finish_turn(main)
            assert main.agents[agent].record.status == "done"
        finally:
            await core.shutdown()
    asyncio.run(go())


def test_explicit_transfer_keeps_prior_evidence_and_does_not_run_by_itself(tmp_path):
    async def go():
        core, model, events, _, _ = await ready_core(tmp_path)
        try:
            project = tmp_path / "proj"
            (project / "module.txt").write_text("FIRST_EXPERT_EVIDENCE " * 300, encoding="utf-8")
            cid = (await core.call("session.new", {"projectId": str(project)}))["id"]
            main = core._by_cid[cid].controller
            main.enable_fusion(Policy("chatgpt:gpt-6.1-sol", ("chatgpt:gpt-6-luna", "chatgpt:gpt-6.1-sol"), "chatgpt:gpt-6-luna", 5))
            task = {"scope": "module.txt", "model": "chatgpt:gpt-6-luna", "role": "worker", "task": "Inspect the file."}
            model.script(call("delegate_task", task, call_id="d1"), call("read_file", {"file_path": "module.txt"}, call_id="read1"),
                         say("Prior expert report."), say("Reviewed."))
            await core.call("session.send", {"id": cid, "text": "Inspect"})
            await finish_turn(main)
            assert "read_file" in str(model.requests[-1]["input"])
            assert "Full tool output retained" in str(model.requests[-1]["input"])
            original = next(iter(main.agents))
            count = len(model.requests)
            result = await core.call("fusion.transferExpert", {"id": cid, "agentId": original, "model": "chatgpt:gpt-6.1-sol", "role": "worker"})
            replacement = result["agentId"]
            assert replacement != original and len(model.requests) == count
            assert main._fusion.registry.agents[original].status == "retired"
            native = main.session_id
            await core.call("session.close", {"id": cid})
            await until(lambda: main.state.value == "closed")
            cid = (await core.call("session.openSaved", {"projectId": str(project), "id": "cg:" + native}))["id"]
            await until(lambda: core._by_cid[cid].controller.state.value == "ready")
            main = core._by_cid[cid].controller
            task.update(model="chatgpt:gpt-6.1-sol", task="Continue inspection.")
            model.script(call("delegate_task", task, call_id="d2"), say("New expert reviewed prior evidence."), say("Done."))
            await core.call("session.send", {"id": cid, "text": "Continue"})
            await finish_turn(main)
            child = main.agents[replacement].controller
            assert child is not None and "transferred" in str(model.requests[-2]["input"]).lower()
            refs = list(child._outputs.root.glob("o-*.txt"))
            assert any("Prior expert report." in path.read_text(encoding="utf-8") for path in refs)
            assert any("FIRST_EXPERT_EVIDENCE" in path.read_text(encoding="utf-8") for path in refs)
        finally:
            await core.shutdown()
    asyncio.run(go())


def test_compaction_rebuilds_owner_and_full_approved_plan(tmp_path):
    async def go():
        with FakeResponses() as fake:
            engine = make_engine(tmp_path, fake)
            main = await engine.open_new(tmp_path / "project", model="gpt-6.1-sol")
            main.enable_fusion(Policy("chatgpt:gpt-6.1-sol", ("chatgpt:gpt-6-luna",), "chatgpt:gpt-6-luna", 5))
            plan = "Approved important steps: " + "verify every boundary. " * 20
            main.saved.plan = {"id": "approved-1", "status": "approved", "text": plan}
            fake.script(call("delegate_task", {"scope": "src/module", "model": "chatgpt:gpt-6-luna", "role": "worker", "task": "Inspect."}),
                        say("Expert evidence."), say("Long initial report " + "details " * 1000), say("Summary of the work."), say("Continued."))
            try:
                assert main.send_prompt("Inspect")
                await finish_turn(main)
                assert any(plan in part.get("text", "") for item in fake.requests[1]["input"]
                           for part in item.get("content", []) if isinstance(part, dict))
                assert main.start_compaction() is None
                await finish_turn(main)
                continuity = main.saved.compaction["continuity"]
                assert "src/module" in continuity and plan in continuity
                assert main.send_prompt("Continue")
                await finish_turn(main)
                next_input = str(fake.requests[-1]["input"])
                assert "src/module" in next_input and plan in next_input
            finally:
                await engine.close_all()
    asyncio.run(go())


def test_unknown_main_usage_blocks_automatic_delegation_and_further_requests(tmp_path):
    async def go():
        with FakeResponses() as fake:
            engine = make_engine(tmp_path, fake)
            main = await engine.open_new(tmp_path / "project", model="gpt-6.1-sol")
            main.enable_fusion(Policy("chatgpt:gpt-6.1-sol", ("chatgpt:gpt-6-luna",), "chatgpt:gpt-6-luna", 3, 50_000))
            event = call("delegate_task", {"scope": "src", "model": "chatgpt:gpt-6-luna", "role": "worker", "task": "Inspect."})
            event[-1]["response"]["usage"] = None
            fake.script(event, say("must not be sent"))
            try:
                assert main.send_prompt("Inspect")
                await finish_turn(main)
                assert len(fake.requests) == 1 and not main.agents
                assert main._fusion.registry.unmetered and main.saved.turns[-1].status == "failed"
            finally:
                await engine.close_all()
    asyncio.run(go())


def test_explicit_main_model_change_preserves_session_and_never_falls_back(tmp_path, monkeypatch):
    async def go():
        core, model, events, _, _ = await ready_core(tmp_path)
        try:
            cid = (await core.call("session.new", {"projectId": str(tmp_path / "proj")}))["id"]
            main = core._by_cid[cid].controller
            main.enable_fusion(Policy("chatgpt:gpt-6.1-sol", ("chatgpt:gpt-6-luna",), "chatgpt:gpt-6-luna", 5))
            model.script(say("Ready."))
            await core.call("session.send", {"id": cid, "text": "Start"})
            await finish_turn(main)
            count = len(model.requests)
            native = main.session_id
            with pytest.raises(AppError):
                await core.call("session.choose", {"id": cid, "field": "model", "value": "chatgpt:gpt-6-luna"})
            assert (await core.call("fusion.changeMain", {"id": cid, "model": "chatgpt:gpt-6-luna"}))["ok"]
            assert len(model.requests) == count and main.session_id == native
            assert main.model == "gpt-6-luna" and main._fusion.registry.policy.main == "chatgpt:gpt-6-luna"
            with pytest.raises(AppError):
                await core.call("fusion.changeMain", {"id": cid, "model": "chatgpt:missing"})
            assert main.model == "gpt-6-luna"
            await core.call("session.close", {"id": cid})
            await until(lambda: main.state.value == "closed")
            cid = (await core.call("session.openSaved", {"projectId": str(tmp_path / "proj"), "id": "cg:" + native}))["id"]
            main = core._by_cid[cid].controller
            await until(lambda: main.state.value == "ready")
            assert main.model == "gpt-6-luna" and main._fusion.registry.policy.main == "chatgpt:gpt-6-luna"
            model.script(say("Continued on the chosen model."))
            await core.call("session.send", {"id": cid, "text": "Continue"})
            await finish_turn(main)
            assert model.requests[-1]["model"] == "gpt-6-luna"
            save = main.manager.sessions.save
            def fail_save(_saved):
                raise OSError("simulated disk failure")
            monkeypatch.setattr(main.manager.sessions, "save", fail_save)
            with pytest.raises(ChatGPTError):
                await core.call("fusion.changeMain", {"id": cid, "model": "chatgpt:gpt-6.1-sol"})
            assert main.model == "gpt-6-luna" and main._fusion.registry.policy.main == "chatgpt:gpt-6-luna"
            monkeypatch.setattr(main.manager.sessions, "save", save)
        finally:
            await core.shutdown()
    asyncio.run(go())


def test_outside_pool_requires_explicit_session_only_authorization(tmp_path):
    async def go():
        core, model, events, _, _ = await ready_core(tmp_path)
        try:
            cid = (await core.call("session.new", {"projectId": str(tmp_path / "proj")}))["id"]
            main = core._by_cid[cid].controller
            main.enable_fusion(Policy("chatgpt:gpt-6.1-sol", ("chatgpt:gpt-6-luna",), "chatgpt:gpt-6-luna", 5))
            task = {"scope": "src", "model": "chatgpt:gpt-6.1-sol", "role": "worker", "task": "Inspect."}
            model.script(call("delegate_task", task, call_id="d1"), say("I need authorization."))
            await core.call("session.send", {"id": cid, "text": "Inspect"})
            await finish_turn(main)
            assert not main.agents and len(model.requests) == 2
            assert (await core.call("fusion.authorizeModel", {"id": cid, "model": "chatgpt:gpt-6.1-sol"}))["ok"]
            assert core.settings.fusion_workers == [] and len(model.requests) == 2
            model.script(call("delegate_task", task, call_id="d2"), say("Expert inspected."), say("Reviewed."))
            await core.call("session.send", {"id": cid, "text": "Proceed with the authorized model"})
            await finish_turn(main)
            assert len(main.agents) == 1 and len(model.requests) == 5
        finally:
            await core.shutdown()
    asyncio.run(go())


def test_budget_extension_requires_explicit_acknowledgement_of_unknown_usage(tmp_path):
    async def go():
        core, model, events, _, _ = await ready_core(tmp_path)
        try:
            cid = (await core.call("session.new", {"projectId": str(tmp_path / "proj")}))["id"]
            main = core._by_cid[cid].controller
            main.enable_fusion(Policy("chatgpt:gpt-6.1-sol", ("chatgpt:gpt-6-luna",), "chatgpt:gpt-6-luna", 2, 50_000))
            events = call("delegate_task", {"scope": "src", "model": "chatgpt:gpt-6-luna", "role": "worker", "task": "Inspect."})
            events[-1]["response"]["usage"] = None
            model.script(events)
            await core.call("session.send", {"id": cid, "text": "Inspect"})
            await finish_turn(main)
            from desktop.core.app_core import AppError
            with pytest.raises(AppError):
                await core.call("fusion.updateBudget", {"id": cid, "maxCalls": 3, "maxTokens": 100_000, "acceptUnknown": False})
            assert main._fusion.registry.unmetered and len(model.requests) == 1
            assert (await core.call("fusion.updateBudget", {"id": cid, "maxCalls": 3, "maxTokens": 100_000, "acceptUnknown": True}))["ok"]
            assert not main._fusion.registry.unmetered and main._fusion.registry.had_unknown
            assert len(model.requests) == 1
            native = main.session_id
            await core.call("session.close", {"id": cid})
            await until(lambda: main.state.value == "closed")
            cid = (await core.call("session.openSaved", {"projectId": str(tmp_path / "proj"), "id": "cg:" + native}))["id"]
            await until(lambda: core._by_cid[cid].controller.state.value == "ready")
            restored = core._by_cid[cid].controller
            assert restored._fusion.registry.had_unknown and not restored._fusion.registry.unmetered
            assert restored._fusion.registry.policy.max_calls == 3
        finally:
            await core.shutdown()
    asyncio.run(go())


def test_fixed_main_model_cannot_be_changed_without_leaving_fusion(tmp_path):
    async def go():
        with FakeResponses() as fake:
            engine = make_engine(tmp_path, fake)
            main = await engine.open_new(tmp_path / "project", model="gpt-6.1-sol")
            try:
                main.enable_fusion(Policy("chatgpt:gpt-6.1-sol", ("chatgpt:gpt-6-luna",), "chatgpt:gpt-6-luna", 2))
                assert not await main.update_settings(model="gpt-6-luna")
                assert main.model == "gpt-6.1-sol"
            finally:
                await engine.close_all()
    asyncio.run(go())
