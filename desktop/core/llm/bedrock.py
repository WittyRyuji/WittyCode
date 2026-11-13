"""Amazon Bedrock Runtime: the request for ``converse-stream`` (and ``invoke-with-response-stream`` for Anthropic's own format) and the
stream that answers it, as the loop's items and events.

The answer does not come as server-sent events but as AWS's **event stream**: binary frames, each with a prelude (total length,
header length, CRC of those eight bytes), headers (``:message-type`` ``event`` / ``exception``, ``:event-type``), a payload (JSON)
and a CRC of everything before it. ``frames`` reads them from the bytes as they arrive.

What Converse asks for, and what the code below does about it:

- Roles alternate and start with the user; a tool's result is a ``toolResult`` block in a ``user`` message that follows the
  assistant message with the ``toolUse`` (results first), so neighbouring items of one role become one message.
- A tool call's input is an object; a tool result and a text block may not be empty (an empty output is sent as ``(no output)``).
- Claude with extended thinking wants the ``reasoningContent`` of the turn's tool loop back unchanged, signature included (or the
  ``redactedContent``); they are kept in the ``reasoning`` item (``wc_reasoning`` + ``wc_signature``, or ``wc_data``) and sent back to
  the same origin only.
- ``cachePoint`` blocks mark a prefix the server may keep; a model that does not support them refuses the request, so they are only
  sent to the models known to take them (Claude 3.5 Haiku, 3.7 and 4 and later, Nova).
- ``maxTokens`` is left out unless the user gave one: another model's own limit is not ours to guess (with thinking on it must exceed
  the budget, so then it is set).
- Input tokens leave out what the cache served and what it wrote; the loop's ``Usage`` counts them in.
"""

from __future__ import annotations

import base64
import json
import logging
import re
import struct
import zlib
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

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
)
from desktop.core.llm.anthropic import anthropic_body, thinking_effort, thinking_fields
from desktop.core.llm.wire import DEFAULT_MAX_OUTPUT, Route, count, declared_functions, images_of, load_json, split_data_url, text_of, usage_of

log = logging.getLogger("desktop.llm")

CACHE_POINT = {"cachePoint": {"type": "default"}}
IMAGE_FORMATS = {"image/png": "png", "image/jpeg": "jpeg", "image/jpg": "jpeg", "image/gif": "gif", "image/webp": "webp"}
ANTHROPIC_VERSION = "bedrock-2023-05-31"
NO_OUTPUT = "(no output)"
MAX_FRAME = 32 * 1024 * 1024
"""A frame larger than this is not one (a corrupt length would otherwise make the reader wait for gigabytes)."""
_CACHING_MODELS = re.compile(r"claude-(?:3-5-haiku|3-7-sonnet|(?:opus|sonnet|haiku|fable)-\d)|nova-(?:micro|lite|pro|premier)")
_EXCEPTION_STATUS = {
    "throttlingException": 429,
    "validationException": 400,
    "modelTimeoutException": 408,
    "modelNotReadyException": 429,
    "serviceUnavailableException": 503,
    "internalServerException": 500,
    "modelStreamErrorException": 500,
    "accessDeniedException": 403,
    "resourceNotFoundException": 404,
}


def endpoint(region: str, override: str = "") -> str:
    """Where Bedrock Runtime is: the user's override (a VPC endpoint, a proxy), else the regional endpoint."""
    return override.rstrip("/") if override else f"https://bedrock-runtime.{region}.amazonaws.com"


def model_url(base: str, model: str, operation: str) -> str:
    """``{base}/model/{id}/{operation}`` with the id encoded once (a colon is ``%3A``, an ARN's slashes ``%2F``)."""
    return f"{base.rstrip('/')}/model/{quote(model, safe='')}/{operation}"


def caches(model: str) -> bool:
    return _CACHING_MODELS.search(model.lower()) is not None


def is_claude(model: str) -> bool:
    return "claude" in model.lower()


# ---------------------------------------------------------------------- the request


def _image_block(url: str) -> dict[str, Any] | None:
    split = split_data_url(url)
    if split is None:
        return None
    image_format = IMAGE_FORMATS.get(split[0].lower())
    return {"image": {"format": image_format, "source": {"bytes": split[1]}}} if image_format else None


