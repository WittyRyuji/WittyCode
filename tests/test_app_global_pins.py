"""The pinned zone: one ordered strip of conversations ahead of all projects.

Pins live in ``settings.pinned_sessions`` across projects; ``project.sessionIds`` still lists every
member, pinned ones included, and ``settings.session_pins`` mirrors the zone per project for older
builds.
"""

from __future__ import annotations

import asyncio

import pytest

from desktop.core.app_core import AppError
from desktop.core.settings import folder_key, load_settings, settings_from_dict
from tests.app_support import make_core, ready_core, save_conversation
from tests.test_app_core import idle, three_saved


def pin_ids(events):
    return events.last("sidebar")["pinnedSessionIds"]


def project_rows(events, folder):
    return next(p["sessionIds"] for p in events.last("sidebar")["projects"] if p["id"] == str(folder))


async def two_projects(core, events, engine, tmp_path):
    """``proj`` holds old/mid/new, ``other`` holds x1/x2; ``other`` leads the list, added last."""
    proj, _ = await three_saved(core, events, tmp_path)
    other = tmp_path / "other"
    other.mkdir()
    save_conversation(engine, other, "x1", "X1", minutes_ago=30)
    save_conversation(engine, other, "x2", "X2", minutes_ago=10)
    await core.call("project.add", {"path": str(other)})
    await core.call("project.refresh")
    core._emit_sidebar()
    return proj, other


def test_pins_form_one_zone_across_projects_and_keep_their_order_over_restart(tmp_path, monkeypatch):
    settings_path = tmp_path / "pin-settings.json"
    monkeypatch.setenv("WITTYCODE_SETTINGS", str(settings_path))

    async def scenario():
        core, _, events, _, engine = await ready_core(tmp_path, projects=False)
        try:
            core.persist = True
            proj, other = await two_projects(core, events, engine, tmp_path)
            assert pin_ids(events) == []
            for project, row in ((proj, "cg:mid"), (other, "cg:x1"), (proj, "cg:old")):
                assert await core.call("session.pin", {"projectId": str(project), "id": row, "pinned": True}) == {"ok": True}
            core._emit_sidebar()
            assert pin_ids(events) == ["cg:old", "cg:x1", "cg:mid"], "newest pin leads one zone across projects"
            rows = {p["id"]: p["sessionIds"] for p in events.last("sidebar")["projects"]}
            assert rows[str(other)] == ["cg:x1", "cg:x2"] and "cg:old" in rows[str(proj)], "membership stays with the project"
            assert events.last("sidebar")["sessions"]["cg:x1"]["pinned"] is True

            # a pinned drag crosses projects; ``projectId`` names the dragged row's own project
            await core.call("session.reorder", {"projectId": str(other), "id": "cg:x1", "targetId": "cg:mid", "after": True})
            assert pin_ids(events) == ["cg:old", "cg:mid", "cg:x1"]
            assert core.settings.session_pins == {folder_key(proj): ["cg:old", "cg:mid"], folder_key(other): ["cg:x1"]}
        finally:
            await core.shutdown()

        settings = load_settings(settings_path)
        assert settings.pinned_sessions == ["cg:old", "cg:mid", "cg:x1"]
        again, _, events2, _, _ = make_core(tmp_path, settings=settings)
        try:
            await again.call("app.init")
            await again.call("project.refresh")
            again._emit_sidebar()
            assert pin_ids(events2) == ["cg:old", "cg:mid", "cg:x1"], "a restart keeps the zone's order"
        finally:
            await again.shutdown()

    asyncio.run(scenario())


def test_archive_and_project_removal_hide_the_row_without_losing_its_pin(tmp_path):
    async def scenario():
        core, _, events, _, engine = await ready_core(tmp_path, projects=False)
        try:
            proj, other = await two_projects(core, events, engine, tmp_path)
            for project, row in ((proj, "cg:mid"), (other, "cg:x1")):
                await core.call("session.pin", {"projectId": str(project), "id": row, "pinned": True})
            assert pin_ids(events) == ["cg:x1", "cg:mid"]

            await core.call("session.archive", {"id": "cg:mid"})
            core._emit_sidebar()
            assert pin_ids(events) == ["cg:x1"]
            assert "cg:mid" not in project_rows(events, proj)
            assert core.settings.pinned_sessions == ["cg:x1", "cg:mid"], "archiving hides, it does not unpin"
            await core.call("session.unarchive", {"id": "cg:mid"})
            assert pin_ids(events) == ["cg:x1", "cg:mid"]

            await core.call("project.remove", {"id": str(other)})
            core._emit_sidebar()
            assert pin_ids(events) == ["cg:mid"]
            assert core.settings.pinned_sessions == ["cg:x1", "cg:mid"]
            await core.call("project.add", {"path": str(other)})
            await core.call("project.refresh")
            core._emit_sidebar()
            assert pin_ids(events) == ["cg:x1", "cg:mid"], "a re-added project brings its pins back in place"

            # unpinning returns the row to the front of its own project's ordinary rows
            await core.call("session.pin", {"projectId": str(proj), "id": "cg:mid", "pinned": False})
            core._emit_sidebar()
            assert pin_ids(events) == ["cg:x1"]
            assert project_rows(events, proj) == ["cg:mid", "cg:new", "cg:old"]
            assert core.settings.pinned_sessions == ["cg:x1"]
            assert not core.settings.session_pins.get(folder_key(proj))
        finally:
            await core.shutdown()

    asyncio.run(scenario())


