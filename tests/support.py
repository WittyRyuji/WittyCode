"""Shared helpers for the test suite (no live model calls anywhere)."""

from __future__ import annotations

import base64
import json
import struct
import zlib
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from droid_sdk import (
    AssistantMessage,
    PermissionRequest,
    QuestionRequest,
    RunFailure,
    RunInterrupted,
    RunSuccess,
    SavedSession,
    TextBlock,
    TextComplete,
    TextDelta,
    TokenUsageUpdate,
    ToolCall,
    ToolResult,
    ToolUseBlock,
    UserMessage,
)

from desktop.core.transcript import ActivityBlock, Transcript, Turn
from desktop.devtools.replay import (
    event_from_record,
    load_records,
    recorded_response,
    request_from_record,
)

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "fixtures"


def fixture_path(name: str) -> Path:
    return FIXTURES / name


def replay_into(transcript: Transcript, records: list[dict[str, Any]], *, answer: bool = True) -> Transcript:
    """Feed recorded records to a transcript the way ``SessionController`` does.

    Handler records become permission / question prompts; with ``answer`` the
    recorded response is applied (the spike's own decision), otherwise the
    prompt is left pending.
    """
    for record in records:
        source, kind = record.get("source"), record.get("type")
        if source == "meta":
            if kind == "TurnStarted":
                transcript.start_turn((record.get("data") or {}).get("prompt") or "")
            continue
        if source == "handler":
            request = request_from_record(record)
            if isinstance(request, PermissionRequest):
                prompt = transcript.add_permission(request)
                response = recorded_response(record)
                if answer and prompt is not None and isinstance(response, str):
                    transcript.answer_permission(prompt.id, response)
            elif isinstance(request, QuestionRequest):
                prompt = transcript.add_question(request)
                response = recorded_response(record)
                if answer and prompt is not None:
                    transcript.answer_question(prompt.id, response)
            continue
        if source == "stream":
            event = event_from_record(record)
            if event is not None:
                transcript.apply(event)
    return transcript


def replay_fixture(name: str, *, answer: bool = True) -> Transcript:
    return replay_into(Transcript(), load_records(fixture_path(name)), answer=answer)


def last_turn(transcript: Transcript) -> Turn:
    return transcript.turns[-1]


def groups(turn: Turn) -> list[ActivityBlock]:
    return [block for block in turn.blocks if isinstance(block, ActivityBlock)]


def load_json(name: str) -> Any:
    return json.loads(fixture_path(name).read_text(encoding="utf-8"))


def history_messages() -> list[Any]:
    """The messages of ``fixtures/history.json`` as the SDK delivers them for a saved session."""
    from droid_sdk.schemas.messages import FactoryDroidMessage

    messages = []
    for raw in load_json("history.json")["messages"]:
        raw = dict(raw)
        raw.pop("_type", None)
        messages.append(FactoryDroidMessage.model_validate(raw))
    return messages


def tiny_png(width: int = 4, height: int = 3, rgb: tuple[int, int, int] = (255, 99, 71)) -> bytes:
    """A real, decodable PNG of one colour, made without Qt (the SDK checks the signature, Qt decodes the rest)."""

    def chunk(kind: bytes, body: bytes) -> bytes:
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF)

    rows = b"".join(b"\x00" + bytes(rgb) * width for _ in range(height))
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b"")


def wire_message(message_id: str, role: str, *content: dict[str, Any], at: float = 1_793_182_944_000.0) -> Any:
    """A message the way Droid saves it (what ``DroidClient.load_session`` returns), built from its JSON shape."""
    from droid_sdk.schemas.messages import FactoryDroidMessage

    return FactoryDroidMessage.model_validate(
        {"id": message_id, "role": role, "content": list(content), "createdAt": at, "updatedAt": at}
    )


QUESTION_BLOCK = {"type": "text", "text": "what is in these?"}
PICTURE_BLOCK = {
    "type": "image",
    "source": {"type": "base64", "data": base64.b64encode(tiny_png(60, 40)).decode("ascii"), "mediaType": "image/png"},
}
# Droid keeps a PDF without its bytes: ``data`` is empty and the extracted text, name and path remain.
PDF_BLOCK = {
    "type": "document",
    "source": {
        "type": "base64",
        "mediaType": "application/pdf",
        "data": "",
        "parsedData": "Invoice 4417",
        "name": "invoice.pdf",
        "path": r"C:\Users\me\.factory\sessions\s1\attachments\invoice.pdf",
    },
}
NOTES_BLOCK = {
    "type": "document",
    "source": {"type": "text", "mediaType": "text/plain", "data": "# 笔记\nbuy milk", "name": "notes.md", "mime": "text/markdown"},
}


