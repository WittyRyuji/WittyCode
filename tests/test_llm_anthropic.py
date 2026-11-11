"""Anthropic's Messages API (``core/llm/anthropic``): the request built from the loop's items, and the stream read back into its events."""

from __future__ import annotations

import pytest

from desktop.core.chatgpt.errors import ChatGPTError
from desktop.core.chatgpt.responses import (
    CallBegun,
    CallDone,
    ReasoningDelta,
    ReasoningDone,
    TextDelta,
    TextDone,
    build_request,
    call_output,
    user_message,
)
from desktop.core.llm.anthropic import anthropic_body, anthropic_messages, read_anthropic, thinking_for
from desktop.core.llm.wire import ANTHROPIC, Route
from tests.llm_support import collect, kinds, lines_of

ROUTE = Route(ANTHROPIC, "https://api.anthropic.com", "claude-x", max_output=16_000)
ORIGIN = ROUTE.origin


def assistant_text(text: str) -> dict:
    return {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": text}]}


def function_call(call_id: str, name: str, arguments: str = "{}") -> dict:
    return {"type": "function_call", "call_id": call_id, "name": name, "arguments": arguments}


def thinking(text: str, signature: str = "sig", origin: str = ORIGIN) -> dict:
    return {"type": "reasoning", "summary": [], "wc_reasoning": text, "wc_signature": signature, "wc_origin": origin}


def read(events) -> list:
    return collect(read_anthropic(lines_of(events), ORIGIN))


def start(input_tokens: int = 100, **usage) -> tuple:
    return ("message_start", {"type": "message_start", "message": {"id": "msg_1", "usage": {"input_tokens": input_tokens, "output_tokens": 1, **usage}}})


def block(index: int, content_block: dict) -> tuple:
    return ("content_block_start", {"type": "content_block_start", "index": index, "content_block": content_block})


def delta(index: int, **fields) -> tuple:
    return ("content_block_delta", {"type": "content_block_delta", "index": index, "delta": fields})


def stop(index: int) -> tuple:
    return ("content_block_stop", {"type": "content_block_stop", "index": index})


def end(reason: str = "end_turn", output_tokens: int = 20) -> list:
    return [
        ("message_delta", {"type": "message_delta", "delta": {"stop_reason": reason}, "usage": {"output_tokens": output_tokens}}),
        ("message_stop", {"type": "message_stop"}),
    ]


# ------------------------------------------------------------------------------------------------ the request


def test_results_go_in_the_user_message_that_follows_the_calls_and_come_before_the_words_of_the_next_message():
    items = [
        user_message("read both"),
        assistant_text("Reading."),
        function_call("c1", "read_file", '{"file_path": "a.txt"}'),
        function_call("c2", "read_file", '{"file_path": "b.txt"}'),
        call_output("c1", "alpha"),
        call_output("c2", "beta"),
        user_message("and then summarise"),
    ]
    assert anthropic_messages(items, ORIGIN) == [
        {"role": "user", "content": [{"type": "text", "text": "read both"}]},
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "Reading."},
                {"type": "tool_use", "id": "c1", "name": "read_file", "input": {"file_path": "a.txt"}},
                {"type": "tool_use", "id": "c2", "name": "read_file", "input": {"file_path": "b.txt"}},
            ],
        },
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "c1", "content": "alpha"},
                {"type": "tool_result", "tool_use_id": "c2", "content": "beta"},
                {"type": "text", "text": "and then summarise"},
            ],
        },
    ]


def test_arguments_that_are_not_an_object_become_an_empty_one_and_blank_words_are_left_out():
    messages = anthropic_messages([user_message("go"), assistant_text("  "), function_call("c1", "ls", "not json"), function_call("c2", "pwd", "")], ORIGIN)
    assert messages[1]["content"] == [{"type": "tool_use", "id": "c1", "name": "ls", "input": {}}, {"type": "tool_use", "id": "c2", "name": "pwd", "input": {}}]
    valid_but_not_an_object = anthropic_messages([user_message("go"), function_call("c1", "ls", "[1, 2]"), function_call("c2", "pwd", "42"), function_call("c3", "x", "null")], ORIGIN)
    assert [block["input"] for block in valid_but_not_an_object[1]["content"]] == [{}, {}, {}], "the API takes an object and nothing else"


