"""ACP tool calls, prompts and statistics in the transcript's terms. Pure (no I/O); shared by the live controller and the
history replay.

Tool calls (``session/update`` ``tool_call`` / ``tool_call_update``; shapes seen on Devin 3000.11):

==================  ==========  =============================  ===========================================================
``inferenceToolName``  ``kind``  rawInput                       transcript
==================  ==========  =============================  ===========================================================
exec                execute     ``command``, ``timeout``       ``Execute``; the output arrives as cumulative text
                                                               snapshots, the exit code in ``_meta.terminal_exit``
read                read        ``file_path``                  ``Read``
write               edit        ``file_path``, ``content``     ``Create`` (a ``diff`` content without ``oldText``)
edit                edit        ``file_path``, ``old_string``  ``Edit`` (a ``diff`` content with ``oldText``)
grep                search      ``query``, ``path``            ``Grep``
find_file_by_name   search      ``query``, ``path``            ``Glob``
(others)            any         whatever                       named by ``inferenceToolName``, else the title
==================  ==========  =============================  ===========================================================
"""

from __future__ import annotations

import base64
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime, timezone
from typing import Any

from desktop.core.attachments import IMAGE, PDF, TEXT, Attachment, sniff_image
from desktop.core.codex.items import (
    APPLY_PATCH,
    CREATE,
    EDIT,
    EXECUTE,
    attachment_block,
    split_handoff,
    user_message_parts,
)
from desktop.core.diffs import FileDiff, diff_for_new_file, diff_from_contents
from desktop.core.transcript import ToolStatus
from desktop.core.usage import Tokens

TOOL_NAME_META = "cognition.ai/inferenceToolName"
TIMESTAMP_META = "cognition.ai/timestamp"
CLIENT_MESSAGE_META = "cognition.ai/clientMessageId"
SUBAGENT_META = "cognition.ai/subagent_context"
READ = "Read"
GREP = "Grep"
GLOB = "Glob"
FETCH = "FetchUrl"
SEARCH = "WebSearch"

__all__ = [
    "APPLY_PATCH",
    "CLIENT_MESSAGE_META",
    "EXECUTE",
    "SUBAGENT_META",
    "TIMESTAMP_META",
    "call_diffs",
    "call_status",
    "content_text",
    "native_name",
    "parse_time",
    "prompt_blocks",
    "split_handoff",
    "stats_tokens",
    "tool_spec",
    "usage_call",
    "user_text_and_attachments",
]


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _meta(update: Mapping[str, Any]) -> Mapping[str, Any]:
    meta = update.get("_meta")
    return meta if isinstance(meta, Mapping) else {}


def native_name(update: Mapping[str, Any]) -> str:
    """Devin's own name for the tool (``exec``): from the update's meta, else the id's prefix (``exec:0#…``)."""
    named = _text(_meta(update).get(TOOL_NAME_META))
    if named:
        return named
    return _text(update.get("toolCallId")).split(":", 1)[0] if ":" in _text(update.get("toolCallId")) else ""


def _path(update: Mapping[str, Any], raw: Mapping[str, Any]) -> str:
    for key in ("file_path", "path", "filePath"):
        if _text(raw.get(key)):
            return raw[key]
    for location in update.get("locations") or ():
        if isinstance(location, Mapping) and _text(location.get("path")):
            return location["path"]
    for content in update.get("content") or ():
        if isinstance(content, Mapping) and content.get("type") == "diff" and _text(content.get("path")):
            return content["path"]
    return ""


def call_diffs(contents: Any) -> tuple[FileDiff, ...]:
    """The ``diff`` entries of a tool call's content: a file without ``oldText`` is a new one."""
    diffs = []
    for item in contents or ():
        if not isinstance(item, Mapping) or item.get("type") != "diff" or not _text(item.get("path")):
            continue
        new = _text(item.get("newText"))
        if item.get("oldText") is None:
            diffs.append(diff_for_new_file(item["path"], new))
        else:
            diffs.append(diff_from_contents(item["path"], _text(item.get("oldText")), new, context=2, numbered=False))
    return tuple(diffs)


