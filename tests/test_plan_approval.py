"""Explicit approval of a durable, versioned plan; all model traffic is local."""
from __future__ import annotations

import asyncio

import pytest

from desktop.core.app_core import AppError
from desktop.core.chatgpt.errors import ChatGPTError
from desktop.core.session_state import SessionState
from tests.app_support import ready_core
from tests.fake_responses import call, say
from tests.fakes import until


async def _new(core, events, project):
    cid = (await core.call("session.new", {"projectId": str(project)}))["id"]
    await until(lambda: core._by_cid[cid].controller.state is SessionState.READY)
    assert (await core.call("session.choose", {"id": cid, "field": "mode", "value": "spec"}))["ok"]
    return cid


def test_submitting_plan_stops_later_tools_in_the_same_response(tmp_path):
    async def go():
        core, model, events, _, _ = await ready_core(tmp_path)
        try:
            cid = await _new(core, events, tmp_path / "proj")
            ctl = core._by_cid[cid].controller
            (tmp_path / "proj" / "private.txt").write_text("SHOULD_NOT_BE_READ", encoding="utf-8")
            batch = [*call("submit_plan", {"plan": "A complete plan."}, call_id="plan-call", item_id="plan-item")[:-1],
                     *call("read_file", {"file_path": "private.txt"}, call_id="later-call", item_id="later-item")]
            model.script(batch)
            await core.call("session.send", {"id": cid, "text": "Plan only"})
            await until(lambda: ctl.state is SessionState.READY and bool(ctl.saved.plan))
            outputs = {item.get("call_id"): item.get("output", "") for item in ctl.saved.items if item.get("type") == "function_call_output"}
            assert set(outputs) == {"plan-call", "later-call"}
            assert "SHOULD_NOT_BE_READ" not in outputs["later-call"]
            assert "not run" in outputs["later-call"].lower()
            assert len(model.requests) == 1 and ctl.mode == "spec"
        finally:
            await core.shutdown()
    asyncio.run(go())


def test_plan_waits_for_approval_and_uses_the_same_session(tmp_path):
    async def go():
        core, model, events, _, _ = await ready_core(tmp_path)
        try:
            cid = await _new(core, events, tmp_path / "proj")
            ctl = core._by_cid[cid].controller
            native, selected = ctl.session_id, ctl.model
            model.script(call("submit_plan", {"plan": "# Plan\n\n1. Change the module.\n2. Run tests."}), say("Executed."))
            await core.call("session.send", {"id": cid, "text": "Plan the change"})
            await until(lambda: ctl.state is SessionState.READY and bool(ctl.saved.plan))
            pending = ctl.saved.plan
            assert pending["status"] == "pending" and "Run tests" in pending["text"]
            assert ctl.mode == "spec" and len(model.requests) == 1
            assert core._session_meta(core._by_cid[cid])["pendingPlan"] == {"id": pending["id"], "text": pending["text"]}
            assert not (await core.call("session.approvePlan", {"id": cid, "planId": "stale"}))["ok"]
            assert ctl.mode == "spec" and len(model.requests) == 1
            assert (await core.call("session.approvePlan", {"id": cid, "planId": pending["id"]}))["ok"]
            await until(lambda: ctl.state is SessionState.READY and len(model.requests) == 2)
            assert ctl.mode == "auto" and ctl.model == selected and ctl.session_id == native
            assert any(pending["text"] in part.get("text", "") for item in model.requests[1]["input"]
                       for part in item.get("content", []) if isinstance(part, dict))
            assert ctl.saved.plan["status"] == "approved" and ctl.saved.plan["execution"] == "started"
            assert not (await core.call("session.approvePlan", {"id": cid, "planId": pending["id"]}))["ok"]
            assert len(model.requests) == 2
        finally:
            await core.shutdown()
    asyncio.run(go())


def test_start_failure_restores_the_pending_plan(tmp_path, monkeypatch):
    async def go():
        core, model, events, _, _ = await ready_core(tmp_path)
        try:
            cid = await _new(core, events, tmp_path / "proj")
            ctl = core._by_cid[cid].controller
            model.script(call("submit_plan", {"plan": "Review then test."}))
            await core.call("session.send", {"id": cid, "text": "Plan"})
            await until(lambda: ctl.state is SessionState.READY and bool(ctl.saved.plan))
            pending = dict(ctl.saved.plan)
            monkeypatch.setattr(ctl, "send_prompt", lambda *args, **kwargs: False)
            assert not (await core.call("session.approvePlan", {"id": cid, "planId": pending["id"]}))["ok"]
            assert ctl.saved.plan == pending and ctl.mode == "spec" and ctl.state is SessionState.READY
            assert ctl.manager.sessions.load(ctl.session_id).plan == pending and len(model.requests) == 1
        finally:
            await core.shutdown()
    asyncio.run(go())


