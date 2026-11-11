"""One small request to the ChatGPT plan that is not part of any conversation: nothing is sent to the user's
conversation, nothing is stored (``store: false``), nothing is left in a list. The memory curator uses it to ask what a
turn holds that is worth keeping."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from desktop.core.chatgpt.responses import TextDelta, TextDone, build_request, user_message

if TYPE_CHECKING:
    from desktop.core.chatgpt.engine import ChatGPTEngine

log = logging.getLogger(__name__)

PREFERRED_MODEL = "gpt-6-luna"
"""The fast model of the plan (``models.FALLBACK_MODELS``): a one-off question does not need the workhorse."""
SYSTEM = "You are a careful assistant. Follow the instructions in the user message exactly and answer in the requested format."
SECONDS = 90.0


async def ask(engine: ChatGPTEngine, prompt: str, *, timeout: float = SECONDS) -> str | None:
    """The model's answer to ``prompt``, or ``None`` when the plan cannot be used now (not signed in, no permission, an
    error: the reason is only logged, it is not the user's turn that failed)."""
    account = engine.cached_account()
    if account.get("auth") != "signed-in" or not account.get("planEnabled"):
        return None
    try:
        models = await engine.list_models()
        model = next((m for m in models if m.id == PREFERRED_MODEL), None) or (models[0] if models else None)
        if model is None:
            return None
        effort = "low" if "low" in model.efforts else None
        request = build_request(model=model.id, instructions=SYSTEM, items=[user_message(prompt)], tools=[], reasoning=effort, shape=engine.shape)
        text = ""
        async with asyncio.timeout(timeout):
            async for event in engine.stream(request):
                if isinstance(event, TextDelta):
                    text += event.text
                elif isinstance(event, TextDone):
                    text = event.text or text
        return text.strip() or None
    except asyncio.CancelledError:
        raise
    except Exception as error:
        log.info("the ChatGPT side request failed: %s", error)
        return None
