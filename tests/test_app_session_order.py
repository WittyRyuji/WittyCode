"""Significant activity promotes a conversation; streams stay put and dragged order persists until the next activity."""

from __future__ import annotations

import asyncio
import json
import time

import pytest

from desktop.core.app_core import AppError
from desktop.core.settings import folder_key, load_settings, settings_from_dict, settings_to_dict
from tests.app_support import make_core, ready_core, save_conversation
from tests.fakes import until
from tests.test_app_core import idle, listed, talk, three_saved


def test_last_activity_decides_default_order_and_old_indices_keep_creation_metadata(tmp_path):
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
        assert listed(events) == ["cg:old", "cg:new", "cg:mid"]
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
        assert core.project_rows()[0]["sessionIds"] == ["cg:row-51", *ids[:-1]], "all rows remain, ordered by activity"
        await core.shutdown()

    asyncio.run(scenario())


@pytest.mark.parametrize("pinned", [False, True])
def test_manual_order_and_creation_identity_follow_an_engine_change(tmp_path, pinned):
    from tests.test_app_core import ask_to_switch, devin_core

    async def scenario():
        core, _, events, _, _ = await devin_core(tmp_path, projects=False)
        proj, _ = await three_saved(core, events, tmp_path)
        cid = (await core.call("session.openSaved", {"id": "cg:old", "projectId": str(proj)}))["id"]
        await idle(core, cid)
        created = core._by_cid[cid].created_at
        await core.call("session.reorder", {"projectId": str(proj), "id": cid, "targetId": "cg:new", "after": False})
        if pinned:
            await core.call("session.pin", {"projectId": str(proj), "id": cid, "pinned": True})
        assert (await ask_to_switch(core, cid))["switched"]
        conv = core._by_cid[cid]
        key = "dv:" + conv.controller.session_id
        core._emit_sidebar()
        assert listed(events) == [cid, "cg:new", "cg:mid"]
        assert core.settings.session_order == [key, "cg:new", "cg:mid"]
        assert conv.created_at == core.settings.session_created[key] == created
        assert "cg:old" not in core.settings.session_created
        if pinned:
            assert core.settings.pinned_sessions == [key]
            assert core.settings.session_pins == {folder_key(proj): [key]}
            assert core._session_meta(conv)["pinned"] is True
            await core.call("session.close", {"id": cid})
            await core.call("project.refresh")
            assert core.project_rows()[0]["sessionIds"] == [key, "cg:new", "cg:mid"]
            reopened = (await core.call("session.openSaved", {"id": "cg:old", "projectId": str(proj)}))["id"]
            assert core.project_rows()[0]["sessionIds"] == [reopened, "cg:new", "cg:mid"]
            assert core._session_meta(core._by_cid[reopened])["pinned"] is True
        await core.shutdown()

    asyncio.run(scenario())


