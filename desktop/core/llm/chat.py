"""Chat Completions: the request for ``POST {base}/chat/completions`` and its stream, as the loop's items and events.

Every OpenAI-compatible server speaks this (DeepSeek, OpenRouter, Ollama, LM Studio, vLLM, Together ...), with differences
that the code below allows for:

- A tool result is its own ``tool`` message and has to follow the assistant message that called for it; one assistant message
  carries the text and all the calls of a step.
- Thinking models send their thoughts as ``delta.reasoning_content`` (DeepSeek, Qwen on vLLM, Kimi ...) or ``delta.reasoning``.
  DeepSeek refuses the next request of a tool-using turn unless the thoughts come back in the assistant message as
  ``reasoning_content``, so they are kept in the ``reasoning`` item (``wc_reasoning``) and sent back to the same origin only.
- Some local models write their thoughts inline between ``<think>`` and ``</think>`` at the start of the answer.
- A tool call's id or index may be missing, or repeated for different calls.
- The usage comes in a last chunk without choices, only when asked for (``stream_options.include_usage``); some servers
  never send it, and the loop then goes by its own estimate.
"""

from __future__ import annotations

import logging
import uuid
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
from desktop.core.llm.wire import Route, declared_functions, images_of, load_json, sse_data, text_of, usage_of

log = logging.getLogger("desktop.llm")

THINK_OPEN = "<think>"
THINK_CLOSE = "</think>"


# ---------------------------------------------------------------------- the request


def chat_messages(instructions: str, items: Sequence[Mapping[str, Any]], origin: str) -> list[dict[str, Any]]:
    """The conversation as ``messages``. ``origin`` decides whose thoughts go back (those the same host wrote)."""
    messages: list[dict[str, Any]] = [{"role": "system", "content": instructions}] if instructions else []
    open_assistant: dict[str, Any] | None = None
    thoughts = ""

    def assistant() -> dict[str, Any]:
        nonlocal open_assistant, thoughts
        if open_assistant is None:
            open_assistant = {"role": "assistant", "content": ""}
            if thoughts:
                open_assistant["reasoning_content"] = thoughts
                thoughts = ""
            messages.append(open_assistant)
        return open_assistant

    for item in items:
        kind = item.get("type")
        if kind == "reasoning":
            if item.get("wc_reasoning") and item.get("wc_origin") == origin:
                thoughts += str(item["wc_reasoning"])
        elif kind == "message" and item.get("role") == "assistant":
            message = assistant()
            message["content"] = (message["content"] or "") + text_of(item)
        elif kind == "function_call":
            message = assistant()
            message.setdefault("tool_calls", []).append(
                {"id": item.get("call_id", ""), "type": "function", "function": {"name": item.get("name", ""), "arguments": item.get("arguments", "") or "{}"}}
            )
        elif kind == "function_call_output":
            open_assistant = None
            messages.append({"role": "tool", "tool_call_id": item.get("call_id", ""), "content": str(item.get("output", ""))})
        elif kind == "message":
            open_assistant = None
            thoughts = ""
            pictures = images_of(item)
            text = text_of(item)
            if pictures:
                parts: list[dict[str, Any]] = [{"type": "text", "text": text}] if text else []
                parts += [{"type": "image_url", "image_url": {"url": url}} for url in pictures]
                messages.append({"role": str(item.get("role", "user")), "content": parts})
            else:
                messages.append({"role": str(item.get("role", "user")), "content": text})
    return messages


