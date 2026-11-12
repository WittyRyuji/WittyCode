"""Models on Amazon Bedrock through the API backend: what the entries become, and a request to a server on this machine that answers
the way Bedrock does (an AWS event stream), signed or sent with a Bedrock API key."""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

import pytest

from desktop.core.chatgpt.errors import ChatGPTError, friendly_failure
from desktop.core.chatgpt.responses import Finished, TextDone, build_request, user_message
from desktop.core.llm.backend import ApiBackend
from desktop.core.providers import ProviderError, ProviderStore
from tests.llm_support import FakeApi, event_frame, event_stream, http_error

CLAUDE = "anthropic.claude-sonnet-4-5-20250929-v1:0"
KEYS = {"AWS_ACCESS_KEY_ID": "AKIDTEST", "AWS_SECRET_ACCESS_KEY": "SECRETTEST", "AWS_SESSION_TOKEN": "TOKENTEST"}


def write_settings(tmp_path: Path, *entries: dict[str, Any]) -> Path:
    path = tmp_path / "factory.json"
    path.write_text(json.dumps({"customModels": list(entries)}), encoding="utf-8")
    return path


def entry(kind: str = "bedrock-converse", base: str = "", **bedrock: Any) -> dict[str, Any]:
    block = {"authMethod": "aws-credentials", "awsRegion": "us-east-1", **bedrock}
    if base:
        block["bedrockBaseUrl"] = base
    return {"model": CLAUDE, "displayName": "Claude on Bedrock", "provider": kind, "bedrock": block}


def store_for(path: Path, environ: dict[str, str] | None = None, **more: Any) -> ProviderStore:
    return ProviderStore(models_file=path, sidecar=path.with_name("providers.json"), environ=KEYS if environ is None else environ, registry_env=lambda: {}, aws_home=path.parent / "aws-home", **more)


def request_for(store: ProviderStore, reasoning: str | None = None) -> dict:
    (spec,) = store.api_models()
    return build_request(model=spec.id, instructions="be brief", items=[user_message("hi")], tools=[], reasoning=reasoning)


async def ask(backend: ApiBackend, request: dict) -> list:
    return [event async for event in backend.stream(request)]


def answer(*texts: str) -> event_stream:
    frames = [event_frame("messageStart", {"role": "assistant"})]
    frames += [event_frame("contentBlockDelta", {"contentBlockIndex": 0, "delta": {"text": text}}) for text in texts]
    frames += [event_frame("contentBlockStop", {"contentBlockIndex": 0}), event_frame("messageStop", {"stopReason": "end_turn"})]
    frames += [event_frame("metadata", {"usage": {"inputTokens": 12, "outputTokens": 3}})]
    whole = b"".join(frames)
    return event_stream(*[whole[i : i + 11] for i in range(0, len(whole), 11)])  # (cut into pieces that end inside frames)


def run(coro):
    import asyncio

    return asyncio.run(coro)


# ------------------------------------------------------------------------------------------------ the models


def test_bedrock_entries_of_every_kind_are_models_of_the_own_loop(tmp_path):
    path = write_settings(
        tmp_path,
        {**entry(), "displayName": "A", "maxContextLimit": 200_000, "maxOutputTokens": 16_000, "reasoningEffort": "medium"},
        {**entry("anthropic"), "displayName": "B", "enableThinking": True, "thinkingMaxTokens": 6000},
        {**entry("openai"), "displayName": "C", "model": "openai.gpt-oss-120b-1:0"},
    )
    specs = store_for(path).api_models()
    assert [(s.display, s.kind) for s in specs] == [("A", "bedrock-converse"), ("B", "anthropic"), ("C", "openai")]
    a, b, c = specs
    assert a.bedrock is not None and a.bedrock.region == "us-east-1" and a.bedrock.method == "aws-credentials"
    assert (a.window, a.max_output, a.reasoning, a.images, a.base_url) == (200_000, 16_000, "medium", True, "")
    assert (a.thinking, b.thinking, c.thinking) == (None, 6000, None)
    assert all(spec.provider_name.startswith("AWS Bedrock") for spec in specs)
    assert len(ApiBackend(store_for(path)).models()) == 3


def test_only_the_kinds_that_think_take_thinking_from_the_entry(tmp_path):
    records = [{**entry(kind), "displayName": kind, "enableThinking": True} for kind in ("bedrock-converse", "anthropic", "openai")]
    specs = store_for(write_settings(tmp_path, *records)).api_models()
    assert [(spec.kind, spec.thinking) for spec in specs] == [("bedrock-converse", 8192), ("anthropic", 8192), ("openai", None)]
    assert store_for(write_settings(tmp_path, {**entry(), "enableThinking": "yes"})).api_models()[0].thinking is None, "only a real true turns it on"