def test_interrupted_approval_start_requires_a_new_explicit_decision(tmp_path):
    async def go():
        core, model, events, _, _ = await ready_core(tmp_path)
        try:
            cid = await _new(core, events, tmp_path / "proj")
            ctl = core._by_cid[cid].controller
            model.script(call("submit_plan", {"plan": "Review then test."}))
            await core.call("session.send", {"id": cid, "text": "Plan"})
            await until(lambda: ctl.state is SessionState.READY and bool(ctl.saved.plan))
            original = dict(ctl.saved.plan)
            ctl.saved.plan = {**original, "status": "approved", "execution": "starting"}
            ctl.mode = "auto"
            ctl._save_now(required=True)
            native = ctl.session_id
            await core.call("session.close", {"id": cid})
            await until(lambda: ctl.state is SessionState.CLOSED)
            reopened = (await core.call("session.openSaved", {"projectId": str(tmp_path / "proj"), "id": "cg:" + native}))["id"]
            await until(lambda: core._by_cid[reopened].controller.state is SessionState.READY)
            restored = core._by_cid[reopened].controller
            assert restored.mode == "spec" and restored.saved.plan["status"] == "pending"
            assert restored.saved.plan["text"] == original["text"] and restored.saved.plan["id"] != original["id"]
            assert "副作用" in core._session_meta(core._by_cid[reopened])["pendingPlan"]["warning"]
            assert not (await core.call("session.approvePlan", {"id": reopened, "planId": original["id"]}))["ok"]
            assert len(model.requests) == 1
        finally:
            await core.shutdown()
    asyncio.run(go())


def test_queued_continue_does_not_bypass_plan_or_hide_it_after_mode_switch(tmp_path):
    async def go():
        core, model, events, _, _ = await ready_core(tmp_path)
        try:
            cid = await _new(core, events, tmp_path / "proj")
            conv = core._by_cid[cid]
            ctl = conv.controller
            model.script([("hold", "planning"), *call("submit_plan", {"plan": "Review the plan."})], say("must not run"))
            await core.call("session.send", {"id": cid, "text": "Plan"})
            await until(lambda: len(model.requests) == 1)
            assert (await core.call("session.send", {"id": cid, "text": "继续", "how": "queue"}))["queued"]
            await core.call("session.choose", {"id": cid, "field": "mode", "value": "auto"})
            model.release("planning")
            await until(lambda: ctl.state is SessionState.READY and ctl.saved.plan.get("status") == "pending")
            await until(lambda: not conv.queue)
            assert ctl.mode == "spec" and len(model.requests) == 1
            assert core._session_meta(conv)["pendingPlan"]["text"] == "Review the plan."
            assert any(data.get("text") == "继续" for data in events.of("restore"))
        finally:
            model.release("planning")
            await core.shutdown()
    asyncio.run(go())


def test_approval_rejects_unsent_attachments_and_mode_switch_is_not_approval(tmp_path):
    async def go():
        core, model, events, _, _ = await ready_core(tmp_path)
        try:
            cid = await _new(core, events, tmp_path / "proj")
            ctl = core._by_cid[cid].controller
            model.script(call("submit_plan", {"plan": "A test plan."}))
            await core.call("session.send", {"id": cid, "text": "Plan"})
            await until(lambda: ctl.state is SessionState.READY and bool(ctl.saved.plan))
            ident = ctl.saved.plan["id"]
            with pytest.raises(AppError, match="计划"):
                await core.call("session.choose", {"id": cid, "field": "model", "value": "devin:swe-2-high"})
            assert ctl.saved.plan["id"] == ident
            await core.call("session.choose", {"id": cid, "field": "mode", "value": "auto"})
            assert not (await core.call("session.approvePlan", {"id": cid, "planId": ident}))["ok"]
            with pytest.raises(AppError, match="计划仍待确认"):
                await core.call("session.send", {"id": cid, "text": "继续"})
            assert ctl.saved.plan["id"] == ident and len(model.requests) == 1
            await core.call("session.choose", {"id": cid, "field": "mode", "value": "spec"})
            await core.call("attachments.add", {"id": cid, "files": [{"name": "note.txt", "type": "text/plain", "data": "aGVsbG8="}]})
            assert not (await core.call("session.approvePlan", {"id": cid, "planId": ident}))["ok"]
            assert ctl.saved.plan["status"] == "pending" and len(model.requests) == 1
        finally:
            await core.shutdown()
    asyncio.run(go())


