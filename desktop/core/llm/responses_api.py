"""OpenAI's Responses API with an API key: the ChatGPT plan's language (``chatgpt.responses``) on ``POST {base}/responses``.

The plan route's request is already this one; what an API key route changes is what the model accepts. A model without reasoning
levels refuses the ``reasoning`` and ``include`` fields, so they are sent only when the model has a level; the answer's room
(``max_output_tokens``) is the API's own. The stream is read by ``chatgpt.responses.read_events``.

On the official endpoint a GPT-6.2+ model appends top-level ``instructions`` after ``input`` and bills them uncached, so they go
as the first developer message there, like the plan route sends them. ``reasoningArgs``/``extraArgs`` keep the final word: the
model the body finally asks for decides, and an entry naming ``instructions`` or ``input`` itself is left alone, as is every
compatible gateway and older model.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from desktop.core.chatgpt.responses import BASE_URL, can_cache_developer_instructions, developer_message, for_responses
from desktop.core.llm.wire import RESPONSES, Route, apply_reasoning_args, text_of


def _official(route: Route) -> bool:
    """``POST https://api.openai.com/v1/responses`` exactly: where the GPT-6.2 billing of top-level instructions is
    documented. A compatible gateway keeps the documented API shape as it is."""
    return route.kind == RESPONSES and route.base_url.rstrip("/") == BASE_URL


def _override_fields(request: Mapping[str, Any], route: Route) -> set[str]:
    """The body fields the user's ``reasoningArgs`` (for this effort, resolved like ``apply_reasoning_args``) and
    ``extraArgs`` set or remove."""
    reasoning = request.get("reasoning")
    effort = request.get("wc_reasoning_effort") or (reasoning.get("effort") if isinstance(reasoning, Mapping) else None)
    return set(route.reasoning_args.get(effort, ())) | set(route.extra_args)


def _cacheable_instructions(body: dict[str, Any], request: Mapping[str, Any], route: Route) -> None:
    """Move top-level ``instructions`` into the first developer message where they are billed uncached otherwise
    (official endpoint, the GPT-6.2+ model the body finally asks for). An override naming ``instructions`` or ``input``
    keeps the final word, so the body is left alone rather than made to duplicate or resurrect them."""
    model = body.get("model")
    if not (_official(route) and isinstance(model, str) and can_cache_developer_instructions(model)):
        return
    if _override_fields(request, route) & {"input", "instructions"}:
        return
    inputs = body.get("input")
    if not isinstance(inputs, list):
        return
    instructions = body.pop("instructions", "")
    if not isinstance(instructions, str) or not instructions:
        return
    first = inputs[0] if inputs else None
    if not (isinstance(first, Mapping) and first.get("role") == "developer" and text_of(first) == instructions):
        inputs.insert(0, developer_message(instructions))


def responses_body(request: Mapping[str, Any], route: Route) -> dict[str, Any]:
    """The JSON body for the route, from the loop's request."""
    body = dict(request)
    body.pop("wc_reasoning_effort", None)
    body.pop("wc_session", None)
    body["model"] = route.model
    body["input"] = for_responses(body.get("input") or (), supports_images=route.supports_images)
    reasoning = body.get("reasoning")
    if not (isinstance(reasoning, Mapping) and reasoning.get("effort")):
        body.pop("reasoning", None)
        body.pop("include", None)
    if route.max_output:
        body["max_output_tokens"] = route.max_output
    apply_reasoning_args(body, request, route)
    body.update(route.extra_args)
    _cacheable_instructions(body, request, route)
    return body
