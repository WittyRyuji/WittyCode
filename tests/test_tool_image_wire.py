"""Tool pictures expand only at the wire, without splitting a parallel tool batch."""
import base64
import json

import pytest

from desktop.core.chatgpt.responses import call_output, for_responses, user_message
from desktop.core.harness.history import NO_TOOL_IMAGES
from desktop.core.llm.anthropic import anthropic_body
from desktop.core.llm.bedrock import converse_body
from desktop.core.llm.chat import chat_body
from desktop.core.llm.responses_api import responses_body
from desktop.core.llm.wire import ANTHROPIC, CHAT, RESPONSES, Route
from tests.tool_image_support import png


def answer(count):
    calls = [{"type": "function_call", "call_id": str(i), "name": "read_file", "arguments": "{}"} for i in range(count)]
    results = [call_output(str(i), f"file-{i}.png (image/png)") for i in range(count)]
    pictures = [base64.b64encode(png(width=i + 1)).decode("ascii") for i in range(count)]
    for result, data in zip(results, pictures):
        result["wc_images"] = [{"media_type": "image/png", "data": data}]
    calls.append({"type": "function_call", "call_id": "text", "name": "read_file", "arguments": "{}"})
    results.append(call_output("text", "plain text"))  # The last output of the batch has no picture.
    return [user_message("Inspect the files."), *calls, *results], results, pictures


@pytest.mark.parametrize("count", [1, 3])
def test_responses_expands_picture_batch_after_last_tool_output(count):
    items, results, pictures = answer(count)
    items.append(user_message("Another question"))
    sent = for_responses(items)
    assert responses_body({"input": items}, Route(RESPONSES, "https://api.example/v1", "model"))["input"] == sent
    assert sent[-2]["role"] == "user" and sent[-1] == items[-1]
    assert sent[-(count + 3):-2] == [call_output(item["call_id"], item["output"]) for item in results]
    label, *parts = sent[-2]["content"]
    assert label["type"] == "input_text" and all(f"file-{i}.png" in label["text"] for i in range(count))
    assert parts == [{"type": "input_image", "image_url": "data:image/png;base64," + data} for data in pictures]
    assert len(items) + 1 == len(sent) and all("wc_images" in item for item in results[:-1])


@pytest.mark.parametrize("count", [1, 3])
def test_chat_expands_after_contiguous_tool_messages(count):
    items, results, pictures = answer(count)
    first, assistant, *tools, attached = chat_body({"input": items}, Route(CHAT, "https://api.example/v1", "model"))["messages"]
    assert first["role"] == "user" and assistant["role"] == "assistant"
    assert [call["id"] for call in assistant["tool_calls"]] == [item["call_id"] for item in results]
    assert tools == [{"role": "tool", "tool_call_id": item["call_id"], "content": item["output"]} for item in results]
    assert attached["role"] == "user"
    label, *parts = attached["content"]
    assert label["type"] == "text" and all(f"file-{i}.png" in label["text"] for i in range(count))
    assert parts == [{"type": "image_url", "image_url": {"url": "data:image/png;base64," + data}} for data in pictures]


@pytest.mark.parametrize("count", [1, 3])
def test_anthropic_puts_picture_inside_its_tool_result(count):
    items, results, pictures = answer(count)
    first, assistant, reply = anthropic_body({"input": items}, Route(ANTHROPIC, "https://api.example", "model"))["messages"]
    assert [first["role"], assistant["role"], reply["role"]] == ["user", "assistant", "user"]
    assert [block["id"] for block in assistant["content"]] == [item["call_id"] for item in results]
    blocks = reply["content"]
    assert len(blocks) == count + 1
    for index, data in enumerate(pictures):
        assert blocks[index] == {"type": "tool_result", "tool_use_id": str(index), "content": [
            {"type": "text", "text": results[index]["output"]},
            {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": data}},
        ]}
    assert blocks[-1]["type"] == "tool_result" and blocks[-1]["content"] == "plain text"


def test_converse_includes_picture_as_user_content_after_all_tool_results():
    items, results, pictures = answer(1)
    reply = converse_body({"input": items}, Route("converse", "https://api.example", "model"))["messages"][-1]
    first, second, label, picture = reply["content"]
    assert reply["role"] == "user"
    assert [block["toolResult"]["toolUseId"] for block in [first, second]] == [item["call_id"] for item in results]
    assert "file-0.png" in label["text"]
    assert picture == {"image": {"format": "png", "source": {"bytes": pictures[0]}}}


@pytest.mark.parametrize("kind,body", [(CHAT, chat_body), (ANTHROPIC, anthropic_body), (RESPONSES, responses_body), ("converse", converse_body)])
def test_text_only_provider_gets_notice_and_no_tool_picture(kind, body):
    items, results, pictures = answer(2)
    route = Route(kind, "https://api.example/v1", "text-only", supports_images=False)
    sent = json.dumps(body({"input": items}, route))
    assert sent.count(NO_TOOL_IMAGES) == 2 and "wc_images" not in sent
    assert all(data not in sent for data in pictures) and "Pictures from tools" not in sent
    assert "wc_images" in results[0] and NO_TOOL_IMAGES not in results[0]["output"]
