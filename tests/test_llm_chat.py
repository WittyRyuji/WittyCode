"""Chat Completions (``core/llm/chat``): the request built from the loop's items, and the stream read back into its events."""

from __future__ import annotations

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
    build_request,
    call_output,
    user_message,
)
from desktop.core.llm.chat import ThinkSplitter, chat_body, chat_messages, read_chat
from desktop.core.llm.wire import CHAT, Route
from tests.llm_support import collect, kinds, lines_of

ROUTE = Route(CHAT, "https://api.deepseek.com/v1", "deepseek-chat", max_output=8000)
ORIGIN = ROUTE.origin


def assistant_text(text: str) -> dict:
    return {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": text}]}


def function_call(call_id: str, name: str, arguments: str = "{}") -> dict:
    return {"type": "function_call", "call_id": call_id, "name": name, "arguments": arguments}


def chunk(delta: dict | None = None, finish: str | None = None, **extra) -> dict:
    payload = {"id": "chatcmpl-1", "choices": [{"index": 0, "delta": delta or {}, "finish_reason": finish}]}
    payload.update(extra)
    return payload


def read(chunks, origin: str = ORIGIN) -> list:
    return collect(read_chat(lines_of(chunks), origin))


# ------------------------------------------------------------------------------------------------ the request


def test_a_step_with_text_and_calls_is_one_assistant_message_and_each_result_a_tool_message():
    items = [
        user_message("look at a.txt and b.txt"),
        assistant_text("I will read both."),
        function_call("c1", "read_file", '{"file_path": "a.txt"}'),
        function_call("c2", "read_file", '{"file_path": "b.txt"}'),
        call_output("c1", "alpha"),
        call_output("c2", "beta"),
        assistant_text("Done."),
    ]
    assert chat_messages("be brief", items, ORIGIN) == [
        {"role": "system", "content": "be brief"},
        {"role": "user", "content": "look at a.txt and b.txt"},
        {
            "role": "assistant",
            "content": "I will read both.",
            "tool_calls": [
                {"id": "c1", "type": "function", "function": {"name": "read_file", "arguments": '{"file_path": "a.txt"}'}},
                {"id": "c2", "type": "function", "function": {"name": "read_file", "arguments": '{"file_path": "b.txt"}'}},
            ],
        },
        {"role": "tool", "tool_call_id": "c1", "content": "alpha"},
        {"role": "tool", "tool_call_id": "c2", "content": "beta"},
        {"role": "assistant", "content": "Done."},
    ]


def test_a_call_with_no_words_has_an_empty_content_and_no_arguments_become_an_empty_object():
    messages = chat_messages("", [user_message("go"), {"type": "function_call", "call_id": "c1", "name": "ls", "arguments": ""}], ORIGIN)
    assert messages == [
        {"role": "user", "content": "go"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "ls", "arguments": "{}"}}]},
    ]


def test_pictures_go_as_image_parts_and_words_alone_as_a_plain_string():
    messages = chat_messages("", [user_message("what is this", ["data:image/png;base64,AAAA"]), user_message("and now?")], ORIGIN)
    assert messages[0]["content"] == [{"type": "text", "text": "what is this"}, {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}]
    assert messages[1]["content"] == "and now?"


def test_the_thoughts_of_the_same_origin_go_back_with_the_assistant_message_and_other_thoughts_do_not():
    thought = {"type": "reasoning", "summary": [], "wc_reasoning": "I should read the file.", "wc_origin": ORIGIN}
    foreign = {"type": "reasoning", "summary": [], "wc_reasoning": "someone else's", "wc_origin": "api.other.example"}
    openai_own = {"type": "reasoning", "summary": [], "encrypted_content": "gAAA"}
    items = [user_message("go"), thought, function_call("c1", "read_file"), call_output("c1", "x"), foreign, openai_own, assistant_text("ok")]
    messages = chat_messages("", items, ORIGIN)
    assert messages[1]["reasoning_content"] == "I should read the file."
    assert "reasoning_content" not in messages[-1], "thoughts of another origin, and OpenAI's encrypted ones, mean nothing here"


def test_the_body_declares_the_tools_the_level_the_room_and_asks_for_the_usage():
    request = build_request(
        model="ignored", instructions="sys", items=[user_message("hi")], reasoning="high",
        tools=[{"name": "read_file", "description": "Read.", "parameters": {"type": "object", "properties": {"p": {"type": "string"}}}}],
    )
    body = chat_body(request, ROUTE)
    assert body["model"] == "deepseek-chat" and body["stream"] is True and body["stream_options"] == {"include_usage": True}
    assert body["tools"] == [{"type": "function", "function": {"name": "read_file", "description": "Read.", "parameters": {"type": "object", "properties": {"p": {"type": "string"}}}}}]
    assert body["tool_choice"] == "auto" and body["reasoning_effort"] == "high" and body["max_tokens"] == 8000
    assert "max_completion_tokens" not in body


