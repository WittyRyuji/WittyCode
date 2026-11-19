"""One optional, tool-free title request from the accepted prompt's main source."""
from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from desktop.core import session_title
from desktop.core.chatgpt.errors import RequestNotSent
from desktop.core.chatgpt.responses import CallBegun, CallDone, Finished, Started, TextDelta, TextDone, build_request, user_message
from desktop.core.fusion.registry import FusionError
from desktop.core.harness import compact
from desktop.core.harness.diagnostics import MAX_RECORDS
from desktop.core.usage import Tokens

if TYPE_CHECKING:
    from desktop.core.chatgpt.controller import ChatGPTController

log = logging.getLogger(__name__)
TIMEOUT = 12.0
ATTEMPT = "fusionTitleAttempt"


def source(controller: ChatGPTController, model: str) -> Any:
    """Only compare in memory; never persist an account or provider configuration with the title."""
    manager = controller.manager
    if model.startswith("devin:"):
        devin = manager.devin
        account = devin.cached_account() if devin is not None else {}
        return (devin, account.get("email")) if account.get("auth") == "signed-in" else None
    if model.startswith("api:"):
        return manager.api.spec(model) if manager.api is not None else None
    return manager.accounts.active_id()


async def generate(controller: ChatGPTController, question: str, model: str, account: Any,
                   allowed: Callable[[], bool]) -> None:
    fusion = controller._fusion
    origin = controller.saved.origin

    def still_allowed() -> bool:
        return (not controller._close_requested and controller._fusion is fusion and controller.model == model
                and allowed() and source(controller, model) == account)

    if (fusion is None or not controller.history_ready or ATTEMPT in origin or not still_allowed()
            or fusion.registry.policy.max_tokens and (fusion._active or fusion._delegations)):
        return
    items = [user_message(session_title.prompt(question))]
    try:
        fusion.check_budget(compact.estimate_items(items) + compact.estimate_text(session_title.SYSTEM))
    except FusionError:
        return

    native = model.startswith("devin:")
    request = build_request(model=model, instructions=session_title.SYSTEM, items=items, tools=[],
                            reasoning=controller._summary_effort(), shape=controller.manager.shape)
    if native:
        from desktop.core.fusion.summary import AcpSummary
        stream = AcpSummary(controller.manager, before_prompt=still_allowed).stream
    else:
        stream = controller.manager.stream

    # A restart in the middle must neither repeat the model call nor assume it was free.
    origin[ATTEMPT] = "pending"
    if not controller._save_now(title_only=True):
        origin.pop(ATTEMPT)
        return
    # Recheck immediately before entering the stream: it captures its route before its first await.
    if not still_allowed():
        origin.pop(ATTEMPT)
        controller._save_now(title_only=True)
        return
    text, finished, tool_call, requested = "", None, False, not native
    title = None
    try:
        async with asyncio.timeout(TIMEOUT), contextlib.aclosing(stream(request)) as events:
            async for event in events:
                if isinstance(event, Started):
                    requested = True
                elif isinstance(event, TextDelta):
                    text += event.text
                elif isinstance(event, TextDone):
                    text = event.text or text
                elif isinstance(event, (CallBegun, CallDone)):
                    tool_call = True  # There are no declarations and no executor on this path.
                elif isinstance(event, Finished):
                    finished = event
                    break
        if finished is not None and not finished.incomplete and not tool_call and still_allowed():
            title = session_title.clean(text)
    except Exception as error:
        if isinstance(error, RequestNotSent):
            requested = False  # A proven local preparation failure cannot have consumed model tokens.
        log.info("Fusion title request did not complete: %s", type(error).__name__)
    finally:
        origin[ATTEMPT] = "attempted"
        usage = finished.usage if finished is not None else None
        tokens = usage.tokens() if usage is not None else None
        if requested:
            if usage is not None:
                task = controller._fusion.measurement if controller._fusion is not None else None
                if task is not None:
                    # The turn's measurement uses the session counter; exclude this independent request.
                    task.main.before = (task.main.before or Tokens()).plus(tokens)
                controller._add_usage(usage, measures_context=False, counts_speed=False, model=model)
            controller.saved.requests.append({"kind": "title", "model": model, "usage": tokens.to_json() if tokens is not None else None})
            del controller.saved.requests[:-MAX_RECORDS]
            if usage is None:
                fusion.registry.unmetered = fusion.registry.had_unknown = True
                if fusion in (controller._fusion, controller._paused_fusion):
                    controller.saved.fusion.update(unmetered=True, hadUnknown=True)
        before = controller.saved.title
        if title and title not in ("新会话", "（无标题）"):
            controller.saved.title = title
        if controller._save_now(title_only=True):
            if controller.saved.title != before:
                controller._notify("fusion_title")
        else:
            controller.saved.title = before
