"""Projects: a new one leads the list, and ``project.reorder`` drags them into place —
neither one disturbs the pinned zone's order."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from desktop.core.app_core import AppError
from desktop.core.settings import load_settings
from tests.app_support import make_core, ready_core, save_conversation
from tests.test_app_global_pins import pin_ids


def project_names(core):
    return [p["name"] for p in core.project_rows()]


def test_new_projects_lead_the_list_and_readding_moves_nothing(tmp_path):
    async def scenario():
        core, _, events, _, _ = await ready_core(tmp_path, projects=False)
        try:
            for name in ("first", "second"):
                folder = tmp_path / name
                folder.mkdir()
                added = await core.call("project.add", {"path": str(folder)})
                assert added["added"] is True and Path(added["id"]).name == name, "the reported row is the one just added"
            assert project_names(core) == ["second", "first"], "the newest project opens the list"
            assert events.last("sidebar")["projects"][0]["name"] == "second"
            with pytest.raises(AppError):
                await core.call("project.add", {"path": str(tmp_path / "first")})
            assert project_names(core) == ["second", "first"]
        finally:
            await core.shutdown()

    asyncio.run(scenario())


def test_project_reorder_moves_rows_and_survives_a_restart_without_touching_the_pins(tmp_path, monkeypatch):
    settings_path = tmp_path / "order-settings.json"
    monkeypatch.setenv("WITTYCODE_SETTINGS", str(settings_path))

    async def scenario():
        core, _, events, _, engine = await ready_core(tmp_path, projects=False)
        try:
            core.persist = True
            folders = []
            for name in ("alpha", "beta", "gamma"):
                folder = tmp_path / name
                folder.mkdir()
                folders.append(folder)
                await core.call("project.add", {"path": str(folder)})
            alpha, beta, gamma = folders
            assert project_names(core) == ["gamma", "beta", "alpha"]

            save_conversation(engine, alpha, "a1", "A1")
            save_conversation(engine, beta, "b1", "B1")
            await core.call("project.refresh")
            for folder, row in ((alpha, "cg:a1"), (beta, "cg:b1")):
                await core.call("session.pin", {"projectId": str(folder), "id": row, "pinned": True})
            assert pin_ids(events) == ["cg:b1", "cg:a1"]

            await core.call("project.reorder", {"id": str(gamma), "targetId": str(alpha), "after": True})
            assert project_names(core) == ["beta", "alpha", "gamma"]
            assert pin_ids(events) == ["cg:b1", "cg:a1"], "a project drag leaves the pinned zone alone"
            await core.call("project.reorder", {"id": str(beta), "targetId": str(gamma), "after": True})
            assert project_names(core) == ["alpha", "gamma", "beta"]
            assert await core.call("project.reorder", {"id": str(beta), "targetId": str(beta)}) == {"ok": True}
            assert project_names(core) == ["alpha", "gamma", "beta"]
        finally:
            await core.shutdown()

        settings = load_settings(settings_path)
        assert [Path(p).name for p in settings.projects] == ["alpha", "gamma", "beta"]
        assert settings.pinned_sessions == ["cg:b1", "cg:a1"]
        again, _, _, _, _ = make_core(tmp_path, settings=settings, projects=False)
        try:
            await again.call("app.init")
            assert project_names(again) == ["alpha", "gamma", "beta"], "the dragged order survives a restart"
        finally:
            await again.shutdown()

    asyncio.run(scenario())


def test_project_reorder_refuses_strays_and_bad_values(tmp_path):
    async def scenario():
        core, _, _, _, _ = await ready_core(tmp_path)
        try:
            (proj,) = core.settings.projects
            stray = tmp_path / "stray"
            stray.mkdir()
            for params in (
                {"id": str(stray), "targetId": proj, "after": True},
                {"id": proj, "targetId": str(stray), "after": False},
                {"id": proj, "targetId": "\0", "after": False},
                {"id": 7, "targetId": proj, "after": True},
                {"id": proj, "targetId": proj, "after": "yes"},
            ):
                with pytest.raises(AppError):
                    await core.call("project.reorder", params)
            assert project_names(core) == [Path(proj).name]
        finally:
            await core.shutdown()

    asyncio.run(scenario())
