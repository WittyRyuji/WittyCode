"""What every adapter shares: the route a request takes, server-sent events, and small helpers for the items."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from desktop.core.chatgpt.responses import Usage

CHAT = "chat"
ANTHROPIC = "anthropic"
RESPONSES = "responses"
KINDS = (CHAT, ANTHROPIC, RESPONSES)

DEFAULT_MAX_OUTPUT = 32_000
"""The answer's room when the model's entry gives none, where the API insists on a number (Anthropic)."""


@dataclass(frozen=True, slots=True)
class Route:
    """Where one request goes and how it is signed. Built per request: the key is read when the request is made."""

    kind: str
    base_url: str
    model: str
    """The id the API knows (not the application's ``api:...`` id)."""
    headers: Mapping[str, str] = field(default_factory=dict)
    """Authentication and the user's own headers, ready to send."""
    extra_args: Mapping[str, Any] = field(default_factory=dict)
    """The user's ``extraArgs``: merged into the request body last, so they have the final word."""
    max_output: int | None = None
    thinking: int | None = None
    """The thought budget (tokens) of an entry that turned extended thinking on itself (``enableThinking``); ``None``: the level decides."""

    @property
    def origin(self) -> str:
        """Who produced what a ``reasoning`` item carries: the host. Only a route of the same origin gets it back."""
        return urlsplit(self.base_url).netloc.lower()

    @property
    def url(self) -> str:
        base = self.base_url.rstrip("/")
        if self.kind == ANTHROPIC:
            return base + ("/messages" if base.endswith("/v1") else "/v1/messages")
        return base + ("/chat/completions" if self.kind == CHAT else "/responses")


async def sse_data(lines: AsyncIterator[str]) -> AsyncIterator[str]:
    """The ``data:`` payload of each server-sent event (its lines joined), in order. Comments, ``event:``, ``id:`` and ``retry:``
    carry nothing the payload does not say itself."""
    data: list[str] = []
    async for line in lines:
        if line == "":
            if data:
                yield "\n".join(data)
                data.clear()
        elif line.startswith("data:"):
            data.append(line[5:].lstrip(" "))
    if data:
        yield "\n".join(data)


def load_json(text: str) -> Mapping[str, Any] | None:
    try:
        value = json.loads(text)
    except ValueError:
        return None
    return value if isinstance(value, Mapping) else None


def count(value: Any) -> int:
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0 else 0


def text_of(item: Mapping[str, Any]) -> str:
    """The words of a message item (user or assistant)."""
    return "".join(
        part.get("text", "")
        for part in item.get("content") or ()
        if isinstance(part, Mapping) and part.get("type") in ("input_text", "output_text", "text") and isinstance(part.get("text"), str)
    )


def images_of(item: Mapping[str, Any]) -> list[str]:
    """The data URLs of the pictures in a user message."""
    return [
        part["image_url"]
        for part in item.get("content") or ()
        if isinstance(part, Mapping) and part.get("type") == "input_image" and isinstance(part.get("image_url"), str)
    ]


def split_data_url(url: str) -> tuple[str, str] | None:
    """``data:image/png;base64,AAAA`` -> ``("image/png", "AAAA")``; ``None`` for anything else."""
    if not url.startswith("data:") or ";base64," not in url:
        return None
    head, _, payload = url.partition(";base64,")
    return head[5:] or "application/octet-stream", payload


def usage_of(input_tokens: Any, output_tokens: Any, cached: Any = 0, reasoning: Any = 0, written: Any = None) -> Usage:
    return Usage(count(input_tokens), count(cached), count(output_tokens), count(reasoning), None if written is None else count(written))


def declared_functions(tools: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """The ``{name, description, parameters}`` of every function in a request's ``tools`` (a namespace is looked into)."""
    found: list[Mapping[str, Any]] = []
    for tool in tools:
        if tool.get("type") == "namespace":
            found += declared_functions(tool.get("tools") or ())
        elif tool.get("name"):
            found.append(tool)
    return found
