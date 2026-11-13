"""Amazon Bedrock Runtime (``core/llm/bedrock``): the Converse request built from the loop's items, the event stream read back into events."""

from __future__ import annotations

import asyncio
import base64
import json

import pytest

from desktop.core.chatgpt.errors import ChatGPTError
from desktop.core.chatgpt.responses import (
    CallBegun,
    CallDone,
    Finished,
    ItemDone,
    ReasoningDelta,
    ReasoningDone,
    TextDelta,
    TextDone,
    call_output,
    user_message,
)
from desktop.core.chatgpt.responses import build_request as _build_request
from desktop.core.llm import bedrock
from desktop.core.llm.anthropic import read_anthropic
from desktop.core.llm.wire import RESPONSES, Route
from tests.llm_support import collect, event_frame, kinds

def build_request(**fields):
    fields.setdefault("reasoning", None)
    return _build_request(**fields)


CLAUDE = "anthropic.claude-sonnet-4-5-20250929-v1:0"
ROUTE = Route("converse", "https://bedrock-runtime.us-east-1.amazonaws.com", CLAUDE)
ORIGIN = ROUTE.origin


def assistant_text(text: str) -> dict:
    return {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": text}]}


def function_call(call_id: str, name: str, arguments: str = "{}") -> dict:
    return {"type": "function_call", "call_id": call_id, "name": name, "arguments": arguments}


def thought(text: str, signature: str = "sig", origin: str = ORIGIN) -> dict:
    return {"type": "reasoning", "summary": [], "wc_reasoning": text, "wc_signature": signature, "wc_origin": origin}


async def pieces(data: list[bytes]):
    for piece in data:
        yield piece


def read(*frames: bytes, whole: bool = False) -> list:
    data = [b"".join(frames)] if whole else [bytes([byte]) for byte in b"".join(frames)]  # (by default one byte at a time: a frame is cut everywhere)
    return collect(bedrock.read_converse(pieces(data), ORIGIN))


def event(name: str, **payload) -> bytes:
    return event_frame(name, payload)


def start_text(index: int = 0) -> bytes:
    return event("contentBlockDelta", contentBlockIndex=index, delta={"text": ""})


def ending(stop: str = "end_turn", usage: dict | None = None) -> list[bytes]:
    return [event("messageStop", stopReason=stop), event("metadata", usage=usage or {"inputTokens": 100, "outputTokens": 20, "totalTokens": 120}, metrics={"latencyMs": 5})]


# ---------------------------------------------------------------------------------------------- the request


def test_results_go_in_the_user_message_that_follows_the_calls_and_come_before_the_words():
    items = [
        user_message("read a and b"),
        assistant_text("Reading both."),
        function_call("c1", "read_file", '{"file_path":"a"}'),
        function_call("c2", "read_file", '{"file_path":"b"}'),
        call_output("c1", "alpha"),
        call_output("c2", "beta"),
        user_message("now summarise"),
    ]
    messages = bedrock.converse_messages(items, ORIGIN)
    assert [m["role"] for m in messages] == ["user", "assistant", "user"]
    assert messages[1]["content"] == [
        {"text": "Reading both."},
        {"toolUse": {"toolUseId": "c1", "name": "read_file", "input": {"file_path": "a"}}},
        {"toolUse": {"toolUseId": "c2", "name": "read_file", "input": {"file_path": "b"}}},
    ]
    assert messages[2]["content"] == [
        {"toolResult": {"toolUseId": "c1", "content": [{"text": "alpha"}]}},
        {"toolResult": {"toolUseId": "c2", "content": [{"text": "beta"}]}},
        {"text": "now summarise"},
    ]


def test_nothing_empty_is_sent_and_a_call_with_bad_arguments_goes_with_none():
    items = [user_message("x"), assistant_text("   "), function_call("c1", "t", "not json"), call_output("c1", "  ")]
    messages = bedrock.converse_messages(items, ORIGIN)
    assert messages[1]["content"] == [{"toolUse": {"toolUseId": "c1", "name": "t", "input": {}}}]
    assert messages[2]["content"] == [{"toolResult": {"toolUseId": "c1", "content": [{"text": bedrock.NO_OUTPUT}]}}]


def test_pictures_go_as_bytes_in_the_formats_converse_knows_and_others_are_left_out():
    png = "data:image/png;base64,AAAA"
    messages = bedrock.converse_messages(
        [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": "look"}, {"type": "input_image", "image_url": png}, {"type": "input_image", "image_url": "data:image/bmp;base64,BBBB"}, {"type": "input_image", "image_url": "https://x/y.png"}]}],
        ORIGIN,
    )
    assert messages[0]["content"] == [{"text": "look"}, {"image": {"format": "png", "source": {"bytes": "AAAA"}}}]


def test_thinking_comes_back_to_the_same_origin_only_and_a_redacted_block_as_it_was():
    items = [
        user_message("go"),
        thought("I should read", "SIG1"),
        {"type": "reasoning", "summary": [], "wc_data": "REDACTED==", "wc_origin": ORIGIN},
        thought("from elsewhere", "SIG2", origin="other.example"),
        function_call("c1", "t"),
        call_output("c1", "ok"),
    ]
    assistant = bedrock.converse_messages(items, ORIGIN)[1]["content"]
    assert assistant[:2] == [
        {"reasoningContent": {"reasoningText": {"text": "I should read", "signature": "SIG1"}}},
        {"reasoningContent": {"redactedContent": "REDACTED=="}},
    ]
    assert len(assistant) == 3 and "toolUse" in assistant[2]


def test_the_body_has_the_system_prompt_the_tools_and_no_room_unless_the_user_gave_one():
    tools = [{"type": "function", "name": "read_file", "description": "Read a file.", "parameters": {"type": "object", "properties": {"p": {"type": "string"}}}}, {"type": "function", "name": "noop"}]
    body = bedrock.converse_body(build_request(model="m", instructions="Be brief.", items=[user_message("hi")], tools=tools), Route("converse", "https://x", "meta.llama3-1-70b-instruct-v1:0"))
    assert body["system"] == [{"text": "Be brief."}] and "inferenceConfig" not in body and "additionalModelRequestFields" not in body
    assert body["toolConfig"]["toolChoice"] == {"auto": {}}
    first, second = (spec["toolSpec"] for spec in body["toolConfig"]["tools"])
    assert first["name"] == "read_file" and first["description"] == "Read a file." and first["inputSchema"] == {"json": tools[0]["parameters"]}
    assert "description" not in second and second["inputSchema"] == {"json": {"type": "object", "properties": {}}}
    limited = bedrock.converse_body(build_request(model="m", instructions="", items=[user_message("hi")], tools=[]), Route("converse", "https://x", "meta.llama3-1-70b-instruct-v1:0", max_output=4096))
    assert limited["inferenceConfig"] == {"maxTokens": 4096} and "system" not in limited and "toolConfig" not in limited


@pytest.mark.parametrize(
    ("model", "caches"),
    [
        ("anthropic.claude-sonnet-4-5-20250929-v1:0", True),
        ("us.anthropic.claude-3-7-sonnet-20250219-v1:0", True),
        ("anthropic.claude-3-5-haiku-20241022-v1:0", True),
        ("global.anthropic.claude-opus-5-5-v1:0", True),
        ("amazon.nova-pro-v1:0", True),
        ("anthropic.claude-3-haiku-20240307-v1:0", False),
        ("anthropic.claude-3-sonnet-20240229-v1:0", False),
        ("meta.llama3-1-70b-instruct-v1:0", False),
        ("mistral.mistral-large-2407-v1:0", False),
    ],
)
def test_cache_points_go_only_to_the_models_that_take_them(model, caches):
    body = bedrock.converse_body(build_request(model="m", instructions="sys", items=[user_message("hi")], tools=[]), Route("converse", "https://x", model))
    point = {"cachePoint": {"type": "default"}}
    assert (point in body["system"]) is caches and (point in body["messages"][-1]["content"]) is caches


def test_thinking_is_asked_of_claude_with_room_for_the_answer_and_not_of_other_models():
    request = build_request(model="m", instructions="", items=[user_message("hi")], tools=[], reasoning="medium")
    claude = bedrock.converse_body(request, ROUTE)
    assert claude["additionalModelRequestFields"] == {"thinking": {"type": "enabled", "budget_tokens": 8192}}
    assert claude["inferenceConfig"]["maxTokens"] > 8192, "the answer must have room beyond the thought"
    small = bedrock.converse_body(request, Route("converse", "https://x", CLAUDE, max_output=2000))
    assert "additionalModelRequestFields" not in small, "no room for 1 024 tokens of thought and an answer"
    llama = bedrock.converse_body(request, Route("converse", "https://x", "meta.llama3-1-70b-instruct-v1:0"))
    assert "additionalModelRequestFields" not in llama
    own = bedrock.converse_body(build_request(model="m", instructions="", items=[user_message("hi")], tools=[]), Route("converse", "https://x", CLAUDE, max_output=20_000, thinking=5000))
    assert own["additionalModelRequestFields"]["thinking"]["budget_tokens"] == 5000, "an entry that turned thinking on has its own budget when no level is chosen"
    chosen = bedrock.converse_body(request, Route("converse", "https://x", CLAUDE, max_output=20_000, thinking=5000))
    assert chosen["additionalModelRequestFields"]["thinking"]["budget_tokens"] == 8192, "the level the user chose wins"


def test_request_metadata_and_extra_args_are_merged_into_the_body():
    route = Route("converse", "https://x", "meta.llama3-1-70b-instruct-v1:0", extra_args={"inferenceConfig": {"temperature": 0.2}, "guardrailConfig": {"guardrailIdentifier": "g"}}, max_output=1000)
    body = bedrock.converse_body(build_request(model="m", instructions="", items=[user_message("hi")], tools=[]), route, {"team": "a"})
    assert body["requestMetadata"] == {"team": "a"} and body["guardrailConfig"] == {"guardrailIdentifier": "g"}
    assert body["inferenceConfig"] == {"maxTokens": 1000, "temperature": 0.2}, "an object is merged into ours"


def test_the_invoke_body_is_anthropics_without_the_model_and_the_stream_flag():
    body = bedrock.invoke_body(build_request(model="m", instructions="sys", items=[user_message("hi")], tools=[]), Route("anthropic", "https://x", CLAUDE, max_output=4000))
    assert body["anthropic_version"] == "bedrock-2023-05-31" and "model" not in body and "stream" not in body
    assert body["max_tokens"] == 4000 and body["messages"][0]["role"] == "user"


def test_the_urls_encode_the_model_id_once():
    base = "https://bedrock-runtime.us-west-2.amazonaws.com"
    assert bedrock.endpoint("us-west-2") == base and bedrock.endpoint("us-west-2", "https://vpce-1.example/") == "https://vpce-1.example"
    assert bedrock.model_url(base + "/", CLAUDE, "converse-stream") == f"{base}/model/anthropic.claude-sonnet-4-5-20250929-v1%3A0/converse-stream"
    assert bedrock.model_url(base, "arn:aws:bedrock:us-east-1:1:inference-profile/us.x", "converse-stream").endswith("/model/arn%3Aaws%3Abedrock%3Aus-east-1%3A1%3Ainference-profile%2Fus.x/converse-stream")


# ---------------------------------------------------------------------------------------------- the event stream


def test_a_frame_is_read_whole_and_a_wrong_checksum_is_refused():
    frame = event_frame("messageStart", {"role": "assistant"})
    parsed = bedrock.parse_frame(frame)
    assert (parsed.kind, parsed.name) == ("event", "messageStart") and json.loads(parsed.payload) == {"role": "assistant"}
    for at in (5, len(frame) - 2, 20):  # the prelude, the message CRC, the headers
        broken = bytearray(frame)
        broken[at] ^= 0xFF
        with pytest.raises(ChatGPTError, match="损坏"):
            bedrock.parse_frame(bytes(broken))


def test_the_reader_assembles_frames_from_pieces_cut_anywhere_and_refuses_a_cut_stream():
    one, two = event_frame("a", {"n": 1}), event_frame("b", {"n": 2})

    async def go(data):
        return [(f.name, json.loads(f.payload)) async for f in bedrock.frames(pieces(data))]

    assert asyncio.run(go([one + two])) == [("a", {"n": 1}), ("b", {"n": 2})]
    cuts = [(one + two)[i : i + 7] for i in range(0, len(one + two), 7)]
    assert asyncio.run(go(cuts)) == [("a", {"n": 1}), ("b", {"n": 2})]
    with pytest.raises(ChatGPTError, match="ended before"):
        asyncio.run(go([one[:-3]]))
    with pytest.raises(ChatGPTError, match="长度"):
        asyncio.run(go([b"\xff\xff\xff\xff" + b"\x00" * 12]))


def test_headers_of_every_type_are_skipped_over():
    import struct
    import zlib

    headers = b"".join(
        [
            bytes([4]) + b"flag" + b"\x00",  # true
            bytes([5]) + b"flag2" + b"\x01",  # false
            bytes([1]) + b"b" + b"\x02" + b"\x05",
            bytes([1]) + b"s" + b"\x03" + struct.pack(">h", -2),
            bytes([1]) + b"i" + b"\x04" + struct.pack(">i", 70000),
            bytes([1]) + b"l" + b"\x05" + struct.pack(">q", 1 << 40),
            bytes([1]) + b"y" + b"\x06" + struct.pack(">H", 2) + b"\x01\x02",
            bytes([1]) + b"t" + b"\x08" + struct.pack(">q", 1234),
            bytes([1]) + b"u" + b"\x09" + bytes(range(16)),
            bytes([13]) + b":message-type" + b"\x07" + struct.pack(">H", 5) + b"event",
        ]
    )
    body = b"{}"
    prelude = struct.pack(">II", 16 + len(headers) + len(body), len(headers))
    message = prelude + struct.pack(">I", zlib.crc32(prelude)) + headers + body
    frame = bedrock.parse_frame(message + struct.pack(">I", zlib.crc32(message)))
    assert frame.kind == "event" and frame.headers["flag"] is True and frame.headers["flag2"] is False
    assert frame.headers["s"] == -2 and frame.headers["i"] == 70000 and frame.headers["l"] == 1 << 40 and frame.headers["y"] == b"\x01\x02"


# ---------------------------------------------------------------------------------------------- the stream: Converse


def test_text_arrives_as_deltas_then_one_message_with_the_usage_counted_in():
    events = read(
        event("messageStart", role="assistant"),
        event("contentBlockDelta", contentBlockIndex=0, delta={"text": "Hel"}),
        event("contentBlockDelta", contentBlockIndex=0, delta={"text": "lo"}),
        event("contentBlockStop", contentBlockIndex=0),
        *ending(usage={"inputTokens": 100, "outputTokens": 20, "cacheReadInputTokens": 40, "cacheWriteInputTokens": 10}),
    )
    assert kinds(events) == ["TextDelta", "TextDelta", "TextDone", "ItemDone", "Finished"]
    assert [e.text for e in events if isinstance(e, TextDelta)] == ["Hel", "lo"]
    done = next(e for e in events if isinstance(e, TextDone))
    assert done.text == "Hello"
    assert next(e for e in events if isinstance(e, ItemDone)).item == {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "Hello"}]}
    usage = events[-1].usage
    assert (usage.input_tokens, usage.cached_tokens, usage.output_tokens, usage.cache_write_tokens) == (150, 40, 20, 10)


