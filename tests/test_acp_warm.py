"""The warm timer owns a transport, not a conversation or expert."""
from __future__ import annotations

import asyncio

from desktop.core.devin import controller as devin_ctl
from desktop.core.fusion import acp
from desktop.core.fusion.registry import Policy
from desktop.core.session_state import SessionState
from tests.app_support import ready_core
from tests.chatgpt_support import finish_turn as finish_own_turn
from tests.devin_support import finish_turn, make_engine, until
from tests.fake_responses import call, say
from tests.fusion_acp_support import make_guarded_engine


def test_shared_devin_wakes_the_same_native_session_after_idle(tmp_path, monkeypatch):
    monkeypatch.setattr(devin_ctl, "ACP_WARM_SECONDS", 0.12)

    async def go():
        harness = make_engine(tmp_path)
        try:
            first = await harness.engine.open_new(tmp_path)
            second = await harness.engine.open_new(tmp_path)
            assert (await finish_turn(first, "First message")).status.value == "done"
            initial = harness.clients[-1]
            first_id = first.session_id
            await until(lambda: first._parked and first._client is None)
            assert first.state is SessionState.READY and second._client is initial and initial.running
            assert (await finish_turn(first, "Second message")).status.value == "done"
            assert first.session_id == first_id and first._client is initial and len(first.transcript.turns) == 2
            await second.close()
            await until(lambda: not initial.running)
            assert (await finish_turn(first, "Third message")).status.value == "done"
            assert first.session_id == first_id and first._client is not initial and len(first.transcript.turns) == 3
            loads = harness.requests("session/load")
            assert [item["sessionId"] for item in loads] == [first_id, first_id]
        finally:
            await harness.engine.close_all()

    asyncio.run(go())


def test_failed_native_load_returns_new_message_without_dispatch(tmp_path, monkeypatch):
    from desktop.core.devin.errors import DevinError

    monkeypatch.setattr(devin_ctl, "ACP_WARM_SECONDS", 0.1)

    async def go():
        harness = make_engine(tmp_path)
        core, _, events, _, _ = await ready_core(tmp_path, devin_engine=harness.engine)
        try:
            model = "devin:swe-2-high"
            await until(lambda: core.catalog.find(model))
            cid = (await core.call("session.new", {"projectId": str(tmp_path / "proj"), "modelId": model}))["id"]
            ctl = core._by_cid[cid].controller
            await core.call("session.send", {"id": cid, "text": "First message"})
            await finish_own_turn(ctl)
            await until(lambda: ctl._parked and ctl._client is None)
            count = len(harness.requests("session/prompt"))

            async def missing(*_args, **_kwargs):
                raise DevinError("Original test session unavailable")

            monkeypatch.setattr(ctl, "_load", missing)
            await core.call("session.send", {"id": cid, "text": "Keep this draft"})
            await until(lambda: bool(events.of("restore")))
            assert events.last("restore")["text"] == "Keep this draft"
            assert len(harness.requests("session/prompt")) == count
            assert ctl._parked and ctl.state is SessionState.READY
        finally:
            await core.shutdown()

    asyncio.run(go())


def test_fusion_expert_warm_reuse_then_native_load(tmp_path, monkeypatch):
    monkeypatch.setattr(acp, "ACP_WARM_SECONDS", 0.4)

    async def go():
        harness = make_guarded_engine(tmp_path)
        core, fake, _, _, _ = await ready_core(tmp_path, devin_engine=harness.engine)
        try:
            model = "devin:swe-2-high"
            await until(lambda: core.catalog.find(model), timeout=20)
            cid = (await core.call("session.new", {"projectId": str(tmp_path / "proj")}))["id"]
            main = core._by_cid[cid].controller
            main.enable_fusion(Policy("chatgpt:gpt-6.1-sol", (model,), model, 0))
            brief = {"scope": "src", "model": model, "role": "worker", "task": "[fusion-read] Inspect."}
            (tmp_path / "proj" / "evidence.txt").write_text("isolated sample", encoding="utf-8")

            async def delegate(text):
                fake.script(call("delegate_task", brief), say("Reviewed."))
                assert main.send_prompt(text)
                await finish_own_turn(main)
                (view,) = main.agents.values()
                assert view.record.status == "done"
                return view

            view = await delegate("First")
            child = view.controller
            client = child._acp.client
            saved = child.saved.origin["fusionAcp"].copy()
            # Prove warm reuse without a wall-clock race against the 400 ms fake timer.
            child._acp._cancel_idle()
            assert (await delegate("Second")).controller is child and child._acp.client is client
            await until(lambda: child._acp.client is None, timeout=12)
            assert (await delegate("Third")).controller is child
            assert child._acp.client is not client
            assert child.saved.origin["fusionAcp"]["session"] == saved["session"]
        finally:
            await core.shutdown()

    asyncio.run(go())
