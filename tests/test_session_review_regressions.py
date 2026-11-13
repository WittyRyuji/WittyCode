"""Focused regressions found in the task's single formal review."""

from __future__ import annotations

import asyncio

from tests.app_support import ready_core
from tests.fakes import until
from tests.test_app_core import devin_core, idle, new_session, talk
from tests.test_factory import engine_for


def test_factory_does_not_copy_the_first_sentence_while_waiting_for_its_native_llm_title(tmp_path):
    async def scenario():
        engine, _, project = engine_for(tmp_path)
        controller = await engine.open_new(project, model="demo-model")
        try:
            assert controller.send_prompt("检查库存接口的参数校验")
            assert controller.title == "新会话", "wait for session_title_updated instead of copying the question"
        finally:
            await engine.close_all()

    asyncio.run(scenario())


def test_factory_segment_restores_only_the_typed_question_beside_the_archived_prefix(tmp_path):
    async def scenario():
        factory, _, _ = engine_for(tmp_path)
        core, plan, events, _, _ = await ready_core(tmp_path, factory_engine=factory)
        try:
            await until(lambda: any(m["source"] == "factory" for m in core.catalog.models))
            cid = await new_session(core, events, tmp_path / "proj")
            await talk(core, plan, cid, "检查库存导入", "检查重复行和事务边界。")
            old_row = core._row_of(core._by_cid[cid])
            await core.call("session.choose", {"id": cid, "field": "model", "value": "factory:demo-model"})
            await core.call("session.send", {"id": cid, "text": "继续检查事务"})
            await idle(core, cid)
            await core.call("session.close", {"id": cid})
            await core.call("project.refresh")
            cid = (await core.call("session.openSaved", {"id": old_row, "projectId": str(tmp_path / "proj")}))["id"]
            await idle(core, cid)
            turns = core._by_cid[cid].controller.transcript.turns
            assert [t.user_block.text for t in turns if t.user_block] == ["检查库存导入", "继续检查事务"]
        finally:
            await core.shutdown()

    asyncio.run(scenario())


def test_the_final_handoff_is_saved_when_the_target_window_is_learned_after_open(tmp_path, monkeypatch):
    async def scenario():
        core, plan, events, _, _ = await devin_core(tmp_path)
        try:
            cid = await new_session(core, events, tmp_path / "proj")
            await talk(core, plan, cid, "问题" * 7500, "回答" * 7500)
            core.catalog.find("devin:swe-2-high")["window"] = 0
            async def learned(*args):
                return 1_000_000
            monkeypatch.setattr(core, "_window_of", learned)
            await core.call("session.choose", {"id": cid, "field": "model", "value": "devin:swe-2-high"})
            conv = core._by_cid[cid]
            saved = core.history.load(core._row_of(conv))
            assert conv.controller.handoff.carried == 2
            assert saved["handoff"] == conv.controller.handoff, "a restart must not restore the preliminary smaller handoff"
        finally:
            await core.shutdown()

    asyncio.run(scenario())
