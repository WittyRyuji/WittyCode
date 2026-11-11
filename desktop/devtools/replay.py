"""Rebuild Droid SDK events from the recorded JSONL fixtures and replay them.

Fixture records look like ``{"seq", "t", "turn", "turn_t", "source", "type", "data"}``
with ``source`` = ``stream`` (SDK event), ``handler`` (permission / question
request seen by the interaction handler) or ``meta``. ``ReplayFactory`` exposes
the same surface the session manager expects from the real SDK, so the whole UI
can run offline (``python -m desktop --replay fixtures\\stream_turn.jsonl``).
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from droid_sdk import (
    ApplyPatchAction,
    ApplyPatchFile,
    AskUserAction,
    AssistantMessage,
    Autonomy,
    CreateFile,
    DocumentBlock,
    DroidProcessError,
    EditAction,
    ErrorEvent,
    ErrorType,
    ExecuteAction,
    ExitSpecModeAction,
    HookExecution,
    ImageBlock,
    InteractionHandlers,
    McpToolAction,
    Mode,
    ModelInfo,
    ModelProvider,
    PermissionOption,
    PermissionRequest,
    PermissionResolved,
    Question,
    QuestionRequest,
    ReasoningEffort,
    RedactedThinkingBlock,
    RunFailure,
    RunInterrupted,
    RunSuccess,
    SavedSession,
    SessionSettings,
    SessionSettingsUpdate,
    SessionTitleUpdated,
    SettingsUpdated,
    TextBlock,
    TextComplete,
    TextDelta,
    ThinkingBlock,
    ThinkingComplete,
    ThinkingDelta,
    TokenUsageUpdate,
    ToolCall,
    ToolCallDelta,
    ToolConfirmationOutcome,
    ToolProgress,
    ToolProgressUpdate,
    ToolResult,
    ToolUseBlock,
    Usage,
    UserMessage,
    WorkingState,
    WorkingStateChanged,
)

from desktop.core.session_manager import HistorySnapshot


def load_records(path: str | Path) -> list[dict[str, Any]]:
    with open(path, encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def _dt(value: Any) -> datetime:
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    return datetime.now(timezone.utc)


def _seconds(value: Any) -> timedelta | None:
    return timedelta(seconds=float(value)) if isinstance(value, (int, float)) else None


def _tool_use(data: Mapping[str, Any]) -> ToolUseBlock:
    return ToolUseBlock(id=data["id"], name=data["name"], input=data.get("input") or {})


def _block(data: Mapping[str, Any]) -> Any:
    kind = data.get("_type")
    if kind == "TextBlock":
        return TextBlock(text=data.get("text") or "", id=data.get("id"))
    if kind == "ThinkingBlock":
        return ThinkingBlock(
            thinking=data.get("thinking") or "",
            signature="",
            id=data.get("id"),
            duration=_seconds(data.get("duration")),
        )
    if kind == "RedactedThinkingBlock":
        return RedactedThinkingBlock(data="", id=data.get("id"))
    if kind == "ToolUseBlock":
        return _tool_use(data)
    return None


def _message(cls: type, data: Mapping[str, Any]) -> Any:
    blocks = [block for block in (_block(item) for item in data.get("content") or []) if block is not None]
    return cls(
        id=data["id"],
        content=tuple(blocks),
        text=data.get("text") or "",
        parent_id=data.get("parent_id"),
        created_at=_dt(data.get("created_at")),
        updated_at=_dt(data.get("updated_at")),
    )


def _usage(cls: type, data: Mapping[str, Any] | None) -> Any:
    if not data:
        return None
    return cls(
        input_tokens=int(data.get("input_tokens") or 0),
        output_tokens=int(data.get("output_tokens") or 0),
        cache_creation_tokens=int(data.get("cache_creation_tokens") or 0),
        cache_read_tokens=int(data.get("cache_read_tokens") or 0),
        thinking_tokens=int(data.get("thinking_tokens") or 0),
        factory_credits=data.get("factory_credits"),
    )


def _enum(cls: type, value: Any) -> Any:
    try:
        return None if value is None else cls(value)
    except ValueError:
        return None


def event_from_record(record: Mapping[str, Any]) -> Any:
    """SDK event for a ``source == "stream"`` record (None for types the UI ignores)."""
    kind, data = record.get("type"), record.get("data") or {}
    if kind == "WorkingStateChanged":
        return WorkingStateChanged(state=WorkingState(data["state"]))
    if kind == "UserMessage":
        return _message(UserMessage, data)
    if kind == "AssistantMessage":
        return _message(AssistantMessage, data)
    if kind == "TextDelta":
        return TextDelta(message_id=data["message_id"], block_index=data["block_index"], text=data["text"])
    if kind == "TextComplete":
        return TextComplete(message_id=data["message_id"], block_index=data["block_index"])
    if kind == "ThinkingDelta":
        return ThinkingDelta(message_id=data["message_id"], block_index=data["block_index"], text=data["text"])
    if kind == "ThinkingComplete":
        return ThinkingComplete(
            message_id=data["message_id"], block_index=data["block_index"], duration=_seconds(data.get("duration"))
        )
    if kind == "ToolCallDelta":
        return ToolCallDelta(tool_use=_tool_use(data["tool_use"]))
    if kind == "ToolCall":
        return ToolCall(name=data["name"], tool_use_id=data["tool_use_id"], input=data.get("input") or {})
    if kind == "ToolProgress":
        update = data.get("update") or {}
        return ToolProgress(
            tool_use_id=data["tool_use_id"],
            tool_name=data.get("tool_name") or "",
            content=data.get("content") or "",
            update=ToolProgressUpdate(
                type=update.get("type") or "status",
                tool_name=update.get("tool_name"),
                status=update.get("status"),
                details=update.get("details"),
                text=update.get("text"),
                error=update.get("error"),
                terminal_id=update.get("terminal_id"),
                full_output=update.get("full_output"),
            ),
        )
    if kind == "ToolResult":
        return ToolResult(
            tool_use_id=data["tool_use_id"],
            tool_name=data.get("tool_name") or "",
            content=data.get("content") if data.get("content") is not None else "",
            is_error=bool(data.get("is_error")),
        )
    if kind == "PermissionResolved":
        return PermissionResolved(
            request_id=data.get("request_id") or "",
            tool_use_ids=tuple(data.get("tool_use_ids") or ()),
            selected_option=ToolConfirmationOutcome(data["selected_option"]),
        )
    if kind == "TokenUsageUpdate":
        return _usage(TokenUsageUpdate, data)
    if kind == "SessionTitleUpdated":
        return SessionTitleUpdated(title=data["title"])
    if kind == "SettingsUpdated":
        settings = data.get("settings") or {}
        return SettingsUpdated(
            settings=SessionSettingsUpdate(
                model=settings.get("model"),
                reasoning_effort=_enum(ReasoningEffort, settings.get("reasoning_effort")),
                mode=_enum(Mode, settings.get("mode")),
                autonomy=_enum(Autonomy, settings.get("autonomy")),
            )
        )
    if kind == "ErrorEvent":
        return ErrorEvent(
            message=data.get("message") or "",
            error_type=_enum(ErrorType, data.get("error_type")) or ErrorType.ERROR,
            timestamp=_dt(data.get("timestamp")),
        )
    if kind == "HookExecution":
        return HookExecution(
            hook_id=data.get("hook_id") or "",
            event_name=data.get("event_name"),
            matcher=data.get("matcher"),
            tool_call_id=data.get("tool_call_id"),
            command=data.get("command"),
            status=data.get("status") or "started",
            exit_code=data.get("exit_code"),
        )
    if kind in ("RunSuccess", "RunInterrupted", "RunFailure"):
        common = {
            "text": data.get("text") or "",
            "messages": (),
            "usage": _usage(Usage, data.get("usage")),
            "duration": _seconds(data.get("duration")) or timedelta(0),
            "turn_count": int(data.get("turn_count") or 1),
            "session_id": data.get("session_id") or "",
        }
        if kind == "RunSuccess":
            return RunSuccess(**common)
        if kind == "RunInterrupted":
            return RunInterrupted(**common)
        error = data.get("error") or None
        return RunFailure(
            subtype=data.get("subtype") or "error_during_execution",
            error=None
            if not error
            else ErrorEvent(
                message=error.get("message") or "",
                error_type=_enum(ErrorType, error.get("error_type")) or ErrorType.ERROR,
            ),
            **common,
        )
    return None


def _action(data: Mapping[str, Any]) -> Any:
    kind = data.get("_type")
    tool_use = _tool_use(data["tool_use"])
    if kind == "ExecuteAction":
        return ExecuteAction(
            tool_use=tool_use,
            full_command=data.get("full_command") or "",
            command=data.get("command") or "",
            extracted_commands=data.get("extracted_commands"),
            impact_level=data.get("impact_level"),
            risk_level_reason=data.get("risk_level_reason"),
        )
    if kind == "EditAction":
        return EditAction(
            tool_use=tool_use,
            file_path=data.get("file_path") or "",
            file_name=data.get("file_name") or "",
            old_content=data.get("old_content"),
            new_content=data.get("new_content"),
        )
    if kind == "CreateFile":
        return CreateFile(
            tool_use=tool_use,
            file_path=data.get("file_path") or "",
            file_name=data.get("file_name") or "",
            content=data.get("content") or "",
        )
    if kind == "ApplyPatchAction":
        files = [
            ApplyPatchFile(
                file_path=item.get("file_path") or "",
                file_name=item.get("file_name") or "",
                operation=item.get("operation") or "update",
                move_to=item.get("move_to"),
                old_content=item.get("old_content"),
                new_content=item.get("new_content"),
            )
            for item in data.get("files") or []
        ]
        return ApplyPatchAction(
            tool_use=tool_use,
            file_path=data.get("file_path") or "",
            file_name=data.get("file_name") or "",
            patch_content=data.get("patch_content") or "",
            old_content=data.get("old_content"),
            new_content=data.get("new_content"),
            files=files or None,
        )
    if kind == "ExitSpecModeAction":
        return ExitSpecModeAction(tool_use=tool_use, plan=data.get("plan") or "", title=data.get("title"))
    if kind == "AskUserAction":
        return AskUserAction(tool_use=tool_use, questionnaire=data.get("questionnaire") or "")
    if kind == "McpToolAction":
        return McpToolAction(
            tool_use=tool_use,
            tool_name=data.get("tool_name") or "",
            impact_level=data.get("impact_level") or "",
            server_name=data.get("server_name"),
            actual_tool_name=data.get("actual_tool_name"),
        )
    return None


def request_from_record(record: Mapping[str, Any]) -> PermissionRequest | QuestionRequest | None:
    """Rebuild the request of a ``source == "handler"`` record."""
    data = (record.get("data") or {}).get("request")
    if not isinstance(data, Mapping):
        return None
    if record.get("type") == "PermissionRequest":
        actions = [action for action in (_action(item) for item in data.get("actions") or []) if action is not None]
        options = [
            PermissionOption(label=item.get("label") or "", value=ToolConfirmationOutcome(item["value"]))
            for item in data.get("options") or []
        ]
        return PermissionRequest(actions=actions, options=options)
    if record.get("type") == "QuestionRequest":
        questions = [
            Question(
                index=item["index"],
                topic=item.get("topic") or "",
                question=item.get("question") or "",
                options=tuple(item.get("options") or ()),
                multi_select=item.get("multi_select"),
            )
            for item in data.get("questions") or []
        ]
        return QuestionRequest(tool_call_id=data.get("tool_call_id") or "", questions=questions)
    return None


def recorded_response(record: Mapping[str, Any]) -> Any:
    """The answer the spike gave: a permission outcome value, a list of answers, or None."""
    data = record.get("data") or {}
    response = data.get("response")
    if isinstance(response, Mapping):
        if "selected_option" in response:
            return response["selected_option"]
        if "answers" in response:
            return None if response.get("cancelled") else [item.get("answer", "") for item in response["answers"]]
    return None


@dataclass
class TurnRecording:
    prompt: str
    records: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class SessionRecording:
    session_id: str
    cwd: str
    settings: dict[str, Any]
    turns: list[TurnRecording]


def split_recording(records: Iterable[Mapping[str, Any]]) -> SessionRecording:
    session_id, cwd, settings = str(uuid.uuid4()), "", {}
    turns: list[TurnRecording] = []
    for record in records:
        kind = record.get("type")
        if record.get("source") == "meta":
            if kind == "SessionOpened":
                data = record.get("data") or {}
                session_id = data.get("session_id") or session_id
                cwd = data.get("cwd") or cwd
                settings = data.get("settings") or settings
            elif kind == "TurnStarted":
                turns.append(TurnRecording(prompt=(record.get("data") or {}).get("prompt") or ""))
            continue
        if turns:
            turns[-1].records.append(dict(record))
    return SessionRecording(session_id, cwd, settings, turns)


# --------------------------------------------------------------------------- replay session


class _ReplayStream:
    def __init__(self, session: ReplaySession, prompt: str, recording: TurnRecording | None, images: Sequence[Any] = (), files: Sequence[Any] = ()) -> None:
        self._session = session
        self._prompt = prompt
        self._recording = recording
        self._images = tuple(images)
        self._files = tuple(files)
        self._result: Any = None

    @property
    def result(self) -> Any:
        if self._result is None:
            raise RuntimeError("The replay stream has not completed")
        return self._result

    async def __aenter__(self) -> _ReplayStream:
        return self

    async def __aexit__(self, *exc: object) -> None:
        self._session._active = None

    def __aiter__(self) -> AsyncIterator[Any]:
        return self._events()

    async def _events(self) -> AsyncIterator[Any]:
        session = self._session
        if self._recording is None:
            for event in session.canned_turn(self._prompt, self._images, self._files):
                if isinstance(event, (RunSuccess, RunInterrupted, RunFailure)):
                    self._result = event
                yield event
            return
        last = 0.0
        finished = False
        for record in self._recording.records:
            stamp = record.get("turn_t")
            if isinstance(stamp, (int, float)):
                delay = max(0.0, (stamp - last) / session.speed)
                last = stamp
                if delay:
                    await asyncio.sleep(min(delay, session.max_delay))
            if session.interrupted:
                session.interrupted = False
                yield RunInterrupted(text="", messages=(), usage=None, duration=timedelta(seconds=last), turn_count=1, session_id=session.id)
                return
            if record.get("source") == "handler":
                request = request_from_record(record)
                if isinstance(request, PermissionRequest) and session.interactions.on_permission:
                    await _maybe_await(session.interactions.on_permission(request))
                elif isinstance(request, QuestionRequest) and session.interactions.on_question:
                    await _maybe_await(session.interactions.on_question(request))
                continue
            if record.get("source") != "stream":
                continue
            event = event_from_record(record)
            if event is None:
                continue
            if isinstance(event, (RunSuccess, RunInterrupted, RunFailure)):
                finished = True
                self._result = event
            yield event
        if not finished:
            raise DroidProcessError("Droid process exited unexpectedly (exit code 1)", exit_code=1)


async def _maybe_await(value: Any) -> Any:
    if hasattr(value, "__await__"):
        return await value
    return value


class ReplaySession:
    """Duck-typed stand-in for ``droid_sdk.Session`` driven by a recording."""

    def __init__(self, recording: SessionRecording, interactions: InteractionHandlers, *, speed: float, max_delay: float, model: str | None = None) -> None:
        self.recording = recording
        self.interactions = interactions
        self.speed = speed
        self.max_delay = max_delay
        self.interrupted = False
        self.sent: list[tuple[str, tuple[Any, ...], tuple[Any, ...]]] = []
        """Every prompt with the pictures and documents it came with (what ``stream`` was given)."""
        self._active: _ReplayStream | None = None
        self._turn_index = 0
        recorded = recording.settings or {}
        self._settings = SessionSettings(
            model=model or recorded.get("model") or "claude-haiku-5-5",
            reasoning_effort=_enum(ReasoningEffort, recorded.get("reasoning_effort")) or ReasoningEffort.LOW,
            mode=_enum(Mode, recorded.get("mode")) or Mode.AUTO,
            autonomy=_enum(Autonomy, recorded.get("autonomy")) or Autonomy.OFF,
        )
        self.cwd = Path(recording.cwd) if recording.cwd else None

    @property
    def id(self) -> str:
        return self.recording.session_id

    @property
    def settings(self) -> SessionSettings:
        return self._settings

    async def open(self) -> None:
        await asyncio.sleep(0.05)

    async def close(self) -> None:
        self._active = None

    async def interrupt(self) -> None:
        if self._active is not None:
            self.interrupted = True

    async def update_settings(self, **changes: Any) -> None:
        current = self._settings
        self._settings = SessionSettings(
            model=changes.get("model") or current.model,
            reasoning_effort=changes.get("reasoning_effort") or current.reasoning_effort,
            mode=changes.get("mode") or current.mode,
            autonomy=changes.get("autonomy") or current.autonomy,
        )

    def stream(
        self,
        prompt: str,
        *,
        images: Sequence[Any] = (),
        files: Sequence[Any] = (),
        include_partial_messages: bool = True,
        timeout: float | None = None,
    ) -> _ReplayStream:
        self.sent.append((prompt, tuple(images), tuple(files)))
        turns = self.recording.turns
        recording = turns[self._turn_index] if self._turn_index < len(turns) else None
        self._turn_index += 1
        self._active = _ReplayStream(self, prompt, recording, images, files)
        return self._active

    def canned_turn(self, prompt: str, images: Sequence[Any] = (), files: Sequence[Any] = ()) -> list[Any]:
        now = datetime.now(timezone.utc)
        message_id = str(uuid.uuid4())
        reply = "（回放已结束：录制里没有更多轮次。）"
        content = (
            TextBlock(text=prompt),
            *(ImageBlock(source=image.source) for image in images),
            *(DocumentBlock(source=document.source) for document in files),
        )
        return [
            WorkingStateChanged(state=WorkingState.STREAMING_ASSISTANT_MESSAGE),
            UserMessage(id=str(uuid.uuid4()), content=content, text=prompt, created_at=now, updated_at=now),
            TextDelta(message_id=message_id, block_index=0, text=reply),
            TextComplete(message_id=message_id, block_index=0),
            RunSuccess(text=reply, messages=(), usage=None, duration=timedelta(seconds=0.2), turn_count=1, session_id=self.id),
        ]


class ReplayFactory:
    """``SessionFactory`` for offline runs: every new session replays one recording."""

    needs_cwd_for_resume = False

    def __init__(self, fixture: str | Path, *, speed: float = 1.0, max_delay: float = 1.5) -> None:
        self.fixture = Path(fixture)
        self.speed = speed
        self.max_delay = max_delay
        self.sessions: list[ReplaySession] = []

    def _recording(self) -> SessionRecording:
        recording = split_recording(load_records(self.fixture))
        if self.sessions:
            recording.session_id = str(uuid.uuid4())
        return recording

    def create(self, *, cwd: Path, model: str | None, reasoning: Any, config: Any, interactions: InteractionHandlers) -> ReplaySession:
        session = ReplaySession(self._recording(), interactions, speed=self.speed, max_delay=self.max_delay, model=model)
        self.sessions.append(session)
        return session

    def resume(self, session_id: str, *, interactions: InteractionHandlers, mcp_servers: Any = ()) -> ReplaySession:
        recording = self._recording()
        recording.session_id = session_id
        recording.turns = []
        session = ReplaySession(recording, interactions, speed=self.speed, max_delay=self.max_delay)
        self.sessions.append(session)
        return session

    async def load_history(self, session_id: str, cwd: Path) -> HistorySnapshot:
        return HistorySnapshot(messages=[], title=None)

    async def load_histories(self, session_ids: Any, cwd: Path) -> Any:
        for session_id in session_ids:
            yield session_id, HistorySnapshot(messages=[], title=None)

    async def list_models(self, cwd: Path) -> list[ModelInfo]:
        return [
            ModelInfo(
                id="claude-haiku-5-5",
                display_name="Haiku 5.5",
                short_display_name="Haiku 5.5",
                model_provider=ModelProvider.ANTHROPIC,
                supported_reasoning_efforts=(ReasoningEffort.OFF, ReasoningEffort.LOW, ReasoningEffort.MEDIUM, ReasoningEffort.HIGH),
                default_reasoning_effort=ReasoningEffort.MEDIUM,
                token_multiplier=0.04,
            )
        ]

    async def list_sessions(self, cwd: Path, limit: int | None = None) -> list[SavedSession]:
        return []