def test_a_method_nobody_knows_is_no_method(tmp_path):
    (spec,) = store_for(write_settings(tmp_path, entry(authMethod="telepathy"))).api_models()
    assert spec.bedrock.method == ""


def test_the_bedrock_block_is_kept_as_written_and_unset_methods_are_none(tmp_path):
    block = entry(awsProfile="work", awsAuthRefresh="aws sso login", awsCredentialExport="export-it", requestMetadata={"team": "${TEAM}", "n": 3})
    del block["bedrock"]["authMethod"]
    (spec,) = store_for(write_settings(tmp_path, block)).api_models()
    settings = spec.bedrock
    assert (settings.method, settings.profile, settings.auth_refresh, settings.credential_export) == ("", "work", "aws sso login", "export-it")
    assert settings.metadata == (("team", "${TEAM}"),), "only string values are tags"


# ------------------------------------------------------------------------------------------------ Converse


def test_a_converse_request_is_signed_and_its_stream_is_read(tmp_path):
    with FakeApi() as api:
        api.script(answer("Hel", "lo"))
        base = api.base_url.removesuffix("/v1")
        store = store_for(write_settings(tmp_path, {**entry(base=base), "reasoningEffort": "low", "maxOutputTokens": 20_000}))
        events = run(ask(ApiBackend(store), request_for(store, "low")))
    ((path, headers, body),) = api.posts
    assert path == "/model/anthropic.claude-sonnet-4-5-20250929-v1%3A0/converse-stream"
    assert headers["authorization"].startswith("AWS4-HMAC-SHA256 Credential=AKIDTEST/") and "/us-east-1/bedrock/aws4_request" in headers["authorization"]
    assert "SignedHeaders=content-type;host;x-amz-date;x-amz-security-token," in headers["authorization"]
    assert headers["x-amz-security-token"] == "TOKENTEST" and len(headers["x-amz-date"]) == 16 and "SECRETTEST" not in json.dumps(headers)
    assert body["messages"] == [{"role": "user", "content": [{"text": "hi"}, {"cachePoint": {"type": "default"}}]}]
    assert body["system"][0] == {"text": "be brief"}
    assert body["additionalModelRequestFields"]["thinking"]["type"] == "enabled" and body["inferenceConfig"] == {"maxTokens": 20_000}
    assert [e.text for e in events if isinstance(e, TextDone)] == ["Hello"]
    finished = events[-1]
    assert isinstance(finished, Finished) and (finished.usage.input_tokens, finished.usage.output_tokens) == (12, 3)


def test_a_bedrock_api_key_is_sent_as_a_bearer_token_and_nothing_is_signed(tmp_path):
    with FakeApi() as api:
        api.script(answer("ok"))
        store = store_for(write_settings(tmp_path, entry(base=api.base_url.removesuffix("/v1"), authMethod="environment-api-key")), {"AWS_BEARER_TOKEN_BEDROCK": "bedrock-key-1"})
        run(ask(ApiBackend(store), request_for(store)))
    ((_path, headers, _body),) = api.posts
    assert headers["authorization"] == "Bearer bedrock-key-1" and "x-amz-date" not in headers


def test_the_users_headers_are_signed_with_the_rest_and_the_variables_in_the_block_are_filled_in(tmp_path):
    with FakeApi() as api:
        api.script(answer("ok"))
        base = api.base_url.removesuffix("/v1")
        record = {**entry(base="${ENDPOINT}", awsRegion="${WHERE}", requestMetadata={"team": "${TEAM}", "project": "wittycode"}), "extraHeaders": {"X-Team": "platform"}}
        store = store_for(write_settings(tmp_path, record), {**KEYS, "ENDPOINT": base, "WHERE": "eu-west-1", "TEAM": "core"})
        run(ask(ApiBackend(store), request_for(store)))
    ((_path, headers, body),) = api.posts
    assert "/eu-west-1/bedrock/aws4_request" in headers["authorization"]
    assert "x-team" in headers["authorization"] and headers["x-team"] == "platform"
    assert body["requestMetadata"] == {"team": "core", "project": "wittycode"}


def test_the_anthropic_wire_goes_to_invoke_with_the_metadata_in_a_signed_header(tmp_path):
    chunks = [
        {"type": "message_start", "message": {"id": "m", "usage": {"input_tokens": 5, "output_tokens": 1}}},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Hi there"}},
        {"type": "content_block_stop", "index": 0},
        {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 4}},
        {"type": "message_stop"},
    ]
    with FakeApi() as api:
        api.script(event_stream(*[event_frame("chunk", {"bytes": base64.b64encode(json.dumps(c).encode()).decode()}) for c in chunks]))
        store = store_for(write_settings(tmp_path, entry("anthropic", base=api.base_url.removesuffix("/v1"), requestMetadata={"team": "core"})))
        events = run(ask(ApiBackend(store), request_for(store)))
    ((path, headers, body),) = api.posts
    assert path == "/model/anthropic.claude-sonnet-4-5-20250929-v1%3A0/invoke-with-response-stream"
    assert json.loads(headers["x-amzn-bedrock-request-metadata"]) == {"team": "core"} and "x-amzn-bedrock-request-metadata" in headers["authorization"]
    assert body["anthropic_version"] == "bedrock-2023-05-31" and "model" not in body and "stream" not in body
    assert [e.text for e in events if isinstance(e, TextDone)] == ["Hi there"]


