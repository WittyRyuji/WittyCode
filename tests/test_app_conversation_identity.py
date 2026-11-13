"""An engine session is a segment of one WittyCode conversation, including after a restart."""

from __future__ import annotations

import asyncio

import pytest

from desktop.core.app_core import AppError
from desktop.core.settings import settings_from_dict, settings_to_dict
from tests.app_support import MODEL, make_core
from tests.devin_support import make_engine
from tests.test_app_core import devin_core, idle, new_session, talk


def test_engine_segments_stay_one_row_after_close_refresh_restart_and_archive(tmp_path):
    async def scenario():
        core, plan, events, _, _ = await devin_core(tmp_path)
        try:
            project = str(tmp_path / "proj")
            cid = await new_session(core, events, project)
            await talk(core, plan, cid, "检查库存导入流程", "先检查字段映射。")
            first = core._row_of(core._by_cid[cid])
            await core.call("session.rename", {"id": cid, "title": "库存导入检查"})
            await core.call("session.choose", {"id": cid, "field": "model", "value": "devin:swe-2-high"})
            await idle(core, cid)
            await core.call("session.send", {"id": cid, "text": "继续检查重复数据"})
            await idle(core, cid)
            await core.call("session.choose", {"id": cid, "field": "model", "value": MODEL})
            await talk(core, plan, cid, "汇总修复项", "补充校验与回归测试。")
            current = core._row_of(core._by_cid[cid])
            await core.call("session.close", {"id": cid})
            await core.call("project.refresh")
            assert core.project_rows()[0]["sessionIds"] == [current], "closed engine segments must not become separate conversations"
            assert core._saved_sessions_json()[current]["title"] == "库存导入检查"
            settings = settings_from_dict(settings_to_dict(core.settings))
        finally:
            await core.shutdown()

        again, _, _, _, _ = make_core(tmp_path, settings=settings, devin_engine=make_engine(tmp_path).engine)
        try:
            await again.call("app.init")
            await again.call("project.refresh")
            assert again.project_rows()[0]["sessionIds"] == [current]
            reopened = (await again.call("session.openSaved", {"id": first, "projectId": project}))["id"]
            await idle(again, reopened)
            conv = again._by_cid[reopened]
            assert again._row_of(conv) == current, "old links reopen the current segment, not a fork"
            users = [t.user_block.text for t in conv.controller.transcript.turns if t.user_block]
            assert users == ["检查库存导入流程", "继续检查重复数据", "汇总修复项"]
            await again.call("session.archive", {"id": reopened})
            await again.call("project.refresh")
            assert again.project_rows()[0]["sessionIds"] == []
            assert len(again._archived_json()) == 1
            await again.call("session.unarchive", {"id": current})
            assert again.project_rows()[0]["sessionIds"] == [current]
        finally:
            await again.shutdown()

    asyncio.run(scenario())


def test_switching_back_without_sending_preserves_the_pending_handoff_after_restart(tmp_path):
    async def scenario():
        core, plan, events, _, _ = await devin_core(tmp_path)
        try:
            cid = await new_session(core, events, tmp_path / "proj")
            await talk(core, plan, cid, "检查库存导入", "需要检查重复行。")
            await core.call("session.choose", {"id": cid, "field": "model", "value": "devin:swe-2-high"})
            await core.call("session.choose", {"id": cid, "field": "model", "value": MODEL})
            head = core._row_of(core._by_cid[cid])
            await core.call("session.close", {"id": cid})
            settings = settings_from_dict(settings_to_dict(core.settings))
        finally:
            await core.shutdown()
        again, service, _, _, _ = make_core(tmp_path, settings=settings)
        try:
            await again.call("app.init")
            await again.call("project.refresh")
            assert again.project_rows()[0]["sessionIds"] == [head]
            cid = (await again.call("session.openSaved", {"id": head, "projectId": str(tmp_path / "proj")}))["id"]
            await idle(again, cid)
            assert again._by_cid[cid].controller.handoff is not None
            await talk(again, service, cid, "继续检查")
            assert "重复行" in str(service.requests[-1]["input"]), "the pending handoff must reach the next model, not just the UI"
            assert [t.user_block.text for t in again._by_cid[cid].controller.transcript.turns if t.user_block] == ["检查库存导入", "继续检查"]
        finally:
            await again.shutdown()

    asyncio.run(scenario())


def test_history_write_failure_rolls_back_without_hiding_the_source(tmp_path, monkeypatch):
    async def scenario():
        core, plan, events, _, _ = await devin_core(tmp_path)
        try:
            cid = await new_session(core, events, tmp_path / "proj")
            await talk(core, plan, cid, "检查库存接口")
            conv = core._by_cid[cid]
            old = conv.controller
            before = list(old.transcript.turns)
            def denied(*args):
                raise OSError("history disk unavailable")
            monkeypatch.setattr(core.history, "save", denied)
            with pytest.raises(AppError, match="没有切换成功"):
                await core.call("session.choose", {"id": cid, "field": "model", "value": "devin:swe-2-high"})
            assert conv.controller is old and old.transcript.turns == before
            assert not core.settings.session_links and not conv.hidden
            assert conv.sidebar_key == core._row_of(conv)
        finally:
            await core.shutdown()

    asyncio.run(scenario())


def test_failed_engine_change_does_not_move_a_manual_title_or_sidebar_identity(tmp_path):
    async def scenario():
        core, plan, events, _, _ = await devin_core(tmp_path, wait_models=False, FAKE_DEVIN_OPEN_FAIL="1")
        try:
            cid = await new_session(core, events, tmp_path / "proj")
            await talk(core, plan, cid, "检查库存接口")
            await core.call("session.rename", {"id": cid, "title": "库存接口"})
            old = core._row_of(core._by_cid[cid])
            try:
                await core.call("session.choose", {"id": cid, "field": "model", "value": "devin:swe-2-high"})
            except Exception:
                pass
            assert core._row_of(core._by_cid[cid]) == old
            assert core._title_of(core._by_cid[cid]) == "库存接口"
            assert core._by_cid[cid].sidebar_key == old
        finally:
            await core.shutdown()

    asyncio.run(scenario())