def tool_spec(update: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
    """``(tool name, input)`` for a ``tool_call`` update."""
    raw = dict(update["rawInput"]) if isinstance(update.get("rawInput"), Mapping) else {}
    native, kind = native_name(update), _text(update.get("kind"))
    path = _path(update, raw)
    if native == "exec" or kind == "execute":
        command = _text(raw.get("command"))
        if not command:
            for content in update.get("content") or ():
                body = content.get("content") if isinstance(content, Mapping) else None
                resource = body.get("resource") if isinstance(body, Mapping) else None
                if isinstance(resource, Mapping) and _text(resource.get("text")):
                    command = resource["text"]
                    break
        data: dict[str, Any] = {"command": command}
        if raw.get("timeout") is not None:
            data["timeout"] = raw["timeout"]
        return EXECUTE, data
    if native == "read" or kind == "read":
        return READ, {"file_path": path}
    if kind == "edit" or native in ("write", "edit", "multi_edit"):
        diffs = [c for c in update.get("content") or () if isinstance(c, Mapping) and c.get("type") == "diff"]
        creates = native == "write" or (bool(diffs) and all(d.get("oldText") is None for d in diffs))
        if len(diffs) > 1:
            return APPLY_PATCH, {"file_path": path, "files": [d["path"] for d in diffs if _text(d.get("path"))]}
        return (CREATE if creates else EDIT), {"file_path": path}
    if native == "find_file_by_name":
        return GLOB, {"pattern": _text(raw.get("query")) or _text(raw.get("pattern")), "path": _text(raw.get("path"))}
    if native == "grep" or kind == "search":
        return GREP, {"pattern": _text(raw.get("query")) or _text(raw.get("pattern")), "path": _text(raw.get("path"))}
    if kind == "fetch":
        return FETCH, {"url": _text(raw.get("url")) or _text(raw.get("query"))}
    name = native or _text(update.get("title")) or kind or "tool"
    return name, raw


def content_text(contents: Any) -> str:
    """What a tool call's content says in words: its text, the names of the files it found, a line per diff."""
    parts: list[str] = []
    for item in contents or ():
        if not isinstance(item, Mapping):
            continue
        if item.get("type") == "diff":
            mark = "A" if item.get("oldText") is None else "M"
            parts.append(f"{mark} {_text(item.get('path'))}")
            continue
        body = item.get("content")
        if not isinstance(body, Mapping):
            continue
        kind = body.get("type")
        if kind == "text" and _text(body.get("text")):
            parts.append(body["text"])
        elif kind == "resource_link":
            parts.append(_text(body.get("name")) or _text(body.get("uri")))
    return "\n".join(parts)


def call_status(status: Any) -> ToolStatus | None:
    """The terminal status of an update; ``None`` while the tool is still going."""
    if status == "completed":
        return ToolStatus.DONE
    if status == "failed":
        return ToolStatus.ERROR
    return None


# ---------------------------------------------------------------------------------------------- statistics


def _dimension(dimensions: Any, uid: str) -> float | None:
    for item in dimensions or ():
        if isinstance(item, Mapping) and item.get("uid") == uid:
            kind = item.get("kind")
            value = kind.get("value") if isinstance(kind, Mapping) else None
            return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None
    return None


def stats_tokens(params: Mapping[str, Any]) -> Tokens | None:
    """What one prompt cost, from ``_cognition.ai/turn_stats`` (``None`` when it carries no figures, as a stopped turn's
    does). ``input_tokens`` there is what was not read from the cache (verified: the call's ``inputTokens`` minus its
    ``cachedReadTokens``, summed over the turn's calls)."""
    dimensions = params.get("responseDimensions")
    read, write = _dimension(dimensions, "input_tokens"), _dimension(dimensions, "output_tokens")
    cached = _dimension(dimensions, "cached_input_tokens")
    if read is None and write is None and cached is None:
        return None
    return Tokens(int(read or 0), int(write or 0), None, int(cached or 0))


def usage_call(update: Mapping[str, Any]) -> tuple[Tokens | None, int | None, int | None]:
    """A ``usage_update`` is one model call: ``(its tokens, the context size after it, the window)``."""
    meta = _meta(update)

    def count(key: str) -> int | None:
        value = meta.get(key)
        return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0 else None

    read, write, cached = count("cognition.ai/inputTokens"), count("cognition.ai/outputTokens"), count("cognition.ai/cachedReadTokens")
    tokens = None
    if read is not None or write is not None:
        cached = cached or 0
        tokens = Tokens(max(0, (read or 0) - cached), write or 0, None, cached)
    used, size = update.get("used"), update.get("size")
    used = int(used) if isinstance(used, (int, float)) and not isinstance(used, bool) and used > 0 else None
    size = int(size) if isinstance(size, (int, float)) and not isinstance(size, bool) and size > 0 else None
    return tokens, used, size


def parse_time(value: Any) -> float | None:
    """``2026-11-01T14:14:55.301589500+00:00`` -> epoch seconds. Devin writes nanoseconds and sometimes a ``Z``; Python 3.11
    (the only version this runs on, ``_pyguard``) reads both, cutting the fraction at six digits. No zone: UTC."""
    if not isinstance(value, str) or not value:
        return None
    try:
        moment = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.timestamp()


# ---------------------------------------------------------------------------------------------- what the user sent


def prompt_blocks(text: str, attachments: Sequence[Attachment] = ()) -> list[dict[str, Any]]:
    """``session/prompt.prompt``: the typed text first, then pictures (inline data) and files (their text)."""
    blocks: list[dict[str, Any]] = [{"type": "text", "text": text}]
    for attachment in attachments:
        if attachment.kind == IMAGE and attachment.data:
            media_type = attachment.media_type or sniff_image(attachment.data) or "image/png"
            blocks.append({"type": "image", "data": base64.b64encode(attachment.data).decode("ascii"), "mimeType": media_type})
        elif attachment.kind == TEXT:
            blocks.append({"type": "text", "text": attachment_block(attachment.name, "text/plain", attachment.text)})
        elif attachment.kind == PDF:
            if not (attachment.parsed_text or "").strip():
                raise ValueError(f"PDF「{attachment.name}」里没有可读的文字")
            blocks.append({"type": "text", "text": attachment_block(attachment.name, "application/pdf", attachment.parsed_text or "")})
    return blocks


def user_text_and_attachments(parts: Iterable[Any]) -> tuple[str, list[Attachment], tuple[int, bool] | None]:
    """A saved user message (its ``user_message_chunk`` contents, in order): what was typed, the attachments, the handoff
    mark. The Codex reader does the work: Devin's pieces are put in its shape."""
    content: list[dict[str, Any]] = []
    for part in parts:
        if not isinstance(part, Mapping):
            continue
        if part.get("type") == "text":
            content.append({"type": "text", "text": _text(part.get("text"))})
        elif part.get("type") == "image" and _text(part.get("data")):
            content.append({"type": "image", "url": f"data:{_text(part.get('mimeType')) or 'image/png'};base64,{part['data']}"})
    return user_message_parts({"content": content})
