"""How full a session's context is: what Droid reports about it and how that is put into words.

Three numbers, all in tokens:

- ``used``: Droid's own record of the last model call it made for the conversation (input, cache reads and
  output), the figure it compares with the compaction budget. It is kept with the saved session, so a resumed
  session has it before its first new call. ``None`` until a call has been recorded and right after a compaction.
  The last call can be a small side call (the session title, made alongside the first turn): a figure too small
  for a call of the conversation is not taken (``plausible``), and each call's own figure is taken from the
  ``session_token_usage_changed`` notification, so a quick conversation call is not lost behind a later side call.
  ``Session.context().used`` is not used: it estimates the fixed prompt only and does not follow the conversation.
- ``window``: the model's context window (``Session.context().limit``); it follows model changes.
- ``budget``: where Droid compacts the conversation by itself (``get_context_breakdown().context_budget``):
  250 000, or the window when that is smaller, unless ``compaction_token_limit`` is set.

The breakdown request is not part of the high-level ``Session`` API: it is reached through ``Session._require_client()``.
Everything that depends on that private door is in ``read_context``; without it only the denominator is known.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from typing import Any

log = logging.getLogger("desktop.context")


@dataclass(frozen=True, slots=True)
class ContextState:
    used: int | None = None
    window: int | None = None
    budget: int | None = None
    model: str = ""
    compacted: bool = False
    """The conversation was compacted and no model call has measured the result yet (the reason ``used`` is unknown)."""
    fixed: int | None = field(default=None, compare=False)
    """Droid's estimate of what every call of the conversation carries before any message (system prompt, tools,
    AGENTS.md, skills). Not shown; it tells a conversation call from a small side call (see ``plausible``)."""


def _tokens(value: Any) -> int | None:
    """A positive whole number of tokens, anything else (missing, zero, garbage) counts as unknown."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value) if value > 0 else None


async def read_context(session: Any) -> ContextState:
    """Ask a live session how full its context is (two small requests to its droid process).

    Raises what the session raises when it is closed or its process is gone; a missing breakdown only leaves
    ``used`` and ``budget`` unknown.
    """
    stats = await session.context()
    state = ContextState(window=_tokens(stats.limit))
    try:
        breakdown = await session._require_client().get_context_breakdown()
    except Exception as error:
        log.debug("no context breakdown: %s: %s", type(error).__name__, error)
        return state
    return replace(
        state,
        used=_tokens(breakdown.last_call_compaction_tokens),
        budget=_tokens(breakdown.context_budget),
        model=str(breakdown.model_id or ""),
        fixed=_fixed_part(breakdown),
    )


def _fixed_part(breakdown: Any) -> int | None:
    """The breakdown's estimate without the messages: what a call carries before the conversation itself."""
    total = _tokens(getattr(breakdown, "used_tokens", None))
    if total is None:
        return None
    messages = 0
    for category in getattr(breakdown, "categories", None) or ():
        if getattr(category, "color_key", None) == "messages" or getattr(category, "name", None) == "Messages":
            messages += _tokens(getattr(category, "tokens", None)) or 0
    return _tokens(total - messages)


def last_call_figure(raw: Any) -> int | None:
    """The figure of one model call from a ``session_token_usage_changed`` notification's ``lastCallTokenUsage``.

    Droid adds input (cache writes are already in it), cache reads and output: the same figure as
    ``last_call_compaction_tokens``, but for this very call, which a later breakdown reading may no longer show.
    """
    if not isinstance(raw, Mapping):
        return None
    total = 0
    for key in ("inputTokens", "cacheReadTokens", "outputTokens"):
        value = raw.get(key, 0)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
            return None
        total += int(value)
    return total or None


def plausible(used: int, fixed: int | None) -> bool:
    """Whether a last-call figure can be a call of the conversation.

    Droid also makes small side calls, the session title above all, which runs alongside the first turn and often
    ends after it: its few hundred tokens would then stand as the conversation's size. Every conversation call
    carries at least the fixed part (estimated, hence the factor of two of slack); a side call carries far less.
    """
    return fixed is None or used * 2 >= fixed


def format_tokens(count: int) -> str:
    """``850``, ``2.5K``, ``25K``, ``872K``, ``1M`` (one decimal below 10K and in the millions, none in between)."""
    if count < 1000:
        return str(count)
    if count < 10_000:
        return f"{count / 1000:.1f}".removesuffix(".0") + "K"
    if count < 999_500:
        return f"{round(count / 1000)}K"
    return f"{count / 1_000_000:.1f}".removesuffix(".0") + "M"