def test_the_openai_wire_goes_to_responses_with_a_bearer_key_and_sends_no_metadata(tmp_path):
    from tests.fake_responses import say

    with FakeApi() as api:
        api.script(say("fine"))
        store = store_for(write_settings(tmp_path, entry("openai", base=api.base_url, authMethod="environment-api-key", requestMetadata={"team": "core"})), {"AWS_BEARER_TOKEN_BEDROCK": "k"})
        events = run(ask(ApiBackend(store), request_for(store)))
    ((path, headers, body),) = api.posts
    assert path == "/v1/responses" and headers["authorization"] == "Bearer k" and "requestMetadata" not in body and "x-amzn-bedrock-request-metadata" not in headers
    assert any(isinstance(e, TextDone) for e in events)


# ------------------------------------------------------------------------------------------------ what goes wrong


def failing(tmp_path, turn, **more) -> ChatGPTError:
    with FakeApi() as api:
        api.script(turn)
        store = store_for(write_settings(tmp_path, entry(base=api.base_url.removesuffix("/v1"))), **more)
        with pytest.raises(ChatGPTError) as caught:
            run(ask(ApiBackend(store), request_for(store)))
    return caught.value


@pytest.mark.parametrize(
    ("status", "error_type", "message", "headline"),
    [
        (403, "ExpiredTokenException:http://internal.amazon.com/coral/com.amazon.coral.service/", "The security token included in the request is expired", "凭据可能过期"),
        (403, "UnrecognizedClientException", "The security token included in the request is invalid.", "凭据可能过期"),
        (403, "AccessDeniedException", "You don't have access to the model with the specified model ID.", "Model access"),
        (400, "ValidationException", "Invocation of model ID anthropic.claude-sonnet-4 with on-demand throughput isn't supported. Retry with an inference profile.", "推理配置文件"),
        (404, "ResourceNotFoundException", "Could not resolve the foundation model", "找不到模型"),
        (429, "ThrottlingException", "Too many requests", "请求太频繁"),
        (500, "InternalServerException", "boom", "暂时出错"),
    ],
)
def test_what_aws_says_is_told_with_what_usually_cures_it(tmp_path, status, error_type, message, headline):
    error = failing(tmp_path, http_error(status, {"message": message}, {"x-amzn-ErrorType": error_type, "x-amzn-RequestId": "req-1"}))
    assert error.code == "api" and headline in error.message and error.status == status and error.request_id == "req-1"
    assert message in error.message or status == 403 and error_type.startswith("Expired")
    assert friendly_failure(error) == error.message


def test_a_bedrock_api_key_that_is_refused_is_told_so_not_as_a_missing_permission(tmp_path):
    error = failing(tmp_path, http_error(403, {"Message": "Invalid API Key format: Must start with pre-defined prefix"}), )
    assert error.code == "api" and "API Key" in error.message and "AWS_BEARER_TOKEN_BEDROCK" in error.message
    assert "Invalid API Key format" in error.message, "AWS's own words come with it, although it capitalises the key of its message"


def test_a_validation_error_about_the_request_keeps_its_own_words(tmp_path):
    error = failing(tmp_path, http_error(400, {"message": "The maximum tokens you requested exceeds the model limit of 8192."}, {"x-amzn-ErrorType": "ValidationException"}))
    assert error.code == "ValidationException" and "exceeds the model limit" in error.message


def test_an_exception_in_the_middle_of_the_stream_is_told_the_same_way(tmp_path):
    turn = event_stream(event_frame("contentBlockDelta", {"contentBlockIndex": 0, "delta": {"text": "par"}}), event_frame("throttlingException", {"message": "Too many requests"}, kind="exception"))
    error = failing(tmp_path, turn)
    assert error.code == "api" and "请求太频繁" in error.message and error.status == 429


