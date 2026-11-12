"""Exact documented API windows, provider precedence and reasoning levels. No real accounts or model calls."""

import asyncio

import pytest

from desktop.core.catalog import build_catalog
from desktop.core.chatgpt.errors import ChatGPTError
from desktop.core.llm.backend import ApiBackend
from desktop.core.llm.model_specs import known_window
from desktop.core.providers import EFFORTS
from tests.llm_support import FakeApi
from tests.test_app_api import answer_of, api_core, api_model_id, meta, new_session, says
from tests.test_llm_backend import ask, model_id_of, request_for, settings_with, store_for
from tests.test_providers import Opener, draft, make_store
from tests.fakes import until


@pytest.mark.parametrize("model_id, window", [("gpt-6.1-sol", 1_050_000), ("gpt-6.1-terra", 1_050_000), ("gpt-6-astra", 1_050_000), ("gpt-5-mini", 400_000), ("gpt-4.1", 1_047_576)])
def test_exact_documented_models_have_a_window(model_id, window):
    assert known_window(model_id) == window
    for unknown in (model_id + "-unknown", "my-" + model_id, "vendor/" + model_id, "unlisted-model"):
        assert known_window(unknown) is None, "no family, gateway alias or version guessing"


def test_a_known_window_is_used_without_changing_the_users_file(tmp_path):
    path = settings_with(tmp_path, "https://api.example/v1", model="gpt-6.1-sol", maxContextLimit=None)
    before = path.read_bytes()
    store = store_for(path)
    spec = store.api_models()[0]
    assert spec.window == 1_050_000
    assert ApiBackend(store).models()[0].context_window == 1_050_000
    assert build_catalog(api_models=[spec]).models[0]["window"] == 1_050_000
    assert store.window_of(spec.id.split(":", 2)[2]) == 1_050_000
    assert "contextWindow" not in store.list()[0]["models"][0], "an automatic fallback must not turn into an explicit override"
    assert path.read_bytes() == before
    overridden = store_for(settings_with(tmp_path, "https://api.example/v1", model="gpt-6.1-sol", maxContextLimit=64_000))
    assert overridden.api_models()[0].window == 64_000, "a gateway may impose a smaller limit"


def test_model_lists_keep_server_limits_before_documented_fallbacks(tmp_path):
    opener = Opener({"data": [{"id": "gpt-6.1-sol", "max_context_window": 72_000}, {"id": "gpt-6-astra"}, {"id": "unknown"}]})
    store = make_store(tmp_path, environ={"OPENROUTER_API_KEY": "test-key"}, opener=opener)
    assert asyncio.run(store.model_details(draft())) == [
        {"id": "gpt-6.1-sol", "contextWindow": 72_000, "contextSource": "server"},
        {"id": "gpt-6-astra", "contextWindow": 1_050_000, "contextSource": "catalog"},
        {"id": "unknown"},
    ]


def test_an_unknown_window_refuses_before_credentials_or_network(tmp_path):
    async def go():
        with FakeApi() as api:
            store = store_for(settings_with(tmp_path, api.base_url, maxContextLimit=None), environ={})
            with pytest.raises(ChatGPTError, match="上下文上限") as error:
                await ask(ApiBackend(store), request_for(model_id_of(store)))
            assert error.value.code == "api" and "填写" in str(error.value)
            assert api.posts == []
    asyncio.run(go())


@pytest.mark.parametrize("level", ["minimal", "xhigh", "max"])
def test_all_configurable_levels_reach_the_api_without_downgrading(tmp_path, level):
    async def go():
        with FakeApi() as api:
            from tests.fake_responses import say
            api.script(list(say("ok")))
            store = store_for(settings_with(tmp_path, api.base_url, provider="openai", reasoningEffort=level))
            backend = ApiBackend(store)
            model = backend.models()[0]
            assert model.default_effort == level and model.efforts == EFFORTS
            assert build_catalog(api_models=store.api_models()).models[0]["efforts"] == list(EFFORTS)
            await ask(backend, request_for(model.id, reasoning=level))
            assert api.posts[0][2]["reasoning"]["effort"] == level
    asyncio.run(go())


def test_a_known_api_window_reaches_the_session_status_and_usage(tmp_path):
    async def go():
        with FakeApi() as api:
            api.script(says("ready"))
            core, _, events, _, _ = await api_core(tmp_path, api, model="gpt-6.1-sol", maxContextLimit=None, reasoningEffort="max")
            try:
                cid = await new_session(core, events, tmp_path, api_model_id(events))
                await core.call("session.choose", {"id": cid, "field": "reasoning", "value": "max"})
                await core.call("session.send", {"id": cid, "text": "hello"})
                await until(lambda: meta(events, cid).get("mark") == "ready" and answer_of(events, cid) == "ready")
                assert meta(events, cid)["context"]["window"] == 1_050_000
                assert meta(events, cid)["context"]["used"] == 220
                assert api.posts[0][2]["reasoning_effort"] == "max"
            finally:
                await core.shutdown()
    asyncio.run(go())