def test_openai_itself_wants_max_completion_tokens_and_the_users_own_arguments_have_the_last_word():
    route = Route(CHAT, "https://api.openai.com/v1", "gpt-x", max_output=1000, extra_args={"temperature": 0.2, "max_completion_tokens": 50})
    body = chat_body(build_request(model="m", instructions="", items=[user_message("hi")], tools=[], reasoning=None), route)
    assert body["max_completion_tokens"] == 50 and body["temperature"] == 0.2 and "max_tokens" not in body
    assert "tools" not in body and "reasoning_effort" not in body and body["messages"] == [{"role": "user", "content": "hi"}]


# ------------------------------------------------------------------------------------------------ the stream


def test_words_arrive_in_pieces_then_one_message_then_the_usage_ends_the_answer():
    events = read([
        chunk({"role": "assistant", "content": ""}),
        chunk({"content": "Hel"}),
        chunk({"content": "lo."}, finish="stop"),
        {"id": "chatcmpl-1", "choices": [], "usage": {"prompt_tokens": 120, "completion_tokens": 7, "prompt_tokens_details": {"cached_tokens": 100}}},
        "[DONE]",
    ])
    assert kinds(events) == ["TextDelta", "TextDelta", "TextDone", "ItemDone", "Finished"]
    assert [e.text for e in events[:2]] == ["Hel", "lo."] and events[2] == TextDone("msg_0", "Hello.")
    assert events[3].item == {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "Hello."}]}
    done = events[4]
    assert done.incomplete == "" and done.response_id == "chatcmpl-1"
    assert (done.usage.input_tokens, done.usage.cached_tokens, done.usage.output_tokens) == (120, 100, 7)
    assert done.usage.context == 127


def test_an_answer_cut_by_the_length_limit_says_so_and_a_filtered_one_too():
    assert read([chunk({"content": "x"}, finish="length"), "[DONE]"])[-1].incomplete == "max_output_tokens"
    assert read([chunk({"content": "x"}, finish="content_filter"), "[DONE]"])[-1].incomplete == "content_filter"


def test_two_calls_in_pieces_are_assembled_by_their_index():
    events = read([
        chunk({"tool_calls": [{"index": 0, "id": "call_a", "type": "function", "function": {"name": "read_file", "arguments": ""}}]}),
        chunk({"tool_calls": [{"index": 0, "function": {"arguments": '{"file_'}}]}),
        chunk({"tool_calls": [{"index": 1, "id": "call_b", "type": "function", "function": {"name": "ls", "arguments": "{}"}}]}),
        chunk({"tool_calls": [{"index": 0, "function": {"arguments": 'path": "a.txt"}'}}]}, finish="tool_calls"),
        "[DONE]",
    ])
    assert [e for e in events if isinstance(e, CallBegun)] == [CallBegun("fc_0", "call_a", "read_file"), CallBegun("fc_1", "call_b", "ls")]
    assert [e for e in events if isinstance(e, CallDone)] == [
        CallDone("fc_0", "call_a", "read_file", '{"file_path": "a.txt"}'),
        CallDone("fc_1", "call_b", "ls", "{}"),
    ]
    assert [e.item for e in events if isinstance(e, ItemDone)] == [
        {"type": "function_call", "call_id": "call_a", "name": "read_file", "arguments": '{"file_path": "a.txt"}'},
        {"type": "function_call", "call_id": "call_b", "name": "ls", "arguments": "{}"},
    ]


def test_calls_without_ids_get_their_own_and_one_index_used_for_two_ids_makes_two_calls():
    anonymous = read([chunk({"tool_calls": [{"function": {"name": "ls", "arguments": "{}"}}]}, finish="tool_calls"), "[DONE]"])
    (call,) = [e for e in anonymous if isinstance(e, CallDone)]
    assert call.call_id.startswith("call_") and len(call.call_id) > 8
    same_index = read([
        chunk({"tool_calls": [{"index": 0, "id": "x1", "function": {"name": "ls", "arguments": "{}"}}]}),
        chunk({"tool_calls": [{"index": 0, "id": "x2", "function": {"name": "pwd", "arguments": "{}"}}]}, finish="tool_calls"),
        "[DONE]",
    ])
    assert [(e.call_id, e.name) for e in same_index if isinstance(e, CallDone)] == [("x1", "ls"), ("x2", "pwd")]