def chat_body(request: Mapping[str, Any], route: Route) -> dict[str, Any]:
    """The JSON body for the route, from the loop's request."""
    body: dict[str, Any] = {
        "model": route.model,
        "messages": chat_messages(str(request.get("instructions") or ""), request.get("input") or (), route.origin),
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    functions = declared_functions(request.get("tools") or ())
    if functions:
        body["tools"] = [
            {"type": "function", "function": {"name": f["name"], "description": f.get("description", ""), "parameters": f.get("parameters") or {"type": "object", "properties": {}}}}
            for f in functions
        ]
        body["tool_choice"] = "auto"
    reasoning = request.get("reasoning")
    effort = reasoning.get("effort") if isinstance(reasoning, Mapping) else None
    if effort:
        body["reasoning_effort"] = effort
    if route.max_output:
        body["max_completion_tokens" if "api.openai.com" in route.base_url else "max_tokens"] = route.max_output
    body.update(route.extra_args)
    return body


# ---------------------------------------------------------------------- the stream


class ThinkSplitter:
    """Splits the answer's start into thoughts and words when the model writes ``<think>...</think>`` inline. Only a tag at the very
    start counts (after blank space): the same text in the middle of an answer is the answer's own."""

    def __init__(self) -> None:
        self._buffer = ""
        self._state = "start"  # start: nothing decided yet | thinking | answering

    def feed(self, text: str) -> list[tuple[bool, str]]:
        """``(is_thought, text)`` pieces for what arrived."""
        self._buffer += text
        out: list[tuple[bool, str]] = []
        while self._buffer:
            if self._state == "start":
                stripped = self._buffer.lstrip()
                if not stripped:
                    return out
                if stripped.startswith(THINK_OPEN):
                    self._buffer = stripped[len(THINK_OPEN) :]
                    self._state = "thinking"
                elif THINK_OPEN.startswith(stripped):
                    return out  # (maybe the beginning of the tag: wait)
                else:
                    self._state = "answering"
            elif self._state == "thinking":
                at = self._buffer.find(THINK_CLOSE)
                if at >= 0:
                    if at:
                        out.append((True, self._buffer[:at]))
                    self._buffer = self._buffer[at + len(THINK_CLOSE) :].lstrip("\r\n")
                    self._state = "answering"
                    continue
                keep = _tag_prefix(self._buffer, THINK_CLOSE)
                send, self._buffer = self._buffer[: len(self._buffer) - keep], self._buffer[len(self._buffer) - keep :]
                if send:
                    out.append((True, send))
                return out
            else:
                out.append((False, self._buffer))
                self._buffer = ""
        return out

    def finish(self) -> list[tuple[bool, str]]:
        """What is left when the answer ends."""
        rest, self._buffer = self._buffer, ""
        if not rest:
            return []
        return [(self._state == "thinking", rest)]


def _tag_prefix(text: str, tag: str) -> int:
    """How many characters at the end of ``text`` could be the start of ``tag``."""
    for size in range(min(len(tag) - 1, len(text)), 0, -1):
        if text.endswith(tag[:size]):
            return size
    return 0


def _error_of(payload: Mapping[str, Any]) -> ChatGPTError:
    error = payload.get("error")
    if isinstance(error, Mapping):
        code = error.get("code") if isinstance(error.get("code"), (str, int)) else error.get("type")
        message = error.get("message") if isinstance(error.get("message"), str) else "The response failed."
        return ChatGPTError(message, code=None if code is None else str(code), body=dict(error))
    return ChatGPTError(str(error or "The response failed."), body=dict(payload))


def _usage(raw: Any) -> Usage | None:
    if not isinstance(raw, Mapping):
        return None
    details = raw.get("prompt_tokens_details")
    cached = details.get("cached_tokens") if isinstance(details, Mapping) else None
    if not cached:
        cached = raw.get("prompt_cache_hit_tokens")  # (DeepSeek)
    spent = raw.get("completion_tokens_details")
    return usage_of(raw.get("prompt_tokens"), raw.get("completion_tokens"), cached, spent.get("reasoning_tokens") if isinstance(spent, Mapping) else 0)


class _Call:
    def __init__(self, call_id: str, slot: int) -> None:
        self.call_id = call_id
        self.slot = slot
        self.name = ""
        self.arguments = ""
        self.begun = False


async def read_chat(lines: AsyncIterator[str], origin: str = "") -> AsyncIterator[Event]:
    """The stream of a chat completion as events. ``origin`` marks the thoughts so only the same origin gets them back. Raises
    ``ChatGPTError`` for an error the server reports in the stream, and for a stream that ends before the answer is complete."""
    thinker = ThinkSplitter()
    own_thoughts = False  # (the server sends them itself: no tags to look for)
    thoughts = ""
    words = ""
    calls: dict[int, _Call] = {}
    slots: list[_Call] = []
    reason = ""
    usage: Usage | None = None
    response_id = ""
    ended = False

    def thought_events(piece: str) -> list[Event]:
        nonlocal thoughts
        thoughts += piece
        return [ReasoningDelta("rs_0", piece)] if piece else []

    def word_events(piece: str) -> list[Event]:
        nonlocal words
        words += piece
        return [TextDelta("msg_0", piece)] if piece else []

    async for data in sse_data(lines):
        if data.strip() == "[DONE]":
            ended = True
            break
        payload = load_json(data)
        if payload is None:
            log.warning("a stream chunk was not JSON: %.200s", data)
            continue
        if payload.get("error"):
            raise _error_of(payload)
        response_id = str(payload.get("id") or response_id)
        if isinstance(payload.get("usage"), Mapping):
            usage = _usage(payload["usage"]) or usage
        choices = payload.get("choices")
        choice = choices[0] if isinstance(choices, list) and choices and isinstance(choices[0], Mapping) else None
        if choice is None:
            continue
        delta = choice.get("delta") if isinstance(choice.get("delta"), Mapping) else {}
        thought = delta.get("reasoning_content") or delta.get("reasoning")
        if isinstance(thought, str) and thought:
            own_thoughts = True
            for event in thought_events(thought):
                yield event
        content = delta.get("content")
        if isinstance(content, list):  # (Mistral sends the answer in parts)
            content = "".join(p.get("text", "") for p in content if isinstance(p, Mapping) and isinstance(p.get("text"), str))
        if isinstance(content, str) and content:
            if own_thoughts:
                pieces = [(False, content)]
            else:
                pieces = thinker.feed(content)
            for is_thought, piece in pieces:
                for event in thought_events(piece) if is_thought else word_events(piece):
                    yield event
        for part in delta.get("tool_calls") or ():
            if not isinstance(part, Mapping):
                continue
            index = part.get("index") if isinstance(part.get("index"), int) else len(slots) - 1 if slots else 0
            call = calls.get(index)
            call_id = part.get("id") if isinstance(part.get("id"), str) and part.get("id") else ""
            if call is None or (call_id and call.call_id and call_id != call.call_id):
                call = _Call(call_id or f"call_{uuid.uuid4().hex[:16]}", len(slots))
                calls[index] = call
                slots.append(call)
            elif call_id and not call.call_id:
                call.call_id = call_id
            function = part.get("function") if isinstance(part.get("function"), Mapping) else {}
            if isinstance(function.get("name"), str) and function["name"] and not call.name:
                call.name = function["name"]
            if isinstance(function.get("arguments"), str):
                call.arguments += function["arguments"]
            if call.name and not call.begun:
                call.begun = True
                yield CallBegun(f"fc_{call.slot}", call.call_id, call.name)
        if choice.get("finish_reason"):
            reason = str(choice["finish_reason"])
    if not ended and not reason:
        raise ChatGPTError("The stream ended before the response was complete.", code="stream")
    if not own_thoughts:
        for is_thought, piece in thinker.finish():
            for event in thought_events(piece) if is_thought else word_events(piece):
                yield event
    if thoughts:
        yield ReasoningDone("rs_0", thoughts)
        yield ItemDone({"type": "reasoning", "summary": [{"type": "summary_text", "text": thoughts}], "wc_reasoning": thoughts, "wc_origin": origin})
    if words:
        yield TextDone("msg_0", words)
        yield ItemDone({"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": words}]})
    for call in slots:
        arguments = call.arguments
        yield CallDone(f"fc_{call.slot}", call.call_id, call.name, arguments)
        yield ItemDone({"type": "function_call", "call_id": call.call_id, "name": call.name, "arguments": arguments})
    incomplete = {"length": "max_output_tokens", "content_filter": "content_filter"}.get(reason, "")
    yield Finished(usage, response_id, incomplete)
