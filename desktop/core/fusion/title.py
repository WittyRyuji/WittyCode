"""One optional, tool-free title request after the main turn has spent its budget."""
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
from desktop.core.transcript import Turn

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


async def generate(controller: ChatGPTController, turn: Turn, question: str, model: str, account: Any,
                   allowed: Callable[[], bool]) -> None:
    fusion = controller._fusion
    origin = controller.saved.origin
    if (fusion is None or fusion._active or fusion._delegations or ATTEMPT in origin or account is None
            or not allowed() or source(controller, model) != account):
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
        stream = AcpSummary(controller.manager, before_prompt=lambda: allowed() and source(controller, model) == account).stream
    else:
        stream = controller.manager.stream

    # A restart in the middle must neither repeat the model call nor assume it was free.
    origin[ATTEMPT] = "pending"
    if not controller._save_now():
        origin.pop(ATTEMPT)
        return
    text, finished, tool_call, requested = "", None, False, not native
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
    except Exception as error:
        if isinstance(error, RequestNotSent):
            requested = False  # A proven local preparation failure cannot have consumed model tokens.
        log.info("Fusion title request did not complete: %s", type(error).__name__)
        return
    finally:
        origin[ATTEMPT] = "attempted"
        usage = finished.usage if finished is not None else None
        if requested:
            if usage is not None:
                controller._add_usage(usage, measures_context=False, counts_speed=False, model=model)
            controller.saved.requests.append({"kind": "title", "model": model, "usage": usage.tokens().to_json() if usage is not None else None})
            del controller.saved.requests[:-MAX_RECORDS]
            if usage is None:
                fusion.registry.unmetered = fusion.registry.had_unknown = True
                controller.saved.fusion = fusion.registry.to_json()
    # The controller checkpoints the title, attempt and usage when releasing the turn's request slot.
    if finished is not None and not finished.incomplete and not tool_call and controller._turn is turn and allowed():
        title = session_title.clean(text)
        if title and title not in ("新会话", "（无标题）"):
            controller.saved.title = title
            controller._notify("fusion_title")
