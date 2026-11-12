"""Local history stays visible while connecting, even when older than the remote row or the connection fails."""

from __future__ import annotations

import asyncio
import threading
import time
from datetime import datetime, timezone

from desktop.core.app_core import SavedRow
from desktop.core.cache import Cache
from desktop.core.session_state import SessionState
from tests.app_support import ready_core, save_conversation
from tests.devin_support import make_engine as make_devin
from tests.fake_responses import say
from tests.fakes import until
from tests.test_app_cache import answer, sessions_of


def local_snapshot(project):
    return {
        "engine": "devin", "native": "cached-session", "project": str(project), "title": "A cached conversation", "activeAt": time.time() - 3600,
        "turns": [{"id": "cached-turn", "status": "done", "prompts": [], "blocks": [
            {"id": "cached-user", "kind": "user", "text": "A question from last time"},
            {"id": "cached-answer", "kind": "text", "markdown": "An answer available offline", "streaming": False},
        ]}],
        "meta": {
            "settings": {"model": "devin:swe-2-high", "reasoning": "high", "autonomy": "code", "mode": "auto"},
            "usage": {"input": 1200, "output": 200, "cacheRead": 100, "cacheWrite": None},
            "context": {"used": 1400, "window": 262144, "model": "swe-2-high", "compacted": False},
        },
    }


def test_stale_local_history_and_settings_survive_connection_failure_without_overwriting_cache(tmp_path):
    async def scenario():
        cache = Cache(tmp_path / "cache")
        harness = make_devin(tmp_path, FAKE_DEVIN_OPEN_DELAY="0.5", FAKE_DEVIN_OPEN_FAIL="1")
        core, _, events, _, _ = await ready_core(tmp_path, devin_engine=harness.engine, cache=cache)
        harness.engine.settings = core.settings
        proj = (tmp_path / "proj").resolve()
        original = local_snapshot(proj)
        cache.save_session("dv:cached-session", original)
        core._saved[str(proj)] = [SavedRow("dv:cached-session", "cached-session", "devin", original["title"], datetime.now(timezone.utc))]
        opening = asyncio.create_task(core.call("session.openSaved", {"id": "dv:cached-session", "projectId": str(proj), "title": original["title"]}))
        try:
            await until(lambda: core.order and answer(events, core.order[0].cid) == "An answer available offline")
            conv = core.order[0]
            assert conv.controller.state is SessionState.OPENING
            assert sessions_of(events)[conv.cid]["settings"] == original["meta"]["settings"]
            await opening
            core._flush(conv)
            assert conv.controller.state is SessionState.DEAD and conv.preview
            assert answer(events, conv.cid) == "An answer available offline"
            assert sessions_of(events)[conv.cid]["context"]["used"] == 1400
            assert sessions_of(events)[conv.cid]["usage"]["output"] == 200
            assert "本地缓存" in sessions_of(events)[conv.cid]["workingLabel"]
            assert any("仍显示本地缓存" in item["text"] for item in events.of("notice"))
            core._write_snapshots_now()
            assert cache.load_session("dv:cached-session")["turns"] == original["turns"]
        finally:
            await opening
            await core.shutdown()
        assert cache.load_session("dv:cached-session")["turns"] == original["turns"]

    asyncio.run(scenario())


def test_the_own_loop_announces_cached_history_before_reading_the_archive_and_then_replaces_it(tmp_path, monkeypatch):
    async def scenario():
        cache = Cache(tmp_path / "cache")
        core, _, events, _, engine = await ready_core(tmp_path, cache=cache)
        proj = (tmp_path / "proj").resolve()
        save_conversation(engine, proj, "local", "The real history")
        cache.save_session("cg:local", {**local_snapshot(proj), "engine": "chatgpt", "native": "local"})
        await core.call("project.refresh")
        load = engine.sessions.load
        release = threading.Event()
        entered = threading.Event()

        def slow_load(key):
            entered.set()
            assert release.wait(10)
            return load(key)

        monkeypatch.setattr(engine.sessions, "load", slow_load)
        opening = asyncio.create_task(core.call("session.openSaved", {"id": "cg:local", "projectId": str(proj)}))
        try:
            await until(lambda: entered.is_set() and core.order and answer(events, core.order[0].cid) == "An answer available offline")
            conv = core.order[0]
            assert conv.controller.state is SessionState.OPENING
            release.set()
            await opening
            core._flush(conv)
            assert not conv.preview and answer(events, conv.cid) == "The real history: done."
            assert events.last("turns")["all"] is True, "real turns replace the cached ones rather than duplicate them"
        finally:
            release.set()
            await opening
            await core.shutdown()

    asyncio.run(scenario())


def test_a_running_turn_is_cached_before_it_finishes(tmp_path):
    async def scenario():
        cache = Cache(tmp_path / "cache")
        core, plan, events, _, _ = await ready_core(tmp_path, cache=cache)
        cid = (await core.call("session.new", {"projectId": str((tmp_path / "proj").resolve())}))["id"]
        conv = core._by_cid[cid]
        plan.script([("hold", "answer"), *say("The eventual answer")])
        await core.call("session.send", {"id": cid, "text": "Keep this even during a long turn"})
        try:
            key = "cg:" + conv.controller.session_id
            await until(lambda: cache.load_session(key) is not None, timeout=6)
            assert conv.controller.state is SessionState.RUNNING
            assert cache.load_session(key)["turns"][0]["blocks"][0]["text"] == "Keep this even during a long turn"
        finally:
            plan.release("answer")
            await core.shutdown()

    asyncio.run(scenario())
