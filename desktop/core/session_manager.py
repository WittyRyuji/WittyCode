"""One controller per open session, plus the manager that owns them all.

No Qt imports here: everything runs on the (qasync) event loop and reports to
the UI through plain listener callbacks. The real Droid SDK and the offline
fixture replayer both satisfy the ``SessionFactory`` protocol.

Traps from the Phase 0 findings handled here:

- ``Session.resume()`` has no cwd parameter and droid inherits this process's
  cwd, so resumes are serialized behind a lock around ``os.chdir``.
- ``close()`` while a turn is active hangs the consuming coroutine, so the
  consumer task is cancelled first (which also sends an interrupt to droid).
- A stop request that droid does not honour in time must not simply cancel the
  consumer: the SDK would then call the session idle while droid may still be
  running the turn. The process is shut down and the same session id resumed
  in a new one (READY only afterwards); if that fails the session is DEAD.
- ``close()`` after a process crash re-raises the sticky error -> swallowed.
- Interaction handlers wait on futures the UI completes; there is no timeout on
  the Droid side, but pending futures are settled (cancel) when the turn ends,
  the session closes, or the process dies, so nothing leaks.
- Invalid model ids are silently substituted; the actual model is read back
  from ``session.settings`` / ``SettingsUpdated`` after opening.
- The SDK starts droid.exe without ``creationflags``; ``DroidFactory`` installs
  ``winproc`` so that no console window opens when the app has no console.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
import uuid
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import Enum
from pathlib import Path
from typing import Any, ClassVar, Protocol

from droid_sdk import (
    Autonomy,
    DroidConnectionError,
    DroidError,
    DroidProcessError,
    InteractionHandlers,
    InvalidAttachmentError,
    Mode,
    ModelInfo,
    PermissionRequest,
    PermissionResponse,
    QuestionRequest,
    QuestionResponse,
    ReasoningEffort,
    RunTimeoutError,
    Runtime,
    SavedSession,
    Session,
    SessionClosedError,
    SessionConfig,
    SessionNotOpenError,
    SessionSettings,
    StdioMcpServerConfig,
    ToolConfirmationOutcome,
)
from droid_sdk.client import DroidClient
from droid_sdk.transport import ProcessTransport

from desktop.core import winproc
from desktop.core.attachments import DEFAULT_PROMPT, IMAGE, Attachment, steer_options, stream_options
from desktop.core.context import ContextState, last_call_figure, plausible, read_context
from desktop.core.droid_limits import CUSTOM, DroidLimits
from desktop.core.context_envelope import wrap as wrap_context
from desktop.core.droid_locator import DroidNotFoundError, locate_droid
from desktop.core.hooks import Hooks, PromptEvent
from desktop.core.settings import AppSettings, normalize_folder
from desktop.core.toolinfo import working_label as label_for_working_state
from desktop.core.handoff import Handoff
from desktop.core.memory.launch import MemoryLaunch, new_run
from desktop.core.transcript import PromptStatus, Transcript, Turn, TurnStatus, UserBlock
from desktop.core.usage import Tokens, own_output

log = logging.getLogger("desktop.sessions")

# Errors that mean "the droid process / its pipe is gone": the session can only
# be brought back with reconnect() (resume by id), never by sending again.
CONNECTION_LOST = (DroidProcessError, DroidConnectionError, SessionClosedError, SessionNotOpenError)
# How long a stop request may take (answer plus the end of the turn) before droid counts as stuck
# and its connection is replaced.
INTERRUPT_GRACE_SECONDS = 8.0
# How long that replacement waits for the old droid process to shut down before giving up and
# reporting the session as disconnected (reconnect() later waits for the shutdown to finish).
RESTART_CLOSE_SECONDS = 15.0
# How long close() waits for a session that is still starting up to finish opening
# (so its process can be shut down properly instead of being orphaned).
OPEN_CLOSE_WAIT_SECONDS = 10.0
SESSION_LIST_LIMIT = 50


class SessionState(str, Enum):
    OPENING = "opening"
    READY = "ready"
    RUNNING = "running"
    DEAD = "dead"
    CLOSED = "closed"


SESSION_STATE_LABELS: dict[SessionState, str] = {
    SessionState.OPENING: "正在连接…",
    SessionState.READY: "就绪",
    SessionState.RUNNING: "运行中",
    SessionState.DEAD: "连接已断开",
    SessionState.CLOSED: "已关闭",
}


class SessionLimitError(RuntimeError):
    """Raised when the configured number of open sessions is already reached."""


def eviction_message(title: str, *, reopenable: bool = True) -> str:
    """What the user is told when an idle session is closed to make room for another one."""
    text = f"已自动关闭最久未使用的会话「{title}」以腾出位置"
    return text + ("，可在侧边栏重新打开。" if reopenable else "。")


@dataclass(frozen=True, slots=True)
class HistorySnapshot:
    messages: list[Any]
    title: str | None
    usage: Any = None
    """What droid has counted for the session so far (sub-agents included): its saved token total."""
    settings: LiveSettings | None = None
    """The model, reasoning level, permission and mode the session was last left with."""
    context_used: int | None = None
    """How big the context was at the last model call (input, cache reads and output), as droid recorded it."""


@dataclass(slots=True)
class Peek:
    """A saved session read without opening it (no process of its own kept): what the cache keeps of it."""

    transcript: Transcript
    title: str | None = None
    settings: LiveSettings | None = None
    usage: Tokens | None = None
    context_used: int | None = None
    window: int | None = None


def history_of(result: Any) -> HistorySnapshot:
    """What droid's ``load_session`` answers, as the application uses it."""
    settings = getattr(result, "settings", None)
    last = getattr(result, "last_call_token_usage", None)
    used = None
    if last is not None:
        used = last_call_figure(
            {
                "inputTokens": getattr(last, "input_tokens", 0) or 0,
                "cacheReadTokens": getattr(last, "cache_read_tokens", 0) or 0,
                "outputTokens": getattr(last, "output_tokens", 0) or 0,
            }
        )
    return HistorySnapshot(
        messages=list(result.session.messages),
        title=result.session.title,
        usage=result.inclusive_token_usage or result.token_usage,
        settings=(
            LiveSettings(
                model=str(settings.model_id or ""),
                reasoning=_enum_value(settings.reasoning_effort),
                autonomy=_enum_value(settings.autonomy_level),
                mode=_enum_value(settings.interaction_mode),
            )
            if settings is not None
            else None
        ),
        context_used=used,
    )


@dataclass(frozen=True, slots=True)
class SteerOutcome:
    """What became of a message given to a running turn."""

    ok: bool
    over: bool = False
    """The turn was already over: the message was not given to anything (it can go as a message of its own)."""
    message: str = ""


STEER_SETTLE_SECONDS = 0.6
"""How long after a turn ended a steer that droid has not echoed is waited for, before it is given back to the user."""


@dataclass(frozen=True, slots=True)
class LiveSettings:
    """What Droid reports for an open session (values are SDK enum values)."""

    model: str
    reasoning: str
    autonomy: str
    mode: str


