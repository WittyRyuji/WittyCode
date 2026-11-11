"""``core.chatgpt.responses`` and ``models``: the request we build, the stream we read (from the loopback fake service
over real HTTP), the history items we keep, the model list."""

from __future__ import annotations

import asyncio
import urllib.request

import pytest

from desktop.core.chatgpt.errors import ChatGPTError, friendly_failure
from desktop.core.chatgpt.http import HttpClient
from desktop.core.chatgpt.models import FALLBACK_MODELS, models_from_result
from desktop.core.chatgpt.responses import (
    CallBegun,
    CallDone,
    Finished,
    ItemDone,
    ReasoningDelta,
    ReasoningDone,
    Shape,
    TextDelta,
    TextDone,
    Usage,
    build_request,
    call_output,
    declare_tools,
    history_item,
    read_events,
    user_message,
)
from tests.fake_responses import FakeResponses, call, calls, cut, fail, http_error, say, usage

# Fields the plan route refuses (docs/chatgpt-plan-harness.md section 2): a request must never carry one.
REFUSED = {
    "background", "conversation", "max_output_tokens", "max_tool_calls", "metadata", "moderation", "multi_agent", "prompt",
    "prompt_cache_retention", "safety_identifier", "temperature", "top_logprobs", "top_p", "truncation", "user", "previous_response_id",
}
TOOLS = [{"name": "read_file", "description": "Read a file.", "parameters": {"type": "object", "properties": {"file_path": {"type": "string"}}}}]


def client() -> HttpClient:
    return HttpClient(urllib.request.build_opener(urllib.request.ProxyHandler({})))


def read(fake: FakeResponses, request: dict | None = None) -> list:
    async def go() -> list:
        lines = client().stream_lines("POST", fake.base_url + "/responses", headers={"Authorization": "Bearer t"}, json_body=request or {})
        return [event async for event in read_events(lines)]

    return asyncio.run(go())


def test_a_text_answer_streams_deltas_then_the_whole_text():
    with FakeResponses() as fake:
        fake.script(say("Hello there", used=usage(100, 20, cached=40, reasoning=5)))
        events = read(fake)
    deltas = [e for e in events if isinstance(e, TextDelta)]
    assert "".join(e.text for e in deltas) == "Hello there" and len(deltas) == 2
    assert [e.text for e in events if isinstance(e, TextDone)] == ["Hello there"]
    done = next(e for e in events if isinstance(e, ItemDone))
    assert done.item["type"] == "message"
    finished = events[-1]
    assert isinstance(finished, Finished) and finished.incomplete == ""
    assert finished.usage == Usage(100, 40, 20, 5)
    tokens = finished.usage.tokens()
    assert (tokens.input_tokens, tokens.output_tokens, tokens.cache_read_tokens, tokens.cache_creation_tokens) == (60, 20, 40, None)
    assert finished.usage.context == 120


def test_reasoning_summary_is_streamed_and_finished():
    with FakeResponses() as fake:
        fake.script(say("Done.", thinking="Looking at the file first."))
        events = read(fake)
    assert [e.text for e in events if isinstance(e, ReasoningDelta)] == ["Looking at the file first."]
    assert [e.text for e in events if isinstance(e, ReasoningDone)] == ["Looking at the file first."]
    reasoning = next(e.item for e in events if isinstance(e, ItemDone) and e.item["type"] == "reasoning")
    assert reasoning["encrypted_content"] == "enc-1"


def test_a_tool_call_arrives_with_its_arguments_text():
    with FakeResponses() as fake:
        fake.script(call("read_file", {"file_path": "a.txt"}, call_id="call_9", item_id="fc_9", text="Reading."))
        events = read(fake)
    begun = next(e for e in events if isinstance(e, CallBegun))
    assert (begun.call_id, begun.name, begun.item_id) == ("call_9", "read_file", "fc_9")
    done = next(e for e in events if isinstance(e, CallDone))
    assert done.arguments == '{"file_path": "a.txt"}' and done.call_id == "call_9"


