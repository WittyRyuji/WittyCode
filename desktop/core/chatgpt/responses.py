"""The Responses API as the ChatGPT plan allows it: the request we send and the stream we read.

The plan route (``POST https://api.openai.com/v1/responses`` with the OAuth access token) is stateless: ``store`` is false,
``stream`` is true, the whole history goes in ``input`` every time, and a list of request fields is refused outright
(``docs/chatgpt-plan-harness.md`` section 2). Output items are replayed intact, including their ids, status and assistant
phase; an output id is not an item reference when the complete item is supplied. A Responses ``reasoning`` item needs
its ``encrypted_content`` (``include`` asks for it); another provider's signed thoughts are kept for that adapter.

``Shape`` holds the choices that depend on what the service accepts and that have not been verified on a real account yet
(flat or namespaced tools, ``include``, the reasoning summary): they are the first thing to change if a request is refused.

The stream is read into neutral events (``TextDelta`` …) so the loop does not know server-sent events. A failed response
(``response.failed`` / ``error``) raises ``ChatGPTError`` with the server's code, so the usage-limit codes reach the user.
"""

from __future__ import annotations

import copy
import json
import logging
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from desktop.core.chatgpt.errors import ChatGPTError
from desktop.core.usage import Tokens

log = logging.getLogger("desktop.chatgpt")

BASE_URL = "https://api.openai.com/v1"
NAMESPACE = "wittycode"


@dataclass(frozen=True, slots=True)
class Shape:
    """What the request looks like where the service's rules are not documented precisely."""

    flat_tools: bool = True
    """Tools as plain top-level ``function`` entries (what Codex sends for its own tools); else grouped in a namespace."""
    encrypted_reasoning: bool = True
    """Ask for ``reasoning.encrypted_content`` so reasoning items can be sent back (needed with ``store: false``)."""
    reasoning_summary: str | None = "auto"
    """``reasoning.summary``: the readable summary shown while the model thinks (``None``: do not ask)."""
    parallel_tool_calls: bool = True
    cache_key: bool = False
    """Optional cache-accounting isolation. GPT-5.6+ routes automatically; a changing key can prevent reuse."""


@dataclass(frozen=True, slots=True)
class Usage:
    input_tokens: int = 0
    """All input tokens of the request, cached ones included."""
    cached_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    cache_write_tokens: int | None = None
    """Input tokens written into the cache, when reported; included in ``input_tokens``."""

    @classmethod
    def of(cls, raw: Any) -> Usage | None:
        if not isinstance(raw, Mapping):
            return None
        details_in = raw.get("input_tokens_details")
        details_out = raw.get("output_tokens_details")
        return cls(
            _count(raw.get("input_tokens")),
            _count(details_in.get("cached_tokens")) if isinstance(details_in, Mapping) else 0,
            _count(raw.get("output_tokens")),
            _count(details_out.get("reasoning_tokens")) if isinstance(details_out, Mapping) else 0,
            _count(details_in["cache_write_tokens"]) if isinstance(details_in, Mapping) and "cache_write_tokens" in details_in else None,
        )

    def tokens(self) -> Tokens:
        """The page's disjoint counters: ordinary input, output, cache write and cache read."""
        written = self.cache_write_tokens or 0
        return Tokens(max(self.input_tokens - self.cached_tokens - written, 0), self.output_tokens, self.cache_write_tokens, self.cached_tokens)

    @property
    def context(self) -> int:
        """What the next request starts from: this request's input and the answer to it."""
        return self.input_tokens + self.output_tokens


def _count(value: Any) -> int:
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0 else 0


# ---------------------------------------------------------------------- events


@dataclass(frozen=True, slots=True)
class TextDelta:
    item_id: str
    text: str


@dataclass(frozen=True, slots=True)
class TextDone:
    item_id: str
    text: str


@dataclass(frozen=True, slots=True)
class ReasoningDelta:
    item_id: str
    text: str


@dataclass(frozen=True, slots=True)
class ReasoningDone:
    item_id: str
    text: str


@dataclass(frozen=True, slots=True)
class CallBegun:
    item_id: str
    call_id: str
    name: str


@dataclass(frozen=True, slots=True)
class CallDone:
    item_id: str
    call_id: str
    name: str
    arguments: str
    """The JSON text the model wrote (it may be invalid: the loop tells the model so)."""


@dataclass(frozen=True, slots=True)
class ItemDone:
    """A finished output item exactly as the server sent it."""

    item: dict[str, Any]


