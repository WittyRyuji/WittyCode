"""Cache Write survives the real AppCore/cache/engine restart path, not just Tokens arithmetic."""
from __future__ import annotations

import asyncio

import pytest

from desktop.core.cache import Cache
from desktop.core.usage import Tokens
from tests.app_support import ready_core
from tests.fake_responses import say, usage
from tests.test_app_usage_models import done, meta, new


@pytest.mark.parametrize("legacy", [False, True])
def test_cache_write_is_not_double_counted_when_the_app_is_restarted(tmp_path, legacy):
    async def scenario():
        cache = Cache(tmp_path / "cache")
        core, plan, events, _, engine = await ready_core(tmp_path, cache=cache)
        try:
            cid = await new(core, events, tmp_path)
            raw = usage(12000, 50, cached=4000)
            raw["input_tokens_details"]["cache_write_tokens"] = 7000
            plan.script(say("first", used=raw))
            await core.call("session.send", {"id": cid, "text": "first"})
            await done(events, cid, 12050)
            expected = Tokens(1000, 50, 7000, 4000)
            assert Tokens.of(meta(events, cid)["usage"]) == expected
            native = core._by_cid[cid].controller.session_id
        finally:
            await core.shutdown()
        if legacy:
            saved = engine.sessions.load(native)
            saved.usage.pop("cacheWrite")
            engine.sessions.save(saved)
        again, next_plan, next_events, _, _ = await ready_core(tmp_path, cache=cache)
        try:
            reopened = (await again.call("session.openSaved", {"projectId": str(tmp_path / "proj"), "id": "cg:" + native}))["id"]
            await done(next_events, reopened, 12050)
            assert Tokens.of(meta(next_events, reopened)["usage"]) == expected
            raw = usage(6000, 20, cached=4000)
            raw["input_tokens_details"]["cache_write_tokens"] = 1000
            next_plan.script(say("second", used=raw))
            await again.call("session.send", {"id": reopened, "text": "second"})
            await done(next_events, reopened, 18070)
            assert Tokens.of(meta(next_events, reopened)["usage"]) == Tokens(2000, 70, 8000, 8000)
        finally:
            await again.shutdown()
    asyncio.run(scenario())