def test_approval_and_revision_do_not_run_when_checkpoint_fails(tmp_path, monkeypatch):
    async def go():
        core, model, events, _, _ = await ready_core(tmp_path)
        try:
            cid = await _new(core, events, tmp_path / "proj")
            ctl = core._by_cid[cid].controller
            model.script(call("submit_plan", {"plan": "A plan with a test."}))
            await core.call("session.send", {"id": cid, "text": "Plan the work"})
            await until(lambda: ctl.state is SessionState.READY and bool(ctl.saved.plan))
            plan = dict(ctl.saved.plan)
            original = ctl.manager.sessions.save
            def fail(_saved):
                raise OSError("demo checkpoint failure")
            monkeypatch.setattr(ctl.manager.sessions, "save", fail)
            with pytest.raises(ChatGPTError):
                await core.call("session.approvePlan", {"id": cid, "planId": plan["id"]})
            assert ctl.mode == "spec" and ctl.saved.plan == plan and len(model.requests) == 1
            with pytest.raises(ChatGPTError):
                await core.call("session.send", {"id": cid, "text": "Revise this"})
            assert ctl.mode == "spec" and ctl.saved.plan == plan and len(model.requests) == 1
            monkeypatch.setattr(ctl.manager.sessions, "save", original)
            assert ctl.manager.sessions.load(ctl.session_id).plan == plan
        finally:
            await core.shutdown()
    asyncio.run(go())


def test_an_incomplete_response_never_presents_a_plan(tmp_path):
    async def go():
        core, model, events, _, _ = await ready_core(tmp_path)
        try:
            cid = await _new(core, events, tmp_path / "proj")
            ctl = core._by_cid[cid].controller
            events = call("submit_plan", {"plan": "This response was cut."})
            events[-1] = {"type": "response.incomplete", "response": {"id": "demo", "status": "incomplete", "incomplete_details": {"reason": "max_output_tokens"}}}
            model.script(events)
            await core.call("session.send", {"id": cid, "text": "Plan this"})
            await until(lambda: ctl.state is SessionState.READY and bool(ctl.saved.turns) and ctl.saved.turns[-1].finished is not None)
            assert not ctl.saved.plan and ctl.saved.turns[-1].status == "incomplete"
            assert core._session_meta(core._by_cid[cid])["pendingPlan"] is None
        finally:
            await core.shutdown()
    asyncio.run(go())


def test_plan_revision_invalidates_the_old_approval_and_survives_reopening(tmp_path):
    async def go():
        core, model, events, _, _ = await ready_core(tmp_path)
        try:
            cid = await _new(core, events, tmp_path / "proj")
            ctl = core._by_cid[cid].controller
            model.script(call("submit_plan", {"plan": "First plan"}), call("submit_plan", {"plan": "Revised plan"}))
            await core.call("session.send", {"id": cid, "text": "Make a plan"})
            await until(lambda: ctl.state is SessionState.READY and bool(ctl.saved.plan))
            first = ctl.saved.plan["id"]
            await core.call("session.send", {"id": cid, "text": "Please revise it"})
            await until(lambda: ctl.state is SessionState.READY and ctl.saved.plan.get("text") == "Revised plan")
            second = ctl.saved.plan["id"]
            assert first != second and ctl.mode == "spec"
            assert not (await core.call("session.approvePlan", {"id": cid, "planId": first}))["ok"]
            native = ctl.session_id
            await core.call("session.close", {"id": cid})
            await until(lambda: ctl.state is SessionState.CLOSED)
            reopened = (await core.call("session.openSaved", {"projectId": str(tmp_path / "proj"), "id": "cg:" + native}))["id"]
            await until(lambda: core._by_cid[reopened].controller.state is SessionState.READY)
            resumed = core._by_cid[reopened].controller
            assert resumed.mode == "spec" and resumed.saved.plan == {"id": second, "text": "Revised plan", "status": "pending"}
            assert core._session_meta(core._by_cid[reopened])["pendingPlan"]["id"] == second
        finally:
            await core.shutdown()
    asyncio.run(go())