def test_two_calls_in_one_answer_come_in_order():
    with FakeResponses() as fake:
        fake.script(calls([("a", "{}", "c1", "f1"), ("b", "{}", "c2", "f2")]))
        events = read(fake)
    assert [e.name for e in events if isinstance(e, CallDone)] == ["a", "b"]


def test_a_failed_response_raises_with_the_servers_code_and_param():
    with FakeResponses() as fake:
        fake.script(fail("subscription_sharing_unsupported_capability", "no", param="tools[0]"))
        with pytest.raises(ChatGPTError) as caught:
            read(fake)
    assert caught.value.code == "subscription_sharing_unsupported_capability"
    assert "tools[0]" in caught.value.message


def test_a_usage_limit_failure_reaches_the_user_in_words():
    with FakeResponses() as fake:
        fake.script(fail("subscription_sharing_usage_limit_exceeded"))
        with pytest.raises(ChatGPTError) as caught:
            read(fake)
    assert "用量" in friendly_failure(caught.value)


def test_an_http_refusal_before_the_stream_keeps_status_and_request_id():
    with FakeResponses() as fake:
        fake.script(http_error(403, {"detail": "region"}, {"x-request-id": "req_77"}))
        with pytest.raises(ChatGPTError) as caught:
            read(fake)
    assert (caught.value.status, caught.value.request_id, caught.value.message) == (403, "req_77", "region")


def test_a_stream_that_stops_early_is_an_error_not_a_silent_end():
    with FakeResponses() as fake:
        fake.script(say("half")[:-1])
        with pytest.raises(ChatGPTError) as caught:
            read(fake)
    assert caught.value.code == "stream"


def test_an_incomplete_response_says_why():
    with FakeResponses() as fake:
        fake.script(cut("partial"))
        events = read(fake)
    assert isinstance(events[-1], Finished) and events[-1].incomplete == "max_output_tokens"


def test_the_request_has_the_fields_the_route_requires_and_none_it_refuses():
    request = build_request(model="gpt-6.1-sol", instructions="Be brief.", items=[user_message("hi")], tools=TOOLS, reasoning="high", conversation="c1")
    assert request["store"] is False and request["stream"] is True
    assert request["instructions"] == "Be brief." and request["model"] == "gpt-6.1-sol"
    assert request["input"][0]["content"][0] == {"type": "input_text", "text": "hi"}
    assert request["reasoning"] == {"effort": "high", "summary": "auto"}
    assert request["include"] == ["reasoning.encrypted_content"]
    assert not REFUSED & set(request)
    assert all(item.get("role") != "system" for item in request["input"])


def test_a_request_with_tools_lets_the_model_choose_and_follows_the_shape_for_parallel_calls():
    on = build_request(model="m", instructions="", items=[], tools=TOOLS, reasoning=None)
    off = build_request(model="m", instructions="", items=[], tools=TOOLS, reasoning=None, shape=Shape(parallel_tool_calls=False))
    assert (on["tool_choice"], on["parallel_tool_calls"]) == ("auto", True)
    assert (off["tool_choice"], off["parallel_tool_calls"]) == ("auto", False)


def test_a_reasoning_summary_that_came_only_as_deltas_is_the_text_of_the_finished_item():
    events = [
        {"type": "response.created", "response": {"id": "r", "status": "in_progress"}},
        {"type": "response.output_item.added", "item": {"type": "reasoning", "id": "rs_1", "summary": []}},
        {"type": "response.reasoning_summary_text.delta", "item_id": "rs_1", "summary_index": 0, "delta": "First "},
        {"type": "response.reasoning_summary_text.delta", "item_id": "rs_1", "summary_index": 0, "delta": "part."},
        {"type": "response.reasoning_summary_text.delta", "item_id": "rs_1", "summary_index": 1, "delta": "Second."},
        {"type": "response.output_item.done", "item": {"type": "reasoning", "id": "rs_1", "summary": [], "encrypted_content": "enc"}},
        {"type": "response.completed", "response": {"id": "r", "status": "completed", "usage": usage()}},
    ]
    with FakeResponses() as fake:
        fake.script(events)
        done = [e for e in read(fake) if isinstance(e, ReasoningDone)]
    assert [e.text for e in done] == ["First part.\n\nSecond."]


