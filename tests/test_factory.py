"""Factory's optional native CLI path is exercised through a real local JSONL subprocess, without a Factory SDK or account."""

import asyncio
import json
import os
from pathlib import Path
import sys

import pytest

from desktop.core.app_core import AppCore
from desktop.core.factory import EXEC_ARGS, FactoryClient, FactoryEngine
from desktop.core.session_state import SessionState
from desktop.core.settings import AppSettings, settings_from_dict
from desktop.core.transcript import TextBlockView, TurnStatus
from tests.app_support import Events, FakePlatform
from tests.fakes import until
from tests.test_app_core import png_upload


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


@pytest.mark.parametrize("exists", [False, True])
def test_explicit_binary_never_falls_through(tmp_path, exists, monkeypatch):
    monkeypatch.chdir(tmp_path)
    binary = tmp_path / "chosen.exe"
    if exists:
        binary.touch()
    engine = FactoryEngine(AppSettings(factory_path=binary.name), tmp_path / "index",
                           locate=lambda _: pytest.fail("explicit path must be authoritative"))
    assert engine.binary() == (str(binary) if exists else None)


def test_windows_binary_discovery_order_without_changing_path(tmp_path, monkeypatch):
    from desktop.core import factory

    monkeypatch.setattr(factory.sys, "platform", "win32")
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "apps"))
    monkeypatch.setattr(factory, "_registered_path_dirs", lambda: iter([tmp_path / "user", tmp_path / "machine"]))
    process = tmp_path / "path" / "droid.exe"
    monkeypatch.setattr(factory.shutil, "which", lambda name: str(process) if name == "droid" and process.is_file() else None)
    install = tmp_path / "apps" / "Factory"
    def bundled(version):
        return install / ("app-" + version) / "resources" / "bin" / "droid.exe"

    # A partially installed newer desktop version must not hide older usable binaries.
    bundled("2.0.0").parent.mkdir(parents=True)
    candidates = [process, tmp_path / "user/droid.exe", tmp_path / "machine/droid.exe",
                  tmp_path / "home/bin/droid.exe", bundled("1.12.0"), bundled("1.12.0-beta"), bundled("1.9.0")]
    for path in candidates:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
    original = os.environ.get("PATH")
    engine = FactoryEngine(AppSettings(), tmp_path / "index")
    for path in candidates:
        assert engine.binary() == str(path)
        path.unlink()
    assert engine.binary() is None
    assert os.environ.get("PATH") == original


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


async def open_app(tmp_path):
    engine, settings, project = engine_for(tmp_path)
    events = Events()
    core = AppCore(settings, FakePlatform(), factory=engine, persist=False, emit=events)
    await core.call("app.init")
    await until(lambda: bool(core.catalog.find("factory:demo-model")))
    cid = (await core.call("session.new", {"projectId": str(project)}))["id"]
    return core, engine, events, project, cid


@pytest.mark.parametrize("failure", ["build", "rpc"])
def test_factory_first_failure_remains_retryable_but_never_indexes_the_draft(tmp_path, monkeypatch, failure):
    async def scenario():
        core, engine, events, project, cid = await open_app(tmp_path)
        controller = core._by_cid[cid].controller
        try:
            if failure == "build":
                def cannot_build(*_):
                    raise ValueError("Demo assembly failure")

                monkeypatch.setattr(controller, "_message", cannot_build)
            await core.call("session.send", {"id": cid, "text": "[reject] try again"})
            await until(lambda: events.of("restore"))
            assert events.last("restore")["text"] == "[reject] try again"
            assert controller.state is SessionState.READY and controller.transcript.turns[-1].status is TurnStatus.FAILED
            assert not controller.history_ready and not await engine.list_sessions(project)
            rpc_log = (tmp_path / "peer" / "calls.jsonl").read_text(encoding="utf-8").splitlines()
            count = sum(json.loads(line)["method"] == "droid.add_user_message" for line in rpc_log)
            assert count == (failure == "rpc")
            await core.call("session.close", {"id": cid})
            await core.call("project.refresh", {"id": str(project)})
            assert core.project_rows()[0]["sessionIds"] == []
        finally:
            await core.shutdown()
        restarted, _, _ = engine_for(tmp_path)
        assert not await restarted.list_sessions(project)

    asyncio.run(scenario())


@pytest.mark.parametrize("stop_error", [False, True])
def test_factory_acknowledged_first_prompt_survives_stop_and_restart_without_readding_draft(tmp_path, stop_error):
    async def scenario():
        core, engine, events, project, cid = await open_app(tmp_path)
        conv = core._by_cid[cid]
        controller = conv.controller
        native = controller.session_id
        text = "[hold]" + (" [stop-error]" if stop_error else "")
        try:
            await core.call("attachments.add", {"id": cid, "files": [png_upload("note.png")]})
            await core.call("session.send", {"id": cid, "text": text})
            await until(lambda: controller.history_ready)
            assert controller.state is SessionState.RUNNING
            assert core.project_rows()[0]["sessionIds"] == [cid]
            assert [item.id for item in await engine.list_sessions(project)] == [native], "persist at ACK, before output"
            await core.call("session.stop", {"id": cid})
            expected = SessionState.DEAD if stop_error else SessionState.READY
            await until(lambda: controller.state is expected and not controller.transcript.active_turn)
            core._flush(conv)
            assert not events.of("restore") and not conv.drafts
            if stop_error:
                await controller.reconnect()
                assert controller.state is SessionState.READY
                assert controller.transcript.turns[0].user_block.text == text
        finally:
            await core.shutdown()
        restarted, _, _ = engine_for(tmp_path)
        try:
            assert [item.id for item in await restarted.list_sessions(project)] == [native]
            reopened = await restarted.open_saved(project, native)
            assert reopened.transcript.turns[0].user_block.text == text
        finally:
            await restarted.close_all()

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