def test_a_tool_call_is_announced_when_it_starts_and_done_with_the_arguments_joined():
    events = read(
        event("contentBlockStart", contentBlockIndex=1, start={"toolUse": {"toolUseId": "tool_1", "name": "read_file"}}),
        event("contentBlockDelta", contentBlockIndex=1, delta={"toolUse": {"input": '{"file_'}}),
        event("contentBlockDelta", contentBlockIndex=1, delta={"toolUse": {"input": 'path":"a.txt"}'}}),
        event("contentBlockStop", contentBlockIndex=1),
        *ending("tool_use"),
    )
    assert kinds(events) == ["CallBegun", "CallDone", "ItemDone", "Finished"]
    assert (events[0].call_id, events[0].name) == ("tool_1", "read_file")
    assert events[1].arguments == '{"file_path":"a.txt"}'
    assert events[2].item == {"type": "function_call", "call_id": "tool_1", "name": "read_file", "arguments": '{"file_path":"a.txt"}'}
    assert events[3].incomplete == ""


def test_a_call_without_arguments_is_an_empty_object():
    events = read(event("contentBlockStart", contentBlockIndex=0, start={"toolUse": {"toolUseId": "t", "name": "noop"}}), event("contentBlockStop", contentBlockIndex=0), *ending("tool_use"))
    assert next(e for e in events if isinstance(e, CallDone)).arguments == "{}"