class SessionFactory(Protocol):
    """What the manager needs from a backend (real SDK or fixture replay)."""

    needs_cwd_for_resume: bool

    def create(
        self,
        *,
        cwd: Path,
        model: str | None,
        reasoning: ReasoningEffort | None,
        config: SessionConfig,
        interactions: InteractionHandlers,
    ) -> Any: ...

    def resume(self, session_id: str, *, interactions: InteractionHandlers, mcp_servers: Sequence[Any] = ()) -> Any: ...

    async def load_history(self, session_id: str, cwd: Path) -> HistorySnapshot: ...

    def load_histories(self, session_ids: Sequence[str], cwd: Path) -> AsyncIterator[tuple[str, HistorySnapshot | BaseException]]:
        """Several sessions' histories one after the other (one droid process for all), each as soon as it is read."""
        ...

    async def list_models(self, cwd: Path) -> list[ModelInfo]: ...

    async def list_sessions(self, cwd: Path, limit: int | None = None) -> list[SavedSession]: ...


class DroidFactory:
    """Spawns real ``droid.exe`` processes through droid-sdk."""

    needs_cwd_for_resume = True

    def __init__(self, locate: Callable[[], Path]) -> None:
        self._locate = locate
        # Without this every droid.exe opens a console window when the app runs under pythonw.exe.
        winproc.install()

    def _runtime(self) -> Runtime:
        # Re-resolve on every spawn: a desktop update may replace app-* folders.
        return Runtime(executable=self._locate())

    def create(
        self,
        *,
        cwd: Path,
        model: str | None,
        reasoning: ReasoningEffort | None,
        config: SessionConfig,
        interactions: InteractionHandlers,
    ) -> Session:
        return Session(
            cwd=cwd,
            model=model,
            reasoning_effort=reasoning,
            config=config,
            interactions=interactions,
            runtime=self._runtime(),
        )

    def resume(self, session_id: str, *, interactions: InteractionHandlers, mcp_servers: Sequence[Any] = ()) -> Session:
        return Session.resume(session_id, interactions=interactions, mcp_servers=tuple(mcp_servers), runtime=self._runtime())

    async def load_history(self, session_id: str, cwd: Path) -> HistorySnapshot:
        client = DroidClient(
            transport=ProcessTransport(exec_path=str(self._locate()), cwd=str(cwd))
        )
        await client.connect()
        try:
            return history_of(await client.load_session(session_id=session_id))
        finally:
            with contextlib.suppress(DroidError):
                await client.close_session(reason="other")
            with contextlib.suppress(DroidError):
                await client.close()

    async def load_histories(
        self, session_ids: Sequence[str], cwd: Path
    ) -> AsyncIterator[tuple[str, HistorySnapshot | BaseException]]:
        """Many sessions through one droid process: it can hold several loaded sessions (2026-11-02, droid 0.251: the
        first load 2.3 s with the process starting, the next ones 0.04 s each), but ``close_session`` ends the process,
        so none is closed in between; the process going away at the end is what releases them."""
        client = DroidClient(transport=ProcessTransport(exec_path=str(self._locate()), cwd=str(cwd)))
        await client.connect()
        try:
            for session_id in session_ids:
                try:
                    yield session_id, history_of(await client.load_session(session_id=session_id))
                except Exception as error:
                    yield session_id, error
                    if isinstance(error, CONNECTION_LOST):
                        return
        finally:
            with contextlib.suppress(DroidError):
                await client.close()

    async def list_models(self, cwd: Path) -> list[ModelInfo]:
        from droid_sdk import list_models

        try:
            return await list_models(runtime=self._runtime(), cwd=cwd)
        except ValueError as error:  # pydantic's ValidationError is a ValueError
            # droid-sdk 0.5.0 knows no ``bedrock-converse`` provider: one such custom model fails the whole list.
            log.warning("the model list did not parse (%s); reading it leniently", type(error).__name__)
            return await self._list_models_leniently(cwd)

    async def _list_models_leniently(self, cwd: Path) -> list[ModelInfo]:
        """The model list with the provider of models the SDK does not know replaced by the generic one."""
        from droid_sdk._high_level.models import _model_from_wire
        from droid_sdk.schemas.enums import DroidServerMethod
        from droid_sdk.schemas.models import ListModelsResult

        from droid_sdk import ModelProvider

        known = {provider.value for provider in ModelProvider}
        client = DroidClient(transport=ProcessTransport(exec_path=str(self._locate()), cwd=str(cwd)))
        await client.connect()
        try:
            response = await client._ensure_protocol().send_request(method=DroidServerMethod.LIST_MODELS.value, params={})
            raw = response.get("result", {})
            models = []
            for item in raw.get("models", []):
                if isinstance(item, dict) and item.get("modelProvider") not in known:
                    item = {**item, "modelProvider": ModelProvider.GENERIC_CHAT_COMPLETION_API.value}
                models.append(item)
            result = ListModelsResult.model_validate({**raw, "models": models})
            return [_model_from_wire(model) for model in result.models]
        finally:
            with contextlib.suppress(DroidError):
                await client.close()

    async def list_sessions(self, cwd: Path, limit: int | None = None) -> list[SavedSession]:
        from droid_sdk import list_sessions

        return await list_sessions(cwd=cwd, limit=limit)


CancelPermission = PermissionResponse(selected_option=ToolConfirmationOutcome.CANCEL)
CancelQuestion = QuestionResponse(cancelled=True)

QuestionAnswers = Sequence["str | Sequence[str]"]


def _cancel_response(request: Any) -> PermissionResponse | QuestionResponse:
    return CancelQuestion if isinstance(request, QuestionRequest) else CancelPermission


def _enum_value(value: Any) -> str:
    return str(getattr(value, "value", value) or "")


def _answer_text(answer: str | Sequence[str]) -> str:
    return answer if isinstance(answer, str) else ", ".join(answer)


