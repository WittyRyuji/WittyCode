"""The context figures a session shows while its own session has not measured them: the cache stands in, but never for a
conversation that was compacted since, or one that moved to another model or window (review G8)."""

from __future__ import annotations

import asyncio
from dataclasses import replace

from tests.test_app_chatgpt import chatgpt_core, new_chatgpt
from tests.chatgpt_support import ACCESS_TOKEN
from tests.fake_responses import FakeResponses
from desktop.core.context import ContextState

CACHED = {"used": 383_000, "window": 272_000, "model": "gpt-6.1-sol", "compacted": False}


def run(coro):
    return asyncio.run(coro)


def shown(core, conv, live: ContextState, cached: dict | None = CACHED) -> dict:
    """What the page is told for a conversation whose session reports ``live`` and whose cached copy says ``cached``."""
    conv.controller.context = live
    conv.cached_meta = {"context": dict(cached)} if cached is not None else {}
    return core._session_meta(conv)["context"]


def test_the_cache_stands_in_until_the_session_has_measured_its_own(tmp_path):
    async def scenario():
        with FakeResponses(token=ACCESS_TOKEN) as fake:
            core, _, events, _ = await chatgpt_core(tmp_path, fake)
            cid = await new_chatgpt(core, events, tmp_path)
            conv = core._by_cid[cid]
            assert shown(core, conv, ContextState(used=None, window=272_000, model="gpt-6.1-sol")) == CACHED
            assert shown(core, conv, ContextState(used=None, window=272_000, model="")) == CACHED, "a session that does not name its model yet"
            assert shown(core, conv, ContextState(used=1_200, window=272_000, model="gpt-6.1-sol"))["used"] == 1_200, "its own measurement wins"
            await core.shutdown()

    run(scenario())


def test_a_compaction_is_not_undone_by_the_cache(tmp_path):
    async def scenario():
        with FakeResponses(token=ACCESS_TOKEN) as fake:
            core, _, events, _ = await chatgpt_core(tmp_path, fake)
            cid = await new_chatgpt(core, events, tmp_path)
            conv = core._by_cid[cid]
            live = ContextState(used=None, window=272_000, model="gpt-6.1-sol", compacted=True)
            context = shown(core, conv, live)
            assert (context["used"], context["compacted"], context["window"]) == (None, True, 272_000), "compacted, not measured since: not 383000 again"
            # the main answer after it was paused or failed leaves the same state: the figure stays unknown
            assert shown(core, conv, replace(live))["used"] is None
            await core.shutdown()

    run(scenario())


def test_the_cache_is_not_used_for_another_model_or_window(tmp_path):
    async def scenario():
        with FakeResponses(token=ACCESS_TOKEN) as fake:
            core, _, events, _ = await chatgpt_core(tmp_path, fake)
            cid = await new_chatgpt(core, events, tmp_path)
            conv = core._by_cid[cid]
            other_model = shown(core, conv, ContextState(used=None, window=272_000, model="gpt-6-astra"))
            assert other_model["used"] is None and other_model["model"] == "gpt-6-astra"
            other_window = shown(core, conv, ContextState(used=None, window=128_000, model="gpt-6.1-sol"))
            assert other_window["used"] is None and other_window["window"] == 128_000, "figures measured against 272K do not describe a 128K window"
            assert shown(core, conv, ContextState(used=None, window=272_000, model="gpt-6.1-sol"), cached=None)["used"] is None
            await core.shutdown()

    run(scenario())