def test_thoughts_come_first_marked_with_their_origin_and_deepseeks_cache_hits_count_as_cached():
    events = read([
        chunk({"reasoning_content": "Let me "}),
        chunk({"reasoning_content": "think."}),
        chunk({"content": "Answer."}, finish="stop"),
        {"id": "c", "choices": [], "usage": {"prompt_tokens": 50, "completion_tokens": 9, "prompt_cache_hit_tokens": 40, "completion_tokens_details": {"reasoning_tokens": 4}}},
        "[DONE]",
    ])
    assert kinds(events) == ["ReasoningDelta", "ReasoningDelta", "TextDelta", "ReasoningDone", "ItemDone", "TextDone", "ItemDone", "Finished"]
    assert events[3] == ReasoningDone("rs_0", "Let me think.")
    assert events[4].item == {"type": "reasoning", "summary": [{"type": "summary_text", "text": "Let me think."}], "wc_reasoning": "Let me think.", "wc_origin": ORIGIN}
    usage = events[-1].usage
    assert (usage.cached_tokens, usage.reasoning_tokens) == (40, 4)


def test_thoughts_written_inline_between_think_tags_are_split_off_even_when_a_tag_is_cut_in_two():
    events = read([chunk({"content": "<thi"}), chunk({"content": "nk>plan it</th"}), chunk({"content": "ink>\n\nThe answer."}, finish="stop"), "[DONE]"])
    assert "".join(e.text for e in events if isinstance(e, ReasoningDelta)) == "plan it"
    assert "".join(e.text for e in events if isinstance(e, TextDelta)) == "The answer."
    assert [e for e in events if isinstance(e, ReasoningDone)] == [ReasoningDone("rs_0", "plan it")]


def test_the_same_tag_in_the_middle_of_an_answer_is_the_answers_own_text():
    events = read([chunk({"content": "Use the "}), chunk({"content": "<think> tag like this."}, finish="stop"), "[DONE]"])
    assert not [e for e in events if isinstance(e, ReasoningDelta)]
    assert [e for e in events if isinstance(e, TextDone)] == [TextDone("msg_0", "Use the <think> tag like this.")]


def test_a_server_that_sends_its_thoughts_itself_gets_no_tag_search_in_its_answer():
    events = read([chunk({"reasoning_content": "hm"}), chunk({"content": "<think>quoted</think> is the "}), chunk({"content": "tag"}, finish="stop"), "[DONE]"])
    assert [e for e in events if isinstance(e, ReasoningDone)] == [ReasoningDone("rs_0", "hm")], "only the server's own thoughts"
    assert [e for e in events if isinstance(e, TextDone)] == [TextDone("msg_0", "<think>quoted</think> is the tag")]
    cut = read([chunk({"reasoning_content": "hm"}), chunk({"content": "<thi"}, finish="stop"), "[DONE]"])
    assert [e for e in cut if isinstance(e, TextDone)] == [TextDone("msg_0", "<thi")], "nothing is held back waiting for a tag"


def test_think_splitter_holds_back_only_what_could_be_a_tag():
    splitter = ThinkSplitter()
    assert splitter.feed("  ") == [] and splitter.feed("<think>abc<") == [(True, "abc")]
    assert splitter.feed("/think>") == [] and splitter.feed("done") == [(False, "done")]
    assert splitter.finish() == []
    late = ThinkSplitter()
    assert late.feed("<think>unfinished") == [(True, "unfinished")] and late.finish() == []


def test_an_answer_in_parts_is_joined():
    events = read([chunk({"content": [{"type": "text", "text": "Hi "}, {"type": "text", "text": "there"}]}, finish="stop"), "[DONE]"])
    assert [e for e in events if isinstance(e, TextDone)] == [TextDone("msg_0", "Hi there")]


def test_done_without_a_finish_reason_still_ends_the_answer_but_a_cut_stream_is_an_error():
    assert isinstance(read([chunk({"content": "ok"}), "[DONE]"])[-1], Finished)
    with pytest.raises(ChatGPTError) as cut:
        read([chunk({"content": "ok"})])
    assert cut.value.code == "stream"


def test_an_error_in_the_stream_is_raised_with_the_servers_words():
    with pytest.raises(ChatGPTError, match="rate limited") as error:
        read([chunk({"content": "a"}), {"error": {"message": "rate limited", "code": 429}}])
    assert error.value.code == "429"


def test_a_server_that_sends_no_usage_ends_with_none():
    done = read([chunk({"content": "ok"}, finish="stop"), "[DONE]"])[-1]
    assert isinstance(done, Finished) and done.usage is None


def test_a_reasoning_delta_is_a_thought_event():
    assert read([chunk({"reasoning": "hm"}), chunk({"content": "x"}, finish="stop"), "[DONE]"])[0] == ReasoningDelta("rs_0", "hm")
    assert TextDelta("a", "b").text == "b"