def test_drags_stay_inside_their_partition(tmp_path):
    async def scenario():
        core, _, events, _, engine = await ready_core(tmp_path, projects=False)
        try:
            proj, other = await two_projects(core, events, engine, tmp_path)
            project = {"projectId": str(proj)}
            await core.call("session.pin", {**project, "id": "cg:mid", "pinned": True})
            for params in (
                {**project, "id": "cg:new", "targetId": "cg:x2"},          # ordinary row, other project
                {**project, "id": "cg:new", "targetId": "cg:mid"},         # ordinary into the pin zone
                {**project, "id": "cg:mid", "targetId": "cg:new"},         # pinned onto an ordinary row
                {**project, "id": "cg:mid", "targetId": "cg:x2"},          # pinned onto ordinary elsewhere
                {**project, "id": "cg:unknown", "targetId": "cg:new"},
                {"projectId": str(other), "id": "cg:new", "targetId": "cg:x2"},   # source is not in the named project
                {"projectId": str(other), "id": "cg:mid", "targetId": "cg:x1"},   # pinned row under a foreign project
            ):
                with pytest.raises(AppError):
                    await core.call("session.reorder", params)
            assert core.settings.pinned_sessions == ["cg:mid"] and not core.settings.session_order
            # a row dropped on itself is a no-op, in the zone too
            assert await core.call("session.reorder", {**project, "id": "cg:mid", "targetId": "cg:mid", "after": True}) == {"ok": True}
            assert pin_ids(events) == ["cg:mid"]
        finally:
            await core.shutdown()

    asyncio.run(scenario())


def test_legacy_project_pins_merge_into_the_zone(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()

    async def scenario():
        settings = settings_from_dict({
            "projects": [str(b), str(a)],
            "session_pins": {str(a): ["cg:a1", "cg:a2"], str(b): ["cg:b1"], str(tmp_path / "gone"): ["cg:lost"]},
        })
        assert settings.pinned_sessions == ["cg:b1", "cg:a1", "cg:a2", "cg:lost"], "project order, then orphans"
        core, _, events, _, engine = make_core(tmp_path, settings=settings, projects=False)
        try:
            save_conversation(engine, a, "a1", "A1")
            save_conversation(engine, a, "a2", "A2")
            save_conversation(engine, b, "b1", "B1")
            save_conversation(engine, b, "b2", "B2")
            await core.call("app.init")
            await core.call("project.refresh")
            core._emit_sidebar()
            assert pin_ids(events) == ["cg:b1", "cg:a1", "cg:a2"], "the orphan's pin stays in the list but off the screen"
            saved = events.last("sidebar")["sessions"]
            assert saved["cg:a1"]["pinned"] is True and saved["cg:b1"]["pinned"] is True and saved["cg:b2"]["pinned"] is False
        finally:
            await core.shutdown()

    asyncio.run(scenario())


def test_an_open_pinned_row_shows_its_cid_until_it_closes(tmp_path):
    async def scenario():
        core, _, events, _, _ = await ready_core(tmp_path, projects=False)
        try:
            proj, _ = await three_saved(core, events, tmp_path)
            project = {"projectId": str(proj)}
            cid = (await core.call("session.openSaved", {**project, "id": "cg:mid"}))["id"]
            await idle(core, cid)
            await core.call("session.pin", {**project, "id": cid, "pinned": True})
            assert pin_ids(events) == [cid], "open rows publish the conversation id"
            assert core.settings.pinned_sessions == ["cg:mid"]
            assert events.of("session")[-1]["patch"] == {"pinned": True}
            await core.call("session.close", {"id": cid})
            await core.call("project.refresh")
            core._emit_sidebar()
            assert pin_ids(events) == ["cg:mid"], "closed again, the native key is the public row"
        finally:
            await core.shutdown()

    asyncio.run(scenario())
