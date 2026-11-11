"""An imported conversation as a conversation of the ChatGPT Plan engine, saved like its own (``harness.store``).

Only the conversation is carried: each user message is a turn, the assistant's answers follow it as message items (its
tool calls are already lines in that text, ``base.Builder``), the items are the Responses API's own message items (what
``chatgpt.responses.user_message`` builds and what ``responses.history_item`` keeps of an answer). There is no
``function_call`` in it, so it is a history the next request can send as it is. The id comes from the source's key, so
importing the same conversation again gives the same id (the caller decides whether to replace or keep the first).
"""

from __future__ import annotations

import time
from collections.abc import Sequence

from desktop.core.chatgpt.responses import user_message
from desktop.core.harness.store import SavedSession, SavedTurn
from desktop.core.importers.base import LABELS, Message, SessionInfo, saved_id


def assistant_item(text: str) -> dict[str, object]:
    return {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": text, "annotations": []}]}


def to_saved_session(
    info: SessionInfo, messages: Sequence[Message], *, model: str, reasoning: str, autonomy: str, now: float | None = None
) -> SavedSession:
    """``model`` / ``reasoning`` / ``autonomy``: what the conversation goes on with. Each turn's ``model`` is the app it
    came from (the page shows it on the turn), its times those of its messages where known."""
    imported = time.time() if now is None else now
    origin_label = LABELS.get(info.app, info.app)
    started = info.started or next((m.at for m in messages if m.at), None) or imported
    items: list[dict[str, object]] = []
    turns: list[SavedTurn] = []
    clock = started
    for message in messages:
        text = message.text.strip()
        if not text or message.role not in ("user", "assistant"):
            continue
        at = message.at or clock
        if message.role == "user" or not turns:
            turns.append(SavedTurn(start=len(items), at=at, finished=at, status="done", model=origin_label))
        items.append(user_message(text) if message.role == "user" else assistant_item(text))
        clock = max(clock, at)
        turns[-1].finished = max(turns[-1].finished or at, at)
    return SavedSession(
        id=saved_id(info.key),
        project=info.project,
        title=info.title,
        model=model,
        reasoning=reasoning,
        autonomy=autonomy,
        created_at=started,
        modified_at=max(info.updated or 0.0, clock),
        items=items,
        turns=turns,
        origin={"app": info.app, "native": info.native_id, "at": imported},
    )
