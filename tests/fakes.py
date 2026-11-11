"""Scriptable stand-ins for droid-sdk sessions: the tests decide what Droid does and when."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from droid_sdk import (
    Autonomy,
    ContextAccuracy,
    ContextUsage,
    InteractionHandlers,
    Mode,
    ModelInfo,
    ModelProvider,
    ReasoningEffort,
    RunFailure,
    RunInterrupted,
    RunSuccess,
    SavedSession,
    SessionSettings,
)

from desktop.core.session_manager import HistorySnapshot
from tests.support import interrupted

_END = object()
STREAM_OPTIONS = frozenset({"images", "files", "output", "timeout", "include_partial_messages"})
"""The keyword arguments ``droid_sdk.Session.stream`` has."""
CONTEXT_WINDOWS = {"claude-haiku-5-5": 872_000, "kimi-k3": 196_608}
"""Context windows by model (the real droid reports these; other models get the first one)."""


class FakeStream:
    def __init__(self, session: FakeSession, prompt: str, images: Sequence[Any] = (), files: Sequence[Any] = (), options: dict[str, Any] | None = None) -> None:
        self.session = session
        self.prompt = prompt
        self.images = tuple(images)
        self.files = tuple(files)
        self.options = options or {}
        self.queue: asyncio.Queue[Any] = asyncio.Queue()
        self._result: Any = None

    async def __aenter__(self) -> FakeStream:
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.session.log.append("stream-cancelled" if exc_type is asyncio.CancelledError else "stream-exit")
        if self.session.active is self:
            self.session.active = None

    def __aiter__(self) -> FakeStream:
        return self

    async def __anext__(self) -> Any:
        item = await self.queue.get()
        if item is _END:
            raise StopAsyncIteration
        if isinstance(item, BaseException):
            raise item
        if isinstance(item, (RunSuccess, RunInterrupted, RunFailure)):
            self._result = item
        return item

    @property
    def result(self) -> Any:
        return self._result

    def push(self, *events: Any) -> None:
        for event in events:
            self.queue.put_nowait(event)

    def end(self) -> None:
        self.queue.put_nowait(_END)


class FakeClient:
    """The one request of the low-level ``DroidClient`` the context reading uses."""

    def __init__(self, session: FakeSession) -> None:
        self.session = session

    async def add_user_message(self, **message: Any) -> None:
        """What a message to a running turn is sent with (the high-level ``stream`` refuses a second one)."""
        session = self.session
        if session.add_message_error is not None:
            raise session.add_message_error
        session.user_messages.append(message)

    async def get_context_breakdown(self) -> Any:
        session = self.session
        if session.breakdown_error is not None:
            raise session.breakdown_error
        return SimpleNamespace(
            model_id=session.settings.model,
            context_budget=session.window() if session.is_custom() else min(session.session_limit or session.compaction_limit, session.window()),
            last_call_compaction_tokens=session.last_call_tokens,
            used_tokens=10_057,
        )


class FakeSession:
    def __init__(
        self,
        interactions: InteractionHandlers,
        *,
        session_id: str,
        model: str = "claude-haiku-5-5",
        open_delay: float = 0.0,
        fail_open: BaseException | None = None,
        interrupt_ends_turn: bool = True,
        last_call_tokens: int | None = None,
    ) -> None:
        self.interactions = interactions
        self._id = session_id
        self.open_delay = open_delay
        self.fail_open = fail_open
        self.interrupt_ends_turn = interrupt_ends_turn
        self.last_call_tokens = last_call_tokens
        """What droid has recorded for the last model call of the conversation (``None``: there was none)."""
        self.compaction_limit = 250_000
        self.session_limit: int | None = None
        """What the session was given with ``update_settings(compaction_token_limit=...)`` (it stays on the session)."""
        self.context_error: BaseException | None = None
        self.breakdown_error: BaseException | None = None
        self.context_delay = 0.0
        self.context_reads = 0
        self.handlers: list[tuple[Callable[[Any], None], str | None]] = []
        self.log: list[str] = []
        self.active: FakeStream | None = None
        self.streams: list[FakeStream] = []
        self.interrupts = 0
        self.user_messages: list[dict[str, Any]] = []
        self.add_message_error: BaseException | None = None
        self.closed = 0
        self.close_error: BaseException | None = None
        self.close_delay = 0.0
        self.cwd_at_open: Path | None = None
        self.tracker: dict[str, int] = {"active": 0, "max": 0}
        self.settings_calls: list[dict[str, Any]] = []
        self.compacts = 0
        self.compact_error: BaseException | None = None
        self.compact_delay = 0.0
        self.compact_removed = 4
        self.compact_successor: FakeSession | None = None
        """The session ``compact`` hands back (``None``: this one, as the real Droid does)."""
        self._settings = SessionSettings(
            model=model,
            reasoning_effort=ReasoningEffort.LOW,
            mode=Mode.AUTO,
            autonomy=Autonomy.OFF,
        )

    @property
    def id(self) -> str:
        return self._id

    @property
    def settings(self) -> SessionSettings:
        return self._settings

    async def open(self) -> None:
        self.log.append("open-start")
        self.cwd_at_open = Path.cwd()
        self.tracker["active"] += 1
        self.tracker["max"] = max(self.tracker["max"], self.tracker["active"])
        try:
            if self.open_delay:
                await asyncio.sleep(self.open_delay)
        finally:
            self.tracker["active"] -= 1
        self.log.append("open-end")
        if self.fail_open is not None:
            raise self.fail_open

    async def close(self) -> None:
        self.log.append("close")
        self.closed += 1
        self.handlers.clear()  # like the SDK, which drops a session's subscriptions when it closes
        if self.close_delay:
            await asyncio.sleep(self.close_delay)
        if self.close_error is not None:
            raise self.close_error

    async def interrupt(self) -> None:
        self.interrupts += 1
        self.log.append("interrupt")
        if self.interrupt_ends_turn and self.active is not None:
            self.active.push(interrupted(0.5))
            self.active.end()

    async def compact(self, *, instructions: str | None = None) -> Any:
        """``Session.compact``: the early part becomes a summary; the session that takes over is returned (here the same object unless ``compact_successor`` is set)."""
        self.compacts += 1
        self.log.append("compact")
        if self.compact_delay:
            await asyncio.sleep(self.compact_delay)
        if self.compact_error is not None:
            raise self.compact_error
        return SimpleNamespace(session=self.compact_successor or self, removed_count=self.compact_removed)

    async def update_settings(self, **changes: Any) -> None:
        self.settings_calls.append(changes)
        if changes.get("compaction_token_limit") is not None:
            self.session_limit = changes["compaction_token_limit"]
        current = self._settings
        self._settings = SessionSettings(
            model=changes.get("model") or current.model,
            reasoning_effort=changes.get("reasoning_effort") or current.reasoning_effort,
            mode=changes.get("mode") or current.mode,
            autonomy=changes.get("autonomy") or current.autonomy,
        )

    def stream(self, prompt: str, **options: Any) -> FakeStream:
        unknown = set(options) - STREAM_OPTIONS
        if unknown:
            raise TypeError(f"stream() got unexpected keyword arguments {sorted(unknown)}")
        stream = FakeStream(self, prompt, options.get("images", ()), options.get("files", ()), options)
        self.active = stream
        self.streams.append(stream)
        return stream

    def is_custom(self) -> bool:
        return self._settings.model.startswith("custom:")

    def window(self) -> int:
        """What droid reports: a custom model has no window it knows (250 000, or what the session was given as its compaction limit;
        measured on droid 0.251.0 in ``sandbox/probe_custom_window.py``), a built-in one its own."""
        if self.is_custom():
            return self.session_limit or 250_000
        return CONTEXT_WINDOWS.get(self._settings.model, CONTEXT_WINDOWS["claude-haiku-5-5"])

    async def context(self) -> ContextUsage:
        """``Session.context()``: the window follows the model; ``used`` is droid's static estimate, which nobody should show."""
        self.context_reads += 1
        if self.context_delay:
            await asyncio.sleep(self.context_delay)
        if self.context_error is not None:
            raise self.context_error
        window = self.window()
        return ContextUsage(
            used=10_057, remaining=window - 10_057, limit=window, accuracy=ContextAccuracy.ESTIMATED, updated_at=datetime.now(timezone.utc)
        )

    def _require_client(self) -> FakeClient:
        return FakeClient(self)

    def on_notification(self, callback: Callable[[Any], None], *, type: str | None = None) -> Callable[[], None]:
        entry = (callback, type)
        self.handlers.append(entry)
        return lambda: self.handlers.remove(entry) if entry in self.handlers else None

    def notify(self, note: dict[str, Any]) -> None:
        """Deliver a raw droid notification (the inner dict, as ``Session.on_notification`` does)."""
        for callback, kind in list(self.handlers):
            if kind is None or kind == note.get("type"):
                callback(note)


