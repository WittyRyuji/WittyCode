"""Provider-neutral history validation shared by the loop, recovery and wire adapters."""
from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
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


MAX_TOOL_IMAGES = 8
EXPIRED_TOOL_IMAGES = "[picture no longer attached: read the file again if needed]"
NO_TOOL_IMAGES = "[picture not attached: the current model does not support images]"


def tool_images_of(item: Mapping[str, Any]) -> list[Mapping[str, str]]:
    if item.get("type") != "function_call_output" or not isinstance(item.get("wc_images"), list):
        return []
    return [image for image in item["wc_images"] if isinstance(image, Mapping)
            and isinstance(image.get("media_type"), str) and isinstance(image.get("data"), str) and image["data"]]


def _tool_image_updates(items: Sequence[Mapping[str, Any]]) -> Iterator[tuple[int, dict[str, Any]]]:
    remaining = MAX_TOOL_IMAGES
    for index in range(len(items) - 1, -1, -1):
        item = items[index]
        pictures = tool_images_of(item)
        if len(pictures) > remaining:
            replacement = dict(item)
            if remaining:
                replacement["wc_images"] = pictures[-remaining:]
            else:
                del replacement["wc_images"]
            text = str(item.get("output", ""))
            if not text.endswith(EXPIRED_TOOL_IMAGES):
                replacement["output"] = f"{text}\n{EXPIRED_TOOL_IMAGES}"
            yield index, replacement
        remaining = max(0, remaining - len(pictures))


def bounded_tool_images(items: Sequence[Mapping[str, Any]]) -> Sequence[Mapping[str, Any]]:
    """A bounded view without changing the caller's history."""
    updates = dict(_tool_image_updates(items))
    return [updates.get(index, item) for index, item in enumerate(items)] if updates else items


def prune_tool_images(items: list[dict[str, Any]]) -> int | None:
    """Drop old pictures in place (the active view shares items with the log). Return the first changed index."""
    first = None
    for index, replacement in _tool_image_updates(items):
        items[index].clear()
        items[index].update(replacement)
        first = index
    return first


def tool_output_text(item: Mapping[str, Any], supports_images: bool) -> str:
    output = str(item.get("output", ""))
    return output if supports_images or not tool_images_of(item) else f"{output}\n{NO_TOOL_IMAGES}"


def with_tool_picture_messages(items: Sequence[Mapping[str, Any]], supports_images: bool) -> Iterator[Mapping[str, Any]]:
    """At the wire only: place tool pictures after all the contiguous outputs of their batch."""
    from desktop.core.chatgpt.responses import user_message

    labels: list[str] = []
    urls: list[str] = []
    for item in bounded_tool_images(items):
        if item.get("type") != "function_call_output":
            if urls:
                yield user_message("Pictures from tools:\n" + "\n".join(labels), urls)
                labels, urls = [], []
            yield item
            continue
        pictures = tool_images_of(item)
        if pictures and supports_images:
            labels.append(str(item.get("output", "")).partition("\n")[0])
            urls.extend(f"data:{image['media_type']};base64,{image['data']}" for image in pictures)
        elif pictures:
            item = {**item, "output": tool_output_text(item, supports_images)}
        yield item
    if urls:
        yield user_message("Pictures from tools:\n" + "\n".join(labels), urls)