# What droid really saved next to a picture (seen in a live session): the paths of the picture's temporary copies,
# as a text block of its own between the picture and the typed text.
REMINDER_BLOCK = {
    "type": "text",
    "text": (
        "<system-reminder>Attached image local file paths:\n"
        "- Image 1: C:\\Users\\me\\.factory\\temp\\images\\6d517230\\893a8a69.png\n\n"
        "These are temporary copies: droid may delete them when it exits or after about two days, so they may be "
        "gone after a restart or resume.</system-reminder>"
    ),
}


def attachment_history() -> list[Any]:
    """A saved session whose user messages carry a picture, a PDF and a text file (two turns), the way droid saves them."""
    return [
        wire_message("u1", "user", PICTURE_BLOCK, REMINDER_BLOCK, PDF_BLOCK, NOTES_BLOCK, QUESTION_BLOCK, at=1_793_182_944_000.0),
        wire_message("a1", "assistant", {"type": "text", "text": "A tangerine and an invoice."}, at=1_793_182_950_000.0),
        wire_message("u2", "user", PICTURE_BLOCK, REMINDER_BLOCK, at=1_793_182_960_000.0),
        wire_message("a2", "assistant", {"type": "text", "text": "The same picture again."}, at=1_793_182_965_000.0),
    ]


def saved_session(session_id: str, title: str, *, minutes_ago: float = 5, messages: int = 4) -> SavedSession:
    modified = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    return SavedSession(
        id=session_id, title=title, owner="me", message_count=messages, modified_at=modified, created_at=modified
    )


def usage(input_tokens: int = 1200, output_tokens: int = 340, cache_read: int = 91000, credits: float | None = 4.5) -> TokenUsageUpdate:
    return TokenUsageUpdate(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_creation_tokens=0,
        cache_read_tokens=cache_read,
        thinking_tokens=0,
        factory_credits=credits,
    )

# --------------------------------------------------------------------------- synthetic SDK events


def delta(text: str, message_id: str = "m1", index: int = 0) -> TextDelta:
    return TextDelta(message_id=message_id, block_index=index, text=text)


def complete(message_id: str = "m1", index: int = 0) -> TextComplete:
    return TextComplete(message_id=message_id, block_index=index)


def tool_call(tool_use_id: str, name: str, **data: Any) -> ToolCall:
    return ToolCall(name=name, tool_use_id=tool_use_id, input=data)


def tool_result(tool_use_id: str, name: str, content: Any, is_error: bool = False) -> ToolResult:
    return ToolResult(tool_use_id=tool_use_id, tool_name=name, content=content, is_error=is_error)


def user_message(message_id: str, text: str) -> UserMessage:
    now = datetime.now(timezone.utc)
    return UserMessage(id=message_id, content=(TextBlock(text=text),), text=text, created_at=now, updated_at=now)


def assistant_message(message_id: str, *content: Any) -> AssistantMessage:
    now = datetime.now(timezone.utc)
    return AssistantMessage(id=message_id, content=tuple(content), text="", parent_id=None, created_at=now, updated_at=now)


def tool_use(tool_use_id: str, name: str, **data: Any) -> ToolUseBlock:
    return ToolUseBlock(id=tool_use_id, name=name, input=data)


def _result(cls: type, text: str, seconds: float, usage: Any, **extra: Any) -> Any:
    return cls(
        text=text,
        messages=(),
        usage=usage,
        duration=timedelta(seconds=seconds),
        turn_count=1,
        session_id="s1",
        **extra,
    )


def success(text: str = "", seconds: float = 1.0, usage: Any = None) -> RunSuccess:
    return _result(RunSuccess, text, seconds, usage)


def interrupted(seconds: float = 1.0) -> RunInterrupted:
    return _result(RunInterrupted, "", seconds, None)


def failure(message: str, seconds: float = 1.0) -> RunFailure:
    from droid_sdk import ErrorEvent, ErrorType

    return _result(
        RunFailure,
        "",
        seconds,
        None,
        subtype="error_during_execution",
        error=ErrorEvent(message=message, error_type=ErrorType.ERROR),
    )

# --------------------------------------------------------------------------- recorded requests


def permission_request(nth: int = 0) -> PermissionRequest:
    """A real permission request (Create / Edit file) taken from the stream_turn recording."""
    found = [
        request
        for request in (
            request_from_record(record)
            for record in load_records(fixture_path("stream_turn.jsonl"))
            if record.get("source") == "handler"
        )
        if isinstance(request, PermissionRequest)
    ]
    return found[nth]


def question_request() -> QuestionRequest:
    from droid_sdk import Question

    return QuestionRequest(
        tool_call_id="ask-1",
        questions=[
            Question(index=0, topic="Pick", question="Pick one?", options=("a", "b"), multi_select=False),
            Question(index=1, topic="Many", question="Pick several?", options=("x", "y", "z"), multi_select=True),
        ],
    )