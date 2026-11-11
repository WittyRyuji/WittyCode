"""``ApiBackend`` against servers on this machine: the models of the providers in settings.json, a request to each kind of API with its
key and headers, the events that come back, and what the user is told when it fails."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

import pytest

from desktop.core.chatgpt.errors import ChatGPTError, friendly_failure
from desktop.core.chatgpt.responses import Finished, ItemDone, TextDone, build_request, user_message
from desktop.core.llm.backend import ApiBackend, api_failure, is_api_model, route_of
from desktop.core.llm.wire import ANTHROPIC, CHAT, RESPONSES
from desktop.core.providers import ApiModelSpec, ProviderError, ProviderStore, _Key
from tests.fake_responses import say
from tests.llm_support import FakeApi, http_error


def settings_with(tmp_path: Path, base_url: str, **entry: Any) -> Path:
    """A settings.json with one custom model; a key in ``entry`` set to ``None`` is left out."""
    path = tmp_path / "factory.json"
    base = {"model": "m-1", "displayName": "Model One", "baseUrl": base_url, "apiKey": "${TEST_API_KEY}", "provider": "generic-chat-completion-api"}
    merged = {**base, **entry}
    path.write_text(json.dumps({"customModels": [{k: v for k, v in merged.items() if v is not None}], "other": 1}), encoding="utf-8")
    return path


def store_for(path: Path, environ: dict[str, str] | None = None, registry: dict[str, str] | None = None, run_command=None) -> ProviderStore:
    return ProviderStore(
        factory_settings=path, sidecar=path.with_name("providers.json"), environ={"TEST_API_KEY": "sk-test-1"} if environ is None else environ,
        registry_env=lambda: registry or {}, run_command=run_command,
    )


def request_for(model_id: str, reasoning: str | None = None) -> dict:
    return build_request(model=model_id, instructions="be brief", items=[user_message("hi")], tools=[], reasoning=reasoning)


async def ask(backend: ApiBackend, request: dict) -> list:
    return [event async for event in backend.stream(request)]


def model_id_of(store: ProviderStore) -> str:
    (spec,) = store.api_models()
    return spec.id


def spec_of(kind: str, **over: Any) -> ApiModelSpec:
    base: dict[str, Any] = dict(
        id="api:p:custom:m-0", native="m", display="M", provider_id="p", provider_name="P", kind=kind, base_url="https://x.example/v1/", window=None,
        max_output=None, reasoning="", images=True, bearer=False, headers=(), extra_args={}, key=_Key("env", "${K}"),
    )
    return ApiModelSpec(**{**base, **over})


# ------------------------------------------------------------------------------------------------ the models


def test_the_providers_entries_are_the_models_with_the_window_the_room_and_the_level_the_user_gave(tmp_path):
    path = settings_with(tmp_path, "https://api.example.com/v1", maxContextLimit=1_024_000, maxOutputTokens=128_000, reasoningEffort="high", noImageSupport=False)
    backend = ApiBackend(store_for(path))
    (model,) = backend.models()
    assert is_api_model(model.id) and model.id.startswith("api:") and model.id.endswith(":custom:Model-One-0")
    assert (model.display_name, model.context_window, model.default_effort, model.efforts, model.supports_images) == ("Model One", 1_024_000, "high", ("off", "low", "medium", "high"), True)
    spec = backend.spec(model.id)
    assert spec is not None and spec.native == "m-1" and spec.max_output == 128_000 and spec.kind == "generic-chat-completion-api"


def test_without_a_level_there_is_no_reasoning_menu_and_a_chat_model_takes_no_pictures_unless_told(tmp_path):
    (chat,) = ApiBackend(store_for(settings_with(tmp_path, "https://a.example/v1"))).models()
    assert (chat.efforts, chat.default_effort, chat.context_window, chat.supports_images) == ((), "", None, False)
    claude = ApiBackend(store_for(settings_with(tmp_path, "https://a.example", provider="anthropic"))).models()[0]
    assert claude.supports_images is True


def test_a_bedrock_model_is_one_of_the_own_loop_and_a_model_that_is_gone_is_unknown(tmp_path):
    path = tmp_path / "f.json"
    entry = {"model": "b", "baseUrl": "https://bedrock-runtime.us-west-2.amazonaws.com", "provider": "bedrock-converse", "bedrock": {"awsRegion": "us-west-2"}}
    path.write_text(json.dumps({"customModels": [entry]}), encoding="utf-8")
    backend = ApiBackend(store_for(path))
    (model,) = backend.models()
    assert model.display_name == "b" and model.description == "AWS Bedrock · us-west-2 · b" and backend.spec(model.id) is not None
    assert backend.spec("api:x:custom:b-0") is None and backend.spec("gpt-6.1-sol") is None


def test_the_models_are_read_again_when_the_file_changes_and_not_before(tmp_path):
    path = settings_with(tmp_path, "https://a.example/v1")
    backend = ApiBackend(store_for(path))
    first = backend.specs()
    assert backend.specs() is first, "nothing changed: the loop asks many times a turn, the file is read once"
    path.write_text(json.dumps({"customModels": []}), encoding="utf-8")
    os.utime(path, ns=(1, path.stat().st_mtime_ns + 5_000_000))
    assert backend.models() == []


# ------------------------------------------------------------------------------------------------ the routes


def test_each_kind_signs_its_request_its_own_way_and_the_users_headers_win():
    chat = route_of(spec_of("generic-chat-completion-api"), "k1", {"X-Team": "core"})
    assert (chat.kind, chat.url) == (CHAT, "https://x.example/v1/chat/completions")
    assert chat.headers == {"Accept": "text/event-stream", "Authorization": "Bearer k1", "X-Team": "core"}
    claude = route_of(spec_of("anthropic", base_url="https://api.anthropic.com"), "k2", {})
    assert (claude.kind, claude.url) == (ANTHROPIC, "https://api.anthropic.com/v1/messages")
    assert claude.headers["x-api-key"] == "k2" and claude.headers["anthropic-version"] == "2023-06-01" and "Authorization" not in claude.headers
    assert route_of(spec_of("anthropic", base_url="https://gw.example/v1"), "k", {}).url == "https://gw.example/v1/messages"
    bearer = route_of(spec_of("anthropic", bearer=True), "k3", {"AUTHORIZATION": "Bearer mine"})
    assert bearer.headers["AUTHORIZATION"] == "Bearer mine" and "Authorization" not in bearer.headers and "x-api-key" not in bearer.headers
    plain = route_of(spec_of("openai"), None, {})
    assert (plain.kind, plain.url) == (RESPONSES, "https://x.example/v1/responses") and "Authorization" not in plain.headers


# ------------------------------------------------------------------------------------------------ a request, end to end


def test_a_chat_model_is_asked_at_its_own_server_with_its_key_and_its_headers(tmp_path):
    async def go() -> None:
        with FakeApi() as api:
            api.script([
                {"id": "c1", "choices": [{"index": 0, "delta": {"content": "Hi there."}, "finish_reason": "stop"}]},
                {"id": "c1", "choices": [], "usage": {"prompt_tokens": 30, "completion_tokens": 4}},
                "[DONE]",
            ])
            store = store_for(settings_with(tmp_path, api.base_url, extraHeaders={"X-Team": "core"}, extraArgs={"temperature": 0}))
            events = await ask(ApiBackend(store), request_for(model_id_of(store), reasoning="off"))
            ((path, headers, body),) = api.posts
            assert path == "/v1/chat/completions" and headers["authorization"] == "Bearer sk-test-1" and headers["x-team"] == "core"
            assert body["model"] == "m-1" and body["temperature"] == 0 and body["messages"][0] == {"role": "system", "content": "be brief"}
            assert "reasoning_effort" not in body, "off is the menu's word, not a level the API knows"
            assert [e for e in events if isinstance(e, TextDone)] == [TextDone("msg_0", "Hi there.")]
            done = events[-1]
            assert isinstance(done, Finished) and done.usage is not None and done.usage.context == 34

    asyncio.run(go())


def test_a_claude_model_is_asked_with_the_x_api_key_and_a_responses_model_with_a_bearer(tmp_path):
    async def go() -> None:
        with FakeApi() as api:
            api.script(
                [
                    ("message_start", {"type": "message_start", "message": {"id": "m1", "usage": {"input_tokens": 5}}}),
                    ("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}),
                    ("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Yes."}}),
                    ("content_block_stop", {"type": "content_block_stop", "index": 0}),
                    ("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 2}}),
                    ("message_stop", {"type": "message_stop"}),
                ],
                list(say("Sure.")),
            )
            claude = store_for(settings_with(tmp_path, api.base_url, provider="anthropic", maxOutputTokens=4000))
            events = await ask(ApiBackend(claude), request_for(model_id_of(claude)))
            path, headers, body = api.posts[0]
            assert path == "/v1/messages" and headers["x-api-key"] == "sk-test-1" and headers["anthropic-version"] == "2023-06-01"
            assert body["max_tokens"] == 4000 and [e for e in events if isinstance(e, TextDone)][0].text == "Yes."

            openai = store_for(settings_with(tmp_path, api.base_url, provider="openai", maxOutputTokens=500))
            events = await ask(ApiBackend(openai), request_for(model_id_of(openai)))
            path, headers, body = api.posts[1]
            assert path == "/v1/responses" and headers["authorization"] == "Bearer sk-test-1"
            assert body["model"] == "m-1" and body["max_output_tokens"] == 500 and "reasoning" not in body and "include" not in body, "no level: no reasoning fields"
            assert [e for e in events if isinstance(e, ItemDone)][0].item["type"] == "message"

    asyncio.run(go())


def test_a_responses_model_with_a_level_asks_for_reasoning_and_its_encrypted_content_back(tmp_path):
    async def go() -> None:
        with FakeApi() as api:
            api.script(list(say("ok")))
            store = store_for(settings_with(tmp_path, api.base_url, provider="openai", reasoningEffort="medium"))
            await ask(ApiBackend(store), request_for(model_id_of(store), reasoning="medium"))
            body = api.posts[0][2]
            assert body["reasoning"]["effort"] == "medium" and body["include"] == ["reasoning.encrypted_content"]
            assert body["store"] is False and body["stream"] is True

    asyncio.run(go())


def test_the_thoughts_another_api_wrote_are_not_sent_to_a_responses_endpoint_but_openais_own_are(tmp_path):
    from desktop.core.chatgpt.responses import for_responses

    foreign = {"type": "reasoning", "summary": [{"type": "summary_text", "text": "x"}], "wc_reasoning": "x", "wc_origin": "api.deepseek.com"}
    own = {"type": "reasoning", "summary": [], "encrypted_content": "gAAA"}
    message = {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "hi"}], "wc_origin": "stray"}
    assert for_responses([user_message("go"), foreign, own, message]) == [
        user_message("go"),
        own,
        {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "hi"}]},
    ]

    async def go() -> None:
        with FakeApi() as api:
            api.script(list(say("ok")))
            store = store_for(settings_with(tmp_path, api.base_url, provider="openai"))
            request = build_request(model=model_id_of(store), instructions="", items=[user_message("go"), foreign, own], tools=[], reasoning=None)
            assert foreign in request["input"], "the request the loop builds keeps them: the adapters of the other APIs read them from it"
            await ask(ApiBackend(store), request)
            assert api.posts[0][2]["input"] == [user_message("go"), own]

    asyncio.run(go())


# ------------------------------------------------------------------------------------------------ the engine in front of it


def test_the_engine_knows_an_api_models_name_window_and_pictures_and_nothing_of_one_that_is_gone(tmp_path):
    from tests.chatgpt_support import make_engine

    path = settings_with(tmp_path, "https://api.example.com/v1", maxContextLimit=64_000, noImageSupport=True)
    store = store_for(path)
    engine = make_engine(tmp_path, None, signed_in=False, api=ApiBackend(store))
    model_id = model_id_of(store)
    info = engine.model_info(model_id)
    assert info is not None and info.display_name == "Model One" and info.supports_images is False
    assert engine.model_window(model_id) == 64_000
    assert engine.model_info("api:gone:custom:x-0") is None and engine.model_window("api:gone:custom:x-0") is None
    assert engine.model_info("gpt-6.1-sol") is not None, "the plan's own models are still listed"
    assert engine.model_window(model_id.replace("api:", "plan:", 1)) is None


def test_what_another_api_wrote_does_not_reach_openai_through_the_plan(tmp_path):
    from tests.chatgpt_support import make_engine
    from tests.fake_responses import FakeResponses
    from tests.chatgpt_support import ACCESS_TOKEN

    foreign = {"type": "reasoning", "summary": [{"type": "summary_text", "text": "x"}], "wc_reasoning": "x", "wc_origin": "api.deepseek.com"}
    own = {"type": "reasoning", "summary": [], "encrypted_content": "gAAA"}

    async def go() -> None:
        with FakeResponses(token=ACCESS_TOKEN) as fake:
            fake.script(say("ok"))
            engine = make_engine(tmp_path, fake)
            request = build_request(model="gpt-6.1-sol", instructions="", items=[user_message("go"), foreign, own], tools=[], reasoning=None)
            await ask(engine, request)
            assert fake.requests[0]["input"] == [user_message("go"), own]
            await engine.close_all()

    asyncio.run(go())


# ------------------------------------------------------------------------------------------------ keys


def test_a_key_set_after_the_application_started_works_at_once_and_a_missing_one_says_which(tmp_path):
    path = settings_with(tmp_path, "https://a.example/v1")
    spec = store_for(path, environ={}, registry={"TEST_API_KEY": "from-registry"}).api_models()[0]
    assert store_for(path, environ={}, registry={"TEST_API_KEY": "from-registry"}).api_key(spec) == "from-registry"
    with pytest.raises(ProviderError, match="TEST_API_KEY"):
        store_for(path, environ={}).api_key(spec)


def test_a_key_command_is_run_once_within_its_time_to_live(tmp_path):
    path = settings_with(tmp_path, "https://a.example/v1", apiKey=None, apiKeyHelper="get-key", apiKeyHelperTtlMs=60_000)
    calls: list[str] = []
    store = store_for(path, run_command=lambda command: calls.append(command) or "helper-key")
    spec = store.api_models()[0]
    assert store.api_key(spec) == "helper-key" and store.api_key(spec) == "helper-key" and calls == ["get-key"]


def test_a_provider_that_needs_no_key_sends_none(tmp_path):
    store = store_for(settings_with(tmp_path, "http://localhost:11434/v1", apiKey=None))
    assert store.api_key(store.api_models()[0]) is None


# ------------------------------------------------------------------------------------------------ failures


@pytest.mark.parametrize(
    ("status", "message", "headline"),
    [
        (401, "Incorrect API key", "没有接受这个密钥"),
        (403, "Forbidden", "没有接受这个密钥"),
        (404, "model not found", "找不到这个接口或模型「m-1」"),
        (429, "slow down", "请求太频繁或额度用完了"),
        (503, "overloaded", "暂时出错了"),
    ],
)
def test_what_the_user_is_told_names_the_provider_and_what_to_check(tmp_path, status, message, headline):
    async def go() -> None:
        with FakeApi() as api:
            store = store_for(settings_with(tmp_path, api.base_url))
            api.script(http_error(status, {"error": {"message": message}}))
            with pytest.raises(ChatGPTError) as raised:
                await ask(ApiBackend(store), request_for(model_id_of(store)))
            text = friendly_failure(raised.value)
            provider = store.api_models()[0].provider_name
            assert headline in text and message in text and provider in text
            assert (raised.value.status, raised.value.code) == (status, "api")

    asyncio.run(go())


def test_a_refusal_about_the_request_itself_keeps_its_own_words_and_code(tmp_path):
    async def go() -> None:
        with FakeApi() as api:
            store = store_for(settings_with(tmp_path, api.base_url))
            api.script(http_error(400, {"error": {"message": "maximum context length is 8192 tokens", "code": "context_length_exceeded"}}))
            with pytest.raises(ChatGPTError) as raised:
                await ask(ApiBackend(store), request_for(model_id_of(store)))
            assert raised.value.code == "context_length_exceeded" and "maximum context length" in raised.value.message

    asyncio.run(go())


def test_a_key_that_is_not_set_and_a_model_that_was_removed_are_told_to_the_user(tmp_path):
    async def go() -> None:
        store = store_for(settings_with(tmp_path, "http://127.0.0.1:9/v1"), environ={})
        backend = ApiBackend(store)
        with pytest.raises(ChatGPTError, match="TEST_API_KEY") as unset:
            await ask(backend, request_for(model_id_of(store)))
        assert unset.value.code == "api" and friendly_failure(unset.value).startswith("环境变量 TEST_API_KEY")
        with pytest.raises(ChatGPTError, match="不在 API Provider 的设置里") as gone:
            await ask(backend, request_for("api:p:custom:gone-0"))
        assert gone.value.code == "api"

    asyncio.run(go())


def test_a_server_that_is_not_there_is_a_network_error_with_the_providers_name(tmp_path):
    async def go() -> None:
        store = store_for(settings_with(tmp_path, "http://127.0.0.1:9/v1"))
        with pytest.raises(ChatGPTError) as raised:
            await ask(ApiBackend(store), request_for(model_id_of(store)))
        assert friendly_failure(raised.value).startswith("连不上 ") and store.api_models()[0].provider_name in friendly_failure(raised.value)

    asyncio.run(go())


def test_a_failure_without_a_status_is_left_alone():
    cut = ChatGPTError("The stream ended before the response was complete.", code="stream")
    assert api_failure(cut, spec_of("generic-chat-completion-api")) is cut