def _arguments(text: Any) -> dict[str, Any]:
    try:
        value = json.loads(text) if isinstance(text, str) and text.strip() else {}
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def converse_messages(items: Sequence[Mapping[str, Any]], origin: str) -> list[dict[str, Any]]:
    """The conversation as Converse ``messages``: alternating roles, results before words, thinking in front of its answer."""
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
                push("assistant", [{"reasoningContent": {"redactedContent": item["wc_data"]}}])
            elif item.get("wc_signature"):
                push("assistant", [{"reasoningContent": {"reasoningText": {"text": item.get("wc_reasoning", ""), "signature": item["wc_signature"]}}}])
        elif kind == "message" and item.get("role") == "assistant":
            text = text_of(item)
            push("assistant", [{"text": text}] if text.strip() else [])
        elif kind == "function_call":
            push("assistant", [{"toolUse": {"toolUseId": item.get("call_id", ""), "name": item.get("name", ""), "input": _arguments(item.get("arguments"))}}])
        elif kind == "function_call_output":
            output = str(item.get("output", ""))
            push("user", [{"toolResult": {"toolUseId": item.get("call_id", ""), "content": [{"text": output if output.strip() else NO_OUTPUT}]}}])
        elif kind == "message":
            blocks: list[dict[str, Any]] = []
            text = text_of(item)
            if text.strip():
                blocks.append({"text": text})
            blocks += [block for block in map(_image_block, images_of(item)) if block is not None]
            push("user", blocks)
    return messages


def _merge(body: dict[str, Any], extra: Mapping[str, Any]) -> None:
    """``extraArgs`` into the body: a setting that is an object (``inferenceConfig``) is merged into ours, anything else replaces ours."""
    for key, value in extra.items():
        if isinstance(value, Mapping) and isinstance(body.get(key), dict):
            body[key] = {**body[key], **value}
        else:
            body[key] = value


def converse_body(request: Mapping[str, Any], route: Route, metadata: Mapping[str, str] | None = None) -> dict[str, Any]:
    """The JSON body of ``converse-stream`` for the route, from the loop's request."""
    model = route.model
    cache = caches(model)
    messages = converse_messages(request.get("input") or (), route.origin)
    if messages and cache:
        messages[-1]["content"].append(CACHE_POINT)
    body: dict[str, Any] = {"messages": messages}
    instructions = str(request.get("instructions") or "")
    if instructions:
        body["system"] = [{"text": instructions}, *([CACHE_POINT] if cache else [])]
    functions = declared_functions(request.get("tools") or ())
    if functions:
        body["toolConfig"] = {
            "tools": [
                {"toolSpec": {"name": f["name"], **({"description": f["description"]} if f.get("description") else {}), "inputSchema": {"json": f.get("parameters") or {"type": "object", "properties": {}}}}}
                for f in functions
            ],
            "toolChoice": {"auto": {}},
        }
    max_tokens = route.max_output
    thinking = thinking_fields(model, thinking_effort(request), max_tokens or DEFAULT_MAX_OUTPUT, route.thinking) if is_claude(model) else {}
    if thinking:
        max_tokens = max_tokens or DEFAULT_MAX_OUTPUT
        body["additionalModelRequestFields"] = thinking
    if max_tokens:
        body["inferenceConfig"] = {"maxTokens": max_tokens}
    if metadata:
        body["requestMetadata"] = dict(metadata)
    _merge(body, route.extra_args)
    return body


def invoke_body(request: Mapping[str, Any], route: Route) -> dict[str, Any]:
    """The body of ``invoke-with-response-stream`` for Anthropic's models: the Messages API's, without ``model`` and ``stream``, with
    the version Bedrock asks for."""
    body = anthropic_body(request, route)
    body.pop("model", None)
    body.pop("stream", None)
    return {"anthropic_version": ANTHROPIC_VERSION, **body}


# ---------------------------------------------------------------------- the event stream


@dataclass(slots=True)
class Frame:
    headers: dict[str, Any] = field(default_factory=dict)
    payload: bytes = b""

    @property
    def kind(self) -> str:
        """``event`` | ``exception`` | ``error``."""
        return str(self.headers.get(":message-type") or "event")

    @property
    def name(self) -> str:
        return str(self.headers.get(":event-type") or self.headers.get(":exception-type") or self.headers.get(":error-code") or "")


