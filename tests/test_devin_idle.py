"""Metadata discovery must not pin the Devin executable while only the own loop is in use."""
from __future__ import annotations

import asyncio

import pytest

from desktop.core.devin.errors import DevinRpcError
from desktop.core.session_state import SessionState
from tests.app_support import ready_core
from tests.chatgpt_support import finish_turn as finish_own_turn
from tests.devin_support import finish_turn, make_engine, run, until
from tests.fake_responses import say


def test_metadata_queries_release_the_process_and_cached_models_need_no_process(tmp_path):
    async def go():
        harness = make_engine(tmp_path)
        models = await harness.engine.list_models()
        assert models and not harness.engine.running and not harness.clients[-1].running
        assert await harness.engine.list_models() == models and len(harness.clients) == 1
        assert await harness.engine.list_sessions(tmp_path) == []
        assert len(harness.clients) == 2 and all(not client.running for client in harness.clients)
        assert not harness.requests("session/prompt")
    run(go())


def test_overlapping_queries_release_only_after_the_last_query(tmp_path, monkeypatch):
    async def go():
        harness = make_engine(tmp_path)
        client = await harness.engine.connection()
        send = client.request
        waiting, release = asyncio.Event(), asyncio.Event()
        async def request(method, params, **options):
            if method == "session/list":
                waiting.set()
                await release.wait()
            return await send(method, params, **options)
        monkeypatch.setattr(client, "request", request)
        listing = asyncio.create_task(harness.engine.list_sessions(tmp_path))
        await waiting.wait()
        assert await harness.engine.list_models()
        assert client.running and harness.engine.running
        release.set()
        assert await listing == []
        assert not client.running and not harness.engine.running
    run(go())


def test_metadata_cleanup_preserves_opening_and_open_sessions_and_last_close_releases(tmp_path, monkeypatch):
    async def go():
        harness = make_engine(tmp_path)
        waiting, release = asyncio.Event(), asyncio.Event()
        ensure = harness.engine.ensure_signed_in
        async def held_signin():
            if not waiting.is_set():  # Hold only session opening; metadata also verifies CLI authentication now.
                waiting.set()
                await release.wait()
            await ensure()
        monkeypatch.setattr(harness.engine, "ensure_signed_in", held_signin)
        opening = asyncio.create_task(harness.engine.open_new(tmp_path))
        await waiting.wait()
        assert await harness.engine.list_models()
        client = harness.clients[-1]
        assert client.running
        release.set()
        first = await opening
        second = await harness.engine.open_new(tmp_path)
        assert await harness.engine.list_sessions(tmp_path) == []
        assert client.running and first.state is second.state is SessionState.READY
        await first.close()
        assert client.running
        assert (await finish_turn(second, "Continue safely")).status.value == "done"
        await second.close()
        assert not client.running and not harness.engine.running
    run(go())


@pytest.mark.parametrize("cancel", [False, True], ids=["query-error", "query-cancelled"])
def test_failed_or_cancelled_metadata_queries_do_not_leave_a_process(tmp_path, monkeypatch, cancel):
    async def go():
        harness = make_engine(tmp_path)
        client = await harness.engine.connection()
        waiting = asyncio.Event()
        async def request(method, params, **options):
            waiting.set()
            if cancel:
                await asyncio.Event().wait()
            raise DevinRpcError(-32603, "Metadata unavailable")
        monkeypatch.setattr(client, "request", request)
        query = asyncio.create_task(harness.engine.list_models())
        await waiting.wait()
        if cancel:
            query.cancel()
        with pytest.raises(asyncio.CancelledError if cancel else DevinRpcError):
            await query
        assert not harness.engine.running and not client.running
        assert harness.engine._connection_users == 0
    run(go())


def test_chatgpt_work_does_not_keep_a_discovery_only_devin_process(tmp_path):
    async def go():
        harness = make_engine(tmp_path)
        core, service, _, _, _ = await ready_core(tmp_path, devin_engine=harness.engine)
        try:
            await until(lambda: any(model["source"] == "devin" for model in core.catalog.models))
            await until(lambda: not harness.engine.running)
            cid = (await core.call("session.new", {"projectId": str(tmp_path / "proj")}))["id"]
            main = core._by_cid[cid].controller
            service.script(say("Own-loop report."))
            assert main.send_prompt("Inspect the module")
            await finish_own_turn(main)
            assert main.saved.turns[-1].status == "done"
            await until(lambda: all(not client.running for client in harness.clients))
            assert not harness.engine.running
            assert not harness.requests("session/prompt")
        finally:
            await core.shutdown()
    run(go())
