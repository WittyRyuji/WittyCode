"""Per-effort wire overrides use temporary settings and local HTTP servers, never a real model."""
from __future__ import annotations

import asyncio
import json

import pytest

from desktop.core.llm.anthropic import anthropic_body
from desktop.core.llm.backend import ApiBackend, route_of
from desktop.core.llm.chat import chat_body
from desktop.core.llm.responses_api import responses_body
from desktop.core.llm.wire import Route
from desktop.core.providers import EFFORTS, ProviderError
from tests.fake_responses import say
from tests.llm_support import FakeApi
from tests.test_llm_backend import ask, model_id_of, request_for, settings_with, spec_of, store_for
from tests.test_providers import draft, entries_of, make_store, model


def answer(kind):
    if kind == "openai":
        return list(say("Done."))
    if kind == "anthropic":
        return [
            ("message_start", {"type": "message_start", "message": {"id": "m", "usage": {"input_tokens": 1}}}),
            ("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 1}}),
            ("message_stop", {"type": "message_stop"}),
        ]
    return [{"choices": [{"index": 0, "delta": {"content": "Done."}, "finish_reason": "stop"}]}, "[DONE]"]


@pytest.mark.parametrize("kind", ["generic-chat-completion-api", "openai", "anthropic"])
def test_existing_wire_shapes_match_the_documented_problem_without_overrides(tmp_path, kind):
    async def go():
        with FakeApi() as api:
            api.script(answer(kind), answer(kind))
            store = store_for(settings_with(tmp_path, api.base_url, provider=kind, apiKey=None, reasoningEffort="high"))
            backend = ApiBackend(store)
            for effort in ("off", "low"):
                await ask(backend, request_for(model_id_of(store), effort))
            off, low = [post[2] for post in api.posts]
            if kind == "anthropic":
                assert off["thinking"] == {"type": "disabled"}
                assert low["thinking"] == {"type": "enabled", "budget_tokens": 2048}
                assert "output_config" not in low
            elif kind == "openai":
                assert "reasoning" not in off and "include" not in off
                assert low["reasoning"] == {"effort": "low", "summary": "auto"}
                assert low["include"] == ["reasoning.encrypted_content"]
            else:
                assert "reasoning_effort" not in off and "thinking" not in off
                assert low["reasoning_effort"] == "low"
    asyncio.run(go())


@pytest.mark.parametrize("kind", ["generic-chat-completion-api", "openai", "anthropic"])
def test_every_selected_effort_reaches_the_local_server_and_off_really_requests_disabled(tmp_path, kind):
    mapping = {}
    for effort in EFFORTS:
        if kind == "openai":
            mapping[effort] = {"reasoning": {"effort": "none" if effort == "off" else effort}, "include": None}
        elif kind == "anthropic":
            mapping[effort] = {"thinking": {"type": "disabled" if effort == "off" else "enabled"}, "output_config": None if effort == "off" else {"effort": effort}}
        else:
            mapping[effort] = {"thinking": {"type": "disabled" if effort == "off" else "enabled"}}

    async def go():
        with FakeApi() as api:
            store = store_for(settings_with(tmp_path, api.base_url, provider=kind, apiKey=None, reasoningEffort="high", reasoningArgs=mapping))
            backend = ApiBackend(store)
            for effort in (*EFFORTS, "off", "low"):
                api.script(answer(kind))
                await ask(backend, request_for(model_id_of(store), effort))
                body = api.posts[-1][2]
                assert "wc_reasoning_effort" not in body
                if kind == "openai":
                    assert body["reasoning"] == {"effort": "none" if effort == "off" else effort}
                    assert "include" not in body and body["stream"] is True and body["store"] is False
                else:
                    assert body["thinking"] == {"type": "disabled" if effort == "off" else "enabled"}
                    if kind == "anthropic":
                        assert "budget_tokens" not in body["thinking"]
                        assert body.get("output_config") == (None if effort == "off" else {"effort": effort})
                    elif effort != "off":
                        assert body["reasoning_effort"] == effort
    asyncio.run(go())


@pytest.mark.parametrize("kind,builder", [("chat", chat_body), ("responses", responses_body), ("anthropic", anthropic_body)])
def test_override_precedence_null_deletes_only_top_level_and_inputs_are_not_modified(kind, builder):
    mapping = {"low": {"reasoning": {"effort": "mapped", "nested": None}, "include": None, "temperature": 0.4}}
    route = Route(kind, "https://example.com/v1", "demo", extra_args={"temperature": 0.8}, reasoning_args=mapping)
    request = request_for("api:p:demo", "low")
    before = json.loads(json.dumps(request))
    body = builder(request, route)
    assert body["temperature"] == 0.8 and body["reasoning"] == {"effort": "mapped", "nested": None}
    assert "include" not in body and request == before and route.reasoning_args == mapping
    no_level = builder(request_for("api:p:demo"), route)
    assert "temperature" in no_level and "reasoning" not in no_level


@pytest.mark.parametrize("bad", ["{", "[]", "null", '{"typo": {}}', '{"off": []}', '{"off": null}', '{"off": true}', '{"low": {"value": NaN}}', '{"max": {"value": Infinity}}', 1, None])
def test_invalid_draft_mapping_is_rejected_before_any_file_is_written(tmp_path, bad):
    store = make_store(tmp_path)
    with pytest.raises(ProviderError, match="思考档位参数"):
        store.save(draft(reasoningArgs=bad))
    assert not store.models_file.exists() and not store.sidecar.exists()


def test_mapping_round_trip_and_legacy_edit_preserves_it_until_explicitly_cleared(tmp_path):
    store = make_store(tmp_path)
    mapping = {"off": {"thinking": {"type": "disabled"}}, "max": {"output_config": {"effort": "max"}}}
    saved = store.save(draft(models=[model("demo", reasoning="high")], reasoningArgs=json.dumps(mapping)))
    assert entries_of(store)[0]["reasoningArgs"] == mapping
    assert json.loads(saved["reasoningArgs"]) == mapping
    assert store.api_models()[0].reasoning_args == mapping
    old_client = {key: value for key, value in saved.items() if key != "reasoningArgs"}
    store.save(old_client)
    assert entries_of(store)[0]["reasoningArgs"] == mapping
    store.save({**saved, "reasoningArgs": ""})
    assert "reasoningArgs" not in entries_of(store)[0]
    assert store.api_models()[0].reasoning_args == {}


def test_mixed_entry_mappings_survive_an_unrelated_provider_edit(tmp_path):
    store = make_store(tmp_path)
    store.save(draft(models=[model("one", reasoning="high"), model("two", reasoning="high")]))
    raw = json.loads(store.models_file.read_text(encoding="utf-8"))
    one = {"off": {"thinking": {"type": "disabled"}}}
    two = {"off": {"reasoning": {"effort": "none"}}}
    for entry, mapping in zip(raw["customModels"], (one, two)):
        entry["reasoningArgs"] = mapping
    store.models_file.write_text(json.dumps(raw), encoding="utf-8")
    view = store.list()[0]
    assert view["reasoningArgsMixed"] is True and "reasoningArgs" not in view
    store.save({**view, "name": "Renamed"})
    assert [entry["reasoningArgs"] for entry in entries_of(store)] == [one, two]
    store.save({**view, "reasoningArgs": "{}"})
    assert all("reasoningArgs" not in entry for entry in entries_of(store))


def test_invalid_update_preserves_existing_files_and_invalid_manual_mapping_fails_explicitly(tmp_path):
    store = make_store(tmp_path)
    saved = store.save(draft())
    before = [path.read_bytes() for path in (store.models_file, store.sidecar)]
    with pytest.raises(ProviderError, match="思考档位参数"):
        store.save({**saved, "reasoningArgs": '{"off": []}'})
    assert [path.read_bytes() for path in (store.models_file, store.sidecar)] == before
    raw = json.loads(store.models_file.read_text(encoding="utf-8"))
    raw["customModels"][0]["reasoningArgs"] = {"typo": {}}
    store.models_file.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ProviderError, match="思考档位参数"):
        store.api_models()


def test_chatgpt_plan_and_bedrock_do_not_gain_provider_specific_overrides():
    from dataclasses import replace
    from desktop.core.providers import BedrockSettings
    from desktop.core.chatgpt.responses import build_request, user_message

    request = build_request(model="gpt-6.1-sol", instructions="", items=[user_message("Hi")], tools=[], reasoning="off")
    assert "wc_reasoning_effort" not in request and "reasoningArgs" not in request
    spec = spec_of("anthropic", reasoning_args={"off": {"thinking": {"type": "disabled"}}})
    bedrock = replace(spec, bedrock=BedrockSettings(method="aws-credentials", region="us-west-2"))
    assert route_of(bedrock, None, {}).reasoning_args == {}
    assert route_of(spec, None, {}).reasoning_args == spec.reasoning_args
