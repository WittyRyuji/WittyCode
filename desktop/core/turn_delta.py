"""A live turn update reuses unchanged blocks already acknowledged by the page's transport state."""
from __future__ import annotations

from typing import Any


def _large(value: Any) -> bool:
    if isinstance(value, str):
        return len(value) >= 4096
    if isinstance(value, dict):
        return any(_large(item) for item in value.values())
    if isinstance(value, list):
        return any(_large(item) for item in value)
    return False


def changed_blocks(current: dict[str, Any], previous: dict[str, Any] | None) -> dict[str, Any]:
    if previous is None:
        return current
    old = {block["id"]: block for block in previous["blocks"]}
    return {**current, "blocks": [
        {"id": block["id"], "kind": "unchanged"} if old.get(block["id"]) == block and _large(block) else block
        for block in current["blocks"]
    ]}
