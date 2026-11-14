"""Provider-neutral history validation shared by the loop, recovery and wire adapters."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any


def message_text(item: Mapping[str, Any]) -> str:
    """Refusals are visible output, not empty replies; preserve their text across adapters, summary and restart."""
    parts = []
    for part in item.get("content") or ():
        if not isinstance(part, Mapping) or part.get("type") not in ("input_text", "output_text", "text", "refusal"):
            continue
        value = part.get("refusal") if part.get("type") == "refusal" else part.get("text")
        if isinstance(value, str):
            parts.append(value)
    return "".join(parts)


def orphan_reasoning(items: Sequence[Mapping[str, Any]]) -> set[int]:
    """Reasoning needs a following assistant output in the same response, not a later user's turn.

    Do not invent text or signatures to repair it. Dropping an unpaired thought cannot replay a side effect; complete
    calls and their outputs are preserved. Consecutive thinking blocks may all belong to one following answer/call.
    """
    orphaned: set[int] = set()
    linked = False
    for index in range(len(items) - 1, -1, -1):
        item = items[index]
        kind = item.get("type")
        if kind == "reasoning":
            if not linked:
                orphaned.add(index)
        elif kind == "function_call":
            linked = True
        elif kind == "message" and item.get("role") == "assistant":
            linked = bool(message_text(item).strip())
        else:
            linked = False
    return orphaned


def replayable(items: Sequence[Mapping[str, Any]]) -> list[Any]:
    dropped = orphan_reasoning(items)
    return [item for index, item in enumerate(items) if index not in dropped]