@dataclass(frozen=True, slots=True)
class Finished:
    usage: Usage | None
    response_id: str = ""
    incomplete: str = ""
    """Why the answer stopped short (``max_output_tokens``, ``content_filter``); empty: it completed."""
    diagnostics: dict[str, Any] = field(default_factory=dict)
    """Response-level diagnostics, kept separate from replayable output items."""


@dataclass(frozen=True, slots=True)
class Started:
    """The server acknowledged this request: a later stream failure must not resend it blindly."""


Event = Started | TextDelta | TextDone | ReasoningDelta | ReasoningDone | CallBegun | CallDone | ItemDone | Finished


# ---------------------------------------------------------------------- the request


def function_tool(name: str, description: str, parameters: Mapping[str, Any]) -> dict[str, Any]:
    return {"type": "function", "name": name, "description": description, "parameters": dict(parameters)}


def declare_tools(tools: Sequence[Mapping[str, Any]], shape: Shape) -> list[dict[str, Any]]:
    """``tools`` are ``{name, description, parameters}``; the request's ``tools`` array for them."""
    functions = [function_tool(t["name"], t.get("description", ""), t.get("parameters") or {"type": "object", "properties": {}}) for t in tools]
    if shape.flat_tools or not functions:
        return functions
    return [{"type": "namespace", "name": NAMESPACE, "description": "Tools for working in the user's project.", "tools": functions}]


def build_request(
    *,
    model: str,
    instructions: str,
    items: Sequence[Mapping[str, Any]],
    tools: Sequence[Mapping[str, Any]],
    reasoning: str | None,
    shape: Shape = Shape(),
    conversation: str = "",
) -> dict[str, Any]:
    request: dict[str, Any] = {
        "model": model,
        "instructions": instructions,
        "input": [dict(item) for item in items],
        "store": False,
        "stream": True,
    }
    if tools:
        request["tools"] = declare_tools(tools, shape)
        request["tool_choice"] = "auto"
        request["parallel_tool_calls"] = shape.parallel_tool_calls
    options: dict[str, Any] = {}
    if reasoning and reasoning != "off":  # ("off" is a choice of the menu, not a level the API knows)
        options["effort"] = reasoning
    if shape.reasoning_summary:
        options["summary"] = shape.reasoning_summary
    if options:
        request["reasoning"] = options
    if reasoning == "off" and model.startswith("api:"):
        request["wc_reasoning_effort"] = "off"  # internal only; API adapters must not lose an explicit disable
    if shape.encrypted_reasoning:
        request["include"] = ["reasoning.encrypted_content"]
    if shape.cache_key and conversation:
        request["prompt_cache_key"] = conversation
    return request


def history_item(item: Mapping[str, Any]) -> dict[str, Any] | None:
    """Replay complete output items, not id references. Do not rebuild them from a field whitelist.

    Unencrypted Responses reasoning cannot be replayed; adapter-local thoughts (including a signature with no visible
    text) remain available to their original provider. Transport cleanup belongs to ``for_responses``.
    """
    if item.get("type") == "reasoning" and not any(item.get(key) for key in ("encrypted_content", "wc_reasoning", "wc_signature", "wc_data")):
        return None
    return copy.deepcopy(dict(item)) if item.get("type") else None