def test_pictures_become_image_blocks():
    messages = anthropic_messages([user_message("what", ["data:image/png;base64,AAAA", "https://example.com/a.png", "file:///no"])], ORIGIN)
    assert messages[0]["content"] == [
        {"type": "text", "text": "what"},
        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AAAA"}},
        {"type": "image", "source": {"type": "url", "url": "https://example.com/a.png"}},
    ]
    jpeg = anthropic_messages([user_message("", ["data:image/jpeg;base64,BBBB"])], ORIGIN)
    assert jpeg[0]["content"][-1]["source"] == {"type": "base64", "media_type": "image/jpeg", "data": "BBBB"}, "the picture keeps its own type"


def test_signed_thinking_goes_back_in_front_of_the_answer_and_nothing_else_does():
    items = [
        user_message("go"),
        thinking("I should list."),
        {"type": "reasoning", "summary": [], "wc_data": "REDACTED", "wc_origin": ORIGIN},
        thinking("another origin", origin="api.other.example"),
        thinking("unsigned", signature=""),
        {"type": "reasoning", "summary": [], "encrypted_content": "gAAA"},
        function_call("c1", "ls"),
    ]
    assert anthropic_messages(items, ORIGIN)[1]["content"] == [
        {"type": "thinking", "thinking": "I should list.", "signature": "sig"},
        {"type": "redacted_thinking", "data": "REDACTED"},
        {"type": "tool_use", "id": "c1", "name": "ls", "input": {}},
    ]


def test_the_body_caches_the_prefix_declares_the_tools_and_sets_the_thinking_budget_under_the_room():
    request = build_request(
        model="m", instructions="be brief", items=[user_message("hi")], reasoning="medium",
        tools=[{"name": "ls", "description": "List.", "parameters": {"type": "object", "properties": {}}}],
    )
    body = anthropic_body(request, ROUTE)
    assert body["model"] == "claude-x" and body["max_tokens"] == 16_000 and body["stream"] is True
    assert body["system"] == [{"type": "text", "text": "be brief", "cache_control": {"type": "ephemeral"}}]
    assert body["messages"] == [{"role": "user", "content": [{"type": "text", "text": "hi", "cache_control": {"type": "ephemeral"}}]}]
    assert body["tools"] == [{"name": "ls", "description": "List.", "input_schema": {"type": "object", "properties": {}}}]
    assert body["tool_choice"] == {"type": "auto"} and body["thinking"] == {"type": "enabled", "budget_tokens": 8192}


def test_without_a_level_there_is_no_thinking_and_the_users_arguments_have_the_last_word():
    route = Route(ANTHROPIC, "https://api.anthropic.com", "claude-x", extra_args={"temperature": 0.3, "max_tokens": 99})
    body = anthropic_body(build_request(model="m", instructions="", items=[user_message("hi")], tools=[], reasoning=None), route)
    assert "thinking" not in body and "system" not in body and "tools" not in body
    assert body["temperature"] == 0.3 and body["max_tokens"] == 99


def test_the_thinking_budget_always_leaves_room_for_the_answer():
    assert thinking_for("high", 100_000) == {"type": "enabled", "budget_tokens": 24_000}
    assert thinking_for("high", 8_000) == {"type": "enabled", "budget_tokens": 8_000 - 1024}
    assert thinking_for("low", 2_000) is None, "less than the API's smallest budget would be left"
    assert thinking_for(None, 100_000) is None and thinking_for("", 100_000) is None


# ------------------------------------------------------------------------------------------------ the stream