def test_missing_credentials_a_missing_region_and_a_bad_variable_say_what_to_do(tmp_path):
    with pytest.raises(ChatGPTError, match="找不到 AWS 凭据") as nothing:
        run(ask(ApiBackend(store_for(write_settings(tmp_path, entry()), {})), request_for(store_for(write_settings(tmp_path, entry()), {}))))
    assert nothing.value.code == "api"
    no_region = {"model": CLAUDE, "provider": "bedrock-converse", "bedrock": {"authMethod": "environment-api-key"}}
    store = store_for(write_settings(tmp_path, no_region), {"AWS_BEARER_TOKEN_BEDROCK": "k"})
    with pytest.raises(ChatGPTError, match="没有指定 AWS 区域"):
        run(ask(ApiBackend(store), request_for(store)))
    store = store_for(write_settings(tmp_path, entry(awsRegion="${NOWHERE}")), KEYS)
    with pytest.raises(ChatGPTError, match="环境变量 NOWHERE 没有设置"):
        run(ask(ApiBackend(store), request_for(store)))


def test_request_metadata_beyond_awss_limits_fails_the_model_and_names_the_key(tmp_path):
    many = {f"k{i}": "v" for i in range(17)}
    for tags, needle in ((many, "最多 16"), ({"bad key!": "v"}, "bad key!"), ({"k": "x" * 257}, "「k」")):
        store = store_for(write_settings(tmp_path, entry(requestMetadata=tags)))
        with pytest.raises(ProviderError, match=needle):
            store.bedrock_plan(store.api_models()[0])
    store = store_for(write_settings(tmp_path, entry("openai", requestMetadata=many)))
    assert store.bedrock_plan(store.api_models()[0]).metadata == {}, "a wire that sends no tags is not failed by them"


def test_a_credentials_command_that_prints_json_signs_the_request(tmp_path):
    output = json.dumps({"Credentials": {"AccessKeyId": "AKIDCMD", "SecretAccessKey": "S", "SessionToken": "T"}})
    with FakeApi() as api:
        api.script(answer("ok"))
        record = entry(base=api.base_url.removesuffix("/v1"), authMethod="credentials-command", awsCredentialExport="print-creds")
        store = store_for(write_settings(tmp_path, record), {}, run_command=lambda command: output if command == "print-creds" else "")
        run(ask(ApiBackend(store), request_for(store)))
    assert api.posts[0][1]["authorization"].startswith("AWS4-HMAC-SHA256 Credential=AKIDCMD/")


def test_a_model_that_was_taken_out_of_the_settings_is_told_to_the_user(tmp_path):
    store = store_for(write_settings(tmp_path, entry()))
    request = request_for(store)
    store.models_file.write_text(json.dumps({"customModels": []}), encoding="utf-8")
    with pytest.raises(ChatGPTError, match="已经不在 API Provider"):
        run(ask(ApiBackend(store_for(store.models_file)), request))


def test_the_openai_wire_without_an_endpoint_goes_to_mantle_and_is_signed_for_it(tmp_path):
    seen: dict[str, Any] = {}

    class Recorder:
        def stream_lines(self, method, url, *, headers=None, json_body=None, body=None, timeout=0):
            seen.update(method=method, url=url, headers=dict(headers or {}))

            async def nothing():
                return
                yield  # pragma: no cover

            return nothing()

    store = store_for(write_settings(tmp_path, entry("openai")))
    with pytest.raises(ChatGPTError, match="ended before"):
        run(ask(ApiBackend(store, http=Recorder()), request_for(store)))
    assert seen["method"] == "POST" and seen["url"] == "https://bedrock-mantle.us-east-1.api.aws/v1/responses"
    assert "/us-east-1/bedrock-mantle/aws4_request" in seen["headers"]["Authorization"]
    store = store_for(write_settings(tmp_path, entry("openai", base="https://vpce.example.com/v1")))
    with pytest.raises(ChatGPTError):
        run(ask(ApiBackend(store, http=Recorder()), request_for(store)))
    assert seen["url"] == "https://vpce.example.com/v1/responses" and "/us-east-1/bedrock/aws4_request" in seen["headers"]["Authorization"]


def test_a_busy_server_and_a_dead_connection_stay_hiccups_when_the_sentence_is_ours(tmp_path):
    import socket

    from desktop.core.chatgpt.errors import is_transient

    busy = failing(tmp_path, http_error(429, {"message": "slow"}, {"x-amzn-ErrorType": "ThrottlingException", "retry-after": "7"}))
    assert busy.code == "api" and busy.retry_after == 7.0 and is_transient(busy)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    store = store_for(write_settings(tmp_path, entry(base=f"http://127.0.0.1:{port}")))
    with pytest.raises(ChatGPTError) as dead:
        run(ask(ApiBackend(store), request_for(store)))
    assert "连不上" in dead.value.message and dead.value.transient is True and is_transient(dead.value)
    refused = failing(tmp_path, http_error(403, {"message": "no"}, {"x-amzn-ErrorType": "AccessDeniedException"}))
    assert not is_transient(refused), "a refusal stays a refusal"