def test_tools_are_flat_functions_by_default_and_a_namespace_on_request():
    flat = declare_tools(TOOLS, Shape())
    assert flat == [{"type": "function", "name": "read_file", "description": "Read a file.", "parameters": TOOLS[0]["parameters"]}]
    grouped = declare_tools(TOOLS, Shape(flat_tools=False))
    assert grouped[0]["type"] == "namespace" and grouped[0]["tools"] == flat


def test_a_request_without_tools_or_reasoning_has_neither():
    request = build_request(model="m", instructions="", items=[], tools=[], reasoning=None, shape=Shape(reasoning_summary=None, encrypted_reasoning=False))
    assert not {"tools", "tool_choice", "parallel_tool_calls", "reasoning", "include", "prompt_cache_key"} & set(request)


def test_a_cache_key_is_sent_only_when_asked_for():
    on = build_request(model="m", instructions="", items=[], tools=[], reasoning=None, shape=Shape(cache_key=True), conversation="abc")
    off = build_request(model="m", instructions="", items=[], tools=[], reasoning=None, conversation="abc")
    assert on["prompt_cache_key"] == "abc" and "prompt_cache_key" not in off


def test_kept_items_lose_their_ids_because_nothing_is_stored_server_side():
    message = history_item({"type": "message", "id": "msg_1", "role": "assistant", "status": "completed", "content": [{"type": "output_text", "text": "x"}]})
    assert message == {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "x"}]}
    function = history_item({"type": "function_call", "id": "fc_1", "call_id": "c1", "name": "n", "arguments": "{}", "status": "completed"})
    assert function == {"type": "function_call", "call_id": "c1", "name": "n", "arguments": "{}"}
    reasoning = history_item({"type": "reasoning", "id": "rs_1", "summary": [], "encrypted_content": "blob", "status": "completed"})
    assert reasoning == {"type": "reasoning", "summary": [], "encrypted_content": "blob"}


def test_reasoning_without_its_encrypted_content_cannot_be_sent_back_and_is_dropped():
    assert history_item({"type": "reasoning", "id": "rs_1", "summary": [{"text": "x"}]}) is None
    assert history_item({"type": "web_search_call", "id": "w"}) is None


def test_item_builders():
    assert call_output("c1", "ok") == {"type": "function_call_output", "call_id": "c1", "output": "ok"}
    assert user_message("hi", ["data:image/png;base64,AAA"])["content"][1] == {"type": "input_image", "image_url": "data:image/png;base64,AAA"}


def test_the_documented_model_list_shows_only_listed_models_with_their_figures():
    with FakeResponses() as fake:
        async def get() -> object:
            response = await client().request("GET", fake.base_url + "/models", headers={"Authorization": "Bearer t"})
            return response.json()

        models = models_from_result(asyncio.run(get()))
    assert [m.id for m in models] == ["gpt-6.1-sol", "gpt-6-luna"]
    sol = models[0]
    assert (sol.display_name, sol.efforts, sol.default_effort, sol.context_window) == ("GPT-6.1-Sol", ("low", "medium", "high"), "medium", 400000)
    assert models[1].context_window is None


def test_models_in_codex_style_spelling_are_read_too_and_hidden_ones_skipped():
    result = {"data": [
        {"id": "a", "displayName": "A", "supportedReasoningEfforts": [{"reasoningEffort": "low"}, "high"], "defaultReasoningEffort": "low", "contextWindow": 1000, "inputModalities": ["text"]},
        {"id": "b", "hidden": True},
        {"nonsense": 1},
    ]}
    models = models_from_result(result)
    assert [m.id for m in models] == ["a"]
    assert models[0].efforts == ("low", "high") and models[0].supports_images is False and models[0].context_window == 1000


def test_an_unreadable_list_is_empty_and_a_fallback_exists():
    assert models_from_result("nonsense") == [] and models_from_result({"models": "x"}) == []
    assert len(FALLBACK_MODELS) >= 2 and all(m.id for m in FALLBACK_MODELS)
