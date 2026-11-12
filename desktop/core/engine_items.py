"""Pieces of a conversation that more than one engine writes the same way: tool names, the exit-code marker after a
command's output, a text file as a block of its own after the typed text, a saved user message with its pictures and
files, and the mark of a conversation another engine handed over (``core/handoff``)."""

from __future__ import annotations

import base64
import binascii
import json
import re
from collections.abc import Mapping
from pathlib import PureWindowsPath
from typing import Any

from desktop.core.attachments import IMAGE, PDF, TEXT, Attachment, image_attachment, sniff_image
from desktop.core.context_envelope import strip as strip_context


EXECUTE = "Execute"


EDIT = "Edit"


CREATE = "Create"


APPLY_PATCH = "ApplyPatch"


ATTACHMENT_TAG = "attachment"


_ATTACHMENT_HEAD = re.compile(r'^<attachment name="([^"\n]*)" type="([^"\n]*)">\n')


HANDOFF_START = "WittyCode restored the earlier messages of this same conversation"


HANDOFF_JSON = "Earlier conversation (JSON):\n"


HANDOFF_END = "\n\nCurrent request:\n"


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _name(path: str) -> str:
    return PureWindowsPath(path).name or path


def command_result(item: Mapping[str, Any], output: str = "") -> str:
    """The output, followed by Droid's exit-code marker so the page shows a non-zero code the same way."""
    aggregated = item.get("aggregatedOutput")
    text = aggregated if isinstance(aggregated, str) else output
    code = item.get("exitCode")
    if isinstance(code, int) and not isinstance(code, bool):
        marker = f"[Process exited with code {code}]"
        return f"{text.rstrip()}\n\n{marker}" if text.strip() else marker
    return text


def attachment_block(name: str, media_type: str, body: str) -> str:
    """A text file (or a PDF's text) as one input item of its own, after the typed text."""
    safe = name.replace('"', "'").replace("\n", " ")
    return f'<{ATTACHMENT_TAG} name="{safe}" type="{media_type}">\n{body}\n</{ATTACHMENT_TAG}>'


def _picture(url: str, number: int) -> Attachment:
    name = f"图片 {number}"
    if url.startswith("data:") and ";base64," in url:
        head, _, payload = url.partition(";base64,")
        try:
            data = base64.b64decode(payload, validate=False)
        except (binascii.Error, ValueError):
            data = b""
        if data:
            return image_attachment(name, data, sniff_image(data) or head.removeprefix("data:"))
    return Attachment(kind=IMAGE, name=name)


def user_message_parts(item: Mapping[str, Any] | None) -> tuple[str, list[Attachment], tuple[int, bool] | None]:
    """A saved ``userMessage``: (what the user typed, attachments as chips/pictures, handoff mark)."""
    if not isinstance(item, Mapping):
        return "", [], None
    texts: list[str] = []
    attachments: list[Attachment] = []
    pictures = 0
    for part in item.get("content") or ():
        if not isinstance(part, Mapping):
            continue
        kind = part.get("type")
        if kind == "text":
            body = _text(part.get("text"))
            head = _ATTACHMENT_HEAD.match(body)
            if head and texts:
                media_type = head.group(2)
                attachments.append(Attachment(kind=PDF if media_type == "application/pdf" else TEXT, name=head.group(1) or "文档", media_type=media_type))
                continue
            texts.append(body)
        elif kind == "image":
            pictures += 1
            attachments.append(_picture(_text(part.get("url")), pictures))
        elif kind == "localImage":
            pictures += 1
            path = _text(part.get("path"))
            attachments.append(Attachment(kind=IMAGE, name=_name(path) or f"图片 {pictures}", path=path))
    typed, handoff = split_handoff("\n".join(texts))
    return typed, attachments, handoff


def split_handoff(text: str) -> tuple[str, tuple[int, bool] | None]:
    """A first message that carried another engine's conversation (``core/handoff``): the typed part and
    ``(carried, truncated)``; other text comes back as it is, except that the context WittyCode added after the typed text
    (``core/context_envelope``) is always taken out."""
    text = strip_context(text)
    if not text.startswith(HANDOFF_START):
        return text, None
    end = text.find(HANDOFF_END)
    if end < 0:
        return text, None
    head = text[:end]
    start = head.find(HANDOFF_JSON)
    carried = 0
    if start >= 0:
        try:
            entries = json.loads(head[start + len(HANDOFF_JSON) :])
        except ValueError:
            entries = None
        carried = len(entries) if isinstance(entries, list) else 0
    truncated = "bounded excerpt" in (head[:start] if start >= 0 else head)
    return text[end + len(HANDOFF_END) :], (carried, truncated)