def test_thinking_keeps_its_text_and_signature_for_the_next_request_and_a_redacted_block_its_data():
    events = read(
        event("contentBlockDelta", contentBlockIndex=0, delta={"reasoningContent": {"text": "Let me "}}),
        event("contentBlockDelta", contentBlockIndex=0, delta={"reasoningContent": {"text": "think"}}),
        event("contentBlockDelta", contentBlockIndex=0, delta={"reasoningContent": {"signature": "SIG=="}}),
        event("contentBlockStop", contentBlockIndex=0),
        event("contentBlockDelta", contentBlockIndex=1, delta={"reasoningContent": {"redactedContent": "SECRET=="}}),
        event("contentBlockStop", contentBlockIndex=1),
        event("contentBlockDelta", contentBlockIndex=2, delta={"text": "Answer"}),
        event("contentBlockStop", contentBlockIndex=2),
        *ending(),
    )
    assert [e.text for e in events if isinstance(e, ReasoningDelta)] == ["Let me ", "think"]
    assert next(e for e in events if isinstance(e, ReasoningDone)).text == "Let me think"
    items = [e.item for e in events if isinstance(e, ItemDone)]
    assert items[0]["wc_reasoning"] == "Let me think" and items[0]["wc_signature"] == "SIG==" and items[0]["wc_origin"] == ORIGIN
    assert items[1] == {"type": "reasoning", "summary": [], "wc_data": "SECRET==", "wc_origin": ORIGIN}
    assert items[2]["role"] == "assistant"
    assert bedrock.converse_messages(items + [user_message("again")], ORIGIN)[0]["content"][0] == {"reasoningContent": {"reasoningText": {"text": "Let me think", "signature": "SIG=="}}}