def _header_value(data: bytes, at: int, kind: int) -> tuple[Any, int]:
    if kind == 0:
        return True, at
    if kind == 1:
        return False, at
    fixed = {2: 1, 3: 2, 4: 4, 5: 8, 8: 8, 9: 16}
    if kind in fixed:
        raw = data[at : at + fixed[kind]]
        if kind in (2, 3, 4, 5):
            return int.from_bytes(raw, "big", signed=True), at + fixed[kind]
        return raw, at + fixed[kind]
    if kind in (6, 7):
        (length,) = struct.unpack_from(">H", data, at)
        raw = data[at + 2 : at + 2 + length]
        return (raw if kind == 6 else raw.decode("utf-8", "replace")), at + 2 + length
    raise ValueError(f"unknown header type {kind}")


def parse_frame(data: bytes) -> Frame:
    """One complete frame (all of its bytes). Raises ``ChatGPTError`` if a CRC or a length is wrong."""
    try:
        total, header_length, prelude_crc = struct.unpack_from(">III", data, 0)
        if zlib.crc32(data[:8]) != prelude_crc or zlib.crc32(data[:-4]) != struct.unpack_from(">I", data, len(data) - 4)[0]:
            raise ValueError("checksum")
        headers: dict[str, Any] = {}
        at, end = 12, 12 + header_length
        while at < end:
            name_length = data[at]
            name = data[at + 1 : at + 1 + name_length].decode("utf-8", "replace")
            kind = data[at + 1 + name_length]
            headers[name], at = _header_value(data, at + 2 + name_length, kind)
        return Frame(headers, data[end : total - 4])
    except (ValueError, struct.error, IndexError):
        raise ChatGPTError("Bedrock 的响应流损坏了（帧校验失败）。", code="stream") from None


async def frames(chunks: AsyncIterator[bytes]) -> AsyncIterator[Frame]:
    """The frames of an event stream, as the bytes that carry them arrive."""
    buffer = bytearray()
    async for piece in chunks:
        buffer += piece
        while len(buffer) >= 12:
            (total,) = struct.unpack_from(">I", buffer, 0)
            if total < 16 or total > MAX_FRAME:
                raise ChatGPTError("Bedrock 的响应流损坏了（帧长度不对）。", code="stream")
            if len(buffer) < total:
                break
            frame = parse_frame(bytes(buffer[:total]))
            del buffer[:total]
            yield frame
    if buffer:
        raise ChatGPTError("The stream ended before the response was complete.", code="stream")


def failure_of(frame: Frame) -> ChatGPTError:
    """The error an ``exception`` / ``error`` frame carries."""
    payload = load_json(frame.payload.decode("utf-8", "replace")) or {}
    message = payload.get("message") if isinstance(payload.get("message"), str) else (frame.payload.decode("utf-8", "replace")[:300] or "The response failed.")
    name = frame.name
    return ChatGPTError(message, code=name or None, status=_EXCEPTION_STATUS.get(name), body=dict(payload) or None)


# ---------------------------------------------------------------------- the stream: Converse


_STOPS = {"max_tokens": "max_output_tokens", "content_filtered": "content_filter", "guardrail_intervened": "content_filter", "model_context_window_exceeded": "context_window"}


class _Block:
    def __init__(self, kind: str, **fields: Any) -> None:
        self.kind = kind
        self.text = ""
        self.signature = ""
        self.redacted = ""
        self.fields = fields


