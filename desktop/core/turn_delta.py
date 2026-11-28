"""Ordered live updates use the last full view sent on this channel as their base."""
from __future__ import annotations

from typing import Any


def _fields(current: dict[str, Any], previous: dict[str, Any], append: str) -> dict[str, Any]:
    changed = {key: value for key, value in current.items() if key not in previous or previous[key] != value}
    removed = list(previous.keys() - current.keys())
    patch: dict[str, Any] = {}
    value, old = changed.get(append), previous.get(append)
    if isinstance(value, str) and isinstance(old, str) and old and value.startswith(old):
        patch["suffix"] = value[len(old):]
        del changed[append]
    if changed:
        patch["fields"] = changed
    if removed:
        patch["remove"] = sorted(removed)
    return patch


def _activity(current: dict[str, Any], previous: dict[str, Any]) -> dict[str, Any]:
    old = {entry["id"]: entry for entry in previous["entries"]}
    additions = []
    for entry in current["entries"]:
        before = old.get(entry["id"])
        if before == entry:
            continue
        if before is None or before["kind"] != entry["kind"]:
            additions.append(entry)
        else:
            key = "text" if entry["kind"] == "thinking" else "output"
            additions.append({"id": entry["id"], "kind": "entryPatch", **_fields(entry, before, key)})
    order = [entry["id"] for entry in current["entries"]]
    natural_order = [*old, *(entry["id"] for entry in additions if entry["id"] not in old)]
    patch = {"id": current["id"], "kind": "activityPatch", "entries": additions}
    if order != natural_order:
        patch["order"] = order
    return patch


def _block(current: dict[str, Any], previous: dict[str, Any] | None) -> dict[str, Any]:
    if previous is None or previous["kind"] != current["kind"]:
        return current
    ident = current["id"]
    if previous == current:
        return {"id": ident, "kind": "unchanged"}
    if current["kind"] == "text":
        return {"id": ident, "kind": "textPatch", **_fields(current, previous, "markdown")}
    if current["kind"] == "activity":
        return _activity(current, previous)
    return current


def _large(value: Any) -> bool:
    if isinstance(value, str):
        return len(value) >= 4096
    if isinstance(value, dict):
        return any(_large(item) for item in value.values())
    return isinstance(value, list) and any(_large(item) for item in value)


def changed_blocks(current: dict[str, Any], previous: dict[str, Any] | None, *, legacy: bool = False) -> dict[str, Any]:
    if previous is None:
        return current
    old = {block["id"]: block for block in previous["blocks"]}
    if legacy:
        return {**current, "blocks": [
            {"id": block["id"], "kind": "unchanged"} if old.get(block["id"]) == block and _large(block) else block
            for block in current["blocks"]
        ]}
    return {**current, "blocks": [_block(block, old.get(block["id"])) for block in current["blocks"]]}