@dataclass(eq=False)
class SessionController:
    """Owns one session: its transcript, consumer task and pending prompts."""

    manager: SessionManager
    project: Path
    transcript: Transcript = field(default_factory=Transcript)
    state: SessionState = SessionState.OPENING
    session_id: str | None = None
    saved_title: str | None = None
    opened_at: float = field(default_factory=time.time)
    used_at: float = field(default_factory=time.time)
    """Which idle session is closed first when a slot is needed: set at construction and when a message is sent."""
    active_at: float = field(default_factory=time.time)
    """When the conversation last had a message sent or answered: the sidebar orders and dates sessions by it. Opening,
    resuming or reconnecting does not change it, so a resumed session keeps its saved time (``open_saved(modified=)``)."""
    last_error: str = ""
    # Set by the manager when it closes this session to stay within the open-session limit.
    eviction_notice: str = ""
    context: ContextState = field(default_factory=ContextState)
    """How full the context is; listeners hear about changes as ``"context"``."""
    engine: ClassVar[str] = "droid"
    handoff: Handoff | None = None
    """Set when this conversation continues from another engine: the first message carries its text (see ``core/handoff``)."""
    saved_settings: LiveSettings | None = None
    """What a resumed session was left with, read with its history: shown while the session itself is still connecting."""
    run: str = field(default_factory=new_run)
    """Tells the shared memory which conversation a note comes from (it is in the command of the memory server)."""
    preface: str = ""
    """The note about the shared memory put after the first message of a new session; dropped once that turn is done."""
    _preface_turn: Turn | None = None
    recalled: set[str] = field(default_factory=set)
    """What the application's hooks gave this conversation with its messages (ids of notes): not given again."""
    _recall_turn: Turn | None = None
    _recall_ids: frozenset[str] = frozenset()
    _handoff_turn: Turn | None = None
    _loaded_usage: Tokens | None = None
    _inclusive_usage: Tokens | None = None
    _steers: dict[str, UserBlock] = field(default_factory=dict)
    """Message ids of what was said to the running turn, with the block that shows it."""
    _unadopted: list[tuple[str, tuple[Attachment, ...]]] = field(default_factory=list)
    _session: Any = None
    _context_task: asyncio.Task[None] | None = None
    _context_dirty: bool = False
    _used_before_compaction: int | None = None
    _user_window: int | None = None
    """The window the user gave the current model of an API provider: shown instead of the 250 000 Droid reports for it (``core/droid_limits``)."""
    _compaction_set: int | None = None
    """The ``compaction_token_limit`` this application gave the session; ``None``: none, Droid's own rule stands."""
    _consumer: asyncio.Task[None] | None = None
    _compacting: bool = False
    """The running turn is a compaction the user asked for (``start_compaction``), not a conversation turn."""
    _pending: dict[str, tuple[Any, asyncio.Future[Any]]] = field(default_factory=dict)
    _listeners: list[Callable[[SessionController, str], None]] = field(default_factory=list)
    _close_task: asyncio.Task[None] | None = None
    _close_requested: bool = False
    _opening: asyncio.Future[None] | None = None
    _background: set[asyncio.Task[Any]] = field(default_factory=set)

    # ------------------------------------------------------------------ plumbing

    def add_listener(self, callback: Callable[[SessionController, str], None]) -> None:
        self._listeners.append(callback)

    def remove_listener(self, callback: Callable[[SessionController, str], None]) -> None:
        if callback in self._listeners:
            self._listeners.remove(callback)

    def _notify(self, kind: str) -> None:
        # A broken listener (e.g. a view whose Qt object is gone) must not break the session or starve the others.
        for callback in list(self._listeners):
            try:
                callback(self, kind)
            except Exception:
                log.exception("session listener failed")
        self.manager._notify(self, kind)

    @property
    def busy(self) -> bool:
        return self.state is SessionState.RUNNING

    @property
    def evicted(self) -> bool:
        return bool(self.eviction_notice)

    @property
    def is_open(self) -> bool:
        return self.state in (SessionState.OPENING, SessionState.READY, SessionState.RUNNING)

    @property
    def title(self) -> str:
        if self.transcript.title:
            return self.transcript.title
        first = next((t for t in self.transcript.turns if t.user_block), None)
        if first is not None and first.user_block is not None:
            lines = first.user_block.text.strip().splitlines()
            text = lines[0] if lines else ""
            if text:
                return text[:60]
        return self.saved_title or "新会话"

    @property
    def model(self) -> str:
        if self.transcript.model:
            return self.transcript.model
        settings = self._settings_or_none()
        return settings.model if settings else ""

    @property
    def usage(self) -> Any:
        """Cumulative ``TokenUsageUpdate`` for this session (``None`` before the first update)."""
        return self.transcript.usage

    def usage_series(self) -> dict[str, Tokens]:
        """What droid has counted for this session, across every resume (empty before anything is known).

        Three sources, each a running total of the same session: the saved one read when the session was opened,
        ``TokenUsageUpdate`` (this session only) and the notifications' ``inclusiveTokenUsage`` (with its
        sub-agents). Their field-wise maximum is the newest, whichever of them has spoken last."""
        best = Tokens.of(self.transcript.usage)
        for other in (self._loaded_usage, self._inclusive_usage):
            if other is not None:
                best = other if best is None else best.peak(other)
        return {} if best is None else {"self": best}

    def _output_so_far(self) -> int | None:
        return own_output(self.usage_series())

    @property
    def working_label(self) -> str:
        if self.state is not SessionState.RUNNING:
            return SESSION_STATE_LABELS[self.state]
        turn = self.transcript.active_turn
        state = self.transcript.working_state
        return label_for_working_state(
            getattr(state, "value", state), stopping=bool(turn is not None and turn.stopping)
        )

    def _settings_or_none(self) -> SessionSettings | None:
        if self._session is None:
            return None
        try:
            return self._session.settings
        except DroidError:
            return None

    def live_settings(self) -> LiveSettings | None:
        settings = self._settings_or_none()
        if settings is None:
            return None
        return LiveSettings(
            model=settings.model,
            reasoning=_enum_value(settings.reasoning_effort),
            autonomy=_enum_value(settings.autonomy),
            mode=_enum_value(settings.mode),
        )

    def _spawn(self, coroutine: Any) -> asyncio.Task[Any]:
        task = asyncio.ensure_future(coroutine)
        self._background.add(task)
        task.add_done_callback(self._background.discard)
        return task

    async def _close_quietly(self, session: Any) -> None:
        if session is not None:
            with contextlib.suppress(Exception):
                await session.close()

    # ------------------------------------------------------------------ lifecycle

    async def open_new(
        self,
        *,
        model: str | None = None,
        reasoning: str | None = None,
        autonomy: str | None = None,
        mode: str | None = None,
    ) -> None:
        """Start a session. What is not given comes from the application's defaults (``""`` lets Droid choose)."""
        settings = self.manager.settings
        model = settings.default_model if model is None else model
        reasoning = settings.reasoning_effort if reasoning is None else reasoning
        autonomy = settings.autonomy if autonomy is None else autonomy
        mode = settings.mode if mode is None else mode
        self.transcript.model = model or None
        self.preface = self.manager.memory.brief_for("droid", self.project) if self.manager.memory is not None else ""
        session = self.manager.factory.create(
            cwd=self.project,
            model=model or None,
            reasoning=ReasoningEffort(reasoning) if reasoning else None,
            config=SessionConfig(autonomy=Autonomy(autonomy), mode=Mode(mode), mcp_servers=self.manager.memory_servers(self)),
            interactions=self._interactions(),
        )
        await self._open(session, resume_id=None)

    async def open_resume(
        self,
        session_id: str,
        *,
        title: str | None = None,
        load_history: bool = True,
        failure_context: str = "",
    ) -> None:
        """Resume a saved session.

        History is loaded first through a one-shot droid process that closes
        itself, then the session is resumed: two processes never hold the same
        session at once. ``load_history=False`` is for reconnects, where the
        transcript already holds everything shown so far. ``failure_context``
        is put in front of the error shown if the resume fails.
        """
        self.saved_title = title or self.saved_title
        self.session_id = session_id
        self.state = SessionState.OPENING
        self._notify("state")
        if load_history:
            try:
                snapshot = await self.manager.factory.load_history(session_id, self.project)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # history is best-effort; the session itself matters
                self.transcript.add_notice(f"历史记录加载失败：{friendly_failure(exc)}")
            else:
                if snapshot.messages:
                    self.transcript.load_history(snapshot.messages)
                if snapshot.title and not self.transcript.title:
                    self.transcript.title = snapshot.title
                self._loaded_usage = Tokens.of(snapshot.usage)
                self.saved_settings = snapshot.settings
                if snapshot.context_used and self.context.used is None:
                    self.context = replace(self.context, used=snapshot.context_used, model=snapshot.settings.model if snapshot.settings else "")
                    self._notify("context")
            self._notify("changed")
        if self._close_requested:
            return
        servers = self.manager.memory_servers(self)
        session = self.manager.factory.resume(session_id, interactions=self._interactions(), **({"mcp_servers": servers} if servers else {}))
        await self._open(session, resume_id=session_id, failure_context=failure_context)

    async def _open(self, session: Any, *, resume_id: str | None, failure_context: str = "") -> None:
        self.state = SessionState.OPENING
        self._opening = asyncio.get_running_loop().create_future()
        self._notify("state")
        try:
            await self._open_session(session, resume_id=resume_id, failure_context=failure_context)
        finally:
            if not self._opening.done():
                self._opening.set_result(None)

    async def _open_session(self, session: Any, *, resume_id: str | None, failure_context: str = "") -> None:
        try:
            if resume_id is not None and self.manager.factory.needs_cwd_for_resume:
                # Session.resume() inherits the process cwd; serialize the chdir.
                async with self.manager.cwd_lock:
                    previous = Path.cwd()
                    os.chdir(self.project)
                    try:
                        await session.open()
                    finally:
                        os.chdir(previous)
            else:
                await session.open()
        except asyncio.CancelledError:
            self._spawn(self._close_quietly(session))
            raise
        except Exception as exc:
            await self._close_quietly(session)
            if self._close_requested:
                return
            self.state = SessionState.DEAD if resume_id else SessionState.CLOSED
            self.last_error = failure_context + friendly_failure(exc)
            self.transcript.add_error(self.last_error, recoverable=resume_id is not None)
            self._notify("state")
            return
        if self._close_requested:
            # close() ran while the process was still starting: nobody owns it now.
            await self._close_quietly(session)
            return
        self._session = session
        self.session_id = session.id
        settings = self._settings_or_none()
        if settings is not None:
            self.transcript.model = settings.model
        self._compaction_set = None  # (a new process: nothing has been given to this session yet)
        await self._sync_compaction()
        self._watch_context(session)
        self.state = SessionState.READY
        self._notify("state")

    def _abandon(self, error: BaseException) -> None:
        """Opening raised before a session existed (e.g. droid.exe is missing): there is nothing to close."""
        self._close_requested = True
        if not isinstance(error, asyncio.CancelledError):
            self.last_error = friendly_failure(error)
        self.state = SessionState.CLOSED
        self._expire_pending()
        self._notify("state")
        self.manager._forget(self)

    async def close(self) -> None:
        if self._close_task is None:
            self._close_requested = True
            self._close_task = asyncio.ensure_future(self._do_close())
        await asyncio.shield(self._close_task)

    async def _do_close(self) -> None:
        opening = self._opening
        if opening is not None and not opening.done():
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(asyncio.shield(opening), OPEN_CLOSE_WAIT_SECONDS)
        session, self._session = self._session, None
        self._expire_pending()
        reading, self._context_task = self._context_task, None
        if reading is not None:
            reading.cancel()
        consumer, self._consumer = self._consumer, None
        if consumer is not None:
            consumer.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await consumer
        await self._close_quietly(session)
        if self._background:
            await asyncio.gather(*list(self._background), return_exceptions=True)
        self.state = SessionState.CLOSED
        self._notify("state")
        self.manager._forget(self)

    async def reconnect(self) -> None:
        """Resume the same session id after the droid process died."""
        if self.state is not SessionState.DEAD or not self.session_id or self._close_requested:
            return
        self.transcript.resolve_recoverable_errors()
        self.transcript.add_notice("正在重新连接…")
        self._notify("changed")
        if self._background:
            await asyncio.gather(*list(self._background), return_exceptions=True)
        await self.open_resume(self.session_id, title=self.saved_title, load_history=False)
        if self.state is SessionState.READY:
            self.transcript.add_notice("已重新连接，可以继续对话。")
            self._notify("changed")

    # ------------------------------------------------------------------ turns

    def attachment_problem(self, attachments: Sequence[Attachment]) -> str | None:
        """Why these attachments cannot be sent in this session right now (``None``: nothing known against it).

        Droid silently swaps pictures for a note when the model cannot see them, which would look like a
        model that ignores the picture, so a model known to lack image input is refused up front.
        """
        if not any(item.kind == IMAGE for item in attachments):
            return None
        info = self.manager.model_info(self.model)
        if info is not None and info.no_image_support:
            return f"当前模型「{info.display_name}」不支持图片输入：请先移除图片，或换一个支持图片的模型。"
        return None

    def send_prompt(self, text: str, attachments: Sequence[Attachment] = ()) -> bool:
        """Start a turn. With attachments the text may be empty (``DEFAULT_PROMPT`` is sent instead)."""
        text = text.strip()
        attachments = tuple(attachments)
        if attachments and not text:
            text = DEFAULT_PROMPT
        if not text or self.state is not SessionState.READY or self._session is None:
            return False
        self.used_at = self.active_at = time.time()
        self.state = SessionState.RUNNING
        context = self.manager.hooks.prompt(PromptEvent(self.engine, self.project, text, frozenset(self.recalled), first=bool(self.preface)))
        sent, carried, outgoing = text, None, attachments
        handoff = self.handoff if self.handoff is not None and not self.handoff.empty else None
        if handoff is not None:
            # The model gets the earlier conversation (and the attachments the user picked) with the message; the
            # page shows only what was typed and attached now.
            sent = handoff.prefix + text
            carried = (handoff.carried, handoff.truncated) if handoff.carried else None
            outgoing = tuple(handoff.attachments) + attachments
        sent += wrap_context(self.preface, context.text)  # (after the message: see core/context_envelope)
        self.transcript.output_probe = self._output_so_far
        turn = self.transcript.start_turn(text, attachments, handoff=carried)
        if context.notice:
            self.transcript.add_notice(context.notice)
        self._handoff_turn = turn if handoff is not None else None
        self._preface_turn = turn if self.preface else None
        self._recall_turn, self._recall_ids = turn, context.ids
        self._consumer = asyncio.ensure_future(self._consume(sent, outgoing))
        self._consumer.add_done_callback(self._consumer_done)
        self._notify("state")
        self._notify("changed")
        return True

    def start_compaction(self) -> str | None:
        """``/compact``: Droid's own compaction (``Session.compact``: the early part becomes a summary and the conversation goes on
        in the session Droid makes for it). It runs as a turn of its own, which the page shows and the stop button can end. Why it
        cannot start (a sentence for the user), or ``None`` when it has started."""
        if self.state is not SessionState.READY or self._session is None:
            return "会话还没就绪，或这一轮还在运行：结束后再压缩。"
        self.used_at = self.active_at = time.time()
        self.state = SessionState.RUNNING
        self.transcript.output_probe = self._output_so_far
        turn = self.transcript.start_turn("/compact")
        self.transcript.set_working_state("compacting_conversation")
        self._compacting = True
        self._consumer = asyncio.ensure_future(self._run_compaction(turn))
        self._consumer.add_done_callback(self._consumer_done)
        self._notify("state")
        self._notify("changed")
        return None

    async def _run_compaction(self, turn: Turn) -> None:
        session = self._session
        try:
            outcome = await session.compact()
            # The session Droid made takes over the connection; what was subscribed on the old one went with it.
            self._session = outcome.session
            self.session_id = outcome.session.id
            self._compaction_set = None
            await self._sync_compaction()
            self._watch_context(outcome.session)
            self._publish_context(replace(self.context, used=None, compacted=True))
            self.transcript.add_notice(
                f"Droid 压缩了上下文：较早的 {outcome.removed_count} 条消息换成了摘要；上面的完整记录还在。"
            )
            self.transcript.finish_turn(turn, TurnStatus.DONE)
        except asyncio.CancelledError:
            self.transcript.abort_turn("压缩已停止，对话没有改动。")
            raise
        except CONNECTION_LOST as exc:
            self._die(exc)
        except DroidError as exc:
            self.transcript.fail_turn("没有压缩，对话没有改动：" + friendly_failure(exc))
        except Exception as exc:  # UI must survive anything the SDK throws
            self.transcript.fail_turn(f"出现内部错误：{type(exc).__name__}: {exc}")
        finally:
            self._compacting = False
            self._settle_expired()
            self._notify("state")
            self._notify("changed")
            self.refresh_context()

    async def steer(self, text: str, attachments: Sequence[Attachment] = ()) -> SteerOutcome:
        """Say something to the turn that is running, now.

        Droid takes a message that arrives during a turn into that same turn, at the next step it takes (measured:
        after the tool call that was running, or after the answer being written, never as a turn of its own), and
        the model reads it from there on. It is shown where it was said, waiting, until droid echoes it.
        """
        text = text.strip()
        attachments = tuple(attachments)
        if attachments and not text:
            text = DEFAULT_PROMPT
        session, turn = self._session, self.transcript.active_turn
        if self.state is not SessionState.RUNNING or session is None or turn is None:
            return SteerOutcome(False, over=True)
        if not text:
            return SteerOutcome(False, message="没有内容")
        if turn.stopping:
            return SteerOutcome(False, message="本轮正在停止")
        message_id = str(uuid.uuid4())
        block = self.transcript.add_steer(turn, text, attachments, message_id)
        self._steers[message_id] = block
        self._notify("changed")
        try:
            client = session._require_client()  # the high-level Session refuses a second message during a turn
            await client.add_user_message(text=text, message_id=message_id, **steer_options(attachments))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._steers.pop(message_id, None)
            self.transcript.remove_steer(turn, block)
            self._notify("changed")
            if isinstance(exc, CONNECTION_LOST):
                return SteerOutcome(False, message=friendly_failure(exc))
            return SteerOutcome(False, message=f"Droid 没有收下：{friendly_failure(exc)}")
        return SteerOutcome(True)

    def take_unadopted(self) -> list[tuple[str, tuple[Attachment, ...]]]:
        """What was said to a turn that droid did not take in (the turn was stopped, or ended first), once."""
        items, self._unadopted = self._unadopted, []
        return items

    def _take_inclusive_usage(self, note: Mapping[str, Any]) -> None:
        """The notification's totals: ``inclusiveTokenUsage`` counts the sub-agents' use too, which ``TokenUsageUpdate`` leaves out."""
        if note.get("sessionId") not in (None, self.session_id):
            return
        seen = Tokens.of(note.get("inclusiveTokenUsage")) or Tokens.of(note.get("tokenUsage"))
        if seen is None:
            return
        before = self._inclusive_usage
        self._inclusive_usage = seen if before is None else before.peak(seen)
        if self._inclusive_usage != before:
            self._notify("changed")

    def _steer_orphaned(self, message_id: str) -> None:
        """The echo of a steer came when no turn was running: droid took it as a turn of its own, which nobody follows.
        That turn is stopped and the words go back to the user (they are in droid's record of the session, though)."""
        block = self._steers.get(message_id)
        if block is None or not block.pending or self.state is SessionState.RUNNING:
            return
        turn = self.transcript.turns[-1] if self.transcript.turns else None
        self._drop_steer(block, turn)
        session = self._session
        if session is not None:
            self._spawn(self._interrupt_quietly(session))
        self._notify("steer")

    async def _interrupt_quietly(self, session: Any) -> None:
        with contextlib.suppress(Exception):
            await session.interrupt()

    def _drop_steer(self, block: UserBlock, turn: Turn | None) -> None:
        for message_id, known in list(self._steers.items()):
            if known is block:
                del self._steers[message_id]
        if turn is not None:
            self.transcript.remove_steer(turn, block)
        self._unadopted.append((block.text, block.attachments))

    def _steer_discarded(self, text: str) -> None:
        """Droid threw away a message it had queued (the turn was stopped before it got to it)."""
        turn = self.transcript.active_turn or (self.transcript.turns[-1] if self.transcript.turns else None)
        block = next((b for b in self.transcript.pending_steers(turn) if b.text.strip() == text.strip()), None)
        if block is not None:
            self._drop_steer(block, turn)
            self._notify("steer")

    async def _settle_steers(self, turn: Turn) -> None:
        """A turn ended with steers droid never echoed. Droid may still start a turn of its own for one that arrived in
        the instant the turn ended; that shows up as the echo (``_on_notification``) within a moment. What is still
        not echoed after it goes back to the user."""
        await asyncio.sleep(STEER_SETTLE_SECONDS)
        for block in self.transcript.pending_steers(turn):
            self._drop_steer(block, turn)
        if self._unadopted:
            self._notify("steer")

    async def interrupt(self) -> None:
        session = self._session
        if self.state is not SessionState.RUNNING or session is None:
            return
        consumer = self._consumer
        if self._compacting:
            # (a compaction is no turn of the session: there is nothing to interrupt on Droid's side; the SDK puts the
            # session back as it was when the operation is cancelled)
            self.transcript.mark_stopping()
            self._notify("changed")
            if consumer is not None and not consumer.done():
                consumer.cancel()
                await asyncio.wait({consumer}, timeout=INTERRUPT_GRACE_SECONDS)
            return
        self.transcript.mark_stopping()
        self._notify("changed")
        loop = asyncio.get_running_loop()
        deadline = loop.time() + INTERRUPT_GRACE_SECONDS
        try:
            await asyncio.wait_for(session.interrupt(), INTERRUPT_GRACE_SECONDS)
        except CONNECTION_LOST as exc:
            self._die(exc)
            return
        except (asyncio.TimeoutError, RunTimeoutError):
            await self._restart_connection(session)  # droid did not even answer the stop request
            return
        except DroidError as exc:
            self.transcript.clear_stopping()
            self.transcript.add_notice(f"停止失败：{exc}")
            self._notify("changed")
            return
        if consumer is None or consumer.done():
            return
        # Not wait_for(shield(consumer)): the consumer is cancelled by close() and restarts, and that
        # must not surface here as this task being cancelled.
        done, _ = await asyncio.wait({consumer}, timeout=max(0.0, deadline - loop.time()))
        if not done:
            await self._restart_connection(session)

    async def _restart_connection(self, stuck: Any) -> None:
        """Replace the droid process of a turn that did not stop on request, keeping the session.

        Cancelling just the turn task would leave the SDK believing the session is idle while droid may
        still be running the turn, and the next prompt would go to a busy process. So the process is shut
        down and the same session id is resumed in a fresh one; only then is the session READY again.
        """
        if self.state is not SessionState.RUNNING or self._session is not stuck:
            return  # the turn ended, the process died or the session was closed while we waited
        log.warning("droid did not stop within %.0f s; replacing its process (session %s)", INTERRUPT_GRACE_SECONDS, self.session_id)
        # Not READY at any point from here on: the consumer's done-callback only flips RUNNING to READY.
        self.state = SessionState.OPENING
        self._session = None
        consumer, self._consumer = self._consumer, None
        self._notify("state")
        # Own task so that a cancelled caller (e.g. the window closing) cannot leave the session half restarted.
        await asyncio.shield(self._spawn(self._replace_process(stuck, consumer)))

    async def _replace_process(self, stuck: Any, consumer: asyncio.Task[None] | None) -> None:
        try:
            await self._replace_process_steps(stuck, consumer)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception("restarting the droid connection failed")
            if not self._close_requested:
                self._give_up(f"停止超时，重启会话连接失败：{friendly_failure(exc)}")

    async def _replace_process_steps(self, stuck: Any, consumer: asyncio.Task[None] | None) -> None:
        if consumer is not None:
            consumer.cancel()
            await asyncio.wait({consumer}, timeout=RESTART_CLOSE_SECONDS)
        self._expire_pending()
        closing = self._spawn(self._close_quietly(stuck))
        await asyncio.wait({closing}, timeout=RESTART_CLOSE_SECONDS)
        if self._close_requested:
            return  # close() is taking everything down; nothing to resume
        if not closing.done():
            # The old process may still be alive and still holds the session: resuming now could
            # run two droids on it. reconnect() waits for this shutdown before it resumes.
            self._give_up("停止超时，旧的 Droid 连接没有及时关闭，暂时无法重启会话连接。")
            return
        if not self.session_id:
            self._give_up("停止超时，并且找不到会话编号，无法重启会话连接。")
            return
        self.transcript.resolve_recoverable_errors()  # e.g. a crash that raced with the restart: it is moot now
        await self.open_resume(
            self.session_id,
            title=self.saved_title,
            load_history=False,
            failure_context="停止超时，重启会话连接失败：",
        )
        if self.state is SessionState.READY:
            self.transcript.add_notice("停止超时，已重启会话连接")
            self._notify("changed")

    def _give_up(self, message: str) -> None:
        self.state = SessionState.DEAD
        self.last_error = message
        self.transcript.add_error(message, recoverable=True)
        self._notify("state")
        self._notify("changed")

    async def update_settings(
        self,
        *,
        model: str | None = None,
        reasoning: ReasoningEffort | None = None,
        autonomy: Autonomy | None = None,
        mode: Mode | None = None,
    ) -> bool:
        if self._session is None or self.state not in (SessionState.READY, SessionState.RUNNING):
            return False
        try:
            await self._session.update_settings(
                model=model, reasoning_effort=reasoning, autonomy=autonomy, mode=mode
            )
        except DroidError as exc:
            self.transcript.add_notice(f"设置更新失败：{exc}")
            self._notify("changed")
            return False
        settings = self._settings_or_none()
        if model and settings is not None:
            self.transcript.model = settings.model
            await self._sync_compaction()
        self._notify("changed")
        self._notify("settings")
        self.refresh_context()  # the window follows the model
        return True

    async def _sync_compaction(self) -> None:
        """Make the session compact where the user's window for this model says, and show that window (``core/droid_limits``).
        Droid does not know the window of an API provider's model, and a value given to the session stays on it when the model
        changes: so this runs when the session is opened and whenever its model is changed."""
        session, model = self._session, self.model
        limits = self.manager.limits
        self._user_window = limits.window(model) if model else None
        want = limits.limit(model) if model else None
        if session is None or want is None or want == self._compaction_set:
            return
        if self._compaction_set is None and not model.startswith(CUSTOM):
            return  # (nothing was given to this session: Droid's own rule for a built-in model stands)
        try:
            await session.update_settings(compaction_token_limit=want)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            log.warning("could not set the compaction point of session %s: %s: %s", self.session_id, type(error).__name__, error)
            self._user_window = None  # (droid compacts where it always did: the page must not show a window it will not keep to)
            return
        self._compaction_set = want

    async def _consume(self, prompt: str, attachments: Sequence[Attachment] = ()) -> None:
        session = self._session
        try:
            # Built here, not in send_prompt: a bad attachment fails the turn like any other Droid error.
            options = stream_options(attachments)
            async with session.stream(prompt, include_partial_messages=True, **options) as stream:
                async for event in stream:
                    self.transcript.apply(event)
                    self._settle_expired()
                    self._notify("changed")
            if self.transcript.active_turn is not None:
                # The stream ended without a terminal event the reducer understood.
                self.transcript.abort_turn("本轮意外结束。")
            self._sync_session_id()
        except asyncio.CancelledError:
            self.transcript.abort_turn("本轮已取消。")
            raise
        except CONNECTION_LOST as exc:
            self._die(exc)
        except DroidError as exc:
            self.transcript.fail_turn(friendly_failure(exc))
        except Exception as exc:  # UI must survive anything the SDK throws
            self.transcript.fail_turn(f"出现内部错误：{type(exc).__name__}: {exc}")
        finally:
            self._settle_expired()
            self._notify("changed")
            self.refresh_context()

    def _sync_session_id(self) -> None:
        # Droid can replace the session behind a Session object (e.g. "continue in a new session").
        if self._session is None:
            return
        with contextlib.suppress(Exception):
            current = self._session.id
            if current and current != self.session_id:
                self.session_id = current
                self._notify("state")

    def _consumer_done(self, task: asyncio.Task[None]) -> None:
        if self._consumer is task:
            self._consumer = None
        if not task.cancelled() and task.exception() is not None:
            self.transcript.fail_turn(f"出现内部错误：{task.exception()}")
        if self._handoff_turn is not None and self._handoff_turn.status is TurnStatus.DONE:
            self.handoff = None  # the new engine has the earlier text now; a failed turn keeps it for the retry
            self._handoff_turn = None
        if self._preface_turn is not None and self._preface_turn.status is TurnStatus.DONE:
            self.preface = ""  # (likewise: it is in the conversation now)
            self._preface_turn = None
        if self._recall_turn is not None and self._recall_turn.status is TurnStatus.DONE:
            self.recalled |= self._recall_ids  # (a failed turn gives them again with the retry)
            self._recall_turn, self._recall_ids = None, frozenset()
        self.active_at = time.time()
        if self.state is SessionState.RUNNING:
            self.state = SessionState.READY
        last = self.transcript.turns[-1] if self.transcript.turns else None
        if self.transcript.pending_steers(last) and not self._close_requested:
            self._spawn(self._settle_steers(last))
        self._steers = {key: block for key, block in self._steers.items() if block.pending}
        self._notify("state")
        self._notify("changed")

    def _die(self, exc: BaseException) -> None:
        turn = self.transcript.active_turn
        for block in self.transcript.pending_steers(turn):  # nobody is left to take them in
            self._drop_steer(block, turn)
        if self._unadopted:
            self._notify("steer")
        dead, self._session = self._session, None
        self.state = SessionState.DEAD
        self.last_error = friendly_failure(exc)
        self.transcript.fail_turn(f"{self.last_error} 可以点击「重新连接」恢复会话。", recoverable=True)
        self._expire_pending()
        if dead is not None:
            # Release the broken transport; close() re-raises the crash, which is expected.
            self._spawn(self._close_quietly(dead))
        self._notify("state")

    # ------------------------------------------------------------------ context size

    def _watch_context(self, session: Any) -> None:
        """Read the context size now, and again whenever droid says a model call or a compaction happened."""
        subscribe = getattr(session, "on_notification", None)
        if subscribe is not None:  # the replayer and test doubles have no raw notifications
            try:
                subscribe(lambda note: self._on_notification(session, note))
            except Exception as error:
                log.warning("cannot follow the notifications of session %s: %s", self.session_id, error)
        self.refresh_context()

    def _on_notification(self, session: Any, note: Mapping[str, Any]) -> None:
        # Runs inside the SDK's reader: whatever happens here must not reach it.
        try:
            if session is not self._session:
                return
            kind = note.get("type")
            if kind == "session_compacted":
                self._used_before_compaction = self.context.used
                self._publish_context(replace(self.context, used=None, compacted=True))
                self.refresh_context()
            elif kind == "session_token_usage_changed":
                # Each call comes with its own figure; the breakdown read after it may already show a later one.
                figure = last_call_figure(note.get("lastCallTokenUsage"))
                if figure is not None and note.get("sessionId") in (None, self.session_id):
                    self._set_context(replace(self.context, used=figure))
                self._take_inclusive_usage(note)
                self.refresh_context()
            elif kind == "queued_messages_discarded":
                self._steer_discarded(str(note.get("text") or ""))
            elif kind == "create_message":
                message = note.get("message")
                if isinstance(message, Mapping) and message.get("role") == "user" and message.get("id") in self._steers:
                    self._steer_orphaned(str(message["id"]))
        except Exception:
            log.exception("handling a droid notification failed")

    def refresh_context(self) -> None:
        """Read how full the context is, in the background (calls pile up into at most one more reading)."""
        if self._session is None:
            return
        if self._context_task is not None and not self._context_task.done():
            self._context_dirty = True
            return
        self._context_task = asyncio.ensure_future(self._follow_context())

    async def _follow_context(self) -> None:
        while True:
            session = self._session
            if session is None:
                return
            self._context_dirty = False
            try:
                reading = await read_context(session)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                log.debug("could not read the context size: %s: %s", type(error).__name__, error)
                reading = replace(self.context, used=None)  # unknown, rather than a figure that may be out of date
            if session is not self._session:
                continue  # the process was replaced meanwhile: read the new one
            self._set_context(reading)
            if not self._context_dirty:
                return

    def _set_context(self, reading: ContextState) -> None:
        current = self.context
        if self._user_window:
            reading = replace(reading, window=self._user_window)
        fixed = reading.fixed if reading.fixed is not None else current.fixed
        used = reading.used
        if used is not None and not plausible(used, fixed):
            used = current.used  # a side call such as the session title: the conversation is as big as before
        if current.compacted and used is not None and used == self._used_before_compaction:
            used = None  # droid still reports the figure from before the compaction
        self._publish_context(replace(reading, used=used, compacted=current.compacted and used is None, fixed=fixed))

    def _publish_context(self, state: ContextState) -> None:
        if state != self.context:
            self.context = state
            self._notify("context")

    # ------------------------------------------------------------------ prompts

    def _interactions(self) -> InteractionHandlers:
        return InteractionHandlers(on_permission=self._on_permission, on_question=self._on_question)

    async def _on_permission(self, request: PermissionRequest) -> PermissionResponse:
        prompt = self.transcript.add_permission(request)
        self._notify("changed")
        if prompt is None or prompt.status is not PromptStatus.PENDING:
            return CancelPermission
        try:
            return await self._wait(prompt.id, request)
        except asyncio.CancelledError:
            self._expire_prompt(prompt.id)
            raise

    async def _on_question(self, request: QuestionRequest) -> QuestionResponse:
        prompt = self.transcript.add_question(request)
        self._notify("changed")
        if prompt is None or prompt.status is not PromptStatus.PENDING:
            return CancelQuestion
        try:
            return await self._wait(prompt.id, request)
        except asyncio.CancelledError:
            self._expire_prompt(prompt.id)
            raise

    async def _wait(self, prompt_id: str, request: Any) -> Any:
        loop = asyncio.get_running_loop()
        future: asyncio.Future[Any] = loop.create_future()
        self._pending[prompt_id] = (request, future)
        try:
            return await future
        finally:
            self._pending.pop(prompt_id, None)
            self._notify("changed")

    def answer_permission(self, prompt_id: str, value: str) -> bool:
        """Answer a pending permission prompt; ``False`` when it is no longer open (late click)."""
        pending = self._pending.get(prompt_id)
        if pending is None or not self.transcript.answer_permission(prompt_id, value):
            return False
        request, future = pending
        try:
            response = request.respond(ToolConfirmationOutcome(value))
        except ValueError:
            response = CancelPermission
        if not future.done():
            future.set_result(response)
        return True

    def answer_question(self, prompt_id: str, answers: QuestionAnswers | None) -> bool:
        """Answer (or, with ``None``, skip) a pending AskUser prompt.

        Each answer is a string, or a list of strings for multi-select questions.
        """
        pending = self._pending.get(prompt_id)
        if pending is None:
            return False
        texts = None if answers is None else [_answer_text(answer) for answer in answers]
        if not self.transcript.answer_question(prompt_id, texts):
            return False
        request, future = pending
        if answers is None:
            response = request.cancel()
        else:
            try:
                response = request.submit(
                    [
                        question.answer(answer) if isinstance(answer, str) else question.answer_multiple(answer)
                        for question, answer in zip(request.questions, answers)
                    ]
                )
            except ValueError:
                response = request.cancel()
        if not future.done():
            future.set_result(response)
        return True

    def _expire_prompt(self, prompt_id: str) -> None:
        self.transcript.expire_prompt(prompt_id)
        pending = self._pending.pop(prompt_id, None)
        if pending is not None:
            request, future = pending
            # A normal cancel answer, not an exception: the handler returns it to the SDK, which
            # forwards it to droid (or drops it when the connection is already gone).
            if not future.done():
                future.set_result(_cancel_response(request))

    def _expire_pending(self) -> None:
        for prompt_id in list(self._pending):
            self._expire_prompt(prompt_id)
        self.transcript.expire_prompts()

    def _settle_expired(self) -> None:
        # The turn ended or the process died while a prompt was open: release
        # the waiting handler with a cancellation instead of leaking it.
        for prompt_id, (request, future) in list(self._pending.items()):
            prompt = self.transcript.prompt(prompt_id)
            if prompt is None or prompt.status is not PromptStatus.PENDING:
                self._pending.pop(prompt_id, None)
                if not future.done():
                    future.set_result(_cancel_response(request))


