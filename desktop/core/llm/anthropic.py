"""Anthropic's Messages API: the request for ``POST {base}/v1/messages`` and its stream, as the loop's items and events.

What differs from the loop's own language, and what the code below does about it:

- There is no ``tool`` role. A tool's result is a ``tool_result`` block in a ``user`` message that follows the assistant message
  with the ``tool_use``; roles must alternate, so neighbouring items of one role become one message (results first, words after).
- A tool call's arguments are an object, not JSON text.
- With extended thinking on, the thinking blocks of the turn's tool loop must come back unchanged, signature included, or the API
  refuses the next request; a block it redacted (``redacted_thinking``) comes back as it was too. They are kept in the ``reasoning``
  item (``wc_reasoning`` + ``wc_signature``, or ``wc_data``) and sent back to the same origin only.
- ``max_tokens`` is required, and with thinking on it must be larger than the thinking budget.
- The input tokens it reports leave out what the cache served and what it wrote; the loop's ``Usage`` counts them in.
- ``cache_control`` marks a prefix the server may keep: the tools and system prompt, and the conversation up to its last block, so
  each step of a long tool loop pays for what is new.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator, Mapping, Sequence
from typing import Any

from desktop.core.chatgpt.errors import ChatGPTError
from desktop.core.chatgpt.responses import (
    CallBegun,
    CallDone,
    Event,
    Finished,
    ItemDone,
    ReasoningDelta,
    ReasoningDone,
    TextDelta,
    TextDone,
    Usage,
)
from desktop.core.llm.wire import DEFAULT_MAX_OUTPUT, Route, count, declared_functions, images_of, load_json, split_data_url, sse_data, text_of, usage_of

log = logging.getLogger("desktop.llm")

VERSION = "2023-06-01"
EPHEMERAL = {"type": "ephemeral"}
THINKING_BUDGETS = {"minimal": 1024, "low": 2048, "medium": 8192, "high": 24_000, "xhigh": 48_000, "max": 64_000}
"""Tokens of thought each level allows (the API's smallest budget is 1 024)."""
MIN_BUDGET = 1024
ANSWER_ROOM = 1024
"""The least of ``max_tokens`` that stays for the answer once the thinking budget is taken."""


# ---------------------------------------------------------------------- the request


def _image_block(url: str) -> dict[str, Any] | None:
    split = split_data_url(url)
    if split is not None:
        return {"type": "image", "source": {"type": "base64", "media_type": split[0], "data": split[1]}}
    if url.startswith(("http://", "https://")):
        return {"type": "image", "source": {"type": "url", "url": url}}
    return None


def _arguments(text: Any) -> dict[str, Any]:
    try:
        value = json.loads(text) if isinstance(text, str) and text.strip() else {}
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def anthropic_messages(items: Sequence[Mapping[str, Any]], origin: str) -> list[dict[str, Any]]:
    """The conversation as ``messages``: alternating roles, results before words, the same origin's thinking in front of its answer."""
    messages: list[dict[str, Any]] = []

    def push(role: str, blocks: list[dict[str, Any]]) -> None:
        if not blocks:
            return
        if messages and messages[-1]["role"] == role:
            messages[-1]["content"].extend(blocks)
        else:
            messages.append({"role": role, "content": list(blocks)})

    for item in items:
        kind = item.get("type")
        if kind == "reasoning":
            if item.get("wc_origin") != origin:
                continue
            if item.get("wc_data"):
                push("assistant", [{"type": "redacted_thinking", "data": item["wc_data"]}])
            elif item.get("wc_reasoning") and item.get("wc_signature"):
                push("assistant", [{"type": "thinking", "thinking": item["wc_reasoning"], "signature": item["wc_signature"]}])
        elif kind == "message" and item.get("role") == "assistant":
            text = text_of(item)
            push("assistant", [{"type": "text", "text": text}] if text.strip() else [])
        elif kind == "function_call":
            push("assistant", [{"type": "tool_use", "id": item.get("call_id", ""), "name": item.get("name", ""), "input": _arguments(item.get("arguments"))}])
        elif kind == "function_call_output":
            push("user", [{"type": "tool_result", "tool_use_id": item.get("call_id", ""), "content": str(item.get("output", ""))}])
        elif kind == "message":
            blocks: list[dict[str, Any]] = []
            text = text_of(item)
            if text.strip():
                blocks.append({"type": "text", "text": text})
            blocks += [block for block in map(_image_block, images_of(item)) if block is not None]
            push("user", blocks)
    return messages


def thinking_for(effort: str | None, max_tokens: int, budget: int | None = None) -> dict[str, Any] | None:
    """The ``thinking`` setting: the level's budget, or, with no level chosen, the entry's own when it turned thinking on
    (``enableThinking`` / ``thinkingMaxTokens``). ``None`` when there is neither or the answer would be left without room."""
    if not effort and not budget:
        return None
    room = min(THINKING_BUDGETS.get(effort, THINKING_BUDGETS["medium"]) if effort else budget or 0, max_tokens - ANSWER_ROOM)
    return {"type": "enabled", "budget_tokens": room} if room >= MIN_BUDGET else None


def anthropic_body(request: Mapping[str, Any], route: Route) -> dict[str, Any]:
    """The JSON body for the route, from the loop's request."""
    max_tokens = route.max_output or DEFAULT_MAX_OUTPUT
    messages = anthropic_messages(request.get("input") or (), route.origin)
    if messages:
        messages[-1]["content"][-1] = {**messages[-1]["content"][-1], "cache_control": EPHEMERAL}
    body: dict[str, Any] = {"model": route.model, "max_tokens": max_tokens, "messages": messages, "stream": True}
    instructions = str(request.get("instructions") or "")
    if instructions:
        body["system"] = [{"type": "text", "text": instructions, "cache_control": EPHEMERAL}]
    functions = declared_functions(request.get("tools") or ())
    if functions:
        body["tools"] = [{"name": f["name"], "description": f.get("description", ""), "input_schema": f.get("parameters") or {"type": "object", "properties": {}}} for f in functions]
        body["tool_choice"] = {"type": "auto"}
    reasoning = request.get("reasoning")
    thinking = thinking_for(reasoning.get("effort") if isinstance(reasoning, Mapping) else None, max_tokens, route.thinking)
    if thinking is not None:
        body["thinking"] = thinking
    body.update(route.extra_args)
    return body


# ---------------------------------------------------------------------- the stream


_STOPS = {"max_tokens": "max_output_tokens", "refusal": "content_filter", "pause_turn": "pause_turn", "model_context_window_exceeded": "context_window"}


def _usage(raw: Mapping[str, Any], earlier: Mapping[str, Any]) -> dict[str, Any]:
    merged = dict(earlier)
    merged.update({key: value for key, value in raw.items() if value is not None})
    return merged


def _to_usage(raw: Mapping[str, Any]) -> Usage:
    cached, written = count(raw.get("cache_read_input_tokens")), count(raw.get("cache_creation_input_tokens"))
    return usage_of(count(raw.get("input_tokens")) + cached + written, raw.get("output_tokens"), cached, 0, written)


def _error_of(payload: Mapping[str, Any]) -> ChatGPTError:
    error = payload.get("error") if isinstance(payload.get("error"), Mapping) else {}
    message = error.get("message") if isinstance(error.get("message"), str) else "The response failed."
    code = error.get("type") if isinstance(error.get("type"), str) else None
    return ChatGPTError(message, code=code, body=dict(error) or dict(payload))


class _Block:
    def __init__(self, kind: str, **fields: Any) -> None:
        self.kind = kind
        self.text = ""
        self.signature = ""
        self.fields = fields


async def read_anthropic(lines: AsyncIterator[str], origin: str = "") -> AsyncIterator[Event]:
    """The stream of a message as events. Raises ``ChatGPTError`` for an ``error`` event and for a stream that ends early."""
    blocks: dict[int, _Block] = {}
    usage: dict[str, Any] = {}
    response_id = ""
    stop = ""
    stopped = False
    async for data in sse_data(lines):
        payload = load_json(data)
        if payload is None:
            log.warning("a stream event was not JSON: %.200s", data)
            continue
        kind = payload.get("type")
        if kind == "error":
            raise _error_of(payload)
        if kind == "message_start":
            message = payload.get("message") if isinstance(payload.get("message"), Mapping) else {}
            response_id = str(message.get("id") or "")
            usage = _usage(message.get("usage") if isinstance(message.get("usage"), Mapping) else {}, usage)
        elif kind == "content_block_start":
            index = payload.get("index") if isinstance(payload.get("index"), int) else len(blocks)
            start = payload.get("content_block") if isinstance(payload.get("content_block"), Mapping) else {}
            block = _Block(str(start.get("type") or "text"), id=str(start.get("id") or ""), name=str(start.get("name") or ""), data=start.get("data"))
            blocks[index] = block
            if block.kind == "tool_use":
                yield CallBegun(f"toolu_{index}", block.fields["id"], block.fields["name"])
            elif block.kind == "text" and isinstance(start.get("text"), str):
                block.text = start["text"]
        elif kind == "content_block_delta":
            block = blocks.get(payload.get("index"))  # type: ignore[arg-type]
            delta = payload.get("delta") if isinstance(payload.get("delta"), Mapping) else {}
            if block is None:
                continue
            piece = delta.get("type")
            if piece == "text_delta":
                block.text += str(delta.get("text") or "")
                yield TextDelta(f"msg_{payload['index']}", str(delta.get("text") or ""))
            elif piece == "thinking_delta":
                block.text += str(delta.get("thinking") or "")
                yield ReasoningDelta(f"rs_{payload['index']}", str(delta.get("thinking") or ""))
            elif piece == "signature_delta":
                block.signature += str(delta.get("signature") or "")
            elif piece == "input_json_delta":
                block.text += str(delta.get("partial_json") or "")
        elif kind == "content_block_stop":
            index = payload.get("index")
            block = blocks.pop(index, None)  # type: ignore[arg-type]
            if block is None:
                continue
            if block.kind == "text":
                yield TextDone(f"msg_{index}", block.text)
                yield ItemDone({"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": block.text}]})
            elif block.kind == "thinking":
                yield ReasoningDone(f"rs_{index}", block.text)
                item: dict[str, Any] = {"type": "reasoning", "summary": [{"type": "summary_text", "text": block.text}], "wc_reasoning": block.text, "wc_origin": origin}
                if block.signature:
                    item["wc_signature"] = block.signature
                yield ItemDone(item)
            elif block.kind == "redacted_thinking":
                yield ItemDone({"type": "reasoning", "summary": [], "wc_data": block.fields.get("data"), "wc_origin": origin})
            elif block.kind == "tool_use":
                arguments = block.text or "{}"
                yield CallDone(f"toolu_{index}", block.fields["id"], block.fields["name"], arguments)
                yield ItemDone({"type": "function_call", "call_id": block.fields["id"], "name": block.fields["name"], "arguments": arguments})
        elif kind == "message_delta":
            delta = payload.get("delta") if isinstance(payload.get("delta"), Mapping) else {}
            stop = str(delta.get("stop_reason") or stop)
            usage = _usage(payload.get("usage") if isinstance(payload.get("usage"), Mapping) else {}, usage)
        elif kind == "message_stop":
            stopped = True
            break
    if not stopped:
        raise ChatGPTError("The stream ended before the response was complete.", code="stream")
    yield Finished(_to_usage(usage) if usage else None, response_id, _STOPS.get(stop, ""))