async def read_converse(chunks: AsyncIterator[bytes], origin: str = "") -> AsyncIterator[Event]:
    """The answer of ``converse-stream`` as events. Raises ``ChatGPTError`` for an exception frame and for a stream that ends early."""
    blocks: dict[int, _Block] = {}
    usage: Mapping[str, Any] = {}
    stop = ""
    stopped = False
    finished = False

    def block_at(index: int, kind: str) -> _Block:
        found = blocks.get(index)
        if found is None or found.kind != kind:
            found = blocks[index] = _Block(kind)
        return found

    async for frame in frames(chunks):
        if frame.kind != "event":
            raise failure_of(frame)
        payload = load_json(frame.payload.decode("utf-8", "replace"))
        if payload is None:
            log.warning("a stream event was not JSON: %.200s", frame.payload)
            continue
        name = frame.name
        index = payload.get("contentBlockIndex") if isinstance(payload.get("contentBlockIndex"), int) else 0
        if name == "contentBlockStart":
            start = payload.get("start") if isinstance(payload.get("start"), Mapping) else {}
            tool = start.get("toolUse") if isinstance(start.get("toolUse"), Mapping) else None
            if tool is not None:
                block = blocks[index] = _Block("tool", id=str(tool.get("toolUseId") or ""), name=str(tool.get("name") or ""))
                yield CallBegun(f"toolu_{index}", block.fields["id"], block.fields["name"])
        elif name == "contentBlockDelta":
            delta = payload.get("delta") if isinstance(payload.get("delta"), Mapping) else {}
            if isinstance(delta.get("text"), str):
                block_at(index, "text").text += delta["text"]
                yield TextDelta(f"msg_{index}", delta["text"])
            elif isinstance(delta.get("toolUse"), Mapping):
                found = blocks.get(index)
                if found is not None:
                    found.text += str(delta["toolUse"].get("input") or "")
            elif isinstance(delta.get("reasoningContent"), Mapping):
                piece = delta["reasoningContent"]
                found = block_at(index, "thinking")
                if isinstance(piece.get("text"), str):
                    found.text += piece["text"]
                    yield ReasoningDelta(f"rs_{index}", piece["text"])
                if isinstance(piece.get("signature"), str):
                    found.signature += piece["signature"]
                if isinstance(piece.get("redactedContent"), str):
                    found.redacted += piece["redactedContent"]
        elif name == "contentBlockStop":
            found = blocks.pop(index, None)
            if found is None:
                continue
            if found.kind == "text":
                yield TextDone(f"msg_{index}", found.text)
                yield ItemDone({"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": found.text}]})
            elif found.kind == "thinking":
                if found.redacted and not found.text:
                    yield ItemDone({"type": "reasoning", "summary": [], "wc_data": found.redacted, "wc_origin": origin})
                else:
                    yield ReasoningDone(f"rs_{index}", found.text)
                    item: dict[str, Any] = {"type": "reasoning", "summary": [{"type": "summary_text", "text": found.text}], "wc_reasoning": found.text, "wc_origin": origin}
                    if found.signature:
                        item["wc_signature"] = found.signature
                    yield ItemDone(item)
            elif found.kind == "tool":
                arguments = found.text or "{}"
                yield CallDone(f"toolu_{index}", found.fields["id"], found.fields["name"], arguments)
                yield ItemDone({"type": "function_call", "call_id": found.fields["id"], "name": found.fields["name"], "arguments": arguments})
        elif name == "messageStop":
            stop = str(payload.get("stopReason") or "")
            stopped = True
        elif name == "metadata":
            usage = payload.get("usage") if isinstance(payload.get("usage"), Mapping) else {}
            finished = True
    if not (stopped or finished):
        raise ChatGPTError("The stream ended before the response was complete.", code="stream")
    cached, written = count(usage.get("cacheReadInputTokens")), count(usage.get("cacheWriteInputTokens"))
    total = (
        usage_of(count(usage.get("inputTokens")) + cached + written, usage.get("outputTokens"), cached, 0, written if "cacheWriteInputTokens" in usage else None)
        if usage
        else None
    )
    yield Finished(total, "", _STOPS.get(stop, ""))


# ---------------------------------------------------------------------- the stream: InvokeModel (Anthropic's own format)


async def invoke_lines(chunks: AsyncIterator[bytes]) -> AsyncIterator[str]:
    """The events of ``invoke-with-response-stream`` as the server-sent-event lines the Anthropic reader takes: each ``chunk`` frame
    carries one event, JSON in base64."""
    async for frame in frames(chunks):
        if frame.kind != "event":
            raise failure_of(frame)
        payload = load_json(frame.payload.decode("utf-8", "replace")) or {}
        encoded = payload.get("bytes")
        if not isinstance(encoded, str):
            continue
        try:
            text = base64.b64decode(encoded).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            log.warning("a chunk was not base64 JSON")
            continue
        yield "data: " + text
        yield ""