def friendly_message(message: str) -> str:
    text = message.strip()
    return text or "未知错误"


_ATTACHMENT_REASONS = (
    ("PDF attachment exceeds", "PDF 超过 3 MB 的上限"),
    ("exceeds the", "超过 5 MB 的上限"),
    ("valid base64", "数据已损坏"),
    ("not a valid PDF", "不是有效的 PDF"),
    ("image", "图片数据无效，或格式不受支持"),
)


def attachment_failure(message: str) -> str:
    lowered = message.lower()
    reason = next((text for needle, text in _ATTACHMENT_REASONS if needle.lower() in lowered), friendly_message(message))
    return f"附件无法发送：{reason}。"


def friendly_failure(exc: BaseException) -> str:
    if isinstance(exc, DroidNotFoundError):
        return str(exc)  # already a complete sentence telling the user what to do
    if isinstance(exc, DroidProcessError):
        code = f"（exit code {exc.exit_code}）" if exc.exit_code is not None else ""
        return f"Droid 进程异常退出{code}。"
    if isinstance(exc, DroidConnectionError):
        return f"与 Droid 的连接已断开：{friendly_message(exc.message)}。"
    if isinstance(exc, (SessionClosedError, SessionNotOpenError)):
        return "会话连接已关闭。"
    if isinstance(exc, InvalidAttachmentError):
        return attachment_failure(exc.message)
    if isinstance(exc, DroidError):
        return f"{type(exc).__name__}: {friendly_message(exc.message)}"
    return f"{type(exc).__name__}: {exc}"