def test_pins_partition_activity_drag_archive_and_restart(tmp_path, monkeypatch):
    settings_path = tmp_path / "pin-settings.json"
    monkeypatch.setenv("WITTYCODE_SETTINGS", str(settings_path))

    async def scenario():
        core, plan, events, _, engine = await ready_core(tmp_path, projects=False)
        try:
            proj, _ = await three_saved(core, events, tmp_path)
            core.persist = True
            project = {"projectId": str(proj)}
            for row in ("cg:old", "cg:mid"):
                assert await core.call("session.pin", {**project, "id": row, "pinned": True}) == {"ok": True}
            assert listed(events) == ["cg:mid", "cg:old", "cg:new"]
            assert events.last("sidebar")["pinnedSessionIds"] == ["cg:mid", "cg:old"]
            assert events.last("sidebar")["sessions"]["cg:old"]["pinned"] is True

            await core.call("session.reorder", {**project, "id": "cg:old", "targetId": "cg:mid", "after": False})
            cid = (await core.call("session.openSaved", {**project, "id": "cg:mid"}))["id"]
            await idle(core, cid)
            await talk(core, plan, cid, "Pinned activity does not reorder the pins")
            assert core.project_rows()[0]["sessionIds"] == ["cg:old", cid, "cg:new"]

            draft = (await core.call("session.new", project))["id"]
            await idle(core, draft)
            assert draft not in core.project_rows()[0]["sessionIds"]
            await talk(core, plan, draft, "This becomes a new ordinary row")
            assert core.project_rows()[0]["sessionIds"] == ["cg:old", cid, draft, "cg:new"]
            await core.call("session.reorder", {**project, "id": "cg:new", "targetId": draft, "after": False})
            assert listed(events) == ["cg:old", cid, "cg:new", draft]
            new_row = core._by_cid[draft].sidebar_key
            await core.call("session.close", {"id": draft})
            await core.call("session.close", {"id": cid})
            await core.call("session.archive", {"id": "cg:old"})
            save_conversation(engine, proj, "later", "Discovered on refresh", minutes_ago=0)
            await core.call("project.refresh")
            assert core.project_rows()[0]["sessionIds"] == ["cg:mid", "cg:later", "cg:new", new_row]
        finally:
            await core.shutdown()

        settings = load_settings(settings_path)
        assert settings.session_pins == {folder_key(proj): ["cg:old", "cg:mid"]}
        again, _, _, _, _ = make_core(tmp_path, settings=settings)
        try:
            await again.call("app.init")
            await again.call("project.refresh")
            assert again.project_rows()[0]["sessionIds"] == ["cg:mid", "cg:later", "cg:new", new_row]
            assert again._archived_json()[0]["id"] == "cg:old"
            await again.call("session.unarchive", {"id": "cg:old"})
            assert again.project_rows()[0]["sessionIds"] == ["cg:old", "cg:mid", "cg:later", "cg:new", new_row]
            await again.call("session.pin", {**project, "id": "cg:old", "pinned": False})
            assert again.project_rows()[0]["sessionIds"] == ["cg:mid", "cg:old", "cg:later", "cg:new", new_row]
            assert again._saved_sessions_json()["cg:old"]["pinned"] is False
        finally:
            await again.shutdown()

    asyncio.run(scenario())


def test_pin_refuses_invalid_project_row_flag_and_cross_partition_drag(tmp_path):
    async def scenario():
        core, _, events, _, engine = await ready_core(tmp_path, projects=False)
        try:
            proj, _ = await three_saved(core, events, tmp_path)
            other = tmp_path / "other"
            other.mkdir()
            save_conversation(engine, other, "other", "Other project")
            await core.call("project.add", {"path": str(other)})
            await core.call("project.refresh")
            project = {"projectId": str(proj)}
            draft = (await core.call("session.new", project))["id"]
            await idle(core, draft)
            await core.call("session.archive", {"id": "cg:mid"})
            params = {**project, "id": "cg:old", "pinned": True}
            for change in ({"id": "unknown"}, {"id": draft}, {"id": "cg:mid"}, {"id": "cg:other"},
                           {"id": []}, {"projectId": str(other)}, {"projectId": "\0"}, {"projectId": []},
                           {"pinned": 1}, {"pinned": "false"}, {"pinned": None}):
                with pytest.raises(AppError):
                    await core.call("session.pin", {**params, **change})
            with pytest.raises(AppError):
                await core.call("session.pin", {**project, "id": "cg:old"})
            assert not core.settings.session_pins
            # No title test: an existing engine row with an empty title can still be pinned.
            old = engine.sessions.load("old")
            old.title = ""
            engine.sessions.save(old)
            await core.call("project.refresh")
            await core.call("session.pin", params)
            await core.call("session.pin", params)  # idempotent
            assert core.settings.session_pins == {folder_key(proj): ["cg:old"]}
            assert next(p for p in core.project_rows() if p["id"] == str(other))["sessionIds"] == ["cg:other"]
            for source, target in (("cg:old", "cg:new"), ("cg:new", "cg:old"), (draft, "cg:new")):
                with pytest.raises(AppError):
                    await core.call("session.reorder", {**project, "id": source, "targetId": target})
            assert not core.settings.session_order
        finally:
            await core.shutdown()

    asyncio.run(scenario())


def test_order_settings_are_sanitized_and_round_trip():
    settings = settings_from_dict({"session_order": ["cg:b", "cg:a", "cg:b", 1, "", []], "session_created": {"cg:a": 12, "bad": True, "nan": float("nan"), "inf": float("inf"), "negative": -1}})
    assert settings.session_order == ["cg:b", "cg:a"] and settings.session_created == {"cg:a": 12.0}
    assert settings_from_dict(settings_to_dict(settings)) == settings