@pytest.mark.parametrize(("stop", "incomplete"), [("end_turn", ""), ("tool_use", ""), ("stop_sequence", ""), ("max_tokens", "max_output_tokens"), ("content_filtered", "content_filter"), ("guardrail_intervened", "content_filter"), ("model_context_window_exceeded", "context_window")])
def test_why_the_answer_stopped_is_told_the_way_the_loop_knows_it(stop, incomplete):
    events = read(event("contentBlockDelta", contentBlockIndex=0, delta={"text": "x"}), event("contentBlockStop", contentBlockIndex=0), *ending(stop))
    assert events[-1].incomplete == incomplete


def test_an_answer_without_usage_has_none_and_one_that_ends_before_the_stop_is_an_error():
    events = read(event("contentBlockDelta", contentBlockIndex=0, delta={"text": "x"}), event("contentBlockStop", contentBlockIndex=0), event("messageStop", stopReason="end_turn"))
    assert isinstance(events[-1], Finished) and events[-1].usage is None
    with pytest.raises(ChatGPTError, match="ended before"):
        read(event("contentBlockDelta", contentBlockIndex=0, delta={"text": "x"}))


@pytest.mark.parametrize(("exception", "status"), [("throttlingException", 429), ("validationException", 400), ("modelStreamErrorException", 500), ("serviceUnavailableException", 503), ("accessDeniedException", 403)])
def test_an_exception_in_the_stream_is_an_error_with_the_status_that_goes_with_it(exception, status):
    with pytest.raises(ChatGPTError) as caught:
        read(event("contentBlockDelta", contentBlockIndex=0, delta={"text": "x"}), event_frame(exception, {"message": "Too many requests, please wait"}, kind="exception"))
    assert caught.value.message == "Too many requests, please wait" and caught.value.code == exception and caught.value.status == status