class FakeFactory:
    needs_cwd_for_resume = True

    def __init__(self) -> None:
        self.sessions: list[FakeSession] = []
        self.calls: list[tuple[str, Any]] = []
        self.history: dict[str, HistorySnapshot] = {}
        self.history_error: BaseException | None = None
        self.resume_servers: list[tuple[Any, ...]] = []
        """The MCP servers each resume was given."""
        self.history_delay = 0.0
        self.histories_delay = 0.0
        """How long each session takes when several are read through one process (the cache's reading ahead)."""
        self.list_delay = 0.0
        """How long listing the models and the saved sessions takes (a real droid.exe: seconds)."""
        self.models: list[ModelInfo] = [
            ModelInfo(
                id="claude-haiku-5-5",
                display_name="Haiku 5.5",
                short_display_name="Haiku 5.5",
                model_provider=ModelProvider.ANTHROPIC,
                supported_reasoning_efforts=(ReasoningEffort.LOW, ReasoningEffort.HIGH),
                default_reasoning_effort=ReasoningEffort.LOW,
            )
        ]
        self.saved: list[SavedSession] = []
        self.next_session: dict[str, Any] = {}
        self.default_options: dict[str, Any] = {}
        self.tracker: dict[str, int] = {"active": 0, "max": 0}
        self._counter = 0

    def _new_session(self, interactions: InteractionHandlers, session_id: str | None) -> FakeSession:
        self._counter += 1
        options = {**self.default_options, **self.next_session}
        self.next_session = {}
        session = FakeSession(interactions, session_id=session_id or f"new-{self._counter}", **options)
        session.tracker = self.tracker
        self.sessions.append(session)
        return session

    def create(self, *, cwd: Path, model: str | None, reasoning: Any, config: Any, interactions: InteractionHandlers) -> FakeSession:
        self.calls.append(("create", {"cwd": cwd, "model": model, "reasoning": reasoning, "config": config}))
        return self._new_session(interactions, None)

    def resume(self, session_id: str, *, interactions: InteractionHandlers, mcp_servers: Any = ()) -> FakeSession:
        self.calls.append(("resume", session_id))
        self.resume_servers.append(tuple(mcp_servers))
        return self._new_session(interactions, session_id)

    async def load_history(self, session_id: str, cwd: Path) -> HistorySnapshot:
        self.calls.append(("load_history", session_id))
        if self.history_delay:
            await asyncio.sleep(self.history_delay)
        if self.history_error is not None:
            raise self.history_error
        return self.history.get(session_id, HistorySnapshot(messages=[], title=None))

    async def load_histories(self, session_ids: Any, cwd: Path) -> Any:
        """Several histories through one fake process (the cache's background reading): ``(id, snapshot or error)``."""
        self.calls.append(("load_histories", list(session_ids)))
        for session_id in session_ids:
            if self.histories_delay:
                await asyncio.sleep(self.histories_delay)
            if self.history_error is not None:
                yield session_id, self.history_error
            else:
                yield session_id, self.history.get(session_id, HistorySnapshot(messages=[], title=None))

    async def list_models(self, cwd: Path) -> list[ModelInfo]:
        self.calls.append(("list_models", cwd))
        if self.list_delay:
            await asyncio.sleep(self.list_delay)
        return list(self.models)

    async def list_sessions(self, cwd: Path, limit: int | None = None) -> list[SavedSession]:
        self.calls.append(("list_sessions", (cwd, limit)))
        if self.list_delay:
            await asyncio.sleep(self.list_delay)
        return list(self.saved)


async def until(predicate: Any, timeout: float = 2.0) -> None:
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while not predicate():
        if loop.time() > end:
            raise AssertionError("condition not reached in time")
        await asyncio.sleep(0.005)