"""Creation order is independent of activity; a dragged order survives native/open row changes and restarts."""

from __future__ import annotations

import asyncio
import json
import time

import pytest

from desktop.core.app_core import AppError
from desktop.core.settings import settings_from_dict, settings_to_dict
from tests.app_support import make_core, ready_core, save_conversation
from tests.fakes import until
from tests.test_app_core import idle, listed, talk, three_saved


def test_creation_not_modification_decides_the_default_order_and_old_indices_are_migrated(tmp_path):
    async def scenario():
        core, _, events, _, engine = await ready_core(tmp_path, projects=False)
        proj, _ = await three_saved(core, events, tmp_path)
        old = engine.sessions.load("old")
        old.modified_at = time.time()
        engine.sessions.save(old)
        index_path = engine.sessions.root / "index.json"
        index = json.loads(index_path.read_text(encoding="utf-8"))
        for row in index["sessions"].values():
            row.pop("created_at")
        index_path.write_text(json.dumps(index), encoding="utf-8")
        await core.call("project.refresh")
        core._emit_sidebar()
        assert listed(events) == ["cg:new", "cg:mid", "cg:old"]
        assert engine.sessions.rows(str(proj))[0].created_at == old.created_at, "creation time comes from the archive, not activity"
        await core.shutdown()

    asyncio.run(scenario())


def test_dragged_order_survives_opening_activity_closing_refresh_and_restart(tmp_path):
    async def scenario():
        core, plan, events, _, _ = await ready_core(tmp_path, projects=False)
        proj, _ = await three_saved(core, events, tmp_path)
        await core.call("session.reorder", {"projectId": str(proj), "id": "cg:old", "targetId": "cg:new", "after": False})
        assert listed(events) == ["cg:old", "cg:new", "cg:mid"]
        cid = (await core.call("session.openSaved", {"id": "cg:old", "projectId": str(proj)}))["id"]
        await idle(core, cid)
        await talk(core, plan, cid, "keep my position")
        core._emit_sidebar()
        assert listed(events) == [cid, "cg:new", "cg:mid"]
        await core.call("session.reorder", {"projectId": str(proj), "id": cid, "targetId": "cg:mid", "after": True})
        assert listed(events) == ["cg:new", "cg:mid", cid]
        await core.call("session.close", {"id": cid})
        await core.call("project.refresh")
        core._emit_sidebar()
        assert listed(events) == ["cg:new", "cg:mid", "cg:old"]
        settings = settings_from_dict(settings_to_dict(core.settings))
        await core.shutdown()

        again, _, _, _, _ = make_core(tmp_path, settings=settings)
        await again.call("app.init")
        await again.call("project.refresh")
        assert again.project_rows()[0]["sessionIds"] == ["cg:new", "cg:mid", "cg:old"]
        save_conversation(again.chatgpt, proj, "later", "A later conversation", minutes_ago=0)
        await again.call("project.refresh")
        assert again.project_rows()[0]["sessionIds"] == ["cg:later", "cg:new", "cg:mid", "cg:old"], "new sessions precede the manual order"
        await again.call("session.archive", {"id": "cg:mid"})
        await again.call("session.unarchive", {"id": "cg:mid"})
        assert again.project_rows()[0]["sessionIds"] == ["cg:later", "cg:new", "cg:mid", "cg:old"]
        await again.shutdown()

    asyncio.run(scenario())


def test_drag_refuses_unknown_rows_cross_project_moves_and_invalid_values(tmp_path):
    async def scenario():
        core, _, events, _, _ = await ready_core(tmp_path, projects=False)
        proj, _ = await three_saved(core, events, tmp_path)
        other = tmp_path / "other"
        other.mkdir()
        save_conversation(core.chatgpt, other, "other", "Another project")
        await core.call("project.add", {"path": str(other)})
        await core.call("project.refresh")
        for change in ({"id": "unknown"}, {"targetId": "cg:other"}, {"after": 1}, {"id": []}, {"projectId": []}):
            params = {"projectId": str(proj), "id": "cg:old", "targetId": "cg:new", "after": False, **change}
            with pytest.raises(AppError):
                await core.call("session.reorder", params)
        assert core.settings.session_order == []
        await core.shutdown()

    asyncio.run(scenario())


def test_rows_do_not_vanish_at_an_activity_based_cutoff(tmp_path):
    async def scenario():
        core, _, _, _, engine = await ready_core(tmp_path)
        proj = (tmp_path / "proj").resolve()
        for i in range(52):
            save_conversation(engine, proj, f"row-{i}", f"Conversation {i}", minutes_ago=i)
        await core.call("project.refresh")
        ids = core.project_rows()[0]["sessionIds"]
        assert len(ids) == 52 and ids[-1] == "cg:row-51"
        old = engine.sessions.load("row-51")
        old.modified_at = time.time()
        engine.sessions.save(old)
        await core.call("project.refresh")
        assert core.project_rows()[0]["sessionIds"] == ids
        await core.shutdown()

    asyncio.run(scenario())


def test_manual_order_and_creation_identity_follow_an_engine_change(tmp_path):
    from tests.test_app_core import devin_core

    async def scenario():
        core, _, events, _, _ = await devin_core(tmp_path, projects=False)
        proj, _ = await three_saved(core, events, tmp_path)
        cid = (await core.call("session.openSaved", {"id": "cg:old", "projectId": str(proj)}))["id"]
        await idle(core, cid)
        created = core._by_cid[cid].created_at
        await core.call("session.reorder", {"projectId": str(proj), "id": cid, "targetId": "cg:new", "after": False})
        assert (await core.call("session.choose", {"id": cid, "field": "model", "value": "devin:swe-2-high"}))["switched"]
        conv = core._by_cid[cid]
        key = "dv:" + conv.controller.session_id
        core._emit_sidebar()
        assert listed(events) == [cid, "cg:new", "cg:mid"]
        assert core.settings.session_order == [key, "cg:new", "cg:mid"]
        assert conv.created_at == core.settings.session_created[key] == created
        assert "cg:old" not in core.settings.session_created
        await core.shutdown()

    asyncio.run(scenario())


def test_order_settings_are_sanitized_and_round_trip():
    settings = settings_from_dict({"session_order": ["cg:b", "cg:a", "cg:b", 1, "", []], "session_created": {"cg:a": 12, "bad": True, "nan": float("nan"), "inf": float("inf"), "negative": -1}})
    assert settings.session_order == ["cg:b", "cg:a"] and settings.session_created == {"cg:a": 12.0}
    assert settings_from_dict(settings_to_dict(settings)) == settings