def test_words_arrive_in_pieces_then_the_message_and_the_usage_counts_what_the_cache_served_and_wrote():
    events = read([
        start(50, cache_read_input_tokens=1000, cache_creation_input_tokens=200),
        block(0, {"type": "text", "text": ""}),
        delta(0, type="text_delta", text="Hel"),
        delta(0, type="text_delta", text="lo."),
        stop(0),
        *end("end_turn", 12),
    ])
    assert kinds(events) == ["TextDelta", "TextDelta", "TextDone", "ItemDone", "Finished"]
    assert events[2] == TextDone("msg_0", "Hello.")
    assert events[3].item == {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "Hello."}]}
    done = events[4]
    assert (done.incomplete, done.response_id) == ("", "msg_1")
    assert (done.usage.input_tokens, done.usage.cached_tokens, done.usage.cache_write_tokens, done.usage.output_tokens) == (1250, 1000, 200, 12)
    tokens = done.usage.tokens()
    assert (tokens.input_tokens, tokens.cache_read_tokens, tokens.cache_creation_tokens) == (50, 1000, 200)


def test_thinking_keeps_its_signature_and_a_tool_call_arrives_as_json_in_pieces():
    events = read([
        start(),
        block(0, {"type": "thinking", "thinking": ""}),
        delta(0, type="thinking_delta", thinking="Let me "),
        delta(0, type="thinking_delta", thinking="look."),
        delta(0, type="signature_delta", signature="SIG=="),
        stop(0),
        block(1, {"type": "tool_use", "id": "toolu_9", "name": "read_file", "input": {}}),
        delta(1, type="input_json_delta", partial_json='{"file_'),
        delta(1, type="input_json_delta", partial_json='path": "a.txt"}'),
        stop(1),
        *end("tool_use"),
    ])
    assert kinds(events) == ["ReasoningDelta", "ReasoningDelta", "ReasoningDone", "ItemDone", "CallBegun", "CallDone", "ItemDone", "Finished"]
    assert events[2] == ReasoningDone("rs_0", "Let me look.")
    assert events[3].item == {"type": "reasoning", "summary": [{"type": "summary_text", "text": "Let me look."}], "wc_reasoning": "Let me look.", "wc_signature": "SIG==", "wc_origin": ORIGIN}
    assert events[4] == CallBegun("toolu_1", "toolu_9", "read_file")
    assert events[5] == CallDone("toolu_1", "toolu_9", "read_file", '{"file_path": "a.txt"}')
    assert events[6].item == {"type": "function_call", "call_id": "toolu_9", "name": "read_file", "arguments": '{"file_path": "a.txt"}'}


def test_a_call_without_arguments_has_an_empty_object_and_a_redacted_block_is_kept():
    events = read([start(), block(0, {"type": "redacted_thinking", "data": "XYZ"}), stop(0), block(1, {"type": "tool_use", "id": "t", "name": "ls", "input": {}}), stop(1), *end("tool_use")])
    assert events[0].item == {"type": "reasoning", "summary": [], "wc_data": "XYZ", "wc_origin": ORIGIN}
    assert [e for e in events if isinstance(e, CallDone)][0].arguments == "{}"


def test_the_reasons_an_answer_stops_short():
    for reason, expected in [("max_tokens", "max_output_tokens"), ("refusal", "content_filter"), ("end_turn", ""), ("tool_use", "")]:
        assert read([start(), *end(reason)])[-1].incomplete == expected


def test_an_error_event_is_raised_and_a_stream_that_stops_early_is_an_error():
    with pytest.raises(ChatGPTError, match="Overloaded") as error:
        read([start(), ("error", {"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}})])
    assert error.value.code == "overloaded_error"
    with pytest.raises(ChatGPTError) as cut:
        read([start(), block(0, {"type": "text", "text": ""}), delta(0, type="text_delta", text="x")])
    assert cut.value.code == "stream"


def test_pings_and_unknown_events_are_ignored():
    events = read([start(), ("ping", {"type": "ping"}), ("something_new", {"type": "something_new"}), *end()])
    assert kinds(events) == ["Finished"]
    assert ReasoningDelta("a", "b").text == "b" and TextDelta("a", "b").text == "b"
