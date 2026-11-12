"""Per-model token shares through the real application and engines, using only loopback fakes."""

from __future__ import annotations

import asyncio

from desktop.core.cache import Cache
from desktop.core.session_state import SessionState
from desktop.core.usage import Tokens
from tests.app_support import ready_core
from tests.devin_support import make_engine as make_devin
from tests.fake_responses import say, usage
from tests.fakes import until


def meta(events, cid):
    value = {}
    for payload in events.of("session"):
        if payload["id"] == cid:
            value.update(payload["patch"])
    return value


async def new(core, events, tmp_path):
    cid = (await core.call("session.new", {"projectId": str(tmp_path / "proj")}))["id"]
    await until(lambda: meta(events, cid).get("mark") == "ready")
    return cid


async def done(events, cid, total):
    await until(lambda: meta(events, cid).get("mark") == "ready" and (Tokens.of(meta(events, cid).get("usage")) or Tokens()).total == total)


def test_model_shares_include_cache_and_survive_closing_and_reopening_the_own_loop(tmp_path):
    async def scenario():
        core, plan, events, _, _ = await ready_core(tmp_path)
        try:
            cid = await new(core, events, tmp_path)
            plan.script(say("one", used=usage(100, 10, cached=20)))
            await core.call("session.send", {"id": cid, "text": "first"})
            await done(events, cid, 110)
            assert meta(events, cid)["usageModels"] == {"chatgpt:gpt-6.1-sol": 110}
            await core.call("session.choose", {"id": cid, "field": "model", "value": "chatgpt:gpt-6-luna"})
            plan.script(say("two", used=usage(200, 20, cached=50)))
            await core.call("session.send", {"id": cid, "text": "second"})
            await done(events, cid, 330)
            expected = {"chatgpt:gpt-6.1-sol": 110, "chatgpt:gpt-6-luna": 220}
            assert meta(events, cid)["usageModels"] == expected
            controller = core._by_cid[cid].controller
            native = controller.session_id
            await core.call("session.close", {"id": cid})
            await until(lambda: controller.state is SessionState.CLOSED)
            reopened = (await core.call("session.openSaved", {"projectId": str(tmp_path / "proj"), "id": "cg:" + native}))["id"]
            await until(lambda: meta(events, reopened).get("mark") == "ready")
            assert (Tokens.of(meta(events, reopened).get("usage")) or Tokens()).total == 330, meta(events, reopened).get("usage")
            assert meta(events, reopened)["usageModels"] == expected
        finally:
            await core.shutdown()

    asyncio.run(scenario())


def test_usage_is_attributed_to_the_running_model_not_the_next_model_chosen(tmp_path):
    async def scenario():
        core, plan, events, _, _ = await ready_core(tmp_path)
        try:
            cid = await new(core, events, tmp_path)
            plan.script([("hold", "one"), *say("done", used=usage(100, 10, cached=20))])
            await core.call("session.send", {"id": cid, "text": "first"})
            await until(lambda: len(plan.requests) == 1)
            await core.call("session.choose", {"id": cid, "field": "model", "value": "chatgpt:gpt-6-luna"})
            plan.release("one")
            await done(events, cid, 110)
            assert meta(events, cid)["usageModels"] == {"chatgpt:gpt-6.1-sol": 110}
        finally:
            plan.release("one")
            await core.shutdown()

    asyncio.run(scenario())


def test_a_devin_turn_with_a_model_change_does_not_guess_the_split_of_its_aggregate(tmp_path):
    async def scenario():
        harness = make_devin(tmp_path)
        core, _, events, _, _ = await ready_core(tmp_path, devin_engine=harness.engine)
        harness.engine.settings = core.settings
        try:
            await until(lambda: any(m["source"] == "devin" for m in core.catalog.models))
            cid = await new(core, events, tmp_path)
            await core.call("session.choose", {"id": cid, "field": "model", "value": "devin:swe-2-high"})
            await core.call("session.send", {"id": cid, "text": "[slow] wait"})
            await until(lambda: meta(events, cid).get("usageModels") == {"devin:swe-2-high": 1010})
            await core.call("session.choose", {"id": cid, "field": "model", "value": "devin:glm-5-2"})
            await until(lambda: meta(events, cid).get("usageModels") == {})
            await core.call("session.stop", {"id": cid})
            await done(events, cid, 1010)
            assert meta(events, cid)["usageModels"] == {}
        finally:
            await core.shutdown()

    asyncio.run(scenario())


def test_an_engine_change_preserves_model_shares_and_caches_them_with_the_total(tmp_path):
    async def scenario():
        harness = make_devin(tmp_path)
        cache = Cache(tmp_path / "cache")
        core, plan, events, _, _ = await ready_core(tmp_path, devin_engine=harness.engine, cache=cache)
        harness.engine.settings = core.settings
        try:
            await until(lambda: any(m["source"] == "devin" for m in core.catalog.models))
            cid = await new(core, events, tmp_path)
            plan.script(say("one", used=usage(100, 10, cached=20)))
            await core.call("session.send", {"id": cid, "text": "first"})
            await done(events, cid, 110)
            await core.call("session.choose", {"id": cid, "field": "model", "value": "devin:swe-2-high"})
            await core.call("session.send", {"id": cid, "text": "second"})
            await done(events, cid, 1322)
            expected = {"chatgpt:gpt-6.1-sol": 110, "devin:swe-2-high": 1212}
            assert meta(events, cid)["usageModels"] == expected
            conv = core._by_cid[cid]
            key = core._row_of(conv)
        finally:
            await core.shutdown()
        cached = cache.load_session(key)
        assert cached["ledger"]["byModel"] == expected
        assert cached["meta"]["usageModels"] == expected
        assert sum(expected.values()) == Tokens.of(cached["meta"]["usage"]).total
        harness2 = make_devin(tmp_path)
        again, _, events2, _, _ = await ready_core(tmp_path, devin_engine=harness2.engine, cache=cache)
        harness2.engine.settings = again.settings
        try:
            await until(lambda: harness2.engine._models is not None)
            reopened = (await again.call("session.openSaved", {"projectId": str(tmp_path / "proj"), "id": key}))["id"]
            await done(events2, reopened, 1322)
            assert meta(events2, reopened)["usageModels"] == expected
            await again.call("session.send", {"id": reopened, "text": "third"})
            await done(events2, reopened, 2534)
            assert meta(events2, reopened)["usageModels"] == {"chatgpt:gpt-6.1-sol": 110, "devin:swe-2-high": 2424}
        finally:
            await again.shutdown()

    asyncio.run(scenario())