def test_pieces_that_arrive_whole_or_byte_by_byte_give_the_same_events():
    frames = [event("contentBlockDelta", contentBlockIndex=0, delta={"text": "ab"}), event("contentBlockStop", contentBlockIndex=0), *ending()]
    assert kinds(read(*frames, whole=True)) == kinds(read(*frames))


# ---------------------------------------------------------------------------------------------- the stream: InvokeModel


def chunk(event_json: dict) -> bytes:
    return event_frame("chunk", {"bytes": base64.b64encode(json.dumps(event_json).encode()).decode()})


def test_invoke_chunks_are_anthropics_events_in_base64_and_read_as_such():
    frames = [
        chunk({"type": "message_start", "message": {"id": "msg_1", "usage": {"input_tokens": 10, "output_tokens": 1}}}),
        chunk({"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}),
        chunk({"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Hi"}}),
        chunk({"type": "content_block_stop", "index": 0}),
        chunk({"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 5}}),
        chunk({"type": "message_stop"}),
    ]
    events = collect(read_anthropic(bedrock.invoke_lines(pieces([b"".join(frames)])), ORIGIN))
    assert kinds(events) == ["Started", "TextDelta", "TextDone", "ItemDone", "Finished"]
    assert events[1].text == "Hi"


def test_an_exception_frame_ends_an_invoke_stream_with_an_error_and_a_chunk_that_is_not_base64_is_skipped():
    async def lines(data):
        return [line async for line in bedrock.invoke_lines(pieces(data))]

    assert asyncio.run(lines([event_frame("chunk", {"bytes": "!!!not base64!!!"}), event_frame("chunk", {"other": 1})])) == []
    with pytest.raises(ChatGPTError, match="slow down") as caught:
        asyncio.run(lines([event_frame("throttlingException", {"message": "slow down"}, kind="exception")]))
    assert caught.value.status == 429


def test_the_openai_kind_is_not_the_converse_wire():
    assert RESPONSES != "converse"