def for_responses(items: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """The history as the Responses API takes it: a ``reasoning`` item that another API's adapter wrote (``wc_*``, no encrypted
    content) means nothing to it and is left out. The request the loop builds keeps those items (the adapters of the other APIs
    read them from it); whoever sends it to a Responses endpoint passes it through here."""
    sent: list[dict[str, Any]] = []
    for item in items:
        if item.get("type") == "reasoning" and not item.get("encrypted_content"):
            continue
        sent.append({key: value for key, value in item.items() if not key.startswith("wc_")})
    return sent


def user_message(text: str, images: Sequence[str] = ()) -> dict[str, Any]:
    """A user turn; ``images`` are data URLs."""
    content: list[dict[str, Any]] = [{"type": "input_text", "text": text}]
    content += [{"type": "input_image", "image_url": url} for url in images]
    return {"type": "message", "role": "user", "content": content}


def call_output(call_id: str, output: str) -> dict[str, Any]:
    return {"type": "function_call_output", "call_id": call_id, "output": output}


# ---------------------------------------------------------------------- the stream


def _message_text(item: Mapping[str, Any]) -> str:
    return "".join(part.get("text", "") for part in item.get("content") or () if isinstance(part, Mapping) and part.get("type") == "output_text")


def _summary_text(item: Mapping[str, Any]) -> str:
    return "\n\n".join(part.get("text", "") for part in item.get("summary") or () if isinstance(part, Mapping) and part.get("text"))


def _failure(response: Mapping[str, Any] | None, event: Mapping[str, Any]) -> ChatGPTError:
    error = (response or {}).get("error") if isinstance(response, Mapping) else None
    if not isinstance(error, Mapping):
        error = event.get("error") if isinstance(event.get("error"), Mapping) else event
    code = error.get("code") if isinstance(error.get("code"), str) else None
    message = error.get("message") if isinstance(error.get("message"), str) else "The response failed."
    param = error.get("param")
    if isinstance(param, str) and param:
        message = f"{message} (param: {param})"
    return ChatGPTError(message, code=code, body=dict(error))


async def read_events(lines: AsyncIterator[str]) -> AsyncIterator[Event]:
    """Server-sent events -> neutral events. Raises ``ChatGPTError`` for a failed response or a stream that ends early."""
    data: list[str] = []
    finished = False
    reasoning_parts: dict[str, dict[int, str]] = {}

    async def flush() -> list[Event]:
        nonlocal finished
        if not data:
            return []
        text = "\n".join(data)
        data.clear()
        if text.strip() == "[DONE]":
            return []
        try:
            event = json.loads(text)
        except ValueError:
            log.warning("a stream event was not JSON: %.200s", text)
            return []
        if not isinstance(event, Mapping):
            return []
        out = _translate(event, reasoning_parts)
        if any(isinstance(item, Finished) for item in out):
            finished = True
        return out

    async for line in lines:
        if line == "":
            for item in await flush():
                yield item
        elif line.startswith("data:"):
            data.append(line[5:].lstrip(" "))
        # (``event:``, ``id:``, ``retry:`` and comments carry nothing the payload does not say itself)
    for item in await flush():
        yield item
    if not finished:
        raise ChatGPTError("The stream ended before the response was complete.", code="stream", transient=False)


def _translate(event: Mapping[str, Any], reasoning_parts: dict[str, dict[int, str]]) -> list[Event]:
    kind = event.get("type")
    item = event.get("item") if isinstance(event.get("item"), Mapping) else {}
    item_id = str(event.get("item_id") or item.get("id") or "")
    if kind in ("response.created", "response.in_progress"):
        return [Started()]
    if kind == "response.output_text.delta":
        return [TextDelta(item_id, str(event.get("delta") or ""))]
    if kind in ("response.reasoning_summary_text.delta", "response.reasoning_text.delta"):
        index = event.get("summary_index") if isinstance(event.get("summary_index"), int) else 0
        parts = reasoning_parts.setdefault(item_id, {})
        parts[index] = parts.get(index, "") + str(event.get("delta") or "")
        return [ReasoningDelta(item_id, str(event.get("delta") or ""))]
    if kind == "response.output_item.added" and item.get("type") == "function_call":
        return [CallBegun(str(item.get("id") or ""), str(item.get("call_id") or ""), str(item.get("name") or ""))]
    if kind == "response.output_item.done":
        out: list[Event] = []
        item_kind = item.get("type")
        if item_kind == "message":
            out.append(TextDone(item_id, _message_text(item)))
        elif item_kind == "reasoning":
            text = _summary_text(item) or "\n\n".join(reasoning_parts.pop(item_id, {}).values())
            out.append(ReasoningDone(item_id, text))
        elif item_kind == "function_call":
            out.append(CallDone(item_id, str(item.get("call_id") or ""), str(item.get("name") or ""), str(item.get("arguments") or "")))
        out.append(ItemDone(dict(item)))
        return out
    if kind in ("response.completed", "response.incomplete"):
        response = event.get("response") if isinstance(event.get("response"), Mapping) else {}
        details = response.get("incomplete_details")
        reason = str(details.get("reason") or "incomplete") if isinstance(details, Mapping) else ("incomplete" if kind == "response.incomplete" else "")
        diagnostics = {key: copy.deepcopy(response[key]) for key in ("reasoning", "input_transformations", "prompt_cache_diagnostics") if key in response}
        return [Finished(Usage.of(response.get("usage")), str(response.get("id") or ""), reason, diagnostics)]
    if kind == "response.failed":
        response = event.get("response") if isinstance(event.get("response"), Mapping) else None
        raise _failure(response, event)
    if kind == "error":
        raise _failure(None, event)
    return []