class SessionManager:
    """Owns every open session, the spawn lock and the open-session limit."""

    def __init__(self, settings: AppSettings, factory: SessionFactory | None = None) -> None:
        self.settings = settings
        if factory is None:
            factory = DroidFactory(lambda: locate_droid(self.settings.droid_path or None).path)
        self.factory = factory
        self.memory: MemoryLaunch | None = None
        """The shared memory, when there is one: every session opened is given its MCP server (see ``core/memory``)."""
        self.hooks = Hooks()
        """The application's hooks (``core/hooks``): consulted before each message of every session."""
        self.limits = DroidLimits()
        """The windows and compaction points of the API providers' models (``core/droid_limits``); inert until the application wires the providers in."""
        self.cwd_lock = asyncio.Lock()
        self.controllers: list[SessionController] = []
        self._listeners: list[Callable[[SessionController, str], None]] = []
        self._models: list[ModelInfo] | None = None
        self._models_lock = asyncio.Lock()
        self._register_lock = asyncio.Lock()

    def add_listener(self, callback: Callable[[SessionController, str], None]) -> None:
        self._listeners.append(callback)

    def remove_listener(self, callback: Callable[[SessionController, str], None]) -> None:
        if callback in self._listeners:
            self._listeners.remove(callback)

    def _notify(self, controller: SessionController, kind: str) -> None:
        for callback in list(self._listeners):
            try:
                callback(controller, kind)
            except Exception:
                log.exception("manager listener failed")

    def _forget(self, controller: SessionController) -> None:
        if controller in self.controllers:
            self.controllers.remove(controller)

    def memory_servers(self, controller: SessionController) -> tuple[StdioMcpServerConfig, ...]:
        """The shared memory's MCP server for a session of this engine (none when the memory is off)."""
        command = self.memory.server_for("droid", controller.project, controller.run) if self.memory is not None else None
        if command is None:
            return ()
        return (StdioMcpServerConfig(name=command.name, command=command.command, args=command.args, env=command.env),)

    def find(self, session_id: str) -> SessionController | None:
        return next((c for c in self.controllers if c.session_id == session_id and not c._close_requested), None)

    def open_controllers(self) -> list[SessionController]:
        return [c for c in self.controllers if c.is_open]

    async def open_new(
        self,
        project: str | Path,
        *,
        model: str | None = None,
        reasoning: str | None = None,
        autonomy: str | None = None,
        mode: str | None = None,
        transcript: Transcript | None = None,
        handoff: Handoff | None = None,
        replacing: bool = False,
    ) -> SessionController:
        """Start a session in ``project``. ``transcript`` / ``handoff`` continue a conversation from another engine.

        ``replacing``: this session takes the place of another one in the same tab (an engine change), so it is not a
        further open session: it neither waits for a slot nor closes another conversation to get one.
        """
        controller = SessionController(manager=self, project=Path(normalize_folder(project)), handoff=handoff)
        if transcript is not None:
            controller.transcript = transcript
        await self._register(controller, exempt=replacing)
        await self._opened(controller, controller.open_new(model=model, reasoning=reasoning, autonomy=autonomy, mode=mode))
        if controller.state is SessionState.CLOSED:
            self._forget(controller)
        return controller

    async def open_saved(
        self, project: str | Path, session_id: str, *, title: str | None = None, modified: float | None = None
    ) -> SessionController:
        """Resume a saved session. ``modified`` (epoch seconds): when it last had a message, kept as its activity time."""
        existing = self.find(session_id)
        if existing is not None:
            return existing
        controller = SessionController(manager=self, project=Path(normalize_folder(project)), session_id=session_id)
        if modified is not None:
            controller.active_at = modified
        holder = await self._register(controller)
        if holder is not controller:
            return holder
        await self._opened(controller, controller.open_resume(session_id, title=title))
        return controller

    async def peek_saved(self, project: str | Path, session_ids: Sequence[str]) -> AsyncIterator[tuple[str, Peek | None]]:
        """Saved sessions read one after the other without resuming any (``None``: that one could not be read)."""
        folder = Path(normalize_folder(project))
        async with contextlib.aclosing(self.factory.load_histories(list(session_ids), folder)) as results:
            async for session_id, outcome in results:
                if isinstance(outcome, BaseException):
                    log.info("could not read the history of %s: %s", session_id, outcome)
                    yield session_id, None
                    continue
                transcript = Transcript()
                if outcome.messages:
                    transcript.load_history(outcome.messages)
                if outcome.title and not transcript.title:
                    transcript.title = outcome.title
                yield session_id, Peek(
                    transcript,
                    title=outcome.title or transcript.title,
                    settings=outcome.settings,
                    usage=Tokens.of(outcome.usage),
                    context_used=outcome.context_used,
                )

    async def _opened(self, controller: SessionController, opening: Any) -> None:
        # An open that raises (or is cancelled) must not keep occupying one of the limited slots.
        try:
            await opening
        except BaseException as error:
            controller._abandon(error)
            raise

    async def _register(self, controller: SessionController, *, exempt: bool = False) -> SessionController:
        """Take one of the limited slots for ``controller`` and return the controller that holds it (``exempt``: it
        replaces a session of the same tab, which holds the slot already: it is only entered in the list).

        Making room means closing an idle session, which takes a while. Registrations are serialized
        so that two opens never both see the same full list, both close the same victim and then both
        register: the new controller is in ``controllers`` (state OPENING, which counts as taken)
        before the next one is looked at. Only the reservation is serialized, not the slow open itself.
        """
        async with self._register_lock:
            if controller.session_id:
                # Another open of the same saved session may have won while this one waited for the lock.
                existing = self.find(controller.session_id)
                if existing is not None:
                    return existing
            if exempt:
                self.controllers.append(controller)
                return controller
            # Every pass either finds a free slot, frees one, or raises; the bound only guards against
            # a close that does not really free its slot.
            for _ in range(len(self.controllers) + 2):
                taken = self.open_controllers()
                if len(taken) < self.settings.max_open_sessions:
                    self.controllers.append(controller)
                    return controller
                closing = next((c for c in taken if c._close_requested), None)
                if closing is not None:
                    await closing.close()  # already on its way out (the user closed it): its slot is coming back
                    continue
                idle = sorted((c for c in taken if c.state is SessionState.READY), key=lambda c: c.used_at)
                if not idle:
                    break
                await self._evict(idle[0])
        raise SessionLimitError(f"最多同时打开 {self.settings.max_open_sessions} 个会话；请先停止或关闭一个。")

    async def _evict(self, victim: SessionController) -> None:
        reopenable = any(turn.user_block is not None for turn in victim.transcript.turns)
        victim.eviction_notice = eviction_message(victim.title, reopenable=reopenable)
        log.info("closing idle session %s to stay within %d open sessions", victim.session_id, self.settings.max_open_sessions)
        victim._notify("evicted")
        await victim.close()

    async def close(self, controller: SessionController) -> None:
        await controller.close()

    async def close_all(self, timeout: float = 20.0) -> None:
        controllers = list(self.controllers)
        if controllers:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*(controller.close() for controller in controllers), return_exceptions=True),
                    timeout,
                )
            except asyncio.TimeoutError:
                pass
        self.controllers.clear()

    async def list_sessions(self, project: str | Path, limit: int | None = SESSION_LIST_LIMIT) -> list[SavedSession]:
        try:
            return await self.factory.list_sessions(Path(normalize_folder(project)), limit)
        except Exception:
            return []

    def model_info(self, model_id: str) -> ModelInfo | None:
        """What the model list (once it has been loaded) says about ``model_id``."""
        return next((model for model in self._models or () if model.id == model_id), None)

    async def list_models(self, project: str | Path, *, refresh: bool = False) -> list[ModelInfo]:
        """Models offered by Droid (cached: spawning droid just to list them is slow)."""
        async with self._models_lock:
            if self._models is None or refresh:
                self._models = list(await self.factory.list_models(Path(normalize_folder(project))))
            return list(self._models)
