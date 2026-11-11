"""A stand-in for droid that reads what it is sent and saves its sessions the way droid does.

It lets a live-check script (e.g. an attachment check) run from start to end offline before a live turn is spent on it. The answers
come from what really arrives through ``stream(images=..., files=...)``: the colours of the picture, the text of the
text file, the text the SDK parsed out of the PDF. A session that is closed and opened again finds its history as
``DroidClient.load_session`` returns it from a real droid (droid 0.251.0): the pictures, a ``<system-reminder>`` block
next to them, and the PDF with its bytes (droid keeps them in a copy under the session's ``attachments`` folder).
"""

from __future__ import annotations

import base64
import re
import uuid
from collections.abc import Sequence
from datetime import datetime, timedelta, timezone
from typing import Any

from droid_sdk import (
    DocumentBlock,
    ImageBlock,
    InteractionHandlers,
    PdfDocumentSource,
    RunSuccess,
    TextBlock,
    TextComplete,
    TextDelta,
    UserMessage,
    WorkingState,
    WorkingStateChanged,
)
from PySide6.QtGui import QColor, QImage

from desktop.core.session_manager import HistorySnapshot
from tests.fakes import FakeFactory, FakeSession
from tests.support import saved_session, wire_message

CODEWORD = re.compile(r"codeword[^\n]*?\bis\s+([\w-]+)", re.IGNORECASE)
TOTAL = re.compile(r"Total due:\s*([\d.,]+\s*[A-Z]{3})")
REMINDER = (
    "<system-reminder>Attached image local file paths ({count}): C:\\Users\\me\\.factory\\temp\\images\\6d517230\\a.png\n\n"
    "These are temporary copies: droid may delete them when it exits or after about two days.</system-reminder>"
)
PALETTE = {"red": "#d62828", "blue": "#1d4ed8", "yellow": "#fff3a0"}
"""The colours of ``devtools.samples.sample_picture``: the echo "sees" them in the pixels it was sent."""
FIRST_MESSAGE_AT = 1_793_182_944_000.0


def colours_in(data: bytes) -> list[str] | None:
    """Names of the palette colours found in a picture; None when the bytes are not a picture."""
    image = QImage.fromData(data)
    if image.isNull():
        return None
    wanted = {name: QColor(code).rgb() for name, code in PALETTE.items()}
    found: set[str] = set()
    for y in range(0, image.height(), 8):
        for x in range(0, image.width(), 8):
            pixel = image.pixel(x, y)
            found.update(name for name, rgb in wanted.items() if pixel == rgb)
    return [name for name in PALETTE if name in found]


def document_text(source: Any) -> str:
    return (source.parsed_data or "") if isinstance(source, PdfDocumentSource) else str(source.data)


def first_match(pattern: re.Pattern[str], texts: Sequence[str]) -> str:
    for text in texts:
        found = pattern.search(text)
        if found:
            return found.group(1)
    return "unknown"


def wire_blocks(prompt: str, images: Sequence[Any], files: Sequence[Any], session_id: str) -> list[dict[str, Any]]:
    """The content of the saved user message: what droid keeps of a prompt with attachments.

    As in a live session: the pictures, then a ``<system-reminder>`` block of droid's own about their temporary
    copies, then the documents, and the typed text last.
    """
    blocks: list[dict[str, Any]] = []
    for image in images:
        source = image.source
        blocks.append({"type": "image", "source": {"type": "base64", "data": source.data, "mediaType": source.media_type}})
    if images:
        blocks.append({"type": "text", "text": REMINDER.format(count=len(images))})
    for document in files:
        source = document.source
        if isinstance(source, PdfDocumentSource):
            saved_path = source.path or rf"C:\Users\me\.factory\sessions\{session_id}\attachments\{source.name}"
            body = {
                "type": "base64",
                "mediaType": "application/pdf",
                "data": source.data,
                "parsedData": source.parsed_data,
                "name": source.name,
                "path": saved_path,
            }
        else:
            body = {"type": "text", "mediaType": "text/plain", "data": source.data, "name": source.name, "mime": source.mime}
        blocks.append({"type": "document", "source": body})
    blocks.append({"type": "text", "text": prompt})
    return blocks


class EchoSession(FakeSession):
    def __init__(self, factory: EchoFactory, interactions: InteractionHandlers, **options: Any) -> None:
        super().__init__(interactions, **options)
        self.factory = factory

    def stream(self, prompt: str, **options: Any):
        stream = super().stream(prompt, **options)
        reply = self.factory.take_turn(self.id, prompt, stream.images, stream.files)
        message_id, now = str(uuid.uuid4()), datetime.now(timezone.utc)
        content = (
            TextBlock(text=prompt),
            *(ImageBlock(source=image.source) for image in stream.images),
            *(DocumentBlock(source=document.source) for document in stream.files),
        )
        stream.push(
            WorkingStateChanged(state=WorkingState.STREAMING_ASSISTANT_MESSAGE),
            UserMessage(id=str(uuid.uuid4()), content=content, text=prompt, created_at=now, updated_at=now),
            TextDelta(message_id=message_id, block_index=0, text=reply),
            TextComplete(message_id=message_id, block_index=0),
            RunSuccess(text=reply, messages=(), usage=None, duration=timedelta(seconds=0.2), turn_count=1, session_id=self.id),
        )
        stream.end()
        return stream


class EchoFactory(FakeFactory):
    def __init__(self) -> None:
        super().__init__()
        self.known: dict[str, list[str]] = {}
        """Per session: the text of every document it was sent (what the model remembers of earlier turns)."""

    def _new_session(self, interactions: InteractionHandlers, session_id: str | None) -> EchoSession:
        self._counter += 1
        options = {**self.default_options, **self.next_session}
        self.next_session = {}
        session = EchoSession(self, interactions, session_id=session_id or f"echo-{self._counter}", **options)
        session.tracker = self.tracker
        self.sessions.append(session)
        return session

    def take_turn(self, session_id: str, prompt: str, images: Sequence[Any], files: Sequence[Any]) -> str:
        known = self.known.setdefault(session_id, [])
        known.extend(document_text(document.source) for document in files)
        parts = []
        for image in images:
            colours = colours_in(base64.b64decode(image.source.data))
            if colours is None:
                parts.append("I could not make out a picture in the image data.")
            else:
                parts.append(f"The picture shows shapes in {', '.join(colours)} and the word TANGERINE-42.")
        if files or not images:
            parts.append(f"codeword={first_match(CODEWORD, known)}; total={first_match(TOTAL, known)}")
        reply = " ".join(parts)
        self._save(session_id, prompt, images, files, reply)
        return reply

    def _save(self, session_id: str, prompt: str, images: Sequence[Any], files: Sequence[Any], reply: str) -> None:
        snapshot = self.history.get(session_id) or HistorySnapshot(messages=[], title=None)
        number = len(snapshot.messages)
        at = FIRST_MESSAGE_AT + number * 1000
        messages = [
            *snapshot.messages,
            wire_message(f"u{number}", "user", *wire_blocks(prompt, images, files, session_id), at=at),
            wire_message(f"a{number}", "assistant", {"type": "text", "text": reply}, at=at + 500),
        ]
        title = snapshot.title or prompt[:40]
        self.history[session_id] = HistorySnapshot(messages=messages, title=title)
        self.saved = [item for item in self.saved if item.id != session_id]
        self.saved.append(saved_session(session_id, title, minutes_ago=0, messages=len(messages)))
