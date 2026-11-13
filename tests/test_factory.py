"""Factory's optional native CLI path is exercised through a real local JSONL subprocess, without a Factory SDK or account."""

import asyncio
import json
import os
from pathlib import Path
import sys

from desktop.core.app_core import AppCore
from desktop.core.factory import EXEC_ARGS, FactoryClient, FactoryEngine
from desktop.core.session_state import SessionState
from desktop.core.settings import AppSettings, settings_from_dict
from desktop.core.transcript import TextBlockView
from tests.app_support import Events, FakePlatform
from tests.fakes import until


def engine_for(tmp_path):
    settings = AppSettings(factory_enabled=True, factory_path=sys.executable, default_model="factory:demo-model", think_chinese=False)
    project = tmp_path / "project"
    project.mkdir(exist_ok=True)
    settings.add_project(project)
    def client(project, **handlers):
        return FactoryClient([sys.executable, str(Path(__file__).with_name("fake_factory.py"))], cwd=project, env={**os.environ, "FAKE_FACTORY_HOME": str(tmp_path / "peer")}, name="Factory test peer", **handlers)
    engine = FactoryEngine(settings, tmp_path / "factory", client_factory=client)
    return engine, settings, project


def test_factory_is_optional_and_native_flags_are_not_acp():
    assert settings_from_dict({}).factory_enabled is False
    assert EXEC_ARGS == ("exec", "--input-format", "stream-jsonrpc", "--output-format", "stream-jsonrpc")
    settings = settings_from_dict({"factory_enabled": True, "factory_path": "droid.exe", "factory_autonomy": "medium"})
    assert settings.factory_enabled and settings.factory_path == "droid.exe" and settings.factory_autonomy == "medium"


def test_native_wire_streams_and_restores_after_an_application_restart(tmp_path):
    async def scenario():
        engine, settings, project = engine_for(tmp_path)
        assert (await engine.list_models())[0]["id"] == "demo-model"
        controller = await engine.open_new(project, model="demo-model", autonomy="off")
        try:
            assert controller.state is SessionState.READY
            assert controller.send_prompt("hello")
            await until(lambda: controller.state is SessionState.READY)
            assert controller.usage_series()["self"].total == 130
            assert controller.usage_model_series()["model:demo-model"][1].total == 130
            assert controller.title == "Factory demo task"
            native = controller.session_id
        finally:
            await engine.close_all()
        again, _, _ = engine_for(tmp_path)
        try:
            restored = await again.open_saved(project, native)
            assert restored.state is SessionState.READY
            assert restored.title == "Factory demo task"
            assert restored.usage_series()["self"].total == 130
            texts = [b.text for t in restored.transcript.turns for b in t.blocks if isinstance(b, TextBlockView)]
            assert texts == ["Factory local answer."]
            assert restored.send_prompt("next")
            await until(lambda: restored.state is SessionState.READY)
            assert restored.usage_series()["self"].total == 260
            assert restored.usage_model_series()["model:demo-model"][1].total == 260
        finally:
            await again.close_all()
    asyncio.run(scenario())


def test_permissions_questions_and_stopping_keep_user_control(tmp_path):
    async def scenario():
        engine, _, project = engine_for(tmp_path)
        controller = await engine.open_new(project, model="demo-model")
        try:
            assert controller.send_prompt("[permission] check")
            await until(lambda: bool(controller.transcript.pending_prompts()))
            prompt = controller.transcript.pending_prompts()[0]
            assert [o.value for o in prompt.options] == ["proceed_once", "cancel"]
            assert not controller.answer_permission(prompt.id, "unoffered")
            assert controller.answer_permission(prompt.id, "cancel")
            await until(lambda: controller.state is SessionState.READY)
            assert not controller.answer_permission(prompt.id, "proceed_once")
            assert controller.send_prompt("[question] ask")
            await until(lambda: bool(controller.transcript.pending_prompts()))
            prompt = controller.transcript.pending_prompts()[0]
            assert prompt.who == "Factory"
            assert controller.answer_question(prompt.id, ["Compact"])
            await until(lambda: controller.state is SessionState.READY)
            assert controller.send_prompt("[hold] wait")
            await asyncio.sleep(0.1)
            await controller.interrupt()
            assert controller.state is SessionState.READY
        finally:
            await engine.close_all()
    asyncio.run(scenario())


def test_the_application_catalog_can_start_a_factory_session_without_an_own_loop(tmp_path):
    async def scenario():
        engine, settings, project = engine_for(tmp_path)
        events = Events()
        core = AppCore(settings, FakePlatform(), factory=engine, persist=False, emit=events)
        try:
            await core.call("app.init")
            await until(lambda: any(m["source"] == "factory" for m in core.catalog.models))
            assert core.catalog.engine_of("factory:demo-model") == "factory"
            cid = (await core.call("session.new", {"projectId": str(project)}))["id"]
            controller = core._by_cid[cid].controller
            assert controller.engine == "factory" and controller.autonomy == "off"
            await core.call("session.send", {"id": cid, "text": "hello"})
            await until(lambda: controller.state is SessionState.READY)
            assert core._session_meta(core._by_cid[cid])["usageModels"] == {"factory:demo-model": 130}
            calls = [json.loads(line) for line in (tmp_path / "peer" / "calls.jsonl").read_text().splitlines()]
            assert any(c["method"] == "droid.initialize_session" for c in calls)
            assert not any(c["method"].startswith("session/") for c in calls)
        finally:
            await core.shutdown()
    asyncio.run(scenario())
