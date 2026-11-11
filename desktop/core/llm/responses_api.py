"""OpenAI's Responses API with an API key: the ChatGPT plan's language (``chatgpt.responses``) on ``POST {base}/responses``.

The plan route's request is already this one; what an API key route changes is what the model accepts. A model without reasoning
levels refuses the ``reasoning`` and ``include`` fields, so they are sent only when the model has a level; the answer's room
(``max_output_tokens``) is the API's own. The stream is read by ``chatgpt.responses.read_events``.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from desktop.core.chatgpt.responses import for_responses
from desktop.core.llm.wire import Route


def responses_body(request: Mapping[str, Any], route: Route) -> dict[str, Any]:
    """The JSON body for the route, from the loop's request."""
    body = dict(request)
    body["model"] = route.model
    body["input"] = for_responses(body.get("input") or ())
    reasoning = body.get("reasoning")
    if not (isinstance(reasoning, Mapping) and reasoning.get("effort")):
        body.pop("reasoning", None)
        body.pop("include", None)
    if route.max_output:
        body["max_output_tokens"] = route.max_output
    body.update(route.extra_args)
    return body
