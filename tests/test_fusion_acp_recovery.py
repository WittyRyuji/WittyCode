"""Only a native session proven never prompted may be recreated after CLI empty-session cleanup."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from desktop.core.fusion.acp import AcpRun
from desktop.core.harness.store import SavedTurn
from desktop.core.transcript import ErrorBlock
from tests.app_support import ready_core
from tests.chatgpt_support import finish_turn
from tests.fakes import until
from tests.fusion_acp_support import make_guarded_engine

DEVIN = "devin:swe-2-high"
OLD_GUARD_ERROR = "Devin 未确认加载 Fusion 执行前保护；未发送模型请求。请更新兼容的 CLI。"


def requests(tmp_path):
    return [json.loads(line) for path in (tmp_path / "guarded").rglob("received.jsonl")
            for line in path.read_text(encoding="utf-8").splitlines()]


async def start(core, tmp_path):
    await until(lambda: any(model["source"] == "devin" for model in core.catalog.models))
    core.settings.fusion_main = core.settings.fusion_light = DEVIN
    core.settings.fusion_workers = [DEVIN]
    cid = (await core.call("session.new", {"projectId": str(tmp_path / "proj"), "fusion": True}))["id"]
    return core._by_cid[cid].controller


@pytest.mark.parametrize("legacy", [False, True], ids=["durable-marker", "old-guard-failure"])
def test_missing_empty_session_is_recreated_without_replaying_any_prior_request(tmp_path, monkeypatch, legacy):
    async def go():
        harness = make_guarded_engine(tmp_path, FAKE_FUSION_NO_GUARD="1", FAKE_FUSION_CACHE_MCP="1")
        core, _, _, _, _ = await ready_core(tmp_path, devin_engine=harness.engine)
        try:
            main = await start(core, tmp_path)
            factory = harness.engine._client_factory
            def checked_factory(binary, **handlers):
                client = factory(binary, **handlers)
                send = client.request
                async def checked(method, params, **options):
                    if method == "session/prompt":
                        stored = main.manager.sessions.load(main.session_id)
                        assert stored.origin["fusionAcp"]["promptAttempted"] is True
                    return await send(method, params, **options)
                client.request = checked
                return client
            monkeypatch.setattr(harness.engine, "_client_factory", checked_factory)
            monkeypatch.setattr("desktop.core.fusion.acp.GUARD_SECONDS", 0.2)
            assert main.send_prompt("Check Fusion")
            await finish_turn(main)
            old = main.saved.origin["fusionAcp"]["session"]
            assert main.saved.origin["fusionAcp"]["promptAttempted"] is False
            assert not any(item.get("method") == "session/prompt" for item in requests(tmp_path))
            if legacy:
                del main.saved.origin["fusionAcp"]["promptAttempted"]
                # Current controllers prune a turn that failed before producing output. Model an older retained failure.
                main.saved.turns.append(SavedTurn(0, 1.0, finished=2.0, status="failed", error=OLD_GUARD_ERROR, model=DEVIN))
                main._save_now(required=True)
            harness.engine._cli_env.pop("FAKE_FUSION_NO_GUARD")
            harness.engine._cli_env["FAKE_FUSION_MISSING_SESSION"] = "1"
            monkeypatch.setattr("desktop.core.fusion.acp.GUARD_SECONDS", 15.0)
            assert main.send_prompt("Try the previously unstarted task")
            await finish_turn(main)
            assert main.saved.turns[-1].status == "done", main.saved.turns[-1].error
            saved = main.saved.origin["fusionAcp"]
            assert saved["session"] != old and saved["previousEmptySession"] == old and saved["promptAttempted"] is True
            methods = [item["method"] for item in requests(tmp_path)]
            assert methods.count("session/new") == 2 and methods.count("session/load") == 1
            assert methods.count("session/prompt") == 1
        finally:
            await core.shutdown()
    asyncio.run(go())


@pytest.mark.parametrize("attempted", [False, True], ids=["unknown-legacy", "already-attempted"])
def test_missing_native_context_is_never_recreated_when_a_request_might_have_run(tmp_path, attempted):
    async def go():
        harness = make_guarded_engine(tmp_path)
        core, _, _, _, _ = await ready_core(tmp_path, devin_engine=harness.engine)
        try:
            main = await start(core, tmp_path)
            if attempted:
                assert main.send_prompt("One confirmed request")
                await finish_turn(main)
                assert main.saved.turns[-1].status == "done"
                await main._acp.close()
            else:
                main.saved.origin["fusionAcp"] = {"session": "missing-native-context", "upto": 0}
            before = dict(main.saved.origin["fusionAcp"])
            harness.engine._cli_env["FAKE_FUSION_MISSING_SESSION"] = "1"
            assert main.send_prompt("Do not silently replay")
            await finish_turn(main)
            assert any(isinstance(block, ErrorBlock) and "未自动重放" in block.message
                       for block in main.transcript.turns[-1].blocks)
            assert main.saved.origin["fusionAcp"] == before
            methods = [item["method"] for item in requests(tmp_path)]
            assert methods.count("session/new") == int(attempted) and methods.count("session/load") == 1
            assert methods.count("session/prompt") == int(attempted)
        finally:
            await core.shutdown()
    asyncio.run(go())


def test_legacy_recovery_requires_exact_local_guard_failures_and_zero_checkpoint():
    good = SimpleNamespace(model=DEVIN, status="failed", error=OLD_GUARD_ERROR)
    host = SimpleNamespace(saved=SimpleNamespace(turns=[good]), _turn=None)
    run = AcpRun(host)
    assert run._never_prompted({"upto": 0})
    assert not run._never_prompted({"upto": 1, "promptAttempted": False})
    assert not run._never_prompted({"upto": 0, "promptAttempted": True})
    assert not run._never_prompted({"upto": 0, "promptAttempted": "false"})
    for model, status, error in [("", "failed", OLD_GUARD_ERROR), (DEVIN, "done", ""),
                                 (DEVIN, "interrupted", ""), (DEVIN, "failed", "EngineRpcError: " + OLD_GUARD_ERROR),
                                 (DEVIN, "failed", "Other unknown failure")]:
        host.saved.turns = [good, SimpleNamespace(model=model, status=status, error=error)]
        assert not run._never_prompted({"upto": 0})
