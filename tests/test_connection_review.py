"""Regression cases confirmed in the final review: truthful attribution and safe managed-skill edits."""

import asyncio
from pathlib import Path

import pytest

from desktop.core.connections import ConnectionError
from desktop.core.session_state import SessionState
from tests.fakes import until
from tests.test_connections import rig, trust
from tests.test_factory import engine_for


def test_a_factory_turn_that_changes_model_does_not_invent_one_models_share(tmp_path):
    async def scenario():
        engine, _, project = engine_for(tmp_path)
        controller = await engine.open_new(project, model="demo-model")
        try:
            controller.send_prompt("[hold] wait")
            await until(lambda: not controller._tasks)
            controller._notification("droid.session_notification", {"sessionId": controller.session_id, "notification": {"type": "settings_updated", "settings": {"modelId": "fallback-model"}}})
            used = {"inputTokens": 100, "outputTokens": 10, "cacheReadTokens": 0}
            controller._notification("droid.session_notification", {"sessionId": controller.session_id, "notification": {"type": "agent_turn_completed", "reason": "completed", "tokenUsage": used, "cumulativeTokenUsage": used}})
            assert controller.state is SessionState.READY and controller.model == "fallback-model"
            assert controller.usage_series()["self"].total == 110
            assert controller.usage_model_series() == {}, "without per-call attribution the mixed turn must stay unknown"
        finally:
            await engine.close_all()
    asyncio.run(scenario())


def make_skill(folder, name):
    file = folder / name / "SKILL.md"
    file.parent.mkdir(parents=True)
    file.write_text(f"---\nname: {name}\ndescription: Local demo\n---\nUse the local task.", encoding="utf-8")
    return file


def test_an_invalid_skill_edit_is_rejected_without_corrupting_the_registry(rig):
    store, _, project = rig
    skill = make_skill(project, "first")
    trust(store, skill, project)
    entry = store.active("skill", project)[0]
    before = store.file.read_bytes()
    with pytest.raises(ConnectionError, match="SKILL.md"):
        store.update(entry["id"], enabled=True, config={}, project=str(project))
    assert store.file.read_bytes() == before and store.skill_sources(project)


def test_editing_a_skill_path_updates_the_watched_source_without_a_duplicate_row(rig):
    store, _, project = rig
    first, second = make_skill(project, "first"), make_skill(project, "second")
    trust(store, first, project)
    entry = store.active("skill", project)[0]
    store.update(entry["id"], enabled=True, config={"path": str(second)}, project=str(project))
    moved = store.active("skill", project)[0]
    assert Path(moved["source"]) == second
    assert moved["name"] == "second"
    assert len([item for item in store.scan(str(project))["items"] if item["kind"] == "skill"]) == 1
    second.write_text(second.read_text() + "\nChanged instructions.", encoding="utf-8")
    assert store.active("skill", project) == [], "fingerprints must follow the file the model actually reads"
